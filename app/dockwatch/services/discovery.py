"""Container port discovery: turn published ports into clickable hotlinks.

Given a monitored endpoint (a Docker-API URL), this service scans the engine's
containers, derives a ``scheme://host:port`` hotlink for every published host
port, and persists those links as :class:`ContainerLink` rows so they can be
labelled, enabled, and promoted into gateway routes.

Host/scheme resolution rules (kept in one place so the list endpoint and the
persisted registry agree):

* **host** — a publish binding with a real interface IP (anything other than
  ``0.0.0.0``/``::``/empty) uses that IP; otherwise the endpoint's host is
  used (e.g. ``tcp://1.2.3.4:2375`` → ``1.2.3.4``, ``unix://...`` → ``localhost``).
* **scheme** — ``https`` when the published port is ``443`` or the container is
  labelled ``trove.link.scheme=https``; otherwise ``http``.
* **label** — container label ``trove.link.name``, falling back to the
  container name / short id.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dockwatch.models.container_link import ContainerLink
from app.dockwatch.models.endpoint import Endpoint
from app.dockwatch.services.docker_service import DockerService

logger = logging.getLogger(__name__)

#: Host bindings that mean "listen on every interface" — use the endpoint host.
_ANY_HOSTS = {"", "0.0.0.0", "::", "::0"}

_trove_link_scheme_label = "trove.link.scheme"
_trove_link_name_label = "trove.link.name"


def derive_endpoint_host(url: str) -> str:
    """Human hostname/IP a remote Docker endpoint targets.

    ``tcp://1.2.3.4:2375`` → ``1.2.3.4``, ``http(s)://host`` → ``host``,
    ``ssh://user@host:22`` → ``host``; socket endpoints have no host, so they
    resolve to ``localhost`` (the Trove host itself).
    """
    if not url:
        return "localhost"
    candidate = url if "://" in url else f"tcp://{url}"
    host = urlsplit(candidate).hostname
    return host or "localhost"


def compute_hotlinks(endpoint_url: str, container: dict[str, Any]) -> list[dict[str, Any]]:
    """Derive clickable hotlinks for a container's published ports.

    ``container`` is a row as produced by ``DockerService.list_containers()``
    (``ports`` uses the ``ContainerPort``/``HostIp``/``HostPort`` shape). Rows
    without a published host port contribute nothing. Returns a list of dicts
    with ``container_id``, ``host``, ``host_port``, ``container_port``,
    ``scheme``, ``url`` and ``label``.
    """
    endpoint_host = derive_endpoint_host(endpoint_url)
    labels = container.get("labels") or {}
    scheme_label = str(labels.get(_trove_link_scheme_label) or "http").lower()
    alias = str(
        labels.get(_trove_link_name_label)
        or container.get("name")
        or container.get("short_id")
        or ""
    )
    container_id = str(container.get("id") or container.get("short_id") or "")
    links: list[dict[str, Any]] = []
    for port in container.get("ports") or []:
        host_port = str(port.get("HostPort") or "").strip()
        if not host_port:
            continue
        host_ip = str(port.get("HostIp") or "").strip()
        host = endpoint_host if host_ip in _ANY_HOSTS else host_ip
        scheme = "https" if host_port == "443" or scheme_label == "https" else "http"
        links.append(
            {
                "container_id": container_id,
                "host": host,
                "host_port": host_port,
                "container_port": str(port.get("ContainerPort") or ""),
                "scheme": scheme,
                "url": f"{scheme}://{host}:{host_port}",
                "label": alias,
            }
        )
    return links


def _link_identity(row: ContainerLink) -> tuple[str, str]:
    return (row.container_id, row.container_port)


async def discover_and_sync(
    db: AsyncSession, service: DockerService, endpoint: Endpoint
) -> dict[str, Any]:
    """Scan one endpoint's containers and upsert published-port links.

    Upsert key is ``(endpoint_id, container_id, container_port)``. Rows from a
    previous scan that were not seen this pass are marked ``stale`` (never
    deleted). Manually-tuned links (``manual=True``) keep their host/scheme.
    Returns a summary dict with ``endpoint_id``, ``endpoint_name``,
    ``discovered`` (new rows), ``total``, ``stale`` and ``links``.
    """
    containers = await service.list_containers(include_stats=False)
    seen: set[tuple[str, str]] = set()
    discovered = 0
    for container in containers:
        state = str(container.get("state") or "unknown")
        for link in compute_hotlinks(endpoint.url, container):
            identity = (link["container_id"], link["container_port"])
            seen.add(identity)
            row = await db.scalar(
                select(ContainerLink).where(
                    ContainerLink.endpoint_id == endpoint.id,
                    ContainerLink.container_id == identity[0],
                    ContainerLink.container_port == identity[1],
                )
            )
            if row is None:
                db.add(
                    ContainerLink(
                        endpoint_id=endpoint.id,
                        container_id=identity[0],
                        short_id=identity[0][:12],
                        container_name=str(container.get("name") or ""),
                        image=str(container.get("image") or ""),
                        state=state,
                        container_port=identity[1],
                        host_port=link["host_port"],
                        host=link["host"],
                        scheme=link["scheme"],
                        url=link["url"],
                        label=link["label"],
                        enabled=True,
                        stale=False,
                    )
                )
                discovered += 1
            else:
                row.container_name = str(container.get("name") or row.container_name)
                row.image = str(container.get("image") or row.image)
                row.state = state
                row.host_port = link["host_port"]
                row.stale = False
                if not row.manual:
                    row.host = link["host"]
                    row.scheme = link["scheme"]
                    row.url = link["url"]
                if not row.label:
                    row.label = link["label"]
    links = list(
        (
            await db.scalars(select(ContainerLink).where(ContainerLink.endpoint_id == endpoint.id))
        ).all()
    )
    for row in links:
        if _link_identity(row) not in seen and not row.stale:
            row.stale = True
    await db.commit()
    for row in links:
        await db.refresh(row)
    return {
        "endpoint_id": endpoint.id,
        "endpoint_name": endpoint.name,
        "discovered": discovered,
        "total": len(links),
        "stale": sum(1 for r in links if r.stale),
        "links": links,
    }

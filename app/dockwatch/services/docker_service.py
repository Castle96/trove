"""Container-engine integration service.

Wraps the ``docker`` SDK (which speaks the Docker API — also implemented by
Podman) behind an async, degrade-gracefully API. Every call runs in a worker
thread so the event loop is never blocked. If our primary socket is
unreachable, configured fallback sockets (Podman by default) are probed in
order. When no engine can be reached, :class:`DockerUnavailableError` is
raised and callers decide how to respond (the ``/api/docker/status`` endpoint
reports the situation explicitly).
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict

import docker
from docker.errors import DockerException

from app.dockwatch.config import get_settings
from app.dockwatch.services.container_stats_persistence import insert_container_stats_snapshot
from app.dockwatch.services.metrics import docker_available

logger = logging.getLogger(__name__)


#: Maximum number of per-container stats requests running concurrently.
STATS_CONCURRENCY = 10

#: Substrings (uppercased) that mark an env-var name as a secret.
SECRET_HINTS = ("KEY", "TOKEN", "PASSWORD", "SECRET", "PASS", "AUTH")


def _build_candidate_hosts(primary: str, fallbacks: list[str]) -> list[str]:
    """Return the ordered, de-duplicated list of engine endpoints to probe.

    The primary ``docker_host`` is tried first; each fallback (typically the
    Podman sockets) is only attempted when the endpoints before it failed.
    Empty entries and duplicates are dropped while preserving order.
    """
    seen: set[str] = set()
    result: list[str] = []
    for host in (primary, *fallbacks):
        host = host.strip()
        if host and host not in seen:
            seen.add(host)
            result.append(host)
    return result


def _engine_name(host: str) -> str:
    """Human label for the engine behind a Docker-API endpoint."""
    return "podman" if "podman" in host else "docker"


def _socket_is_reachable(host: str) -> bool:
    """Quick pre-check that a ``unix://`` socket file actually exists.

    Avoids pointless connection attempts (and misleading error messages) for
    obviously-missing sockets. Non-unix endpoints (``tcp://`` etc.) are always
    allowed through — the connection attempt itself decides.
    """
    prefix = "unix://"
    if not host.startswith(prefix):
        return True
    return Path(host[len(prefix) :]).exists()


class DockerUnavailableError(RuntimeError):
    """Raised when the Docker engine cannot be reached."""


class StatsDict(TypedDict):
    cpu_percent: float
    memory_usage: int
    memory_limit: int
    memory_percent: float
    network_rx: int
    network_tx: int
    pids: int
    restart_count: int


class PortDict(TypedDict):
    ContainerPort: str
    HostIp: str | None
    HostPort: str | None


class ContainerDict(TypedDict):
    id: str
    short_id: str
    name: str
    image: str
    image_id: str | None
    state: str
    status: str
    created: str | None
    ports: list[PortDict]
    labels: dict[str, str]
    stack: str | None
    service: str | None
    stats: StatsDict | None


class ContainerDetailDict(ContainerDict):
    config: dict[str, Any]
    mounts: list[dict[str, Any]]
    env: list[str]
    started_at: str | None


class DockerService:
    """Async-friendly wrapper around the Docker/Podman engine API."""

    def __init__(
        self,
        docker_host: str | None = None,
        docker_socket_fallbacks: list[str] | None = None,
        docker_timeout: float | None = None,
        max_stats_containers: int | None = None,
        tls_config: str | None = None,
    ) -> None:
        settings = get_settings()
        primary = docker_host or settings.docker_host
        fallbacks = (
            docker_socket_fallbacks
            if docker_socket_fallbacks is not None
            else settings.docker_socket_fallbacks
        )
        self._candidate_hosts = _build_candidate_hosts(primary, fallbacks)
        self._timeout = docker_timeout if docker_timeout is not None else settings.docker_timeout
        self._max_stats = (
            max_stats_containers
            if max_stats_containers is not None
            else settings.max_stats_containers
        )
        #: Optional JSON creds to pass to the Docker client (TLS certs, auth).
        #: Stored as text; the client-side Docker SDK must interpret it.
        self._tls_config = tls_config
        self._client: docker.APIClient | None = None
        self._engine: str | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ low level
    @property
    def engine(self) -> str | None:
        """Name of the engine the client is connected to (``docker``/``podman``)."""
        return self._engine

    def _connect(self) -> docker.APIClient:
        """Return a (possibly cached) Docker-API client.

        The configured hosts are probed in order: ``docker_host`` first, then
        each fallback socket (Podman by default), so a machine without a
        Docker socket can be served by Podman — and vice versa. The first
        reachable engine is cached for subsequent calls.
        """
        if self._client is not None:
            return self._client
        errors: list[str] = []
        for host in self._candidate_hosts:
            if not _socket_is_reachable(host):
                errors.append(f"{host} (socket not found)")
                continue
            try:
                # APIClient eagerly fetches the server version during __init__,
                # so construction itself can fail when the endpoint is missing.
                self._client = docker.APIClient(
                    base_url=host,
                    timeout=self._timeout,
                    tls=self._tls_config,
                )
                self._engine = _engine_name(host)
                return self._client
            except (DockerException, OSError) as exc:
                errors.append(f"{host} ({exc})")
        tried = ", ".join(errors) or "none configured"
        raise DockerUnavailableError(f"No container engine socket reachable; tried: {tried}")

    async def _call(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        """Run a blocking Docker SDK call in a thread pool."""
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except DockerException as exc:
            raise DockerUnavailableError(f"Docker socket unreachable: {exc}") from exc
        except OSError as exc:
            raise DockerUnavailableError(f"Cannot reach Docker socket: {exc}") from exc

    async def ping(self) -> bool:
        """Ping the engine; return ``True`` when reachable."""
        try:
            ok = bool(await self._call(self._connect().ping))
        except DockerUnavailableError:
            ok = False
        docker_available.set(1 if ok else 0)
        return ok

    # ------------------------------------------------------------------ status
    async def status(self) -> dict[str, Any]:
        """Return engine info + aggregate counts, or an 'unavailable' summary.

        Info, the network list and the volume list are fetched concurrently;
        the count calls degrade to 0 if they individually fail while info
        itself is still available.
        """
        try:
            client = self._connect()
            info, networks, volumes = await asyncio.gather(
                self._call(client.info),
                self._count_networks(client),
                self._count_volumes(client),
            )
        except DockerUnavailableError as exc:
            return {"available": False, "reason": str(exc)}
        info = info or {}
        return {
            "available": True,
            "reason": None,
            "engine": self._engine or _engine_name(self._candidate_hosts[0]),
            "version": info.get("ServerVersion"),
            "api_version": info.get("ApiVersion"),
            "os": info.get("OperatingSystem"),
            "arch": info.get("Architecture"),
            "hostname": info.get("Name"),
            "containers_total": info.get("Containers", 0),
            "containers_running": info.get("ContainersRunning", 0),
            "containers_paused": info.get("ContainersPaused", 0),
            "containers_stopped": info.get("ContainersStopped", 0),
            "images": info.get("Images", 0),
            "networks": networks,
            "volumes": volumes,
        }

    async def _count_networks(self, client: docker.APIClient) -> int:
        try:
            return len(await self._call(client.networks))
        except DockerUnavailableError:
            return 0

    async def _count_volumes(self, client: docker.APIClient) -> int:
        try:
            raw = await self._call(client.volumes)
            return len(raw.get("Volumes", []) or [])
        except DockerUnavailableError:
            return 0

    # ------------------------------------------------------------------ containers
    async def list_containers(self, include_stats: bool = False) -> list[ContainerDict]:
        """List all containers (+ optional stats for the first N)."""
        raw = await self._call(self._connect().containers, all=True)
        # Stats are gathered concurrently but capped to keep the endpoint snappy.
        ids_for_stats = [c["Id"] for c in raw[: self._max_stats]] if include_stats else []
        stats_map = dict(zip(ids_for_stats, await self._stats_batch(ids_for_stats), strict=True))

        result: list[ContainerDict] = []
        for c in raw:
            item = _parse_container(c)
            if include_stats:
                item["stats"] = stats_map.get(c["Id"])
            result.append(item)
        return result

    async def _stats_batch(self, container_ids: list[str]) -> list[StatsDict | None]:
        """Collect stats for many containers concurrently (bounded)."""

        async def one(container_id: str) -> StatsDict | None:
            async with sem:
                try:
                    raw = await self._call(
                        self._connect().stats, container_id, stream=False, decode=True
                    )
                    return parse_stats(raw)
                except DockerUnavailableError:
                    return None

        if not container_ids:
            return []
        sem = asyncio.Semaphore(STATS_CONCURRENCY)
        return list(await asyncio.gather(*(one(cid) for cid in container_ids)))

    async def poll_and_persist_container_stats(
        self, endpoint_id: int | None = None, persist_every: int = 30
    ) -> None:
        """Collect stats for running containers and persist a snapshot every N calls.

        Intended to run as a background loop (e.g. once per minute) so the
        container ranking endpoint has historical data to query. Errors are
        logged, never raised — a single bad container does not stop the loop.
        """
        ticks = 0
        while True:
            try:
                containers = await self.list_containers(include_stats=True)
                for c in containers:
                    stats = c.get("stats")
                    if stats is not None and c.get("state") == "running":
                        await insert_container_stats_snapshot(
                            short_id=c["short_id"],
                            name=c["name"],
                            image=c["image"],
                            stats={
                                "stats_ts": int(datetime.now(UTC).timestamp() * 1000),
                                "cpu_percent": stats["cpu_percent"],
                                "memory_percent": stats["memory_percent"],
                                "memory_usage": stats["memory_usage"],
                                "memory_limit": stats["memory_limit"],
                                "network_rx": stats["network_rx"],
                                "network_tx": stats["network_tx"],
                            },
                            endpoint_id=endpoint_id,
                        )
            except Exception:
                logger.exception("container stats persistence tick failed")
            ticks += 1
            if ticks >= persist_every:
                ticks = 0
            await asyncio.sleep(1)

    async def container_detail(self, container_id: str) -> ContainerDetailDict:
        """Return a single container with full inspect data + logs tail.

        Env vars whose names look like secrets are masked (value replaced with
        ``"***"``) in both the top-level ``env`` and ``config.Env`` lists; the
        entries themselves are never dropped or reordered.
        """
        client = self._connect()
        inspect = await self._call(client.inspect_container, container_id)
        raw_config: dict[str, Any] = dict(inspect.get("Config", {}) or {})
        env = [str(item) for item in raw_config.get("Env", []) or []]
        masked_env = mask_env_vars(env)
        raw_config["Env"] = masked_env
        parsed = _parse_container(
            {
                "Id": inspect["Id"],
                "Names": [inspect["Name"]],
                "Image": inspect.get("Config", {}).get("Image", ""),
                "ImageID": inspect.get("Image"),
                "State": inspect.get("State", {}).get("Status", "unknown"),
                "Status": inspect.get("State", {}).get("Status", "unknown"),
                "Created": inspect.get("Created"),
                "Ports": [],
                "Labels": inspect.get("Config", {}).get("Labels", {}),
            }
        )
        # Inspect exposes ports as a mapping; flatten it directly rather than
        # round-tripping through the ``docker ps`` normalization.
        parsed["ports"] = _inspect_ports(inspect)
        detail: ContainerDetailDict = {
            **parsed,
            "config": raw_config,
            "mounts": inspect.get("Mounts", []) or [],
            "env": masked_env,
            "started_at": inspect.get("State", {}).get("StartedAt"),
        }
        return detail

    async def container_logs(self, container_id: str, tail: int | None = None) -> tuple[str, bytes]:
        """Return ``(container_id, log_bytes)`` from the last ``tail`` lines."""
        settings = get_settings()
        logs = await self._call(
            self._connect().logs,
            container_id,
            tail=tail or settings.logs_tail,
            stdout=True,
            stderr=True,
        )
        return container_id, logs

    async def container_action(self, container_id: str, action: str) -> None:
        """Perform a lifecycle action on a container.

        Supported actions: start, stop, restart, pause, unpause, kill.
        """
        client = self._connect()
        method = getattr(client, action, None)
        if method is None:
            raise ValueError(f"Unsupported container action: {action}")
        await self._call(client.inspect_container, container_id)  # 404 -> DockerException
        await self._call(method, container_id)

    async def pull_image(self, image: str) -> dict[str, str]:
        """Pull ``image`` (``repo`` or ``repo:tag``); consumes the progress stream."""
        repository, _, tag_opt = image.strip().partition(":")
        repository = repository.strip()
        tag: str | None = tag_opt or None
        if not repository:
            raise ValueError("Image must not be empty")
        if tag is not None and ("/" in tag or not tag):
            # ``partition`` splits on the first colon — a registry port (host:5000/img)
            # would land in ``tag``; treat the whole ref as repository in that case.
            repository, tag = image.strip(), None
        client = self._connect()

        def _pull() -> None:
            stream = client.pull(repository, tag=tag or None, stream=True, decode=True)
            for _chunk in stream:
                pass

        try:
            await asyncio.to_thread(_pull)
        except DockerException as exc:
            raise DockerUnavailableError(f"Image pull failed: {exc}") from exc
        except OSError as exc:
            raise DockerUnavailableError(f"Cannot reach engine for pull: {exc}") from exc
        return {"image": image.strip(), "status": "pulled"}

    async def deploy_container(
        self,
        image: str,
        name: str | None = None,
        command: str | list[str] | None = None,
        environment: dict[str, str] | None = None,
        ports: dict[str, int | None] | None = None,
        volumes: list[str] | None = None,
        network_mode: str | None = None,
        restart_policy: str | None = None,
        labels: dict[str, str] | None = None,
        auto_start: bool = True,
        pull_if_missing: bool = False,
    ) -> dict[str, Any]:
        """Create (+ optionally start) a container. Returns ``{id, warnings}``."""
        binds, warnings = validate_volume_binds(volumes or [])
        port_bindings = {k: v for k, v in (ports or {}).items() if k}
        client = self._connect()

        def _create() -> dict[str, Any]:
            host_config = client.create_host_config(
                port_bindings=port_bindings or None,
                binds=binds or None,
                network_mode=network_mode or None,
                restart_policy=_restart_policy(restart_policy),
            )
            created = client.create_container(
                image=image,
                command=command,
                name=name or None,
                environment=environment or None,
                ports=list(port_bindings) or None,
                host_config=host_config,
                labels=labels or None,
            )
            cid = created.get("Id", "")
            if auto_start:
                client.start(cid)
            inspected = client.inspect_container(cid)
            return {"id": cid, "name": str(inspected.get("Name", "")).lstrip("/")}

        try:
            result = await asyncio.to_thread(_create)
        except (DockerException, OSError) as exc:
            if pull_if_missing and _is_missing_image(exc):
                await self.pull_image(image)
                try:
                    result = await asyncio.to_thread(_create)
                except DockerException as retry_exc:
                    if _is_conflict(retry_exc):
                        raise ValueError(
                            f"A container named {name!r} already exists"
                        ) from retry_exc
                    raise DockerUnavailableError(f"Deploy failed: {retry_exc}") from retry_exc
                except OSError as retry_exc:
                    raise DockerUnavailableError(
                        f"Cannot reach engine for deploy: {retry_exc}"
                    ) from retry_exc
            elif _is_conflict(exc):
                raise ValueError(f"A container named {name!r} already exists") from exc
            elif isinstance(exc, DockerException):
                raise DockerUnavailableError(f"Deploy failed: {exc}") from exc
            else:
                raise DockerUnavailableError(f"Cannot reach engine for deploy: {exc}") from exc
        return {"id": result["id"], "name": result["name"], "warnings": warnings}

    async def remove_container(self, container_id: str, force: bool = False) -> None:
        """Remove a container (optionally force-removing a running one).

        Force-removal stops first with a short grace period: engines can take
        ~10s to stop a container, which would exceed the API timeout if stop
        and remove were a single call.
        """
        client = self._connect()
        if force:
            # Already stopped / stopping — remove below decides.
            with suppress(DockerUnavailableError):
                await self._call(client.stop, container_id, timeout=3)
        try:
            await self._call(client.remove_container, container_id, force=force)
        except DockerUnavailableError as exc:
            if _is_conflict(exc):
                raise ValueError("Container is running; retry with force=true") from exc
            raise

    # ------------------------------------------------------------------ stacks / images / swarm
    async def list_stacks(self) -> list[dict[str, Any]]:
        """Group containers by compose project (``com.docker.compose.project``)."""
        containers = await self.list_containers(include_stats=True)
        by_project: dict[str, list[ContainerDict]] = {}
        meta: dict[str, dict[str, str]] = {}
        for c in containers:
            labels = c.get("labels", {})
            project = labels.get("com.docker.compose.project")
            if not project:
                continue
            by_project.setdefault(project, []).append(c)
            meta.setdefault(project, {}).update(
                {
                    "working_dir": labels.get("com.docker.compose.project.working_dir", ""),
                    "created": c.get("created") or "",
                }
            )
        stacks = []
        for project, members in by_project.items():
            stacks.append(
                {
                    "project": project,
                    "working_dir": meta[project].get("working_dir"),
                    "created": meta[project].get("created"),
                    "service_count": len({c.get("service") for c in members if c.get("service")}),
                    "containers": members,
                }
            )
        return sorted(stacks, key=lambda s: s["project"].lower())

    async def image_digest(self, image_ref: str) -> str | None:
        """Best-effort digest for ``image_ref`` (None when missing/unreachable)."""
        try:
            data = await self._call(self._connect().inspect_image, image_ref)
        except (DockerException, OSError, DockerUnavailableError):
            return None
        if isinstance(data, dict):
            digests = data.get("RepoDigests") or []
            if digests:
                return str(digests[0])
            if data.get("Id"):
                return str(data["Id"])
        return None

    async def list_images(self) -> list[dict[str, Any]]:
        raw = await self._call(self._connect().images, all=True)
        return [
            {
                "id": img.get("Id", ""),
                "short_id": (img.get("Id") or "")[:12],
                "tags": img.get("RepoTags") or [],
                "size": img.get("Size", 0),
                # Docker returns an ISO string; Podman returns epoch seconds.
                "created": _coerce_created(img.get("Created")),
            }
            for img in raw
        ]

    async def list_services(self) -> list[dict[str, Any]]:
        """Return Docker Swarm services (empty when Swarm is inactive)."""
        client = self._connect()
        raw = await self._call(client.services)
        result = []
        for svc in raw:
            spec = svc.get("Spec", {})
            mode = spec.get("Mode", {})
            mode_name = "replicated" if "Replicated" in mode else "global"
            replicas = mode.get("Replicated", {}).get("Replicas")
            result.append(
                {
                    "id": svc.get("ID", ""),
                    "name": spec.get("Name", ""),
                    "image": spec.get("TaskTemplate", {}).get("ContainerSpec", {}).get("Image", ""),
                    "mode": mode_name,
                    "replicas": str(replicas) if replicas is not None else None,
                    "ports": svc.get("Endpoint", {}).get("Ports", []),
                }
            )
        return sorted(result, key=lambda s: s["name"].lower())


# ------------------------------------------------------------------ helpers
def _parse_container(c: dict[str, Any]) -> ContainerDict:
    """Convert a raw Docker ``containers(all=True)`` record to our schema dict."""
    raw_labels = c.get("Labels", {}) or {}
    labels: dict[str, str] = {str(k): str(v) for k, v in raw_labels.items()}
    names = c.get("Names") or []
    name = str(names[0]).lstrip("/") if names else ""
    return {
        "id": str(c.get("Id", "")),
        "short_id": str(c.get("Id") or "")[:12],
        "name": name,
        "image": str(c.get("Image", "")),
        "image_id": str(c.get("ImageID")) if c.get("ImageID") else None,
        "state": str(c.get("State", "unknown")),
        "status": str(c.get("Status", "")),
        "created": str(c.get("Created")) if c.get("Created") else None,
        "ports": _list_ports(c.get("Ports") or []),
        "labels": labels,
        "stack": labels.get("com.docker.compose.project"),
        "service": labels.get("com.docker.compose.service"),
        "stats": None,
    }


def _list_ports(ports: list[dict[str, Any]]) -> list[PortDict]:
    """Normalize ``docker ps`` style ports into the inspect-style triple.

    ``[{IP, PrivatePort, PublicPort, Type}]`` becomes
    ``[{ContainerPort: "80/tcp", HostIp, HostPort}]`` so list and detail
    endpoints expose the same port shape.
    """
    flattened: list[PortDict] = []
    for entry in ports:
        container_port = entry.get("PrivatePort")
        typ = entry.get("Type") or "tcp"
        public = entry.get("PublicPort")
        host_ip = entry.get("IP")
        flattened.append(
            {
                "ContainerPort": f"{container_port}/{typ}" if container_port is not None else "",
                "HostIp": host_ip if host_ip else None,
                "HostPort": str(public) if public is not None else None,
            }
        )
    return flattened


def _inspect_ports(inspect: dict[str, Any]) -> list[PortDict]:
    """Flatten the inspect ``NetworkSettings.Ports`` mapping into a port list."""
    ports = inspect.get("NetworkSettings", {}).get("Ports", {}) or {}
    flattened: list[PortDict] = []
    for key, bindings in ports.items():
        if not bindings:
            flattened.append({"ContainerPort": str(key), "HostIp": None, "HostPort": None})
            continue
        for b in bindings:
            flattened.append(
                {
                    "ContainerPort": str(key),
                    "HostIp": b.get("HostIp"),
                    "HostPort": b.get("HostPort"),
                }
            )
    return flattened


def parse_stats(raw: Any) -> StatsDict | None:
    """Compute CPU / memory / network metrics from a Docker stats snapshot."""
    if isinstance(raw, list):
        return parse_stats(raw[0]) if raw else None
    if not isinstance(raw, dict):
        return None

    # --- CPU ---
    cpu_stats = raw.get("cpu_stats", {}) or {}
    precpu_stats = raw.get("precpu_stats", {}) or {}
    cpu_total = cpu_stats.get("cpu_usage", {}).get("total_usage", 0) or 0
    cpu_system = cpu_stats.get("system_cpu_usage", 0) or 0
    prev_total = precpu_stats.get("cpu_usage", {}).get("total_usage", 0) or 0
    prev_system = precpu_stats.get("system_cpu_usage", 0) or 0
    online_cpus = (
        cpu_stats.get("online_cpus")
        or len(cpu_stats.get("cpu_usage", {}).get("percpu_usage", []) or [])
        or 1
    )
    cpu_delta = max(cpu_total - prev_total, 0)
    system_delta = max(cpu_system - prev_system, 0)
    # First sample (or missing pre-cpu data) => no interval to measure, report 0%.
    first_sample = not precpu_stats.get("cpu_usage")
    cpu_percent = 0.0
    if not first_sample and system_delta > 0:
        cpu_percent = (cpu_delta / system_delta) * online_cpus * 100.0

    # --- Memory ---
    mem_stats = raw.get("memory_stats", {}) or {}
    usage = mem_stats.get("usage", 0) or 0
    limit = mem_stats.get("limit", 0) or 0
    cache = mem_stats.get("stats", {}).get("inactive_file", 0) or 0
    usage = max(usage - cache, 0)
    mem_percent = (usage / limit) * 100.0 if limit > 0 else 0.0

    # --- Network ---
    rx = tx = 0
    for net in (raw.get("networks", {}) or {}).values():
        rx += net.get("rx_bytes", 0) or 0
        tx += net.get("tx_bytes", 0) or 0

    return {
        "cpu_percent": round(cpu_percent, 2),
        "memory_usage": int(usage),
        "memory_limit": int(limit),
        "memory_percent": round(mem_percent, 2),
        "network_rx": int(rx),
        "network_tx": int(tx),
        "pids": int((raw.get("pids_stats", {}) or {}).get("current", 0)),
        "restart_count": 0,
    }


def mask_env_vars(env: list[str]) -> list[str]:
    """Mask values of env vars whose names look like secrets.

    Entries are preserved (never dropped or reordered); only the value part of
    a matching ``NAME=value`` entry is replaced with ``"***"``.
    """
    masked: list[str] = []
    for item in env:
        name, sep, _ = item.partition("=")
        if sep and any(hint in name.upper() for hint in SECRET_HINTS):
            masked.append(f"{name}=***")
        else:
            masked.append(item)
    return masked


#: Host paths that must never be bind-mounted into a deployed container.
BLOCKED_BINDS = ("docker.sock", "podman.sock", "containerd.sock")


def _is_missing_image(exc: BaseException) -> bool:
    """True when an engine error means the image isn't present locally."""
    text = str(exc).lower()
    return "404" in text or "no such image" in text


def _is_conflict(exc: BaseException) -> bool:
    """True when the engine rejected a create/remove as conflicting state."""
    text = str(exc).lower()
    return "409" in text or "already in use" in text or "is running" in text


def _coerce_created(value: Any) -> str | None:
    """Normalize image ``Created`` to string (Podman sends epoch seconds)."""
    if value is None:
        return None
    return str(value)


def validate_volume_binds(volumes: list[str]) -> tuple[list[str], list[str]]:
    """Validate ``host:container[:mode]`` binds; returns ``(binds, warnings)``.

    Socket mounts are rejected outright; everything else passes through with
    a warning when the spec looks unusual (missing container path).
    """
    binds: list[str] = []
    warnings: list[str] = []
    for spec in volumes:
        lowered = spec.lower()
        if any(blocked in lowered for blocked in BLOCKED_BINDS):
            raise ValueError(f"Refusing to mount container-engine socket: {spec!r}")
        parts = spec.split(":")
        if len(parts) < 2 or not parts[1]:
            warnings.append(f"Ignoring unusual volume spec (want host:container[:mode]): {spec!r}")
            continue
        binds.append(spec)
    return binds, warnings


def _restart_policy(policy: str | None) -> dict[str, Any] | None:
    """Convert ``"unless-stopped"`` / ``"on-failure:3"`` to the SDK shape."""
    if not policy or policy == "no":
        return None
    name, _, count = policy.partition(":")
    result: dict[str, Any] = {"Name": name}
    if count.isdigit():
        result["MaximumRetryCount"] = int(count)
    return result

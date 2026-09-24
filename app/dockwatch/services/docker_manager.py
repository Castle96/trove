"""Multi-endpoint Docker manager.

Caches one :class:`DockerService` per endpoint URL and fans out status /
listing calls concurrently with bounded parallelism. A single endpoint
failing never fails the whole fleet — each result carries its own
``available`` flag.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.dockwatch.config import get_settings
from app.dockwatch.models.endpoint import Endpoint
from app.dockwatch.services.docker_service import DockerService

logger = logging.getLogger(__name__)


class DockerEndpointManager:
    """Factory + cache for per-endpoint Docker clients."""

    def __init__(self, default_service: DockerService | None = None) -> None:
        self._default = default_service
        self._services: dict[str, DockerService] = {}
        self._lock = asyncio.Lock()

    @property
    def default(self) -> DockerService:
        """Local engine service (lazy so tests can patch settings)."""
        if self._default is None:
            from app.dockwatch.api.docker import docker_service as local_service

            self._default = local_service
        return self._default

    async def for_url(self, url: str, credentials: str | None = None) -> DockerService:
        """Return a cached service for ``url`` (creating it on first use)."""
        async with self._lock:
            service = self._services.get(url)
            if service is None:
                settings = get_settings()
                service = DockerService(
                    docker_host=url,
                    docker_socket_fallbacks=[],
                    docker_timeout=settings.docker_timeout,
                    max_stats_containers=settings.max_stats_containers,
                    tls_config=credentials,
                )
                self._services[url] = service
            return service

    async def for_endpoint(self, endpoint: Endpoint) -> DockerService:
        """Return the client for a DB endpoint row."""
        return await self.for_url(
            endpoint.url,
            credentials=endpoint.credentials,
        )

    def clear(self) -> None:
        """Drop cached per-endpoint clients (used by tests)."""
        self._services.clear()

    async def fleet_status(
        self, endpoints: list[Endpoint], poll_timeout: float | None = None
    ) -> list[dict[str, Any]]:
        """Poll ``status()`` on local + every enabled endpoint concurrently."""
        settings = get_settings()
        limit = max(settings.fleet_poll_concurrency, 1)
        sem = asyncio.Semaphore(limit)
        timeout = poll_timeout or settings.fleet_poll_timeout

        async def one_local() -> dict[str, Any]:
            async with sem:
                try:
                    status = await asyncio.wait_for(self.default.status(), timeout)
                except Exception as exc:
                    logger.warning("fleet local status failed: %s", exc)
                    return {
                        "endpoint_id": None,
                        "endpoint_name": "local",
                        "available": False,
                        "reason": str(exc),
                    }
                return {"endpoint_id": None, "endpoint_name": "local", **status}

        async def one_remote(endpoint: Endpoint) -> dict[str, Any]:
            async with sem:
                try:
                    service = await self.for_endpoint(endpoint)
                    status = await asyncio.wait_for(service.status(), timeout)
                except Exception as exc:
                    logger.warning("fleet endpoint %s failed: %s", endpoint.name, exc)
                    return {
                        "endpoint_id": endpoint.id,
                        "endpoint_name": endpoint.name,
                        "available": False,
                        "reason": str(exc),
                    }
                return {
                    "endpoint_id": endpoint.id,
                    "endpoint_name": endpoint.name,
                    **status,
                }

        remotes = [e for e in endpoints if e.enabled]
        results = await asyncio.gather(one_local(), *(one_remote(e) for e in remotes))
        # Attach the endpoint ``kind`` so fleet consumers can branch on it.
        by_id = {e.id: getattr(e, "kind", "docker") for e in endpoints if e.id is not None}
        for r in results:
            eid = r.get("endpoint_id")
            r["kind"] = (by_id.get(eid) if isinstance(eid, int) else None) or "docker"
        return list(results)


docker_manager = DockerEndpointManager()

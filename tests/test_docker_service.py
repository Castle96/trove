"""Unit tests for the Docker-API service's graceful-degradation paths."""

from __future__ import annotations

from unittest.mock import AsyncMock

from app.dockwatch.services import docker_service as ds


def test_socket_reachable_unix_socket_exists() -> None:
    assert ds._socket_is_reachable(f"unix://{__file__}")


def test_socket_reachable_missing_socket_is_false() -> None:
    assert ds._socket_is_reachable("unix:///definitely/not/a/socket") is False


def test_socket_reachable_blocked_socket_is_false(monkeypatch) -> None:
    """SELinux can make stat() of docker.sock raise PermissionError (EACCES).

    On Python 3.13 Path.exists() no longer swallows OSError, so the probe must
    handle it itself — otherwise /api/ready 500s and the stats loop spams logs.
    """

    def raise_permission_error(_self) -> bool:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(ds.Path, "exists", raise_permission_error)
    assert ds._socket_is_reachable("unix:///var/run/docker.sock") is False


def test_socket_reachable_tcp_is_always_allowed() -> None:
    assert ds._socket_is_reachable("tcp://127.0.0.1:2375")


def test_ping_handles_stat_permission_error(monkeypatch) -> None:
    """ping() must return False (never raise) when the socket is unreadable."""

    def raise_permission_error(_self) -> bool:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(ds.Path, "exists", raise_permission_error)
    service = ds.DockerService(
        docker_host="unix:///var/run/docker.sock", docker_socket_fallbacks=[]
    )
    import asyncio

    assert asyncio.run(service.ping()) is False


async def test_ready_degrades_when_docker_unavailable(client, monkeypatch) -> None:
    """Readiness stays 200 with docker:"unavailable" instead of 500ing."""
    from app.main import docker_service

    monkeypatch.setattr(docker_service, "ping", AsyncMock(return_value=False))
    resp = await client.get("/api/ready")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ready"] is True
    assert body["checks"]["docker"] == "unavailable"
    assert body["checks"]["database"] == "ok"

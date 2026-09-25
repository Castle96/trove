"""API tests for discovered hotlinks (discover, list, patch, delete, promote).

Requires the ``dw_db`` fixture so the Dockwatch tables exist under a temp
file; gateway routes are created in the real (seeded) Trove temp DB via the
standard ``client`` fixture.
"""

from __future__ import annotations

import asyncio

import pytest_asyncio

from app.dockwatch.services.docker_manager import docker_manager

# ---------------------------------------------------------------------------
# Fake Docker service
# ---------------------------------------------------------------------------


class FakeDocker:
    def __init__(self, containers, available: bool = True):
        self._containers = [dict(c) for c in containers]
        self._available = available

    def set_containers(self, containers) -> None:
        self._containers = [dict(c) for c in containers]

    async def status(self) -> dict:
        if not self._available:
            return {"available": False, "reason": "engine gone"}
        return {
            "available": True,
            "engine": "test",
            "version": "1.0",
            "containers_total": len(self._containers),
            "containers_running": sum(1 for c in self._containers if c.get("state") == "running"),
            "containers_stopped": sum(1 for c in self._containers if c.get("state") != "running"),
        }

    async def list_containers(self, include_stats: bool = False):
        return [dict(c) for c in self._containers]


WEB = {
    "id": "cont1",
    "short_id": "cont1",
    "name": "web",
    "image": "nginx:latest",
    "state": "running",
    "labels": {},
    "ports": [
        {"ContainerPort": "80/tcp", "HostIp": None, "HostPort": "8080"},
        {"ContainerPort": "443/tcp", "HostIp": "10.0.0.9", "HostPort": "443"},
        {"ContainerPort": "53/udp", "HostIp": None, "HostPort": None},
    ],
}
API = {
    "id": "cont2",
    "short_id": "cont2",
    "name": "api",
    "image": "app:1",
    "state": "running",
    "labels": {"trove.link.scheme": "https", "trove.link.name": "API backend"},
    "ports": [{"ContainerPort": "3000/tcp", "HostIp": None, "HostPort": "3000"}],
}


def stub_engine(monkeypatch, service) -> None:
    async def for_endpoint(endpoint):
        return service

    monkeypatch.setattr(docker_manager, "for_endpoint", for_endpoint)


def stub_no_auto(monkeypatch) -> None:
    from app.dockwatch.api import endpoints as ep_mod

    monkeypatch.setattr(ep_mod, "_queue_discovery", lambda endpoint_id: None)


async def make_endpoint(client, name="edge", url="tcp://1.2.3.4:2375", **overrides):
    res = await client.post(
        "/api/endpoints",
        json={"name": name, "url": url, "kind": "docker", "enabled": True, **overrides},
    )
    assert res.status_code == 201, res.text
    return res.json()


# --------------------------------------------------------------------------- discovery
@pytest_asyncio.fixture
async def service():
    return FakeDocker([WEB, API])


async def test_discover_endpoint_creates_hotlinks(client, dw_db, monkeypatch, service) -> None:
    stub_engine(monkeypatch, service)
    stub_no_auto(monkeypatch)
    endpoint = await make_endpoint(client)

    res = await client.post(f"/api/endpoints/{endpoint['id']}/discover")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["endpoint_id"] == endpoint["id"]
    assert body["discovered"] == 3
    assert body["total"] == 3
    assert body["stale"] == 0

    by_port = {lnk["container_port"]: lnk for lnk in body["links"]}
    assert by_port["80/tcp"]["url"] == "http://1.2.3.4:8080"
    assert by_port["80/tcp"]["host"] == "1.2.3.4"
    assert by_port["443/tcp"]["url"] == "https://10.0.0.9:443"
    assert by_port["443/tcp"]["scheme"] == "https"
    assert by_port["3000/tcp"]["scheme"] == "https"  # trove.link.scheme label
    assert by_port["3000/tcp"]["label"] == "API backend"

    listed = (await client.get(f"/api/endpoints/{endpoint['id']}/links")).json()
    assert len(listed) == 3
    all_links = (await client.get("/api/links")).json()
    assert all(lnk["endpoint_name"] == "edge" for lnk in all_links)


async def test_discover_is_idempotent_and_marks_stale(client, dw_db, monkeypatch, service) -> None:
    stub_engine(monkeypatch, service)
    stub_no_auto(monkeypatch)
    endpoint = await make_endpoint(client)

    await client.post(f"/api/endpoints/{endpoint['id']}/discover")

    again = (await client.post(f"/api/endpoints/{endpoint['id']}/discover")).json()
    assert again["discovered"] == 0
    assert again["total"] == 3

    # One container disappears from the next scan -> its ports go stale.
    service.set_containers([API])
    res = await client.post(f"/api/endpoints/{endpoint['id']}/discover")
    body = res.json()
    assert body["stale"] == 2
    assert body["total"] == 3
    stale = [lnk for lnk in body["links"] if lnk["stale"]]
    assert {lnk["container_port"] for lnk in stale} == {"80/tcp", "443/tcp"}

    # include_stale=false hides them; the record itself is never deleted.
    active = (await client.get(f"/api/endpoints/{endpoint['id']}/links?include_stale=false")).json()
    assert len(active) == 1


async def test_link_patch_manual_edits_survive_resync(client, dw_db, monkeypatch, service) -> None:
    stub_engine(monkeypatch, service)
    stub_no_auto(monkeypatch)
    endpoint = await make_endpoint(client)
    await client.post(f"/api/endpoints/{endpoint['id']}/discover")
    link_id = (await client.get("/api/links")).json()[0]["id"]

    patched = (
        await client.patch(
            f"/api/links/{link_id}",
            json={"scheme": "https", "host": "lb.example.net", "label": "prod web"},
        )
    ).json()
    assert patched["manual"] is True
    assert patched["url"] == "https://lb.example.net:8080"
    assert patched["label"] == "prod web"

    # Rescan keeps the hand-tuned host/scheme despite an unchanged endpoint host.
    await client.post(f"/api/endpoints/{endpoint['id']}/discover")
    after = (await client.get(f"/api/links/{link_id}")).json()
    assert after["host"] == "lb.example.net"
    assert after["scheme"] == "https"
    assert after["manual"] is True


async def test_link_patch_disable_and_sort(client, dw_db, monkeypatch, service) -> None:
    stub_engine(monkeypatch, service)
    stub_no_auto(monkeypatch)
    endpoint = await make_endpoint(client)
    await client.post(f"/api/endpoints/{endpoint['id']}/discover")
    first = (await client.get("/api/links", params={"limit": 500})).json()[0]

    res = await client.patch(f"/api/links/{first['id']}", json={"enabled": False, "sort_order": 99})
    assert res.status_code == 200
    assert res.json()["enabled"] is False
    assert res.json()["sort_order"] == 99


async def test_delete_link(client, dw_db, monkeypatch, service) -> None:
    stub_engine(monkeypatch, service)
    stub_no_auto(monkeypatch)
    endpoint = await make_endpoint(client)
    await client.post(f"/api/endpoints/{endpoint['id']}/discover")
    first = (await client.get("/api/links")).json()[0]

    res = await client.delete(f"/api/links/{first['id']}")
    assert res.status_code == 204
    assert len((await client.get("/api/links")).json()) == 2
    assert (await client.get(f"/api/links/{first['id']}")).status_code == 404


# --------------------------------------------------------------------------- mapping to gateway
async def test_map_link_to_gateway_creates_route(client, dw_db, monkeypatch, service) -> None:
    stub_engine(monkeypatch, service)
    stub_no_auto(monkeypatch)
    gid = (
        await client.post("/api/gateway", json={"name": "Apps", "slug": "apps", "enabled": True})
    ).json()["id"]
    endpoint = await make_endpoint(client)
    await client.post(f"/api/endpoints/{endpoint['id']}/discover")
    link = (await client.get("/api/links")).json()[0]

    res = await client.post(
        f"/api/links/{link['id']}/map-to-gateway",
        json={"gateway_id": gid, "path": "/*", "auth_mode": "open", "rate_limit_rpm": 0},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["route_id"] > 0
    assert body["gateway_slug"] == "apps"
    assert body["url"] == link["url"]

    routes = (await client.get(f"/api/gateway/{gid}/routes")).json()
    assert any(r["upstream_url"] == link["url"] and r["id"] == body["route_id"] for r in routes)
    assert any(r["path"] == "/*" and r["auth_mode"] == "open" for r in routes)

    after = (await client.get(f"/api/links/{link['id']}")).json()
    assert after["gateway_route_id"] == body["route_id"]


async def test_map_link_to_gateway_unknown_gateway(client, dw_db, monkeypatch, service) -> None:
    stub_engine(monkeypatch, service)
    stub_no_auto(monkeypatch)
    endpoint = await make_endpoint(client)
    await client.post(f"/api/endpoints/{endpoint['id']}/discover")
    link = (await client.get("/api/links")).json()[0]

    res = await client.post(
        f"/api/links/{link['id']}/map-to-gateway",
        json={"gateway_id": 9999, "path": "/*"},
    )
    assert res.status_code == 404


# --------------------------------------------------------------------------- auto-discovery
async def test_endpoint_test_auto_discovers(client, dw_db, monkeypatch, service) -> None:
    stub_engine(monkeypatch, service)
    stub_no_auto(monkeypatch)
    endpoint = await make_endpoint(client)

    res = await client.post(f"/api/endpoints/{endpoint['id']}/test")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["available"] is True
    assert body["links_discovered"] == 3

    assert len((await client.get(f"/api/endpoints/{endpoint['id']}/links")).json()) == 3


async def test_endpoint_create_runs_background_discovery(
    client, dw_db, monkeypatch, service
) -> None:
    stub_engine(monkeypatch, service)
    # NOTE: _queue_discovery intentionally NOT stubbed — we verify the task.
    await make_endpoint(client, name="auto")

    for _ in range(50):
        links = (await client.get("/api/links")).json()
        if len(links) == 3:
            break
        await asyncio.sleep(0.05)
    assert len(links) == 3, links


async def test_endpoint_test_unavailable_no_discovery(client, dw_db, monkeypatch, service) -> None:
    service._available = False
    stub_engine(monkeypatch, service)
    stub_no_auto(monkeypatch)
    endpoint = await make_endpoint(client)

    res = await client.post(f"/api/endpoints/{endpoint['id']}/test")
    body = res.json()
    assert body["available"] is False
    assert body["links_discovered"] == 0

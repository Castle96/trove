"""API Gateway subsystem tests: CRUD, proxy hot path, auth, rate limits, TLS.

Each test gets a fresh temp DB (see conftest). Proxy tests relay to a local
threaded HTTP echo server started on a random port, so nothing depends on the
live demo upstream on port 9099.
"""

from __future__ import annotations

import http.server
import json
import threading
import time

import pytest_asyncio

# ---------------------------------------------------------------------------
# Local echo upstream (threaded, random port)
# ---------------------------------------------------------------------------


class _EchoHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, payload: bytes, status: int = 200, ctype: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _echo(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        payload = json.dumps(
            {"method": self.command, "path": self.path, "body": body.decode("utf-8", "replace")}
        ).encode()
        self._send(payload)

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/slow"):
            time.sleep(1.0)
            self._send(b'{"slow":true}')
            return
        self._echo()

    def do_POST(self):  # noqa: N802
        self._echo()

    def do_PUT(self):  # noqa: N802
        self._echo()

    def do_PATCH(self):  # noqa: N802
        self._echo()

    def do_DELETE(self):  # noqa: N802
        self._echo()

    def do_OPTIONS(self):  # noqa: N802
        self._send(b"{}")

    def do_HEAD(self):  # noqa: N802
        self._echo()

    def log_message(self, *args) -> None:
        pass


@pytest_asyncio.fixture
async def upstream():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _EchoHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


# ---------------------------------------------------------------------------
# Domain helpers (pure HTTP — the API is administrator-agnostic in open mode)
# ---------------------------------------------------------------------------


async def make_gateway(client, slug="apps", **overrides):
    payload = {"name": slug.title(), "slug": slug, "enabled": True, **overrides}
    res = await client.post("/api/gateway", json=payload)
    assert res.status_code == 201, res.text
    return res.json()


async def make_route(client, gateway_id, path="/v1/*", upstream=None, **overrides):
    payload = {
        "name": path,
        "methods": ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"],
        "path": path,
        "upstream_url": upstream,
        "auth_mode": "open",
        "rate_limit_rpm": 0,
        "timeout_ms": 30_000,
        "strip_prefix": False,
        "enabled": True,
        **overrides,
    }
    res = await client.post(f"/api/gateway/{gateway_id}/routes", json=payload)
    assert res.status_code == 201, res.text
    return res.json()


async def make_consumer(client, gateway_id, name="Shop"):
    res = await client.post(f"/api/gateway/{gateway_id}/consumers", json={"name": name})
    assert res.status_code == 201, res.text
    return res.json()


async def make_key(client, consumer_id, custom: str | None = None):
    payload = {"label": "test"}
    if custom is not None:
        payload["key"] = custom
    res = await client.post(f"/api/gateway/consumers/{consumer_id}/keys", json=payload)
    assert res.status_code == 201, res.text
    return res.json()


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


async def test_gateway_crud(client) -> None:
    created = await make_gateway(client, "crud")
    gid = created["id"]
    assert created["slug"] == "crud"
    assert created["tls_status"] == "none"
    assert created["route_count"] == 0

    # duplicate slug -> 409
    res = await client.post("/api/gateway", json={"name": "again", "slug": "crud"})
    assert res.status_code == 409

    # get + patch
    got = (await client.get(f"/api/gateway/{gid}")).json()
    assert got["name"] == "Crud"
    updated = await client.patch(f"/api/gateway/{gid}", json={"name": "Renamed", "enabled": False})
    assert updated.status_code == 200
    assert updated.json()["name"] == "Renamed"
    assert updated.json()["enabled"] is False

    # list
    listing = (await client.get("/api/gateway")).json()
    assert any(g["id"] == gid and g["name"] == "Renamed" for g in listing)

    # delete
    res = await client.delete(f"/api/gateway/{gid}")
    assert res.status_code == 200
    assert (await client.get("/api/gateway")).json() == []


async def test_slug_pattern_and_validation(client) -> None:
    res = await client.post("/api/gateway", json={"name": "Bad", "slug": "Bad Slug"})
    assert res.status_code == 422
    res = await client.post("/api/gateway", json={"name": "Ok", "slug": "1abc"})
    assert res.status_code == 201


async def test_route_and_consumer_crud(client, upstream) -> None:
    g = await make_gateway(client, "chained")
    r = await make_route(client, g["id"], upstream=upstream)
    c = await make_consumer(client, g["id"])

    routes = (await client.get(f"/api/gateway/{g['id']}/routes")).json()
    assert len(routes) == 1 and routes[0]["id"] == r["id"]

    consumers = (await client.get(f"/api/gateway/{g['id']}/consumers")).json()
    assert len(consumers) == 1
    assert consumers[0]["key_count"] == 0

    # counts on the gateway read
    detail = (await client.get(f"/api/gateway/{g['id']}")).json()
    assert detail["route_count"] == 1
    assert detail["consumer_count"] == 1

    # patch route + consumer
    res = await client.patch(
        f"/api/gateway/routes/{r['id']}", json={"name": "renamed-route", "enabled": False}
    )
    assert res.status_code == 200 and res.json()["enabled"] is False
    res = await client.patch(f"/api/gateway/consumers/{c['id']}", json={"enabled": False})
    assert res.status_code == 200 and res.json()["enabled"] is False

    await client.delete(f"/api/gateway/routes/{r['id']}")
    assert (await client.get(f"/api/gateway/{g['id']}/routes")).json() == []


# ---------------------------------------------------------------------------
# Proxy hot path
# ---------------------------------------------------------------------------


async def test_open_route_proxies_upstream(client, upstream) -> None:
    g = await make_gateway(client, "open1")
    await make_route(client, g["id"], path="/v1/*", upstream=upstream)

    res = await client.get("/gw/open1/v1/echo")
    assert res.status_code == 200
    body = res.json()
    assert body["method"] == "GET"
    assert body["path"] == "/v1/echo"  # no strip_prefix

    # trailing-slash normalization
    res = await client.get("/gw/open1/v1/echo/")
    assert res.status_code == 200
    assert res.json()["path"] == "/v1/echo/"

    # unknown gateway -> 404; no matching route -> 404
    assert (await client.get("/gw/nope/v1/echo")).status_code == 404
    assert (await client.get("/gw/open1/other/thing")).status_code == 404


async def test_strip_prefix_rewrites_path(client, upstream) -> None:
    g = await make_gateway(client, "strip")
    await make_route(client, g["id"], path="/api/*", upstream=upstream, strip_prefix=True)

    res = await client.get("/gw/strip/api/echo/endpoint")
    assert res.status_code == 200
    # /api/* -> upstream receives /echo/endpoint
    assert res.json()["path"] == "/echo/endpoint"


async def test_method_and_body_passthrough(client, upstream) -> None:
    g = await make_gateway(client, "post")
    await make_route(client, g["id"], path="/*", upstream=upstream)

    res = await client.post("/gw/post/items", json={"hello": "world"})
    assert res.status_code == 200
    body = res.json()
    assert body["method"] == "POST"
    assert body["path"] == "/items"
    assert json.loads(body["body"]) == {"hello": "world"}

    # query params must be forwarded
    res = await client.get("/gw/post/search?q=alpha&n=2")
    assert res.json()["path"] == "/search?q=alpha&n=2"


async def test_unreachable_upstream_returns_502(client) -> None:
    g = await make_gateway(client, "dead")
    await make_route(client, g["id"], path="/*", upstream="http://127.0.0.1:1")
    res = await client.get("/gw/dead/anything")
    assert res.status_code == 502
    body = res.json()
    assert "upstream" in body["error"]


async def test_upstream_timeout_returns_504(client, upstream) -> None:
    g = await make_gateway(client, "slow")
    await make_route(
        client, g["id"], path="/slow", upstream=upstream, methods=["GET"], timeout_ms=100
    )
    res = await client.get("/gw/slow/slow")
    assert res.status_code == 504
    assert "timeout" in res.json()["error"]


# ---------------------------------------------------------------------------
# API-key auth + lifecycle
# ---------------------------------------------------------------------------


async def test_secure_route_requires_key(client, upstream) -> None:
    g = await make_gateway(client, "sec1")
    await make_route(client, g["id"], path="/v1/*", upstream=upstream, auth_mode="api_key")
    c = await make_consumer(client, g["id"])
    created = await make_key(client, c["id"])
    key = created["key"]

    # no key / wrong key
    res = await client.get("/gw/sec1/v1/echo")
    assert res.status_code == 401
    res = await client.get("/gw/sec1/v1/echo", headers={"X-API-Key": "wrong-key-123"})
    assert res.status_code == 401

    # valid key (also via Authorization Bearer variant)
    res = await client.get("/gw/sec1/v1/echo", headers={"X-API-Key": key})
    assert res.status_code == 200
    res = await client.get("/gw/sec1/v1/echo", headers={"Authorization": f"Bearer {key}"})
    assert res.status_code == 200

    # last_used_at was stamped
    rows = (await client.get(f"/api/gateway/consumers/{c['id']}/keys")).json()
    assert rows[0]["last_used_at"] is not None


async def test_key_length_minimum(client, upstream) -> None:
    g = await make_gateway(client, "len")
    await make_route(client, g["id"], path="/*", upstream=upstream, auth_mode="api_key")
    c = await make_consumer(client, g["id"])

    # 7 chars -> 422 (schema min 8)
    res = await client.post(f"/api/gateway/consumers/{c['id']}/keys", json={"key": "1234567"})
    assert res.status_code == 422

    # exactly 8 chars -> accepted and usable
    created = await make_key(client, c["id"], custom="12345678")
    res = await client.get("/gw/len/x", headers={"X-API-Key": created["key"]})
    assert res.status_code == 200

    # duplicate key -> 409
    res = await client.post(f"/api/gateway/consumers/{c['id']}/keys", json={"key": "12345678"})
    assert res.status_code == 409


async def test_key_disable_expire_delete_lifecycle(client, upstream) -> None:
    g = await make_gateway(client, "life")
    await make_route(client, g["id"], path="/*", upstream=upstream, auth_mode="api_key")
    c = await make_consumer(client, g["id"])
    created = await make_key(client, c["id"])
    key = created["key"]
    head = {"X-API-Key": key}

    assert (await client.get("/gw/life/x", headers=head)).status_code == 200

    # disable -> 403
    key_id = created["id"]
    res = await client.patch(f"/api/gateway/keys/{key_id}", json={"enabled": False})
    assert res.status_code == 200
    assert (await client.get("/gw/life/x", headers=head)).status_code == 403

    # re-enable -> 200
    await client.patch(f"/api/gateway/keys/{key_id}", json={"enabled": True})
    assert (await client.get("/gw/life/x", headers=head)).status_code == 200

    # delete -> 401
    assert (await client.delete(f"/api/gateway/keys/{key_id}")).status_code == 200
    assert (await client.get("/gw/life/x", headers=head)).status_code == 401


async def test_rotated_key_invalidates_old(client, upstream) -> None:
    g = await make_gateway(client, "rot")
    await make_route(client, g["id"], path="/*", upstream=upstream, auth_mode="api_key")
    c = await make_consumer(client, g["id"])
    created = await make_key(client, c["id"])
    head = {"X-API-Key": created["key"]}
    assert (await client.get("/gw/rot/x", headers=head)).status_code == 200

    rotated = (await client.post(f"/api/gateway/keys/{created['id']}/rotate")).json()
    assert rotated["key"] != created["key"]

    # old key rejected, new key accepted
    assert (await client.get("/gw/rot/x", headers=head)).status_code == 401
    assert (await client.get("/gw/rot/x", headers={"X-API-Key": rotated["key"]})).status_code == 200


async def test_consumer_delete_kills_its_keys(client, upstream) -> None:
    g = await make_gateway(client, "cdel")
    await make_route(client, g["id"], path="/*", upstream=upstream, auth_mode="api_key")
    c = await make_consumer(client, g["id"])
    key = (await make_key(client, c["id"]))["key"]

    assert (await client.get("/gw/cdel/x", headers={"X-API-Key": key})).status_code == 200

    assert (await client.delete(f"/api/gateway/consumers/{c['id']}")).status_code == 200

    # The key row is gone, so it can no longer authenticate.
    res = await client.get("/gw/cdel/x", headers={"X-API-Key": key})
    assert res.status_code == 401


async def test_rate_limit_enforced(client, upstream) -> None:
    g = await make_gateway(client, "rl")
    await make_route(
        client, g["id"], path="/v1/*", upstream=upstream, auth_mode="api_key", rate_limit_rpm=2
    )
    c = await make_consumer(client, g["id"])
    key = (await make_key(client, c["id"]))["key"]
    head = {"X-API-Key": key}

    assert (await client.get("/gw/rl/v1/echo", headers=head)).status_code == 200
    assert (await client.get("/gw/rl/v1/echo", headers=head)).status_code == 200
    res = await client.get("/gw/rl/v1/echo", headers=head)
    assert res.status_code == 429
    assert res.headers.get("retry-after") == "60"
    assert "rate limit" in res.json()["error"]


async def test_gateway_delete_cleans_children(client, upstream) -> None:
    g = await make_gateway(client, "gdel")
    await make_route(client, g["id"], path="/*", upstream=upstream, auth_mode="api_key")
    c = await make_consumer(client, g["id"])
    key = (await make_key(client, c["id"]))["key"]

    await client.delete(f"/api/gateway/{g['id']}")

    # proxy 404; routes/consumers gone; consumer's keys can't be listed
    assert (await client.get("/gw/gdel/x", headers={"X-API-Key": key})).status_code == 404
    assert (await client.get(f"/api/gateway/{g['id']}/routes")).status_code == 404
    assert (await client.get(f"/api/gateway/consumers/{c['id']}/keys")).status_code == 404


# ---------------------------------------------------------------------------
# Logs + console
# ---------------------------------------------------------------------------


async def test_logs_recorded_and_cleared(client, upstream) -> None:
    g = await make_gateway(client, "logs")
    await make_route(client, g["id"], path="/*", upstream=upstream)

    await client.get("/gw/logs/one")
    await client.get("/gw/logs/two")

    logs = (await client.get(f"/api/gateway/logs?gateway_id={g['id']}")).json()
    assert len(logs) == 2
    paths = {log["path"] for log in logs}
    assert "/gw/logs/one" in paths and "/gw/logs/two" in paths
    assert all(log["status"] == 200 for log in logs)
    assert logs[0]["id"] > logs[1]["id"]  # newest first

    res = await client.delete(f"/api/gateway/logs?gateway_id={g['id']}")
    assert res.status_code == 200 and res.json()["deleted"] == 2
    assert (await client.get(f"/api/gateway/logs?gateway_id={g['id']}")).json() == []


async def test_direct_test_console(client, upstream) -> None:
    res = await client.post(
        "/api/gateway/test",
        json={"method": "GET", "url": f"{upstream}/v1/ping", "timeout_ms": 2000},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["status"] == 200
    assert body["latency_ms"] >= 0
    assert "v1/ping" in body["body"]


# ---------------------------------------------------------------------------
# TLS derivation (simulated issuance)
# ---------------------------------------------------------------------------


async def test_tls_status_defaults_to_none(client) -> None:
    g = await make_gateway(client, "tlsmin")
    assert g["tls_status"] == "none"
    assert g["tls_enabled"] is False


async def test_tls_issue_with_domain_goes_valid(client) -> None:
    g = await make_gateway(
        client, "tlsgw", tls_enabled=True, tls_domain="api.gw.test", issuer="Let's Encrypt"
    )
    assert g["tls_status"] == "valid"
    assert g["cert_id"] is not None

    # re-fetch derives the same status
    detail = (await client.get(f"/api/gateway/{g['id']}")).json()
    assert detail["tls_status"] == "valid"

    # a certificate was created through the shared pipeline
    certs = (await client.get("/api/certs")).json()
    assert any(c["id"] == g["cert_id"] for c in certs)

    # renew keeps it valid
    res = await client.post(f"/api/gateway/{g['id']}/renew-tls")
    assert res.status_code == 200
    assert res.json()["tls_status"] == "valid"


async def test_tls_issue_requires_domain(client) -> None:
    res = await client.post("/api/gateway", json={"name": "x", "slug": "xtls", "tls_enabled": True})
    # no tls_domain -> gateway still created but TLS stays none (no cert issued)
    assert res.status_code == 201
    assert res.json()["tls_status"] == "none"

    # issuing directly without a domain is rejected
    gid = res.json()["id"]
    res = await client.post(f"/api/gateway/{gid}/issue-tls")
    assert res.status_code == 422

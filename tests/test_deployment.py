"""Certificate deployment: target CRUD, SSH/webhook push, records, auto-deploy."""

from __future__ import annotations

from app.database import get_session_factory
from app.services import deployment_service, webhook_service


def _target_payload(**overrides) -> dict:
    payload = {
        "name": "web1",
        "host": "192.168.1.20",
        "port": 22,
        "ssh_user": "root",
        "cert_path": "/etc/trove/host.crt",
        "key_path": "/etc/trove/host.key",
        "chain_path": "/etc/trove/chain.pem",
        "reload_command": "systemctl reload nginx",
        "webhook_url": "",
        "auto_deploy": True,
        "enabled": True,
    }
    payload.update(overrides)
    return payload


async def _make_webhook_target(client) -> dict:
    res = await client.post(
        "/api/deployments",
        json=_target_payload(
            name="webhook-only",
            ssh_private_key="",
            ssh_password="",
            cert_path="",
            webhook_url="https://hooks.example.local/reload",
        ),
    )
    assert res.status_code == 201
    return res.json()


async def _seed_cert(client, headers: dict | None = None) -> str:
    res = await client.get("/api/certs", headers=headers or {})
    assert res.status_code == 200
    return res.json()[0]["id"]


class _FakeConn:
    """Minimal asyncssh client fake for _ssh_* helpers."""

    def __init__(self, exit_status: int = 0):
        self.exit_status = exit_status
        self.closed = False

    async def run(self, command, check=False):
        output = type("Out", (), {"exit_status": self.exit_status, "stdout": "ok", "stderr": ""})()
        return output

    def close(self) -> None:
        self.closed = True


# ---------------------------------------------------------------------------
# Target CRUD
# ---------------------------------------------------------------------------


async def test_target_list_requires_auth_when_api_key_set(client, monkeypatch) -> None:
    monkeypatch.setenv("TROVE_API_KEY", "sekret-test-key")
    no_key = await client.get("/api/deployments")
    assert no_key.status_code == 401
    ok = await client.get("/api/deployments", headers={"Authorization": "Bearer sekret-test-key"})
    assert ok.status_code == 200


async def test_create_target_webhook_only_hides_secrets(client) -> None:
    target = await _make_webhook_target(client)
    assert "ssh_private_key" not in target and "ssh_password" not in target
    assert target["ssh_key_configured"] is False
    assert target["ssh_password_configured"] is False
    assert target["auto_deploy"] is True
    assert target["reload_command"] == "systemctl reload nginx"


async def test_create_target_encrypts_ssh_creds_at_rest(client) -> None:
    plaintext = "-----BEGIN OPENSSH PRIVATE KEY-----\nsekret\n-----END OPENSSH PRIVATE KEY-----\n"
    res = await client.post(
        "/api/deployments", json=_target_payload(ssh_private_key=plaintext, ssh_password="hunter2")
    )
    assert res.status_code == 201
    body = res.json()
    assert body["ssh_key_configured"] is True
    assert body["ssh_password_configured"] is True

    async with get_session_factory()() as db:
        target = await deployment_service.get_target(db, body["id"])
        assert target is not None
        assert plaintext not in target.ssh_private_key
        from app.services import key_store

        assert key_store.decrypt_value(target.ssh_private_key) == plaintext
        assert key_store.decrypt_value(target.ssh_password) == "hunter2"


async def test_create_target_requires_some_auth(client) -> None:
    res = await client.post(
        "/api/deployments",
        json=_target_payload(
            name="nada",
            ssh_private_key="",
            ssh_password="",
            webhook_url="",
            reload_command="",
        ),
    )
    assert res.status_code == 422
    assert "credentials" in res.json()["detail"]


async def test_duplicate_target_name_conflicts(client) -> None:
    await _make_webhook_target(client)
    dup = await client.post(
        "/api/deployments", json=_target_payload(name="webhook-only", ssh_password="hunter2")
    )
    assert dup.status_code == 409


async def test_update_and_delete_target(client) -> None:
    target = await _make_webhook_target(client)
    res = await client.patch(
        f"/api/deployments/{target['id']}",
        json={"reload_command": "systemctl reload traefik", "auto_deploy": False},
    )
    assert res.status_code == 200
    assert res.json()["reload_command"] == "systemctl reload traefik"
    assert res.json()["auto_deploy"] is False

    gone = await client.delete(f"/api/deployments/{target['id']}")
    assert gone.status_code == 200
    assert (await client.get(f"/api/deployments/{target['id']}")).status_code == 404


async def test_target_not_found(client) -> None:
    assert (await client.get("/api/deployments/9999")).status_code == 404
    assert (await client.delete("/api/deployments/9999")).status_code == 404


# ---------------------------------------------------------------------------
# Assignment
# ---------------------------------------------------------------------------


async def test_assign_list_unassign(client) -> None:
    cert_id = await _seed_cert(client)
    target = await _make_webhook_target(client)

    res = await client.post(f"/api/certs/{cert_id}/deployments", json={"target_id": target["id"]})
    assert res.status_code == 201
    assignments = res.json()
    assert len(assignments) == 1
    assert assignments[0]["target_id"] == target["id"]
    assert assignments[0]["last_state"] is None

    listings = await client.get(f"/api/certs/{cert_id}/deployments")
    assert listings.status_code == 200
    assert len(listings.json()) == 1

    history = await client.get(f"/api/certs/{cert_id}/deployments/history")
    assert history.status_code == 200
    assert history.json() == []

    removed = await client.delete(f"/api/certs/{cert_id}/deployments/{target['id']}")
    assert removed.status_code == 200
    assert (await client.get(f"/api/certs/{cert_id}/deployments")).json() == []

    missing = await client.delete(f"/api/certs/{cert_id}/deployments/{target['id']}")
    assert missing.status_code == 404


async def test_assign_to_missing_target(client) -> None:
    cert_id = await _seed_cert(client)
    res = await client.post(f"/api/certs/{cert_id}/deployments", json={"target_id": 9999})
    assert res.status_code == 404


async def test_deploy_endpoints_require_operator(client, monkeypatch) -> None:
    from app.config import get_settings as get_cached_settings

    monkeypatch.setenv("TROVE_API_KEY", "sekret-test-key")
    get_cached_settings.cache_clear()
    try:
        auth = {"Authorization": "Bearer sekret-test-key"}
        cert_id = await _seed_cert(client, headers=auth)
        res = await client.post(
            f"/api/certs/{cert_id}/deploy",
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert res.status_code == 401
    finally:
        get_cached_settings.cache_clear()


# ---------------------------------------------------------------------------
# Deploy (webhook-only target)
# ---------------------------------------------------------------------------


async def test_manual_deploy_webhook_only_success(client, monkeypatch) -> None:
    cert_id = await _seed_cert(client)
    target = await _make_webhook_target(client)
    assert (
        await client.post(f"/api/certs/{cert_id}/deployments", json={"target_id": target["id"]})
    ).status_code == 201

    calls: list[dict] = []

    async def _fake_webhook(url, payload):
        calls.append({"url": url, "payload": payload})
        return True, "ok"

    monkeypatch.setattr(webhook_service, "fire_reload_webhook", _fake_webhook)

    res = await client.post(f"/api/certs/{cert_id}/deploy")
    assert res.status_code == 200
    body = res.json()
    assert body["deployed"] == [target["id"]]
    assert body["failed"] == []
    assert calls and calls[0]["payload"]["action"] == "deploy"

    assignments = (await client.get(f"/api/certs/{cert_id}/deployments")).json()
    assert assignments[0]["last_state"] == "success"

    history = (await client.get(f"/api/certs/{cert_id}/deployments/history")).json()
    assert history[-1]["state"] == "success"


async def test_manual_deploy_ssh_without_key_fails_cleanly(client, monkeypatch) -> None:
    cert_id = await _seed_cert(client)  # seed certs have no stored private key
    res = await client.post(
        "/api/deployments",
        json=_target_payload(name="ssh-no-key", ssh_password="hunter2"),
    )
    assert res.status_code == 201
    ssh_target = res.json()
    assert (
        await client.post(f"/api/certs/{cert_id}/deployments", json={"target_id": ssh_target["id"]})
    ).status_code == 201

    res = await client.post(f"/api/certs/{cert_id}/deploy")
    assert res.status_code == 200
    body = res.json()
    assert body["failed"] == [ssh_target["id"]]
    assert "no private key" in body["details"][str(ssh_target["id"])]

    history = (await client.get(f"/api/certs/{cert_id}/deployments/history")).json()
    assert history[-1]["state"] == "failed"


# ---------------------------------------------------------------------------
# Deploy (SSH target with stored key) - fake transport
# ---------------------------------------------------------------------------


async def _issue_ca_signed_cert(client, cn="deploy.test") -> dict:
    root_res = await client.post(
        "/api/ca/root",
        json={
            "name": "Deploy Root",
            "cn": "Deploy Root CA",
            "key_type": "ECDSA P-256",
            "validity_days": 3650,
        },
    )
    assert root_res.status_code == 201
    root = root_res.json()
    assert (await client.post("/api/ca/active", json={"ca_id": root["id"]})).status_code == 200
    res = await client.post(
        "/api/certs",
        json={
            "cn": cn,
            "sans": [cn],
            "issuer": "Deploy Root CA",
            "protocol": "Local CA",
            "key_type": "ECDSA P-256",
            "validity_days": 90,
            "auto_renew": True,
        },
    )
    assert res.status_code == 201
    return res.json()


async def test_manual_deploy_ssh_success(client, monkeypatch) -> None:
    cert = await _issue_ca_signed_cert(client)
    cert_id = cert["id"]

    res = await client.post(
        "/api/deployments",
        json=_target_payload(
            name="ssh-host",
            ssh_private_key="-----BEGIN OPENSSH PRIVATE KEY-----\nsekret\n-----END OPENSSH PRIVATE KEY-----\n",
            ssh_password="",
        ),
    )
    assert res.status_code == 201
    target = res.json()
    assert (
        await client.post(f"/api/certs/{cert_id}/deployments", json={"target_id": target["id"]})
    ).status_code == 201

    pushes: list[tuple[str, bytes, int]] = []
    ran_commands: list[str] = []
    fake_conn = _FakeConn()

    async def _fake_connect(t):
        return fake_conn

    async def _fake_put(conn, path, data, mode):
        pushes.append((path, data, mode))

    async def _fake_run(conn, command):
        ran_commands.append(command)
        return "ok"

    monkeypatch.setattr(deployment_service, "_ssh_connect", _fake_connect)
    monkeypatch.setattr(deployment_service, "_ssh_put_bytes", _fake_put)
    monkeypatch.setattr(deployment_service, "_ssh_run", _fake_run)

    res = await client.post(f"/api/certs/{cert_id}/deploy")
    assert res.status_code == 200
    body = res.json()
    assert body["deployed"] == [target["id"]]
    assert fake_conn.closed is True

    pushed_paths = {p for p, _d, _m in pushes}
    assert pushed_paths == {"/etc/trove/host.crt", "/etc/trove/host.key", "/etc/trove/chain.pem"}
    modes = {p: m for p, _d, m in pushes}
    assert modes["/etc/trove/host.key"] == 0o600
    assert modes["/etc/trove/host.crt"] == 0o644
    assert ran_commands == ["systemctl reload nginx"]

    # Full chain was built: leaf + Deploy Root CA.
    chain_text = dict((p, d) for p, d, _m in pushes)["/etc/trove/chain.pem"].decode()
    assert chain_text.count("BEGIN CERTIFICATE") == 2

    assignments = (await client.get(f"/api/certs/{cert_id}/deployments")).json()
    assert assignments[0]["last_state"] == "success"
    assert "Deployed" in assignments[0]["last_detail"]


async def test_auto_deploy_skips_non_auto_and_disabled_targets(client, monkeypatch) -> None:
    cert = await _issue_ca_signed_cert(client)
    cert_id = cert["id"]

    enabled = await client.post(
        "/api/deployments",
        json=_target_payload(name="auto-yes", ssh_password="hunter2"),
    )
    manual_only = await client.post(
        "/api/deployments",
        json=_target_payload(name="auto-no", ssh_password="hunter2", auto_deploy=False),
    )
    disabled = await client.post(
        "/api/deployments",
        json=_target_payload(name="disabled", ssh_password="hunter2", enabled=False),
    )
    for t in (enabled, manual_only, disabled):
        assert t.status_code == 201
        assert (
            await client.post(
                f"/api/certs/{cert_id}/deployments", json={"target_id": t.json()["id"]}
            )
        ).status_code == 201

    async def _fake_connect(t):
        return _FakeConn()

    async def _fake_put(conn, p, d, m):
        return None

    async def _fake_run(conn, cmd):
        return "ok"

    monkeypatch.setattr(deployment_service, "_ssh_connect", _fake_connect)
    monkeypatch.setattr(deployment_service, "_ssh_put_bytes", _fake_put)
    monkeypatch.setattr(deployment_service, "_ssh_run", _fake_run)

    auto = await client.post(f"/api/certs/{cert_id}/deploy")
    assert auto.status_code == 200
    deployed_ids = set(auto.json()["deployed"])
    assert deployed_ids == {enabled.json()["id"], manual_only.json()["id"]}

    # Simulate the post-renew auto hook vs a manual pass.
    async with get_session_factory()() as db:
        cert_row = await deployment_service.get_cert(db, cert_id)
        records = await deployment_service.deploy_cert_targets(db, cert_row, reason="renew")
        auto_targets = {r.target_id for r in records}
        assert auto_targets == {enabled.json()["id"]}

        records = await deployment_service.deploy_cert_targets(db, cert_row, reason="manual")
        manual_targets = {r.target_id for r in records}
        assert manual_targets == {enabled.json()["id"], manual_only.json()["id"]}
        await db.commit()


# ---------------------------------------------------------------------------
# Service-level: chain building + records
# ---------------------------------------------------------------------------


async def test_build_fullchain_pulls_local_ca(client) -> None:
    cert = await _issue_ca_signed_cert(client)
    async with get_session_factory()() as db:
        cert_row = await deployment_service.get_cert(db, cert["id"])
        chain = await deployment_service.build_fullchain(db, cert_row)
    assert chain.count("BEGIN CERTIFICATE") == 2  # leaf + root


async def test_build_fullchain_foreign_issuer_leaf_only(client) -> None:
    cert_id = await _seed_cert(client)
    async with get_session_factory()() as db:
        cert_row = await deployment_service.get_cert(db, cert_id)
        chain = await deployment_service.build_fullchain(db, cert_row)
    assert chain.count("BEGIN CERTIFICATE") == 1
    assert "BEGIN CERTIFICATE" in chain


async def test_deploy_records_limit_and_order(client) -> None:
    cert_id = await _seed_cert(client)
    target = await _make_webhook_target(client)
    async with get_session_factory()() as db:
        from app.models import DeploymentRecord

        for i in range(5):
            db.add(
                DeploymentRecord(
                    cert_id=cert_id,
                    target_id=target["id"],
                    serial=f"00{i}",
                    state="success" if i % 2 == 0 else "failed",
                    detail=f"attempt {i}",
                )
            )
        await db.commit()

    history = (await client.get(f"/api/certs/{cert_id}/deployments/history?limit=3")).json()
    assert [r["detail"] for r in history] == ["attempt 2", "attempt 3", "attempt 4"]

    async with get_session_factory()() as db:
        records = await deployment_service.list_records(db, cert_id, limit=2)
        assert [r.serial for r in records] == ["003", "004"]


async def test_ssh_run_failure_records_failed(client, monkeypatch) -> None:
    cert = await _issue_ca_signed_cert(client)
    cert_id = cert["id"]
    res = await client.post(
        "/api/deployments",
        json=_target_payload(name="reload-fails", ssh_password="hunter2"),
    )
    target = res.json()
    assert (
        await client.post(f"/api/certs/{cert_id}/deployments", json={"target_id": target["id"]})
    ).status_code == 201

    async def _fake_connect(t):
        return _FakeConn(exit_status=1)

    async def _fake_put(conn, p, d, m):
        return None

    monkeypatch.setattr(deployment_service, "_ssh_connect", _fake_connect)
    monkeypatch.setattr(deployment_service, "_ssh_put_bytes", _fake_put)
    monkeypatch.setattr(deployment_service, "_ssh_run", deployment_service._ssh_run)

    res = await client.post(f"/api/certs/{cert_id}/deploy")
    assert res.status_code == 200
    body = res.json()
    assert body["failed"] == [target["id"]]
    assert "reload command exited 1" in body["details"][str(target["id"])]

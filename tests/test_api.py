from __future__ import annotations

from datetime import UTC, datetime, timedelta

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app.services import crypto_service

ISSUE_PAYLOAD = {
    "cn": "api.home.arpa",
    "sans": ["api.home.arpa", "gw.home.arpa"],
    "issuer": "Let's Encrypt",
    "protocol": "ACME DNS-01 (Cloudflare)",
    "key_type": "ECDSA P-256",
    "validity_days": 90,
    "auto_renew": True,
}


async def test_health(client) -> None:
    res = await client.get("/api/health")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert body["app"] == "Trove"


async def test_seeded_list(client) -> None:
    res = await client.get("/api/certs")
    assert res.status_code == 200
    certs = res.json()
    assert len(certs) == 5
    statuses = {c["status"] for c in certs}
    assert {"valid", "expiring", "expired"} <= statuses
    for cert in certs:
        assert cert["serial"]
        assert cert["fingerprint"]
        assert cert["not_after"]


async def test_metrics(client) -> None:
    res = await client.get("/api/certs/metrics")
    assert res.status_code == 200
    metrics = res.json()
    assert metrics["total"] == 5
    assert metrics["expired"] == 1  # the seeded self-signed cert
    assert metrics["auto_renew"] == 4
    assert metrics["valid"] + metrics["expiring"] + metrics["expired"] == 5


async def test_issue_cert(client) -> None:
    res = await client.post("/api/certs", json=ISSUE_PAYLOAD)
    assert res.status_code == 201
    cert = res.json()
    assert cert["cn"] == "api.home.arpa"
    assert cert["status"] == "valid"
    assert cert["days_left"] == 90
    assert cert["sans"] == ["api.home.arpa", "gw.home.arpa"]

    # It should appear in the list.
    listing = (await client.get("/api/certs")).json()
    assert len(listing) == 6
    assert any(c["id"] == cert["id"] for c in listing)

    # Logs should include the issuance.
    logs = (await client.get("/api/logs")).json()
    assert any("issued" in log["message"].lower() for log in logs)


async def test_issue_rejects_blank_cn(client) -> None:
    payload = dict(ISSUE_PAYLOAD)
    payload["cn"] = "   "
    res = await client.post("/api/certs", json=payload)
    assert res.status_code == 422


async def test_renew_extends_expiry(client) -> None:
    certs = (await client.get("/api/certs")).json()
    target = next(c for c in certs if c["status"] == "expiring")

    res = await client.post(f"/api/certs/{target['id']}/renew", json={"reason": "rekey"})
    assert res.status_code == 200
    renewed = res.json()
    assert renewed["days_left"] == 90
    assert renewed["serial"] != target["serial"]
    assert renewed["status"] == "valid"


async def test_reissue_alias(client) -> None:
    certs = (await client.get("/api/certs")).json()
    target = certs[0]
    res = await client.post(
        f"/api/certs/{target['id']}/reissue",
        json={"reason": "sans_update", "force_challenge": True},
    )
    assert res.status_code == 200
    assert res.json()["id"] == target["id"]


async def test_toggle_auto_renew(client) -> None:
    certs = (await client.get("/api/certs")).json()
    target = next(c for c in certs if c["auto_renew"])
    assert target["auto_renew"] is True

    res = await client.patch(f"/api/certs/{target['id']}", json={"auto_renew": False})
    assert res.status_code == 200
    assert res.json()["auto_renew"] is False


async def test_batch_renew(client) -> None:
    res = await client.post("/api/certs/batch-renew")
    assert res.status_code == 200
    renewed = res.json()["renewed"]
    # Seed: 1 expired + 2 expiring are inside the window -> 3 renewals.
    assert len(renewed) == 3

    certs = (await client.get("/api/certs")).json()
    for cert in certs:
        if cert["id"] in renewed:
            assert cert["days_left"] == 90


async def test_revoke_removes_from_active_list(client) -> None:
    certs = (await client.get("/api/certs")).json()
    target = certs[0]

    res = await client.delete(f"/api/certs/{target['id']}")
    assert res.status_code == 200
    assert res.json() == {"ok": True}

    after = (await client.get("/api/certs")).json()
    assert all(c["id"] != target["id"] for c in after)

    # Revoked cert is gone from the default view but still retrievable with the flag.
    detail = await client.get(f"/api/certs/{target['id']}")
    assert detail.status_code == 404


async def test_import_invalid_pem(client) -> None:
    res = await client.post(
        "/api/certs/import", json={"cn": "broken.local", "pem": "this is not a pem"}
    )
    assert res.status_code == 422


async def test_import_valid_pem(client) -> None:
    # Build a real self-signed cert to import (60 days, self-signed).
    data = crypto_service.generate_self_signed(
        cn="router.local.lan",
        sans=["router.local.lan"],
        validity_days=60,
        key_type="RSA 2048",
        issuer_name="My Router CA",
    )
    res = await client.post(
        "/api/certs/import",
        json={"cn": "router.local.lan", "pem": data["cert_pem"], "issuer": None},
    )
    assert res.status_code == 201
    cert = res.json()
    assert cert["cn"] == "router.local.lan"
    assert cert["issuer"] == "My Router CA"  # parsed from the PEM itself
    assert cert["serial"] == data["serial"]
    assert cert["fingerprint"] == data["fingerprint"]
    assert cert["key_type"].startswith("RSA")
    assert cert["status"] == "valid"
    assert cert["days_left"] == 60


async def test_import_parses_key_from_pem(client) -> None:
    # EC key should be detected from the real public key, not guessed.
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, "ec.local.lan")])
    now = datetime.now(UTC).replace(tzinfo=None)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=80))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("ec.local.lan")]), critical=False)
        .sign(key, hashes.SHA256())
    )
    pem = cert.public_bytes(serialization.Encoding.PEM).decode()

    res = await client.post("/api/certs/import", json={"cn": "ec.local.lan", "pem": pem})
    assert res.status_code == 201
    assert "ECDSA" in res.json()["key_type"]


async def test_time_advance_triggers_auto_renew_and_status_change(client) -> None:
    before = (await client.get("/api/certs")).json()
    expiring_ids = {c["id"] for c in before if c["status"] == "expiring" and c["auto_renew"]}
    assert len(expiring_ids) == 2

    res = await client.post("/api/time/advance", json={"days": 40})
    assert res.status_code == 200
    time_info = res.json()
    assert time_info["offset_days"] == 40
    assert time_info["sim_now"] > time_info["real_now"]

    after = (await client.get("/api/certs")).json()
    # Auto-renewed expiring certs now have ~90 days left and are valid again.
    for cert in after:
        if cert["id"] in expiring_ids:
            assert cert["status"] == "valid"
            assert cert["days_left"] == 90

    # Logs contain the AUTO-RENEW TRIGGER entries.
    logs = (await client.get("/api/logs")).json()
    assert any("AUTO-RENEW TRIGGER" in log["message"] for log in logs)


async def test_time_reset(client) -> None:
    await client.post("/api/time/advance", json={"days": 60})
    res = await client.post("/api/time/reset")
    assert res.status_code == 200
    assert res.json()["offset_days"] == 0


async def test_search_and_filter(client) -> None:
    search = (await client.get("/api/certs", params={"search": "vaultwarden"})).json()
    assert len(search) == 1
    assert search[0]["cn"] == "vaultwarden.internal"

    expired = (await client.get("/api/certs", params={"filter": "expired"})).json()
    assert len(expired) == 1
    assert expired[0]["status"] == "expired"

    auto = (await client.get("/api/certs", params={"filter": "autorenew"})).json()
    assert len(auto) == 4
    assert all(c["auto_renew"] for c in auto)


async def test_download_pem(client) -> None:
    certs = (await client.get("/api/certs")).json()
    target = certs[0]
    res = await client.get(f"/api/certs/{target['id']}/pem")
    assert res.status_code == 200
    assert "BEGIN CERTIFICATE" in res.text
    assert "attachment" in res.headers["content-disposition"]


async def test_settings_roundtrip_masks_token(client) -> None:
    res = await client.put(
        "/api/settings",
        json={
            "acme_directory_url": "https://ca.lan:9000/acme/acme/directory",
            "webhook_url": "https://router.lan:2019/load",
            "cf_token": "super-secret-token",
        },
    )
    assert res.status_code == 200
    settings = res.json()
    assert settings["acme_directory_url"].endswith("/directory")
    assert settings["cf_token_masked"] is True

    # The raw token must never be returned.
    fetched = await client.get("/api/settings")
    assert "super-secret-token" not in str(fetched.json())


async def test_sync(client) -> None:
    res = await client.post("/api/sync")
    assert res.status_code == 200
    assert res.json()["ok"] is True
    logs = (await client.get("/api/logs")).json()
    assert any("sync" in log["message"].lower() for log in logs)


async def test_clear_logs(client) -> None:
    await client.post("/api/sync")
    assert len((await client.get("/api/logs")).json()) > 0
    res = await client.delete("/api/logs")
    assert res.status_code == 200
    assert (await client.get("/api/logs")).json() == []


async def test_optional_api_key_auth(client, monkeypatch) -> None:
    """When TROVE_API_KEY is set, requests must present the bearer token."""
    from app.config import get_settings as get_cached_settings

    monkeypatch.setenv("TROVE_API_KEY", "sekret-test-key")
    get_cached_settings.cache_clear()
    try:
        denied = await client.get("/api/certs")
        assert denied.status_code == 401

        allowed = await client.get(
            "/api/certs", headers={"Authorization": "Bearer sekret-test-key"}
        )
        assert allowed.status_code == 200
        assert len(allowed.json()) == 5
    finally:
        get_cached_settings.cache_clear()


async def test_dockwatch_routes_share_api_key_auth(client, monkeypatch) -> None:
    """Dockwatch routers sit behind the same principal gate as the cert API."""
    from app.config import get_settings as get_cached_settings

    monkeypatch.setenv("TROVE_API_KEY", "sekret-test-key")
    get_cached_settings.cache_clear()
    try:
        denied = await client.get("/api/models/fleet")
        assert denied.status_code in (401, 403)

        allowed = await client.get(
            "/api/models/fleet",
            headers={"Authorization": "Bearer sekret-test-key"},
        )
        assert allowed.status_code == 200
    finally:
        get_cached_settings.cache_clear()

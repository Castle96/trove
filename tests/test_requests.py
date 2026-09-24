"""Issuance approval workflow."""

from __future__ import annotations

from app.config import get_settings

ISSUE = {
    "cn": "approve.test",
    "sans": ["approve.test"],
    "issuer": "Let's Encrypt",
    "protocol": "ACME DNS-01 (Cloudflare)",
    "key_type": "ECDSA P-256",
    "validity_days": 90,
    "auto_renew": True,
}


async def test_approval_disabled_issues_directly(client) -> None:
    assert get_settings().require_approval is False
    res = await client.post("/api/certs", json=ISSUE)
    assert res.status_code == 201
    assert res.json()["status"] == "valid"
    assert (await client.get("/api/requests")).json() == []


async def test_approval_workflow_full_cycle(client, monkeypatch) -> None:
    # require_approval is read from settings per-request via the env gate.
    monkeypatch.setenv("TROVE_REQUIRE_APPROVAL", "true")
    get_settings.cache_clear()

    # Submitting a cert now creates a pending request, not a certificate.
    res = await client.post("/api/certs", json=ISSUE)
    assert res.status_code == 201
    body = res.json()
    assert body["status"] == "pending"
    assert body["cn"] == "approve.test"
    request_id = body["id"]

    # No certificate exists yet.
    certs = (await client.get("/api/certs")).json()
    assert all(c["cn"] != "approve.test" for c in certs)

    # Pending list shows it.
    pending = (await client.get("/api/requests", params={"filter": "pending"})).json()
    assert any(r["id"] == request_id for r in pending)

    # Deny path.
    deny = await client.post(
        f"/api/requests/{request_id}/deny",
        json={"reason": "not authorized"},
    )
    assert deny.status_code == 200
    assert deny.json()["status"] == "denied"

    # Approving a denied request conflicts.
    conflict = await client.post(f"/api/requests/{request_id}/approve")
    assert conflict.status_code == 409

    # New request -> approve -> cert is issued.
    res = await client.post("/api/certs", json=ISSUE)
    assert res.status_code == 201
    request_id = res.json()["id"]
    approved = await client.post(f"/api/requests/{request_id}/approve")
    assert approved.status_code == 200
    issued = approved.json()
    assert issued["status"] == "valid"
    certs = (await client.get("/api/certs")).json()
    assert any(c["id"] == issued["id"] for c in certs)

    # Issuing directly through the request listing is reflected.
    decided = (await client.get("/api/requests", params={"filter": "approved"})).json()
    assert any(r["id"] == request_id and r["cert_id"] == issued["id"] for r in decided)

    get_settings.cache_clear()

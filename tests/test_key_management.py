"""Encrypted key at rest, key download, PKCS#12 export, key rotation."""

from __future__ import annotations

from app.database import get_session_factory
from app.models import Certificate
from app.services import crypto_service, key_store

ISSUE = {
    "cn": "keys.test",
    "sans": ["keys.test"],
    "issuer": "Let's Encrypt",
    "protocol": "Simulated",
    "key_type": "ECDSA P-256",
    "validity_days": 90,
    "auto_renew": True,
}


async def _issue(client) -> dict:
    res = await client.post("/api/certs", json=ISSUE)
    assert res.status_code == 201
    return res.json()


async def test_issued_key_is_encrypted_at_rest(client) -> None:
    cert = await _issue(client)
    async with get_session_factory()() as db:
        row = await db.get(Certificate, cert["id"])
        assert row is not None
        assert row.private_key
        # Stored blob must be ciphertext, never the plaintext key.
        assert "BEGIN PRIVATE KEY" not in row.private_key
        assert row.private_key.startswith("v1:")
        decrypted = key_store.decrypt_value(row.private_key)
    assert "BEGIN PRIVATE KEY" in decrypted


async def test_key_download(client) -> None:
    cert = await _issue(client)
    res = await client.get(f"/api/certs/{cert['id']}/key")
    assert res.status_code == 200
    assert "BEGIN PRIVATE KEY" in res.text
    assert "attachment" in res.headers["content-disposition"]


async def test_pkcs12_export(client) -> None:
    cert = await _issue(client)
    no_pass = await client.get(f"/api/certs/{cert['id']}/p12")
    assert no_pass.status_code == 400

    res = await client.get(f"/api/certs/{cert['id']}/p12", params={"password": "hunter2"})
    assert res.status_code == 200
    assert res.headers["content-type"] == "application/x-pkcs12"
    assert res.headers["content-disposition"].endswith('.p12"')

    from cryptography.hazmat.primitives.serialization import pkcs12

    key, xcert, _extra = pkcs12.load_key_and_certificates(res.content, b"hunter2")
    assert key is not None and xcert is not None
    assert xcert.subject.rfc4514_string().endswith("keys.test")


async def test_rotate_key_changes_material(client) -> None:
    cert = await _issue(client)
    old_serial = cert["serial"]
    old_key = (await client.get(f"/api/certs/{cert['id']}/key")).text

    res = await client.post(f"/api/certs/{cert['id']}/rotate-key")
    assert res.status_code == 200
    assert res.json()["serial"] != old_serial

    new_key = (await client.get(f"/api/certs/{cert['id']}/key")).text
    assert new_key != old_key
    assert "BEGIN PRIVATE KEY" in new_key


async def test_key_download_missing_for_imported(client) -> None:
    data = crypto_service.generate_self_signed(
        cn="imported.test",
        sans=["imported.test"],
        validity_days=30,
        key_type="ECDSA P-256",
        issuer_name="X",
    )
    res = await client.post(
        "/api/certs/import", json={"cn": "imported.test", "pem": data["cert_pem"]}
    )
    assert res.status_code == 201
    cert = res.json()

    key_res = await client.get(f"/api/certs/{cert['id']}/key")
    assert key_res.status_code == 404
    p12_res = await client.get(f"/api/certs/{cert['id']}/p12", params={"password": "pw"})
    assert p12_res.status_code == 404

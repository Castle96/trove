"""Local CA management: root/intermediate creation, CSR signing, active CA, chains."""

from __future__ import annotations

from app.services import crypto_service


async def _create_root(client, cn="Trove Test Root CA") -> dict:
    res = await client.post(
        "/api/ca/root",
        json={
            "name": "Test Root",
            "cn": cn,
            "key_type": "ECDSA P-256",
            "validity_days": 3650,
        },
    )
    assert res.status_code == 201
    ca = res.json()
    # Activate explicitly so CA-signed issuance tests work end-to-end.
    set_res = await client.post("/api/ca/active", json={"ca_id": ca["id"]})
    assert set_res.status_code == 200
    return set_res.json()


async def test_create_root_ca(client) -> None:
    ca = await _create_root(client)
    assert ca["kind"] == "root"
    assert ca["cn"] == "Trove Test Root CA"
    assert ca["serial"]
    assert ca["fingerprint"]
    assert ca["enabled"] is True
    assert ca["active"] is True

    # A second CA is NOT auto-activated - explicit choice.
    second = await client.post(
        "/api/ca/root",
        json={
            "name": "Test Root 2",
            "cn": "Trove Test Root CA 2",
            "key_type": "ECDSA P-256",
            "validity_days": 3650,
        },
    )
    assert second.status_code == 201
    assert second.json()["active"] is False


async def test_create_intermediate_signed_by_root(client) -> None:
    root = await _create_root(client)
    res = await client.post(
        "/api/ca/intermediate",
        json={
            "name": "Test Int",
            "cn": "Trove Test Int CA",
            "key_type": "ECDSA P-256",
            "validity_days": 1825,
            "parent_id": root["id"],
        },
    )
    assert res.status_code == 201
    intermediate = res.json()
    assert intermediate["kind"] == "intermediate"
    assert intermediate["parent_id"] == root["id"]

    from cryptography import x509

    ic = x509.load_pem_x509_certificate(
        (await client.get(f"/api/ca/{intermediate['id']}/cert")).text.encode()
    )
    root_cert = x509.load_pem_x509_certificate(
        (await client.get(f"/api/ca/{root['id']}/cert")).text.encode()
    )
    assert ic.issuer == root_cert.subject


def _ca_cert(client, ca_id: int) -> str:
    res = client.get(f"/api/ca/{ca_id}/cert")
    assert res.status_code == 200
    assert "BEGIN CERTIFICATE" in res.text
    return res.text


async def test_ca_list_and_chain(client) -> None:
    root = await _create_root(client)
    await client.post(
        "/api/ca/intermediate",
        json={
            "name": "Test Int",
            "cn": "Trove Int CA",
            "key_type": "ECDSA P-256",
            "validity_days": 1825,
            "parent_id": root["id"],
        },
    )
    cas = (await client.get("/api/ca")).json()
    assert len(cas) == 2

    int_id = next(c["id"] for c in cas if c["kind"] == "intermediate")
    chain = await client.get(f"/api/ca/{int_id}/chain")
    assert chain.status_code == 200
    assert chain.text.count("BEGIN CERTIFICATE") == 2  # intermediate + root


async def test_set_active_ca(client) -> None:
    root = await _create_root(client)
    res = await client.post("/api/ca/active", json={"ca_id": None})
    assert res.status_code == 200
    assert res.json() is None

    res = await client.post("/api/ca/active", json={"ca_id": root["id"]})
    assert res.status_code == 200
    assert res.json()["active"] is True


async def test_signing_through_ca_changes_issuer(client) -> None:
    """Issued certs get signed by the active CA instead of being self-signed."""
    await _create_root(client)
    res = await client.post(
        "/api/certs",
        json={
            "cn": "ca-signed.home.arpa",
            "sans": ["ca-signed.home.arpa"],
            "issuer": "Trove Test Root CA",
            "protocol": "Local CA",
            "key_type": "ECDSA P-256",
            "validity_days": 90,
            "auto_renew": True,
        },
    )
    assert res.status_code == 201
    cert = res.json()
    pem = (await client.get(f"/api/certs/{cert['id']}/pem")).text

    from cryptography import x509

    leaf = x509.load_pem_x509_certificate(pem.encode())
    assert leaf.issuer.rfc4514_string().endswith("Trove Test Root CA")


async def test_issued_issuer_labels_the_actual_signer(client) -> None:
    """With an active CA, the stored issuer is the CA CN, not the request label."""
    ca = await _create_root(client, cn="Truthful Home CA")
    res = await client.post(
        "/api/certs",
        json={
            "cn": "truthful.home.arpa",
            "sans": ["truthful.home.arpa"],
            "issuer": "Let's Encrypt",
            "protocol": "ACME DNS-01 (Cloudflare)",
            "key_type": "ECDSA P-256",
            "validity_days": 90,
            "auto_renew": True,
        },
    )
    assert res.status_code == 201
    assert res.json()["issuer"] == ca["cn"]


async def test_local_only_issuance_rejects_acme_settings(client, monkeypatch) -> None:
    """TROVE_REQUIRE_LOCAL_ISSUANCE forbids switching the provider to ACME."""
    from app.config import get_settings

    monkeypatch.setenv("TROVE_REQUIRE_LOCAL_ISSUANCE", "true")
    get_settings.cache_clear()
    try:
        res = await client.put(
            "/api/settings",
            json={"provider": "acme"},
        )
        assert res.status_code == 422
        body = res.json()
        assert "acme" in str(body["detail"] if "detail" in body else body).lower()
    finally:
        get_settings.cache_clear()

    # Simulated provider remains usable.
    res = await client.put("/api/settings", json={"provider": "simulated"})
    assert res.status_code == 200


async def test_sign_csr_returns_cert(client) -> None:
    await _create_root(client)
    csr_der, _key = crypto_service.generate_csr(
        cn="csr.test", sans=["csr.test"], key_type="RSA 2048"
    )
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    csr = x509.load_der_x509_csr(csr_der)
    csr_pem = csr.public_bytes(serialization.Encoding.PEM).decode()
    res = await client.post(
        "/api/ca/sign-csr",
        json={"csr": csr_pem, "validity_days": 90},
    )
    assert res.status_code == 200
    body = res.json()
    assert "BEGIN CERTIFICATE" in body["cert_pem"]
    assert body["issuer"].endswith("Trove Test Root CA")

    leaf = x509.load_pem_x509_certificate(body["cert_pem"].encode())
    assert leaf.subject.get_attributes_for_oid(x509.oid.NameOID.COMMON_NAME)[0].value == "csr.test"
    assert leaf.not_valid_after_utc > leaf.not_valid_before_utc


async def test_ca_not_found(client) -> None:
    res = await client.get("/api/ca/9999")
    assert res.status_code == 404


async def test_delete_ca(client) -> None:
    root = await _create_root(client)
    res = await client.delete(f"/api/ca/{root['id']}")
    assert res.status_code == 200
    gone = await client.get("/api/ca")
    assert all(c["id"] != root["id"] for c in gone.json())

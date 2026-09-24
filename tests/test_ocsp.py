"""OCSP responder + CRL using the managed local CA."""

from __future__ import annotations


async def _create_ca_and_cert(client) -> tuple[dict, dict]:
    ca_res = await client.post(
        "/api/ca/root",
        json={
            "name": "OCSP Root",
            "cn": "OCSP Root CA",
            "key_type": "RSA 2048",
            "validity_days": 3650,
        },
    )
    assert ca_res.status_code == 201
    cert_res = await client.post(
        "/api/certs",
        json={
            "cn": "ocsp.test",
            "sans": ["ocsp.test"],
            "issuer": "OCSP Root CA",
            "protocol": "Local CA",
            "key_type": "ECDSA P-256",
            "validity_days": 90,
            "auto_renew": False,
        },
    )
    assert cert_res.status_code == 201
    return ca_res.json(), cert_res.json()


async def test_ocsp_501_without_ca(client) -> None:
    res = await client.get("/api/ocsp", params={"serial": "abcd"})
    assert res.status_code == 501


async def test_ocsp_get_good(client) -> None:
    _ca, cert = await _create_ca_and_cert(client)
    res = await client.get("/api/ocsp", params={"serial": cert["serial"]})
    assert res.status_code == 200
    assert res.headers["content-type"] == "application/ocsp-response"
    assert len(res.content) > 200

    from cryptography.x509 import ocsp

    response = ocsp.load_der_ocsp_response(res.content)
    assert response.response_status == ocsp.OCSPResponseStatus.SUCCESSFUL
    assert response.certificate_status == ocsp.OCSPCertStatus.GOOD


async def test_ocsp_get_revoked(client) -> None:
    _ca, cert = await _create_ca_and_cert(client)
    res = await client.delete(
        f"/api/certs/{cert['id']}", params={"revoke": "true", "reason": "key_compromise"}
    )
    assert res.status_code == 200

    from cryptography.x509 import ocsp

    res = await client.get("/api/ocsp", params={"serial": cert["serial"]})
    assert res.status_code == 200
    response = ocsp.load_der_ocsp_response(res.content)
    assert response.certificate_status == ocsp.OCSPCertStatus.REVOKED


async def test_ocsp_get_unknown(client) -> None:
    await client.post(
        "/api/ca/root",
        json={
            "name": "OCSP Root",
            "cn": "OCSP Root CA",
            "key_type": "RSA 2048",
            "validity_days": 3650,
        },
    )
    res = await client.get("/api/ocsp", params={"serial": "DEADBEEF"})
    assert res.status_code == 200

    from cryptography.x509 import ocsp

    response = ocsp.load_der_ocsp_response(res.content)
    assert response.certificate_status == ocsp.OCSPCertStatus.UNKNOWN


async def test_ocsp_post_request(client) -> None:
    ca, cert = await _create_ca_and_cert(client)
    # Build a minimal OCSPRequest for the cert's serial using the CA as issuer.
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.x509 import ocsp

    ca_cert = x509.load_pem_x509_certificate(
        (await client.get(f"/api/ca/{ca['id']}/cert")).text.encode()
    )
    builder = ocsp.OCSPRequestBuilder()
    builder = builder.add_certificate(
        ca_cert,  # cert reference (serial is what matters to us)
        ca_cert,
        hashes.SHA256(),
    )
    der = builder.build().public_bytes(serialization.Encoding.DER)

    res = await client.post("/api/ocsp", content=der)
    assert res.status_code == 200
    response = ocsp.load_der_ocsp_response(res.content)
    assert response.response_status == ocsp.OCSPResponseStatus.SUCCESSFUL


async def test_crl_works_with_managed_ca(client) -> None:
    _ca, cert = await _create_ca_and_cert(client)
    await client.delete(
        f"/api/certs/{cert['id']}", params={"revoke": "true", "reason": "superseded"}
    )
    res = await client.get("/api/crl")
    assert res.status_code == 200
    assert res.headers["content-type"] == "application/pkix-crl"
    assert "BEGIN X509 CRL" in res.text

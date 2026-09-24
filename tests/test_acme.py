"""Unit tests for the RFC 8555 ACME client + provider seam.

The network-facing classes (CloudflareDNS, ACMEClient, ACMEIssuer) are tested
against scripted httpx transports — nothing here talks to a live ACME server.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.services import acme_service as acme
from app.services import crypto_service, providers

# ---------------------------------------------------------------------------
# Fake HTTP transport scripting
# ---------------------------------------------------------------------------


class _FakeHttp:
    """Minimal httpx.Client stand-in recording calls and returning canned data."""

    def __init__(self, **kwargs) -> None:  # noqa: ANN003
        self.calls: list[tuple[str, str]] = []

    def head(self, url, **kwargs) -> httpx.Response:  # noqa: ANN003
        self.calls.append(("head", url))
        return httpx.Response(
            200, request=httpx.Request("HEAD", url), headers={"Replay-Nonce": "nonce-1"}
        )

    def get(self, url, **kwargs) -> httpx.Response:  # noqa: ANN003
        self.calls.append(("get", url))
        req = httpx.Request("GET", url)
        if "zones" in url:
            return httpx.Response(
                200,
                request=req,
                json={"success": True, "result": [{"id": "zone-1", "name": "example.com"}]},
            )
        if "directory" in url:
            return httpx.Response(
                200,
                request=req,
                json={
                    "newNonce": "https://ca.example/nonce",
                    "newAccount": "https://ca.example/acct",
                    "newOrder": "https://ca.example/orders",
                    "revokeCert": "https://ca.example/revoke",
                },
            )
        return httpx.Response(200, request=req, json={})

    def post(self, url, **kwargs) -> httpx.Response:  # noqa: ANN003
        self.calls.append(("post", url))
        req = httpx.Request("POST", url)
        headers = {"Replay-Nonce": "nonce-2"}
        if "newAccount" in url or url.endswith("/acct"):
            headers["Location"] = "https://ca.example/acct/123"
        if "orders" in url:
            headers["Location"] = "https://ca.example/order/456"
        return httpx.Response(201, request=req, headers=headers, json={"status": "valid"})

    def delete(self, url, **kwargs) -> httpx.Response:  # noqa: ANN003
        self.calls.append(("delete", url))
        return httpx.Response(200, request=httpx.Request("DELETE", url), json={"success": True})

    def close(self) -> None:
        pass


def test_cloudflare_dns_requires_token() -> None:
    with pytest.raises(acme.ACMEError, match="no Cloudflare API token"):
        acme.CloudflareDNS("")


def test_cloudflare_dns_create_and_delete_txt(monkeypatch) -> None:
    fake = _FakeHttp()

    def post(url, **kwargs):  # noqa: ANN003
        fake.calls.append(("post", url))
        if "dns_records" in url:
            return httpx.Response(
                200,
                request=httpx.Request("POST", url),
                json={"success": True, "result": {"id": "rec-1"}},
            )
        return httpx.Response(201, request=httpx.Request("POST", url))

    fake.post = post
    monkeypatch.setattr(acme.httpx, "Client", lambda **kw: fake)
    solver = acme.CloudflareDNS("tok", zone_hint="sub.example.com")

    zone_id, record_id = solver.create_txt("*.example.com", "digest-value")
    assert zone_id == "zone-1" and record_id == "rec-1"

    solver.delete_txt(zone_id, record_id)
    assert any(
        c[1].endswith("/zones/zone-1/dns_records/rec-1") for c in fake.calls if c[0] == "delete"
    )


def test_cloudflare_dns_find_zone_wildcard(monkeypatch) -> None:
    fake = _FakeHttp()
    # fall back to an empty zone list so the not-found path raises
    fake.get = lambda url, **kw: httpx.Response(  # noqa: ANN003
        200, request=httpx.Request("GET", url), json={"success": True, "result": []}
    )
    monkeypatch.setattr(acme.httpx, "Client", lambda **kw: fake)
    solver = acme.CloudflareDNS("tok")
    with pytest.raises(acme.ACMEError, match="No active Cloudflare zone covers"):
        solver.find_zone("nope.example.com")


def test_acme_client_new_account_jws_and_nonce(monkeypatch) -> None:
    fake = _FakeHttp()
    monkeypatch.setattr(acme.httpx, "Client", lambda **kw: fake)

    import cryptography.hazmat.primitives.asymmetric.ec as ec

    key = ec.generate_private_key(ec.SECP256R1())
    client = acme.ACMEClient("https://ca.example/directory", key)
    client.fetch_directory()  # sets _directory from the get()

    posts: list[tuple[str, dict, bool]] = []
    real_post = client._post

    def spy_post(url, payload, use_kid=True):
        posts.append((url, payload, use_kid))
        return real_post(url, payload, use_kid)

    monkeypatch.setattr(client, "_post", spy_post)
    kid = client.new_account(contact_email="ops@example.com")

    assert kid == "https://ca.example/acct/123"
    assert client._kid == kid
    ((post_url, payload, use_kid),) = posts
    assert post_url == "https://ca.example/acct"
    assert use_kid is False  # first account JWS carries a jwk, not a kid
    assert payload["termsOfServiceAgreed"] is True
    assert payload["contact"] == ["mailto:ops@example.com"]

    # A fresh JWS must be compact-JSON with protected/payload/signature and a
    # rotated nonce acting as the anti-replay key.
    jws = json.loads(client._jws("https://ca.example/x", {"hi": 1}, use_kid=False))
    for field in ("protected", "payload", "signature"):
        assert jws[field]
    header = json.loads(acme._unb64u(jws["protected"]).decode())
    assert header["jwk"]["kty"] == "EC"
    assert "kid" not in header
    assert header["nonce"] in ("nonce-1", "nonce-2")
    assert json.loads(acme._unb64u(jws["payload"]).decode()) == {"hi": 1}


def test_acme_http01_rejects_wildcard() -> None:
    cfg = acme.ACMEConfig(directory_url="https://ca.example/dir", validation="http-01")
    issuer = acme.ACMEIssuer(cfg)
    with pytest.raises(acme.ACMEError, match="wildcard"):
        issuer.issue(cn="*.example.com", sans=[], key_type="ECDSA P-256", validity_days=90)


def test_acme_challenge_for_raises_on_missing_type() -> None:
    cfg = acme.ACMEConfig(directory_url="x", validation="dns-01")
    issuer = acme.ACMEIssuer(cfg)
    with pytest.raises(acme.ACMEError, match="offers no dns-01 challenge"):
        issuer._challenge_for({"challenges": [{"type": "http-01"}]})


def test_acme_order_url_fallback() -> None:
    assert acme.ACMEIssuer._order_url({"url": "https://ca/order/1"}) == "https://ca/order/1"
    assert (
        acme.ACMEIssuer._order_url({"finalize": "https://ca/order/1/finalize/2"})
        == "https://ca/order/1/order/2"
    )


# ---------------------------------------------------------------------------
# Provider dispatch + IssuanceError wrapping
# ---------------------------------------------------------------------------


def test_get_provider_dispatch() -> None:
    assert providers.get_provider("simulated").name == "simulated"
    assert providers.get_provider().name == "simulated"  # default fallback


def test_get_provider_acme_unconfigured() -> None:
    with pytest.raises(providers.IssuanceError, match="not configured"):
        providers.get_provider("acme", providers.ProviderConfig(name="acme"))


def test_acme_provider_issues_without_private_key(monkeypatch) -> None:
    # Build a real cert to act as the returned chain, then fake the issuer.
    generated = crypto_service.generate_self_signed(
        cn="acme.test",
        sans=["acme.test"],
        validity_days=30,
        key_type="ECDSA P-256",
        issuer_name="acme",
    )

    class _FakeIssuer:
        def __init__(self, *a, **k) -> None:  # noqa: ANN003
            pass

        def issue(self, **kw) -> dict:  # noqa: ANN003
            return crypto_service.parse_pem(generated["cert_pem"])

    monkeypatch.setattr(providers.acme_service, "ACMEIssuer", _FakeIssuer)
    cfg = acme.ACMEConfig(directory_url="https://ca.example/dir", validation="http-01")
    provider = providers.ACMEProvider(cfg)
    issued = provider.issue(
        cn="acme.test", sans=[], key_type="ECDSA P-256", validity_days=30, issuer="acme"
    )

    assert issued.private_key_pem is None  # ACME keys stay with the CA by design
    assert issued.serial == generated["serial"]
    assert issued.cert_pem == generated["cert_pem"]


def test_acme_provider_wraps_acme_error(monkeypatch) -> None:
    class _Boom:
        def __init__(self, *a, **k) -> None:  # noqa: ANN003
            pass

        def issue(self, **kw):  # noqa: ANN003
            raise acme.ACMEError("server 400: badNonce")

    monkeypatch.setattr(providers.acme_service, "ACMEIssuer", _Boom)
    provider = providers.ACMEProvider(
        acme.ACMEConfig(directory_url="https://ca.example/dir", validation="dns-01")
    )
    with pytest.raises(providers.IssuanceError, match="badNonce"):
        provider.issue(cn="x", sans=[], key_type="ECDSA P-256", validity_days=30, issuer="acme")

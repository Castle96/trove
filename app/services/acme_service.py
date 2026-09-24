"""Minimal, self-contained RFC 8555 ACME client.

Dependencies: only ``httpx`` (HTTP) and ``cryptography`` (JWS/CSR/x509) already
used elsewhere -- no ``acme``/``certbot`` packages required.

Flow implemented (section references to RFC 8555):

1. fetch directory (sec 7.1.1)
2. newNonce (sec 7.2) -- every nonce is single-use
3. newAccount with ECDSA P-256 account key (sec 7.3)
4. newOrder (sec 7.4)
5. per-authorization challenge selection + response:
   - ``dns-01`` (sec 8.4) -- TXT record ``_acme-challenge.<domain>`` written via
     Cloudflare API (or any :class:`DNSChallengeSolver` injected for tests)
   - ``http-01`` (sec 8.3) -- token file written to a webroot directory
6. poll order to ``ready`` -> finalize with CSR (sec 7.4) -> poll to ``valid``
7. download the PEM certificate chain (sec 7.4.2)

The entire flow is synchronous and intended to run inside ``asyncio.to_thread``
(the account key lives on disk so renewals reuse it).
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from . import crypto_service

USER_AGENT = "trove/0.1 (RFC8555)"
MAX_POLL_ATTEMPTS = 45
POLL_DELAY = 2.0

Reason = str


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class ACMEError(RuntimeError):
    pass


def _ec_p256_jwk(pub: ec.EllipticCurvePublicKey) -> dict[str, str]:
    n = pub.public_numbers()
    return {
        "crv": "P-256",
        "kty": "EC",
        "x": _b64u(n.x.to_bytes(32, "big")),
        "y": _b64u(n.y.to_bytes(32, "big")),
    }


def _jwk_thumbprint(account_key: ec.EllipticCurvePrivateKey) -> str:
    jwk = json.dumps(_ec_p256_jwk(account_key.public_key()), separators=(",", ":"), sort_keys=True)
    return _b64u(hashlib.sha256(jwk.encode("utf-8")).digest())


# ---------------------------------------------------------------------------
# Cloudflare DNS solver (dns-01)
# ---------------------------------------------------------------------------


class CloudflareDNS:
    """Create/remove ``_acme-challenge`` TXT records via the Cloudflare API v4."""

    BASE = "https://api.cloudflare.com/client/v4"

    def __init__(self, api_token: str, zone_hint: str = ""):
        if not api_token:
            raise ACMEError("dns-01 selected but no Cloudflare API token configured")
        self.zone_hint = zone_hint.strip().lower()
        self._http = httpx.Client(
            timeout=30.0,
            headers={
                "Authorization": f"Bearer {api_token}",
                "User-Agent": USER_AGENT,
                "Content-Type": "application/json",
            },
        )

    def close(self) -> None:
        self._http.close()

    @staticmethod
    def _strip_wildcard(domain: str) -> str:
        return domain[2:] if domain.startswith("*.") else domain

    def _raise(self, resp: httpx.Response) -> None:
        if resp.status_code >= 400:
            try:
                body = resp.json()
                errors = body.get("errors") or [{"message": body.get("errors")}]
                detail = "; ".join(e.get("message", str(e)) for e in errors)
            except Exception:
                detail = resp.text[:300]
            raise ACMEError(f"Cloudflare API {resp.status_code}: {detail}")

    def _zones(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        page = 1
        while True:
            resp = self._http.get(
                f"{self.BASE}/zones",
                params={"status": "active", "per_page": 50, "page": page},
            )
            self._raise(resp)
            result = resp.json().get("result", [])
            out.extend(result)
            if len(result) < 50:
                break
            page += 1
        return out

    def find_zone(self, domain: str) -> str:
        apex = self._strip_wildcard(domain).rstrip(".").lower()
        zones = self._zones()
        candidates = [
            z for z in zones if apex == z["name"].lower() or apex.endswith("." + z["name"].lower())
        ]
        best = max(candidates, key=lambda z: len(z["name"]), default=None)
        if best is None:
            raise ACMEError(f"No active Cloudflare zone covers {apex}")
        return best["id"]

    def create_txt(self, domain: str, value: str) -> tuple[str, str]:
        """Create the TXT record; returns (zone_id, record_id)."""
        record_name = f"_acme-challenge.{self._strip_wildcard(domain)}"
        zone_id = self.find_zone(domain)
        resp = self._http.post(
            f"{self.BASE}/zones/{zone_id}/dns_records",
            json={"type": "TXT", "name": record_name, "content": value, "ttl": 120},
        )
        self._raise(resp)
        record = resp.json().get("result", {})
        record_id = record.get("id", "")
        if not record_id:
            raise ACMEError("Cloudflare did not return a DNS record id")
        return zone_id, record_id

    def delete_txt(self, zone_id: str, record_id: str) -> None:
        resp = self._http.delete(f"{self.BASE}/zones/{zone_id}/dns_records/{record_id}")
        self._raise(resp)


# ---------------------------------------------------------------------------
# ACME HTTP client (JWS + nonce handling)
# ---------------------------------------------------------------------------


class ACMEClient:
    def __init__(
        self, directory_url: str, account_key: ec.EllipticCurvePrivateKey, timeout: float = 30.0
    ):
        self._directory_url = directory_url.rstrip("/")
        self._account_key = account_key
        self._http = httpx.Client(
            timeout=timeout, headers={"User-Agent": USER_AGENT}, trust_env=True
        )
        self._nonce: str | None = None
        self._kid: str | None = None
        self._directory: dict[str, str] = {}

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> ACMEClient:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    # -- plumbing -----------------------------------------------------------

    def _raise(self, resp: httpx.Response) -> None:
        if resp.status_code >= 400:
            try:
                body = resp.json()
                detail = body.get("detail") or (body.get("error") or {}).get("detail")
            except Exception:
                detail = resp.text[:300]
            raise ACMEError(f"ACME server {resp.status_code}: {detail}")

    def _new_nonce(self) -> str:
        url = self._directory["newNonce"]
        resp = self._http.head(url)
        self._raise(resp)
        nonce = resp.headers.get("Replay-Nonce")
        if not nonce:
            raise ACMEError("ACME server did not return a Replay-Nonce")
        return nonce

    def _jws(self, url: str, payload: dict | None, use_kid: bool) -> str:
        if self._nonce is None:
            self._nonce = self._new_nonce()
        protected: dict[str, Any] = {
            "alg": "ES256",
            "nonce": self._nonce,
            "url": url,
        }
        if use_kid and self._kid:
            protected["kid"] = self._kid
        else:
            protected["jwk"] = _ec_p256_jwk(self._account_key.public_key())

        protected_b64 = _b64u(json.dumps(protected, separators=(",", ":")).encode("utf-8"))
        body = (
            ""
            if payload is None
            else _b64u(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        )
        signing_input = f"{protected_b64}.{body}".encode("ascii")
        der_sig = self._account_key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der_sig)
        raw = r.to_bytes(32, "big") + s.to_bytes(32, "big")
        return json.dumps(
            {
                "protected": protected_b64,
                "payload": body,
                "signature": _b64u(raw),
            },
            separators=(",", ":"),
        )

    def _post(self, url: str, payload: dict | None, use_kid: bool = True) -> httpx.Response:
        body = self._jws(url, payload, use_kid)
        resp = self._http.post(url, content=body, headers={"Content-Type": "application/jose+json"})
        next_nonce = resp.headers.get("Replay-Nonce")
        self._nonce = next_nonce  # always rotate; refetch if the server omits it
        self._raise(resp)
        return resp

    def fetch_directory(self) -> None:
        self._directory = self._http.get(
            self._directory_url, headers={"Accept": "application/json"}
        ).json()
        for key in ("newNonce", "newAccount", "newOrder", "revokeCert"):
            if key not in self._directory:
                raise ACMEError(f"ACME directory missing '{key}' entry")

    # -- operations ---------------------------------------------------------

    def new_account(self, contact_email: str = "") -> str:
        payload: dict[str, Any] = {"termsOfServiceAgreed": True}
        if contact_email:
            payload["contact"] = [f"mailto:{contact_email}"]
        resp = self._post(self._directory["newAccount"], payload, use_kid=False)
        kid = resp.headers.get("Location")
        if not kid:
            raise ACMEError("ACME newAccount response missing Location header")
        self._kid = kid
        return kid

    def new_order(self, domains: list[str]) -> dict[str, Any]:
        identifiers = [{"type": "dns", "value": d} for d in domains]
        resp = self._post(self._directory["newOrder"], {"identifiers": identifiers}, use_kid=True)
        result = resp.json()
        result["url"] = resp.headers.get("Location", "")
        return result

    def post_as_get(self, url: str) -> dict[str, Any]:
        return self._post(url, None, use_kid=True).json()

    def answer_challenge(self, challenge_url: str) -> dict[str, Any]:
        return self._post(challenge_url, {}, use_kid=True).json()

    def finalize(self, finalize_url: str, csr_der: bytes) -> dict[str, Any]:
        resp = self._post(finalize_url, {"csr": _b64u(csr_der)}, use_kid=True)
        return resp.json()

    def download_certificate(self, cert_url: str) -> str:
        resp = self._post(cert_url, None, use_kid=True)
        return resp.text  # application/pem-certificate-chain


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


@dataclass
class ACMEConfig:
    directory_url: str
    contact_email: str = ""
    validation: str = "http-01"  # dns-01 | http-01
    webroot: str = ""
    cloudflare_token: str = ""
    cloudflare_zone_hint: str = ""
    account_key_path: str = ""
    timeout: float = 30.0


class ACMEIssuer:
    """High-level ``issue`` that returns a :class:`~providers.IssuedCertData` payload."""

    def __init__(
        self, config: ACMEConfig, dns_solver: Any | None = None, account_key: Any | None = None
    ):
        self.config = config
        self._dns_solver = dns_solver  # injected for tests; else CloudflareDNS
        self._account_key = account_key  # injected for tests; else loaded/generated

    # -- account key -------------------------------------------------------

    def _load_or_create_account_key(self) -> ec.EllipticCurvePrivateKey:
        if self._account_key is not None:
            return self._account_key
        path = Path(self.config.account_key_path) if self.config.account_key_path else None
        if path and path.exists():
            key = serialization.load_pem_private_key(path.read_bytes(), password=None)
            if isinstance(key, ec.EllipticCurvePrivateKey):
                return key
        key = ec.generate_private_key(ec.SECP256R1())
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(
                key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
            )
        return key

    # -- challenge helpers -------------------------------------------------

    def _challenge_for(self, authz: dict[str, Any]) -> dict[str, Any]:
        for challenge in authz.get("challenges", []):
            if challenge.get("type") == self.config.validation:
                return challenge
        raise ACMEError(
            f"ACME authorization offers no {self.config.validation} challenge "
            f"(types: {[c.get('type') for c in authz.get('challenges', [])]})"
        )

    def _wait_authz(self, client: ACMEClient, authz_url: str) -> None:
        for _ in range(MAX_POLL_ATTEMPTS):
            authz = client.post_as_get(authz_url)
            if authz.get("status") == "valid":
                return
            if authz.get("status") == "invalid":
                raise ACMEError(
                    f"ACME authorization failed: {json.dumps(authz.get('challenges', [])[:1])}"
                )
            time.sleep(POLL_DELAY)
        raise ACMEError("Timed out waiting for ACME authorization")

    def _solve_dns01(
        self, client: ACMEClient, domain: str, challenge: dict[str, Any], thumbprint: str
    ) -> None:
        key_auth = f"{challenge['token']}.{thumbprint}"
        digest = _b64u(hashlib.sha256(key_auth.encode("utf-8")).digest())
        solver = self._dns_solver if self._dns_solver is not None else None
        if solver is None:
            solver = CloudflareDNS(self.config.cloudflare_token, self.config.cloudflare_zone_hint)
        owned = False
        zone_id, record_id = "", ""
        try:
            zone_id, record_id = solver.create_txt(domain, digest)
            owned = True
            client.answer_challenge(challenge["url"])
            self._wait_authz(client, self._authz_for(challenge))
        finally:
            if owned and zone_id and record_id:
                try:
                    solver.delete_txt(zone_id, record_id)
                except Exception:
                    pass  # record cleanup best-effort
            if self._dns_solver is None:
                solver.close()

    def _solve_http01(self, client: ACMEClient, challenge: dict[str, Any], thumbprint: str) -> None:
        key_auth = f"{challenge['token']}.{thumbprint}"
        if not self.config.webroot:
            raise ACMEError("http-01 selected but no webroot configured")
        webroot = Path(self.config.webroot)
        challenge_dir = webroot / ".well-known" / "acme-challenge"
        challenge_dir.mkdir(parents=True, exist_ok=True)
        token_file = challenge_dir / challenge["token"]
        try:
            token_file.write_text(key_auth, encoding="utf-8")
            client.answer_challenge(challenge["url"])
            self._wait_authz(client, self._authz_for(challenge))
        finally:
            try:
                token_file.unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _authz_for(challenge: dict[str, Any]) -> str:
        # challenge payload doesn't carry its parent authz URL; the caller wraps
        # challenges with the authz URL below.
        return challenge.get("authz_url", "")

    # -- main flow ---------------------------------------------------------

    def issue(self, cn: str, sans: list[str], key_type: str, validity_days: int) -> dict[str, Any]:
        del validity_days  # CA determines validity for ACME-issued certs
        domains = list(dict.fromkeys([cn, *sans]))
        for d in domains:
            if d.startswith("*.") and self.config.validation == "http-01":
                raise ACMEError("HTTP-01 cannot be used with wildcard certificates; use dns-01")

        account_key = self._load_or_create_account_key()
        thumbprint = _jwk_thumbprint(account_key)
        client = ACMEClient(self.config.directory_url, account_key, self.config.timeout)
        with client:
            client.fetch_directory()
            client.new_account(self.config.contact_email)
            order = client.new_order(domains)

            for authz_url in order.get("authorizations", []):
                authz = client.post_as_get(authz_url)
                challenge = self._challenge_for(authz)
                challenge["authz_url"] = authz_url
                identifier = (authz.get("identifier") or {}).get("value", cn)
                if self.config.validation == "dns-01":
                    self._solve_dns01(client, identifier, challenge, thumbprint)
                else:
                    self._solve_http01(client, challenge, thumbprint)

            for _ in range(MAX_POLL_ATTEMPTS):
                order = client.post_as_get(self._order_url(order))
                if order.get("status") == "ready":
                    break
                if order.get("status") in ("invalid", "expired"):
                    raise ACMEError(f"ACME order ended in '{order.get('status')}' status")
                time.sleep(POLL_DELAY)
            else:
                raise ACMEError("Timed out waiting for ACME order to become ready")

            csr_der, _key = crypto_service.generate_csr(cn=cn, sans=sans, key_type=key_type)
            client.finalize(order["finalize"], csr_der)

            for _ in range(MAX_POLL_ATTEMPTS):
                order = client.post_as_get(self._order_url(order))
                if order.get("status") == "valid":
                    break
                if order.get("status") == "invalid":
                    raise ACMEError("ACME order failed during finalization")
                time.sleep(POLL_DELAY)
            else:
                raise ACMEError("Timed out waiting for ACME certificate")

            cert_pem = client.download_certificate(order["certificate"])

        # Parse the leaf (first PEM block) for metadata.
        leaf_pem = cert_pem.split("-----END CERTIFICATE-----")[0] + "-----END CERTIFICATE-----"
        meta = crypto_service.parse_pem(leaf_pem)
        meta["cert_pem"] = cert_pem  # full chain
        return meta

    @staticmethod
    def _order_url(order: dict[str, Any]) -> str:
        # new_order() stamps the absolute order URL from its Location header;
        # finalize() returns an order body that may omit it.
        return order.get("url") or order.get("finalize", "").replace("/finalize/", "/order/")

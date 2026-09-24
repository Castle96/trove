"""Issuance provider interface + pluggable implementations.

* :class:`SimulatedProvider` -- issues real self-signed leaf certificates
  locally so the whole lifecycle runs end-to-end without a CA.
* :class:`ACMEProvider` -- a real RFC 8555 client (see ``acme_service``)
  supporting ``dns-01`` (Cloudflare) and ``http-01`` (webroot) validation.

Select the active provider via the Settings ``provider`` field; the rest of
the system (services, API, dashboard) is provider-agnostic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from cryptography import x509

from . import acme_service, crypto_service


class IssuanceError(RuntimeError):
    """Raised when a provider cannot complete issuance (bad config, CA 4xx...)."""


@dataclass
class IssuedCertData:
    cert_pem: str
    serial: str
    fingerprint: str
    not_before: datetime
    not_after: datetime
    #: PKCS#8 PEM private key, when the provider can capture it (local
    #: SimulatedProvider / CA signing). ACME issuance leaves the key with the
    #: ACME account/CA by design, so this stays None there.
    private_key_pem: str | None = None


class Provider(Protocol):
    name: str

    def issue(
        self,
        *,
        cn: str,
        sans: list[str],
        key_type: str,
        validity_days: int,
        issuer: str,
        not_before: datetime | None = None,
    ) -> IssuedCertData: ...

    def renew(
        self,
        *,
        cn: str,
        sans: list[str],
        key_type: str,
        issuer: str,
        not_before: datetime,
        validity_days: int,
    ) -> IssuedCertData: ...


class SimulatedProvider:
    """Issues real certificates, preferring the configured local CA when present.

    Without a CA the leaf is self-signed under the requested issuer label (the
    historical "simulated" behaviour); with a CA context the leaf is generated
    from a fresh key + CSR and signed by the CA, so the whole chain is real.
    """

    name = "simulated"

    def __init__(self, local_ca: LocalCASigningContext | None = None):
        self._local_ca = local_ca

    def issue(
        self,
        *,
        cn: str,
        sans: list[str],
        key_type: str,
        validity_days: int,
        issuer: str,
        not_before: datetime | None = None,
    ) -> IssuedCertData:
        if self._local_ca is not None:
            csr_der, key = crypto_service.generate_csr(cn=cn, sans=sans, key_type=key_type)
            data = crypto_service.sign_csr(
                csr=x509.load_der_x509_csr(csr_der),
                ca_cert_pem=self._local_ca.cert_pem,
                ca_key_pem=self._local_ca.key_pem,
                validity_days=validity_days,
                not_before=not_before,
            )
            data["private_key_pem"] = crypto_service.serialize_private_key_pem(key)
        else:
            data = crypto_service.generate_self_signed(
                cn=cn,
                sans=sans,
                validity_days=validity_days,
                key_type=key_type,
                issuer_name=issuer,
                not_before=not_before,
            )
        return IssuedCertData(
            cert_pem=data["cert_pem"],
            serial=data["serial"],
            fingerprint=data["fingerprint"],
            not_before=data["not_before"],
            not_after=data["not_after"],
            private_key_pem=data.get("private_key_pem"),
        )

    def renew(
        self,
        *,
        cn: str,
        sans: list[str],
        key_type: str,
        issuer: str,
        not_before: datetime,
        validity_days: int,
    ) -> IssuedCertData:
        return self.issue(
            cn=cn,
            sans=sans,
            key_type=key_type,
            validity_days=validity_days,
            issuer=issuer,
            not_before=not_before,
        )


class ACMEProvider:
    """Real ACME issuance (RFC 8555) with DNS-01/HTTP-01 validation."""

    name = "acme"

    def __init__(self, config: acme_service.ACMEConfig):
        self._config = config

    @staticmethod
    def _wrap(exc: Exception) -> IssuanceError:
        return IssuanceError(f"ACME issuance failed: {exc}")

    def issue(
        self,
        *,
        cn: str,
        sans: list[str],
        key_type: str,
        validity_days: int,
        issuer: str,
        not_before: datetime | None = None,
    ) -> IssuedCertData:
        del issuer, not_before
        try:
            data = acme_service.ACMEIssuer(self._config).issue(
                cn=cn, sans=sans, key_type=key_type, validity_days=validity_days
            )
        except acme_service.ACMEError as exc:
            raise self._wrap(exc) from exc
        except Exception as exc:  # noqa: BLE001
            raise self._wrap(exc) from exc
        return IssuedCertData(
            cert_pem=data["cert_pem"],
            serial=data["serial"],
            fingerprint=data["fingerprint"],
            not_before=data["not_before"],
            not_after=data["not_after"],
        )

    def renew(
        self,
        *,
        cn: str,
        sans: list[str],
        key_type: str,
        issuer: str,
        not_before: datetime,
        validity_days: int,
    ) -> IssuedCertData:
        return self.issue(
            cn=cn,
            sans=sans,
            key_type=key_type,
            validity_days=validity_days,
            issuer=issuer,
            not_before=not_before,
        )


@dataclass
class LocalCASigningContext:
    """Plaintext CA key material handed to providers at resolve time."""

    cert_pem: str
    key_pem: str


@dataclass
class ProviderConfig:
    name: str = "simulated"
    acme: acme_service.ACMEConfig | None = None
    local_ca: LocalCASigningContext | None = None


def get_provider(name: str | None = None, config: ProviderConfig | None = None) -> Provider:
    """Return the active provider (dispatch on the configured provider name)."""
    selected = (name or (config.name if config else "simulated") or "simulated").lower()
    if selected == "acme":
        if config is None or config.acme is None or not config.acme.directory_url:
            raise IssuanceError(
                "ACME provider selected but not configured - set an ACME directory URL"
            )
        return ACMEProvider(config.acme)
    return SimulatedProvider(local_ca=config.local_ca if config else None)

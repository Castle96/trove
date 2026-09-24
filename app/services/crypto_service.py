"""Real x509 helpers built on the `cryptography` library.

Certificates issued by the dashboard are genuinely generated (keys + CSRs
signed locally), and imported PEMs are actually parsed so displayed metadata
(CN, SANs, issuer, expiry, serial, fingerprint, key spec) is derived from the
certificate itself rather than guessed.

Also provides CSR generation (for RFC 8555 ACME) and CRL building (for a
locally-configured CA).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509 import ocsp
from cryptography.x509.oid import NameOID

from ..timeutil import utcnow

PrivateKey = ec.EllipticCurvePrivateKey | rsa.RSAPrivateKey | ed25519.Ed25519PrivateKey

#: CA key usage as required of a certificate authority certificate.
CA_KEY_USAGE = x509.KeyUsage(
    digital_signature=True,
    content_commitment=False,
    key_encipherment=False,
    data_encipherment=False,
    key_agreement=False,
    key_cert_sign=True,
    crl_sign=True,
    encipher_only=False,
    decipher_only=False,
)

SUPPORTED_KEY_TYPES = (
    "ECDSA P-256",
    "ECDSA P-384",
    "ECDSA P-521",
    "Ed25519",
    "RSA 2048",
    "RSA 3072",
    "RSA 4096",
)


def generate_key(key_type: str) -> PrivateKey:
    """Create a private key for one of the supported key specs."""
    match key_type:
        case "ECDSA P-256":
            return ec.generate_private_key(ec.SECP256R1())
        case "ECDSA P-384":
            return ec.generate_private_key(ec.SECP384R1())
        case "ECDSA P-521":
            return ec.generate_private_key(ec.SECP521R1())
        case "Ed25519":
            return ed25519.Ed25519PrivateKey.generate()
        case "RSA 2048":
            return rsa.generate_private_key(public_exponent=65537, key_size=2048)
        case "RSA 3072":
            return rsa.generate_private_key(public_exponent=65537, key_size=3072)
        case "RSA 4096":
            return rsa.generate_private_key(public_exponent=65537, key_size=4096)
        case _:
            return ec.generate_private_key(ec.SECP256R1())


def _sign_algorithm(key: PrivateKey) -> hashes.HashAlgorithm | None:
    # Ed25519 signs with a fixed internal hash; pass algorithm=None.
    return None if isinstance(key, ed25519.Ed25519PrivateKey) else hashes.SHA256()


def _hex_colons(hex_str: str) -> str:
    return ":".join(hex_str[i : i + 2] for i in range(0, len(hex_str), 2))


def format_serial(cert: x509.Certificate) -> str:
    return _hex_colons(f"{cert.serial_number:02x}")


def format_fingerprint(cert: x509.Certificate) -> str:
    return _hex_colons(cert.fingerprint(hashes.SHA256()).hex().upper())


def generate_self_signed(
    *,
    cn: str,
    sans: list[str],
    validity_days: int,
    key_type: str,
    issuer_name: str,
    not_before: datetime | None = None,
) -> dict[str, Any]:
    """Generate a real key pair + leaf certificate (self-signed under issuer_name).

    This is what the simulated provider returns: fully valid x509 objects so the
    whole lifecycle pipeline (serial, fingerprint, expiry math, PEM download)
    runs on real data.
    """
    key = generate_key(key_type)
    start = not_before or utcnow()
    end = start + timedelta(days=validity_days)

    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, issuer_name)])

    alt_names = [x509.DNSName(cn)]
    alt_names.extend(x509.DNSName(s) for s in sans if s and s != cn)

    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(end)
        .add_extension(
            x509.SubjectAlternativeName(alt_names)
            if alt_names
            else x509.SubjectAlternativeName([]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
    )
    cert = builder.sign(private_key=key, algorithm=_sign_algorithm(key))
    pem = cert.public_bytes(serialization.Encoding.PEM).decode("ascii")

    return {
        "cert_pem": pem,
        "private_key_pem": serialize_private_key_pem(key),
        "serial": format_serial(cert),
        "fingerprint": format_fingerprint(cert),
        "not_before": start,
        "not_after": end,
    }


def serialize_private_key_pem(key: PrivateKey) -> str:
    """Encrypt-free PEM serialization (the caller is responsible for at-rest protection)."""
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")


def deserialize_private_key_pem(pem: str) -> PrivateKey:
    """Load a PEM PKCS#8 private key (no password protection)."""
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    key = load_pem_private_key(pem.encode("ascii"), password=None)
    if isinstance(key, (ec.EllipticCurvePrivateKey, rsa.RSAPrivateKey, ed25519.Ed25519PrivateKey)):
        return key
    raise ValueError("Unsupported private key type (only EC, RSA, Ed25519 supported)")


def build_pkcs12(*, cert_pem: str, key_pem: str, password: str | None) -> bytes:
    """Bundles a certificate + private key into a PKCS#12 container.

    ``password`` must be non-empty (enforced by the route) or None for an
    unencrypted container. Returns DER bytes suitable for ``application/x-pkcs12``.
    """
    cert = x509.load_pem_x509_certificate(cert_pem.encode("ascii"))
    key = deserialize_private_key_pem(key_pem)
    encryption = (
        serialization.BestAvailableEncryption(password.encode("utf-8"))
        if password
        else serialization.NoEncryption()
    )
    return pkcs12.serialize_key_and_certificates(
        name=b"trove",
        key=key,
        cert=cert,
        cas=None,
        encryption_algorithm=encryption,
    )


def load_csr(csr: str) -> x509.CertificateSigningRequest:
    """Load a PKCS#10 CSR from PEM or DER (base64-encoded) text."""
    data = csr.encode("ascii")
    try:
        if b"-----BEGIN" in data:
            return x509.load_pem_x509_csr(data)
        # Bare DER: accept plain binary-encoded base64 (e.g. URL-arg safe).
        import base64

        return x509.load_der_x509_csr(base64.b64decode(data, validate=True))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Invalid CSR: {exc}") from exc


def build_ca_certificate(
    *,
    cn: str,
    key_type: str,
    validity_days: int,
    parent_cert_pem: str | None = None,
    parent_key_pem: str | None = None,
    is_root: bool,
    not_before: datetime | None = None,
) -> dict[str, Any]:
    """Create a CA certificate, optionally signed by a parent (intermediate).

    Root CAs self-sign; intermediates are signed by ``parent_cert_pem`` /
    ``parent_key_pem``. Returns the CA cert PEM + its private key PEM.
    """
    key = generate_key(key_type)
    start = not_before or utcnow()
    end = start + timedelta(days=validity_days)

    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    issuer = (
        subject
        if is_root or not parent_cert_pem
        else x509.load_pem_x509_certificate(parent_cert_pem.encode("ascii")).subject
    )

    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(end)
        .add_extension(
            x509.BasicConstraints(ca=True, path_length=None if is_root else 0), critical=True
        )
        .add_extension(CA_KEY_USAGE, critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
    )
    signing_key = key if is_root else deserialize_private_key_pem(parent_key_pem or "")
    cert = builder.sign(private_key=signing_key, algorithm=_sign_algorithm(signing_key))

    return {
        "cert_pem": cert.public_bytes(serialization.Encoding.PEM).decode("ascii"),
        "private_key_pem": serialize_private_key_pem(key),
        "serial": format_serial(cert),
        "fingerprint": format_fingerprint(cert),
        "not_before": start,
        "not_after": end,
    }


def sign_csr(
    *,
    csr: x509.CertificateSigningRequest,
    ca_cert_pem: str,
    ca_key_pem: str,
    validity_days: int,
    not_before: datetime | None = None,
    is_ca: bool = False,
) -> dict[str, Any]:
    """Sign a PKCS#10 CSR with the given CA key/cert.

    Returns the leaf (or CA) certificate PEM + metadata. SANs come from the CSR.
    """
    ca_cert = x509.load_pem_x509_certificate(ca_cert_pem.encode("ascii"))
    ca_key = deserialize_private_key_pem(ca_key_pem)
    start = not_before or utcnow()
    end = start + timedelta(days=validity_days)

    builder = (
        x509.CertificateBuilder()
        .subject_name(csr.subject)
        .issuer_name(ca_cert.subject)
        .public_key(csr.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(end)
    )
    try:
        san_ext = csr.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        builder = builder.add_extension(san_ext.value, critical=False)
    except x509.ExtensionNotFound:
        pass
    if is_ca:
        builder = builder.add_extension(
            x509.BasicConstraints(ca=True, path_length=None), critical=True
        )
        builder = builder.add_extension(CA_KEY_USAGE, critical=True)
    else:
        builder = builder.add_extension(
            x509.BasicConstraints(ca=False, path_length=None), critical=True
        )
        builder = builder.add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        builder = builder.add_extension(
            x509.SubjectKeyIdentifier.from_public_key(csr.public_key()), critical=False
        )
        builder = builder.add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_cert.public_key()),
            critical=False,
        )

    cert = builder.sign(private_key=ca_key, algorithm=_sign_algorithm(ca_key))
    return {
        "cert_pem": cert.public_bytes(serialization.Encoding.PEM).decode("ascii"),
        "serial": format_serial(cert),
        "fingerprint": format_fingerprint(cert),
        "not_before": start,
        "not_after": end,
    }


def _reason_flags(reason: str) -> x509.ReasonFlags:
    """Map a user-supplied revocation reason to an X.509 ReasonFlags value.

    Accepts both the API-friendly snake_case (``key_compromise``) and the
    canonical X.509 camelCase value (``keyCompromise``).
    """
    key = (reason or "unspecified").strip().replace(" ", "_").replace("-", "_")
    try:
        return x509.ReasonFlags[key]
    except KeyError:
        try:
            return x509.ReasonFlags(key)
        except ValueError:
            return x509.ReasonFlags.unspecified


def build_ocsp_response(
    *,
    ca_cert_pem: str,
    ca_key_pem: str,
    cert_serial: str,
    cert_status: str = "good",
    revoked_at: datetime | None = None,
    reason: str = "unspecified",
    this_update: datetime | None = None,
    next_update_days: int = 7,
) -> bytes:
    """Build a signed RFC 6960 OCSP response (DER) for a single certificate.

    ``cert_status`` is one of ``good``, ``revoked``, ``unknown``. The response
    is identified via the CA's name/key hashes + the certificate serial, so the
    full leaf certificate is not required.
    """
    ca_cert = x509.load_pem_x509_certificate(ca_cert_pem.encode("ascii"))
    ca_key = deserialize_private_key_pem(ca_key_pem)
    serial_num = (
        _serial_from_hex_with_colons(cert_serial)
        if ":" in cert_serial
        else int(str(cert_serial), 16)
    )
    now = this_update or utcnow()

    def _hash(data: bytes) -> bytes:
        digest = hashes.Hash(hashes.SHA256())
        digest.update(data)
        return digest.finalize()

    builder = ocsp.OCSPResponseBuilder()
    status_key = cert_status.upper() if cert_status in ("good", "revoked", "unknown") else "UNKNOWN"
    builder = builder.add_response_by_hash(
        issuer_name_hash=_hash(ca_cert.subject.public_bytes(serialization.Encoding.DER)),
        issuer_key_hash=_hash(
            ca_cert.public_key().public_bytes(
                serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
            )
        ),
        serial_number=serial_num,
        algorithm=hashes.SHA256(),
        cert_status=ocsp.OCSPCertStatus[status_key],
        this_update=now,
        next_update=now + timedelta(days=next_update_days),
        revocation_time=revoked_at or (now if cert_status == "revoked" else None),
        revocation_reason=_reason_flags(reason) if cert_status == "revoked" else None,
    )
    builder = builder.responder_id(ocsp.OCSPResponderEncoding.NAME, ca_cert)
    builder = builder.certificates([ca_cert])
    response = builder.sign(private_key=ca_key, algorithm=hashes.SHA256())
    return response.public_bytes(serialization.Encoding.DER)


def generate_csr(*, cn: str, sans: list[str], key_type: str) -> tuple[bytes, PrivateKey]:
    """Build a DER-encoded PKCS#10 CSR (+ the key it was signed with).

    Used by the ACME client: the identifier set SANs == order identifiers.
    """
    key = generate_key(key_type)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    alt = [x509.DNSName(s) for s in ([cn, *sans]) if s]
    # Deduplicate while keeping order.
    seen: set[str] = set()
    alt_dedup = [d for d in alt if not (d.value in seen or seen.add(d.value))]
    builder = x509.CertificateSigningRequestBuilder().subject_name(name)
    builder = builder.add_extension(x509.SubjectAlternativeName(alt_dedup), critical=False)
    builder = builder.add_extension(
        x509.BasicConstraints(ca=False, path_length=None), critical=True
    )
    csr = builder.sign(private_key=key, algorithm=_sign_algorithm(key))
    return csr.public_bytes(serialization.Encoding.DER), key


def _serial_from_hex_with_colons(serial: str) -> int:
    return int(serial.replace(":", ""), 16)


def build_crl(
    *,
    revoked: list[dict[str, Any]],
    ca_cert_pem: str,
    ca_key_pem: str,
    this_update: datetime | None = None,
    next_update_days: int = 7,
) -> str:
    """Build a signed CRL (PEM) from (serial, revoked_at, reason) entries.

    ``reason`` is an x509.ReasonFlags value name (e.g. ``key_compromise``,
    ``cessation_of_operation``) or ``unspecified``.
    """
    ca_cert = x509.load_pem_x509_certificate(ca_cert_pem.encode("ascii"))
    ca_key = serialization.load_pem_private_key(ca_key_pem.encode("ascii"), password=None)

    now = this_update or utcnow()
    builder = x509.CertificateRevocationListBuilder()
    builder = builder.issuer_name(ca_cert.subject)
    builder = builder.last_update(now)
    builder = builder.next_update(now + timedelta(days=next_update_days))

    for entry in revoked:
        serial = entry.get("serial", "")
        serial_num = _serial_from_hex_with_colons(serial) if ":" in serial else int(str(serial), 16)
        reason = entry.get("reason") or "unspecified"
        reason_flag = _reason_flags(reason)
        revoked_at = entry.get("revoked_at") or now
        builder = builder.add_revoked_certificate(
            x509.RevokedCertificateBuilder()
            .serial_number(serial_num)
            .revocation_date(revoked_at)
            .add_extension(x509.CRLReason(reason_flag), critical=False)
            .build()
        )

    crl = builder.sign(private_key=ca_key, algorithm=_sign_algorithm(ca_key))
    return crl.public_bytes(serialization.Encoding.PEM).decode("ascii")


def parse_pem(cert_pem: str) -> dict[str, Any]:
    """Parse a PEM certificate and extract its real metadata."""
    cert = x509.load_pem_x509_certificate(cert_pem.encode("ascii"))

    cn_attrs = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    cn = cn_attrs[0].value if cn_attrs else "unknown"

    sans: list[str] = []
    try:
        san_ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        sans = list(san_ext.value.get_values_for_type(x509.DNSName))
    except x509.ExtensionNotFound:
        pass

    is_attrs = cert.issuer.get_attributes_for_oid(NameOID.COMMON_NAME)
    issuer = is_attrs[0].value if is_attrs else "Unknown CA"

    pub = cert.public_key()
    if isinstance(pub, ec.EllipticCurvePublicKey):
        key_type = f"ECDSA {pub.curve.name}"
    elif isinstance(pub, rsa.RSAPublicKey):
        key_type = f"RSA {pub.key_size}"
    elif isinstance(pub, ed25519.Ed25519PublicKey):
        key_type = "Ed25519"
    else:
        key_type = "Unknown"

    return {
        "cn": cn,
        "sans": sans,
        "issuer": issuer,
        "key_type": key_type,
        "serial": format_serial(cert),
        "fingerprint": format_fingerprint(cert),
        "not_before": cert.not_valid_before_utc.replace(tzinfo=None),
        "not_after": cert.not_valid_after_utc.replace(tzinfo=None),
        "cert_pem": cert_pem,
    }

"""OCSP responder (RFC 6960 minimal) over locally-managed CA material.

- ``POST /api/ocsp`` - DER OCSP request body; returns an ``application/ocsp-response``
- ``GET  /api/ocsp`` - ``?serial=...`` convenience returning an ``application/ocsp-response``
  (hex with or without colons); status comes from the trove certificates table.

The responder is unauthenticated on purpose (OCSP clients typically do not
send credentials); it only answers for certificates whose serial it can find in
the local database, otherwise it returns an ``unknown`` response.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from sqlalchemy import select

from ..deps import DB
from ..models import Certificate
from ..services import cert_service, crypto_service

router = APIRouter(prefix="/ocsp", tags=["system"])

OCSP_MEDIA = "application/ocsp-response"


async def _material_or_501(db) -> tuple[str, str]:
    material = await cert_service.get_ocsp_signing_material(db)
    if material is None:
        raise HTTPException(
            status.HTTP_501_NOT_IMPLEMENTED,
            "No OCSP responder configured - create a local CA (POST /api/ca/root) or set "
            "TROVE_CA_CERT_PATH / TROVE_CA_KEY_PATH",
        )
    return material


async def _find_cert(db, serial: str) -> Certificate | None:
    """Locate a certificate by serial (case/colon-insensitive)."""
    needle = serial.replace(":", "").lower()
    rows = (await db.execute(select(Certificate))).scalars().all()
    for cert in rows:
        if cert.serial.replace(":", "").lower() == needle:
            return cert
    return None


async def _respond(ca_cert_pem: str, ca_key_pem: str, serial: str, db) -> Response:
    cert = await _find_cert(db, serial)
    status_ = "unknown"
    revoked_at = None
    reason = "unspecified"
    if cert is not None:
        status_ = "revoked" if cert.revoked else "good"
        if cert.revoked:
            revoked_at = cert.revoked_at
            reason = cert.revocation_reason or "unspecified"
    der = crypto_service.build_ocsp_response(
        ca_cert_pem=ca_cert_pem,
        ca_key_pem=ca_key_pem,
        cert_serial=cert.serial if cert else serial,
        cert_status=status_,
        revoked_at=revoked_at,
        reason=reason,
    )
    return Response(content=der, media_type=OCSP_MEDIA)


@router.post("")
async def ocsp_post(request: Request, db: DB) -> Response:
    """Process a DER OCSP request and return the DER signed response."""
    material = await _material_or_501(db)
    body = await request.body()
    if not body:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Empty OCSP request body")
    try:
        req = _load_ocsp_request(body)
        serial = f"{int(req.serial_number):02x}"
    except Exception as exc:  # noqa: BLE001 - invalid request body
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid OCSP request: {exc}") from exc
    return await _respond(material[0], material[1], serial, db)


@router.get("")
async def ocsp_get(
    db: DB, serial: str = Query("", description="cert serial (hex, colons ok)")
) -> Response:
    """Convenience GET responder by certificate serial."""
    material = await _material_or_501(db)
    if not serial.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "serial query parameter is required")
    return await _respond(material[0], material[1], serial.strip(), db)


def _load_ocsp_request(body: bytes):
    """Parse a DER OCSP request (kept importable for tests)."""
    from cryptography.x509 import ocsp

    try:
        return ocsp.load_der_ocsp_request(body)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"could not parse OCSP request: {exc}") from exc

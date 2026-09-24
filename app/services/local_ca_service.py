"""Local CA (internal PKI) management.

Creates and manages root/intermediate certificate authorities stored in the
trove database. CA private keys are protected at rest through
:mod:`app.services.key_store` (AES-256-GCM); CA certificates are stored in
clear so they can be served/exported freely.

The active CA (see ``cert_service._active_local_ca``) signs locally issued
leaves through the SimulatedProvider, and backs the CRL + OCSP endpoints.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import models
from ..timeutil import utcnow
from . import crypto_service, key_store
from .cert_service import (
    SETTING_LOCAL_CA_ID,
    log_entry,
    set_setting,
)

VALID_KEY_TYPES = set(crypto_service.SUPPORTED_KEY_TYPES)


async def create_root_ca(
    db: AsyncSession,
    *,
    name: str,
    cn: str,
    key_type: str,
    validity_days: int,
) -> models.CaAuthority:
    """Create a self-signed root CA."""
    if key_type not in VALID_KEY_TYPES:
        raise ValueError(f"key_type must be one of: {', '.join(sorted(VALID_KEY_TYPES))}")
    data = crypto_service.build_ca_certificate(
        cn=cn,
        key_type=key_type,
        validity_days=validity_days,
        is_root=True,
    )
    ca = models.CaAuthority(
        name=name.strip() or cn,
        cn=cn,
        kind="root",
        parent_id=None,
        key_type=key_type,
        serial=data["serial"],
        fingerprint=data["fingerprint"],
        not_before=data["not_before"],
        not_after=data["not_after"],
        cert_pem=data["cert_pem"],
        private_key=key_store.encrypt_value(data["private_key_pem"]),
        enabled=True,
        created_at=utcnow(),
    )
    db.add(ca)
    await db.flush()
    await log_entry(db, f"Root CA created: {cn} ({name})", "success")
    return ca


async def create_intermediate_ca(
    db: AsyncSession,
    *,
    name: str,
    cn: str,
    key_type: str,
    validity_days: int,
    parent_id: int,
) -> models.CaAuthority:
    """Create an intermediate CA signed by an existing (root) CA."""
    if key_type not in VALID_KEY_TYPES:
        raise ValueError(f"key_type must be one of: {', '.join(sorted(VALID_KEY_TYPES))}")
    parent = await db.get(models.CaAuthority, parent_id)
    if parent is None:
        raise ValueError("parent CA not found")
    if not parent.private_key:
        raise ValueError("parent CA has no private key stored")
    parent_key = key_store.decrypt_value(parent.private_key)

    data = crypto_service.build_ca_certificate(
        cn=cn,
        key_type=key_type,
        validity_days=validity_days,
        parent_cert_pem=parent.cert_pem,
        parent_key_pem=parent_key,
        is_root=False,
    )
    ca = models.CaAuthority(
        name=name.strip() or cn,
        cn=cn,
        kind="intermediate",
        parent_id=parent.id,
        key_type=key_type,
        serial=data["serial"],
        fingerprint=data["fingerprint"],
        not_before=data["not_before"],
        not_after=data["not_after"],
        cert_pem=data["cert_pem"],
        private_key=key_store.encrypt_value(data["private_key_pem"]),
        enabled=True,
        created_at=utcnow(),
    )
    db.add(ca)
    await db.flush()
    await log_entry(db, f"Intermediate CA created: {cn} signed by {parent.cn}", "success")
    return ca


async def list_cas(db: AsyncSession) -> list[models.CaAuthority]:
    stmt = select(models.CaAuthority).order_by(models.CaAuthority.kind, models.CaAuthority.id)
    return list((await db.execute(stmt)).scalars().all())


async def get_ca(db: AsyncSession, ca_id: int) -> models.CaAuthority | None:
    return await db.get(models.CaAuthority, ca_id)


async def set_active_ca(db: AsyncSession, ca_id: int | None) -> None:
    """Select which CA signs locally issued leaves / OCSP + CRL."""
    if ca_id is not None:
        ca = await db.get(models.CaAuthority, ca_id)
        if ca is None:
            raise ValueError("CA not found")
        if not ca.enabled:
            raise ValueError("CA is disabled")
        await set_setting(db, SETTING_LOCAL_CA_ID, str(ca.id))
    else:
        await set_setting(db, SETTING_LOCAL_CA_ID, "")


async def get_active_ca_id(db: AsyncSession) -> int | None:
    from .cert_service import get_setting

    raw = await get_setting(db, SETTING_LOCAL_CA_ID, "")
    return int(raw) if raw.isdigit() else None


async def set_ca_enabled(db: AsyncSession, ca: models.CaAuthority, enabled: bool) -> None:
    ca.enabled = enabled
    await log_entry(db, f"CA {ca.cn} {'enabled' if enabled else 'disabled'}", "info")


async def delete_ca(db: AsyncSession, ca: models.CaAuthority) -> None:
    """Remove a CA (root also removes its descendants via the parent FK)."""
    ca_id = ca.id
    await db.delete(ca)
    await log_entry(db, f"CA {ca.cn} deleted (id={ca_id})", "warning")


async def sign_csr_with_ca(
    db: AsyncSession,
    *,
    csr_pem_or_der: str,
    ca_id: int | None = None,
    validity_days: int,
) -> dict:
    """Sign an incoming CSR with a specific (or the active) CA.

    Returns the leaf certificate PEM + metadata. The CSR's key is generated by
    the requester, so no private key material is stored here.
    """
    ca = await db.get(models.CaAuthority, ca_id) if ca_id else None
    if ca is None:
        from .cert_service import _active_local_ca

        ca = await _active_local_ca(db)
    if ca is None:
        raise ValueError("no CA available to sign the CSR - create one first")
    if not ca.private_key:
        raise ValueError("CA has no private key stored")
    ca_key = key_store.decrypt_value(ca.private_key)

    csr = crypto_service.load_csr(csr_pem_or_der)
    data = crypto_service.sign_csr(
        csr=csr,
        ca_cert_pem=ca.cert_pem,
        ca_key_pem=ca_key,
        validity_days=validity_days,
    )
    await log_entry(db, f"CSR signed for {csr.subject.rfc4514_string()} by {ca.cn}", "success")
    return {
        "cert_pem": data["cert_pem"],
        "serial": data["serial"],
        "fingerprint": data["fingerprint"],
        "not_before": data["not_before"],
        "not_after": data["not_after"],
        "issuer": ca.cn,
    }

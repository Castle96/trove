"""Issuance approval workflow.

When the ``require_approval`` setting is enabled, ``POST /api/certs`` creates a
pending :class:`~app.models.IssueRequest` instead of issuing directly. Admins
approve (the request is then issued) or deny (recorded for audit).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import models, schemas
from ..timeutil import utcnow
from . import cert_service


def require_approval_enabled() -> bool:
    """Whether issuance requests must be reviewed (env-level gate)."""
    from ..config import get_settings

    return get_settings().require_approval


async def create_request(
    db: AsyncSession,
    payload: schemas.CertCreate,
    requested_by: str | None,
) -> models.IssueRequest:
    req = models.IssueRequest(
        cn=payload.cn,
        sans=payload.sans or [payload.cn],
        issuer=payload.issuer,
        protocol=payload.protocol,
        key_type=payload.key_type,
        validity_days=payload.validity_days,
        auto_renew=payload.auto_renew,
        requested_by=requested_by,
        status="pending",
        created_at=utcnow(),
    )
    db.add(req)
    await db.flush()
    await cert_service.log_entry(
        db,
        f"Issuance request #{req.id} submitted for {payload.cn} by {requested_by or 'anonymous'} "
        f"(pending approval)",
        "info",
    )
    await cert_service.notify_event(
        db,
        "Certificate issuance pending approval",
        f"{payload.cn} requested by {requested_by or 'anonymous'} - approve to issue",
        "info",
    )
    return req


async def list_requests(
    db: AsyncSession, status_filter: str = "all", limit: int = 100
) -> list[models.IssueRequest]:
    stmt = select(models.IssueRequest).order_by(models.IssueRequest.created_at.desc())
    if status_filter in ("pending", "approved", "denied"):
        stmt = stmt.where(models.IssueRequest.status == status_filter)
    rows = (await db.execute(stmt)).scalars().all()
    return list(rows[: max(1, min(limit, 500))])


async def get_request(db: AsyncSession, request_id: int) -> models.IssueRequest | None:
    return await db.get(models.IssueRequest, request_id)


async def approve_request(
    db: AsyncSession, req: models.IssueRequest, decided_by: str | None
) -> models.Certificate:
    """Approve + issue the request; returns the new certificate."""
    if req.status != "pending":
        raise ValueError(f"request is already {req.status}")
    payload = schemas.CertCreate(
        cn=req.cn,
        sans=req.sans or [],
        issuer=req.issuer,
        protocol=req.protocol,
        key_type=req.key_type,
        validity_days=req.validity_days,
        auto_renew=req.auto_renew,
    )
    cert = await cert_service.issue_cert(db, payload)
    req.status = "approved"
    req.decided_at = utcnow()
    req.decided_by = decided_by
    req.cert_id = cert.id
    await cert_service.log_entry(
        db, f"Issuance request #{req.id} approved by {decided_by or 'admin'}", "success"
    )
    await cert_service.notify_event(
        db, "Issuance approved", f"{req.cn} approved and issued ({cert.id})", "success"
    )
    return cert


async def deny_request(
    db: AsyncSession,
    req: models.IssueRequest,
    decided_by: str | None,
    reason: str,
) -> None:
    if req.status != "pending":
        raise ValueError(f"request is already {req.status}")
    req.status = "denied"
    req.decided_at = utcnow()
    req.decided_by = decided_by
    req.denial_reason = (reason or "unspecified").strip()[:255] or "unspecified"
    await cert_service.log_entry(
        db,
        f"Issuance request #{req.id} denied by {decided_by or 'admin'}: {req.denial_reason}",
        "warning",
    )
    await cert_service.notify_event(
        db, "Issuance denied", f"{req.cn} denied ({req.denial_reason})", "warning"
    )

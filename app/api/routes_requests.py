"""Issuance approval workflow endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from .. import models, schemas
from ..deps import DB, Admin, Auth, get_principal
from ..services import providers, requests_service

router = APIRouter(prefix="/requests", tags=["auth"], dependencies=[Depends(get_principal)])


def _read(req: models.IssueRequest) -> schemas.IssueRequestRead:
    return schemas.IssueRequestRead(
        id=req.id,
        cn=req.cn,
        sans=req.sans or [],
        issuer=req.issuer,
        protocol=req.protocol,
        key_type=req.key_type,
        validity_days=req.validity_days,
        auto_renew=req.auto_renew,
        requested_by=req.requested_by,
        status=req.status,
        denial_reason=req.denial_reason,
        cert_id=req.cert_id,
        created_at=req.created_at,
        decided_at=req.decided_at,
        decided_by=req.decided_by,
    )


@router.get("", response_model=list[schemas.IssueRequestRead])
async def list_requests(
    db: DB,
    _auth: Auth,
    filter: str = Query("all", description="all|pending|approved|denied"),
    limit: int = Query(100, ge=1, le=500),
) -> list[schemas.IssueRequestRead]:
    rows = await requests_service.list_requests(db, filter, limit)
    return [_read(r) for r in rows]


@router.get("/{request_id}", response_model=schemas.IssueRequestRead)
async def get_request(request_id: int, db: DB, _auth: Auth) -> schemas.IssueRequestRead:
    req = await requests_service.get_request(db, request_id)
    if not req:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Request not found")
    return _read(req)


@router.post("/{request_id}/approve", response_model=schemas.CertRead | schemas.IssueRequestRead)
async def approve_request(
    request_id: int, db: DB, principal: Admin
) -> schemas.CertRead | schemas.IssueRequestRead:
    """Approve a pending request and issue the certificate."""
    req = await requests_service.get_request(db, request_id)
    if not req:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Request not found")
    try:
        cert = await requests_service.approve_request(db, req, principal.username or principal.via)
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except providers.IssuanceError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    await db.commit()
    await db.refresh(cert)
    return await _read_request_or_cert(db, req, cert)


@router.post("/{request_id}/deny", response_model=schemas.IssueRequestRead)
async def deny_request(
    request_id: int,
    payload: schemas.RequestDecision,
    db: DB,
    principal: Admin,
) -> schemas.IssueRequestRead:
    req = await requests_service.get_request(db, request_id)
    if not req:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Request not found")
    try:
        await requests_service.deny_request(
            db, req, principal.username or principal.via, payload.reason
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    await db.commit()
    await db.refresh(req)
    return _read(req)


async def _read_request_or_cert(
    db, req: models.IssueRequest, cert: models.Certificate
) -> schemas.CertRead | schemas.IssueRequestRead:
    """CertRead after approval (frontend navigates to the issued cert)."""
    from ..services import cert_service

    now = await cert_service.sim_now(db)
    window = await cert_service.get_renew_window(db)
    return cert_service.cert_to_read(cert, now, window)

"""Certificate CRUD + lifecycle endpoints."""

from __future__ import annotations

import csv
import io
import json

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession

from .. import models, schemas
from ..deps import DB, ROLE_RANK, Admin, Auth, Operator, get_principal
from ..services import cert_service, deployment_service, providers, requests_service

router = APIRouter(
    prefix="/certs",
    tags=["certs"],
    dependencies=[Depends(get_principal)],
)


async def _read(db: AsyncSession, cert: models.Certificate) -> schemas.CertRead:
    now = await cert_service.sim_now(db)
    window = await cert_service.get_renew_window(db)
    return cert_service.cert_to_read(cert, now, window)


@router.get("", response_model=list[schemas.CertRead])
async def list_certs(
    db: DB,
    search: str = "",
    filter: str = Query("all", description="all|expiring|expired|autorenew"),
    include_revoked: bool = False,
    include_deleted: bool = False,
    limit: int | None = Query(None, ge=1, le=1000, description="page size (default: all)"),
    offset: int = Query(0, ge=0),
) -> list[schemas.CertRead]:
    return await cert_service.list_certs(
        db, search, filter, include_revoked, include_deleted, limit, offset
    )


@router.post(
    "",
    response_model=schemas.CertRead | schemas.IssueRequestRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_cert(
    payload: schemas.CertCreate, db: DB, principal: Auth
) -> schemas.CertRead | schemas.IssueRequestRead:
    # Approval workflow: any authenticated user may *request* issuance.
    if requests_service.require_approval_enabled():
        req = await requests_service.create_request(
            db, payload, principal.username or principal.via
        )
        await db.commit()
        return schemas.IssueRequestRead(
            id=req.id,
            cn=req.cn,
            sans=req.sans,
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
    # Direct issuance requires operator+ (open mode counts as full access).
    if principal.role != "open" and ROLE_RANK.get(principal.role, 0) < ROLE_RANK["operator"]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Requires 'operator' role or higher")
    try:
        cert = await cert_service.issue_cert(db, payload)
    except providers.IssuanceError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    await db.commit()
    await db.refresh(cert)
    return await _read(db, cert)


@router.post("/batch-renew", response_model=schemas.BatchRenewResponse)
async def batch_renew(db: DB, _op: Operator) -> schemas.BatchRenewResponse:
    result = await cert_service.batch_renew(db)
    await db.commit()
    return result


@router.post("/import", response_model=schemas.CertRead, status_code=status.HTTP_201_CREATED)
async def import_cert(payload: schemas.CertImport, db: DB, _op: Operator) -> schemas.CertRead:
    try:
        cert = await cert_service.import_cert(db, payload)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    await db.commit()
    await db.refresh(cert)
    return await _read(db, cert)


@router.get("/export")
async def export_certs(db: DB, format: str = Query("csv", description="csv|json")) -> Response:
    rows = await cert_service.list_certs(db, include_revoked=True)
    if format == "json":
        body = json.dumps([r.model_dump(mode="json") for r in rows], indent=2)
        return Response(
            content=body,
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="trove-certs.json"'},
        )

    out = io.StringIO()
    writer = csv.DictWriter(
        out,
        fieldnames=[
            "id",
            "cn",
            "sans",
            "issuer",
            "protocol",
            "key_type",
            "serial",
            "fingerprint",
            "not_before",
            "not_after",
            "days_left",
            "status",
            "auto_renew",
            "revoked",
            "revocation_reason",
            "created_at",
        ],
    )
    writer.writeheader()
    for r in rows:
        writer.writerow(
            {
                "id": r.id,
                "cn": r.cn,
                "sans": ",".join(r.sans),
                "issuer": r.issuer,
                "protocol": r.protocol,
                "key_type": r.key_type,
                "serial": r.serial,
                "fingerprint": r.fingerprint,
                "not_before": r.not_before,
                "not_after": r.not_after,
                "days_left": r.days_left,
                "status": r.status,
                "auto_renew": r.auto_renew,
                "revoked": r.revoked,
                "revocation_reason": r.revocation_reason,
                "created_at": r.created_at,
            }
        )
    return Response(
        content=out.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="trove-certs.csv"'},
    )


@router.get("/metrics", response_model=schemas.Metrics)
async def metrics(db: DB) -> schemas.Metrics:
    return await cert_service.get_metrics(db)


@router.get("/{cert_id}", response_model=schemas.CertRead)
async def get_cert(cert_id: str, db: DB) -> schemas.CertRead:
    cert = await cert_service.get_cert(db, cert_id)
    if not cert or cert.revoked:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Certificate not found")
    return await _read(db, cert)


@router.patch("/{cert_id}", response_model=schemas.CertRead)
async def update_cert(
    cert_id: str, payload: schemas.CertUpdate, db: DB, _op: Operator
) -> schemas.CertRead:
    cert = await cert_service.get_cert(db, cert_id)
    if not cert or cert.revoked:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Certificate not found")

    cert.auto_renew = payload.auto_renew
    await cert_service.log_entry(
        db,
        f"Auto-renew rule updated for {cert.cn}: {'ENABLED' if payload.auto_renew else 'DISABLED'}",
        "info",
        cert.id,
    )
    await db.commit()
    await db.refresh(cert)
    return await _read(db, cert)


@router.post("/{cert_id}/renew", response_model=schemas.CertRead)
async def renew_cert(
    cert_id: str,
    db: DB,
    payload: schemas.RenewRequest | None = None,
    _op: Operator = None,
) -> schemas.CertRead:
    cert = await cert_service.get_cert(db, cert_id)
    if not cert or cert.revoked:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Certificate not found")

    reason = payload.reason if payload else "routine"
    if payload and payload.force_challenge:
        await cert_service.log_entry(db, "Bypassing cached ACME authorizations", "acme", cert.id)

    try:
        renewed = await cert_service.renew_cert(db, cert, reason)
    except providers.IssuanceError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    await db.commit()
    await db.refresh(renewed)
    return await _read(db, cert)


@router.post("/{cert_id}/reissue", response_model=schemas.CertRead)
async def reissue_cert(
    cert_id: str,
    db: DB,
    payload: schemas.RenewRequest | None = None,
    _op: Operator = None,
) -> schemas.CertRead:
    return await renew_cert(cert_id, db, payload, _op)


@router.delete("/{cert_id}")
async def delete_cert(
    cert_id: str,
    db: DB,
    revoke: bool = Query(True),
    reason: str = Query("unspecified", max_length=64),
    _op: Operator = None,
) -> dict[str, bool]:
    cert = await cert_service.get_cert(db, cert_id)
    if not cert:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Certificate not found")

    if revoke and not cert.revoked:
        await cert_service.revoke_cert(db, cert, reason)
    else:
        # Soft delete: audit copy retained, hidden from active views.
        await cert_service.soft_delete_cert(db, cert)

    await db.commit()
    return {"ok": True}


@router.post("/{cert_id}/restore", response_model=schemas.CertRead)
async def restore_cert(cert_id: str, db: DB, _op: Operator = None) -> schemas.CertRead:
    cert = await cert_service.get_cert(db, cert_id)
    if not cert:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Certificate not found")
    if not cert.deleted_at:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Certificate is not deleted")
    await cert_service.restore_cert(db, cert)
    await db.commit()
    await db.refresh(cert)
    return await _read(db, cert)


@router.post("/{cert_id}/rotate-key", response_model=schemas.CertRead)
async def rotate_key(cert_id: str, db: DB, _op: Operator = None) -> schemas.CertRead:
    """Reissue with fresh key material (key rotation)."""
    cert = await cert_service.get_cert(db, cert_id)
    if not cert or cert.revoked:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Certificate not found")
    try:
        rotated = await cert_service.rotate_key(db, cert)
    except providers.IssuanceError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    await db.commit()
    await db.refresh(rotated)
    return await _read(db, cert)


@router.get("/{cert_id}/key")
async def download_key(cert_id: str, db: DB, _admin: Admin = None) -> Response:
    """Download the PKCS#8 private key (admin only, only when stored)."""
    cert = await cert_service.get_cert(db, cert_id)
    if not cert or cert.revoked:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Certificate not found")
    key = await cert_service.get_cert_private_key(db, cert)
    if not key:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "No private key stored for this certificate "
            "(ACME-managed or legacy record without key material)",
        )
    filename = f"{cert.cn.replace('*', 'wildcard')}.key".replace("/", "_")
    return PlainTextResponse(
        content=key,
        media_type="application/x-pem-file",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{cert_id}/p12")
async def export_pkcs12(
    cert_id: str,
    db: DB,
    password: str = Query("", min_length=0, max_length=256),
    _admin: Admin = None,
) -> Response:
    """Export certificate + private key as a PKCS#12 container (admin only)."""
    cert = await cert_service.get_cert(db, cert_id)
    if not cert or cert.revoked:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Certificate not found")
    if not cert.cert_pem:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No certificate body stored")
    key = await cert_service.get_cert_private_key(db, cert)
    if not key:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "No private key stored for this certificate - cannot build a PKCS#12 bundle",
        )
    if not password:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "A password is required to protect the PKCS#12 export",
        )
    from ..services import crypto_service

    p12_bytes = crypto_service.build_pkcs12(cert_pem=cert.cert_pem, key_pem=key, password=password)
    filename = f"{cert.cn.replace('*', 'wildcard')}.p12".replace("/", "_")
    return Response(
        content=p12_bytes,
        media_type="application/x-pkcs12",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{cert_id}/pem")
async def download_pem(cert_id: str, db: DB) -> Response:
    cert = await cert_service.get_cert(db, cert_id)
    if not cert or cert.revoked:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Certificate not found")
    if not cert.cert_pem:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "No PEM stored for this certificate (legacy record without certificate body)",
        )
    filename = f"{cert.cn.replace('*', 'wildcard')}.crt".replace("/", "_")
    return PlainTextResponse(
        content=cert.cert_pem,
        media_type="application/x-pem-file",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Certificate deployment (targets are managed under /api/deployments)
# ---------------------------------------------------------------------------


async def _cert_deployments(
    db: AsyncSession, cert: models.Certificate
) -> list[schemas.CertDeploymentRead]:
    out: list[schemas.CertDeploymentRead] = []
    for target in await deployment_service.assigned_targets(db, cert.id):
        last = await deployment_service.last_record(db, cert.id, target.id)
        out.append(
            schemas.CertDeploymentRead(
                target_id=target.id,
                target_name=target.name,
                auto_deploy=target.auto_deploy,
                enabled=target.enabled,
                last_state=last.state if last else None,
                last_detail=last.detail if last else "",
                last_at=last.created_at if last else None,
            )
        )
    return out


def _deploy_result(records: list[models.DeploymentRecord]) -> schemas.DeployResult:
    result = schemas.DeployResult()
    for record in records:
        tid = record.target_id if record.target_id is not None else -1
        if record.state == "success":
            result.deployed.append(tid)
        else:
            result.failed.append(tid)
        result.details[str(tid)] = record.detail
    return result


@router.get("/{cert_id}/deployments", response_model=list[schemas.CertDeploymentRead])
async def list_cert_deployments(cert_id: str, db: DB) -> list[schemas.CertDeploymentRead]:
    cert = await cert_service.get_cert(db, cert_id)
    if not cert or cert.revoked:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Certificate not found")
    return await _cert_deployments(db, cert)


@router.post(
    "/{cert_id}/deployments",
    response_model=list[schemas.CertDeploymentRead],
    status_code=status.HTTP_201_CREATED,
)
async def assign_deployment_target(
    cert_id: str, payload: schemas.CertDeploymentAssign, db: DB, _op: Operator
) -> list[schemas.CertDeploymentRead]:
    cert = await cert_service.get_cert(db, cert_id)
    if not cert or cert.revoked:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Certificate not found")
    target = await deployment_service.get_target(db, payload.target_id)
    if not target:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Deployment target not found")
    await deployment_service.assign_target(db, cert.id, target.id)
    await db.commit()
    return await _cert_deployments(db, cert)


@router.delete("/{cert_id}/deployments/{target_id}")
async def unassign_deployment_target(
    cert_id: str, target_id: int, db: DB, _op: Operator
) -> dict[str, bool]:
    cert = await cert_service.get_cert(db, cert_id)
    if not cert or cert.revoked:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Certificate not found")
    removed = await deployment_service.unassign_target(db, cert.id, target_id)
    if not removed:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Certificate is not assigned to that target")
    await db.commit()
    return {"ok": True}


@router.get("/{cert_id}/deployments/history", response_model=list[schemas.DeploymentRecordRead])
async def deployment_history(
    cert_id: str, db: DB, limit: int = Query(50, ge=1, le=500)
) -> list[schemas.DeploymentRecordRead]:
    cert = await cert_service.get_cert(db, cert_id)
    if not cert or cert.revoked:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Certificate not found")
    return await deployment_service.list_records(db, cert.id, limit)


@router.post("/{cert_id}/deploy", response_model=schemas.DeployResult)
async def deploy_cert_now(cert_id: str, db: DB, _op: Operator) -> schemas.DeployResult:
    """Manually redeploy to every assigned target (regardless of auto_deploy)."""
    cert = await cert_service.get_cert(db, cert_id)
    if not cert or cert.revoked:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Certificate not found")
    records = await deployment_service.deploy_cert_targets(db, cert, reason="manual")
    await db.commit()
    return _deploy_result(records)

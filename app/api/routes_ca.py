"""Local CA (internal PKI) management: create root/intermediate, sign CSRs, download."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import PlainTextResponse

from .. import models, schemas
from ..deps import DB, Admin, Auth, Operator, get_principal
from ..services import local_ca_service

router = APIRouter(prefix="/ca", tags=["certs"], dependencies=[Depends(get_principal)])


async def _read(ca: models.CaAuthority, active_id: int | None) -> schemas.CaRead:
    return schemas.CaRead(
        id=ca.id,
        name=ca.name,
        cn=ca.cn,
        kind=ca.kind,
        parent_id=ca.parent_id,
        key_type=ca.key_type,
        serial=ca.serial,
        fingerprint=ca.fingerprint,
        not_before=ca.not_before,
        not_after=ca.not_after,
        enabled=ca.enabled,
        active=active_id == ca.id,
        created_at=ca.created_at,
    )


@router.get("", response_model=list[schemas.CaRead])
async def list_cas(db: DB, _auth: Auth) -> list[schemas.CaRead]:
    active_id = await local_ca_service.get_active_ca_id(db)
    cas = await local_ca_service.list_cas(db)
    return [await _read(ca, active_id) for ca in cas]


@router.post("/root", response_model=schemas.CaRead, status_code=status.HTTP_201_CREATED)
async def create_root(payload: schemas.CaCreate, db: DB, _op: Operator) -> schemas.CaRead:
    try:
        ca = await local_ca_service.create_root_ca(
            db,
            name=payload.name,
            cn=payload.cn,
            key_type=payload.key_type,
            validity_days=payload.validity_days,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    await db.commit()
    await db.refresh(ca)
    return await _read(ca, await local_ca_service.get_active_ca_id(db))


@router.post("/intermediate", response_model=schemas.CaRead, status_code=status.HTTP_201_CREATED)
async def create_intermediate(
    payload: schemas.CaIntermediateCreate, db: DB, _op: Operator
) -> schemas.CaRead:
    try:
        ca = await local_ca_service.create_intermediate_ca(
            db,
            name=payload.name,
            cn=payload.cn,
            key_type=payload.key_type,
            validity_days=payload.validity_days,
            parent_id=payload.parent_id,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    await db.commit()
    await db.refresh(ca)
    return await _read(ca, await local_ca_service.get_active_ca_id(db))


@router.post("/active", response_model=schemas.CaRead | None)
async def set_active(payload: schemas.CaSetActive, db: DB, _admin: Admin) -> schemas.CaRead | None:
    try:
        await local_ca_service.set_active_ca(db, payload.ca_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    await db.commit()
    active_id = await local_ca_service.get_active_ca_id(db)
    if active_id is None:
        return None
    ca = await db.get(models.CaAuthority, active_id)
    return await _read(ca, active_id) if ca else None


@router.get("/{ca_id}", response_model=schemas.CaRead)
async def get_ca(ca_id: int, db: DB, _auth: Auth) -> schemas.CaRead:
    ca = await local_ca_service.get_ca(db, ca_id)
    if not ca:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "CA not found")
    return await _read(ca, await local_ca_service.get_active_ca_id(db))


@router.get("/{ca_id}/cert")
async def download_ca_cert(ca_id: int, db: DB, _auth: Auth) -> PlainTextResponse:
    ca = await local_ca_service.get_ca(db, ca_id)
    if not ca:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "CA not found")
    return PlainTextResponse(
        content=ca.cert_pem,
        media_type="application/x-pem-file",
        headers={
            "Content-Disposition": f'attachment; filename="{ca.cn.replace("*", "wildcard")}.crt"'
        },
    )


@router.get("/{ca_id}/chain")
async def download_ca_chain(ca_id: int, db: DB, _auth: Auth) -> PlainTextResponse:
    """Download the CA cert plus any ancestor chain (roots above intermediates)."""
    ca = await local_ca_service.get_ca(db, ca_id)
    if not ca:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "CA not found")
    chain: list[str] = [ca.cert_pem]
    seen: set[int] = {ca.id}
    parent_id = ca.parent_id
    while parent_id is not None and parent_id not in seen:
        parent = await db.get(models.CaAuthority, parent_id)
        if parent is None:
            break
        chain.append(parent.cert_pem)
        seen.add(parent.id)
        parent_id = parent.parent_id
    return PlainTextResponse(
        content="\n".join(chain),
        media_type="application/x-pem-file",
        headers={
            "Content-Disposition": f'attachment; filename="{ca.cn.replace("*", "wildcard")}-chain.pem"'
        },
    )


@router.post("/{ca_id}/csr", response_model=schemas.CSRSignResult)
async def sign_csr(ca_id: int, payload: schemas.CSRSignRequest, db: DB, _op: Operator):
    """Sign an external CSR with this CA (CA id takes precedence over the payload's)."""
    try:
        result = await local_ca_service.sign_csr_with_ca(
            db,
            csr_pem_or_der=payload.csr,
            ca_id=ca_id,
            validity_days=payload.validity_days,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    await db.commit()
    return schemas.CSRSignResult(**result)


@router.post("/sign-csr", response_model=schemas.CSRSignResult)
async def sign_csr_active(payload: schemas.CSRSignRequest, db: DB, _op: Operator):
    """Sign a CSR with the active CA."""
    try:
        result = await local_ca_service.sign_csr_with_ca(
            db,
            csr_pem_or_der=payload.csr,
            ca_id=payload.ca_id,
            validity_days=payload.validity_days,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    await db.commit()
    return schemas.CSRSignResult(**result)


@router.patch("/{ca_id}/enabled", response_model=schemas.CaRead)
async def set_enabled(ca_id: int, db: DB, _admin: Admin) -> schemas.CaRead:
    ca = await local_ca_service.get_ca(db, ca_id)
    if not ca:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "CA not found")
    ca.enabled = not ca.enabled
    await db.commit()
    await db.refresh(ca)
    return await _read(ca, await local_ca_service.get_active_ca_id(db))


@router.delete("/{ca_id}")
async def delete_ca(ca_id: int, db: DB, _admin: Admin) -> dict[str, bool]:
    ca = await local_ca_service.get_ca(db, ca_id)
    if not ca:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "CA not found")
    active_id = await local_ca_service.get_active_ca_id(db)
    if active_id == ca.id:
        await local_ca_service.set_active_ca(db, None)
    await local_ca_service.delete_ca(db, ca)
    await db.commit()
    return {"ok": True}

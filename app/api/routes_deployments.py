"""Deployment target CRUD (admin-gated: targets carry SSH credentials)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError

from .. import schemas
from ..deps import DB, Admin, get_principal
from ..services import deployment_service

router = APIRouter(
    prefix="/deployments",
    tags=["deployments"],
    dependencies=[Depends(get_principal)],
)


@router.get("", response_model=list[schemas.DeploymentTargetRead])
async def list_targets(db: DB) -> list[schemas.DeploymentTargetRead]:
    return await deployment_service.list_targets(db)


@router.post(
    "",
    response_model=schemas.DeploymentTargetRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_target(
    payload: schemas.DeploymentTargetCreate, db: DB, _admin: Admin
) -> schemas.DeploymentTargetRead:
    try:
        target = await deployment_service.create_target(db, payload)
    except deployment_service.DeploymentError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Deployment target name already exists"
        ) from exc
    await db.commit()
    await db.refresh(target)
    return target


@router.get("/{target_id}", response_model=schemas.DeploymentTargetRead)
async def get_target(target_id: int, db: DB) -> schemas.DeploymentTargetRead:
    target = await deployment_service.get_target(db, target_id)
    if not target:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Deployment target not found")
    return target


@router.patch("/{target_id}", response_model=schemas.DeploymentTargetRead)
async def update_target(
    target_id: int, payload: schemas.DeploymentTargetUpdate, db: DB, _admin: Admin
) -> schemas.DeploymentTargetRead:
    target = await deployment_service.get_target(db, target_id)
    if not target:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Deployment target not found")
    try:
        target = await deployment_service.update_target(db, target, payload)
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Deployment target name already exists"
        ) from exc
    await db.commit()
    await db.refresh(target)
    return target


@router.delete("/{target_id}")
async def delete_target(target_id: int, db: DB, _admin: Admin) -> dict[str, bool]:
    target = await deployment_service.get_target(db, target_id)
    if not target:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Deployment target not found")
    await deployment_service.delete_target(db, target)
    await db.commit()
    return {"ok": True}

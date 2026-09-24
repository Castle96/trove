"""Admin endpoints for users and API tokens (admin role only)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from .. import models, schemas
from ..deps import DB, Admin
from ..services import auth_service

router = APIRouter(prefix="/users", tags=["users"], dependencies=[])


def _user_read(user: models.User) -> schemas.UserRead:
    return schemas.UserRead(
        id=user.id,
        username=user.username,
        role=user.role,
        is_active=user.is_active,
        created_at=user.created_at,
    )


@router.get("", response_model=list[schemas.UserRead])
async def list_users(db: DB, _admin: Admin) -> list[schemas.UserRead]:
    rows = (await db.execute(select(models.User).order_by(models.User.id))).scalars().all()
    return [_user_read(u) for u in rows]


@router.post("", response_model=schemas.UserRead, status_code=status.HTTP_201_CREATED)
async def create_user(payload: schemas.UserCreate, db: DB, _admin: Admin) -> schemas.UserRead:
    try:
        user = await auth_service.create_user(db, payload.username, payload.password, payload.role)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    await db.commit()
    await db.refresh(user)
    return _user_read(user)


@router.patch("/{user_id}", response_model=schemas.UserRead)
async def update_user(
    user_id: int, payload: schemas.UserUpdate, db: DB, _admin: Admin
) -> schemas.UserRead:
    user = await db.get(models.User, user_id)
    if not user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")

    if payload.password:
        user.password_hash = auth_service.hash_password(payload.password)
    if payload.role:
        if payload.role not in auth_service.VALID_ROLES:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Invalid role")
        user.role = payload.role
    if payload.is_active is not None:
        if user.id == _admin.user_id and not payload.is_active:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "You cannot disable your own account")
        user.is_active = payload.is_active

    await db.commit()
    await db.refresh(user)
    return _user_read(user)


@router.delete("/{user_id}")
async def delete_user(user_id: int, db: DB, _admin: Admin) -> dict[str, bool]:
    user = await db.get(models.User, user_id)
    if not user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    if _admin.user_id and user.id == _admin.user_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You cannot delete your own account")
    await db.delete(user)
    await db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# API tokens (bearer tokens, can be user-bound or standalone service tokens)
# ---------------------------------------------------------------------------


@router.get("/tokens", response_model=list[schemas.TokenRead])
async def list_tokens(db: DB, _admin: Admin) -> list[schemas.TokenRead]:
    rows = (await db.execute(select(models.ApiToken).order_by(models.ApiToken.id))).scalars().all()
    return [
        schemas.TokenRead(
            id=t.id,
            user_id=t.user_id,
            role=t.role,
            label=t.label,
            created_at=t.created_at,
            expires_at=t.expires_at,
            last_used_at=t.last_used_at,
        )
        for t in rows
    ]


@router.post("/tokens", response_model=schemas.TokenCreated, status_code=status.HTTP_201_CREATED)
async def create_token(payload: schemas.TokenCreate, db: DB, _admin: Admin) -> schemas.TokenCreated:
    role = payload.role or "viewer"
    if payload.user_id is not None:
        user = await db.get(models.User, payload.user_id)
        if not user:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
        role = payload.role or user.role
    if role not in auth_service.VALID_ROLES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Invalid role")

    token = auth_service.generate_token()
    entry = models.ApiToken(
        token_hash=auth_service.hash_token(token),
        user_id=payload.user_id,
        role=role,
        label=payload.label.strip(),
    )
    db.add(entry)
    await db.flush()
    await db.commit()
    return schemas.TokenCreated(token=token, id=entry.id, role=role, label=entry.label)


@router.delete("/tokens/{token_id}")
async def delete_token(token_id: int, db: DB, _admin: Admin) -> dict[str, bool]:
    token = await db.get(models.ApiToken, token_id)
    if not token:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Token not found")
    await db.delete(token)
    await db.commit()
    return {"ok": True}

"""Authentication endpoints: login, logout, current identity."""

from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, status
from sqlalchemy import select

from .. import schemas
from ..deps import DB, Auth
from ..models import ApiToken
from ..services import auth_service

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=schemas.LoginResponse)
async def login(payload: schemas.LoginRequest, db: DB) -> schemas.LoginResponse:
    user = await auth_service.get_user_by_username(db, payload.username)
    if (
        not user
        or not user.is_active
        or not auth_service.verify_password(payload.password, user.password_hash)
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid username or password")
    token = auth_service.generate_token()
    db.add(
        ApiToken(
            token_hash=auth_service.hash_token(token),
            user_id=user.id,
            role=user.role,
            label="login",
        )
    )
    await db.commit()
    return schemas.LoginResponse(token=token, username=user.username, role=user.role, via="token")


@router.post("/logout")
async def logout(
    db: DB,
    principal: Auth,
    authorization: str | None = Header(default=None),
) -> dict[str, bool]:
    if principal.via == "token" and authorization and authorization.startswith("Bearer "):
        bearer = authorization[7:].strip()
        token = (
            await db.execute(
                select(ApiToken).where(ApiToken.token_hash == auth_service.hash_token(bearer))
            )
        ).scalar_one_or_none()
        if token:
            await db.delete(token)
            await db.commit()
    return {"ok": True}


@router.get("/me", response_model=schemas.PrincipalInfo)
async def me(principal: Auth) -> schemas.PrincipalInfo:
    return schemas.PrincipalInfo(
        authenticated=principal.authenticated,
        username=principal.username,
        role=principal.role,
        via=principal.via,
    )

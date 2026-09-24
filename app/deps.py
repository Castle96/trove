"""Authentication: legacy shared API key + multi-user bearer tokens with roles.

Three modes, resolved in order:

1. ``TROVE_API_KEY`` (legacy) -- ``Authorization: Bearer <key>`` grants admin.
2. User bearer tokens -- created via ``POST /api/auth/login`` or the admin
   token endpoint; scoped to the user's role (or an explicit token role).
3. Open mode -- when neither an API key nor any users are configured, the API
   is open (fine for an isolated LAN lab).

Roles (ascending): ``viewer`` < ``operator`` < ``admin``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings
from .database import get_db
from .models import ApiToken, User
from .services.auth_service import hash_token
from .timeutil import utcnow

DB = Annotated[AsyncSession, Depends(get_db)]

ROLE_RANK = {"viewer": 1, "operator": 2, "admin": 3}


@dataclass
class Principal:
    role: str  # "admin" | "operator" | "viewer" | "open"
    user_id: int | None = None
    username: str | None = None
    via: str = "open"  # "apikey" | "token" | "open"

    @property
    def authenticated(self) -> bool:
        return self.role != "open"


def _unauthorized(detail: str = "Missing or invalid credentials") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_principal(
    authorization: str | None = Header(default=None),
    db: DB = None,
) -> Principal:
    settings = get_settings()
    bearer = (authorization or "").strip()
    if bearer.startswith("Bearer "):
        bearer = bearer[7:].strip()
    else:
        bearer = ""

    # 1. Legacy shared API key -> admin.
    if settings.api_key and bearer == settings.api_key:
        return Principal(role="admin", username="api-key", via="apikey")

    # 2. User bearer token.
    if bearer:
        token = (
            await db.execute(select(ApiToken).where(ApiToken.token_hash == hash_token(bearer)))
        ).scalar_one_or_none()
        if token:
            if token.expires_at and token.expires_at <= utcnow():
                raise _unauthorized("Token expired")
            user = await db.get(User, token.user_id) if token.user_id else None
            if token.user_id and (user is None or not user.is_active):
                raise _unauthorized("User account is disabled")
            token.last_used_at = utcnow()
            await db.commit()
            role = token.role or (user.role if user else "viewer")
            return Principal(
                role=role,
                user_id=token.user_id,
                username=user.username if user else None,
                via="token",
            )

    # 3. Open mode only when no security is configured.
    if not settings.api_key:
        user_count = await db.scalar(select(func.count()).select_from(User))
        if user_count == 0:
            return Principal(role="open", via="open")

    raise _unauthorized()


Auth = Annotated[Principal, Depends(get_principal)]


def require_role(min_role: str):
    """Dependency factory: reject principals below a role threshold.

    Reuses :func:`get_principal` so the lookup runs once per request.
    """

    async def _dep(principal: Annotated[Principal, Depends(get_principal)]) -> Principal:
        if principal.role == "open":
            return principal  # open access implies full privileges locally
        if ROLE_RANK.get(principal.role, 0) < ROLE_RANK[min_role]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires '{min_role}' role or higher",
            )
        return principal

    return _dep


Admin = Annotated[Principal, Depends(require_role("admin"))]
Operator = Annotated[Principal, Depends(require_role("operator"))]

"""Shared API dependencies — adapted for the unified Trove app.

The original Dockwatch token model (``DOCKWATCH_AUTH_TOKEN`` /
``DOCKWATCH_AUTH_READONLY_TOKENS``) is superseded by Trove's unified
principal system (coordinated legacy API key, user bearer tokens, roles):

* ``admin``/``operator`` principals (or open mode) map to Dockwatch's
  ``admin`` role: full read **and** write access.
* ``viewer`` principals map to Dockwatch's ``read`` role: they may call
  read endpoints but are rejected (403) from mutating endpoints via the
  :func:`require_write` dependency.
* When no security is configured (Trove open mode), the API is open and
  both dependencies are no-ops.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import get_principal

DB = Annotated[AsyncSession, Depends(get_db)]

Role = Literal["admin", "read"] | None


async def verify_token(
    authorization: str | None = Header(default=None),
    x_dockwatch_token: str | None = Header(default=None, alias="X-Dockwatch-Token"),
    db: DB = None,
) -> Role:
    """Resolve the caller's role from a unified Trove principal.

    Accepts ``Authorization: Bearer <token>`` (Trove API key or user token)
    or the legacy ``X-Dockwatch-Token`` header (mapped to the same bearer
    flow). Returns ``"admin"`` for admin/operator principals or open mode,
    ``"read"`` for viewers, and ``None`` before any authentication is
    configured.
    """
    token_header = authorization
    if not token_header and x_dockwatch_token:
        token_header = "Bearer " + x_dockwatch_token.strip()

    principal = await get_principal(authorization=token_header, db=db)
    if not principal.authenticated:
        return None  # open API

    if principal.role in ("admin", "operator"):
        return "admin"
    return "read"


async def require_write(role: Role = Depends(verify_token)) -> None:
    """Reject read-only tokens/principals from mutating endpoints."""
    if role is not None and role != "admin":
        raise HTTPException(status_code=403, detail="Read-only token cannot modify resources")


__all__ = ["Role", "verify_token", "require_write"]

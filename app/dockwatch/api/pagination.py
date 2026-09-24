"""Shared pagination helpers (limit/offset + generic envelope)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends, Query
from pydantic import BaseModel
from sqlalchemy import Select, func, select


class Pagination(BaseModel):
    """Validated limit/offset pair returned by the pagination dependency."""

    limit: int = 50
    offset: int = 0


def pagination_params(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Pagination:
    """FastAPI dependency: ``pagination: Pagination = Depends(pagination_params)``."""
    return Pagination(limit=limit, offset=offset)


# Reusable Annotated dependency:
#   pagination: PaginationParams
PaginationParams = Annotated[Pagination, Depends(pagination_params)]

# Annotated scalar aliases for inline use (keeps backward-compat list responses):
#   def list_sites(db: DB, limit: LimitQuery = 50, offset: OffsetQuery = 0)
LimitQuery = Annotated[int, Query(ge=1, le=200)]
OffsetQuery = Annotated[int, Query(ge=0)]

# Wider bound for high-volume Docker listings (max 500).
DockerLimitQuery = Annotated[int, Query(ge=1, le=500)]


class PaginatedResponse[T](BaseModel):
    """Generic envelope: ``{"items": [...], "total": N, "limit": L, "offset": O}``."""

    items: list[T]
    total: int
    limit: int
    offset: int


def paginate_select(stmt: Select[Any], limit: int, offset: int) -> Select[Any]:
    """Apply ``LIMIT/OFFSET`` to a SQLAlchemy select (clamps to valid bounds)."""
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    return stmt.limit(limit).offset(offset)


def paginate_list[T](items: list[T], limit: int, offset: int) -> list[T]:
    """Slice an in-memory list (Docker/fleet results) by offset/limit."""
    offset = max(0, offset)
    limit = max(1, limit)
    return items[offset : offset + limit]


def count_select(stmt: Select[Any]) -> Select[Any]:
    """Wrap a select in ``SELECT count(*)`` for total-row queries.

    Usage::

        total = await db.scalar(count_select(stmt)) or 0
        rows = await db.scalars(paginate_select(stmt.order_by(...), limit, offset))
    """
    subq = stmt.order_by(None).limit(None).offset(None).subquery()
    return select(func.count()).select_from(subq)

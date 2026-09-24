"""Container resource ranking — top-N by avg CPU over trailing window."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Query

from app.dockwatch.services.container_stats_persistence import fetch_container_ranking

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["docker"])

__all__ = ["router"]


@router.get("/docker/containers/ranking")
async def container_ranking(
    range: int = Query(default=300, ge=60, le=604800, description="Trailing window (s)"),
    limit: int = Query(default=10, ge=1, le=50),
    endpoint_id: int | None = Query(
        default=None, description="Filter to one endpoint (None = local)"
    ),
) -> dict[str, Any]:
    """Top containers by average CPU (then memory) over the trailing window.

    Returns [{short_id, name, image, avg_cpu, avg_mem, peak_cpu, net_rx, net_tx}].
    """
    try:
        rows = await fetch_container_ranking(
            range_seconds=range, limit=limit, endpoint_id=endpoint_id
        )
    except Exception as exc:
        logger.warning("container ranking query failed: %s", exc)
        rows = []
    return {"range_seconds": range, "limit": limit, "endpoint_id": endpoint_id, "containers": rows}

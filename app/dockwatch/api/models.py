"""REST API for the per-node smol-model runtimes (Models view)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from app.dockwatch.config import get_settings
from app.dockwatch.services.llm_service import fleet_overview

router = APIRouter(prefix="/api/models", tags=["models"])


@router.get("/fleet")
async def models_fleet(
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    """Per-node model runtime status (local smol models fan-out)."""
    overview = await fleet_overview()
    nodes = overview.get("nodes", [])
    overview["nodes"] = nodes[:limit]
    return overview


@router.get("/config")
async def models_config() -> dict[str, Any]:
    """The configured model nodes (name/url/engine) for display."""
    nodes = [n.model_dump() for n in get_settings().model_nodes]
    return {"nodes": nodes}

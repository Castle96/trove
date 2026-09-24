"""Prometheus metrics endpoint.

Deliberately NOT behind ``verify_token``: Prometheus scrapers don't send
bearer tokens, and the endpoint only exposes aggregate counters/histograms
(safe to whitelist at the proxy if desired).
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import Response

from app.dockwatch.services.metrics import render_metrics

router = APIRouter(prefix="/api", tags=["metrics"])


@router.get("/metrics")
async def metrics() -> Response:
    """Return the Prometheus text exposition format."""
    return Response(content=render_metrics(), media_type="text/plain; version=0.0.4")

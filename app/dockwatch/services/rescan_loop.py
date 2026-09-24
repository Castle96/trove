"""Periodic Trivy rescan of stale cached image scans.

A single asyncio task (started in the FastAPI lifespan next to the
host-metrics sampler) wakes hourly and force-rescans cached rows older than
``settings.trivy_cache_hours``. Missing Trivy binary or scan failures are
logged, never fatal.
"""

from __future__ import annotations

import asyncio
import logging
import time

from app.dockwatch.config import get_settings

logger = logging.getLogger(__name__)

#: How often the stale check runs (the freshness bar itself is trivy_cache_hours).
RESCAN_INTERVAL_SECONDS = 3600.0


async def _rescan_loop(interval: float | None = None) -> None:
    poll = interval if interval is not None else RESCAN_INTERVAL_SECONDS
    while True:
        try:
            from app.dockwatch.services.trivy_service import list_scans, scan_image

            stale_after_ms = int(get_settings().trivy_cache_hours * 3600 * 1000)
            now_ms = int(time.time() * 1000)
            for row in await list_scans():
                if now_ms - row.scanned_at_ms >= stale_after_ms:
                    try:
                        await scan_image(row.image_ref, endpoint_id=row.endpoint_id, force=True)
                        logger.info("rescan completed for %s", row.image_ref)
                    except Exception:
                        logger.exception("rescan failed for %s", row.image_ref)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("vulnerability rescan tick failed")
        await asyncio.sleep(poll)


async def start_rescan_loop() -> asyncio.Task[None]:
    """Start the rescan task; returns the task so callers can cancel it."""
    return asyncio.create_task(_rescan_loop())

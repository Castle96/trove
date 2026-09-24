"""Background sampler that keeps host metrics fresh and persisted.

A single asyncio task (started in the FastAPI lifespan) samples host metrics
every ``settings.monitor_sampler_interval`` seconds. Samples are buffered and
flushed to SQLite in bulk every ``FLUSH_EVERY_TICKS`` ticks to reduce write
pressure, and old rows are pruned on a rolling schedule. On startup the
sampler restores recent history from the database so the start-page graphs
survive restarts.
"""

from __future__ import annotations

import asyncio
import logging

from app.dockwatch.config import get_settings
from app.dockwatch.services.alerter import notify_anomaly
from app.dockwatch.services.metrics import monitor_anomalies_total, monitor_ticks_total
from app.dockwatch.services.monitor_persistence import (
    fetch_recent,
    insert_samples,
    prune_older_than,
)
from app.dockwatch.services.monitor_service import MonitorSampleDict, monitor_service

logger = logging.getLogger(__name__)

#: Persist this many consecutive samples in one bulk insert.
FLUSH_EVERY_TICKS = 5


async def _sampler_loop(
    interval: float | None = None,
    flush_every: int = FLUSH_EVERY_TICKS,
) -> None:
    settings = get_settings()
    if interval is None:
        interval = settings.monitor_sampler_interval

    # Restore history so graphs render immediately after a restart.
    try:
        recent = await fetch_recent(limit=300)
        monitor_service.set_history(recent)
        logger.info("restored %d persisted monitor samples", len(recent))
    except Exception:
        logger.exception("failed to restore monitor history from database")

    pending: list[MonitorSampleDict] = []
    ticks_since_flush = 0
    next_prune = asyncio.get_running_loop().time()
    while True:
        try:
            sample = monitor_service.sample()
            monitor_ticks_total.labels(metric="cpu").inc()
            monitor_ticks_total.labels(metric="mem").inc()
            if sample["cpu_anomaly"]:
                monitor_anomalies_total.labels(metric="cpu").inc()
            if sample["mem_anomaly"]:
                monitor_anomalies_total.labels(metric="mem").inc()
            if sample["cpu_anomaly"] or sample["mem_anomaly"]:
                try:
                    notified = await notify_anomaly(sample)
                    if notified:
                        logger.info("anomaly alert sent for %s", ",".join(notified))
                except Exception:
                    logger.exception("anomaly notification failed")
            pending.append(sample)
            ticks_since_flush += 1
            if ticks_since_flush >= flush_every:
                await insert_samples(pending)
                pending.clear()
                ticks_since_flush = 0
            if asyncio.get_running_loop().time() >= next_prune:
                removed = await prune_older_than(settings.monitor_retention_days)
                logger.debug("pruned %d old monitor samples", removed)
                next_prune = asyncio.get_running_loop().time() + 3600
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("monitor sampler tick failed")
        await asyncio.sleep(interval)


async def start_monitor_sampler() -> asyncio.Task[None]:
    """Start the sampler task; returns the task so callers can cancel it."""
    return asyncio.create_task(_sampler_loop())

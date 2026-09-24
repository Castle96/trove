"""Outbound webhook alerting for host-metric anomalies.

Contract
--------
When the background sampler produces a ``MonitorSampleDict`` whose
``cpu_anomaly`` / ``mem_anomaly`` flags are set (z-score beyond threshold),
:func:`notify_anomaly` POSTs one JSON event per flagged metric to
``settings.alert_webhook_url`` (ntfy, Slack-compatible, or any generic HTTP
receiver). Each metric ("cpu"/"mem") has an independent cooldown
(``settings.alert_cooldown_seconds``) so a sustained spike does not spam the
endpoint; the cooldown is only refreshed after a successful 2xx response, so
a failing webhook never suppresses a later retry.

``notify_anomaly`` never raises: every individual request is guarded and
logged, and the function returns the list of metric names actually notified
(empty when alerting is disabled, no webhook is configured, or the sample has
no anomaly flags set). Callers such as ``monitor_loop._sampler_loop`` can
treat it as fire-and-forget.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from datetime import UTC, datetime

import httpx

from app.dockwatch.config import get_settings
from app.dockwatch.services.metrics import inc_alert_sent
from app.dockwatch.services.monitor_service import MonitorSampleDict

logger = logging.getLogger(__name__)

#: ``time.monotonic()`` of the last successful webhook, keyed by metric name.
_last_sent: dict[str, float] = {}
#: Guards ``_last_sent`` (the sampler loop is single-threaded, but be safe).
_lock = threading.Lock()


def _within_cooldown(metric: str, cooldown: float, now: float | None = None) -> bool:
    """True when ``metric`` was notified more recently than ``cooldown``.

    A metric that has never been notified is never within the cooldown, so the
    first anomaly always fires.
    """
    if cooldown <= 0:
        return False
    if now is None:
        now = time.monotonic()
    last = _last_sent.get(metric)
    return last is not None and now - last < cooldown


def _payload(metric: str, sample: MonitorSampleDict) -> dict[str, object]:
    """Build the JSON payload for one anomaly webhook."""
    if metric == "cpu":
        value: float = sample["cpu"]
        z_score: float = sample["z_cpu"]
    else:
        value = sample["memory"]["percent"]
        z_score = sample["z_mem"]
    return {
        "event": "anomaly",
        "metric": metric,
        "value": value,
        "z_score": z_score,
        "timestamp": datetime.fromtimestamp(sample["t"] / 1000, tz=UTC).isoformat(),
        "hostname": socket.gethostname(),
    }


async def notify_anomaly(sample: MonitorSampleDict) -> list[str]:
    """POST anomaly webhooks for flagged metrics, honoring per-metric cooldowns.

    Returns the list of metric names ("cpu"/"mem") whose webhook was accepted
    (HTTP 2xx) during this call. Never raises.
    """
    settings = get_settings()
    if not settings.enable_alerting or not settings.alert_webhook_url:
        return []
    if not sample["cpu_anomaly"] and not sample["mem_anomaly"]:
        return []

    anomalies = {"cpu": sample["cpu_anomaly"], "mem": sample["mem_anomaly"]}
    notified: list[str] = []
    for metric in ("cpu", "mem"):
        if not anomalies[metric]:
            continue
        if _within_cooldown(metric, settings.alert_cooldown_seconds):
            continue
        try:
            async with httpx.AsyncClient(timeout=settings.alert_webhook_timeout) as client:
                response = await client.post(
                    settings.alert_webhook_url, json=_payload(metric, sample)
                )
        except Exception as exc:
            logger.warning("anomaly webhook failed for %s: %s", metric, exc)
            continue
        if not 200 <= response.status_code < 300:
            logger.warning(
                "anomaly webhook rejected with status %s for %s", response.status_code, metric
            )
            continue
        with _lock:
            _last_sent[metric] = time.monotonic()
        inc_alert_sent()
        notified.append(metric)
    return notified

"""Prometheus-format metrics: counters, gauges, histogram and the ASGI middleware.

Pure-ASGI middleware (no ``BaseHTTPMiddleware``) mirroring the pattern already
used by ``RequestIDMiddleware`` in ``app.main``. Each HTTP request records one
``request_count`` sample (normalized method/path + status) and one
``request_latency`` histogram sample; label cardinality stays bounded by
collapsing variable path segments (hex IDs, UUIDs, digit runs) to ``{id}``.
"""

from __future__ import annotations

import re
import time
from typing import Any

from prometheus_client import Counter, Gauge, Histogram, generate_latest

#: HTTP requests by normalized method/path and response status. The ``method``
#: label intentionally holds the normalized *path* (not the HTTP verb) so
#: route-level cardinality stays bounded and dashboards can group by route.
request_count = Counter(
    "dockwatch_http_requests_total",
    "HTTP requests",
    ["method", "status"],
)
#: HTTP request wall-clock duration in seconds (start to ``http.response.start``).
request_latency = Histogram(
    "dockwatch_http_request_duration_seconds",
    "HTTP request duration",
    buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10],
)
#: Anomaly alerts delivered to the configured webhook.
alert_sent_total = Counter("dockwatch_alerts_sent_total", "Anomaly alerts delivered")
#: 1 if the Docker engine is reachable (maintained by the Docker service layer).
docker_available = Gauge("dockwatch_docker_available", "1 if the Docker engine is reachable")
#: Monitor sampler ticks, labeled by metric (cpu / mem / ...).
monitor_ticks_total = Counter("dockwatch_monitor_ticks_total", "Monitor sampler ticks", ["metric"])
#: Monitor anomalies detected, labeled by metric (cpu / mem / ...).
monitor_anomalies_total = Counter(
    "dockwatch_monitor_anomalies_total", "Monitor anomalies detected", ["metric"]
)

#: A dynamic path segment: a UUID (8-4-4-4-12 hex), a pure digit run, or a
#: long hex run (Docker/Trivy IDs and prefixes, e.g. ``abcd1234``).
_SEGMENT_RE = re.compile(
    r"(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|\d+|[0-9a-f]{8,})",
    re.IGNORECASE,
)


def _normalize_path(path: str) -> str:
    """Collapse dynamic segments (UUIDs, digit runs, hex IDs) to ``{id}``.

    Keeps metric label cardinality bounded for routes with variable segments,
    e.g. ``/docker/containers/abcd1234/logs`` becomes
    ``/docker/containers/{id}/logs`` while ``/api/health`` stays intact.
    """
    segments = path.split("/")
    for index, segment in enumerate(segments):
        if segment and _SEGMENT_RE.fullmatch(segment):
            segments[index] = "{id}"
    return "/".join(segments)


class MetricsMiddleware:
    """Record per-request counters and the latency histogram.

    Pure-ASGI (no ``BaseHTTPMiddleware``) matching the codebase's existing
    middleware pattern. Non-HTTP scopes (lifespan, websockets) pass through
    untouched. Duration is measured from request start to the
    ``http.response.start`` message.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        start = time.perf_counter()
        status_holder: dict[str, int] = {"status": 500}

        async def send_wrapper(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = int(message.get("status") or 500)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            status = status_holder["status"]
            method = _normalize_path(str(scope.get("path", "")))
            request_count.labels(method=method, status=str(status)).inc()
            request_latency.observe(time.perf_counter() - start)


def render_metrics() -> str:
    """Serialize all registered metrics in Prometheus text exposition format."""
    return generate_latest().decode()


def inc_alert_sent() -> None:
    """Record one delivered anomaly alert."""
    alert_sent_total.inc()

"""Application configuration.

Settings are read from environment variables (prefix ``DOCKWATCH_``) or a
local ``.env`` file, and validated with pydantic-settings.
"""

import os
from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ModelNode(BaseModel):
    """One configured LLM runtime node for the Models view.

    Validated at startup so a typo in ``DOCKWATCH_MODEL_NODES`` fails fast
    instead of silently producing an unreachable node at poll time.
    """

    name: str
    url: str
    engine: Literal["ollama", "llamacpp"] = "ollama"

    @field_validator("url")
    @classmethod
    def _normalize_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value.startswith(("http://", "https://")):
            raise ValueError("model node url must start with http:// or https://")
        return value


def _default_docker_socket_fallbacks() -> list[str]:
    """Socket URLs probed after ``docker_host`` when Docker is unreachable.

    Podman exposes a Docker-compatible API socket, so both the rootful
    (``/run/podman/podman.sock``) and rootless (per-user runtime dir) sockets
    are natural fallbacks — Dockwatch keeps working on Podman-only machines.
    """
    sockets = ["unix:///run/podman/podman.sock"]
    xdg_runtime = os.environ.get("XDG_RUNTIME_DIR")
    if xdg_runtime:
        sockets.append(f"unix://{xdg_runtime}/podman/podman.sock")
    elif hasattr(os, "getuid"):
        sockets.append(f"unix:///run/user/{os.getuid()}/podman/podman.sock")
    return sockets


class Settings(BaseSettings):
    """Runtime settings for Dockwatch."""

    model_config = SettingsConfigDict(
        env_prefix="DOCKWATCH_", env_file=".env", extra="ignore", case_sensitive=False
    )

    app_name: str = "Dockwatch"
    #: ASGI host/port (used when the app is started programmatically).
    host: str = "0.0.0.0"
    port: int = 8000
    #: Root log level for the application logger.
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    #: CORS origins allowed to call the API (empty = same-origin only).
    cors_origins: list[str] = []
    #: Primary bearer token protecting the /api/* routers. This token has the
    #: ``admin`` role: full read + write access to every router. When unset the
    #: API is open (probes like /api/health and /api/ready stay unauthenticated).
    auth_token: str | None = None
    #: Additional tokens granted the ``read`` role: they may call GET/read
    #: endpoints but are rejected (403) from any mutating endpoint (container
    #: actions, deploys, pulls, removes, inventory/fleet/scan writes, ingest).
    #: Override with a JSON list, e.g.
    #: ``DOCKWATCH_AUTH_READONLY_TOKENS='["token-a", "token-b"]'``.
    auth_readonly_tokens: list[str] = []
    #: Serve the interactive API docs (/docs, /redoc, /openapi.json). When the
    #: API is protected behind ``auth_token`` consider disabling these to avoid
    #: leaking the API surface to unauthenticated visitors.
    enable_openapi: bool = True
    #: Master switch for the response-hardening headers (CSP, nosniff, ...).
    security_headers: bool = True
    #: Optional override for the ``Content-Security-Policy`` header. When unset
    #: a default tuned for the bundled SPA (self-only, inline scripts/styles,
    #: Google Fonts) is used. Set to an empty string to send no CSP at all.
    csp_override: str | None = None
    #: Host header values accepted by TrustedHostMiddleware. Defaults to ``*``
    #: (accept any host) for drop-in self-hosting; set this to your domain(s)
    #: to enable host-header validation, e.g. ``["dockwatch.example.com"]``.
    allowed_hosts: list[str] = ["*"]
    #: Per-request wall-clock budget in seconds before the request is aborted
    #: with a 504. Applies to every route via the access-log middleware.
    request_timeout: float = 30.0
    #: Maximum seconds to wait for in-flight background work during shutdown
    #: before abandoning it (thread-pool Docker/psutil/Trivy calls cannot be
    #: cancelled, so shutdown must not block on them indefinitely).
    shutdown_grace_seconds: float = 3.0
    database_url: str = "sqlite+aiosqlite:///./dockwatch.db"
    #: Create missing tables on startup via ``Base.metadata.create_all``. On by
    #: default (hypervisor + container): ``create_all`` is safe to run every
    #: boot because it only adds missing tables and never mutates existing ones.
    #: Set to false when the schema is managed by an external migration tool.
    create_all_on_startup: bool = True
    #: Connection pool sizing (ignored for SQLite, which uses a single writer).
    database_pool_size: int = 5
    database_max_overflow: int = 10
    #: Location of the Docker socket (or a TCP endpoint such as tcp://host:2375).
    #: This is tried first; when it cannot be reached the fallback sockets below
    #: are probed in order (Podman by default).
    docker_host: str = "unix:///var/run/docker.sock"
    #: Ordered list of Docker-API-compatible sockets tried after ``docker_host``
    #: when it is unreachable. Defaults to the Podman sockets (rootful + rootless).
    #: Override with a JSON list, e.g. ``DOCKWATCH_DOCKER_SOCKET_FALLBACKS='["unix:///run/podman/podman.sock"]'``.
    docker_socket_fallbacks: list[str] = _default_docker_socket_fallbacks()
    #: Connection timeout for Docker API calls in seconds.
    docker_timeout: float = 5.0
    #: Maximum number of containers for which per-container stats are collected
    #: in a single listing request (to keep the endpoint responsive).
    max_stats_containers: int = 50
    #: Tail this many log lines when fetching container logs.
    logs_tail: int = 200
    #: Background sampler that persists host metrics to SQLite. Disable in
    #: tests/CI where only the HTTP endpoints are exercised.
    enable_monitor_sampler: bool = True
    #: Sampler tick in seconds.
    monitor_sampler_interval: float = 2.0
    #: Samples older than this many days are pruned from the database.
    monitor_retention_days: int = 7
    #: |z-score| above which a CPU/memory sample is flagged as an anomaly.
    monitor_anomaly_zscore: float = 2.5
    #: Max concurrent polls when fan-out spans multiple endpoints.
    fleet_poll_concurrency: int = 5
    #: Timeout for a single remote endpoint poll in seconds.
    fleet_poll_timeout: float = 8.0
    #: Trivy binary (resolved via PATH; ``~/.local/bin/trivy`` works too).
    trivy_bin: str = "trivy"
    #: Per-scan timeout in seconds (DB download happens on first run).
    trivy_timeout: float = 180.0
    #: Image source Trivy reads from: ``docker`` matches the Dockerfile and
    #: docker-compose defaults; override to ``podman`` on Podman-only hosts.
    trivy_image_src: str = "docker"
    #: Cached scan older than this many hours is considered stale.
    trivy_cache_hours: float = 24.0
    #: Background task that rescans stale image vulnerabilities hourly.
    enable_rescan_loop: bool = True
    #: LLM runtime nodes for the Models view: list of ``{name, url, engine}``.
    #: ``engine`` is ``ollama`` (Ollama API) or ``llamacpp`` (llama-server).
    #: Defaults to empty (no leak of internal infrastructure addresses).
    #: Override with ``DOCKWATCH_MODEL_NODES`` as a JSON array. Entries are
    #: validated at startup (name, http(s) url, supported engine).
    model_nodes: list[ModelNode] = []
    #: Parallelism + timeout for polling model nodes.
    model_poll_concurrency: int = 5
    model_poll_timeout: float = 5.0
    #: Voice assistant (Jarvis) pipeline telemetry: the master switch for the
    #: Voice dashboard, Jarvis agent registration, and retention pruning.
    enable_voice: bool = True
    #: Keep a ``kind="voice"`` Agent named "jarvis" registered on the swarm
    #: dashboard so the voice pipeline shows up as a living worker.
    voice_jarvis_agent: bool = True
    #: Emit synthetic pipeline turns on an interval so the Voice dashboard is
    #: alive without a running audio stack. Keep False in production — the real
    #: worker should report turns via POST /api/voice/ingest.
    enable_voice_demo: bool = False
    #: Seconds between synthetic demo turns.
    voice_demo_interval: float = 15.0
    #: Voice turns/stage samples older than this many days are pruned.
    voice_retention_days: int = 30
    #: Serve Prometheus-format metrics at ``/api/metrics`` (request counts,
    #: latencies, Docker call latency, panic/sanity counters). Safe to scrape
    #: without auth when the whitelist below is applied at the proxy.
    enable_metrics: bool = True
    #: Master switch for anomaly alerting.
    enable_alerting: bool = True
    #: Webhook URL (e.g. ntfy, Slack-compatible, generic HTTP). When set, each
    #: anomaly event is POSTed as JSON. Leave empty to disable outbound alerts.
    alert_webhook_url: str | None = None
    #: Per-request timeout when POSTing an alert in seconds.
    alert_webhook_timeout: float = 5.0
    #: Minimum seconds between two alerts for the same anomaly key
    #: (``cpu`` / ``mem``), so a sustained spike doesn't spam the webhook.
    alert_cooldown_seconds: float = 300.0
    #: Background task that persists container stats snapshots so the ranking
    #: endpoint has historical data to query. A snapshot is written per running
    #: container every ``container_stats_interval`` seconds.
    enable_container_stats: bool = True
    #: Seconds between container stats snapshots (poll rate for the background loop).
    container_stats_interval: float = 60.0


@lru_cache
def get_settings() -> Settings:
    """Return a cached, process-wide :class:`Settings` instance."""
    return Settings()

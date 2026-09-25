# Dockwatch — Docker monitoring, host metrics, scans, inventory, swarm, voice

The Dockwatch subsystem lives in `app/dockwatch/` and is mounted into the
unified app on the same FastAPI instance. Every route is principal-gated
(`app/main.py` wires `Depends(get_principal)` on all dockwatch routers); writes
additionally require `operator`, and destructive/admin actions `admin`.

## Modules at a glance

| Area | Router | Service(s) |
|------|--------|-----------|
| Docker API | `api/docker.py` | `services/docker_service.py`, `docker_manager.py` |
| Container ranking | `api/container_ranking.py` | `services/container_stats_persistence.py` |
| Host monitoring | `api/monitor.py` | `monitor_loop.py` (sampler), `monitor_service.py`, `monitor_persistence.py`, `alerter.py` |
| Vulnerability scans | `api/security.py` | `trivy_service.py`, `rescan_loop.py` |
| Inventory | `api/inventory.py` | `models/inventory.py` |
| Fleet endpoints | `api/endpoints.py` | `api/deps.py` (read-only token support) |
| LLM model nodes | `api/models.py` | `llm_service.py` |
| Agent swarm | `api/swarm.py` | swarm models/schemas |
| Voice pipeline | `api/voice.py` | `voice_pipeline.py`, `voice_worker.py`, `voice_persistence.py` |
| Prometheus | `api/metrics.py` | `services/metrics.py` middleware |
| Infrastructure | `middleware.py`, `query_id` | access log, request IDs, security headers |

## Environment (`DOCKWATCH_*`)

The full set is defined in `app/dockwatch/config.py`; the commonly-tuned ones:

| Variable | Default | Meaning |
|----------|---------|---------|
| `DOCKWATCH_DATABASE_URL` | `sqlite+aiosqlite:///./dockwatch.db` | its own SQLite DB (WAL enabled) |
| `DOCKWATCH_DOCKER_HOST` | `unix:///var/run/docker.sock` | primary Docker API endpoint |
| `DOCKWATCH_DOCKER_SOCKET_FALLBACKS` | Podman sockets | probed when primary is unreachable |
| `DOCKWATCH_MAX_STATS_CONTAINERS` | 50 | per-listing stats cap |
| `DOCKWATCH_ENABLE_MONITOR_SAMPLER` | `true` | background host-metrics sampler |
| `DOCKWATCH_MONITOR_RETENTION_DAYS` | 7 | sample retention |
| `DOCKWATCH_MONITOR_ANOMALY_ZSCORE` | 2.5 | anomaly threshold (|z|) |
| `DOCKWATCH_ALERT_WEBHOOK_URL` | — | ntfy/Slack/generic webhook for anomalies |
| `DOCKWATCH_ALERT_COOLDOWN_SECONDS` | 300 | min gap between alerts for one key |
| `DOCKWATCH_TRIVY_BIN` | `trivy` | Trivy binary (PATH-resolved) |
| `DOCKWATCH_TRIVY_IMAGE_SRC` | `docker` | `docker` or `podman` image source |
| `DOCKWATCH_ENABLE_RESCAN_LOOP` | `true` | hourly stale-vulnerability rescan |
| `DOCKWATCH_MODEL_NODES` | `[]` | JSON list `{name,url,engine}` (ollama\llamacpp) |
| `DOCKWATCH_ENABLE_VOICE` | `true` | Jarvis voice telemetry master switch |
| `DOCKWATCH_ENABLE_VOICE_DEMO` | `false` | synthetic demo turns (never in prod) |
| `DOCKWATCH_ENABLE_CONTAINER_STATS` | `true` | snapshot loop for the ranking endpoint |
| `DOCKWATCH_ENABLE_ALERTING`, `DOCKWATCH_ENABLE_METRICS` | `true` | feature switches |
| `DOCKWATCH_ENABLE_OPENAPI` | `true` | serve `/docs`/`/openapi.json` |
| `DOCKWATCH_ALLOWED_HOSTS` | `["*"]` | host-header validation (set your domain!) |
| `DOCKWATCH_CORS_ORIGINS` | `[]` | permitted cross-origin callers |
| `DOCKWATCH_REQUEST_TIMEOUT` | 30.0 | per-request wall-clock budget → 504 |
| `DOCKWATCH_SECURITY_HEADERS` / `DOCKWATCH_CSP_OVERRIDE` | `true` | response hardening |

## Docker

Requires a Docker-API-compatible socket. The shipped `docker-compose.yml`
omits the socket mount (root-equivalent, and unresolvable on nested/dind
hosts). To enable live container stats on a trusted host, add
`/var/run/docker.sock:/var/run/docker.sock` under `volumes`, add the host's
Docker `group_add` GID so the non-root container user can read it, and on
SELinux-enforcing hosts run `sudo setsebool -P container_connect_any on`.
Podman fallbacks are configured by default so a rootless Podman host works
without config. Without a socket, host metrics still work and container stats
report "unavailable".

## Host monitoring

`monitor_loop.py` samples CPU/memory/disk every `monitor_sampler_interval` and
persists a rolling window (`monitor_retention_days`). The API endpoints serve
the current snapshot, series history, anomaly list, and an anomaly-density
summary. Anomalies are flagged with a rolling z-score; `alerter.py` posts them
to `DOCKWATCH_ALERT_WEBHOOK_URL` with dedupe by `(key, cooldown)`.

## Vulnerability scanning

`trivy_service.py` wraps the Trivy CLI (JSON output), caching results for
`trivy_cache_hours`. `rescan_loop.py` rescans stale image records hourly. If
Trivy is missing, scanning endpoints return a clear 501/503 rather than a hard
crash — install Trivy (or point `DOCKWATCH_TRIVY_BIN` at a local copy) on the
host and restart.

## Fleet endpoints

Remote Dockwatch instances register as endpoints (name + URL + token). The
`/api/fleet/*` fan-out polls multiple endpoints concurrently
(`fleet_poll_concurrency` / `fleet_poll_timeout`) and aggregates per-container
rows. `DOCKWATCH_AUTH_READONLY_TOKENS` (legacy) grants a read-only role to
such agents.

## Voice pipeline (Jarvis)

`enable_voice` turns on the voice dashboard, Jarvis agent registration, and
retention pruning. With `enable_voice_demo`, synthetic turns keep the dashboard
alive for demos. Production uses `POST /api/voice/ingest` (report real turns)
or the bundled worker:

```bash
uv run jarvis-worker                    # see app/dockwatch/services/voice_worker.py
# with microphone input:
uv run --extra voice-mic jarvis-worker
```

## Metrics

`GET /api/metrics` exports Prometheus-format counters (requests, latencies,
Docker call latency, panic/sanity counters) and is deliberately
unauthenticated — restrict it at the proxy if exposed publicly.

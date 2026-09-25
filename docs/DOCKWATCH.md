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
| Dev pipelines | `api/pipeline.py` | `code_agents.py`, `pipeline_persistence.py`, `code_worker.py` (`code-agent` script) |
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

## Port discovery (hotlinks)

Scanning an endpoint (`POST /api/endpoints/{id}/discover`) lists its
containers, finds every **published** host port, and persists one
`ContainerLink` per `(endpoint, container, port)` in the Dockwatch DB
(`container_links`). Links are clickable `scheme://host:port` hotlinks that
the Fleet tab renders and can promote into gateway routes
(`POST /api/links/{id}/map-to-gateway` → a `GatewayRoute` on the Trove side,
proxied at `/gw/<slug>`).

Discovery runs automatically (fire-and-forget) right after an endpoint is
created and synchronously after every successful `test`. It is an *upsert*:
re-scans never duplicate rows, and a container that stops appearing is only
flagged `stale` (never deleted) so history survives. Editing a link's
`scheme`/`host` marks it `manual` and resyncs leave it alone.

**How the hotlink URL is derived** (single rule-set in
`app/dockwatch/services/discovery.py`):

- **host** — a publish binding with a real interface IP (anything other than
  `0.0.0.0`/`::`/empty) uses that IP; otherwise the endpoint host is used
  (`tcp://1.2.3.4:2375` → `1.2.3.4`, `unix://...` → `localhost`).
- **scheme** — `https` when the published port is `443` or the container is
  labelled `trove.link.scheme=https`; else `http`.
- **alias** — container label `trove.link.name`, falling back to the
  container name / short id.

The same rules produce a live `links` array on every
`/api/docker/containers` row, so the Containers tab shows hotlink chips even
before a scan persists anything.

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

## Dev pipelines (smol language agents)

`DOCKWATCH_CODE_AGENTS` registers a "family of models" — one small code agent
per language (by default **ray → rust, fleet → go, jarvis → python**), each
served by a smol model (default `qwen2.5-coder:1.5b`) on an Ollama/llama.cpp
node — as `kind="code"` fleet endpoints *plus* swarm agents. They are seeded
idempotently at startup; each becomes a card in the **Pipelines** tab:

```json
DOCKWATCH_CODE_AGENTS=[
  {"name":"ray","language":"rust","model":"qwen2.5-coder:1.5b","engine":"ollama"},
  {"name":"fleet","language":"go","model":"qwen2.5-coder:1.5b","engine":"ollama"},
  {"name":"jarvis","language":"python","model":"qwen2.5-coder:1.5b","engine":"ollama"}
]
```

The control plane stays **telemetry-only**: it probes each node's model runtime
(`/api/tags`, kind `code`) for Fleet status and stores per-stage pipeline runs
in its own DB (`pipeline_runs`, `pipeline_stage_samples`). A worker runs where
the toolchains live —

```bash
uv run code-agent --agent-name ray --trove-url http://trove:8000 \
    --ollama-url http://ray:11434 --model qwen2.5-coder:1.5b \
    --fix-depth 2
```

Each poll cycle it heartbeats (`POST /api/swarm/agents/{id}/heartbeat`), finds
its enrolled project (a swarm Project with a `repo_url` set on the Projects
tab — repos stay unset until you enroll them, and point at your own git
server), clones/fetches the branch, then runs
`checkout → deps → format → lint → build → test → report` with per-language
commands. Every result is reported via `POST /api/pipeline/ingest`; a failing
stage short-circuits the rest unless `--fix-depth > 0`, in which case a local
`smolagents` `CodeAgent` (needs `uv sync --extra agents` for the model client)
is asked to repair it and the stage re-runs. The Fleet tab's "test" action also
works on code endpoints (Ollama-style `/api/tags` probe).

**Local sanity fleet** — brings up a throwaway control plane on :8001, a
shared Ollama, and the three workers to exercise the whole loop before the
real tailnet rollout:

```bash
docker build -f Dockerfile.agents -t trove-code-agent:latest .
docker compose -f docker-compose.yml -f docker-compose.agents.yml \
    --profile agents up -d --build
```

**Tailnet rollout** — on each node (or a JIT node per language), run the agent
image with that node's hostname so trove reaches it by Tailscale magicDNS:
`TROVE_URL=http://trove:<port>`, `CODE_AGENT_NAME=ray`,
`OLLAMA_BASE_URL=http://ray:11434`, `CODE_AGENT_MODEL=<family-tag>`. The
Pipelines tab shows each node's live stage flow, and `docker-compose.agents.yml`
carries the compose wiring for the local profile.

**Training** — the model family is meant to be fine-tuned on your real repos;
see [`training/README.md`](../training/README.md) for the `git log -p` dataset
builder + QLoRA scaffold that produces adapters the nodes serve via `ollama
create <family-tag>`.

## Metrics

`GET /api/metrics` exports Prometheus-format counters (requests, latencies,
Docker call latency, panic/sanity counters) and is deliberately
unauthenticated — restrict it at the proxy if exposed publicly.

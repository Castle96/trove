# Operations — configuration, backup/restore, troubleshooting

## Data layout

```
data/
  trove.db        # PKI subsystem: certificates, CAs, users, settings, gateway
  dockwatch.db    # monitoring subsystem: metrics, scans, inventory, voice, swarm
  keys/master.key # AES-256-GCM master key (0600) — auto-generated unless
                  # TROVE_KEY_ENCRYPTION_KEY is set
  keys/…          # (encrypted key material lives in the DB, not here)
  ca/             # optional filesystem CA (TROVE_CA_CERT_PATH / KEY_PATH)
```

Both databases are SQLite with WAL journaling. The unified app runs **one
uvicorn worker** by design: SQLite is single-writer and the scheduler loops are
best-effort. Add Postgres (`+asyncpg`) behind a proxy only if you measure the
need.

## Configuration reference

See `.env.example` for the annotated template. Two prefixes:

- `TROVE_*` — `app/config.py` (PKI, approvals, scheduler, Vault, local CA,
  ACME webroot) and settings persisted in the DB (`/api/settings`).
- `DOCKWATCH_*` — `app/dockwatch/config.py` (monitoring; see
  `docs/DOCKWATCH.md`).

Secrets policy: the Cloudflare token and SMTP password default to the `settings`
table (masked in the API). Setting `TROVE_VAULT_ADDR` + `TROVE_VAULT_TOKEN`
routes those reads/writes through HashiCorp Vault KV v2 at `TROVE_VAULT_PATH`
(default `trove`). `GET /api/system/health` reports
`secrets_backend: vault|local` and Vault liveness.

## Backups

Back up the whole `data/` directory (or at least):

| Item | Consequence if lost |
|------|---------------------|
| `keys/master.key` (`data/keys/`) | stored private keys become unrecoverable |
| `TROVE_KEY_ENCRYPTION_KEY` (if you set it) | same as above |
| `trove.db` | all cert records, users, gateway config, settings |
| `dockwatch.db` | metrics history, scans, inventory, voice, swarm |
| `data/ca/` | filesystem-CA material (if used instead of the managed CA) |

Automate with a nightly `restic`/`borg`/`rclone` job; restore is "drop files
back in `data/` and restart". Keys are 0600 and owned by the container user —
restore must preserve ownership.

### Encryption keys

- No master key → auto-generated on first use under `data/keys/master.key`.
- Production: export `TROVE_KEY_ENCRYPTION_KEY` from your secret manager, or
  store `master.key` in your backup.
- Rotate by decrypting/re-encrypting via the key-management test/scripts
  (`tests/test_key_management.py` documents the envelope; there is no API for
  bulk re-key yet — do it offline with `key_store`).

## Serving in front

For anything beyond localhost, put TLS in front:

1. Reverse-proxy TLS: Caddy (auto-HTTPS) or nginx terminates TLS and forwards
   to `127.0.0.1:8000`.
2. Set `DOCKWATCH_ALLOWED_HOSTS` to your real hostname(s) so
   TrustedHostMiddleware rejects spoofed Host headers.
3. Keep `/api/metrics`, `/api/ocsp` and `/api/crl` reachable by your scrapers
   and end-clients, but restrict `/docs` (set `DOCKWATCH_ENABLE_OPENAPI=false`)
   once the API surface is stable if you want to hide it.
4. Lock the control plane with `TROVE_API_KEY` or users (see README
   "Authentication").

## Threat model / hardening

- The Docker socket mount (`/var/run/docker.sock`) is root-equivalent on the
  host — the shipped `docker-compose.yml` omits it; if you add it for live
  container stats, keep the container trusted on a trusted network. On
  SELinux-enforcing hosts, also run `sudo setsebool -P container_connect_any on`.
- The gateway hot path `/gw/<slug>` is public; protect every real route with
  `api_key` + a rate limit.
- OCSP `/api/ocsp` is unauthenticated by design (standard OCSP clients send no
  credentials); it only answers `unknown` for unknown serials.
- Keys at rest are AES-256-GCM, but the master key is in the same data volume
  unless you inject `TROVE_KEY_ENCRYPTION_KEY`/Vault — physical access to
  `data/` yields decryption.
- The dashboard CSP is `self`-only and all frontend assets are self-hosted
  (no CDN); keep it that way when adding views.

## Troubleshooting

| Symptom | Likely cause / fix |
|---------|--------------------|
| Health ok but `/api/docker/*` empty/5xx | Docker socket not readable by the container user (`group_add` GID mismatch), or Podman-only host lacking a fallback socket. On SELinux-enforcing hosts also run `sudo setsebool -P container_connect_any on`. Check `GET /api/ready` `checks.docker` and container logs. |
| Anomaly webhooks spam | raise `DOCKWATCH_ALERT_COOLDOWN_SECONDS` or lower `DOCKWATCH_MONITOR_ANOMALY_ZSCORE` |
| Trivy scan 501/503 | Trivy not installed / `DOCKWATCH_TRIVY_BIN` not resolvable; first scan downloads the DB (`trivy_timeout`) |
| `database is locked` | many writers; single-worker topology is expected — schedule only one process against SQLite |
| Keys unrecoverable after restore | master.key missing/mismatched — restore the whole `data/keys/` tree |
| `/gw/<slug>` 404 | gateway disabled, slug typo, no matching route pattern, or route disabled |
| `/gw/<slug>` 401 / 403 | route is `api_key` and key missing/invalid (401) or disabled (403) |

## Upgrades

Schema upgrades are handled additively on boot:
- Trove: `app/database.py` `_MIGRATIONS` runs in order.
- Dockwatch: `Base.metadata.create_all` is additive (`DOCKWATCH_CREATE_ALL_ON_STARTUP`,
  default on).

Before upgrading a long-lived deployment: back up `data/`, read the CHANGELOG,
and smoke-test against a copy.

## Monitoring the monitor

- `/api/ready` — readiness (both DBs + Docker status).
- `/api/health` — liveness used by the container healthcheck.
- `/api/metrics` — Prometheus export (scrape it; alert on request-error rate,
  Docker call latency, and memory RSS).
- Docker `HEALTHCHECK` hits `/api/health` every 30s.

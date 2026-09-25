# Trove — homelab control plane: PKI, Docker, inventory, agents, APIs

[![CI](https://github.com/actions/workflows/ci.yml/badge.svg)](.github/workflows/ci.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%20%7C%203.13-blue)](pyproject.toml)
[![Ruff](https://img.shields.io/badge/lint-ruff-000000)](.pre-commit-config.yaml)
[![License: MIT](LICENSE)](https://opensource.org/licenses/MIT)

One FastAPI process, three subsystems behind a single dashboard and a single
auth model:

| Subsystem | What it does | Docs |
|-----------|--------------|------|
| **PKI / CertVault** | Real x509 certificate lifecycle: issuance, local CA, OCSP/CRL, approvals, renewals, encrypted keys | this README + `docs/OPERATIONS.md` |
| **Dockwatch** | Docker monitoring (containers/stacks/images/services), host + container metrics, Trivy vulnerability scans, infrastructure inventory, agent swarm, Jarvis voice pipeline | `docs/DOCKWATCH.md` |
| **API Gateway** | Kong-style `/<slug>` routes with consumers, API keys, per-route rate limits, TLS issuance, and a public hot path at `/gw/<slug>` | `docs/GATEWAY.md` |

## Quickstart

```bash
cp .env.example .env          # edit as needed
uv sync
uv run uvicorn app.main:app --reload --port 8000
```

Open http://localhost:8000 — the dashboard is served at `/`, API docs at
`/docs`, and the readiness probe at `/api/ready`.

### Docker

```bash
docker compose up --build -d
```

The container runs as a non-root user, keeps both databases + the encrypted
key material under the bind-mounted `./data` volume, and mounts the Docker
socket so dockwatch can read container stats (see the compose file comments —
this grants the container host-root-level control of Docker).

```bash
docker compose logs -f trove
```

## Highlights

### PKI & certificate lifecycle

- **Real x509 crypto** — certificates are actually generated (`cryptography`).
  Imported PEMs are actually parsed (CN/SANs/issuer/expiry/serial/fingerprint
  extracted, not guessed).
- **Encrypted keys at rest** — generated private keys are AES-256-GCM encrypted
  (`app/services/key_store.py`) before they touch the database. The master key
  comes from `TROVE_KEY_ENCRYPTION_KEY` or an auto-generated
  `data/keys/master.key` (0600). Key/PKCS#12 download endpoints are admin-only.
- **Managed local CA** — `POST /api/ca/root` / `/intermediate` builds a real
  root/intermediate CA whose key stays encrypted. Leaves issued through the
  local-CA path are signed by the active CA instead of self-signed, and the
  certificate's stored `issuer` is the signing CA's CN (not a free-text label).
- **Local-only issuance guard** — with `TROVE_REQUIRE_LOCAL_ISSUANCE=true` the
  ACME provider is rejected at issuance/renewal and via the settings API,
  enforcing a zero-external-issuers invariant: every cert comes from the
  managed CA. Demo seed data is opt-in (`TROVE_SEED_DEMO_DATA`).
- **OCSP responder + CRL** — `/api/ocsp` (RFC 6960, DER POST or `?serial=` GET)
  and `/api/crl` are served from the managed CA.
- **Pluggable providers** — `providers.py` defines the issuance interface.
  `SimulatedProvider` issues real leaf certs locally (CA-signed when a local CA
  exists); `ACMEProvider` runs a self-contained RFC 8555 client (dns-01 via
  Cloudflare, http-01 via webroot) so Let's Encrypt / step-ca / ZeroSSL work
  end-to-end. Select the provider and its credentials from the *Providers &
  Webhooks* panel.
- **Approval workflow** — with `TROVE_REQUIRE_APPROVAL=true`, `POST /api/certs`
  creates a pending `IssueRequest`; admins approve/deny via `/api/requests`.
- **Expiry reminders** — a daily, rate-limited pass notifies about certs
  expiring within `expiry_reminder_days` (default 14) via webhook / ntfy / email.
- **Soft delete + pagination** — deleting without `revoke=false` keeps an audit
  copy (`deleted_at`) you can restore; listings support `include_deleted`,
  `limit`, `offset`, and `filter=all|expiring|expired|autorenew`.
- **Certificate deployment** — push issued material to hosts over SSH/SFTP
  (`/api/deployments`) or a webhook-only target. Targets hold encrypted
  credentials and per-file cert/key/chain paths with an optional reload command;
  files are written atomically (0600 for keys). Assigned certs redeploy
  automatically on issue/renew and on demand via `POST /api/certs/{id}/deploy`;
  every attempt lands in `deployment_records` for audit. See `docs/DEPLOYMENT.md`.
- **Time simulation** — `POST /api/time/advance` shifts the server-side clock
  (`sim_offset_days`); advancing time triggers eligible auto-renewals, so you
  can exercise the whole lifecycle deterministically.

### Dockwatch (monitoring & more)

- **Docker API** — containers, images, stacks, services, logs, per-container
  stats and a resource-usage ranking (`/api/docker/containers/ranking`).
- **Port discovery** — scan any endpoint and its published ports become
  clickable hotlinks (`/api/endpoints/{id}/discover`) that can be promoted
  one-click into gateway routes.
- **Host metrics** — a background sampler persists CPU/memory/disk samples with
  z-score anomaly detection and webhook alerting (cooldown-bucketed).
- **Vulnerability scanning** — Trivy image scans with a background rescan loop
  for stale results.
- **Infrastructure inventory** — sites, racks, devices, and IP addresses.
- **Agent swarm** — agents, projects, tasks, approvals, notifications.
- **Models** — Ollama / llama.cpp runtime node status.
- **Voice (Jarvis)** — pipeline telemetry: live stages, latency, turns; a
  `jarvis-worker` console script is provided.

### API gateway

- **Public hot path** `/gw/<slug>/<path>` proxy in `gateway_proxy.py` with
  per-route upstream URLs, path/strip-prefix rewriting, method passthrough and
  a request timeout → 504.
- **Consumers + API keys** (hashed at rest, prefixed/suffix-prefixed display,
  rotation invalidates the old hash immediately), plus per-key expiry.
- **Per-route rate limits** (rpm → 429 with `Retry-After`) and `open` routes.
- **TLS** — a gateway can carry its own certificate issued through the shared
  PKI pipeline (`issue-tls` / `renew-tls`), served through the proxy.
- **Request logs + a console test endpoint** (`/api/gateway/test`).

## Authentication

One model guards both subsystems (see `app/deps.py`). Three modes, resolved in
order:

1. **Shared API key** — set `TROVE_API_KEY`; `Authorization: Bearer <key>`
   grants `admin`.
2. **User bearer tokens** — `POST /api/auth/login` (or an admin-issued token)
   returns a bearer token scoped to the user's role.
3. **Open mode** — no API key and no users configured: full local access
   (suitable for an isolated LAN).

Roles ascend `viewer` < `operator` < `admin`; writes require `operator`+,
destructive/admin operations require `admin`. Dockwatch routers are unified
behind the same principals (`app/main.py`). The legacy `DOCKWATCH_AUTH_TOKEN`
setting still exists but is superseded by Trove's auth in the unified app.

The gateway hot path `/gw/<slug>` is **unauthenticated by the control plane**
— it is protected by each route's own `auth_mode` (`open` or `api_key`).

## Configuration

Copy `.env.example` and adjust. Every setting is prefixed `TROVE_` (PKI /
gateway) or `DOCKWATCH_` (monitoring). Secrets (Cloudflare token, SMTP
password) live in the `settings` table by default; set `TROVE_VAULT_ADDR` +
`TROVE_VAULT_TOKEN` to route reads/writes through HashiCorp Vault KV v2 instead.

## Layout

```
app/
  main.py               # FastAPI factory: both DBs, auth, middleware, routers, SPA
  config.py             # TROVE_* pydantic-settings
  database.py           # async SQLAlchemy engine + additive migrations
  models.py             # Certificate, RenewalLog, Setting, IssueRequest, CaAuthority,
                        # User, ApiToken, Gateway*, ...
  schemas.py            # request/response models
  deps.py               # auth: API key / user tokens / open mode + role gates
  services/
    crypto_service.py   # x509 generation, CA build, CSR sign, OCSP, CRL, PKCS#12
    key_store.py        # AES-256-GCM encryption at rest (v1 envelope)
    providers.py        # Provider interface: SimulatedProvider + ACMEProvider
    acme_service.py     # self-contained RFC 8555 ACME client (dns-01/http-01)
    cert_service.py     # issue/import/renew/revoke/rotate/soft-delete/logs/time-sim/settings
    local_ca_service.py # root/intermediate CA lifecycle + CSR signing
    requests_service.py # approval workflow
    gateway_service.py  # gateway CRUD, API-key hashing, TLS via PKI pipeline
    secret_store.py     # Vault KV v2 backend for settings-secrets
    notification_service.py  # webhook/ntfy/email fan-out (renewals + reminders)
    webhook_service.py       # async webhook fire with retry-friendly timeout
  api/
    routes_certs.py     # /api/certs/*
    routes_ca.py        # /api/ca/* (managed local CA)
    routes_ocsp.py      # /api/ocsp (RFC 6960 responder)
    routes_requests.py  # /api/requests/* (approval workflow)
    routes_system.py    # /api/health /time /settings /logs /sync /crl
    routes_auth.py      # /api/auth (login/logout/me)
    routes_users.py     # /api/users + /api/users/tokens
    routes_gateway.py   # /api/gateway/*
    gateway_proxy.py    # /gw/<slug>/<path> hot path
  dockwatch/            # monitoring subsystem (see docs/DOCKWATCH.md)
    api/                # docker, inventory, monitor, security, swarm, voice, ...
    services/           # samplers, scanner, alerting, voice pipeline ...
frontend/
  index.html            # dashboard (all three subsystems)
  css/app.css           # imports vendor/fonts (self-hosted fonts)
  js/{api,terminal,modals,app}.js + js/dockwatch/*.js
  vendor/               # self-hosted FontAwesome + Inter/JetBrains Mono woff2
docs/
  DOCKWATCH.md          # monitoring subsystem guide
  GATEWAY.md            # gateway guide
  OPERATIONS.md         # backup/restore, TLS-in-front, configuration reference
tests/
  test_api.py           # core API + crypto + time-simulation
  test_ca.py            # local CA lifecycle + CSR signing
  test_key_management.py# encrypted-at-rest, key download, PKCS#12, rotation
  test_ocsp.py          # OCSP good/revoked/unknown + CRL
  test_requests.py      # approval workflow on/off
  test_lifecycle.py     # soft delete/restore, pagination, reminders
  test_gateway.py       # gateway CRUD, proxy hot path, keys, rate limits, TLS
```

## API surface

Flags below: most routes are also exercised on the dashboard. The full OpenAPI
schema ships at `/docs`.

| Method | Path | Purpose |
|--------|------|---------|
| **PKI & certs** | | |
| GET | `/api/certs` | list (search/filter/pagination, `include_deleted`) |
| POST | `/api/certs` | issue a certificate (or create approval request) |
| GET | `/api/certs/{id}` | single certificate |
| PATCH | `/api/certs/{id}` | toggle `auto_renew` |
| POST | `/api/certs/{id}/renew` / `reissue` | renew / reissue |
| POST | `/api/certs/{id}/rotate-key` | reissue with fresh key material |
| POST | `/api/certs/{id}/restore` | restore a soft-deleted certificate |
| DELETE | `/api/certs/{id}` | revoke (`revoke=true`, default) or soft-delete |
| POST | `/api/certs/batch-renew` | renew all certs inside window |
| POST | `/api/certs/import` | import + parse a PEM |
| GET | `/api/certs/{id}/pem` / `key` / `p12` | download bundle / private key (admin) / PKCS#12 (admin) |
| GET | `/api/certs/metrics` | status rollup (Prometheus-friendly) |
| **Deployment** | | |
| GET/POST | `/api/deployments` | list / create deployment targets (admin) |
| GET/PATCH/DELETE | `/api/deployments/{id}` | target detail / update / delete (admin) |
| GET/POST/DELETE | `/api/certs/{id}/deployments[/{target_id}]` | list / assign / unassign cert targets |
| POST | `/api/certs/{id}/deploy` | manual redeploy to assigned targets (operator+) |
| GET | `/api/certs/{id}/deployments/history` | per-cert deployment audit records |
| **Local CA** | | |
| GET/POST | `/api/ca` | list / manage local root + intermediate CAs |
| POST | `/api/ca/root` / `intermediate` | create a root / intermediate |
| POST | `/api/ca/active` | select the active signing CA |
| GET | `/api/ca/{id}/cert` / `chain` | download CA certificate / chain |
| POST | `/api/ca/{id}/csr` + `/api/ca/sign-csr` | sign an external CSR |
| PATCH | `/api/ca/{id}/enabled`, DELETE `/api/ca/{id}` | enable / remove a CA |
| **OCSP / CRL** | | |
| POST/GET | `/api/ocsp` | RFC 6960 responder (DER body or `?serial=`) |
| GET | `/api/crl` | CRL signed by the managed/filesystem CA |
| **Approvals** | | |
| GET/POST/DELETE | `/api/requests` | approval workflow (approve/deny per request) |
| **System** | | |
| GET | `/api/health`, `/api/ready` | liveness / readiness probes (unauthenticated) |
| GET | `/api/time` + POST `/advance` `/reset` | time simulation |
| GET/PUT | `/api/settings` | provider/webhook/config (secrets masked) |
| GET | `/api/system/health` | extended health incl. secrets backend + Vault |
| POST | `/api/sync` | simulated ACME/proxy sync |
| GET | `/api/logs` (+ `/export`, DELETE) | lifecycle terminal history |
| **Auth & users** | | |
| POST | `/api/auth/login` / `logout`; GET `/api/auth/me` | bearer-token auth |
| GET/POST | `/api/users`; PATCH/DELETE `/api/users/{id}` | user administration |
| GET/POST/DELETE | `/api/users/tokens` | issue/revoke API tokens |
| **Gateway management** | | |
| GET/POST | `/api/gateway` | list / create gateway (slug, base_url, TLS) |
| GET/PATCH/DELETE | `/api/gateway/{id}` | gateway detail / update / delete |
| POST | `/api/gateway/{id}/issue-tls` / `renew-tls` | issue/renew gateway cert via PKI |
| GET/POST | `/api/gateway/{id}/routes` | list / create routes |
| GET/PATCH/DELETE | `/api/gateway/routes/{id}` | route detail / update / delete |
| GET/POST | `/api/gateway/{id}/consumers` | list / create consumers |
| PATCH/DELETE | `/api/gateway/consumers/{id}` | consumer update / delete |
| POST | `/api/gateway/consumers/{id}/keys` | issue an API key (returns plaintext once) |
| GET/PATCH/DELETE | `/api/gateway/keys/{id}` | keys detail/update/delete; POST `/{id}/rotate` |
| GET/DELETE | `/api/gateway/logs`; POST `/api/gateway/test` | request logs / direct test console |
| **Gateway hot path** | | |
| ANY | `/gw/{slug}/{path}` | proxy to the route upstream (route-level auth/limits) |
| **Dockwatch** | (docs/DOCKWATCH.md) | |
| GET | `/api/docker/status`, `/containers`, `/containers/{id}[ /logs]` | Docker API |
| GET | `/api/docker/stacks`, `/images`, `/services`, `/activity` | stack/image/service views |
| GET | `/api/docker/containers/ranking` | container resource-usage ranking |
| GET/POST | `/api/monitor/{status,snapshot,series,anomalies,history}` | host metrics + anomalies |
| GET/POST | `/api/security/scans` (+ `/detail`) | Trivy scans |
| GET/POST | `/api/inventory/*` | sites, racks, devices, IP addresses, search |
| GET/POST | `/api/endpoints` (+ `/fleet/*`, `/count`) | remote fleet endpoints |
| GET/POST | `/api/endpoints/{id}/discover`, `/api/links` | published-port discovery + hotlink registry |
| GET/PATCH/DELETE | `/api/links/{id}`, `/api/links/{id}/map-to-gateway` | tune / delete / promote hotlinks to routes |
| GET/POST | `/api/swarm/*` | agents, projects, tasks, approvals, notifications |
| GET | `/api/models/fleet`, `/config` | Ollama/llama.cpp nodes |
| GET/POST | `/api/voice/*` | Jarvis pipeline telemetry / ingest |
| GET | `/api/metrics` | Prometheus export (unauthenticated) |

## Security notes

- `private_key` columns are AES-256-GCM ciphertext; the master key is never
  stored in the DB. Back up `data/keys/` (or `TROVE_KEY_ENCRYPTION_KEY`) or the
  encrypted keys become unrecoverable.
- OCSP is intentionally unauthenticated (standard OCSP clients don't send
  credentials) and only answers `unknown` for serials it cannot find.
- Mounted OCSP/CRL/CA material should be served behind TLS in production.
- ACME-managed certificates have no private key stored, so `/key` and `/p12`
  return 404 for them — by design.
- The Docker socket mount is root-equivalent on the host (see compose). Keep
  this container trusted and on a trusted host.
- Secrets in the `settings` table are masked in the API (`cf_token_masked`,
  `notify_smtp_pass_masked`); prefer the Vault backend
  (`TROVE_VAULT_ADDR` + `TROVE_VAULT_TOKEN`) in production. Keep webhook URLs
  HTTPS.

## Tests

```bash
uv run python -m pytest          # full suite (fast, no Docker required)
uv run ruff check app tests      # lint
uv run ruff format app tests     # format
```

The suite currently exercises the PKI core, local CA, OCSP/CRL, approval
workflow, lifecycles, the gateway (including the proxy hot path against a local
echo server), plus unit coverage of the ACME client, Vault secret store, and
notification fan-out. CI (`.github/workflows/ci.yml`) runs ruff + pytest on
Python 3.12 and 3.13 via `uv`, with a coverage floor enforced through
`--cov-fail-under`.

## Going to production

- Put Caddy/nginx in front with TLS unless strictly LAN-only; set
  `DOCKWATCH_ALLOWED_HOSTS` to your domain(s) so TrustedHostMiddleware validates
  the Host header.
- Point `TROVE_KEY_ENCRYPTION_KEY` (or a Vault/KMS-managed key) explicitly and
  back up `data/` regularly.
- Move `cf_token` / SMTP password to Vault via `TROVE_VAULT_*`.
- Enable user auth (`TROVE_ADMIN_USERNAME`/`TROVE_ADMIN_PASSWORD` bootstrap or
  `/api/users`) instead of the open/shared-key modes when more than one person
  has access.
- The Docker socket mount is root-equivalent on the host; the shipped
  `docker-compose.yml` omits it by default. Add it only on a trusted single-host
  daemon if you need live container stats (see comment in `docker-compose.yml`).
- Single uvicorn worker is the supported topology — SQLite is single-writer and
  the scheduler loops are best-effort. Scale with Postgres
  (`TROVE_DATABASE_URL`/`DOCKWATCH_DATABASE_URL` with `+asyncpg`) behind a proxy
  only if you measure the need.

See `docs/OPERATIONS.md` for backup/restore, monitoring, and troubleshooting.

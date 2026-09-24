# Trove — homelab control plane: PKI, Docker, inventory, agents, APIs

Unified PKI & certificate lifecycle manager (formerly CertVault) fused with the
Dockwatch monitoring platform — plus an API gateway, so Trove also fronts your
app APIs at `/gw/<slug>` with API keys, rate limits, and TLS.

## Highlights

- **Real x509 crypto** — certificates are actually generated (`cryptography`)
  and imported PEMs are actually parsed (CN/SANs/issuer/expiry/serial/fingerprint
  extracted, not guessed).
- **Encrypted keys at rest** — generated private keys are AES-256-GCM encrypted
  (`app/services/key_store.py`) before they touch the database. The master key
  comes from `TROVE_KEY_ENCRYPTION_KEY` or an auto-generated
  `data/keys/master.key` (0600). Download keys / export PKCS#12 only when you
  actually need them — both are admin-only endpoints.
- **Managed local CA** — `POST /api/ca/root` (and `/intermediate`) creates a
  real root/intermediate CA whose key is stored encrypted. Leaves issued via the
  Local CA protocol are signed by the active CA instead of self-signed.
- **OCSP responder + CRL** — `/api/ocsp` (RFC 6960, DER POST or `?serial=`
  GET) and `/api/crl` are served from the managed CA, so clients can validate
  status without shipping your CA key to a third party.
- **Approval workflow** — with `TROVE_REQUIRE_APPROVAL=true`, `POST /api/certs`
  creates a pending `IssueRequest`; admins approve/deny it via `/api/requests`.
  Direct issuance is then gated to `operator`+ roles (open mode still passes).
- **Expiry reminders** — a daily, rate-limited pass notifies about certs
  expiring within `expiry_reminder_days` (default 14) through the same webhook /
  ntfy / email channels as renewals.
- **Soft delete / pagination** — deleting without `revoke=false` keeps an audit
  copy (`deleted_at`) that you can restore; listings support `include_deleted`,
  `limit`, `offset`, and `filter=all|expiring|expired|autorenew`.
- **Pluggable providers** — `app/services/providers.py` defines the issuance
  interface. `SimulatedProvider` issues real leaf certs locally (optionally
  CA-signed) so the full lifecycle works end-to-end. A real step-ca / Let's
  Encrypt ACME client slots in behind the same interface; ACME keys stay with
  the ACME server by design.
- **Time simulation** — server-side `sim_offset_days` setting; advancing time
  triggers automatic renewals, like the original demo but persisted and
  deterministic.
- **Optional auth** — set `TROVE_API_KEY` and the API requires
  `Authorization: Bearer <key>`. Roles: `viewer` < `operator` < `admin` via
  `/api/users` + `/api/auth` bearer tokens; open mode (no key, no users) implies
  full access for an isolated LAN.
- **No secrets in source** — provider config lives in the DB/UI and the UI
  renders token fields masked. Keep the webhook URL HTTPS.

## Quickstart

```bash
cd trove
cp .env.example .env          # edit as needed
uv sync
uv run uvicorn app.main:app --reload --port 8000
```

Open http://localhost:8000 — the dashboard is served at `/`, API docs at
`/docs`.

### Docker

```bash
docker compose up --build -d
```

The container runs as a non-root user, keeps databases + encrypted keys under
the bind-mounted `./data` volume, and healthchecks `/api/health`.

## Layout

```
app/
  main.py            # FastAPI factory + static frontend mount
  config.py          # pydantic-settings (env prefix TROVE_)
  database.py        # async SQLAlchemy engine/session + additive migrations
  models.py          # Certificate, RenewalLog, Setting, IssueRequest, CaAuthority
  schemas.py         # request/response models
  deps.py            # auth: API key / user tokens / open mode + role gates
  services/
    crypto_service.py  # x509 generation, CA build, CSR sign, OCSP, CRL, PKCS#12
    key_store.py       # AES-256-GCM encryption at rest (v1 envelope)
    providers.py       # issuance interface + SimulatedProvider + LocalCA context
    cert_service.py    # issue/import/renew/revoke/rotate/soft-delete/logs/time-sim
    local_ca_service.py# root/intermediate CA lifecycle + CSR signing
    requests_service.py# approval workflow (create/approve/deny)
    webhook_service.py # async webhook fire with retry-friendly timeout
    acme_service.py    # RFC 8555 ACME client skeleton
    notification_service.py  # webhook/ntfy/email notification fan-out
  api/
    routes_certs.py    # /api/certs/*
    routes_ca.py       # /api/ca/* (managed local CA)
    routes_ocsp.py     # /api/ocsp (RFC 6960 responder)
    routes_requests.py # /api/requests/* (approval workflow)
    routes_system.py   # /api/health /time /settings /logs /sync /crl
  seed.py              # first-run demo certificates
frontend/
  index.html           # dashboard
  css/app.css
  js/{api,terminal,modals,app}.js
tests/
  test_api.py          # core API + crypto + time-simulation
  test_ca.py           # local CA lifecycle + CSR signing
  test_key_management.py  # encrypted-at-rest, key download, PKCS#12, rotation
  test_ocsp.py         # OCSP good/revoked/unknown + CRL
  test_requests.py     # approval workflow on/off
  test_lifecycle.py    # soft delete/restore, pagination, reminders
```

## API surface

| Method | Path | Purpose |
|--------|------|---------|
| GET  | `/api/certs` | list (search/filter/pagination, `include_deleted`) |
| POST | `/api/certs` | issue a certificate (or create approval request) |
| GET  | `/api/certs/{id}` | single certificate |
| PATCH| `/api/certs/{id}` | toggle `auto_renew` |
| POST | `/api/certs/{id}/renew` / `reissue` | renew / reissue |
| POST | `/api/certs/{id}/rotate-key` | reissue with fresh key material |
| POST | `/api/certs/{id}/restore` | restore a soft-deleted certificate |
| DELETE | `/api/certs/{id}` | revoke (`revoke=true`, default) or soft-delete |
| POST | `/api/certs/batch-renew` | renew all certs inside window |
| POST | `/api/certs/import` | import + parse a PEM |
| GET  | `/api/certs/{id}/pem` | download certificate bundle |
| GET  | `/api/certs/{id}/key` | download PKCS#8 private key (admin) |
| GET  | `/api/certs/{id}/p12` | download PKCS#12 bundle, `?password=` (admin) |
| GET  | `/api/metrics` | status rollup (Prometheus-friendly) |
| POST | `/api/time/advance` / `reset` | simulate time travel |
| GET/PUT | `/api/settings` | provider/webhook/config (secrets masked) |
| POST | `/api/sync` | simulated ACME/proxy sync |
| GET | `/api/crl` | CRL signed by the managed/filesystem CA |
| GET/DELETE | `/api/logs` | lifecycle terminal history |
| GET/POST | `/api/ca` | list / manage local root + intermediate CAs |
| GET | `/api/ca/{id}/cert`, `/chain` | download CA certificate / chain |
| POST | `/api/ca/{id}/csr` + `/api/ca/sign-csr` | sign an external CSR |
| POST | `/api/ca/active` | select the active signing CA |
| POST | `/api/ocsp` | RFC 6960 OCSP responder (DER body) |
| GET | `/api/ocsp?serial=` | OCSP responder convenience |
| GET/POST/DELETE | `/api/requests` | approval workflow |

## Security notes

- `private_key` columns are AES-256-GCM ciphertext; the master key is never
  stored in the DB. Back up `data/keys/` (or `TROVE_KEY_ENCRYPTION_KEY`) or the
  encrypted keys become unrecoverable.
- OCSP is intentionally unauthenticated (standard OCSP clients don't send
  credentials) and only answers `unknown` for serials it cannot find.
- Mounted OCSP/CRL/CA material should be served behind TLS in production.
- ACME-managed and legacy imported certificates have no private key stored, so
  `/key` and `/p12` return 404 for them — by design.

## Tests

```bash
uv run python -m pytest          # full suite (fast, no Docker required)
uv run ruff check app tests      # lint
uv run ruff format app tests     # format
```

Additive DB migrations live in `app/database.py` (`_MIGRATIONS`); existing
deployments upgrade in place on boot. CI (`.github/workflows/ci.yml`) runs ruff
+ pytest on Python 3.12 and 3.13 via `uv`.

## Going to production

- Self-host Tailwind + FontAwesome with a build step before exposing the
  dashboard (currently CDN).
- Move `cf_token` handling to HashiCorp Vault / secrets manager (Vault KVv2
  hooks exist in `app/services/secret_store.py`).
- Swap `SimulatedProvider` for an RFC 8555 ACME client to issue real
  Public-Trust or step-ca certs.
- Put uvicorn/Caddy/nginx in front with TLS unless strictly LAN-only.
- Run multiple uvicorn workers behind a proxy for the scheduler/voice paths
  only if the extra complexity is worth it — the scheduler is best-effort and
  single-writer SQLite favors one process.
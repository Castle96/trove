# Changelog

All notable changes to this project are documented here. The format is based
on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.4.0] - 2026-09-25

### Added
- **Port discovery (hotlinks)** — scanning an endpoint
  (`POST /api/endpoints/{id}/discover`) lists its containers and persists one
  `ContainerLink` per published host port. Links are clickable
  `scheme://host:port` hotlinks managed under `/api/links` (list per endpoint
  or fleet-wide, `PATCH` to relabel/retune/enable, `DELETE` to forget) and
  shown as chips on the Containers tab.
- **Promote to gateway** — `POST /api/links/{id}/map-to-gateway` turns a
  discovered hotlink into a `GatewayRoute` (upstream = the discovered URL) in
  one click, proxied at `/gw/<slug>`. The link records the resulting
  `gateway_route_id` (plain integer across the separate Dockwatch/Trove DBs).
- **Automatic discovery** — runs fire-and-forget on endpoint create and
  synchronously after every successful endpoint `test` (reported as
  `links_discovered` on `EndpointStatus`).
- **Idempotent upsert semantics** — resyncs never duplicate rows; containers
  that vanish from a scan are flagged `stale` (kept, never deleted). Editing a
  link's scheme/host marks it `manual` so later scans don't overwrite it.
- **Derivation rules** — host = specific `HostIp` binding, else the endpoint
  host (`tcp://1.2.3.4:2375` → `1.2.3.4`, sockets → `localhost`); scheme =
  `https` for published port `443` or container label `trove.link.scheme=https`;
  alias = `trove.link.name` label, else container name. The same rules feed
  live hotlink chips on `/api/docker/containers`.

### Changed
- Version bumped to 0.4.0.

## [0.3.0] - 2026-09-24

### Added
- `TROVE_REQUIRE_LOCAL_ISSUANCE` — local-only issuance guard: when enabled the
  ACME provider is rejected at issuance/renewal and via the settings API, so
  no certificate can come from an external CA (zero-external-issuers
  invariant).
- `TROVE_SEED_DEMO_DATA` — demo seeding is now opt-in (default off); production
  boot never injects external-issuer demo certs.
- Locally issued certificates now record the signing CA's CN as their
  `issuer`, so the label truthfully names the signer instead of echoing a
  free-text UI field.
- Self-hosted fonts (Inter, JetBrains Mono) under `frontend/vendor/fonts/`;
  the dashboard no longer makes any CDN requests (CSP is now strictly `self`).
- Docs for the Dockwatch and gateway subsystems (`docs/DOCKWATCH.md`,
  `docs/GATEWAY.md`) plus an operations guide (`docs/OPERATIONS.md`).
- Expanded `.env.example` covering the full `TROVE_*` + `DOCKWATCH_*` surface.
- `LICENSE` (MIT), `CONTRIBUTING.md`, `CHANGELOG.md`, a `dependabot`
  configuration (`.github/dependabot.yml`) and a tag-based release workflow.
- CI enforces a coverage floor (`--cov-fail-under=50`, valid across the
  Python 3.12/3.13 matrix; the suite yields ~54% there vs ~60% on 3.14).
- Trivy subprocess tests made hermetic (patch `resolve_trivy_bin`, not its
  `__call__`) so CI no longer fails when no `trivy` binary exists.
- Unit test suites for the ACME RFC 8555 client, Vault secret store,
  notification fan-out, Trivy scan service and LLM model-node probing, plus a
  test proving the dockwatch routers share the unified API-key gate.
- **Certificate deployment** (`docs/DEPLOYMENT.md`): managed deployment targets
  with AES-256-GCM-encrypted SSH credentials, atomic SFTP push of
  cert/key/fullchain (0600 keys), optional reload command / webhook, an audit
  history, auto-deploy on issue/renew, and manual redeploy via
  `POST /api/certs/{id}/deploy`. Target CRUD under `/api/deployments` is
  admin-gated.

### Changed
- README rewritten to describe all three subsystems and the real auth model.
- Dashboard issue form defaults to the managed CA (`Trove Managed CA`) instead
  of an external issuer.
- `app/__version__` synced with the package version (0.3.0).

### Fixed
- `/api/ready` no longer 500s and the container-stats loop no longer spams
  errors when `docker.sock` exists but cannot be `stat`ed (e.g. SELinux
  blocking): Python 3.13 `Path.exists()` no longer swallows `OSError`, so the
  socket probe now handles it and degrades to "docker unavailable". On
  SELinux-enforcing hosts this also needs `sudo setsebool -P
  container_connect_any on` (documented in `docs/OPERATIONS.md` and the
  compose file).
- Corrected stale comments that claimed an Alembic-managed schema and a
  container entrypoint that never existed.

### Removed
- Stale standalone `homelab_certificate_management_dashboard.html` demo and the
  leftover `frontend/_probe_backup.html`.
- Unused `alembic` runtime dependency (schema is managed by additive
  `create_all` / migrations in `app/database.py`).
- Docker socket mount (`/var/run/docker.sock`) + `group_add` from the shipped
  `docker-compose.yml`: sharing a control socket is root-equivalent and, on
  nested/dind hosts, the bind does not resolve to a usable socket anyway.
  Dockwatch still reports host metrics; container stats show "docker
  unavailable". Docs (`README`, `DOCKWATCH.md`, `OPERATIONS.md`) now describe
  it as an opt-in for trusted single-host deployments.

## [0.2.0] - 2026-09-23

### Added
- Encrypted private keys at rest (AES-256-GCM, `app/services/key_store.py`).
- Managed local CA (`/api/ca/root`, `/api/ca/intermediate`) with CSR signing.
- OCSP responder (RFC 6960) + CRL served from the managed CA.
- Approval workflow for issuance (`TROVE_REQUIRE_APPROVAL`).
- Expiry reminders (daily, webhook/ntfy/email fan-out).
- API gateway: gateways, routes, consumers, hashed API keys, rate limits, TLS.
- Unified auth covering both subsystems (API key / user tokens / open mode).

## [0.1.0] - 2026-09-22

### Added
- Initial scaffold: certificate lifecycle (issue/import/renew/revoke/rotate),
  time simulation, terminal logs, simulated provider, dockwatch monitoring
  core, Docker + CI setup, and operational docs.

# Changelog

All notable changes to this project are documented here. The format is based
on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Self-hosted fonts (Inter, JetBrains Mono) under `frontend/vendor/fonts/`;
  the dashboard no longer makes any CDN requests (CSP is now strictly `self`).
- Docs for the Dockwatch and gateway subsystems (`docs/DOCKWATCH.md`,
  `docs/GATEWAY.md`) plus an operations guide (`docs/OPERATIONS.md`).
- Expanded `.env.example` covering the full `TROVE_*` + `DOCKWATCH_*` surface.
- `LICENSE` (MIT), `CONTRIBUTING.md`, `CHANGELOG.md`, a `dependabot`
  configuration (`.github/dependabot.yml`) and a tag-based release workflow.
- CI enforces a coverage floor (`--cov-fail-under=55`).
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
- `app/__version__` synced with the package version (0.2.0).

### Removed
- Stale standalone `homelab_certificate_management_dashboard.html` demo.
- Unused `alembic` runtime dependency (schema is managed by additive
  `create_all` / migrations in `app/database.py`).

### Fixed
- Corrected stale comments that claimed an Alembic-managed schema and a
  container entrypoint that never existed.

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

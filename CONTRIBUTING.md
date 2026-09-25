# Contributing to Trove

Thanks for helping out. This project is a homelab-grade control plane — code
should stay boring, typed, and easy to test.

## Ground rules

- One uvicorn worker, SQLite-friendly, no heavy new runtime deps without a
  discussion first.
- No new comments unless they earn their place; prefer expressive names +
  docstrings on public functions.
- Never commit secrets, `.env`, or `data/`.
- The dashboard must stay fully self-hosted (no CDN assets) and CSP
  `self`-only.

## Setup

```bash
uv sync                      # installs dev + runtime deps
uv run pre-commit install    # ruff + formatting checks on commit
```

## Making a change

1. Create a branch (`git switch -c feat/your-thing`).
2. Write the code + tests. Tests live in `tests/` and reuse the
   `client` fixture (temp DB + ASGI transport) or the gateway `upstream`
   echo server.
3. Run the checks:

```bash
uv run python -m pytest                 # full suite + coverage report
uv run ruff check app tests             # lint
uv run ruff format app tests            # formatting
```

4. Add a `CHANGELOG.md` entry under `[Unreleased]`.
5. Commit with a concise, conventional message (`feat:`, `fix:`, `docs:`,
   `chore:`, `test:`). Don't amend CI failures into one mega-commit — add a
   follow-up commit.

## Testing expectations

Coverage floor: the CI job runs `pytest --cov-fail-under=50`, calibrated to the
lowest result across the supported Python matrix (~54% on 3.12/3.13; ~60% on
3.14). Keep new code covered; the biggest historical gaps (ACME, Vault,
notifications, Trivy, the monitor loops) are high-value, low-cost units to test
with httpx `MockTransport` or injected fakes. Docker/Trivy/Podman are **not**
required for tests.

## CI / release

`.github/workflows/ci.yml` lints + tests on 3.12/3.13. Releases are driven by
tags: push `vX.Y.Z`, the release workflow builds and uploads the wheel +
source distribution to a GitHub release. Keep `pyproject.toml::version` and
`app/__init__.py::__version__` in sync.

## Where things live

- PKI/certs: `app/services/{cert,crypto,local_ca,acme,providers}_service`.
- Monitoring: `app/dockwatch/`.
- Gateway: `app/api/routes_gateway.py` + `app/api/gateway_proxy.py`.
- Docs: `README.md` + `docs/`.

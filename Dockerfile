# syntax=docker/dockerfile:1

# ---- Build stage: create a locked, bytecode-compiled venv with uv ----
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv

# Lockfile + manifest first so Docker layer caching can skip dependency
# re-resolution unless they change.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# App source (excludes tests/CI/dev files via context .dockerignore).
COPY app ./app
COPY frontend ./frontend
COPY README.md ./

# ---- Runtime stage: minimal slim image, non-root user ----
FROM python:3.13-slim AS runtime

# whisper/piper need libgomp for OpenMP at runtime.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 trove \
    && mkdir -p /app/data && chown -R trove:trove /app /app/data

WORKDIR /app

COPY --from=builder --chown=trove:trove /app/.venv /app/.venv
COPY --from=builder --chown=trove:trove /app/app /app/app
COPY --from=builder --chown=trove:trove /app/frontend /app/frontend
COPY --from=builder --chown=trove:trove /app/README.md /app/README.md

USER trove
EXPOSE 8000

ENV PATH="/app/.venv/bin:$PATH" \
    TROVE_DATA_DIR=/app/data \
    DOCKWATCH_DATABASE_URL=sqlite+aiosqlite:////app/data/dockwatch.db \
    TROVE_DATABASE_URL=sqlite+aiosqlite:////app/data/trove.db

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://localhost:8000/api/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
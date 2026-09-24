"""Secret storage backend.

By default secrets (Cloudflare token, SMTP password, ...) live in the local
``settings`` table. When ``TROVE_VAULT_ADDR`` + ``TROVE_VAULT_TOKEN``
are configured, secrets are read/written through HashiCorp Vault's KV v2
secrets engine instead (path default ``trove``).

Reads/writes are synchronous (httpx) and are called through
``asyncio.to_thread`` by the async services.
"""

from __future__ import annotations

import httpx

from ..config import get_settings

VAULT_TIMEOUT = 8.0


def vault_configured() -> bool:
    settings = get_settings()
    return bool(settings.vault_addr and settings.vault_token)


def _kv2_base() -> str:
    settings = get_settings()
    addr = settings.vault_addr.rstrip("/")
    path = settings.vault_path.strip("/")
    return f"{addr}/v1/{path}/data"


def kv2_read(key: str) -> str | None:
    settings = get_settings()
    url = f"{_kv2_base()}/{key}"
    resp = httpx.get(
        url,
        headers={"X-Vault-Token": settings.vault_token},
        timeout=VAULT_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json().get("data", {})
    value = (data.get("data") or {}).get("value")
    return str(value) if value is not None else None


def kv2_write(key: str, value: str) -> None:
    settings = get_settings()
    url = f"{_kv2_base()}/{key}"
    resp = httpx.post(
        url,
        json={"data": {"value": value}},
        headers={"X-Vault-Token": settings.vault_token},
        timeout=VAULT_TIMEOUT,
    )
    resp.raise_for_status()


def vault_health() -> dict:
    """Cheap liveness probe for the /api/system health summary."""
    settings = get_settings()
    try:
        resp = httpx.get(
            f"{settings.vault_addr.rstrip('/')}/v1/sys/health",
            headers={"X-Vault-Token": settings.vault_token},
            timeout=VAULT_TIMEOUT,
        )
        return {"ok": resp.status_code < 500, "code": resp.status_code}
    except httpx.HTTPError as exc:
        return {"ok": False, "error": str(exc)}

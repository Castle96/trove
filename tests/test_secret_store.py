"""Unit tests for the Vault KV v2 secrets backend."""

from __future__ import annotations

import httpx

from app.config import get_settings
from app.services import secret_store


def _resp(method: str, url: str, status: int = 200, **kw) -> httpx.Response:  # noqa: ANN003
    return httpx.Response(status, request=httpx.Request(method, url), **kw)


def _init_vault_env(monkeypatch) -> None:
    monkeypatch.setenv("TROVE_VAULT_ADDR", "https://vault.example.com")
    monkeypatch.setenv("TROVE_VAULT_TOKEN", "root-token")
    monkeypatch.setenv("TROVE_VAULT_PATH", "trove")
    get_settings.cache_clear()


def test_vault_configured_false_by_default() -> None:
    assert secret_store.vault_configured() is False


def test_vault_configured_true(monkeypatch) -> None:
    _init_vault_env(monkeypatch)
    try:
        assert secret_store.vault_configured() is True
    finally:
        get_settings.cache_clear()


def test_kv2_read_returns_value(monkeypatch) -> None:
    _init_vault_env(monkeypatch)

    def fake_get(url, headers=None, timeout=8.0):  # noqa: ANN001
        assert url == "https://vault.example.com/v1/trove/data/cf_token"
        assert headers["X-Vault-Token"] == "root-token"
        return _resp(
            "GET",
            "https://vault.example.com/v1/trove/data/cf_token",
            json={"data": {"data": {"value": "cloudflare-secret"}}},
        )

    monkeypatch.setattr(secret_store.httpx, "get", fake_get)
    try:
        assert secret_store.kv2_read("cf_token") == "cloudflare-secret"
    finally:
        get_settings.cache_clear()


def test_kv2_read_missing_key_returns_none(monkeypatch) -> None:
    _init_vault_env(monkeypatch)

    def fake_get(url, headers=None, timeout=8.0):  # noqa: ANN001
        return _resp("GET", "https://vault.example.com/v1/trove/data/cf_token", json={"data": {}})

    monkeypatch.setattr(secret_store.httpx, "get", fake_get)
    try:
        assert secret_store.kv2_read("unknown") is None
    finally:
        get_settings.cache_clear()


def test_kv2_write_posts_value(monkeypatch) -> None:
    _init_vault_env(monkeypatch)

    def fake_post(url, json=None, headers=None, timeout=8.0):  # noqa: ANN001
        assert url == "https://vault.example.com/v1/trove/data/cf_token"
        assert json == {"data": {"value": "new-token"}}
        return _resp("POST", "https://vault.example.com/v1/trove/data/cf_token")

    monkeypatch.setattr(secret_store.httpx, "post", fake_post)
    try:
        secret_store.kv2_write("cf_token", "new-token")
    finally:
        get_settings.cache_clear()


def test_vault_health_ok(monkeypatch) -> None:
    _init_vault_env(monkeypatch)

    def fake_get(url, headers=None, timeout=8.0):  # noqa: ANN001
        assert url == "https://vault.example.com/v1/sys/health"
        return httpx.Response(200, json={"sealed": False})

    monkeypatch.setattr(secret_store.httpx, "get", fake_get)
    try:
        health = secret_store.vault_health()
        assert health == {"ok": True, "code": 200}
    finally:
        get_settings.cache_clear()


def test_vault_health_network_error(monkeypatch) -> None:
    _init_vault_env(monkeypatch)

    def fake_get(url, headers=None, timeout=8.0):  # noqa: ANN001
        raise httpx.ConnectError("unreachable")

    monkeypatch.setattr(secret_store.httpx, "get", fake_get)
    try:
        health = secret_store.vault_health()
        assert health["ok"] is False
        assert "unreachable" in health["error"]
    finally:
        get_settings.cache_clear()

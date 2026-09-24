"""Unit tests for lifecycle notification dispatch (webhook / ntfy / email)."""

from __future__ import annotations

import httpx
import pytest

from app.services import notification_service as svc


@pytest.fixture(autouse=True)
def _reset_ntfy_url(monkeypatch) -> None:
    monkeypatch.setattr(svc, "NTFY_URL", svc.NTFY_DEFAULT)


class _FakeAsyncClient:
    """AsyncClient stand-in returning a canned response or raising."""

    def __init__(self, response: httpx.Response | None = None, error: Exception | None = None):
        self.response = response
        self.error = error
        self.last_post: tuple[str, dict] | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:  # noqa: ANN002
        return None

    async def post(self, url, *, json=None, content=None, headers=None):  # noqa: ANN003
        self.last_post = (url, dict(json or {"content": content, "headers": headers}))
        if self.error is not None:
            raise self.error
        return self.response  # type: ignore[no-any-return]


def _make_client(status: int = 200, error: Exception | None = None) -> _FakeAsyncClient:
    client = _FakeAsyncClient(error=error)
    client.response = httpx.Response(status, request=httpx.Request("POST", "https://hw"))
    return client


def _patch_client(monkeypatch, status: int = 200) -> _FakeAsyncClient:
    fake = _make_client(status)
    monkeypatch.setattr(svc.httpx, "AsyncClient", lambda **kw: fake)
    return fake


def test_any_configured() -> None:
    assert svc.any_configured(svc.NotifyTargets()) is False
    assert svc.any_configured(svc.NotifyTargets(webhook_url="https://h")) is True
    assert svc.any_configured(svc.NotifyTargets(ntfy_topic="alerts")) is True
    assert svc.any_configured(svc.NotifyTargets(email_to="a@b.c")) is False  # needs smtp_host
    assert svc.any_configured(svc.NotifyTargets(email_to="a@b.c", smtp_host="smtp")) is True


def test_ntfy_priority() -> None:
    assert svc._ntfy_priority("error") == 4
    assert svc._ntfy_priority("warning") == 3
    assert svc._ntfy_priority("success") == 2
    assert svc._ntfy_priority("info") == 2


@pytest.mark.asyncio
async def test_post_webhook_success(monkeypatch) -> None:
    fake = _patch_client(monkeypatch)
    ok, msg = await svc._post_webhook("https://hooks.example.com", {"event": "trove"})
    assert ok is True
    assert "200" in msg
    url, json_payload = fake.last_post
    assert url == "https://hooks.example.com"
    assert json_payload == {"event": "trove"}


@pytest.mark.asyncio
async def test_post_webhook_http_error(monkeypatch) -> None:
    fake = _patch_client(monkeypatch, status=500)
    ok, msg = await svc._post_webhook("https://hooks.example.com", {})
    assert ok is False
    assert "failed" in msg
    assert fake.last_post is not None


@pytest.mark.asyncio
async def test_post_webhook_connection_error(monkeypatch) -> None:
    fake = _make_client(error=httpx.ConnectError("refused"))
    monkeypatch.setattr(svc.httpx, "AsyncClient", lambda **kw: fake)
    ok, msg = await svc._post_webhook("https://hooks.example.com", {})
    assert ok is False
    assert "refused" in msg


@pytest.mark.asyncio
async def test_post_ntfy_strips_slashes_and_sets_priority(monkeypatch) -> None:
    fake = _patch_client(monkeypatch)
    ok, _msg = await svc._post_ntfy("/alerts", "cert expired", "CN=foo", "error")
    assert ok is True
    url, payload = fake.last_post
    assert url == f"{svc.NTFY_DEFAULT}/alerts"
    assert payload["content"] == "CN=foo"
    assert payload["headers"]["Title"] == "cert expired"
    assert payload["headers"]["Priority"] == "4"


def test_send_email_sync_smtp(monkeypatch) -> None:
    calls: dict[str, list] = {"ehlo": [], "login": [], "send": []}

    class _FakeSMTP:
        def __init__(self, host, port, **kw):  # noqa: ANN003
            calls["init"] = [host, port]
            self.login_called = False

        def __enter__(self):
            return self

        def __exit__(self, *exc) -> None:  # noqa: ANN002
            return None

        def ehlo(self) -> None:
            calls["ehlo"].append(True)

        def login(self, user, password) -> None:  # noqa: ANN003
            self.login_called = True
            calls["login"].extend([user, password])

        def send_message(self, msg) -> None:  # noqa: ANN003
            calls["send"].append(msg)

    monkeypatch.setattr(svc.smtplib, "SMTP", _FakeSMTP)
    targets = svc.NotifyTargets(
        email_to="b@c.d", smtp_host="smtp.example.com", smtp_user="u1", smtp_pass="pw1"
    )
    ok, msg = svc._send_email_sync(targets, "T", "M")
    assert ok is True
    assert "sent" in msg
    assert calls["init"] == ["smtp.example.com", 587]
    assert calls["login"] == ["u1", "pw1"]
    assert calls["send"][0]["Subject"] == "T"


def test_send_email_sync_smtp_ssl(monkeypatch) -> None:
    class _FakeSMTP_SSL:
        def __init__(self, host, port=587, **kw):  # noqa: ANN003
            self.port = port

        def __enter__(self):
            return self

        def __exit__(self, *exc) -> None:  # noqa: ANN002
            return None

        def ehlo(self) -> None:
            pass

        def send_message(self, msg) -> None:  # noqa: ANN003
            pass

    monkeypatch.setattr(
        svc.smtplib, "SMTP", lambda *a, **k: (_ for _ in ()).throw(AssertionError("SMTP used"))
    )
    monkeypatch.setattr(svc.smtplib, "SMTP_SSL", _FakeSMTP_SSL)
    targets = svc.NotifyTargets(email_to="b@c.d", smtp_host="smtp.example.com", smtp_port=465)
    ok, _msg = svc._send_email_sync(targets, "T", "M")
    assert ok is True


def test_send_email_sync_failure(monkeypatch) -> None:
    class _BoomSMTP:
        def __enter__(self):
            return self

        def __exit__(self, *exc) -> None:  # noqa: ANN002
            return None

        def ehlo(self):
            raise RuntimeError("refused")

    monkeypatch.setattr(svc.smtplib, "SMTP", lambda *a, **k: _BoomSMTP())
    targets = svc.NotifyTargets(email_to="b@c.d", smtp_host="smtp.example.com")
    ok, msg = svc._send_email_sync(targets, "T", "M")
    assert ok is False
    assert "refused" in msg


@pytest.mark.asyncio
async def test_notify_fans_out_to_all_configured_channels(monkeypatch) -> None:
    results: list[str] = []

    async def fake_webhook(url, payload):
        results.append(f"webhook:{payload['title']}")
        return True, "ok"

    async def fake_ntfy(topic, title, message, level):
        results.append(f"ntfy:{title}")
        return True, "ok"

    def fake_email(targets, title, message):
        results.append(f"email:{title}")
        return True, "ok"

    monkeypatch.setattr(svc, "_post_webhook", fake_webhook)
    monkeypatch.setattr(svc, "_post_ntfy", fake_ntfy)
    monkeypatch.setattr(svc, "_send_email_sync", fake_email)
    targets = svc.NotifyTargets(
        webhook_url="https://h", ntfy_topic="t", email_to="b@c.d", smtp_host="smtp"
    )
    out = await svc.notify(targets, "cert renewed", "msg", level="success")
    assert len(out) == 3
    assert results == ["webhook:cert renewed", "ntfy:cert renewed", "email:cert renewed"]


@pytest.mark.asyncio
async def test_notify_with_no_channels_returns_empty(monkeypatch) -> None:
    monkeypatch.setattr(
        svc, "_post_webhook", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no webhook"))
    )
    monkeypatch.setattr(
        svc, "_post_ntfy", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no ntfy"))
    )
    assert await svc.notify(svc.NotifyTargets(), "T", "M") == []

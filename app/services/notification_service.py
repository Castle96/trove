"""Notifications for certificate lifecycle events.

Channels (all optional, additive):

* **Webhook**  -- generic HTTP POST ``{title, message, level, event}``.
* **ntfy**     -- push notification via ``https://ntfy.sh/<topic>`` (or any
  self-hosted ntfy server through ``NOTIFY_NTFY_URL`` env override).
* **Email**    -- SMTP via the stdlib ``smtplib`` (runs in a worker thread so
  the event loop is never blocked).
"""

from __future__ import annotations

import asyncio
import os
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage

import httpx

NTFY_DEFAULT = "https://ntfy.sh"
NTFY_URL = os.environ.get("TROVE_NOTIFY_NTFY_URL", NTFY_DEFAULT).rstrip("/")
HTTP_TIMEOUT = 8.0


@dataclass
class NotifyTargets:
    webhook_url: str = ""
    ntfy_topic: str = ""
    email_to: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_pass: str = ""
    email_from: str = "trove@localhost"


def any_configured(targets: NotifyTargets) -> bool:
    return bool(
        targets.webhook_url or targets.ntfy_topic or (targets.email_to and targets.smtp_host)
    )


def _ntfy_priority(level: str) -> int:
    return {"error": 4, "warning": 3, "success": 2}.get(level, 2)


async def _post_webhook(url: str, payload: dict) -> tuple[bool, str]:
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            resp = await client.post(url, json=payload)
        resp.raise_for_status()
        return True, f"notification webhook {url} -> {resp.status_code}"
    except httpx.HTTPError as exc:
        return False, f"notification webhook failed: {exc}"


async def _post_ntfy(topic: str, title: str, message: str, level: str) -> tuple[bool, str]:
    topic = topic.strip().lstrip("/")
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            resp = await client.post(
                f"{NTFY_URL}/{topic}",
                content=message,
                headers={
                    "Title": title,
                    "Priority": str(_ntfy_priority(level)),
                    "Tags": "shield",
                },
            )
        resp.raise_for_status()
        return True, f"ntfy notification sent to {topic}"
    except httpx.HTTPError as exc:
        return False, f"ntfy notification failed: {exc}"


def _send_email_sync(targets: NotifyTargets, title: str, message: str) -> tuple[bool, str]:
    msg = EmailMessage()
    msg["Subject"] = title
    msg["From"] = targets.email_from
    msg["To"] = targets.email_to
    msg.set_content(message)
    try:
        if targets.smtp_port == 465:
            smtp_ctx = smtplib.SMTP_SSL(targets.smtp_host, targets.smtp_port, timeout=HTTP_TIMEOUT)
        else:
            smtp_ctx = smtplib.SMTP(targets.smtp_host, targets.smtp_port, timeout=HTTP_TIMEOUT)
        with smtp_ctx as smtp:
            smtp.ehlo()
            if targets.smtp_user:
                smtp.login(targets.smtp_user, targets.smtp_pass or "")
            smtp.send_message(msg)
        return True, f"email notification sent to {targets.email_to}"
    except Exception as exc:  # noqa: BLE001 - surface any SMTP failure
        return False, f"email notification failed: {exc}"


async def notify(
    targets: NotifyTargets, title: str, message: str, level: str = "info"
) -> list[tuple[bool, str]]:
    """Send a notification through every configured channel.

    Returns a list of (ok, human_readable_result) tuples. Never raises.
    """
    results: list[tuple[bool, str]] = []
    if targets.webhook_url:
        results.append(
            await _post_webhook(
                targets.webhook_url,
                {"event": "trove", "title": title, "message": message, "level": level},
            )
        )
    if targets.ntfy_topic:
        results.append(await _post_ntfy(targets.ntfy_topic, title, message, level))
    if targets.email_to and targets.smtp_host:
        results.append(await asyncio.to_thread(_send_email_sync, targets, title, message))
    return results

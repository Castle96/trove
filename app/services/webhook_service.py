"""Async webhook delivery for reverse-proxy reloads (Caddy/Nginx style)."""

from __future__ import annotations

import httpx

WEBHOOK_TIMEOUT_SECONDS = 5.0


async def fire_reload_webhook(url: str, payload: dict) -> tuple[bool, str]:
    """POST a JSON reload payload. Returns (ok, human_readable_result)."""
    if not url:
        return True, "No reload webhook configured - skipped"
    try:
        async with httpx.AsyncClient(timeout=WEBHOOK_TIMEOUT_SECONDS) as client:
            response = await client.post(url, json=payload)
        response.raise_for_status()
        return True, f"Reload webhook {url} returned {response.status_code}"
    except httpx.HTTPError as exc:
        return False, f"Reload webhook failed: {exc}"


def validate_webhook_url(url: str) -> bool:
    """Basic sanity check: require http(s) scheme (prefer HTTPS)."""
    if not url:
        return True
    return url.startswith(("https://", "http://"))

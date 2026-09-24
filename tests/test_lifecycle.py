"""Soft delete/restore, pagination, and expiry reminder notifications."""

from __future__ import annotations

from app.database import get_session_factory
from app.services import cert_service


async def test_soft_delete_and_restore(client) -> None:
    certs = (await client.get("/api/certs")).json()
    target = certs[0]

    # Soft delete (non-revoking).
    res = await client.delete(f"/api/certs/{target['id']}", params={"revoke": "false"})
    assert res.status_code == 200

    after = (await client.get("/api/certs")).json()
    assert all(c["id"] != target["id"] for c in after)

    # Still visible when including deleted (audit trail).
    with_deleted = (await client.get("/api/certs", params={"include_deleted": "true"})).json()
    assert any(c["id"] == target["id"] for c in with_deleted)

    # Restore brings it back.
    res = await client.post(f"/api/certs/{target['id']}/restore")
    assert res.status_code == 200
    restored = (await client.get("/api/certs")).json()
    assert any(c["id"] == target["id"] for c in restored)


async def test_pagination(client) -> None:
    full = (await client.get("/api/certs")).json()
    assert len(full) == 5

    page = (await client.get("/api/certs", params={"limit": 2, "offset": 0})).json()
    assert len(page) == 2
    assert page[0]["id"] == full[0]["id"]

    page2 = (await client.get("/api/certs", params={"limit": 2, "offset": 2})).json()
    assert len(page2) == 2
    assert page2[0]["id"] == full[2]["id"]

    # Combined with search/filter.
    filtered = (await client.get("/api/certs", params={"filter": "expiring", "limit": 1})).json()
    assert len(filtered) == 1


async def test_expiry_reminders_run_once_daily(client) -> None:
    # Expiring seed certs: 6 and 9 days out, inside the 14-day reminder window.
    async with get_session_factory()() as db:
        notified = await cert_service.remind_expiring(db)
        assert len(notified) == 2  # 6d + 9d certs

        # Rate-limited: a second call the same day does nothing.
        again = await cert_service.remind_expiring(db)
        assert again == []

        # Simulated time can push new certs into the window, but the daily
        # gate still applies; next-day pass fires again.
        from app.services.cert_service import advance_time

        await advance_time(db, 1)
        # last-reminder date is compared against real UTC date, so simulate
        # by clearing the marker.
        from app.services.cert_service import SETTING_LAST_REMINDER, set_setting

        await set_setting(db, SETTING_LAST_REMINDER, "")
        notified_2 = await cert_service.remind_expiring(db)
        assert len(notified_2) == 2  # still the same two (their window moved too)


async def test_reminders_notify_for_auto_renew_off(client) -> None:
    # Turn auto-renew off for the 6-day cert so reminder math is exercised.
    certs = (await client.get("/api/certs")).json()
    expiring = [c for c in certs if c["status"] == "expiring"]
    assert expiring
    await client.patch(f"/api/certs/{expiring[0]['id']}", json={"auto_renew": False})

    async with get_session_factory()() as db:
        notified = await cert_service.remind_expiring(db)
    assert len(notified) >= 1

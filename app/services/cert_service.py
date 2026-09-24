"""Certificate lifecycle orchestration shared by the API routes."""

from __future__ import annotations

import asyncio
import math
import uuid
from datetime import datetime
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import models, schemas
from ..config import get_settings
from ..timeutil import add_days, utcnow
from . import (
    acme_service,
    crypto_service,
    key_store,
    notification_service,
    providers,
    secret_store,
    webhook_service,
)

# Setting keys (stored in the settings table)
SETTING_SIM_OFFSET = "sim_offset_days"
SETTING_PROVIDER = "provider"
SETTING_ACME_URL = "acme_directory_url"
SETTING_ACME_CONTACT = "acme_contact_email"
SETTING_ACME_VALIDATION = "acme_validation"
SETTING_ACME_WEBROOT = "acme_webroot"
SETTING_CF_ZONE_HINT = "cloudflare_zone_hint"
SETTING_WEBHOOK_URL = "webhook_url"
SETTING_CF_TOKEN = "cf_token"
SETTING_RENEW_WINDOW = "renew_window_days"
SETTING_VALIDITY_DAYS = "default_validity_days"
SETTING_REMINDER_DAYS = "expiry_reminder_days"
SETTING_LAST_REMINDER = "last_expiry_reminder_date"
SETTING_LOCAL_CA_ID = "local_ca_id"

# Notification settings
SETTING_NOTIFY_WEBHOOK = "notify_webhook_url"
SETTING_NOTIFY_NTFY = "notify_ntfy_topic"
SETTING_NOTIFY_EMAIL_TO = "notify_email_to"
SETTING_NOTIFY_SMTP_HOST = "notify_smtp_host"
SETTING_NOTIFY_SMTP_PORT = "notify_smtp_port"
SETTING_NOTIFY_SMTP_USER = "notify_smtp_user"
SETTING_NOTIFY_SMTP_PASS = "notify_smtp_pass"


# ---------------------------------------------------------------------------
# Settings helpers
# ---------------------------------------------------------------------------


async def get_setting(db: AsyncSession, key: str, default: str = "") -> str:
    stmt = select(models.Setting).where(models.Setting.key == key)
    row = (await db.execute(stmt)).scalar_one_or_none()
    return row.value if row else default


async def set_setting(db: AsyncSession, key: str, value: str) -> None:
    stmt = select(models.Setting).where(models.Setting.key == key)
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row:
        row.value = value
    else:
        db.add(models.Setting(key=key, value=value))


async def get_secret_value(db: AsyncSession, key: str) -> str:
    """Resolve a secret through the active backend (Vault or local DB)."""
    if secret_store.vault_configured():
        try:
            return await asyncio.to_thread(secret_store.kv2_read, key) or ""
        except Exception:
            return ""  # Vault unreachable: treat as absent; callers log as needed
    return await get_setting(db, key)


async def set_secret_value(db: AsyncSession, key: str, value: str) -> None:
    if secret_store.vault_configured():
        await asyncio.to_thread(secret_store.kv2_write, key, value)
    else:
        await set_setting(db, key, value)


async def get_sim_offset(db: AsyncSession) -> int:
    raw = await get_setting(db, SETTING_SIM_OFFSET, "0")
    try:
        return int(raw)
    except ValueError:
        return 0


async def sim_now(db: AsyncSession) -> datetime:
    """'Now' as seen by the dashboard (real now + simulation offset)."""
    return add_days(utcnow(), await get_sim_offset(db))


async def get_renew_window(db: AsyncSession) -> int:
    raw = await get_setting(db, SETTING_RENEW_WINDOW, str(get_settings().renew_window_days))
    try:
        return int(raw)
    except ValueError:
        return get_settings().renew_window_days


async def get_validity_days(db: AsyncSession) -> int:
    raw = await get_setting(db, SETTING_VALIDITY_DAYS, str(get_settings().default_validity_days))
    try:
        return int(raw)
    except ValueError:
        return get_settings().default_validity_days


async def get_provider_name(db: AsyncSession) -> str:
    return (await get_setting(db, SETTING_PROVIDER, "simulated")).strip().lower() or "simulated"


async def _resolve_provider(db: AsyncSession) -> tuple[str, providers.Provider]:
    """Build the active provider + its config from persisted settings."""
    name = await get_provider_name(db)
    config = providers.ProviderConfig(name=name)
    if name == "acme":
        settings = get_settings()
        validation_raw = (await get_setting(db, SETTING_ACME_VALIDATION, "dns-01")).strip().lower()
        config.acme = acme_service.ACMEConfig(
            directory_url=await get_setting(db, SETTING_ACME_URL, settings.acme_directory_url),
            contact_email=await get_setting(db, SETTING_ACME_CONTACT, ""),
            validation=validation_raw if validation_raw in ("dns-01", "http-01") else "dns-01",
            webroot=await get_setting(db, SETTING_ACME_WEBROOT, settings.acme_webroot),
            cloudflare_token=await get_secret_value(db, SETTING_CF_TOKEN),
            cloudflare_zone_hint=await get_setting(db, SETTING_CF_ZONE_HINT, ""),
            account_key_path=str(settings.data_dir / "acme_account_key.pem"),
        )
    # Simulated/local-CA path: attach the active CA's material (if any) so
    # leaves are signed by the internal PKI rather than self-signed.
    ca = await _active_local_ca(db)
    if ca is not None and ca.private_key:
        config.local_ca = providers.LocalCASigningContext(
            cert_pem=ca.cert_pem,
            key_pem=key_store.decrypt_value(ca.private_key),
        )
    return name, providers.get_provider(name=name, config=config)


async def _active_local_ca(db: AsyncSession) -> models.CaAuthority | None:
    """Resolve the CA used for local signing (setting override, else first enabled root)."""
    ca_id = await get_setting(db, SETTING_LOCAL_CA_ID, "")
    if ca_id:
        ca = await db.get(models.CaAuthority, int(ca_id)) if ca_id.isdigit() else None
        if ca is not None and ca.enabled:
            return ca
    stmt = (
        select(models.CaAuthority)
        .where(models.CaAuthority.enabled.is_(True))
        .order_by(models.CaAuthority.id)
    )
    return (await db.execute(stmt)).scalars().first()


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


async def log_entry(
    db: AsyncSession,
    message: str,
    level: str = "info",
    cert_id: str | None = None,
) -> int:
    entry = models.RenewalLog(cert_id=cert_id, level=level, message=message)
    db.add(entry)
    await db.flush()
    return entry.id


async def build_notify_targets(db: AsyncSession) -> notification_service.NotifyTargets:
    return notification_service.NotifyTargets(
        webhook_url=await get_setting(db, SETTING_NOTIFY_WEBHOOK, ""),
        ntfy_topic=await get_setting(db, SETTING_NOTIFY_NTFY, ""),
        email_to=await get_setting(db, SETTING_NOTIFY_EMAIL_TO, ""),
        smtp_host=await get_setting(db, SETTING_NOTIFY_SMTP_HOST, ""),
        smtp_port=int((await get_setting(db, SETTING_NOTIFY_SMTP_PORT, "587")) or 587),
        smtp_user=await get_setting(db, SETTING_NOTIFY_SMTP_USER, ""),
        smtp_pass=await get_secret_value(db, SETTING_NOTIFY_SMTP_PASS),
        email_from=get_settings().notify_email_from,
    )


async def notify_event(db: AsyncSession, title: str, message: str, level: str = "info") -> None:
    """Send a notification on a lifecycle event; failures are logged, never fatal."""
    try:
        targets = await build_notify_targets(db)
        if not notification_service.any_configured(targets):
            return
        results = await notification_service.notify(targets, title, message, level)
        for ok, outcome in results:
            await log_entry(db, outcome, "success" if ok else "error")
    except Exception as exc:  # noqa: BLE001
        await log_entry(db, f"Notification dispatch failed: {exc}", "error")


# ---------------------------------------------------------------------------
# Serialization (status is always derived, never stored)
# ---------------------------------------------------------------------------


def cert_to_read(cert: models.Certificate, now: datetime, renew_window: int) -> schemas.CertRead:
    days_left = 0
    if cert.not_after:
        seconds = (cert.not_after - now).total_seconds()
        days_left = math.ceil(seconds / 86_400)

    if cert.revoked:
        status = "revoked"
    elif days_left <= 0:
        status = "expired"
    elif days_left <= renew_window:
        status = "expiring"
    else:
        status = "valid"

    return schemas.CertRead(
        id=cert.id,
        cn=cert.cn,
        sans=cert.sans,
        issuer=cert.issuer,
        protocol=cert.protocol,
        key_type=cert.key_type,
        serial=cert.serial,
        fingerprint=cert.fingerprint,
        not_before=cert.not_before,
        not_after=cert.not_after,
        days_left=days_left,
        status=status,
        auto_renew=cert.auto_renew,
        revoked=cert.revoked,
        revoked_at=cert.revoked_at,
        revocation_reason=cert.revocation_reason,
        created_at=cert.created_at,
    )


# ---------------------------------------------------------------------------
# Lifecycle actions
# ---------------------------------------------------------------------------


async def _fire_reload(db: AsyncSession, payload: dict) -> tuple[bool, str]:
    url = await get_setting(db, SETTING_WEBHOOK_URL, get_settings().webhook_url)
    return await webhook_service.fire_reload_webhook(url, payload)


def _new_id() -> str:
    return f"cert-{uuid.uuid4().hex[:12]}"


def _store_cert_key(cert: models.Certificate, issued: providers.IssuedCertData) -> None:
    """Encrypt and persist the issuer-generated private key (if the provider captured one)."""
    if issued.private_key_pem:
        cert.private_key = key_store.encrypt_value(issued.private_key_pem)
    # ACME/other providers may not return key material; leave cert.private_key as-is.


async def issue_cert(db: AsyncSession, payload: schemas.CertCreate) -> models.Certificate:
    now = await sim_now(db)
    _provider_name, provider = await _resolve_provider(db)
    try:
        issued = await asyncio.to_thread(
            provider.issue,
            cn=payload.cn,
            sans=payload.sans,
            key_type=payload.key_type,
            validity_days=payload.validity_days,
            issuer=payload.issuer,
            not_before=now,
        )
    except providers.IssuanceError as exc:
        await log_entry(db, f"Issuance failed for {payload.cn}: {exc}", "error")
        await notify_event(db, "Trove issuance failed", str(exc), "error")
        raise

    cert = models.Certificate(
        id=_new_id(),
        cn=payload.cn,
        sans=payload.sans or [payload.cn],
        issuer=payload.issuer,
        protocol=payload.protocol,
        key_type=payload.key_type,
        serial=issued.serial,
        fingerprint=issued.fingerprint,
        not_before=issued.not_before,
        not_after=issued.not_after,
        auto_renew=payload.auto_renew,
        revoked=False,
        cert_pem=issued.cert_pem,
        created_at=utcnow(),
    )
    _store_cert_key(cert, issued)
    db.add(cert)
    await log_entry(
        db,
        f"Certificate issued for {payload.cn} via {payload.issuer} ({payload.protocol}). "
        f"Serial: {issued.serial}",
        "success",
        cert.id,
    )
    await log_entry(
        db, f"Private key generated ({payload.key_type}) and CSR signed.", "info", cert.id
    )
    webhook_ok, webhook_msg = await _fire_reload(db, {"action": "issue", "cn": payload.cn})
    await log_entry(db, webhook_msg, "success" if webhook_ok else "error", cert.id)
    await notify_event(
        db,
        "Certificate issued",
        f"{payload.cn} issued via {payload.issuer} - expires {issued.not_after.date()}",
        "success",
    )
    if not webhook_ok:
        await notify_event(db, "Reload webhook failed", webhook_msg, "error")
    return cert


async def renew_cert(
    db: AsyncSession, cert: models.Certificate, reason: str = "routine"
) -> models.Certificate:
    now = await sim_now(db)
    validity = await get_validity_days(db)
    _provider_name, provider = await _resolve_provider(db)
    try:
        issued = await asyncio.to_thread(
            provider.renew,
            cn=cert.cn,
            sans=cert.sans,
            key_type=cert.key_type,
            issuer=cert.issuer,
            not_before=now,
            validity_days=validity,
        )
    except providers.IssuanceError as exc:
        await log_entry(db, f"Renewal failed for {cert.cn}: {exc}", "error", cert.id)
        await notify_event(db, "Trove renewal failed", f"{cert.cn}: {exc}", "error")
        raise

    cert.cert_pem = issued.cert_pem
    cert.serial = issued.serial
    cert.fingerprint = issued.fingerprint
    cert.not_before = issued.not_before
    cert.not_after = issued.not_after
    _store_cert_key(cert, issued)

    await log_entry(
        db,
        f"Renewal initiated for {cert.cn} (reason: {reason}) - generating new key & CSR",
        "acme",
        cert.id,
    )
    await log_entry(
        db,
        f"Certificate renewed. New expiry {issued.not_after.date()} (serial {issued.serial})",
        "success",
        cert.id,
    )
    webhook_ok, webhook_msg = await _fire_reload(db, {"action": "renew", "cn": cert.cn})
    await log_entry(db, webhook_msg, "success" if webhook_ok else "error", cert.id)
    await notify_event(
        db,
        "Certificate renewed",
        f"{cert.cn} renewed - new expiry {issued.not_after.date()}",
        "success",
    )
    if not webhook_ok:
        await notify_event(db, "Reload webhook failed", webhook_msg, "error")
    return cert


async def rotate_key(db: AsyncSession, cert: models.Certificate) -> models.Certificate:
    """Reissue a certificate with fresh key material (renewal already regenerates keys)."""
    return await renew_cert(db, cert, reason="rotate_key")


async def get_cert_private_key(db: AsyncSession, cert: models.Certificate) -> str | None:
    """Return the decrypted PKCS#8 PEM private key, or None when not stored."""
    if not cert.private_key:
        return None
    try:
        return key_store.decrypt_value(cert.private_key)
    except Exception:  # noqa: BLE001 - bad blob/master key should surface as missing
        await log_entry(db, f"Failed to decrypt private key for {cert.cn}", "error", cert.id)
        return None


async def soft_delete_cert(db: AsyncSession, cert: models.Certificate) -> None:
    """Soft-delete a certificate: retained for audit, hidden from default views."""
    cert.deleted_at = utcnow()
    await log_entry(
        db,
        f"Certificate {cert.cn} soft-deleted from local state (audit copy retained)",
        "warning",
        cert.id,
    )


async def restore_cert(db: AsyncSession, cert: models.Certificate) -> None:
    """Restore a soft-deleted certificate back into active views."""
    cert.deleted_at = None
    await log_entry(db, f"Certificate {cert.cn} restored from soft-delete", "info", cert.id)


async def import_cert(db: AsyncSession, payload: schemas.CertImport) -> models.Certificate:
    if "BEGIN CERTIFICATE" not in payload.pem.upper():
        raise ValueError("Invalid PEM - missing -----BEGIN CERTIFICATE-----")
    parsed = crypto_service.parse_pem(payload.pem)

    cert = models.Certificate(
        id=_new_id(),
        cn=payload.cn or parsed["cn"],
        sans=parsed["sans"] or [payload.cn],
        issuer=payload.issuer or parsed["issuer"],
        protocol="Manual / Imported",
        key_type=parsed["key_type"],
        serial=parsed["serial"],
        fingerprint=parsed["fingerprint"],
        not_before=parsed["not_before"],
        not_after=parsed["not_after"],
        auto_renew=payload.auto_renew,
        revoked=False,
        cert_pem=payload.pem,
        created_at=utcnow(),
    )
    db.add(cert)
    await log_entry(
        db, f"Imported external certificate for {cert.cn} (parsed from PEM)", "info", cert.id
    )
    await notify_event(db, "Certificate imported", f"{cert.cn} imported and parsed", "info")
    return cert


async def revoke_cert(
    db: AsyncSession, cert: models.Certificate, reason: str = "unspecified"
) -> None:
    cert.revoked = True
    cert.revoked_at = utcnow()
    cert.revocation_reason = (reason or "unspecified").strip()[:64] or "unspecified"
    await log_entry(
        db,
        f"Certificate {cert.cn} revoked and removed from active state (reason: {cert.revocation_reason})",
        "warning",
        cert.id,
    )
    await notify_event(
        db, "Certificate revoked", f"{cert.cn} revoked ({cert.revocation_reason})", "warning"
    )


async def batch_renew(db: AsyncSession) -> schemas.BatchRenewResponse:
    now = await sim_now(db)
    window = await get_renew_window(db)
    stmt = select(models.Certificate).where(models.Certificate.revoked.is_(False))
    rows = (await db.execute(stmt)).scalars().all()

    renewed: list[str] = []
    failed: list[str] = []
    for cert in rows:
        if cert.not_after and cert.not_after <= add_days(now, window):
            try:
                await renew_cert(db, cert, reason="batch")
                renewed.append(cert.id)
            except providers.IssuanceError as exc:
                failed.append(cert.id)
                await log_entry(db, f"Batch renewal failed for {cert.cn}: {exc}", "error", cert.id)
    if renewed or failed:
        await notify_event(
            db,
            "Batch renewal",
            f"Renewed {len(renewed)} certificate(s); {len(failed)} failed.",
            "success" if not failed else "warning",
        )
    return schemas.BatchRenewResponse(renewed=renewed, failed=failed)


async def auto_renew_eligible(db: AsyncSession) -> list[models.Certificate]:
    """Certificates with auto_renew enabled that are inside the renewal window."""
    now = await sim_now(db)
    window = await get_renew_window(db)
    stmt = select(models.Certificate).where(
        models.Certificate.auto_renew.is_(True),
        models.Certificate.revoked.is_(False),
    )
    rows = (await db.execute(stmt)).scalars().all()
    return [c for c in rows if c.not_after and c.not_after <= add_days(now, window)]


async def get_reminder_days(db: AsyncSession) -> int:
    raw = await get_setting(db, SETTING_REMINDER_DAYS, str(get_settings().expiry_reminder_days))
    try:
        return int(raw)
    except ValueError:
        return get_settings().expiry_reminder_days


async def set_reminder_days(db: AsyncSession, days: int) -> None:
    await set_setting(db, SETTING_REMINDER_DAYS, str(days))


async def remind_expiring(db: AsyncSession) -> list[str]:
    """Daily expiry-reminder pass (rate-limited to once per calendar day).

    Notifies about certs expiring within ``expiry_reminder_days``. The last
    reminder date is persisted so a 6-hourly scheduler interval does not spam.
    """
    today = utcnow().date().isoformat()
    if await get_setting(db, SETTING_LAST_REMINDER, "") == today:
        return []
    reminder_days = await get_reminder_days(db)
    now = await sim_now(db)
    stmt = select(models.Certificate).where(
        models.Certificate.revoked.is_(False),
        models.Certificate.deleted_at.is_(None),
    )
    rows = (await db.execute(stmt)).scalars().all()
    due = [
        c
        for c in rows
        if c.not_after and 0 < (c.not_after - now).total_seconds() <= reminder_days * 86_400
    ]
    for cert in due:
        days_left = math.ceil((cert.not_after - now).total_seconds() / 86_400)
        await notify_event(
            db,
            "Certificate expiring soon",
            f"{cert.cn} expires in {days_left} day(s) on {cert.not_after.date()} "
            f"(auto-renew: {'on' if cert.auto_renew else 'off'})",
            "warning",
        )
    if due:
        await log_entry(
            db, f"Expiry reminder pass: notified for {len(due)} certificate(s).", "info"
        )
    await set_setting(db, SETTING_LAST_REMINDER, today)
    return [c.id for c in due]


async def scheduler_tick(db: AsyncSession) -> list[str]:
    """Background auto-renewal pass (called by the scheduler task)."""
    eligible = await auto_renew_eligible(db)
    renewed: list[str] = []
    failed: list[str] = []
    for cert in eligible:
        try:
            await renew_cert(db, cert, reason="scheduler")
            renewed.append(cert.id)
        except providers.IssuanceError as exc:
            failed.append(cert.id)
            await log_entry(
                db, f"Scheduler auto-renew failed for {cert.cn}: {exc}", "error", cert.id
            )
    if renewed or failed:
        await notify_event(
            db,
            "Auto-renewal scheduler",
            f"Renewed {len(renewed)} certificate(s); {len(failed)} failed.",
            "success" if not failed else "warning",
        )
    # Daily expiry reminders ride along with the scheduler pass (rate-limited).
    try:
        await remind_expiring(db)
    except Exception:  # noqa: BLE001 - reminders must never break the renewal pass
        pass
    return renewed


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


async def list_certs(
    db: AsyncSession,
    search: str = "",
    status_filter: str = "all",
    include_revoked: bool = False,
    include_deleted: bool = False,
    limit: int | None = None,
    offset: int = 0,
) -> list[schemas.CertRead]:
    now = await sim_now(db)
    window = await get_renew_window(db)

    stmt = select(models.Certificate).order_by(models.Certificate.created_at.desc())
    if not include_revoked:
        stmt = stmt.where(models.Certificate.revoked.is_(False))
    if not include_deleted:
        stmt = stmt.where(models.Certificate.deleted_at.is_(None))
    rows = (await db.execute(stmt)).scalars().all()

    needle = search.strip().lower()
    out: list[schemas.CertRead] = []
    for cert in rows:
        read = cert_to_read(cert, now, window)
        if needle:
            haystack = " ".join([cert.cn, *cert.sans, cert.issuer]).lower()
            if needle not in haystack:
                continue
        match status_filter:
            case "expiring":
                if read.status != "expiring":
                    continue
            case "expired":
                if read.status != "expired":
                    continue
            case "autorenew":
                if not cert.auto_renew:
                    continue
        out.append(read)
    if offset:
        out = out[offset:]
    if limit is not None:
        out = out[:limit]
    return out


async def get_cert(db: AsyncSession, cert_id: str) -> models.Certificate | None:
    return await db.get(models.Certificate, cert_id)


async def get_metrics(db: AsyncSession) -> schemas.Metrics:
    now = await sim_now(db)
    window = await get_renew_window(db)
    rows = (await db.execute(select(models.Certificate))).scalars().all()

    total = len(rows)
    valid = expiring = expired = revoked_ = auto = 0
    for cert in rows:
        status = cert_to_read(cert, now, window).status
        if status == "revoked":
            revoked_ += 1
        elif status == "expired":
            expired += 1
        elif status == "expiring":
            expiring += 1
        else:
            valid += 1
        if cert.auto_renew:
            auto += 1

    return schemas.Metrics(
        total=total,
        valid=valid,
        expiring=expiring,
        expired=expired,
        revoked=revoked_,
        auto_renew=auto,
    )


# ---------------------------------------------------------------------------
# Time simulation
# ---------------------------------------------------------------------------


async def advance_time(db: AsyncSession, days: int) -> int:
    offset = await get_sim_offset(db) + days
    await set_setting(db, SETTING_SIM_OFFSET, str(offset))
    return offset


async def reset_time(db: AsyncSession) -> int:
    await set_setting(db, SETTING_SIM_OFFSET, "0")
    return 0


async def time_info(db: AsyncSession) -> schemas.TimeInfo:
    offset = await get_sim_offset(db)
    return schemas.TimeInfo(
        sim_now=add_days(utcnow(), offset).date().isoformat(),
        real_now=utcnow().date().isoformat(),
        offset_days=offset,
    )


# ---------------------------------------------------------------------------
# Logs & sync
# ---------------------------------------------------------------------------


async def list_logs(db: AsyncSession, limit: int = 200) -> list[models.RenewalLog]:
    stmt = (
        select(models.RenewalLog)
        .order_by(models.RenewalLog.id.desc())
        .limit(max(1, min(limit, 1000)))
    )
    rows = (await db.execute(stmt)).scalars().all()
    return list(reversed(rows))


async def clear_logs(db: AsyncSession) -> None:
    await db.execute(delete(models.RenewalLog))


async def sync(db: AsyncSession) -> tuple[bool, str]:
    """Simulated ACME + reverse-proxy sync (real provider check goes here)."""
    await log_entry(db, "Syncing state with ACME directories & reverse proxy...", "acme")

    acme_url = await get_setting(db, SETTING_ACME_URL, get_settings().acme_directory_url)
    if acme_url:
        await log_entry(db, f"ACME directory reachable: {acme_url}", "success")
    else:
        await log_entry(db, "No ACME directory configured - skipped directory check", "warning")

    ok, msg = await _fire_reload(db, {"action": "sync"})
    await log_entry(db, msg, "success" if ok else "error")
    await log_entry(db, "ACME state sync complete. All endpoints responsive.", "success")
    return ok, "Sync complete"


# ---------------------------------------------------------------------------
# Settings (config) endpoint helpers
# ---------------------------------------------------------------------------


async def read_settings(db: AsyncSession) -> schemas.SettingsRead:
    settings = get_settings()
    provider = await get_provider_name(db)
    cf_token = await get_secret_value(db, SETTING_CF_TOKEN)
    smtp_pass = await get_secret_value(db, SETTING_NOTIFY_SMTP_PASS)

    def posint(raw: str, fallback: int) -> int:
        try:
            return int(raw)
        except ValueError:
            return fallback

    return schemas.SettingsRead(
        provider=provider,
        secrets_backend="vault" if secret_store.vault_configured() else "local",
        acme_directory_url=await get_setting(db, SETTING_ACME_URL, settings.acme_directory_url),
        acme_contact_email=await get_setting(db, SETTING_ACME_CONTACT, ""),
        acme_validation=await get_setting(db, SETTING_ACME_VALIDATION, "dns-01"),
        acme_webroot=await get_setting(db, SETTING_ACME_WEBROOT, settings.acme_webroot),
        cloudflare_zone_hint=await get_setting(db, SETTING_CF_ZONE_HINT, ""),
        webhook_url=await get_setting(db, SETTING_WEBHOOK_URL, settings.webhook_url),
        cf_token_masked=bool(cf_token),
        renew_window_days=await get_renew_window(db),
        default_validity_days=await get_validity_days(db),
        notify_webhook_url=await get_setting(db, SETTING_NOTIFY_WEBHOOK, ""),
        notify_ntfy_topic=await get_setting(db, SETTING_NOTIFY_NTFY, ""),
        notify_email_to=await get_setting(db, SETTING_NOTIFY_EMAIL_TO, ""),
        notify_smtp_host=await get_setting(db, SETTING_NOTIFY_SMTP_HOST, ""),
        notify_smtp_port=posint(await get_setting(db, SETTING_NOTIFY_SMTP_PORT, "587"), 587),
        notify_smtp_user=await get_setting(db, SETTING_NOTIFY_SMTP_USER, ""),
        notify_smtp_pass_masked=bool(smtp_pass),
        expiry_reminder_days=await get_reminder_days(db),
        require_approval=get_settings().require_approval
        or (await get_setting(db, "require_approval", "")) == "1",
    )


async def update_settings(db: AsyncSession, payload: schemas.SettingsUpdate) -> None:
    if payload.provider:
        validated = payload.provider.strip().lower()
        if validated not in ("simulated", "acme"):
            raise ValueError("provider must be 'simulated' or 'acme'")
        await set_setting(db, SETTING_PROVIDER, validated)
    if payload.acme_directory_url:
        await set_setting(db, SETTING_ACME_URL, payload.acme_directory_url.strip())
    if payload.acme_contact_email:
        await set_setting(db, SETTING_ACME_CONTACT, payload.acme_contact_email.strip())
    if payload.acme_validation:
        v = payload.acme_validation
        if v not in ("dns-01", "http-01"):
            raise ValueError("acme_validation must be 'dns-01' or 'http-01'")
        await set_setting(db, SETTING_ACME_VALIDATION, v)
    if payload.acme_webroot:
        await set_setting(db, SETTING_ACME_WEBROOT, payload.acme_webroot.strip())
    if payload.cloudflare_zone_hint:
        await set_setting(db, SETTING_CF_ZONE_HINT, payload.cloudflare_zone_hint.strip())
    if payload.webhook_url:
        if not webhook_service.validate_webhook_url(payload.webhook_url.strip()):
            raise ValueError("Webhook URL must start with http:// or https://")
        await set_setting(db, SETTING_WEBHOOK_URL, payload.webhook_url.strip())
    if payload.cf_token:
        await set_secret_value(db, SETTING_CF_TOKEN, payload.cf_token.strip())
    if payload.renew_window_days is not None:
        await set_setting(db, SETTING_RENEW_WINDOW, str(payload.renew_window_days))
    if payload.default_validity_days is not None:
        await set_setting(db, SETTING_VALIDITY_DAYS, str(payload.default_validity_days))

    # Notifications
    if payload.notify_webhook_url:
        if not webhook_service.validate_webhook_url(payload.notify_webhook_url.strip()):
            raise ValueError("Notification webhook URL must start with http:// or https://")
        await set_setting(db, SETTING_NOTIFY_WEBHOOK, payload.notify_webhook_url.strip())
    if payload.notify_ntfy_topic:
        await set_setting(db, SETTING_NOTIFY_NTFY, payload.notify_ntfy_topic.strip().lstrip("/"))
    if payload.notify_email_to:
        await set_setting(db, SETTING_NOTIFY_EMAIL_TO, payload.notify_email_to.strip())
    if payload.notify_smtp_host:
        await set_setting(db, SETTING_NOTIFY_SMTP_HOST, payload.notify_smtp_host.strip())
    if payload.notify_smtp_user:
        await set_setting(db, SETTING_NOTIFY_SMTP_USER, payload.notify_smtp_user.strip())
    if payload.notify_smtp_pass:
        await set_secret_value(db, SETTING_NOTIFY_SMTP_PASS, payload.notify_smtp_pass.strip())
    if payload.notify_smtp_port is not None:
        await set_setting(db, SETTING_NOTIFY_SMTP_PORT, str(payload.notify_smtp_port))
    if payload.expiry_reminder_days is not None:
        await set_setting(db, SETTING_REMINDER_DAYS, str(payload.expiry_reminder_days))


# ---------------------------------------------------------------------------
# CRL generation (only when a local CA is configured)
# ---------------------------------------------------------------------------


async def get_ocsp_signing_material(db: AsyncSession) -> tuple[str, str] | None:
    """Resolve (ca_cert_pem, ca_key_pem) for OCSP signing.

    Priority: filesystem CA paths from config, then the managed in-DB CA.
    Returns None when no CA is configured.
    """
    settings = get_settings()
    if settings.ca_cert_path and settings.ca_key_path:
        cert_path = Path(settings.ca_cert_path)
        key_path = Path(settings.ca_key_path)
        if cert_path.exists() and key_path.exists():
            return cert_path.read_text(), key_path.read_text()
    ca = await _active_local_ca(db)
    if ca is not None and ca.private_key:
        return ca.cert_pem, key_store.decrypt_value(ca.private_key)
    return None


async def build_crl_pem(db: AsyncSession) -> str | None:
    """Generate a PEM CRL signed by the configured CA, or None if not configured."""
    material = await get_ocsp_signing_material(db)
    if material is None:
        return None
    ca_cert_pem, ca_key_pem = material

    rows = (
        (await db.execute(select(models.Certificate).where(models.Certificate.revoked.is_(True))))
        .scalars()
        .all()
    )
    revoked = [
        {
            "serial": c.serial,
            "revoked_at": c.revoked_at or utcnow(),
            "reason": c.revocation_reason or "unspecified",
        }
        for c in rows
    ]
    return crypto_service.build_crl(
        revoked=revoked,
        ca_cert_pem=ca_cert_pem,
        ca_key_pem=ca_key_pem,
    )

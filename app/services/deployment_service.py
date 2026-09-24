"""Certificate deployment: push issued material to hosts over SSH/SFTP.

A :class:`DeploymentTarget` pins where a certificate's PEM, private key and
full chain land on a remote host, plus an optional reload command and webhook
run after a successful push. Keys/credentials are stored in SQLite encrypted
via :mod:`app.services.key_store` (AES-256-GCM), decrypted only in-memory at
deploy time.

Each attempt writes a :class:`~app.models.DeploymentRecord` audit row. SSH host
keys are not pinned (``known_hosts=None``) - fine for a homelab, documented in
``docs/DEPLOYMENT.md``.
"""

from __future__ import annotations

import asyncio
import time

import asyncssh
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import models, schemas
from ..timeutil import utcnow
from . import key_store, webhook_service

SSH_TIMEOUT = 30.0
_DETAIL_TRUNCATE = 2000


class DeploymentError(RuntimeError):
    """Raised when a deploy attempt cannot complete."""


# ---------------------------------------------------------------------------
# Target CRUD
# ---------------------------------------------------------------------------


def _scrub(raw: str) -> str:
    return (raw or "").strip()


async def list_targets(db: AsyncSession) -> list[models.DeploymentTarget]:
    stmt = select(models.DeploymentTarget).order_by(models.DeploymentTarget.name)
    return list((await db.execute(stmt)).scalars().all())


async def get_target(db: AsyncSession, target_id: int) -> models.DeploymentTarget | None:
    return await db.get(models.DeploymentTarget, target_id)


async def get_cert(db: AsyncSession, cert_id: str) -> models.Certificate | None:
    return await db.get(models.Certificate, cert_id)


async def create_target(
    db: AsyncSession, payload: schemas.DeploymentTargetCreate
) -> models.DeploymentTarget:
    ssh_key = payload.ssh_private_key.strip()
    ssh_pass = payload.ssh_password.strip()
    target = models.DeploymentTarget(
        name=_scrub(payload.name),
        host=_scrub(payload.host),
        port=payload.port,
        ssh_user=_scrub(payload.ssh_user),
        ssh_private_key=key_store.encrypt_value(payload.ssh_private_key) if ssh_key else None,
        ssh_password=key_store.encrypt_value(payload.ssh_password) if ssh_pass else None,
        cert_path=_scrub(payload.cert_path),
        key_path=_scrub(payload.key_path),
        chain_path=_scrub(payload.chain_path),
        reload_command=_scrub(payload.reload_command),
        webhook_url=_scrub(payload.webhook_url),
        auto_deploy=payload.auto_deploy,
        enabled=payload.enabled,
        created_at=utcnow(),
    )
    if not (target.ssh_private_key or target.ssh_password or target.webhook_url):
        raise DeploymentError("target needs SSH credentials or a webhook URL")
    db.add(target)
    await db.flush()
    return target


async def update_target(
    db: AsyncSession, target: models.DeploymentTarget, payload: schemas.DeploymentTargetUpdate
) -> models.DeploymentTarget:
    if payload.name is not None:
        target.name = _scrub(payload.name)
    if payload.host is not None:
        target.host = _scrub(payload.host)
    if payload.port is not None:
        target.port = payload.port
    if payload.ssh_user is not None:
        target.ssh_user = _scrub(payload.ssh_user)
    if payload.ssh_private_key:
        target.ssh_private_key = key_store.encrypt_value(payload.ssh_private_key)
    if payload.ssh_password:
        target.ssh_password = key_store.encrypt_value(payload.ssh_password)
    if payload.cert_path:
        target.cert_path = _scrub(payload.cert_path)
    if payload.key_path:
        target.key_path = _scrub(payload.key_path)
    if payload.chain_path:
        target.chain_path = _scrub(payload.chain_path)
    if payload.reload_command is not None:
        target.reload_command = _scrub(payload.reload_command)
    if payload.webhook_url is not None:
        target.webhook_url = _scrub(payload.webhook_url)
    if payload.auto_deploy is not None:
        target.auto_deploy = payload.auto_deploy
    if payload.enabled is not None:
        target.enabled = payload.enabled
    return target


async def delete_target(db: AsyncSession, target: models.DeploymentTarget) -> None:
    await db.delete(target)


def target_has_ssh(target: models.DeploymentTarget) -> bool:
    return bool(target.ssh_private_key or target.ssh_password)


# ---------------------------------------------------------------------------
# Assignment (cert <-> targets)
# ---------------------------------------------------------------------------


async def assign_target(db: AsyncSession, cert_id: str, target_id: int) -> bool:
    exists = (
        await db.execute(
            select(models.CertDeploymentTarget).where(
                models.CertDeploymentTarget.cert_id == cert_id,
                models.CertDeploymentTarget.target_id == target_id,
            )
        )
    ).scalar_one_or_none()
    if exists:
        return False
    db.add(models.CertDeploymentTarget(cert_id=cert_id, target_id=target_id))
    await db.flush()
    return True


async def unassign_target(db: AsyncSession, cert_id: str, target_id: int) -> bool:
    row = (
        await db.execute(
            select(models.CertDeploymentTarget).where(
                models.CertDeploymentTarget.cert_id == cert_id,
                models.CertDeploymentTarget.target_id == target_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return False
    await db.delete(row)
    await db.flush()
    return True


async def assigned_target_ids(db: AsyncSession, cert_id: str) -> list[int]:
    stmt = select(models.CertDeploymentTarget.target_id).where(
        models.CertDeploymentTarget.cert_id == cert_id
    )
    return list((await db.execute(stmt)).scalars().all())


async def assigned_targets(db: AsyncSession, cert_id: str) -> list[models.DeploymentTarget]:
    ids = await assigned_target_ids(db, cert_id)
    if not ids:
        return []
    stmt = select(models.DeploymentTarget).where(models.DeploymentTarget.id.in_(ids))
    return list((await db.execute(stmt)).scalars().all())


# ---------------------------------------------------------------------------
# Chain construction
# ---------------------------------------------------------------------------


async def build_fullchain(db: AsyncSession, cert: models.Certificate) -> str:
    """Leaf PEM plus the managed-CA issuer chain (when the issuer is local)."""
    parts: list[str] = [cert.cert_pem or ""]
    ca = await _find_ca_by_issuer(db, cert.issuer or "")
    while ca is not None:
        parts.append(ca.cert_pem)
        if not ca.parent_id or ca.kind == "root":
            break
        ca = await db.get(models.CaAuthority, ca.parent_id)
    return "\n".join(p for p in parts if p).strip()


async def _find_ca_by_issuer(db: AsyncSession, issuer: str) -> models.CaAuthority | None:
    if not issuer:
        return None
    stmt = select(models.CaAuthority).where(models.CaAuthority.cn == issuer)
    return (await db.execute(stmt)).scalars().first()


# ---------------------------------------------------------------------------
# SSH transport (asyncssh)
# ---------------------------------------------------------------------------


async def _ssh_connect(target: models.DeploymentTarget) -> asyncssh.SSHClientConnection:
    kwargs: dict = {"known_hosts": None, "connect_timeout": min(SSH_TIMEOUT, 15.0)}
    if target.ssh_private_key:
        kwargs["client_keys"] = [key_store.decrypt_value(target.ssh_private_key)]
    if target.ssh_password:
        kwargs["password"] = key_store.decrypt_value(target.ssh_password)
    return await asyncio.wait_for(
        asyncssh.connect(target.host, port=target.port, username=target.ssh_user, **kwargs),
        timeout=SSH_TIMEOUT,
    )


async def _ssh_put_bytes(
    conn: asyncssh.SSHClientConnection, remote_path: str, data: bytes, mode: int
) -> str:
    """Write ``data`` to ``remote_path`` atomically (tmp file -> rename)."""
    async with conn.start_sftp_client() as sftp:
        tmp = f"{remote_path}.trove_tmp"
        async with sftp.open(tmp, "wb") as fh:
            await fh.write(data)
        try:
            await sftp.chmod(tmp, mode)
        except asyncssh.sftp.SFTPError:
            pass
        try:
            await sftp.stat(remote_path)
            await sftp.remove(remote_path)
        except (asyncssh.sftp.SFTPError, OSError):
            pass
        await sftp.rename(tmp, remote_path)
    return remote_path


async def _ssh_run(conn: asyncssh.SSHClientConnection, command: str) -> str:
    result = await conn.run(command, check=False)
    output = (f"{result.stdout}\n{result.stderr}").strip()
    if result.exit_status not in (0, None):
        raise DeploymentError(f"reload command exited {result.exit_status}: {output[-500:]}")
    return output or "ok"


# ---------------------------------------------------------------------------
# Logging/notifications (deferred imports avoid a cycle with cert_service)
# ---------------------------------------------------------------------------


async def _log(
    db: AsyncSession, message: str, level: str = "info", cert_id: str | None = None
) -> int:
    from .cert_service import log_entry  # deferred: avoids an import cycle

    return await log_entry(db, message, level, cert_id)


async def _notify(db: AsyncSession, title: str, message: str, level: str = "info") -> None:
    from .cert_service import notify_event  # deferred: avoids an import cycle

    await notify_event(db, title, message, level)


# ---------------------------------------------------------------------------
# Deploy
# ---------------------------------------------------------------------------


async def deploy_cert(
    db: AsyncSession,
    cert: models.Certificate,
    target: models.DeploymentTarget,
) -> models.DeploymentRecord:
    """Deploy ``cert`` to ``target``; always returns a record (never raises)."""
    started = time.perf_counter()

    try:
        if not cert.cert_pem:
            raise DeploymentError("certificate has no PEM body stored")
        key_pem = key_store.decrypt_value(cert.private_key) if cert.private_key else None

        if target_has_ssh(target) and not key_pem:
            raise DeploymentError(
                "no private key stored for this certificate - cannot deploy over SSH "
                "(ACME-managed keys stay with the CA; use a webhook-only target instead)"
            )

        chain_pem = await build_fullchain(db, cert)

        conn = None
        if target_has_ssh(target):
            conn = await _ssh_connect(target)
        try:
            if conn is not None:
                pushes: list[tuple[str, bytes, int]] = []
                if target.cert_path:
                    pushes.append((target.cert_path, cert.cert_pem.encode(), 0o644))
                if key_pem and target.key_path:
                    pushes.append((target.key_path, key_pem.encode(), 0o600))
                if chain_pem and target.chain_path:
                    pushes.append((target.chain_path, chain_pem.encode(), 0o644))
                if not pushes:
                    raise DeploymentError("target has no cert/key/chain paths configured")
                for path, data, mode in pushes:
                    await asyncio.wait_for(
                        _ssh_put_bytes(conn, path, data, mode), timeout=SSH_TIMEOUT
                    )
                if target.reload_command:
                    await asyncio.wait_for(
                        _ssh_run(conn, target.reload_command), timeout=SSH_TIMEOUT
                    )
        finally:
            if conn is not None:
                conn.close()

        if target.webhook_url:
            ok, msg = await webhook_service.fire_reload_webhook(
                target.webhook_url,
                {
                    "action": "deploy",
                    "target": target.name,
                    "cn": cert.cn,
                    "serial": cert.serial,
                    "cert": cert.cert_pem,
                    "chain": chain_pem,
                },
            )
            if not ok:
                raise DeploymentError(msg)

        transport = "over SSH" if conn is not None else "via webhook"
        state = "success"
        detail = f"Deployed to {target.name} {transport} on {target.host}"
    except Exception as exc:  # noqa: BLE001 - every failure must land in a record
        state = "failed"
        detail = f"Deploy to {target.name} failed: {exc}"

    duration_ms = int((time.perf_counter() - started) * 1000)
    record = models.DeploymentRecord(
        cert_id=cert.id,
        target_id=target.id,
        serial=cert.serial,
        state=state,
        detail=detail[:_DETAIL_TRUNCATE],
        duration_ms=duration_ms,
        created_at=utcnow(),
    )
    db.add(record)
    await db.flush()
    await _log(
        db,
        detail,
        "success" if state == "success" else "error",
        cert.id,
    )
    return record


async def deploy_cert_targets(
    db: AsyncSession, cert: models.Certificate, reason: str
) -> list[models.DeploymentRecord]:
    """Deploy to every assigned, enabled target.

    ``reason="manual"`` ignores the target's ``auto_deploy`` flag (explicit
    redeploy); any other reason honors it. Used as the post-issue/post-renew
    hook and by the manual deploy endpoint.
    """
    targets = await assigned_targets(db, cert.id)
    eligible = [t for t in targets if t.enabled and (reason == "manual" or t.auto_deploy)]
    records: list[models.DeploymentRecord] = []
    for target in eligible:
        records.append(await deploy_cert(db, cert, target))
    await _log(db, f"Deployment pass for {cert.cn} ({reason}): {len(records)} target(s).")
    failed = [r for r in records if r.state != "success"]
    if failed:
        await _notify(
            db,
            "Certificate deployment failed",
            f"{cert.cn}: {len(failed)}/{len(records)} target(s) failed - check deployment records",
            "error",
        )
    return records


# ---------------------------------------------------------------------------
# History / status
# ---------------------------------------------------------------------------


async def last_record(
    db: AsyncSession, cert_id: str, target_id: int
) -> models.DeploymentRecord | None:
    stmt = (
        select(models.DeploymentRecord)
        .where(
            models.DeploymentRecord.cert_id == cert_id,
            models.DeploymentRecord.target_id == target_id,
        )
        .order_by(models.DeploymentRecord.id.desc())
        .limit(1)
    )
    return (await db.execute(stmt)).scalars().first()


async def list_records(
    db: AsyncSession, cert_id: str, limit: int = 50
) -> list[models.DeploymentRecord]:
    stmt = (
        select(models.DeploymentRecord)
        .where(models.DeploymentRecord.cert_id == cert_id)
        .order_by(models.DeploymentRecord.id.desc())
        .limit(max(1, min(limit, 500)))
    )
    return list(reversed((await db.execute(stmt)).scalars().all()))

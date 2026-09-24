"""Seed a small demo dataset on first run (relative to today)."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import models
from .services import providers
from .timeutil import add_days, utcnow

SEED_CERTS = [
    {
        "cn": "*.home.arpa",
        "sans": ["home.arpa", "vault.home.arpa", "proxmox.home.arpa"],
        "issuer": "Let's Encrypt",
        "protocol": "ACME DNS-01 (Cloudflare)",
        "key_type": "ECDSA P-256",
        "validity_days": 90,
        "days_until_expiry": 38,
        "auto_renew": True,
    },
    {
        "cn": "caddy.local.lan",
        "sans": ["caddy.local.lan", "ingress.local.lan"],
        "issuer": "step-ca (Internal PKI)",
        "protocol": "Internal CA API",
        "key_type": "ECDSA P-256",
        "validity_days": 90,
        "days_until_expiry": 6,
        "auto_renew": True,
    },
    {
        "cn": "pve-node01.datacenter.lan",
        "sans": ["pve-node01.datacenter.lan"],
        "issuer": "Self-Signed",
        "protocol": "Manual",
        "key_type": "RSA 2048",
        "validity_days": 365,
        "days_until_expiry": -21,
        "auto_renew": False,
    },
    {
        "cn": "homeassistant.local",
        "sans": ["homeassistant.local", "ha.lan"],
        "issuer": "Let's Encrypt",
        "protocol": "ACME HTTP-01 (Caddy/Nginx)",
        "key_type": "ECDSA P-256",
        "validity_days": 90,
        "days_until_expiry": 21,
        "auto_renew": True,
    },
    {
        "cn": "vaultwarden.internal",
        "sans": ["vaultwarden.internal"],
        "issuer": "Vault PKI Engine",
        "protocol": "Internal CA API",
        "key_type": "RSA 4096",
        "validity_days": 30,
        "days_until_expiry": 9,
        "auto_renew": True,
    },
]


async def seed_if_empty(db: AsyncSession) -> None:
    count = await db.scalar(select(func.count()).select_from(models.Certificate))
    if count:
        return

    provider = providers.get_provider()
    now = utcnow()
    for entry in SEED_CERTS:
        not_after = add_days(now, entry["days_until_expiry"])
        not_before = add_days(not_after, -entry["validity_days"])
        issued = provider.issue(
            cn=entry["cn"],
            sans=entry["sans"],
            key_type=entry["key_type"],
            validity_days=entry["validity_days"],
            issuer=entry["issuer"],
            not_before=not_before,
        )
        db.add(
            models.Certificate(
                id=f"seed-{uuid.uuid4().hex[:10]}",
                cn=entry["cn"],
                sans=entry["sans"],
                issuer=entry["issuer"],
                protocol=entry["protocol"],
                key_type=entry["key_type"],
                serial=issued.serial,
                fingerprint=issued.fingerprint,
                not_before=issued.not_before,
                not_after=issued.not_after,
                auto_renew=entry["auto_renew"],
                revoked=False,
                cert_pem=issued.cert_pem,
                created_at=now,
            )
        )
    await db.commit()

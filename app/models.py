from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base
from .timeutil import utcnow


class Certificate(Base):
    __tablename__ = "certificates"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    cn: Mapped[str] = mapped_column(String(255), index=True)
    sans: Mapped[list] = mapped_column(JSON, default=list)
    issuer: Mapped[str] = mapped_column(String(255))
    protocol: Mapped[str] = mapped_column(String(255))
    key_type: Mapped[str] = mapped_column(String(64), default="ECDSA P-256")
    serial: Mapped[str] = mapped_column(String(255), default="")
    fingerprint: Mapped[str] = mapped_column(String(255), default="")
    not_before: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    not_after: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    auto_renew: Mapped[bool] = mapped_column(Boolean, default=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revocation_reason: Mapped[str] = mapped_column(String(64), default="")
    cert_pem: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: AES-256-GCM encrypted PKCS#8 private key (see services.key_store).
    private_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Soft-delete marker (rows are retained for audit, excluded from views).
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class RenewalLog(Base):
    __tablename__ = "renewal_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cert_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("certificates.id", ondelete="SET NULL"), nullable=True
    )
    level: Mapped[str] = mapped_column(String(16), default="info")
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16), default="viewer")  # admin | operator | viewer
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ApiToken(Base):
    __tablename__ = "api_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    role: Mapped[str] = mapped_column(String(16), default="viewer")  # optional scoped override
    label: Mapped[str] = mapped_column(String(64), default="")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


# ---------------------------------------------------------------------------
# API Gateway subsystem (Kong-style: gateways -> routes -> consumers -> keys)
# Gates are exposed at /gw/<slug>/... and may mint ACME certs for a domain.
# ---------------------------------------------------------------------------


class Gateway(Base):
    __tablename__ = "gateways"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    base_url: Mapped[str] = mapped_column(String(255), default="")  # public URL (display)
    tls_domain: Mapped[str] = mapped_column(String(255), default="")  # hostname to protect
    tls_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    issuer: Mapped[str] = mapped_column(String(255), default="Let's Encrypt")
    cert_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("certificates.id", ondelete="SET NULL"), nullable=True
    )
    tls_status: Mapped[str] = mapped_column(
        String(16), default="none"
    )  # none|issuing|valid|expiring|expired|failed
    tls_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class GatewayRoute(Base):
    __tablename__ = "gateway_routes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    gateway_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("gateways.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(128))
    methods: Mapped[list] = mapped_column(JSON, default=list)  # e.g. ["GET","POST"]
    path: Mapped[str] = mapped_column(String(255))  # sub-path after /gw/<slug>, e.g. "/v1/*"
    upstream_url: Mapped[str] = mapped_column(String(255))  # http://host:port
    strip_prefix: Mapped[bool] = mapped_column(Boolean, default=False)
    timeout_ms: Mapped[int] = mapped_column(Integer, default=30_000)
    auth_mode: Mapped[str] = mapped_column(String(16), default="open")  # open|api_key
    rate_limit_rpm: Mapped[int] = mapped_column(Integer, default=0)  # 0 = unlimited
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class GatewayConsumer(Base):
    __tablename__ = "gateway_consumers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    gateway_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("gateways.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class GatewayApiKey(Base):
    __tablename__ = "gateway_api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    consumer_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("gateway_consumers.id", ondelete="CASCADE"), index=True
    )
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)  # sha256 hex
    key_prefix: Mapped[str] = mapped_column(String(24), default="")  # display prefix only
    label: Mapped[str] = mapped_column(String(64), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class GatewayLog(Base):
    __tablename__ = "gateway_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    gateway_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("gateways.id", ondelete="SET NULL"), nullable=True, index=True
    )
    route_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("gateway_routes.id", ondelete="SET NULL"), nullable=True
    )
    consumer_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("gateway_consumers.id", ondelete="SET NULL"), nullable=True
    )
    method: Mapped[str] = mapped_column(String(8), default="GET")
    path: Mapped[str] = mapped_column(String(512), default="")
    status: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    client: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


# ---------------------------------------------------------------------------
# Issuance approval workflow
# ---------------------------------------------------------------------------


class IssueRequest(Base):
    """A pending issuance request waiting for an admin decision.

    When ``require_approval`` is enabled, ``POST /api/certs`` no longer issues
    directly - it creates one of these rows in ``pending`` state. An admin
    approves (request is issued) or denies (recorded for audit).
    """

    __tablename__ = "issue_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cn: Mapped[str] = mapped_column(String(255), index=True)
    sans: Mapped[list] = mapped_column(JSON, default=list)
    issuer: Mapped[str] = mapped_column(String(255))
    protocol: Mapped[str] = mapped_column(String(255))
    key_type: Mapped[str] = mapped_column(String(64), default="ECDSA P-256")
    validity_days: Mapped[int] = mapped_column(Integer, default=90)
    auto_renew: Mapped[bool] = mapped_column(Boolean, default=True)
    requested_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|approved|denied
    denial_reason: Mapped[str] = mapped_column(String(255), default="")
    cert_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("certificates.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(64), nullable=True)


# ---------------------------------------------------------------------------
# Local CA (internal PKI) authorities
# ---------------------------------------------------------------------------


class CaAuthority(Base):
    """A locally-managed certificate authority (root or intermediate).

    The CA certificate is stored in clear; the CA's private key is stored
    encrypted at rest through ``services.key_store`` (AES-256-GCM).
    """

    __tablename__ = "ca_authorities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128))
    cn: Mapped[str] = mapped_column(String(255), index=True)
    kind: Mapped[str] = mapped_column(String(16), default="root")  # root|intermediate
    parent_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("ca_authorities.id", ondelete="SET NULL"), nullable=True
    )
    key_type: Mapped[str] = mapped_column(String(64), default="ECDSA P-256")
    serial: Mapped[str] = mapped_column(String(255), default="")
    fingerprint: Mapped[str] = mapped_column(String(255), default="")
    not_before: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    not_after: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cert_pem: Mapped[str] = mapped_column(Text)
    #: AES-256-GCM encrypted PKCS#8 private key (see services.key_store).
    private_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    #: Copies of the cert PEM for roots this CA will embed in chains.
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


# ---------------------------------------------------------------------------
# Certificate deployment (push issued material to hosts / webservers)
# ---------------------------------------------------------------------------


class DeploymentTarget(Base):
    """A host that receives pushed certificate material.

    Auth is either an encrypted SSH private key (``ssh_private_key``) or an
    encrypted password (``ssh_password``); both go through ``services.key_store``
    (AES-256-GCM) so plaintext credentials never touch SQLite. When only
    ``webhook_url`` is set the target is webhook-only. Files are written
    atomically (temp file -> rename) and keys are chmod 0600.
    """

    __tablename__ = "deployment_targets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    host: Mapped[str] = mapped_column(String(255))
    port: Mapped[int] = mapped_column(Integer, default=22)
    ssh_user: Mapped[str] = mapped_column(String(64), default="root")
    #: Encrypted PKCS#8 private key (AES-256-GCM via key_store) for key auth.
    ssh_private_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Encrypted password (AES-256-GCM via key_store) for password auth.
    ssh_password: Mapped[str | None] = mapped_column(Text, nullable=True)
    cert_path: Mapped[str] = mapped_column(String(255), default="")
    key_path: Mapped[str] = mapped_column(String(255), default="")
    chain_path: Mapped[str] = mapped_column(String(255), default="")
    #: Command run after a successful push (e.g. ``systemctl reload nginx``).
    reload_command: Mapped[str] = mapped_column(String(255), default="")
    #: Optional webhook URL hit after a successful push (reuses reload webhooks).
    webhook_url: Mapped[str] = mapped_column(String(255), default="")
    #: Deploy automatically on issue/renew when this target is assigned.
    auto_deploy: Mapped[bool] = mapped_column(Boolean, default=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    @property
    def ssh_key_configured(self) -> bool:
        return bool(self.ssh_private_key)

    @property
    def ssh_password_configured(self) -> bool:
        return bool(self.ssh_password)


class CertDeploymentTarget(Base):
    """Many-to-many: which targets a certificate is deployed to."""

    __tablename__ = "cert_deployment_targets"

    cert_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("certificates.id", ondelete="CASCADE"), primary_key=True
    )
    target_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("deployment_targets.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class DeploymentRecord(Base):
    """One deployment attempt (audit: which target got which version, when)."""

    __tablename__ = "deployment_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cert_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("certificates.id", ondelete="SET NULL"), nullable=True
    )
    target_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("deployment_targets.id", ondelete="SET NULL"), nullable=True
    )
    serial: Mapped[str] = mapped_column(String(255), default="")
    state: Mapped[str] = mapped_column(String(16), default="success")  # success|failed|skipped
    detail: Mapped[str] = mapped_column(Text, default="")
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

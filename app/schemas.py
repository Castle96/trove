from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CertCreate(BaseModel):
    cn: str = Field(min_length=1, max_length=255)
    sans: list[str] = Field(default_factory=list)
    issuer: str = Field(default="Let's Encrypt", min_length=1, max_length=255)
    protocol: str = Field(default="ACME DNS-01 (Cloudflare)", min_length=1, max_length=255)
    key_type: str = Field(default="ECDSA P-256")
    validity_days: int = Field(default=90, ge=1, le=825)
    auto_renew: bool = True

    @field_validator("cn", "issuer", "protocol")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("field may not be blank")
        return v

    @field_validator("sans")
    @classmethod
    def _strip_sans(cls, v: list[str]) -> list[str]:
        return [s.strip() for s in v if s.strip()]


class CertImport(BaseModel):
    cn: str = Field(min_length=1, max_length=255)
    pem: str = Field(min_length=20)
    issuer: str | None = None
    auto_renew: bool = False


class CertUpdate(BaseModel):
    auto_renew: bool


class CertRead(BaseModel):
    id: str
    cn: str
    sans: list[str]
    issuer: str
    protocol: str
    key_type: str
    serial: str
    fingerprint: str
    not_before: datetime | None
    not_after: datetime | None
    days_left: int
    status: str
    auto_renew: bool
    revoked: bool
    revoked_at: datetime | None = None
    revocation_reason: str = ""
    created_at: datetime | None


class RevokeRequest(BaseModel):
    reason: str = Field(default="unspecified", max_length=64)


class RenewRequest(BaseModel):
    reason: str = Field(default="routine", max_length=255)
    force_challenge: bool = True


class BatchRenewResponse(BaseModel):
    renewed: list[str]
    failed: list[str] = Field(default_factory=list)


class TimeInfo(BaseModel):
    sim_now: str
    real_now: str
    offset_days: int


class TimeAdvance(BaseModel):
    days: int = Field(ge=-3650, le=3650)


class SettingsRead(BaseModel):
    provider: str  # simulated | acme
    secrets_backend: str  # local | vault
    acme_directory_url: str
    acme_contact_email: str = ""
    acme_validation: str = "dns-01"
    acme_webroot: str = ""
    cloudflare_zone_hint: str = ""
    webhook_url: str
    cf_token_masked: bool
    renew_window_days: int
    default_validity_days: int
    notify_webhook_url: str = ""
    notify_ntfy_topic: str = ""
    notify_email_to: str = ""
    notify_smtp_host: str = ""
    notify_smtp_port: int = 587
    notify_smtp_user: str = ""
    notify_smtp_pass_masked: bool = False
    expiry_reminder_days: int = 14
    require_approval: bool = False


class SettingsUpdate(BaseModel):
    provider: Literal["simulated", "acme"] | None = None
    acme_directory_url: str = ""
    acme_contact_email: str = ""
    acme_validation: Literal["dns-01", "http-01"] | None = None
    acme_webroot: str = ""
    cloudflare_zone_hint: str = ""
    webhook_url: str = ""
    cf_token: str = ""
    renew_window_days: int | None = Field(default=None, ge=1, le=365)
    default_validity_days: int | None = Field(default=None, ge=1, le=825)
    notify_webhook_url: str = ""
    notify_ntfy_topic: str = ""
    notify_email_to: str = ""
    notify_smtp_host: str = ""
    notify_smtp_port: int | None = Field(default=None, ge=1, le=65535)
    notify_smtp_user: str = ""
    notify_smtp_pass: str = ""
    expiry_reminder_days: int | None = Field(default=None, ge=1, le=365)


class LogRead(BaseModel):
    id: int
    cert_id: str | None
    level: str
    message: str
    created_at: datetime


class Metrics(BaseModel):
    total: int
    valid: int
    expiring: int
    expired: int
    revoked: int
    auto_renew: int


class SyncResult(BaseModel):
    ok: bool
    message: str


class Health(BaseModel):
    status: str
    app: str
    version: str


# ---------------------------------------------------------------------------
# Local CA (internal PKI)
# ---------------------------------------------------------------------------


class CaCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    cn: str = Field(min_length=1, max_length=255)
    key_type: str = Field(default="ECDSA P-256")
    validity_days: int = Field(default=3650, ge=1, le=8250)

    @field_validator("cn")
    @classmethod
    def _strip_cn(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("cn may not be blank")
        return v


class CaIntermediateCreate(CaCreate):
    parent_id: int


class CaRead(BaseModel):
    id: int
    name: str
    cn: str
    kind: str
    parent_id: int | None = None
    key_type: str
    serial: str
    fingerprint: str
    not_before: datetime | None = None
    not_after: datetime | None = None
    enabled: bool = True
    active: bool = False
    created_at: datetime


class CaSetActive(BaseModel):
    ca_id: int | None = None


class CSRSignRequest(BaseModel):
    csr: str = Field(min_length=10)  # PEM (or base64 DER)
    ca_id: int | None = Field(default=None, description="leave empty to use the active CA")
    validity_days: int = Field(default=90, ge=1, le=825)


class CSRSignResult(BaseModel):
    cert_pem: str
    serial: str
    fingerprint: str
    not_before: datetime
    not_after: datetime
    issuer: str


# ---------------------------------------------------------------------------
# Issuance approval workflow
# ---------------------------------------------------------------------------


class IssueRequestRead(BaseModel):
    id: int
    cn: str
    sans: list[str]
    issuer: str
    protocol: str
    key_type: str
    validity_days: int
    auto_renew: bool
    requested_by: str | None = None
    status: str
    denial_reason: str = ""
    cert_id: str | None = None
    created_at: datetime
    decided_at: datetime | None = None
    decided_by: str | None = None


class RequestDecision(BaseModel):
    reason: str = Field(default="", max_length=255)


# ---------------------------------------------------------------------------
# OCSP
# ---------------------------------------------------------------------------


class OcspStatusRead(BaseModel):
    serial: str
    status: Literal["good", "revoked", "unknown"]
    revoked_at: datetime | None = None
    revocation_reason: str = ""
    response_pem: str | None = None


# ---------------------------------------------------------------------------
# Auth / users
# ---------------------------------------------------------------------------


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1)


class LoginResponse(BaseModel):
    token: str
    username: str
    role: str
    via: str = "token"


class PrincipalInfo(BaseModel):
    authenticated: bool
    username: str | None = None
    role: str
    via: str


class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$")
    password: str = Field(min_length=8, max_length=256)
    role: Literal["admin", "operator", "viewer"] = "viewer"


class UserUpdate(BaseModel):
    password: str | None = Field(default=None, min_length=8, max_length=256)
    role: Literal["admin", "operator", "viewer"] | None = None
    is_active: bool | None = None


class UserRead(BaseModel):
    id: int
    username: str
    role: str
    is_active: bool
    created_at: datetime


class TokenCreate(BaseModel):
    user_id: int | None = None
    role: Literal["admin", "operator", "viewer"] | None = None
    label: str = Field(default="", max_length=64)


class TokenRead(BaseModel):
    id: int
    user_id: int | None
    role: str
    label: str
    created_at: datetime
    expires_at: datetime | None
    last_used_at: datetime | None


class TokenCreated(BaseModel):
    token: str  # shown exactly once
    id: int
    role: str
    label: str


# ---------------------------------------------------------------------------
# API Gateway (gateways -> routes -> consumers -> keys)
# ---------------------------------------------------------------------------


class GatewayCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    slug: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$")
    description: str = Field(default="", max_length=2000)
    enabled: bool = True
    base_url: str = Field(default="", max_length=255)
    tls_domain: str = Field(default="", max_length=255)
    tls_enabled: bool = False
    issuer: str = Field(default="Let's Encrypt", max_length=255)

    @field_validator("slug", "tls_domain", "base_url")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()


class GatewayUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=2000)
    enabled: bool | None = None
    base_url: str | None = Field(default=None, max_length=255)
    tls_domain: str | None = Field(default=None, max_length=255)
    tls_enabled: bool | None = None
    issuer: str | None = Field(default=None, max_length=255)


class GatewayRead(BaseModel):
    id: int
    slug: str
    name: str
    description: str = ""
    enabled: bool = True
    base_url: str = ""
    tls_domain: str = ""
    tls_enabled: bool = False
    issuer: str = ""
    cert_id: str | None = None
    tls_status: str = "none"
    tls_error: str = ""
    route_count: int = 0
    consumer_count: int = 0
    created_at: datetime
    updated_at: datetime | None = None


class GatewayRouteCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    methods: list[str] = Field(default_factory=lambda: ["GET"])
    path: str = Field(min_length=1, max_length=255)
    upstream_url: str = Field(min_length=8, max_length=255)
    strip_prefix: bool = False
    timeout_ms: int = Field(default=30_000, ge=100, le=600_000)
    auth_mode: Literal["open", "api_key"] = "open"
    rate_limit_rpm: int = Field(default=0, ge=0, le=1_000_000)
    enabled: bool = True

    @field_validator("methods")
    @classmethod
    def _check_methods(cls, v: list[str]) -> list[str]:
        methods = [m.strip().upper() for m in v if m.strip()]
        allowed = {"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"}
        if not methods:
            raise ValueError("at least one method is required")
        for m in methods:
            if m not in allowed:
                raise ValueError(f"unsupported method {m}")
        return methods

    @field_validator("path")
    @classmethod
    def _check_path(cls, v: str) -> str:
        v = v.strip()
        if not v.startswith("/"):
            raise ValueError("path must start with '/'")
        return v

    @field_validator("upstream_url")
    @classmethod
    def _check_upstream(cls, v: str) -> str:
        v = v.strip().rstrip("/")
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("upstream_url must start with http:// or https://")
        return v


class GatewayRouteUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    methods: list[str] | None = None
    path: str | None = None
    upstream_url: str | None = Field(default=None, max_length=255)
    strip_prefix: bool | None = None
    timeout_ms: int | None = Field(default=None, ge=100, le=600_000)
    auth_mode: Literal["open", "api_key"] | None = None
    rate_limit_rpm: int | None = Field(default=None, ge=0, le=1_000_000)
    enabled: bool | None = None


class GatewayRouteRead(BaseModel):
    id: int
    gateway_id: int
    name: str
    methods: list[str]
    path: str
    upstream_url: str
    strip_prefix: bool = False
    timeout_ms: int = 30_000
    auth_mode: str = "open"
    rate_limit_rpm: int = 0
    enabled: bool = True
    created_at: datetime


class GatewayConsumerCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=2000)


class GatewayConsumerUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=2000)
    enabled: bool | None = None


class GatewayConsumerRead(BaseModel):
    id: int
    gateway_id: int
    name: str
    description: str = ""
    enabled: bool = True
    key_count: int = 0
    created_at: datetime


class GatewayApiKeyCreate(BaseModel):
    label: str = Field(default="", max_length=64)
    expires_at: datetime | None = None
    key: str | None = Field(
        default=None, min_length=8, max_length=256, description="optional custom key (>= 8 chars)"
    )


class GatewayApiKeyCreated(BaseModel):
    id: int
    consumer_id: int
    key: str  # plaintext, shown exactly once
    key_prefix: str
    label: str = ""
    expires_at: datetime | None = None


class GatewayApiKeyUpdate(BaseModel):
    label: str | None = Field(default=None, max_length=64)
    enabled: bool | None = None
    expires_at: datetime | None = None


class GatewayApiKeyRead(BaseModel):
    id: int
    consumer_id: int
    key_prefix: str = ""
    label: str = ""
    enabled: bool = True
    expires_at: datetime | None = None
    last_used_at: datetime | None = None
    created_at: datetime


class GatewayLogRead(BaseModel):
    id: int
    gateway_id: int | None = None
    route_id: int | None = None
    consumer_id: int | None = None
    method: str = "GET"
    path: str = ""
    status: int = 0
    latency_ms: int = 0
    client: str = ""
    created_at: datetime


class GatewayDirectTest(BaseModel):
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"] = "GET"
    url: str = Field(min_length=8, max_length=512)
    headers: dict[str, str] = Field(default_factory=dict)
    body: str | None = None
    timeout_ms: int = Field(default=15_000, ge=100, le=120_000)

    @field_validator("url")
    @classmethod
    def _check_url(cls, v: str) -> str:
        v = v.strip()
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("url must start with http:// or https://")
        return v


class GatewayDirectTestResult(BaseModel):
    ok: bool
    status: int | None = None
    latency_ms: int = 0
    headers: dict[str, str] = Field(default_factory=dict)
    body: str = ""
    error: str = ""


# ---------------------------------------------------------------------------
# Certificate deployment
# ---------------------------------------------------------------------------


class DeploymentTargetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(default=22, ge=1, le=65535)
    ssh_user: str = Field(default="root", max_length=64)
    #: Raw PEM private key or password; encrypted at rest by the service.
    ssh_private_key: str = ""
    ssh_password: str = ""
    cert_path: str = Field(default="", max_length=255)
    key_path: str = Field(default="", max_length=255)
    chain_path: str = Field(default="", max_length=255)
    reload_command: str = Field(default="", max_length=255)
    webhook_url: str = Field(default="", max_length=255)
    auto_deploy: bool = True
    enabled: bool = True

    @field_validator("name", "host", "ssh_user")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()

    @field_validator("cert_path", "key_path", "chain_path")
    @classmethod
    def _absolute_paths(cls, v: str) -> str:
        return (v or "").strip()

    @field_validator("webhook_url")
    @classmethod
    def _check_webhook_url(cls, v: str) -> str:
        v = (v or "").strip()
        if v and not v.startswith(("http://", "https://")):
            raise ValueError("webhook_url must start with http:// or https://")
        return v


class DeploymentTargetUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    host: str | None = Field(default=None, min_length=1, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    ssh_user: str | None = Field(default=None, max_length=64)
    ssh_private_key: str = ""
    ssh_password: str = ""
    cert_path: str = ""
    key_path: str = ""
    chain_path: str = ""
    reload_command: str = ""
    webhook_url: str = ""
    auto_deploy: bool | None = None
    enabled: bool | None = None


class DeploymentTargetRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    host: str
    port: int
    ssh_user: str
    ssh_key_configured: bool = False
    ssh_password_configured: bool = False
    cert_path: str
    key_path: str
    chain_path: str
    reload_command: str
    webhook_url: str
    auto_deploy: bool
    enabled: bool
    created_at: datetime


class DeploymentRecordRead(BaseModel):
    id: int
    target_id: int | None = None
    serial: str = ""
    state: str = "success"
    detail: str = ""
    duration_ms: int = 0
    created_at: datetime


class CertDeploymentAssign(BaseModel):
    target_id: int


class CertDeploymentRead(BaseModel):
    """A certificate's assignment to a target with its latest attempt."""

    target_id: int
    target_name: str
    auto_deploy: bool
    enabled: bool
    last_state: str | None = None
    last_detail: str = ""
    last_at: datetime | None = None


class DeployResult(BaseModel):
    """Results of a manual deploy-via-API call, grouped by success/failure."""

    deployed: list[int] = Field(default_factory=list)  # target ids
    failed: list[int] = Field(default_factory=list)  # target ids
    details: dict[str, str] = Field(default_factory=dict)  # target id -> message

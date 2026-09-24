from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Runtime configuration, overridable via env or a local .env file."""

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_prefix="TROVE_",
        extra="ignore",
    )

    app_name: str = "Trove"
    database_url: str = "sqlite+aiosqlite:///./trove.db"
    api_key: str = ""
    acme_directory_url: str = "https://ca.local.lan:9000/acme/acme/directory"
    webhook_url: str = ""
    default_validity_days: int = 90
    renew_window_days: int = 14
    frontend_dir: Path = BASE_DIR / "frontend"
    data_dir: Path = BASE_DIR / "data"

    # --- Multi-user bootstrap (optional) ---
    # Set both to auto-create the first admin account on startup.
    admin_username: str = ""
    admin_password: str = ""

    # --- Background auto-renewal scheduler ---
    scheduler_enabled: bool = True
    scheduler_interval_seconds: int = 6 * 60 * 60  # every 6 hours

    # --- Optional HashiCorp Vault secrets backend ---
    vault_addr: str = ""
    vault_token: str = ""
    vault_path: str = "trove"

    # --- Optional local CA (for CRL generation over internally-issued certs) ---
    ca_cert_path: str = ""
    ca_key_path: str = ""

    # --- ACME http-01 webroot (served at /.well-known/acme-challenge) ---
    acme_webroot: str = ""

    # --- Email notifications default sender ---
    notify_email_from: str = "trove@localhost"

    # --- Sensitive material at rest ---
    # Master key for AES-256-GCM encryption of stored private keys. When empty,
    # a key is auto-generated under data/keys/master.key (0600) on first use.
    key_encryption_key: str = ""

    # --- Expiry reminder notifications (daily pass, days before expiry) ---
    expiry_reminder_days: int = 14

    # --- Issuance approval workflow ---
    require_approval: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()

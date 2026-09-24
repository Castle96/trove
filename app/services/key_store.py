"""Encryption-at-rest for sensitive key material.

Private keys captured at issuance (and local CA keys) are stored in SQLite as
AES-256-GCM ciphertext instead of plaintext. The master key comes from
``TROVE_KEY_ENCRYPTION_KEY`` when set; otherwise an ephemeral-managed key is
auto-generated on first use at ``data/keys/master.key`` (0600 permissions).

Blob format: ``v1:<base64(nonce || ciphertext || tag)>``.
"""

from __future__ import annotations

import base64
import os
import secrets
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ..config import get_settings

BLOB_PREFIX = "v1:"
NONCE_SIZE = 12


def _master_key_bytes() -> bytes:
    """Resolve the 32-byte AES master key (env override or on-disk generated)."""
    settings = get_settings()
    if settings.key_encryption_key:
        raw = settings.key_encryption_key.strip()
        try:
            decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
            if len(decoded) == 32:
                return decoded
        except Exception:  # noqa: BLE001 - fall through to raw UTF-8 handling
            pass
        # Accept a raw 32-char string too (hashed to 32 bytes deterministically).
        if len(raw) == 32:
            return raw.encode("utf-8")
        # Any other string: derive a stable 32-byte key via SHA-256.
        from hashlib import sha256

        return sha256(raw.encode("utf-8")).digest()

    key_file = Path(settings.data_dir) / "keys" / "master.key"
    if key_file.exists():
        data = key_file.read_bytes()
        if len(data) == 32:
            return data
    # Auto-generate once (dir 0700, file 0600 so siblings can't read it).
    key_file.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(key_file.parent, 0o700)
    key = secrets.token_bytes(32)
    fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, key)
    finally:
        os.close(fd)
    return key


def encrypt_value(plaintext: str) -> str:
    """Encrypt a PEM/string value into the versioned blob format."""
    if not plaintext:
        return ""
    aesgcm = AESGCM(_master_key_bytes())
    nonce = secrets.token_bytes(NONCE_SIZE)
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
    return BLOB_PREFIX + base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")


def decrypt_value(blob: str) -> str:
    """Decrypt a versioned blob back to the original plaintext."""
    if not blob:
        return ""
    if not blob.startswith(BLOB_PREFIX):
        raise ValueError("unsupported key blob format")
    payload = base64.urlsafe_b64decode(blob[len(BLOB_PREFIX) :])
    nonce, ciphertext = payload[:NONCE_SIZE], payload[NONCE_SIZE:]
    aesgcm = AESGCM(_master_key_bytes())
    return aesgcm.decrypt(nonce, ciphertext, None).decode("utf-8")


def key_backend() -> str:
    """Human-readable backend name for the /api/system/health summary."""
    settings = get_settings()
    if settings.key_encryption_key:
        return "env"
    key_file = Path(settings.data_dir) / "keys" / "master.key"
    return "file" if key_file.exists() else "uninitialized"

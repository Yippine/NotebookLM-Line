from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from services.crypto_service import decrypt_text, encrypt_text


def encrypt_channel_credentials(
    channel_secret: str, access_token: str
) -> tuple[str, str]:
    """Encrypt both LINE credentials before any database write."""

    return encrypt_text(channel_secret), encrypt_text(access_token)


def read_channel_credentials(row: Mapping[str, Any]) -> tuple[str, str]:
    """Return LINE credentials, preferring the encrypted migration fields.

    The plaintext fallback exists only for the explicitly reversible rollout
    window. New and updated Channel records never depend on that fallback.
    """

    return read_channel_secret(row), read_channel_access_token(row)


def _read_one(row: Mapping[str, Any], *, encrypted_name: str, legacy_name: str) -> str:
    keys = set(row.keys())
    encrypted = row[encrypted_name] if encrypted_name in keys else None
    if encrypted:
        return decrypt_text(encrypted)
    legacy = row[legacy_name] if legacy_name in keys else None
    if legacy:
        return str(legacy)
    raise ValueError("LINE credential is unavailable")


def read_channel_secret(row: Mapping[str, Any]) -> str:
    return _read_one(
        row,
        encrypted_name="channel_secret_encrypted",
        legacy_name="channel_secret",
    )


def read_channel_access_token(row: Mapping[str, Any]) -> str:
    return _read_one(
        row,
        encrypted_name="channel_access_token_encrypted",
        legacy_name="channel_access_token",
    )

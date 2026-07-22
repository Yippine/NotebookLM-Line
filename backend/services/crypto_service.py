from __future__ import annotations

import json
import logging
from functools import lru_cache
from typing import Any

from cryptography.fernet import Fernet

from config import settings

logger = logging.getLogger(__name__)


class EncryptionConfigurationError(RuntimeError):
    """Raised when encrypted data could not be protected across restarts."""


@lru_cache(maxsize=1)
def _get_fernet() -> Fernet:
    raw_key = settings.encryption_key.strip()
    if not raw_key:
        if settings.is_production:
            raise EncryptionConfigurationError(
                "Production requires a persistent ENCRYPTION_KEY"
            )
        logger.warning(
            "ENCRYPTION_KEY is not configured; using an ephemeral development key"
        )
        raw_key = Fernet.generate_key().decode("ascii")

    try:
        return Fernet(raw_key.encode("ascii"))
    except (TypeError, ValueError) as exc:
        raise EncryptionConfigurationError(
            "ENCRYPTION_KEY is not a valid Fernet key"
        ) from exc


def ensure_encryption_ready() -> None:
    """Validate encryption configuration during application startup."""

    _get_fernet()


def reset_encryption_cache() -> None:
    """Clear the cached cipher. Intended for configuration tests only."""

    _get_fernet.cache_clear()


def encrypt_text(value: str) -> str:
    return _get_fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_text(token: str) -> str:
    return _get_fernet().decrypt(token.encode("ascii")).decode("utf-8")


def encrypt_json(data: Any) -> str:
    serialized = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return encrypt_text(serialized)


def decrypt_json(token: str) -> Any:
    return json.loads(decrypt_text(token))


# Backward-compatible aliases for the old per-Channel NotebookLM flow. New code
# should use the explicit text/json helpers so that credential types are clear.
def encrypt(data: dict) -> str:
    return encrypt_json(data)


def decrypt(token: str) -> dict:
    value = decrypt_json(token)
    if not isinstance(value, dict):
        raise ValueError("Encrypted value is not a JSON object")
    return value

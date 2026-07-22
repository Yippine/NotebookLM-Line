from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from config import settings
from services.crypto_service import (
    EncryptionConfigurationError,
    decrypt_json,
    decrypt_text,
    encrypt_json,
    encrypt_text,
    ensure_encryption_ready,
    reset_encryption_cache,
)


def test_production_requires_persistent_fernet_key(monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "encryption_key", "")
    reset_encryption_cache()
    with pytest.raises(EncryptionConfigurationError):
        ensure_encryption_ready()
    reset_encryption_cache()


def test_invalid_fernet_key_is_rejected(monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "encryption_key", "not-a-fernet-key")
    reset_encryption_cache()
    with pytest.raises(EncryptionConfigurationError):
        ensure_encryption_ready()
    reset_encryption_cache()


def test_text_and_json_round_trip(monkeypatch):
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "encryption_key", Fernet.generate_key().decode())
    reset_encryption_cache()
    text_ciphertext = encrypt_text("敏感資料")
    json_ciphertext = encrypt_json({"cookies": [{"value": "secret"}]})
    assert "敏感資料" not in text_ciphertext
    assert decrypt_text(text_ciphertext) == "敏感資料"
    assert decrypt_json(json_ciphertext) == {"cookies": [{"value": "secret"}]}
    reset_encryption_cache()

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from config import settings
from database import init_db, rollback_line_credential_migration
from services.course_account_service import CourseAccountRepository
from services.crypto_service import (
    EncryptionConfigurationError,
    decrypt_json,
    decrypt_text,
    encrypt_json,
    encrypt_text,
    ensure_encryption_ready,
    reset_encryption_cache,
)


def test_fernet_round_trip_uses_fixed_configured_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(
        settings, "encryption_key", Fernet.generate_key().decode("ascii")
    )
    reset_encryption_cache()
    try:
        ciphertext = encrypt_text("LINE-secret-不可明文")
        assert ciphertext != "LINE-secret-不可明文"
        assert decrypt_text(ciphertext) == "LINE-secret-不可明文"

        payload = {"cookies": [{"name": "SID", "value": "google-cookie-secret"}]}
        encrypted_payload = encrypt_json(payload)
        assert "google-cookie-secret" not in encrypted_payload
        assert decrypt_json(encrypted_payload) == payload
    finally:
        reset_encryption_cache()


@pytest.mark.parametrize("configured_key", ["", "not-a-fernet-key"])
def test_production_encryption_fails_fast_when_key_is_missing_or_invalid(
    configured_key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "encryption_key", configured_key)
    reset_encryption_cache()
    try:
        with pytest.raises(EncryptionConfigurationError):
            ensure_encryption_ready()
    finally:
        reset_encryption_cache()


def _create_legacy_database(path: Path) -> None:
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            CREATE TABLE channels (
                channel_id TEXT PRIMARY KEY,
                channel_secret TEXT,
                channel_access_token TEXT,
                nlm_auth_json_encrypted TEXT,
                notebook_id TEXT,
                expires_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE invite_codes (
                code TEXT PRIMARY KEY,
                used INTEGER NOT NULL DEFAULT 0,
                channel_id TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        db.execute(
            """
            INSERT INTO channels (
                channel_id, channel_secret, channel_access_token,
                nlm_auth_json_encrypted, notebook_id, expires_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-channel",
                "legacy-line-secret",
                "legacy-line-token",
                "legacy-nlm-ciphertext",
                "LegacyNotebook_123",
                "2030-01-02T03:04:05+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )
        db.execute(
            "INSERT INTO invite_codes (code, used, channel_id, created_at) VALUES (?, ?, ?, ?)",
            ("legacy-invite", 1, "legacy-channel", "2026-01-01T00:00:00+00:00"),
        )
        db.commit()


def test_migration_and_explicit_rollback_preserve_legacy_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "legacy.db"
    _create_legacy_database(db_path)
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(
        settings, "encryption_key", Fernet.generate_key().decode("ascii")
    )
    reset_encryption_cache()
    try:
        asyncio.run(init_db(str(db_path)))

        with sqlite3.connect(db_path) as db:
            db.row_factory = sqlite3.Row
            channel = db.execute(
                "SELECT * FROM channels WHERE channel_id='legacy-channel'"
            ).fetchone()
            invite = db.execute(
                "SELECT * FROM invite_codes WHERE code='legacy-invite'"
            ).fetchone()
            tables = {
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }

            assert channel is not None
            assert channel["notebook_id"] == "LegacyNotebook_123"
            assert channel["binding_status"] == "unbound"
            assert channel["expires_at"] == "2030-01-02T03:04:05+00:00"
            assert channel["created_at"] == "2026-01-01T00:00:00+00:00"
            assert (
                decrypt_text(channel["channel_secret_encrypted"])
                == "legacy-line-secret"
            )
            assert (
                decrypt_text(channel["channel_access_token_encrypted"])
                == "legacy-line-token"
            )
            assert invite is not None
            assert invite["channel_id"] == "legacy-channel"
            assert invite["used"] == 1
            assert {
                "course_notebook_account",
                "setup_sessions",
                "admin_sessions",
            } <= tables

            # Emulate the post-rollout plaintext cleanup, then verify the
            # explicit rollback helper can reconstruct the legacy fields.
            db.execute(
                "UPDATE channels SET channel_secret=NULL, channel_access_token=NULL WHERE channel_id=?",
                ("legacy-channel",),
            )
            db.commit()

        asyncio.run(rollback_line_credential_migration(str(db_path)))
        with sqlite3.connect(db_path) as db:
            restored = db.execute(
                "SELECT channel_secret, channel_access_token, notebook_id, expires_at FROM channels WHERE channel_id=?",
                ("legacy-channel",),
            ).fetchone()
        assert restored == (
            "legacy-line-secret",
            "legacy-line-token",
            "LegacyNotebook_123",
            "2030-01-02T03:04:05+00:00",
        )
    finally:
        reset_encryption_cache()


def test_course_account_authorization_is_encrypted_and_safe_model_omits_it(
    backend_db: Path,
) -> None:
    payload = {"cookies": [{"name": "SID", "value": "central-google-secret"}]}
    repository = CourseAccountRepository(str(backend_db))
    record = asyncio.run(
        repository.replace_authorization(
            email="course@example.com",
            auth_payload=payload,
            auth_mode="storage_state",
            checked_at="2026-07-22T00:00:00+00:00",
        )
    )

    assert record.auth_encrypted is not None
    assert "central-google-secret" not in record.auth_encrypted
    assert decrypt_json(record.auth_encrypted) == payload
    assert "auth_encrypted" not in record.safe_dict()
    assert "cookies" not in record.safe_dict()

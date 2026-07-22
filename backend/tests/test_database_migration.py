from __future__ import annotations

import os
import sqlite3

import aiosqlite
import pytest
from cryptography.fernet import Fernet

from config import settings
from database import init_db, rollback_line_credential_migration
from services.crypto_service import decrypt_text, reset_encryption_cache


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission mode assertion")
@pytest.mark.anyio
async def test_production_database_and_directory_are_private(tmp_path, monkeypatch):
    data_directory = tmp_path / "data"
    db_path = data_directory / "v2.db"
    monkeypatch.setattr(settings, "environment", "production")

    await init_db(str(db_path))

    assert data_directory.stat().st_mode & 0o777 == 0o700
    assert db_path.stat().st_mode & 0o777 == 0o600


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission mode assertion")
@pytest.mark.anyio
async def test_production_rejects_shared_database_directory(tmp_path, monkeypatch):
    data_directory = tmp_path / "shared-data"
    data_directory.mkdir(mode=0o755)
    data_directory.chmod(0o755)
    monkeypatch.setattr(settings, "environment", "production")

    with pytest.raises(PermissionError, match="database directory"):
        await init_db(str(data_directory / "v2.db"))


@pytest.mark.anyio
async def test_legacy_database_migrates_without_losing_bindings(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy.db"
    key = Fernet.generate_key().decode()
    monkeypatch.setattr(settings, "db_path", str(db_path))
    monkeypatch.setattr(settings, "encryption_key", key)
    monkeypatch.setattr(settings, "environment", "development")
    reset_encryption_cache()

    with sqlite3.connect(db_path) as db:
        db.execute(
            """
            CREATE TABLE channels (
                channel_id TEXT PRIMARY KEY,
                channel_secret TEXT NOT NULL,
                channel_access_token TEXT NOT NULL,
                nlm_auth_json_encrypted TEXT,
                notebook_id TEXT,
                expires_at DATETIME,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        db.execute(
            """
            CREATE TABLE invite_codes (
                code TEXT PRIMARY KEY,
                student_name TEXT DEFAULT '',
                used INTEGER DEFAULT 0,
                channel_id TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        db.execute(
            """
            INSERT INTO channels
                (channel_id, channel_secret, channel_access_token, notebook_id, expires_at)
            VALUES ('channel-a', 'line-secret', 'line-token', 'notebook-a',
                    '2030-01-01T00:00:00+00:00')
            """
        )
        db.execute(
            """
            INSERT INTO invite_codes (code, student_name, used, channel_id)
            VALUES ('invite-a', '學員 A', 1, 'channel-a')
            """
        )
        db.execute(
            """
            CREATE TABLE line_webhook_events (
                event_id TEXT PRIMARY KEY,
                received_at TEXT NOT NULL
            )
            """
        )
        db.execute(
            """
            INSERT INTO line_webhook_events (event_id, received_at)
            VALUES ('legacy-event', '2026-07-20T00:00:00+00:00')
            """
        )

    await init_db(str(db_path))
    # Idempotency is part of the deploy/restart contract.
    await init_db(str(db_path))

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        row = await (
            await db.execute("SELECT * FROM channels WHERE channel_id='channel-a'")
        ).fetchone()
        invite = await (
            await db.execute("SELECT * FROM invite_codes WHERE code='invite-a'")
        ).fetchone()
        tables = {
            item[0]
            for item in await (
                await db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            ).fetchall()
        }
        course = await (
            await db.execute("SELECT * FROM course_notebook_account WHERE id=1")
        ).fetchone()
        course_settings = await (
            await db.execute("SELECT * FROM course_settings WHERE id=1")
        ).fetchone()
        legacy_event = await (
            await db.execute(
                "SELECT * FROM line_webhook_events WHERE event_id='legacy-event'"
            )
        ).fetchone()

    assert row["channel_id"] == "channel-a"
    assert row["notebook_id"] == "notebook-a"
    assert row["expires_at"] == "2030-01-01T00:00:00+00:00"
    # Preserve the ID for rollback, but require the centralized sharing probe.
    assert row["binding_status"] == "unbound"
    assert row["binding_check_previous_status"] is None
    assert decrypt_text(row["channel_secret_encrypted"]) == "line-secret"
    assert decrypt_text(row["channel_access_token_encrypted"]) == "line-token"
    assert invite["student_name"] == "學員 A"
    assert invite["channel_id"] == "channel-a"
    assert {
        "course_notebook_account",
        "course_settings",
        "setup_sessions",
        "admin_sessions",
        "line_webhook_replay_state",
    } <= tables
    assert course["health_status"] == "unconfigured"
    assert course_settings["course_expires_at"] == "2030-01-01T00:00:00+00:00"
    # Old rows cannot reveal whether their background Push succeeded. Marking
    # them completed avoids replaying every retained event during deployment;
    # all events admitted after migration use explicit pending leases.
    assert legacy_event["status"] == "completed"
    assert legacy_event["completed_at"] == legacy_event["received_at"]
    assert legacy_event["attempt_count"] == 1
    assert legacy_event["claim_token"] is None
    assert legacy_event["tenant_key"] is None
    assert legacy_event["payload_encrypted"] is None
    assert legacy_event["payload_version"] is None
    assert legacy_event["failed_at"] is None

    # Simulate the post-verification plaintext cleanup, then prove the rollback
    # helper can restore values for the previous application version.
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE channels SET channel_secret='', channel_access_token='' WHERE channel_id='channel-a'"
        )
        await db.commit()
    await rollback_line_credential_migration(str(db_path))
    async with aiosqlite.connect(db_path) as db:
        restored = await (
            await db.execute(
                "SELECT channel_secret, channel_access_token FROM channels WHERE channel_id='channel-a'"
            )
        ).fetchone()
    assert restored == ("line-secret", "line-token")
    reset_encryption_cache()

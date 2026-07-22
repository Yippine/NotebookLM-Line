from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

import aiosqlite

from config import settings

# Kept for compatibility with the existing routers. New code should call
# get_db_path(), which also makes isolated test databases straightforward.
DB = settings.db_path


def get_db_path() -> str:
    return settings.db_path


async def _column_names(db: aiosqlite.Connection, table: str) -> set[str]:
    cursor = await db.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in await cursor.fetchall()}


async def _add_missing_columns(
    db: aiosqlite.Connection,
    table: str,
    definitions: Mapping[str, str],
) -> None:
    columns = await _column_names(db, table)
    for name, definition in definitions.items():
        if name not in columns:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


async def _migrate_line_credentials(db: aiosqlite.Connection) -> None:
    """Copy legacy plaintext LINE credentials into encrypted columns.

    Plaintext is deliberately retained for the reversible migration window.
    All newly created or updated Channels write only encrypted values. The
    explicit rollback helper can repopulate legacy columns before rolling the
    application back; final plaintext cleanup belongs to rollout task 7.4.
    """

    from services.crypto_service import encrypt_text

    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        """
        SELECT channel_id, channel_secret, channel_access_token,
               channel_secret_encrypted, channel_access_token_encrypted
        FROM channels
        """
    )
    for row in await cursor.fetchall():
        secret_encrypted = row["channel_secret_encrypted"]
        token_encrypted = row["channel_access_token_encrypted"]
        if not secret_encrypted and row["channel_secret"]:
            secret_encrypted = encrypt_text(row["channel_secret"])
        if not token_encrypted and row["channel_access_token"]:
            token_encrypted = encrypt_text(row["channel_access_token"])
        if (
            secret_encrypted != row["channel_secret_encrypted"]
            or token_encrypted != row["channel_access_token_encrypted"]
        ):
            await db.execute(
                """
                UPDATE channels
                SET channel_secret_encrypted=?, channel_access_token_encrypted=?
                WHERE channel_id=?
                """,
                (secret_encrypted, token_encrypted, row["channel_id"]),
            )


async def init_db(db_path: str | None = None) -> None:
    path = db_path or get_db_path()
    db_directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(db_directory, mode=0o700, exist_ok=True)
    if settings.is_production:
        # The database contains encrypted credentials and may briefly contain
        # encrypted pending LINE questions. Create it privately before SQLite
        # opens it, and repair overly broad modes from an earlier deployment.
        directory_mode = os.stat(db_directory).st_mode & 0o777
        if directory_mode & 0o077:
            raise PermissionError(
                "Production database directory must not be accessible by group or others"
            )
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(descriptor)
        os.chmod(path, 0o600)

    async with aiosqlite.connect(path) as db:
        await db.execute("PRAGMA foreign_keys=ON")
        try:
            await db.execute("BEGIN")
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS channels (
                    channel_id TEXT PRIMARY KEY,
                    channel_secret TEXT,
                    channel_access_token TEXT,
                    channel_secret_encrypted TEXT,
                    channel_access_token_encrypted TEXT,
                    nlm_auth_json_encrypted TEXT,
                    notebook_id TEXT,
                    notebook_display_name TEXT,
                    binding_status TEXT NOT NULL DEFAULT 'unbound',
                    binding_revision INTEGER NOT NULL DEFAULT 0,
                    binding_operation_id TEXT,
                    binding_check_previous_status TEXT,
                    bound_at TEXT,
                    last_access_checked_at TEXT,
                    expires_at TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS invite_codes (
                    code TEXT PRIMARY KEY,
                    student_name TEXT DEFAULT '',
                    used INTEGER NOT NULL DEFAULT 0,
                    channel_id TEXT,
                    expires_at TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

            await _add_missing_columns(
                db,
                "channels",
                {
                    "expires_at": "TEXT",
                    "channel_secret_encrypted": "TEXT",
                    "channel_access_token_encrypted": "TEXT",
                    "notebook_display_name": "TEXT",
                    "binding_status": "TEXT NOT NULL DEFAULT 'unbound'",
                    "binding_revision": "INTEGER NOT NULL DEFAULT 0",
                    "binding_operation_id": "TEXT",
                    "binding_check_previous_status": "TEXT",
                    "bound_at": "TEXT",
                    "last_access_checked_at": "TEXT",
                    "updated_at": "TEXT",
                },
            )
            await _add_missing_columns(
                db,
                "invite_codes",
                {
                    "student_name": "TEXT DEFAULT ''",
                    "channel_id": "TEXT",
                    "expires_at": "TEXT",
                },
            )

            # Course expiry is a course-wide policy, not a property owned by
            # whichever learner happened to bind first.  Keep the legacy
            # channel column synchronized for webhook compatibility, while a
            # singleton row remains authoritative for current and future
            # learners.  The initial value preserves an existing deployment's
            # configured expiry during migration.
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS course_settings (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    course_expires_at TEXT,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            await db.execute(
                """
                INSERT OR IGNORE INTO course_settings (id, course_expires_at)
                SELECT 1, (
                    SELECT expires_at
                    FROM channels
                    WHERE expires_at IS NOT NULL
                    ORDER BY updated_at DESC
                    LIMIT 1
                )
                """
            )

            # Do not infer a verified shared binding from a legacy Notebook ID.
            # Existing IDs and cookies stay available for rollback, but the
            # default `unbound` state forces the learner to share the Notebook
            # with the course account and pass the new URL probe first.

            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS course_notebook_account (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    email TEXT,
                    auth_encrypted TEXT,
                    auth_mode TEXT,
                    health_status TEXT NOT NULL DEFAULT 'unconfigured'
                        CHECK (health_status IN ('unconfigured', 'healthy', 'expired', 'error')),
                    last_success_at TEXT,
                    last_checked_at TEXT,
                    error_code TEXT,
                    auth_revision INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            await db.execute(
                """
                INSERT OR IGNORE INTO course_notebook_account (id, health_status)
                VALUES (1, 'unconfigured')
                """
            )
            await _add_missing_columns(
                db,
                "course_notebook_account",
                {"auth_revision": "INTEGER NOT NULL DEFAULT 0"},
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS setup_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    token_hash TEXT NOT NULL UNIQUE,
                    invite_code TEXT NOT NULL,
                    channel_id TEXT,
                    scope TEXT NOT NULL DEFAULT 'channel:setup',
                    expires_at TEXT NOT NULL,
                    revoked_at TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (invite_code) REFERENCES invite_codes(code) ON DELETE CASCADE
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS admin_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    token_hash TEXT NOT NULL UNIQUE,
                    scope TEXT NOT NULL DEFAULT 'admin',
                    expires_at TEXT NOT NULL,
                    revoked_at TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_setup_sessions_hash ON setup_sessions(token_hash)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_admin_sessions_hash ON admin_sessions(token_hash)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_setup_sessions_invite ON setup_sessions(invite_code)"
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS line_webhook_events (
                    event_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL DEFAULT 'completed'
                        CHECK (status IN ('pending', 'completed')),
                    received_at TEXT NOT NULL,
                    claimed_at TEXT,
                    completed_at TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 1,
                    claim_token TEXT,
                    tenant_key TEXT,
                    payload_encrypted TEXT,
                    payload_version INTEGER,
                    next_attempt_at TEXT,
                    failure_count INTEGER NOT NULL DEFAULT 0,
                    last_error_code TEXT,
                    failed_at TEXT
                )
                """
            )
            # Older builds stored an event id permanently at admission time.
            # Treat those rows as completed during migration: their actual
            # outcome is unknowable, and replaying every retained event would
            # duplicate previously delivered LINE messages. New claims always
            # insert an explicit `pending` status.
            await _add_missing_columns(
                db,
                "line_webhook_events",
                {
                    "status": (
                        "TEXT NOT NULL DEFAULT 'completed' "
                        "CHECK (status IN ('pending', 'completed'))"
                    ),
                    "claimed_at": "TEXT",
                    "completed_at": "TEXT",
                    "attempt_count": "INTEGER NOT NULL DEFAULT 1",
                    "claim_token": "TEXT",
                    "tenant_key": "TEXT",
                    "payload_encrypted": "TEXT",
                    "payload_version": "INTEGER",
                    "next_attempt_at": "TEXT",
                    "failure_count": "INTEGER NOT NULL DEFAULT 0",
                    "last_error_code": "TEXT",
                    "failed_at": "TEXT",
                },
            )
            await db.execute(
                """
                UPDATE line_webhook_events
                SET completed_at=received_at
                WHERE status='completed' AND completed_at IS NULL
                """
            )
            await db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_line_webhook_events_received
                ON line_webhook_events(received_at)
                """
            )
            await db.execute("DROP INDEX IF EXISTS idx_line_webhook_events_claim")
            await db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_line_webhook_events_claim
                ON line_webhook_events(
                    status, tenant_key, next_attempt_at, claimed_at
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS line_webhook_replay_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    last_tenant_key TEXT
                )
                """
            )
            await db.execute(
                """
                INSERT OR IGNORE INTO line_webhook_replay_state (
                    id, last_tenant_key
                ) VALUES (1, NULL)
                """
            )

            await _migrate_line_credentials(db)
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    if settings.is_production:
        os.chmod(path, 0o600)


async def migrate_line_credentials(db_path: str | None = None) -> None:
    """Idempotently encrypt any legacy plaintext LINE credentials."""

    path = db_path or get_db_path()
    async with aiosqlite.connect(path) as db:
        try:
            await db.execute("BEGIN")
            await _migrate_line_credentials(db)
            await db.commit()
        except Exception:
            await db.rollback()
            raise


async def rollback_line_credential_migration(db_path: str | None = None) -> None:
    """Restore legacy plaintext fields from ciphertext before app rollback."""

    from services.crypto_service import decrypt_text

    path = db_path or get_db_path()
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        try:
            await db.execute("BEGIN")
            cursor = await db.execute(
                """
                SELECT channel_id, channel_secret_encrypted,
                       channel_access_token_encrypted
                FROM channels
                """
            )
            for row in await cursor.fetchall():
                if (
                    not row["channel_secret_encrypted"]
                    or not row["channel_access_token_encrypted"]
                ):
                    continue
                await db.execute(
                    """
                    UPDATE channels
                    SET channel_secret=?, channel_access_token=?
                    WHERE channel_id=?
                    """,
                    (
                        decrypt_text(row["channel_secret_encrypted"]),
                        decrypt_text(row["channel_access_token_encrypted"]),
                        row["channel_id"],
                    ),
                )
            await db.commit()
        except Exception:
            await db.rollback()
            raise


async def fetch_one(
    query: str,
    parameters: tuple[Any, ...] = (),
    db_path: str | None = None,
) -> aiosqlite.Row | None:
    """Small row helper shared by security services."""

    async with aiosqlite.connect(db_path or get_db_path()) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(query, parameters)
        return await cursor.fetchone()

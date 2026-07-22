from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import aiosqlite
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from config import settings
from database import get_db_path

_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class IssuedSession:
    token: str
    expires_at: datetime


@dataclass(frozen=True)
class SetupPrincipal:
    session_id: int
    invite_code: str
    channel_id: str | None
    scope: str
    expires_at: datetime


@dataclass(frozen=True)
class AdminPrincipal:
    session_id: int
    scope: str
    expires_at: datetime


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=401,
        detail="工作階段無效或已過期，請重新登入",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _raw_bearer(credentials: HTTPAuthorizationCredentials | None) -> str:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _unauthorized()
    token = credentials.credentials.strip()
    if not token:
        raise _unauthorized()
    return token


async def issue_setup_session(
    invite_code: str,
    channel_id: str | None = None,
    *,
    ttl_seconds: int | None = None,
) -> IssuedSession:
    token = secrets.token_urlsafe(48)
    expires_at = _utcnow() + timedelta(
        seconds=ttl_seconds or settings.setup_session_ttl_seconds
    )

    async with aiosqlite.connect(get_db_path()) as db:
        cursor = await db.execute(
            "SELECT channel_id FROM invite_codes WHERE code=?", (invite_code,)
        )
        invite = await cursor.fetchone()
        if invite is None:
            raise ValueError("Unknown invite code")
        invite_channel = invite[0]
        if invite_channel and channel_id and invite_channel != channel_id:
            raise ValueError("Invite code does not own that Channel")
        effective_channel = invite_channel or channel_id
        # A fresh login for the same invitation invalidates older browser
        # sessions, so a leaked historic setup token cannot remain active.
        await db.execute(
            """
            UPDATE setup_sessions SET revoked_at=?
            WHERE invite_code=? AND revoked_at IS NULL
            """,
            (_utcnow().isoformat(), invite_code),
        )
        await db.execute(
            """
            INSERT INTO setup_sessions
                (token_hash, invite_code, channel_id, scope, expires_at)
            VALUES (?, ?, ?, 'channel:setup', ?)
            """,
            (hash_token(token), invite_code, effective_channel, expires_at.isoformat()),
        )
        await db.commit()
    return IssuedSession(token=token, expires_at=expires_at)


async def issue_admin_session(*, ttl_seconds: int | None = None) -> IssuedSession:
    token = secrets.token_urlsafe(48)
    expires_at = _utcnow() + timedelta(
        seconds=ttl_seconds or settings.admin_session_ttl_seconds
    )
    async with aiosqlite.connect(get_db_path()) as db:
        await db.execute(
            """
            INSERT INTO admin_sessions (token_hash, scope, expires_at)
            VALUES (?, 'admin', ?)
            """,
            (hash_token(token), expires_at.isoformat()),
        )
        await db.commit()
    return IssuedSession(token=token, expires_at=expires_at)


async def resolve_setup_token(token: str) -> SetupPrincipal:
    async with aiosqlite.connect(get_db_path()) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT s.id, s.invite_code, s.channel_id, s.scope,
                   s.expires_at, s.revoked_at,
                   i.channel_id AS invite_channel_id
            FROM setup_sessions s
            JOIN invite_codes i ON i.code=s.invite_code
            WHERE s.token_hash=?
            """,
            (hash_token(token),),
        )
        row = await cursor.fetchone()

    if row is None or row["revoked_at"] is not None or row["scope"] != "channel:setup":
        raise _unauthorized()
    expires_at = _parse_timestamp(row["expires_at"])
    if expires_at <= _utcnow():
        raise _unauthorized()
    if (
        row["channel_id"]
        and row["invite_channel_id"]
        and row["channel_id"] != row["invite_channel_id"]
    ):
        raise _unauthorized()
    return SetupPrincipal(
        session_id=row["id"],
        invite_code=row["invite_code"],
        channel_id=row["channel_id"] or row["invite_channel_id"],
        scope=row["scope"],
        expires_at=expires_at,
    )


async def resolve_admin_token(token: str) -> AdminPrincipal:
    async with aiosqlite.connect(get_db_path()) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT id, scope, expires_at, revoked_at
            FROM admin_sessions
            WHERE token_hash=?
            """,
            (hash_token(token),),
        )
        row = await cursor.fetchone()

    if row is None or row["revoked_at"] is not None or row["scope"] != "admin":
        raise _unauthorized()
    expires_at = _parse_timestamp(row["expires_at"])
    if expires_at <= _utcnow():
        raise _unauthorized()
    return AdminPrincipal(
        session_id=row["id"],
        scope=row["scope"],
        expires_at=expires_at,
    )


async def require_setup_session(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> SetupPrincipal:
    return await resolve_setup_token(_raw_bearer(credentials))


async def require_admin_session(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> AdminPrincipal:
    return await resolve_admin_token(_raw_bearer(credentials))


def assert_channel_scope(principal: SetupPrincipal, channel_id: str) -> None:
    if principal.channel_id is None or not secrets.compare_digest(
        principal.channel_id.encode("utf-8"), channel_id.encode("utf-8")
    ):
        # The same response is used whether the Channel exists or not.
        raise HTTPException(403, "無權操作此 Channel")


async def scope_setup_session_to_channel(session_id: int, channel_id: str) -> None:
    """Atomically bind an unscoped setup session and its invite to a Channel."""

    async with aiosqlite.connect(get_db_path()) as db:
        db.row_factory = aiosqlite.Row
        try:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """
                SELECT invite_code, channel_id, expires_at, revoked_at
                FROM setup_sessions WHERE id=?
                """,
                (session_id,),
            )
            row = await cursor.fetchone()
            if (
                row is None
                or row["revoked_at"] is not None
                or _parse_timestamp(row["expires_at"]) <= _utcnow()
            ):
                raise _unauthorized()
            if row["channel_id"] and row["channel_id"] != channel_id:
                raise HTTPException(403, "無權操作此 Channel")

            cursor = await db.execute(
                "SELECT channel_id FROM invite_codes WHERE code=?",
                (row["invite_code"],),
            )
            invite = await cursor.fetchone()
            if invite is None or (invite[0] and invite[0] != channel_id):
                raise HTTPException(403, "無權操作此 Channel")

            cursor = await db.execute(
                """
                SELECT 1 FROM invite_codes
                WHERE channel_id=? AND code<>?
                LIMIT 1
                """,
                (channel_id, row["invite_code"]),
            )
            if await cursor.fetchone():
                raise HTTPException(403, "無權操作此 Channel")

            await db.execute(
                "UPDATE setup_sessions SET channel_id=? WHERE id=?",
                (channel_id, session_id),
            )
            await db.execute(
                "UPDATE invite_codes SET channel_id=?, used=1 WHERE code=?",
                (channel_id, row["invite_code"]),
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise


async def revoke_setup_session(session_id: int) -> None:
    async with aiosqlite.connect(get_db_path()) as db:
        await db.execute(
            "UPDATE setup_sessions SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
            (_utcnow().isoformat(), session_id),
        )
        await db.commit()


async def revoke_admin_session(session_id: int) -> None:
    async with aiosqlite.connect(get_db_path()) as db:
        await db.execute(
            "UPDATE admin_sessions SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
            (_utcnow().isoformat(), session_id),
        )
        await db.commit()

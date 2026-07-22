from __future__ import annotations

import aiosqlite
import pytest
from fastapi import HTTPException

from config import settings
from database import init_db
from services.session_service import (
    assert_channel_scope,
    hash_token,
    issue_admin_session,
    issue_setup_session,
    resolve_admin_token,
    resolve_setup_token,
    revoke_admin_session,
    revoke_setup_session,
    scope_setup_session_to_channel,
)


@pytest.mark.anyio
async def test_setup_session_is_hashed_scoped_persistent_and_revocable(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "sessions.db"
    monkeypatch.setattr(settings, "db_path", str(db_path))
    await init_db(str(db_path))
    async with aiosqlite.connect(db_path) as db:
        await db.execute("INSERT INTO invite_codes(code) VALUES ('invite-a')")
        await db.commit()

    issued = await issue_setup_session("invite-a")
    async with aiosqlite.connect(db_path) as db:
        row = await (
            await db.execute(
                "SELECT id, token_hash, channel_id FROM setup_sessions WHERE invite_code='invite-a'"
            )
        ).fetchone()
    assert issued.token not in row
    assert row[1] == hash_token(issued.token)
    assert row[2] is None

    principal = await resolve_setup_token(issued.token)
    await scope_setup_session_to_channel(principal.session_id, "channel-a")
    scoped = await resolve_setup_token(issued.token)
    assert scoped.channel_id == "channel-a"
    assert_channel_scope(scoped, "channel-a")
    with pytest.raises(HTTPException) as wrong_scope:
        assert_channel_scope(scoped, "channel-b")
    assert wrong_scope.value.status_code == 403

    await revoke_setup_session(scoped.session_id)
    with pytest.raises(HTTPException) as revoked:
        await resolve_setup_token(issued.token)
    assert revoked.value.status_code == 401


@pytest.mark.anyio
async def test_expired_setup_and_admin_sessions_are_rejected(tmp_path, monkeypatch):
    db_path = tmp_path / "expired.db"
    monkeypatch.setattr(settings, "db_path", str(db_path))
    await init_db(str(db_path))
    async with aiosqlite.connect(db_path) as db:
        await db.execute("INSERT INTO invite_codes(code) VALUES ('invite-a')")
        await db.commit()

    expired = await issue_setup_session("invite-a", ttl_seconds=-1)
    with pytest.raises(HTTPException) as setup_error:
        await resolve_setup_token(expired.token)
    assert setup_error.value.status_code == 401

    admin = await issue_admin_session()
    principal = await resolve_admin_token(admin.token)
    await revoke_admin_session(principal.session_id)
    with pytest.raises(HTTPException) as admin_error:
        await resolve_admin_token(admin.token)
    assert admin_error.value.status_code == 401


@pytest.mark.anyio
async def test_two_invites_cannot_claim_the_same_channel(tmp_path, monkeypatch):
    db_path = tmp_path / "ownership.db"
    monkeypatch.setattr(settings, "db_path", str(db_path))
    await init_db(str(db_path))
    async with aiosqlite.connect(db_path) as db:
        await db.executemany(
            "INSERT INTO invite_codes(code) VALUES (?)",
            [("invite-a",), ("invite-b",)],
        )
        await db.commit()

    first = await issue_setup_session("invite-a")
    second = await issue_setup_session("invite-b")
    first_principal = await resolve_setup_token(first.token)
    second_principal = await resolve_setup_token(second.token)
    await scope_setup_session_to_channel(first_principal.session_id, "channel-a")
    with pytest.raises(HTTPException) as conflict:
        await scope_setup_session_to_channel(second_principal.session_id, "channel-a")
    assert conflict.value.status_code == 403

    async with aiosqlite.connect(db_path) as db:
        await db.execute("DELETE FROM invite_codes WHERE code='invite-a'")
        await db.commit()
    with pytest.raises(HTTPException) as removed_invite:
        await resolve_setup_token(first.token)
    assert removed_invite.value.status_code == 401

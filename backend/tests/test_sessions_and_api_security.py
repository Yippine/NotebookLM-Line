from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException

from config import settings
from services.logging_service import sanitize_for_log
from services.rate_limit_service import limiter
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


def _insert_invite(db_path: Path, code: str, channel_id: str | None = None) -> None:
    with sqlite3.connect(db_path) as db:
        db.execute(
            "INSERT INTO invite_codes (code, used, channel_id) VALUES (?, ?, ?)",
            (code, int(channel_id is not None), channel_id),
        )
        db.commit()


def _insert_channel(db_path: Path, channel_id: str) -> None:
    with sqlite3.connect(db_path) as db:
        db.execute(
            """
            INSERT INTO channels (
                channel_id, channel_secret_encrypted,
                channel_access_token_encrypted, binding_status
            ) VALUES (?, 'encrypted-secret', 'encrypted-token', 'unbound')
            """,
            (channel_id,),
        )
        db.commit()


def test_setup_session_is_hashed_persistent_scoped_and_revocable(
    backend_db: Path,
) -> None:
    _insert_invite(backend_db, "invite-a", "channel-a")
    issued = asyncio.run(issue_setup_session("invite-a", "channel-a", ttl_seconds=120))

    with sqlite3.connect(backend_db) as db:
        stored = db.execute(
            "SELECT token_hash, invite_code, channel_id, scope, revoked_at FROM setup_sessions"
        ).fetchone()
    assert stored is not None
    assert stored[0] == hash_token(issued.token)
    assert stored[0] != issued.token
    assert issued.token not in "|".join(str(value) for value in stored)
    assert stored[1:4] == ("invite-a", "channel-a", "channel:setup")

    principal = asyncio.run(resolve_setup_token(issued.token))
    assert principal.channel_id == "channel-a"
    assert_channel_scope(principal, "channel-a")
    with pytest.raises(HTTPException) as wrong_channel:
        assert_channel_scope(principal, "channel-b")
    assert wrong_channel.value.status_code == 403

    asyncio.run(revoke_setup_session(principal.session_id))
    with pytest.raises(HTTPException) as revoked:
        asyncio.run(resolve_setup_token(issued.token))
    assert revoked.value.status_code == 401


def test_new_setup_login_revokes_previous_session_for_same_invite(
    backend_db: Path,
) -> None:
    _insert_invite(backend_db, "reused-invite", "channel-a")
    first = asyncio.run(issue_setup_session("reused-invite", "channel-a"))
    second = asyncio.run(issue_setup_session("reused-invite", "channel-a"))

    with pytest.raises(HTTPException) as old_session:
        asyncio.run(resolve_setup_token(first.token))
    assert old_session.value.status_code == 401
    assert asyncio.run(resolve_setup_token(second.token)).channel_id == "channel-a"


def test_setup_session_rejects_unexpected_scope(backend_db: Path) -> None:
    _insert_invite(backend_db, "invite-wrong-scope", "channel-a")
    issued = asyncio.run(
        issue_setup_session("invite-wrong-scope", "channel-a", ttl_seconds=120)
    )
    with sqlite3.connect(backend_db) as db:
        db.execute(
            "UPDATE setup_sessions SET scope='admin' WHERE token_hash=?",
            (hash_token(issued.token),),
        )
        db.commit()

    with pytest.raises(HTTPException) as rejected:
        asyncio.run(resolve_setup_token(issued.token))
    assert rejected.value.status_code == 401


def test_unscoped_setup_session_can_claim_only_one_channel(backend_db: Path) -> None:
    _insert_invite(backend_db, "invite-unscoped")
    issued = asyncio.run(issue_setup_session("invite-unscoped", ttl_seconds=120))
    principal = asyncio.run(resolve_setup_token(issued.token))

    asyncio.run(scope_setup_session_to_channel(principal.session_id, "channel-a"))
    scoped = asyncio.run(resolve_setup_token(issued.token))
    assert scoped.channel_id == "channel-a"
    with sqlite3.connect(backend_db) as db:
        invite_channel = db.execute(
            "SELECT channel_id FROM invite_codes WHERE code='invite-unscoped'"
        ).fetchone()[0]
    assert invite_channel == "channel-a"

    with pytest.raises(HTTPException) as mismatch:
        asyncio.run(scope_setup_session_to_channel(principal.session_id, "channel-b"))
    assert mismatch.value.status_code == 403


def test_expired_setup_and_admin_sessions_are_rejected(backend_db: Path) -> None:
    _insert_invite(backend_db, "expired-invite")
    setup = asyncio.run(issue_setup_session("expired-invite", ttl_seconds=-1))
    admin = asyncio.run(issue_admin_session(ttl_seconds=-1))

    with pytest.raises(HTTPException) as setup_error:
        asyncio.run(resolve_setup_token(setup.token))
    with pytest.raises(HTTPException) as admin_error:
        asyncio.run(resolve_admin_token(admin.token))
    assert setup_error.value.status_code == 401
    assert admin_error.value.status_code == 401


def test_admin_session_is_hashed_and_revocable(backend_db: Path) -> None:
    issued = asyncio.run(issue_admin_session(ttl_seconds=120))
    with sqlite3.connect(backend_db) as db:
        stored_hash, scope = db.execute(
            "SELECT token_hash, scope FROM admin_sessions"
        ).fetchone()
    assert stored_hash == hash_token(issued.token)
    assert stored_hash != issued.token
    assert scope == "admin"

    principal = asyncio.run(resolve_admin_token(issued.token))
    asyncio.run(revoke_admin_session(principal.session_id))
    with pytest.raises(HTTPException) as revoked:
        asyncio.run(resolve_admin_token(issued.token))
    assert revoked.value.status_code == 401


@pytest.mark.anyio
async def test_query_string_secrets_are_rejected_before_authorization(
    asgi_app,
) -> None:
    transport = httpx.ASGITransport(app=asgi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        token_response = await client.get("/api/channels/anything?token=setup-secret")
        password_response = await client.get(
            "/api/admin/students?admin_password=admin-secret"
        )

    assert token_response.status_code == 400
    assert password_response.status_code == 400
    combined = token_response.text + password_response.text
    assert "setup-secret" not in combined
    assert "admin-secret" not in combined
    assert token_response.headers.get("x-request-id")


@pytest.mark.anyio
async def test_validation_errors_never_reflect_sensitive_input(asgi_app) -> None:
    oversized_password = "password-secret-" + ("x" * 1100)
    transport = httpx.ASGITransport(app=asgi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/api/admin/login", json={"password": oversized_password}
        )

    assert response.status_code == 422
    assert response.json()["detail"] == "輸入資料格式不正確"
    assert "password-secret" not in response.text
    assert response.headers.get("x-request-id")


@pytest.mark.anyio
async def test_setup_bearer_cannot_read_another_channel(
    backend_db: Path,
    asgi_app,
) -> None:
    _insert_channel(backend_db, "channel-a")
    _insert_channel(backend_db, "channel-b")
    _insert_invite(backend_db, "invite-a", "channel-a")
    issued = await issue_setup_session("invite-a", "channel-a")
    headers = {"Authorization": f"Bearer {issued.token}"}

    transport = httpx.ASGITransport(app=asgi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        own = await client.get("/api/channels/channel-a", headers=headers)
        other = await client.get("/api/channels/channel-b", headers=headers)
        unknown = await client.get("/api/channels/does-not-exist", headers=headers)

    assert own.status_code == 200
    assert other.status_code == 403
    assert unknown.status_code == 403
    assert other.json() == unknown.json()


@pytest.mark.anyio
async def test_expired_invite_cannot_issue_setup_session(
    backend_db: Path,
    asgi_app,
) -> None:
    with sqlite3.connect(backend_db) as db:
        db.execute(
            """
            INSERT INTO invite_codes (code, expires_at)
            VALUES ('expired-invite-code', '2020-01-01T00:00:00+00:00')
            """
        )
        db.commit()

    transport = httpx.ASGITransport(app=asgi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/api/verify-invite", json={"code": "expired-invite-code"}
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "邀請碼已過期，請聯繫講師"


@pytest.mark.anyio
async def test_admin_login_uses_body_and_persists_only_token_hash(
    backend_db: Path,
    asgi_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    password = "admin-password-not-in-url"
    monkeypatch.setattr(settings, "admin_password", password)
    await limiter.reset()
    transport = httpx.ASGITransport(app=asgi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.post("/api/admin/login", json={"password": password})

    assert response.status_code == 200
    token = response.json()["token"]
    with sqlite3.connect(backend_db) as db:
        token_hash = db.execute("SELECT token_hash FROM admin_sessions").fetchone()[0]
    assert token_hash == hash_token(token)
    assert token != token_hash
    assert password not in response.text


@pytest.mark.anyio
async def test_admin_login_supports_unicode_password(
    asgi_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    password = "課程管理者專用密碼-安全版本"
    monkeypatch.setattr(settings, "admin_password", password)
    await limiter.reset()
    transport = httpx.ASGITransport(app=asgi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.post("/api/admin/login", json={"password": password})

    assert response.status_code == 200
    assert password not in response.text


@pytest.mark.anyio
async def test_cors_allows_configured_origin_and_denies_unknown_origin(
    asgi_app,
) -> None:
    transport = httpx.ASGITransport(app=asgi_app)
    base_headers = {"Access-Control-Request-Method": "GET"}
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        allowed = await client.options(
            "/api/admin/students",
            headers={"Origin": "http://localhost:5173", **base_headers},
        )
        denied = await client.options(
            "/api/admin/students",
            headers={"Origin": "https://attacker.example", **base_headers},
        )

    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert denied.status_code == 400
    assert "access-control-allow-origin" not in denied.headers


@pytest.mark.anyio
async def test_high_risk_login_rate_limit_returns_retry_after(
    asgi_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "rate_limit_admin_login_attempts", 1)
    await limiter.reset()
    transport = httpx.ASGITransport(app=asgi_app, client=("198.51.100.20", 12345))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        first = await client.post("/api/admin/login", json={"password": "wrong"})
        second = await client.post("/api/admin/login", json={"password": "wrong"})

    assert first.status_code == 401
    assert second.status_code == 429
    assert int(second.headers["retry-after"]) >= 1


def test_log_sanitizer_removes_all_supported_secret_shapes() -> None:
    raw = (
        "Bearer bearer-secret "
        "https://api.example/path?token=query-token&admin_password=query-password "
        "https://notebooklm.google.com/notebook/CapabilityId_123 "
        '{"storage_state_json":"google-cookie", "channel_secret":"line-secret"}'
    )
    sanitized = sanitize_for_log(raw)
    for secret in (
        "bearer-secret",
        "query-token",
        "query-password",
        "CapabilityId_123",
        "google-cookie",
        "line-secret",
    ):
        assert secret not in sanitized
    assert "<redacted>" in sanitized


def test_log_sanitizer_redacts_invite_and_nested_storage_state() -> None:
    cookie_secret = "google-cookie-secret"
    invite_secret = "course-invite-secret"
    raw = {
        "invite_code": invite_secret,
        "storage_state_json": {
            "cookies": [
                {"name": "SID", "value": cookie_secret, "domain": ".google.com"}
            ]
        },
    }
    sanitized = sanitize_for_log(raw)
    query = sanitize_for_log(f"https://example.test/setup?code={invite_secret}")

    assert cookie_secret not in sanitized
    assert invite_secret not in sanitized
    assert invite_secret not in query

from __future__ import annotations

import aiosqlite
import httpx
import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from config import settings
from database import init_db
from main import app
from models import ChannelCreate
from services.crypto_service import decrypt_text, reset_encryption_cache
from services.rate_limit_service import limiter


def test_channel_credentials_strip_edges_and_reject_whitespace_only() -> None:
    normalized = ChannelCreate(
        channel_id="  channel-a\n",
        channel_secret="\tline-secret ",
        channel_access_token=" line-access-token\r\n",
    )

    assert normalized.model_dump() == {
        "channel_id": "channel-a",
        "channel_secret": "line-secret",
        "channel_access_token": "line-access-token",
    }
    with pytest.raises(ValidationError):
        ChannelCreate(
            channel_id="channel-a",
            channel_secret=" \t\r\n ",
            channel_access_token="line-access-token",
        )


@pytest.mark.anyio
async def test_bearer_sessions_query_rejection_cors_and_encrypted_channel(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "api.db"
    monkeypatch.setattr(settings, "db_path", str(db_path))
    monkeypatch.setattr(settings, "admin_password", "correct horse battery staple")
    monkeypatch.setattr(settings, "encryption_key", Fernet.generate_key().decode())
    reset_encryption_cache()
    await init_db(str(db_path))
    await limiter.reset()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        leaked_admin = await client.get(
            "/api/admin/students?admin_password=correct-horse"
        )
        assert leaked_admin.status_code == 400
        assert leaked_admin.headers["x-request-id"]

        unauthenticated = await client.get("/api/admin/students")
        assert unauthenticated.status_code == 401

        login = await client.post(
            "/api/admin/login", json={"password": "correct horse battery staple"}
        )
        assert login.status_code == 200
        admin_token = login.json()["token"]
        assert login.json()["token_type"] == "bearer"

        students = await client.get(
            "/api/admin/students",
            headers={
                "Authorization": f"Bearer {admin_token}",
                "Origin": "http://localhost:5173",
            },
        )
        assert students.status_code == 200
        assert (
            students.headers["access-control-allow-origin"] == "http://localhost:5173"
        )

        disallowed = await client.get(
            "/api/admin/students",
            headers={
                "Authorization": f"Bearer {admin_token}",
                "Origin": "https://attacker.example",
            },
        )
        assert disallowed.status_code == 200
        assert "access-control-allow-origin" not in disallowed.headers

        generated = await client.post(
            "/api/invite-codes/generate?count=1",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        invite_code = generated.json()["codes"][0]
        verified = await client.post("/api/verify-invite", json={"code": invite_code})
        assert verified.status_code == 200
        setup_token = verified.json()["token"]

        rejected_query = await client.post(
            f"/api/channels?token={setup_token}",
            json={
                "channel_id": "channel-a",
                "channel_secret": "line-secret",
                "channel_access_token": "line-access-token",
            },
        )
        assert rejected_query.status_code == 400

        created = await client.post(
            "/api/channels",
            headers={"Authorization": f"Bearer {setup_token}"},
            json={
                "channel_id": "  channel-a\n",
                "channel_secret": "\tline-secret ",
                "channel_access_token": " line-access-token\r\n",
            },
        )
        assert created.status_code == 200, created.text
        assert created.json()["channel_id"] == "channel-a"

        own = await client.get(
            "/api/channels/channel-a",
            headers={"Authorization": f"Bearer {setup_token}"},
        )
        assert own.status_code == 200
        assert own.json()["binding_status"] == "unbound"

        cross_channel = await client.get(
            "/api/channels/channel-b",
            headers={"Authorization": f"Bearer {setup_token}"},
        )
        assert cross_channel.status_code == 403

    async with aiosqlite.connect(db_path) as db:
        row = await (
            await db.execute(
                """
                SELECT channel_secret, channel_access_token,
                       channel_secret_encrypted, channel_access_token_encrypted
                FROM channels WHERE channel_id='channel-a'
                """
            )
        ).fetchone()
        setup_row = await (
            await db.execute("SELECT token_hash FROM setup_sessions LIMIT 1")
        ).fetchone()
        admin_row = await (
            await db.execute("SELECT token_hash FROM admin_sessions LIMIT 1")
        ).fetchone()
    assert row[0:2] == ("", "")
    assert decrypt_text(row[2]) == "line-secret"
    assert decrypt_text(row[3]) == "line-access-token"
    assert setup_token not in setup_row[0]
    assert admin_token not in admin_row[0]
    reset_encryption_cache()

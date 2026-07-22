from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json

import aiosqlite
from fastapi import FastAPI
import httpx
import pytest

from database import init_db
from routers import webhook


def signature(body: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


@pytest.mark.anyio
async def test_expired_channel_state_is_only_returned_after_signature_check(
    tmp_path, monkeypatch
):
    path = str(tmp_path / "webhook.db")
    await init_db(path)
    secret = "channel-secret"
    expired = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    async with aiosqlite.connect(path) as db:
        await db.execute(
            """
            INSERT INTO channels (
                channel_id, channel_secret, channel_access_token, expires_at
            ) VALUES ('channel-a', ?, 'access-token', ?)
            """,
            (secret, expired),
        )
        await db.commit()
    monkeypatch.setattr(webhook, "DB", path)
    app = FastAPI()
    app.include_router(webhook.router)
    body = json.dumps({"events": []}, separators=(",", ":")).encode()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        invalid = await client.post(
            "/webhook/channel-a",
            content=body,
            headers={"X-Line-Signature": "invalid"},
        )
        valid = await client.post(
            "/webhook/channel-a",
            content=body,
            headers={"X-Line-Signature": signature(body, secret)},
        )

    assert invalid.status_code == 403
    assert valid.status_code == 200
    assert valid.json() == {"status": "expired"}


@pytest.mark.anyio
async def test_malformed_signed_event_is_ignored_not_raised(tmp_path, monkeypatch):
    path = str(tmp_path / "webhook.db")
    await init_db(path)
    secret = "channel-secret"
    async with aiosqlite.connect(path) as db:
        await db.execute(
            """
            INSERT INTO channels (channel_id, channel_secret, channel_access_token)
            VALUES ('channel-a', ?, 'access-token')
            """,
            (secret,),
        )
        await db.commit()
    monkeypatch.setattr(webhook, "DB", path)
    app = FastAPI()
    app.include_router(webhook.router)
    body = json.dumps({"events": [{"type": "message"}, "bad"]}).encode()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/webhook/channel-a",
            content=body,
            headers={"X-Line-Signature": signature(body, secret)},
        )

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from config import settings


async def _login(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(settings, "admin_password", "expiry-admin-password")
    response = await client.post(
        "/api/admin/login", json={"password": "expiry-admin-password"}
    )
    assert response.status_code == 200
    return response.json()["token"]


@pytest.mark.anyio
async def test_course_expiry_applies_only_to_channels_existing_at_click_time(
    backend_db, asgi_app, monkeypatch
):
    transport = httpx.ASGITransport(app=asgi_app)
    expiry = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        admin_token = await _login(client, monkeypatch)
        headers = {"Authorization": f"Bearer {admin_token}"}

        generated = await client.post(
            "/api/invite-codes/generate?count=2", headers=headers
        )
        first_invite, future_invite = generated.json()["codes"]
        named = await client.put(
            f"/api/admin/invite-codes/{first_invite}/name",
            headers=headers,
            json={"student_name": " 測試學員 "},
        )
        assert named.status_code == 200
        assert named.json()["student_name"] == "測試學員"
        verified = await client.post("/api/verify-invite", json={"code": first_invite})
        setup_headers = {"Authorization": f"Bearer {verified.json()['token']}"}
        created = await client.post(
            "/api/channels",
            headers=setup_headers,
            json={
                "channel_id": "existing-channel",
                "channel_secret": "secret",
                "channel_access_token": "token",
            },
        )
        assert created.status_code == 200
        students = await client.get("/api/admin/students", headers=headers)
        named_student = next(
            student for student in students.json() if student["code"] == first_invite
        )
        assert named_student["student_name"] == "測試學員"

        configured = await client.put(
            "/api/admin/set-expiry",
            headers=headers,
            json={"expires_at": expiry},
        )
        assert configured.status_code == 200
        normalized_expiry = configured.json()["expires_at"]
        assert configured.json()["applied_count"] == 1

        future_verified = await client.post(
            "/api/verify-invite", json={"code": future_invite}
        )
        future_created = await client.post(
            "/api/channels",
            headers={"Authorization": f"Bearer {future_verified.json()['token']}"},
            json={
                "channel_id": "future-channel",
                "channel_secret": "secret",
                "channel_access_token": "token",
            },
        )
        assert future_created.status_code == 200

        later_expiry = (datetime.now(timezone.utc) + timedelta(days=60)).isoformat()
        only_unassigned = await client.put(
            "/api/admin/set-expiry",
            headers=headers,
            json={"expires_at": later_expiry, "mode": "unassigned"},
        )
        assert only_unassigned.status_code == 200
        assert only_unassigned.json()["applied_count"] == 1
        normalized_later_expiry = only_unassigned.json()["expires_at"]

        current = await client.get("/api/admin/set-expiry", headers=headers)
        assert current.json() == {
            "expires_at": normalized_later_expiry,
            "applied_count": 2,
            "total_count": 2,
        }

        with sqlite3.connect(backend_db) as db:
            assert db.execute(
                "SELECT expires_at FROM channels WHERE channel_id='existing-channel'"
            ).fetchone() == (normalized_expiry,)
            assert db.execute(
                "SELECT expires_at FROM channels WHERE channel_id='future-channel'"
            ).fetchone() == (normalized_later_expiry,)

        individual = await client.put(
            "/api/admin/channels/existing-channel/expiry",
            headers=headers,
            json={"expires_at": later_expiry},
        )
        assert individual.status_code == 200
        assert individual.json()["expires_at"] == normalized_later_expiry

        selected_expiry = (datetime.now(timezone.utc) + timedelta(days=90)).isoformat()
        selected = await client.put(
            "/api/admin/channels/expiry-batch",
            headers=headers,
            json={
                "channel_ids": ["existing-channel"],
                "expires_at": selected_expiry,
            },
        )
        assert selected.status_code == 200
        assert selected.json()["applied_count"] == 1
        with sqlite3.connect(backend_db) as db:
            existing_expiry = db.execute(
                "SELECT expires_at FROM channels WHERE channel_id='existing-channel'"
            ).fetchone()[0]
            future_expiry = db.execute(
                "SELECT expires_at FROM channels WHERE channel_id='future-channel'"
            ).fetchone()[0]
        assert existing_expiry != future_expiry
        assert future_expiry == normalized_later_expiry

        cancelled = await client.put(
            "/api/admin/set-expiry",
            headers=headers,
            json={"expires_at": None, "mode": "all"},
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["expires_at"] is None

    with sqlite3.connect(backend_db) as db:
        assert db.execute(
            "SELECT course_expires_at FROM course_settings WHERE id=1"
        ).fetchone() == (None,)
        # Cancellation keeps both bindings and clears only their policies.
        assert db.execute("SELECT COUNT(*) FROM channels").fetchone() == (2,)
        assert db.execute(
            "SELECT COUNT(*) FROM channels WHERE expires_at IS NOT NULL"
        ).fetchone() == (0,)


@pytest.mark.anyio
async def test_previous_batch_expiry_does_not_block_an_unused_invite(
    backend_db, asgi_app, monkeypatch
):
    del backend_db
    transport = httpx.ASGITransport(app=asgi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        admin_token = await _login(client, monkeypatch)
        headers = {"Authorization": f"Bearer {admin_token}"}
        generated = await client.post(
            "/api/invite-codes/generate?count=1", headers=headers
        )
        invite = generated.json()["codes"][0]
        await client.put(
            "/api/admin/set-expiry",
            headers=headers,
            json={"expires_at": "2000-01-01T00:00:00Z"},
        )

        response = await client.post("/api/verify-invite", json={"code": invite})

    assert response.status_code == 200


@pytest.mark.anyio
async def test_admin_can_delete_only_unused_invite_codes(backend_db, asgi_app, monkeypatch):
    del backend_db
    transport = httpx.ASGITransport(app=asgi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        admin_token = await _login(client, monkeypatch)
        headers = {"Authorization": f"Bearer {admin_token}"}
        generated = await client.post(
            "/api/invite-codes/generate?count=2", headers=headers
        )
        assert generated.status_code == 200
        unused_code, used_code = generated.json()["codes"]

        deleted = await client.delete(
            f"/api/admin/invite-codes/{unused_code}", headers=headers
        )
        assert deleted.status_code == 200
        assert deleted.json() == {"status": "deleted", "code": unused_code}

        missing = await client.delete(
            f"/api/admin/invite-codes/{unused_code}", headers=headers
        )
        assert missing.status_code == 404

        verified = await client.post(
            "/api/verify-invite", json={"code": used_code}
        )
        assert verified.status_code == 200
        in_use = await client.delete(
            f"/api/admin/invite-codes/{used_code}", headers=headers
        )
        assert in_use.status_code == 409
        assert in_use.json()["detail"]["code"] == "invite_code_in_use"


@pytest.mark.anyio
async def test_expiry_cleanup_deletes_only_expired_learner_bindings(backend_db):
    from main import cleanup_expired

    with sqlite3.connect(backend_db) as db:
        db.execute(
            "INSERT INTO channels (channel_id, expires_at) VALUES (?, ?)",
            ("expired-channel", "2000-01-01T00:00:00+00:00"),
        )
        db.execute(
            "INSERT INTO channels (channel_id, expires_at) VALUES (?, NULL)",
            ("new-channel",),
        )
        db.execute(
            """
            INSERT INTO invite_codes (code, used, channel_id)
            VALUES ('expired-invite', 1, 'expired-channel')
            """
        )
        db.execute(
            """
            INSERT INTO setup_sessions
                (token_hash, invite_code, channel_id, expires_at)
            VALUES
                ('expired-token-hash', 'expired-invite', 'expired-channel',
                 '2030-01-01T00:00:00+00:00')
            """
        )
        db.execute("INSERT INTO invite_codes (code, used) VALUES ('unused-invite', 0)")
        db.execute(
            """
            INSERT INTO invite_codes (code, used, channel_id)
            VALUES ('new-invite', 1, 'new-channel')
            """
        )
        db.execute(
            """
            UPDATE course_settings
            SET course_expires_at='2000-01-01T00:00:00+00:00'
            WHERE id=1
            """
        )
        db.commit()

    await cleanup_expired()

    with sqlite3.connect(backend_db) as db:
        assert db.execute(
            "SELECT channel_id FROM channels ORDER BY channel_id"
        ).fetchall() == [("new-channel",)]
        assert db.execute("SELECT code FROM invite_codes ORDER BY code").fetchall() == [
            ("new-invite",),
            ("unused-invite",),
        ]
        assert db.execute("SELECT COUNT(*) FROM setup_sessions").fetchone() == (0,)
        assert db.execute(
            "SELECT course_expires_at FROM course_settings WHERE id=1"
        ).fetchone() == (None,)


@pytest.mark.anyio
async def test_deleted_channel_webhook_is_silently_ignored(asgi_app):
    transport = httpx.ASGITransport(app=asgi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/webhook/deleted-channel",
            json={"events": []},
        )

    assert response.status_code == 200
    assert response.json() == {"status": "unbound"}

import asyncio
import json
import os
import sqlite3

import database
import services.alert_service as alert_service
import services.nlm_service as nlm_service
from config import settings
from services.crypto_service import encrypt


class _FakeNotebooksAPI:
    def __init__(self, marker: str):
        self._marker = marker

    async def list(self):
        if self._marker == "broken":
            raise RuntimeError("session expired")
        return []


class _FakeClient:
    def __init__(self, marker: str):
        self.notebooks = _FakeNotebooksAPI(marker)


class _FakeStorageContext:
    async def __aenter__(self):
        # 環境變數只會在 __aenter__ 期間被設定（見 _run_with_auth
        # 那個範圍很窄的鎖）——必須在這裡就擷取它，而不是等到鎖
        # （以及環境變數）被釋放之後，在某個協程裡才讀取。
        auth = json.loads(os.environ["NOTEBOOKLM_AUTH_JSON"])
        return _FakeClient(auth.get("marker", "healthy"))

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return None


def _setup(tmp_path, monkeypatch, channels: dict[str, str]):
    """``channels`` 將 channel_id 對應到 "healthy" | "broken"。"""
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(database, "DB", db_path)
    monkeypatch.setattr(nlm_service, "DB", db_path)
    asyncio.run(database.init_db())

    conn = sqlite3.connect(db_path)
    for channel_id, marker in channels.items():
        encrypted = encrypt({"marker": marker})
        conn.execute(
            "INSERT INTO channels "
            "(channel_id, channel_secret, channel_access_token, nlm_auth_json_encrypted, notebook_id) "
            "VALUES (?,?,?,?,?)",
            (channel_id, "secret", "token", encrypted, "notebook-1"),
        )
    conn.commit()
    conn.close()

    class _FakeNotebookLMClient:
        @staticmethod
        def from_storage():
            return _FakeStorageContext()

    monkeypatch.setattr(nlm_service, "NotebookLMClient", _FakeNotebookLMClient)

    monkeypatch.setattr(settings, "admin_line_user_id", "U-admin")
    monkeypatch.setattr(settings, "admin_alert_access_token", "token-123")
    monkeypatch.setattr(alert_service, "_last_sent", {})

    alerts = []

    async def fake_push_text(user_id, access_token, text):
        alerts.append(text)

    monkeypatch.setattr(alert_service, "push_text", fake_push_text)

    return alerts


def test_healthy_channel_does_not_alert(tmp_path, monkeypatch):
    alerts = _setup(tmp_path, monkeypatch, {"chan-ok": "healthy"})

    asyncio.run(nlm_service.check_all_channels_health())

    assert alerts == []


def test_broken_channel_alerts_admin(tmp_path, monkeypatch):
    alerts = _setup(tmp_path, monkeypatch, {"chan-broken": "broken"})

    asyncio.run(nlm_service.check_all_channels_health())

    assert len(alerts) == 1
    assert "chan-broken" in alerts[0]


def test_one_broken_channel_does_not_stop_checking_others(tmp_path, monkeypatch):
    alerts = _setup(
        tmp_path,
        monkeypatch,
        {"chan-broken": "broken", "chan-ok": "healthy", "chan-broken-2": "broken"},
    )

    asyncio.run(nlm_service.check_all_channels_health())

    assert len(alerts) == 2
    alerted_channels = {"chan-broken", "chan-broken-2"}
    assert all(any(c in a for c in alerted_channels) for a in alerts)


def _mark_marker(tmp_path, channel_id: str, marker: str) -> None:
    """Overwrite a channel's stored auth so the next health check sees a
    different (healthy/broken) marker, simulating the underlying session
    actually recovering or breaking between check cycles."""
    db_path = str(tmp_path / "test.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE channels SET nlm_auth_json_encrypted=? WHERE channel_id=?",
        (encrypt({"marker": marker}), channel_id),
    )
    conn.commit()
    conn.close()


def test_repeated_failure_across_check_cycles_only_alerts_once(tmp_path, monkeypatch):
    """A channel that stays broken across multiple 4-hourly check cycles
    must only trigger one alert (at the healthy->expired transition), not
    one per cycle — otherwise the admin gets paged every 4 hours for the
    same still-unresolved incident."""
    alerts = _setup(tmp_path, monkeypatch, {"chan-broken": "broken"})

    asyncio.run(nlm_service.check_all_channels_health())
    asyncio.run(nlm_service.check_all_channels_health())
    asyncio.run(nlm_service.check_all_channels_health())

    assert len(alerts) == 1


def test_recovery_then_new_failure_alerts_again(tmp_path, monkeypatch):
    """Once a channel recovers (expired -> healthy), a subsequent fresh
    failure must alert again — the dedup is per-incident, not permanent
    after the first alert ever sent for that channel."""
    alerts = _setup(tmp_path, monkeypatch, {"chan-flaky": "broken"})

    asyncio.run(nlm_service.check_all_channels_health())
    assert len(alerts) == 1

    _mark_marker(tmp_path, "chan-flaky", "healthy")
    asyncio.run(nlm_service.check_all_channels_health())
    assert len(alerts) == 1  # recovery itself is not alerted

    _mark_marker(tmp_path, "chan-flaky", "broken")
    asyncio.run(nlm_service.check_all_channels_health())
    assert len(alerts) == 2  # a fresh incident alerts again

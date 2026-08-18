import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))
import nlm_cookie_refresh  # noqa: E402


def _write_storage_state(tmp_path):
    path = tmp_path / "storage_state.json"
    path.write_text(json.dumps({"cookies": []}), encoding="utf-8")
    return str(path)


def _stub_alerts(monkeypatch):
    """main() 現在都透過 send_admin_alert_with_cooldown 發告警——這裡把
    它換成一個直接記錄訊息本文的假函式，讓既有測試不用管冷卻邏輯，
    只要斷言「有沒有發、發了什麼」。"""
    alerts = []
    monkeypatch.setattr(
        nlm_cookie_refresh, "send_admin_alert_with_cooldown",
        lambda reason_key, msg: alerts.append(msg),
    )
    return alerts


def test_success_path_does_not_notify_admin(monkeypatch, tmp_path):
    """只有失敗才告警——正常運作時完全靜默，不發「一切正常」的訊息。"""
    alerts = _stub_alerts(monkeypatch)

    monkeypatch.setattr(nlm_cookie_refresh, "any_channel_expired", lambda: True)
    monkeypatch.setattr(nlm_cookie_refresh, "refresh_cookies", lambda: True)
    monkeypatch.setattr(nlm_cookie_refresh, "bound_channel_ids", lambda: ["chan-1", "chan-2", "chan-3"])
    monkeypatch.setattr(nlm_cookie_refresh, "_find_local_storage_state", lambda: _write_storage_state(tmp_path))
    monkeypatch.setattr(nlm_cookie_refresh, "rebind_channel", lambda cid, storage_state: None)

    nlm_cookie_refresh.main()

    assert alerts == []


def test_no_channel_expired_skips_refresh_entirely(monkeypatch):
    """沒有任何 channel 被標成 expired，就是這支腳本從『固定排程盲目
    刷新』改成『偵測到掛了才刷新』的核心行為——不該去讀瀏覽器 cookie、
    不該打 admin API、更不該發告警。"""
    alerts = _stub_alerts(monkeypatch)
    refresh_calls = []

    monkeypatch.setattr(nlm_cookie_refresh, "any_channel_expired", lambda: False)
    monkeypatch.setattr(nlm_cookie_refresh, "refresh_cookies", lambda: refresh_calls.append(1) or True)

    nlm_cookie_refresh.main()

    assert refresh_calls == []
    assert alerts == []


def test_no_bound_channels_does_not_notify_admin(monkeypatch):
    alerts = _stub_alerts(monkeypatch)

    monkeypatch.setattr(nlm_cookie_refresh, "any_channel_expired", lambda: True)
    monkeypatch.setattr(nlm_cookie_refresh, "refresh_cookies", lambda: True)
    monkeypatch.setattr(nlm_cookie_refresh, "bound_channel_ids", lambda: [])

    nlm_cookie_refresh.main()

    # 沒有任何 channel 可以續期，沒有實際結果好回報，不該發通知。
    assert alerts == []


def test_cookie_read_failure_still_notifies_with_existing_message(monkeypatch):
    """既有的失敗路徑（cookie 讀取失敗）維持原本的通知內容不變。"""
    alerts = _stub_alerts(monkeypatch)

    monkeypatch.setattr(nlm_cookie_refresh, "any_channel_expired", lambda: True)
    monkeypatch.setattr(nlm_cookie_refresh, "refresh_cookies", lambda: False)

    with pytest.raises(SystemExit):
        nlm_cookie_refresh.main()

    assert len(alerts) == 1
    assert "無法從本機瀏覽器讀取" in alerts[0]


def test_storage_state_missing_still_notifies_with_existing_message(monkeypatch):
    """既有的失敗路徑（找不到 storage_state.json）維持原本的通知內容不變。"""
    alerts = _stub_alerts(monkeypatch)

    monkeypatch.setattr(nlm_cookie_refresh, "any_channel_expired", lambda: True)
    monkeypatch.setattr(nlm_cookie_refresh, "refresh_cookies", lambda: True)
    monkeypatch.setattr(nlm_cookie_refresh, "bound_channel_ids", lambda: ["chan-1"])
    monkeypatch.setattr(nlm_cookie_refresh, "_find_local_storage_state", lambda: None)

    with pytest.raises(SystemExit):
        nlm_cookie_refresh.main()

    assert len(alerts) == 1
    assert "找不到" in alerts[0]


def test_shred_file_overwrites_content_and_removes_file(tmp_path):
    path = tmp_path / "storage_state.json"
    path.write_bytes(b'{"cookies": ["secret-value"]}')

    nlm_cookie_refresh._shred_file(str(path))

    # 檔案本身要真的不見，而不是只是清空內容。
    assert not path.exists()


def test_successful_run_shreds_the_storage_state_file(monkeypatch, tmp_path):
    storage_path = _write_storage_state(tmp_path)
    _stub_alerts(monkeypatch)

    monkeypatch.setattr(nlm_cookie_refresh, "any_channel_expired", lambda: True)
    monkeypatch.setattr(nlm_cookie_refresh, "refresh_cookies", lambda: True)
    monkeypatch.setattr(nlm_cookie_refresh, "bound_channel_ids", lambda: ["chan-1"])
    monkeypatch.setattr(nlm_cookie_refresh, "_find_local_storage_state", lambda: storage_path)
    monkeypatch.setattr(nlm_cookie_refresh, "rebind_channel", lambda cid, storage_state: None)

    nlm_cookie_refresh.main()

    # 內容已經上傳給後端，本機這份含有 cookie 的檔案不該再留著。
    assert not os.path.exists(storage_path)


def test_read_failure_still_shreds_the_file(monkeypatch, tmp_path):
    """就算讀取／解析失敗，磁碟上那份檔案還是曾經含有 cookie 內容，
    一樣要清掉，不能因為讀取失敗就放著不管。"""
    path = tmp_path / "storage_state.json"
    path.write_text("not valid json", encoding="utf-8")
    _stub_alerts(monkeypatch)

    monkeypatch.setattr(nlm_cookie_refresh, "any_channel_expired", lambda: True)
    monkeypatch.setattr(nlm_cookie_refresh, "refresh_cookies", lambda: True)
    monkeypatch.setattr(nlm_cookie_refresh, "bound_channel_ids", lambda: ["chan-1"])
    monkeypatch.setattr(nlm_cookie_refresh, "_find_local_storage_state", lambda: str(path))

    with pytest.raises(SystemExit):
        nlm_cookie_refresh.main()

    assert not path.exists()


def test_partial_rebind_failure_still_notifies_with_existing_message(monkeypatch, tmp_path):
    """既有的失敗路徑（部分 channel rebind 失敗）維持原本的通知內容不變，
    且不會被誤認成功、也不會額外多發一則成功通知。"""
    alerts = _stub_alerts(monkeypatch)

    monkeypatch.setattr(nlm_cookie_refresh, "any_channel_expired", lambda: True)
    monkeypatch.setattr(nlm_cookie_refresh, "refresh_cookies", lambda: True)
    monkeypatch.setattr(nlm_cookie_refresh, "bound_channel_ids", lambda: ["chan-good", "chan-bad"])
    monkeypatch.setattr(nlm_cookie_refresh, "_find_local_storage_state", lambda: _write_storage_state(tmp_path))

    def fake_rebind(cid, storage_state):
        return None if cid == "chan-good" else f"{cid}: boom"

    monkeypatch.setattr(nlm_cookie_refresh, "rebind_channel", fake_rebind)

    with pytest.raises(SystemExit):
        nlm_cookie_refresh.main()

    assert len(alerts) == 1
    assert "chan-bad: boom" in alerts[0]
    assert "回寫失敗" in alerts[0]


def test_any_channel_expired_reads_health_status_from_db(tmp_path, monkeypatch):
    """any_channel_expired() 是這支腳本『偵測到掛了才刷新』的閘門——
    直接對著一份真的 SQLite 檔案驗證它讀的是正確的欄位與條件，
    而不是只靠 monkeypatch 過的假版本間接測試。"""
    import sqlite3

    db_path = tmp_path / "data.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE channels (channel_id TEXT, notebook_id TEXT, nlm_health_status TEXT)"
    )
    conn.execute(
        "INSERT INTO channels VALUES (?,?,?)", ("chan-1", "nb-1", "healthy")
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(nlm_cookie_refresh, "DB_PATH", db_path)

    assert nlm_cookie_refresh.any_channel_expired() is False

    conn = sqlite3.connect(str(db_path))
    conn.execute("UPDATE channels SET nlm_health_status='expired' WHERE channel_id='chan-1'")
    conn.commit()
    conn.close()

    assert nlm_cookie_refresh.any_channel_expired() is True


def test_send_admin_alert_with_cooldown_suppresses_repeat_within_window(tmp_path, monkeypatch):
    """排程現在跑得很頻繁，同一個尚未解決的失敗原因不該每次都發一則
    新的告警——在冷卻視窗內只發第一次。"""
    monkeypatch.setattr(nlm_cookie_refresh, "ALERT_STATE_PATH", tmp_path / "alert_state.json")
    sent = []
    monkeypatch.setattr(nlm_cookie_refresh, "send_admin_alert", lambda msg: sent.append(msg))

    nlm_cookie_refresh.send_admin_alert_with_cooldown("some_reason", "first failure")
    nlm_cookie_refresh.send_admin_alert_with_cooldown("some_reason", "second failure, same reason")

    assert sent == ["first failure"]


def test_send_admin_alert_with_cooldown_does_not_suppress_different_reason(tmp_path, monkeypatch):
    """不同的失敗原因彼此獨立，不該被同一個冷卻視窗誤擋。"""
    monkeypatch.setattr(nlm_cookie_refresh, "ALERT_STATE_PATH", tmp_path / "alert_state.json")
    sent = []
    monkeypatch.setattr(nlm_cookie_refresh, "send_admin_alert", lambda msg: sent.append(msg))

    nlm_cookie_refresh.send_admin_alert_with_cooldown("reason_a", "failure A")
    nlm_cookie_refresh.send_admin_alert_with_cooldown("reason_b", "failure B")

    assert sent == ["failure A", "failure B"]


def test_successful_run_clears_alert_state_so_next_failure_alerts_immediately(monkeypatch, tmp_path):
    """成功刷新一次之後，就算馬上又壞掉、還是同一種失敗原因，也該
    立刻告警，不能被上一次故障留下的冷卻視窗擋住。"""
    alert_state_path = tmp_path / "alert_state.json"
    monkeypatch.setattr(nlm_cookie_refresh, "ALERT_STATE_PATH", alert_state_path)
    alert_state_path.write_text(
        json.dumps({"browser_cookie_read_failed": 0}), encoding="utf-8"
    )

    monkeypatch.setattr(nlm_cookie_refresh, "any_channel_expired", lambda: True)
    monkeypatch.setattr(nlm_cookie_refresh, "refresh_cookies", lambda: True)
    monkeypatch.setattr(nlm_cookie_refresh, "bound_channel_ids", lambda: ["chan-1"])
    monkeypatch.setattr(nlm_cookie_refresh, "_find_local_storage_state", lambda: _write_storage_state(tmp_path))
    monkeypatch.setattr(nlm_cookie_refresh, "rebind_channel", lambda cid, storage_state: None)

    nlm_cookie_refresh.main()

    assert not alert_state_path.exists()

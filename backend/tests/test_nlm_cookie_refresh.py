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


def _isolate_alert_state(monkeypatch, tmp_path):
    """把 ALERT_STATE_PATH 跟 REFRESH_ATTEMPT_STATE_PATH 都換成這次
    測試專用的暫存檔案，避免真的去讀寫專案自己 data/ 底下的兩份狀態
    檔——不隔離的話，同一次 pytest 執行裡前一個測試留下的時間戳記，
    會讓後面的測試被誤判成『最近才發過告警／嘗試過修復』而被節流
    跳過。這兩份狀態檔刻意分開存放（見 nlm_cookie_refresh.py 裡
    REFRESH_ATTEMPT_STATE_PATH 旁的說明），這裡也跟著分開設定。"""
    monkeypatch.setattr(nlm_cookie_refresh, "ALERT_STATE_PATH", tmp_path / "alert_state.json")
    monkeypatch.setattr(
        nlm_cookie_refresh, "REFRESH_ATTEMPT_STATE_PATH", tmp_path / "refresh_attempt_state.json"
    )


def test_success_path_does_not_notify_admin(monkeypatch, tmp_path):
    """只有失敗才告警——正常運作時完全靜默，不發「一切正常」的訊息。"""
    _isolate_alert_state(monkeypatch, tmp_path)
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


def test_no_bound_channels_does_not_notify_admin(monkeypatch, tmp_path):
    _isolate_alert_state(monkeypatch, tmp_path)
    alerts = _stub_alerts(monkeypatch)

    monkeypatch.setattr(nlm_cookie_refresh, "any_channel_expired", lambda: True)
    monkeypatch.setattr(nlm_cookie_refresh, "refresh_cookies", lambda: True)
    monkeypatch.setattr(nlm_cookie_refresh, "bound_channel_ids", lambda: [])

    nlm_cookie_refresh.main()

    # 沒有任何 channel 可以續期，沒有實際結果好回報，不該發通知。
    assert alerts == []


def test_cookie_read_failure_still_notifies_with_existing_message(monkeypatch, tmp_path):
    """既有的失敗路徑（cookie 讀取失敗）維持原本的通知內容不變。"""
    _isolate_alert_state(monkeypatch, tmp_path)
    alerts = _stub_alerts(monkeypatch)

    monkeypatch.setattr(nlm_cookie_refresh, "any_channel_expired", lambda: True)
    monkeypatch.setattr(nlm_cookie_refresh, "refresh_cookies", lambda: False)

    with pytest.raises(SystemExit):
        nlm_cookie_refresh.main()

    assert len(alerts) == 1
    assert "無法從本機瀏覽器讀取" in alerts[0]


def test_storage_state_missing_still_notifies_with_existing_message(monkeypatch, tmp_path):
    """既有的失敗路徑（找不到 storage_state.json）維持原本的通知內容不變。"""
    _isolate_alert_state(monkeypatch, tmp_path)
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
    _isolate_alert_state(monkeypatch, tmp_path)
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
    _isolate_alert_state(monkeypatch, tmp_path)
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
    _isolate_alert_state(monkeypatch, tmp_path)
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
    monkeypatch.setattr(
        nlm_cookie_refresh, "REFRESH_ATTEMPT_STATE_PATH", tmp_path / "refresh_attempt_state.json"
    )
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


def test_repeated_runs_within_cooldown_only_attempt_refresh_once(monkeypatch, tmp_path):
    """回歸測試：偵測閘門（any_channel_expired）本身可以排得很密（每
    分鐘），但如果同一個 channel 持續卡在 expired 沒有自己好，不該
    每次排程都真的去對 Google 打一次 refresh_cookies/rebind——那樣
    幾十分鐘內就會對 Google 連續嘗試幾十次，容易被判定為可疑的自動化
    行為，反而提高整個 session 被判 TrueExpiry（需要人工重新登入）的
    機率。同一輪冷卻視窗內，第二次呼叫 main() 應該直接跳過，不再
    呼叫 refresh_cookies。"""
    _isolate_alert_state(monkeypatch, tmp_path)
    refresh_calls = []

    monkeypatch.setattr(nlm_cookie_refresh, "any_channel_expired", lambda: True)
    monkeypatch.setattr(
        nlm_cookie_refresh, "refresh_cookies", lambda: refresh_calls.append(1) or False
    )
    monkeypatch.setattr(nlm_cookie_refresh, "send_admin_alert_with_cooldown", lambda *a, **k: None)

    with pytest.raises(SystemExit):
        nlm_cookie_refresh.main()
    # 第二次呼叫被冷卻擋下，直接安靜返回，不會再走到失敗路徑的
    # sys.exit(1)。
    nlm_cookie_refresh.main()

    # 兩次呼叫都偵測到 expired，但只有第一次真的去嘗試刷新。
    assert refresh_calls == [1]


def test_successful_run_does_not_clear_the_refresh_attempt_cooldown(monkeypatch, tmp_path):
    """回歸測試：重現真實發生過的事故——一次成功的刷新，結尾會呼叫
    _clear_alert_state() 把 ALERT_STATE_PATH 整個刪掉，讓下次故障能
    立刻告警；但如果修復嘗試的冷卻時間戳記也存在同一份檔案裡，就會被
    這個「成功後清狀態」的動作一起清掉。這支腳本現在排得很密（每
    分鐘一次），一旦冷卻被清掉，下一輪如果這個 channel 又被標成
    expired（例如剛好在同一款車型、同一個帳號的 flapping session），
    就會立刻又打一次 Google，完全沒有節流效果——這正是 2026-08-19
    那次事故的根本原因：同一個 channel 被連續 10 幾分鐘、每分鐘重新
    綁定一次。修復嘗試的冷卻必須跟告警狀態存在不同檔案，不能被
    「這次剛好成功」影響。"""
    _isolate_alert_state(monkeypatch, tmp_path)

    monkeypatch.setattr(nlm_cookie_refresh, "any_channel_expired", lambda: True)
    monkeypatch.setattr(nlm_cookie_refresh, "refresh_cookies", lambda: True)
    monkeypatch.setattr(nlm_cookie_refresh, "bound_channel_ids", lambda: ["chan-1"])
    monkeypatch.setattr(nlm_cookie_refresh, "_find_local_storage_state", lambda: _write_storage_state(tmp_path))
    monkeypatch.setattr(nlm_cookie_refresh, "rebind_channel", lambda cid, storage_state: None)

    nlm_cookie_refresh.main()

    # main() 成功收尾，ALERT_STATE_PATH 照舊會被清掉……
    assert nlm_cookie_refresh._load_alert_state() == {}
    # ……但修復嘗試的冷卻紀錄必須還在，下一輪 1 分鐘後的排程如果又
    # 偵測到 expired，也不該立刻又打一次 Google。
    assert nlm_cookie_refresh._refresh_attempt_allowed() is False


def test_refresh_attempt_allowed_again_after_cooldown_elapses(monkeypatch, tmp_path):
    """冷卻視窗過了之後，還是要繼續嘗試修復——不能因為一次嘗試沒有
    立刻成功，就永久放棄後續的自動修復。"""
    _isolate_alert_state(monkeypatch, tmp_path)
    monkeypatch.setattr(nlm_cookie_refresh, "_REFRESH_ATTEMPT_COOLDOWN_SECONDS", 0)

    refresh_calls = []
    monkeypatch.setattr(nlm_cookie_refresh, "any_channel_expired", lambda: True)
    monkeypatch.setattr(
        nlm_cookie_refresh, "refresh_cookies", lambda: refresh_calls.append(1) or False
    )
    monkeypatch.setattr(nlm_cookie_refresh, "send_admin_alert_with_cooldown", lambda *a, **k: None)

    with pytest.raises(SystemExit):
        nlm_cookie_refresh.main()
    with pytest.raises(SystemExit):
        nlm_cookie_refresh.main()

    # 冷卻時間設為 0，代表沒有節流空間——兩次呼叫都該真的嘗試。
    assert refresh_calls == [1, 1]


def test_attempt_recorded_even_when_refresh_itself_fails(monkeypatch, tmp_path):
    """時間戳記要在『動手嘗試』的當下就記錄，而不是等結果出爐——即使
    這次嘗試本身失敗，下一次嘗試依然要等滿一個冷卻週期，不能因為
    『失敗了所以不算數』又立刻重打一次 Google。"""
    _isolate_alert_state(monkeypatch, tmp_path)
    monkeypatch.setattr(nlm_cookie_refresh, "any_channel_expired", lambda: True)
    monkeypatch.setattr(nlm_cookie_refresh, "refresh_cookies", lambda: False)
    monkeypatch.setattr(nlm_cookie_refresh, "send_admin_alert_with_cooldown", lambda *a, **k: None)

    with pytest.raises(SystemExit):
        nlm_cookie_refresh.main()

    assert nlm_cookie_refresh._refresh_attempt_allowed() is False

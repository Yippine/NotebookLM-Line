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


def test_success_path_does_not_notify_admin(monkeypatch, tmp_path):
    """只有失敗才告警——正常運作時完全靜默，不發「一切正常」的訊息。"""
    alerts = []

    monkeypatch.setattr(nlm_cookie_refresh, "refresh_cookies", lambda: True)
    monkeypatch.setattr(nlm_cookie_refresh, "bound_channel_ids", lambda: ["chan-1", "chan-2", "chan-3"])
    monkeypatch.setattr(nlm_cookie_refresh, "_find_local_storage_state", lambda: _write_storage_state(tmp_path))
    monkeypatch.setattr(nlm_cookie_refresh, "rebind_channel", lambda cid, storage_state: None)
    monkeypatch.setattr(nlm_cookie_refresh, "send_admin_alert", lambda msg: alerts.append(msg))

    nlm_cookie_refresh.main()

    assert alerts == []


def test_no_bound_channels_does_not_notify_admin(monkeypatch):
    alerts = []

    monkeypatch.setattr(nlm_cookie_refresh, "refresh_cookies", lambda: True)
    monkeypatch.setattr(nlm_cookie_refresh, "bound_channel_ids", lambda: [])
    monkeypatch.setattr(nlm_cookie_refresh, "send_admin_alert", lambda msg: alerts.append(msg))

    nlm_cookie_refresh.main()

    # 沒有任何 channel 可以續期，沒有實際結果好回報，不該發通知。
    assert alerts == []


def test_cookie_read_failure_still_notifies_with_existing_message(monkeypatch):
    """既有的失敗路徑（cookie 讀取失敗）維持原本的通知內容不變。"""
    alerts = []

    monkeypatch.setattr(nlm_cookie_refresh, "refresh_cookies", lambda: False)
    monkeypatch.setattr(nlm_cookie_refresh, "send_admin_alert", lambda msg: alerts.append(msg))

    with pytest.raises(SystemExit):
        nlm_cookie_refresh.main()

    assert len(alerts) == 1
    assert "無法從本機瀏覽器讀取" in alerts[0]


def test_storage_state_missing_still_notifies_with_existing_message(monkeypatch):
    """既有的失敗路徑（找不到 storage_state.json）維持原本的通知內容不變。"""
    alerts = []

    monkeypatch.setattr(nlm_cookie_refresh, "refresh_cookies", lambda: True)
    monkeypatch.setattr(nlm_cookie_refresh, "bound_channel_ids", lambda: ["chan-1"])
    monkeypatch.setattr(nlm_cookie_refresh, "_find_local_storage_state", lambda: None)
    monkeypatch.setattr(nlm_cookie_refresh, "send_admin_alert", lambda msg: alerts.append(msg))

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

    monkeypatch.setattr(nlm_cookie_refresh, "refresh_cookies", lambda: True)
    monkeypatch.setattr(nlm_cookie_refresh, "bound_channel_ids", lambda: ["chan-1"])
    monkeypatch.setattr(nlm_cookie_refresh, "_find_local_storage_state", lambda: storage_path)
    monkeypatch.setattr(nlm_cookie_refresh, "rebind_channel", lambda cid, storage_state: None)
    monkeypatch.setattr(nlm_cookie_refresh, "send_admin_alert", lambda msg: None)

    nlm_cookie_refresh.main()

    # 內容已經上傳給後端，本機這份含有 cookie 的檔案不該再留著。
    assert not os.path.exists(storage_path)


def test_read_failure_still_shreds_the_file(monkeypatch, tmp_path):
    """就算讀取／解析失敗，磁碟上那份檔案還是曾經含有 cookie 內容，
    一樣要清掉，不能因為讀取失敗就放著不管。"""
    path = tmp_path / "storage_state.json"
    path.write_text("not valid json", encoding="utf-8")

    monkeypatch.setattr(nlm_cookie_refresh, "refresh_cookies", lambda: True)
    monkeypatch.setattr(nlm_cookie_refresh, "bound_channel_ids", lambda: ["chan-1"])
    monkeypatch.setattr(nlm_cookie_refresh, "_find_local_storage_state", lambda: str(path))
    monkeypatch.setattr(nlm_cookie_refresh, "send_admin_alert", lambda msg: None)

    with pytest.raises(SystemExit):
        nlm_cookie_refresh.main()

    assert not path.exists()


def test_partial_rebind_failure_still_notifies_with_existing_message(monkeypatch, tmp_path):
    """既有的失敗路徑（部分 channel rebind 失敗）維持原本的通知內容不變，
    且不會被誤認成功、也不會額外多發一則成功通知。"""
    alerts = []

    monkeypatch.setattr(nlm_cookie_refresh, "refresh_cookies", lambda: True)
    monkeypatch.setattr(nlm_cookie_refresh, "bound_channel_ids", lambda: ["chan-good", "chan-bad"])
    monkeypatch.setattr(nlm_cookie_refresh, "_find_local_storage_state", lambda: _write_storage_state(tmp_path))

    def fake_rebind(cid, storage_state):
        return None if cid == "chan-good" else f"{cid}: boom"

    monkeypatch.setattr(nlm_cookie_refresh, "rebind_channel", fake_rebind)
    monkeypatch.setattr(nlm_cookie_refresh, "send_admin_alert", lambda msg: alerts.append(msg))

    with pytest.raises(SystemExit):
        nlm_cookie_refresh.main()

    assert len(alerts) == 1
    assert "chan-bad: boom" in alerts[0]
    assert "回寫失敗" in alerts[0]

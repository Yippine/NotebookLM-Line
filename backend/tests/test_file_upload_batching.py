import asyncio

import routers.webhook as webhook


def _run(coro):
    return asyncio.run(coro)


def _file_event(reply_token: str, file_id: str, file_name: str, source: dict | None = None) -> dict:
    return {
        "type": "message",
        "replyToken": reply_token,
        "source": source or {"type": "user", "userId": "user-1"},
        "message": {"type": "file", "id": file_id, "fileName": file_name},
    }


def test_summarize_upload_results_lists_all_vendor_names_when_five_or_fewer():
    results = [
        "✅ 知識庫已更新。已新增檔案：Autostar杰運汽車_20260805.md",
        "✅ 知識庫已更新。已新增檔案：伊利安_20260720.md",
        "✅ 知識庫已更新。已覆蓋同名舊檔案並上傳：捷勝汽車_20260805.md",
    ]

    summary = webhook._summarize_upload_results(results)

    assert summary == "✅ 知識庫已更新：Autostar杰運汽車、伊利安、捷勝汽車"


def test_summarize_upload_results_truncates_past_five_vendors():
    results = [
        f"✅ 知識庫已更新。已新增檔案：廠商{i}_20260805.md" for i in range(1, 8)
    ]

    summary = webhook._summarize_upload_results(results)

    assert summary == "✅ 知識庫已更新：廠商1、廠商2、廠商3、廠商4、廠商5...等7份"


def test_summarize_upload_results_keeps_failure_detail_alongside_successes():
    results = [
        "✅ 知識庫已更新。已新增檔案：廠商A_20260805.md",
        "⚠️ 更新知識庫失敗：檔案格式不支援",
    ]

    summary = webhook._summarize_upload_results(results)

    assert "✅ 知識庫已更新：廠商A" in summary
    assert "⚠️ 更新知識庫失敗：檔案格式不支援" in summary


def test_summarize_upload_results_all_failed_has_no_success_line():
    results = ["⚠️ 更新知識庫失敗：連線逾時"]

    summary = webhook._summarize_upload_results(results)

    assert "✅" not in summary
    assert "⚠️ 更新知識庫失敗：連線逾時" in summary


def test_handle_file_batch_shows_working_indicator_only_once(monkeypatch):
    indicator_calls = []
    replied = []

    async def fake_show_loading(target_id, access_token, seconds=30):
        indicator_calls.append(target_id)
        return True

    async def fake_update_knowledge_base(channel_id, file_bytes, file_name):
        return f"✅ 知識庫已更新。已新增檔案：{file_name}"

    async def fake_download_content(file_id, access_token):
        return b"bytes"

    async def fake_reply_text(reply_token, access_token, text, mention_user_id=None, mention_display_name=None):
        replied.append((reply_token, text))

    monkeypatch.setattr(webhook, "show_loading", fake_show_loading)
    monkeypatch.setattr(webhook, "download_content", fake_download_content)
    monkeypatch.setattr(webhook, "update_knowledge_base", fake_update_knowledge_base)
    monkeypatch.setattr(webhook, "reply_text", fake_reply_text)

    file_events = [
        _file_event("reply-1", "file-1", "廠商A_20260805.md"),
        _file_event("reply-2", "file-2", "廠商B_20260805.md"),
        _file_event("reply-3", "file-3", "廠商C_20260805.md"),
    ]

    _run(webhook._handle_file_batch("channel-1", "token-1", file_events))

    # 三個檔案只顯示一次處理中提示，而不是三次。
    assert indicator_calls == ["user-1"]

    # 只用其中一個 reply_token 回覆一則合併訊息，而不是三則各自的訊息。
    assert len(replied) == 1
    reply_token, text = replied[0]
    assert reply_token == "reply-3"  # 用最後一個 event 的 reply_token
    assert text == "✅ 知識庫已更新：廠商A、廠商B、廠商C"

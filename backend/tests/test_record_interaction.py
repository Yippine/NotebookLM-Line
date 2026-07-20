import asyncio
import time

import routers.webhook as webhook


def _run(coro):
    return asyncio.run(coro)


def test_save_text_record_and_append_sheet_row_run_concurrently(monkeypatch):
    """Both calls only depend on folder_id/folder_link, not on each other —
    they should run concurrently instead of one waiting on the other."""
    calls = []

    async def fake_get_display_name(access_token, user_id, group_id=None, room_id=None):
        return "小明"

    async def fake_get_or_create_user_folder(channel_id, line_user_id, display_name):
        return "folder-1", "https://drive.example/folder-1"

    async def fake_save_text_record(folder_id, question, answer, timestamp):
        calls.append("save_start")
        await asyncio.sleep(0.1)
        calls.append("save_end")

    async def fake_append_sheet_row(date_time, nickname, folder_link, status, note=""):
        calls.append("append_start")
        await asyncio.sleep(0.1)
        calls.append("append_end")

    monkeypatch.setattr(webhook, "get_display_name", fake_get_display_name)
    monkeypatch.setattr(
        webhook.google_log_service, "get_or_create_user_folder", fake_get_or_create_user_folder
    )
    monkeypatch.setattr(webhook.google_log_service, "save_text_record", fake_save_text_record)
    monkeypatch.setattr(webhook.google_log_service, "append_sheet_row", fake_append_sheet_row)

    start = time.perf_counter()
    _run(webhook._record_interaction(
        "channel-1", None, None, "user-1", "token-1", "Q?", "A.", "已完成",
    ))
    elapsed = time.perf_counter() - start

    # Both handlers started before either finished — proof they overlapped
    # rather than running one after the other.
    assert calls.index("append_start") < calls.index("save_end")
    # Sequential would take ~0.2s (two 0.1s sleeps back to back); concurrent
    # stays close to 0.1s. Generous margin to avoid timing flakiness.
    assert elapsed < 0.18


def test_one_call_failing_does_not_orphan_the_other(monkeypatch):
    """With the default asyncio.gather(return_exceptions=False), a failure
    in one awaitable propagates immediately and leaves the other running as
    an untracked task — which defeats the whole point of awaiting this
    function (see its docstring: a task not covered by the request's
    lifecycle can get killed mid-write on shutdown). This must not happen:
    both calls should always run to completion."""
    calls = []

    async def fake_get_display_name(access_token, user_id, group_id=None, room_id=None):
        return "小明"

    async def fake_get_or_create_user_folder(channel_id, line_user_id, display_name):
        return "folder-1", "https://drive.example/folder-1"

    async def fake_save_text_record(folder_id, question, answer, timestamp):
        raise RuntimeError("Drive upload failed")

    async def fake_append_sheet_row(date_time, nickname, folder_link, status, note=""):
        await asyncio.sleep(0.05)  # still in flight when save_text_record raises
        calls.append("append_completed")

    monkeypatch.setattr(webhook, "get_display_name", fake_get_display_name)
    monkeypatch.setattr(
        webhook.google_log_service, "get_or_create_user_folder", fake_get_or_create_user_folder
    )
    monkeypatch.setattr(webhook.google_log_service, "save_text_record", fake_save_text_record)
    monkeypatch.setattr(webhook.google_log_service, "append_sheet_row", fake_append_sheet_row)

    # Must not raise — failures here are logged, never propagated to the
    # webhook handler.
    _run(webhook._record_interaction(
        "channel-1", None, None, "user-1", "token-1", "Q?", "A.", "已完成",
    ))

    assert calls == ["append_completed"]

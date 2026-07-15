import asyncio
from datetime import datetime, timedelta

import aiosqlite
import pytest

import database
import services.google_log_service as google_log_service


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = str(tmp_path / "test.db")
    monkeypatch.setattr(database, "DB", path)
    monkeypatch.setattr(google_log_service, "DB", path)
    _run(database.init_db())
    return path


def test_retention_cutoff_handles_month_rollover(monkeypatch):
    fixed_now = datetime(2026, 2, 15, 10, 0, 0, tzinfo=google_log_service._BEIJING_TZ)

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now

    monkeypatch.setattr(google_log_service, "datetime", _FixedDatetime)

    cutoff = google_log_service._retention_cutoff(4)

    assert cutoff.year == 2025
    assert cutoff.month == 10
    assert cutoff.tzinfo is None  # naive, matches how sheet timestamps are stored


def test_delete_old_sheet_rows_only_deletes_contiguous_old_block(monkeypatch):
    """Rows are appended chronologically, so old rows always form one block
    right after the header — this should issue exactly one row-range
    delete rather than deleting/skipping individual rows."""
    sheet_values = [
        "2025/01/01 10:00:00",  # old
        "2025/02/01 10:00:00",  # old
        "2026/06/01 10:00:00",  # recent
        "2026/06/15 10:00:00",  # recent
    ]
    deleted_ranges = []

    monkeypatch.setattr(google_log_service, "_read_sheet_time_column", lambda: sheet_values)
    monkeypatch.setattr(google_log_service, "_get_sheet_id", lambda: 42)
    monkeypatch.setattr(
        google_log_service, "_delete_sheet_row_range",
        lambda sheet_id, start, end: deleted_ranges.append((sheet_id, start, end)),
    )

    cutoff = datetime(2026, 1, 1)
    deleted = _run(google_log_service.delete_old_sheet_rows(cutoff))

    assert deleted == 2
    assert deleted_ranges == [(42, 1, 3)]  # rows 2-3 (0-indexed 1..3 exclusive)


def test_delete_old_sheet_rows_stops_at_first_recent_row_even_if_later_rows_are_old():
    """Defensive: only ever deletes the leading contiguous old block, never
    skips over a "recent" row to delete an out-of-order old one later —
    avoids ever deleting the wrong row range."""
    sheet_values = [
        "2025/01/01 10:00:00",  # old
        "2026/06/01 10:00:00",  # recent — stops here
        "2025/02/01 10:00:00",  # old but unreachable, out of order
    ]
    deleted_ranges = []

    import services.google_log_service as gls
    orig_read = gls._read_sheet_time_column
    orig_get_id = gls._get_sheet_id
    orig_delete = gls._delete_sheet_row_range
    try:
        gls._read_sheet_time_column = lambda: sheet_values
        gls._get_sheet_id = lambda: 1
        gls._delete_sheet_row_range = lambda sheet_id, start, end: deleted_ranges.append((start, end))

        cutoff = datetime(2026, 1, 1)
        deleted = _run(gls.delete_old_sheet_rows(cutoff))
    finally:
        gls._read_sheet_time_column = orig_read
        gls._get_sheet_id = orig_get_id
        gls._delete_sheet_row_range = orig_delete

    assert deleted == 1
    assert deleted_ranges == [(1, 2)]


def test_delete_old_sheet_rows_noop_when_nothing_old(monkeypatch):
    monkeypatch.setattr(google_log_service, "_read_sheet_time_column", lambda: ["2026/06/01 10:00:00"])
    monkeypatch.setattr(google_log_service, "_get_sheet_id", lambda: (_ for _ in ()).throw(AssertionError("should not be called")))

    deleted = _run(google_log_service.delete_old_sheet_rows(datetime(2026, 1, 1)))

    assert deleted == 0


def test_delete_old_drive_files_only_touches_known_folders(db_path, monkeypatch):
    async def _seed():
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                "INSERT INTO user_conversations (channel_id, line_user_id, drive_folder_id) VALUES (?, ?, ?)",
                ("ch1", "user1", "folder-a"),
            )
            await db.execute(
                "INSERT INTO user_conversations (channel_id, line_user_id, drive_folder_id) VALUES (?, ?, ?)",
                ("ch1", "user2", "folder-b"),
            )
            await db.commit()
    _run(_seed())

    listed_for = []
    deleted_files = []

    def fake_list(folder_id, cutoff_iso):
        listed_for.append(folder_id)
        return {"folder-a": ["file-1", "file-2"], "folder-b": []}[folder_id]

    monkeypatch.setattr(google_log_service, "_list_old_files_in_folder", fake_list)
    monkeypatch.setattr(google_log_service, "_delete_drive_file", lambda file_id: deleted_files.append(file_id))

    deleted_count = _run(google_log_service.delete_old_drive_files(datetime(2026, 1, 1)))

    assert set(listed_for) == {"folder-a", "folder-b"}
    assert deleted_files == ["file-1", "file-2"]
    assert deleted_count == 2


def test_delete_old_drive_files_continues_after_one_folder_errors(db_path, monkeypatch):
    async def _seed():
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                "INSERT INTO user_conversations (channel_id, line_user_id, drive_folder_id) VALUES (?, ?, ?)",
                ("ch1", "user1", "folder-bad"),
            )
            await db.execute(
                "INSERT INTO user_conversations (channel_id, line_user_id, drive_folder_id) VALUES (?, ?, ?)",
                ("ch1", "user2", "folder-ok"),
            )
            await db.commit()
    _run(_seed())

    def fake_list(folder_id, cutoff_iso):
        if folder_id == "folder-bad":
            raise RuntimeError("Drive API error")
        return ["file-ok"]

    deleted_files = []
    monkeypatch.setattr(google_log_service, "_list_old_files_in_folder", fake_list)
    monkeypatch.setattr(google_log_service, "_delete_drive_file", lambda file_id: deleted_files.append(file_id))

    deleted_count = _run(google_log_service.delete_old_drive_files(datetime(2026, 1, 1)))

    assert deleted_files == ["file-ok"]
    assert deleted_count == 1

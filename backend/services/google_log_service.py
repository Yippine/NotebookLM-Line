import asyncio
import io
import logging
from datetime import datetime, timedelta, timezone

import aiosqlite
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

from config import settings
from database import DB

_SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]

_drive = None
_sheets = None

logger = logging.getLogger(__name__)

RETENTION_MONTHS = 4
_BEIJING_TZ = timezone(timedelta(hours=8))
_SHEET_TIME_FORMAT = "%Y/%m/%d %H:%M:%S"


def _load_credentials() -> Credentials:
    """Load the OAuth user token (see scripts/google_oauth_setup.py).

    Uses the authorizing user's own Drive/Sheets identity rather than a
    service account — service accounts have no personal storage quota and
    can't upload file content into a regular (non-Shared-Drive) folder.
    """
    creds = Credentials.from_authorized_user_file(settings.google_oauth_token_path, _SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(settings.google_oauth_token_path, "w", encoding="utf-8") as f:
            f.write(creds.to_json())
    return creds


def _clients():
    """Lazily build the Drive/Sheets clients (blocking; only called via to_thread)."""
    global _drive, _sheets
    if _drive is None:
        credentials = _load_credentials()
        _drive = build("drive", "v3", credentials=credentials)
        _sheets = build("sheets", "v4", credentials=credentials)
    return _drive, _sheets


def _create_drive_folder(folder_name: str) -> tuple[str, str]:
    drive, _ = _clients()
    metadata = {
        "name": folder_name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [settings.google_drive_parent_folder_id],
    }
    folder = drive.files().create(body=metadata, fields="id, webViewLink").execute()
    return folder["id"], folder["webViewLink"]


def _upload_text_record(folder_id: str, file_name: str, content: str) -> str:
    drive, _ = _clients()
    media = MediaIoBaseUpload(io.BytesIO(content.encode("utf-8")), mimetype="text/plain")
    metadata = {"name": file_name, "parents": [folder_id]}
    file = drive.files().create(body=metadata, media_body=media, fields="id, webViewLink").execute()
    return file["webViewLink"]


def _append_sheet_row(row: list[str]) -> None:
    _, sheets = _clients()
    sheets.spreadsheets().values().append(
        spreadsheetId=settings.google_sheet_id,
        range="A:E",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": [row]},
    ).execute()


async def get_or_create_user_folder(channel_id: str, line_user_id: str, display_name: str) -> tuple[str, str]:
    """Return (folder_id, folder_link) for this user, creating the Drive
    folder on first contact and caching it for reuse afterward."""
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT drive_folder_id, drive_folder_link FROM user_conversations "
            "WHERE channel_id=? AND line_user_id=?",
            (channel_id, line_user_id),
        )
        row = await cur.fetchone()

    if row and row["drive_folder_id"]:
        return row["drive_folder_id"], row["drive_folder_link"]

    folder_name = f"{display_name}_{line_user_id}"
    folder_id, folder_link = await asyncio.to_thread(_create_drive_folder, folder_name)

    async with aiosqlite.connect(DB) as db:
        await db.execute(
            """
            INSERT INTO user_conversations (channel_id, line_user_id, drive_folder_id, drive_folder_link, updated_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(channel_id, line_user_id)
            DO UPDATE SET drive_folder_id=excluded.drive_folder_id,
                          drive_folder_link=excluded.drive_folder_link,
                          updated_at=CURRENT_TIMESTAMP
            """,
            (channel_id, line_user_id, folder_id, folder_link),
        )
        await db.commit()

    return folder_id, folder_link


async def save_text_record(folder_id: str, question: str, answer: str, timestamp: datetime) -> str:
    """Save a Q&A text record into the user's folder. Returns the file's web link."""
    content = (
        f"時間：{timestamp.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"問題：{question}\n"
        f"回答：{answer}\n"
    )
    file_name = f"{timestamp.strftime('%Y%m%d_%H%M%S')}.txt"
    return await asyncio.to_thread(_upload_text_record, folder_id, file_name, content)


async def append_sheet_row(
    date_time: datetime, nickname: str, folder_link: str, status: str, note: str = ""
) -> None:
    row = [date_time.strftime("%Y-%m-%d %H:%M:%S"), nickname, folder_link, status, note]
    await asyncio.to_thread(_append_sheet_row, row)


def _retention_cutoff(months: int) -> datetime:
    """Naive Beijing-local cutoff ``months`` months before now — naive
    because that's how timestamps are stored (see ``append_sheet_row``'s
    strftime, which drops tzinfo), so comparisons stay apples-to-apples."""
    now = datetime.now(_BEIJING_TZ)
    total = now.year * 12 + (now.month - 1) - months
    year, month0 = divmod(total, 12)
    return now.replace(year=year, month=month0 + 1, day=min(now.day, 28), tzinfo=None)


def _get_sheet_id() -> int:
    _, sheets = _clients()
    meta = sheets.spreadsheets().get(
        spreadsheetId=settings.google_sheet_id, fields="sheets.properties"
    ).execute()
    return meta["sheets"][0]["properties"]["sheetId"]


def _read_sheet_time_column() -> list[str]:
    _, sheets = _clients()
    result = sheets.spreadsheets().values().get(
        spreadsheetId=settings.google_sheet_id,
        range="A2:A",
        valueRenderOption="FORMATTED_VALUE",
    ).execute()
    return [row[0] if row else "" for row in result.get("values", [])]


def _delete_sheet_row_range(sheet_id: int, start_index: int, end_index: int) -> None:
    """0-indexed, end_index exclusive (Sheets API deleteDimension semantics).
    Deleting the row dimension (not just clearing cell contents) is what
    makes the remaining rows shift up automatically."""
    _, sheets = _clients()
    body = {
        "requests": [{
            "deleteDimension": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "ROWS",
                    "startIndex": start_index,
                    "endIndex": end_index,
                }
            }
        }]
    }
    sheets.spreadsheets().batchUpdate(spreadsheetId=settings.google_sheet_id, body=body).execute()


async def delete_old_sheet_rows(cutoff: datetime) -> int:
    """Delete tracking-sheet rows older than ``cutoff`` (the "data table").

    Rows are always appended in chronological order, so the old ones form
    one contiguous block right after the header — deleted as a single row
    range so the sheet compacts upward on its own, no manual re-indexing.
    Returns the number of rows deleted.
    """
    time_values = await asyncio.to_thread(_read_sheet_time_column)

    old_count = 0
    for raw in time_values:
        if not raw:
            break
        try:
            row_time = datetime.strptime(raw, _SHEET_TIME_FORMAT)
        except ValueError:
            logger.warning(f"Unparseable time value in tracking sheet, stopping cleanup scan: {raw!r}")
            break
        if row_time >= cutoff:
            break
        old_count += 1

    if old_count == 0:
        return 0

    sheet_id = await asyncio.to_thread(_get_sheet_id)
    # Row 1 is the header; data starts at row 2 (0-indexed row 1).
    await asyncio.to_thread(_delete_sheet_row_range, sheet_id, 1, 1 + old_count)
    return old_count


def _list_old_files_in_folder(folder_id: str, cutoff_iso: str) -> list[str]:
    drive, _ = _clients()
    file_ids: list[str] = []
    page_token = None
    query = f"'{folder_id}' in parents and trashed=false and createdTime < '{cutoff_iso}'"
    while True:
        resp = drive.files().list(
            q=query, fields="nextPageToken, files(id)", pageToken=page_token
        ).execute()
        file_ids.extend(f["id"] for f in resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return file_ids


def _delete_drive_file(file_id: str) -> None:
    drive, _ = _clients()
    drive.files().delete(fileId=file_id).execute()


async def delete_old_drive_files(cutoff: datetime) -> int:
    """Delete every Q&A record file older than ``cutoff`` (the "conversation
    process") across all known per-user Drive folders. Returns the number
    of files deleted."""
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute(
            "SELECT DISTINCT drive_folder_id FROM user_conversations WHERE drive_folder_id IS NOT NULL"
        )
        folder_ids = [r[0] for r in await cur.fetchall()]

    cutoff_iso = cutoff.replace(tzinfo=_BEIJING_TZ).isoformat()
    deleted = 0
    for folder_id in folder_ids:
        try:
            file_ids = await asyncio.to_thread(_list_old_files_in_folder, folder_id, cutoff_iso)
            for file_id in file_ids:
                await asyncio.to_thread(_delete_drive_file, file_id)
                deleted += 1
        except Exception as e:
            logger.error(f"Failed to clean up Drive folder {folder_id}: {e}")
    return deleted


async def cleanup_old_conversation_records(months: int = RETENTION_MONTHS) -> dict:
    """Delete conversation records older than ``months`` months — both the
    tracking-sheet rows (the data table) and the underlying per-user Drive
    Q&A record files (the conversation content). Best-effort: called from a
    background scheduler, failures are logged rather than raised."""
    cutoff = _retention_cutoff(months)
    deleted_rows = await delete_old_sheet_rows(cutoff)
    deleted_files = await delete_old_drive_files(cutoff)
    return {
        "deleted_rows": deleted_rows,
        "deleted_files": deleted_files,
        "cutoff": cutoff.isoformat(),
    }

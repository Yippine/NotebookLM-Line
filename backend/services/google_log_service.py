import asyncio
import io
from datetime import datetime

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

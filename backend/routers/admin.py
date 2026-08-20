from __future__ import annotations

import csv
import io
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone

import aiosqlite
from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Literal

from config import settings
from database import get_db_path
from models import (
    AdminLoginRequest,
    ChannelCreate,
    ChannelOut,
    InviteVerify,
    SessionTokenOut,
)
from services.channel_credentials import encrypt_channel_credentials
from services.logging_service import log_security_event
from services.session_service import (
    AdminPrincipal,
    SetupPrincipal,
    assert_channel_scope,
    issue_admin_session,
    issue_setup_session,
    require_admin_session,
    require_setup_session,
    revoke_admin_session,
    revoke_setup_session,
    scope_setup_session_to_channel,
)

router = APIRouter(tags=["admin"])
logger = logging.getLogger(__name__)


def _parse_expiry(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _is_https(request: Request) -> bool:
    if request.url.scheme == "https":
        return True
    return settings.trust_proxy_headers and (
        request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip().lower()
        == "https"
    )


# --- Authentication APIs ---


@router.post("/admin/login", response_model=SessionTokenOut)
async def admin_login(body: AdminLoginRequest, request: Request) -> SessionTokenOut:
    if settings.is_production and not _is_https(request):
        raise HTTPException(400, "管理員登入只接受 HTTPS")

    valid = secrets.compare_digest(
        body.password.encode("utf-8"), settings.admin_password.encode("utf-8")
    )
    if not valid:
        log_security_event(logger, action="admin_login", outcome="denied")
        raise HTTPException(401, "帳號或密碼錯誤")

    issued = await issue_admin_session()
    log_security_event(logger, action="admin_login", outcome="success")
    return SessionTokenOut(token=issued.token, expires_at=issued.expires_at)


@router.post("/admin/logout")
async def admin_logout(
    principal: AdminPrincipal = Depends(require_admin_session),
) -> dict[str, str]:
    await revoke_admin_session(principal.session_id)
    return {"status": "logged_out"}


# --- Student-facing APIs ---


@router.post("/verify-invite", response_model=SessionTokenOut)
async def verify_invite(body: InviteVerify) -> SessionTokenOut:
    async with aiosqlite.connect(get_db_path()) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT code, channel_id, expires_at FROM invite_codes WHERE code=?",
            (body.code,),
        )
        row = await cursor.fetchone()
        if not row:
            log_security_event(logger, action="verify_invite", outcome="denied")
            raise HTTPException(400, "邀請碼無效")
        if row["expires_at"] and _parse_expiry(row["expires_at"]) <= datetime.now(
            timezone.utc
        ):
            log_security_event(logger, action="verify_invite", outcome="expired")
            raise HTTPException(400, "邀請碼已過期，請聯繫講師")

        if row["channel_id"]:
            cursor = await db.execute(
                "SELECT expires_at FROM channels WHERE channel_id=?",
                (row["channel_id"],),
            )
            channel = await cursor.fetchone()
            if (
                channel
                and channel["expires_at"]
                and channel["expires_at"] < datetime.now(timezone.utc).isoformat()
            ):
                raise HTTPException(400, "此帳號已過期，請聯繫講師")

        await db.execute("UPDATE invite_codes SET used=1 WHERE code=?", (body.code,))
        await db.commit()

    issued = await issue_setup_session(body.code, row["channel_id"])
    log_security_event(
        logger,
        action="verify_invite",
        outcome="success",
        resource_id=row["channel_id"],
    )
    return SessionTokenOut(
        token=issued.token,
        expires_at=issued.expires_at,
        channel_id=row["channel_id"],
    )


@router.post("/setup/logout")
async def setup_logout(
    principal: SetupPrincipal = Depends(require_setup_session),
) -> dict[str, str]:
    await revoke_setup_session(principal.session_id)
    return {"status": "logged_out"}


@router.post("/channels")
async def create_channel(
    body: ChannelCreate,
    principal: SetupPrincipal = Depends(require_setup_session),
) -> dict[str, str]:
    if principal.channel_id is not None:
        assert_channel_scope(principal, body.channel_id)
    else:
        # Prevent an unscoped invitation from claiming a Channel already owned
        # by a different invite before the scope is persisted.
        async with aiosqlite.connect(get_db_path()) as db:
            cursor = await db.execute(
                "SELECT code FROM invite_codes WHERE channel_id=? AND code<>? LIMIT 1",
                (body.channel_id, principal.invite_code),
            )
            if await cursor.fetchone():
                raise HTTPException(403, "無權操作此 Channel")
        await scope_setup_session_to_channel(principal.session_id, body.channel_id)

    secret_encrypted, token_encrypted = encrypt_channel_credentials(
        body.channel_secret, body.channel_access_token
    )
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(get_db_path()) as db:
        cursor = await db.execute(
            "SELECT channel_id FROM channels WHERE channel_id=?", (body.channel_id,)
        )
        if await cursor.fetchone():
            await db.execute(
                """
                UPDATE channels
                SET channel_secret='', channel_access_token='',
                    channel_secret_encrypted=?, channel_access_token_encrypted=?,
                    updated_at=?
                WHERE channel_id=?
                """,
                (secret_encrypted, token_encrypted, now, body.channel_id),
            )
        else:
            # Empty legacy values support databases created by the previous
            # NOT NULL schema without retaining plaintext secrets.
            await db.execute(
                """
                INSERT INTO channels
                    (channel_id, channel_secret, channel_access_token,
                     channel_secret_encrypted, channel_access_token_encrypted,
                     binding_status, expires_at, updated_at)
                VALUES (?, '', '', ?, ?, 'unbound', ?, ?)
                """,
                (
                    body.channel_id,
                    secret_encrypted,
                    token_encrypted,
                    None,
                    now,
                ),
            )
        await db.commit()

    log_security_event(
        logger,
        action="channel_update",
        outcome="success",
        resource_id=body.channel_id,
    )
    return {
        "channel_id": body.channel_id,
        "webhook_url": f"{settings.webhook_base_url}/webhook/{body.channel_id}",
    }


@router.get("/channels/{channel_id}", response_model=ChannelOut)
async def get_channel(
    channel_id: str,
    principal: SetupPrincipal = Depends(require_setup_session),
) -> ChannelOut:
    assert_channel_scope(principal, channel_id)
    async with aiosqlite.connect(get_db_path()) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT channel_id, notebook_id, notebook_display_name,
                   binding_status, last_access_checked_at
            FROM channels WHERE channel_id=?
            """,
            (channel_id,),
        )
        row = await cursor.fetchone()
    if not row:
        raise HTTPException(404, "Channel 不存在")

    status = row["binding_status"] or ("bound" if row["notebook_id"] else "unbound")
    return ChannelOut(
        channel_id=row["channel_id"],
        notebook_id=row["notebook_id"],
        notebook_display_name=row["notebook_display_name"],
        binding_status=status,
        last_access_checked_at=row["last_access_checked_at"],
        # A legacy per-student cookie is not proof that the centralized course
        # account can access the Notebook; migration must pass URL validation.
        nlm_bound=status == "bound",
        webhook_url=f"{settings.webhook_base_url}/webhook/{row['channel_id']}",
    )


# --- Admin APIs ---


@router.post("/invite-codes/generate")
async def generate_invite_codes(
    count: int = 5,
    _principal: AdminPrincipal = Depends(require_admin_session),
) -> dict[str, list[str]]:
    if count < 1 or count > 500:
        raise HTTPException(400, "數量必須介於 1 到 500")
    codes = [secrets.token_urlsafe(8) for _ in range(count)]
    expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=settings.invite_code_ttl_seconds)
    ).isoformat()
    async with aiosqlite.connect(get_db_path()) as db:
        await db.executemany(
            "INSERT INTO invite_codes (code, expires_at) VALUES (?, ?)",
            [(code, expires_at) for code in codes],
        )
        await db.commit()
    return {"codes": codes}


@router.post("/admin/import-csv")
async def import_csv(
    file: UploadFile = File(...),
    _principal: AdminPrincipal = Depends(require_admin_session),
) -> dict:
    content = (await file.read()).decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(content))
    if not reader.fieldnames:
        raise HTTPException(400, "CSV 格式錯誤：無法讀取欄位")

    name_col = next(
        (
            col
            for col in reader.fieldnames
            if col.strip().lower() in ("name", "姓名", "學員姓名", "student_name")
        ),
        None,
    )
    if not name_col:
        names = ", ".join(reader.fieldnames)
        raise HTTPException(
            400,
            f"CSV 缺少姓名欄位（支援：name, 姓名, 學員姓名）。找到的欄位：{names}",
        )

    results: list[dict[str, str]] = []
    expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=settings.invite_code_ttl_seconds)
    ).isoformat()
    async with aiosqlite.connect(get_db_path()) as db:
        for row in reader:
            raw = row[name_col].strip()
            if not raw:
                continue
            for item in re.split(r"[、,，/]", raw):
                name = re.sub(r"[（(][^）)]*[）)]", "", item).strip()
                if not name:
                    continue
                code = secrets.token_urlsafe(8)
                await db.execute(
                    """
                    INSERT INTO invite_codes (code, student_name, expires_at)
                    VALUES (?, ?, ?)
                    """,
                    (code, name, expires_at),
                )
                results.append({"name": name, "code": code})
        await db.commit()
    return {"imported": len(results), "students": results}


@router.get("/admin/students")
async def list_students(
    _principal: AdminPrincipal = Depends(require_admin_session),
) -> list[dict]:
    async with aiosqlite.connect(get_db_path()) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT i.code, i.student_name, i.used, i.channel_id, i.created_at,
                   i.expires_at AS invite_expires_at,
                   c.notebook_id, c.binding_status, c.expires_at
            FROM invite_codes i
            LEFT JOIN channels c ON i.channel_id = c.channel_id
            ORDER BY i.created_at DESC
            """
        )
        rows = await cursor.fetchall()
    return [
        {
            "code": row["code"],
            "student_name": row["student_name"] or "",
            "used": bool(row["used"]),
            "channel_id": row["channel_id"],
            "nlm_bound": row["binding_status"] == "bound",
            "binding_status": row["binding_status"] or "unbound",
            "notebook_id": row["notebook_id"],
            "expires_at": row["expires_at"],
            "created_at": row["created_at"],
            "invite_expires_at": row["invite_expires_at"],
        }
        for row in rows
    ]


class ExpiresAtRequest(BaseModel):
    expires_at: str | None
    mode: Literal["unassigned", "all"] = "unassigned"


class BatchExpiresAtRequest(BaseModel):
    channel_ids: list[str]
    expires_at: str | None


class StudentNameRequest(BaseModel):
    student_name: str


@router.get("/admin/set-expiry")
async def get_expiry(
    _principal: AdminPrincipal = Depends(require_admin_session),
) -> dict[str, str | int | None]:
    async with aiosqlite.connect(get_db_path()) as db:
        cursor = await db.execute(
            "SELECT course_expires_at FROM course_settings WHERE id=1"
        )
        row = await cursor.fetchone()
        counts = await (
            await db.execute(
                """
                SELECT COUNT(*),
                       SUM(CASE WHEN expires_at IS NOT NULL THEN 1 ELSE 0 END)
                FROM channels
                """
            )
        ).fetchone()
    return {
        "expires_at": row[0] if row else None,
        "applied_count": int(counts[1] or 0),
        "total_count": int(counts[0] or 0),
    }


@router.put("/admin/set-expiry")
async def set_expiry(
    body: ExpiresAtRequest,
    _principal: AdminPrincipal = Depends(require_admin_session),
) -> dict[str, str | int | None]:
    normalized_expiry: str | None = None
    if body.expires_at is not None:
        try:
            normalized_expiry = _parse_expiry(body.expires_at).isoformat()
        except ValueError as exc:
            raise HTTPException(400, "到期時間格式錯誤") from exc
    async with aiosqlite.connect(get_db_path()) as db:
        await db.execute("BEGIN IMMEDIATE")
        await db.execute(
            """
            INSERT INTO course_settings (id, course_expires_at, updated_at)
            VALUES (1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                course_expires_at=excluded.course_expires_at,
                updated_at=excluded.updated_at
            """,
            (normalized_expiry, datetime.now(timezone.utc).isoformat()),
        )
        if normalized_expiry is None or body.mode == "all":
            updated = await db.execute(
                "UPDATE channels SET expires_at=?, updated_at=?",
                (normalized_expiry, datetime.now(timezone.utc).isoformat()),
            )
        else:
            updated = await db.execute(
                """
                UPDATE channels SET expires_at=?, updated_at=?
                WHERE expires_at IS NULL
                """,
                (normalized_expiry, datetime.now(timezone.utc).isoformat()),
            )
        total_row = await (await db.execute("SELECT COUNT(*) FROM channels")).fetchone()
        await db.commit()
    return {
        "status": "ok",
        "expires_at": normalized_expiry,
        "applied_count": updated.rowcount if normalized_expiry else 0,
        "total_count": int(total_row[0] or 0),
    }


@router.put("/admin/channels/{channel_id}/expiry")
async def set_channel_expiry(
    channel_id: str,
    body: ExpiresAtRequest,
    _principal: AdminPrincipal = Depends(require_admin_session),
) -> dict[str, str | None]:
    normalized_expiry: str | None = None
    if body.expires_at is not None:
        try:
            normalized_expiry = _parse_expiry(body.expires_at).isoformat()
        except ValueError as exc:
            raise HTTPException(400, "到期時間格式錯誤") from exc
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(get_db_path()) as db:
        await db.execute("BEGIN IMMEDIATE")
        updated = await db.execute(
            "UPDATE channels SET expires_at=?, updated_at=? WHERE channel_id=?",
            (normalized_expiry, now, channel_id),
        )
        if updated.rowcount == 0:
            await db.rollback()
            raise HTTPException(404, "Channel 不存在")
        if normalized_expiry is not None:
            await db.execute(
                """
                UPDATE course_settings
                SET course_expires_at=?, updated_at=? WHERE id=1
                """,
                (normalized_expiry, now),
            )
        await db.commit()
    return {"status": "ok", "expires_at": normalized_expiry}


@router.put("/admin/invite-codes/{code}/name")
async def update_invite_student_name(
    code: str,
    body: StudentNameRequest,
    _principal: AdminPrincipal = Depends(require_admin_session),
) -> dict[str, str]:
    student_name = body.student_name.strip()
    if len(student_name) > 100 or any(
        ord(character) < 32 for character in student_name
    ):
        raise HTTPException(400, "姓名格式不正確")
    async with aiosqlite.connect(get_db_path()) as db:
        updated = await db.execute(
            "UPDATE invite_codes SET student_name=? WHERE code=?",
            (student_name, code),
        )
        if updated.rowcount == 0:
            await db.rollback()
            raise HTTPException(404, "邀請碼不存在")
        await db.commit()
    return {"status": "ok", "student_name": student_name}


@router.delete("/admin/invite-codes/{code}")
async def delete_unused_invite_code(
    code: str,
    _principal: AdminPrincipal = Depends(require_admin_session),
) -> dict[str, str]:
    """Delete an invite code only while it is still unused.

    Used invite codes represent a learner's binding and must continue to be
    removed through the existing Channel deletion flow instead.
    """

    async with aiosqlite.connect(get_db_path()) as db:
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            "SELECT used FROM invite_codes WHERE code=?",
            (code,),
        )
        row = await cursor.fetchone()
        if row is None:
            await db.rollback()
            raise HTTPException(404, "邀請碼不存在")
        if row[0]:
            await db.rollback()
            raise HTTPException(
                409,
                detail={
                    "code": "invite_code_in_use",
                    "message": "已使用的邀請碼不可刪除",
                },
            )

        deleted = await db.execute(
            "DELETE FROM invite_codes WHERE code=? AND used=0",
            (code,),
        )
        if deleted.rowcount != 1:
            await db.rollback()
            raise HTTPException(409, "邀請碼狀態已變更，請重新整理")
        await db.commit()
    return {"status": "deleted", "code": code}


@router.put("/admin/channels/expiry-batch")
async def set_selected_channels_expiry(
    body: BatchExpiresAtRequest,
    _principal: AdminPrincipal = Depends(require_admin_session),
) -> dict[str, str | int | None]:
    channel_ids = list(
        dict.fromkeys(item.strip() for item in body.channel_ids if item.strip())
    )
    if not channel_ids or len(channel_ids) > 500:
        raise HTTPException(400, "請選擇 1 至 500 位學員")
    normalized_expiry: str | None = None
    if body.expires_at is not None:
        try:
            normalized_expiry = _parse_expiry(body.expires_at).isoformat()
        except ValueError as exc:
            raise HTTPException(400, "到期時間格式錯誤") from exc

    placeholders = ",".join("?" for _ in channel_ids)
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(get_db_path()) as db:
        await db.execute("BEGIN IMMEDIATE")
        existing = await (
            await db.execute(
                f"SELECT COUNT(*) FROM channels WHERE channel_id IN ({placeholders})",
                channel_ids,
            )
        ).fetchone()
        if int(existing[0]) != len(channel_ids):
            await db.rollback()
            raise HTTPException(404, "部分學員綁定已不存在，請重新整理")
        updated = await db.execute(
            f"""
            UPDATE channels SET expires_at=?, updated_at=?
            WHERE channel_id IN ({placeholders})
            """,
            (normalized_expiry, now, *channel_ids),
        )
        if normalized_expiry is not None:
            await db.execute(
                """
                UPDATE course_settings
                SET course_expires_at=?, updated_at=? WHERE id=1
                """,
                (normalized_expiry, now),
            )
        await db.commit()
    return {
        "status": "ok",
        "expires_at": normalized_expiry,
        "applied_count": updated.rowcount,
    }


@router.delete("/admin/channels/{channel_id}")
async def delete_channel(
    channel_id: str,
    _principal: AdminPrincipal = Depends(require_admin_session),
) -> dict[str, str]:
    async with aiosqlite.connect(get_db_path()) as db:
        await db.execute("DELETE FROM channels WHERE channel_id=?", (channel_id,))
        await db.execute("DELETE FROM invite_codes WHERE channel_id=?", (channel_id,))
        await db.commit()
    return {"status": "deleted", "channel_id": channel_id}


@router.delete("/admin/clear-all")
async def clear_all_bindings(
    _principal: AdminPrincipal = Depends(require_admin_session),
) -> dict[str, str]:
    async with aiosqlite.connect(get_db_path()) as db:
        await db.execute("DELETE FROM channels")
        await db.execute("DELETE FROM invite_codes WHERE used=1")
        await db.commit()
    return {"status": "cleared"}


@router.get("/admin/export-csv")
async def export_csv(
    _principal: AdminPrincipal = Depends(require_admin_session),
) -> StreamingResponse:
    async with aiosqlite.connect(get_db_path()) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT student_name, code, used, channel_id FROM invite_codes ORDER BY created_at"
        )
        rows = await cursor.fetchall()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["姓名", "邀請碼", "已使用", "Channel ID"])
    for row in rows:
        writer.writerow(
            [
                row["student_name"] or "",
                row["code"],
                "是" if row["used"] else "否",
                row["channel_id"] or "",
            ]
        )
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=students.csv"},
    )

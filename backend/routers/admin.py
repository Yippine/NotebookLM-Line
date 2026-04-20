import csv
import io
import re
import secrets
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, UploadFile, File
from pydantic import BaseModel
import aiosqlite
from database import DB
from models import InviteVerify, ChannelCreate, ChannelOut
from config import settings

router = APIRouter(tags=["admin"])

# In-memory session tokens: token -> invite_code
_sessions: dict[str, bool] = {}
_session_codes: dict[str, str] = {}


# --- Student-facing APIs ---

@router.post("/verify-invite")
async def verify_invite(body: InviteVerify):
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT code, channel_id FROM invite_codes WHERE code=?", (body.code,)
        )
        row = await cur.fetchone()
        if not row:
            raise HTTPException(400, "邀請碼無效")

        # Check if linked channel has expired
        if row["channel_id"]:
            cur2 = await db.execute(
                "SELECT expires_at FROM channels WHERE channel_id=?", (row["channel_id"],)
            )
            ch = await cur2.fetchone()
            if ch and ch["expires_at"] and ch["expires_at"] < datetime.now(timezone.utc).isoformat():
                raise HTTPException(400, "此帳號已過期，請聯繫講師")

        await db.execute("UPDATE invite_codes SET used=1 WHERE code=?", (body.code,))
        await db.commit()

    token = secrets.token_urlsafe(32)
    _sessions[token] = True
    _session_codes[token] = body.code

    result: dict = {"token": token}
    if row["channel_id"]:
        result["channel_id"] = row["channel_id"]
    return result


def _check_token(token: str):
    if token not in _sessions:
        raise HTTPException(401, "未授權，請先驗證邀請碼")


@router.post("/channels")
async def create_channel(body: ChannelCreate, token: str = ""):
    _check_token(token)
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT channel_id FROM channels WHERE channel_id=?", (body.channel_id,))
        if await cur.fetchone():
            await db.execute(
                "UPDATE channels SET channel_secret=?, channel_access_token=? WHERE channel_id=?",
                (body.channel_secret, body.channel_access_token, body.channel_id),
            )
        else:
            # Auto-apply current expiry setting for new channels
            cur2 = await db.execute("SELECT expires_at FROM channels WHERE expires_at IS NOT NULL LIMIT 1")
            expiry_row = await cur2.fetchone()
            expires_at = expiry_row[0] if expiry_row else None
            await db.execute(
                "INSERT INTO channels (channel_id, channel_secret, channel_access_token, expires_at) VALUES (?,?,?,?)",
                (body.channel_id, body.channel_secret, body.channel_access_token, expires_at),
            )
        invite_code = _session_codes.get(token)
        if invite_code:
            await db.execute(
                "UPDATE invite_codes SET channel_id=? WHERE code=?",
                (body.channel_id, invite_code),
            )
        await db.commit()
    webhook_url = f"{settings.webhook_base_url}/webhook/{body.channel_id}"
    return {"channel_id": body.channel_id, "webhook_url": webhook_url}


@router.get("/channels/{channel_id}", response_model=ChannelOut)
async def get_channel(channel_id: str, token: str = ""):
    _check_token(token)
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM channels WHERE channel_id=?", (channel_id,))
        row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "Channel 不存在")
    return ChannelOut(
        channel_id=row["channel_id"],
        notebook_id=row["notebook_id"],
        nlm_bound=row["nlm_auth_json_encrypted"] is not None,
        webhook_url=f"{settings.webhook_base_url}/webhook/{row['channel_id']}",
    )


# --- Admin APIs ---

def _check_admin(password: str):
    if password != settings.admin_password:
        raise HTTPException(401, "管理員密碼錯誤")


@router.post("/invite-codes/generate")
async def generate_invite_codes(count: int = 5, admin_password: str = ""):
    _check_admin(admin_password)
    codes = [secrets.token_urlsafe(8) for _ in range(count)]
    async with aiosqlite.connect(DB) as db:
        await db.executemany(
            "INSERT INTO invite_codes (code) VALUES (?)", [(c,) for c in codes]
        )
        await db.commit()
    return {"codes": codes}


@router.post("/admin/import-csv")
async def import_csv(admin_password: str = "", file: UploadFile = File(...)):
    """Import student CSV, auto-generate invite codes. CSV needs a 'name' column."""
    _check_admin(admin_password)

    content = (await file.read()).decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(content))

    # Find name column (flexible matching)
    if not reader.fieldnames:
        raise HTTPException(400, "CSV 格式錯誤：無法讀取欄位")

    name_col = None
    for col in reader.fieldnames:
        if col.strip().lower() in ("name", "姓名", "學員姓名", "student_name"):
            name_col = col
            break
    if not name_col:
        raise HTTPException(400, f"CSV 缺少姓名欄位（支援：name, 姓名, 學員姓名）。找到的欄位：{', '.join(reader.fieldnames)}")

    results = []
    async with aiosqlite.connect(DB) as db:
        for row in reader:
            raw = row[name_col].strip()
            if not raw:
                continue
            # Split multiple names: 頓號、comma、slash
            names = re.split(r"[、,，/]", raw)
            for n in names:
                # Remove parenthetical annotations like （男）(女)
                name = re.sub(r"[（(][^）)]*[）)]", "", n).strip()
                if not name:
                    continue
                code = secrets.token_urlsafe(8)
                await db.execute(
                    "INSERT INTO invite_codes (code, student_name) VALUES (?, ?)",
                    (code, name),
                )
                results.append({"name": name, "code": code})
        await db.commit()

    return {"imported": len(results), "students": results}


@router.get("/admin/students")
async def list_students(admin_password: str = ""):
    _check_admin(admin_password)
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT i.code, i.student_name, i.used, i.channel_id, i.created_at,
                   c.nlm_auth_json_encrypted IS NOT NULL as nlm_bound,
                   c.notebook_id, c.expires_at
            FROM invite_codes i
            LEFT JOIN channels c ON i.channel_id = c.channel_id
            ORDER BY i.created_at DESC
        """)
        rows = await cur.fetchall()
    return [
        {
            "code": r["code"],
            "student_name": r["student_name"] or "",
            "used": bool(r["used"]),
            "channel_id": r["channel_id"],
            "nlm_bound": bool(r["nlm_bound"]) if r["nlm_bound"] is not None else False,
            "notebook_id": r["notebook_id"],
            "expires_at": r["expires_at"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]


class ExpiresAtRequest(BaseModel):
    expires_at: str  # ISO format datetime


@router.put("/admin/set-expiry")
async def set_expiry(body: ExpiresAtRequest, admin_password: str = ""):
    """Set expiry date for ALL channels (course end date)."""
    _check_admin(admin_password)
    async with aiosqlite.connect(DB) as db:
        await db.execute("UPDATE channels SET expires_at=?", (body.expires_at,))
        await db.commit()
    return {"status": "ok", "expires_at": body.expires_at}


@router.delete("/admin/channels/{channel_id}")
async def delete_channel(channel_id: str, admin_password: str = ""):
    _check_admin(admin_password)
    async with aiosqlite.connect(DB) as db:
        await db.execute("DELETE FROM channels WHERE channel_id=?", (channel_id,))
        await db.execute("DELETE FROM invite_codes WHERE channel_id=?", (channel_id,))
        await db.commit()
    return {"status": "deleted", "channel_id": channel_id}


@router.delete("/admin/clear-all")
async def clear_all_bindings(admin_password: str = ""):
    """Delete all channels and reset all invite codes."""
    _check_admin(admin_password)
    async with aiosqlite.connect(DB) as db:
        await db.execute("DELETE FROM channels")
        await db.execute("DELETE FROM invite_codes WHERE used=1")
        await db.commit()
    return {"status": "cleared"}


@router.get("/admin/export-csv")
async def export_csv(admin_password: str = ""):
    """Export student name ↔ invite code mapping as CSV."""
    _check_admin(admin_password)
    from fastapi.responses import StreamingResponse

    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT student_name, code, used, channel_id FROM invite_codes ORDER BY created_at"
        )
        rows = await cur.fetchall()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["姓名", "邀請碼", "已使用", "Channel ID"])
    for r in rows:
        writer.writerow([r["student_name"] or "", r["code"], "是" if r["used"] else "否", r["channel_id"] or ""])

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=students.csv"},
    )

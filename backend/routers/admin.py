import secrets
from fastapi import APIRouter, HTTPException
import aiosqlite
from database import DB
from models import InviteVerify, ChannelCreate, ChannelOut
from config import settings

router = APIRouter(tags=["admin"])

# In-memory session tokens: token -> invite_code
_sessions: dict[str, bool] = {}
_session_codes: dict[str, str] = {}


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
            # Already exists — update LINE info only, preserve NLM binding
            await db.execute(
                "UPDATE channels SET channel_secret=?, channel_access_token=? WHERE channel_id=?",
                (body.channel_secret, body.channel_access_token, body.channel_id),
            )
        else:
            await db.execute(
                "INSERT INTO channels (channel_id, channel_secret, channel_access_token) VALUES (?,?,?)",
                (body.channel_id, body.channel_secret, body.channel_access_token),
            )
        # Link invite code to this channel
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


@router.post("/invite-codes/generate")
async def generate_invite_codes(count: int = 10, admin_password: str = ""):
    """Generate invite codes (admin only)."""
    if admin_password != settings.admin_password:
        raise HTTPException(401, "管理員密碼錯誤")
    codes = [secrets.token_urlsafe(8) for _ in range(count)]
    async with aiosqlite.connect(DB) as db:
        await db.executemany(
            "INSERT INTO invite_codes (code) VALUES (?)", [(c,) for c in codes]
        )
        await db.commit()
    return {"codes": codes}


@router.get("/admin/students")
async def list_students(admin_password: str = ""):
    """List all invite codes with their channel binding status."""
    if admin_password != settings.admin_password:
        raise HTTPException(401, "管理員密碼錯誤")
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT i.code, i.used, i.channel_id, i.created_at,
                   c.nlm_auth_json_encrypted IS NOT NULL as nlm_bound,
                   c.notebook_id
            FROM invite_codes i
            LEFT JOIN channels c ON i.channel_id = c.channel_id
            ORDER BY i.created_at DESC
        """)
        rows = await cur.fetchall()
    return [
        {
            "code": r["code"],
            "used": bool(r["used"]),
            "channel_id": r["channel_id"],
            "nlm_bound": bool(r["nlm_bound"]) if r["nlm_bound"] is not None else False,
            "notebook_id": r["notebook_id"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]


@router.delete("/admin/channels/{channel_id}")
async def delete_channel(channel_id: str, admin_password: str = ""):
    """Delete a channel and reset its invite code."""
    if admin_password != settings.admin_password:
        raise HTTPException(401, "管理員密碼錯誤")
    async with aiosqlite.connect(DB) as db:
        await db.execute("DELETE FROM channels WHERE channel_id=?", (channel_id,))
        await db.execute(
            "UPDATE invite_codes SET used=0, channel_id=NULL WHERE channel_id=?",
            (channel_id,),
        )
        await db.commit()
    return {"status": "deleted", "channel_id": channel_id}

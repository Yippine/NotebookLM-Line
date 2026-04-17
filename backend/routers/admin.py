import secrets
from fastapi import APIRouter, HTTPException
import aiosqlite
from database import DB
from models import InviteVerify, ChannelCreate, ChannelOut
from config import settings

router = APIRouter(tags=["admin"])

# In-memory session tokens (simple approach for 40 users)
_sessions: dict[str, bool] = {}


@router.post("/verify-invite")
async def verify_invite(body: InviteVerify):
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute(
            "SELECT code FROM invite_codes WHERE code=? AND used=0", (body.code,)
        )
        row = await cur.fetchone()
        if not row:
            raise HTTPException(400, "邀請碼無效或已使用")
        await db.execute("UPDATE invite_codes SET used=1 WHERE code=?", (body.code,))
        await db.commit()

    token = secrets.token_urlsafe(32)
    _sessions[token] = True
    return {"token": token}


def _check_token(token: str):
    if token not in _sessions:
        raise HTTPException(401, "未授權，請先驗證邀請碼")


@router.post("/channels")
async def create_channel(body: ChannelCreate, token: str = ""):
    _check_token(token)
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "INSERT OR REPLACE INTO channels (channel_id, channel_secret, channel_access_token) VALUES (?,?,?)",
            (body.channel_id, body.channel_secret, body.channel_access_token),
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
async def generate_invite_codes(count: int = 10):
    """Utility: generate invite codes (call from admin/CLI)."""
    codes = [secrets.token_urlsafe(8) for _ in range(count)]
    async with aiosqlite.connect(DB) as db:
        await db.executemany(
            "INSERT INTO invite_codes (code) VALUES (?)", [(c,) for c in codes]
        )
        await db.commit()
    return {"codes": codes}

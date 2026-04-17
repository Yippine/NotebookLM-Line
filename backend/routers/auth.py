from fastapi import APIRouter, HTTPException
import aiosqlite
import json
import glob
import os
from database import DB
from models import NlmLoginRequest
from services.nlm_service import bind_nlm

router = APIRouter(tags=["auth"])


def _find_storage_state() -> str | None:
    """Find the most recently created storage_state.json."""
    home = os.path.expanduser("~")
    patterns = [
        os.path.join(home, ".notebooklm", "profiles", "*", "storage_state.json"),
        os.path.join(home, ".notebooklm", "browser_profile", "storage_state.json"),
        os.path.join(home, ".config", "notebooklm", "profiles", "*", "storage_state.json"),
    ]
    files = []
    for p in patterns:
        files.extend(glob.glob(p))
    if not files:
        return None
    return max(files, key=os.path.getmtime)


@router.post("/channels/{channel_id}/nlm-login")
async def nlm_login(channel_id: str, body: NlmLoginRequest):
    """Upload storage_state.json content to bind NLM."""
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT channel_id FROM channels WHERE channel_id=?", (channel_id,))
        if not await cur.fetchone():
            raise HTTPException(404, "Channel 不存在，請先建立 Channel")

    try:
        notebook_id = await bind_nlm(channel_id, body.storage_state_json)
    except Exception as e:
        raise HTTPException(400, f"NotebookLM 綁定失敗：{e}")

    return {
        "status": "bound",
        "notebook_id": notebook_id,
        "message": "NotebookLM 綁定成功" + (f"，已選取 Notebook: {notebook_id}" if notebook_id else "，但未找到任何 Notebook"),
    }


@router.post("/channels/{channel_id}/nlm-bind-local")
async def nlm_bind_local(channel_id: str):
    """Auto-read storage_state.json from server filesystem after manual `notebooklm login`."""
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT channel_id FROM channels WHERE channel_id=?", (channel_id,))
        if not await cur.fetchone():
            raise HTTPException(404, "Channel 不存在，請先建立 Channel")

    path = _find_storage_state()
    if not path:
        raise HTTPException(
            400,
            "找不到 storage_state.json。請先在伺服器 terminal 執行 notebooklm login 完成登入。"
        )

    try:
        with open(path, "r", encoding="utf-8") as f:
            storage_state = json.load(f)
    except Exception as e:
        raise HTTPException(400, f"讀取 storage_state.json 失敗：{e}")

    try:
        notebook_id = await bind_nlm(channel_id, storage_state)
    except Exception as e:
        raise HTTPException(400, f"NotebookLM 綁定失敗：{e}")

    return {
        "status": "bound",
        "notebook_id": notebook_id,
        "storage_path": path,
        "message": "NotebookLM 綁定成功" + (f"，已選取 Notebook: {notebook_id}" if notebook_id else "，但未找到任何 Notebook"),
    }


@router.get("/channels/{channel_id}/nlm-status")
async def nlm_status(channel_id: str):
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT nlm_auth_json_encrypted, notebook_id FROM channels WHERE channel_id=?",
            (channel_id,),
        )
        row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "Channel 不存在")
    return {
        "bound": row["nlm_auth_json_encrypted"] is not None,
        "notebook_id": row["notebook_id"],
    }

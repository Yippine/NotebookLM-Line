from fastapi import APIRouter, HTTPException
import aiosqlite
import json
import glob
import os
from database import DB
from models import NlmLoginRequest, NotebookSelect
from services.crypto_service import decrypt
from services.nlm_service import (
    bind_nlm,
    configure_restricted_topic_guard,
    list_notebooks_for_channel,
    select_notebook,
)

router = APIRouter(tags=["auth"])


def _find_storage_state() -> str | None:
    """尋找最近建立的 storage_state.json。"""
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
    """上傳 storage_state.json 的內容以綁定 NLM。"""
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT channel_id FROM channels WHERE channel_id=?", (channel_id,))
        if not await cur.fetchone():
            raise HTTPException(404, "Channel 不存在，請先建立 Channel")

    try:
        notebook_id, notebooks = await bind_nlm(channel_id, body.storage_state_json)
    except Exception as e:
        raise HTTPException(400, f"NotebookLM 綁定失敗：{e}")

    return {
        "status": "bound",
        "notebook_id": notebook_id,
        "notebooks": notebooks,
        "message": "NotebookLM 綁定成功",
    }


@router.post("/channels/{channel_id}/nlm-bind-local")
async def nlm_bind_local(channel_id: str):
    """在手動執行 `notebooklm login` 後，自動從伺服器檔案系統讀取 storage_state.json。"""
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
        notebook_id, notebooks = await bind_nlm(channel_id, storage_state)
    except Exception as e:
        raise HTTPException(400, f"NotebookLM 綁定失敗：{e}")

    return {
        "status": "bound",
        "notebook_id": notebook_id,
        "notebooks": notebooks,
        "storage_path": path,
        "message": "NotebookLM 綁定成功",
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


@router.post("/channels/{channel_id}/refresh-guard")
async def refresh_guard(channel_id: str):
    """將目前的限制主題人設指令重新推送到一個
    已綁定 channel 的筆記本（例如調整 prompt 文字之後）。"""
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT nlm_auth_json_encrypted, notebook_id FROM channels WHERE channel_id=?",
            (channel_id,),
        )
        row = await cur.fetchone()

    if not row or not row["nlm_auth_json_encrypted"] or not row["notebook_id"]:
        raise HTTPException(400, "此 Channel 尚未綁定 NotebookLM")

    storage_state = decrypt(row["nlm_auth_json_encrypted"])
    try:
        await configure_restricted_topic_guard(storage_state, row["notebook_id"])
    except Exception as e:
        raise HTTPException(400, f"套用限制主題設定失敗：{e}")

    return {"status": "ok", "channel_id": channel_id}


@router.get("/channels/{channel_id}/notebooks")
async def get_notebooks(channel_id: str):
    """列出此 channel 已綁定 NLM 帳號下的所有筆記本。"""
    try:
        notebooks = await list_notebooks_for_channel(channel_id)
    except Exception as e:
        raise HTTPException(400, f"取得筆記本清單失敗：{e}")
    return {"notebooks": notebooks}


@router.put("/channels/{channel_id}/notebook")
async def set_notebook(channel_id: str, body: NotebookSelect):
    """選擇此 channel 要使用哪一個筆記本。"""
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT channel_id FROM channels WHERE channel_id=?", (channel_id,))
        if not await cur.fetchone():
            raise HTTPException(404, "Channel 不存在")
    await select_notebook(channel_id, body.notebook_id)
    return {"status": "ok", "notebook_id": body.notebook_id}

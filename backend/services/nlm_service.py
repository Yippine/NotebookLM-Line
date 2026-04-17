import json
import os
import asyncio
from notebooklm import NotebookLMClient
from services.crypto_service import encrypt, decrypt
from database import DB
import aiosqlite

# Lock to prevent concurrent env var overwrites
_nlm_lock = asyncio.Lock()


async def _run_with_auth(auth_json: dict, coro_fn):
    """Run a notebooklm-py operation with the given auth, thread-safe."""
    async with _nlm_lock:
        old = os.environ.get("NOTEBOOKLM_AUTH_JSON")
        os.environ["NOTEBOOKLM_AUTH_JSON"] = json.dumps(auth_json)
        try:
            async with await NotebookLMClient.from_storage() as client:
                return await coro_fn(client)
        finally:
            if old is None:
                os.environ.pop("NOTEBOOKLM_AUTH_JSON", None)
            else:
                os.environ["NOTEBOOKLM_AUTH_JSON"] = old


async def bind_nlm(channel_id: str, storage_state: dict):
    """Encrypt and store the NLM storage_state, then fetch first notebook ID."""
    encrypted = encrypt(storage_state)

    async def _get_first_notebook(client):
        notebooks = await client.notebooks.list()
        return notebooks[0].id if notebooks else None

    notebook_id = await _run_with_auth(storage_state, _get_first_notebook)

    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "UPDATE channels SET nlm_auth_json_encrypted=?, notebook_id=? WHERE channel_id=?",
            (encrypted, notebook_id, channel_id),
        )
        await db.commit()

    return notebook_id


async def ask_question(channel_id: str, question: str) -> str:
    """Load cookie for channel, ask NotebookLM, return answer text."""
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT nlm_auth_json_encrypted, notebook_id FROM channels WHERE channel_id=?",
            (channel_id,),
        )
        row = await cur.fetchone()

    if not row or not row["nlm_auth_json_encrypted"] or not row["notebook_id"]:
        return "⚠️ NotebookLM 尚未綁定，請聯繫管理員完成設定。"

    storage_state = decrypt(row["nlm_auth_json_encrypted"])
    notebook_id = row["notebook_id"]

    async def _ask(client):
        result = await client.chat.ask(notebook_id, question)
        return result.answer

    try:
        return await _run_with_auth(storage_state, _ask)
    except Exception as e:
        return f"⚠️ 查詢失敗：{e}"

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

    notebooks = await list_notebooks(storage_state)
    notebook_id = notebooks[0]["id"] if notebooks else None

    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "UPDATE channels SET nlm_auth_json_encrypted=?, notebook_id=? WHERE channel_id=?",
            (encrypted, notebook_id, channel_id),
        )
        await db.commit()

    return notebook_id, notebooks


async def list_notebooks(storage_state: dict) -> list[dict]:
    """Return list of {id, title} for all notebooks."""
    async def _list(client):
        notebooks = await client.notebooks.list()
        return [{"id": nb.id, "title": nb.title} for nb in notebooks]

    return await _run_with_auth(storage_state, _list)


async def list_notebooks_for_channel(channel_id: str) -> list[dict]:
    """Fetch notebook list using stored cookie for a channel."""
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT nlm_auth_json_encrypted FROM channels WHERE channel_id=?",
            (channel_id,),
        )
        row = await cur.fetchone()

    if not row or not row["nlm_auth_json_encrypted"]:
        return []

    storage_state = decrypt(row["nlm_auth_json_encrypted"])
    return await list_notebooks(storage_state)


async def select_notebook(channel_id: str, notebook_id: str):
    """Update the selected notebook for a channel."""
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "UPDATE channels SET notebook_id=? WHERE channel_id=?",
            (notebook_id, channel_id),
        )
        await db.commit()


async def ask_question(channel_id: str, question: str) -> list[str]:
    """Load cookie for channel, ask NotebookLM, return [answer, sources] messages."""
    from services.text_formatter import format_for_line, build_sources_message

    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT nlm_auth_json_encrypted, notebook_id FROM channels WHERE channel_id=?",
            (channel_id,),
        )
        row = await cur.fetchone()

    if not row or not row["nlm_auth_json_encrypted"] or not row["notebook_id"]:
        return ["⚠️ NotebookLM 尚未綁定，請聯繫管理員完成設定。"]

    storage_state = decrypt(row["nlm_auth_json_encrypted"])
    notebook_id = row["notebook_id"]

    async def _ask(client):
        result = await client.chat.ask(notebook_id, question)
        # Build source_id → title map
        source_map = {}
        if result.references:
            sources = await client.sources.list(notebook_id)
            id_to_title = {s.id: s.title for s in sources}
            for ref in result.references:
                if ref.citation_number is not None and ref.source_id in id_to_title:
                    source_map[ref.citation_number] = id_to_title[ref.source_id]
        return result.answer, source_map

    try:
        answer, source_map = await _run_with_auth(storage_state, _ask)
        messages = [format_for_line(answer)]
        sources_msg = build_sources_message(answer, source_map)
        if sources_msg:
            messages.append(sources_msg)
        return messages
    except Exception as e:
        return [f"⚠️ 查詢失敗：{e}"]

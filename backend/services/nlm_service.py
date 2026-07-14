import json
import os
import asyncio
import mimetypes
import tempfile
from pathlib import Path
from notebooklm import NotebookLMClient, ChatGoal
from markitdown import MarkItDownException
from services.crypto_service import encrypt, decrypt
from services.doc_converter import convert_to_markdown
from database import DB
import aiosqlite

# Lock to prevent concurrent env var overwrites
_nlm_lock = asyncio.Lock()

# Priority instruction enforced at the NotebookLM notebook level (not just the
# LINE-side keyword filter in routers/webhook.py), so the model itself refuses
# these topics even when a question doesn't match the keyword list.
RESTRICTED_TOPIC_CUSTOM_PROMPT = (
    "你是僅供諮詢用途的客服助理，以下規則優先於所有其他指示：\n"
    "只有當使用者問題的「主要目的」就是交易本身時才需要拒答，包括：詢問價格、報價、"
    "折扣、付款方式、訂金、簽約、下單、收購價；要求代為下單、收款或代購；詢問授權、"
    "加盟、經銷資格；要求你承諾或保證任何結果。\n"
    "以下情況即使句子中出現「買」「賣」「這台車」等字眼，仍然要當作一般諮詢正常回答，"
    "不可拒答：詢問車輛規格、配備、功能、性能等技術資訊；詢問是否有特定年份、車型、"
    "顏色的現車或庫存；詢問展示中心、服務據點、保固、保養等一般諮詢。\n"
    "只有在確定使用者的核心意圖就是交易本身時，才只回覆：「本機器人僅供諮詢用途，"
    "不可以回答有關交易、買賣、授權、承諾等事宜。」"
)


async def configure_restricted_topic_guard(storage_state: dict, notebook_id: str) -> None:
    """Set the notebook's custom chat persona to refuse transaction-related topics."""

    async def _configure(client):
        await client.chat.configure(
            notebook_id,
            goal=ChatGoal.CUSTOM,
            custom_prompt=RESTRICTED_TOPIC_CUSTOM_PROMPT,
        )

    await _run_with_auth(storage_state, _configure)


async def _run_with_auth(auth_json: dict, coro_fn):
    """Run a notebooklm-py operation with the given auth.

    NOTEBOOKLM_AUTH_JSON is a single process-wide env var, and notebooklm-py
    only reads it while opening a session (inside ``__aenter__``) — once
    open, credentials are bound to the client instance and the env var is
    never consulted again. So only that handshake needs to be serialized
    against other channels swapping the env var out from under it; the slow
    part (asking a question, uploading a source, etc.) runs outside the lock
    so different channels' requests don't queue behind each other.
    """
    client_ctx = NotebookLMClient.from_storage()
    async with _nlm_lock:
        old = os.environ.get("NOTEBOOKLM_AUTH_JSON")
        os.environ["NOTEBOOKLM_AUTH_JSON"] = json.dumps(auth_json)
        try:
            client = await client_ctx.__aenter__()
        finally:
            if old is None:
                os.environ.pop("NOTEBOOKLM_AUTH_JSON", None)
            else:
                os.environ["NOTEBOOKLM_AUTH_JSON"] = old

    try:
        return await coro_fn(client)
    finally:
        await client_ctx.__aexit__(None, None, None)


async def bind_nlm(channel_id: str, storage_state: dict):
    """Encrypt and store the NLM storage_state, then fetch first notebook ID."""
    encrypted = encrypt(storage_state)

    notebooks = await list_notebooks(storage_state)
    notebook_id = notebooks[0]["id"] if notebooks else None

    if notebook_id:
        await configure_restricted_topic_guard(storage_state, notebook_id)

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
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT nlm_auth_json_encrypted FROM channels WHERE channel_id=?",
            (channel_id,),
        )
        row = await cur.fetchone()
        await db.execute(
            "UPDATE channels SET notebook_id=? WHERE channel_id=?",
            (notebook_id, channel_id),
        )
        await db.commit()

    if row and row["nlm_auth_json_encrypted"]:
        storage_state = decrypt(row["nlm_auth_json_encrypted"])
        await configure_restricted_topic_guard(storage_state, notebook_id)


async def replace_sources_for_notebook(
    storage_state: dict,
    notebook_id: str,
    file_bytes: bytes,
    file_name: str,
    mime_type: str | None = None,
    title: str | None = None,
) -> str:
    """Upload a file to the notebook.

    If an existing source has the same name (title), it is deleted and
    replaced by the newly uploaded file. Sources with different names are
    left untouched, so the knowledge base accumulates files instead of being
    wiped on every upload.
    """

    async def _replace(client):
        existing_sources = await client.sources.list(notebook_id)
        uploaded_title = title or Path(file_name).name

        # Only sources whose title exactly matches the new file's name are
        # considered "the same file" and will be overwritten.
        duplicate_sources = [
            s for s in existing_sources if s.title == uploaded_title
        ]

        with tempfile.NamedTemporaryFile(
            suffix=Path(file_name).suffix or ".txt",
            delete=False,
        ) as tmp_file:
            tmp_file.write(file_bytes)
            temp_path = tmp_file.name

        try:
            source = await client.sources.add_file(
                notebook_id,
                temp_path,
                mime_type=mime_type,
                title=uploaded_title,
                wait=True,
                wait_timeout=180.0,
            )
            for source_item in duplicate_sources:
                await client.sources.delete(notebook_id, source_item.id)

            final_title = source.title or uploaded_title
            if duplicate_sources:
                return f"✅ 知識庫已更新。已覆蓋同名舊檔案並上傳：{final_title}"
            return f"✅ 知識庫已更新。已新增檔案：{final_title}"
        finally:
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass

    return await _run_with_auth(storage_state, _replace)


async def update_knowledge_base(
    channel_id: str,
    file_bytes: bytes,
    file_name: str,
    title: str | None = None,
) -> str:
    """Convert an uploaded file to Markdown, then upload it to the channel's notebook."""
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT nlm_auth_json_encrypted, notebook_id FROM channels WHERE channel_id=?",
            (channel_id,),
        )
        row = await cur.fetchone()

    if not row or not row["nlm_auth_json_encrypted"] or not row["notebook_id"]:
        return "⚠️ NotebookLM 尚未綁定，請聯繫管理員完成設定。"

    try:
        markdown_bytes, markdown_name = convert_to_markdown(file_bytes, file_name)
    except MarkItDownException as e:
        return (
            f"⚠️ 檔案轉換失敗，無法上傳：{e}\n"
            "請確認檔案格式是否支援（PDF、Word、PowerPoint、Excel、純文字等）。"
        )

    storage_state = decrypt(row["nlm_auth_json_encrypted"])
    notebook_id = row["notebook_id"]

    try:
        return await replace_sources_for_notebook(
            storage_state,
            notebook_id,
            markdown_bytes,
            markdown_name,
            mime_type="text/markdown",
            title=title,
        )
    except Exception as e:
        return f"⚠️ 更新知識庫失敗：{e}"


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

import json
import logging
import os
import asyncio
import mimetypes
import tempfile
from pathlib import Path
from notebooklm import NotebookLMClient, ChatGoal
from markitdown import MarkItDownException
from services.alert_service import notify_admin
from services.crypto_service import encrypt, decrypt
from services.doc_converter import convert_to_markdown
from database import DB
import aiosqlite

logger = logging.getLogger(__name__)

# 避免並行覆寫環境變數的鎖
_nlm_lock = asyncio.Lock()

# 重複使用的 NotebookLMClient session，以 channel_id 為 key。
# ask_question 會在每一則 LINE 訊息時被呼叫，而透過 _run_with_auth
# 開啟一個 session 會建立一個全新的 httpx.AsyncClient（連線池是冷的，
# 所以第一次 RPC 要付出一次全新的 TCP/TLS 交握成本），關閉時又要把
# cookie jar 寫回磁碟——如果同一個 channel 過一會兒又要問下一個
# 問題，這些都是純粹的額外開銷。只有這條高頻的提問路徑會用到這個
# 快取；不常發生的管理操作（上傳、設定、列表）仍然走 _run_with_auth
# 每次呼叫都開啟再關閉的路徑，因為那邊多一次交握相對於這些操作
# 本身的延遲來說可以忽略不計，而且把快取的作用範圍限縮得窄一點，
# 也能限制快取 bug 一旦出現時的影響範圍。
_client_cache: dict[str, tuple] = {}
_client_cache_lock = asyncio.Lock()


async def _get_cached_client(channel_id: str, auth_json: dict):
    """回傳此 channel 已快取、已開啟的 NotebookLMClient，
    首次使用時才建立。"""
    async with _client_cache_lock:
        cached = _client_cache.get(channel_id)
        if cached is not None:
            return cached[1]

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

        _client_cache[channel_id] = (client_ctx, client)
        return client


async def _invalidate_client(channel_id: str) -> None:
    """捨棄並關閉某個 channel 已快取的 session，例如因為它已經
    失效，或是因為該 channel 的認證資訊剛被 bind_nlm 取代。"""
    async with _client_cache_lock:
        cached = _client_cache.pop(channel_id, None)
    if cached is not None:
        client_ctx, _ = cached
        try:
            await client_ctx.__aexit__(None, None, None)
        except Exception:
            logger.warning("Failed to close stale NotebookLM session for channel %s", channel_id)


async def aclose_all_clients() -> None:
    """關閉所有已快取的 NotebookLM session。應在應用程式關閉時呼叫。"""
    async with _client_cache_lock:
        cached_items = list(_client_cache.items())
        _client_cache.clear()
    for channel_id, (client_ctx, _) in cached_items:
        try:
            await client_ctx.__aexit__(None, None, None)
        except Exception:
            logger.warning("Failed to close NotebookLM session for channel %s", channel_id)

# client.sources.list(notebook_id)結果的快取，以 notebook_id 為 key。
# ask_question 幾乎每一個問題都需要 id->title 對照表來標註引用來源，
# 但來源實際上只會透過 replace_sources_for_notebook 改變（它會讓
# 對應的快取項目失效）——所以每一個問題都重新抓一次完整清單，
# 大多數時候都是純粹多餘的一次 API 往返。
_sources_cache: dict[str, list] = {}


def _invalidate_sources_cache(notebook_id: str) -> None:
    _sources_cache.pop(notebook_id, None)

# 這是在 NotebookLM 筆記本層級強制執行的優先指令（不只是
# routers/webhook.py 中 LINE 端的關鍵字過濾），讓模型本身即使在
# 問題沒有比對到關鍵字清單時，也會主動拒答這些主題。
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
    """將筆記本的自訂聊天人設設定為拒絕討論交易相關主題。"""

    async def _configure(client):
        await client.chat.configure(
            notebook_id,
            goal=ChatGoal.CUSTOM,
            custom_prompt=RESTRICTED_TOPIC_CUSTOM_PROMPT,
        )

    await _run_with_auth(storage_state, _configure)


async def _run_with_auth(auth_json: dict, coro_fn):
    """以指定的認證資訊執行一個 notebooklm-py 操作。

    NOTEBOOKLM_AUTH_JSON 是一個整個行程共用的環境變數，而
    notebooklm-py 只會在開啟 session 時（在 ``__aenter__`` 內部）
    讀取它一次——一旦開啟完成，認證資訊就會綁定在該 client
    實例上，之後不會再去讀取這個環境變數。所以只有這個交握
    步驟需要與其他 channel 在背後互相調換環境變數的行為做序列化；
    真正耗時的部分（提問、上傳來源等）都在鎖外執行，這樣不同
    channel 的請求才不會互相排隊卡住彼此。
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
    """加密並儲存 NLM 的 storage_state，然後取得第一個筆記本 ID。"""
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

    # 這個 channel 任何已快取的 session 都是用舊的認證資訊開啟的——
    # 把它捨棄掉，讓下一次 ask_question 用新的認證資訊重新開啟一個。
    await _invalidate_client(channel_id)

    return notebook_id, notebooks


async def list_notebooks(storage_state: dict) -> list[dict]:
    """回傳所有筆記本的 {id, title} 清單。"""
    async def _list(client):
        notebooks = await client.notebooks.list()
        return [{"id": nb.id, "title": nb.title} for nb in notebooks]

    return await _run_with_auth(storage_state, _list)


async def list_notebooks_for_channel(channel_id: str) -> list[dict]:
    """使用某個 channel 已儲存的 cookie 取得筆記本清單。"""
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
    """更新某個 channel 所選擇的筆記本。"""
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
    """將檔案上傳到筆記本。

    若已存在同名（title）的來源，會先刪除它，再以新上傳的檔案
    取代。不同名稱的來源則不受影響，讓知識庫能持續累積檔案，
    而不是每次上傳都被清空重來。
    """

    async def _replace(client):
        existing_sources = await client.sources.list(notebook_id)
        uploaded_title = title or Path(file_name).name

        # 只有標題與新檔案名稱完全相符的來源，才會被視為「同一份
        # 檔案」而遭到覆蓋。
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

            _invalidate_sources_cache(notebook_id)

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
    """將上傳的檔案轉換為 Markdown，再上傳到該 channel 的筆記本。"""
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


async def _set_health_status(channel_id: str, status: str) -> None:
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "UPDATE channels SET nlm_health_status=? WHERE channel_id=?",
            (status, channel_id),
        )
        await db.commit()


async def check_all_channels_health() -> None:
    """主動探測每個已綁定 channel 的 NotebookLM session，讓失效的
    cookie 能依排程被發現，而不是要等到某個學生剛好先問了問題
    （然後還得等有人注意到投訴）。每個 channel 都是獨立檢查的——
    某一個 channel 的 session 失效，不應該讓其餘 channel 的檢查
    也跟著停下來。

    通知只在狀態真正從 healthy（或從未檢查過）轉變成 expired 的
    那一刻發送一次——同一個 channel 持續壞著的話，接下來每 4 小時
    再檢查到同樣的失敗，不會重複告警，直到它恢復正常、之後又再次
    壞掉為止，才會發出下一次通知。
    """
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT channel_id, nlm_auth_json_encrypted, nlm_health_status FROM channels "
            "WHERE nlm_auth_json_encrypted IS NOT NULL AND notebook_id IS NOT NULL"
        )
        rows = await cur.fetchall()

    for row in rows:
        channel_id = row["channel_id"]
        was_expired = row["nlm_health_status"] == "expired"
        try:
            storage_state = decrypt(row["nlm_auth_json_encrypted"])
            await list_notebooks(storage_state)
        except Exception as e:
            logger.warning(f"[{channel_id}] NotebookLM health check failed: {e}")
            await _set_health_status(channel_id, "expired")
            if not was_expired:
                # 剛剛才變成 expired（不是本來就已知壞掉、還沒修好）——
                # 這是真正值得發一次通知的「狀態轉換」瞬間。
                await notify_admin(
                    f"nlm-health:{channel_id}",
                    f"⚠️ 定期健康檢查發現頻道 {channel_id} 的 NotebookLM 登入已失效：{e}\n"
                    "請重新 notebooklm login 並更新綁定，目前還沒有學員回報，及早處理可避免影響到他們。",
                    # 這裡已經用 DB 裡的 healthy/expired 狀態轉換做過精確的
                    # 防重複判斷了，不需要再疊加 notify_admin 自己那套以
                    # 時間為準的冷卻機制——否則短時間內真的又壞一次時，
                    # 會被那個冷卻機制誤擋下來。
                    cooldown_seconds=0,
                )
        else:
            if was_expired:
                await _set_health_status(channel_id, "healthy")


async def _get_conversation_id(channel_id: str, line_user_id: str) -> str | None:
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute(
            "SELECT conversation_id FROM user_conversations WHERE channel_id=? AND line_user_id=?",
            (channel_id, line_user_id),
        )
        row = await cur.fetchone()
    return row[0] if row else None


async def _save_conversation_id(channel_id: str, line_user_id: str, conversation_id: str) -> None:
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            """
            INSERT INTO user_conversations (channel_id, line_user_id, conversation_id, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(channel_id, line_user_id)
            DO UPDATE SET conversation_id=excluded.conversation_id, updated_at=CURRENT_TIMESTAMP
            """,
            (channel_id, line_user_id, conversation_id),
        )
        await db.commit()


async def ask_question(channel_id: str, question: str, line_user_id: str | None = None) -> list[str]:
    """載入該 channel 的 cookie，向 NotebookLM 提問，並回傳可直接
    用於 LINE 的答案訊息。

    當有提供 ``line_user_id`` 時，這個問題會延續該使用者在這個
    筆記本上自己的 NotebookLM 對話串（跨請求持久保存），讓後續
    追問能帶著先前的上下文。若沒有提供（例如 LINE 不會給匿名
    群組成員一個 id），則每個問題都會獨立提問。
    """
    from services.text_formatter import format_for_line, build_answer_messages

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

    conversation_id = (
        await _get_conversation_id(channel_id, line_user_id) if line_user_id else None
    )

    async def _ask(client):
        result = await client.chat.ask(notebook_id, question, conversation_id=conversation_id)
        # 建立 source_id → title 的對照表
        source_map = {}
        if result.references:
            sources = _sources_cache.get(notebook_id)
            if sources is None:
                sources = await client.sources.list(notebook_id)
                _sources_cache[notebook_id] = sources
            id_to_title = {s.id: s.title for s in sources}
            for ref in result.references:
                if ref.citation_number is not None and ref.source_id in id_to_title:
                    source_map[ref.citation_number] = id_to_title[ref.source_id]
        logger.info(
            f"[{channel_id}] raw answer (for vendor-split debugging): {result.answer!r} "
            f"source_map={source_map!r}"
        )
        return result.answer, result.conversation_id, source_map

    try:
        try:
            client = await _get_cached_client(channel_id, storage_state)
            answer, new_conversation_id, source_map = await _ask(client)
        except Exception:
            # 快取的 session 可能已經失效（連線中斷、cookie 過期）——
            # 在把錯誤呈現給使用者之前，先捨棄它並用一個全新開啟的
            # session 重試一次。
            await _invalidate_client(channel_id)
            client = await _get_cached_client(channel_id, storage_state)
            answer, new_conversation_id, source_map = await _ask(client)

        if line_user_id and new_conversation_id:
            await _save_conversation_id(channel_id, line_user_id, new_conversation_id)
        return build_answer_messages(format_for_line(answer), source_map)
    except Exception as e:
        await notify_admin(
            f"nlm:{channel_id}",
            f"⚠️ NotebookLM 查詢失敗（頻道 {channel_id}）：{e}\n"
            "常見原因是登入 cookie 失效，請確認是否需要重新 notebooklm login。",
        )
        return [f"⚠️ 查詢失敗：{e}"]

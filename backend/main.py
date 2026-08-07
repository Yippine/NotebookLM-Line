import asyncio
import logging
from datetime import datetime, timezone
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager
from database import init_db, DB
import aiosqlite
import os

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def cleanup_expired():
    """刪除已過期的 channel，並重設其邀請碼。"""
    async with aiosqlite.connect(DB) as db:
        now = datetime.now(timezone.utc).isoformat()
        # 找出已過期的 channel
        cur = await db.execute(
            "SELECT channel_id FROM channels WHERE expires_at IS NOT NULL AND expires_at < ?",
            (now,),
        )
        expired = [r[0] for r in await cur.fetchall()]

        if expired:
            for cid in expired:
                await db.execute("DELETE FROM channels WHERE channel_id=?", (cid,))
                await db.execute("DELETE FROM invite_codes WHERE channel_id=?", (cid,))
            await db.commit()
            logger.info(f"Cleaned up {len(expired)} expired channels: {expired}")


async def expiry_scheduler():
    """背景任務：每 60 秒檢查一次是否有已過期的 channel。"""
    while True:
        try:
            await cleanup_expired()
        except Exception as e:
            logger.error(f"Expiry cleanup error: {e}")
        await asyncio.sleep(60)


async def conversation_retention_scheduler():
    """背景任務：每天清除超過保留期限的對話紀錄（追蹤表的列與
    Drive 上的問答檔案）。"""
    from services import google_log_service

    while True:
        try:
            result = await google_log_service.cleanup_old_conversation_records()
            if result["deleted_rows"] or result["deleted_files"]:
                logger.info(f"Conversation retention cleanup: {result}")
        except Exception as e:
            logger.error(f"Conversation retention cleanup error: {e}")
        await asyncio.sleep(24 * 60 * 60)


async def nlm_health_scheduler():
    """背景任務：每隔幾個小時主動探測每個已綁定 channel 的 NotebookLM
    session，讓失效的登入 cookie 能在學生的提問碰到它之前
    就被發現並發出告警。"""
    from services.nlm_service import check_all_channels_health

    while True:
        try:
            await check_all_channels_health()
        except Exception as e:
            logger.error(f"NotebookLM health check error: {e}")
        await asyncio.sleep(4 * 60 * 60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    expiry_task = asyncio.create_task(expiry_scheduler())
    retention_task = asyncio.create_task(conversation_retention_scheduler())
    nlm_health_task = asyncio.create_task(nlm_health_scheduler())
    yield
    expiry_task.cancel()
    retention_task.cancel()
    nlm_health_task.cancel()
    from services.line_service import aclose_client
    from services.nlm_service import aclose_all_clients
    await aclose_client()
    await aclose_all_clients()


app = FastAPI(title="NotebookLM LINE Bot Platform", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

from routers import admin, auth, webhook

app.include_router(admin.router, prefix="/api")
app.include_router(auth.router, prefix="/api")
app.include_router(webhook.router)

# 如果存在則提供 React 建置後的檔案
static_dir = os.path.join(os.path.dirname(__file__), "static")
if not os.path.isdir(static_dir):
    static_dir = os.path.join(os.path.dirname(__file__), "..", "frontend", "dist")
if os.path.isdir(static_dir):
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="frontend")
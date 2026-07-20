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
    """Delete expired channels and reset their invite codes."""
    async with aiosqlite.connect(DB) as db:
        now = datetime.now(timezone.utc).isoformat()
        # Find expired channels
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
    """Background task: check for expired channels every 60 seconds."""
    while True:
        try:
            await cleanup_expired()
        except Exception as e:
            logger.error(f"Expiry cleanup error: {e}")
        await asyncio.sleep(60)


async def conversation_retention_scheduler():
    """Background task: purge conversation records (tracking-sheet rows and
    Drive Q&A files) older than the retention window, once a day."""
    from services import google_log_service

    while True:
        try:
            result = await google_log_service.cleanup_old_conversation_records()
            if result["deleted_rows"] or result["deleted_files"]:
                logger.info(f"Conversation retention cleanup: {result}")
        except Exception as e:
            logger.error(f"Conversation retention cleanup error: {e}")
        await asyncio.sleep(24 * 60 * 60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    expiry_task = asyncio.create_task(expiry_scheduler())
    retention_task = asyncio.create_task(conversation_retention_scheduler())
    yield
    expiry_task.cancel()
    retention_task.cancel()
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

# Serve React build if exists
static_dir = os.path.join(os.path.dirname(__file__), "static")
if not os.path.isdir(static_dir):
    static_dir = os.path.join(os.path.dirname(__file__), "..", "frontend", "dist")
if os.path.isdir(static_dir):
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="frontend")
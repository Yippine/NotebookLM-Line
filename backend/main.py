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


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    task = asyncio.create_task(expiry_scheduler())
    yield
    task.cancel()


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

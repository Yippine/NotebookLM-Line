import asyncio
import logging
import logging.handlers
from datetime import datetime, timezone
from pathlib import Path
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager
from database import init_db, DB
from config import settings
import aiosqlite
import os

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

# `docker logs` 只保留「當前這個容器實例」的輸出——容器一旦被重新
# 建立（不管是手動部署、還是機器睡眠喚醒後 Docker Desktop 重啟
# 容器），前面的紀錄就整個消失，2026-08-11/12 好幾次事後追查都因此
# 卡住。額外把 log 寫進一份會隨每日輪替、但保留歷史的檔案，放在
# 跟 DB 同一個目錄下（也就是 docker-compose.yml 掛的 ./data volume，
# 不會隨容器重建而消失）。StreamHandler 仍然保留，`docker logs`
# 即時查看的體驗不受影響，這只是多一份不會消失的副本。
_log_dir = Path(DB).parent / "logs"
_log_dir.mkdir(parents=True, exist_ok=True)
_file_handler = logging.handlers.TimedRotatingFileHandler(
    _log_dir / "backend.log",
    when="midnight",
    backupCount=30,
    encoding="utf-8",
)
_file_handler.setFormatter(logging.Formatter(_LOG_FORMAT))

logging.basicConfig(
    level=logging.INFO,
    format=_LOG_FORMAT,
    handlers=[logging.StreamHandler(), _file_handler],
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


async def nlm_health_scheduler():
    """背景任務：每半小時主動探測每個已綁定 channel 的 NotebookLM
    session，讓失效的登入 cookie 能在學生的提問碰到它之前
    就被發現並發出告警。"""
    from services.nlm_service import check_all_channels_health

    while True:
        try:
            await check_all_channels_health()
        except Exception as e:
            logger.error(f"NotebookLM health check error: {e}")
        await asyncio.sleep(30 * 60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    expiry_task = asyncio.create_task(expiry_scheduler())
    if settings.nlm_health_scheduler_enabled:
        nlm_health_task = asyncio.create_task(nlm_health_scheduler())
    else:
        nlm_health_task = None
        logger.warning("NLM health scheduler is disabled via NLM_HEALTH_SCHEDULER_ENABLED=false")
    yield
    expiry_task.cancel()
    if nlm_health_task is not None:
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
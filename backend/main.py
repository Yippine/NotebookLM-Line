import asyncio
from contextlib import asynccontextmanager, suppress
import hashlib
import logging
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from database import get_db_path, init_db
import aiosqlite
import os

from config import settings
from services.course_account_service import course_account_service
from services.crypto_service import ensure_encryption_ready
from services.logging_service import configure_sensitive_logging
from services.security_middleware import (
    HighRiskRateLimitMiddleware,
    RequestSecurityMiddleware,
)
from routers import admin, auth, webhook

logger = logging.getLogger(__name__)
configure_sensitive_logging()


async def cleanup_expired():
    """Permanently remove only learner bindings whose expiry has elapsed."""
    async with aiosqlite.connect(get_db_path()) as db:
        now = datetime.now(timezone.utc).isoformat()
        await db.execute("BEGIN IMMEDIATE")
        cur = await db.execute(
            "SELECT channel_id FROM channels WHERE expires_at IS NOT NULL AND expires_at < ?",
            (now,),
        )
        expired_channels = [row[0] for row in await cur.fetchall()]

        for channel_id in expired_channels:
            # Setup sessions reference invite codes, so remove them first even
            # on SQLite connections where foreign-key cascades are disabled.
            await db.execute(
                """
                DELETE FROM setup_sessions
                WHERE channel_id=? OR invite_code IN (
                    SELECT code FROM invite_codes WHERE channel_id=?
                )
                """,
                (channel_id, channel_id),
            )
            await db.execute(
                "DELETE FROM invite_codes WHERE channel_id=?", (channel_id,)
            )
            await db.execute(
                "DELETE FROM line_webhook_events WHERE tenant_key=?",
                (hashlib.sha256(channel_id.encode("utf-8")).hexdigest(),),
            )
            await db.execute("DELETE FROM channels WHERE channel_id=?", (channel_id,))

        if expired_channels:
            # The stored date is only the last batch selection shown in the
            # admin UI. Clear it once no remaining Channel uses that date.
            await db.execute(
                """
                UPDATE course_settings
                SET course_expires_at=NULL, updated_at=?
                WHERE id=1 AND course_expires_at IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM channels
                      WHERE expires_at=course_settings.course_expires_at
                  )
                """,
                (now,),
            )
            logger.info(
                "Course expiry cleanup removed %d learner bindings",
                len(expired_channels),
            )
        event_cutoff = (
            datetime.now(timezone.utc)
            - timedelta(seconds=settings.line_event_dedupe_retention_seconds)
        ).isoformat()
        await db.execute(
            """
            DELETE FROM line_webhook_events
            WHERE status='completed'
              AND COALESCE(completed_at, received_at) < ?
            """,
            (event_cutoff,),
        )
        await db.commit()


async def expiry_scheduler():
    """Background task: enforce course expiry every 60 seconds."""
    while True:
        try:
            await cleanup_expired()
        except Exception:
            logger.exception("Expiry cleanup failed")
        await asyncio.sleep(60)


async def course_account_health_scheduler():
    """Periodically refresh the non-sensitive centralized account status."""

    while True:
        await asyncio.sleep(settings.course_account_health_check_interval_seconds)
        try:
            status = await course_account_service.check_health()
            logger.info(
                "course_account_health outcome=%s",
                status.get("health_status", "error"),
            )
        except Exception as error:
            logger.error(
                "course_account_health outcome=failed error_type=%s",
                type(error).__name__,
            )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Uvicorn installs its access-log handlers after importing the app, so run
    # this again here to attach the redaction filter to those final handlers.
    configure_sensitive_logging()
    settings.validate_runtime()
    ensure_encryption_ready()
    await init_db()
    recovered_claims = await webhook.recover_pending_event_claims()
    if recovered_claims:
        logger.warning("line_event_recovery released_claims=%d", recovered_claims)
    webhook.start_background_processing()
    replayed_events = await webhook.replay_due_pending_events()
    if replayed_events:
        logger.warning("line_event_recovery scheduled_events=%d", replayed_events)
    tasks = [
        asyncio.create_task(expiry_scheduler(), name="expiry-scheduler"),
        asyncio.create_task(
            course_account_health_scheduler(), name="course-account-health-scheduler"
        ),
        asyncio.create_task(
            webhook.replay_pending_event_scheduler(),
            name="line-event-replay-scheduler",
        ),
    ]
    try:
        yield
    finally:
        drained = await webhook.drain_background_tasks(
            settings.line_background_shutdown_timeout_seconds
        )
        logger.info(
            "line_background_shutdown completed=%d cancelled=%d",
            drained["completed"],
            drained["cancelled"],
        )
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task


app = FastAPI(title="NotebookLM LINE Bot Platform", lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def validation_error_response(
    request: Request, _error: RequestValidationError
) -> JSONResponse:
    """Never reflect rejected passwords, tokens, or authorization JSON."""

    request_id = getattr(request.state, "request_id", "-")
    return JSONResponse(
        status_code=422,
        content={"detail": "輸入資料格式不正確", "request_id": request_id},
        headers={"X-Request-ID": request_id},
    )


# Middleware is added inside-out by Starlette. Add CORS last so allowed origins
# also receive CORS headers for security-middleware 400/429 responses.
app.add_middleware(HighRiskRateLimitMiddleware)
app.add_middleware(RequestSecurityMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allowed_origins_list,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
)

app.include_router(admin.router, prefix="/api")
app.include_router(auth.router, prefix="/api")
app.include_router(webhook.router)


@app.get("/health", include_in_schema=False, response_model=None)
async def health():
    """Non-sensitive readiness check used by the V2 deployment."""

    try:
        async with aiosqlite.connect(get_db_path()) as db:
            await db.execute("SELECT 1")
    except Exception:
        logger.error("health_check outcome=database_unavailable")
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    return {"status": "ok"}


# Serve React build if exists
static_dir = os.path.join(os.path.dirname(__file__), "static")
if not os.path.isdir(static_dir):
    static_dir = os.path.join(os.path.dirname(__file__), "..", "frontend", "dist")
if os.path.isdir(static_dir):
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="frontend")

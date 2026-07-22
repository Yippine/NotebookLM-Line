from __future__ import annotations

import logging
import re
import time
import uuid

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from config import settings
from services.logging_service import request_id_var
from services.rate_limit_service import (
    category_for_request,
    limiter,
    request_ip,
    request_subject,
)

logger = logging.getLogger(__name__)

SECRET_QUERY_KEYS = frozenset(
    {
        "token",
        "setup_token",
        "admin_token",
        "admin_password",
        "password",
        "channel_secret",
        "channel_access_token",
        "access_token",
        "authorization",
    }
)
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _request_id(request: Request) -> str:
    candidate = request.headers.get("x-request-id", "")
    if _SAFE_REQUEST_ID.fullmatch(candidate):
        return candidate
    return uuid.uuid4().hex


class RequestSecurityMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        request_id = _request_id(request)
        request.state.request_id = request_id
        context_token = request_id_var.set(request_id)
        started = time.monotonic()
        status_code = 500
        try:
            query_keys = {key.lower() for key in request.query_params.keys()}
            if query_keys & SECRET_QUERY_KEYS:
                status_code = 400
                return JSONResponse(
                    status_code=400,
                    content={
                        "detail": "敏感資料不得放在網址，請使用 Authorization Header 或 request body",
                        "request_id": request_id,
                    },
                    headers={"X-Request-ID": request_id},
                )

            response = await call_next(request)
            status_code = response.status_code
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            duration_ms = (time.monotonic() - started) * 1000
            # Only method/path/status are logged. Query strings and bodies never
            # enter application access logs.
            logger.info(
                "request method=%s path=%s status=%s request_id=%s duration_ms=%.1f",
                request.method,
                request.url.path,
                status_code,
                request_id,
                duration_ms,
            )
            request_id_var.reset(context_token)


class HighRiskRateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        category = category_for_request(request.method, request.url.path)
        if category:
            limit = {
                "invite": settings.rate_limit_invite_attempts,
                "admin_login": settings.rate_limit_admin_login_attempts,
                "channel_update": settings.rate_limit_channel_updates,
                "notebook_binding": settings.rate_limit_notebook_bindings,
                "course_account_reauth": settings.rate_limit_course_account_reauth,
            }[category]
            subjects = [(f"{category}:ip", request_ip(request))]
            if category not in {"invite", "admin_login"}:
                session_subject = request_subject(request)
                if session_subject != subjects[0][1]:
                    subjects.append((f"{category}:session", session_subject))
            for bucket_category, bucket_subject in subjects:
                decision = await limiter.check(
                    bucket_category,
                    bucket_subject,
                    limit=limit,
                    window_seconds=settings.rate_limit_window_seconds,
                )
                if not decision.allowed:
                    request_id = getattr(request.state, "request_id", uuid.uuid4().hex)
                    return JSONResponse(
                        status_code=429,
                        content={
                            "detail": "操作過於頻繁，請稍後再試",
                            "request_id": request_id,
                        },
                        headers={
                            "Retry-After": str(decision.retry_after_seconds),
                            "X-Request-ID": request_id,
                        },
                    )
        return await call_next(request)

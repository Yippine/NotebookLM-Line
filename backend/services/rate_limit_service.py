from __future__ import annotations

import asyncio
import hashlib
import math
import time
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass

from fastapi import HTTPException, Request

from config import settings


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    retry_after_seconds: int = 0


class InMemoryRateLimiter:
    """A bounded-process limiter suitable for the current single API service.

    Persistent authentication state is stored in SQLite. Rate-limit state is
    intentionally short lived; multi-instance deployment should replace this
    implementation with a shared backend while keeping the same interface.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_buckets: int = 10_000,
    ) -> None:
        self._clock = clock
        self._max_buckets = max_buckets
        self._events: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def check(
        self,
        category: str,
        subject: str,
        *,
        limit: int,
        window_seconds: int,
    ) -> RateLimitDecision:
        if limit <= 0 or window_seconds <= 0:
            return RateLimitDecision(allowed=True)

        now = self._clock()
        cutoff = now - window_seconds
        key = (category, subject)
        async with self._lock:
            bucket = self._events.get(key)
            if bucket is None:
                if len(self._events) >= self._max_buckets:
                    stale_keys = [
                        candidate
                        for candidate, events in self._events.items()
                        if not events or events[-1] <= cutoff
                    ]
                    for stale in stale_keys:
                        self._events.pop(stale, None)
                if len(self._events) >= self._max_buckets:
                    # Fail closed for never-seen subjects rather than allowing
                    # attacker-controlled identities to grow memory forever.
                    return RateLimitDecision(False, max(1, window_seconds))
                bucket = deque()
                self._events[key] = bucket
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= limit:
                retry_after = max(1, math.ceil(window_seconds - (now - bucket[0])))
                return RateLimitDecision(False, retry_after)
            bucket.append(now)

        return RateLimitDecision(allowed=True)

    async def reset(self) -> None:
        async with self._lock:
            self._events.clear()


limiter = InMemoryRateLimiter()


def _limit_for(category: str) -> int:
    limits = {
        "invite": settings.rate_limit_invite_attempts,
        "admin_login": settings.rate_limit_admin_login_attempts,
        "channel_update": settings.rate_limit_channel_updates,
        "notebook_binding": settings.rate_limit_notebook_bindings,
        "course_account_reauth": settings.rate_limit_course_account_reauth,
    }
    return limits[category]


def request_ip(request: Request) -> str:
    forwarded = (
        request.headers.get("x-forwarded-for") if settings.trust_proxy_headers else None
    )
    if forwarded:
        return forwarded.split(",", 1)[0].strip()
    return request.client.host if request.client else "unknown"


def request_subject(request: Request, *, include_authorization: bool = True) -> str:
    ip = request_ip(request)
    authorization = request.headers.get("authorization", "")
    if include_authorization and authorization:
        fingerprint = hashlib.sha256(authorization.encode("utf-8")).hexdigest()[:16]
        return f"{ip}:{fingerprint}"
    return ip


async def enforce_rate_limit(
    request: Request,
    category: str,
    *,
    subject: str | None = None,
) -> None:
    subjects = [(f"{category}:ip", request_ip(request))]
    session_subject = subject or request_subject(request)
    if session_subject != subjects[0][1]:
        subjects.append((f"{category}:session", session_subject))
    for bucket_category, bucket_subject in subjects:
        decision = await limiter.check(
            bucket_category,
            bucket_subject,
            limit=_limit_for(category),
            window_seconds=settings.rate_limit_window_seconds,
        )
        if not decision.allowed:
            raise HTTPException(
                status_code=429,
                detail="操作過於頻繁，請稍後再試",
                headers={"Retry-After": str(decision.retry_after_seconds)},
            )


def category_for_request(method: str, path: str) -> str | None:
    normalized_method = method.upper()
    if normalized_method == "POST" and path == "/api/verify-invite":
        return "invite"
    if normalized_method == "POST" and path == "/api/admin/login":
        return "admin_login"
    if normalized_method in {"POST", "PUT", "PATCH"}:
        if path == "/api/channels" or path.endswith("/channel"):
            return "channel_update"
        # Notebook binding and course-account routes call enforce_rate_limit()
        # with the authenticated session ID, which is stronger than a generic
        # pre-auth middleware subject and avoids double counting.
    return None

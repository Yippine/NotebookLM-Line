from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi import FastAPI

from config import settings
from routers import auth
from services.session_service import (
    AdminPrincipal,
    SetupPrincipal,
    require_admin_session,
    require_setup_session,
)


def make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(auth.router, prefix="/api")
    return app


@pytest.mark.anyio
async def test_shared_binding_routes_reject_missing_bearer():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=make_app()), base_url="http://test"
    ) as client:
        public = await client.get("/api/course-account/public")
        binding = await client.get("/api/channels/channel-a/notebook-binding")

    assert public.status_code == 401
    assert binding.status_code == 401


@pytest.mark.anyio
async def test_channel_scope_rejects_cross_channel_access_without_lookup():
    app = make_app()
    app.dependency_overrides[require_setup_session] = lambda: SetupPrincipal(
        session_id=1,
        invite_code="invite-a",
        channel_id="channel-a",
        scope="channel:setup",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/channels/channel-b/notebook-binding")

    assert response.status_code == 403
    assert "Channel 不存在" not in response.text


@pytest.mark.anyio
async def test_legacy_cookie_endpoints_are_admin_only_and_flag_hidden(monkeypatch):
    app = make_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        unauthenticated = await client.post(
            "/api/channels/channel-a/nlm-login",
            json={"storage_state_json": {"cookies": []}},
        )
    assert unauthenticated.status_code == 401

    app.dependency_overrides[require_admin_session] = lambda: AdminPrincipal(
        session_id=2,
        scope="admin",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    monkeypatch.setattr(settings, "legacy_nlm_binding_enabled", False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        disabled = await client.post(
            "/api/channels/channel-a/nlm-login",
            json={"storage_state_json": {"cookies": []}},
        )
        local_disabled = await client.post("/api/channels/channel-a/nlm-bind-local")

    assert disabled.status_code == 404
    assert local_disabled.status_code == 404


@pytest.mark.anyio
async def test_admin_course_account_route_requires_admin_not_setup_principal():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=make_app()), base_url="http://test"
    ) as client:
        response = await client.get("/api/admin/course-account")
    assert response.status_code == 401

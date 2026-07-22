from __future__ import annotations

import httpx
import pytest


@pytest.mark.anyio
async def test_health_is_public_and_non_sensitive(asgi_app) -> None:
    transport = httpx.ASGITransport(app=asgi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

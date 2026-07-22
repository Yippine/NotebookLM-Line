from __future__ import annotations

import uuid

import httpx
import pytest

from services import line_service


class _FakeAsyncClient:
    def __init__(self, response: httpx.Response, calls: list[dict]) -> None:
        self.response = response
        self.calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, url: str, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return self.response


@pytest.mark.anyio
async def test_show_loading_uses_official_start_endpoint_and_accepts_202(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []
    response = httpx.Response(
        202,
        request=httpx.Request(
            "POST", "https://api.line.me/v2/bot/chat/loading/start"
        ),
    )
    monkeypatch.setattr(
        line_service.httpx,
        "AsyncClient",
        lambda: _FakeAsyncClient(response, calls),
    )

    assert await line_service.show_loading("line-user", "access-token", 60)
    assert calls == [
        {
            "url": "https://api.line.me/v2/bot/chat/loading/start",
            "headers": {"Authorization": "Bearer access-token"},
            "json": {"chatId": "line-user", "loadingSeconds": 60},
        }
    ]


@pytest.mark.anyio
async def test_push_retry_key_header_and_accepted_409_are_idempotent_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []
    response = httpx.Response(
        409,
        headers={"X-Line-Accepted-Request-Id": "accepted-request-id"},
        request=httpx.Request("POST", "https://api.line.me/v2/bot/message/push"),
    )
    monkeypatch.setattr(
        line_service.httpx,
        "AsyncClient",
        lambda: _FakeAsyncClient(response, calls),
    )
    retry_key = str(uuid.uuid4())

    await line_service.push_text(
        "line-user",
        "access-token",
        "answer",
        retry_key=retry_key,
    )

    assert calls[0]["headers"]["X-Line-Retry-Key"] == retry_key
    assert calls[0]["headers"]["Authorization"] == "Bearer access-token"


@pytest.mark.anyio
async def test_push_409_without_accepted_request_id_remains_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = httpx.Response(
        409,
        request=httpx.Request("POST", "https://api.line.me/v2/bot/message/push"),
    )
    monkeypatch.setattr(
        line_service.httpx,
        "AsyncClient",
        lambda: _FakeAsyncClient(response, []),
    )

    with pytest.raises(httpx.HTTPStatusError):
        await line_service.push_text(
            "line-user",
            "access-token",
            "answer",
            retry_key=str(uuid.uuid4()),
        )


@pytest.mark.anyio
async def test_push_rejects_non_uuid_retry_key_before_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []
    response = httpx.Response(
        200,
        request=httpx.Request("POST", "https://api.line.me/v2/bot/message/push"),
    )
    monkeypatch.setattr(
        line_service.httpx,
        "AsyncClient",
        lambda: _FakeAsyncClient(response, calls),
    )

    with pytest.raises(ValueError, match="UUID"):
        await line_service.push_text(
            "line-user",
            "access-token",
            "answer",
            retry_key="not-a-uuid",
        )
    assert calls == []

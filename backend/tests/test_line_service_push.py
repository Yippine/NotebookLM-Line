import asyncio

import httpx
import pytest

import services.line_service as line_service


class _FakeResponse:
    def __init__(self, status_code, text="", headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}


def _patch_post(monkeypatch, responses):
    """讓 httpx.AsyncClient.post 依序回傳 ``responses`` 中的每一個回應。"""
    calls = []

    async def fake_post(self, url, headers=None, json=None):
        calls.append((url, json))
        return responses[len(calls) - 1]

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    return calls


def test_push_text_succeeds_silently_on_200(monkeypatch):
    calls = _patch_post(monkeypatch, [_FakeResponse(200)])
    asyncio.run(line_service.push_text("user-1", "token", "hello"))
    assert len(calls) == 1


def test_push_text_retries_once_after_429_then_succeeds(monkeypatch):
    calls = _patch_post(monkeypatch, [
        _FakeResponse(429, headers={"Retry-After": "0"}),
        _FakeResponse(200),
    ])

    async def no_sleep(_seconds):
        pass
    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    asyncio.run(line_service.push_text("user-1", "token", "hello"))
    assert len(calls) == 2


def test_push_text_raises_when_still_failing_after_retry(monkeypatch):
    _patch_post(monkeypatch, [
        _FakeResponse(429, headers={"Retry-After": "0"}),
        _FakeResponse(429, headers={"Retry-After": "0"}),
    ])

    async def no_sleep(_seconds):
        pass
    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    with pytest.raises(RuntimeError):
        asyncio.run(line_service.push_text("user-1", "token", "hello"))


def test_push_text_raises_on_non_retryable_error_without_swallowing_it(monkeypatch):
    _patch_post(monkeypatch, [_FakeResponse(400, text="bad request")])

    with pytest.raises(RuntimeError, match="400"):
        asyncio.run(line_service.push_text("user-1", "token", "hello"))

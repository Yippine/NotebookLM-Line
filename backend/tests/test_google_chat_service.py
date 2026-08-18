import asyncio

import httpx
import pytest

import services.google_chat_service as google_chat_service


class _FakeResponse:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


def _patch_post(monkeypatch, responses):
    """讓 httpx.AsyncClient.post 依序回傳 ``responses`` 中的每一個回應。"""
    calls = []

    async def fake_post(self, url, json=None):
        calls.append((url, json))
        return responses[len(calls) - 1]

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    return calls


def test_send_message_posts_text_payload_to_webhook_url(monkeypatch):
    calls = _patch_post(monkeypatch, [_FakeResponse(200)])

    asyncio.run(google_chat_service.send_message("https://chat.googleapis.com/v1/spaces/x/messages?key=y", "hello"))

    assert calls == [("https://chat.googleapis.com/v1/spaces/x/messages?key=y", {"text": "hello"})]


def test_send_message_raises_on_non_200_without_swallowing_it(monkeypatch):
    _patch_post(monkeypatch, [_FakeResponse(400, text="bad request")])

    with pytest.raises(RuntimeError, match="400"):
        asyncio.run(google_chat_service.send_message("https://chat.googleapis.com/v1/spaces/x/messages?key=y", "hello"))

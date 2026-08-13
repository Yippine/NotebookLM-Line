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


def test_show_loading_hits_the_correct_endpoint_with_start_suffix(monkeypatch):
    """回歸測試：實際發生過的真實案例——這個端點原本漏了結尾的
    "/start"，打的其實是 `/v2/bot/chat/loading`（官方文件上的正確
    路徑是 `/v2/bot/chat/loading/start`），導致每次呼叫都收到 404，
    1:1 聊天的載入動畫從未真正顯示過，卻因為包在 best-effort 的
    try/except 裡而一直靜默失敗、沒被發現。"""
    calls = _patch_post(monkeypatch, [_FakeResponse(202)])

    result = asyncio.run(line_service.show_loading("user-1", "token"))

    assert result is True
    assert len(calls) == 1
    url, payload = calls[0]
    assert url == "https://api.line.me/v2/bot/chat/loading/start"
    assert payload == {"chatId": "user-1", "loadingSeconds": 20}


def test_show_loading_treats_202_as_success_not_only_200(monkeypatch):
    """回歸測試：實際發生過的真實案例——修對端點路徑之後，這個
    端點實測回傳的是 202（配空的 "{}"），不是像大多數端點那樣回
    200，原本的判斷只認 200，把每一次真正成功的呼叫都當成失敗。"""
    _patch_post(monkeypatch, [_FakeResponse(202, text="{}")])

    result = asyncio.run(line_service.show_loading("user-1", "token"))

    assert result is True


def test_show_loading_returns_false_on_genuine_failure_without_raising(monkeypatch):
    _patch_post(monkeypatch, [_FakeResponse(404, text="Not Found")])

    result = asyncio.run(line_service.show_loading("user-1", "token"))

    assert result is False

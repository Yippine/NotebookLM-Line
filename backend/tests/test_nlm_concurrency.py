import asyncio
import os
import time

import pytest

import services.nlm_service as nlm_service


class _FakeClient:
    def __init__(self, auth):
        self.auth = auth


class _FakeStorageContext:
    """用來替代 notebooklm.NotebookLMClient.from_storage()。

    模擬真實函式庫在認證交握上的行為：
    NOTEBOOKLM_AUTH_JSON 只會在 __aenter__ 中被讀取一次。
    """

    async def __aenter__(self):
        raw = os.environ.get("NOTEBOOKLM_AUTH_JSON")
        return _FakeClient(auth=raw)

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return None


class _FakeNotebookLMClient:
    @staticmethod
    def from_storage():
        return _FakeStorageContext()


@pytest.fixture(autouse=True)
def fake_notebooklm_client(monkeypatch):
    monkeypatch.setattr(nlm_service, "NotebookLMClient", _FakeNotebookLMClient)


def test_concurrent_channels_do_not_cross_contaminate_auth_and_run_in_parallel():
    """兩個 channel 各自緩慢的操作，彼此的認證資訊不能互相污染，
    而且彼此不應該序列化排隊執行（只有交握本身才應該序列化）。"""

    async def slow_op(client, sleep_for: float):
        # 在交握鎖釋放*之後*才執行。如果鎖仍然涵蓋這段，
        # 兩個並行呼叫就會需要 sleep_for * 2 的時間。
        await asyncio.sleep(sleep_for)
        return client.auth

    async def run_for_channel(channel_id: str, sleep_for: float):
        return await nlm_service._run_with_auth(
            {"channel": channel_id},
            lambda client: slow_op(client, sleep_for),
        )

    async def main():
        start = time.monotonic()
        results = await asyncio.gather(
            run_for_channel("A", 0.3),
            run_for_channel("B", 0.3),
        )
        elapsed = time.monotonic() - start
        return results, elapsed

    results, elapsed = asyncio.run(main())

    assert results[0] == '{"channel": "A"}'
    assert results[1] == '{"channel": "B"}'
    # 若被同一把鎖序列化：約需 0.6 秒。並行執行緩慢部分：約需 0.3 秒。
    assert elapsed < 0.5, f"expected concurrent execution, took {elapsed:.2f}s"


def test_env_var_restored_after_call():
    async def quick_op(client):
        return client.auth

    os.environ["NOTEBOOKLM_AUTH_JSON"] = "pre-existing-value"
    try:
        asyncio.run(nlm_service._run_with_auth({"channel": "A"}, quick_op))
        assert os.environ["NOTEBOOKLM_AUTH_JSON"] == "pre-existing-value"
    finally:
        os.environ.pop("NOTEBOOKLM_AUTH_JSON", None)

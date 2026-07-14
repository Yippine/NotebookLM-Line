import asyncio
import os
import time

import pytest

import services.nlm_service as nlm_service


class _FakeClient:
    def __init__(self, auth):
        self.auth = auth


class _FakeStorageContext:
    """Stands in for notebooklm.NotebookLMClient.from_storage().

    Mirrors the real library's behavior of the auth handshake:
    NOTEBOOKLM_AUTH_JSON is only read once, in __aenter__.
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
    """Two channels' slow operations must not corrupt each other's auth,
    and must not serialize behind one another (only the handshake should)."""

    async def slow_op(client, sleep_for: float):
        # Runs *after* the handshake lock is released. If the lock still
        # covered this, two concurrent calls would take sleep_for * 2.
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
    # Serialized behind one lock: ~0.6s. Parallel slow parts: ~0.3s.
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

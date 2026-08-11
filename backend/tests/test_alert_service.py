import asyncio

import services.alert_service as alert_service
from config import settings


def _configure_admin(monkeypatch):
    monkeypatch.setattr(settings, "admin_line_user_id", "U-admin")
    monkeypatch.setattr(settings, "admin_alert_access_token", "token-123")
    monkeypatch.setattr(alert_service, "_last_sent", {})


def test_noop_when_admin_alerting_not_configured(monkeypatch):
    monkeypatch.setattr(settings, "admin_line_user_id", "")
    monkeypatch.setattr(settings, "admin_alert_access_token", "")

    calls = []

    async def fake_push_text(user_id, access_token, text):
        calls.append((user_id, access_token, text))

    monkeypatch.setattr(alert_service, "push_text", fake_push_text)

    asyncio.run(alert_service.notify_admin("some-key", "should not send"))

    assert calls == []


def test_sends_push_when_configured(monkeypatch):
    _configure_admin(monkeypatch)

    calls = []

    async def fake_push_text(user_id, access_token, text):
        calls.append((user_id, access_token, text))

    monkeypatch.setattr(alert_service, "push_text", fake_push_text)

    asyncio.run(alert_service.notify_admin("some-key", "tunnel is down"))

    assert calls == [("U-admin", "token-123", "tunnel is down")]


def test_repeated_alerts_for_same_key_are_collapsed_by_cooldown(monkeypatch):
    _configure_admin(monkeypatch)

    calls = []

    async def fake_push_text(user_id, access_token, text):
        calls.append(text)

    monkeypatch.setattr(alert_service, "push_text", fake_push_text)

    asyncio.run(alert_service.notify_admin("nlm:channel-1", "first failure"))
    asyncio.run(alert_service.notify_admin("nlm:channel-1", "second failure, same key"))

    assert calls == ["first failure"]


def test_different_keys_are_not_collapsed(monkeypatch):
    _configure_admin(monkeypatch)

    calls = []

    async def fake_push_text(user_id, access_token, text):
        calls.append(text)

    monkeypatch.setattr(alert_service, "push_text", fake_push_text)

    asyncio.run(alert_service.notify_admin("nlm:channel-1", "channel 1 failure"))
    asyncio.run(alert_service.notify_admin("nlm:channel-2", "channel 2 failure"))

    assert calls == ["channel 1 failure", "channel 2 failure"]


def test_push_failure_is_swallowed_not_raised(monkeypatch):
    _configure_admin(monkeypatch)

    async def failing_push_text(user_id, access_token, text):
        raise RuntimeError("LINE API is down")

    monkeypatch.setattr(alert_service, "push_text", failing_push_text)

    # 即使底層的 push 失敗，也不應該拋出例外。
    asyncio.run(alert_service.notify_admin("some-key", "message"))


def test_sends_to_multiple_admins_when_comma_separated(monkeypatch):
    monkeypatch.setattr(settings, "admin_line_user_id", "U-admin1, U-admin2 ,U-admin3")
    monkeypatch.setattr(settings, "admin_alert_access_token", "token-123")
    monkeypatch.setattr(alert_service, "_last_sent", {})

    calls = []

    async def fake_push_text(user_id, access_token, text):
        calls.append((user_id, text))

    monkeypatch.setattr(alert_service, "push_text", fake_push_text)

    asyncio.run(alert_service.notify_admin("some-key", "tunnel is down"))

    assert sorted(calls) == [
        ("U-admin1", "tunnel is down"),
        ("U-admin2", "tunnel is down"),
        ("U-admin3", "tunnel is down"),
    ]


def test_one_admins_push_failure_does_not_block_the_others(monkeypatch):
    monkeypatch.setattr(settings, "admin_line_user_id", "U-good1,U-bad,U-good2")
    monkeypatch.setattr(settings, "admin_alert_access_token", "token-123")
    monkeypatch.setattr(alert_service, "_last_sent", {})

    calls = []

    async def fake_push_text(user_id, access_token, text):
        if user_id == "U-bad":
            raise RuntimeError("this user unfriended the bot")
        calls.append(user_id)

    monkeypatch.setattr(alert_service, "push_text", fake_push_text)

    # 不應拋出例外，且另外兩個人仍要收到。
    asyncio.run(alert_service.notify_admin("some-key", "message"))

    assert sorted(calls) == ["U-good1", "U-good2"]


def test_cooldown_seconds_override_bypasses_default_window(monkeypatch):
    """A caller with its own precise dedup (e.g. a DB-backed state
    transition) can pass cooldown_seconds=0 to send immediately even
    within the default cooldown window."""
    _configure_admin(monkeypatch)

    calls = []

    async def fake_push_text(user_id, access_token, text):
        calls.append(text)

    monkeypatch.setattr(alert_service, "push_text", fake_push_text)

    asyncio.run(alert_service.notify_admin("nlm-health:chan-1", "first"))
    asyncio.run(alert_service.notify_admin("nlm-health:chan-1", "second", cooldown_seconds=0))

    assert calls == ["first", "second"]

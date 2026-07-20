from routers.webhook import BOT_NAME, _has_text_mention, _strip_text_mention_trigger


def test_bare_at_sign_no_longer_triggers():
    """A bare '@' with no bot name (e.g. an email address, or someone
    mentioning a different LINE contact) must not be treated as a mention."""
    assert _has_text_mention("我的信箱是 test@example.com") is False
    assert _has_text_mention("@其他人 你覺得呢") is False


def test_bot_name_mention_triggers():
    assert _has_text_mention(f"@{BOT_NAME} Corolla Cross 有哪些車") is True


def test_generic_robot_mention_triggers():
    assert _has_text_mention("@機器人 你好") is True


def test_strip_text_mention_trigger_removes_bot_name():
    result = _strip_text_mention_trigger(f"@{BOT_NAME} Corolla Cross 有哪些車")
    assert result == "Corolla Cross 有哪些車"


def test_strip_text_mention_trigger_removes_generic_robot():
    result = _strip_text_mention_trigger("@機器人 你好")
    assert result == "你好"


def test_strip_text_mention_trigger_leaves_empty_string_when_only_mention():
    assert _strip_text_mention_trigger(f"@{BOT_NAME}") == ""
    assert _strip_text_mention_trigger("@機器人") == ""

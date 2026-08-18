import asyncio

import routers.webhook as webhook


def _run(coro):
    return asyncio.run(coro)


def test_five_or_fewer_messages_all_go_through_reply_only(monkeypatch):
    replied = []
    pushed = []

    async def fake_ask_question(channel_id, question, line_user_id=None):
        return [f"【廠商{i}】\n\n內容" for i in range(1, 6)]  # 剛好 5 則

    async def fake_reply_text(reply_token, access_token, text, mention_user_id=None, mention_display_name=None):
        replied.append(text)

    async def fake_push_text(target_id, access_token, text):
        pushed.append(text)

    monkeypatch.setattr(webhook, "ask_question", fake_ask_question)
    monkeypatch.setattr(webhook, "reply_text", fake_reply_text)
    monkeypatch.setattr(webhook, "push_text", fake_push_text)

    _run(webhook._ask_and_reply(
        "channel-1", "reply-token-1", "token-1", "有哪些現車",
        sender_user_id="user-1", group_id="group-1",
    ))

    assert len(replied) == 1
    assert len(replied[0]) == 5
    assert pushed == []


def test_more_than_five_messages_pushes_the_overflow_instead_of_dropping_it(monkeypatch):
    """重現實際會發生的真實案例：8 家廠商各自一則，超過 LINE reply
    一次 5 則的上限——後面 3 家不該被悄悄丟掉，改用 push 補送。"""
    replied = []
    pushed = []

    async def fake_ask_question(channel_id, question, line_user_id=None):
        return [f"【廠商{i}】\n\n內容" for i in range(1, 9)]  # 8 則

    async def fake_reply_text(reply_token, access_token, text, mention_user_id=None, mention_display_name=None):
        replied.append(text)

    async def fake_push_text(target_id, access_token, text):
        pushed.append((target_id, text))

    monkeypatch.setattr(webhook, "ask_question", fake_ask_question)
    monkeypatch.setattr(webhook, "reply_text", fake_reply_text)
    monkeypatch.setattr(webhook, "push_text", fake_push_text)

    _run(webhook._ask_and_reply(
        "channel-1", "reply-token-1", "token-1", "有哪些現車",
        sender_user_id="user-1", group_id="group-1",
    ))

    assert len(replied) == 1
    assert len(replied[0]) == 5
    assert replied[0] == ["【廠商1】\n\n內容", "【廠商2】\n\n內容", "【廠商3】\n\n內容", "【廠商4】\n\n內容", "【廠商5】\n\n內容"]

    assert len(pushed) == 3
    assert [text for _, text in pushed] == ["【廠商6】\n\n內容", "【廠商7】\n\n內容", "【廠商8】\n\n內容"]
    assert all(target_id == "group-1" for target_id, _ in pushed)  # 群組優先於個人 id


def test_overflow_push_failure_does_not_trigger_the_generic_error_fallback(monkeypatch):
    """主要回答（前 5 則）已經送出去了，超過額度上限的 push 補送
    失敗，不該讓使用者看到「系統發生錯誤」這種好像整個查詢都失敗
    的訊息——那是誤導，主要的回答其實已經收到了。"""
    replied = []

    async def fake_ask_question(channel_id, question, line_user_id=None):
        return [f"【廠商{i}】\n\n內容" for i in range(1, 8)]  # 7 則

    async def fake_reply_text(reply_token, access_token, text, mention_user_id=None, mention_display_name=None):
        replied.append(text)

    async def fake_push_text(target_id, access_token, text):
        raise RuntimeError("LINE push 失敗：429 monthly limit")

    monkeypatch.setattr(webhook, "ask_question", fake_ask_question)
    monkeypatch.setattr(webhook, "reply_text", fake_reply_text)
    monkeypatch.setattr(webhook, "push_text", fake_push_text)

    _run(webhook._ask_and_reply(
        "channel-1", "reply-token-1", "token-1", "有哪些現車",
        sender_user_id="user-1", group_id="group-1",
    ))

    # 只有一次 reply_text 呼叫（前 5 則的那次），沒有額外的
    # 「⚠️ 系統發生錯誤」備援回覆。
    assert len(replied) == 1
    assert replied[0] != "⚠️ 系統發生錯誤，請稍後再試。"


def test_overflow_with_no_usable_target_id_is_logged_not_raised(monkeypatch):
    """匿名群組成員（沒有 group_id/room_id/sender_user_id 任何一個
    可用 id）時，超過額度上限的部分沒有地方可以 push，只能記錄下來
    ——不能讓整個查詢因此被判定失敗。"""
    replied = []

    async def fake_ask_question(channel_id, question, line_user_id=None):
        return [f"【廠商{i}】\n\n內容" for i in range(1, 7)]  # 6 則

    async def fake_reply_text(reply_token, access_token, text, mention_user_id=None, mention_display_name=None):
        replied.append(text)

    monkeypatch.setattr(webhook, "ask_question", fake_ask_question)
    monkeypatch.setattr(webhook, "reply_text", fake_reply_text)

    _run(webhook._ask_and_reply(
        "channel-1", "reply-token-1", "token-1", "有哪些現車",
        sender_user_id=None, group_id=None, room_id=None,
    ))

    assert len(replied) == 1
    assert len(replied[0]) == 5


def test_expired_reply_token_after_slow_query_falls_back_to_push(monkeypatch):
    """重現實際會發生的真實案例：跨多家廠商的問題常需要 3-5 分鐘才能
    生成完，遠超過 LINE reply token 的有效期限。ask_question 已經成功
    拿到答案了，只是 reply_text 因為 token 過期而失敗——這時候不該
    用同一個（一次性、已失效的）token 再重試一次 reply，那必然還是
    會失敗，過去這樣做的結果就是使用者完全收不到任何東西。應該改用
    push 把已經生成好的答案送出去。"""
    pushed = []
    admin_alerts = []

    async def fake_ask_question(channel_id, question, line_user_id=None):
        return ["【廠商1】\n\n內容"]

    async def fake_reply_text(reply_token, access_token, text, mention_user_id=None, mention_display_name=None):
        raise RuntimeError("LINE reply 失敗：400 Invalid reply token")

    async def fake_push_text(target_id, access_token, text):
        pushed.append((target_id, text))

    async def fake_notify_admin(key, message, **kwargs):
        admin_alerts.append((key, message))

    monkeypatch.setattr(webhook, "ask_question", fake_ask_question)
    monkeypatch.setattr(webhook, "reply_text", fake_reply_text)
    monkeypatch.setattr(webhook, "push_text", fake_push_text)
    monkeypatch.setattr(webhook, "notify_admin", fake_notify_admin)

    _run(webhook._ask_and_reply(
        "channel-1", "reply-token-1", "token-1", "幫我整理所有廠商的現車",
        sender_user_id="user-1", group_id="group-1",
    ))

    assert pushed == [("group-1", "【廠商1】\n\n內容")]
    # 答案成功送達使用者了，不該再額外發一則「使用者完全沒收到回覆」的
    # 管理員告警。
    assert admin_alerts == []


def test_expired_reply_token_with_no_target_id_notifies_admin(monkeypatch):
    """匿名群組成員完全沒有可用的 id 時，reply 失敗又沒地方可以
    push——沒辦法補救，但至少要讓管理員知道這個使用者完全沒收到
    任何回覆，而不是靜默吞掉。"""
    admin_alerts = []

    async def fake_ask_question(channel_id, question, line_user_id=None):
        return ["【廠商1】\n\n內容"]

    async def fake_reply_text(reply_token, access_token, text, mention_user_id=None, mention_display_name=None):
        raise RuntimeError("LINE reply 失敗：400 Invalid reply token")

    async def fake_notify_admin(key, message, **kwargs):
        admin_alerts.append((key, message))

    monkeypatch.setattr(webhook, "ask_question", fake_ask_question)
    monkeypatch.setattr(webhook, "reply_text", fake_reply_text)
    monkeypatch.setattr(webhook, "notify_admin", fake_notify_admin)

    _run(webhook._ask_and_reply(
        "channel-1", "reply-token-1", "token-1", "幫我整理所有廠商的現車",
        sender_user_id=None, group_id=None, room_id=None,
    ))

    assert len(admin_alerts) == 1
    assert admin_alerts[0][0] == "silent:channel-1"


def test_expired_reply_token_and_push_both_failing_notifies_admin(monkeypatch):
    """reply 跟 push 補救都失敗的最壞情況——使用者依然完全收不到
    任何東西，這時候一定要通知管理員，這是唯一能發現這件事發生過的
    管道。"""
    admin_alerts = []

    async def fake_ask_question(channel_id, question, line_user_id=None):
        return ["【廠商1】\n\n內容"]

    async def fake_reply_text(reply_token, access_token, text, mention_user_id=None, mention_display_name=None):
        raise RuntimeError("LINE reply 失敗：400 Invalid reply token")

    async def fake_push_text(target_id, access_token, text):
        raise RuntimeError("LINE push 失敗：429 monthly limit")

    async def fake_notify_admin(key, message, **kwargs):
        admin_alerts.append((key, message))

    monkeypatch.setattr(webhook, "ask_question", fake_ask_question)
    monkeypatch.setattr(webhook, "reply_text", fake_reply_text)
    monkeypatch.setattr(webhook, "push_text", fake_push_text)
    monkeypatch.setattr(webhook, "notify_admin", fake_notify_admin)

    _run(webhook._ask_and_reply(
        "channel-1", "reply-token-1", "token-1", "幫我整理所有廠商的現車",
        sender_user_id="user-1", group_id="group-1",
    ))

    assert len(admin_alerts) == 1
    assert admin_alerts[0][0] == "silent:channel-1"

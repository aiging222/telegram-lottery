import asyncio
import logging
import sys
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import pytest
from telegram import ChatMember, Update
from telegram.error import BadRequest, ChatMigrated, Forbidden, TimedOut

from lottery.bot import (
    ALLOWED_UPDATES,
    GROUP_BUDGET,
    BotHandlers,
    Settings,
    TokenFilter,
    build_application,
    main,
    register_commands,
)
from lottery.core import LotteryError, Store
from lottery.points import Points
from lottery.views import card, chunks, result_text, standing_text

TOKEN = "123456:SECRET-token"
USER = {"id": 123, "is_bot": False, "first_name": "Alice"}
GROUP = {"id": -100, "type": "supergroup"}
SHANGHAI = ZoneInfo("Asia/Shanghai")


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path / "test.sqlite3")
    rid = store.create(99, "测试抽奖", 2, 60)
    return store, rid, BotHandlers(store, {99}, SHANGHAI)


def fake_bot(**methods):
    """A bot whose API calls all succeed; tests replace or inspect the ones they need."""
    names = (
        "send_message",
        "edit_message_text",
        "pin_chat_message",
        "unpin_chat_message",
        "delete_messages",
        "get_chat_member",
    )
    bot = SimpleNamespace(**{name: AsyncMock() for name in names})
    bot.send_message.return_value = SimpleNamespace(message_id=88)
    return SimpleNamespace(**vars(bot) | methods)


def command_update(text, user_id=99, chat_type="private", chat_id=GROUP["id"]):
    chat = SimpleNamespace(
        type=chat_type, id=user_id if chat_type == "private" else chat_id, title="测试群"
    )
    message = SimpleNamespace(
        text=text,
        chat=chat,
        message_id=10,
        sender_chat=None,
        reply_text=AsyncMock(return_value=SimpleNamespace(message_id=77)),
        reply_document=AsyncMock(),
    )
    update = SimpleNamespace(
        effective_message=message,
        effective_user=SimpleNamespace(id=user_id, is_bot=False),
        effective_chat=chat,
    )
    return update, SimpleNamespace(args=text.split()[1:], bot=fake_bot())


def run_command(handlers, text, user_id=99, chat_type="private", chat_id=GROUP["id"]):
    update, context = command_update(text, user_id, chat_type, chat_id)
    asyncio.run(handlers.command(update, context))
    return update.effective_message


def member(status, **fields):
    return AsyncMock(return_value=SimpleNamespace(status=status, **fields))


def join_click(handlers, rid, get_chat_member, answer=None):
    query = SimpleNamespace(
        data=f"join:{rid}",
        answer=answer or AsyncMock(),
        from_user=SimpleNamespace(id=123, full_name="Alice", is_bot=False),
    )
    context = SimpleNamespace(bot=fake_bot(get_chat_member=get_chat_member))
    asyncio.run(handlers.callback(SimpleNamespace(callback_query=query), context))
    return query.answer.call_args.args[0]


def test_non_admin_cannot_change_weights_or_draw(setup):
    store, rid, handlers = setup
    for text in [f"/weight {rid} 123 10", f"/freeze {rid}", f"/draw {rid}", f"/export {rid}"]:
        message = run_command(handlers, text, user_id=123)
        assert "仅限" in message.reply_text.call_args.args[0]
    assert store.view(rid)["status"] == "OPEN"
    assert len(store.export(rid)["audit"]) == 1


def test_sensitive_command_requires_private_chat(setup):
    store, rid, handlers = setup
    message = run_command(handlers, f"/weight {rid} 123 10", chat_type="supergroup")
    assert "私聊" in message.reply_text.call_args.args[0]
    assert len(store.export(rid)["audit"]) == 1


def test_admin_flow_and_group_draw(setup):
    store, rid, handlers = setup
    run_command(handlers, f"/rule {rid} vip 2")
    run_command(handlers, f"/grant {rid} 123 vip")
    store.join(rid, 123, "Alice")
    assert store.view(rid)["entries"][0]["weight"] == 3
    message = run_command(handlers, f"/publish {rid}", chat_type="supergroup")
    assert message.reply_text.call_args.kwargs["reply_markup"] is not None
    assert store.view(rid)["chat_id"] == GROUP["id"]
    run_command(handlers, f"/freeze {rid}")
    first = run_command(handlers, f"/draw {rid}", chat_type="supergroup")
    second = run_command(handlers, f"/draw {rid}", chat_type="supergroup")
    assert first.reply_text.call_args == second.reply_text.call_args
    assert "Alice" in first.reply_text.call_args.args[0]


def test_replies_in_a_group_count_against_its_budget(setup):
    _, rid, handlers = setup
    run_command(handlers, f"/publish {rid}", chat_type="supergroup")  # the card
    run_command(handlers, f"/freeze {rid}")  # a private chat has no budget
    run_command(handlers, f"/draw {rid}", chat_type="supergroup")  # the result
    for _ in range(GROUP_BUDGET - 3):
        run_command(handlers, f"/result {rid}", chat_type="supergroup")
    assert handlers.room(GROUP["id"])
    run_command(handlers, f"/result {rid}", chat_type="supergroup")
    assert not handlers.room(GROUP["id"])


def test_bad_arguments_do_not_modify_data(setup):
    store, rid, handlers = setup
    for text in [
        f"/weight {rid} 123",
        f"/weight {rid} 123 -1",
        f"/weight {rid} 123 1.5",
        f"/rule {rid} bad/tag 2",
        "/draw 9999999999999999999999999",
    ]:
        assert run_command(handlers, text).reply_text.await_count
    assert len(store.export(rid)["audit"]) == 1


def test_callback_registration_and_deadline(setup):
    store, rid, handlers = setup
    store.bind(rid, 99, GROUP["id"])
    assert "成功" in join_click(handlers, rid, member(ChatMember.MEMBER))
    assert "无需重复" in join_click(handlers, rid, member(ChatMember.MEMBER))
    store.freeze(rid, 99)
    assert "报名已截止" in join_click(handlers, rid, member(ChatMember.MEMBER))


def test_join_requires_group_binding(setup):
    store, rid, handlers = setup
    lookup = member(ChatMember.MEMBER)
    assert "尚未在群里发布" in join_click(handlers, rid, lookup)
    lookup.assert_not_awaited()
    assert store.view(rid)["entries"] == []


@pytest.mark.parametrize(
    "status, fields, allowed",
    [
        (ChatMember.OWNER, {}, True),
        (ChatMember.ADMINISTRATOR, {}, True),
        (ChatMember.MEMBER, {}, True),
        (ChatMember.RESTRICTED, {"is_member": True}, True),
        (ChatMember.RESTRICTED, {"is_member": False}, False),
        (ChatMember.LEFT, {}, False),
        (ChatMember.BANNED, {}, False),
    ],
)
def test_join_checks_current_group_membership(setup, status, fields, allowed):
    store, rid, handlers = setup
    store.bind(rid, 99, GROUP["id"])
    lookup = member(status, **fields)
    text = join_click(handlers, rid, lookup)
    assert lookup.await_args.args == (GROUP["id"], 123)
    assert ("成功" in text) is allowed
    assert len(store.view(rid)["entries"]) == int(allowed)


def test_membership_lookup_failure_fails_closed(setup):
    store, rid, handlers = setup
    store.bind(rid, 99, GROUP["id"])
    text = join_click(handlers, rid, AsyncMock(side_effect=Forbidden("bot was kicked")))
    assert "暂时无法确认" in text
    assert store.view(rid)["entries"] == []


def test_join_follows_supergroup_upgrade(setup):
    store, rid, handlers = setup
    store.bind(rid, 99, -1)
    lookup = AsyncMock(side_effect=[ChatMigrated(-1001), SimpleNamespace(status=ChatMember.MEMBER)])
    assert "成功" in join_click(handlers, rid, lookup)
    assert lookup.await_args.args == (-1001, 123)
    assert store.view(rid)["chat_id"] == -1001


@pytest.mark.parametrize(
    "message",
    [
        SimpleNamespace(chat=SimpleNamespace(id=-1), migrate_to_chat_id=-1001),
        SimpleNamespace(
            chat=SimpleNamespace(id=-1001), migrate_to_chat_id=None, migrate_from_chat_id=-1
        ),
    ],
)
def test_supergroup_upgrade_message_moves_binding(setup, message):
    store, rid, handlers = setup
    store.bind(rid, 99, -1)
    context = SimpleNamespace(bot=fake_bot())
    asyncio.run(handlers.migrate(SimpleNamespace(effective_message=message), context))
    assert store.view(rid)["chat_id"] == -1001
    context.bot.send_message.assert_not_awaited()  # it never had a card to replace


def test_supergroup_upgrade_reposts_open_cards(setup):
    store, rid, handlers = setup
    store.bind(rid, 99, -1)
    store.set_card(rid, 500)
    drawn = store.create(99, "已开奖", 1, 60, chat_id=-1)
    store.set_card(drawn, 501)
    store.freeze(drawn, 99)
    store.draw(drawn, 99)
    bot = fake_bot()
    message = SimpleNamespace(chat=SimpleNamespace(id=-1), migrate_to_chat_id=-1001)
    asyncio.run(
        handlers.migrate(SimpleNamespace(effective_message=message), SimpleNamespace(bot=bot))
    )
    assert bot.send_message.await_count == 1  # the open raffle only
    assert bot.send_message.await_args.args[0] == -1001
    assert store.view(rid)["card_message_id"] == 88
    assert store.view(drawn)["card_message_id"] is None
    bot.pin_chat_message.assert_awaited_once()
    bot.unpin_chat_message.assert_not_awaited()  # the old card is out of reach


def test_publish_binds_first_group_only(setup):
    store, rid, handlers = setup
    unbound = run_command(handlers, f"/raffle {rid}")
    assert "尚未在群里发布" in unbound.reply_text.call_args.args[0]
    assert unbound.reply_text.call_args.kwargs["reply_markup"] is None
    run_command(handlers, f"/publish {rid}", chat_type="supergroup")
    assert store.view(rid)["card_message_id"] == 77
    other = run_command(handlers, f"/publish {rid}", chat_type="supergroup", chat_id=-200)
    assert "其他群" in other.reply_text.call_args.args[0]
    assert store.view(rid)["chat_id"] == GROUP["id"]


def test_closed_card_and_short_result(setup):
    store, rid, _ = setup
    store.join(rid, 123, "Alice")
    store.override(rid, 99, 123, 0)
    store.freeze(rid, 99)
    text, markup = card(store.view(rid), SHANGHAI)
    assert text.endswith("报名已截止")
    assert markup is None
    assert "实际抽出 0 名" in result_text(store.draw(rid, 99))


def test_card_keeps_weights_private_but_says_the_raffle_is_weighted(setup):
    store, rid, _ = setup
    store.bind(rid, 99, GROUP["id"])
    store.join(rid, 123, "Alice")
    plain, markup = card(store.view(rid), SHANGHAI)
    assert "加成" not in plain
    assert [b.text for row in markup.inline_keyboard for b in row] == ["🎟 参与抽奖"]
    store.rule(rid, 99, "vip", 5)
    store.grant(rid, 99, 123, "vip")
    weighted, _ = card(store.view(rid), SHANGHAI)
    assert "本场设有中奖加成" in weighted
    assert "vip" not in weighted
    assert "权重" not in weighted


def test_result_mentions_winners_and_escapes_names(setup):
    store, rid, _ = setup
    store.join(rid, 123, "<b>Al & ice</b>")
    store.freeze(rid, 99)
    result = store.draw(rid, 99)
    html = result_text(result, mention=True)
    assert '<a href="tg://user?id=123">&lt;b&gt;Al &amp; ice&lt;/b&gt;</a>' in html
    assert "<b>Al & ice</b>（ID：123）" in result_text(result)


def test_long_messages_split_at_line_breaks():
    lines = [f'<a href="tg://user?id={n}">name {n}</a>' for n in range(200)]
    parts = chunks("\n".join(lines))
    assert len(parts) > 1
    assert all(len(part) <= 1500 for part in parts)
    assert "\n".join(parts).split("\n") == lines


def test_build_application_offline(tmp_path, monkeypatch):
    # httpx reads proxy variables; an ambient SOCKS proxy would need the optional socksio.
    for name in ("ALL_PROXY", "HTTPS_PROXY", "HTTP_PROXY"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)
    settings = Settings(
        "123456:offline-test-token", frozenset({99}), str(tmp_path / "test.sqlite3"), SHANGHAI
    )
    app = build_application(settings)
    assert len(app.handlers[0]) == 12
    assert app.concurrent_updates > 1
    assert {"chat_member", "my_chat_member"} <= set(ALLOWED_UPDATES)
    jobs = [job.callback.__name__ for job in app.job_queue.jobs()]
    assert jobs == ["auto_draw", "cleanup", "sync_all_admins", "save_activity"]


def test_missing_configuration_fails_closed(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    with pytest.raises(LotteryError, match="TELEGRAM_BOT_TOKEN"):
        Settings.from_env()
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:test")
    monkeypatch.setenv("ADMIN_USER_IDS", "")
    with pytest.raises(LotteryError, match="ADMIN_USER_IDS"):
        Settings.from_env()


def test_timezone_setting(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:test")
    monkeypatch.setenv("ADMIN_USER_IDS", "99")
    monkeypatch.delenv("TIMEZONE", raising=False)
    assert Settings.from_env().timezone == SHANGHAI
    monkeypatch.setenv("TIMEZONE", "America/New_York")
    assert Settings.from_env().timezone == ZoneInfo("America/New_York")
    for bad in ("Mars/Olympus", "../etc/passwd"):
        monkeypatch.setenv("TIMEZONE", bad)
        with pytest.raises(LotteryError, match="TIMEZONE"):
            Settings.from_env()


@pytest.mark.parametrize(
    "clock, zone, expected",
    [
        (1_800_000_000, "Asia/Shanghai", "2027-01-15 17:00（UTC+08:00）"),
        (1_800_000_000, "UTC", "2027-01-15 09:00（UTC+00:00）"),
        (1_800_000_000, "Asia/Kolkata", "2027-01-15 14:30（UTC+05:30）"),
        (1_800_000_000, "America/New_York", "2027-01-15 04:00（UTC-05:00）"),
        (1_783_000_000, "America/New_York", "2026-07-02 10:46（UTC-04:00）"),  # DST
    ],
)
def test_card_shows_deadline_in_configured_timezone(tmp_path, clock, zone, expected):
    store = Store(tmp_path / "tz.sqlite3", clock=lambda: clock)
    rid = store.create(99, "时区", 1, 60)
    text, _ = card(store.view(rid), ZoneInfo(zone))
    assert f"开奖时间：{expected}" in text


def test_delivery_failure_does_not_reroll(setup):
    store, rid, handlers = setup
    store.join(rid, 123, "Alice")
    store.freeze(rid, 99)
    update, context = command_update(f"/draw {rid}")
    update.effective_message.reply_text.side_effect = RuntimeError("network down")
    with pytest.raises(RuntimeError):
        asyncio.run(handlers.command(update, context))
    saved = store.view(rid)["result"]
    run_command(handlers, f"/draw {rid}")
    assert store.view(rid)["result"] == saved


@pytest.mark.parametrize(
    "text",
    [
        "/new 2 60 test",
        "/config 1 1 100",
        "/rule 1 vip 2",
        "/grant 1 123 vip",
        "/revoke 1 123 vip",
        "/weight 1 123 10",
        "/preview 1",
        "/export 1",
        "/freeze 1",
        "/raffles",
        "/rules 1",
    ],
)
def test_private_operations_never_reply_with_sensitive_data_in_group(setup, text):
    store, _, handlers = setup
    message = run_command(handlers, text, chat_type="supergroup")
    assert message.reply_text.call_args.args[0] == "请私聊机器人执行此命令。"
    message.reply_document.assert_not_awaited()
    assert len(store.recent()) == 1
    assert len(store.export(1)["audit"]) == 1


def test_old_weight_buttons_no_longer_reveal_weights(setup):
    store, rid, handlers = setup
    store.join(rid, 123, "Alice")
    store.override(rid, 99, 123, 10)
    query = SimpleNamespace(
        data=f"weight:{rid}", answer=AsyncMock(), from_user=SimpleNamespace(id=123, is_bot=False)
    )
    asyncio.run(handlers.callback(SimpleNamespace(callback_query=query), None))
    assert query.answer.call_args.args[0] == "该功能已下线。"


def test_rules_are_for_super_admins_only(setup):
    _, rid, handlers = setup
    message = run_command(handlers, f"/rules {rid}", user_id=123)
    assert "仅限" in message.reply_text.call_args.args[0]


def test_weight_change_refreshes_published_card(setup):
    store, rid, handlers = setup
    run_command(handlers, f"/publish {rid}", chat_type="supergroup")
    store.rule(rid, 99, "vip", 2)
    bot = fake_bot()
    asyncio.run(handlers.refresh_card(bot, rid))
    assert bot.edit_message_text.await_args.kwargs["message_id"] == 77
    assert "本场设有中奖加成" in bot.edit_message_text.await_args.args[0]


def test_anonymous_sender_cannot_execute_management_command(setup):
    store, rid, handlers = setup
    update, context = command_update(f"/freeze {rid}")
    update.effective_message.sender_chat = SimpleNamespace(id=-1001)
    asyncio.run(handlers.command(update, context))
    assert store.view(rid)["status"] == "OPEN"


def test_freeze_after_draw_reports_saved_result(setup):
    store, rid, handlers = setup
    store.freeze(rid, 99)
    store.draw(rid, 99)
    message = run_command(handlers, f"/freeze {rid}")
    assert "已开奖" in message.reply_text.call_args.args[0]


def test_freeze_after_cancel_says_so(setup):
    store, rid, handlers = setup
    store.cancel(rid, 99)
    message = run_command(handlers, f"/freeze {rid}")
    assert message.reply_text.call_args.args[0] == "本场抽奖已取消。"


def test_stale_button_answer_failure_keeps_join_and_stays_silent(setup):
    store, rid, handlers = setup
    store.bind(rid, 99, GROUP["id"])
    stale = AsyncMock(side_effect=BadRequest("Query is too old"))
    join_click(handlers, rid, member(ChatMember.MEMBER), answer=stale)
    assert [p["user_id"] for p in store.view(rid)["entries"]] == [123]


def telegram_update(payload):
    bot = fake_bot(answer_callback_query=AsyncMock())
    update = Update.de_json({"update_id": 1, **payload}, None)
    for obj in (update.callback_query, update.effective_message):
        if obj is not None:
            obj.set_bot(bot)
    return update, bot


def test_button_error_is_answered_privately_not_in_group(setup):
    _, _, handlers = setup
    card_message = {"message_id": 5, "date": 0, "chat": GROUP, "text": "card"}
    update, bot = telegram_update(
        {
            "callback_query": {
                "id": "q",
                "chat_instance": "c",
                "data": "join:1",
                "from": USER,
                "message": card_message,
            }
        }
    )
    context = SimpleNamespace(bot=bot, error=RuntimeError("database is locked"))
    asyncio.run(handlers.on_error(update, context))
    bot.send_message.assert_not_awaited()
    assert bot.answer_callback_query.await_args.kwargs["show_alert"] is True


def test_command_error_replies_and_is_tidied_away_in_groups(due):
    store, _, now, handlers = due
    message = {"message_id": 5, "date": 0, "chat": GROUP, "from": USER, "text": "/draw 1"}
    update, bot = telegram_update({"message": message})
    asyncio.run(handlers.on_error(update, SimpleNamespace(bot=bot, error=RuntimeError("down"))))
    assert "请重试" in bot.send_message.await_args.kwargs["text"]
    now[0] += 600
    assert store.due_deletions() == {GROUP["id"]: [5, 88]}  # the command and the reply


@pytest.mark.parametrize(
    "chat, text, answered",
    [
        (GROUP, "今天吃什么好呢", False),  # chat the bot only counts
        (GROUP, None, False),  # a photo, say
        (GROUP, "签到", True),
        (GROUP, "/签到", True),
        (GROUP, "加灵石 50", True),
        ({"id": 123, "type": "private"}, "123456789 +50", True),
    ],
)
def test_errors_answer_only_messages_that_ask_the_bot(due, chat, text, answered):
    handlers = due[3]
    message = {"message_id": 5, "date": 0, "chat": chat, "from": USER}
    update, bot = telegram_update({"message": message | ({"text": text} if text else {})})
    asyncio.run(handlers.on_error(update, SimpleNamespace(bot=bot, error=RuntimeError("locked"))))
    assert bot.send_message.await_count == int(answered)


def test_token_filter_masks_message_and_traceback():
    record = logging.LogRecord(
        "t", logging.ERROR, __file__, 1, "GET %s", (f"https://x/bot{TOKEN}/getMe",), None
    )
    try:
        raise RuntimeError(f"failed for {TOKEN}")
    except RuntimeError:
        record.exc_info = sys.exc_info()
    TokenFilter(TOKEN).filter(record)
    text = logging.Formatter().format(record)
    assert "SECRET" not in text
    assert "bot123456:***/getMe" in text
    assert "RuntimeError: failed for 123456:***" in text


def test_startup_failure_keeps_cause_but_masks_token(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("ADMIN_USER_IDS", "99")
    monkeypatch.setattr(logging.root, "handlers", [])  # main() installs its own handler

    def broken(settings):
        raise RuntimeError(f'install "python-telegram-bot[socks]" ({settings.token})')

    monkeypatch.setattr("lottery.bot.build_application", broken)
    with pytest.raises(SystemExit) as exit_info:
        main()
    message = str(exit_info.value)
    assert 'RuntimeError: install "python-telegram-bot[socks]"' in message
    assert "SECRET" not in message


def test_raffles_are_looked_up_only_in_their_group(setup):
    store, rid, handlers = setup
    store.bind(rid, 99, GROUP["id"])
    elsewhere = "请在发布这场抽奖的群里查询。"
    for text in [f"/raffle {rid}", f"/result {rid}"]:
        private = run_command(handlers, text, user_id=123)
        other = run_command(handlers, text, user_id=123, chat_type="supergroup", chat_id=-200)
        here = run_command(handlers, text, user_id=123, chat_type="supergroup")
        super_admin = run_command(handlers, text)
        assert private.reply_text.call_args.args[0] == elsewhere
        assert other.reply_text.call_args.args[0] == elsewhere
        assert here.reply_text.call_args.args[0] != elsewhere
        assert super_admin.reply_text.call_args.args[0] != elsewhere


def test_one_persons_commands_run_in_the_order_sent(setup):
    store, rid, handlers = setup
    add_rule = store.rule

    def slow_rule(*args):
        time.sleep(0.05)  # the /grant right behind must wait for the rule it needs
        return add_rule(*args)

    store.rule = slow_rule
    rule, rule_context = command_update(f"/rule {rid} vip 2")
    grant, grant_context = command_update(f"/grant {rid} 123 vip")

    async def both():
        await asyncio.gather(
            handlers.command(rule, rule_context), handlers.command(grant, grant_context)
        )

    asyncio.run(both())
    assert "已保存" in grant.effective_message.reply_text.call_args.args[0]


def test_repeated_grant_and_missing_revoke_are_reported(setup):
    store, rid, handlers = setup
    run_command(handlers, f"/rule {rid} vip 2")
    replies = [
        run_command(handlers, text).reply_text.call_args.args[0]
        for text in [f"/grant {rid} 123 vip"] * 2 + [f"/revoke {rid} 123 vip"] * 2
    ]
    assert "已保存" in replies[0]
    assert "已有此条件" in replies[1]
    assert "已保存" in replies[2]
    assert "没有此条件" in replies[3]
    actions = [e["action"] for e in store.export(rid)["audit"]]
    assert (actions.count("grant"), actions.count("revoke")) == (1, 1)


def test_unchanged_settings_are_reported(setup):
    store, rid, handlers = setup
    for text in [f"/config {rid} 1 100", f"/weight {rid} 123 auto"]:
        reply_text = run_command(handlers, text).reply_text.call_args.args[0]
        assert reply_text == "与现有设置相同，未做修改。"
    assert len(store.export(rid)["audit"]) == 1


def member_update(status, left_at, old_status=ChatMember.MEMBER, **fields):
    change = SimpleNamespace(
        chat=SimpleNamespace(id=GROUP["id"]),
        date=datetime.fromtimestamp(left_at, UTC),
        old_chat_member=SimpleNamespace(status=old_status),
        new_chat_member=SimpleNamespace(status=status, user=SimpleNamespace(id=123), **fields),
    )
    return SimpleNamespace(chat_member=change)


def test_promoted_and_demoted_admins_are_followed(setup):
    store, _, handlers = setup
    store.remember_group(GROUP["id"], "测试群")
    promoted = member_update(ChatMember.ADMINISTRATOR, store.clock())
    asyncio.run(handlers.member_changed(promoted, None))
    assert store.managed_groups(123) == [(GROUP["id"], "测试群")]
    demoted = member_update(ChatMember.MEMBER, store.clock(), old_status=ChatMember.ADMINISTRATOR)
    asyncio.run(handlers.member_changed(demoted, None))
    assert store.managed_groups(123) == []


@pytest.mark.parametrize(
    "status, fields, kept",
    [
        (ChatMember.LEFT, {}, False),
        (ChatMember.BANNED, {}, False),
        (ChatMember.RESTRICTED, {"is_member": False}, False),
        (ChatMember.RESTRICTED, {"is_member": True}, True),
        (ChatMember.ADMINISTRATOR, {}, True),
    ],
)
def test_leaving_group_cancels_join(setup, status, fields, kept):
    store, rid, handlers = setup
    store.bind(rid, 99, GROUP["id"])
    store.join(rid, 123, "Alice")
    left_at = store.view(rid)["deadline"] - 60
    asyncio.run(handlers.member_changed(member_update(status, left_at, **fields), None))
    assert len(store.view(rid)["entries"]) == int(kept)


def test_leaving_group_updates_the_card(setup):
    store, rid, handlers = setup
    store.bind(rid, 99, GROUP["id"])
    store.join(rid, 123, "Alice")
    jobs = SimpleNamespace(get_jobs_by_name=lambda name: [], run_once=Mock())
    left_at = store.view(rid)["deadline"] - 60
    update = member_update(ChatMember.LEFT, left_at)
    asyncio.run(handlers.member_changed(update, SimpleNamespace(job_queue=jobs)))
    assert jobs.run_once.call_args.kwargs["name"] == f"card:{rid}"


def test_a_leave_just_after_a_click_is_not_overtaken_by_it(setup):
    store, rid, handlers = setup
    store.bind(rid, 99, GROUP["id"])
    looking_up = asyncio.Event()

    async def slow_lookup(chat_id, user_id):
        looking_up.set()
        await asyncio.sleep(0.05)  # Telegram answers while the leave comes in
        return SimpleNamespace(status=ChatMember.MEMBER)

    query = SimpleNamespace(
        data=f"join:{rid}",
        answer=AsyncMock(),
        from_user=SimpleNamespace(id=123, full_name="Alice", is_bot=False),
    )
    context = SimpleNamespace(bot=fake_bot(get_chat_member=AsyncMock(side_effect=slow_lookup)))
    leave = member_update(ChatMember.LEFT, store.view(rid)["deadline"] - 60)

    async def click_then_leave():
        click = asyncio.create_task(
            handlers.callback(SimpleNamespace(callback_query=query), context)
        )
        await looking_up.wait()
        await asyncio.gather(click, handlers.member_changed(leave, None))

    asyncio.run(click_then_leave())
    assert store.view(rid)["entries"] == []


def test_rejoining_group_allows_joining_again(setup):
    store, rid, handlers = setup
    store.bind(rid, 99, GROUP["id"])
    assert "成功" in join_click(handlers, rid, member(ChatMember.MEMBER))
    left_at = store.view(rid)["deadline"] - 60
    asyncio.run(handlers.member_changed(member_update(ChatMember.LEFT, left_at), None))
    assert store.view(rid)["entries"] == []
    assert "成功" in join_click(handlers, rid, member(ChatMember.MEMBER))


def test_raffles_lists_current_status(setup):
    _, rid, handlers = setup
    text = run_command(handlers, "/raffles").reply_text.call_args.args[0]
    assert f"{rid}｜测试抽奖｜报名中" in text


@pytest.fixture
def due(tmp_path):
    now = [1_800_000_000.0]
    store = Store(tmp_path / "auto.sqlite3", clock=lambda: now[0])
    rid = store.create(99, "自动开奖", 1, 60)
    store.bind(rid, 99, GROUP["id"])
    store.join(rid, 123, "Alice")
    return store, rid, now, BotHandlers(store, {99}, SHANGHAI)


def run_auto_draw(handlers, send=None):
    bot = fake_bot(**({"send_message": send} if send else {}))
    asyncio.run(handlers.auto_draw(SimpleNamespace(bot=bot)))
    return bot.send_message


def test_auto_draw_announces_once_after_deadline(due):
    store, rid, now, handlers = due
    run_auto_draw(handlers).assert_not_awaited()
    now[0] += 3600
    send = run_auto_draw(handlers)
    assert send.await_args.args[0] == GROUP["id"]
    assert "Alice" in send.await_args.args[1]
    assert store.view(rid)["status"] == "DRAWN"
    run_auto_draw(handlers).assert_not_awaited()


def test_auto_draw_retries_after_network_error(due):
    _, _, now, handlers = due
    now[0] += 3600
    run_auto_draw(handlers, AsyncMock(side_effect=TimedOut()))
    assert run_auto_draw(handlers).await_count == 1


def test_auto_draw_stops_when_bot_cannot_post(due):
    store, rid, now, handlers = due
    now[0] += 3600
    run_auto_draw(handlers, AsyncMock(side_effect=Forbidden("bot was kicked")))
    run_auto_draw(handlers).assert_not_awaited()
    assert store.view(rid)["result"]["winners"][0]["user_id"] == 123
    assert store.export(rid)["audit"][-1]["action"] == "announce"


def test_auto_draw_follows_supergroup_upgrade(due):
    _, _, now, handlers = due
    now[0] += 3600
    run_auto_draw(handlers, AsyncMock(side_effect=ChatMigrated(-1001)))
    assert run_auto_draw(handlers).await_args.args[0] == -1001


@pytest.mark.parametrize("chat_type, group_posts", [("supergroup", 0), ("private", 1)])
def test_early_manual_draw_reaches_group_once(due, chat_type, group_posts):
    store, rid, _, handlers = due
    store.freeze(rid, 99)
    run_command(handlers, f"/draw {rid}", chat_type=chat_type)
    assert run_auto_draw(handlers).await_count == group_posts
    run_auto_draw(handlers).assert_not_awaited()


async def slow_reply(*args, **kwargs):
    await asyncio.sleep(0.05)  # a network round trip: other tasks run meanwhile
    return SimpleNamespace(message_id=88)


def test_publishing_a_drawn_raffle_in_its_group_announces_it(due):
    store, rid, _, handlers = due
    store.freeze(rid, 99)
    run_command(handlers, f"/draw {rid}")  # in private: the group has not seen it yet
    message = run_command(handlers, f"/publish {rid}", chat_type="supergroup")
    assert "开奖结果" in message.reply_text.call_args.args[0]
    run_auto_draw(handlers).assert_not_awaited()


def test_overlapping_announcements_post_the_result_once(due):
    store, rid, now, handlers = due
    now[0] += 3600
    bot = fake_bot(send_message=AsyncMock(side_effect=slow_reply))

    async def overlap():
        # The menu's draw button while the deadline job and a filling join run.
        context = SimpleNamespace(bot=bot)
        await asyncio.gather(
            handlers.announce(bot, rid, GROUP["id"]),
            handlers.auto_draw(context),
            handlers.auto_draw(context),
        )

    asyncio.run(overlap())
    assert bot.send_message.await_count == 1
    assert [e["action"] for e in store.export(rid)["audit"]].count("announce") == 1


def test_draw_in_the_group_is_not_announced_again_by_the_job(due):
    store, rid, _, handlers = due
    store.freeze(rid, 99)
    update, context = command_update(f"/draw {rid}", chat_type="supergroup")
    replying = asyncio.Event()

    async def reply_text(*args, **kwargs):
        replying.set()
        return await slow_reply()

    update.effective_message.reply_text = AsyncMock(side_effect=reply_text)
    job_bot = fake_bot()

    async def overlap():
        draw = asyncio.create_task(handlers.command(update, context))
        await replying.wait()  # drawn and being posted: the job now sees it as due
        await asyncio.gather(draw, handlers.auto_draw(SimpleNamespace(bot=job_bot)))

    asyncio.run(overlap())
    job_bot.send_message.assert_not_awaited()
    assert update.effective_message.reply_text.await_count == 1


def test_designated_winners_are_listed_like_the_others(due):
    store, rid, now, handlers = due  # one place; Alice has joined
    store.join(rid, 124, "Bob")
    store.designate(rid, 99, 124)
    preview = run_command(handlers, f"/preview {rid}").reply_text.call_args.args[0]
    assert "Bob / 124：权重 1（规则），首轮 指定获奖" in preview
    assert "Alice / 123：权重 1（规则），首轮 0%" in preview  # the only place is taken
    now[0] += 3600
    announcement = run_auto_draw(handlers).await_args.args[1]
    assert "Bob" in announcement and "Alice" not in announcement
    assert "指定" not in announcement


def test_group_text_counts_for_activity_raffles(due):
    store, rid, now, handlers = due
    store.cancel(rid, 99)  # only the activity raffle below is due
    ranked = store.create(
        99,
        "话痨榜",
        2,
        deadline=now[0] + 3600,
        chat_id=GROUP["id"],
        kind="rank",
        prizes=[["a", 1], ["b", 1]],
    )

    points = Points(handlers)
    points.randbelow = lambda total: total - 1  # no 灵石 rewards to answer

    def say(user_id, at=5, text="大家好", **fields):
        message = SimpleNamespace(
            chat=SimpleNamespace(id=GROUP["id"], type="supergroup"),
            date=datetime.fromtimestamp(now[0] + at, UTC),
            text=text,
            caption=None,
            message_id=12,
            reply_text=AsyncMock(return_value=SimpleNamespace(message_id=13)),
            sender_chat=fields.pop("sender_chat", None),
        )
        user = SimpleNamespace(
            **{"id": user_id, "full_name": f"u{user_id}", "is_bot": False} | fields
        )
        update = SimpleNamespace(effective_message=message, effective_user=user)
        asyncio.run(points.message(update, SimpleNamespace(bot=fake_bot())))

    for user_id, at in ((123, 5), (123, 15), (124, 5)):
        say(user_id, at)
    say(123, 17)  # two seconds after the last: too quick to count
    say(124, 25, "好的")  # too short
    say(124, 35, "灵石榜")  # a 灵石 word
    say(125, is_bot=True)
    say(126, sender_chat=SimpleNamespace(id=GROUP["id"]))  # an anonymous admin
    asyncio.run(handlers.save_activity(None))
    now[0] += 60
    text, markup = card(store.view(ranked), SHANGHAI)
    assert "🏆 第一名 a、第二名 b\n💬 按发言次数排名，前 2 名获奖" in text
    assert text.endswith("👉 在群里发言即可参与")
    assert markup.inline_keyboard[0][0].callback_data == f"rank:{ranked}"

    def standing(user_id):
        query = SimpleNamespace(
            data=f"rank:{ranked}",
            answer=AsyncMock(),
            from_user=SimpleNamespace(id=user_id, full_name="x", is_bot=False),
        )
        context = SimpleNamespace(bot=fake_bot())
        asyncio.run(handlers.callback(SimpleNamespace(callback_query=query), context))
        return query.answer.await_args.args[0]

    assert standing(124) == (
        "📊 发言排名 · 前 2 名获奖\n1. u123 · 2 次\n2. u124 · 1 次\n你：第 2 名 · 1 次"
    )
    assert standing(999).endswith("你还没有发言，发言即可参与排名")
    preview = run_command(handlers, f"/preview {ranked}").reply_text.call_args.args[0]
    assert "u123 / 123：发言 2 次\nu124 / 124：发言 1 次\n展示前 30 人" in preview
    now[0] += 3600
    announcement = run_auto_draw(handlers).await_args.args[1]
    assert '1. <a href="tg://user?id=123">u123</a> — a（发言 2 次）' in announcement
    assert '2. <a href="tg://user?id=124">u124</a> — b（发言 1 次）' in announcement


def test_standing_in_a_reach_raffle_and_its_length():
    reach = {"kind": "reach", "min_messages": 10, "winner_count": 2}
    ranked = [
        {"user_id": 1, "display_name": "A", "messages": 12},
        {"user_id": 2, "display_name": "B", "messages": 7},
    ]
    assert standing_text(reach, ranked, 2) == (
        "📊 发言满 10 次即可参与抽奖\n已达标 1 人\n你已发言 7 次，还差 3 次"
    )
    assert standing_text(reach, ranked, 1).endswith("你已发言 12 次，已达标")
    assert standing_text(reach, ranked, 3).endswith("你已发言 0 次，还差 10 次")
    rank = {"kind": "rank", "min_messages": None, "winner_count": 10}
    crowd = [{"user_id": i, "display_name": "名" * 128, "messages": 100_000 - i} for i in range(9)]
    assert len(standing_text(rank, crowd, 8)) <= 200  # Telegram's limit for the alert
    assert standing_text(rank, crowd, 8).endswith("你：第 9 名 · 99992 次")


def test_the_command_menu_lists_start_and_id_and_link_in_groups():
    bot = SimpleNamespace(set_my_commands=AsyncMock())
    asyncio.run(register_commands(SimpleNamespace(bot=bot)))
    menus = {
        getattr(call.kwargs.get("scope"), "type", "default"): [c.command for c in call.args[0]]
        for call in bot.set_my_commands.await_args_list
    }
    assert menus == {"default": ["start", "id"], "all_group_chats": ["start", "id", "link"]}


def test_preview_shows_corrections(setup):
    store, _, handlers = setup
    rid = store.create(99, "话痨榜", 1, 60, chat_id=GROUP["id"], kind="rank", prizes=[["a", 1]])
    store.adjust(rid, 99, 123, to=4)
    preview = run_command(handlers, f"/preview {rid}").reply_text.call_args.args[0]
    assert "用户 123 / 123：发言 4 次（手动 +4）" in preview


def test_activity_raffles_take_no_joins(setup):
    store, _, handlers = setup
    rid = store.create(99, "话痨榜", 1, 60, chat_id=GROUP["id"], kind="rank", prizes=[["a", 1]])
    answer = join_click(handlers, rid, member(ChatMember.MEMBER))
    assert answer == "这场抽奖在群里发言即可参与，不用报名。"


def test_result_names_each_winners_prize():
    result = {
        "raffle_id": 1,
        "title": "t",
        "requested_count": 2,
        "winners": [
            {"user_id": 1, "display_name": "A", "prize": "iPhone"},
            {"user_id": 2, "display_name": "B", "prize": "<1usdt>"},
        ],
        "snapshot_hash": "h",
    }
    assert "1. A（ID：1） — iPhone" in result_text(result)
    assert "摘要" not in result_text(result, mention=True)  # the digest stays in /export
    assert '2. <a href="tg://user?id=2">B</a> — &lt;1usdt&gt;' in result_text(result, mention=True)


def test_command_errors_in_groups_are_deleted_later(due):
    store, _, now, handlers = due
    run_command(handlers, "/new 1 60 x", user_id=123, chat_type="supergroup")  # not an admin
    run_command(handlers, "/new 1 60 x", user_id=123)  # private chats are left alone
    now[0] += 600
    assert store.due_deletions() == {GROUP["id"]: [10, 77]}


def long_list(store):
    """A raffle in the group whose result takes several messages."""
    rid = store.create(99, "长名单", 30, 60, chat_id=GROUP["id"])
    for uid in range(1, 31):
        store.join(rid, uid, "名" * 100)
    return rid


def test_a_long_result_carries_on_from_the_part_that_failed(due):
    store, rid, now, handlers = due
    store.cancel(rid, 99)  # only the long list is due
    long = long_list(store)
    now[0] += 3600
    posted, failures = [], [TimedOut()]

    async def send_message(chat_id, text, **kwargs):
        if len(posted) == 1 and failures:
            raise failures.pop()  # the second part fails once
        posted.append(text)
        return SimpleNamespace(message_id=len(posted))

    bot = fake_bot(send_message=AsyncMock(side_effect=send_message))
    for _ in range(2):  # the second pass retries
        asyncio.run(handlers.auto_draw(SimpleNamespace(bot=bot)))
    parts = chunks(result_text(store.view(long)["result"], mention=True))
    assert len(parts) > 2
    assert posted == parts  # every part once, in order
    assert bot.pin_chat_message.await_args.args[1] == 1  # the first part


def test_a_long_result_drawn_in_the_group_pins_its_first_part(due):
    store, rid, _, handlers = due
    store.cancel(rid, 99)
    long = long_list(store)
    store.freeze(long, 99)
    update, context = command_update(f"/draw {long}", chat_type="supergroup")
    ids = iter(range(100, 200))
    update.effective_message.reply_text = AsyncMock(
        side_effect=lambda *args, **kwargs: SimpleNamespace(message_id=next(ids))
    )
    asyncio.run(handlers.command(update, context))
    assert update.effective_message.reply_text.await_count > 2
    assert context.bot.pin_chat_message.await_args.args[1] == 100


def test_messages_left_in_an_upgraded_group_are_given_up(due):
    store, rid, now, handlers = due
    store.schedule_deletions(GROUP["id"], [10], now[0])
    bot = fake_bot(delete_messages=AsyncMock(side_effect=ChatMigrated(-1001)))
    asyncio.run(handlers.cleanup(SimpleNamespace(bot=bot)))
    assert store.due_deletions() == {}
    assert store.view(rid)["chat_id"] == -1001


def test_rankings_are_worked_out_once_in_a_while(tmp_path, monkeypatch):
    now = [1_800_000_000.0]
    store = Store(tmp_path / "rank.sqlite3", clock=lambda: now[0])
    handlers = BotHandlers(store, {99}, SHANGHAI)
    rid = store.create(99, "话痨榜", 1, 60, chat_id=GROUP["id"], kind="rank", prizes=[["a", 1]])
    worked_out = []
    ranking = store.ranking
    monkeypatch.setattr(store, "ranking", lambda r: worked_out.append(r) or ranking(r))

    def press(user_id):
        query = SimpleNamespace(
            data=f"rank:{rid}",
            answer=AsyncMock(),
            from_user=SimpleNamespace(id=user_id, full_name="x", is_bot=False),
        )
        context = SimpleNamespace(bot=fake_bot())
        asyncio.run(handlers.callback(SimpleNamespace(callback_query=query), context))
        return query.answer.await_args.args[0]

    assert press(1).endswith("你还没有发言，发言即可参与排名")
    store.count_message(GROUP["id"], 1, "u1", now[0] + 1)
    now[0] += 29
    assert press(1).endswith("你还没有发言，发言即可参与排名")  # shown again for 30 seconds
    assert press(2).endswith("你还没有发言，发言即可参与排名")
    assert len(worked_out) == 1
    now[0] += 1
    assert press(1).endswith("你：第 1 名 · 1 次")
    assert len(worked_out) == 2
    now[0] += 3600  # counting has ended: no ranking any more
    assert press(1) == "统计已截止，结果以开奖公告为准。"


def test_presses_at_once_share_one_ranking(setup):
    store, _, handlers = setup
    rid = store.create(99, "话痨榜", 1, 60, chat_id=GROUP["id"], kind="rank", prizes=[["a", 1]])
    worked_out = []

    def slow(r):
        worked_out.append(r)
        time.sleep(0.05)
        return Store.ranking(store, r)

    store.ranking = slow

    async def crowd():
        return await asyncio.gather(*(handlers.ranking(rid) for _ in range(10)))

    assert len(asyncio.run(crowd())) == 10
    assert worked_out == [rid]


def test_the_database_uses_write_ahead_logging(setup):
    store, _, _ = setup
    with store.reading() as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_short_delays_are_timed_to_the_second(setup, monkeypatch):
    store, _, handlers = setup
    store.remember_group(GROUP["id"], "测试群")
    bot = fake_bot()
    waited = []

    async def wait(seconds):
        waited.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", wait)

    async def tidy(setting):
        await handlers.tidy(bot, GROUP["id"], [1, 2], setting)
        await asyncio.gather(*handlers._deleting)

    asyncio.run(tidy("delete_notices"))  # 3 seconds by default
    assert waited == [3]
    bot.delete_messages.assert_awaited_once_with(GROUP["id"], [1, 2])
    with store.reading() as db:  # done, so no longer waiting for the 30-second pass
        assert db.execute("SELECT COUNT(*) FROM deletions").fetchone()[0] == 0
    asyncio.run(tidy("delete_keyword"))  # a minute: left to the 30-second pass
    assert waited == [3]
    with store.reading() as db:
        assert db.execute("SELECT COUNT(*) FROM deletions").fetchone()[0] == 2


def arrival(user_id, at, link=None, by=None, request=False):
    """A chat_member update: user_id joined the group at `at`, by an invite link or added
    by someone."""
    user = SimpleNamespace(id=user_id, is_bot=False, full_name=f"u{user_id}")
    adder = SimpleNamespace(id=by, is_bot=False, full_name=f"u{by}") if by else user
    change = SimpleNamespace(
        chat=SimpleNamespace(id=GROUP["id"]),
        date=datetime.fromtimestamp(at, UTC),
        old_chat_member=SimpleNamespace(status=ChatMember.LEFT),
        new_chat_member=SimpleNamespace(status=ChatMember.MEMBER, user=user),
        invite_link=link and SimpleNamespace(invite_link=link),
        from_user=adder,
        via_join_request=request,
    )
    return SimpleNamespace(chat_member=change)


@pytest.fixture
def inviting(tmp_path):
    now = [1_800_000_000.0]
    store = Store(tmp_path / "invite.sqlite3", clock=lambda: now[0])
    store.remember_group(GROUP["id"], "测试群")
    return store, now, BotHandlers(store, {99}, SHANGHAI)


def test_joins_are_credited_to_whoever_brought_them_in(inviting):
    store, now, handlers = inviting
    by_link = store.create(
        99, "链接榜", 1, 60, chat_id=GROUP["id"], kind="rank", prizes=[["a", 1]], invite_via="link"
    )
    by_adding = store.create(
        99, "拉人榜", 1, 60, chat_id=GROUP["id"], kind="rank", prizes=[["a", 1]], invite_via="add"
    )
    store.save_invite_link(GROUP["id"], 1, "https://t.me/+eve", "Eve")
    context = SimpleNamespace(bot=fake_bot(), job_queue=None)
    for update in (
        arrival(11, now[0] + 10, link="https://t.me/+eve"),
        arrival(12, now[0] + 11, link="https://t.me/+someone-elses"),  # not the bot's link
        arrival(13, now[0] + 12, by=2),
        arrival(14, now[0] + 13, by=2, request=True),  # an admin letting them in
        arrival(15, now[0] + 14),  # came in by themselves
    ):
        asyncio.run(handlers.member_changed(update, context))
    assert [(e["user_id"], e["display_name"]) for e in store.ranking(by_link)[1]] == [(1, "Eve")]
    assert [(e["user_id"], e["display_name"]) for e in store.ranking(by_adding)[1]] == [(2, "u2")]


def test_invite_reach_raffles_are_drawn_once_enough_have_enough(inviting):
    store, now, handlers = inviting
    rid = store.create(
        99,
        "拉人抽奖",
        1,
        deadline=now[0] + 86400,
        chat_id=GROUP["id"],
        kind="reach",
        prizes=[["a", 1]],
        invite_via="add",
        min_messages=2,
        target=1,
    )
    bot = fake_bot()
    context = SimpleNamespace(bot=bot, job_queue=None)
    asyncio.run(handlers.member_changed(arrival(11, now[0] + 10, by=2), context))
    bot.send_message.assert_not_awaited()
    asyncio.run(handlers.member_changed(arrival(12, now[0] + 20, by=2), context))
    announcement = bot.send_message.await_args.args[1]
    assert '1. <a href="tg://user?id=2">u2</a> — a（邀请 2 人）' in announcement
    assert store.view(rid)["status"] == "DRAWN"


def test_members_get_their_invite_link(inviting):
    store, now, handlers = inviting
    bot = fake_bot(
        create_chat_invite_link=AsyncMock(
            return_value=SimpleNamespace(invite_link="https://t.me/+alice")
        ),
        get_chat_member=member(ChatMember.MEMBER),
    )
    update, context = command_update("/link", user_id=123, chat_type="supergroup")
    update.effective_user.full_name = "Alice"
    context.bot = bot
    asyncio.run(handlers.link(update, context))
    text = update.effective_message.reply_text.call_args.args[0]
    assert text.startswith("🔗 你在「测试群」的专属邀请链接：\nhttps://t.me/+alice\n")
    assert bot.create_chat_invite_link.await_args.kwargs == {"name": "Alice"}
    assert store.link_owner(GROUP["id"], "https://t.me/+alice") == (123, "Alice")
    now[0] += 3
    assert store.due_deletions() == {}  # left a minute to be copied, not 3 seconds
    now[0] += 57
    assert store.due_deletions() == {GROUP["id"]: [10, 77]}
    # The same link every time, also in private.
    user = SimpleNamespace(id=123, full_name="Alice")
    again = asyncio.run(handlers.invite_link_text(bot, GROUP["id"], user))
    assert "https://t.me/+alice" in again
    assert bot.create_chat_invite_link.await_count == 1
    bot.get_chat_member = member(ChatMember.LEFT)
    stranger = SimpleNamespace(id=456, full_name="Mallory")
    refused = asyncio.run(handlers.invite_link_text(bot, GROUP["id"], stranger))
    assert refused == "只有群成员才能领取这个群的邀请链接。"


def test_the_card_button_opens_a_private_chat_for_the_link(inviting):
    store, _, handlers = inviting
    rid = store.create(
        99, "链接榜", 1, 60, chat_id=GROUP["id"], kind="rank", prizes=[["a", 1]], invite_via="link"
    )
    text, markup = card(store.view(rid), SHANGHAI)
    assert "🪁 按邀请人数排名，前 1 名获奖\n📊 统计 " in text
    assert "起用专属邀请链接进群的新成员\n" in text
    assert [b.callback_data for b in markup.inline_keyboard[0]] == [f"invite:{rid}", f"rank:{rid}"]
    query = SimpleNamespace(
        data=f"invite:{rid}",
        answer=AsyncMock(),
        from_user=SimpleNamespace(id=123, full_name="Alice", is_bot=False),
    )
    bot = fake_bot(username="lottery_test_bot")
    asyncio.run(handlers.callback(SimpleNamespace(callback_query=query), SimpleNamespace(bot=bot)))
    assert query.answer.await_args.kwargs == {
        "url": f"https://t.me/lottery_test_bot?start=inv{GROUP['id']}"
    }


def test_standing_in_an_invite_raffle():
    rank = {"kind": "rank", "winner_count": 1, "invite_via": "link"}
    ranked = [{"user_id": 1, "display_name": "Eve", "invites": 3}]
    assert (
        standing_text(rank, ranked, 1)
        == "📊 邀请排名 · 前 1 名获奖\n1. Eve · 3 人\n你：第 1 名 · 3 人"
    )
    assert standing_text(rank, ranked, 2).endswith("你还没有邀请到新成员，邀请好友进群即可参与排名")
    reach = {"kind": "reach", "winner_count": 1, "min_messages": 5, "invite_via": "add"}
    assert standing_text(reach, ranked, 1) == (
        "📊 邀请满 5 人即可参与抽奖\n已达标 0 人\n你已邀请 3 人，还差 2 人"
    )


def test_members_join_a_report_raffle_by_joining_its_report_group(inviting):
    store, now, handlers = inviting
    store.remember_group(-200, "报道群")
    rid = store.create(
        99, "报道", 2, 60, chat_id=GROUP["id"], report_chat=-200, report_link="https://t.me/r"
    )
    text, markup = card(store.view(rid), SHANGHAI)
    assert "👉 加入「报道群」即可参与；已经在里面的，点「✅ 我已加入报道群」" in text
    buttons = markup.inline_keyboard[0]
    assert [(b.text, b.url, b.callback_data) for b in buttons] == [
        ("➡️ 进入报道群", "https://t.me/r", None),
        ("✅ 我已加入报道群", None, f"join:{rid}"),
    ]
    in_group_a = {11, 123}

    async def lookup(chat_id, user_id):
        member = chat_id == GROUP["id"] and user_id in in_group_a
        return SimpleNamespace(status=ChatMember.MEMBER if member else ChatMember.LEFT)

    bot = fake_bot(get_chat_member=AsyncMock(side_effect=lookup))
    context = SimpleNamespace(bot=bot, job_queue=None)
    for user_id in (11, 12):  # a member of the raffle's group, and someone else
        update = arrival(user_id, now[0] + 10, by=user_id)
        update.chat_member.chat = SimpleNamespace(id=-200)
        asyncio.run(handlers.member_changed(update, context))
    assert [e["user_id"] for e in store.view(rid)["entries"]] == [11]
    # Already in the report group before: the card's button, which checks both groups.
    assert (
        join_click(handlers, rid, AsyncMock(side_effect=lookup))
        == "请先加入「报道群」，加入后会自动报名。"
    )

    async def both(chat_id, user_id):
        return SimpleNamespace(status=ChatMember.MEMBER)

    assert join_click(handlers, rid, AsyncMock(side_effect=both)) == "报名成功！"
    assert [e["user_id"] for e in store.view(rid)["entries"]] == [11, 123]

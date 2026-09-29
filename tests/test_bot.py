import asyncio
import logging
import sys
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from telegram import ChatMember, Update
from telegram.error import BadRequest, ChatMigrated, Forbidden, TimedOut

from lottery.bot import (
    ALLOWED_UPDATES,
    BotHandlers,
    Settings,
    TokenFilter,
    build_application,
    main,
    on_error,
)
from lottery.core import LotteryError, Store
from lottery.views import card, chunks, result_text

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
    asyncio.run(handlers.migrate(SimpleNamespace(effective_message=message), None))
    assert store.view(rid)["chat_id"] == -1001


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
    assert len(app.handlers[0]) == 10
    assert app.concurrent_updates == 1
    assert {"chat_member", "my_chat_member"} <= set(ALLOWED_UPDATES)
    assert [job.callback.__name__ for job in app.job_queue.jobs()] == ["auto_draw", "cleanup"]


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


def test_button_error_is_answered_privately_not_in_group():
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
    asyncio.run(on_error(update, SimpleNamespace(error=RuntimeError("database is locked"))))
    bot.send_message.assert_not_awaited()
    assert bot.answer_callback_query.await_args.kwargs["show_alert"] is True


def test_command_error_still_replies_in_chat():
    message = {"message_id": 5, "date": 0, "chat": GROUP, "from": USER, "text": "/draw 1"}
    update, bot = telegram_update({"message": message})
    asyncio.run(on_error(update, SimpleNamespace(error=RuntimeError("network down"))))
    assert "请重试" in bot.send_message.await_args.kwargs["text"]


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


def member_update(status, left_at, **fields):
    change = SimpleNamespace(
        chat=SimpleNamespace(id=GROUP["id"]),
        date=datetime.fromtimestamp(left_at, UTC),
        new_chat_member=SimpleNamespace(status=status, user=SimpleNamespace(id=123), **fields),
    )
    return SimpleNamespace(chat_member=change)


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

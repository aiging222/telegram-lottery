import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from telegram import ChatMember
from telegram.error import Forbidden

from lottery.bot import BotHandlers
from lottery.core import LotteryError, Store
from lottery.menu import Menu, parse_deadline

SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = 1_800_000_000.0  # 2027-01-15 16:00 in Shanghai
GROUP = -100
ADMIN = 99  # super admin from ADMIN_USER_IDS
OWNER = 5  # the group's owner
MEMBER = 6


@pytest.fixture
def env(tmp_path):
    now = [NOW]
    store = Store(tmp_path / "menu.sqlite3", clock=lambda: now[0])
    store.remember_group(GROUP, "测试群")
    handlers = BotHandlers(store, {ADMIN}, SHANGHAI)
    statuses = {(GROUP, OWNER): ChatMember.OWNER, (GROUP, MEMBER): ChatMember.MEMBER}

    async def get_chat_member(chat_id, user_id):
        return SimpleNamespace(status=statuses.get((chat_id, user_id), ChatMember.LEFT))

    bot = SimpleNamespace(
        username="lottery_test_bot",
        get_chat_member=AsyncMock(side_effect=get_chat_member),
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=500)),
        edit_message_text=AsyncMock(),
    )
    return SimpleNamespace(
        store=store, menu=Menu(handlers), handlers=handlers, bot=bot, now=now, statuses=statuses
    )


def labels(markup):
    buttons = [b for row in markup.inline_keyboard for b in row] if markup else []
    return {b.text: b.callback_data or b.url for b in buttons}


def press(env, user_id, data):
    query = SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=user_id, is_bot=False),
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    context = SimpleNamespace(bot=env.bot)
    asyncio.run(env.menu.callback(SimpleNamespace(callback_query=query), context))
    return query


def shown(query):
    call = query.edit_message_text.await_args
    return call.args[0], labels(call.kwargs["reply_markup"])


def type_text(env, user_id, text):
    message = SimpleNamespace(text=text, reply_text=AsyncMock())
    update = SimpleNamespace(effective_message=message, effective_user=SimpleNamespace(id=user_id))
    asyncio.run(env.menu.text(update, SimpleNamespace(bot=env.bot)))
    call = message.reply_text.await_args
    return call.args[0], labels(call.kwargs.get("reply_markup"))


def start(env, user_id, *args, chat_type="private"):
    message = SimpleNamespace(reply_text=AsyncMock())
    chat_id = user_id if chat_type == "private" else GROUP
    update = SimpleNamespace(
        effective_message=message,
        effective_user=SimpleNamespace(id=user_id, is_bot=False),
        effective_chat=SimpleNamespace(type=chat_type, id=chat_id, title="测试群"),
    )
    asyncio.run(env.menu.start(update, SimpleNamespace(bot=env.bot, args=list(args))))
    return message


def test_start_offers_to_add_the_bot_to_a_group(env):
    call = start(env, MEMBER).reply_text.await_args
    assert call.args[0].startswith("🎁 抽奖助手")
    assert labels(call.kwargs["reply_markup"]) == {
        "➕ 添加到群组": "https://t.me/lottery_test_bot?startgroup=menu&admin=delete_messages+pin_messages",
        "📋 我的群": "m:groups",
    }


def test_group_link_opens_the_menu_for_group_admins_only(env):
    text = start(env, OWNER, f"g{GROUP}").reply_text.await_args.args[0]
    assert text == "🎁 测试群 · 抽奖\n进行中 0 ｜ 已开奖 0 ｜ 已取消 0"
    denied = start(env, MEMBER, f"g{GROUP}").reply_text.await_args.args[0]
    assert denied == "只有该群的管理员可以管理抽奖。"
    env.bot.get_chat_member.reset_mock()
    start(env, ADMIN, f"g{GROUP}")
    env.bot.get_chat_member.assert_not_awaited()  # super admins need no lookup


def test_start_in_a_group_posts_the_menu_button(env):
    start(env, MEMBER, chat_type="supergroup")
    call = env.bot.send_message.await_args
    assert call.args[0] == GROUP
    assert labels(call.kwargs["reply_markup"]) == {
        "⚙️ 管理抽奖": f"https://t.me/lottery_test_bot?start=g{GROUP}"
    }


def test_members_cannot_use_admin_buttons(env):
    query = press(env, MEMBER, f"m:new:{GROUP}")
    assert query.answer.await_args.args[0] == "只有该群的管理员可以管理抽奖。"
    query.edit_message_text.assert_not_awaited()
    assert env.store.draft(MEMBER) is None


def test_admin_lookup_fails_closed_and_is_cached(env):
    env.bot.get_chat_member.side_effect = Forbidden("bot was kicked")
    assert not asyncio.run(env.menu.can_manage(env.bot, OWNER, GROUP))
    assert not asyncio.run(env.menu.can_manage(env.bot, OWNER, GROUP))
    assert env.bot.get_chat_member.await_count == 1


def test_my_groups_lists_only_groups_the_user_manages(env):
    env.store.remember_group(-200, "别人的群")
    text, buttons = shown(press(env, OWNER, "m:groups"))
    assert text == "选择要管理的群："
    assert buttons["测试群"] == f"m:g:{GROUP}"
    assert "别人的群" not in buttons
    text, _ = shown(press(env, MEMBER, "m:groups"))
    assert text.startswith("还没有你能管理的群")


def test_wizard_publishes_a_timed_raffle(env):
    text, _ = shown(press(env, OWNER, f"m:new:{GROUP}"))
    assert text == "① 奖品名称？直接发送文字。"
    text, buttons = type_text(env, OWNER, "坦克300")
    assert text == "② 中奖人数"
    text, buttons = shown(press(env, OWNER, buttons["2"]))
    assert text == "③ 开奖方式"
    text, buttons = shown(press(env, OWNER, buttons["⏰ 定时开奖"]))
    assert text == "④ 多久后开奖？"
    text, buttons = shown(press(env, OWNER, buttons["1天"]))
    assert text == "坦克300\n2 人中奖 · 开奖时间：2027-01-16 16:00（UTC+08:00）\n确认后发布到群。"
    text, _ = shown(press(env, OWNER, buttons["✅ 发布到群"]))
    assert text == "✅ 已发布到群。"
    (raffle,), _ = env.store.group_raffles(GROUP, 0, 8)
    assert (raffle["title"], raffle["winner_count"], raffle["deadline"]) == (
        "坦克300",
        2,
        NOW + 86400,
    )
    assert raffle["card_message_id"] == 500
    chat_id, card_text = env.bot.send_message.await_args.args
    assert chat_id == GROUP
    assert card_text.startswith("🎁 坦克300")
    assert env.store.draft(OWNER) is None


def test_wizard_typed_values_and_full_mode(env):
    press(env, OWNER, f"m:new:{GROUP}")
    type_text(env, OWNER, "耳机")
    text, _ = shown(press(env, OWNER, "m:c:x"))
    assert text == "请输入中奖人数（1～100）。"
    text, _ = type_text(env, OWNER, "abc")
    assert text == "中奖人数需为 1～100 的整数，请重新输入。"
    text, buttons = type_text(env, OWNER, "20")
    assert text == "③ 开奖方式"
    text, buttons = shown(press(env, OWNER, buttons["👥 满人开奖"]))
    assert text == "④ 满多少人开奖？"
    assert "10" not in buttons  # fewer people than winners makes no sense
    text, buttons = shown(press(env, OWNER, buttons["50"]))
    assert "满 50 人开奖（最晚 2027-01-22 16:00（UTC+08:00））" in text
    press(env, OWNER, buttons["✅ 发布到群"])
    (raffle,), _ = env.store.group_raffles(GROUP, 0, 8)
    assert (raffle["winner_count"], raffle["target_count"]) == (20, 50)


def test_wizard_typed_deadline(env):
    press(env, OWNER, f"m:new:{GROUP}")
    type_text(env, OWNER, "耳机")
    press(env, OWNER, "m:c:1")
    press(env, OWNER, "m:mode:t")
    press(env, OWNER, "m:t:x")
    text, _ = type_text(env, OWNER, "0分钟")
    assert text == "开奖时间需在 1 分钟到 365 天之后，请重新输入。"
    text, _ = type_text(env, OWNER, "01-20 20:00")
    assert "开奖时间：2027-01-20 20:00（UTC+08:00）" in text


@pytest.mark.parametrize(
    "typed, expected",
    [
        ("90分钟", NOW + 5400),
        ("2小时", NOW + 7200),
        ("3天", NOW + 259200),
        ("45", NOW + 2700),
        ("2 H", NOW + 7200),
        ("2027-01-20 20:00", datetime(2027, 1, 20, 20, tzinfo=SHANGHAI).timestamp()),
        ("01-20 20：00", datetime(2027, 1, 20, 20, tzinfo=SHANGHAI).timestamp()),
    ],
)
def test_parse_deadline(typed, expected):
    assert parse_deadline(typed, NOW, SHANGHAI) == expected


def test_parse_deadline_rejects_nonsense():
    with pytest.raises(LotteryError):
        parse_deadline("明天晚上", NOW, SHANGHAI)


def test_wizard_can_be_abandoned_and_expires(env):
    press(env, OWNER, f"m:new:{GROUP}")
    text, _ = shown(press(env, OWNER, "m:quit"))
    assert text.startswith("🎁 测试群 · 抽奖")
    assert env.store.draft(OWNER) is None
    assert press(env, OWNER, "m:c:2").answer.await_args.args[0] == "操作已过期，请重新开始。"
    text, _ = type_text(env, OWNER, "随便说点什么")
    assert text == "发送 /start 打开菜单。"


def test_draw_now_from_records(env):
    rid = env.store.create(OWNER, "耳机", 1, 60, chat_id=GROUP)
    env.store.set_card(rid, 500)
    env.store.join(rid, 1, "Alice")
    text, buttons = shown(press(env, OWNER, f"m:list:{GROUP}:0"))
    assert buttons["耳机 · 报名中"] == f"m:r:{rid}"
    text, buttons = shown(press(env, OWNER, f"m:r:{rid}"))
    assert {"🎲 立即开奖", "⏹ 截止报名", "✖ 取消抽奖", "📣 重新发布"} <= set(buttons)
    text, buttons = shown(press(env, OWNER, buttons["🎲 立即开奖"]))
    assert text.endswith("确定现在开奖？")
    text, buttons = shown(press(env, OWNER, buttons["✅ 确定"]))
    assert "已开奖" in text
    assert "中奖：Alice" in text
    announcement = env.bot.send_message.await_args
    assert announcement.args[0] == GROUP
    assert 'tg://user?id=1"' in announcement.args[1]
    assert env.bot.edit_message_text.await_args.kwargs["message_id"] == 500  # card closed
    assert env.store.pending_announcements() == []


def test_cancel_from_records(env):
    rid = env.store.create(OWNER, "耳机", 1, 60, chat_id=GROUP)
    env.store.set_card(rid, 500)
    text, buttons = shown(press(env, OWNER, f"m:ask:cancel:{rid}"))
    assert text.endswith("确定取消这场抽奖？取消后不能恢复。")
    text, buttons = shown(press(env, OWNER, buttons["✅ 确定"]))
    assert "已取消" in text
    assert "🎲 立即开奖" not in buttons
    assert env.bot.send_message.await_args.args == (GROUP, "「耳机」抽奖已取消。")
    card_edit = env.bot.edit_message_text.await_args
    assert card_edit.args[0].endswith("已取消")
    assert card_edit.kwargs["reply_markup"] is None
    assert env.store.group_summary(GROUP)["cancelled"] == 1


def test_freeze_and_repost_from_records(env):
    rid = env.store.create(OWNER, "耳机", 1, 60, chat_id=GROUP)
    shown(press(env, OWNER, f"m:repost:{rid}"))
    assert env.store.view(rid)["card_message_id"] == 500
    text, buttons = shown(press(env, OWNER, f"m:do:freeze:{rid}"))
    assert "报名已截止" in text
    assert "⏹ 截止报名" not in buttons
    query = press(env, OWNER, f"m:repost:{rid}")
    assert query.answer.await_args.args[0] == "只有报名中的抽奖可以重新发布。"


def test_records_of_another_group_are_off_limits(env):
    rid = env.store.create(ADMIN, "别的群", 1, 60, chat_id=-200)
    query = press(env, OWNER, f"m:r:{rid}")
    assert query.answer.await_args.args[0] == "只有该群的管理员可以管理抽奖。"


def test_last_join_of_a_full_raffle_draws_at_once(env):
    rid = env.store.create(OWNER, "满人", 1, 60, chat_id=GROUP, target=2)
    for uid in (1, 2):
        env.statuses[(GROUP, uid)] = ChatMember.MEMBER
        query = SimpleNamespace(
            data=f"join:{rid}",
            answer=AsyncMock(),
            from_user=SimpleNamespace(id=uid, full_name=f"user{uid}", is_bot=False),
        )
        context = SimpleNamespace(bot=env.bot)
        asyncio.run(env.handlers.callback(SimpleNamespace(callback_query=query), context))
        assert query.answer.await_args.args[0] == "报名成功！"
    assert env.store.view(rid)["status"] == "DRAWN"
    chat_id, text = env.bot.send_message.await_args.args
    assert chat_id == GROUP
    assert "满人 开奖结果" in text


def test_bot_added_to_group_posts_welcome_and_is_remembered(env):
    change = SimpleNamespace(
        chat=SimpleNamespace(id=-300, type="supergroup", title="新群"),
        old_chat_member=SimpleNamespace(status=ChatMember.LEFT),
        new_chat_member=SimpleNamespace(status=ChatMember.ADMINISTRATOR),
    )
    context = SimpleNamespace(bot=env.bot)
    asyncio.run(env.menu.bot_membership(SimpleNamespace(my_chat_member=change), context))
    call = env.bot.send_message.await_args
    assert call.args == (-300, "✅ 已就绪。群管理员点击下面按钮发起和管理抽奖。")
    assert labels(call.kwargs["reply_markup"]) == {
        "⚙️ 管理抽奖": "https://t.me/lottery_test_bot?start=g-300"
    }
    assert (-300, "新群") in env.store.groups()
    change.old_chat_member = change.new_chat_member
    change.new_chat_member = SimpleNamespace(status=ChatMember.LEFT)
    asyncio.run(env.menu.bot_membership(SimpleNamespace(my_chat_member=change), context))
    assert (-300, "新群") not in env.store.groups()
    assert env.bot.send_message.await_count == 1


def test_bot_added_without_admin_rights_asks_for_them(env):
    change = SimpleNamespace(
        chat=SimpleNamespace(id=-300, type="group", title="新群"),
        old_chat_member=SimpleNamespace(status=ChatMember.LEFT),
        new_chat_member=SimpleNamespace(status=ChatMember.MEMBER),
    )
    context = SimpleNamespace(bot=env.bot)
    asyncio.run(env.menu.bot_membership(SimpleNamespace(my_chat_member=change), context))
    assert env.bot.send_message.await_args.args[1].startswith("请把我设为管理员")

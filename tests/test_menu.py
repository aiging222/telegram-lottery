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
from lottery.menu import NOT_MANAGER, Menu, parse_deadline

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
        send_document=AsyncMock(),
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


HEADER = "🎁 发起抽奖（/cancel 退出）\n\n"


def test_wizard_publishes_a_timed_raffle(env):
    text, _ = shown(press(env, OWNER, f"m:new:{GROUP}"))
    assert text == HEADER + "请发送奖品名称，例如：1USDT"
    text, buttons = type_text(env, OWNER, "1usdt")
    assert text.endswith("「1usdt」有几份？点按钮或直接发送数字：")
    text, buttons = shown(press(env, OWNER, buttons["2"]))
    assert text == HEADER + "├ 奖品：1usdt ×2\n\n怎么开奖？"
    text, buttons = shown(press(env, OWNER, buttons["⏰ 定时开奖"]))
    assert text.endswith("🕒 现在是 2027-01-15 16:00（UTC+08:00）")
    text, buttons = shown(press(env, OWNER, buttons["1天"]))
    assert text.endswith("├ 开奖时间：2027-01-16 16:00（UTC+08:00）\n\n怎么参与？")
    text, buttons = shown(press(env, OWNER, buttons["🎟 点按钮参与"]))
    assert text.endswith("最后，请发送抽奖活动名称：")
    text, buttons = type_text(env, OWNER, "周末福利")
    assert text == HEADER + (
        "周末福利\n├ 奖品：1usdt ×2\n├ 开奖时间：2027-01-16 16:00（UTC+08:00）\n├ 点按钮参与\n\n"
        "🎉 已填写完成，发布到「测试群」？"
    )
    assert set(buttons) == {"✅ 发布抽奖", "❌ 取消发布", "➕ 添加奖品", "🔄 换个群"}
    text, _ = shown(press(env, OWNER, buttons["✅ 发布抽奖"]))
    assert text == "✅ 已发布到群。"
    (row,), _ = env.store.group_raffles(GROUP, 0, 8)
    raffle = env.store.view(row["id"])
    assert (raffle["title"], raffle["winner_count"], raffle["deadline"]) == (
        "周末福利",
        2,
        NOW + 86400,
    )
    assert (raffle["prizes"], raffle["keyword"], raffle["card_message_id"]) == (
        [["1usdt", 2]],
        None,
        500,
    )
    chat_id, card_text = env.bot.send_message.await_args.args
    assert chat_id == GROUP
    assert card_text.startswith("🎁 周末福利  #1\n🏆 1usdt ×2\n")
    assert env.store.draft(OWNER) is None


def test_wizard_several_prizes_keyword_and_full_mode(env):
    press(env, OWNER, f"m:new:{GROUP}")
    type_text(env, OWNER, "iPhone")
    text, _ = type_text(env, OWNER, "1")  # counts can be typed instead of pressed
    assert text.endswith("怎么开奖？")
    assert type_text(env, OWNER, "满人")[0] == "请点上面消息里的按钮选择，或发送 /cancel 退出。"
    text, buttons = shown(press(env, OWNER, "m:mode:f"))
    assert text.endswith("满多少人开奖？点按钮或直接发送数字（至少 1）：")
    assert type_text(env, OWNER, "abc")[0] == "满人开奖人数需为 1～100000 的整数，请重新输入。"
    text, _ = type_text(env, OWNER, "3")
    assert "├ 满 3 人开奖（最晚 2027-01-22 16:00（UTC+08:00））" in text
    text, _ = shown(press(env, OWNER, "m:j:k"))
    assert text.endswith("请发送参与口令，群友在群里发这句话就能参与，例如：帅哥")
    assert type_text(env, OWNER, "/start")[0] == "口令需为 1～32 字，且不能以 / 开头。"
    text, buttons = type_text(env, OWNER, "帅哥")
    text, buttons = shown(press(env, OWNER, buttons["用「iPhone」作名称"]))
    assert "iPhone\n├ 奖品：iPhone ×1\n" in text
    assert "├ 在群里发送「帅哥」参与" in text
    text, _ = shown(press(env, OWNER, buttons["➕ 添加奖品"]))
    assert text.endswith("请发送下一个奖品的名称：")
    type_text(env, OWNER, "1usdt")
    assert type_text(env, OWNER, "3")[0] == "中奖总人数不能超过满人开奖人数 3。"
    text, buttons = type_text(env, OWNER, "2")
    assert "├ 奖品：iPhone ×1、1usdt ×2" in text
    assert "➕ 添加奖品" not in buttons  # every place is taken
    press(env, OWNER, "m:pub")
    raffle = env.store.view(1)
    assert (raffle["winner_count"], raffle["target_count"], raffle["keyword"]) == (3, 3, "帅哥")
    assert raffle["prizes"] == [["iPhone", 1], ["1usdt", 2]]
    card = env.bot.send_message.await_args
    assert "👉 在群里发送「帅哥」参与" in card.args[1]
    assert card.kwargs["reply_markup"] is None  # no button to press


def test_wizard_typed_deadline(env):
    press(env, OWNER, f"m:new:{GROUP}")
    type_text(env, OWNER, "耳机")
    press(env, OWNER, "m:c:1")
    press(env, OWNER, "m:mode:t")
    text, _ = type_text(env, OWNER, "0分钟")
    assert text == "开奖时间需在 1 分钟到 365 天之后，请重新输入。"
    text, _ = type_text(env, OWNER, "01-20 20:00")
    assert "├ 开奖时间：2027-01-20 20:00（UTC+08:00）" in text


def test_wizard_can_publish_to_another_group(env):
    env.store.remember_group(-200, "二群")
    env.store.remember_group(-300, "别人的群")
    env.statuses[(-200, OWNER)] = ChatMember.ADMINISTRATOR
    fill_wizard(env, OWNER)
    text, buttons = shown(press(env, OWNER, "m:to"))
    assert text == "发布到哪个群？"
    assert buttons == {"✅ 测试群": f"m:to:{GROUP}", "二群": "m:to:-200", "⬅️ 返回": "m:cur"}
    text, _ = shown(press(env, OWNER, "m:to:-200"))
    assert text.endswith("发布到「二群」？")
    assert press(env, OWNER, "m:to:-300").answer.await_args.args[0] == NOT_MANAGER
    press(env, OWNER, "m:pub")
    assert env.store.view(1)["chat_id"] == -200
    assert env.bot.send_message.await_args.args[0] == -200


def test_cancel_command_leaves_the_wizard(env):
    press(env, OWNER, f"m:new:{GROUP}")
    for expected in ("🎁 测试群 · 抽奖", "已退出。发送 /start 打开菜单。"):
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(
            effective_message=message, effective_user=SimpleNamespace(id=OWNER)
        )
        asyncio.run(env.menu.cancel(update, SimpleNamespace(bot=env.bot)))
        assert message.reply_text.await_args.args[0].startswith(expected)
    assert env.store.draft(OWNER) is None


def say(env, user_id, text, chat_id=GROUP):
    """A group message, as the keyword handler sees it."""
    message = SimpleNamespace(
        text=text, chat=SimpleNamespace(id=chat_id), sender_chat=None, set_reaction=AsyncMock()
    )
    user = SimpleNamespace(id=user_id, full_name=f"user{user_id}", is_bot=False)
    update = SimpleNamespace(effective_message=message, effective_user=user)
    asyncio.run(env.handlers.keyword(update, SimpleNamespace(bot=env.bot, job_queue=None)))
    return message


def test_joining_by_keyword(env):
    rid = env.store.create(OWNER, "口令", 1, 60, chat_id=GROUP, keyword="Hello")
    assert say(env, 1, " hello ").set_reaction.await_args.args == ("🎉",)
    say(env, 1, "HELLO").set_reaction.assert_not_awaited()  # already joined
    say(env, 2, "hello there").set_reaction.assert_not_awaited()
    say(env, 3, "hello", chat_id=-200).set_reaction.assert_not_awaited()
    assert [e["user_id"] for e in env.store.view(rid)["entries"]] == [1]
    full = env.store.create(OWNER, "满人", 1, 60, chat_id=GROUP, keyword="go", target=1)
    say(env, 4, "go")
    assert env.store.view(full)["status"] == "DRAWN"  # the last place draws at once


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


@pytest.fixture
def weighted_env(env):
    rid = env.store.create(OWNER, "耳机", 1, 60, chat_id=GROUP)
    env.store.set_card(rid, 500)
    env.store.join(rid, 1, "Alice")
    env.store.join(rid, 2, "Bob")
    env.rid = rid
    return env


def test_only_super_admins_see_the_bonus_button(weighted_env):
    env, rid = weighted_env, weighted_env.rid
    _, buttons = shown(press(env, OWNER, f"m:r:{rid}"))
    assert "⚖️ 中奖加成" not in buttons
    _, buttons = shown(press(env, ADMIN, f"m:r:{rid}"))
    assert buttons["⚖️ 中奖加成"] == f"m:w:{rid}:0"
    for data in (f"m:w:{rid}:0", f"m:ws:{rid}:1:10:0", f"m:export:{rid}:0"):
        assert (
            press(env, OWNER, data).answer.await_args.args[0] == "只有超级管理员可以设置中奖加成。"
        )
    assert env.store.view(rid)["entries"][0]["override"] is None
    env.bot.send_document.assert_not_awaited()


def test_set_weights_with_buttons(weighted_env):
    env, rid = weighted_env, weighted_env.rid
    text, buttons = shown(press(env, ADMIN, f"m:w:{rid}:0"))
    assert text == "⚖️ 中奖加成 · 耳机  #1\n默认权重 1 · 上限 100\n已参与 2 人 · 已调整 0 人"
    text, buttons = shown(press(env, ADMIN, buttons["Alice · 1 · 50%"]))
    assert text == "Alice（ID：1）\n权重 1 · 首轮 50% · 默认"
    assert {"0 排除", "1", "2", "3", "5", "10", "其他"} <= set(buttons)
    assert "↩️ 恢复默认" not in buttons
    text, buttons = shown(press(env, ADMIN, buttons["3"]))
    assert text.endswith("已调整 1 人")
    assert {"Alice · 3 · 75%", "Bob · 1 · 25%"} <= set(buttons)
    card = env.bot.edit_message_text.await_args
    assert card.kwargs["message_id"] == 500
    assert "本场设有中奖加成" in card.args[0]  # the first weight adds the notice
    text, buttons = shown(press(env, ADMIN, buttons["Alice · 3 · 75%"]))
    assert text.endswith("· 单独设置")
    _, buttons = shown(press(env, ADMIN, buttons["↩️ 恢复默认"]))
    assert "Alice · 1 · 50%" in buttons
    assert env.bot.edit_message_text.await_count == 1  # the notice was already there
    assert env.store.view(rid)["weighted"]


def test_typed_weights_presets_and_defaults(weighted_env):
    env, rid = weighted_env, weighted_env.rid
    text, _ = shown(press(env, ADMIN, f"m:ws:{rid}:1:x:0"))
    assert text == "请输入权重（0～100），0 表示不参与抽取。"
    assert type_text(env, ADMIN, "abc")[0] == "权重需为 0～100 的整数，请重新输入。"
    text, buttons = type_text(env, ADMIN, "7")
    assert "Alice · 7 · 88%" in buttons
    shown(press(env, ADMIN, f"m:wid:{rid}:0"))
    assert type_text(env, ADMIN, "12345")[0].startswith("请发送用户 ID 和权重")
    _, buttons = type_text(env, ADMIN, "12345 5")
    text, buttons = shown(press(env, ADMIN, buttons["ID 12345 · 5（未报名）"]))
    assert text == "用户 12345（未报名）\n预设权重 5，报名后生效"
    text, _ = shown(press(env, ADMIN, f"m:wc:{rid}:0"))
    assert text.startswith("当前默认权重 1，上限 100。")
    assert type_text(env, ADMIN, "5 3")[0] == "默认权重不能超过上限。"
    text, _ = type_text(env, ADMIN, "2 50")
    assert "默认权重 2 · 上限 50" in text
    weights = {e["user_id"]: e["weight"] for e in env.store.view(rid)["entries"]}
    assert weights == {1: 7, 2: 2}


def test_another_button_drops_the_typed_answer(weighted_env):
    env, rid = weighted_env, weighted_env.rid
    press(env, ADMIN, f"m:ws:{rid}:1:x:0")
    press(env, ADMIN, f"m:w:{rid}:0")
    assert type_text(env, ADMIN, "5")[0] == "发送 /start 打开菜单。"
    assert env.store.view(rid)["entries"][0]["override"] is None


def test_weights_lock_when_signup_closes(weighted_env):
    env, rid = weighted_env, weighted_env.rid
    env.store.override(rid, ADMIN, 1, 3)
    env.store.freeze(rid, ADMIN)
    text, buttons = shown(press(env, ADMIN, f"m:w:{rid}:0"))
    assert text.endswith("报名已截止，权重已锁定。\nAlice · 3 · 75%\nBob · 1 · 25%")
    assert set(buttons) == {"📄 导出记录", "⬅️ 返回"}
    query = press(env, ADMIN, f"m:ws:{rid}:1:10:0")
    assert query.answer.await_args.args[0] == "报名已截止，权重已锁定。"


def test_export_goes_to_the_super_admin(weighted_env):
    env, rid = weighted_env, weighted_env.rid
    press(env, ADMIN, f"m:export:{rid}:0")
    call = env.bot.send_document.await_args
    assert call.args == (ADMIN,)
    assert call.kwargs["filename"] == f"lottery-{rid}.json"


def fill_wizard(env, user_id):
    press(env, user_id, f"m:new:{GROUP}")
    type_text(env, user_id, "坦克300")
    press(env, user_id, "m:c:1")
    press(env, user_id, "m:mode:t")
    press(env, user_id, "m:t:1440")
    press(env, user_id, "m:j:b")
    return type_text(env, user_id, "坦克300 抽奖")


def test_super_admins_can_publish_with_the_notice_from_the_start(env):
    _, buttons = fill_wizard(env, ADMIN)
    text, buttons = shown(press(env, ADMIN, buttons["⚖️ 中奖加成：关"]))
    assert "\n本场设有中奖加成\n" in text
    assert "⚖️ 中奖加成：开" in buttons
    _, buttons = shown(press(env, ADMIN, "m:pub"))
    (raffle,), _ = env.store.group_raffles(GROUP, 0, 8)
    assert buttons["⚖️ 设置加成"] == f"m:w:{raffle['id']}:0"
    assert "本场设有中奖加成" in env.bot.send_message.await_args.args[1]


def test_group_admins_get_no_bonus_switch(env):
    _, buttons = fill_wizard(env, OWNER)
    assert not any(label.startswith("⚖️") for label in buttons)
    assert press(env, OWNER, "m:wz").answer.await_args.args[0] == "只有超级管理员可以设置中奖加成。"
    _, buttons = shown(press(env, OWNER, "m:pub"))
    assert "⚖️ 设置加成" not in buttons
    assert "加成" not in env.bot.send_message.await_args.args[1]

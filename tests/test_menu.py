import asyncio
import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from telegram import ChatMember
from telegram.error import BadRequest, ChatMigrated, Forbidden, TimedOut

from lottery.bot import BotHandlers
from lottery.core import LotteryError, Store
from lottery.menu import NOT_MANAGER, STALE, Menu, parse_time

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

    async def get_chat_administrators(chat_id):
        return [
            SimpleNamespace(user=SimpleNamespace(id=user_id))
            for (chat, user_id), status in statuses.items()
            if chat == chat_id and status in (ChatMember.OWNER, ChatMember.ADMINISTRATOR)
        ]

    bot = SimpleNamespace(
        id=4242,
        username="lottery_test_bot",
        get_chat_member=AsyncMock(side_effect=get_chat_member),
        get_chat_administrators=AsyncMock(side_effect=get_chat_administrators),
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=500)),
        edit_message_text=AsyncMock(),
        send_document=AsyncMock(),
        pin_chat_message=AsyncMock(),
        unpin_chat_message=AsyncMock(),
        delete_messages=AsyncMock(),
    )
    return SimpleNamespace(
        store=store, menu=Menu(handlers), handlers=handlers, bot=bot, now=now, statuses=statuses
    )


def labels(markup):
    buttons = [b for row in markup.inline_keyboard for b in row] if markup else []
    return {b.text: b.callback_data or b.url for b in buttons}


def query_for(user_id, data):
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=user_id, is_bot=False),
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )


def press(env, user_id, data):
    query = query_for(user_id, data)
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
    message = SimpleNamespace(message_id=11, reply_text=AsyncMock())
    chat_id = user_id if chat_type == "private" else GROUP
    update = SimpleNamespace(
        effective_message=message,
        effective_user=SimpleNamespace(id=user_id, is_bot=False, full_name=f"user{user_id}"),
        effective_chat=SimpleNamespace(type=chat_type, id=chat_id, title="测试群"),
    )
    asyncio.run(env.menu.start(update, SimpleNamespace(bot=env.bot, args=list(args))))
    return message


def test_start_offers_to_add_the_bot_to_a_group(env):
    call = start(env, MEMBER).reply_text.await_args
    assert call.args[0].startswith("🎁 抽奖助手")
    assert labels(call.kwargs["reply_markup"]) == {
        "➕ 添加到群组": (
            "https://t.me/lottery_test_bot?startgroup=menu"
            "&admin=delete_messages+pin_messages+restrict_members+invite_users"
        ),
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
    text, _ = shown(press(env, OWNER, "m:groups"))
    assert text.startswith("还没有你能管理的群")  # never seen managing a group yet
    start(env, OWNER, f"g{GROUP}")  # the group's "⚙️ 管理抽奖" button
    text, buttons = shown(press(env, OWNER, "m:groups"))
    assert text == "选择要管理的群："
    assert buttons["测试群"] == f"m:g:{GROUP}"
    assert "别人的群" not in buttons
    # Telegram is asked about the user's own groups only, not every group the bot is in.
    assert {call.args[0] for call in env.bot.get_chat_member.await_args_list} == {GROUP}
    text, _ = shown(press(env, MEMBER, "m:groups"))
    assert text.startswith("还没有你能管理的群")
    env.bot.get_chat_member.reset_mock()
    _, buttons = shown(press(env, ADMIN, "m:groups"))
    assert {"测试群", "别人的群"} <= set(buttons)
    env.bot.get_chat_member.assert_not_awaited()


def test_my_groups_drops_a_group_the_user_no_longer_manages(env):
    start(env, OWNER, f"g{GROUP}")
    env.statuses[(GROUP, OWNER)] = ChatMember.MEMBER
    env.menu._managers.clear()  # the 60-second cache has run out
    text, _ = shown(press(env, OWNER, "m:groups"))
    assert text.startswith("还没有你能管理的群")
    assert env.store.managed_groups(OWNER) == []


def test_admins_are_recorded_at_startup(env):
    env.store.remember_group(-200, "二群")
    env.store.set_manager(GROUP, MEMBER, True)  # an admin once, demoted while the bot was away
    lookup = env.bot.get_chat_administrators.side_effect

    async def flaky(chat_id):
        if chat_id == -200:
            raise TimedOut()
        return await lookup(chat_id)

    env.bot.get_chat_administrators.side_effect = flaky
    asyncio.run(env.menu.sync_all_admins(SimpleNamespace(bot=env.bot)))
    assert env.store.managed_groups(OWNER) == [(GROUP, "测试群")]
    assert env.store.managed_groups(MEMBER) == []
    # The owner never pressed the group's button, yet finds the group.
    _, buttons = shown(press(env, OWNER, "m:groups"))
    assert buttons["测试群"] == f"m:g:{GROUP}"


def test_startup_notices_groups_that_changed_while_the_bot_was_away(env):
    env.store.remember_group(-200, "被踢的群")
    env.store.remember_group(-300, "已删除的群")
    env.store.remember_group(-1, "升级了的群")
    env.statuses[(-1001, OWNER)] = ChatMember.OWNER
    errors = {
        -200: Forbidden("bot was kicked from the supergroup chat"),
        -300: BadRequest("Chat not found"),
        -1: ChatMigrated(-1001),
    }
    lookup = env.bot.get_chat_administrators.side_effect

    async def get_chat_administrators(chat_id):
        if chat_id in errors:
            raise errors[chat_id]
        return await lookup(chat_id)

    env.bot.get_chat_administrators.side_effect = get_chat_administrators
    asyncio.run(env.menu.sync_all_admins(SimpleNamespace(bot=env.bot)))
    listed = [(-1001, "升级了的群"), (GROUP, "测试群")]
    assert env.store.groups() == listed  # no longer the groups the bot is gone from
    assert env.store.managed_groups(OWNER) == listed


HEADER = "🎁 发起抽奖（/cancel 退出）\n\n"


def test_wizard_publishes_a_timed_raffle(env):
    text, buttons = shown(press(env, OWNER, f"m:new:{GROUP}"))
    assert text.endswith("选择抽奖类型：")
    assert {"🎟 普通抽奖", "🔥 群活跃抽奖"} <= set(buttons)
    text, _ = shown(press(env, OWNER, buttons["🎟 普通抽奖"]))
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
    assert text == "✅ 已发布到「测试群」。"
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
    press(env, OWNER, "m:k:join")
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


def test_wizard_creates_an_activity_ranking(env):
    press(env, OWNER, f"m:new:{GROUP}")
    text, buttons = shown(press(env, OWNER, "m:k:act"))
    assert {"1️⃣ 根据活跃排名抽奖", "2️⃣ 达到发言次数参与随机抽奖"} <= set(buttons)
    text, _ = shown(press(env, OWNER, buttons["⬅️ 返回选择抽奖类型"]))
    assert text.endswith("选择抽奖类型：")
    press(env, OWNER, "m:k:act")
    text, _ = shown(press(env, OWNER, "m:ka:rank"))
    assert "├ 类型：群活跃抽奖 · 按发言排名" in text
    assert "发言次数从什么时候开始统计？" in text and "本群还没有记录到发言" in text
    too_early = type_text(env, OWNER, "2026-12-01 00:00")[0]
    assert too_early == "统计开始时间需在 30 天前到一年之内，请重新输入。"
    text, buttons = type_text(env, OWNER, "01-15 12:00")  # four hours ago
    assert "├ 统计：从 2027-01-15 12:00（UTC+08:00） 起的文字发言" in text
    assert press(env, OWNER, "m:ka:reach").answer.await_args.args[0] == STALE
    text, buttons = shown(press(env, OWNER, buttons["1小时"]))
    assert text.endswith("请发送第一名的奖品，例如：1USDT")
    assert "👉 结束添加奖品，进入下一步" not in buttons
    text, buttons = type_text(env, OWNER, "10USDT")
    assert "├ 奖品：第一名 10USDT" in text
    assert text.endswith("请发送第二名的奖品，例如：1USDT")
    type_text(env, OWNER, "5USDT")
    text, _ = shown(press(env, OWNER, buttons["👉 结束添加奖品，进入下一步"]))
    assert text.endswith("最后，请发送抽奖活动名称：")
    text, buttons = type_text(env, OWNER, "本周话痨榜")
    assert "├ 奖品：第一名 10USDT、第二名 5USDT" in text
    assert "⚖️ 中奖加成：关" not in buttons
    assert shown(press(env, OWNER, buttons["✅ 发布抽奖"]))[0] == "✅ 已发布到「测试群」。"
    raffle = env.store.view(1)
    assert (raffle["kind"], raffle["winner_count"], raffle["prizes"]) == (
        "rank",
        2,
        [["10USDT", 1], ["5USDT", 1]],
    )
    assert raffle["count_from"] == datetime(2027, 1, 15, 12, tzinfo=SHANGHAI).timestamp()
    assert raffle["deadline"] == NOW + 3600
    card = env.bot.send_message.await_args
    assert "👉 在群里发言即可参与" in card.args[1]
    assert labels(card.kwargs["reply_markup"]) == {"📊 查看我的排名": "rank:1"}


def test_counting_may_start_last_year(env):
    press(env, OWNER, f"m:new:{GROUP}")
    press(env, OWNER, "m:k:act")
    press(env, OWNER, "m:ka:rank")
    text, _ = type_text(env, OWNER, "12-31 18:00")  # fifteen days ago, not this December
    assert "├ 统计：从 2026-12-31 18:00（UTC+08:00） 起的文字发言" in text


def test_wizard_creates_an_activity_draw(env):
    press(env, OWNER, f"m:new:{GROUP}")
    press(env, OWNER, "m:k:act")
    press(env, OWNER, "m:ka:reach")
    text, _ = shown(press(env, OWNER, "m:fr:0"))
    assert "├ 统计：从抽奖发布时起的文字发言" in text
    text, buttons = type_text(env, OWNER, "2小时")
    assert text.endswith("至少发言多少次才能参与抽奖？点按钮或直接发送数字：")
    text, _ = shown(press(env, OWNER, buttons["20"]))
    assert "├ 类型：群活跃抽奖 · 发言满 20 次参与抽奖" in text
    type_text(env, OWNER, "1usdt")
    type_text(env, OWNER, "3")
    press(env, OWNER, "m:tt")
    press(env, OWNER, "m:pub")
    raffle = env.store.view(1)
    assert (raffle["kind"], raffle["winner_count"], raffle["min_messages"]) == ("reach", 3, 20)
    assert (raffle["count_from"], raffle["deadline"]) == (NOW, NOW + 7200)


def test_activity_raffles_in_the_records(env):
    rid = env.store.create(
        OWNER, "话痨榜", 2, 60, chat_id=GROUP, kind="rank", prizes=[["a", 1], ["b", 1]]
    )
    env.store.count_message(GROUP, 7, "Tom", NOW + 30)
    env.now[0] += 90
    text, buttons = shown(press(env, ADMIN, f"m:r:{rid}"))
    assert "🏆 第一名 a、第二名 b\n💬 按发言次数排名，前 2 名获奖\n2 人中奖 · 已发言 1 人" in text
    assert "⚖️ 中奖加成" not in buttons
    text, _ = shown(press(env, ADMIN, buttons["📄 导出记录"]))
    assert text.startswith("话痨榜")
    env.bot.send_document.assert_awaited_once()
    query = press(env, ADMIN, f"m:w:{rid}:0")
    assert query.answer.await_args.args[0].startswith("群活跃抽奖按发言次数决定")


def test_super_admins_correct_message_counts(env):
    rid = env.store.create(
        OWNER, "话痨榜", 2, 60, chat_id=GROUP, kind="rank", prizes=[["a", 1], ["b", 1]]
    )
    env.store.count_message(GROUP, 7, "Tom", NOW + 30)
    env.now[0] += 90
    _, buttons = shown(press(env, OWNER, f"m:r:{rid}"))
    assert "✏️ 修改发言次数" not in buttons
    denied = press(env, OWNER, f"m:ac:{rid}:0").answer.await_args.args[0]
    assert denied == "只有超级管理员可以修改发言次数。"
    _, buttons = shown(press(env, ADMIN, f"m:r:{rid}"))
    text, buttons = shown(press(env, ADMIN, buttons["✏️ 修改发言次数"]))
    assert "已发言 1 人 · 已修改 0 人" in text
    text, buttons = shown(press(env, ADMIN, buttons["1. Tom · 1 次"]))
    assert text == "Tom（ID：7）\n发言 1 次 · 机器人记录 1 次\n当前第 1 名"
    text, buttons = shown(press(env, ADMIN, buttons["+5"]))
    assert text.startswith("Tom（ID：7）\n发言 6 次 · 机器人记录 1 次 · 手动 +5")
    text, _ = shown(press(env, ADMIN, buttons["✏️ 改为…"]))
    assert text == "请发送正确的发言次数（机器人记录 1 次）："
    text, _ = type_text(env, ADMIN, "10")
    assert "发言 10 次 · 机器人记录 1 次 · 手动 +9" in text
    shown(press(env, ADMIN, f"m:aid:{rid}:0"))
    assert type_text(env, ADMIN, "8")[0].startswith("请发送用户 ID 和发言次数")
    text, _ = type_text(env, ADMIN, "8 3")
    assert text.startswith("用户 8（ID：8）\n发言 3 次 · 机器人记录 0 次 · 手动 +3")
    text, buttons = shown(press(env, ADMIN, f"m:ac:{rid}:0"))
    assert {"1. Tom · 10 次（手动 +9）", "2. 用户 8 · 3 次（手动 +3）"} <= set(buttons)
    _, buttons = shown(press(env, ADMIN, buttons["1. Tom · 10 次（手动 +9）"]))
    text, _ = shown(press(env, ADMIN, buttons["↩️ 清除修改"]))
    assert text == "Tom（ID：7）\n发言 1 次 · 机器人记录 1 次\n当前第 2 名"


def test_wizard_typed_deadline(env):
    press(env, OWNER, f"m:new:{GROUP}")
    press(env, OWNER, "m:k:join")
    type_text(env, OWNER, "耳机")
    press(env, OWNER, "m:c:1")
    press(env, OWNER, "m:mode:t")
    text, _ = type_text(env, OWNER, "0分钟")
    assert text == "开奖时间需在 1 分钟到 365 天之后，请重新输入。"
    past = "这个时间已经过了，开奖时间不能早于现在，请重新输入；明年的日期请写上年份。"
    for gone in ("01-14 20:00", "01-15 15:00", "2027-01-10 12:00"):  # never moved to 2028
        assert type_text(env, OWNER, gone)[0] == past
    text, _ = type_text(env, OWNER, "01-20 20:00")
    assert "├ 开奖时间：2027-01-20 20:00（UTC+08:00）" in text


def test_typed_duration_counts_from_publishing(env):
    press(env, OWNER, f"m:new:{GROUP}")
    press(env, OWNER, "m:k:join")
    type_text(env, OWNER, "耳机")
    press(env, OWNER, "m:c:1")
    press(env, OWNER, "m:mode:t")
    type_text(env, OWNER, "30分钟")
    press(env, OWNER, "m:j:b")
    press(env, OWNER, "m:tt")
    env.now[0] += 3600  # the admin comes back to the confirmation page an hour later
    assert shown(press(env, OWNER, "m:pub"))[0] == "✅ 已发布到「测试群」。"
    assert env.store.view(1)["deadline"] == env.now[0] + 1800


def test_typed_date_that_has_passed_is_asked_again(env):
    press(env, OWNER, f"m:new:{GROUP}")
    press(env, OWNER, "m:k:join")
    type_text(env, OWNER, "耳机")
    press(env, OWNER, "m:c:1")
    press(env, OWNER, "m:mode:t")
    type_text(env, OWNER, "01-15 17:00")
    press(env, OWNER, "m:j:b")
    press(env, OWNER, "m:tt")
    env.now[0] += 7200  # 18:00, the time typed is gone
    text, buttons = shown(press(env, OWNER, "m:pub"))
    assert text.startswith("⚠️ 填写的开奖时间已经过了，请重新填写。\n\n" + HEADER)
    assert "├ 开奖时间" not in text
    assert text.endswith("🕒 现在是 2027-01-15 18:00（UTC+08:00）")
    text, _ = shown(press(env, OWNER, buttons["1小时"]))
    assert text.endswith("发布到「测试群」？")
    assert shown(press(env, OWNER, "m:pub"))[0] == "✅ 已发布到「测试群」。"
    assert env.store.view(1)["deadline"] == env.now[0] + 3600


def test_buttons_left_on_earlier_wizard_messages_are_stale(env):
    fill_wizard(env, OWNER)  # at the confirmation page: timed, joined by button
    for data in ("m:mode:f", "m:f:10", "m:t:60", "m:c:1", "m:j:k", "m:tt"):
        assert press(env, OWNER, data).answer.await_args.args[0] == STALE
    _, data = env.store.draft(OWNER)
    assert (data["mode"], data["join"], "target" in data) == ("t", "b", False)
    press(env, OWNER, "m:more")
    assert press(env, OWNER, "m:pub").answer.await_args.args[0] == STALE  # a prize is asked
    assert env.store.group_summary(GROUP)["active"] == 0


def test_a_double_tap_on_publish_publishes_once(env):
    fill_wizard(env, OWNER)

    async def slow_send(*args, **kwargs):
        await asyncio.sleep(0.05)
        return SimpleNamespace(message_id=500)

    env.bot.send_message.side_effect = slow_send
    taps = [query_for(OWNER, "m:pub") for _ in range(2)]

    async def double_tap():
        context = SimpleNamespace(bot=env.bot)
        updates = [SimpleNamespace(callback_query=tap) for tap in taps]
        await asyncio.gather(*(env.menu.callback(update, context) for update in updates))

    asyncio.run(double_tap())
    assert env.store.group_summary(GROUP)["active"] == 1
    assert taps[1].answer.await_args.args[0] == "操作已过期，请重新开始。"


def test_no_prize_is_added_beyond_the_limit(env):
    data = {"prizes": [[f"奖{i}", 1] for i in range(10)], "mode": "t", "minutes": 60}
    data |= {"join": "b", "title": "十种奖品"}
    _, markup = asyncio.run(env.menu.advance(OWNER, GROUP, data))
    assert "➕ 添加奖品" not in labels(markup)
    query = press(env, OWNER, "m:more")  # left on an earlier confirmation page
    assert query.answer.await_args.args[0].startswith("奖品最多 10 种")


def test_a_draft_saved_by_the_previous_version_carries_on(env):
    env.store.save_draft(OWNER, GROUP, {"prizes": [["耳机", 1]], "step": None})  # "怎么开奖？"
    text, _ = shown(press(env, OWNER, "m:mode:t"))
    assert "什么时候开奖？" in text


def test_wizard_can_publish_to_another_group(env):
    env.store.remember_group(-200, "二群")
    env.store.remember_group(-300, "别人的群")
    env.statuses[(-200, OWNER)] = ChatMember.ADMINISTRATOR
    start(env, OWNER, "g-200")  # seen managing 二群 once
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
        text=text,
        chat=SimpleNamespace(id=chat_id),
        message_id=12,
        sender_chat=None,
        set_reaction=AsyncMock(),
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
        ("90分钟", {"minutes": 90}),
        ("2小时", {"minutes": 120}),
        ("3天", {"minutes": 4320}),
        ("45", {"minutes": 45}),
        ("2 H", {"minutes": 120}),
        ("2027-01-20 20:00", {"deadline": datetime(2027, 1, 20, 20, tzinfo=SHANGHAI).timestamp()}),
        ("01-20 20：00", {"deadline": datetime(2027, 1, 20, 20, tzinfo=SHANGHAI).timestamp()}),
    ],
)
def test_parse_time(typed, expected):
    assert parse_time(typed, NOW, SHANGHAI) == expected


def test_a_date_without_year_is_the_nearest_one():
    december = datetime(2027, 12, 20, 12, tzinfo=SHANGHAI).timestamp()
    next_january = datetime(2028, 1, 5, 20, tzinfo=SHANGHAI).timestamp()
    assert parse_time("01-05 20:00", december, SHANGHAI) == {"deadline": next_january}
    an_hour_ago = datetime(2027, 1, 15, 15, tzinfo=SHANGHAI).timestamp()
    assert parse_time("01-15 15:00", NOW, SHANGHAI) == {"deadline": an_hour_ago}  # not 2028
    new_year = datetime(2028, 1, 2, 10, tzinfo=SHANGHAI).timestamp()
    last_december = datetime(2027, 12, 31, 18, tzinfo=SHANGHAI).timestamp()
    # Two days ago, not next December.
    assert parse_time("12-31 18:00", new_year, SHANGHAI) == {"deadline": last_december}


def test_parse_time_rejects_nonsense():
    with pytest.raises(LotteryError):
        parse_time("明天晚上", NOW, SHANGHAI)


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


def test_a_long_list_of_winners_is_cut_to_fit_one_message(env):
    prizes = [[f"{'奖' * 63}{i}", 10] for i in range(10)]
    rid = env.store.create(OWNER, "大抽奖", 100, 60, chat_id=GROUP, prizes=prizes)
    for uid in range(1, 101):
        env.store.join(rid, uid, "😀" * 128)  # emoji count twice towards Telegram's limit
    env.store.freeze(rid, OWNER)
    env.store.draw(rid, OWNER)
    text, _ = shown(press(env, OWNER, f"m:r:{rid}"))
    assert len(text.encode("utf-16-le")) // 2 <= 4096
    assert "\n中奖：😀" in text
    assert text.endswith("…等 100 人，完整名单见开奖公告")


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
    env.statuses[(-300, OWNER)] = ChatMember.OWNER
    env.statuses[(-300, MEMBER)] = ChatMember.ADMINISTRATOR
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
    for admin in (OWNER, MEMBER):  # every admin, not only whoever added the bot
        assert env.store.managed_groups(admin) == [(-300, "新群")]
    change.old_chat_member = change.new_chat_member
    change.new_chat_member = SimpleNamespace(status=ChatMember.LEFT)
    asyncio.run(env.menu.bot_membership(SimpleNamespace(my_chat_member=change), context))
    assert (-300, "新群") not in env.store.groups()
    assert env.store.managed_groups(OWNER) == []
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
    text, buttons = shown(press(env, ADMIN, buttons["Alice · 权重 1 · 概率 50%"]))
    assert text == "Alice（ID：1）\n权重 1 · 概率 50% · 默认"
    assert {"0 排除", "1", "2", "3", "5", "10", "其他"} <= set(buttons)
    assert "↩️ 恢复默认" not in buttons
    text, buttons = shown(press(env, ADMIN, buttons["3"]))
    assert text.endswith("已调整 1 人")
    assert {"Alice · 权重 3 · 概率 75%", "Bob · 权重 1 · 概率 25%"} <= set(buttons)
    card = env.bot.edit_message_text.await_args
    assert card.kwargs["message_id"] == 500
    assert "本场设有中奖加成" in card.args[0]  # the first weight adds the notice
    text, buttons = shown(press(env, ADMIN, buttons["Alice · 权重 3 · 概率 75%"]))
    assert text.endswith("· 单独设置")
    _, buttons = shown(press(env, ADMIN, buttons["↩️ 恢复默认"]))
    assert "Alice · 权重 1 · 概率 50%" in buttons
    assert env.bot.edit_message_text.await_count == 1  # the notice was already there
    assert env.store.view(rid)["weighted"]


def test_designate_a_winner_with_buttons(weighted_env):
    env, rid = weighted_env, weighted_env.rid  # one place; Alice and Bob have joined
    _, buttons = shown(press(env, ADMIN, f"m:wu:{rid}:1:0"))
    text, buttons = shown(press(env, ADMIN, buttons["🎯 指定获奖"]))
    assert text == "Alice（ID：1）\n🎯 已指定获奖，开奖时直接中奖"
    assert "↩️ 取消指定" in buttons
    env.bot.edit_message_text.assert_not_awaited()  # the card shows nothing of it
    assert not env.store.view(rid)["weighted"]
    text, buttons = shown(press(env, ADMIN, f"m:w:{rid}:0"))
    assert text.endswith("已参与 2 人 · 已调整 0 人 · 指定 1 人")
    assert {"Alice · 🎯 指定获奖", "Bob · 权重 1 · 概率 0%"} <= set(buttons)
    query = press(env, ADMIN, f"m:wd:{rid}:2:0")
    assert query.answer.await_args.args[0] == "指定人数不能超过中奖人数 1。"
    query = press(env, OWNER, f"m:wd:{rid}:2:0")
    assert query.answer.await_args.args[0] == "只有超级管理员可以设置中奖加成。"
    _, buttons = shown(press(env, ADMIN, f"m:wd:{rid}:1:0"))  # taken back
    assert "🎯 指定获奖" in buttons
    assert [e["designated"] for e in env.store.view(rid)["entries"]] == [False, False]


def test_typed_weights_presets_and_defaults(weighted_env):
    env, rid = weighted_env, weighted_env.rid
    text, _ = shown(press(env, ADMIN, f"m:ws:{rid}:1:x:0"))
    assert text == "请输入权重（0～100），0 表示不参与抽取。"
    assert type_text(env, ADMIN, "abc")[0] == "权重需为 0～100 的整数，请重新输入。"
    text, buttons = type_text(env, ADMIN, "7")
    assert "Alice · 权重 7 · 概率 88%" in buttons
    shown(press(env, ADMIN, f"m:wid:{rid}:0"))
    assert type_text(env, ADMIN, "12345")[0].startswith("请发送用户 ID 和权重")
    _, buttons = type_text(env, ADMIN, "12345 5")
    text, buttons = shown(press(env, ADMIN, buttons["ID 12345 · 权重 5（未报名）"]))
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
    assert text.endswith(
        "报名已截止，权重已锁定。\nAlice · 权重 3 · 概率 75%\nBob · 权重 1 · 概率 25%"
    )
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
    press(env, user_id, "m:k:join")
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


def test_settings_page(env):
    env.bot.get_chat_member = AsyncMock(
        return_value=SimpleNamespace(
            status=ChatMember.ADMINISTRATOR, can_pin_messages=True, can_delete_messages=False
        )
    )
    _, buttons = shown(press(env, ADMIN, f"m:g:{GROUP}"))
    assert buttons["⚙️ 抽奖设置"] == f"m:set:{GROUP}"
    text, buttons = shown(press(env, ADMIN, f"m:set:{GROUP}"))
    assert text.startswith(
        "⚙️ 测试群 · 抽奖设置\n机器人权限：置顶消息 ✅ · 删除消息 ❌ · 邀请用户 ❌\n缺少的权限"
    )
    assert env.bot.get_chat_member.await_args.args == (GROUP, 4242)
    assert list(buttons) == [
        "📌 置顶报名卡片：开",
        "📌 置顶开奖公告：开",
        "🧹 删除口令消息：1分钟后",
        "🗑 删除机器人通知：3秒后",
        "📜 修改记录",
        "⬅️ 返回",
    ]
    text, buttons = shown(press(env, ADMIN, buttons["🧹 删除口令消息：1分钟后"]))
    assert text == (
        "🧹 删除口令消息：1分钟后\n群友发的口令消息多久后删除？\n"
        "点按钮，或直接发送分钟数（0～1440，0 表示立即删除）："
    )
    assert list(buttons) == [
        "立即",
        "3秒后",
        "1分钟后",
        "5分钟后",
        "10分钟后",
        "1小时后",
        "不删",
        "⬅️ 返回",
    ]
    _, buttons = shown(press(env, ADMIN, buttons["不删"]))
    assert "🧹 删除口令消息：不删" in buttons
    assert env.store.group_settings(GROUP)["delete_keyword"] is None
    press(env, ADMIN, f"m:sk:{GROUP}:delete_notices")
    assert type_text(env, ADMIN, "1441")[0] == "分钟数需为 0～1440 的整数，请重新输入。"
    _, buttons = type_text(env, ADMIN, "90")
    assert "🗑 删除机器人通知：90分钟后" in buttons
    assert env.store.group_settings(GROUP)["delete_notices"] == 5400
    # A button from a menu shown before this asks too, instead of cycling.
    text, _ = shown(press(env, ADMIN, f"m:sv:{GROUP}:delete_notices"))
    assert text.startswith("🗑 删除机器人通知：90分钟后\n")
    _, buttons = shown(press(env, ADMIN, f"m:sv:{GROUP}:pin_card"))
    assert "📌 置顶报名卡片：关" in buttons
    assert not env.store.group_settings(GROUP)["pin_card"]


def test_only_group_admins_change_settings(env):
    query = press(env, MEMBER, f"m:sv:{GROUP}:pin_card")
    assert query.answer.await_args.args[0] == NOT_MANAGER
    assert env.store.group_settings(GROUP)["pin_card"]


def sends(env, *ids):
    env.bot.send_message = AsyncMock(side_effect=[SimpleNamespace(message_id=n) for n in ids])


def pins(env):
    return [call.args[1] for call in env.bot.pin_chat_message.await_args_list]


def unpins(env):
    return [call.kwargs["message_id"] for call in env.bot.unpin_chat_message.await_args_list]


def test_cards_and_results_are_pinned(env):
    sends(env, 500, 501, 502, 503)
    fill_wizard(env, OWNER)
    press(env, OWNER, "m:pub")  # card 500
    assert env.bot.pin_chat_message.await_args.kwargs == {"disable_notification": True}
    env.store.join(1, 1, "Alice")
    press(env, OWNER, "m:do:draw:1")  # result 501
    assert (pins(env), unpins(env)) == ([500, 501], [500])  # card off, result on
    rid = env.store.create(OWNER, "第二场", 1, 60, chat_id=GROUP)
    asyncio.run(env.handlers.publish_card(env.bot, rid))  # card 502
    press(env, OWNER, f"m:do:draw:{rid}")  # result 503 replaces 501
    assert (pins(env), unpins(env)) == ([500, 501, 502, 503], [500, 502, 501])


def test_reposting_moves_the_pin(env):
    sends(env, 500, 501)
    rid = env.store.create(OWNER, "耳机", 1, 60, chat_id=GROUP)
    asyncio.run(env.handlers.publish_card(env.bot, rid))
    press(env, OWNER, f"m:repost:{rid}")
    assert (pins(env), unpins(env)) == ([500, 501], [500])


def test_nothing_is_pinned_when_switched_off(env):
    env.store.set_group_setting(GROUP, "pin_card", False)
    env.store.set_group_setting(GROUP, "pin_result", False)
    rid = env.store.create(OWNER, "耳机", 1, 60, chat_id=GROUP)
    asyncio.run(env.handlers.publish_card(env.bot, rid))
    env.store.join(rid, 1, "Alice")
    press(env, OWNER, f"m:do:draw:{rid}")
    env.bot.pin_chat_message.assert_not_awaited()
    env.bot.unpin_chat_message.assert_not_awaited()


def run_cleanup(env):
    asyncio.run(env.handlers.cleanup(SimpleNamespace(bot=env.bot)))


def test_keyword_messages_are_deleted_later(env):
    env.store.create(OWNER, "口令", 1, 60, chat_id=GROUP, keyword="hello")
    say(env, 1, "hello")
    run_cleanup(env)
    env.bot.delete_messages.assert_not_awaited()
    env.now[0] += 60
    run_cleanup(env)
    assert env.bot.delete_messages.await_args.args == (GROUP, [12])
    assert env.store.due_deletions() == {}
    say(env, 2, "no keyword here")
    env.now[0] += 60
    assert env.store.due_deletions() == {}  # other chatter is left alone


def test_keyword_messages_deleted_at_once_or_kept(env):
    env.store.create(OWNER, "口令", 2, 60, chat_id=GROUP, keyword="hello")
    env.store.set_group_setting(GROUP, "delete_keyword", 0)
    message = say(env, 1, "hello")
    message.set_reaction.assert_not_awaited()  # it is gone before anyone sees a reaction
    assert env.bot.delete_messages.await_args.args == (GROUP, [12])
    env.store.set_group_setting(GROUP, "delete_keyword", None)
    say(env, 2, "hello")
    env.now[0] += 3600
    assert env.store.due_deletions() == {}
    assert env.bot.delete_messages.await_count == 1


def test_group_notices_are_deleted_later(env):
    start(env, MEMBER, chat_type="supergroup")  # the /start (11) and the welcome (500)
    rid = env.store.create(OWNER, "耳机", 1, 60, chat_id=GROUP)
    sends(env, 501)
    press(env, OWNER, f"m:do:cancel:{rid}")  # the cancel notice (501)
    env.now[0] += 3
    assert env.store.due_deletions() == {GROUP: [501]}
    env.now[0] += 57  # the welcome stays a minute, for its button to be pressed
    assert env.store.due_deletions() == {GROUP: [501, 11, 500]}


def test_failed_deletions_are_retried_only_when_worth_it(env):
    env.store.schedule_deletions(GROUP, [1], env.now[0])
    env.bot.delete_messages.side_effect = TimedOut()
    run_cleanup(env)
    assert env.store.due_deletions() == {GROUP: [1]}  # a network hiccup: try again
    env.bot.delete_messages.side_effect = Forbidden("not enough rights")
    run_cleanup(env)
    assert env.store.due_deletions() == {}  # no right to delete: give up


def test_points_settings_for_group_admins(env):
    _, buttons = shown(press(env, OWNER, f"m:g:{GROUP}"))
    assert buttons["💎 灵石设置"] == f"m:pt:{GROUP}"
    text, buttons = shown(press(env, OWNER, f"m:pt:{GROUP}"))
    assert text == (
        "💎 测试群 · 灵石设置\n"
        "群友在群里发送「签到」「灵石」「灵石榜」使用，机器人的回复按抽奖设置里"
        "「删除机器人通知」的时间删除。\n"
        "人多的群建议点「📌 发布灵石面板」：群友点按钮签到、查询，结果只弹给自己看。\n"
        "📅 签到：每天 10 灵石\n"
        "💬 发言奖励：每条有效发言有 5% 的概率得 5 灵石，每天不限次数\n"
        "⚡ 暴击：得到发言奖励时有 10% 的概率翻 2 倍\n"
        "✍️ 有效发言：至少 3 个字，距上一条有效发言 5 秒以上。群活跃抽奖也只统计有效发言。\n"
        "机器人权限：封禁用户 ❌（签到刷屏时禁言）\n"
        "没有这个权限时，刷屏只警告、不禁言。"
    )
    assert list(buttons) == [
        "💎 灵石功能：开",
        "📅 签到：10 灵石",
        "🎲 奖励概率：5%",
        "🎁 发言奖励：5 灵石",
        "🔁 每天最多：不限",
        "⚡ 暴击率：10%",
        "✖️ 暴击倍数：2 倍",
        "✍️ 最少字数：3 字",
        "⏱ 发言间隔：5 秒",
        "📌 发布灵石面板",
        "📜 修改记录",
        "⬅️ 返回",
    ]  # balances are for super admins
    text, buttons = shown(press(env, OWNER, buttons["📅 签到：10 灵石"]))
    assert text == (
        "📅 签到：10 灵石\n每天签到得多少灵石？0 表示关闭签到。\n点按钮或直接发送数字（0～10000）："
    )
    assert list(buttons) == ["5 灵石", "10 灵石", "20 灵石", "50 灵石", "⬅️ 返回"]
    assert type_text(env, OWNER, "abc")[0] == "签到需为 0～10000 的整数，请重新输入。"
    text, buttons = type_text(env, OWNER, "15")
    assert "📅 签到：每天 15 灵石" in text
    assert "📅 签到：15 灵石" in buttons
    _, buttons = shown(press(env, OWNER, f"m:pk:{GROUP}:reward_points"))
    text, _ = shown(press(env, OWNER, buttons["关"]))
    assert "💬 发言奖励：关闭" in text
    text, buttons = shown(press(env, OWNER, f"m:pv:{GROUP}:points:0"))
    assert "灵石功能已关闭" in text
    assert list(buttons) == [
        "💎 灵石功能：关",
        "✍️ 最少字数：3 字",
        "⏱ 发言间隔：5 秒",
        "📜 修改记录",
        "⬅️ 返回",
    ]
    assert not env.store.group_settings(GROUP)["points"]
    assert press(env, MEMBER, f"m:pv:{GROUP}:points:1").answer.await_args.args[0] == NOT_MANAGER
    assert not env.store.group_settings(GROUP)["points"]


def test_points_settings_show_whether_the_bot_can_mute(env):
    env.statuses[(GROUP, 4242)] = ChatMember.ADMINISTRATOR
    lookup = env.bot.get_chat_member.side_effect

    async def rights(chat_id, user_id):
        member = await lookup(chat_id, user_id)
        return SimpleNamespace(status=member.status, can_restrict_members=True)

    env.bot.get_chat_member.side_effect = rights
    text, _ = shown(press(env, OWNER, f"m:pt:{GROUP}"))
    assert text.endswith("机器人权限：封禁用户 ✅（签到刷屏时禁言）")


def test_super_admins_adjust_balances(env):
    env.store.check_in(GROUP, 7, "Tom", "2027-01-15", 10)
    _, buttons = shown(press(env, OWNER, f"m:pt:{GROUP}"))
    assert "✏️ 修改余额" not in buttons
    denied = press(env, OWNER, f"m:pb:{GROUP}:0").answer.await_args.args[0]
    assert denied == "只有超级管理员可以修改余额和导出流水。"
    _, buttons = shown(press(env, ADMIN, f"m:pt:{GROUP}"))
    _, buttons = shown(press(env, ADMIN, buttons["✏️ 修改余额"]))
    assert buttons["1. Tom · 10"] == f"m:pu:{GROUP}:7:0"
    text, buttons = shown(press(env, ADMIN, buttons["1. Tom · 10"]))
    assert text == "Tom（ID：7）\n💎 10 灵石"
    assert list(buttons)[:6] == ["+10", "+50", "+100", "-10", "-50", "-100"]
    text, _ = shown(press(env, ADMIN, buttons["+50"]))
    assert text == "Tom（ID：7）\n💎 60 灵石"
    refused = press(env, ADMIN, f"m:pa:{GROUP}:7:-100:0").answer.await_args.args[0]
    assert refused == "余额不足：当前 60 灵石，最多扣 60。"
    shown(press(env, ADMIN, f"m:pa:{GROUP}:7:x:0"))
    assert type_text(env, ADMIN, "-20")[0] == "Tom（ID：7）\n💎 40 灵石"
    shown(press(env, ADMIN, f"m:pid:{GROUP}:0"))
    assert type_text(env, ADMIN, "8")[0].startswith("请发送用户 ID 和要加减的灵石")
    assert type_text(env, ADMIN, "8 +30")[0] == "用户 8（ID：8）\n💎 30 灵石"
    text, _ = shown(press(env, ADMIN, f"m:px:{GROUP}"))
    assert text.startswith("💎 测试群 · 灵石设置")
    call = env.bot.send_document.await_args
    assert call.args == (ADMIN,)
    assert call.kwargs["filename"] == f"points{GROUP}.json"
    exported = json.loads(call.kwargs["document"].getvalue())
    assert [(e["user_id"], e["delta"], e["reason"]) for e in exported["ledger"]] == [
        (7, 10, "checkin"),
        (7, 50, "adjust"),
        (7, -20, "adjust"),
        (8, 30, "adjust"),
    ]


def test_wizard_publishes_a_points_raffle(env):
    text, buttons = shown(press(env, OWNER, f"m:new:{GROUP}"))
    assert "🪙 积分抽奖：用签到、发言得到的灵石报名，参与时扣除" in text
    text, buttons = shown(press(env, OWNER, buttons["🪙 积分抽奖"]))
    assert text == HEADER + (
        "├ 类型：积分抽奖\n\n"
        "🪙 积分抽奖：群成员通过签到或发言获得灵石，参与本抽奖会扣除报名所需的灵石。\n"
        "怎么开奖？"
    )
    assert list(buttons) == ["⏰ 定时开奖", "👥 满人开奖", "⬅️ 返回选择抽奖类型", "✖ 取消"]
    text, _ = shown(press(env, OWNER, buttons["⬅️ 返回选择抽奖类型"]))
    assert text.endswith("选择抽奖类型：")
    press(env, OWNER, "m:k:points")
    text, _ = shown(press(env, OWNER, "m:mode:f"))
    assert text.endswith("请发送奖品名称，例如：1USDT")
    type_text(env, OWNER, "1usdt")
    press(env, OWNER, "m:c:2")
    text, buttons = shown(press(env, OWNER, "m:f:10"))
    assert text.endswith("参与一次需要多少灵石？点按钮或直接发送数字，群友报名时扣除：")
    assert list(buttons) == ["10 灵石", "20 灵石", "50 灵石", "100 灵石", "✖ 取消"]
    assert type_text(env, OWNER, "0")[0] == "参与所需灵石需为 1～1000000 的整数，请重新输入。"
    text, _ = type_text(env, OWNER, "30")
    assert "├ 类型：积分抽奖 · 参与需 30 灵石\n" in text
    assert text.endswith("怎么参与？")
    press(env, OWNER, "m:j:b")
    _, buttons = type_text(env, OWNER, "灵石福利")
    assert set(buttons) == {"✅ 发布抽奖", "❌ 取消发布", "➕ 添加奖品", "🔄 换个群"}
    press(env, OWNER, "m:pub")
    raffle = env.store.view(1)
    assert (raffle["kind"], raffle["cost"], raffle["target_count"]) == ("join", 30, 10)
    card = env.bot.send_message.await_args
    assert "🪙 参与需 30 灵石，报名时扣除" in card.args[1]
    assert labels(card.kwargs["reply_markup"]) == {"🎟 参与抽奖（30 灵石）": "join:1"}
    text, _ = shown(press(env, OWNER, "m:r:1"))
    assert "🪙 参与需 30 灵石，报名时扣除" in text
    env.store.adjust_points(GROUP, ADMIN, 7, 50)
    env.store.join(1, 7, "Tom")
    press(env, OWNER, "m:do:cancel:1")
    notice = env.bot.send_message.await_args.args[1]
    assert notice == "「灵石福利」抽奖已取消。报名扣除的灵石已全部退还。"
    assert env.store.holder(GROUP, 7)["balance"] == 50


def test_points_raffles_warn_when_the_group_earns_none(env):
    env.store.set_group_setting(GROUP, "points", False)
    press(env, OWNER, f"m:new:{GROUP}")
    press(env, OWNER, "m:k:points")
    press(env, OWNER, "m:mode:t")
    type_text(env, OWNER, "1usdt")
    press(env, OWNER, "m:c:1")
    text, _ = shown(press(env, OWNER, "m:t:60"))
    assert "⚠️ 本群的灵石功能已关闭，群友现在得不到灵石。\n参与一次需要多少灵石？" in text


def test_super_admins_weigh_points_raffles(env):
    press(env, ADMIN, f"m:new:{GROUP}")
    press(env, ADMIN, "m:k:points")
    press(env, ADMIN, "m:mode:t")
    type_text(env, ADMIN, "1usdt")
    press(env, ADMIN, "m:c:1")
    press(env, ADMIN, "m:t:60")
    press(env, ADMIN, "m:co:10")
    press(env, ADMIN, "m:j:b")
    _, buttons = type_text(env, ADMIN, "积分")
    assert "⚖️ 中奖加成：关" in buttons
    _, buttons = shown(press(env, ADMIN, "m:pub"))
    assert buttons["⚖️ 设置加成"] == "m:w:1:0"


def test_daily_rewards_are_unlimited_unless_limited(env):
    text, buttons = shown(press(env, OWNER, f"m:pk:{GROUP}:reward_daily"))
    assert text == (
        "🔁 每天最多：不限\n每人每天最多领几次发言奖励？0 表示不限。\n"
        "点按钮或直接发送数字（0～1000）："
    )
    assert list(buttons) == ["1 次", "3 次", "5 次", "10 次", "不限", "⬅️ 返回"]
    text, buttons = shown(press(env, OWNER, buttons["5 次"]))
    assert "💬 发言奖励：每条有效发言有 5% 的概率得 5 灵石，每天最多 5 次\n" in text
    assert "🔁 每天最多：5 次" in buttons
    text, buttons = shown(press(env, OWNER, f"m:pv:{GROUP}:reward_daily:0"))
    assert "🔁 每天最多：不限" in buttons
    assert env.store.group_settings(GROUP)["reward_daily"] == 0


def test_reward_chance_setting(env):
    text, buttons = shown(press(env, OWNER, f"m:pk:{GROUP}:reward_chance"))
    assert text == (
        "🎲 奖励概率：5%\n每条有效发言有百分之几的概率获得发言奖励？\n"
        "点按钮或直接发送数字（1～100）："
    )
    assert list(buttons) == ["1%", "3%", "5%", "10%", "20%", "⬅️ 返回"]
    text, buttons = type_text(env, OWNER, "8")
    assert "每条有效发言有 8% 的概率得 5 灵石" in text
    assert "🎲 奖励概率：8%" in buttons
    press(env, OWNER, f"m:pv:{GROUP}:crit_percent:0")
    text, _ = shown(press(env, OWNER, f"m:pt:{GROUP}"))
    assert "\n⚡ 暴击：关闭\n" in text


def test_setting_changes_are_recorded(env):
    env.statuses[(GROUP, 8)] = ChatMember.ADMINISTRATOR

    def press_as(user_id, name, data):
        query = query_for(user_id, data)
        query.from_user.full_name = name
        asyncio.run(
            env.menu.callback(SimpleNamespace(callback_query=query), SimpleNamespace(bot=env.bot))
        )
        return shown(query)

    text, _ = press_as(OWNER, "张三", f"m:log:{GROUP}:pt")
    assert text == "📜 测试群 · 灵石设置的修改记录\n还没有修改过。"
    press_as(OWNER, "张三", f"m:pv:{GROUP}:reward_chance:8")
    env.now[0] += 60
    text, buttons = press_as(8, "李四", f"m:pv:{GROUP}:points:0")
    assert text.endswith("\n🕘 最近修改：01-15 16:01 李四：💎 灵石功能 开 → 关")
    press_as(8, "李四", f"m:pv:{GROUP}:points:0")  # already off: nothing to record
    text, buttons = press_as(OWNER, "张三", buttons["📜 修改记录"])
    assert text == (
        "📜 测试群 · 灵石设置的修改记录\n"
        "01-15 16:01 李四：💎 灵石功能 开 → 关\n"
        "01-15 16:00 张三：🎲 奖励概率 5% → 8%"
    )
    assert buttons == {"⬅️ 返回": f"m:pt:{GROUP}"}
    # The 抽奖设置 page keeps a record of its own.
    text, _ = press_as(OWNER, "张三", f"m:sd:{GROUP}:delete_keyword:0")
    assert "\n🕘 最近修改：01-15 16:01 张三：🧹 删除口令消息 1分钟后 → 立即" in text
    text, _ = press_as(OWNER, "张三", f"m:log:{GROUP}:set")
    assert (
        text == "📜 测试群 · 抽奖设置的修改记录\n01-15 16:01 张三：🧹 删除口令消息 1分钟后 → 立即"
    )
    # Typed answers are recorded too, and members may not read the record.
    press_as(OWNER, "张三", f"m:pk:{GROUP}:checkin_points")
    type_text(env, OWNER, "20")
    changes = env.store.setting_changes(GROUP)
    assert (changes[0]["key"], changes[0]["after"], changes[0]["actor_id"]) == (
        "checkin_points",
        20,
        OWNER,
    )
    assert press(env, MEMBER, f"m:log:{GROUP}:pt").answer.await_args.args[0] == NOT_MANAGER
    press(env, ADMIN, f"m:px:{GROUP}")
    exported = json.loads(env.bot.send_document.await_args.kwargs["document"].getvalue())
    assert [c["key"] for c in exported["setting_changes"]] == [
        "checkin_points",
        "delete_keyword",
        "points",
        "reward_chance",
    ]


def test_publishing_names_the_group_even_when_it_fails(env):
    fill_wizard(env, OWNER)
    env.bot.send_message = AsyncMock(side_effect=Forbidden("bot was kicked"))
    text, buttons = shown(press(env, OWNER, "m:pub"))
    assert text == "已创建，但没能发到「测试群」。请确认我在群里并能发言，再到抽奖记录里重新发布。"
    assert "📜 抽奖记录" in buttons


def test_super_admins_find_members_by_name(env):
    env.store.count_message(GROUP, 7, "Tom Lee", NOW)
    env.store.count_message(GROUP, 8, "Tommy", NOW)
    env.store.check_in(GROUP, 9, "Jerry", "2027-01-15", 10)
    _, buttons = shown(press(env, ADMIN, f"m:pb:{GROUP}:0"))
    text, _ = shown(press(env, ADMIN, buttons["🔍 按名字查找"]))
    assert text == "发送要找的人的名字，写其中几个字就行："
    text, buttons = type_text(env, ADMIN, "tom")
    assert text == "🔍 名字里有「tom」的人，点一个加减灵石：\n也可以接着发别的名字。"
    assert buttons == {
        "Tom Lee · 0 灵石": f"m:pu:{GROUP}:7:0",
        "Tommy · 0 灵石": f"m:pu:{GROUP}:8:0",
        "⬅️ 返回": f"m:pb:{GROUP}:0",
    }
    text, _ = type_text(env, ADMIN, "小明")  # still asking: another name
    assert text.startswith("没找到名字里有「小明」的人，换几个字再发一次。")
    text, buttons = shown(press(env, ADMIN, f"m:pu:{GROUP}:7:0"))
    assert text == "Tom Lee（ID：7）\n💎 0 灵石"
    text, _ = shown(press(env, ADMIN, buttons["+10"]))
    assert text == "Tom Lee（ID：7）\n💎 10 灵石"
    denied = press(env, OWNER, f"m:pf:{GROUP}:0").answer.await_args.args[0]
    assert denied == "只有超级管理员可以修改余额和导出流水。"


def test_admins_post_the_points_panel(env):
    sends(env, 600, 601)
    text, _ = shown(press(env, OWNER, f"m:pp:{GROUP}"))
    assert text.startswith("✅ 灵石面板已发到群里并置顶。\n\n💎 测试群 · 灵石设置")
    call = env.bot.send_message.await_args
    assert call.args[0] == GROUP
    assert call.args[1].startswith("💎 灵石\n")
    assert labels(call.kwargs["reply_markup"]) == {
        "📅 签到": "pts:checkin",
        "💎 我的灵石": "pts:wallet",
        "🏆 灵石榜": "pts:board",
    }
    assert pins(env) == [600]
    press(env, OWNER, f"m:pp:{GROUP}")  # a new panel takes the old one's place
    assert pins(env) == [600, 601]
    assert unpins(env) == [600]
    env.bot.delete_messages.assert_awaited_with(GROUP, [600])
    assert press(env, MEMBER, f"m:pp:{GROUP}").answer.await_args.args[0] == NOT_MANAGER


def test_the_panel_goes_back_on_top_once_a_raffle_ends(env):
    sends(env, 600, 601, 602, 603)
    press(env, OWNER, f"m:pp:{GROUP}")  # the panel (600)
    fill_wizard(env, OWNER)
    press(env, OWNER, "m:pub")  # the card (601), pinned above the panel
    press(env, OWNER, "m:do:draw:1")  # the result (602), then the panel again (603)
    assert pins(env) == [600, 601, 602, 603]
    assert unpins(env) == [601, 600]  # the card, then the old panel
    env.bot.delete_messages.assert_awaited_with(GROUP, [600])
    assert env.bot.send_message.await_args.args[1].startswith("💎 灵石\n")


def test_the_panel_waits_for_the_last_raffle_to_end(env):
    sends(env, 600, 601, 602, 603, 604, 605)
    press(env, OWNER, f"m:pp:{GROUP}")  # the panel (600)
    first, second = (env.store.create(OWNER, t, 1, 60, chat_id=GROUP) for t in ("甲", "乙"))
    for rid in (first, second):
        asyncio.run(env.handlers.publish_card(env.bot, rid))  # cards 601 and 602
    press(env, OWNER, f"m:do:cancel:{first}")  # its notice (603); 乙's card is still up
    assert pins(env) == [600, 601, 602]
    press(env, OWNER, f"m:do:cancel:{second}")  # its notice (604), then the panel (605)
    assert pins(env) == [600, 601, 602, 605]


def test_a_panel_that_cannot_be_pinned_leaves_the_old_one(env):
    sends(env, 600, 601, 602, 603)
    press(env, OWNER, f"m:pp:{GROUP}")  # the panel (600)
    rid = env.store.create(OWNER, "甲", 1, 60, chat_id=GROUP)
    asyncio.run(env.handlers.publish_card(env.bot, rid))  # the card (601)
    env.bot.pin_chat_message = AsyncMock(side_effect=BadRequest("not enough rights"))
    press(env, OWNER, f"m:do:cancel:{rid}")  # its notice (602); the new panel (603) goes
    env.bot.delete_messages.assert_awaited_with(GROUP, [603])
    assert 600 not in unpins(env)
    assert env.store.restorable_panel(GROUP, True) == 600


def test_no_panel_is_posted_where_there_was_none_or_no_points(env):
    sends(env, 600, 601, 602, 603)
    first = env.store.create(OWNER, "甲", 1, 60, chat_id=GROUP)
    asyncio.run(env.handlers.publish_card(env.bot, first))  # the card (600)
    press(env, OWNER, f"m:do:cancel:{first}")  # its notice (601): no panel ever posted
    assert env.bot.send_message.await_count == 2
    press(env, OWNER, f"m:pp:{GROUP}")  # the panel (602)
    env.store.set_group_setting(GROUP, "points", False)
    second = env.store.create(OWNER, "乙", 1, 60, chat_id=GROUP)
    sends(env, 700, 701)
    asyncio.run(env.handlers.publish_card(env.bot, second))  # the card (700)
    press(env, OWNER, f"m:do:cancel:{second}")  # its notice (701), and no panel
    assert env.bot.send_message.await_count == 2


def test_admins_are_told_when_the_panel_could_not_be_pinned(env):
    env.bot.pin_chat_message = AsyncMock(side_effect=BadRequest("not enough rights"))
    text, _ = shown(press(env, OWNER, f"m:pp:{GROUP}"))
    assert text.startswith(
        "✅ 灵石面板已发到群里，但没能置顶：请给机器人打开「置顶消息」权限。\n\n"
    )


def test_wizard_creates_an_invite_ranking(env):
    _, buttons = shown(press(env, OWNER, f"m:new:{GROUP}"))
    text, buttons = shown(press(env, OWNER, buttons["🪁 邀请抽奖"]))
    assert "专属链接邀请：" in text
    assert text.endswith("选择邀请方式：")
    assert list(buttons) == ["🔗 专属链接邀请", "⚠️ 添加成员邀请", "⬅️ 返回选择抽奖类型", "✖ 取消"]
    text, _ = shown(press(env, OWNER, buttons["⬅️ 返回选择抽奖类型"]))
    assert text.endswith("选择抽奖类型：")
    press(env, OWNER, "m:k:inv")
    text, buttons = shown(press(env, OWNER, "m:iv:link"))
    assert text == HEADER + (
        "├ 类型：邀请抽奖 · 专属链接邀请\n\n"
        "🪁 邀请抽奖：根据邀请排名抽奖，或达到邀请人数参与随机抽奖。\n选择一种："
    )
    text, _ = shown(press(env, OWNER, buttons["⬅️ 返回选择邀请方式"]))
    assert text.endswith("选择邀请方式：")
    press(env, OWNER, "m:iv:link")
    text, _ = shown(press(env, OWNER, "m:ik:rank"))
    assert "├ 类型：邀请抽奖 · 专属链接邀请 · 按邀请人数排名\n" in text
    assert "什么时候开奖？" in text
    press(env, OWNER, "m:t:1440")
    text, _ = type_text(env, OWNER, "10usdt")
    assert text.endswith("请发送第二名的奖品，例如：1USDT")
    press(env, OWNER, "m:pe")
    _, buttons = type_text(env, OWNER, "拉新榜")
    press(env, OWNER, "m:pub")
    raffle = env.store.view(1)
    assert (raffle["kind"], raffle["invite_via"], raffle["deadline"]) == (
        "rank",
        "link",
        NOW + 86400,
    )
    card = env.bot.send_message.await_args
    assert "🪁 按邀请人数排名，前 1 名获奖" in card.args[1]
    assert "🔗 领取我的邀请链接" in labels(card.kwargs["reply_markup"])
    text, buttons = shown(press(env, ADMIN, "m:r:1"))
    assert "1 人中奖 · 有邀请 0 人" in text
    assert "✏️ 修改发言次数" not in buttons  # message counts only


def test_wizard_creates_an_invite_draw_that_ends_once_enough_reach_it(env):
    press(env, OWNER, f"m:new:{GROUP}")
    press(env, OWNER, "m:k:inv")
    press(env, OWNER, "m:iv:add")
    text, buttons = shown(press(env, OWNER, "m:ik:reach"))
    assert text.endswith("至少邀请多少人才能参与抽奖？点按钮或直接发送数字：")
    assert list(buttons) == ["1", "3", "5", "10", "20", "✖ 取消"]
    text, _ = shown(press(env, OWNER, "m:mm:3"))
    assert "├ 类型：邀请抽奖 · 添加成员邀请 · 邀请满 3 人参与抽奖\n" in text
    assert text.endswith("怎么开奖？")
    text, _ = shown(press(env, OWNER, "m:mode:f"))
    assert text.endswith("满多少人达标就开奖？点按钮或直接发送数字：")
    text, _ = type_text(env, OWNER, "2")
    assert "├ 满 2 人达标即开奖（最晚 2027-01-22 16:00（UTC+08:00））" in text
    type_text(env, OWNER, "1usdt")
    assert type_text(env, OWNER, "3")[0] == "中奖总人数不能超过满人开奖人数 2。"
    type_text(env, OWNER, "2")
    press(env, OWNER, "m:tt")
    press(env, OWNER, "m:pub")
    raffle = env.store.view(1)
    assert (raffle["kind"], raffle["invite_via"], raffle["min_messages"]) == ("reach", "add", 3)
    assert (raffle["target_count"], raffle["winner_count"]) == (2, 2)
    card = env.bot.send_message.await_args
    assert "🪁 邀请满 3 人即可参与，抽 2 人" in card.args[1]
    assert "👉 用「添加成员」把好友拉进群即可参与" in card.args[1]
    assert list(labels(card.kwargs["reply_markup"])) == ["📊 查看我的邀请"]


def test_members_get_their_invite_link_in_private(env):
    env.bot.create_chat_invite_link = AsyncMock(
        return_value=SimpleNamespace(invite_link="https://t.me/+six")
    )
    message = start(env, MEMBER, f"inv{GROUP}")
    assert message.reply_text.await_args.args[0].startswith(
        "🔗 你在「测试群」的专属邀请链接：\nhttps://t.me/+six\n"
    )
    stranger = start(env, 42, f"inv{GROUP}")
    assert stranger.reply_text.await_args.args[0] == "只有群成员才能领取这个群的邀请链接。"
    assert start(env, MEMBER, "invalid").reply_text.await_args.args[0] == "这个链接无效。"


def test_wizard_creates_a_report_raffle(env):
    env.store.remember_group(-200, "报道群")
    env.statuses[(-200, OWNER)] = ChatMember.OWNER
    env.store.set_manager(-200, OWNER, True)
    env.bot.get_chat = AsyncMock(return_value=SimpleNamespace(username="report_group"))
    _, buttons = shown(press(env, OWNER, f"m:new:{GROUP}"))
    text, buttons = shown(press(env, OWNER, buttons["🙋 指定群报道抽奖"]))
    assert text.endswith("是否继续创建？")
    assert list(buttons) == ["▶️ 继续", "⬅️ 返回选择抽奖类型", "✖ 取消"]
    text, buttons = shown(press(env, OWNER, buttons["▶️ 继续"]))
    assert "选择报道群（你管理、机器人也在的群）" in text
    assert list(buttons) == ["报道群", "⬅️ 返回", "✖ 取消"]  # not the group it is for
    text, _ = shown(press(env, OWNER, buttons["报道群"]))
    assert "├ 类型：指定群报道抽奖 · 报道群「报道群」\n" in text
    assert "├ 加入「报道群」即可参与\n" in text
    assert text.endswith("请发送奖品名称，例如：1USDT")
    type_text(env, OWNER, "1usdt")
    press(env, OWNER, "m:c:1")
    press(env, OWNER, "m:mode:t")
    press(env, OWNER, "m:t:60")
    press(env, OWNER, "m:tt")
    press(env, OWNER, "m:pub")
    raffle = env.store.view(1)
    assert (raffle["report_chat"], raffle["report_link"], raffle["keyword"]) == (
        -200,
        "https://t.me/report_group",
        None,
    )
    card = env.bot.send_message.await_args
    assert card.args[0] == GROUP
    assert "👉 加入「报道群」即可参与" in card.args[1]
    assert labels(card.kwargs["reply_markup"]) == {
        "➡️ 进入报道群": "https://t.me/report_group",
        "✅ 我已加入报道群": "join:1",
    }


def test_a_report_group_can_be_typed_as_a_public_link(env):
    env.statuses[(-300, OWNER)] = ChatMember.OWNER
    env.statuses[(-300, 4242)] = ChatMember.ADMINISTRATOR  # the bot is in it
    env.bot.get_chat = AsyncMock(
        return_value=SimpleNamespace(
            id=-300, type="supergroup", title="公开群", username="pub_group"
        )
    )
    press(env, OWNER, f"m:new:{GROUP}")
    press(env, OWNER, "m:k:rep")
    press(env, OWNER, "m:ro:go")
    assert type_text(env, OWNER, "https://t.me/+AbCdEf")[0].startswith(
        "私密群的邀请链接认不出是哪个群"
    )
    assert type_text(env, OWNER, "随便写写")[0].startswith("请发送公开群的链接")
    text, _ = type_text(env, OWNER, "https://t.me/pub_group")
    assert "├ 类型：指定群报道抽奖 · 报道群「公开群」\n" in text
    assert env.bot.get_chat.await_args.args == ("@pub_group",)
    _, data = env.store.draft(OWNER)
    assert (data["report"], data["report_link"]) == (-300, "https://t.me/pub_group")


def test_a_typed_report_group_must_be_the_admins_own(env):
    env.bot.get_chat = AsyncMock(
        return_value=SimpleNamespace(id=-300, type="supergroup", title="别人的群", username="x")
    )
    press(env, OWNER, f"m:new:{GROUP}")
    press(env, OWNER, "m:k:rep")
    press(env, OWNER, "m:ro:go")
    assert (
        type_text(env, OWNER, "@xxxx")[0]
        == "机器人还不在这个群里，请先把机器人拉进去并设为管理员。"
    )
    env.statuses[(-300, 4242)] = ChatMember.ADMINISTRATOR
    assert type_text(env, OWNER, "@xxxx")[0] == "只能选你管理的群作报道群。"

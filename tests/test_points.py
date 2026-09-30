import asyncio
import itertools
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from telegram import ChatPermissions
from telegram.error import BadRequest

from lottery.bot import BotHandlers
from lottery.core import Store
from lottery.points import ALREADY, WARNING, Points

SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = 1_800_000_000.0  # 2027-01-15 16:00 in Shanghai
MIDNIGHT = NOW + 8 * 3600  # 2027-01-16 00:00 in Shanghai
GROUP = -100
MESSAGE_IDS = itertools.count(1)


@pytest.fixture
def env(tmp_path):
    now = [NOW]
    store = Store(tmp_path / "points.sqlite3", clock=lambda: now[0])
    store.remember_group(GROUP, "测试群")
    handlers = BotHandlers(store, {99}, SHANGHAI)
    bot = SimpleNamespace(restrict_chat_member=AsyncMock(), delete_messages=AsyncMock())
    return SimpleNamespace(
        store=store, handlers=handlers, points=Points(handlers), bot=bot, now=now
    )


def say(env, user_id, text, at=0, points=None):
    """A group message sent `at` seconds after NOW; returns the bot's reply, if any."""
    env.now[0] = NOW + at
    message = SimpleNamespace(
        text=text,
        caption=None,
        chat=SimpleNamespace(id=GROUP, type="supergroup"),
        message_id=next(MESSAGE_IDS),
        date=datetime.fromtimestamp(NOW + at, UTC),
        sender_chat=None,
        reply_text=AsyncMock(return_value=SimpleNamespace(message_id=next(MESSAGE_IDS))),
    )
    user = SimpleNamespace(id=user_id, full_name=f"user{user_id}", is_bot=False)
    update = SimpleNamespace(effective_message=message, effective_user=user)
    asyncio.run((points or env.points).message(update, SimpleNamespace(bot=env.bot)))
    call = message.reply_text.await_args
    return call.args[0] if call else None


def test_checking_in(env):
    assert say(env, 1, "签到") == "✅ 签到成功，获得 10 灵石\n💎 当前 10 灵石 · 今天第 1 个签到"
    assert say(env, 2, " /签到 ", at=30).endswith("今天第 2 个签到")
    assert say(env, 1, "签到", at=600) == ALREADY
    # The day begins at midnight in the bot's time zone.
    assert say(env, 1, "签到", at=MIDNIGHT - NOW - 1) == ALREADY
    assert say(env, 1, "签到", at=MIDNIGHT - NOW).startswith("✅ 签到成功")
    # A restart forgets who checked in, but the database does not.
    assert say(env, 1, "签到", at=MIDNIGHT - NOW + 600, points=Points(env.handlers)) == ALREADY


def test_answers_are_tidied_away_with_the_question(env):
    say(env, 1, "签到")
    assert env.store.due_deletions() == {}
    env.now[0] += 600  # 删除机器人通知: 10 minutes
    assert len(env.store.due_deletions()[GROUP]) == 2


def test_checking_in_again_and_again_gets_a_mute(env):
    say(env, 1, "签到")
    answers = [say(env, 1, "签到", at=10 + i) for i in range(5)]
    assert answers[:4] == [ALREADY, ALREADY, ALREADY, WARNING]
    assert answers[4] == "🔇 user1 频繁发送签到，已禁言 5 分钟。"
    call = env.bot.restrict_chat_member.await_args
    assert call.args == (GROUP, 1, ChatPermissions.no_permissions())
    assert call.kwargs == {"until_date": int(NOW) + 14 + 300}
    # Once muted, counting starts over.
    assert say(env, 1, "签到", at=400) == ALREADY


def test_repeats_a_minute_apart_are_no_spam(env):
    say(env, 1, "签到")
    assert {say(env, 1, "签到", at=61 * i) for i in range(1, 8)} == {ALREADY}
    env.bot.restrict_chat_member.assert_not_awaited()


def test_a_member_who_cannot_be_muted_is_warned(env):
    env.bot.restrict_chat_member.side_effect = BadRequest("Can't remove chat owner")
    say(env, 1, "签到")
    answers = [say(env, 1, "签到", at=10 + i) for i in range(6)]
    assert answers[3:] == [WARNING, WARNING, WARNING]


def test_speaking_earns_points(env):
    env.store.set_group_setting(GROUP, "reward_every", 3)
    env.store.set_group_setting(GROUP, "reward_daily", 2)
    env.store.set_group_setting(GROUP, "crit_percent", 0)
    replies = [say(env, 1, f"第{i}条消息", at=10 * i) for i in range(1, 10)]
    assert replies == [
        None,
        None,
        "💬 user1 活跃发言，获得 5 灵石（今日 1/2）",
        None,
        None,
        "💬 user1 活跃发言，获得 5 灵石（今日 2/2）",
        None,
        None,
        None,  # the day's limit
    ]
    assert env.store.holder(GROUP, 1)["balance"] == 10


def test_only_real_messages_earn(env):
    env.store.set_group_setting(GROUP, "reward_every", 2)
    env.store.set_group_setting(GROUP, "crit_percent", 100)
    assert say(env, 1, "你好呀") is None
    assert say(env, 1, "也太快了吧", at=3) is None  # three seconds after the last
    assert say(env, 1, "你 好", at=10) is None  # two characters: too short
    assert say(env, 1, "签到", at=20).startswith("✅")  # a 灵石 word is not speaking
    assert say(env, 1, "灵石榜", at=30).startswith("💎")
    assert say(env, 1, "这次可以了", at=40) == "⚡ 暴击！user1 活跃发言，获得 10 灵石（今日 1/5）"


def test_progress_survives_a_restart(env):
    env.store.set_group_setting(GROUP, "reward_every", 3)
    say(env, 1, "第一条消息")
    say(env, 1, "第二条消息", at=10)
    asyncio.run(env.handlers.save_activity(None))
    assert say(env, 1, "第三条消息", at=20, points=Points(env.handlers)).startswith("💬 user1")


def test_wallet_and_board(env):
    env.store.set_group_setting(GROUP, "reward_every", 4)
    assert say(env, 1, "灵石") == (
        "💎 user1 的灵石：0\n"
        "📅 今日未签到，发送「签到」领取 10 灵石\n"
        "💬 今日发言奖励 0/5 次，再发 4 条发言可领下一次"
    )
    assert say(env, 1, "灵石榜", at=1) == "💎 灵石榜\n还没有人获得灵石，发送「签到」领取第一笔吧。"
    say(env, 1, "签到", at=2)
    say(env, 1, "说一句话", at=10)
    assert say(env, 1, "我的灵石", at=20) == (
        "💎 user1 的灵石：10\n📅 今日已签到\n💬 今日发言奖励 0/5 次，再发 3 条发言可领下一次"
    )
    say(env, 2, "签到", at=30)
    env.store.adjust_points(GROUP, 99, 3, 50)
    assert say(env, 2, "灵石榜", at=40) == (
        "💎 灵石榜\n🥇 用户 3 · 50\n🥈 user1 · 10\n🥉 user2 · 10\n你：第 3 名 · 10 灵石"
    )
    assert say(env, 4, "灵石榜", at=50).endswith("你还没有灵石")


def test_points_can_be_turned_off(env):
    env.store.set_group_setting(GROUP, "points", False)
    env.store.set_group_setting(GROUP, "reward_every", 1)
    assert say(env, 1, "签到") is None
    assert say(env, 1, "灵石", at=10) is None
    assert say(env, 1, "照样统计发言", at=20) is None
    assert env.store.messages_since(GROUP, 1, NOW) == 1  # activity raffles still count
    assert env.store.holder(GROUP, 1)["balance"] == 0
    env.store.set_group_setting(GROUP, "points", True)
    env.store.set_group_setting(GROUP, "checkin_points", 0)
    assert say(env, 1, "签到", at=30) == "本群没有开启签到。"

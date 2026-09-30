import asyncio
import itertools
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from telegram import ChatPermissions
from telegram.error import BadRequest, TimedOut

from lottery.bot import BotHandlers
from lottery.core import Store
from lottery.points import ALREADY, POINTS_OFF, WARNING, Points
from lottery.views import card

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
    points = Points(handlers)
    points.randbelow = lambda total: total - 1  # no reward unless the chance is 100%
    return SimpleNamespace(store=store, handlers=handlers, points=points, bot=bot, now=now)


def post(env, user_id, text, at=0, points=None):
    """A group message sent `at` seconds after NOW, once the bot has handled it."""
    env.now[0] = NOW + at
    message = SimpleNamespace(
        text=text,
        caption=None,
        chat=SimpleNamespace(id=GROUP, type="supergroup"),
        message_id=next(MESSAGE_IDS),
        date=datetime.fromtimestamp(NOW + at, UTC),
        sender_chat=None,
        reply_text=AsyncMock(return_value=SimpleNamespace(message_id=next(MESSAGE_IDS))),
        set_reaction=AsyncMock(),
    )
    user = SimpleNamespace(id=user_id, full_name=f"user{user_id}", is_bot=False)
    update = SimpleNamespace(effective_message=message, effective_user=user)
    asyncio.run((points or env.points).message(update, SimpleNamespace(bot=env.bot)))
    return message


def say(env, user_id, text, at=0, points=None):
    """A group message sent `at` seconds after NOW; returns the bot's reply, if any."""
    call = post(env, user_id, text, at, points).reply_text.await_args
    return call.args[0] if call else None


def reaction(message):
    call = message.set_reaction.await_args
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
    env.now[0] += 3  # 删除机器人通知: 3 seconds
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


def test_speaking_earns_points_by_chance(env):
    env.store.set_group_setting(GROUP, "reward_daily", 2)
    env.store.set_group_setting(GROUP, "crit_percent", 0)
    rolls = iter([50, 4, 5, 0, 99, 3])  # a roll below the chance, 5 in 100, rewards
    env.points.randbelow = lambda total: next(rolls)
    replies = [say(env, 1, f"第{i}条消息", at=10 * i) for i in range(1, 7)]
    assert replies == [
        None,
        "💬 user1 活跃发言，获得 5 灵石",
        None,
        "💬 user1 活跃发言，获得 5 灵石",
        None,
        None,  # the day's limit
    ]
    assert env.store.holder(GROUP, 1)["balance"] == 10
    assert say(env, 1, "灵石", at=100).endswith("\n💬 今日发言奖励 2/2 次")


def test_only_real_messages_earn(env):
    env.store.set_group_setting(GROUP, "reward_chance", 100)
    env.store.set_group_setting(GROUP, "crit_percent", 100)
    assert say(env, 1, "你好呀") == "⚡ 暴击！user1 活跃发言，获得 10 灵石"
    assert say(env, 1, "也太快了吧", at=3) is None  # three seconds after the last
    assert say(env, 1, "你 好", at=10) is None  # two characters: too short
    assert say(env, 1, "签到", at=20).startswith("✅")  # a 灵石 word is not speaking
    assert say(env, 1, "灵石榜", at=30).startswith("💎")
    assert say(env, 1, "这次可以了", at=40) == "⚡ 暴击！user1 活跃发言，获得 10 灵石"


def test_wallet_and_board(env):
    assert say(env, 1, "灵石") == (
        "💎 user1 的灵石：0\n"
        "📅 今日未签到，发送「签到」领取 10 灵石\n"
        "💬 今日发言奖励 0 次，多发言就有机会获得"
    )
    assert say(env, 1, "灵石榜", at=1) == "💎 灵石榜\n还没有人获得灵石，发送「签到」领取第一笔吧。"
    say(env, 1, "签到", at=2)
    assert say(env, 1, "我的灵石", at=20).startswith("💎 user1 的灵石：10\n📅 今日已签到\n")
    say(env, 2, "签到", at=30)
    env.store.adjust_points(GROUP, 99, 3, 50)
    assert say(env, 2, "灵石榜", at=40) == (
        "💎 灵石榜\n🥇 用户 3 · 50\n🥈 user1 · 10\n🥉 user2 · 10\n你：第 3 名 · 10 灵石"
    )
    assert say(env, 4, "灵石榜", at=50).endswith("你还没有灵石")


def test_points_can_be_turned_off(env):
    env.store.set_group_setting(GROUP, "points", False)
    env.store.set_group_setting(GROUP, "reward_chance", 100)
    assert say(env, 1, "签到") is None
    assert say(env, 1, "灵石", at=10) is None
    assert say(env, 1, "照样统计发言", at=20) is None
    env.store.flush_activity()
    with env.store.reading() as db:  # activity raffles still count it
        assert db.execute("SELECT SUM(count) FROM activity WHERE user_id=1").fetchone()[0] == 1
    assert env.store.holder(GROUP, 1)["balance"] == 0
    env.store.set_group_setting(GROUP, "points", True)
    env.store.set_group_setting(GROUP, "checkin_points", 0)
    assert say(env, 1, "签到", at=30) == "本群没有开启签到。"


def click_join(env, rid, user_id):
    """Press a card's join button; returns the answer shown to that member alone."""
    query = SimpleNamespace(
        data=f"join:{rid}",
        answer=AsyncMock(),
        from_user=SimpleNamespace(id=user_id, full_name=f"user{user_id}", is_bot=False),
    )
    bot = SimpleNamespace(get_chat_member=AsyncMock(return_value=SimpleNamespace(status="member")))
    context = SimpleNamespace(bot=bot, job_queue=None)
    asyncio.run(env.handlers.callback(SimpleNamespace(callback_query=query), context))
    return query.answer.await_args.args[0]


def test_joining_a_points_raffle(env):
    rid = env.store.create(99, "积分", 1, 60, chat_id=GROUP, cost=20)
    text, markup = card(env.store.view(rid), SHANGHAI)
    assert "中奖 1 人 · 已参与 0 人\n🪙 参与需 20 灵石，报名时扣除\n" in text
    assert markup.inline_keyboard[0][0].text == "🎟 参与抽奖（20 灵石）"
    assert click_join(env, rid, 1) == (
        "灵石不足：参与需要 20 灵石，你有 0 灵石。发送「签到」或多发言可以获得灵石。"
    )
    env.store.adjust_points(GROUP, 99, 1, 25)
    assert click_join(env, rid, 1) == "报名成功，扣除 20 灵石，剩余 5 灵石。"
    assert click_join(env, rid, 1) == "你已报名，无需重复报名。"


def test_joining_a_points_raffle_by_keyword(env):
    env.store.create(99, "积分", 2, 60, chat_id=GROUP, cost=20, keyword="冲")
    env.store.adjust_points(GROUP, 99, 1, 20)

    def send(user_id):
        message = SimpleNamespace(
            text="冲",
            chat=SimpleNamespace(id=GROUP, type="supergroup"),
            message_id=next(MESSAGE_IDS),
            sender_chat=None,
            set_reaction=AsyncMock(),
            reply_text=AsyncMock(return_value=SimpleNamespace(message_id=next(MESSAGE_IDS))),
        )
        user = SimpleNamespace(id=user_id, full_name=f"user{user_id}", is_bot=False)
        update = SimpleNamespace(effective_message=message, effective_user=user)
        asyncio.run(env.handlers.keyword(update, SimpleNamespace(bot=env.bot, job_queue=None)))
        call = message.reply_text.await_args
        return call.args[0] if call else None

    assert send(1) == "报名成功，扣除 20 灵石，剩余 0 灵石。"
    assert send(1) is None  # already in: nothing more to say
    assert send(2).startswith("灵石不足：参与需要 20 灵石，你有 0 灵石。")


def test_rewards_without_a_daily_limit(env):
    for key, value in (("reward_chance", 100), ("reward_daily", 0), ("crit_percent", 0)):
        env.store.set_group_setting(GROUP, key, value)
    replies = [say(env, 1, f"第{i}条消息", at=10 * i) for i in range(1, 8)]
    assert replies == ["💬 user1 活跃发言，获得 5 灵石"] * 7
    wallet = say(env, 1, "灵石", at=100)
    assert wallet.endswith("💬 今日发言奖励 7 次，多发言就有机会获得")


def test_super_admins_adjust_by_replying(env):
    tom = SimpleNamespace(id=7, full_name="Tom", is_bot=False)

    def reply(sender, text, to=tom, **fields):
        env.now[0] += 10
        message = SimpleNamespace(
            text=text,
            caption=None,
            chat=SimpleNamespace(id=GROUP, type="supergroup"),
            message_id=next(MESSAGE_IDS),
            date=datetime.fromtimestamp(env.now[0], UTC),
            sender_chat=None,
            reply_to_message=to
            and SimpleNamespace(**({"from_user": to, "forum_topic_created": None} | fields)),
            reply_text=AsyncMock(return_value=SimpleNamespace(message_id=next(MESSAGE_IDS))),
        )
        user = SimpleNamespace(id=sender, full_name=f"user{sender}", is_bot=False)
        update = SimpleNamespace(effective_message=message, effective_user=user)
        asyncio.run(env.points.message(update, SimpleNamespace(bot=env.bot)))
        call = message.reply_text.await_args
        return call.args[0] if call else None

    assert reply(99, "加灵石 50") == "✅ 已给 Tom 加 50 灵石，现有 50 灵石。"
    assert reply(99, "扣灵石80") == "余额不足：当前 50 灵石，最多扣 50。"
    assert reply(99, " 扣灵石 20 ") == "✅ 已给 Tom 扣 20 灵石，现有 30 灵石。"
    assert env.store.holder(GROUP, 7) == {"display_name": "Tom", "balance": 30}
    assert reply(99, "加30灵石") == "✅ 已给 Tom 加 30 灵石，现有 60 灵石。"
    assert reply(99, "扣 20 灵石") == "✅ 已给 Tom 扣 20 灵石，现有 40 灵石。"
    for chat in ("我加30灵石", "加30灵石吧", "加灵石", "加30"):  # anything else is just chat
        assert reply(99, chat) is None
    assert env.store.holder(GROUP, 7)["balance"] == 40
    ask = "请回复要加减灵石的成员发的消息，再发「加灵石 50」或「扣灵石 20」。"
    assert reply(99, "加灵石 50", to=None) == ask
    assert reply(99, "加灵石 50", forum_topic_created=True) == ask  # a topic's first message
    assert reply(7, "加灵石 50") is None  # only super admins
    assert env.store.holder(GROUP, 7)["balance"] == 40
    env.store.flush_activity()
    with env.store.reading() as db:  # the four chats count as speaking, the rest not
        assert db.execute("SELECT SUM(count) FROM activity").fetchone()[0] == 4


def press_panel(env, user_id, action, status="member"):
    """Press a button on the 灵石 panel; returns what pops up for that member alone."""
    query = SimpleNamespace(
        data=f"pts:{action}",
        answer=AsyncMock(),
        from_user=SimpleNamespace(id=user_id, full_name=f"user{user_id}", is_bot=False),
        message=SimpleNamespace(chat=SimpleNamespace(id=GROUP)),
    )
    env.bot.get_chat_member = AsyncMock(return_value=SimpleNamespace(status=status))
    asyncio.run(
        env.points.button(SimpleNamespace(callback_query=query), SimpleNamespace(bot=env.bot))
    )
    call = query.answer.await_args
    assert call.kwargs == {"show_alert": True}
    return call.args[0]


def test_the_panel_answers_only_the_member_who_pressed(env):
    # The bot sends the group nothing: env.bot cannot even send messages.
    assert press_panel(env, 1, "checkin") == (
        "✅ 签到成功，获得 10 灵石\n💎 当前 10 灵石 · 今天第 1 个签到"
    )
    assert press_panel(env, 1, "checkin") == ALREADY
    env.bot.get_chat_member.assert_not_awaited()  # known to have checked in: no lookup
    assert say(env, 1, "签到") == ALREADY  # the same check-in as by text
    assert press_panel(env, 2, "checkin", status="left") == "只有本群成员可以签到。"
    env.bot.get_chat_member = AsyncMock(side_effect=TimedOut())
    query = SimpleNamespace(
        data="pts:checkin",
        answer=AsyncMock(),
        from_user=SimpleNamespace(id=3, full_name="user3", is_bot=False),
        message=SimpleNamespace(chat=SimpleNamespace(id=GROUP)),
    )
    asyncio.run(
        env.points.button(SimpleNamespace(callback_query=query), SimpleNamespace(bot=env.bot))
    )
    assert query.answer.await_args.args[0] == "暂时无法确认你的群成员身份，请稍后重试。"
    assert press_panel(env, 1, "wallet") == (
        "💎 你的灵石：10\n📅 今日已签到\n💬 今日发言奖励 0 次，多发言就有机会获得"
    )
    for uid in range(10, 17):
        env.store.adjust_points(GROUP, 99, uid, 100 + uid)
        env.store.check_in(GROUP, uid, "名" * 50, "2027-01-15", 10)
    board = press_panel(env, 1, "board")
    assert board.startswith("💎 灵石榜\n🥇 名名名名名名名名名名 · 126\n")
    assert board.count("\n") == 6  # the top five and the one who pressed
    assert board.endswith("你：第 8 名 · 10 灵石")
    assert len(board) <= 200
    env.store.set_group_setting(GROUP, "points", False)
    assert press_panel(env, 1, "wallet") == POINTS_OFF
    assert env.store.due_deletions() == {}


def test_past_the_budget_answers_become_reactions(env):
    env.store.set_group_setting(GROUP, "reward_chance", 100)
    env.store.set_group_setting(GROUP, "crit_percent", 0)
    for uid in range(1, 16):  # fifteen answers in a minute: the budget
        assert say(env, uid, "签到", at=uid).startswith("✅")
    checked_in = post(env, 16, "签到", at=20)
    assert checked_in.reply_text.await_count == 0
    assert reaction(checked_in) == "👍"
    assert env.store.holder(GROUP, 16)["balance"] == 10  # 灵石 all the same
    assert reaction(post(env, 16, "签到", at=21)) == "👌"
    assert reaction(post(env, 16, "灵石", at=22)) == "👀"
    rewarded = post(env, 16, "大家早上好", at=23)
    assert (rewarded.reply_text.await_count, reaction(rewarded)) == (0, "🎉")
    assert env.store.holder(GROUP, 16)["balance"] == 15
    env.now[0] += 600  # the question is tidied away all the same
    assert checked_in.message_id in env.store.due_deletions()[GROUP]
    assert say(env, 17, "签到", at=61).startswith("✅")  # a minute on, room again

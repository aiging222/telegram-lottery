import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import ChatMember, MessageEntity
from telegram.error import BadRequest, TimedOut

from lottery.bot import BotHandlers
from lottery.core import Store
from lottery.moderation import NO_RIGHT, Moderation

GROUP = -100
SUPER = 99  # from ADMIN_USER_IDS
OWNER, ADMIN, WEAK_ADMIN, MEMBER, BANNED = 1, 2, 3, 4, 5
BOT = 4242


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path / "moderation.sqlite3", clock=lambda: 1_800_000_000.0)
    store.remember_group(GROUP, "测试群")
    handlers = BotHandlers(store, {SUPER}, None)
    statuses = {
        OWNER: ChatMember.OWNER,
        ADMIN: ChatMember.ADMINISTRATOR,
        WEAK_ADMIN: ChatMember.ADMINISTRATOR,
        MEMBER: ChatMember.MEMBER,
        BANNED: ChatMember.BANNED,
    }

    async def get_chat_member(chat_id, user_id):
        if user_id == 404:
            raise BadRequest("User not found")
        return SimpleNamespace(
            status=statuses.get(user_id, ChatMember.LEFT),
            can_restrict_members=user_id == ADMIN,
            user=SimpleNamespace(full_name=f"user{user_id}"),
        )

    bot = SimpleNamespace(
        id=BOT,
        get_chat_member=AsyncMock(side_effect=get_chat_member),
        ban_chat_member=AsyncMock(),
        unban_chat_member=AsyncMock(),
        delete_messages=AsyncMock(),
    )
    return SimpleNamespace(moderation=Moderation(handlers), bot=bot, store=store)


def send(env, user_id, text, reply_to=None, chat_type="supergroup", entities=(), anonymous=False):
    """/ban or /unban sent by user_id; returns the bot's answer."""
    replied = None
    if reply_to is not None:
        replied = SimpleNamespace(
            from_user=SimpleNamespace(id=reply_to), sender_chat=None, forum_topic_created=None
        )
    message = SimpleNamespace(
        text=text,
        chat=SimpleNamespace(id=GROUP, type=chat_type),
        message_id=10,
        sender_chat=SimpleNamespace(id=GROUP) if anonymous else None,
        reply_to_message=replied,
        entities=list(entities),
        reply_text=AsyncMock(return_value=SimpleNamespace(message_id=11)),
    )
    update = SimpleNamespace(
        effective_message=message,
        effective_user=SimpleNamespace(id=user_id, is_bot=False),
        effective_chat=message.chat,
    )
    context = SimpleNamespace(bot=env.bot, args=text.split()[1:])
    asyncio.run(env.moderation.command(update, context))
    return message.reply_text.await_args.args[0]


def test_admins_ban_by_reply_or_user_id(env):
    assert send(env, ADMIN, "/ban", reply_to=MEMBER) == "🚫 已封禁 user4（ID：4）。"
    env.bot.ban_chat_member.assert_awaited_once_with(GROUP, MEMBER)
    assert send(env, OWNER, "/ban@lottery_bot 6") == "🚫 已封禁 user6（ID：6）。"
    assert send(env, SUPER, "/ban 7") == "🚫 已封禁 user7（ID：7）。"
    # Picked from the member list by name: the mention says who it is.
    mention = SimpleNamespace(type=MessageEntity.TEXT_MENTION, user=SimpleNamespace(id=8))
    assert send(env, ADMIN, "/ban 小明", entities=[mention]) == "🚫 已封禁 user8（ID：8）。"
    assert env.bot.ban_chat_member.await_count == 4


def test_only_admins_who_may_ban_can(env):
    refused = "只有群主和有「封禁用户」权限的群管理员可以使用 /ban。"
    assert send(env, MEMBER, "/ban 6") == refused
    assert send(env, WEAK_ADMIN, "/ban 6") == refused  # an admin without the right
    assert send(env, ADMIN, "/ban 6", anonymous=True).startswith("匿名发言时查不到你的管理员权限")
    assert send(env, ADMIN, "/ban 6", chat_type="private") == "请在群里使用 /ban。"
    env.bot.ban_chat_member.assert_not_awaited()


def test_who_cannot_be_banned(env):
    assert (
        send(env, ADMIN, "/ban")
        == "用法：回复要封禁的人发的消息，发送 /ban；或者发送 /ban 用户ID。"
    )
    assert send(env, ADMIN, "/ban abc").startswith("用法：")
    assert send(env, ADMIN, "/ban", reply_to=OWNER) == "user1（ID：1）是群管理员，不能封禁。"
    assert send(env, OWNER, "/ban 3") == "user3（ID：3）是群管理员，不能封禁。"
    assert send(env, ADMIN, "/ban", reply_to=ADMIN) == "不能对自己操作。"
    assert send(env, ADMIN, "/ban", reply_to=BOT) == "不能对机器人自己操作。"
    assert send(env, ADMIN, "/ban 5") == "user5（ID：5）已经被封禁了。"
    assert send(env, ADMIN, "/ban 404") == "找不到这个用户，请检查用户 ID。"
    env.bot.ban_chat_member.assert_not_awaited()


def test_admins_unban(env):
    assert send(env, ADMIN, "/unban 5") == "✅ 已解除 user5（ID：5）的封禁，可以重新进群了。"
    env.bot.unban_chat_member.assert_awaited_once_with(GROUP, BANNED, only_if_banned=True)
    assert send(env, OWNER, "/unban", reply_to=MEMBER) == "user4（ID：4）没有被封禁。"
    assert (
        send(env, MEMBER, "/unban 5") == "只有群主和有「封禁用户」权限的群管理员可以使用 /unban。"
    )
    assert env.bot.unban_chat_member.await_count == 1


def test_telegram_refusing_is_explained(env):
    env.bot.ban_chat_member.side_effect = BadRequest("Not enough rights to restrict/unrestrict")
    assert send(env, ADMIN, "/ban 6") == NO_RIGHT
    env.bot.ban_chat_member.side_effect = TimedOut()
    assert send(env, ADMIN, "/ban 6") == "操作失败，请稍后重试。"


def test_the_command_and_answer_are_tidied_away(env):
    send(env, ADMIN, "/ban 6")
    with env.store.reading() as db:
        # 删除机器人通知: 3 seconds, timed right here as well as saved.
        assert db.execute("SELECT COUNT(*) FROM deletions").fetchone()[0] == 2

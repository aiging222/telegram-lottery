"""/ban and /unban in groups. Only the group's owner, its admins who may ban members
themselves, and the super admins can use them: an admin without that right must not be able
to ban through the bot what they could not ban by hand.

The member is the one whose message the command answers, one picked from the member list
by name (a mention that carries who it is), or one whose user ID follows the command."""

import logging

from telegram import ChatMember, MessageEntity
from telegram.error import BadRequest, TelegramError

from lottery.menu import MANAGERS, one_at_a_time, sender
from lottery.views import name_text

LOG = logging.getLogger(__name__)
USAGE = {
    "ban": "用法：回复要封禁的人发的消息，发送 /ban；或者发送 /ban 用户ID。",
    "unban": "用法：回复被封禁的人以前发的消息，发送 /unban；或者发送 /unban 用户ID。",
}
NOT_ALLOWED = "只有群主和有「封禁用户」权限的群管理员可以使用 /{}。"
NO_RIGHT = "机器人没有「封禁用户」权限，请群主在群管理员设置里给机器人打开。"


class Moderation:
    def __init__(self, handlers):
        self.handlers = handlers

    def user_lock(self, user_id):
        return self.handlers.user_lock(user_id)

    @one_at_a_time(sender)
    async def command(self, update, context):
        message, user, chat = update.effective_message, update.effective_user, update.effective_chat
        if message is None or user is None or user.is_bot:
            return
        command = message.text.split()[0][1:].split("@")[0].lower()
        if chat.type not in ("group", "supergroup"):
            await self.handlers.say(message, f"请在群里使用 /{command}。")
            return
        if message.sender_chat:  # an anonymous admin, whose rights cannot be looked up
            text = f"匿名发言时查不到你的管理员权限，请关闭匿名后再用 /{command}。"
        else:
            try:
                text = await self.run(context.bot, message, user, command, context.args)
            except BadRequest as exc:
                problem = str(exc).lower()
                if "rights" in problem or "admin" in problem:
                    text = NO_RIGHT
                elif "not found" in problem or "invalid" in problem:
                    text = "找不到这个用户，请检查用户 ID。"
                else:
                    text = f"操作失败：{exc}"
            except TelegramError as exc:
                LOG.warning("群 %s 的 /%s 失败：%s", chat.id, command, exc)
                text = "操作失败，请稍后重试。"
        await self.handlers.notice(context.bot, message, text)

    async def run(self, bot, message, user, command, args):
        """Ban or unban as `command` says; the answer to give."""
        chat_id = message.chat.id
        if not await self.may_ban(bot, chat_id, user.id):
            return NOT_ALLOWED.format(command)
        target = self.target(message, args)
        if target is None:
            return USAGE[command]
        if target == bot.id:
            return "不能对机器人自己操作。"
        if target == user.id:
            return "不能对自己操作。"
        member = await bot.get_chat_member(chat_id, target)
        name = f"{name_text(member.user.full_name) or '用户'}（ID：{target}）"
        if command == "ban":
            if member.status in MANAGERS:
                return f"{name}是群管理员，不能封禁。"
            if member.status == ChatMember.BANNED:
                return f"{name}已经被封禁了。"
            await bot.ban_chat_member(chat_id, target)
            return f"🚫 已封禁 {name}。"
        if member.status != ChatMember.BANNED:
            return f"{name}没有被封禁。"
        # only_if_banned: never kick someone who is in the group.
        await bot.unban_chat_member(chat_id, target, only_if_banned=True)
        return f"✅ 已解除 {name}的封禁，可以重新进群了。"

    async def may_ban(self, bot, chat_id, user_id):
        if user_id in self.handlers.admin_ids:
            return True
        member = await bot.get_chat_member(chat_id, user_id)
        if member.status == ChatMember.OWNER:
            return True
        return member.status == ChatMember.ADMINISTRATOR and bool(
            getattr(member, "can_restrict_members", False)
        )

    @staticmethod
    def target(message, args):
        """The user ID the command is about, or None if it names nobody."""
        reply = message.reply_to_message
        # In a forum topic every message answers the topic's first one, reply or not; and a
        # message sent as a channel or an anonymous admin shows no one to ban.
        if (
            reply is not None
            and not getattr(reply, "forum_topic_created", None)
            and reply.from_user is not None
            and not reply.sender_chat
        ):
            return reply.from_user.id
        for entity in message.entities or ():
            if entity.type == MessageEntity.TEXT_MENTION and entity.user is not None:
                return entity.user.id
        if args and args[0].isdigit() and 0 < int(args[0]) < 2**63:
            return int(args[0])
        return None

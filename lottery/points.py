"""Every text message in a group comes here. The 灵石 words are answered: members check in
once a day with 「签到」 and look up their 灵石 and the group's ranking, and super admins give
or take 灵石 by replying to a member's message. Other messages count for activity raffles
when they are long enough and not sent too quickly, and each such message has a chance of
earning 灵石.

Telegram lets the bot send a group only about 20 messages a minute. Past its budget (see
BotHandlers.room) these answers become reactions; and the 灵石 panel's buttons answer in a
pop-up for the member who pressed alone, which sends the group nothing at all."""

import asyncio
import logging
import re
import secrets
from datetime import datetime

from telegram import ChatPermissions
from telegram.error import TelegramError

from lottery.core import LotteryError
from lottery.menu import one_at_a_time, presser, sender
from lottery.views import (
    alert_text,
    board_text,
    checkin_text,
    name_text,
    reward_text,
    wallet_text,
)

LOG = logging.getLogger(__name__)
# What members send, as the method that answers it. Such messages never count as speaking.
WORDS = {
    "签到": "check_in",
    "/签到": "check_in",
    "灵石": "wallet",
    "我的灵石": "wallet",
    "灵石榜": "board",
}
# A super admin's reply to a member's message: 「加灵石 50」 or 「扣灵石 20」, also written
# 「加50灵石」 or 「扣20灵石」. Nothing else may be in the message.
ADJUST = re.compile(r"(加|扣) *(?:灵石 *(?P<after>[0-9]{1,9})|(?P<before>[0-9]{1,9}) *灵石)")
BOARD_SIZE = 10
PANEL_BOARD_SIZE = 5  # a pop-up shows at most 200 characters
# Reactions standing in for answers once the group's budget is spent. Telegram allows only
# some emoji as reactions (✅ is not one of them).
DONE, NOTED, SEEN, NO = "👍", "👌", "👀", "🤷"
# Checking in again within SPAM_WINDOW seconds: the WARN_AT-th time is warned, from the
# MUTE_AT-th on the member is muted for MUTE_SECONDS.
SPAM_WINDOW = 60
WARN_AT = 4
MUTE_AT = 5
MUTE_SECONDS = 300
ALREADY = "今天已经签到过了"
WARNING = "已签到，频繁发送将导致账号禁言"
POINTS_OFF = "本群的灵石功能已关闭。"
NO_CHECK_IN = "本群没有开启签到。"


class Points:
    def __init__(self, handlers):
        self.handlers = handlers
        self.store = handlers.store
        self.randbelow = secrets.randbelow  # rolls whether a message earns a reward
        # Kept in memory, and forgotten a day later: (chat_id, user_id, day) known to have
        # checked in, and (chat_id, user_id) -> when they checked in again within the last
        # SPAM_WINDOW.
        self._checked = set()
        self._repeats = {}
        self._day = ""

    def user_lock(self, user_id):
        return self.handlers.user_lock(user_id)

    def today(self, at):
        """The local date of `at`, as "2027-01-15"."""
        day = datetime.fromtimestamp(at, self.handlers.timezone).date().isoformat()
        if day > self._day:  # a new day: forget the one before
            self._day = day
            self._checked = {key for key in self._checked if key[2] == day}
            self._repeats.clear()
        return day

    async def answer(self, bot, message, text, reaction):
        """Answer a 灵石 word, both tidied away later; with the group's budget spent, react
        to it instead."""
        if self.handlers.room(message.chat.id):
            await self.handlers.notice(bot, message, text)
            return
        await self.react(message, reaction)
        await self.handlers.tidy(bot, message.chat.id, [message.message_id], "delete_notices")

    async def react(self, message, reaction):
        try:
            await message.set_reaction(reaction)
        except TelegramError as exc:  # reactions may be off in the group
            LOG.info("群 %s 的表情回应失败：%s", message.chat.id, exc)

    async def enter(self, chat_id, user, day, amount):
        """Check user in for `day`: what check_in() gives, None if they already were."""
        key = (chat_id, user.id, day)
        if key in self._checked:
            return None
        got = await asyncio.to_thread(
            self.store.check_in, chat_id, user.id, name_text(user.full_name), day, amount
        )
        self._checked.add(key)
        return got

    @one_at_a_time(presser)
    async def button(self, update, context):
        """A press on the 灵石 panel: the answer pops up for that member alone."""
        query = update.callback_query
        if query is None or query.from_user.is_bot:
            return
        chat_id = query.message.chat.id
        settings = await asyncio.to_thread(self.store.group_settings, chat_id)
        if settings["points"]:
            press = getattr(self, "press_" + query.data.split(":")[1])
            text = await press(context.bot, chat_id, query.from_user, settings)
        else:
            text = POINTS_OFF
        try:
            await query.answer(alert_text(text), show_alert=True)
        except TelegramError as exc:
            LOG.warning("灵石面板按钮应答失败：%s", exc)

    async def press_checkin(self, bot, chat_id, user, settings):
        if not settings["checkin_points"]:
            return NO_CHECK_IN
        day = self.today(self.store.clock())
        if (chat_id, user.id, day) in self._checked:
            return ALREADY  # no need to ask Telegram about them again
        # Anyone who sees the panel can press it, even outside a public group.
        try:
            member = await self.handlers.group_member(bot, chat_id, user.id)
        except TelegramError as exc:
            LOG.warning("群成员校验失败：%s", exc)
            return "暂时无法确认你的群成员身份，请稍后重试。"
        if not member:
            return "只有本群成员可以签到。"
        got = await self.enter(chat_id, user, day, settings["checkin_points"])
        return checkin_text(got) if got else ALREADY

    async def press_wallet(self, bot, chat_id, user, settings):
        day = self.today(self.store.clock())
        wallet = await asyncio.to_thread(self.store.wallet, chat_id, user.id, day)
        return wallet_text(wallet, settings)

    async def press_board(self, bot, chat_id, user, settings):
        top, _ = await asyncio.to_thread(self.store.holders, chat_id, 0, PANEL_BOARD_SIZE)
        mine = await asyncio.to_thread(self.store.standing, chat_id, user.id)
        return board_text(top, mine, width=10)

    @one_at_a_time(sender)
    async def message(self, update, context):
        message, user = update.effective_message, update.effective_user
        if message is None or user is None or user.is_bot or message.sender_chat:
            return  # channels, anonymous admins and bots neither count nor earn
        chat_id = message.chat.id
        text = message.text or message.caption or ""
        at = message.date.timestamp()
        adjust = ADJUST.fullmatch(text.strip())
        if adjust:
            if user.id in self.handlers.admin_ids:
                await self.adjust(context.bot, message, user, adjust)
            return
        settings = await asyncio.to_thread(self.store.group_settings, chat_id)
        word = WORDS.get(text.strip())
        if word:
            if settings["points"]:
                await getattr(self, word)(context.bot, message, user, settings, at)
            return
        if len("".join(text.split())) < settings["min_chars"]:
            return
        name = name_text(user.full_name)
        if not self.store.count_message(chat_id, user.id, name, at, settings["cooldown"]):
            return
        rewarding = settings["points"] and settings["reward_points"]
        if rewarding and self.randbelow(100) < settings["reward_chance"]:
            await self.reward(context.bot, message, user.id, name, settings, at)

    async def adjust(self, bot, message, user, match):
        """A super admin gives or takes 灵石 by replying to the member's message."""
        reply = message.reply_to_message
        target = getattr(reply, "from_user", None)
        # In a forum topic every message answers the topic's first one, reply or not.
        if getattr(reply, "forum_topic_created", None) or target is None or target.is_bot:
            text = "请回复要加减灵石的成员发的消息，再发「加灵石 50」或「扣灵石 20」。"
        else:
            name = name_text(target.full_name)
            amount = int(match["after"] or match["before"])
            delta = amount if match[1] == "加" else -amount
            try:
                balance = await asyncio.to_thread(
                    self.store.adjust_points, message.chat.id, user.id, target.id, delta, name
                )
            except LotteryError as exc:
                text = str(exc)
            else:
                text = f"✅ 已给 {name} {match[1]} {amount} 灵石，现有 {balance} 灵石。"
                await self.answer(bot, message, text, DONE)
                return
        await self.answer(bot, message, text, NO)

    async def reward(self, bot, message, user_id, name, settings, at):
        day = self.today(at)
        chat_id = message.chat.id
        got = await asyncio.to_thread(
            self.store.reward_message,
            chat_id,
            user_id,
            name,
            day,
            amount=settings["reward_points"],
            limit=settings["reward_daily"],
            crit_percent=settings["crit_percent"],
            crit_times=settings["crit_times"],
        )
        if got is None:
            return  # the day's rewards are all taken
        if not self.handlers.room(chat_id):
            await self.react(message, "⚡" if got["crit"] else "🎉")  # 灵石 given all the same
            return
        try:
            sent = await message.reply_text(reward_text(name, got))
        except TelegramError as exc:
            LOG.info("群 %s 的发言奖励通知发送失败：%s", chat_id, exc)
            return
        self.handlers.spent(chat_id)
        await self.handlers.tidy(bot, chat_id, [sent.message_id], "delete_notices")

    async def check_in(self, bot, message, user, settings, at):
        if not settings["checkin_points"]:
            await self.answer(bot, message, NO_CHECK_IN, NO)
            return
        got = await self.enter(message.chat.id, user, self.today(at), settings["checkin_points"])
        if got:
            await self.answer(bot, message, checkin_text(got), DONE)
        else:
            await self.answer(bot, message, await self.repeated(bot, message, user, at), NOTED)

    async def repeated(self, bot, message, user, at):
        """The answer to checking in again: a warning if it happens WARN_AT times within
        SPAM_WINDOW, and five minutes' mute from MUTE_AT times on."""
        key = (message.chat.id, user.id)
        times = [t for t in self._repeats.get(key, ()) if at - t < SPAM_WINDOW] + [at]
        self._repeats[key] = times
        if len(times) < WARN_AT:
            return ALREADY
        if len(times) < MUTE_AT:
            return WARNING
        # Telegram lifts the mute at until_date, counted from now, not from when it was sent.
        until = int(self.store.clock()) + MUTE_SECONDS
        try:
            await bot.restrict_chat_member(
                message.chat.id, user.id, ChatPermissions.no_permissions(), until_date=until
            )
        except TelegramError as exc:
            # No right to restrict members, or the member is an admin.
            LOG.info("群 %s 禁言 %s 失败：%s", message.chat.id, user.id, exc)
            return WARNING
        del self._repeats[key]
        return f"🔇 {name_text(user.full_name)} 频繁发送签到，已禁言 {MUTE_SECONDS // 60} 分钟。"

    async def wallet(self, bot, message, user, settings, at):
        day = self.today(at)
        chat_id = message.chat.id
        wallet = await asyncio.to_thread(self.store.wallet, chat_id, user.id, day)
        text = wallet_text(wallet, settings, name_text(user.full_name))
        await self.answer(bot, message, text, SEEN)

    async def board(self, bot, message, user, settings, at):
        chat_id = message.chat.id
        top, _ = await asyncio.to_thread(self.store.holders, chat_id, 0, BOARD_SIZE)
        mine = await asyncio.to_thread(self.store.standing, chat_id, user.id)
        await self.answer(bot, message, board_text(top, mine), SEEN)

"""Every text message in a group comes here. The 灵石 words are answered: members check in
once a day with 「签到」 and look up their 灵石 and the group's ranking. Other messages count
for activity raffles and earn 灵石 when they are long enough and not sent too quickly."""

import asyncio
import logging
from datetime import datetime

from telegram import ChatPermissions
from telegram.error import TelegramError

from lottery.menu import one_at_a_time, sender
from lottery.views import board_text, checkin_text, name_text, reward_text, wallet_text

LOG = logging.getLogger(__name__)
# What members send, as the method that answers it. Such messages never count as speaking.
WORDS = {
    "签到": "check_in",
    "/签到": "check_in",
    "灵石": "wallet",
    "我的灵石": "wallet",
    "灵石榜": "board",
}
BOARD_SIZE = 10
# Checking in again within SPAM_WINDOW seconds: the WARN_AT-th time is warned, from the
# MUTE_AT-th on the member is muted for MUTE_SECONDS.
SPAM_WINDOW = 60
WARN_AT = 4
MUTE_AT = 5
MUTE_SECONDS = 300
ALREADY = "今天已经签到过了"
WARNING = "已签到，频繁发送将导致账号禁言"


class Points:
    def __init__(self, handlers):
        self.handlers = handlers
        self.store = handlers.store
        # Kept in memory, and forgotten a day later: (chat_id, user_id, day start) -> their
        # messages counted that day; (chat_id, user_id, day) known to have checked in; and
        # (chat_id, user_id) -> when they checked in again within the last SPAM_WINDOW.
        self._spoken = {}
        self._checked = set()
        self._repeats = {}
        self._day = ""

    def user_lock(self, user_id):
        return self.handlers.user_lock(user_id)

    def today(self, at):
        """The local day of `at`, as (date text, when it began)."""
        moment = datetime.fromtimestamp(at, self.handlers.timezone)
        day = moment.date().isoformat()
        start = moment.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        if day > self._day:  # a new day: forget the one before
            self._day = day
            self._spoken = {key: n for key, n in self._spoken.items() if key[2] >= start}
            self._checked = {key for key in self._checked if key[2] == day}
            self._repeats.clear()
        return day, start

    @one_at_a_time(sender)
    async def message(self, update, context):
        message, user = update.effective_message, update.effective_user
        if message is None or user is None or user.is_bot or message.sender_chat:
            return  # channels, anonymous admins and bots neither count nor earn
        chat_id = message.chat.id
        text = message.text or message.caption or ""
        at = message.date.timestamp()
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
        spoken = await self.spoken_today(chat_id, user.id, at, 1)
        rewarding = settings["points"] and settings["reward_points"]
        if rewarding and spoken % settings["reward_every"] == 0:
            await self.reward(context.bot, message, user.id, name, settings, at)

    async def spoken_today(self, chat_id, user_id, at, more=0):
        """How many of the member's messages counted today, `more` just counted included."""
        _, start = self.today(at)
        key = (chat_id, user_id, start)
        if key in self._spoken:
            self._spoken[key] += more
        else:  # the first time today since the bot started; saved counts include `more`
            self._spoken[key] = await asyncio.to_thread(
                self.store.messages_since, chat_id, user_id, start
            )
        return self._spoken[key]

    async def reward(self, bot, message, user_id, name, settings, at):
        day, _ = self.today(at)
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
        try:
            sent = await message.reply_text(reward_text(name, got, settings["reward_daily"]))
        except TelegramError as exc:
            LOG.info("群 %s 的发言奖励通知发送失败：%s", chat_id, exc)
            return
        await self.handlers.tidy(bot, chat_id, [sent.message_id], "delete_notices")

    async def check_in(self, bot, message, user, settings, at):
        if not settings["checkin_points"]:
            await self.handlers.notice(bot, message, "本群没有开启签到。")
            return
        day, _ = self.today(at)
        chat_id = message.chat.id
        key = (chat_id, user.id, day)
        got = None
        if key not in self._checked:
            got = await asyncio.to_thread(
                self.store.check_in,
                chat_id,
                user.id,
                name_text(user.full_name),
                day,
                settings["checkin_points"],
            )
            self._checked.add(key)
        if got:
            await self.handlers.notice(bot, message, checkin_text(got))
        else:
            await self.handlers.notice(bot, message, await self.repeated(bot, message, user, at))

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
        day, _ = self.today(at)
        chat_id = message.chat.id
        wallet = await asyncio.to_thread(self.store.wallet, chat_id, user.id, day)
        spoken = await self.spoken_today(chat_id, user.id, at)
        text = wallet_text(name_text(user.full_name), wallet, settings, spoken)
        await self.handlers.notice(bot, message, text)

    async def board(self, bot, message, user, settings, at):
        chat_id = message.chat.id
        top, _ = await asyncio.to_thread(self.store.holders, chat_id, 0, BOARD_SIZE)
        mine = await asyncio.to_thread(self.store.standing, chat_id, user.id)
        await self.handlers.notice(bot, message, board_text(top, mine))

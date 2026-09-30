"""Button menus in private chat. Group admins create and manage their group's raffles;
the super admins listed in ADMIN_USER_IDS may manage every group and alone see and set
the weights."""

import asyncio
import functools
import logging
import re
import time
from datetime import datetime

from telegram import ChatMember, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, ChatMigrated, Forbidden, TelegramError

from lottery.core import (
    LOOK_BACK_DAYS,
    MAX_PRIZES,
    SETTING_LIMITS,
    LotteryError,
    check_keyword,
    integer,
)
from lottery.views import (
    EXPORT_CAPTION,
    INVITE_WAYS,
    activity_rule,
    chances,
    cost_text,
    counting_text,
    draw_rule,
    export_file,
    holder_name,
    join_text,
    name_text,
    place,
    points_file,
    prize_text,
    status_text,
    when_text,
)

LOG = logging.getLogger(__name__)
MANAGERS = {ChatMember.OWNER, ChatMember.ADMINISTRATOR}
GONE = {ChatMember.LEFT, ChatMember.BANNED}
ADMIN_CACHE_SECONDS = 60
PAGE_SIZE = 8
MESSAGE_LIMIT = 4096  # Telegram's characters in a message
FULL_DEADLINE_MINUTES = 7 * 1440  # a raffle that never fills up still ends after a week
COUNTS = (1, 2, 3, 5, 10)
DURATIONS = (("1小时", 60), ("6小时", 360), ("1天", 1440), ("3天", 4320), ("7天", 10080))
TARGETS = (10, 50, 100)
MINIMUMS = (5, 10, 20, 50, 100)
INVITES_NEEDED = (1, 3, 5, 10, 20)
# Invite raffles in the creation wizard: its kind is "inv" until the draw is picked.
INVITE_KINDS = {"irank": "rank", "ireach": "reach"}
COSTS = (10, 20, 50, 100)
WEIGHTS = (0, 1, 2, 3, 5, 10)
CORRECTIONS = (1, 5, 10, -1, -5, -10)
UNITS = {"分钟": 1, "分": 1, "m": 1, "小时": 60, "时": 60, "h": 60, "天": 1440, "d": 1440}
CONFIRM = {
    "draw": "确定现在开奖？",
    "freeze": "确定截止报名？截止后不能再报名。",
    "cancel": "确定取消这场抽奖？取消后不能恢复。",
}
# Group settings: button label, and for switches the values one press cycles through; the
# deletion delays are picked from DELAYS or typed in minutes instead.
SETTINGS = {
    "pin_card": ("📌 置顶报名卡片", (True, False)),
    "pin_result": ("📌 置顶开奖公告", (True, False)),
    "delete_keyword": ("🧹 删除口令消息", None),
    "delete_notices": ("🗑 删除机器人通知", None),
}
DELAYS = (0, 3, 60, 300, 600, 3600, None)  # seconds after posting; None keeps the message
MAX_DELAY_MINUTES = 1440
DELAY_QUESTIONS = {
    "delete_keyword": "群友发的口令消息多久后删除？",
    "delete_notices": "机器人在群里的通知（签到、查询、奖励、提示等）多久后删除？",
}
# 灵石 settings: button label, how a value reads, preset buttons and the question asked.
POINT_SETTINGS = {
    "checkin_points": (
        "📅 签到",
        "{} 灵石",
        (5, 10, 20, 50),
        "每天签到得多少灵石？0 表示关闭签到。",
    ),
    "reward_chance": (
        "🎲 奖励概率",
        "{}%",
        (1, 3, 5, 10, 20),
        "每条有效发言有百分之几的概率获得发言奖励？",
    ),
    "reward_points": ("🎁 发言奖励", "{} 灵石", (0, 1, 5, 10), "每次奖励多少灵石？0 表示不奖励。"),
    "reward_daily": (
        "🔁 每天最多",
        "{} 次",
        (1, 3, 5, 10, 0),
        "每人每天最多领几次发言奖励？0 表示不限。",
    ),
    "crit_percent": ("⚡ 暴击率", "{}%", (0, 5, 10, 20), "发言奖励暴击的概率是百分之几？"),
    "crit_times": ("✖️ 暴击倍数", "{} 倍", (2, 3, 5), "暴击时奖励是平时的几倍？"),
    "min_chars": ("✍️ 最少字数", "{} 字", (1, 2, 3, 5), "一条发言至少几个字才算有效发言？"),
    "cooldown": (
        "⏱ 发言间隔",
        "{} 秒",
        (0, 3, 5, 10),
        "距上一条有效发言至少几秒，才算新的一条？0 表示不限。",
    ),
}
# How a 0 reads where it means more than the number.
ZERO_TEXT = {"checkin_points": "关", "reward_points": "关", "reward_daily": "不限"}
BALANCE_STEPS = (10, 50, 100, -10, -50, -100)
POINT_KEYS = ("points", *POINT_SETTINGS)  # the settings on the 灵石 page
CHANGES_SHOWN = 15
NOT_MANAGER = "只有该群的管理员可以管理抽奖。"
NOT_SUPER = "只有超级管理员可以设置中奖加成。"
NOT_BANKER = "只有超级管理员可以修改余额和导出流水。"
EXPIRED = "操作已过期，请重新开始。"
STALE = "这个按钮已过期，请用最新一条消息里的按钮。"
# The question each creation wizard button answers. Buttons left on earlier messages count
# only while the draft is still at that question, so they cannot undo later answers.
BUTTON_STEPS = {
    "k": "kind",
    "ka": "activity",
    "fr": "since",
    "pe": "rank",
    "mm": "minimum",
    "iv": "via",
    "ik": "ikind",
    "ro": "repok",
    "rc": "report",
    "co": "cost",
    "c": "count",
    "mode": "mode",
    "t": "time",
    "f": "target",
    "j": "join",
    "tt": "title",
    **dict.fromkeys(("more", "to", "cur", "pub", "wz"), "confirm"),
}
TYPED_STEPS = {
    "prize",
    "count",
    "time",
    "target",
    "keyword",
    "title",
    "since",
    "rank",
    "minimum",
    "cost",
    "report",
}
# A public group typed as a link, t.me/name or @name; private invite links name no group.
PUBLIC_GROUP = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me)/([A-Za-z0-9_]{4,32})/?|@([A-Za-z0-9_]{4,32})"
)


def button(text, data):
    return InlineKeyboardButton(text, callback_data=data)


def keyboard(*rows):
    return InlineKeyboardMarkup([list(row) for row in rows if row])


def in_rows(buttons, width):
    return [buttons[i : i + width] for i in range(0, len(buttons), width)]


CANCEL_ROW = (button("✖ 取消", "m:quit"),)


def one_at_a_time(user_of):
    """Handle one person's updates one after another, in the order they came, although the
    bot handles updates concurrently; user_of(update) is that person. A double tap on
    "✅ 发布抽奖" thus cannot publish a draft twice."""

    def wrap(handler):
        @functools.wraps(handler)
        async def run(self, update, context):
            user = user_of(update)
            if user is None:
                return await handler(self, update, context)
            async with self.user_lock(user.id):
                return await handler(self, update, context)

        return run

    return wrap


def sender(update):
    return update.effective_user


def presser(update):
    return update.callback_query.from_user if update.callback_query else None


def room_for_prize(data):
    """Whether the draft takes another prize: 10 kinds and 100 winners at most, and no more
    winners than the people that fill the raffle."""
    total = sum(count for _, count in data["prizes"])
    return len(data["prizes"]) < MAX_PRIZES and total < min(100, data.get("target") or 100)


def local_time(text, timezone):
    """'YYYY-MM-DD HH:MM' in timezone as a timestamp, or None if text is not one."""
    try:
        moment = datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=timezone)
    except ValueError:
        return None
    return moment.timestamp()


def parse_time(text, now, timezone):
    """A typed answer to "when" as the draft field it fills: {"minutes": n} for '90分钟' /
    '2小时' / '3天' / '45' (minutes), counted from publishing like the buttons, or
    {"deadline": timestamp} for 'YYYY-MM-DD HH:MM' / 'MM-DD HH:MM'.

    Without a year it is the nearest such date, before or after: in December "01-05" is next
    January, in early January "12-31" last December. A draw's time that comes out past is
    refused rather than moved to next year."""
    text = " ".join(text.replace("：", ":").split())
    match = re.fullmatch(r"(\d{1,6}) ?(分钟|分|小时|时|天|m|h|d)?", text, re.IGNORECASE)
    if match:
        return {"minutes": int(match[1]) * UNITS[(match[2] or "分钟").lower()]}
    deadline = local_time(text, timezone)
    if deadline is None:
        year = datetime.fromtimestamp(now, timezone).year
        dates = [local_time(f"{y}-{text}", timezone) for y in (year - 1, year, year + 1)]
        deadline = min(filter(None, dates), key=lambda d: abs(d - now), default=None)
    if deadline is None:
        raise LotteryError("看不懂这个时间，请按 2026-10-05 20:00 或 2小时 这样输入。")
    return {"deadline": deadline}


def setting_text(value):
    if value is True or value is False:
        return "开" if value else "关"
    if value is None:
        return "不删"
    if value == 0:
        return "立即"
    if value < 60:
        return f"{value}秒后"
    if value % 3600 == 0:
        return f"{value // 3600}小时后"
    return f"{value // 60}分钟后"


def point_value(key, value):
    if value == 0 and key in ZERO_TEXT:
        return ZERO_TEXT[key]
    return POINT_SETTINGS[key][1].format(value)


def change_text(change, timezone):
    """One change to a group setting, as "01-15 16:00 张三：🎲 奖励概率 5% → 8%"."""
    key = change["key"]
    if key in POINT_SETTINGS:
        label, shown = POINT_SETTINGS[key][0], functools.partial(point_value, key)
    else:
        label = SETTINGS[key][0] if key in SETTINGS else "💎 灵石功能"
        shown = setting_text
    who = name_text(change["actor_name"]) or f"用户 {change['actor_id']}"
    when = datetime.fromtimestamp(change["at"], timezone)
    before, after = shown(change["before"]), shown(change["after"])
    return f"{when:%m-%d %H:%M} {who}：{label} {before} → {after}"


def utf16_len(text):
    """Length as Telegram counts it: emoji and other characters beyond the BMP count twice."""
    return len(text.encode("utf-16-le")) // 2


def winners_line(winners, room):
    """中奖：A（prize）、B（prize）… in at most `room` of Telegram's characters; when not all
    fit, as many as do and how many won in all."""
    names = [
        name_text(w["display_name"]) + (f"（{w['prize']}）" if w.get("prize") else "")
        for w in winners
    ]
    line = "中奖：" + ("、".join(names) or "无")
    if utf16_len(line) <= room:
        return line
    tail = f"…等 {len(names)} 人，完整名单见开奖公告"
    line = "中奖："
    for index, name in enumerate(names):
        more = ("、" if index else "") + name
        if utf16_len(line + more + tail) > room:
            break
        line += more
    return line + tail


def number(text, name, low, high):
    try:
        return integer(int(text), name, low, high)
    except ValueError:
        raise LotteryError(f"{name}需为 {low}～{high} 的整数，请重新输入。") from None


class Menu:
    def __init__(self, handlers):
        self.handlers = handlers
        self.store = handlers.store
        self._managers = {}  # (chat_id, user_id) -> (expires_at, allowed)
        # user_id -> (kind, raffle_id or chat_id, user_id or setting or None, page): a page
        # waiting for a typed answer. Any button press or /start drops it; a restart forgets it.
        self._asking = {}
        # user_id -> the name they last used the menu under, for the record of setting changes.
        self._names = {}

    def user_lock(self, user_id):
        return self.handlers.user_lock(user_id)

    def saw(self, user):
        self._names[user.id] = name_text(getattr(user, "full_name", "") or "")

    async def set_setting(self, user_id, chat_id, key, value):
        """Change a group setting as user_id, who goes into the record of changes."""
        change = functools.partial(
            self.store.set_group_setting,
            chat_id,
            key,
            value,
            actor=user_id,
            actor_name=self._names.get(user_id, ""),
        )
        await asyncio.to_thread(change)

    # Permissions

    async def can_manage(self, bot, user_id, chat_id):
        if user_id in self.handlers.admin_ids:
            return True
        cached = self._managers.get((chat_id, user_id))
        if cached and cached[0] > time.monotonic():
            return cached[1]
        try:
            allowed = (await bot.get_chat_member(chat_id, user_id)).status in MANAGERS
        except TelegramError as exc:
            LOG.warning("无法确认 %s 是否为群 %s 的管理员：%s", user_id, chat_id, exc)
            allowed = False  # fail closed
        else:
            await asyncio.to_thread(self.store.set_manager, chat_id, user_id, allowed)
        self._managers[(chat_id, user_id)] = (time.monotonic() + ADMIN_CACHE_SECONDS, allowed)
        return allowed

    async def require(self, bot, user_id, chat_id):
        if chat_id is None or not await self.can_manage(bot, user_id, chat_id):
            raise LotteryError(NOT_MANAGER)

    async def sync_admins(self, bot, chat_id):
        """Record the group's admins as Telegram lists them, so they find it in "我的群"."""
        try:
            admins = await bot.get_chat_administrators(chat_id)
        except ChatMigrated as exc:
            await self.handlers.follow_migration(bot, chat_id, exc.new_chat_id)
            await self.sync_admins(bot, exc.new_chat_id)
            return
        except (Forbidden, BadRequest) as exc:
            # Removed or deleted while the bot was away longer than Telegram keeps updates.
            LOG.warning("群 %s 已无法访问，不再列出：%s", chat_id, exc)
            await asyncio.to_thread(self.store.deactivate_group, chat_id)
            return
        except TelegramError as exc:
            LOG.warning("无法获取群 %s 的管理员名单：%s", chat_id, exc)
            return
        await asyncio.to_thread(self.store.set_admins, chat_id, [a.user.id for a in admins])

    async def sync_all_admins(self, context):
        """At startup, since admins may have changed while the bot was away (chat_member
        updates keep the list current while it runs)."""
        for chat_id, _ in await asyncio.to_thread(self.store.groups):
            await self.sync_admins(context.bot, chat_id)

    async def my_groups(self, bot, user_id):
        """Groups user_id manages, as (chat_id, title). Only super admins get every group the
        bot is in; anyone else is checked against the groups they were last seen managing,
        so a press costs a lookup per own group rather than one per group the bot is in."""
        if self.is_super(user_id):
            return await asyncio.to_thread(self.store.groups)
        seen = await asyncio.to_thread(self.store.managed_groups, user_id)
        return [(chat, title) for chat, title in seen if await self.can_manage(bot, user_id, chat)]

    def is_super(self, user_id):
        return user_id in self.handlers.admin_ids

    def require_super(self, user_id):
        if not self.is_super(user_id):
            raise LotteryError(NOT_SUPER)

    # Entry points

    @staticmethod
    def add_button(bot):
        # startgroup opens Telegram's group picker; admin= pre-selects the rights we ask for.
        url = (
            f"https://t.me/{bot.username}?startgroup=menu"
            "&admin=delete_messages+pin_messages+restrict_members+invite_users"
        )
        return InlineKeyboardButton("➕ 添加到群组", url=url)

    async def welcome(self, bot, chat_id, text, trigger=None):
        """Post a message in the group whose button opens that group's menu in private.
        It is tidied away later, together with the /start that asked for it."""
        link = f"https://t.me/{bot.username}?start=g{chat_id}"
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("⚙️ 管理抽奖", url=link)]])
        try:
            sent = await bot.send_message(chat_id, text, reply_markup=markup)
        except TelegramError as exc:
            LOG.warning("群 %s 的欢迎消息发送失败：%s", chat_id, exc)
            return
        self.handlers.spent(chat_id)
        ids = [sent.message_id] if trigger is None else [trigger, sent.message_id]
        # Left a minute at least, for an admin to press its button.
        await self.handlers.tidy(bot, chat_id, ids, "delete_notices", at_least=60)

    @one_at_a_time(sender)
    async def start(self, update, context):
        message, user, chat = update.effective_message, update.effective_user, update.effective_chat
        if message is None or user is None or user.is_bot:
            return
        if chat.type != "private":
            await asyncio.to_thread(self.store.remember_group, chat.id, chat.title or str(chat.id))
            await self.welcome(
                context.bot,
                chat.id,
                "点击下面按钮管理本群抽奖（仅限群管理员）。",
                message.message_id,
            )
            return
        self._asking.pop(user.id, None)
        payload = context.args[0] if context.args else ""
        if payload.startswith("inv"):  # 「🔗 领取我的邀请链接」 on an invite raffle's card
            try:
                chat_id = int(payload[3:])
            except ValueError:
                await message.reply_text("这个链接无效。")
                return
            text = await self.handlers.invite_link_text(context.bot, chat_id, user)
            await message.reply_text(text)
            return
        if payload.startswith("g"):
            try:
                chat_id = int(payload[1:])
            except ValueError:
                chat_id = None
            if chat_id is not None and await self.can_manage(context.bot, user.id, chat_id):
                text, markup = await self.group_menu(chat_id)
            else:
                text, markup = NOT_MANAGER, None
        else:
            text = "🎁 抽奖助手\n把我拉进群并设为管理员，就能在群里发起抽奖。"
            markup = keyboard((self.add_button(context.bot),), (button("📋 我的群", "m:groups"),))
        await message.reply_text(text, reply_markup=markup)

    async def bot_membership(self, update, context):
        """The bot joined, left or changed rights in a chat."""
        change = update.my_chat_member
        chat = change.chat
        if chat.type not in ("group", "supergroup"):
            return
        status = change.new_chat_member.status
        await asyncio.to_thread(
            self.store.remember_group, chat.id, chat.title or str(chat.id), status not in GONE
        )
        if status in GONE or change.old_chat_member.status not in GONE:
            return  # it left, or only its rights changed
        await self.sync_admins(context.bot, chat.id)
        if status == ChatMember.ADMINISTRATOR:
            text = "✅ 已就绪。群管理员点击下面按钮发起和管理抽奖。"
        else:
            text = "请把我设为管理员（需要：删除消息、置顶消息、封禁用户、邀请用户），否则无法正常工作。"
        await self.welcome(context.bot, chat.id, text)

    # Button presses

    @one_at_a_time(presser)
    async def callback(self, update, context):
        query = update.callback_query
        if query is None or query.from_user.is_bot:
            return
        action, *args = query.data.split(":")[1:]
        self._asking.pop(query.from_user.id, None)
        self.saw(query.from_user)
        try:
            text, markup = await self.route(context, query.from_user.id, action, args)
        except LotteryError as exc:
            await query.answer(str(exc), show_alert=True)
            return
        except (ValueError, IndexError, KeyError):
            await query.answer("按钮已失效。", show_alert=True)
            return
        await query.answer()
        try:
            await query.edit_message_text(text, reply_markup=markup)
        except BadRequest as exc:
            if "not modified" in str(exc):
                return
            # The menu message was deleted or cannot be edited; carry on in a fresh one.
            await context.bot.send_message(query.from_user.id, text, reply_markup=markup)

    async def route(self, context, user_id, action, args):
        bot = context.bot
        if action == "groups":
            return await self.groups_menu(bot, user_id)
        if action == "g":
            await self.require(bot, user_id, int(args[0]))
            return await self.group_menu(int(args[0]))
        if action == "new":
            return await self.wizard_start(bot, user_id, int(args[0]))
        if action == "quit":
            return await self.wizard_quit(user_id)
        if action in BUTTON_STEPS:
            return await self.wizard_step(bot, user_id, action, args[0] if args else "")
        if action == "list":
            await self.require(bot, user_id, int(args[0]))
            return await self.records(int(args[0]), int(args[1]))
        if action == "r":
            return await self.detail(bot, user_id, int(args[0]))
        if action == "ask":
            return await self.confirm(bot, user_id, args[0], int(args[1]))
        if action == "do":
            return await self.act(bot, user_id, args[0], int(args[1]))
        if action == "repost":
            return await self.repost(bot, user_id, int(args[0]))
        if action in ("set", "sv", "sk", "sd"):
            chat_id = int(args[0])
            await self.require(bot, user_id, chat_id)
            if action == "sv" and SETTINGS[args[1]][1] is None:
                action = "sk"  # a delay's button from a menu shown before they were picked
            if action == "sv":
                await self.cycle_setting(user_id, chat_id, args[1])
            elif action == "sk":
                return await self.delay_prompt(user_id, chat_id, args[1])
            elif action == "sd":
                if args[1] not in DELAY_QUESTIONS:
                    raise ValueError(args[1])
                delay = None if args[2] == "n" else int(args[2])
                await self.set_setting(user_id, chat_id, args[1], delay)
            return await self.settings(bot, chat_id)
        if action == "log":
            chat_id = int(args[0])
            await self.require(bot, user_id, chat_id)
            return await self.changes(chat_id, args[1])
        if action in ("w", "wu", "ws", "wd", "wid", "wc", "export"):
            self.require_super(user_id)
            return await self.weight_action(bot, user_id, action, args)
        if action in ("ac", "au", "as", "aid"):
            if not self.is_super(user_id):
                raise LotteryError("只有超级管理员可以修改发言次数。")
            return await self.count_action(user_id, action, args)
        if action in ("pt", "pk", "pv", "pp"):
            chat_id = int(args[0])
            await self.require(bot, user_id, chat_id)
            return await self.points_action(bot, user_id, action, chat_id, args[1:])
        if action in ("pb", "pu", "pa", "pid", "pf", "px"):
            if not self.is_super(user_id):
                raise LotteryError(NOT_BANKER)
            return await self.balance_action(bot, user_id, action, int(args[0]), args[1:])
        raise ValueError(action)

    # Group menu

    async def groups_menu(self, bot, user_id):
        mine = await self.my_groups(bot, user_id)
        if not mine:
            return (
                "还没有你能管理的群。先把我拉进群并设为管理员；"
                "我已在群里的话，在群里发送 /start，再点「⚙️ 管理抽奖」。"
            ), keyboard((self.add_button(bot),))
        rows = [(button(title[:30], f"m:g:{chat}"),) for chat, title in mine]
        return "选择要管理的群：", keyboard(*rows, (self.add_button(bot),))

    async def group_menu(self, chat_id):
        title = await asyncio.to_thread(self.store.group_title, chat_id)
        counts = await asyncio.to_thread(self.store.group_summary, chat_id)
        text = (
            f"🎁 {title} · 抽奖\n进行中 {counts['active']} ｜ 已开奖 {counts['drawn']}"
            f" ｜ 已取消 {counts['cancelled']}"
        )
        return text, keyboard(
            (
                button("➕ 发起抽奖", f"m:new:{chat_id}"),
                button("📜 抽奖记录", f"m:list:{chat_id}:0"),
            ),
            (button("⚙️ 抽奖设置", f"m:set:{chat_id}"), button("💎 灵石设置", f"m:pt:{chat_id}")),
            (button("🔄 切换群", "m:groups"),),
        )

    # Creation wizard. The draft survives restarts. Every question shows what is filled in so
    # far; data["step"] names the question asked, typed answers go to the TYPED_STEPS, and
    # buttons are shortcuts.

    async def wizard_start(self, bot, user_id, chat_id):
        await self.require(bot, user_id, chat_id)
        return await self.advance(user_id, chat_id, {"prizes": []})

    async def advance(self, user_id, chat_id, data):
        """Save the draft together with its next question, and return that question."""
        text, markup = await self.next_prompt(user_id, chat_id, data)
        await asyncio.to_thread(self.store.save_draft, user_id, chat_id, data)
        return text, markup

    async def wizard_quit(self, user_id):
        found = await asyncio.to_thread(self.store.draft, user_id)
        await asyncio.to_thread(self.store.drop_draft, user_id)
        return (
            await self.group_menu(found[0]) if found else ("已退出。发送 /start 打开菜单。", None)
        )

    @one_at_a_time(sender)
    async def cancel(self, update, context):
        """/cancel leaves the creation wizard or a weight prompt."""
        self._asking.pop(update.effective_user.id, None)
        text, markup = await self.wizard_quit(update.effective_user.id)
        await update.effective_message.reply_text(text, reply_markup=markup)

    async def wizard_step(self, bot, user_id, action, value):
        found = await asyncio.to_thread(self.store.draft, user_id)
        if not found:
            raise LotteryError(EXPIRED)
        chat_id, data = found
        if data.get("step") is None:
            await self.next_prompt(user_id, chat_id, data)  # saved before steps were all named
        if data["step"] != BUTTON_STEPS[action]:
            raise LotteryError(STALE)
        if action == "pub":
            return await self.publish(bot, user_id, chat_id, data)
        if action == "to":
            if not value:
                return await self.target_groups(bot, user_id, chat_id)
            chat_id = int(value)
            await self.require(bot, user_id, chat_id)
        elif action == "wz":
            self.require_super(user_id)
            data["weighted"] = not data.get("weighted")
        elif action == "more":
            if not room_for_prize(data):
                raise LotteryError("奖品最多 10 种、共 100 人，不超过满人开奖人数。")
            if data["kind"] in ("rank", "irank"):
                data.pop("ranks_done")
            else:
                data["adding"] = True
        elif action == "pe":
            if not data["prizes"]:
                raise LotteryError("请先发送第一名的奖品。")
            data["ranks_done"] = True
        elif action == "tt":
            self.answer(data, "title", data["prizes"][0][0])
        elif action == "ro":
            if value == "back":
                del data["kind"]
            else:  # the groups to pick from, those the admin manages besides this one
                mine = await self.my_groups(bot, user_id)
                data["report_choices"] = [[chat, title] for chat, title in mine if chat != chat_id]
        elif action == "rc":
            if value == "back":
                data.pop("report_choices")
            else:
                chosen = dict(data["report_choices"])
                report = int(value)
                if report not in chosen:
                    raise LotteryError(STALE)
                await self.require(bot, user_id, report)
                data.update(report=report, report_title=chosen[report])
        elif action != "cur":
            self.answer(data, BUTTON_STEPS[action], value, typed=False)
        return await self.advance(user_id, chat_id, data)

    @one_at_a_time(sender)
    async def text(self, update, context):
        """A typed answer to the current wizard step or weight prompt."""
        message, user = update.effective_message, update.effective_user
        self.saw(user)
        asking = self._asking.get(user.id)
        if asking:
            points = asking[0] in ("setting", "balance", "balance_id", "balance_find")
            typed = self.points_typed if points else self.weight_typed
            if asking[0] == "delay":
                typed = self.delay_typed
            await typed(context.bot, message, user.id, asking)
            return
        found = await asyncio.to_thread(self.store.draft, user.id)
        if not found:
            await message.reply_text("发送 /start 打开菜单。")
            return
        chat_id, data = found
        if data.get("step") not in TYPED_STEPS:
            await message.reply_text("请点上面消息里的按钮选择，或发送 /cancel 退出。")
            return
        try:
            if data["step"] == "report":
                await self.report_typed(context.bot, user.id, chat_id, data, message.text)
            else:
                self.answer(data, data["step"], message.text.strip())
        except LotteryError as exc:
            await message.reply_text(str(exc), reply_markup=keyboard(CANCEL_ROW))
            return
        text, markup = await self.advance(user.id, chat_id, data)
        await message.reply_text(text, reply_markup=markup)

    def answer(self, data, step, value, typed=True):
        """Fill in one answer, typed or from a button."""
        total = sum(count for _, count in data["prizes"])
        if step == "prize":
            if not 1 <= len(value) <= 64:
                raise LotteryError("奖品名称需为 1～64 字，请重新输入。")
            data["pending"] = value
            data.pop("adding", None)
        elif step == "count":
            count = number(value, "份数", 1, 100 - total)
            if data.get("target") and total + count > data["target"]:
                raise LotteryError(f"中奖总人数不能超过满人开奖人数 {data['target']}。")
            data["prizes"].append([data.pop("pending"), count])
        elif step == "mode":
            if value == "back":  # 积分抽奖 asks this first, right after the kind
                del data["kind"]
            else:
                data["mode"] = {"t": "t", "f": "f"}[value]
        elif step == "time":
            now = self.store.clock()
            if typed:
                when = parse_time(value, now, self.handlers.timezone)
            else:
                when = {"minutes": integer(int(value), "报名时长（分钟）", 1, 525600)}
            deadline = when["deadline"] if "deadline" in when else now + when["minutes"] * 60
            if deadline < now:
                raise LotteryError(
                    "这个时间已经过了，开奖时间不能早于现在，请重新输入；明年的日期请写上年份。"
                )
            if not now + 60 <= deadline <= now + 525600 * 60:
                raise LotteryError("开奖时间需在 1 分钟到 365 天之后，请重新输入。")
            if deadline <= (data.get("count_from") or 0):
                raise LotteryError("开奖时间要晚于开始统计发言的时间，请重新输入。")
            data.update(when)
        elif step == "kind":
            kinds = ("join", "act", "points", "inv", "rep")
            data["kind"] = {kind: kind for kind in kinds}[value]
        elif step == "via":
            if value == "back":
                del data["kind"]
            else:
                data["via"] = {"link": "link", "add": "add"}[value]
        elif step == "ikind":
            if value == "back":
                del data["via"]
            else:
                data["kind"] = {"rank": "irank", "reach": "ireach"}[value]
                if value == "rank":
                    data["mode"] = "t"  # drawn at a set time
        elif step == "activity":
            if value == "back":
                del data["kind"]
            else:
                data["kind"] = {"rank": "rank", "reach": "reach"}[value]
                data["mode"] = "t"  # drawn at a set time
        elif step == "since":
            data["count_from"] = self.since(value)
        elif step == "rank":
            if not 1 <= len(value) <= 64:
                raise LotteryError("奖品名称需为 1～64 字，请重新输入。")
            data["prizes"].append([value, 1])
            if len(data["prizes"]) == MAX_PRIZES:
                data["ranks_done"] = True
        elif step == "minimum":
            name = "邀请人数" if data["kind"] == "ireach" else "发言次数"
            data["min_messages"] = number(value, name, 1, 100_000)
        elif step == "cost":
            data["cost"] = number(value, "参与所需灵石", 1, 1_000_000)
        elif step == "target":
            data["target"] = number(value, "满人开奖人数", max(total, 1), 100_000)
        elif step == "join":
            data["join"] = {"b": "b", "k": "k"}[value]
        elif step == "keyword":
            data["keyword"] = check_keyword(value)
        else:
            if not 1 <= len(value) <= 160:
                raise LotteryError("活动名称需为 1～160 字，请重新输入。")
            data["title"] = value

    async def next_prompt(self, user_id, chat_id, data):
        """The next unanswered question; sets data["step"] to it."""
        if "kind" not in data and (data["prizes"] or "pending" in data):
            data["kind"] = "join"  # saved before there was a choice of kind
        questions = {
            None: [self.ask_kind],
            "act": [self.ask_activity],
            "join": [
                self.ask_prizes,
                self.ask_mode,
                self.ask_time,
                self.ask_target,
                self.ask_join,
                self.ask_keyword,
                self.ask_title,
            ],
            "points": [
                self.ask_mode,
                self.ask_prizes,
                self.ask_time,
                self.ask_target,
                self.ask_cost,
                self.ask_join,
                self.ask_keyword,
                self.ask_title,
            ],
            "inv": [self.ask_via, self.ask_invite_kind],
            "rep": [
                self.ask_report_intro,
                self.ask_report_chat,
                self.ask_prizes,
                self.ask_mode,
                self.ask_time,
                self.ask_target,
                self.ask_title,
            ],
            "irank": [self.ask_time, self.ask_ranks, self.ask_title],
            "ireach": [
                self.ask_minimum,
                self.ask_mode,
                self.ask_time,
                self.ask_target,
                self.ask_prizes,
                self.ask_title,
            ],
            "rank": [self.ask_since, self.ask_time, self.ask_ranks, self.ask_title],
            "reach": [
                self.ask_since,
                self.ask_time,
                self.ask_minimum,
                self.ask_prizes,
                self.ask_title,
            ],
        }[data.get("kind")]
        for ask in questions:
            prompt = await ask(chat_id, data)
            if prompt:
                return prompt
        return await self.ask_confirm(user_id, chat_id, data)

    async def ask_kind(self, chat_id, data):
        data["step"] = "kind"
        ask = (
            "🎟 普通抽奖：点按钮或发口令参与\n"
            "🔥 群活跃抽奖：按发言排名，或发言达到次数参与随机抽奖\n"
            "🪙 积分抽奖：用签到、发言得到的灵石报名，参与时扣除\n"
            "🪁 邀请抽奖：用专属链接或「添加成员」拉人进群，按邀请人数排名或达到人数参与抽奖\n"
            "🙋 指定群报道抽奖：本群成员加入指定的报道群即可参与\n\n"
            "选择抽奖类型："
        )
        return self.draft_text(data, ask), keyboard(
            (button("🎟 普通抽奖", "m:k:join"), button("🔥 群活跃抽奖", "m:k:act")),
            (button("🪙 积分抽奖", "m:k:points"), button("🪁 邀请抽奖", "m:k:inv")),
            (button("🙋 指定群报道抽奖", "m:k:rep"),),
            CANCEL_ROW,
        )

    async def ask_report_intro(self, chat_id, data):
        if "report_choices" in data:
            return None
        data["step"] = "repok"
        ask = (
            "🙋 指定群报道抽奖：本群成员加入指定的报道群即可参与。进报道群时自动报名；"
            "原来就在报道群里的，点卡片上的「✅ 我已加入报道群」报名。开奖前退出任一个群都会取消报名。\n\n"
            "注意：两个群都要把机器人拉进去并设为管理员，机器人才收得到有人进群的通知；"
            "报道群是私密群的话，机器人还要有「邀请用户」权限，才能在卡片上放进群按钮。\n\n"
            "是否继续创建？"
        )
        return self.draft_text(data, ask), keyboard(
            (button("▶️ 继续", "m:ro:go"),),
            (button("⬅️ 返回选择抽奖类型", "m:ro:back"),),
            CANCEL_ROW,
        )

    async def ask_report_chat(self, chat_id, data):
        if "report" in data:
            return None
        data["step"] = "report"
        choices = [(button(title[:30], f"m:rc:{chat}"),) for chat, title in data["report_choices"]]
        ask = (
            "选择报道群（你管理、机器人也在的群），或发送报道群的链接，"
            "如 https://t.me/xxx 或 @xxx（只支持公开群）："
        )
        if not choices:
            ask = "还没有你管理、机器人也在的其他群。请发送报道群的链接，如 https://t.me/xxx 或 @xxx（只支持公开群）："
        return self.draft_text(data, ask), keyboard(
            *choices, (button("⬅️ 返回", "m:rc:back"),), CANCEL_ROW
        )

    async def report_typed(self, bot, user_id, chat_id, data, text):
        """A report group typed as a public group's link."""
        match = PUBLIC_GROUP.fullmatch(text.strip())
        if not match:
            if "t.me/+" in text or "joinchat" in text:
                raise LotteryError(
                    "私密群的邀请链接认不出是哪个群，请点上面的按钮选择，或发送公开群的链接。"
                )
            raise LotteryError(
                "请发送公开群的链接，如 https://t.me/xxx 或 @xxx，或点上面的按钮选择。"
            )
        name = match[1] or match[2]
        try:
            chat = await bot.get_chat(f"@{name}")
        except TelegramError:
            raise LotteryError("找不到这个群，请确认链接无误。") from None
        if chat.type not in ("group", "supergroup"):
            raise LotteryError("这不是一个群，请发送群的链接。")
        if chat.id == chat_id:
            raise LotteryError("报道群不能是发布抽奖的这个群，请换一个。")
        try:
            me = await bot.get_chat_member(chat.id, bot.id)
        except TelegramError:
            me = None
        if me is None or me.status not in (ChatMember.ADMINISTRATOR, ChatMember.MEMBER):
            raise LotteryError("机器人还不在这个群里，请先把机器人拉进去并设为管理员。")
        if not await self.can_manage(bot, user_id, chat.id):
            raise LotteryError("只能选你管理的群作报道群。")
        title = chat.title or f"@{name}"
        await asyncio.to_thread(self.store.remember_group, chat.id, title)
        data.update(report=chat.id, report_title=title, report_link=f"https://t.me/{name}")

    async def ask_via(self, chat_id, data):
        if "via" in data:
            return None
        data["step"] = "via"
        ask = (
            "🪁 邀请抽奖\n\n"
            "专属链接邀请：群成员点抽奖卡片上的「🔗 领取我的邀请链接」私聊领取，或在群里发送 "
            "/link，用自己的专属链接拉人进群\n\n"
            "添加成员邀请：群成员用「添加成员」直接拉人进群。群里要允许成员添加成员；"
            "这种方式容易拉小号刷人数\n\n"
            "只算发布抽奖之后第一次进群的新成员；开奖前退群的不算。\n"
            "选择邀请方式："
        )
        return self.draft_text(data, ask), keyboard(
            (button("🔗 专属链接邀请", "m:iv:link"), button("⚠️ 添加成员邀请", "m:iv:add")),
            (button("⬅️ 返回选择抽奖类型", "m:iv:back"),),
            CANCEL_ROW,
        )

    async def ask_invite_kind(self, chat_id, data):
        data["step"] = "ikind"
        ask = "🪁 邀请抽奖：根据邀请排名抽奖，或达到邀请人数参与随机抽奖。\n选择一种："
        return self.draft_text(data, ask), keyboard(
            (button("🏆 邀请排名抽奖", "m:ik:rank"), button("🎯 邀请次数抽奖", "m:ik:reach")),
            (button("⬅️ 返回选择邀请方式", "m:ik:back"),),
            CANCEL_ROW,
        )

    async def ask_activity(self, chat_id, data):
        data["step"] = "activity"
        ask = "🔥 群活跃抽奖：按发言排名，或发言达到次数参与随机抽奖。\n选择一种："
        return self.draft_text(data, ask), keyboard(
            (button("1️⃣ 根据活跃排名抽奖", "m:ka:rank"),),
            (button("2️⃣ 达到发言次数参与随机抽奖", "m:ka:reach"),),
            (button("⬅️ 返回选择抽奖类型", "m:ka:back"),),
            CANCEL_ROW,
        )

    async def ask_prizes(self, chat_id, data):
        prizes = data["prizes"]
        total = sum(count for _, count in prizes)
        if "pending" in data:
            data["step"] = "count"
            choices = [button(str(n), f"m:c:{n}") for n in COUNTS if n <= 100 - total]
            ask = f"「{data['pending']}」有几份？点按钮或直接发送数字："
            return self.draft_text(data, ask), keyboard(choices, CANCEL_ROW)
        if not prizes or data.get("adding"):
            data["step"] = "prize"
            ask = "请发送奖品名称，例如：1USDT" if not prizes else "请发送下一个奖品的名称："
            return self.draft_text(data, ask), keyboard(CANCEL_ROW)
        return None

    async def ask_mode(self, chat_id, data):
        if "mode" in data:
            return None
        data["step"] = "mode"
        ask, back = "怎么开奖？", ()
        if data["kind"] == "points":
            ask = (
                "🪙 积分抽奖：群成员通过签到或发言获得灵石，参与本抽奖会扣除报名所需的灵石。\n"
                "怎么开奖？"
            )
            back = (button("⬅️ 返回选择抽奖类型", "m:mode:back"),)
        return self.draft_text(data, ask), keyboard(
            (button("⏰ 定时开奖", "m:mode:t"), button("👥 满人开奖", "m:mode:f")),
            back,
            CANCEL_ROW,
        )

    async def ask_time(self, chat_id, data):
        if data["mode"] != "t" or "minutes" in data or "deadline" in data:
            return None
        data["step"] = "time"
        now = when_text(self.store.clock(), self.handlers.timezone)
        ask = (
            "什么时候开奖？点按钮，或发送时间，例如 2026-10-05 20:00、90分钟、2小时。\n"
            f"🕒 现在是 {now}"
        )
        choices = [button(label, f"m:t:{m}") for label, m in DURATIONS]
        return self.draft_text(data, ask), keyboard(*in_rows(choices, 3), CANCEL_ROW)

    async def ask_target(self, chat_id, data):
        if data["mode"] != "f" or "target" in data:
            return None
        data["step"] = "target"
        total = max(sum(count for _, count in data["prizes"]), 1)
        choices = [button(str(n), f"m:f:{n}") for n in TARGETS if n >= total]
        ask = f"满多少人开奖？点按钮或直接发送数字（至少 {total}）："
        if data["kind"] == "ireach":
            ask = "满多少人达标就开奖？点按钮或直接发送数字："
        return self.draft_text(data, ask), keyboard(choices, CANCEL_ROW)

    async def ask_join(self, chat_id, data):
        if "join" in data:
            return None
        data["step"] = "join"
        return self.draft_text(data, "怎么参与？"), keyboard(
            (button("🎟 点按钮参与", "m:j:b"), button("💬 发口令参与", "m:j:k")), CANCEL_ROW
        )

    async def ask_keyword(self, chat_id, data):
        if data["join"] != "k" or "keyword" in data:
            return None
        data["step"] = "keyword"
        ask = "请发送参与口令，群友在群里发这句话就能参与，例如：帅哥"
        return self.draft_text(data, ask), keyboard(CANCEL_ROW)

    async def ask_since(self, chat_id, data):
        if "count_from" in data:
            return None
        data["step"] = "since"
        zone = self.handlers.timezone
        since = await asyncio.to_thread(self.store.activity_since, chat_id)
        record = (
            f"本群从 {when_text(since, zone)} 开始记录发言" if since else "本群还没有记录到发言"
        )
        ask = (
            f"发言次数从什么时候开始统计？最早可以选 {LOOK_BACK_DAYS} 天前；"
            "从抽奖发布时开始统计请发送 0。\n"
            "格式：2026-09-30 18:41\n"
            f"📊 {record}，只统计文字消息\n"
            f"🕒 现在是 {when_text(self.store.clock(), zone)}"
        )
        return self.draft_text(data, ask), keyboard(
            (button("▶️ 从发布时开始", "m:fr:0"),), CANCEL_ROW
        )

    async def ask_ranks(self, chat_id, data):
        if data.get("ranks_done"):
            return None
        data["step"] = "rank"
        prizes = data["prizes"]
        ask = f"请发送{place(len(prizes))}的奖品，例如：1USDT"
        done = (button("👉 结束添加奖品，进入下一步", "m:pe"),) if prizes else ()
        return self.draft_text(data, ask), keyboard(done, CANCEL_ROW)

    async def ask_cost(self, chat_id, data):
        if "cost" in data:
            return None
        data["step"] = "cost"
        ask = "参与一次需要多少灵石？点按钮或直接发送数字，群友报名时扣除："
        if not (await asyncio.to_thread(self.store.group_settings, chat_id))["points"]:
            ask = "⚠️ 本群的灵石功能已关闭，群友现在得不到灵石。\n" + ask
        choices = [button(f"{n} 灵石", f"m:co:{n}") for n in COSTS]
        return self.draft_text(data, ask), keyboard(choices, CANCEL_ROW)

    async def ask_minimum(self, chat_id, data):
        if "min_messages" in data:
            return None
        data["step"] = "minimum"
        if data["kind"] == "ireach":
            choices = [button(str(n), f"m:mm:{n}") for n in INVITES_NEEDED]
            ask = "至少邀请多少人才能参与抽奖？点按钮或直接发送数字："
        else:
            choices = [button(str(n), f"m:mm:{n}") for n in MINIMUMS]
            ask = "至少发言多少次才能参与抽奖？点按钮或直接发送数字："
        return self.draft_text(data, ask), keyboard(choices, CANCEL_ROW)

    async def ask_title(self, chat_id, data):
        if "title" in data:
            return None
        data["step"] = "title"
        first = data["prizes"][0][0][:20]
        return self.draft_text(data, "最后，请发送抽奖活动名称："), keyboard(
            (button(f"用「{first}」作名称", "m:tt"),), CANCEL_ROW
        )

    async def ask_confirm(self, user_id, chat_id, data):
        data["step"] = "confirm"
        group = await asyncio.to_thread(self.store.group_title, chat_id)
        more = (button("➕ 添加奖品", "m:more"),) if room_for_prize(data) else ()
        bonus = ()
        if self.is_super(user_id) and data["kind"] in ("join", "points", "rep"):
            # Turning this on shows the bonus notice on the card from the very start, so it
            # does not appear halfway through when the first weight is set.
            bonus = (button(f"⚖️ 中奖加成：{'开' if data.get('weighted') else '关'}", "m:wz"),)
        return self.draft_text(data, f"🎉 已填写完成，发布到「{group}」？"), keyboard(
            (button("✅ 发布抽奖", "m:pub"), button("❌ 取消发布", "m:quit")),
            (*more, button("🔄 换个群", "m:to")),
            bonus,
        )

    def since(self, value):
        """When an activity raffle starts counting messages; 0 for when it is published."""
        if value.strip() == "0":
            return 0
        now = self.store.clock()
        when = parse_time(value, now, self.handlers.timezone)
        if "deadline" not in when:
            raise LotteryError("请按 2026-09-30 18:41 这样输入时间，或发送 0。")
        if not now - LOOK_BACK_DAYS * 86400 <= when["deadline"] <= now + 364 * 86400:
            raise LotteryError(f"统计开始时间需在 {LOOK_BACK_DAYS} 天前到一年之内，请重新输入。")
        return when["deadline"]

    def draft_text(self, data, ask):
        """The wizard header: what is filled in so far, then the question."""
        lines = ["🎁 发起抽奖（/cancel 退出）", ""]
        kind = data.get("kind")
        if data.get("title"):
            lines.append(data["title"])
        if kind == "points":
            cost = f" · 参与需 {data['cost']} 灵石" if "cost" in data else ""
            lines.append(f"├ 类型：积分抽奖{cost}")
        elif kind == "rank":
            lines.append("├ 类型：群活跃抽奖 · 按发言排名")
        elif kind == "reach":
            need = f"发言满 {data['min_messages']} 次" if "min_messages" in data else "发言达到次数"
            lines.append(f"├ 类型：群活跃抽奖 · {need}参与抽奖")
        elif kind == "rep":
            where = f" · 报道群「{data['report_title']}」" if "report" in data else ""
            lines.append(f"├ 类型：指定群报道抽奖{where}")
        elif kind in ("inv", "irank", "ireach") and "via" in data:
            way = f"├ 类型：邀请抽奖 · {INVITE_WAYS[data['via']]}"
            if kind == "irank":
                way += " · 按邀请人数排名"
            elif kind == "ireach":
                need = (
                    f"邀请满 {data['min_messages']} 人"
                    if "min_messages" in data
                    else "邀请达到人数"
                )
                way += f" · {need}参与抽奖"
            lines.append(way)
        if "count_from" in data:
            since = data["count_from"] and when_text(data["count_from"], self.handlers.timezone)
            lines.append(f"├ 统计：从{f' {since} ' if since else '抽奖发布时'}起的文字发言")
        if data["prizes"]:
            lines.append(f"├ 奖品：{prize_text(data['prizes'], INVITE_KINDS.get(kind, kind))}")
        if data.get("target") or data.get("minutes") or data.get("deadline"):
            rule = draw_rule(
                {
                    "deadline": self.deadline_of(data),
                    "target_count": data.get("target"),
                    "invite_via": data.get("via"),
                },
                self.handlers.timezone,
            )
            lines.append(f"├ {rule}")
        if data.get("join") == "b" or data.get("keyword"):
            lines.append(f"├ {join_text(data.get('keyword'))}")
        elif "report" in data:
            lines.append(f"├ {join_text(None, data['report_title'])}")
        if data.get("weighted"):
            lines.append("本场设有中奖加成")
        if len(lines) > 2:
            lines.append("")
        lines.append(ask)
        return "\n".join(lines)

    def deadline_of(self, data):
        if data.get("deadline"):
            return data["deadline"]
        return self.store.clock() + data.get("minutes", FULL_DEADLINE_MINUTES) * 60

    async def way_in(self, bot, chat_id):
        """A link into a report group for the card: its public address, or an invite link
        the bot makes; None if neither can be had."""
        try:
            chat = await bot.get_chat(chat_id)
            if chat.username:
                return f"https://t.me/{chat.username}"
            made = await bot.create_chat_invite_link(chat_id, name="指定群报道抽奖")
        except TelegramError as exc:
            LOG.warning("没能拿到报道群 %s 的进群链接：%s", chat_id, exc)
            return None
        return made.invite_link

    async def target_groups(self, bot, user_id, current):
        rows = [
            (button(("✅ " if chat == current else "") + title[:30], f"m:to:{chat}"),)
            for chat, title in await self.my_groups(bot, user_id)
        ]
        return "发布到哪个群？", keyboard(*rows, (button("⬅️ 返回", "m:cur"),))

    async def publish(self, bot, user_id, chat_id, data):
        await self.require(bot, user_id, chat_id)
        if data.get("deadline") and data["deadline"] < self.store.clock() + 60:
            # The date typed has come while the draft waited: ask when to draw again.
            data.pop("deadline")
            data.pop("minutes", None)
            text, markup = await self.advance(user_id, chat_id, data)
            return "⚠️ 填写的开奖时间已经过了，请重新填写。\n\n" + text, markup
        prizes = data["prizes"]
        kind = data.get("kind", "join")
        if kind in ("join", "points"):
            options = {
                "target": data.get("target"),
                "weighted": bool(data.get("weighted")) and self.is_super(user_id),
                "keyword": data.get("keyword") if data["join"] == "k" else None,
                "cost": data.get("cost"),  # 积分抽奖 is a join raffle that costs 灵石
            }
            minutes = data.get("minutes") or (
                FULL_DEADLINE_MINUTES if data["mode"] == "f" else None
            )
        elif kind == "rep":
            await self.require(bot, user_id, data["report"])
            options = {
                "target": data.get("target"),
                "weighted": bool(data.get("weighted")) and self.is_super(user_id),
                "report_chat": data["report"],
                "report_link": data.get("report_link") or await self.way_in(bot, data["report"]),
            }
            minutes = data.get("minutes") or (
                FULL_DEADLINE_MINUTES if data["mode"] == "f" else None
            )
        elif kind in INVITE_KINDS:
            options = {
                "kind": INVITE_KINDS[kind],
                "invite_via": data["via"],
                "min_messages": data.get("min_messages"),
                "target": data.get("target"),
            }
            minutes = data.get("minutes") or (
                FULL_DEADLINE_MINUTES if data["mode"] == "f" else None
            )
        else:
            options = {
                "kind": kind,
                "count_from": data["count_from"] or None,  # 0: from now on
                "min_messages": data.get("min_messages"),
            }
            minutes = data.get("minutes")
        rid = await asyncio.to_thread(
            functools.partial(
                self.store.create,
                user_id,
                data["title"],
                sum(count for _, count in prizes),
                minutes,
                deadline=data.get("deadline"),
                chat_id=chat_id,
                prizes=prizes,
                **options,
            )
        )
        await asyncio.to_thread(self.store.drop_draft, user_id)
        weights = self.is_super(user_id) and kind in ("join", "points", "rep")
        back = keyboard(
            (button("⚖️ 设置加成", f"m:w:{rid}:0"),) if weights else (),
            (button("📜 抽奖记录", f"m:list:{chat_id}:0"), button("⬅️ 返回", f"m:g:{chat_id}")),
        )
        group = await asyncio.to_thread(self.store.group_title, chat_id)
        try:
            await self.handlers.publish_card(bot, rid)
        except TelegramError as exc:
            LOG.warning("抽奖 %s 发到群 %s 失败：%s", rid, chat_id, exc)
            return (
                f"已创建，但没能发到「{group}」。请确认我在群里并能发言，再到抽奖记录里重新发布。"
            ), back
        return f"✅ 已发布到「{group}」。", back

    # Group settings

    async def settings(self, bot, chat_id):
        title = await asyncio.to_thread(self.store.group_title, chat_id)
        values = await asyncio.to_thread(self.store.group_settings, chat_id)
        lines = [f"⚙️ {title} · 抽奖设置"]
        try:
            me = await bot.get_chat_member(chat_id, bot.id)
        except TelegramError as exc:
            LOG.warning("无法查询机器人在群 %s 的权限：%s", chat_id, exc)
            lines.append("暂时查不到机器人在群里的权限。")
        else:
            admin = me.status == ChatMember.ADMINISTRATOR
            rights = {
                "置顶消息": admin and getattr(me, "can_pin_messages", False),
                "删除消息": admin and getattr(me, "can_delete_messages", False),
                "邀请用户": admin and getattr(me, "can_invite_users", False),
            }
            lines.append(
                "机器人权限：" + " · ".join(f"{k} {'✅' if v else '❌'}" for k, v in rights.items())
            )
            if not all(rights.values()):
                lines.append("缺少的权限请群主在群管理员设置里给机器人打开，否则对应功能不生效。")
        lines += await self.last_change(chat_id, list(SETTINGS))
        rows = [
            (
                button(
                    f"{label}：{setting_text(values[key])}",
                    f"m:{'sv' if choices else 'sk'}:{chat_id}:{key}",
                ),
            )
            for key, (label, choices) in SETTINGS.items()
        ]
        return "\n".join(lines), keyboard(
            *rows,
            (button("📜 修改记录", f"m:log:{chat_id}:set"), button("⬅️ 返回", f"m:g:{chat_id}")),
        )

    async def last_change(self, chat_id, keys):
        """The line that says who last changed one of these settings, if anyone did."""
        found = await asyncio.to_thread(self.store.setting_changes, chat_id, keys, 1)
        return [f"🕘 最近修改：{change_text(found[0], self.handlers.timezone)}"] if found else []

    async def changes(self, chat_id, page):
        """The latest changes to the settings on the 抽奖设置 ("set") or 灵石 ("pt") page."""
        keys = {"set": list(SETTINGS), "pt": list(POINT_KEYS)}[page]
        title = await asyncio.to_thread(self.store.group_title, chat_id)
        found = await asyncio.to_thread(self.store.setting_changes, chat_id, keys, CHANGES_SHOWN)
        name = {"set": "抽奖设置", "pt": "灵石设置"}[page]
        lines = [f"📜 {title} · {name}的修改记录"]
        lines += [change_text(change, self.handlers.timezone) for change in found] or [
            "还没有修改过。"
        ]
        if len(found) == CHANGES_SHOWN:
            lines.append(f"只显示最近 {CHANGES_SHOWN} 条。")
        return "\n".join(lines), keyboard((button("⬅️ 返回", f"m:{page}:{chat_id}"),))

    async def delay_prompt(self, user_id, chat_id, key):
        """Ask how long after posting messages of a kind are deleted."""
        label = SETTINGS[key][0]
        value = (await asyncio.to_thread(self.store.group_settings, chat_id))[key]
        self._asking[user_id] = ("delay", chat_id, key, 0)
        text = (
            f"{label}：{setting_text(value)}\n{DELAY_QUESTIONS[key]}\n"
            f"点按钮，或直接发送分钟数（0～{MAX_DELAY_MINUTES}，0 表示立即删除）："
        )
        choices = [
            button(setting_text(delay), f"m:sd:{chat_id}:{key}:{'n' if delay is None else delay}")
            for delay in DELAYS
        ]
        return text, keyboard(*in_rows(choices, 3), (button("⬅️ 返回", f"m:set:{chat_id}"),))

    async def delay_typed(self, bot, message, user_id, asking):
        _, chat_id, key, _ = asking
        try:
            minutes = number(message.text.strip(), "分钟数", 0, MAX_DELAY_MINUTES)
            await self.set_setting(user_id, chat_id, key, minutes * 60)
        except LotteryError as exc:
            back = keyboard((button("⬅️ 返回", f"m:set:{chat_id}"),))
            await message.reply_text(str(exc), reply_markup=back)
            return
        self._asking.pop(user_id, None)
        text, markup = await self.settings(bot, chat_id)
        await message.reply_text(text, reply_markup=markup)

    async def cycle_setting(self, user_id, chat_id, key):
        choices = SETTINGS[key][1]
        current = (await asyncio.to_thread(self.store.group_settings, chat_id))[key]
        after = (
            choices[(choices.index(current) + 1) % len(choices)]
            if current in choices
            else choices[0]
        )
        await self.set_setting(user_id, chat_id, key, after)

    # 灵石 settings, for group admins (route() checks): how 灵石 are earned, and what counts
    # as a message for them and for activity raffles.

    async def points_action(self, bot, user_id, action, chat_id, args):
        if action == "pk":
            return await self.point_prompt(user_id, chat_id, args[0])
        if action == "pp":
            try:
                pinned = await self.handlers.post_panel(bot, chat_id)
            except TelegramError as exc:
                raise LotteryError(f"发布失败：{exc}") from None
            text, markup = await self.points_page(bot, user_id, chat_id)
            if pinned:
                return "✅ 灵石面板已发到群里并置顶。\n\n" + text, markup
            done = "✅ 灵石面板已发到群里，但没能置顶：请给机器人打开「置顶消息」权限。"
            return done + "\n\n" + text, markup
        if action == "pv":
            key = args[0]
            if key == "points":
                value = bool(int(args[1]))
            elif key in POINT_SETTINGS:
                value = int(args[1])
            else:
                raise ValueError(key)
            await self.set_setting(user_id, chat_id, key, value)
        return await self.points_page(bot, user_id, chat_id)

    async def points_page(self, bot, user_id, chat_id):
        title = await asyncio.to_thread(self.store.group_title, chat_id)
        v = await asyncio.to_thread(self.store.group_settings, chat_id)
        lines = [f"💎 {title} · 灵石设置"]
        if v["points"]:
            checkin = f"每天 {v['checkin_points']} 灵石" if v["checkin_points"] else "关闭"
            reward, crit = "关闭", "关闭"
            if v["reward_points"]:
                daily = f"每天最多 {v['reward_daily']} 次" if v["reward_daily"] else "每天不限次数"
                reward = (
                    f"每条有效发言有 {v['reward_chance']}% 的概率得 {v['reward_points']} 灵石，"
                    f"{daily}"
                )
            if v["reward_points"] and v["crit_percent"]:
                crit = f"得到发言奖励时有 {v['crit_percent']}% 的概率翻 {v['crit_times']} 倍"
            lines += [
                (
                    "群友在群里发送「签到」「灵石」「灵石榜」使用，机器人的回复按抽奖设置里"
                    "「删除机器人通知」的时间删除。"
                ),
                "人多的群建议点「📌 发布灵石面板」：群友点按钮签到、查询，结果只弹给自己看。",
                f"📅 签到：{checkin}",
                f"💬 发言奖励：{reward}",
                f"⚡ 暴击：{crit}",
            ]
        else:
            lines.append("灵石功能已关闭：群友发送「签到」等不会有回应，发言也不奖励灵石。")
        gap = f"，距上一条有效发言 {v['cooldown']} 秒以上" if v["cooldown"] else ""
        lines.append(f"✍️ 有效发言：至少 {v['min_chars']} 个字{gap}。群活跃抽奖也只统计有效发言。")
        try:
            me = await bot.get_chat_member(chat_id, bot.id)
        except TelegramError as exc:
            LOG.warning("无法查询机器人在群 %s 的权限：%s", chat_id, exc)
        else:
            mute = me.status == ChatMember.ADMINISTRATOR and getattr(
                me, "can_restrict_members", False
            )
            lines.append(f"机器人权限：封禁用户 {'✅' if mute else '❌'}（签到刷屏时禁言）")
            if not mute:
                lines.append("没有这个权限时，刷屏只警告、不禁言。")
        lines += await self.last_change(chat_id, list(POINT_KEYS))
        keys = list(POINT_SETTINGS) if v["points"] else ["min_chars", "cooldown"]
        choices = [
            button(f"{POINT_SETTINGS[key][0]}：{point_value(key, v[key])}", f"m:pk:{chat_id}:{key}")
            for key in keys
        ]
        rows = [
            (
                button(
                    f"💎 灵石功能：{'开' if v['points'] else '关'}",
                    f"m:pv:{chat_id}:points:{int(not v['points'])}",
                ),
            ),
            *in_rows(choices, 2),
        ]
        if v["points"]:
            rows.append((button("📌 发布灵石面板", f"m:pp:{chat_id}"),))
        if self.is_super(user_id):
            rows.append(
                (
                    button("✏️ 修改余额", f"m:pb:{chat_id}:0"),
                    button("📄 导出流水", f"m:px:{chat_id}"),
                )
            )
        rows.append(
            (button("📜 修改记录", f"m:log:{chat_id}:pt"), button("⬅️ 返回", f"m:g:{chat_id}"))
        )
        return "\n".join(lines), keyboard(*rows)

    async def point_prompt(self, user_id, chat_id, key):
        label, _, presets, question = POINT_SETTINGS[key]
        low, high = SETTING_LIMITS[key]
        value = (await asyncio.to_thread(self.store.group_settings, chat_id))[key]
        self._asking[user_id] = ("setting", chat_id, key, 0)
        text = (
            f"{label}：{point_value(key, value)}\n{question}\n"
            f"点按钮或直接发送数字（{low}～{high}）："
        )
        choices = [button(point_value(key, n), f"m:pv:{chat_id}:{key}:{n}") for n in presets]
        return text, keyboard(choices, (button("⬅️ 返回", f"m:pt:{chat_id}"),))

    # 灵石 balances, for super admins only (route() checks).

    async def balance_action(self, bot, user_id, action, chat_id, args):
        if action == "px":
            exported = await asyncio.to_thread(self.store.export_points, chat_id)
            document, filename = points_file(exported)
            await bot.send_document(
                user_id, document=document, filename=filename, caption="本群的灵石余额和全部流水。"
            )
            return await self.points_page(bot, user_id, chat_id)
        page = int(args[-1])
        if action == "pb":
            return await self.balances(chat_id, page)
        if action == "pf":
            self._asking[user_id] = ("balance_find", chat_id, None, page)
            back = keyboard((button("⬅️ 返回", f"m:pb:{chat_id}:{page}"),))
            return "发送要找的人的名字，写其中几个字就行：", back
        if action == "pid":
            self._asking[user_id] = ("balance_id", chat_id, None, page)
            back = keyboard((button("⬅️ 返回", f"m:pb:{chat_id}:{page}"),))
            return (
                "发送用户 ID 和要加减的灵石，用空格分开，例如：123456789 +50 或 123456789 -20",
                back,
            )
        uid = int(args[0])
        if action == "pa" and args[1] == "x":
            self._asking[user_id] = ("balance", chat_id, uid, page)
            back = keyboard((button("⬅️ 返回", f"m:pu:{chat_id}:{uid}:{page}"),))
            return "发送要加减的灵石，例如 +50 或 -20：", back
        if action == "pa":
            await asyncio.to_thread(self.store.adjust_points, chat_id, user_id, uid, int(args[1]))
        return await self.balance_page(chat_id, uid, page)

    async def balances(self, chat_id, page):
        title = await asyncio.to_thread(self.store.group_title, chat_id)
        rows, more = await asyncio.to_thread(self.store.holders, chat_id, page, PAGE_SIZE)
        lines = [
            f"✏️ 修改余额 · {title}",
            (
                "点成员加减灵石；列表里没有的人可以按名字查找，也可以在群里回复他的消息，"
                "发「加灵石 50」或「扣灵石 20」。每次修改都记入流水。"
            ),
        ]
        if not rows:
            lines.append("还没有人有灵石。")
        people = [
            (
                button(
                    f"{rank}. {holder_name(row)[:16]} · {row['balance']}",
                    f"m:pu:{chat_id}:{row['user_id']}:{page}",
                ),
            )
            for rank, row in enumerate(rows, page * PAGE_SIZE + 1)
        ]
        nav = []
        if page:
            nav.append(button("◂ 上一页", f"m:pb:{chat_id}:{page - 1}"))
        if more:
            nav.append(button("下一页 ▸", f"m:pb:{chat_id}:{page + 1}"))
        return "\n".join(lines), keyboard(
            *people,
            nav,
            (
                button("🔍 按名字查找", f"m:pf:{chat_id}:{page}"),
                button("➕ 按用户 ID 修改", f"m:pid:{chat_id}:{page}"),
            ),
            (button("⬅️ 返回", f"m:pt:{chat_id}"),),
        )

    async def found_members(self, chat_id, text, page):
        """The members whose name has `text` in it, to pick one whose 灵石 to change."""
        rows, more = await asyncio.to_thread(self.store.find_members, chat_id, text)
        back = (button("⬅️ 返回", f"m:pb:{chat_id}:{page}"),)
        if not rows:
            return (
                f"没找到名字里有「{text}」的人，换几个字再发一次。\n"
                "机器人只认识在群里发过言或有灵石的人；找不到的话，可以在群里回复他的消息，"
                "发「加灵石 50」或「扣灵石 20」。"
            ), keyboard(back)
        people = [
            (
                button(
                    f"{holder_name(row)[:16]} · {row['balance']} 灵石",
                    f"m:pu:{chat_id}:{row['user_id']}:{page}",
                ),
            )
            for row in rows
        ]
        lines = [f"🔍 名字里有「{text}」的人，点一个加减灵石："]
        if more:
            lines.append(f"只列出前 {len(rows)} 个，多写几个字可以缩小范围。")
        lines.append("也可以接着发别的名字。")
        return "\n".join(lines), keyboard(*people, back)

    async def balance_page(self, chat_id, uid, page):
        """One member's 灵石, with buttons to add or take away."""
        row = await asyncio.to_thread(self.store.holder, chat_id, uid)
        name = name_text(row["display_name"]) or f"用户 {uid}"
        choices = [button(f"{n:+d}", f"m:pa:{chat_id}:{uid}:{n}:{page}") for n in BALANCE_STEPS]
        return f"{name}（ID：{uid}）\n💎 {row['balance']} 灵石", keyboard(
            *in_rows(choices, 3),
            (button("✏️ 输入数量", f"m:pa:{chat_id}:{uid}:x:{page}"),),
            (button("⬅️ 返回", f"m:pb:{chat_id}:{page}"),),
        )

    async def points_typed(self, bot, message, user_id, asking):
        kind, chat_id, key, page = asking
        parts = message.text.split()
        if kind == "balance_find":  # still asking, so another name can be sent
            text, markup = await self.found_members(chat_id, message.text.strip()[:64], page)
            await message.reply_text(text, reply_markup=markup)
            return
        try:
            if kind == "setting":
                name = POINT_SETTINGS[key][0].split(" ", 1)[1]
                value = number(message.text.strip(), name, *SETTING_LIMITS[key])
                await self.set_setting(user_id, chat_id, key, value)
            else:
                if kind == "balance":
                    uid, amount = key, message.text.strip()
                elif len(parts) != 2 or not parts[0].isdigit():
                    raise LotteryError(
                        "请发送用户 ID 和要加减的灵石，用空格分开，例如：123456789 +50"
                    )
                else:
                    uid, amount = integer(int(parts[0]), "用户 ID", 1, 2**63 - 1), parts[1]
                delta = number(amount, "加减的数量", -1_000_000_000, 1_000_000_000)
                await asyncio.to_thread(self.store.adjust_points, chat_id, user_id, uid, delta)
        except LotteryError as exc:
            back = f"m:pt:{chat_id}" if kind == "setting" else f"m:pb:{chat_id}:{page}"
            await message.reply_text(str(exc), reply_markup=keyboard((button("⬅️ 返回", back),)))
            return
        self._asking.pop(user_id, None)
        if kind == "setting":
            text, markup = await self.points_page(bot, user_id, chat_id)
        else:
            text, markup = await self.balance_page(chat_id, uid, page)
        await message.reply_text(text, reply_markup=markup)

    # Records

    async def records(self, chat_id, page):
        raffles, more = await asyncio.to_thread(self.store.group_raffles, chat_id, page, PAGE_SIZE)
        rows = [
            (button(f"{r['title'][:20]} · {status_text(r)}", f"m:r:{r['id']}"),) for r in raffles
        ]
        nav = []
        if page:
            nav.append(button("◂ 上一页", f"m:list:{chat_id}:{page - 1}"))
        if more:
            nav.append(button("下一页 ▸", f"m:list:{chat_id}:{page + 1}"))
        text = "📜 抽奖记录" if raffles else "还没有抽奖。"
        return text, keyboard(*rows, nav, (button("⬅️ 返回", f"m:g:{chat_id}"),))

    async def managed_raffle(self, bot, user_id, rid):
        raffle = await asyncio.to_thread(self.store.view, rid)
        await self.require(bot, user_id, raffle["chat_id"])
        return raffle

    async def detail(self, bot, user_id, rid):
        raffle = await self.managed_raffle(bot, user_id, rid)
        kind = raffle["kind"]
        zone = self.handlers.timezone
        if kind == "join":
            lines = [
                f"{raffle['title']}  #{rid} · {status_text(raffle)}",
                f"{raffle['winner_count']} 人中奖 · 已参与 {len(raffle['entries'])} 人",
                draw_rule(raffle, zone),
                join_text(raffle["keyword"], raffle.get("report_title")),
            ]
            if raffle["cost"]:
                lines.append(cost_text(raffle["cost"]))
        else:
            via = raffle["invite_via"]
            counted = "已达标" if kind == "reach" else "有邀请" if via else "已发言"
            lines = [
                f"{raffle['title']}  #{rid} · {status_text(raffle)}",
                activity_rule(kind, raffle["winner_count"], raffle["min_messages"], via),
                f"{raffle['winner_count']} 人中奖 · {counted} {len(raffle['entries'])} 人",
                counting_text(raffle["count_from"], zone, via),
                draw_rule(raffle, zone),
            ]
        if raffle["prizes"]:
            lines.insert(1, f"🏆 {prize_text(raffle['prizes'], kind)}")
        if raffle["weighted"]:
            lines.append("本场设有中奖加成")
        if raffle["result"]:
            # A hundred winners with long names would pass Telegram's limit for a message.
            room = MESSAGE_LIMIT - utf16_len("\n".join(lines)) - 1
            lines.append(winners_line(raffle["result"]["winners"], room))
        rows = []
        if raffle["status"] == "OPEN":
            rows += [
                (
                    button("🎲 立即开奖", f"m:ask:draw:{rid}"),
                    button("⏹ 截止报名", f"m:ask:freeze:{rid}"),
                ),
                (
                    button("✖ 取消抽奖", f"m:ask:cancel:{rid}"),
                    button("📣 重新发布", f"m:repost:{rid}"),
                ),
            ]
        elif raffle["status"] == "FROZEN":
            rows.append(
                (
                    button("🎲 立即开奖", f"m:ask:draw:{rid}"),
                    button("✖ 取消抽奖", f"m:ask:cancel:{rid}"),
                )
            )
        if self.is_super(user_id) and kind == "join":
            rows.append((button("⚖️ 中奖加成", f"m:w:{rid}:0"),))
        elif self.is_super(user_id):
            export = button("📄 导出记录", f"m:export:{rid}:0")
            if raffle["status"] == "OPEN" and not raffle["invite_via"]:
                rows.append((button("✏️ 修改发言次数", f"m:ac:{rid}:0"), export))
            else:
                rows.append((export,))
        rows.append((button("⬅️ 返回", f"m:list:{raffle['chat_id']}:0"),))
        return "\n".join(lines), keyboard(*rows)

    async def confirm(self, bot, user_id, action, rid):
        raffle = await self.managed_raffle(bot, user_id, rid)
        return f"{raffle['title']}  #{rid}\n{CONFIRM[action]}", keyboard(
            (button("✅ 确定", f"m:do:{action}:{rid}"), button("⬅️ 返回", f"m:r:{rid}"))
        )

    async def act(self, bot, user_id, action, rid):
        raffle = await self.managed_raffle(bot, user_id, rid)
        chat_id = raffle["chat_id"]
        if action == "freeze":
            await asyncio.to_thread(self.store.freeze, rid, user_id)
            await self.handlers.refresh_card(bot, rid)
        elif action == "draw":
            if raffle["status"] == "DRAWN":
                raise LotteryError("本场抽奖已开奖。")
            await asyncio.to_thread(self.store.freeze, rid, user_id)
            await asyncio.to_thread(self.store.draw, rid, user_id)
            await self.handlers.announce(bot, rid, chat_id)
        elif action == "cancel":
            if await asyncio.to_thread(self.store.cancel, rid, user_id):
                await self.handlers.refresh_card(bot, rid)
                text = f"「{raffle['title']}」抽奖已取消。"
                if raffle["cost"] and raffle["entries"]:
                    text += "报名扣除的灵石已全部退还。"
                try:
                    sent = await bot.send_message(chat_id, text)
                except TelegramError as exc:
                    LOG.warning("抽奖 %s 的取消通知发送失败：%s", rid, exc)
                else:
                    self.handlers.spent(chat_id)
                    await self.handlers.tidy(bot, chat_id, [sent.message_id], "delete_notices")
                if raffle["card_message_id"] is not None:
                    await self.handlers.restore_panel(bot, chat_id)
        else:
            raise ValueError(action)
        return await self.detail(bot, user_id, rid)

    async def repost(self, bot, user_id, rid):
        raffle = await self.managed_raffle(bot, user_id, rid)
        if raffle["status"] != "OPEN":
            raise LotteryError("只有报名中的抽奖可以重新发布。")
        try:
            await self.handlers.publish_card(bot, rid)
        except TelegramError as exc:
            raise LotteryError(f"发布失败：{exc}") from None
        return await self.detail(bot, user_id, rid)

    # Weights, for super admins only (route() checks). Participants never see any of this;
    # the group card only says whether the raffle is weighted.

    async def weight_action(self, bot, user_id, action, args):
        rid = int(args[0])
        if action == "w":
            return await self.weights(rid, int(args[1]))
        if action == "export":
            exported = await asyncio.to_thread(self.store.export, rid)
            document, filename = export_file(exported)
            await bot.send_document(
                user_id, document=document, filename=filename, caption=EXPORT_CAPTION
            )
            if exported["raffle"]["kind"] != "join":  # exported from the records page
                return await self.detail(bot, user_id, rid)
            return await self.weights(rid, int(args[1]))
        raffle = self.weighs(await asyncio.to_thread(self.store.view, rid))
        if raffle["status"] != "OPEN":
            raise LotteryError(f"{status_text(raffle)}，权重已锁定。")
        page = int(args[-1])
        back = keyboard((button("⬅️ 返回", f"m:w:{rid}:{page}"),))
        if action == "wid":
            self._asking[user_id] = ("preset", rid, None, page)
            return "发送用户 ID 和权重，用空格分开，例如：123456789 5", back
        if action == "wc":
            self._asking[user_id] = ("config", rid, None, page)
            return (
                f"当前默认权重 {raffle['default_weight']}，上限 {raffle['weight_cap']}。\n"
                "发送新的默认权重和上限，用空格分开，例如：1 100"
            ), back
        uid = int(args[1])
        if action == "wu":
            return await self.person(raffle, uid, page)
        if action == "wd":
            entry = next((e for e in raffle["entries"] if e["user_id"] == uid), None)
            chosen = not (entry and entry["designated"])
            await asyncio.to_thread(self.store.designate, rid, user_id, uid, chosen)
            return await self.person(await asyncio.to_thread(self.store.view, rid), uid, page)
        value = args[2]
        if value == "x":
            self._asking[user_id] = ("weight", rid, uid, page)
            back = keyboard((button("⬅️ 返回", f"m:wu:{rid}:{uid}:{page}"),))
            return f"请输入权重（0～{raffle['weight_cap']}），0 表示不参与抽取。", back
        weight = None if value == "a" else int(value)
        await asyncio.to_thread(self.store.override, rid, user_id, uid, weight)
        await self.weights_changed(bot, raffle)
        return await self.weights(rid, page)

    async def weights_changed(self, bot, before):
        """The first bonus or personal weight adds the notice to the group card."""
        if not before["weighted"]:
            await self.handlers.refresh_card(bot, before["id"])

    @staticmethod
    def weighs(raffle):
        if raffle["kind"] != "join":
            raise LotteryError("群活跃抽奖按发言次数决定，不能设置权重或指定获奖。")
        return raffle

    async def weights(self, rid, page):
        raffle = self.weighs(await asyncio.to_thread(self.store.view, rid))
        entries = raffle["entries"]
        odds = chances(entries, raffle["winner_count"])
        adjusted = sum(1 for e in entries if e["override"] is not None or e["tags"])
        designated = sum(1 for e in entries if e.get("designated"))
        lines = [
            f"⚖️ 中奖加成 · {raffle['title']}  #{rid}",
            f"默认权重 {raffle['default_weight']} · 上限 {raffle['weight_cap']}",
            f"已参与 {len(entries)} 人 · 已调整 {adjusted} 人"
            + (f" · 指定 {designated} 人" if designated else ""),
        ]
        people = []
        for e in entries:
            name = name_text(e["display_name"])[:16]
            if e.get("designated"):
                people.append((e["user_id"], f"{name} · 🎯 指定获奖"))
            else:
                chance = odds[e["user_id"]]
                people.append((e["user_id"], f"{name} · 权重 {e['weight']} · 概率 {chance}"))
        editable = raffle["status"] == "OPEN"
        if editable:
            presets = await asyncio.to_thread(self.store.presets, rid)
            people += [(uid, f"ID {uid} · 权重 {weight}（未报名）") for uid, weight in presets]
        shown = people[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
        rows = []
        if not people:
            lines.append("还没有人报名。")
        if editable:
            rows += [(button(label, f"m:wu:{rid}:{uid}:{page}"),) for uid, label in shown]
        else:
            lines.append(f"{status_text(raffle)}，权重已锁定。")
            lines += [label for _, label in shown]
        nav = []
        if page:
            nav.append(button("◂ 上一页", f"m:w:{rid}:{page - 1}"))
        if len(people) > (page + 1) * PAGE_SIZE:
            nav.append(button("下一页 ▸", f"m:w:{rid}:{page + 1}"))
        rows.append(nav)
        if editable:
            rows.append(
                (
                    button("➕ 按用户 ID 设置", f"m:wid:{rid}:{page}"),
                    button("⚙️ 默认与上限", f"m:wc:{rid}:{page}"),
                )
            )
        rows.append(
            (button("📄 导出记录", f"m:export:{rid}:{page}"), button("⬅️ 返回", f"m:r:{rid}"))
        )
        return "\n".join(lines), keyboard(*rows)

    async def person(self, raffle, uid, page):
        rid = raffle["id"]
        odds = chances(raffle["entries"], raffle["winner_count"])
        entry = next((e for e in raffle["entries"] if e["user_id"] == uid), None)
        designate = ()
        if entry:
            if entry["override"] is not None:
                source = "单独设置"
            else:
                source = "、".join(f"{t['tag']} +{t['bonus']}" for t in entry["tags"]) or "默认"
            text = (
                f"{name_text(entry['display_name'])}（ID：{uid}）\n"
                f"权重 {entry['weight']} · 概率 {odds[uid]} · {source}"
            )
            if entry["designated"]:
                text = f"{name_text(entry['display_name'])}（ID：{uid}）\n🎯 已指定获奖，开奖时直接中奖"
            label = "↩️ 取消指定" if entry["designated"] else "🎯 指定获奖"
            designate = (button(label, f"m:wd:{rid}:{uid}:{page}"),)
            overridden = entry["override"] is not None
        else:
            preset = dict(await asyncio.to_thread(self.store.presets, rid)).get(uid)
            overridden = preset is not None
            text = f"用户 {uid}（未报名）\n" + (
                f"预设权重 {preset}，报名后生效" if overridden else "没有预设权重"
            )
        choices = [
            button("0 排除" if w == 0 else str(w), f"m:ws:{rid}:{uid}:{w}:{page}")
            for w in WEIGHTS
            if w <= raffle["weight_cap"]
        ]
        choices.append(button("其他", f"m:ws:{rid}:{uid}:x:{page}"))
        return text, keyboard(
            *in_rows(choices, 4),
            (button("↩️ 恢复默认", f"m:ws:{rid}:{uid}:a:{page}"),) if overridden else (),
            designate,
            (button("⬅️ 返回", f"m:w:{rid}:{page}"),),
        )

    # Message counts of activity raffles, for super admins (route() checks): corrections
    # for messages the bot missed, say while its network was down.

    async def count_action(self, user_id, action, args):
        rid, page = int(args[0]), int(args[-1])
        if action == "ac":
            return await self.counts(rid, page)
        if action == "aid":
            await asyncio.to_thread(self.store.ranking, rid)  # raises once counting is over
            self._asking[user_id] = ("count_id", rid, None, page)
            back = keyboard((button("⬅️ 返回", f"m:ac:{rid}:{page}"),))
            return "发送用户 ID 和正确的发言次数，用空格分开，例如：123456789 30", back
        uid = int(args[1])
        if action == "as" and args[2] == "x":
            counted, _ = await asyncio.to_thread(self.store.counted, rid, uid)
            self._asking[user_id] = ("count", rid, uid, page)
            back = keyboard((button("⬅️ 返回", f"m:au:{rid}:{uid}:{page}"),))
            return f"请发送正确的发言次数（机器人记录 {counted} 次）：", back
        if action == "as":
            by = int(args[2])
            await asyncio.to_thread(functools.partial(self.store.adjust, rid, user_id, uid, by=by))
        return await self.speaker(rid, uid, page)

    async def counts(self, rid, page):
        raffle, ranked = await asyncio.to_thread(self.store.ranking, rid)
        adjusted = sum(1 for e in ranked if e.get("adjusted"))
        lines = [
            f"✏️ 发言次数 · {raffle['title']}  #{rid}",
            f"已发言 {len(ranked)} 人 · 已修改 {adjusted} 人",
            (
                "机器人漏记时（比如网络中断），可以在这里补上。"
                "修改加在机器人统计的次数上，之后的发言照常累计。"
            ),
        ]
        if not ranked:
            lines.append("还没有人发言。")
        rows = []
        first = page * PAGE_SIZE
        for rank, e in enumerate(ranked[first : first + PAGE_SIZE], first + 1):
            label = f"{rank}. {name_text(e['display_name'])[:16]}"
            label += f" · {e['messages']} 次"
            if e.get("adjusted"):
                label += f"（手动 {e['adjusted']:+d}）"
            rows.append((button(label, f"m:au:{rid}:{e['user_id']}:{page}"),))
        nav = []
        if page:
            nav.append(button("◂ 上一页", f"m:ac:{rid}:{page - 1}"))
        if len(ranked) > (page + 1) * PAGE_SIZE:
            nav.append(button("下一页 ▸", f"m:ac:{rid}:{page + 1}"))
        return "\n".join(lines), keyboard(
            *rows,
            nav,
            (button("➕ 按用户 ID 修改", f"m:aid:{rid}:{page}"),),
            (button("⬅️ 返回", f"m:r:{rid}"),),
        )

    async def speaker(self, rid, uid, page):
        """One member's message count, with buttons to correct it."""
        _, ranked = await asyncio.to_thread(self.store.ranking, rid)
        counted, delta = await asyncio.to_thread(self.store.counted, rid, uid)
        entry = next((e for e in ranked if e["user_id"] == uid), None)
        name = name_text(entry["display_name"]) if entry else f"用户 {uid}"
        count = f"发言 {counted + delta} 次 · 机器人记录 {counted} 次"
        if delta:
            count += f" · 手动 {delta:+d}"
        lines = [f"{name}（ID：{uid}）", count]
        if entry:
            lines.append(f"当前第 {ranked.index(entry) + 1} 名")
        choices = [button(f"{n:+d}", f"m:as:{rid}:{uid}:{n}:{page}") for n in CORRECTIONS]
        return "\n".join(lines), keyboard(
            *in_rows(choices, 3),
            (button("✏️ 改为…", f"m:as:{rid}:{uid}:x:{page}"),),
            (button("↩️ 清除修改", f"m:as:{rid}:{uid}:{-delta}:{page}"),) if delta else (),
            (button("⬅️ 返回", f"m:ac:{rid}:{page}"),),
        )

    async def weight_typed(self, bot, message, user_id, asking):
        kind, rid, uid, page = asking
        parts = message.text.split()
        try:
            raffle = await asyncio.to_thread(self.store.view, rid)
            cap = raffle["weight_cap"]
            if kind == "weight":
                weight = number(message.text.strip(), "权重", 0, cap)
                await asyncio.to_thread(self.store.override, rid, user_id, uid, weight)
            elif kind == "preset":
                if len(parts) != 2 or not parts[0].isdigit():
                    raise LotteryError("请发送用户 ID 和权重，用空格分开，例如：123456789 5")
                weight = number(parts[1], "权重", 0, cap)
                target = integer(int(parts[0]), "用户 ID", 1, 2**63 - 1)
                await asyncio.to_thread(self.store.override, rid, user_id, target, weight)
            elif kind == "config":
                if len(parts) != 2:
                    raise LotteryError("请发送默认权重和上限，用空格分开，例如：1 100")
                default = number(parts[0], "默认权重", 0, 1_000_000)
                cap = number(parts[1], "权重上限", 1, 1_000_000)
                await asyncio.to_thread(self.store.configure, rid, user_id, default, cap)
            else:  # a message count, for a member picked from the list or by user ID
                if kind == "count":
                    total = number(message.text.strip(), "发言次数", 0, 1_000_000)
                elif len(parts) != 2 or not parts[0].isdigit():
                    raise LotteryError("请发送用户 ID 和发言次数，用空格分开，例如：123456789 30")
                else:
                    uid = integer(int(parts[0]), "用户 ID", 1, 2**63 - 1)
                    total = number(parts[1], "发言次数", 0, 1_000_000)
                adjust = functools.partial(self.store.adjust, rid, user_id, uid, to=total)
                await asyncio.to_thread(adjust)
        except LotteryError as exc:
            counting = kind in ("count", "count_id")
            back = keyboard((button("⬅️ 返回", f"m:{'ac' if counting else 'w'}:{rid}:{page}"),))
            await message.reply_text(str(exc), reply_markup=back)
            return
        self._asking.pop(user_id, None)
        if kind in ("count", "count_id"):
            text, markup = await self.speaker(rid, uid, page)
        else:
            if kind != "config":
                await self.weights_changed(bot, raffle)
            text, markup = await self.weights(rid, page)
        await message.reply_text(text, reply_markup=markup)

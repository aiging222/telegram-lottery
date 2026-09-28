"""Button menus in private chat. Group admins create and manage their group's raffles;
the super admins listed in ADMIN_USER_IDS may manage every group and alone see and set
the weights."""

import asyncio
import logging
import re
import time
from datetime import datetime

from telegram import ChatMember, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, TelegramError

from lottery.core import LotteryError, integer
from lottery.views import EXPORT_CAPTION, draw_rule, export_file, name_text, percent, status_text

LOG = logging.getLogger(__name__)
MANAGERS = {ChatMember.OWNER, ChatMember.ADMINISTRATOR}
GONE = {ChatMember.LEFT, ChatMember.BANNED}
ADMIN_CACHE_SECONDS = 60
PAGE_SIZE = 8
FULL_DEADLINE_MINUTES = 7 * 1440  # a raffle that never fills up still ends after a week
COUNTS = (1, 2, 3, 5, 10)
DURATIONS = (("1小时", 60), ("6小时", 360), ("1天", 1440), ("3天", 4320), ("7天", 10080))
TARGETS = (10, 50, 100)
WEIGHTS = (0, 1, 2, 3, 5, 10)
UNITS = {"分钟": 1, "分": 1, "m": 1, "小时": 60, "时": 60, "h": 60, "天": 1440, "d": 1440}
CONFIRM = {
    "draw": "确定现在开奖？",
    "freeze": "确定截止报名？截止后不能再报名。",
    "cancel": "确定取消这场抽奖？取消后不能恢复。",
}
NOT_MANAGER = "只有该群的管理员可以管理抽奖。"
NOT_SUPER = "只有超级管理员可以设置中奖加成。"
EXPIRED = "操作已过期，请重新开始。"


def button(text, data):
    return InlineKeyboardButton(text, callback_data=data)


def keyboard(*rows):
    return InlineKeyboardMarkup([list(row) for row in rows if row])


def in_rows(buttons, width):
    return [buttons[i : i + width] for i in range(0, len(buttons), width)]


CANCEL_ROW = (button("✖ 取消", "m:quit"),)


def parse_deadline(text, now, timezone):
    """'90分钟' / '2小时' / '3天' / '45' (minutes), or 'YYYY-MM-DD HH:MM' / 'MM-DD HH:MM'."""
    text = " ".join(text.replace("：", ":").split())
    match = re.fullmatch(r"(\d{1,6}) ?(分钟|分|小时|时|天|m|h|d)?", text, re.IGNORECASE)
    if match:
        return now + int(match[1]) * UNITS[(match[2] or "分钟").lower()] * 60
    year = datetime.fromtimestamp(now, timezone).year
    for candidate in (text, f"{year}-{text}"):
        try:
            moment = datetime.strptime(candidate, "%Y-%m-%d %H:%M").replace(tzinfo=timezone)
        except ValueError:
            continue
        return moment.timestamp()
    raise LotteryError("看不懂这个时间，请按 2026-10-05 20:00 或 2小时 这样输入。")


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
        # user_id -> (kind, raffle_id, user_id or None, page): a weight page waiting for a
        # typed answer. Any button press or /start drops it; a restart forgets it.
        self._asking = {}

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
        self._managers[(chat_id, user_id)] = (time.monotonic() + ADMIN_CACHE_SECONDS, allowed)
        return allowed

    async def require(self, bot, user_id, chat_id):
        if chat_id is None or not await self.can_manage(bot, user_id, chat_id):
            raise LotteryError(NOT_MANAGER)

    def is_super(self, user_id):
        return user_id in self.handlers.admin_ids

    def require_super(self, user_id):
        if not self.is_super(user_id):
            raise LotteryError(NOT_SUPER)

    # Entry points

    @staticmethod
    def add_button(bot):
        # startgroup opens Telegram's group picker; admin= pre-selects the rights we ask for.
        url = f"https://t.me/{bot.username}?startgroup=menu&admin=delete_messages+pin_messages"
        return InlineKeyboardButton("➕ 添加到群组", url=url)

    async def welcome(self, bot, chat_id, text):
        """Post a message in the group whose button opens that group's menu in private."""
        link = f"https://t.me/{bot.username}?start=g{chat_id}"
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("⚙️ 管理抽奖", url=link)]])
        try:
            await bot.send_message(chat_id, text, reply_markup=markup)
        except TelegramError as exc:
            LOG.warning("群 %s 的欢迎消息发送失败：%s", chat_id, exc)

    async def start(self, update, context):
        message, user, chat = update.effective_message, update.effective_user, update.effective_chat
        if message is None or user is None or user.is_bot:
            return
        if chat.type != "private":
            await asyncio.to_thread(self.store.remember_group, chat.id, chat.title or str(chat.id))
            await self.welcome(context.bot, chat.id, "点击下面按钮管理本群抽奖（仅限群管理员）。")
            return
        self._asking.pop(user.id, None)
        payload = context.args[0] if context.args else ""
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
        if status == ChatMember.ADMINISTRATOR:
            text = "✅ 已就绪。群管理员点击下面按钮发起和管理抽奖。"
        else:
            text = "请把我设为管理员（需要：删除消息、置顶消息），否则无法正常工作。"
        await self.welcome(context.bot, chat.id, text)

    # Button presses

    async def callback(self, update, context):
        query = update.callback_query
        if query is None or query.from_user.is_bot:
            return
        action, *args = query.data.split(":")[1:]
        self._asking.pop(query.from_user.id, None)
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
        if action in ("c", "mode", "t", "f", "pub", "wz"):
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
        if action in ("w", "wu", "ws", "wid", "wc", "export"):
            self.require_super(user_id)
            return await self.weight_action(bot, user_id, action, args)
        raise ValueError(action)

    # Group menu

    async def groups_menu(self, bot, user_id):
        groups = await asyncio.to_thread(self.store.groups)
        mine = [
            (chat, title) for chat, title in groups if await self.can_manage(bot, user_id, chat)
        ]
        if not mine:
            return "还没有你能管理的群。先把我拉进群并设为管理员。", keyboard(
                (self.add_button(bot),)
            )
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
            (button("🔄 切换群", "m:groups"),),
        )

    # Creation wizard. The draft survives restarts; each step either shows buttons or,
    # after "其他", waits for a typed answer (data["step"]).

    async def wizard_start(self, bot, user_id, chat_id):
        await self.require(bot, user_id, chat_id)
        await asyncio.to_thread(self.store.save_draft, user_id, chat_id, {"step": "title"})
        return "① 奖品名称？直接发送文字。", keyboard(CANCEL_ROW)

    async def wizard_quit(self, user_id):
        found = await asyncio.to_thread(self.store.draft, user_id)
        await asyncio.to_thread(self.store.drop_draft, user_id)
        return await self.group_menu(found[0]) if found else ("已取消。", None)

    async def wizard_step(self, bot, user_id, action, value):
        found = await asyncio.to_thread(self.store.draft, user_id)
        if not found:
            raise LotteryError(EXPIRED)
        chat_id, data = found
        if action == "pub":
            return await self.publish(bot, user_id, chat_id, data)
        if action == "wz":
            self.require_super(user_id)
            data["weighted"] = not data.get("weighted")
            await asyncio.to_thread(self.store.save_draft, user_id, chat_id, data)
            return self.next_prompt(data, user_id)
        if value == "x":
            data["step"] = {"c": "count", "t": "time", "f": "target"}[action]
            await asyncio.to_thread(self.store.save_draft, user_id, chat_id, data)
            return self.ask_typed(data), keyboard(CANCEL_ROW)
        if action == "c":
            data["count"] = integer(int(value), "中奖人数", 1, 100)
        elif action == "mode":
            data["mode"] = {"t": "t", "f": "f"}[value]
        elif action == "t":
            data["minutes"] = integer(int(value), "报名时长（分钟）", 1, 525600)
            data.pop("deadline", None)
        else:
            data["target"] = integer(int(value), "满人开奖人数", data["count"], 100_000)
        data["step"] = None
        await asyncio.to_thread(self.store.save_draft, user_id, chat_id, data)
        return self.next_prompt(data, user_id)

    async def text(self, update, context):
        """A typed answer to the current wizard step."""
        message, user = update.effective_message, update.effective_user
        asking = self._asking.get(user.id)
        if asking:
            await self.weight_typed(context.bot, message, user.id, asking)
            return
        found = await asyncio.to_thread(self.store.draft, user.id)
        if not found or not found[1].get("step"):
            await message.reply_text("发送 /start 打开菜单。")
            return
        chat_id, data = found
        try:
            self.typed(data, message.text.strip())
        except LotteryError as exc:
            await message.reply_text(str(exc), reply_markup=keyboard(CANCEL_ROW))
            return
        data["step"] = None
        await asyncio.to_thread(self.store.save_draft, user.id, chat_id, data)
        text, markup = self.next_prompt(data, user.id)
        await message.reply_text(text, reply_markup=markup)

    def typed(self, data, text):
        step = data["step"]
        if step == "title":
            if not 1 <= len(text) <= 160:
                raise LotteryError("奖品名称需为 1～160 字，请重新输入。")
            data["title"] = text
        elif step == "count":
            data["count"] = number(text, "中奖人数", 1, 100)
        elif step == "target":
            data["target"] = number(text, "满人开奖人数", data["count"], 100_000)
        else:
            now = self.store.clock()
            deadline = parse_deadline(text, now, self.handlers.timezone)
            if not now + 60 <= deadline <= now + 525600 * 60:
                raise LotteryError("开奖时间需在 1 分钟到 365 天之后，请重新输入。")
            data["deadline"] = deadline
            data.pop("minutes", None)

    @staticmethod
    def ask_typed(data):
        if data["step"] == "count":
            return "请输入中奖人数（1～100）。"
        if data["step"] == "time":
            return "请输入开奖时间，例如 2026-10-05 20:00，或 90分钟、2小时、3天。"
        return f"请输入满多少人开奖（{data['count']}～100000）。"

    def next_prompt(self, data, user_id):
        if "count" not in data:
            choices = [button(str(n), f"m:c:{n}") for n in COUNTS] + [button("其他", "m:c:x")]
            return "② 中奖人数", keyboard(*in_rows(choices, 3), CANCEL_ROW)
        if "mode" not in data:
            return "③ 开奖方式", keyboard(
                (button("⏰ 定时开奖", "m:mode:t"), button("👥 满人开奖", "m:mode:f")), CANCEL_ROW
            )
        if data["mode"] == "t" and "minutes" not in data and "deadline" not in data:
            choices = [button(label, f"m:t:{m}") for label, m in DURATIONS]
            return "④ 多久后开奖？", keyboard(
                *in_rows([*choices, button("其他", "m:t:x")], 3), CANCEL_ROW
            )
        if data["mode"] == "f" and "target" not in data:
            choices = [button(str(n), f"m:f:{n}") for n in TARGETS if n >= data["count"]]
            return "④ 满多少人开奖？", keyboard(
                *in_rows([*choices, button("其他", "m:f:x")], 4), CANCEL_ROW
            )
        bonus = ()
        if self.is_super(user_id):
            # Turning this on shows the bonus notice on the card from the very start, so it
            # does not appear halfway through when the first weight is set.
            bonus = (button(f"⚖️ 中奖加成：{'开' if data.get('weighted') else '关'}", "m:wz"),)
        return self.summary(data), keyboard((button("✅ 发布到群", "m:pub"),), bonus, CANCEL_ROW)

    def deadline_of(self, data):
        if data.get("deadline"):
            return data["deadline"]
        return self.store.clock() + data.get("minutes", FULL_DEADLINE_MINUTES) * 60

    def summary(self, data):
        rule = draw_rule(
            {"deadline": self.deadline_of(data), "target_count": data.get("target")},
            self.handlers.timezone,
        )
        bonus = "\n本场设有中奖加成" if data.get("weighted") else ""
        return f"{data['title']}\n{data['count']} 人中奖 · {rule}{bonus}\n确认后发布到群。"

    async def publish(self, bot, user_id, chat_id, data):
        await self.require(bot, user_id, chat_id)
        if "title" not in data or "count" not in data:
            raise LotteryError(EXPIRED)
        rid = await asyncio.to_thread(
            self.store.create,
            user_id,
            data["title"],
            data["count"],
            data.get("minutes") or (FULL_DEADLINE_MINUTES if data.get("mode") == "f" else None),
            deadline=data.get("deadline"),
            chat_id=chat_id,
            target=data.get("target"),
            weighted=bool(data.get("weighted")) and self.is_super(user_id),
        )
        await asyncio.to_thread(self.store.drop_draft, user_id)
        back = keyboard(
            (button("⚖️ 设置加成", f"m:w:{rid}:0"),) if self.is_super(user_id) else (),
            (button("📜 抽奖记录", f"m:list:{chat_id}:0"), button("⬅️ 返回", f"m:g:{chat_id}")),
        )
        try:
            await self.handlers.publish_card(bot, rid)
        except TelegramError as exc:
            LOG.warning("抽奖 %s 发到群 %s 失败：%s", rid, chat_id, exc)
            return "已创建，但没能发到群里。请确认我在群里并能发言，再到抽奖记录里重新发布。", back
        return "✅ 已发布到群。", back

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
        lines = [
            f"{raffle['title']}  #{rid} · {status_text(raffle)}",
            f"{raffle['winner_count']} 人中奖 · 已参与 {len(raffle['entries'])} 人",
            draw_rule(raffle, self.handlers.timezone),
        ]
        if raffle["weighted"]:
            lines.append("本场设有中奖加成")
        if raffle["result"]:
            names = "、".join(name_text(w["display_name"]) for w in raffle["result"]["winners"])
            lines.append(f"中奖：{names or '无'}")
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
        if self.is_super(user_id):
            rows.append((button("⚖️ 中奖加成", f"m:w:{rid}:0"),))
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
                try:
                    await bot.send_message(chat_id, f"「{raffle['title']}」抽奖已取消。")
                except TelegramError as exc:
                    LOG.warning("抽奖 %s 的取消通知发送失败：%s", rid, exc)
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
            return await self.weights(rid, int(args[1]))
        raffle = await asyncio.to_thread(self.store.view, rid)
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

    async def weights(self, rid, page):
        raffle = await asyncio.to_thread(self.store.view, rid)
        entries = raffle["entries"]
        total = sum(e["weight"] for e in entries)
        adjusted = sum(1 for e in entries if e["override"] is not None or e["tags"])
        lines = [
            f"⚖️ 中奖加成 · {raffle['title']}  #{rid}",
            f"默认权重 {raffle['default_weight']} · 上限 {raffle['weight_cap']}",
            f"已参与 {len(entries)} 人 · 已调整 {adjusted} 人",
        ]
        people = []
        for e in entries:
            chance = percent(e["weight"], total)
            people.append(
                (e["user_id"], f"{name_text(e['display_name'])[:16]} · {e['weight']} · {chance}")
            )
        editable = raffle["status"] == "OPEN"
        if editable:
            presets = await asyncio.to_thread(self.store.presets, rid)
            people += [(uid, f"ID {uid} · {weight}（未报名）") for uid, weight in presets]
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
        total = sum(e["weight"] for e in raffle["entries"])
        entry = next((e for e in raffle["entries"] if e["user_id"] == uid), None)
        if entry:
            if entry["override"] is not None:
                source = "单独设置"
            else:
                source = "、".join(f"{t['tag']} +{t['bonus']}" for t in entry["tags"]) or "默认"
            text = (
                f"{name_text(entry['display_name'])}（ID：{uid}）\n"
                f"权重 {entry['weight']} · 首轮 {percent(entry['weight'], total)} · {source}"
            )
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
            (button("⬅️ 返回", f"m:w:{rid}:{page}"),),
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
            else:
                if len(parts) != 2:
                    raise LotteryError("请发送默认权重和上限，用空格分开，例如：1 100")
                default = number(parts[0], "默认权重", 0, 1_000_000)
                cap = number(parts[1], "权重上限", 1, 1_000_000)
                await asyncio.to_thread(self.store.configure, rid, user_id, default, cap)
        except LotteryError as exc:
            back = keyboard((button("⬅️ 返回", f"m:w:{rid}:{page}"),))
            await message.reply_text(str(exc), reply_markup=back)
            return
        self._asking.pop(user_id, None)
        if kind != "config":
            await self.weights_changed(bot, raffle)
        text, markup = await self.weights(rid, page)
        await message.reply_text(text, reply_markup=markup)

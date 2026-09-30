"""Telegram command interface. Configuration is loaded only at startup."""

import asyncio
import logging
import os
import weakref
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv
from telegram import BotCommand, BotCommandScopeAllGroupChats, ChatMember, Update
from telegram.error import BadRequest, ChatMigrated, Forbidden, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from lottery.core import MAX_KEYWORD, LotteryError, NotEnoughPoints, Store, integer
from lottery.menu import MANAGERS, Menu, one_at_a_time, presser, sender
from lottery.points import ADJUST, WORDS, Points
from lottery.views import (
    EXPORT_CAPTION,
    card,
    chances,
    chunks,
    export_file,
    joined_text,
    name_text,
    points_panel,
    result_text,
    standing_text,
    status_text,
)

LOG = logging.getLogger(__name__)
MEMBER_STATUSES = {ChatMember.OWNER, ChatMember.ADMINISTRATOR, ChatMember.MEMBER}
DEFAULT_TIMEZONE = "Asia/Shanghai"
# chat_member carries leave/kick events and my_chat_member tells the bot it joined a group;
# Telegram sends chat_member only to bots that are group admins.
ALLOWED_UPDATES = ["message", "callback_query", "chat_member", "my_chat_member"]
AUTO_DRAW_SECONDS = 30  # how often due raffles are drawn and old messages deleted
CARD_REFRESH_SECONDS = 5  # joins arriving within this window share one card edit
SAVE_ACTIVITY_SECONDS = 10  # message counts wait in memory at most this long
RANKING_SECONDS = 30  # how long a ranking behind the card's 📊 button is shown again
# Telegram lets a bot send about 20 messages a minute to one group. The bot keeps count and,
# past GROUP_BUDGET in the last minute, answers 灵石 with a reaction instead of a message,
# leaving room for raffle cards and results.
GROUP_BUDGET = 15
# Deletions due sooner than this are timed to the second instead of waiting for the next pass.
QUICK_DELETE_SECONDS = 60
JOINED_REACTION = "🎉"  # a keyword join is confirmed quietly, with a reaction
ADMIN_COMMANDS = {
    "new",
    "config",
    "rule",
    "rules",
    "grant",
    "revoke",
    "weight",
    "preview",
    "export",
    "freeze",
    "draw",
    "publish",
    "raffles",
}
PRIVATE_COMMANDS = ADMIN_COMMANDS - {"publish", "draw"}
USAGE = {
    "new": "/new 中奖名额 报名分钟数 标题",
    "config": "/config 抽奖ID 默认权重 权重上限",
    "rule": "/rule 抽奖ID 规则名 加成",
    "grant": "/grant 抽奖ID 用户ID 规则名",
    "revoke": "/revoke 抽奖ID 用户ID 规则名",
    "weight": "/weight 抽奖ID 用户ID 权重或auto",
    **{
        name: f"/{name} 抽奖ID"
        for name in (
            "preview",
            "export",
            "freeze",
            "draw",
            "publish",
            "raffle",
            "result",
            "rules",
        )
    },
}
COUNTS = {name: 1 for name in USAGE}
COUNTS.update({name: 3 for name in ("config", "rule", "grant", "revoke", "weight")})
PUBLIC_HELP = """🎁 抽奖机器人
群管理员私聊我发送 /start，用按钮发起和管理抽奖。
/raffle 抽奖ID — 在发布群里查看抽奖
/result 抽奖ID — 在发布群里查看开奖结果
/id — 查看自己的用户 ID
/link — 在群里领取自己的专属邀请链接（邀请抽奖用）
在群里发送「签到」领灵石，「灵石」查看自己的灵石，「灵石榜」看排行。"""
ADMIN_HELP = """

超级管理员命令（除发布和开奖外均在私聊使用）。
权重也可以用按钮设置：/start → 我的群 → 抽奖记录 → 某场抽奖 → ⚖️ 中奖加成。

/new 3 60 周末抽奖 — 创建活动，60 分钟后截止
/config 1 1 100 — 默认权重 1，上限 100
/rule 1 vip 2 — vip 规则加成 2
/grant 1 123456789 vip — 授予用户 vip 条件
/revoke 1 123456789 vip — 撤销条件
/weight 1 123456789 10 — 覆盖该用户权重为 10
/weight 1 123456789 0 — 排除该用户
/weight 1 123456789 auto — 恢复规则计算
/rules 1 — 查看权重规则（不对群友公开）
/preview 1 — 预览名单及首轮概率
/export 1 — 导出完整 JSON 及修改记录
/publish 1 — 在目标群发布报名卡片，首次发布即绑定该群，仅限群成员报名
（机器人需为群管理员，才能可靠查询成员身份并收到退群通知）
/freeze 1 — 提前截止报名，不可撤销
/draw 1 — 报名截止后立即开奖，并在当前聊天公布
（到截止时间会自动开奖并公布到发布群，不必手动执行）
/raffles — 最近 20 场抽奖

示例里的 1 是抽奖 ID，创建后请替换为实际 ID。"""


@dataclass(frozen=True)
class Settings:
    token: str
    admin_ids: frozenset[int]
    database_path: str
    timezone: ZoneInfo

    @classmethod
    def from_env(cls):
        load_dotenv(Path.cwd() / ".env")
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not token or token == "replace_with_botfather_token" or ":" not in token:
            raise LotteryError("请先在 .env 中设置 TELEGRAM_BOT_TOKEN。")
        raw = os.environ.get("ADMIN_USER_IDS", "")
        try:
            admins = frozenset(int(value.strip()) for value in raw.split(",") if value.strip())
            if not admins:
                raise ValueError
            for uid in admins:
                integer(uid, "管理员 ID", 1, 2**63 - 1)
        except ValueError:
            raise LotteryError("ADMIN_USER_IDS 必须为逗号分隔的正整数用户 ID。") from None
        zone = os.environ.get("TIMEZONE", "").strip() or DEFAULT_TIMEZONE
        try:
            timezone = ZoneInfo(zone)
        except (ZoneInfoNotFoundError, ValueError):
            raise LotteryError(
                f"TIMEZONE 无效：{zone}。请填写 IANA 时区名，例如 Asia/Shanghai。"
            ) from None
        return cls(token, admins, os.environ.get("DATABASE_PATH", "data/lottery.sqlite3"), timezone)


def asks_bot(message):
    """Whether a message asks the bot for something, so that a failure to handle it is
    worth an answer: anything in private; in a group a command, a 灵石 word or a super
    admin's 「加灵石」. Other group messages are read only to be counted, and an error
    there must not answer someone's chat."""
    if message.chat.type == "private":
        return True
    text = (message.text or "").strip()
    return text.startswith("/") or text in WORDS or ADJUST.fullmatch(text) is not None


def in_group(member):
    # Restricted users may still be in the group; only ChatMemberRestricted has is_member.
    return member.status in MEMBER_STATUSES or getattr(member, "is_member", False)


async def reply(message, text, markup=None, html=False):
    """Reply in as many messages as needed, the markup on the last; returns them all."""
    parts = chunks(text)
    return [
        await message.reply_text(
            part,
            reply_markup=markup if index == len(parts) - 1 else None,
            parse_mode="HTML" if html else None,
        )
        for index, part in enumerate(parts)
    ]


class BotHandlers:
    def __init__(self, store, admin_ids, timezone):
        self.store = store
        self.admin_ids = admin_ids
        self.timezone = timezone
        # The deadline job runs alongside updates, so a join that fills a raffle, the menu's
        # draw button or /draw in the group can reach the same result at the same moment.
        # Posting it happens under this lock, and whoever comes second finds it announced.
        self._announcing = asyncio.Lock()
        # raffle ID -> message IDs of the parts of a long result already posted, so a retry
        # carries on from the part that failed. A restart in between posts it all again.
        self._posted_parts = {}
        # user ID -> the lock that keeps that person's updates in order; see one_at_a_time.
        self._user_locks = weakref.WeakValueDictionary()
        # raffle ID -> (until when, raffle, ranking) for the card's 📊 button; see ranking().
        self._rankings = {}
        # chat ID -> when the bot sent its messages there in the last minute; see room().
        self._sent = {}
        self._deleting = set()  # deletions waiting a few seconds; see tidy()
        self._ranking = asyncio.Lock()

    def user_lock(self, user_id):
        lock = self._user_locks.get(user_id)
        if lock is None:
            lock = self._user_locks[user_id] = asyncio.Lock()
        return lock

    @one_at_a_time(sender)
    async def command(self, update, context):
        message, user = update.effective_message, update.effective_user
        if message is None or user is None or user.is_bot or message.sender_chat:
            return
        command = message.text.split()[0][1:].split("@")[0].lower()
        args = context.args
        chat = update.effective_chat
        if chat.type in ("group", "supergroup"):
            # Groups that had the bot before menus existed show up in "我的群" this way.
            await asyncio.to_thread(
                self.store.remember_group, chat.id, getattr(chat, "title", None) or str(chat.id)
            )
        if command in ADMIN_COMMANDS and user.id not in self.admin_ids:
            await self.notice(context.bot, message, "此命令仅限配置的机器人管理员使用。")
            return
        if command in PRIVATE_COMMANDS and chat.type != "private":
            await self.notice(context.bot, message, "请私聊机器人执行此命令。")
            return
        try:
            if command == "help":
                await reply(
                    message, PUBLIC_HELP + (ADMIN_HELP if user.id in self.admin_ids else "")
                )
                return
            if command == "id":
                await reply(message, f"你的 Telegram 用户 ID：{user.id}")
                return
            if command == "raffles":
                rows = await asyncio.to_thread(self.store.recent)
                await reply(
                    message,
                    "\n".join(f"{r['id']}｜{r['title']}｜{status_text(r)}" for r in rows)
                    or "暂无抽奖。",
                )
                return
            if command == "new":
                if len(args) < 3:
                    raise LotteryError("用法：" + USAGE[command])
                rid = await asyncio.to_thread(
                    self.store.create, user.id, " ".join(args[2:]), int(args[0]), int(args[1])
                )
                await reply(message, f"已创建抽奖 {rid}。配置完成后，在目标群发送 /publish {rid}。")
                return
            if len(args) != COUNTS[command]:
                raise LotteryError("用法：" + USAGE[command])
            rid = integer(int(args[0]), "抽奖 ID", 1, 2**63 - 1)
            if command == "config":
                changed = await asyncio.to_thread(
                    self.store.configure, rid, user.id, int(args[1]), int(args[2])
                )
            elif command == "rule":
                changed = await asyncio.to_thread(
                    self.store.rule, rid, user.id, args[1], int(args[2])
                )
            elif command in ("grant", "revoke"):
                changed = await asyncio.to_thread(
                    self.store.grant, rid, user.id, int(args[1]), args[2], command == "grant"
                )
                if not changed:
                    await reply(
                        message,
                        "该用户已有此条件，未做修改。"
                        if command == "grant"
                        else "该用户没有此条件，未做修改。",
                    )
                    return
            elif command == "weight":
                value = None if args[2].lower() == "auto" else int(args[2])
                changed = await asyncio.to_thread(
                    self.store.override, rid, user.id, int(args[1]), value
                )
            elif command == "freeze":
                frozen = await asyncio.to_thread(self.store.freeze, rid, user.id)
                if frozen["status"] == "DRAWN":
                    text = f"抽奖 {rid} 已开奖，可使用 /result {rid} 查看已保存结果。"
                elif frozen["chat_id"] is not None:
                    text = (
                        f"抽奖 {rid} 报名已截止，到原定截止时间会自动开奖并公布到发布群；"
                        f"也可以现在用 /draw {rid} 立即开奖。"
                    )
                else:
                    text = f"抽奖 {rid} 报名已截止。它还没有发布到群，不会自动开奖，请用 /draw {rid} 开奖。"
                await reply(message, text)
                return
            elif command == "draw":
                async with self._announcing:
                    result = await asyncio.to_thread(self.store.draw, rid, user.id)
                    await self.post_result(context.bot, message, rid, result, user.id)
                return
            elif command == "export":
                exported = await asyncio.to_thread(self.store.export, rid)
                document, filename = export_file(exported)
                await message.reply_document(
                    document=document, filename=filename, caption=EXPORT_CAPTION
                )
                return
            else:
                if command == "publish" and chat.type != "private":
                    await asyncio.to_thread(self.store.bind, rid, user.id, chat.id)
                raffle = await asyncio.to_thread(self.store.view, rid)
                if (
                    command in ("raffle", "result")
                    and raffle["chat_id"] != chat.id
                    and user.id not in self.admin_ids
                ):
                    # IDs count up, so anyone could otherwise read every group's winners.
                    raise LotteryError("请在发布这场抽奖的群里查询。")
                if command == "publish" and raffle["result"]:
                    async with self._announcing:
                        await self.post_result(context.bot, message, rid, raffle["result"], user.id)
                elif command in ("publish", "raffle"):
                    if raffle["result"]:
                        await reply(message, result_text(raffle["result"], mention=True), html=True)
                    else:
                        text, markup = card(raffle, self.timezone)
                        sent = await reply(message, text, markup)
                        if command == "publish" and raffle["chat_id"] == chat.id:
                            await self.card_posted(context.bot, raffle, sent[-1].message_id)
                elif command == "result":
                    if raffle["result"]:
                        await reply(message, result_text(raffle["result"], mention=True), html=True)
                    else:
                        await reply(message, "尚未开奖，到开奖时间会自动公布。")
                elif command == "rules":
                    await reply(
                        message,
                        f"默认权重 {raffle['default_weight']}，上限 {raffle['weight_cap']}。\n"
                        "个人覆盖值优先，规则加成由管理员核验后授予。\n"
                        + (
                            "\n".join(f"{r['tag']}：+{r['bonus']}" for r in raffle["rules"])
                            or "暂无规则。"
                        ),
                    )
                elif command == "preview" and raffle["invite_via"]:
                    lines = [f"抽奖 {rid}｜{status_text(raffle)}｜按邀请人数"]
                    lines += [
                        f"{name_text(p['display_name'])} / {p['user_id']}：邀请 {p['invites']} 人"
                        for p in raffle["entries"][:30]
                    ]
                elif command == "preview" and raffle["kind"] != "join":
                    lines = [f"抽奖 {rid}｜{status_text(raffle)}｜按发言次数"]
                    for p in raffle["entries"][:30]:
                        correction = f"（手动 {p['adjusted']:+d}）" if p.get("adjusted") else ""
                        lines.append(
                            f"{name_text(p['display_name'])} / {p['user_id']}："
                            f"发言 {p['messages']} 次{correction}"
                        )
                elif command == "preview":
                    odds = chances(raffle["entries"], raffle["winner_count"])
                    total = sum(p["weight"] for p in raffle["entries"])
                    lines = [f"抽奖 {rid}｜{status_text(raffle)}｜总权重 {total}"]
                    for p in raffle["entries"][:30]:
                        source = "个人覆盖" if p["override"] is not None else "规则"
                        lines.append(
                            f"{name_text(p['display_name'])} / {p['user_id']}："
                            f"权重 {p['weight']}（{source}），首轮 {odds[p['user_id']]}"
                        )
                if command == "preview":
                    lines.append("展示前 30 人。完整名单和记录使用 /export。")
                    await reply(message, "\n".join(lines))
                return
            if not changed:
                await reply(message, "与现有设置相同，未做修改。")
                return
            # A first bonus or personal weight must show up on the group card.
            self.refresh_card_soon(context, rid)
            await reply(message, f"抽奖 {rid} 的配置已保存，可用 /preview {rid} 查看。")
        except LotteryError as exc:
            await self.notice(context.bot, message, str(exc))
        except (ValueError, OverflowError):
            await self.notice(
                context.bot, message, "数字参数格式错误。用法：" + USAGE.get(command, "/help")
            )

    @one_at_a_time(presser)
    async def callback(self, update, context):
        query = update.callback_query
        if query is None or query.from_user.is_bot:
            return
        url = None
        try:
            action, raw_id = query.data.split(":", 1)
            rid = integer(int(raw_id), "抽奖 ID", 1, 2**63 - 1)
            if action == "join":
                chat_id = await asyncio.to_thread(self.store.target_chat, rid)
                try:
                    member = await self.group_member(context.bot, chat_id, query.from_user.id)
                except TelegramError as exc:
                    # Fail closed: without a definite answer nobody is let in.
                    LOG.warning("群成员校验失败：%s", exc)
                    raise LotteryError("暂时无法确认你的群成员身份，请稍后重试。") from None
                if not member:
                    raise LotteryError("仅限发布群的成员报名，请先加入该群。")
                report = await asyncio.to_thread(self.store.report_group, rid)
                if report is not None:
                    try:
                        there = await self.group_member(context.bot, report, query.from_user.id)
                    except TelegramError as exc:
                        LOG.warning("报道群成员校验失败：%s", exc)
                        raise LotteryError("暂时无法确认你在不在报道群，请稍后重试。") from None
                    if not there:
                        title = await asyncio.to_thread(self.store.group_title, report)
                        raise LotteryError(f"请先加入「{title}」，加入后会自动报名。")
                added = await asyncio.to_thread(
                    self.store.join, rid, query.from_user.id, name_text(query.from_user.full_name)
                )
                text = joined_text(added)
                if added:
                    await self.after_join(context, rid)
            elif action == "rank":
                raffle, ranked = await self.ranking(rid)
                text = standing_text(raffle, ranked, query.from_user.id)
            elif action == "invite":
                raffle = await asyncio.to_thread(self.store.view, rid)
                if raffle["status"] != "OPEN" or not raffle["invite_via"]:
                    raise LotteryError("统计已截止，结果以开奖公告为准。")
                # A t.me link to the bot, opened by the member's app: a private chat with it
                # that hands them their link (see Menu.start).
                url = f"https://t.me/{context.bot.username}?start=inv{raffle['chat_id']}"
            else:
                # Cards published before weights became private still carry this button.
                text = "该功能已下线。"
        except (LotteryError, ValueError, OverflowError) as exc:
            text = str(exc) if isinstance(exc, LotteryError) else "无效的抽奖按钮。"
        try:
            if url:
                await query.answer(url=url)
            else:
                await query.answer(text, show_alert=True)
        except TelegramError as exc:
            # Clicks replayed after downtime are too old to answer. Any join above is
            # already saved, so there is nothing to retry and nothing to tell the group.
            LOG.warning("按钮应答失败：%s", exc)

    def room(self, chat_id):
        """Whether the bot may still send chat_id a message this minute; see GROUP_BUDGET."""
        now = self.store.clock()
        sent = [at for at in self._sent.get(chat_id, ()) if now - at < 60]
        self._sent[chat_id] = sent
        return len(sent) < GROUP_BUDGET

    def spent(self, chat_id, count=1):
        """Count messages just sent to chat_id against its budget."""
        self._sent.setdefault(chat_id, []).extend([self.store.clock()] * count)

    async def ranking(self, rid):
        """An open activity raffle and its ranking, for the card's 📊 button. Working it out
        reads every message counted, a second or so in a big group, and many may press at
        once: whoever presses within RANKING_SECONDS of it being worked out gets the same one,
        and only one is worked out at a time."""
        async with self._ranking:
            now = self.store.clock()
            found = self._rankings.get(rid)
            if found is None or found[0] <= now or found[1]["deadline"] <= now:
                raffle, ranked = await asyncio.to_thread(self.store.ranking, rid)
                self._rankings = {k: v for k, v in self._rankings.items() if v[0] > now}
                found = self._rankings[rid] = (now + RANKING_SECONDS, raffle, ranked)
        return found[1], found[2]

    @one_at_a_time(sender)
    async def keyword(self, update, context):
        """Join by sending a raffle's keyword in its group. Whoever writes in the group is a
        member, so no membership lookup is needed; admins must not post anonymously. Joins
        that cost 灵石 are answered with what was paid, or why it could not be."""
        message, user = update.effective_message, update.effective_user
        if message is None or user is None or user.is_bot or message.sender_chat:
            return
        text = message.text.strip()
        if len(text) > MAX_KEYWORD:
            return
        rids = await asyncio.to_thread(self.store.keyword_raffles, message.chat.id, text)
        if not rids:
            return
        joined = False
        notes = []
        for rid in rids:
            try:
                added = await asyncio.to_thread(
                    self.store.join, rid, user.id, name_text(user.full_name)
                )
            except NotEnoughPoints as exc:
                notes.append(str(exc))
                continue
            except LotteryError:
                continue  # full or just closed
            if added:
                joined = True
                if added["paid"]:
                    notes.append(joined_text(added))
                await self.after_join(context, rid)
        settings = await asyncio.to_thread(self.store.group_settings, message.chat.id)
        if joined and settings["delete_keyword"] != 0:
            try:
                await message.set_reaction(JOINED_REACTION)
            except TelegramError as exc:
                LOG.info("报名成功的表情回应失败：%s", exc)
        if notes:
            sent = await reply(message, "\n".join(notes))
            self.spent(message.chat.id, len(sent))
            ids = [part.message_id for part in sent]
            await self.tidy(context.bot, message.chat.id, ids, "delete_notices")
        await self.tidy(context.bot, message.chat.id, [message.message_id], "delete_keyword")

    async def save_activity(self, _context):
        """Save the message counts kept in memory; also run when the bot stops."""
        await asyncio.to_thread(self.store.flush_activity)

    @one_at_a_time(lambda update: update.chat_member.new_chat_member.user)
    async def member_changed(self, update, context):
        change = update.chat_member
        member = change.new_chat_member
        if MANAGERS & {change.old_chat_member.status, member.status}:
            # Promotions and demotions come here too; "我的群" follows them.
            await asyncio.to_thread(
                self.store.set_manager, change.chat.id, member.user.id, member.status in MANAGERS
            )
        if in_group(member):
            if not in_group(change.old_chat_member):
                await self.member_joined(context, change)
            return
        # Judge by when the member left, not when the update arrives: after downtime a
        # leave from before the deadline still cancels the join if the list is not frozen.
        left = await asyncio.to_thread(
            self.store.leave_group,
            change.chat.id,
            change.new_chat_member.user.id,
            change.date.timestamp(),
        )
        for rid in left:
            self.refresh_card_soon(context, rid)  # one fewer on the card

    async def member_joined(self, context, change):
        """Record who brought a new member in, for invite raffles: the owner of the invite
        link they came by, or whoever added them. Invite raffles that now have as many
        members with enough invites as they wait for are drawn."""
        user = change.new_chat_member.user
        if user.is_bot:
            return
        chat_id = change.chat.id
        inviter, name, via = None, "", None
        link = change.invite_link
        adder = change.from_user
        if link is not None:
            # Only links the bot made are shown in full, so only those are found.
            owner = await asyncio.to_thread(self.store.link_owner, chat_id, link.invite_link)
            if owner is not None:
                (inviter, name), via = owner, "link"
        elif adder is not None and adder.id != user.id and not adder.is_bot:
            if not change.via_join_request:  # else it is the admin who let them in
                inviter, name, via = adder.id, name_text(adder.full_name), "add"
        counted = await asyncio.to_thread(
            self.store.joined, chat_id, user.id, change.date.timestamp(), inviter, name, via
        )
        if counted:
            for rid in await asyncio.to_thread(self.store.full_invite_raffles, chat_id):
                await self.announce(context.bot, rid, chat_id)
        await self.report_in(context, chat_id, user)

    async def report_in(self, context, chat_id, user):
        """user joined chat_id: join them to the report raffles it is the report group of,
        if they are members of the raffle's own group."""
        for rid, home in await asyncio.to_thread(self.store.report_raffles, chat_id):
            try:
                if not await self.group_member(context.bot, home, user.id):
                    continue
                added = await asyncio.to_thread(
                    self.store.join, rid, user.id, name_text(user.full_name)
                )
            except (TelegramError, LotteryError) as exc:
                # They can still press the card's button; full or just closed are fine.
                LOG.info("报道抽奖 %s 自动报名 %s 未成功：%s", rid, user.id, exc)
                continue
            if added:
                await self.after_join(context, rid)

    async def invite_link_text(self, bot, chat_id, user, check=True):
        """The answer to asking for one's own invite link to chat_id: the link, made once
        and kept, or why there is none. With `check`, only a member of the group gets one."""
        if check:
            try:
                member = await self.group_member(bot, chat_id, user.id)
            except TelegramError as exc:
                LOG.warning("群成员校验失败：%s", exc)
                return "暂时无法确认你的群成员身份，请稍后重试。"
            if not member:
                return "只有群成员才能领取这个群的邀请链接。"
        link = await asyncio.to_thread(self.store.invite_link, chat_id, user.id)
        if link is None:
            name = name_text(user.full_name)
            try:
                made = await bot.create_chat_invite_link(chat_id, name=(name or str(user.id))[:32])
            except TelegramError as exc:
                LOG.warning("群 %s 生成邀请链接失败：%s", chat_id, exc)
                return (
                    "没能生成邀请链接：机器人需要是群管理员并有「邀请用户」权限，请联系群管理员。"
                )
            link = made.invite_link
            await asyncio.to_thread(self.store.save_invite_link, chat_id, user.id, link, name)
        title = await asyncio.to_thread(self.store.group_title, chat_id)
        return (
            f"🔗 你在「{title}」的专属邀请链接：\n{link}\n\n"
            "把它发给好友，好友通过这条链接进群，就算你邀请的。"
        )

    @one_at_a_time(sender)
    async def link(self, update, context):
        """/link in a group: the sender's own invite link, left a minute to be copied."""
        message, user, chat = update.effective_message, update.effective_user, update.effective_chat
        if message is None or user is None or user.is_bot or message.sender_chat:
            return
        if chat.type not in ("group", "supergroup"):
            await reply(message, "请在群里发送 /link，或点抽奖卡片上的「🔗 领取我的邀请链接」。")
            return
        # Whoever writes in the group is in it: no need to ask Telegram.
        text = await self.invite_link_text(context.bot, chat.id, user, check=False)
        await self.notice(context.bot, message, text, at_least=60)

    async def after_join(self, context, rid):
        if await asyncio.to_thread(self.store.is_full, rid):
            await self.auto_draw(context)  # full: draw now rather than on the next pass
        else:
            self.refresh_card_soon(context, rid)

    async def auto_draw(self, context):
        """Draw raffles that are due (deadline passed or full); announce each result once."""
        for rid, chat_id in await asyncio.to_thread(self.store.pending_announcements):
            await self.announce(context.bot, rid, chat_id)

    async def announce(self, bot, rid, chat_id):
        """Draw if needed, post the result to the raffle's group and close its card."""
        async with self._announcing:
            if not await asyncio.to_thread(self.store.announcement_due, rid):
                return
            sent = self._posted_parts.setdefault(rid, [])
            try:
                result = await asyncio.to_thread(self.store.draw, rid, 0)
                for part in chunks(result_text(result, mention=True))[len(sent) :]:
                    message = await bot.send_message(chat_id, part, parse_mode="HTML")
                    sent.append(message.message_id)
                    self.spent(chat_id)
            except ChatMigrated as exc:
                # The group became a supergroup; announce there in full on the next pass.
                del self._posted_parts[rid]
                await self.follow_migration(bot, chat_id, exc.new_chat_id)
                return
            except (Forbidden, BadRequest) as exc:
                # The bot was removed or the chat is gone: stop retrying. The result stays
                # saved and can still be read with /result.
                del self._posted_parts[rid]
                LOG.warning("抽奖 %s 的开奖结果无法发到群 %s：%s", rid, chat_id, exc)
                await asyncio.to_thread(self.store.mark_announced, rid, 0, chat_id, str(exc))
                return
            except TelegramError as exc:
                LOG.warning("抽奖 %s 的开奖公告发送失败，稍后重试：%s", rid, exc)
                return
            del self._posted_parts[rid]
            await asyncio.to_thread(self.store.mark_announced, rid, 0, chat_id)
            await self.refresh_card(bot, rid)
            await self.pin_result(bot, chat_id, sent[0])
            await self.restore_panel(bot, chat_id)

    async def post_result(self, bot, message, rid, result, actor):
        """Reply with a result. Posted in the raffle's own group, it is the announcement
        there, so the deadline job need not send it; callers hold self._announcing."""
        sent = await reply(message, result_text(result, mention=True), html=True)
        chat_id = message.chat.id
        if await asyncio.to_thread(self.store.mark_announced, rid, actor, chat_id):
            await self.refresh_card(bot, rid)
            await self.pin_result(bot, chat_id, sent[0].message_id)
            await self.restore_panel(bot, chat_id)

    async def publish_card(self, bot, rid):
        """Post the card to the raffle's group and remember it for later edits."""
        raffle = await asyncio.to_thread(self.store.view, rid)
        text, markup = card(raffle, self.timezone)
        sent = await bot.send_message(raffle["chat_id"], text, reply_markup=markup)
        self.spent(raffle["chat_id"])
        await self.card_posted(bot, raffle, sent.message_id)

    async def card_posted(self, bot, raffle, message_id):
        """Track the newest card of a raffle and pin it in place of the one before."""
        await asyncio.to_thread(self.store.set_card, raffle["id"], message_id)
        chat_id = raffle["chat_id"]
        if not (await asyncio.to_thread(self.store.group_settings, chat_id))["pin_card"]:
            return
        if raffle["card_message_id"] not in (None, message_id):
            await self.unpin(bot, chat_id, raffle["card_message_id"])
        await self.pin(bot, chat_id, message_id)

    async def pin_result(self, bot, chat_id, message_id):
        """Pin a result announcement; only the group's latest result stays pinned."""
        if not (await asyncio.to_thread(self.store.group_settings, chat_id))["pin_result"]:
            return
        await self.pin(bot, chat_id, message_id)
        before = await asyncio.to_thread(self.store.swap_pinned_result, chat_id, message_id)
        if before is not None and before != message_id:
            await self.unpin(bot, chat_id, before)

    async def pin(self, bot, chat_id, message_id):
        """Pin quietly; whether it worked. Pinning is a courtesy: without the right, the
        raffle works all the same."""
        try:
            await bot.pin_chat_message(chat_id, message_id, disable_notification=True)
        except TelegramError as exc:
            LOG.info("群 %s 置顶消息失败：%s", chat_id, exc)
            return False
        return True

    async def post_panel(self, bot, chat_id, only_pinned=False):
        """Post the group's 灵石 panel and pin it in place of the one before, which is taken
        down; returns whether it was pinned. With only_pinned, a panel that cannot be pinned
        is taken back instead, and the one before stays."""
        text, markup = points_panel()
        sent = await bot.send_message(chat_id, text, reply_markup=markup)
        self.spent(chat_id)
        pinned = await self.pin(bot, chat_id, sent.message_id)
        if only_pinned and not pinned:
            await self.delete(bot, chat_id, [sent.message_id])
            return False
        before = await asyncio.to_thread(self.store.swap_points_panel, chat_id, sent.message_id)
        if before is not None:
            await self.unpin(bot, chat_id, before)
            # Telegram lets bots delete messages for 48 hours; an older panel stays behind,
            # its buttons working all the same.
            await self.delete(bot, chat_id, [before])
        return pinned

    async def restore_panel(self, bot, chat_id):
        """A raffle has ended: put the group's 灵石 panel back on top of its pinned messages,
        which the raffle's card and result pushed it down from. Not while another raffle's
        card is still pinned there: the last to end brings it back."""
        settings = await asyncio.to_thread(self.store.group_settings, chat_id)
        if not settings["points"] or not (settings["pin_card"] or settings["pin_result"]):
            return  # no 灵石, or nothing of the bot's pushed the panel down
        panel = await asyncio.to_thread(self.store.restorable_panel, chat_id, settings["pin_card"])
        if panel is None:
            return
        try:
            await self.post_panel(bot, chat_id, only_pinned=True)
        except TelegramError as exc:
            LOG.warning("群 %s 的灵石面板没能放回置顶：%s", chat_id, exc)

    async def unpin(self, bot, chat_id, message_id):
        try:
            await bot.unpin_chat_message(chat_id, message_id=message_id)
        except TelegramError as exc:
            LOG.info("群 %s 取消置顶失败：%s", chat_id, exc)

    # Tidying up: group messages are deleted now or later, as each group's settings say.

    async def notice(self, bot, message, text, markup=None, at_least=0):
        """Answer a command; in a group both the command and the answer are tidied away, no
        sooner than `at_least` seconds."""
        sent = await reply(message, text, markup)
        if message.chat.type in ("group", "supergroup"):
            self.spent(message.chat.id, len(sent))
            ids = [message.message_id, *(part.message_id for part in sent)]
            await self.tidy(bot, message.chat.id, ids, "delete_notices", at_least)

    async def tidy(self, bot, chat_id, message_ids, setting, at_least=0):
        """Delete messages as the group's `setting` says, but no sooner than `at_least`
        seconds. The deletion is saved, so a restart does not forget it; one due within
        QUICK_DELETE_SECONDS is also timed here, since the pass every 30 seconds would be
        late for it."""
        delay = (await asyncio.to_thread(self.store.group_settings, chat_id))[setting]
        if delay is None:
            return
        delay = max(delay, at_least)
        if delay == 0:
            await self.delete(bot, chat_id, message_ids)
            return
        due = self.store.clock() + delay
        await asyncio.to_thread(self.store.schedule_deletions, chat_id, message_ids, due)
        if delay < QUICK_DELETE_SECONDS:
            task = asyncio.create_task(self._delete_later(bot, chat_id, message_ids, delay))
            self._deleting.add(task)
            task.add_done_callback(self._deleting.discard)

    async def _delete_later(self, bot, chat_id, message_ids, delay):
        await asyncio.sleep(delay)
        if await self.delete(bot, chat_id, message_ids):
            await asyncio.to_thread(self.store.drop_deletions, chat_id, message_ids)

    async def delete(self, bot, chat_id, message_ids):
        """Delete messages; False only when it is worth trying again later."""
        try:
            await bot.delete_messages(chat_id, message_ids)
        except ChatMigrated as exc:
            # Messages left behind in a group upgraded to a supergroup cannot be deleted.
            await self.follow_migration(bot, chat_id, exc.new_chat_id)
        except (Forbidden, BadRequest) as exc:
            # No right to delete, or the messages are gone or too old: give up on them.
            LOG.info("群 %s 删除消息失败：%s", chat_id, exc)
        except TelegramError as exc:
            LOG.warning("群 %s 删除消息失败，稍后重试：%s", chat_id, exc)
            return False
        return True

    async def cleanup(self, context):
        """Delete the group messages whose time has come."""
        for chat_id, ids in (await asyncio.to_thread(self.store.due_deletions)).items():
            if await self.delete(context.bot, chat_id, ids):
                await asyncio.to_thread(self.store.drop_deletions, chat_id, ids)

    async def refresh_card(self, bot, rid):
        """Redraw the group card in place: participant count, status and button."""
        self._rankings.pop(rid, None)  # frozen, drawn or cancelled: no ranking to show
        raffle = await asyncio.to_thread(self.store.view, rid)
        if raffle["card_message_id"] is None or raffle["chat_id"] is None:
            return
        text, markup = card(raffle, self.timezone)
        try:
            await bot.edit_message_text(
                text,
                chat_id=raffle["chat_id"],
                message_id=raffle["card_message_id"],
                reply_markup=markup,
            )
        except TelegramError as exc:
            # Unchanged text, a deleted card or a lost group: the card is cosmetic.
            LOG.info("抽奖 %s 的卡片未更新：%s", rid, exc)
        if raffle["status"] in ("DRAWN", "CANCELLED"):
            settings = await asyncio.to_thread(self.store.group_settings, raffle["chat_id"])
            if settings["pin_card"]:
                await self.unpin(bot, raffle["chat_id"], raffle["card_message_id"])

    def refresh_card_soon(self, context, rid):
        # Joins come in bursts; a single edit a few seconds later covers all of them.
        jobs = getattr(context, "job_queue", None)
        if jobs is None or jobs.get_jobs_by_name(f"card:{rid}"):
            return
        jobs.run_once(self._refresh_job, CARD_REFRESH_SECONDS, data=rid, name=f"card:{rid}")

    async def _refresh_job(self, context):
        await self.refresh_card(context.bot, context.job.data)

    async def on_error(self, update, context):
        # TokenFilter, installed by main(), masks the token in the message and traceback.
        LOG.error("处理更新失败。", exc_info=context.error)
        if not isinstance(update, Update):
            return
        try:
            if update.callback_query:
                # effective_message is the shared group card; tell only the person who clicked.
                await update.callback_query.answer("操作未能完成，请重试。", show_alert=True)
            elif update.effective_message and asks_bot(update.effective_message):
                # In a group it is tidied away with the message it answers.
                await self.notice(
                    context.bot,
                    update.effective_message,
                    "操作未能完成，请重试。若开奖已保存，重试会返回相同结果。",
                )
        except Exception:  # reporting an error must not raise another
            LOG.exception("错误提示发送失败。")

    async def migrate(self, update, context):
        # Telegram sends one service message in the old group and one in the new supergroup.
        message = update.effective_message
        if message.migrate_to_chat_id:
            old, new = message.chat.id, message.migrate_to_chat_id
        else:
            old, new = message.migrate_from_chat_id, message.chat.id
        await self.follow_migration(context.bot, old, new)

    async def follow_migration(self, bot, old_chat_id, new_chat_id):
        """Follow a group's upgrade to a supergroup, which changes its chat ID. The cards stay
        behind in the old group, so raffles still open get a new one in the supergroup."""
        left_behind = await asyncio.to_thread(self.store.migrate_chat, old_chat_id, new_chat_id)
        for rid in left_behind:
            if (await asyncio.to_thread(self.store.view, rid))["status"] != "OPEN":
                continue
            try:
                await self.publish_card(bot, rid)
            except TelegramError as exc:
                LOG.warning("抽奖 %s 的卡片未能发到升级后的群 %s：%s", rid, new_chat_id, exc)

    async def group_member(self, bot, chat_id, user_id):
        """Ask Telegram whether user_id is in chat_id now, following a supergroup upgrade."""
        try:
            member = await bot.get_chat_member(chat_id, user_id)
        except ChatMigrated as exc:
            await self.follow_migration(bot, chat_id, exc.new_chat_id)
            member = await bot.get_chat_member(exc.new_chat_id, user_id)
        return in_group(member)


def redact(text, token):
    """Mask the secret half of a bot token; the numeric bot ID before ':' is public."""
    secret = token.partition(":")[2]
    return text.replace(secret, "***") if secret else text


class TokenFilter(logging.Filter):
    """Mask the bot token in every record, including formatted tracebacks."""

    def __init__(self, token):
        super().__init__()
        self.token = token

    def filter(self, record):
        record.msg, record.args = redact(record.getMessage(), self.token), None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text, self.token)
        if record.stack_info:
            record.stack_info = redact(record.stack_info, self.token)
        return True


async def register_commands(app):
    # Everything else is done with buttons; the other commands still work when typed.
    # Groups also list /link, which answers only there.
    commands = [BotCommand("start", "打开菜单"), BotCommand("id", "查看我的用户 ID")]
    await app.bot.set_my_commands(commands)
    await app.bot.set_my_commands(
        [*commands, BotCommand("link", "领取我的专属邀请链接")],
        scope=BotCommandScopeAllGroupChats(),
    )


def build_application(settings):
    handlers = BotHandlers(Store(settings.database_path), settings.admin_ids, settings.timezone)
    app = (
        Application.builder()
        .token(settings.token)
        # Updates run concurrently, so a slow Telegram lookup for one click holds up no one
        # else. Store calls are transactions, results are posted under a lock, and each
        # person's own updates keep their order (one_at_a_time).
        .concurrent_updates(True)
        .post_init(register_commands)
        .post_shutdown(handlers.save_activity)
        .build()
    )
    menu = Menu(handlers)
    commands = sorted(ADMIN_COMMANDS | {"help", "id", "raffle", "result"})
    app.add_handler(CommandHandler("start", menu.start))
    app.add_handler(CommandHandler("cancel", menu.cancel, filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler(commands, handlers.command))
    app.add_handler(CommandHandler("link", handlers.link))
    app.add_handler(
        CallbackQueryHandler(handlers.callback, pattern=r"^(join|rank|weight|invite):[0-9]{1,19}$")
    )
    app.add_handler(CallbackQueryHandler(menu.callback, pattern=r"^m:"))
    points = Points(handlers)
    app.add_handler(CallbackQueryHandler(points.button, pattern=r"^pts:(checkin|wallet|board)$"))
    app.add_handler(MessageHandler(filters.StatusUpdate.MIGRATE, handlers.migrate))
    app.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, menu.text)
    )
    app.add_handler(
        MessageHandler(filters.ChatType.GROUPS & filters.TEXT & ~filters.COMMAND, handlers.keyword)
    )
    # A handler group of its own, so that group text is also counted for activity raffles
    # and 灵石 besides the above. 「/签到」 is no command to Telegram, which allows only
    # Latin letters, digits and underscores in those, so it arrives as text.
    texts = filters.ChatType.GROUPS & (filters.TEXT | filters.CAPTION) & ~filters.COMMAND
    app.add_handler(MessageHandler(texts, points.message), group=1)
    app.add_handler(ChatMemberHandler(handlers.member_changed, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(ChatMemberHandler(menu.bot_membership, ChatMemberHandler.MY_CHAT_MEMBER))
    for job in (handlers.auto_draw, handlers.cleanup):
        app.job_queue.run_repeating(job, interval=AUTO_DRAW_SECONDS, first=AUTO_DRAW_SECONDS)
    app.job_queue.run_once(menu.sync_all_admins, when=0)
    app.job_queue.run_repeating(
        handlers.save_activity, interval=SAVE_ACTIVITY_SECONDS, first=SAVE_ACTIVITY_SECONDS
    )
    app.add_error_handler(handlers.on_error)
    return app


def main():
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    # The HTTP layer logs every polling request; PTB's own warnings stay visible, and
    # TokenFilter masks the token in whatever gets through.
    logging.getLogger("httpx").setLevel(logging.CRITICAL)
    logging.getLogger("httpcore").setLevel(logging.CRITICAL)
    try:
        settings = Settings.from_env()
        for handler in logging.getLogger().handlers:
            handler.addFilter(TokenFilter(settings.token))
        application = build_application(settings)
        print("抽奖机器人正在启动。按 Ctrl+C 停止。", flush=True)
        application.run_polling(allowed_updates=ALLOWED_UPDATES, drop_pending_updates=False)
    except LotteryError as exc:
        raise SystemExit(str(exc)) from None
    except Exception as exc:  # noqa: BLE001 - CLI boundary: keep the cause, mask the token.
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        detail = redact(f"{type(exc).__name__}: {exc}", token)
        raise SystemExit(f"启动或运行失败：{detail}\n请检查令牌、网络代理和数据库路径。") from None


if __name__ == "__main__":
    main()

"""Telegram command interface. Configuration is loaded only at startup."""

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv
from telegram import BotCommand, ChatMember, Update
from telegram.error import BadRequest, ChatMigrated, Forbidden, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from lottery.core import MAX_KEYWORD, LotteryError, Store, integer
from lottery.menu import MANAGERS, Menu
from lottery.views import (
    EXPORT_CAPTION,
    card,
    chunks,
    export_file,
    name_text,
    percent,
    result_text,
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
/id — 查看自己的用户 ID"""
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


async def group_member(bot, store, chat_id, user_id):
    """Ask Telegram whether user_id is in chat_id now, following a supergroup upgrade."""
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except ChatMigrated as exc:
        await asyncio.to_thread(store.migrate_chat, chat_id, exc.new_chat_id)
        member = await bot.get_chat_member(exc.new_chat_id, user_id)
    return in_group(member)


def in_group(member):
    # Restricted users may still be in the group; only ChatMemberRestricted has is_member.
    return member.status in MEMBER_STATUSES or getattr(member, "is_member", False)


async def reply(message, text, markup=None, html=False):
    """Reply in as many messages as needed; returns the last one sent."""
    parts = chunks(text)
    sent = None
    for index, part in enumerate(parts):
        sent = await message.reply_text(
            part,
            reply_markup=markup if index == len(parts) - 1 else None,
            parse_mode="HTML" if html else None,
        )
    return sent


class BotHandlers:
    def __init__(self, store, admin_ids, timezone):
        self.store = store
        self.admin_ids = admin_ids
        self.timezone = timezone
        # The deadline job runs alongside updates, so a join that fills a raffle, the menu's
        # draw button or /draw in the group can reach the same result at the same moment.
        # Posting it happens under this lock, and whoever comes second finds it announced.
        self._announcing = asyncio.Lock()

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
                            await self.card_posted(context.bot, raffle, sent.message_id)
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
                elif command == "preview":
                    total = sum(p["weight"] for p in raffle["entries"])
                    lines = [f"抽奖 {rid}｜{status_text(raffle)}｜总权重 {total}"]
                    for p in raffle["entries"][:30]:
                        source = "个人覆盖" if p["override"] is not None else "规则"
                        lines.append(
                            f"{name_text(p['display_name'])} / {p['user_id']}："
                            f"权重 {p['weight']}（{source}），首轮 {percent(p['weight'], total)}"
                        )
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

    async def callback(self, update, context):
        query = update.callback_query
        if query is None or query.from_user.is_bot:
            return
        try:
            action, raw_id = query.data.split(":", 1)
            rid = integer(int(raw_id), "抽奖 ID", 1, 2**63 - 1)
            if action == "join":
                chat_id = await asyncio.to_thread(self.store.target_chat, rid)
                try:
                    member = await group_member(
                        context.bot, self.store, chat_id, query.from_user.id
                    )
                except TelegramError as exc:
                    # Fail closed: without a definite answer nobody is let in.
                    LOG.warning("群成员校验失败：%s", exc)
                    raise LotteryError("暂时无法确认你的群成员身份，请稍后重试。") from None
                if not member:
                    raise LotteryError("仅限发布群的成员报名，请先加入该群。")
                added = await asyncio.to_thread(
                    self.store.join, rid, query.from_user.id, name_text(query.from_user.full_name)
                )
                text = "报名成功！" if added else "你已报名，无需重复报名。"
                if added:
                    await self.after_join(context, rid)
            else:
                # Cards published before weights became private still carry this button.
                text = "该功能已下线。"
        except (LotteryError, ValueError, OverflowError) as exc:
            text = str(exc) if isinstance(exc, LotteryError) else "无效的抽奖按钮。"
        try:
            await query.answer(text, show_alert=True)
        except TelegramError as exc:
            # Clicks replayed after downtime are too old to answer. Any join above is
            # already saved, so there is nothing to retry and nothing to tell the group.
            LOG.warning("按钮应答失败：%s", exc)

    async def keyword(self, update, context):
        """Join by sending a raffle's keyword in its group. Whoever writes in the group is a
        member, so no membership lookup is needed; admins must not post anonymously."""
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
        for rid in rids:
            try:
                added = await asyncio.to_thread(
                    self.store.join, rid, user.id, name_text(user.full_name)
                )
            except LotteryError:
                continue  # full or just closed
            if added:
                joined = True
                await self.after_join(context, rid)
        settings = await asyncio.to_thread(self.store.group_settings, message.chat.id)
        if joined and settings["delete_keyword"] != 0:
            try:
                await message.set_reaction(JOINED_REACTION)
            except TelegramError as exc:
                LOG.info("报名成功的表情回应失败：%s", exc)
        await self.tidy(context.bot, message.chat.id, [message.message_id], "delete_keyword")

    async def member_changed(self, update, context):
        change = update.chat_member
        member = change.new_chat_member
        if MANAGERS & {change.old_chat_member.status, member.status}:
            # Promotions and demotions come here too; "我的群" follows them.
            await asyncio.to_thread(
                self.store.set_manager, change.chat.id, member.user.id, member.status in MANAGERS
            )
        if in_group(member):
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
            try:
                result = await asyncio.to_thread(self.store.draw, rid, 0)
                sent = [
                    await bot.send_message(chat_id, part, parse_mode="HTML")
                    for part in chunks(result_text(result, mention=True))
                ]
            except ChatMigrated as exc:
                # The group became a supergroup; announce there on the next pass.
                await asyncio.to_thread(self.store.migrate_chat, chat_id, exc.new_chat_id)
                return
            except (Forbidden, BadRequest) as exc:
                # The bot was removed or the chat is gone: stop retrying. The result stays
                # saved and can still be read with /result.
                LOG.warning("抽奖 %s 的开奖结果无法发到群 %s：%s", rid, chat_id, exc)
                await asyncio.to_thread(self.store.mark_announced, rid, 0, chat_id, str(exc))
                return
            except TelegramError as exc:
                LOG.warning("抽奖 %s 的开奖公告发送失败，稍后重试：%s", rid, exc)
                return
            await asyncio.to_thread(self.store.mark_announced, rid, 0, chat_id)
            await self.refresh_card(bot, rid)
            await self.pin_result(bot, chat_id, sent[0].message_id)

    async def post_result(self, bot, message, rid, result, actor):
        """Reply with a result. Posted in the raffle's own group, it is the announcement
        there, so the deadline job need not send it; callers hold self._announcing."""
        sent = await reply(message, result_text(result, mention=True), html=True)
        chat_id = message.chat.id
        if await asyncio.to_thread(self.store.mark_announced, rid, actor, chat_id):
            await self.refresh_card(bot, rid)
            await self.pin_result(bot, chat_id, sent.message_id)

    async def publish_card(self, bot, rid):
        """Post the card to the raffle's group and remember it for later edits."""
        raffle = await asyncio.to_thread(self.store.view, rid)
        text, markup = card(raffle, self.timezone)
        sent = await bot.send_message(raffle["chat_id"], text, reply_markup=markup)
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
        # Pinning is a courtesy: without the right, the raffle works all the same.
        try:
            await bot.pin_chat_message(chat_id, message_id, disable_notification=True)
        except TelegramError as exc:
            LOG.info("群 %s 置顶消息失败：%s", chat_id, exc)

    async def unpin(self, bot, chat_id, message_id):
        try:
            await bot.unpin_chat_message(chat_id, message_id=message_id)
        except TelegramError as exc:
            LOG.info("群 %s 取消置顶失败：%s", chat_id, exc)

    # Tidying up: group messages are deleted now or later, as each group's settings say.

    async def notice(self, bot, message, text, markup=None):
        """Answer a command; in a group both the command and the answer are tidied away."""
        sent = await reply(message, text, markup)
        if message.chat.type in ("group", "supergroup"):
            ids = [message.message_id, sent.message_id]
            await self.tidy(bot, message.chat.id, ids, "delete_notices")

    async def tidy(self, bot, chat_id, message_ids, setting):
        delay = (await asyncio.to_thread(self.store.group_settings, chat_id))[setting]
        if delay is None:
            return
        if delay == 0:
            await self.delete(bot, chat_id, message_ids)
            return
        due = self.store.clock() + delay
        await asyncio.to_thread(self.store.schedule_deletions, chat_id, message_ids, due)

    async def delete(self, bot, chat_id, message_ids):
        """Delete messages; False only when it is worth trying again later."""
        try:
            await bot.delete_messages(chat_id, message_ids)
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

    async def migrate(self, update, context):
        # Telegram sends one service message in the old group and one in the new supergroup.
        message = update.effective_message
        if message.migrate_to_chat_id:
            old, new = message.chat.id, message.migrate_to_chat_id
        else:
            old, new = message.migrate_from_chat_id, message.chat.id
        await asyncio.to_thread(self.store.migrate_chat, old, new)


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


async def on_error(update, context):
    # TokenFilter, installed by main(), masks the token in the message and traceback.
    LOG.error("处理更新失败。", exc_info=context.error)
    if not isinstance(update, Update):
        return
    try:
        if update.callback_query:
            # effective_message is the shared group card; tell only the person who clicked.
            await update.callback_query.answer("操作未能完成，请重试。", show_alert=True)
        elif update.effective_message:
            await reply(
                update.effective_message, "操作未能完成，请重试。若开奖已保存，重试会返回相同结果。"
            )
    except TelegramError:
        LOG.error("错误提示发送失败。")


async def register_commands(app):
    await app.bot.set_my_commands(
        [
            BotCommand("start", "打开菜单"),
            BotCommand("cancel", "退出正在进行的操作"),
            BotCommand("help", "使用说明"),
            BotCommand("raffle", "查看抽奖"),
            BotCommand("result", "查看开奖结果"),
            BotCommand("id", "查看我的用户 ID"),
        ]
    )


def build_application(settings):
    handlers = BotHandlers(Store(settings.database_path), settings.admin_ids, settings.timezone)
    app = (
        Application.builder()
        .token(settings.token)
        .concurrent_updates(False)
        .post_init(register_commands)
        .build()
    )
    menu = Menu(handlers)
    commands = sorted(ADMIN_COMMANDS | {"help", "id", "raffle", "result"})
    app.add_handler(CommandHandler("start", menu.start))
    app.add_handler(CommandHandler("cancel", menu.cancel, filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler(commands, handlers.command))
    app.add_handler(CallbackQueryHandler(handlers.callback, pattern=r"^(join|weight):[0-9]{1,19}$"))
    app.add_handler(CallbackQueryHandler(menu.callback, pattern=r"^m:"))
    app.add_handler(MessageHandler(filters.StatusUpdate.MIGRATE, handlers.migrate))
    app.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, menu.text)
    )
    app.add_handler(
        MessageHandler(filters.ChatType.GROUPS & filters.TEXT & ~filters.COMMAND, handlers.keyword)
    )
    app.add_handler(ChatMemberHandler(handlers.member_changed, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(ChatMemberHandler(menu.bot_membership, ChatMemberHandler.MY_CHAT_MEMBER))
    for job in (handlers.auto_draw, handlers.cleanup):
        app.job_queue.run_repeating(job, interval=AUTO_DRAW_SECONDS, first=AUTO_DRAW_SECONDS)
    app.job_queue.run_once(menu.sync_all_admins, when=0)
    app.add_error_handler(on_error)
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

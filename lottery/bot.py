"""Telegram command interface. Configuration is loaded only at startup."""

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

from dotenv import load_dotenv
from telegram import BotCommand, ChatMember, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import ChatMigrated, TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from lottery.core import LotteryError, Store, integer

LOG = logging.getLogger(__name__)
MEMBER_STATUSES = {ChatMember.OWNER, ChatMember.ADMINISTRATOR, ChatMember.MEMBER}
ADMIN_COMMANDS = {
    "new",
    "config",
    "rule",
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
            "myweight",
            "rules",
        )
    },
}
COUNTS = {name: 1 for name in USAGE}
COUNTS.update({name: 3 for name in ("config", "rule", "grant", "revoke", "weight")})
PUBLIC_HELP = """🎲 加权抽奖机器人
/id — 查看自己的 Telegram 用户 ID
/raffle 抽奖ID — 查看抽奖和报名按钮
/rules 抽奖ID — 查看完整权重规则
/myweight 抽奖ID — 私聊查询自己的权重
/result 抽奖ID — 查询已保存的开奖结果

抽奖按权重随机抽取，每人最多中奖一次。权重为 0 不参与抽取。
仅限发布群的成员报名。规则加成由管理员核验后授予；管理员可在截止前覆盖个人权重。
截止后名单与权重不可修改，由管理员执行开奖。"""
ADMIN_HELP = """

管理员命令（除发布和开奖外均在私聊使用）：
请直接私聊机器人配置权重；如果发到群里，指令文字本身仍会被群成员看到。
/new 3 60 周末抽奖 — 创建活动，60 分钟后截止
/config 1 1 100 — 默认权重 1，上限 100
/rule 1 vip 2 — vip 规则加成 2
/grant 1 123456789 vip — 授予用户 vip 条件
/revoke 1 123456789 vip — 撤销条件
/weight 1 123456789 10 — 覆盖该用户权重为 10
/weight 1 123456789 0 — 排除该用户
/weight 1 123456789 auto — 恢复规则计算
/preview 1 — 预览名单及首轮概率
/export 1 — 导出完整 JSON 及修改记录
/publish 1 — 在目标群发布报名卡片，首次发布即绑定该群，仅限群成员报名
（机器人需为群管理员，才能可靠查询成员身份）
/freeze 1 — 提前截止，冻结后不可撤销
/draw 1 — 截止或冻结后开奖，并在当前聊天公布
/raffles — 最近 20 场抽奖

示例里的 1 是抽奖 ID，创建后请替换为实际 ID。"""


@dataclass(frozen=True)
class Settings:
    token: str
    admin_ids: frozenset[int]
    database_path: str

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
        return cls(token, admins, os.environ.get("DATABASE_PATH", "data/lottery.sqlite3"))


def name_text(text):
    return " ".join(text.split())[:128]


def status_text(raffle):
    return {"OPEN": "报名中", "FROZEN": "已冻结，等待开奖", "DRAWN": "已开奖"}[raffle["status"]]


def card(raffle):
    deadline = datetime.fromtimestamp(raffle["deadline"], timezone(timedelta(hours=8)))
    rules = "、".join(f"{r['tag']} +{r['bonus']}" for r in raffle["rules"][:8]) or "暂无加成规则"
    if len(raffle["rules"]) > 8:
        rules += "（完整规则见 /rules）"
    text = (
        f"🎲 {raffle['title']}\n抽奖 ID：{raffle['id']}\n状态：{status_text(raffle)}\n"
        f"中奖名额：{raffle['winner_count']}\n已报名：{len(raffle['entries'])} 人\n"
        f"截止：{deadline:%Y-%m-%d %H:%M:%S}（UTC+8）\n\n"
        f"默认权重：{raffle['default_weight']}；上限：{raffle['weight_cap']}\n"
        f"规则加成：{rules}\n"
        "规则由管理员核验后授予，个人覆盖值优先；截止前可能调整。\n"
        "权重越高，抽取机会越大；0 权重不参与，每人最多中奖一次。\n"
        "按钮查询的是当前状态，上方报名人数为发布时数据。"
    )
    buttons = []
    if raffle["status"] == "OPEN" and raffle["chat_id"] is not None:
        text += "\n仅限发布群的成员报名。"
        buttons.append([InlineKeyboardButton("🎟 报名", callback_data=f"join:{raffle['id']}")])
    elif raffle["status"] == "OPEN":
        text += f"\n尚未在群里发布：管理员在目标群发送 /publish {raffle['id']} 后开放报名。"
    buttons.append([InlineKeyboardButton("查看我的权重", callback_data=f"weight:{raffle['id']}")])
    return text, InlineKeyboardMarkup(buttons)


def personal_weight(raffle, user_id):
    person = next((p for p in raffle["entries"] if p["user_id"] == user_id), None)
    if person is None:
        return "你尚未报名本场抽奖。"
    total = sum(p["weight"] for p in raffle["entries"])
    chance = person["weight"] / total * 100 if total else 0
    source = "个人覆盖" if person["override"] is not None else "规则计算"
    return (
        f"权重：{person['weight']}（{source}）\n"
        f"首轮概率：{chance:.2f}%\n状态：{status_text(raffle)}\n"
        "多名中奖者的最终中奖概率不是首轮概率乘名额。"
    )


def result_text(result):
    lines = [f"🎉 {result['title']}｜开奖结果", f"抽奖 ID：{result['raffle_id']}"]
    for index, winner in enumerate(result["winners"], 1):
        lines.append(f"{index}. {name_text(winner['display_name'])}（ID：{winner['user_id']}）")
    if len(result["winners"]) < result["requested_count"]:
        lines.append(
            f"正权重人数不足：原定 {result['requested_count']} 名，"
            f"实际抽出 {len(result['winners'])} 名。"
        )
    lines.extend(
        [
            "每人最多中奖一次。此结果已保存，重复开奖不会重新抽取。",
            f"名单摘要：{result['snapshot_hash']}",
        ]
    )
    return "\n".join(lines)


async def group_member(bot, store, chat_id, user_id):
    """Ask Telegram whether user_id is in chat_id now, following a supergroup upgrade."""
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except ChatMigrated as exc:
        await asyncio.to_thread(store.migrate_chat, chat_id, exc.new_chat_id)
        member = await bot.get_chat_member(exc.new_chat_id, user_id)
    # Restricted users may still be in the group; only ChatMemberRestricted has is_member.
    return member.status in MEMBER_STATUSES or getattr(member, "is_member", False)


async def reply(message, text, markup=None):
    # Keep even non-BMP characters below Telegram's UTF-16 length limit.
    chunks = [text[i : i + 1500] for i in range(0, len(text), 1500)]
    for index, chunk in enumerate(chunks):
        await message.reply_text(chunk, reply_markup=markup if index == len(chunks) - 1 else None)


class BotHandlers:
    def __init__(self, store, admin_ids):
        self.store = store
        self.admin_ids = admin_ids

    async def command(self, update, context):
        message, user = update.effective_message, update.effective_user
        if message is None or user is None or user.is_bot or message.sender_chat:
            return
        command = message.text.split()[0][1:].split("@")[0].lower()
        args = context.args
        if command in ADMIN_COMMANDS and user.id not in self.admin_ids:
            await reply(message, "此命令仅限配置的机器人管理员使用。")
            return
        if command in PRIVATE_COMMANDS | {"myweight"} and update.effective_chat.type != "private":
            await reply(message, "请私聊机器人执行此命令。")
            return
        try:
            if command in ("start", "help"):
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
                await asyncio.to_thread(
                    self.store.configure, rid, user.id, int(args[1]), int(args[2])
                )
            elif command == "rule":
                await asyncio.to_thread(self.store.rule, rid, user.id, args[1], int(args[2]))
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
                await asyncio.to_thread(self.store.override, rid, user.id, int(args[1]), value)
            elif command == "freeze":
                frozen = await asyncio.to_thread(self.store.freeze, rid, user.id)
                text = (
                    f"抽奖 {rid} 已开奖，可使用 /result {rid} 查看已保存结果。"
                    if frozen["status"] == "DRAWN"
                    else f"抽奖 {rid} 已冻结，可使用 /draw {rid} 开奖。"
                )
                await reply(message, text)
                return
            elif command == "draw":
                result = await asyncio.to_thread(self.store.draw, rid, user.id)
                await reply(message, result_text(result))
                return
            elif command == "export":
                exported = await asyncio.to_thread(self.store.export, rid)
                stream = BytesIO(json.dumps(exported, ensure_ascii=False, indent=2).encode())
                await message.reply_document(
                    document=stream,
                    filename=f"lottery-{rid}.json",
                    caption="完整名单、规则、快照、开奖结果和管理员修改记录。",
                )
                return
            else:
                if command == "publish" and update.effective_chat.type != "private":
                    await asyncio.to_thread(self.store.bind, rid, user.id, update.effective_chat.id)
                raffle = await asyncio.to_thread(self.store.view, rid)
                if command in ("publish", "raffle"):
                    if raffle["result"]:
                        await reply(message, result_text(raffle["result"]))
                    else:
                        text, markup = card(raffle)
                        await reply(message, text, markup)
                elif command == "result":
                    await reply(
                        message,
                        result_text(raffle["result"])
                        if raffle["result"]
                        else "尚未开奖，请等待管理员执行开奖。",
                    )
                elif command == "myweight":
                    await reply(message, personal_weight(raffle, user.id))
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
                        chance = 100 * p["weight"] / total if total else 0
                        source = "个人覆盖" if p["override"] is not None else "规则"
                        lines.append(
                            f"{name_text(p['display_name'])} / {p['user_id']}："
                            f"权重 {p['weight']}（{source}），首轮 {chance:.2f}%"
                        )
                    lines.append("展示前 30 人。完整名单和记录使用 /export。")
                    await reply(message, "\n".join(lines))
                return
            await reply(message, f"抽奖 {rid} 的配置已保存，可用 /preview {rid} 查看。")
        except LotteryError as exc:
            await reply(message, str(exc))
        except (ValueError, OverflowError):
            await reply(message, "数字参数格式错误。用法：" + USAGE.get(command, "/help"))

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
                text = (
                    "报名成功！可点击“查看我的权重”查询。" if added else "你已报名，无需重复报名。"
                )
            else:
                raffle = await asyncio.to_thread(self.store.view, rid)
                text = personal_weight(raffle, query.from_user.id)
        except (LotteryError, ValueError, OverflowError) as exc:
            text = str(exc) if isinstance(exc, LotteryError) else "无效的抽奖按钮。"
        try:
            await query.answer(text, show_alert=True)
        except TelegramError as exc:
            # Clicks replayed after downtime are too old to answer. Any join above is
            # already saved, so there is nothing to retry and nothing to tell the group.
            LOG.warning("按钮应答失败：%s", exc)

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
            BotCommand("help", "使用说明"),
            BotCommand("id", "查看我的用户 ID"),
            BotCommand("raffle", "查看抽奖并报名"),
            BotCommand("myweight", "查看我的权重"),
            BotCommand("result", "查看开奖结果"),
        ]
    )


def build_application(settings):
    handlers = BotHandlers(Store(settings.database_path), settings.admin_ids)
    app = (
        Application.builder()
        .token(settings.token)
        .concurrent_updates(False)
        .post_init(register_commands)
        .build()
    )
    commands = sorted(
        ADMIN_COMMANDS | {"start", "help", "id", "raffle", "result", "myweight", "rules"}
    )
    app.add_handler(CommandHandler(commands, handlers.command))
    app.add_handler(CallbackQueryHandler(handlers.callback, pattern=r"^(join|weight):[0-9]{1,19}$"))
    app.add_handler(MessageHandler(filters.StatusUpdate.MIGRATE, handlers.migrate))
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
        application.run_polling(
            allowed_updates=["message", "callback_query"], drop_pending_updates=False
        )
    except LotteryError as exc:
        raise SystemExit(str(exc)) from None
    except Exception as exc:  # noqa: BLE001 - CLI boundary: keep the cause, mask the token.
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        detail = redact(f"{type(exc).__name__}: {exc}", token)
        raise SystemExit(f"启动或运行失败：{detail}\n请检查令牌、网络代理和数据库路径。") from None


if __name__ == "__main__":
    main()

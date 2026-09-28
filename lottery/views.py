"""Text and cards shown in Telegram. Pure functions, no I/O."""

import json
from datetime import datetime
from html import escape
from io import BytesIO

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

STATUS = {"OPEN": "报名中", "FROZEN": "报名已截止", "DRAWN": "已开奖", "CANCELLED": "已取消"}
EXPORT_CAPTION = "完整名单、规则、快照、开奖结果和管理员修改记录。"


def name_text(text):
    return " ".join(text.split())[:128]


def status_text(raffle):
    return STATUS[raffle["status"]]


def when_text(timestamp, timezone):
    moment = datetime.fromtimestamp(timestamp, timezone)
    offset = f"{moment:%z}"  # per date, so daylight saving time shows correctly
    return f"{moment:%Y-%m-%d %H:%M}（UTC{offset[:3]}:{offset[3:]}）"


def percent(weight, total):
    """First-round chance, with enough digits that a real chance never shows as 0%."""
    value = 100 * weight / total if total else 0
    digits = 0 if value >= 10 or value == 0 else 1 if value >= 1 else 2
    return f"{value:.{digits}f}%"


def draw_rule(raffle, timezone):
    """When the raffle is drawn: at its deadline, or once full but no later than it."""
    when = when_text(raffle["deadline"], timezone)
    if raffle["target_count"]:
        return f"满 {raffle['target_count']} 人开奖（最晚 {when}）"
    return f"开奖时间：{when}"


def card(raffle, timezone):
    """The group card. Participants see the essentials only; weight details stay private,
    but a weighted raffle always says that it is weighted."""
    lines = [
        f"🎁 {raffle['title']}  #{raffle['id']}",
        f"中奖 {raffle['winner_count']} 人 · 已参与 {len(raffle['entries'])} 人",
        draw_rule(raffle, timezone),
    ]
    if raffle["weighted"]:
        lines.append("本场设有中奖加成")
    markup = None
    if raffle["status"] == "OPEN" and raffle["chat_id"] is not None:
        markup = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🎟 参与抽奖", callback_data=f"join:{raffle['id']}")]]
        )
    elif raffle["status"] == "OPEN":
        lines.append("尚未在群里发布，暂不能报名。")
    else:
        lines.append(status_text(raffle))
    return "\n".join(lines), markup


def result_text(result, mention=False):
    """The draw announcement. With mention=True it is HTML that notifies every winner."""
    safe = escape if mention else str
    lines = [f"🎉 {safe(result['title'])} 开奖结果  #{result['raffle_id']}"]
    for index, winner in enumerate(result["winners"], 1):
        name = safe(name_text(winner["display_name"]))
        if mention:
            lines.append(f'{index}. <a href="tg://user?id={winner["user_id"]}">{name}</a>')
        else:
            lines.append(f"{index}. {name}（ID：{winner['user_id']}）")
    if len(result["winners"]) < result["requested_count"]:
        lines.append(
            f"有效参与人数不足：原定 {result['requested_count']} 名，"
            f"实际抽出 {len(result['winners'])} 名。"
        )
    lines.append(f"名单摘要：{result['snapshot_hash']}")
    return "\n".join(lines)


def chunks(text, limit=1500):
    """Split a long message at line breaks, so an HTML link is never cut in half.

    1500 characters stay below Telegram's 4096 UTF-16 units even for non-BMP text."""
    parts = []
    for line in text.split("\n") if text else []:
        if parts and len(parts[-1]) + 1 + len(line) <= limit:
            parts[-1] += "\n" + line
        else:
            parts.extend(line[i : i + limit] for i in range(0, max(len(line), 1), limit))
    return parts


def export_file(exported):
    """The /export attachment as (file, file name)."""
    raw = json.dumps(exported, ensure_ascii=False, indent=2).encode()
    return BytesIO(raw), f"lottery-{exported['raffle']['id']}.json"

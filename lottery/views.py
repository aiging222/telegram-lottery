"""Text and cards shown in Telegram. Pure functions, no I/O."""

import json
from datetime import datetime
from html import escape
from io import BytesIO

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

STATUS = {"OPEN": "报名中", "FROZEN": "报名已截止", "DRAWN": "已开奖", "CANCELLED": "已取消"}
PLACES = "一二三四五六七八九十"
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


def chances(entries, winner_count):
    """Each entry's chance in the first random pick, as text by user ID. Designated winners
    take their places before it; when they take them all, nobody else has a chance."""
    designated = sum(1 for e in entries if e.get("designated"))
    drawn = [e for e in entries if not e.get("designated")]
    total = sum(e["weight"] for e in drawn) if designated < winner_count else 0
    return {
        e["user_id"]: "指定获奖" if e.get("designated") else percent(e["weight"], total)
        for e in entries
    }


def place(index):
    """第一名, 第二名, … for index 0, 1, …"""
    return f"第{PLACES[index]}名"


def prize_text(prizes, kind="join"):
    if kind == "rank":
        return "、".join(f"{place(i)} {name}" for i, (name, _) in enumerate(prizes))
    return "、".join(f"{name} ×{count}" for name, count in prizes)


def activity_rule(kind, winner_count, min_messages):
    """How a group activity raffle is won."""
    if kind == "rank":
        return f"💬 按发言次数排名，前 {winner_count} 名获奖"
    return f"💬 发言满 {min_messages} 次即可参与，抽 {winner_count} 人"


def counting_text(count_from, timezone):
    return f"📊 统计 {when_text(count_from, timezone)} 起的文字发言"


def join_text(keyword):
    return f"在群里发送「{keyword}」参与" if keyword else "点按钮参与"


def draw_rule(raffle, timezone):
    """When the raffle is drawn: at its deadline, or once full but no later than it."""
    when = when_text(raffle["deadline"], timezone)
    if raffle["target_count"]:
        return f"满 {raffle['target_count']} 人开奖（最晚 {when}）"
    return f"开奖时间：{when}"


def card(raffle, timezone):
    """The group card. Participants see the essentials only; weight details stay private,
    but a weighted raffle always says that it is weighted."""
    kind = raffle.get("kind", "join")
    lines = [f"🎁 {raffle['title']}  #{raffle['id']}"]
    if raffle["prizes"]:
        lines.append(f"🏆 {prize_text(raffle['prizes'], kind)}")
    if kind == "join":
        lines.append(f"中奖 {raffle['winner_count']} 人 · 已参与 {len(raffle['entries'])} 人")
    else:
        lines += [
            activity_rule(kind, raffle["winner_count"], raffle["min_messages"]),
            counting_text(raffle["count_from"], timezone),
        ]
    lines.append(draw_rule(raffle, timezone))
    if raffle["weighted"]:
        lines.append("本场设有中奖加成")
    markup = None
    if raffle["status"] == "OPEN" and raffle["chat_id"] is not None:
        if kind != "join":
            lines.append("👉 在群里发言即可参与")
        elif raffle["keyword"]:
            lines.append(f"👉 {join_text(raffle['keyword'])}")
        else:
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
        prize = f" — {safe(winner['prize'])}" if winner.get("prize") else ""
        if "messages" in winner:  # group activity raffles
            prize += f"（发言 {winner['messages']} 次）"
        if mention:
            link = f'<a href="tg://user?id={winner["user_id"]}">{name}</a>'
            lines.append(f"{index}. {link}{prize}")
        else:
            lines.append(f"{index}. {name}（ID：{winner['user_id']}）{prize}")
    if len(result["winners"]) < result["requested_count"]:
        lines.append(
            f"有效参与人数不足：原定 {result['requested_count']} 名，"
            f"实际抽出 {len(result['winners'])} 名。"
        )
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

"""Transactional storage and integer-weighted sampling without replacement."""

import hashlib
import json
import math
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path


class LotteryError(ValueError):
    pass


class NotEnoughPoints(LotteryError):
    """Too few 灵石 to join a raffle that costs them."""


def integer(value, name, low=0, high=1_000_000):
    if type(value) is not int or not low <= value <= high:
        raise LotteryError(f"{name}必须为 {low}～{high} 的整数。")
    return value


def check_prizes(prizes):
    """[[name, count], ...] with 1..MAX_PRIZES kinds and 1..100 winners in all."""
    if not 1 <= len(prizes) <= MAX_PRIZES:
        raise LotteryError(f"奖品需为 1～{MAX_PRIZES} 种。")
    for name, count in prizes:
        if not 1 <= len(name.strip()) <= 64:
            raise LotteryError("奖品名称需为 1～64 字。")
        integer(count, "奖品份数", 1, 100)
    return integer(sum(count for _, count in prizes), "中奖总人数", 1, 100)


def check_keyword(keyword):
    if not 1 <= len(keyword) <= MAX_KEYWORD or keyword.startswith("/"):
        raise LotteryError(f"口令需为 1～{MAX_KEYWORD} 字，且不能以 / 开头。")
    return keyword


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def weighted_draw(entries, count, randbelow=secrets.randbelow):
    integer(count, "中奖名额", 1, 100)
    pool = []
    seen = set()
    for entry in entries:
        weight = integer(entry["weight"], "权重")
        if entry["user_id"] in seen:
            raise LotteryError("候选名单存在重复用户。")
        seen.add(entry["user_id"])
        if weight:
            pool.append(dict(entry))
    winners = []
    for _ in range(min(count, len(pool))):
        ticket = randbelow(sum(p["weight"] for p in pool))
        for index, person in enumerate(pool):
            if ticket < person["weight"]:
                winners.append(pool.pop(index))
                break
            ticket -= person["weight"]
    return winners


def pick_winners(entries, count, randbelow=secrets.randbelow):
    """Designated entries win first, in their order; the places left are drawn by weight
    from everyone else."""
    winners = [dict(entry) for entry in entries if entry.get("designated")][:count]
    if len(winners) < count:
        rest = [entry for entry in entries if not entry.get("designated")]
        winners += weighted_draw(rest, count - len(winners), randbelow)
    return winners


# Migration N upgrades a database from schema version N-1 (PRAGMA user_version) to N.
# Append new migrations; never edit one that has shipped.
MIGRATIONS = [
    # 1: initial schema. IF NOT EXISTS lets unversioned databases from 0.1.0 pass through.
    """
    CREATE TABLE IF NOT EXISTS raffles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        winner_count INTEGER NOT NULL,
        deadline REAL NOT NULL,
        default_weight INTEGER NOT NULL DEFAULT 1,
        weight_cap INTEGER NOT NULL DEFAULT 100,
        status TEXT NOT NULL DEFAULT 'OPEN',
        created_by INTEGER NOT NULL,
        snapshot TEXT,
        snapshot_hash TEXT,
        result TEXT
    );
    CREATE TABLE IF NOT EXISTS participants (
        raffle_id INTEGER REFERENCES raffles(id),
        user_id INTEGER NOT NULL,
        display_name TEXT NOT NULL,
        PRIMARY KEY (raffle_id, user_id)
    );
    CREATE TABLE IF NOT EXISTS rules (
        raffle_id INTEGER REFERENCES raffles(id),
        tag TEXT NOT NULL,
        bonus INTEGER NOT NULL,
        PRIMARY KEY (raffle_id, tag)
    );
    CREATE TABLE IF NOT EXISTS grants (
        raffle_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        tag TEXT NOT NULL,
        PRIMARY KEY (raffle_id, user_id, tag),
        FOREIGN KEY (raffle_id, tag) REFERENCES rules(raffle_id, tag)
    );
    CREATE TABLE IF NOT EXISTS overrides (
        raffle_id INTEGER REFERENCES raffles(id),
        user_id INTEGER NOT NULL,
        weight INTEGER NOT NULL,
        PRIMARY KEY (raffle_id, user_id)
    );
    CREATE TABLE IF NOT EXISTS audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        raffle_id INTEGER REFERENCES raffles(id),
        actor_id INTEGER NOT NULL,
        action TEXT NOT NULL,
        details TEXT NOT NULL,
        at REAL NOT NULL
    );
    """,
    # 2: group binding for member-only joins.
    "ALTER TABLE raffles ADD COLUMN chat_id INTEGER",
    # 3: export and audit lookups filter by raffle.
    "CREATE INDEX IF NOT EXISTS audit_by_raffle ON audit(raffle_id)",
    # 4: automatic draws announce each result in the bound group once. Raffles already
    # drawn or past their deadline were handled by hand before this, so skip them.
    """
    ALTER TABLE raffles ADD COLUMN announced_at REAL;
    UPDATE raffles SET announced_at = 0
    WHERE result IS NOT NULL OR deadline <= CAST(strftime('%s', 'now') AS REAL)
    """,
    # 5: button menus. Raffles may end once full and remember their group card for edits;
    # the bot remembers its groups; each admin has at most one unfinished creation wizard.
    """
    ALTER TABLE raffles ADD COLUMN target_count INTEGER;
    ALTER TABLE raffles ADD COLUMN card_message_id INTEGER;
    CREATE INDEX IF NOT EXISTS raffles_by_chat ON raffles(chat_id);
    CREATE TABLE IF NOT EXISTS groups (
        chat_id INTEGER PRIMARY KEY,
        title TEXT NOT NULL,
        active INTEGER NOT NULL,
        updated_at REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS drafts (
        user_id INTEGER PRIMARY KEY,
        chat_id INTEGER NOT NULL,
        data TEXT NOT NULL,
        updated_at REAL NOT NULL
    )
    """,
    # 6: whether the group card says "本场设有中奖加成". Once any bonus or personal weight
    # is set it stays on, so the notice never disappears from a card that showed it.
    """
    ALTER TABLE raffles ADD COLUMN weighted INTEGER NOT NULL DEFAULT 0;
    UPDATE raffles SET weighted = 1
    WHERE id IN (SELECT raffle_id FROM overrides)
    OR id IN (SELECT raffle_id FROM rules WHERE bonus > 0)
    """,
    # 7: a prize list (JSON [[name, count], ...], handed out in draw order) and an optional
    # keyword that people send in the group to join instead of pressing the card's button.
    """
    ALTER TABLE raffles ADD COLUMN prizes TEXT;
    ALTER TABLE raffles ADD COLUMN keyword TEXT
    """,
    # 8: per-group settings (JSON over GROUP_DEFAULTS), the result the bot last pinned in
    # each group, and group messages waiting to be deleted.
    """
    ALTER TABLE groups ADD COLUMN settings TEXT;
    ALTER TABLE groups ADD COLUMN pinned_result INTEGER;
    CREATE TABLE IF NOT EXISTS deletions (
        chat_id INTEGER NOT NULL,
        message_id INTEGER NOT NULL,
        due_at REAL NOT NULL,
        PRIMARY KEY (chat_id, message_id)
    );
    CREATE INDEX IF NOT EXISTS deletions_by_due ON deletions(due_at)
    """,
    # 9: the groups each user was last seen managing, so that "我的群" asks Telegram about
    # those only instead of every group the bot is in.
    """
    CREATE TABLE IF NOT EXISTS managers (
        chat_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        PRIMARY KEY (chat_id, user_id)
    )
    """,
    # 10: participants a super admin names as winners ahead of the draw.
    """
    CREATE TABLE IF NOT EXISTS designations (
        raffle_id INTEGER NOT NULL REFERENCES raffles(id),
        user_id INTEGER NOT NULL,
        PRIMARY KEY (raffle_id, user_id)
    )
    """,
    # 11: group activity raffles, won by the text messages sent in the group from count_from
    # on: "rank" gives the prizes to the most active, "reach" draws among those with at least
    # min_messages. Messages are counted per member and minute; speakers keeps the name each
    # member last wrote under and when they last left.
    """
    ALTER TABLE raffles ADD COLUMN kind TEXT NOT NULL DEFAULT 'join';
    ALTER TABLE raffles ADD COLUMN count_from REAL;
    ALTER TABLE raffles ADD COLUMN min_messages INTEGER;
    CREATE TABLE IF NOT EXISTS activity (
        chat_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        minute INTEGER NOT NULL,
        count INTEGER NOT NULL,
        last_at REAL NOT NULL,
        PRIMARY KEY (chat_id, user_id, minute)
    );
    CREATE INDEX IF NOT EXISTS activity_by_minute ON activity(chat_id, minute);
    CREATE TABLE IF NOT EXISTS speakers (
        chat_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        display_name TEXT NOT NULL,
        left_at REAL,
        PRIMARY KEY (chat_id, user_id)
    )
    """,
    # 12: super admins' corrections to members' message counts in an activity raffle, for
    # messages the bot missed: delta is added to what it counts; at is when it was set.
    """
    CREATE TABLE IF NOT EXISTS adjustments (
        raffle_id INTEGER NOT NULL REFERENCES raffles(id),
        user_id INTEGER NOT NULL,
        delta INTEGER NOT NULL,
        at REAL NOT NULL,
        PRIMARY KEY (raffle_id, user_id)
    )
    """,
    # 13: 灵石, points members earn in each group by checking in once a day and by speaking.
    # points holds each member's balance there, the day they last checked in and the message
    # rewards taken on reward_day; ledger records every change and the balance after it.
    """
    CREATE TABLE IF NOT EXISTS points (
        chat_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        display_name TEXT NOT NULL,
        balance INTEGER NOT NULL DEFAULT 0,
        checkin_day TEXT,
        reward_day TEXT,
        rewards INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (chat_id, user_id)
    );
    CREATE INDEX IF NOT EXISTS points_by_balance ON points(chat_id, balance);
    CREATE TABLE IF NOT EXISTS ledger (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        delta INTEGER NOT NULL,
        balance INTEGER NOT NULL,
        reason TEXT NOT NULL,
        actor_id INTEGER NOT NULL,
        at REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS ledger_by_chat ON ledger(chat_id)
    """,
    # 14: 积分抽奖, raffles that cost `cost` 灵石 to join. Each participant's `paid` is given
    # back if the raffle is cancelled; ledger rows name the raffle they were paid into.
    """
    ALTER TABLE raffles ADD COLUMN cost INTEGER;
    ALTER TABLE participants ADD COLUMN paid INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE ledger ADD COLUMN raffle_id INTEGER
    """,
    # 15: who changed which group setting, when, and from what (JSON values; `before` is
    # what applied then, the default if it was never set).
    """
    CREATE TABLE IF NOT EXISTS setting_changes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER NOT NULL,
        key TEXT NOT NULL,
        before TEXT NOT NULL,
        after TEXT NOT NULL,
        actor_id INTEGER NOT NULL,
        actor_name TEXT NOT NULL,
        at REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS setting_changes_by_chat ON setting_changes(chat_id)
    """,
    # 16: the 灵石 panel each group has pinned: a message whose buttons answer the member who
    # presses them alone.
    "ALTER TABLE groups ADD COLUMN points_panel INTEGER",
    # 17: invite raffles, "rank" and "reach" raffles that count the new members each member
    # brought in from count_from on, by their own invite link ("link") or by adding them
    # ("add"); min_messages is then the invites needed. invites keeps who brought in whom,
    # once for each member ever; departed, who has left and not come back.
    """
    ALTER TABLE raffles ADD COLUMN invite_via TEXT;
    CREATE TABLE IF NOT EXISTS invite_links (
        chat_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        link TEXT NOT NULL,
        display_name TEXT NOT NULL,
        PRIMARY KEY (chat_id, user_id)
    );
    CREATE INDEX IF NOT EXISTS invite_links_by_link ON invite_links(link);
    CREATE TABLE IF NOT EXISTS invites (
        chat_id INTEGER NOT NULL,
        invitee_id INTEGER NOT NULL,
        inviter_id INTEGER NOT NULL,
        inviter_name TEXT NOT NULL,
        via TEXT NOT NULL,
        joined_at REAL NOT NULL,
        left_at REAL,
        PRIMARY KEY (chat_id, invitee_id)
    );
    CREATE INDEX IF NOT EXISTS invites_by_inviter ON invites(chat_id, inviter_id);
    CREATE TABLE IF NOT EXISTS departed (
        chat_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        at REAL NOT NULL,
        PRIMARY KEY (chat_id, user_id)
    )
    """,
]
# Deletion delays are seconds after posting: 0 deletes at once, None keeps the message.
GROUP_DEFAULTS = {
    "pin_card": True,
    "pin_result": True,
    "delete_keyword": 60,
    "delete_notices": 3,
    # 灵石: a check-in a day, and for every message a chance of reward_chance in 100 of a
    # reward, at most reward_daily a day (0: no limit), multiplied by crit_times with a chance
    # of crit_percent in 100.
    "points": True,
    "checkin_points": 10,
    "reward_chance": 5,
    "reward_points": 5,
    "reward_daily": 0,
    "crit_percent": 10,
    "crit_times": 2,
    # A message counts, for activity raffles and 灵石 alike, if it has at least min_chars
    # characters besides spaces and comes at least cooldown seconds after the last that did.
    "min_chars": 3,
    "cooldown": 5,
}
SETTING_LIMITS = {
    "checkin_points": (0, 10_000),
    "reward_chance": (1, 100),
    "reward_points": (0, 10_000),
    "reward_daily": (0, 1_000),
    "crit_percent": (0, 100),
    "crit_times": (2, 10),
    "min_chars": (1, 100),
    "cooldown": (0, 3_600),
}
# Telegram lets bots delete group messages for 48 hours; older ones are given up on.
DELETE_WINDOW = 47 * 3600
MAX_PRIZES = 10
MAX_KEYWORD = 32
# join: people join by button or keyword; rank and reach: group activity raffles.
KINDS = ("join", "rank", "reach")
INVITE_WAYS = ("link", "add")  # an invite raffle counts joins by invite link, or by adding
LOOK_BACK_DAYS = 30  # how far back an activity raffle may start counting
KEEP_ACTIVITY = 31 * 86400


class Store:
    def __init__(self, path, clock=time.time):
        self.path = str(path)
        self.clock = clock
        # Message counts not saved yet: (chat_id, user_id, minute) -> (count, last message
        # time), and (chat_id, user_id) -> the name last written under. See count_message().
        self._pending = {}
        self._names = {}
        # (chat_id, user_id) -> when their last counted message was sent, for the cooldown.
        self._last_counted = {}
        self._pending_lock = threading.Lock()
        self._purged_at = 0.0
        # chat_id -> its settings, read for every group message; set_group_setting() and
        # migrate_chat() are the only writers.
        self._settings = {}
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.reading() as db:
            # Write-ahead logging: reads and the one write at a time no longer wait for each
            # other. The mode stays with the file; SQLite keeps it in a -wal file beside the
            # database until the last connection closes.
            db.execute("PRAGMA journal_mode = WAL")
        with self.transaction() as db:
            self._migrate(db)

    def _migrate(self, db):
        # Plain execute() keeps DDL inside the transaction, unlike executescript(), which
        # commits first; a failed migration therefore leaves schema and version untouched.
        version = db.execute("PRAGMA user_version").fetchone()[0]
        for number, script in enumerate(MIGRATIONS[version:], version + 1):
            for statement in filter(str.strip, script.split(";")):
                db.execute(statement)
            db.execute(f"PRAGMA user_version = {number}")

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @contextmanager
    def reading(self):
        """A connection for a lone query. It takes no write lock, so unlike transaction() it
        neither waits for other transactions to finish nor makes them wait."""
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()

    def audit(self, db, raffle_id, actor, action, details):
        db.execute(
            "INSERT INTO audit(raffle_id,actor_id,action,details,at) VALUES(?,?,?,?,?)",
            (raffle_id, actor, action, encode(details), self.clock()),
        )

    def _get(self, db, raffle_id):
        row = db.execute("SELECT * FROM raffles WHERE id=?", (raffle_id,)).fetchone()
        if row is None:
            raise LotteryError("抽奖不存在。")
        return dict(row)

    def _open(self, raffle):
        return raffle["status"] == "OPEN" and self.clock() < raffle["deadline"]

    def _full(self, db, raffle):
        """Whether `target_count` have joined, or for an invite raffle have enough invites."""
        target = raffle["target_count"]
        if target is None:
            return False
        if raffle["invite_via"]:
            return len(self._invited(db, raffle)) >= target
        count = db.execute(
            "SELECT COUNT(*) FROM participants WHERE raffle_id=?", (raffle["id"],)
        ).fetchone()[0]
        return count >= target

    def _editable(self, db, raffle_id):
        raffle = self._get(db, raffle_id)
        if raffle["status"] == "CANCELLED":
            raise LotteryError("本场抽奖已取消。")
        if not self._open(raffle):
            raise LotteryError("本场抽奖报名已截止，不能报名或修改权重。")
        return raffle

    def bind(self, raffle_id, actor, chat_id):
        """Tie an open raffle to the one group whose members may join; it never moves."""
        with self.transaction() as db:
            raffle = self._get(db, raffle_id)
            if not self._open(raffle) or raffle["chat_id"] == chat_id:
                return  # closed cards carry no join button, so any chat may show them
            if raffle["chat_id"] is not None:
                raise LotteryError(
                    "本场抽奖已在其他群发布，仅限该群成员报名。如需在此群抽奖，请新建抽奖。"
                )
            db.execute("UPDATE raffles SET chat_id=? WHERE id=?", (chat_id, raffle_id))
            self.audit(db, raffle_id, actor, "bind", {"chat_id": chat_id})

    def target_chat(self, raffle_id):
        """The group a join must be verified against; raises while joining is closed."""
        with self.transaction() as db:
            chat_id = self._editable(db, raffle_id)["chat_id"]
        if chat_id is None:
            raise LotteryError("本场抽奖尚未在群里发布，暂不能报名。")
        return chat_id

    def migrate_chat(self, old_chat_id, new_chat_id):
        """Follow Telegram's group-to-supergroup upgrade, which changes the chat ID. The old
        group's messages stay behind, out of the bot's reach, so cards, pins and deletions
        there are forgotten. Returns the raffles whose card was left behind."""
        self.flush_activity()
        with self.transaction() as db:
            rows = db.execute(
                "SELECT id,card_message_id FROM raffles WHERE chat_id=?", (old_chat_id,)
            ).fetchall()
            db.execute(
                "UPDATE raffles SET chat_id=?,card_message_id=NULL WHERE chat_id=?",
                (new_chat_id, old_chat_id),
            )
            db.execute(
                "UPDATE OR REPLACE groups SET chat_id=?,pinned_result=NULL,points_panel=NULL "
                "WHERE chat_id=?",
                (new_chat_id, old_chat_id),
            )
            db.execute("UPDATE drafts SET chat_id=? WHERE chat_id=?", (new_chat_id, old_chat_id))
            db.execute(
                "UPDATE OR REPLACE managers SET chat_id=? WHERE chat_id=?",
                (new_chat_id, old_chat_id),
            )
            db.execute("DELETE FROM deletions WHERE chat_id=?", (old_chat_id,))
            db.execute(
                "INSERT INTO activity SELECT ?,user_id,minute,count,last_at FROM activity "
                "WHERE chat_id=? ON CONFLICT(chat_id,user_id,minute) DO UPDATE SET "
                "count=count+excluded.count,last_at=max(last_at,excluded.last_at)",
                (new_chat_id, old_chat_id),
            )
            db.execute("DELETE FROM activity WHERE chat_id=?", (old_chat_id,))
            db.execute(
                "UPDATE OR REPLACE speakers SET chat_id=? WHERE chat_id=?",
                (new_chat_id, old_chat_id),
            )
            db.execute(
                "UPDATE OR REPLACE points SET chat_id=? WHERE chat_id=?", (new_chat_id, old_chat_id)
            )
            db.execute("UPDATE ledger SET chat_id=? WHERE chat_id=?", (new_chat_id, old_chat_id))
            db.execute(
                "UPDATE setting_changes SET chat_id=? WHERE chat_id=?", (new_chat_id, old_chat_id)
            )
            for table in ("invites", "departed"):
                db.execute(
                    f"UPDATE OR REPLACE {table} SET chat_id=? WHERE chat_id=?",
                    (new_chat_id, old_chat_id),
                )
            # Invite links lead to the old group; members get new ones for the supergroup.
            db.execute("DELETE FROM invite_links WHERE chat_id=?", (old_chat_id,))
            for row in rows:
                self.audit(
                    db, row["id"], 0, "migrate_chat", {"before": old_chat_id, "after": new_chat_id}
                )
        self._settings.pop(old_chat_id, None)
        self._settings.pop(new_chat_id, None)
        return [row["id"] for row in rows if row["card_message_id"] is not None]

    def leave_group(self, chat_id, user_id, left_at):
        """Cancel user_id's joins in raffles bound to chat_id that were still open at left_at.

        Frozen raffles are untouched: their snapshot is the fixed list of the draw. Activity
        raffles leave out whoever left after their last message. Their 灵石 there are gone,
        an invite that brought them in no longer counts, and their own invites count no more
        unless they come back.
        """
        self.flush_activity()  # so that the leave comes after every message saved
        with self.transaction() as db:
            db.execute(
                "UPDATE speakers SET left_at=? WHERE chat_id=? AND user_id=?",
                (left_at, chat_id, user_id),
            )
            db.execute(
                "UPDATE invites SET left_at=? WHERE chat_id=? AND invitee_id=? AND left_at IS NULL",
                (left_at, chat_id, user_id),
            )
            db.execute("INSERT OR REPLACE INTO departed VALUES(?,?,?)", (chat_id, user_id, left_at))
            row = db.execute(
                "SELECT balance FROM points WHERE chat_id=? AND user_id=?", (chat_id, user_id)
            ).fetchone()
            if row and row["balance"]:
                self._credit(db, chat_id, user_id, -row["balance"], "leave", 0)
            ids = [
                row["id"]
                for row in db.execute(
                    "SELECT r.id FROM raffles r JOIN participants p ON p.raffle_id=r.id "
                    "WHERE r.chat_id=? AND p.user_id=? AND r.status='OPEN' AND r.deadline>?",
                    (chat_id, user_id, left_at),
                )
            ]
            for raffle_id in ids:
                for table in ("participants", "designations"):
                    db.execute(
                        f"DELETE FROM {table} WHERE raffle_id=? AND user_id=?", (raffle_id, user_id)
                    )
                self.audit(db, raffle_id, 0, "leave", {"user_id": user_id, "chat_id": chat_id})
            return ids

    def create(
        self,
        actor,
        title,
        winner_count,
        minutes=None,
        *,
        deadline=None,
        chat_id=None,
        target=None,
        weighted=False,
        prizes=None,
        keyword=None,
        kind="join",
        count_from=None,
        min_messages=None,
        cost=None,
        invite_via=None,
    ):
        """Create a raffle ending after `minutes` or at `deadline`, or earlier once `target`
        people have joined. Menus create it already bound to `chat_id`; `weighted` shows the
        bonus notice on the card from the start, before any weight is set. `prizes` are
        handed out in draw order and must add up to `winner_count`; with a `keyword`, people
        join by sending it in the group.

        A "rank" or "reach" raffle counts the text messages sent in its group from
        `count_from` (default: now) on: "rank" hands its prizes, one per place, to the most
        active; "reach" draws among those with at least `min_messages`.

        With `invite_via` ("link" or "add") such a raffle counts the new members each member
        brought in from its publishing on instead, and `min_messages` is the invites needed;
        a "reach" one may then be drawn once `target` members have enough.

        Joining a raffle with a `cost` takes that many of the member's 灵石 in its group."""
        integer(winner_count, "中奖名额", 1, 100)
        if kind not in KINDS:
            raise LotteryError("抽奖类型无效。")
        if invite_via is not None and (invite_via not in INVITE_WAYS or kind == "join"):
            raise LotteryError("邀请方式无效。")
        if kind != "join":
            full = invite_via is not None and kind == "reach"  # enough people with enough
            if prizes is None or chat_id is None or keyword is not None:
                raise LotteryError("群活跃抽奖和邀请抽奖需要奖品和发布群，不用口令。")
            if target is not None and not full:
                raise LotteryError("只有邀请次数抽奖可以满人开奖。")
            if kind == "rank" and any(count != 1 for _, count in prizes):
                raise LotteryError("排名抽奖每个名次一份奖品。")
            if invite_via is not None and count_from is not None:
                raise LotteryError("邀请抽奖从发布时开始统计。")
        if (kind == "reach") != (min_messages is not None):
            raise LotteryError("只有达标抽奖需要设置次数。")
        if min_messages is not None:
            integer(min_messages, "邀请人数" if invite_via else "发言次数", 1, 100_000)
        if kind == "join" and count_from is not None:
            raise LotteryError("只有群活跃抽奖统计发言。")
        if cost is not None:
            integer(cost, "参与所需灵石", 1, 1_000_000)
            if kind != "join" or chat_id is None:
                raise LotteryError("积分抽奖需要报名参与，并发布到群里。")
        if prizes is not None:
            prizes = [[name.strip(), count] for name, count in prizes]
            if check_prizes(prizes) != winner_count:
                raise LotteryError("奖品份数之和需等于中奖人数。")
        if keyword is not None:
            keyword = check_keyword(keyword.strip())
        if not 1 <= len(title.strip()) <= 160:
            raise LotteryError("标题长度需为 1～160 字。")
        if target is not None:
            integer(target, "满人开奖人数", winner_count, 100_000)
        with self.transaction() as db:
            now = self.clock()
            if deadline is None:
                integer(minutes, "报名时长（分钟）", 1, 525600)
                deadline = now + minutes * 60
            elif not now + 60 <= deadline <= now + 525600 * 60:
                raise LotteryError("开奖时间需在 1 分钟到 365 天之后。")
            if kind != "join":
                if count_from is None:
                    count_from = now
                elif not now - LOOK_BACK_DAYS * 86400 <= count_from < deadline:
                    raise LotteryError(
                        f"发言最早从 {LOOK_BACK_DAYS} 天前开始统计，且要早于开奖时间。"
                    )
            cursor = db.execute(
                "INSERT INTO raffles(title,winner_count,deadline,created_by,chat_id,target_count,"
                "weighted,prizes,keyword,kind,count_from,min_messages,cost,invite_via) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    title.strip(),
                    winner_count,
                    deadline,
                    actor,
                    chat_id,
                    target,
                    int(weighted),
                    None if prizes is None else encode(prizes),
                    keyword,
                    kind,
                    count_from,
                    min_messages,
                    cost,
                    invite_via,
                ),
            )
            raffle_id = cursor.lastrowid
            details = {
                "title": title,
                "deadline": deadline,
                "winner_count": winner_count,
                "chat_id": chat_id,
                "target": target,
                "weighted": weighted,
                "prizes": prizes,
                "keyword": keyword,
            }
            if kind != "join":
                details |= {"kind": kind, "count_from": count_from, "min_messages": min_messages}
            if invite_via is not None:
                details["invite_via"] = invite_via
            if cost is not None:
                details["cost"] = cost
            self.audit(db, raffle_id, actor, "create", details)
            return raffle_id

    def join(self, raffle_id, user_id, display_name):
        """Join user_id: {"paid": 灵石 paid, "balance": 灵石 left or None if it costs none}, or
        None if they already joined. Raises NotEnoughPoints if they cannot pay."""
        integer(user_id, "用户 ID", 1, 2**63 - 1)
        with self.transaction() as db:
            raffle = self._editable(db, raffle_id)
            if raffle["kind"] != "join":
                raise LotteryError("这场抽奖在群里发言即可参与，不用报名。")
            if db.execute(
                "SELECT 1 FROM participants WHERE raffle_id=? AND user_id=?", (raffle_id, user_id)
            ).fetchone():
                return None
            if self._full(db, raffle):
                raise LotteryError("名额已满，即将开奖。")
            cost, balance = raffle["cost"] or 0, None
            if cost:
                chat_id = raffle["chat_id"]
                have = self._holder(db, chat_id, user_id, display_name)["balance"]
                if have < cost:
                    raise NotEnoughPoints(
                        f"灵石不足：参与需要 {cost} 灵石，你有 {have} 灵石。"
                        "发送「签到」或多发言可以获得灵石。"
                    )
                balance = self._credit(db, chat_id, user_id, -cost, "raffle", 0, raffle_id)
            db.execute(
                "INSERT INTO participants(raffle_id,user_id,display_name,paid) VALUES(?,?,?,?)",
                (raffle_id, user_id, display_name[:128], cost),
            )
            return {"paid": cost, "balance": balance}

    def keyword_raffles(self, chat_id, text):
        """Open raffles of chat_id that people join by sending `text` (any letter case).
        Asked for every short message in every group, so it is a plain read."""
        with self.reading() as db:
            rows = db.execute(
                "SELECT id,keyword FROM raffles WHERE chat_id=? AND status='OPEN' "
                "AND keyword IS NOT NULL AND deadline>?",
                (chat_id, self.clock()),
            ).fetchall()
        return [row["id"] for row in rows if row["keyword"].casefold() == text.casefold()]

    def is_full(self, raffle_id):
        with self.transaction() as db:
            return self._full(db, self._get(db, raffle_id))

    def cancel(self, raffle_id, actor):
        """Call off a raffle that has not been drawn, giving back the 灵石 paid to join it;
        False if it already was cancelled."""
        with self.transaction() as db:
            raffle = self._get(db, raffle_id)
            if raffle["status"] == "DRAWN":
                raise LotteryError("本场抽奖已开奖，不能取消。")
            if raffle["status"] == "CANCELLED":
                return False
            db.execute("UPDATE raffles SET status='CANCELLED' WHERE id=?", (raffle_id,))
            for row in db.execute(
                "SELECT user_id,paid FROM participants WHERE raffle_id=? AND paid>0", (raffle_id,)
            ).fetchall():
                self._holder(db, raffle["chat_id"], row["user_id"])
                self._credit(
                    db, raffle["chat_id"], row["user_id"], row["paid"], "refund", actor, raffle_id
                )
            self.audit(db, raffle_id, actor, "cancel", {})
            return True

    def set_card(self, raffle_id, message_id):
        with self.transaction() as db:
            db.execute("UPDATE raffles SET card_message_id=? WHERE id=?", (message_id, raffle_id))

    def configure(self, raffle_id, actor, default, cap):
        integer(default, "默认权重")
        integer(cap, "权重上限", 1)
        if default > cap:
            raise LotteryError("默认权重不能超过上限。")
        with self.transaction() as db:
            before = self._weightable(self._editable(db, raffle_id))
            if (before["default_weight"], before["weight_cap"]) == (default, cap):
                return False  # already so: no change to record
            maximum = db.execute(
                "SELECT MAX(weight) FROM overrides WHERE raffle_id=?", (raffle_id,)
            ).fetchone()[0]
            if maximum is not None and maximum > cap:
                raise LotteryError("已有个人覆盖值高于新上限，请先调整个人权重。")
            db.execute(
                "UPDATE raffles SET default_weight=?,weight_cap=? WHERE id=?",
                (default, cap, raffle_id),
            )
            self.audit(
                db,
                raffle_id,
                actor,
                "configure",
                {
                    "before": [before["default_weight"], before["weight_cap"]],
                    "after": [default, cap],
                },
            )
            return True

    def rule(self, raffle_id, actor, tag, bonus):
        if not re.fullmatch(r"[A-Za-z0-9_\-]{1,32}", tag):
            raise LotteryError("规则名限 1～32 位字母、数字、下划线和短横线。")
        integer(bonus, "加成")
        with self.transaction() as db:
            self._weightable(self._editable(db, raffle_id))
            before = db.execute(
                "SELECT bonus FROM rules WHERE raffle_id=? AND tag=?", (raffle_id, tag)
            ).fetchone()
            if before and before[0] == bonus:
                return False  # already so: no change to record
            db.execute(
                "INSERT INTO rules VALUES(?,?,?) ON CONFLICT(raffle_id,tag) "
                "DO UPDATE SET bonus=excluded.bonus",
                (raffle_id, tag, bonus),
            )
            if bonus:
                self._mark_weighted(db, raffle_id)
            self.audit(
                db,
                raffle_id,
                actor,
                "rule",
                {"tag": tag, "before": before[0] if before else None, "after": bonus},
            )
            return True

    def grant(self, raffle_id, actor, user_id, tag, enabled=True):
        integer(user_id, "用户 ID", 1, 2**63 - 1)
        with self.transaction() as db:
            self._weightable(self._editable(db, raffle_id))
            if not db.execute(
                "SELECT 1 FROM rules WHERE raffle_id=? AND tag=?", (raffle_id, tag)
            ).fetchone():
                raise LotteryError("规则不存在，请先使用 /rule 添加。")
            if enabled:
                cursor = db.execute(
                    "INSERT OR IGNORE INTO grants VALUES(?,?,?)", (raffle_id, user_id, tag)
                )
            else:
                cursor = db.execute(
                    "DELETE FROM grants WHERE raffle_id=? AND user_id=? AND tag=?",
                    (raffle_id, user_id, tag),
                )
            if cursor.rowcount == 0:
                return False  # already granted, or nothing to revoke: no change to record
            self.audit(
                db,
                raffle_id,
                actor,
                "grant" if enabled else "revoke",
                {"user_id": user_id, "tag": tag},
            )
            return True

    def override(self, raffle_id, actor, user_id, weight):
        integer(user_id, "用户 ID", 1, 2**63 - 1)
        with self.transaction() as db:
            raffle = self._weightable(self._editable(db, raffle_id))
            if weight is not None:
                integer(weight, "个人权重", 0, raffle["weight_cap"])
            before = db.execute(
                "SELECT weight FROM overrides WHERE raffle_id=? AND user_id=?", (raffle_id, user_id)
            ).fetchone()
            if (before[0] if before else None) == weight:
                return False  # already so, or no override to remove: no change to record
            if weight is None:
                db.execute(
                    "DELETE FROM overrides WHERE raffle_id=? AND user_id=?", (raffle_id, user_id)
                )
            else:
                db.execute(
                    "INSERT INTO overrides VALUES(?,?,?) ON CONFLICT(raffle_id,user_id) "
                    "DO UPDATE SET weight=excluded.weight",
                    (raffle_id, user_id, weight),
                )
                self._mark_weighted(db, raffle_id)
            self.audit(
                db,
                raffle_id,
                actor,
                "override",
                {"user_id": user_id, "before": before[0] if before else None, "after": weight},
            )
            return True

    def designate(self, raffle_id, actor, user_id, chosen=True):
        """Name a participant as a winner ahead of the draw, or take it back; False if that
        was so already. Designated winners take the first places and prizes. Unlike weights,
        a designation adds no notice to the card."""
        with self.transaction() as db:
            raffle = self._weightable(self._editable(db, raffle_id))
            if chosen:
                if not db.execute(
                    "SELECT 1 FROM participants WHERE raffle_id=? AND user_id=?",
                    (raffle_id, user_id),
                ).fetchone():
                    raise LotteryError("只能指定已报名的成员。")
                taken = [
                    row[0]
                    for row in db.execute(
                        "SELECT user_id FROM designations WHERE raffle_id=?", (raffle_id,)
                    )
                ]
                if user_id in taken:
                    return False
                if len(taken) >= raffle["winner_count"]:
                    raise LotteryError(f"指定人数不能超过中奖人数 {raffle['winner_count']}。")
                db.execute("INSERT INTO designations VALUES(?,?)", (raffle_id, user_id))
            elif not db.execute(
                "DELETE FROM designations WHERE raffle_id=? AND user_id=?", (raffle_id, user_id)
            ).rowcount:
                return False
            self.audit(
                db,
                raffle_id,
                actor,
                "designate" if chosen else "undesignate",
                {"user_id": user_id},
            )
            return True

    def _weightable(self, raffle):
        if raffle["kind"] != "join":
            raise LotteryError("群活跃抽奖按发言次数决定，不能设置权重或指定获奖。")
        return raffle

    def _mark_weighted(self, db, raffle_id):
        db.execute("UPDATE raffles SET weighted=1 WHERE id=?", (raffle_id,))

    # Group activity: every member's text messages, counted per minute.

    def count_message(self, chat_id, user_id, display_name, at, cooldown=0):
        """Count a member's text message sent at `at`, unless it comes within `cooldown`
        seconds of the last one counted; returns whether it counted. Counts wait in memory,
        cheap for every message in every group, until flush_activity() saves them."""
        key = (chat_id, user_id, int(at // 60))
        with self._pending_lock:
            last_counted = self._last_counted.get((chat_id, user_id))
            if last_counted is not None and abs(at - last_counted) < cooldown:
                return False
            self._last_counted[chat_id, user_id] = max(at, last_counted or 0.0)
            count, last = self._pending.get(key, (0, 0.0))
            self._pending[key] = (count + 1, max(last, at))
            self._names[chat_id, user_id] = display_name[:128]
        return True

    def flush_activity(self):
        """Save the counts waiting in memory. Once an hour, also forget counts older than a
        month, unless an open activity raffle still counts from further back."""
        with self._pending_lock:
            pending, self._pending = self._pending, {}
            names, self._names = self._names, {}
        now = self.clock()
        purge = now - self._purged_at >= 3600
        if purge:
            cooled = now - SETTING_LIMITS["cooldown"][1]
            with self._pending_lock:
                self._last_counted = {k: t for k, t in self._last_counted.items() if t > cooled}
        if not pending and not names and not purge:
            return
        try:
            with self.transaction() as db:
                db.executemany(
                    "INSERT INTO activity VALUES(?,?,?,?,?) ON CONFLICT(chat_id,user_id,minute) "
                    "DO UPDATE SET count=count+excluded.count,last_at=max(last_at,excluded.last_at)",
                    [(*key, count, last) for key, (count, last) in pending.items()],
                )
                db.executemany(
                    "INSERT INTO speakers(chat_id,user_id,display_name) VALUES(?,?,?) "
                    "ON CONFLICT(chat_id,user_id) DO UPDATE SET display_name=excluded.display_name",
                    [(*key, name) for key, name in names.items()],
                )
                if purge:
                    oldest = db.execute(
                        "SELECT MIN(count_from) FROM raffles WHERE kind!='join' AND status='OPEN'"
                    ).fetchone()[0]
                    keep = (
                        now - KEEP_ACTIVITY if oldest is None else min(now - KEEP_ACTIVITY, oldest)
                    )
                    db.execute("DELETE FROM activity WHERE minute<?", (int(keep // 60),))
                    db.execute(
                        "DELETE FROM speakers WHERE NOT EXISTS (SELECT 1 FROM activity a "
                        "WHERE a.chat_id=speakers.chat_id AND a.user_id=speakers.user_id)"
                    )
        except BaseException:
            with self._pending_lock:  # keep them for the next try
                for key, (count, last) in pending.items():
                    more, later = self._pending.get(key, (0, 0.0))
                    self._pending[key] = (count + more, max(last, later))
                for key, name in names.items():
                    self._names.setdefault(key, name)
            raise
        if purge:
            self._purged_at = now

    def activity_since(self, chat_id):
        """When the bot's record of chat_id's messages begins, or None if it has none."""
        with self.reading() as db:
            first = db.execute(
                "SELECT MIN(minute) FROM activity WHERE chat_id=?", (chat_id,)
            ).fetchone()[0]
        with self._pending_lock:
            waiting = [minute for chat, _, minute in self._pending if chat == chat_id]
        minutes = [minute for minute in (first, *waiting) if minute is not None]
        return min(minutes) * 60 if minutes else None

    def ranking(self, raffle_id):
        """An open activity raffle and everyone ranked in it so far, including those still
        short of a "reach" raffle's minimum."""
        with self.reading() as db:
            raffle = self._get(db, raffle_id)
            if raffle["kind"] == "join":
                raise LotteryError("这场抽奖不按发言次数。")
            if not self._open(raffle):
                raise LotteryError("统计已截止，结果以开奖公告为准。")
            if raffle["invite_via"]:
                return raffle, self._invited(db, raffle, everyone=True)
            return raffle, self._speakers(db, raffle, everyone=True)

    def adjust(self, raffle_id, actor, user_id, *, by=None, to=None):
        """Correct a member's message count in an open activity raffle for messages the bot
        missed: add `by` (or take it away), or make the total `to`. The correction stays on
        top of whatever is counted later. False if nothing changed."""
        integer(user_id, "用户 ID", 1, 2**63 - 1)
        with self.transaction() as db:
            raffle = self._get(db, raffle_id)
            if raffle["kind"] == "join" or raffle["invite_via"]:
                raise LotteryError("只有群活跃抽奖能修改发言次数。")
            if not self._open(raffle):
                raise LotteryError("统计已截止，不能再修改发言次数。")
            counted = self._tally(db, raffle).get(user_id, [0])[0]
            row = db.execute(
                "SELECT delta FROM adjustments WHERE raffle_id=? AND user_id=?",
                (raffle_id, user_id),
            ).fetchone()
            before = row[0] if row else 0
            if to is not None:
                after = integer(to, "发言次数", 0, 1_000_000) - counted
            else:
                after = before + integer(by, "修改的次数", -1_000_000, 1_000_000)
            if counted + after < 0:
                raise LotteryError("发言次数不能小于 0。")
            if after == before:
                return False
            if after:
                db.execute(
                    "INSERT INTO adjustments VALUES(?,?,?,?) ON CONFLICT(raffle_id,user_id) "
                    "DO UPDATE SET delta=excluded.delta,at=excluded.at",
                    (raffle_id, user_id, after, self.clock()),
                )
            else:
                db.execute(
                    "DELETE FROM adjustments WHERE raffle_id=? AND user_id=?", (raffle_id, user_id)
                )
            self.audit(
                db,
                raffle_id,
                actor,
                "adjust",
                {"user_id": user_id, "counted": counted, "before": before, "after": after},
            )
            return True

    def counted(self, raffle_id, user_id):
        """A member's messages in an activity raffle so far: (as counted, correction)."""
        with self.reading() as db:
            raffle = self._get(db, raffle_id)
            counted = self._tally(db, raffle).get(user_id, [0])[0]
            row = db.execute(
                "SELECT delta FROM adjustments WHERE raffle_id=? AND user_id=?",
                (raffle_id, user_id),
            ).fetchone()
        return counted, row[0] if row else 0

    def _speakers(self, db, raffle, everyone=False):
        """Who wrote in the raffle's group from count_from until the deadline (or now), with
        the corrections of adjust() on top: the most messages first and, among equals,
        whoever got to that number first. Members who left after their last message there
        are out; "reach" keeps those with enough unless `everyone` is asked for."""
        if raffle["chat_id"] is None:
            return []
        tally = self._tally(db, raffle)
        corrections = {
            row[0]: (row[1], row[2])
            for row in db.execute(
                "SELECT user_id,delta,at FROM adjustments WHERE raffle_id=?", (raffle["id"],)
            )
        }
        for uid in corrections.keys() - tally.keys():  # added by hand only
            row = db.execute(
                "SELECT display_name,left_at FROM speakers WHERE chat_id=? AND user_id=?",
                (raffle["chat_id"], uid),
            ).fetchone()
            tally[uid] = [0, 0.0, *(row or (None, None))]
        entries = []
        for uid, (count, last, name, left) in tally.items():
            delta, at = corrections.get(uid, (0, 0.0))
            if count + delta <= 0 or (left is not None and left >= last):
                continue
            entry = {
                "user_id": uid,
                "display_name": name or f"用户 {uid}",
                "messages": count + delta,
                "reached_at": max(last, at),
                "weight": 1,
            }
            if delta:
                entry["adjusted"] = delta
            entries.append(entry)
        entries.sort(key=lambda e: (-e["messages"], e["reached_at"], e["user_id"]))
        if raffle["kind"] == "reach" and not everyone:
            entries = [e for e in entries if e["messages"] >= raffle["min_messages"]]
        return entries

    def _tally(self, db, raffle):
        """Each member's messages in the raffle's window as the bot counted them, saved or
        not: user_id -> [count, last message time, name, when they last left]."""
        chat_id = raffle["chat_id"]
        if chat_id is None:
            return {}
        start = int(raffle["count_from"] // 60)
        stop = math.ceil(min(raffle["deadline"], self.clock()) / 60)
        tally = {
            row[0]: list(row[1:])
            for row in db.execute(
                "SELECT a.user_id,SUM(a.count),MAX(a.last_at),s.display_name,s.left_at "
                "FROM activity a JOIN speakers s ON s.chat_id=a.chat_id AND s.user_id=a.user_id "
                "WHERE a.chat_id=? AND a.minute>=? AND a.minute<? GROUP BY a.user_id",
                (chat_id, start, stop),
            )
        }
        with self._pending_lock:  # counted but not saved yet
            for (chat, uid, minute), (count, last) in self._pending.items():
                if chat == chat_id and start <= minute < stop:
                    spoken = tally.setdefault(uid, [0, 0.0, None, None])
                    spoken[0] += count
                    spoken[1] = max(spoken[1], last)
            for (chat, uid), name in self._names.items():
                if chat == chat_id and uid in tally:
                    tally[uid][2] = name
        return tally

    def _invited(self, db, raffle, everyone=False):
        """Who brought new members into the raffle's group from count_from until the
        deadline, the way it counts: most invites first and, among equals, whoever got to that
        number first. Invites of members who left before the deadline do not count, nor do
        those of an inviter who left; "reach" keeps those with enough unless `everyone`."""
        deadline = raffle["deadline"]
        gone = {
            row[0]
            for row in db.execute(
                "SELECT user_id FROM departed WHERE chat_id=? AND at<?",
                (raffle["chat_id"], deadline),
            )
        }
        entries = {}
        for row in db.execute(
            "SELECT inviter_id,inviter_name,joined_at FROM invites WHERE chat_id=? AND via=? "
            "AND joined_at>=? AND joined_at<? AND (left_at IS NULL OR left_at>=?) "
            "ORDER BY joined_at",
            (raffle["chat_id"], raffle["invite_via"], raffle["count_from"], deadline, deadline),
        ):
            uid = row["inviter_id"]
            if uid in gone:
                continue
            entry = entries.setdefault(uid, {"user_id": uid, "invites": 0, "weight": 1})
            entry["invites"] += 1
            entry["reached_at"] = row["joined_at"]
            entry["display_name"] = row["inviter_name"] or f"用户 {uid}"
        ranked = sorted(
            entries.values(), key=lambda e: (-e["invites"], e["reached_at"], e["user_id"])
        )
        if raffle["kind"] == "reach" and not everyone:
            ranked = [e for e in ranked if e["invites"] >= raffle["min_messages"]]
        return ranked

    def joined(self, chat_id, user_id, at, inviter_id=None, inviter_name="", via=None):
        """user_id joined chat_id at `at`, brought in by inviter_id `via` "link" or "add" if
        known. It counts as an invite only the first time the bot sees them join, never for
        someone it saw there before, and never for bringing oneself in. Whether it counted."""
        with self.transaction() as db:
            known = any(
                db.execute(
                    f"SELECT 1 FROM {table} WHERE chat_id=? AND {column}=?", (chat_id, user_id)
                ).fetchone()
                for table, column in (
                    ("invites", "invitee_id"),
                    ("departed", "user_id"),
                    ("speakers", "user_id"),
                    ("points", "user_id"),
                )
            )
            db.execute("DELETE FROM departed WHERE chat_id=? AND user_id=?", (chat_id, user_id))
            if known or inviter_id is None or inviter_id == user_id or via not in INVITE_WAYS:
                return False
            db.execute(
                "INSERT INTO invites(chat_id,invitee_id,inviter_id,inviter_name,via,joined_at) "
                "VALUES(?,?,?,?,?,?)",
                (chat_id, user_id, inviter_id, inviter_name[:128], via, at),
            )
            return True

    def full_invite_raffles(self, chat_id):
        """Open invite raffles of chat_id that have as many members with enough invites as
        they wait for, to be drawn now."""
        with self.transaction() as db:
            return [
                row["id"]
                for row in db.execute(
                    "SELECT * FROM raffles WHERE chat_id=? AND status='OPEN' "
                    "AND invite_via IS NOT NULL AND target_count IS NOT NULL AND deadline>?",
                    (chat_id, self.clock()),
                ).fetchall()
                if self._full(db, dict(row))
            ]

    def invite_link(self, chat_id, user_id):
        with self.reading() as db:
            row = db.execute(
                "SELECT link FROM invite_links WHERE chat_id=? AND user_id=?", (chat_id, user_id)
            ).fetchone()
        return row[0] if row else None

    def save_invite_link(self, chat_id, user_id, link, display_name):
        with self.transaction() as db:
            db.execute(
                "INSERT OR REPLACE INTO invite_links VALUES(?,?,?,?)",
                (chat_id, user_id, link, display_name[:128]),
            )

    def link_owner(self, chat_id, link):
        """Whose invite link to chat_id `link` is, as (user_id, name), or None."""
        with self.reading() as db:
            row = db.execute(
                "SELECT user_id,display_name FROM invite_links WHERE chat_id=? AND link=?",
                (chat_id, link),
            ).fetchone()
        return tuple(row) if row else None

    def presets(self, raffle_id):
        """Personal weights of people who have not joined, as (user_id, weight)."""
        with self.transaction() as db:
            return [
                (row["user_id"], row["weight"])
                for row in db.execute(
                    "SELECT o.user_id,o.weight FROM overrides o LEFT JOIN participants p "
                    "ON p.raffle_id=o.raffle_id AND p.user_id=o.user_id "
                    "WHERE o.raffle_id=? AND p.user_id IS NULL ORDER BY o.user_id",
                    (raffle_id,),
                )
            ]

    def _entries(self, db, raffle):
        if raffle["invite_via"]:
            return self._invited(db, raffle)
        if raffle["kind"] != "join":
            return self._speakers(db, raffle)
        rid = raffle["id"]
        people = db.execute(
            "SELECT * FROM participants WHERE raffle_id=? ORDER BY user_id", (rid,)
        ).fetchall()
        overrides = dict(
            db.execute("SELECT user_id,weight FROM overrides WHERE raffle_id=?", (rid,))
        )
        designated = {
            row[0]
            for row in db.execute("SELECT user_id FROM designations WHERE raffle_id=?", (rid,))
        }
        bonuses = {}
        for row in db.execute(
            "SELECT g.user_id,g.tag,r.bonus FROM grants g JOIN rules r "
            "ON g.raffle_id=r.raffle_id AND g.tag=r.tag WHERE g.raffle_id=? "
            "ORDER BY g.tag",
            (rid,),
        ):
            bonuses.setdefault(row["user_id"], []).append(
                {"tag": row["tag"], "bonus": row["bonus"]}
            )
        entries = []
        for person in people:
            uid = person["user_id"]
            tags = bonuses.get(uid, [])
            override = overrides.get(uid)
            weight = (
                override
                if override is not None
                else min(
                    raffle["weight_cap"], raffle["default_weight"] + sum(t["bonus"] for t in tags)
                )
            )
            entries.append(
                {
                    "user_id": uid,
                    "display_name": person["display_name"],
                    "weight": weight,
                    "override": override,
                    "tags": tags,
                    "designated": uid in designated,
                }
            )
        return entries

    def _freeze(self, db, raffle, actor):
        if raffle["status"] != "OPEN":
            return raffle
        snapshot = {
            "raffle_id": raffle["id"],
            "title": raffle["title"],
            "winner_count": raffle["winner_count"],
            "deadline": raffle["deadline"],
            "default_weight": raffle["default_weight"],
            "weight_cap": raffle["weight_cap"],
            "rules": [
                dict(r)
                for r in db.execute(
                    "SELECT tag,bonus FROM rules WHERE raffle_id=? ORDER BY tag", (raffle["id"],)
                )
            ],
            "entries": self._entries(db, raffle),
        }
        if raffle["kind"] != "join":
            snapshot |= {
                "kind": raffle["kind"],
                "count_from": raffle["count_from"],
                "min_messages": raffle["min_messages"],
            }
            if raffle["invite_via"]:
                snapshot["invite_via"] = raffle["invite_via"]
        raw = encode(snapshot)
        digest = hashlib.sha256(raw.encode()).hexdigest()
        db.execute(
            "UPDATE raffles SET status='FROZEN',snapshot=?,snapshot_hash=? WHERE id=?",
            (raw, digest, raffle["id"]),
        )
        self.audit(db, raffle["id"], actor, "freeze", {"snapshot_hash": digest})
        return self._get(db, raffle["id"])

    def freeze(self, raffle_id, actor):
        with self.transaction() as db:
            raffle = self._get(db, raffle_id)
            if raffle["status"] == "CANCELLED":
                raise LotteryError("本场抽奖已取消。")
            return self._freeze(db, raffle, actor)

    def view(self, raffle_id):
        with self.transaction() as db:
            return self._view(db, raffle_id)

    def _view(self, db, raffle_id):
        raffle = self._get(db, raffle_id)
        if self.clock() >= raffle["deadline"]:
            raffle = self._freeze(db, raffle, 0)
        if raffle["snapshot"]:
            snapshot = json.loads(raffle["snapshot"])
            raffle["entries"], raffle["rules"] = snapshot["entries"], snapshot["rules"]
        else:
            raffle["entries"] = self._entries(db, raffle)
            raffle["rules"] = [
                dict(r)
                for r in db.execute(
                    "SELECT tag,bonus FROM rules WHERE raffle_id=? ORDER BY tag", (raffle_id,)
                )
            ]
        raffle["weighted"] = bool(raffle["weighted"])
        raffle["prizes"] = json.loads(raffle["prizes"]) if raffle["prizes"] else None
        raffle["result"] = json.loads(raffle["result"]) if raffle["result"] else None
        return raffle

    def draw(self, raffle_id, actor):
        with self.transaction() as db:
            raffle = self._get(db, raffle_id)
            if raffle["result"]:
                return json.loads(raffle["result"])
            if raffle["status"] == "CANCELLED":
                raise LotteryError("本场抽奖已取消。")
            if self._open(raffle) and not self._full(db, raffle):
                raise LotteryError("尚未到截止时间。如需提前开奖，请先 /freeze 截止报名。")
            raffle = self._freeze(db, raffle, actor)
            snapshot = json.loads(raffle["snapshot"])
            if raffle["kind"] == "rank":
                winners = [dict(entry) for entry in snapshot["entries"][: raffle["winner_count"]]]
            else:
                winners = pick_winners(snapshot["entries"], raffle["winner_count"])
            if raffle["prizes"]:
                # The first winners drawn take the first prizes listed.
                names = [name for name, count in json.loads(raffle["prizes"]) for _ in range(count)]
                for winner, name in zip(winners, names, strict=False):
                    winner["prize"] = name
            result = {
                "raffle_id": raffle_id,
                "title": raffle["title"],
                "requested_count": raffle["winner_count"],
                "winners": winners,
                "snapshot_hash": raffle["snapshot_hash"],
                "drawn_at": datetime.fromtimestamp(self.clock(), UTC).isoformat(),
            }
            db.execute(
                "UPDATE raffles SET status='DRAWN',result=? WHERE id=?", (encode(result), raffle_id)
            )
            self.audit(db, raffle_id, actor, "draw", result)
            return result

    def pending_announcements(self):
        """Group-bound raffles whose result is due in their group: drawn, past deadline or full."""
        with self.transaction() as db:
            return [
                (row["id"], row["chat_id"])
                for row in db.execute(
                    "SELECT id,chat_id FROM raffles r WHERE chat_id IS NOT NULL "
                    "AND announced_at IS NULL AND status!='CANCELLED' "
                    "AND (result IS NOT NULL OR deadline<=? OR (target_count IS NOT NULL AND "
                    "(SELECT COUNT(*) FROM participants p WHERE p.raffle_id=r.id)>=target_count)) "
                    "ORDER BY deadline",
                    (self.clock(),),
                )
            ]

    def announcement_due(self, raffle_id):
        """Whether the result has yet to reach the group: neither announced nor cancelled."""
        with self.transaction() as db:
            raffle = self._get(db, raffle_id)
        return raffle["announced_at"] is None and raffle["status"] != "CANCELLED"

    def mark_announced(self, raffle_id, actor, chat_id, error=None):
        """Record that the result reached its group (or never can); False if nothing was due."""
        with self.transaction() as db:
            cursor = db.execute(
                "UPDATE raffles SET announced_at=? WHERE id=? AND chat_id=? "
                "AND result IS NOT NULL AND announced_at IS NULL",
                (self.clock(), raffle_id, chat_id),
            )
            if cursor.rowcount:
                self.audit(db, raffle_id, actor, "announce", {"chat_id": chat_id, "error": error})
            return cursor.rowcount == 1

    def recent(self):
        with self.transaction() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM raffles ORDER BY id DESC LIMIT 20")]
            # Same freeze-on-read rule as view(), applied to every row in one transaction.
            return [r if self._open(r) else self._freeze(db, r, 0) for r in rows]

    def group_summary(self, chat_id):
        with self.transaction() as db:
            counts = dict(
                db.execute(
                    "SELECT status,COUNT(*) FROM raffles WHERE chat_id=? GROUP BY status",
                    (chat_id,),
                ).fetchall()
            )
        return {
            "active": counts.get("OPEN", 0) + counts.get("FROZEN", 0),
            "drawn": counts.get("DRAWN", 0),
            "cancelled": counts.get("CANCELLED", 0),
        }

    def group_raffles(self, chat_id, page, size):
        """One page of a group's raffles, newest first, and whether another page follows."""
        with self.transaction() as db:
            rows = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM raffles WHERE chat_id=? ORDER BY id DESC LIMIT ? OFFSET ?",
                    (chat_id, size + 1, page * size),
                )
            ]
            shown = [r if self._open(r) else self._freeze(db, r, 0) for r in rows[:size]]
            return shown, len(rows) > size

    def remember_group(self, chat_id, title, active=True):
        with self.transaction() as db:
            db.execute(
                "INSERT INTO groups(chat_id,title,active,updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(chat_id) DO UPDATE SET "
                "title=excluded.title,active=excluded.active,updated_at=excluded.updated_at",
                (chat_id, title, int(active), self.clock()),
            )

    def deactivate_group(self, chat_id):
        """The bot turns out not to be in chat_id any more, without having been told."""
        with self.transaction() as db:
            db.execute(
                "UPDATE groups SET active=0,updated_at=? WHERE chat_id=?", (self.clock(), chat_id)
            )

    def groups(self):
        """Groups the bot is currently in, as (chat_id, title)."""
        with self.transaction() as db:
            return [
                (r["chat_id"], r["title"])
                for r in db.execute(
                    "SELECT chat_id,title FROM groups WHERE active=1 ORDER BY title"
                )
            ]

    def set_manager(self, chat_id, user_id, manages):
        """Remember whether Telegram last said user_id manages chat_id."""
        with self.transaction() as db:
            if manages:
                db.execute("INSERT OR IGNORE INTO managers VALUES(?,?)", (chat_id, user_id))
            else:
                db.execute("DELETE FROM managers WHERE chat_id=? AND user_id=?", (chat_id, user_id))

    def set_admins(self, chat_id, user_ids):
        """Replace what is known about chat_id's managers with Telegram's list of admins."""
        with self.transaction() as db:
            db.execute("DELETE FROM managers WHERE chat_id=?", (chat_id,))
            db.executemany(
                "INSERT OR IGNORE INTO managers VALUES(?,?)", [(chat_id, u) for u in user_ids]
            )

    def managed_groups(self, user_id):
        """Groups the bot is in that user_id was last seen managing, as (chat_id, title)."""
        with self.transaction() as db:
            return [
                (r["chat_id"], r["title"])
                for r in db.execute(
                    "SELECT g.chat_id,g.title FROM groups g JOIN managers m "
                    "ON m.chat_id=g.chat_id WHERE m.user_id=? AND g.active=1 ORDER BY g.title",
                    (user_id,),
                )
            ]

    def group_settings(self, chat_id):
        settings = self._settings.get(chat_id)
        if settings is None:
            with self.reading() as db:
                row = db.execute(
                    "SELECT settings FROM groups WHERE chat_id=?", (chat_id,)
                ).fetchone()
            stored = json.loads(row["settings"]) if row and row["settings"] else {}
            settings = {key: stored.get(key, value) for key, value in GROUP_DEFAULTS.items()}
            self._settings[chat_id] = settings
        return dict(settings)

    def set_group_setting(self, chat_id, key, value, actor=0, actor_name=""):
        """Change one of a group's settings, recording who did; False if it was so already."""
        if key not in GROUP_DEFAULTS:
            raise LotteryError("没有这个设置。")
        if key in SETTING_LIMITS:
            integer(value, "设置值", *SETTING_LIMITS[key])
        if key in ("delete_keyword", "delete_notices") and value is not None:
            integer(value, "删除时间（秒）", 0, DELETE_WINDOW)
        with self.transaction() as db:
            row = db.execute("SELECT settings FROM groups WHERE chat_id=?", (chat_id,)).fetchone()
            if row is None:
                raise LotteryError("机器人还不在这个群里。")
            stored = json.loads(row["settings"]) if row["settings"] else {}
            before = stored.get(key, GROUP_DEFAULTS[key])
            if before == value:
                return False
            stored[key] = value
            db.execute("UPDATE groups SET settings=? WHERE chat_id=?", (encode(stored), chat_id))
            db.execute(
                "INSERT INTO setting_changes(chat_id,key,before,after,actor_id,actor_name,at) "
                "VALUES(?,?,?,?,?,?,?)",
                (chat_id, key, encode(before), encode(value), actor, actor_name, self.clock()),
            )
        self._settings.pop(chat_id, None)
        return True

    def setting_changes(self, chat_id, keys=None, limit=None):
        """Changes to chat_id's settings (those in `keys`, if given), newest first."""
        with self.reading() as db:
            rows = db.execute(
                "SELECT key,before,after,actor_id,actor_name,at FROM setting_changes "
                "WHERE chat_id=? ORDER BY id DESC",
                (chat_id,),
            ).fetchall()
        changes = [
            dict(row) | {"before": json.loads(row["before"]), "after": json.loads(row["after"])}
            for row in rows
            if keys is None or row["key"] in keys
        ]
        return changes[:limit]

    # 灵石: points members earn in each group, kept apart per group. Every change goes into
    # the ledger with the balance after it. `day` is the local date, as "2027-01-15".

    def _holder(self, db, chat_id, user_id, display_name=None):
        """user_id's 灵石 row in chat_id, made at 0 if new; the name is updated when given,
        and a new row without one takes the name they last wrote under."""
        db.execute(
            "INSERT OR IGNORE INTO points(chat_id,user_id,display_name) VALUES(?,?,?)",
            (chat_id, user_id, display_name or self._speaker_name(db, chat_id, user_id)),
        )
        if display_name:
            db.execute(
                "UPDATE points SET display_name=? WHERE chat_id=? AND user_id=?",
                (display_name[:128], chat_id, user_id),
            )
        return db.execute(
            "SELECT * FROM points WHERE chat_id=? AND user_id=?", (chat_id, user_id)
        ).fetchone()

    def _credit(self, db, chat_id, user_id, delta, reason, actor, raffle_id=None):
        """Add delta to a balance and write it in the ledger, with the raffle it was paid
        into or given back from; returns the new balance."""
        db.execute(
            "UPDATE points SET balance=balance+? WHERE chat_id=? AND user_id=?",
            (delta, chat_id, user_id),
        )
        balance = db.execute(
            "SELECT balance FROM points WHERE chat_id=? AND user_id=?", (chat_id, user_id)
        ).fetchone()[0]
        db.execute(
            "INSERT INTO ledger(chat_id,user_id,delta,balance,reason,actor_id,at,raffle_id) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (chat_id, user_id, delta, balance, reason, actor, self.clock(), raffle_id),
        )
        return balance

    def check_in(self, chat_id, user_id, display_name, day, amount):
        """Give `amount` for checking in on `day`: {"amount", "balance", "place"}, the place
        being how many checked in that day so far; None if user_id already did."""
        with self.transaction() as db:
            if self._holder(db, chat_id, user_id, display_name)["checkin_day"] == day:
                return None
            db.execute(
                "UPDATE points SET checkin_day=? WHERE chat_id=? AND user_id=?",
                (day, chat_id, user_id),
            )
            balance = self._credit(db, chat_id, user_id, amount, "checkin", 0)
            place = db.execute(
                "SELECT COUNT(*) FROM points WHERE chat_id=? AND checkin_day=?", (chat_id, day)
            ).fetchone()[0]
            return {"amount": amount, "balance": balance, "place": place}

    def reward_message(
        self,
        chat_id,
        user_id,
        display_name,
        day,
        *,
        amount,
        limit,
        crit_percent=0,
        crit_times=2,
        randbelow=secrets.randbelow,
    ):
        """Reward speaking with `amount`, or crit_times as much with a chance of crit_percent
        in 100, at most `limit` times on `day` (0: no limit): {"amount", "crit", "balance",
        "rewards"}, rewards counting those of the day; None once the limit is reached."""
        with self.transaction() as db:
            row = self._holder(db, chat_id, user_id, display_name)
            rewards = row["rewards"] if row["reward_day"] == day else 0
            if limit and rewards >= limit:
                return None
            crit = randbelow(100) < crit_percent
            amount *= crit_times if crit else 1
            db.execute(
                "UPDATE points SET reward_day=?,rewards=? WHERE chat_id=? AND user_id=?",
                (day, rewards + 1, chat_id, user_id),
            )
            balance = self._credit(db, chat_id, user_id, amount, "crit" if crit else "message", 0)
            return {"amount": amount, "crit": crit, "balance": balance, "rewards": rewards + 1}

    def wallet(self, chat_id, user_id, day):
        """user_id's 灵石 in chat_id, whether they checked in on `day` and the message
        rewards they took that day."""
        with self.reading() as db:
            row = db.execute(
                "SELECT * FROM points WHERE chat_id=? AND user_id=?", (chat_id, user_id)
            ).fetchone()
        if row is None:
            return {"balance": 0, "checked_in": False, "rewards": 0}
        return {
            "balance": row["balance"],
            "checked_in": row["checkin_day"] == day,
            "rewards": row["rewards"] if row["reward_day"] == day else 0,
        }

    def holder(self, chat_id, user_id):
        """user_id's name and balance in chat_id; the name is empty if never seen."""
        with self.reading() as db:
            row = db.execute(
                "SELECT display_name,balance FROM points WHERE chat_id=? AND user_id=?",
                (chat_id, user_id),
            ).fetchone()
            if row:
                return dict(row)
            return {"display_name": self._speaker_name(db, chat_id, user_id), "balance": 0}

    def _speaker_name(self, db, chat_id, user_id):
        row = db.execute(
            "SELECT display_name FROM speakers WHERE chat_id=? AND user_id=?", (chat_id, user_id)
        ).fetchone()
        return row[0] if row else ""

    def holders(self, chat_id, page, size):
        """One page of chat_id's members with 灵石, the richest first, and whether another
        page follows."""
        with self.reading() as db:
            rows = [
                dict(r)
                for r in db.execute(
                    "SELECT user_id,display_name,balance FROM points WHERE chat_id=? "
                    "AND balance>0 ORDER BY balance DESC,user_id LIMIT ? OFFSET ?",
                    (chat_id, size + 1, page * size),
                )
            ]
        return rows[:size], len(rows) > size

    def standing(self, chat_id, user_id):
        """user_id's place among chat_id's members with 灵石 and their balance, or None."""
        with self.reading() as db:
            row = db.execute(
                "SELECT balance FROM points WHERE chat_id=? AND user_id=?", (chat_id, user_id)
            ).fetchone()
            if row is None or row["balance"] <= 0:
                return None
            balance = row["balance"]
            ahead = db.execute(
                "SELECT COUNT(*) FROM points WHERE chat_id=? "
                "AND (balance>? OR (balance=? AND user_id<?))",
                (chat_id, balance, balance, user_id),
            ).fetchone()[0]
        return {"place": ahead + 1, "balance": balance}

    def find_members(self, chat_id, text, limit=10):
        """Members of chat_id the bot knows, from their messages or 灵石, whose name has
        `text` in it (Latin letter case aside), the richest first: (rows, whether more)."""
        self.flush_activity()  # names seen in the last few seconds
        with self.reading() as db:
            found = {}
            for table in ("speakers", "points"):  # the name in points is the newer
                for row in db.execute(
                    f"SELECT user_id,display_name FROM {table} "
                    "WHERE chat_id=? AND instr(lower(display_name),lower(?))>0",
                    (chat_id, text),
                ):
                    found[row["user_id"]] = row["display_name"]
            balances = dict(
                db.execute("SELECT user_id,balance FROM points WHERE chat_id=?", (chat_id,))
            )
        rows = [
            {"user_id": uid, "display_name": name, "balance": balances.get(uid, 0)}
            for uid, name in found.items()
        ]
        rows.sort(key=lambda r: (-r["balance"], r["display_name"], r["user_id"]))
        return rows[:limit], len(rows) > limit

    def adjust_points(self, chat_id, actor, user_id, delta, display_name=None):
        """A super admin adds delta 灵石 to user_id's balance (takes it away if negative);
        returns the new balance. The name, if given, is the one the member now goes by."""
        integer(user_id, "用户 ID", 1, 2**63 - 1)
        integer(delta, "加减的数量", -1_000_000_000, 1_000_000_000)
        if not delta:
            raise LotteryError("加减的数量不能为 0。")
        with self.transaction() as db:
            balance = self._holder(db, chat_id, user_id, display_name)["balance"]
            if balance + delta < 0:
                raise LotteryError(f"余额不足：当前 {balance} 灵石，最多扣 {balance}。")
            return self._credit(db, chat_id, user_id, delta, "adjust", actor)

    def export_points(self, chat_id):
        """chat_id's balances, its whole ledger and the changes to its settings."""
        title = self.group_title(chat_id)
        with self.reading() as db:
            balances = [
                dict(r)
                for r in db.execute(
                    "SELECT user_id,display_name,balance FROM points WHERE chat_id=? "
                    "ORDER BY balance DESC,user_id",
                    (chat_id,),
                )
            ]
            ledger = [
                dict(r)
                for r in db.execute(
                    "SELECT id,user_id,delta,balance,reason,raffle_id,actor_id,at FROM ledger "
                    "WHERE chat_id=? ORDER BY id",
                    (chat_id,),
                )
            ]
        return {
            "chat_id": chat_id,
            "title": title,
            "balances": balances,
            "ledger": ledger,
            "setting_changes": self.setting_changes(chat_id),
        }

    def restorable_panel(self, chat_id, cards_pinned):
        """chat_id's 灵石 panel, or None if it has none or, with cards_pinned, the card of a
        raffle not drawn yet is still pinned there."""
        with self.reading() as db:
            row = db.execute(
                "SELECT points_panel FROM groups WHERE chat_id=?", (chat_id,)
            ).fetchone()
            busy = (
                cards_pinned
                and db.execute(
                    "SELECT 1 FROM raffles WHERE chat_id=? AND status IN ('OPEN','FROZEN') "
                    "AND card_message_id IS NOT NULL",
                    (chat_id,),
                ).fetchone()
            )
        return None if row is None or busy else row["points_panel"]

    def swap_points_panel(self, chat_id, message_id):
        """Remember the 灵石 panel just posted in chat_id; returns the one posted before."""
        with self.transaction() as db:
            row = db.execute(
                "SELECT points_panel FROM groups WHERE chat_id=?", (chat_id,)
            ).fetchone()
            db.execute("UPDATE groups SET points_panel=? WHERE chat_id=?", (message_id, chat_id))
        return row["points_panel"] if row else None

    def swap_pinned_result(self, chat_id, message_id):
        """Remember the result just pinned in chat_id; returns the one pinned before."""
        with self.transaction() as db:
            row = db.execute(
                "SELECT pinned_result FROM groups WHERE chat_id=?", (chat_id,)
            ).fetchone()
            db.execute("UPDATE groups SET pinned_result=? WHERE chat_id=?", (message_id, chat_id))
        return row["pinned_result"] if row else None

    def schedule_deletions(self, chat_id, message_ids, due_at):
        with self.transaction() as db:
            db.executemany(
                "INSERT OR REPLACE INTO deletions VALUES(?,?,?)",
                [(chat_id, message_id, due_at) for message_id in message_ids],
            )

    def due_deletions(self, limit=100):
        """Messages due for deletion as {chat_id: [message_id, ...]}, at most `limit` per
        chat (Telegram's batch size). Entries too old to delete any more are dropped."""
        now = self.clock()
        with self.transaction() as db:
            db.execute("DELETE FROM deletions WHERE due_at<?", (now - DELETE_WINDOW,))
            due = {}
            for row in db.execute(
                "SELECT chat_id,message_id FROM deletions WHERE due_at<=? ORDER BY due_at",
                (now,),
            ):
                ids = due.setdefault(row["chat_id"], [])
                if len(ids) < limit:
                    ids.append(row["message_id"])
            return due

    def drop_deletions(self, chat_id, message_ids):
        with self.transaction() as db:
            db.executemany(
                "DELETE FROM deletions WHERE chat_id=? AND message_id=?",
                [(chat_id, message_id) for message_id in message_ids],
            )

    def group_title(self, chat_id):
        with self.transaction() as db:
            row = db.execute("SELECT title FROM groups WHERE chat_id=?", (chat_id,)).fetchone()
        return row["title"] if row else str(chat_id)

    def save_draft(self, user_id, chat_id, data):
        with self.transaction() as db:
            db.execute(
                "INSERT INTO drafts VALUES(?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
                "chat_id=excluded.chat_id,data=excluded.data,updated_at=excluded.updated_at",
                (user_id, chat_id, encode(data), self.clock()),
            )

    def draft(self, user_id):
        """The user's unfinished creation wizard as (chat_id, data), or None."""
        with self.transaction() as db:
            row = db.execute(
                "SELECT chat_id,data FROM drafts WHERE user_id=?", (user_id,)
            ).fetchone()
        return (row["chat_id"], json.loads(row["data"])) if row else None

    def drop_draft(self, user_id):
        with self.transaction() as db:
            db.execute("DELETE FROM drafts WHERE user_id=?", (user_id,))

    def export(self, raffle_id):
        with self.transaction() as db:
            raffle = self._view(db, raffle_id)
            audit = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM audit WHERE raffle_id=? ORDER BY id", (raffle_id,)
                )
            ]
        return {"raffle": raffle, "audit": audit}

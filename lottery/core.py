"""Transactional storage and integer-weighted sampling without replacement."""

import hashlib
import json
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path


class LotteryError(ValueError):
    pass


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
]
MAX_PRIZES = 10
MAX_KEYWORD = 32


class Store:
    def __init__(self, path, clock=time.time):
        self.path = str(path)
        self.clock = clock
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
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
        target = raffle["target_count"]
        return (
            target is not None
            and db.execute(
                "SELECT COUNT(*) FROM participants WHERE raffle_id=?", (raffle["id"],)
            ).fetchone()[0]
            >= target
        )

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
        """Follow Telegram's group-to-supergroup upgrade, which changes the chat ID."""
        with self.transaction() as db:
            ids = [
                row["id"]
                for row in db.execute("SELECT id FROM raffles WHERE chat_id=?", (old_chat_id,))
            ]
            db.execute("UPDATE raffles SET chat_id=? WHERE chat_id=?", (new_chat_id, old_chat_id))
            db.execute(
                "UPDATE OR REPLACE groups SET chat_id=? WHERE chat_id=?", (new_chat_id, old_chat_id)
            )
            db.execute("UPDATE drafts SET chat_id=? WHERE chat_id=?", (new_chat_id, old_chat_id))
            for raffle_id in ids:
                self.audit(
                    db, raffle_id, 0, "migrate_chat", {"before": old_chat_id, "after": new_chat_id}
                )

    def leave_group(self, chat_id, user_id, left_at):
        """Cancel user_id's joins in raffles bound to chat_id that were still open at left_at.

        Frozen raffles are untouched: their snapshot is the fixed list of the draw.
        """
        with self.transaction() as db:
            ids = [
                row["id"]
                for row in db.execute(
                    "SELECT r.id FROM raffles r JOIN participants p ON p.raffle_id=r.id "
                    "WHERE r.chat_id=? AND p.user_id=? AND r.status='OPEN' AND r.deadline>?",
                    (chat_id, user_id, left_at),
                )
            ]
            for raffle_id in ids:
                db.execute(
                    "DELETE FROM participants WHERE raffle_id=? AND user_id=?", (raffle_id, user_id)
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
    ):
        """Create a raffle ending after `minutes` or at `deadline`, or earlier once `target`
        people have joined. Menus create it already bound to `chat_id`; `weighted` shows the
        bonus notice on the card from the start, before any weight is set. `prizes` are
        handed out in draw order and must add up to `winner_count`; with a `keyword`, people
        join by sending it in the group."""
        integer(winner_count, "中奖名额", 1, 100)
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
            cursor = db.execute(
                "INSERT INTO raffles(title,winner_count,deadline,created_by,chat_id,"
                "target_count,weighted,prizes,keyword) VALUES(?,?,?,?,?,?,?,?,?)",
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
                ),
            )
            raffle_id = cursor.lastrowid
            self.audit(
                db,
                raffle_id,
                actor,
                "create",
                {
                    "title": title,
                    "deadline": deadline,
                    "winner_count": winner_count,
                    "chat_id": chat_id,
                    "target": target,
                    "weighted": weighted,
                    "prizes": prizes,
                    "keyword": keyword,
                },
            )
            return raffle_id

    def join(self, raffle_id, user_id, display_name):
        integer(user_id, "用户 ID", 1, 2**63 - 1)
        with self.transaction() as db:
            raffle = self._editable(db, raffle_id)
            if db.execute(
                "SELECT 1 FROM participants WHERE raffle_id=? AND user_id=?", (raffle_id, user_id)
            ).fetchone():
                return False
            if self._full(db, raffle):
                raise LotteryError("名额已满，即将开奖。")
            db.execute(
                "INSERT INTO participants VALUES(?,?,?)", (raffle_id, user_id, display_name[:128])
            )
            return True

    def keyword_raffles(self, chat_id, text):
        """Open raffles of chat_id that people join by sending `text` (any letter case)."""
        with self.transaction() as db:
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
        """Call off a raffle that has not been drawn; False if it already was cancelled."""
        with self.transaction() as db:
            raffle = self._get(db, raffle_id)
            if raffle["status"] == "DRAWN":
                raise LotteryError("本场抽奖已开奖，不能取消。")
            if raffle["status"] == "CANCELLED":
                return False
            db.execute("UPDATE raffles SET status='CANCELLED' WHERE id=?", (raffle_id,))
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
            before = self._editable(db, raffle_id)
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

    def rule(self, raffle_id, actor, tag, bonus):
        if not re.fullmatch(r"[A-Za-z0-9_\-]{1,32}", tag):
            raise LotteryError("规则名限 1～32 位字母、数字、下划线和短横线。")
        integer(bonus, "加成")
        with self.transaction() as db:
            self._editable(db, raffle_id)
            before = db.execute(
                "SELECT bonus FROM rules WHERE raffle_id=? AND tag=?", (raffle_id, tag)
            ).fetchone()
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

    def grant(self, raffle_id, actor, user_id, tag, enabled=True):
        integer(user_id, "用户 ID", 1, 2**63 - 1)
        with self.transaction() as db:
            self._editable(db, raffle_id)
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
            raffle = self._editable(db, raffle_id)
            if weight is not None:
                integer(weight, "个人权重", 0, raffle["weight_cap"])
            before = db.execute(
                "SELECT weight FROM overrides WHERE raffle_id=? AND user_id=?", (raffle_id, user_id)
            ).fetchone()
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

    def _mark_weighted(self, db, raffle_id):
        db.execute("UPDATE raffles SET weighted=1 WHERE id=?", (raffle_id,))

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
        rid = raffle["id"]
        people = db.execute(
            "SELECT * FROM participants WHERE raffle_id=? ORDER BY user_id", (rid,)
        ).fetchall()
        overrides = dict(
            db.execute("SELECT user_id,weight FROM overrides WHERE raffle_id=?", (rid,))
        )
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
            return self._freeze(db, self._get(db, raffle_id), actor)

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
            winners = weighted_draw(snapshot["entries"], raffle["winner_count"])
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
                "INSERT INTO groups VALUES(?,?,?,?) ON CONFLICT(chat_id) DO UPDATE SET "
                "title=excluded.title,active=excluded.active,updated_at=excluded.updated_at",
                (chat_id, title, int(active), self.clock()),
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

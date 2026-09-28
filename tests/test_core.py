import hashlib
import json
import sqlite3
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import pytest

from lottery.core import MIGRATIONS, LotteryError, Store, weighted_draw


@pytest.fixture
def setup(tmp_path):
    now = [1_800_000_000.0]
    store = Store(tmp_path / "lottery.sqlite3", clock=lambda: now[0])
    rid = store.create(99, "测试抽奖", 3, 60)
    return store, rid, now


def test_exact_weighted_intervals():
    entries = [{"user_id": i, "weight": w} for i, w in enumerate([1, 3, 6, 0])]
    outcomes = [weighted_draw(entries, 1, lambda total, t=t: t)[0]["user_id"] for t in range(10)]
    assert Counter(outcomes) == {0: 1, 1: 3, 2: 6}


def test_without_replacement_and_zero_weight():
    entries = [{"user_id": i, "weight": w} for i, w in enumerate([0, 1, 100, 0, 3])]
    totals = []

    def choose_last(total):
        totals.append(total)
        return total - 1

    winners = weighted_draw(entries, 10, choose_last)
    assert [p["user_id"] for p in winners] == [4, 2, 1]
    assert totals == [104, 101, 1]
    assert len(entries) == 5


@pytest.mark.parametrize("weight", [-1, 1.5, True, 1_000_001])
def test_invalid_weights(weight):
    with pytest.raises(LotteryError):
        weighted_draw([{"user_id": 1, "weight": weight}], 1)


def test_duplicate_candidates_rejected():
    with pytest.raises(LotteryError):
        weighted_draw([{"user_id": 1, "weight": 1}, {"user_id": 1, "weight": 2}], 1)


def test_rules_override_and_restore(setup):
    store, rid, _ = setup
    store.configure(rid, 99, 1, 5)
    store.rule(rid, 99, "vip", 2)
    store.rule(rid, 99, "task", 4)
    assert store.grant(rid, 99, 123, "vip")
    assert not store.grant(rid, 99, 123, "vip")
    assert store.grant(rid, 99, 123, "task")
    assert store.join(rid, 123, "Alice")
    assert not store.join(rid, 123, "Renamed")
    assert store.view(rid)["entries"][0]["weight"] == 5
    store.override(rid, 99, 123, 0)
    assert store.view(rid)["entries"][0]["weight"] == 0
    store.override(rid, 99, 123, None)
    assert store.grant(rid, 99, 123, "task", False)
    assert not store.grant(rid, 99, 123, "task", False)
    assert store.view(rid)["entries"][0]["weight"] == 3
    store.rule(rid, 99, "vip", 3)
    assert store.view(rid)["entries"][0]["weight"] == 4
    actions = [e["action"] for e in store.export(rid)["audit"]]
    assert (actions.count("grant"), actions.count("revoke")) == (2, 1)


def test_override_before_join_and_cap_validation(setup):
    store, rid, _ = setup
    store.override(rid, 99, 123, 10)
    store.join(rid, 123, "Alice")
    assert store.view(rid)["entries"][0]["weight"] == 10
    with pytest.raises(LotteryError):
        store.configure(rid, 99, 1, 5)
    with pytest.raises(LotteryError):
        store.override(rid, 99, 124, 101)
    assert store.view(rid)["weight_cap"] == 100


@pytest.mark.parametrize(
    "operation",
    [
        lambda s, r: s.join(r, 123, "late"),
        lambda s, r: s.configure(r, 99, 2, 100),
        lambda s, r: s.rule(r, 99, "vip", 2),
        lambda s, r: s.grant(r, 99, 123, "vip"),
        lambda s, r: s.grant(r, 99, 123, "vip", False),
        lambda s, r: s.override(r, 99, 123, 10),
        lambda s, r: s.override(r, 99, 123, None),
    ],
)
@pytest.mark.parametrize("cutoff", ["deadline", "manual"])
def test_all_changes_blocked_after_cutoff(setup, operation, cutoff):
    store, rid, now = setup
    store.rule(rid, 99, "vip", 2)
    if cutoff == "deadline":
        now[0] += 3600
    else:
        store.freeze(rid, 99)
    with pytest.raises(LotteryError):
        operation(store, rid)


def test_freeze_snapshot_hash_and_result_survive_restart(setup):
    store, rid, now = setup
    for uid in range(1, 6):
        store.join(rid, uid, f"user-{uid}")
    with pytest.raises(LotteryError):
        store.draw(rid, 99)
    store.freeze(rid, 99)
    frozen = store.view(rid)
    assert hashlib.sha256(frozen["snapshot"].encode()).hexdigest() == frozen["snapshot_hash"]
    result = store.draw(rid, 99)
    restarted = Store(store.path, clock=lambda: now[0])
    assert restarted.draw(rid, 100) == result
    assert len({p["user_id"] for p in result["winners"]}) == 3
    assert result["snapshot_hash"] == frozen["snapshot_hash"]
    actions = [e["action"] for e in restarted.export(rid)["audit"]]
    assert actions.count("freeze") == 1
    assert actions.count("draw") == 1


def test_deadline_freezes_on_read(setup):
    store, rid, now = setup
    store.join(rid, 1, "one")
    now[0] += 3600
    assert store.view(rid)["status"] == "FROZEN"
    assert len(store.draw(rid, 99)["winners"]) == 1


def test_parallel_draws_commit_exactly_one_result(setup):
    store, rid, _ = setup
    for uid in range(1, 21):
        store.join(rid, uid, str(uid))
    store.freeze(rid, 99)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda actor: store.draw(rid, actor), range(100, 116)))
    assert all(result == results[0] for result in results)
    assert sum(e["action"] == "draw" for e in store.export(rid)["audit"]) == 1


def test_parallel_duplicate_registration(setup):
    store, rid, _ = setup
    with ThreadPoolExecutor(max_workers=8) as pool:
        joined = list(pool.map(lambda _: store.join(rid, 123, "Alice"), range(16)))
    assert sum(joined) == 1
    assert len(store.view(rid)["entries"]) == 1


def test_all_zero_and_empty_draw_persist(setup):
    store, rid, _ = setup
    store.join(rid, 123, "Alice")
    store.override(rid, 99, 123, 0)
    store.freeze(rid, 99)
    result = store.draw(rid, 99)
    assert result["winners"] == []
    assert store.draw(rid, 99) == result
    empty = store.create(99, "empty", 1, 1)
    store.freeze(empty, 99)
    assert store.draw(empty, 99)["winners"] == []


def test_audit_old_and_new_values(setup):
    store, rid, _ = setup
    store.override(rid, 99, 123, 10)
    store.override(rid, 99, 123, 0)
    entry = store.export(rid)["audit"][-1]
    assert entry["actor_id"] == 99
    assert json.loads(entry["details"]) == {"user_id": 123, "before": 10, "after": 0}


def test_random_failure_rolls_back_draw(setup, monkeypatch):
    store, rid, now = setup
    store.join(rid, 123, "Alice")
    now[0] += 3600

    def fail(*args):
        raise RuntimeError("rng unavailable")

    monkeypatch.setattr("lottery.core.weighted_draw", fail)
    with pytest.raises(RuntimeError):
        store.draw(rid, 99)
    with store.transaction() as db:
        row = db.execute("SELECT status,result,snapshot FROM raffles WHERE id=?", (rid,)).fetchone()
        assert tuple(row) == ("OPEN", None, None)
        assert db.execute("SELECT COUNT(*) FROM audit WHERE action='draw'").fetchone()[0] == 0


def test_group_binding_rules(setup):
    store, rid, now = setup
    with pytest.raises(LotteryError, match="尚未在群里发布"):
        store.target_chat(rid)
    store.bind(rid, 99, -100)
    store.bind(rid, 99, -100)
    with pytest.raises(LotteryError, match="其他群"):
        store.bind(rid, 99, -200)
    store.migrate_chat(-100, -1001)
    assert store.target_chat(rid) == -1001
    actions = [e["action"] for e in store.export(rid)["audit"]]
    assert actions.count("bind") == 1
    assert actions.count("migrate_chat") == 1
    now[0] += 3600
    with pytest.raises(LotteryError, match="截止"):
        store.target_chat(rid)
    store.bind(rid, 99, -200)  # closed raffles may be shown anywhere; binding stays
    assert store.view(rid)["chat_id"] == -1001


def test_database_from_before_group_binding_is_upgraded(tmp_path):
    path = tmp_path / "old.sqlite3"
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE raffles (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, "
        "winner_count INTEGER NOT NULL, deadline REAL NOT NULL, "
        "default_weight INTEGER NOT NULL DEFAULT 1, weight_cap INTEGER NOT NULL DEFAULT 100, "
        "status TEXT NOT NULL DEFAULT 'OPEN', created_by INTEGER NOT NULL, snapshot TEXT, "
        "snapshot_hash TEXT, result TEXT)"
    )
    db.execute("INSERT INTO raffles(title,winner_count,deadline,created_by) VALUES('old',1,9e9,99)")
    db.commit()
    db.close()
    store = Store(path)
    assert store.view(1)["chat_id"] is None
    store.bind(1, 99, -100)
    assert store.target_chat(1) == -100
    with store.transaction() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='audit_by_raffle'").fetchone()


def test_failed_migration_changes_nothing(tmp_path, monkeypatch):
    path = tmp_path / "lottery.sqlite3"
    Store(path)
    broken = [*MIGRATIONS, "CREATE TABLE extra (x); SELECT * FROM missing"]
    monkeypatch.setattr("lottery.core.MIGRATIONS", broken)
    with pytest.raises(sqlite3.OperationalError):
        Store(path)
    db = sqlite3.connect(path)
    assert db.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
    assert db.execute("SELECT 1 FROM sqlite_master WHERE name='extra'").fetchone() is None
    db.close()


def test_recent_freezes_expired_raffles_in_one_pass(setup):
    store, rid, now = setup
    later = store.create(99, "later", 1, 120)
    now[0] += 3600
    assert {r["id"]: r["status"] for r in store.recent()} == {rid: "FROZEN", later: "OPEN"}
    assert store.view(rid)["snapshot_hash"]


def test_leaving_group_cancels_joins_only_in_open_raffles_of_that_group(setup):
    store, rid, _ = setup
    other = store.create(99, "other group", 1, 60)
    frozen = store.create(99, "frozen", 1, 60)
    for raffle_id, chat_id in ((rid, -100), (other, -200), (frozen, -100)):
        store.bind(raffle_id, 99, chat_id)
        store.join(raffle_id, 123, "Alice")
        store.join(raffle_id, 456, "Bob")
    store.freeze(frozen, 99)
    assert store.leave_group(-100, 123, store.clock()) == [rid]
    assert [e["user_id"] for e in store.view(rid)["entries"]] == [456]
    assert len(store.view(other)["entries"]) == 2
    assert len(store.view(frozen)["entries"]) == 2  # the frozen snapshot is final
    entry = store.export(rid)["audit"][-1]
    assert (entry["action"], entry["actor_id"]) == ("leave", 0)
    assert store.leave_group(-100, 123, store.clock()) == []


def test_leave_is_judged_by_when_it_happened(setup):
    store, rid, now = setup
    deadline = now[0] + 3600
    store.bind(rid, 99, -100)
    for uid in (1, 2):
        store.join(rid, uid, str(uid))
    now[0] = deadline + 60  # the bot restarts after the deadline and replays old updates
    assert store.leave_group(-100, 1, deadline - 1) == [rid]
    assert store.leave_group(-100, 2, deadline + 1) == []
    assert [e["user_id"] for e in store.view(rid)["entries"]] == [2]


def test_auto_draw_bookkeeping(setup):
    store, rid, now = setup
    store.create(99, "never published", 1, 60)
    store.bind(rid, 99, -100)
    assert store.pending_announcements() == []
    now[0] += 3600
    assert store.pending_announcements() == [(rid, -100)]  # unpublished raffles are skipped
    assert not store.mark_announced(rid, 0, -100)  # nothing drawn yet
    store.draw(rid, 0)
    assert not store.mark_announced(rid, 0, -200)  # not the raffle's group
    assert store.mark_announced(rid, 0, -100)
    assert not store.mark_announced(rid, 0, -100)
    assert store.pending_announcements() == []
    assert store.export(rid)["audit"][-1]["action"] == "announce"


def test_upgrade_does_not_announce_raffles_already_past(tmp_path):
    path = tmp_path / "v3.sqlite3"
    db = sqlite3.connect(path)
    for script in MIGRATIONS[:3]:
        for statement in filter(str.strip, script.split(";")):
            db.execute(statement)
    db.execute("PRAGMA user_version = 3")
    for title, deadline, result in [
        ("drawn", 1.0, "{}"),
        ("expired", 1.0, None),
        ("future", 9e9, None),
    ]:
        db.execute(
            "INSERT INTO raffles(title,winner_count,deadline,created_by,chat_id,result) "
            "VALUES(?,1,?,99,-100,?)",
            (title, deadline, result),
        )
    db.commit()
    db.close()
    store = Store(path, clock=lambda: 9e9 + 1)
    assert store.pending_announcements() == [(3, -100)]


def test_raffles_isolated(setup):
    store, rid, _ = setup
    other = store.create(99, "other", 1, 60)
    store.override(rid, 99, 123, 0)
    store.join(other, 123, "Alice")
    assert store.view(other)["entries"][0]["weight"] == 1


def test_raffle_ends_once_full(setup):
    store, _, now = setup
    rid = store.create(99, "满人", 1, deadline=now[0] + 7200, chat_id=-100, target=2)
    store.join(rid, 1, "one")
    with pytest.raises(LotteryError, match="尚未到截止时间"):
        store.draw(rid, 0)
    assert store.pending_announcements() == []
    store.join(rid, 2, "two")
    assert store.is_full(rid)
    assert not store.join(rid, 2, "two")  # joining twice is still just "already joined"
    with pytest.raises(LotteryError, match="名额已满"):
        store.join(rid, 3, "three")
    assert store.pending_announcements() == [(rid, -100)]
    assert len(store.draw(rid, 0)["winners"]) == 1


@pytest.mark.parametrize(
    "options, message",
    [
        ({"minutes": 60, "target": 1}, "满人开奖人数"),  # fewer people than winners
        ({"deadline": 1_800_000_030.0}, "1 分钟"),  # too soon
        ({"minutes": 60, "prizes": [["a", 1]]}, "奖品份数之和"),  # 1 prize, 2 winners
        ({"minutes": 60, "prizes": []}, "奖品需为"),
        ({"minutes": 60, "prizes": [[" ", 2]]}, "奖品名称"),
        ({"minutes": 60, "keyword": "/start"}, "口令"),
        ({"minutes": 60, "keyword": "x" * 33}, "口令"),
    ],
)
def test_create_validation(setup, options, message):
    store, _, _ = setup
    with pytest.raises(LotteryError, match=message):
        store.create(99, "x", 2, **options)


def test_cancel(setup):
    store, rid, now = setup
    store.bind(rid, 99, -100)
    store.join(rid, 1, "one")
    assert store.cancel(rid, 99)
    assert not store.cancel(rid, 99)
    for operation in (lambda: store.join(rid, 2, "two"), lambda: store.draw(rid, 99)):
        with pytest.raises(LotteryError, match="已取消"):
            operation()
    now[0] += 3600
    assert store.pending_announcements() == []
    assert store.view(rid)["status"] == "CANCELLED"
    drawn = store.create(99, "drawn", 1, 60)
    store.freeze(drawn, 99)
    store.draw(drawn, 99)
    with pytest.raises(LotteryError, match="已开奖"):
        store.cancel(drawn, 99)


def test_group_listing_and_summary(setup):
    store, _, now = setup
    ids = [store.create(99, f"r{n}", 1, 60, chat_id=-100) for n in range(3)]
    store.cancel(ids[0], 99)
    store.freeze(ids[1], 99)
    store.draw(ids[1], 99)
    assert store.group_summary(-100) == {"active": 1, "drawn": 1, "cancelled": 1}
    page, more = store.group_raffles(-100, 0, 2)
    assert ([r["id"] for r in page], more) == ([ids[2], ids[1]], True)
    page, more = store.group_raffles(-100, 1, 2)
    assert ([r["id"] for r in page], more) == ([ids[0]], False)
    now[0] += 3600
    assert store.group_raffles(-100, 0, 8)[0][0]["status"] == "FROZEN"  # freeze on read


def test_groups_and_drafts(setup):
    store, _, _ = setup
    store.remember_group(-100, "甲群")
    store.remember_group(-200, "乙群")
    store.remember_group(-200, "乙群", active=False)
    assert store.groups() == [(-100, "甲群")]
    assert store.group_title(-300) == "-300"
    store.save_draft(7, -100, {"step": "title"})
    store.save_draft(7, -100, {"title": "奖", "step": None})
    assert store.draft(7) == (-100, {"title": "奖", "step": None})
    store.migrate_chat(-100, -1001)
    assert store.groups() == [(-1001, "甲群")]
    assert store.draft(7)[0] == -1001
    store.drop_draft(7)
    assert store.draft(7) is None


def test_weighted_notice_stays_once_set(setup):
    store, rid, now = setup
    store.rule(rid, 99, "idle", 0)
    assert not store.view(rid)["weighted"]  # a zero bonus changes nobody's odds
    store.override(rid, 99, 123, 5)
    store.override(rid, 99, 123, None)
    assert store.view(rid)["weighted"]  # still on after the weight is taken back
    now[0] += 3600
    assert store.view(rid)["weighted"]  # and after the list is frozen
    assert store.view(store.create(99, "开场即加权", 1, 60, weighted=True))["weighted"]
    other = store.create(99, "规则", 1, 60)
    store.rule(other, 99, "vip", 2)
    assert store.view(other)["weighted"]


def test_presets_are_weights_of_people_not_joined(setup):
    store, rid, _ = setup
    store.join(rid, 1, "one")
    store.override(rid, 99, 1, 3)
    store.override(rid, 99, 7, 5)
    assert store.presets(rid) == [(7, 5)]
    store.join(rid, 7, "seven")
    assert store.presets(rid) == []
    assert {e["user_id"]: e["weight"] for e in store.view(rid)["entries"]} == {1: 3, 7: 5}


def test_upgrade_marks_raffles_that_already_had_weights(tmp_path):
    path = tmp_path / "v5.sqlite3"
    db = sqlite3.connect(path)
    for script in MIGRATIONS[:5]:
        for statement in filter(str.strip, script.split(";")):
            db.execute(statement)
    db.execute("PRAGMA user_version = 5")
    for title in ("plain", "override", "bonus", "zero bonus"):
        db.execute(
            "INSERT INTO raffles(title,winner_count,deadline,created_by) VALUES(?,1,9e9,99)",
            (title,),
        )
    db.execute("INSERT INTO overrides VALUES(2,123,5)")
    db.execute("INSERT INTO rules VALUES(3,'vip',2)")
    db.execute("INSERT INTO rules VALUES(4,'idle',0)")
    db.commit()
    db.close()
    store = Store(path)
    assert [store.view(rid)["weighted"] for rid in (1, 2, 3, 4)] == [False, True, True, False]


def test_prizes_go_to_winners_in_draw_order(setup):
    store, _, _ = setup
    rid = store.create(99, "多奖品", 3, 60, prizes=[["iPhone", 1], ["1usdt", 2]])
    for uid in (1, 2, 3, 4):
        store.join(rid, uid, f"u{uid}")
    store.freeze(rid, 99)
    winners = store.draw(rid, 99)["winners"]
    assert [w["prize"] for w in winners] == ["iPhone", "1usdt", "1usdt"]
    assert store.view(rid)["prizes"] == [["iPhone", 1], ["1usdt", 2]]
    short = store.create(99, "人不够", 3, 60, prizes=[["iPhone", 1], ["1usdt", 2]])
    store.join(short, 1, "u1")
    store.freeze(short, 99)
    assert [w["prize"] for w in store.draw(short, 99)["winners"]] == ["iPhone"]


def test_keyword_raffles(setup):
    store, _, now = setup
    rid = store.create(99, "a", 1, 60, chat_id=-100, keyword=" Hello ")
    store.create(99, "b", 1, 60, chat_id=-200, keyword="hello")
    store.create(99, "c", 1, 60, chat_id=-100)
    assert store.keyword_raffles(-100, "HELLO") == [rid]
    assert store.keyword_raffles(-100, "hell") == []
    now[0] += 3600
    assert store.keyword_raffles(-100, "hello") == []

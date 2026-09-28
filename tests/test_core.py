import hashlib
import json
import sqlite3
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import pytest

from lottery.core import LotteryError, Store, weighted_draw


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
    store.grant(rid, 99, 123, "vip")
    store.grant(rid, 99, 123, "vip")
    store.grant(rid, 99, 123, "task")
    assert store.join(rid, 123, "Alice")
    assert not store.join(rid, 123, "Renamed")
    assert store.view(rid)["entries"][0]["weight"] == 5
    store.override(rid, 99, 123, 0)
    assert store.view(rid)["entries"][0]["weight"] == 0
    store.override(rid, 99, 123, None)
    store.grant(rid, 99, 123, "task", False)
    assert store.view(rid)["entries"][0]["weight"] == 3
    store.rule(rid, 99, "vip", 3)
    assert store.view(rid)["entries"][0]["weight"] == 4


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


def test_raffles_isolated(setup):
    store, rid, _ = setup
    other = store.create(99, "other", 1, 60)
    store.override(rid, 99, 123, 0)
    store.join(other, 123, "Alice")
    assert store.view(other)["entries"][0]["weight"] == 1

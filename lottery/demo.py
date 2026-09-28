"""Offline demonstration; does not access Telegram or the production database."""

from pathlib import Path
from tempfile import TemporaryDirectory

from lottery.core import Store
from lottery.views import result_text


def main():
    with TemporaryDirectory(prefix="lottery-demo-") as directory:
        store = Store(Path(directory) / "demo.sqlite3")
        rid = store.create(99, "离线演示抽奖", 2, 60)
        store.rule(rid, 99, "vip", 2)
        for uid, name in [(101, "小明"), (102, "小红"), (103, "小林"), (104, "小王")]:
            store.join(rid, uid, name)
        store.grant(rid, 99, 102, "vip")
        store.override(rid, 99, 103, 6)
        store.override(rid, 99, 104, 0)
        raffle = store.view(rid)
        total = sum(p["weight"] for p in raffle["entries"])
        print("演示权重（只有超级管理员能看到）：")
        for person in raffle["entries"]:
            chance = 100 * person["weight"] / total
            print(f"  {person['display_name']}：权重 {person['weight']}，首轮 {chance:.0f}%")
        store.freeze(rid, 99)
        result = store.draw(rid, 99)
        print("\n" + result_text(result))
        assert store.draw(rid, 99) == result
        print("\n重复开奖验证通过：返回同一份结果。未连接 Telegram。")


if __name__ == "__main__":
    main()

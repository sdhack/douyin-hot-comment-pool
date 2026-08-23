# -*- coding: utf-8 -*-
"""离线自测：验证三级门槛 + 每日配额达标即停，不依赖网络/MediaCrawler。

用内置样例与真实数据（可选）验证 filter_pool 评分与 run_daily 配额逻辑。
真实数据位: --real <本机已抓 comments.json>（可选，默认只跑样例）。
"""
import json
import os
import sqlite3
import sys
import tempfile
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))
import filter_pool  # noqa: E402
import run_daily  # noqa: E402


_SAMPLES = [
    # (content, like, replies, expected_pass)
    ("花三个月钱买的东西，结果发现是智商税，姐妹们一定要看完这条，真的气死了", 5200, 300, True),
    ("哈哈哈", 80000, 10, False),                      # 短口水
    ("学到了，收藏了", 3000, 5, False),                  # 口水
    ("在我家楼下那家店，我闺蜜说好用我就买了，现在天天后悔，真香，别买贵了", 2300, 80, True),
    ("已读", 9999, 0, False),                            # 排除词
    ("第一次知道原来熬夜真会掉头发，医生都这么说，我后悔没早点看", 1500, 40, True),
    ("666", 50000, 100, False),                          # 排除词
    ("普通", 10, 1, False),                              # 互动不足
]


def _make_source(records):
    """把样例转成 by_aweme 聚合 dict 结构，供 run_daily 读。"""
    by = {}
    for i, (content, likes, replies, _p) in enumerate(records):
        by[str(1000 + i)] = {"n": 1, "max": 100, "comments": [{
            "content": content, "nickname": "t", "like_count": likes,
            "sub_comment_count": replies, "comment_id": str(i), "create_time": 0,
        }]}
    return {"summary": {}, "by_aweme": by}


def main():
    tmp = tempfile.mkdtemp(prefix="hcp_selfcheck_")
    # 1) filter_pool 打分正确性
    src = os.path.join(tmp, "sample.json")
    json.dump(_make_source(_SAMPLES), open(src, "w", encoding="utf-8"), ensure_ascii=False)
    out = os.path.join(tmp, "cand.json")
    ap = ["--in", src, "--out", out, "--min-likes", "1000", "--min-replies", "40",
          "--min-len", "20", "--min-score", "50"]
    old = sys.argv[:]
    sys.argv = ["filter_pool.py"] + ap
    try:
        filter_pool.main()
    finally:
        sys.argv = old
    cands = json.load(open(out, encoding="utf-8"))["candidates"]
    # comment_id 与样例下标一一对应（_make_source 里 comment_id = str(i)）
    got = {c["comment_id"] for c in cands}
    exp_pass = {str(i) for i, r in enumerate(_SAMPLES) if r[3]}
    print(f"[selfcheck-1] filter_pool 入选 {len(cands)} 条：{sorted(got)}")
    print(f"[selfcheck-1] 期望入选 comment_id：{sorted(exp_pass)}")
    assert got == exp_pass, f"filter_pool 逻辑不符: got={got} expected={exp_pass}"

    # 2) run_daily 达标即停：配额 3，样例只应有 3 入选（数据默认入库，写 isolation db）
    root = os.path.join(tmp, "root")
    os.makedirs(root, exist_ok=True)
    tdb = os.path.join(tmp, "selfcheck.db")   # 隔离库，避免污染/干扰真实库
    # 预置样例三表 → 满足外键链：accounts(creator_hash) → videos(creator_hash) → comments(aweme_id)；hits.comment_id→comments
    c0 = run_daily._db.open_db(tdb)
    run_daily._db.ensure_schema(c0)
    c0.execute("INSERT OR IGNORE INTO accounts(creator_hash) VALUES(?)", ("S",))
    for i in range(len(_SAMPLES)):
        c0.execute("INSERT OR IGNORE INTO videos(aweme_id, creator_hash) VALUES(?,?)",
                   (str(1000 + i), "S"))
    for i in range(len(_SAMPLES)):
        c0.execute("INSERT OR IGNORE INTO comments(comment_id, aweme_id) VALUES(?,?)",
                   (str(i), str(1000 + i)))
    c0.commit()
    c0.close()
    ap = ["--root", root, "--account", "pool", "--offline-source", src, "--db", tdb,
          "--keywords", "调试占位", "--per-keyword", "0",
          "--min-likes", "1000", "--min-replies", "40", "--min-len", "20",
          "--min-score", "50", "--quota", "3"]
    sys.argv = ["run_daily.py"] + ap
    try:
        rc = run_daily.main()
    finally:
        sys.argv = old
    assert rc == 0, f"run_daily 返回 {rc}"
    with sqlite3.connect(tdb) as conn:
        conn.row_factory = sqlite3.Row
        added = conn.execute(
            "SELECT COUNT(*) AS n FROM hits WHERE hit_date=?", (date.today().isoformat(),)).fetchone()["n"]
    print(f"[selfcheck-2] 达标即停：quota=3 实入池={added}")
    assert added == 3, f"配额达标即停失效: 期望3 实得{added}"

    # 3) 再跑一次：配额已满应直接提示达标即停，不重复（当日 hits 仍为 3）
    sys.argv = ["run_daily.py"] + ap
    rc = run_daily.main()
    assert rc == 0
    with sqlite3.connect(tdb) as conn:
        conn.row_factory = sqlite3.Row
        d2 = conn.execute(
            "SELECT COUNT(*) AS n FROM hits WHERE hit_date=?", (date.today().isoformat(),)).fetchone()["n"]
    print(f"[selfcheck-3] 幂等：当日已满仍为 {d2}")
    assert d2 == 3, "重复运行不应新增"

    print("=== Selfcheck PASSED ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
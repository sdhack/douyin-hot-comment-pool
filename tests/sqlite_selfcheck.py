# -*- coding: utf-8 -*-
"""SQLite 数据中心端到端自测。用独立临时库，不依赖网络。"""
import os
import sys
import tempfile
import json

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SD = os.path.join(SKILL, "sqlite")
sys.path.insert(0, SD)

import db as dbmod
import loader
import hits_backfill

TMPDIR = tempfile.mkdtemp(prefix="sqlite_selfcheck_")
DB = os.path.join(TMPDIR, "t.db")

PASS, FAIL = [], []


def ok(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  " + str(detail)) if detail else ""))


# 构造样例批次
base = os.path.join(TMPDIR, "bat_douyin")
jsonl = os.path.join(base, "douyin", "jsonl")
os.makedirs(jsonl, exist_ok=True)
cont = os.path.join(jsonl, "search_contents_2026.txt.jsonl")
comm = os.path.join(jsonl, "search_comments_2026.txt.jsonl")

with open(cont, "w", encoding="utf-8") as f:
    f.write(json.dumps({"aweme_id": "111", "creator_hash": "uA", "nickname": "A",
                        "title": "视频甲", "liked_count": 100, "comment_count": 10,
                        "source_keyword": "测试"}, ensure_ascii=False) + "\n")
    f.write(json.dumps({"aweme_id": "222", "creator_hash": "uA", "nickname": "A",
                        "title": "视频乙", "liked_count": 200, "comment_count": 20,
                        "source_keyword": "测试"}, ensure_ascii=False) + "\n")
with open(comm, "w", encoding="utf-8") as f:
    f.write(json.dumps({"comment_id": "c1", "aweme_id": "111", "creator_hash": "uB",
                        "content": "钩子真相断言语气浓", "like_count": 5000, "sub_comment_count": 60,
                        "parent_comment_id": "0", "create_time": 100}, ensure_ascii=False) + "\n")
    f.write(json.dumps({"comment_id": "c2", "aweme_id": "111", "creator_hash": "uC",
                        "content": "回复一句", "like_count": 1, "sub_comment_count": 0,
                        "parent_comment_id": "c1", "create_time": 101}, ensure_ascii=False) + "\n")
    f.write(json.dumps({"comment_id": "c3", "aweme_id": "222", "creator_hash": "uB",
                        "content": "短", "like_count": 2000, "sub_comment_count": 5,
                        "parent_comment_id": "0", "create_time": 102}, ensure_ascii=False) + "\n")

print("== 1. 建库 ==")
dbmod.init_db(DB)
ok("建6表+视图", all(x in dbmod.init_db(DB) for x in
    ("accounts", "videos", "comments", "ancestry", "hits", "batches", "vw_hot_comments")))

print("== 2. 导入 ==")
conn = dbmod.open_db(DB)
r = loader.import_batch(conn, base, keyword="测试", account="bat1", batches=False)
ok("导入视频2", r["items"]["videos"] == 2, r["items"])
ok("导入评论3", r["items"]["comments"] == 3)
ok("讨论串 一级2/二级1",
   conn.execute("select count(*) from ancestry where depth=1").fetchone()["count(*)"] == 2
   and conn.execute("select count(*) from ancestry where depth=2").fetchone()["count(*)"] == 1)
conn.close()

print("== 3. 幂等重复导入 ==")
conn = dbmod.open_db(DB)
loader.import_batch(conn, base, keyword="测试", account="bat1", batches=False)
ok("评论仍3条", conn.execute("select count(*) from comments").fetchone()["count(*)"] == 3)
ok("视频仍2条", conn.execute("select count(*) from videos").fetchone()["count(*)"] == 2)
conn.close()

print("== 4. hits 回流 ==")
conn = dbmod.open_db(DB)
pool = {"pool": [{"comment_id": "c1", "aweme_id": "111", "score": 90, "reasons": ["钩子"],
                  "content": "x", "like_count": 5000, "sub_comment_count": 60}]}
hits_backfill.write_hits(conn, pool["pool"])
ok("hits写入1", conn.execute("select count(*) from hits").fetchone()["count(*)"] == 1)
ok("hits关联评论score=90", conn.execute(
    "select h.score from hits h join comments c on c.comment_id=h.comment_id where h.comment_id='c1'"
).fetchone()["score"] == 90)
conn.close()

print("== 5. 讨论串视图 ==")
conn = dbmod.open_db(DB)
rows = conn.execute(
    "select depth, comment_id from vw_threads where root_comment_id='c1' order by create_time").fetchall()
ok("root串 一级c1+二级c2", [(x["depth"], x["comment_id"]) for x in rows] == [(1, "c1"), (2, "c2")])
conn.close()

print("\n=== 结果: %d PASS / %d FAIL ===" % (len(PASS), len(FAIL)))
sys.exit(1 if FAIL else 0)
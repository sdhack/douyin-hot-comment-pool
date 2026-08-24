# -*- coding: utf-8 -*-
"""把爆款沉淀 pool 命中回流进 hits 表（绑定 comment_id）。

pool 是技能筛选出的爆款列表（如 pool/<account>.json 的 pool 数组），
这里把它们写入 SQLite hits 表，并尽量回补到触发 source 的 comments/videos/accounts（若已入库）。

用法:
  python tools/hits_backfill.py --pool <pool.json> [--db <path>]
"""
import argparse
import json
import os
import sys
import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db import open_db, ensure_schema, DEFAULT_DB
import loader


def write_hits(conn, pool_entries, hit_date=None, batch_id=""):
    hit_date = hit_date or datetime.datetime.now().strftime("%Y-%m-%d")
    stmt = """INSERT INTO hits(comment_id, aweme_id, score, passed_level, reasons, score_break, hit_date, batch_id)
              VALUES(?,?,?,?,?,?,?,?)
              ON CONFLICT(comment_id) DO UPDATE SET
                score=excluded.score, passed_level=excluded.passed_level,
                reasons=excluded.reasons, score_break=excluded.score_break,
                hit_date=min(hits.hit_date, excluded.hit_date),
                batch_id=COALESCE(NULLIF(excluded.batch_id,''), hits.batch_id)"""  # fix Bug-4: 保留最早命中日；批次号仅在不为空时回填
    n = 0
    for c in pool_entries:
        cid = str(c.get("comment_id") or "")
        if not cid:
            continue
        conn.execute(stmt, (
            cid, str(c.get("aweme_id") or ""), float(c.get("score") or 0),
            "123", json.dumps(c.get("reasons") or [], ensure_ascii=False),
            json.dumps(c.get("score_breakdown") or {}, ensure_ascii=False),
            hit_date, str(batch_id or "")))
        n += 1
    conn.commit()
    return n


def load_pool(path):
    d = json.load(open(path, encoding="utf-8"))
    if isinstance(d, dict):
        pool = d.get("pool", [])
        if not pool and "candidates" in d:
            pool = d["candidates"]
    elif isinstance(d, list):
        pool = d
    else:
        pool = []
    return pool


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", required=True)
    ap.add_argument("--db", default=None)
    a = ap.parse_args()
    dbp = a.db or DEFAULT_DB
    conn = open_db(dbp)
    ensure_schema(conn)
    pool = load_pool(a.pool)
    n = write_hits(conn, pool)
    print("[hits] 写入爆款命中 %d 条 -> %s" % (n, dbp))
    conn.close()


if __name__ == "__main__":
    main()
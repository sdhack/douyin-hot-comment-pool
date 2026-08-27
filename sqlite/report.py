# -*- coding: utf-8 -*-
"""SQLite 数据分析接口：汇总 / 累计 / 爆款榜 / 讨论串 / 账号榜。

用法:
  python report.py --stats --db <path>           汇总概览
  python report.py --hot --top 20 --db <path>    爆款命中榜 (JOIN 视频/作者)
  python report.py --threads --cid <comment_id>  讨论串
  python report.py --accounts --top 10 --db <path> 高互动作者榜
  python report.py --batches --db <path>         采集批次历史
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db import open_db, DEFAULT_DB


def stats(conn):
    out = {}
    out["videos"] = conn.execute("select count(*) n from videos").fetchone()["n"]
    out["comments"] = conn.execute("select count(*) n from comments").fetchone()["n"]
    out["accounts"] = conn.execute("select count(*) n from accounts").fetchone()["n"]
    out["hits"] = conn.execute("select count(*) n from hits").fetchone()["n"]
    out["batches"] = conn.execute("select count(*) n from batches").fetchone()["n"]
    out["keywords"] = [r["keyword"] for r in conn.execute(
        "select distinct source_keyword keyword from videos where source_keyword!=''")]
    out["by_source"] = conn.execute(
        "select source_keyword k,count(*) n from videos group by source_keyword order by n desc").fetchall()
    return out


def hot_top(conn, top=20, by="score", min_likes=0):
    col = "h.score" if by == "score" else "c.like_count"
    rows = conn.execute(f"""
        SELECT h.comment_id, h.score, h.hit_date, c.content, c.like_count, c.sub_comment_count,
               v.aweme_id, v.title, v.source_keyword, a.nickname AS author
        FROM hits h
        LEFT JOIN comments c ON c.comment_id = h.comment_id
        LEFT JOIN videos v   ON v.aweme_id = c.aweme_id
        LEFT JOIN accounts a ON a.creator_hash = v.creator_hash  -- fix Bug-8:
        WHERE c.like_count >= ?
        ORDER BY {col} DESC
        LIMIT ?""", (min_likes, top)).fetchall()
    for r in rows:
        r["content"] = r["content"] or ""
    return rows


def accounts_top(conn, top=10):
    return conn.execute("""
        SELECT a.nickname, a.creator_hash,
               COUNT(DISTINCT v.aweme_id)  AS videos,
               COUNT(DISTINCT c.comment_id) AS comments
        FROM accounts a
        LEFT JOIN videos v   ON v.creator_hash = a.creator_hash
        LEFT JOIN comments c ON c.aweme_id = v.aweme_id
        GROUP BY a.creator_hash
        ORDER BY comments DESC, videos DESC
        LIMIT ?""", (top,)).fetchall()


def batches_info(conn):
    return conn.execute("""
        SELECT batch_id, account, keyword, started_at, finished_at,
               videos_count, comments_count, error
        FROM batches ORDER BY started_at DESC""").fetchall()


def thread(conn, root_comment_id):
    return conn.execute("""
        SELECT a.depth, c.comment_id, c.parent_comment_id, a.root_comment_id,
               c.content, c.like_count, c.create_time
        FROM ancestry a
        JOIN comments c ON c.comment_id = a.child_comment_id
        WHERE a.root_comment_id = ?
        ORDER BY a.depth, COALESCE(c.create_time,0)""", (root_comment_id,)).fetchall()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None)
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--hot", action="store_true")
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--by", default="score", choices=("score", "likes"))
    ap.add_argument("--min-likes", type=int, default=0)
    ap.add_argument("--threads", action="store_true")
    ap.add_argument("--cid", default="")
    ap.add_argument("--accounts", action="store_true")
    ap.add_argument("--accounts-top", type=int, default=10)
    ap.add_argument("--batches", action="store_true")
    a = ap.parse_args()
    conn = open_db(a.db or DEFAULT_DB)

    if a.stats:
        s = stats(conn)
        print("== 汇总 ==")
        for k in ("videos", "comments", "accounts", "hits", "batches"):
            print(f"  {k}: {s[k]}")
        print("  关键词:", ", ".join(s["keywords"]) or "-")
        print("  按来源词视频数:", s["by_source"])
    if a.hot:
        print(f"\n== 爆款命中榜 Top {a.top} (by {a.by}, min_likes={a.min_likes}) ==")
        for i, r in enumerate(hot_top(conn, a.top, a.by, a.min_likes), 1):
            print(f"{i}. score {r['score']} 赞 {r['like_count']} 回 {r['sub_comment_count']} | {r['content'][:36]}")
            print(f"     视频 {r['aweme_id']} 词[{r['source_keyword']}] 作者 {r['author']}")
            print(f"     标题 {r['title'][:40]}")
    if a.threads:
        th = thread(conn, a.cid)
        if not th:
            print(f"无讨论串（根 {a.cid}）")
        for r in th:
            print("  " * (r["depth"] - 1), f"[{r['comment_id'][:10]}] {r['content'][:50]} (赞{r['like_count']})")
    if a.accounts:
        print(f"\n== 高互动作者榜 Top {a.accounts_top} ==")
        for i, r in enumerate(accounts_top(conn, a.accounts_top), 1):
            print(f"{i}. {r['nickname']} 视频 {r['videos']} 评论 {r['comments']}")
    if a.batches:
        print("\n== 采集批次历史 ==")
        for r in batches_info(conn):
            kw = r["keyword"] or "-"
            print(f"  {r['batch_id'][-24:]} 词[{kw}] 视频{r['videos_count']} 评论{r['comments_count']} "
                  f"{r['started_at'][:19]} err={r['error'] or '无'}")
    conn.close()


if __name__ == "__main__":
    main()
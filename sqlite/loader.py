# -*- coding: utf-8 -*-
"""把 MediaCrawler 搜索批次 jsonl（contents + comments）幂等导入 SQLite。

扫描 <batch_dir>/douyin/jsonl/*.jsonl：
  * <both>_contents_*.jsonl  -> 批量 upsert accounts + videos
  * <both>_comments_*.jsonl -> 批量 upsert comments，并据 parent_comment_id 写 ancestry
  * 同名/同 root 目录可重复跑：UPSERT 保证不产生重复行。

用法:
  python tools/db_loader.py --dir <batch目录> [--keyword 关键词] [--account 标识] [--db <path>]
    --dir 必须指向含 douyin/jsonl 的批次根（如 .../hcp-prod-20260823-130907）
    --all  一次性导入 skill 目录 ../../ 下所有历史 hcp 搜索批次目录（backfill）
"""
import argparse
import glob
import json
import os
import sys
import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db import open_db, ensure_schema, DEFAULT_DB

ROOT_MARK = "douyin"      # batch根下含 douyin/jsonl 即视为有效批次
CONTENT_GLOBS = ("*_contents_*.jsonl", "*_dedup.jsonl")
COMMENT_GLOBS = ("*_comments_*.jsonl",)


def _batch_id(root):
    return os.path.basename(os.path.normpath(root))


def _jsonl_rows(path):
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except Exception:
                pass


def _is_content_row(r):
    return bool(r.get("aweme_id")) and ("title" in r or "desc" in r or "liked_count" in r)


def _is_comment_row(r):
    return bool(r.get("comment_id"))


def _safe_int(v, d=0):
    try:
        x = int(v)
        return x
    except (TypeError, ValueError):
        try:
            return int(float(str(v)))
        except (TypeError, ValueError):
            return d


def _pictures_url(r):
    pics = r.get("pictures")
    if not pics:
        return ""
    if isinstance(pics, str):
        return pics
    urls = []
    for p in pics:
        if isinstance(p, str):
            urls.append(p)
        elif isinstance(p, dict):
            u = p.get("url_list") or p.get("url") or p.get("urls")
            if isinstance(u, list):
                urls.extend(u)
            elif u:
                urls.append(u)
    return json.dumps([u for u in urls if u], ensure_ascii=False) if urls else ""


def import_batch(conn, batch_dir, keyword="", account="", batches=True, batch_id=""):
    """返回 {videos, comments, accounts}; 显式传 batch_id 可避免同账号跨天批次行被覆盖(fix Bug-5)。"""
    batch_dir = os.path.normpath(batch_dir)
    # 向下定位 douyin/jsonl（可能在 batch根 或 crawl_<account>/ 下）
    jsonl_d = os.path.join(batch_dir, ROOT_MARK, "jsonl")
    if not os.path.isdir(jsonl_d):
        for dp, dns, fns in os.walk(batch_dir):
            cand = os.path.join(dp, ROOT_MARK, "jsonl")
            if os.path.isdir(cand):
                jsonl_d = cand
                break
    if not os.path.isdir(jsonl_d) or not glob.glob(os.path.join(jsonl_d, "*.jsonl")):
        raise FileNotFoundError(f"不是有效批次目录(缺含jsonl的 {ROOT_MARK}/jsonl): {batch_dir}")
    bid = batch_id or account or _batch_id(batch_dir)  # fix Bug-5: 显式批次号优先，防跨天覆盖

    # 预写批次元信息
    if batches:
        conn.execute(
            """INSERT INTO batches(batch_id, run_root, account, keyword, started_at)
               VALUES(?,?,?,?,?)
               ON CONFLICT(batch_id) DO UPDATE
                 SET run_root=excluded.run_root, account=excluded.account,
                     keyword=excluded.keyword, started_at=excluded.started_at""",
            (bid, batch_dir, account, keyword,
             datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")))

    # ---- 汇总内容 jsonl ----
    content_paths, comm_paths = [], []
    for pat in CONTENT_GLOBS:
        content_paths += glob.glob(os.path.join(jsonl_d, pat))
    for pat in COMMENT_GLOBS:
        comm_paths += glob.glob(os.path.join(jsonl_d, pat))
    content_paths = sorted(set(content_paths))
    comm_paths = sorted(set(comm_paths))

    acc_stmt = """INSERT INTO accounts(creator_hash, nickname, first_seen, last_seen)
                  VALUES(?,?,?,?)
                  ON CONFLICT(creator_hash) DO UPDATE SET
                    nickname=CASE WHEN excluded.nickname!='' THEN excluded.nickname ELSE accounts.nickname END,
                    last_seen=excluded.last_seen"""
    vid_stmt = """INSERT INTO videos(aweme_id, creator_hash, nickname, title, delta_desc, aweme_type,
                  liked_count, collected_count, comment_count, share_count, create_time,
                  source_keyword, first_seen_batch, aweme_url, cover_url,
                  video_download_url, music_download_url, note_download_url)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                  ON CONFLICT(aweme_id) DO UPDATE SET
                    liked_count=excluded.liked_count, collected_count=excluded.collected_count,
                    comment_count=excluded.comment_count, share_count=excluded.share_count,
                    comment_count=excluded.comment_count, nickname=excluded.nickname,
                    source_keyword=excluded.source_keyword"""  # fix Bug-10: 新关键词覆盖旧词（不再首见冻结）
    com_stmt = """INSERT INTO comments(comment_id, aweme_id, creator_hash, parent_comment_id, content,
                  like_count, sub_comment_count, create_time, last_modify_ts, pictures_url, batch_id)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?)
                  ON CONFLICT(comment_id) DO UPDATE SET
                    content=excluded.content, like_count=excluded.like_count,
                    sub_comment_count=excluded.sub_comment_count,
                    create_time=excluded.create_time, last_modify_ts=excluded.last_modify_ts,
                    batch_id=excluded.batch_id"""
    anc_stmt = """INSERT OR IGNORE INTO ancestry(child_comment_id, parent_comment_id, root_comment_id, depth)
                  VALUES(?,?,?,?)"""

    stats = {"accounts": 0, "videos": 0, "comments": 0, "ancestry": 0}
    now = datetime.datetime.now().strftime("%Y-%m-%d")

    # ---- 先内容（作者/视频） ----
    seen_vids = set()
    for cp in content_paths:
        for r in _jsonl_rows(cp):
            if not _is_content_row(r):
                continue
            ahash = r.get("creator_hash") or r.get("creator_id") or ""
            nickname = r.get("nickname") or ""
            if ahash:
                conn.execute(acc_stmt, (ahash, nickname, now, now))
                stats["accounts"] += 1
            vid = str(r.get("aweme_id") or "")
            if not vid or vid in seen_vids and vid:
                if not vid:
                    continue
            seen_vids.add(vid)
            conn.execute(vid_stmt, (
                vid, ahash, nickname, r.get("title") or "", r.get("delta_desc") or r.get("desc") or "",
                r.get("aweme_type"), _safe_int(r.get("liked_count")), _safe_int(r.get("collected_count")),
                _safe_int(r.get("comment_count")), _safe_int(r.get("share_count")), r.get("create_time"),
                keyword or r.get("source_keyword") or "", bid,
                r.get("aweme_url") or "", r.get("cover_url") or "",
                r.get("video_download_url") or "", r.get("music_download_url") or "",
                r.get("note_download_url") or "",
            ))
            stats["videos"] += 1

    # ---- 后评论 ----
    for cfile in comm_paths:
        for row in _jsonl_rows(cfile):
            if not _is_comment_row(row):
                continue
            cid = str(row.get("comment_id") or "")
            if not cid:
                continue
            parent = str(row.get("parent_comment_id") or "")
            if parent in ("", "0", "None", "null"):
                parent = ""   # 一级评论
            root = parent if parent else cid
            depth = 2 if parent else 1
            conn.execute(com_stmt, (
                cid, str(row.get("aweme_id") or ""), row.get("creator_hash") or "",
                parent, (row.get("content") or "").strip(),
                _safe_int(row.get("like_count")), _safe_int(row.get("sub_comment_count")),
                row.get("create_time"), row.get("last_modify_ts"),
                _pictures_url(row), bid,
            ))
            stats["comments"] += 1
            conn.execute(anc_stmt, (cid, parent, root, depth))
            stats["ancestry"] += 1

    # 收尾批次统计
    conn.execute("""UPDATE batches SET videos_count=?, comments_count=?,
                    finished_at=? , error='' WHERE batch_id=?""",
                 (stats["videos"], stats["comments"],
                  datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), bid))
    conn.commit()
    return {"batch_id": bid, "items": stats}


def _has_ancestor_prefix(dp, prefix_keys):
    """dp 自身或任一祖先目录名以 prefix 开头即为爆款评论池批次。"""
    parts = []
    cur = os.path.normpath(dp)
    while cur and cur != os.path.dirname(cur):
        parts.append(os.path.basename(cur))
        cur = os.path.dirname(cur)
    return any(any(p.lower().startswith(k) for k in prefix_keys) for p in parts)


def find_all_batches(root, prefix_keys=("hcp",)):
    """在工作根下递归找含 douyin/jsonl 的批次目录，仅保留爆款评论池相关（目录或其祖先名匹配前缀）。
    返回的是 batch 根（含 douyin/jsonl 的那层之上的整段起始目录），供 import_batch 递归定位。"""
    found = []
    for dp, dns, fns in os.walk(os.path.abspath(root)):
        if os.path.isdir(os.path.join(dp, ROOT_MARK, "jsonl")):
            if _has_ancestor_prefix(dp, prefix_keys):
                found.append(dp)
    # 上溯到最外层 搜索批次根（如 hcp-prod-日期），避免仅记录 crawl_xxx 层
    roots = set()
    for dp in found:
        cur = os.path.normpath(dp)
        while True:
            parent = os.path.dirname(cur)
            if not parent or os.path.dirname(parent) == parent:
                break
            base = os.path.basename(parent).lower()
            if any(base.startswith(k) for k in prefix_keys):
                cur = parent
            else:
                break
        roots.add(cur)
    return sorted(roots)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="")
    ap.add_argument("--keyword", default="")
    ap.add_argument("--account", default="")
    ap.add_argument("--db", default=None)
    ap.add_argument("--all", nargs="?", const=".", help="导入工作根下所有搜索批次目录（backfill）")
    a = ap.parse_args()
    dbp = a.db or DEFAULT_DB
    conn = open_db(dbp)
    ensure_schema(conn)

    if a.dir:
        res = import_batch(conn, a.dir, keyword=a.keyword, account=a.account)
        print("[loader] 批次 %s 完成: %s" % (res["batch_id"], res["items"]))
    elif a.all is not None:
        root = os.path.abspath(a.all)
        dirs = find_all_batches(root)
        print("[loader] 找到搜索批次目录 %d 个" % len(dirs))
        for d in dirs:
            try:
                res = import_batch(conn, d)
                print("  + %s : %s" % (d, res["items"]))
            except Exception as e:
                print("  ! %s : %s" % (d, e))
    else:
        ap.print_help()
    conn.close()


if __name__ == "__main__":
    main()
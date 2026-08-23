# -*- coding: utf-8 -*-
"""爆款评论池·每日调度（核心编排：逐词实时采集→实时入库→达标即停→清理→报告）。

单一命令完成"今天刷一堆爆款评论"，主路径为实时 MediaCrawler 采集：
  1) 逐关键词采集（collect_search.py 内嵌 MediaCrawler 调度）；
  2) 每个关键词抓完立即实时入库：jsonl 经 loader 幂等导入 accounts/videos/comments/ancestry/batches；
  3) 三级门槛筛选在内存中完成（aggregate_paths 聚合，不产生任何聚合 JSON）；
  4) 达标即停：每天最多入池 QUOTA 条（默认 5）。当日已入选数实时取自 hits 表（hit_date=今天），
     每个关键词处理完即复查，达到 QUOTA 立即停止后续采集；
  5) 零 JSON 残留：入库成功后整段运行目录（含 MediaCrawler jsonl/cursor/指针）随即删除，
     数据只落 SQLite；入库失败则保留现场便于排障；
  6) 末尾输出当日采集报告（逐词统计/当日命中明细/库内累计/停止原因）。
  --offline-source 仅排障：喂存量聚合 JSON 跳过采集（同样只入库，不写 json）。

用法（主路径-实时采集）:
  python tools/run_daily.py --root <工作根> --account <slug> --keywords "甲;乙;丙"
      [--preset safe|fast]            # 默认 safe=慢档(并发1/延时3-8s,最稳)；fast=快档(并发3/延时1-3s,提速)
      # 显式 --speed/--sleep-*/--per-keyword/--comments-count 会覆盖 preset
      [--min-likes 1000] [--min-replies 50] [--min-len 30] [--min-score 55]
      [--quota 5] [--dry-run]
用法（调试-离线）:
  python tools/run_daily.py --root <根> --account pool --keywords "<任一>" --per-keyword 0
      --offline-source <聚合 comments.json> --quota 5

产物: 技能内置 SQLite（sqlite/douyin_hotpool.db）——唯一数据落点，磁盘无 JSON 残留
      - 采集原始数据 → accounts / videos / comments / ancestry / batches（loader 幂等）
      - 精选爆款命中 → hits 表（爆款榜 / 达标即停）
"""
import argparse
import datetime
import glob
import json
import os
import shutil
import subprocess
import sys
import time

TOOLS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TOOLS)
import aggregate_comments  # noqa: E402
import filter_pool  # noqa: E402
import _presets  # noqa: E402
_SQLITE_DIR = os.path.join(os.path.dirname(TOOLS), "sqlite")
sys.path.insert(0, _SQLITE_DIR)
import db as _db  # noqa: E402
import loader as _loader  # noqa: E402
import hits_backfill as _hits  # noqa: E402


def _open_db(db_path=None):
    conn = _db.open_db(db_path or _db.DEFAULT_DB)
    _db.ensure_schema(conn)
    return conn


def _today_ids(conn):
    """当日已入池的精选 comment_id 集合（hits.hit_date=今天），作为达标即停依据。"""
    today = datetime.date.today().isoformat()
    return {row["comment_id"] for row in conn.execute(
        "SELECT comment_id FROM hits WHERE hit_date=?", (today,))}


def _already(ids_today, cand):
    return bool(cand.get("comment_id") and cand["comment_id"] in ids_today)


def _hit_ids_all(conn):
    """历史全量命中 comment_id 集合（跨天去重：同 comment_id 不重复入选）。"""
    return {row["comment_id"] for row in conn.execute("SELECT comment_id FROM hits")}


def _hit_texts_all(conn):
    """历史全量命中文本集合（同文本不重复入选：不同视频下的雷同评论只计一次）。"""
    return {(row["content"] or "").strip() for row in conn.execute(
        "SELECT c.content AS content FROM hits h JOIN comments c ON c.comment_id=h.comment_id")}


def _dup_guard(conn):
    """一次性取跨天去重上下文：(历史命中id集, 历史命中文本集)。"""
    return _hit_ids_all(conn), _hit_texts_all(conn)


def _is_dup(cand, done_ids, done_texts):
    """当日已入选 / 历史同 id / 历史同文本 → 视为重复，不再入选。"""
    cid = str(cand.get("comment_id") or "")
    txt = (cand.get("content") or "").strip()
    return (cid and cid in done_ids) or (txt and txt in done_texts)


def _run_offline(conn, source, account, quota, args, dry_run):
    today = datetime.date.today().isoformat()
    ids_today = _today_ids(conn)
    remaining = max(0, quota - len(ids_today))
    print(f"[daily] 日期={today} 当日已入池={len(ids_today)} 配额={quota} 剩余={remaining}")

    if remaining <= 0:
        print(f"[达标即停] 今日已达 {quota} 条配额上限，直接结束。")
        return 0

    # 筛选
    records = json.load(open(source, encoding="utf-8"))
    if isinstance(records, dict):
        candidate_blobs = list(filter_pool.iter_records(records))
    else:
        candidate_blobs = records
    cands = _screen(candidate_blobs, args)

    # 过滤重复（当日 + 历史全量：同id/同文本不重复入选）+ 按分排序取满 remaining
    done_ids, done_texts = _dup_guard(conn)
    fresh = [c for c in cands
             if not _already(ids_today, c) and not _is_dup(c, done_ids, done_texts)]
    fresh.sort(key=lambda c: (c["score"], c["like_count"]), reverse=True)
    pick = fresh[:remaining]

    if dry_run:
        print(f"[dry-run] 命中候选 {len(cands)} / 新候选 {len(fresh)} / 将入池 {len(pick)}")
        for c in pick:
            print(f"   +{c['content'][:40]}... [{c['score']}]")
        return 0

    if pick:
        n = _hits.write_hits(conn, pick)
        conn.commit()
        print(f"[daily] 入库爆款命中 {n} 条（今日累计 {len(_today_ids(conn))}/{quota}）→ {args.db or _db.DEFAULT_DB}")
    print("[下一阶段] 爆款榜查询：python sqlite/report.py --hot")
    return 0 if len(pick) else 2


def _screen(blobs, args):
    """对原始评论 blob 做三级门槛，返回已评分的候选（保留 filter_pool 打分口径）。"""
    out = []
    for r in blobs:
        content = (r.get("content") or "").strip()
        if not content:
            continue
        likes = int(r.get("like_count") or 0)
        replies = int(r.get("sub_comment_count") or r.get("reply_count") or 0)
        if likes < args.min_likes and replies < args.min_replies:
            continue
        elen = filter_pool._eff_len(content)
        if elen < args.min_len:
            continue
        if filter_pool._is_excluded(content):
            continue
        score, bd, why = filter_pool._score(content)
        if score < args.min_score:
            continue
        out.append({
            "aweme_id": str(r.get("aweme_id") or ""),
            "comment_id": str(r.get("comment_id") or ""),
            "nickname": r.get("nickname") or "",
            "content": content,
            "like_count": likes,
            "sub_comment_count": replies,
            "create_time": r.get("create_time"),
            "len": elen, "score": score, "score_breakdown": bd, "reasons": why,
        })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--account", default="pool")
    ap.add_argument("--keywords", required=True, help="关键词，分号分隔；实时 MediaCrawler 采集+筛选为主路径")
    ap.add_argument("--preset", default="safe", choices=["safe", "fast"],
                    help="采集档位：safe=慢档(并发1/延时3-8s，最稳)，fast=快档(并发3/延时1-3s，提速但风控面大)。默认 safe。")
    ap.add_argument("--per-keyword", type=int, default=10,
                    help="每关键词最多采样视频数（达标5条无需30；改小加快）")
    ap.add_argument("--comments-count", type=int, default=30,
                    help="单视频最多一级评论数（改小加快）")
    ap.add_argument("--speed", default="safe", choices=["safe", "normal", "fast"],
                    help="并发 level: safe=1 / normal=2 / fast=3（被显式指定时优先于 --preset）")
    ap.add_argument("--lt", default="qrcode")
    ap.add_argument("--cookies", default=None)
    ap.add_argument("--sleep-min", type=float, default=3)
    ap.add_argument("--sleep-max", type=float, default=3)
    ap.add_argument("--retry-fail", type=int, default=2)
    ap.add_argument("--max-min", type=float, default=45)
    ap.add_argument("--min-likes", type=int, default=1000)
    ap.add_argument("--min-replies", type=int, default=50)
    ap.add_argument("--min-len", type=int, default=30)
    ap.add_argument("--min-score", type=float, default=55)
    ap.add_argument("--quota", type=int, default=5)
    ap.add_argument("--db", default=None,
                    help="SQLite 库路径（默认技能内置 sqlite/douyin_hotpool.db；测试可传隔离库）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--offline-source", default=None,
                    help="仅调试：喂存量聚合 JSON 做筛选（跳过采集），日常主路径不要用")
    a = ap.parse_args()
    _presets.apply_preset(a, sys.argv)

    conn = _open_db(a.db)  # 采集数据默认入库 SQLite
    # 主路径：逐关键词 实时采集→实时入库→内存筛选→hits→清理 JSON→达标即停→报告
    if a.offline_source:
        if not os.path.isfile(a.offline_source):
            sys.exit(f"[ERR] --offline-source 不存在: {a.offline_source}（仅为调试用，正常应实时采集）")
        print("[提示] 你正在用离线模式（--offline-source），仅适用于调试；主流程应走实时 MediaCrawler 采集。")
        return _run_offline(conn, a.offline_source, a.account, a.quota, a, a.dry_run)

    return _run_realtime(conn, a)


def _crawl_keyword(a, kw):
    """调 collect_search 采集单个关键词。返回 (returncode, run_root|None)。"""
    sub_args = ["--root", a.root, "--account", a.account, "--keywords", kw,
                "--per-keyword", str(a.per_keyword), "--comments-count", str(a.comments_count),
                "--speed", a.speed, "--lt", a.lt, "--sleep-min", str(a.sleep_min),
                "--sleep-max", str(a.sleep_max), "--retry-fail", str(a.retry_fail),
                "--max-min", str(a.max_min)]
    if a.cookies:
        sub_args += ["--cookies", a.cookies]
    r = subprocess.run([sys.executable, os.path.join(TOOLS, "collect_search.py")] + sub_args)
    run_root = None
    pointer = os.path.join(a.root, f".douyin-crawl-current-{a.account}.json")
    if os.path.isfile(pointer):
        try:
            run_root = json.load(open(pointer, encoding="utf-8")).get("run_root")
        except Exception:
            pass
    return r.returncode, run_root


def _ingest_keyword_run(conn, a, kw, run_root):
    """单关键词实时入库：loader 导入→内存聚合+三级筛选→hits(配额封顶)→清理运行目录。

    返回报表行 dict（kw/videos/comments/new_hits/status/picks/cleaned）。
    清理策略：入库成功或确认无数据 → 整段删除 run_root（零 JSON 残留）；入库失败保留现场。
    """
    row = {"kw": kw, "videos": 0, "comments": 0, "new_hits": 0,
           "status": "空结果", "note": "", "picks": [], "cleaned": False}
    crawl_dir = os.path.join(run_root, "crawl_" + a.account)
    comm_fps = sorted(glob.glob(os.path.join(crawl_dir, "**", "search_comments_*.jsonl"),
                                recursive=True))
    if not comm_fps:
        row["note"] = "无 search_comments_*.jsonl（登录态/风控/无匹配）"
        print(f"  [daily] {row['note']}")

    ingested = False
    if comm_fps:
        try:
            # batch_id 按词+时间戳区分：同账号多词不互相覆盖 batches 统计
            bid = f"{a.account}.{kw}.{datetime.datetime.now().strftime('%H%M%S')}"
            res = _loader.import_batch(conn, crawl_dir, keyword=kw, account=a.account,
                                       batch_id=bid)
            conn.commit()
            ingested = True
            row["videos"], row["comments"] = res["items"]["videos"], res["items"]["comments"]
            print(f"  [daily] 实时入库: 视频 {row['videos']} / 评论 {row['comments']}")
        except Exception as e:
            row["status"] = "入库失败"
            row["note"] = str(e)
            print(f"  [daily] 原始采集数据入库失败（保留现场不删文件）: {e}")

    if ingested:
        # 三级筛选：内存聚合（不落聚合 JSON）→ 打分 → 配额封顶写入 hits
        by_aweme, summary = aggregate_comments.aggregate_paths(comm_fps, max_n=100)
        cands = _screen(list(filter_pool.iter_records(
            {"summary": summary, "by_aweme": by_aweme})), a)
        cands.sort(key=lambda c: (c["score"], c["like_count"]), reverse=True)
        ids = _today_ids(conn)
        done_ids, done_texts = _dup_guard(conn)   # 跨天去重：历史命中不再重复入选
        picks = []
        for c in cands:
            if len(ids) + len(picks) >= a.quota:
                break
            if _already(ids, c) or _is_dup(c, done_ids, done_texts):
                continue
            picks.append(c)
        if picks:
            _hits.write_hits(conn, picks)
            conn.commit()
            row["new_hits"] = len(picks)
            row["picks"] = picks
            print(f"  [daily] +入库爆款命中 {len(picks)} 条（当日 {len(_today_ids(conn))}/{a.quota}）")
        else:
            print("  [daily] 本关键词无达标入选（原始数据已入库）")
        row["status"] = "成功"

    # 实时清理：数据已在 SQLite（或确认本来就没数据）→ 删除整段运行目录，零 JSON 残留
    if ingested or not comm_fps:
        shutil.rmtree(run_root, ignore_errors=True)
        row["cleaned"] = not os.path.isdir(run_root)
        if row["cleaned"]:
            print(f"  [daily] 已清理采集产物目录（数据在库，无 JSON 残留）: {run_root}")
    return row


def _print_report(conn, a, rows, t0):
    """输出当日采集报告：逐词统计 / 当日命中明细 / 库内累计 / 停止原因。"""
    today = datetime.date.today().isoformat()
    ids = _today_ids(conn)
    used = len(ids)
    print()
    print("=" * 62)
    print(f"[当日采集报告] {today}  库: {a.db or _db.DEFAULT_DB}")
    print("-" * 62)
    if rows:
        for r in rows:
            line = (f"  「{r['kw']}」 采集视频 {r['videos']} / 入库评论 {r['comments']} / "
                    f"新增命中 {r['new_hits']}  [{r['status']}]")
            if r["note"]:
                line += f"  {r['note'][:48]}"
            print(line)
    else:
        print("  （无采集记录）")
    print("-" * 62)
    print(f"  当日入池: {used}/{a.quota}" + ("（已达标）" if used >= a.quota else "（未达标，可再跑补足）"))
    for h in conn.execute(
            """SELECT h.score, c.like_count, c.sub_comment_count, c.content
               FROM hits h JOIN comments c ON c.comment_id=h.comment_id
               WHERE h.hit_date=? ORDER BY h.score DESC""", (today,)):
        print(f"    {h['score']:.0f}分 {h['like_count']}赞/{h['sub_comment_count']}回 "
              f"{(h['content'] or '').strip()[:36]}")
    tot = conn.execute(
        "SELECT (SELECT COUNT(*) FROM videos) AS v, (SELECT COUNT(*) FROM comments) AS c, "
        "(SELECT COUNT(*) FROM hits) AS h").fetchone()
    print(f"  库内累计: 视频 {tot['v']} / 评论 {tot['c']} / 爆款命中 {tot['h']}")
    print(f"  耗时 {time.time() - t0:.0f}s  JSON 残留: 0（采集产物已随入库清理）")
    print("=" * 62)
    print("[下一阶段] 爆款榜查询：python sqlite/report.py --hot")


def _run_realtime(conn, a):
    """实时采集主路径：逐关键词 采集→实时入库→内存筛选→hits→清理；达标即停；末尾报告。"""
    keyword_list = [k.strip() for k in (a.keywords or "").split(";") if k.strip()]
    if not keyword_list:
        sys.exit("[ERR] 实时采集需 --keywords（分号分隔）")
    if a.dry_run:
        print(f"[daily] dry-run：将采集 {len(keyword_list)} 个关键词，配额 {a.quota}（不写入库）")
        return 0

    t0 = time.time()
    rows = []
    for kw in keyword_list:
        # 达标即停（硬保证）：每个关键词开抓前复查当日配额
        if len(_today_ids(conn)) >= a.quota:
            print("[达标即停] 今日配额已满，无需继续采集，直接停止")
            break
        print(f"[daily] 采集关键词「{kw}」")
        rc, run_root = _crawl_keyword(a, kw)
        if rc != 0:
            print(f"  [daily] 关键词「{kw}」采集退出码 {rc}；若有部分数据仍会实时入库")
        if not run_root or not os.path.isdir(run_root):
            rows.append({"kw": kw, "videos": 0, "comments": 0, "new_hits": 0,
                         "status": "无运行目录", "note": f"rc={rc}", "picks": [],
                         "cleaned": False})
            continue
        rows.append(_ingest_keyword_run(conn, a, kw, run_root))

    # 收尾：删运行指针（数据全在 SQLite，控制文件也不留 JSON）
    pointer = os.path.join(a.root, f".douyin-crawl-current-{a.account}.json")
    try:
        os.remove(pointer)
    except OSError:
        pass

    _print_report(conn, a, rows, t0)
    return 0


if __name__ == "__main__":
    sys.exit(main())

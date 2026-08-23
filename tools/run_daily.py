# -*- coding: utf-8 -*-
"""爆款评论池·每日调度（核心编排：采样→筛选→每日配额达标即停→沉淀→断点续跑）。

单一命令完成"今天刷一堆爆款评论"，主路径为实时 MediaCrawler 采集：
  1) 关键词广撒网→ MediaCrawler 直接抓视频+评论（collect_search.py 内嵌调度，零外部技能依赖）；
  2) 各关键词评论经 aggregate_comments 聚合为 comments.json（临时中间产物，不入池）；
  3) 三级门槛筛选（感高互动→字数→可成文性，复用 filter_pool.py 函数）；
  4) 达标即停：每天最多入池 QUOTA 条（默认 5）。当日已入选数取自 hits 表（hit_date=今天），
     达到 QUOTA 立即结束（不再继续采集）；
  5) 入库：原始采集数据经 loader 幂等导入 accounts/videos/comments/ancestry/batches；
     筛选精选爆款写入 hits 表（爆款榜 / 达标即停依据）。
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

产物: 技能内置 SQLite（sqlite/douyin_hotpool.db）
      - 采集原始数据 → accounts / videos / comments / ancestry / batches（loader 幂等）
      - 精选爆款命中 → hits 表（爆款榜 / 达标即停）
      采集数据默认入库，不再输出 pool/<account>.json。
"""
import argparse
import datetime
import json
import os
import subprocess
import sys

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

    # 过滤重复 + 按分排序取满 remaining
    fresh = [c for c in cands if not _already(ids_today, c)]
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
    # 主路径：实时 MediaCrawler 关键词采集 → 聚合 → 三级筛选 → 入库(hits+原始) → 达标即停
    if a.offline_source:
        if not os.path.isfile(a.offline_source):
            sys.exit(f"[ERR] --offline-source 不存在: {a.offline_source}（仅为调试用，正常应实时采集）")
        print("[提示] 你正在用离线模式（--offline-source），仅适用于调试；主流程应走实时 MediaCrawler 采集。")
        return _run_offline(conn, a.offline_source, a.account, a.quota, a, a.dry_run)

    return _run_offline_collect(conn, a)


def _run_offline_collect(conn, a):
    """实时采集一口闷：逐关键词→聚合→三级筛选→入库（原始数据 loader + 精选 hits）。失败不中断，收集到配额即止。"""
    root = a.root
    account = a.account

    collect_args = [sys.executable,
                    os.path.join(os.path.dirname(os.path.abspath(__file__)), "collect_search.py")]
    keyword_list = [k.strip() for k in (a.keywords or "").split(";") if k.strip()]
    if not keyword_list:
        sys.exit("[ERR] 实时采集需 --keywords（分号分隔）")
    if a.dry_run:
        print(f"[daily] dry-run：将采集 {len(keyword_list)} 个关键词，配额 {a.quota}（不写入库）")
        return 0

    for kw in keyword_list:
        if len(_today_ids(conn)) >= a.quota:
            print("[达标即停] 今日配额已满，停止后续采集")
            break
        print(f"[daily] 采集关键词「{kw}」")
        sub_args = ["--root", root, "--account", account, "--keywords", kw,
                    "--per-keyword", str(a.per_keyword), "--comments-count", str(a.comments_count),
                    "--speed", a.speed, "--lt", a.lt, "--sleep-min", str(a.sleep_min),
                    "--sleep-max", str(a.sleep_max), "--retry-fail", str(a.retry_fail),
                    "--max-min", str(a.max_min)]
        if a.cookies:
            sub_args += ["--cookies", a.cookies]
        r = subprocess.run(collect_args + sub_args)
        if r.returncode != 0:
            print(f"  [daily] 关键词「{kw}」采集退出码 {r.returncode}；"
                  "不丢弃已有部分数据，下方仍尝试聚合本次抓取的存量评论")
        # 定位最新 run 目录找聚合产物：无论成功与否，只要已抓到评论就聚合入池
        # （避免「抓了一部分却在超时/重试失败时整轮返 0 被当成无新增，丢掉有效样本」）
        run_root = None
        pointer = os.path.join(root, f".douyin-crawl-current-{account}.json")
        if os.path.isfile(pointer):
            try:
                run_root = json.load(open(pointer, encoding="utf-8")).get("run_root")
            except Exception:
                pass
        if not run_root:
            continue
        # 本次采集的原始数据默认入库（loader 幂等）；聚合临时 json 只作筛选输入
        crawl_dir = os.path.join(run_root, "crawl_" + account)
        try:
            res = _loader.import_batch(conn, crawl_dir, keyword=kw, account=account)
            conn.commit()
            print(f"  [daily] 原始数据已入库: 视频 {res['items']['videos']} / 评论 {res['items']['comments']}")
        except Exception as e:
            print(f"  [daily] 原始采集数据入库失败: {e}")

        ag = os.path.join(run_root, "comments_aggregated.json")
        # run_root 复用同一目录，上一轮/此前会残留旧聚合文件；
        # 本次已新采集成功，必须删除旧聚合并强制用最新全量数据重新聚合，否则会调度旧数据漏掉本轮爆款。
        if os.path.isfile(ag):
            try:
                os.remove(ag)
            except OSError:
                pass
        ag = _aggregate(root, account, run_root)
        if not ag:
            continue
        # 筛选精选 → 写 hits 表
        recs = json.load(open(ag, encoding="utf-8"))
        cands = _screen(list(filter_pool.iter_records(recs)), a)
        cands.sort(key=lambda c: (c["score"], c["like_count"]), reverse=True)
        ids = _today_ids(conn)
        picks = []
        for c in cands:
            if len(ids) + len(picks) >= a.quota:
                break
            if _already(ids, c):
                continue
            picks.append(c)
        if picks:
            _hits.write_hits(conn, picks)
            conn.commit()
            print(f"  [daily] +入库爆款命中 {len(picks)} 条（当日 {len(_today_ids(conn))}/{a.quota}）")
        else:
            print("  [daily] 本关键词无达标入选（原始数据已入库）")
        # 聚合临时 json 用后即删（采集数据默认入库，不落 json 沉淀）
        try:
            os.remove(ag)
        except OSError:
            pass

    print(f"[daily] 实时采集完成：今日入池 {len(_today_ids(conn))}/{a.quota} → {a.db or _db.DEFAULT_DB}")
    return 0


def _aggregate(root, account, run_root):
    src = os.path.join(run_root, "crawl_" + account)
    ag = os.path.join(run_root, "comments_aggregated.json")
    cmd = [sys.executable,
           os.path.join(os.path.dirname(os.path.abspath(__file__)), "aggregate_comments.py"),
           "--in", src, "--out", ag]
    r = subprocess.run(cmd)
    return ag if r.returncode == 0 and os.path.isfile(ag) else None


if __name__ == "__main__":
    sys.exit(main())
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
  生产策略固定：仅允许调整当日配额 QUOTA。

用法（主路径-实时采集）:
  python tools/run_daily.py --root <工作根> --account <slug> --keywords "甲;乙;丙"
      [--quota 5]

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
import builtins as _bi

def _flush_print(*a, **k):
    k.setdefault("flush", True)
    _bi.print(*a, **k)
print = _flush_print


TOOLS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TOOLS)
import aggregate_comments  # noqa: E402
import filter_pool  # noqa: E402
import ai_scorer as _ai  # noqa: E402
import _presets  # noqa: E402
_SQLITE_DIR = os.path.join(os.path.dirname(TOOLS), "sqlite")
sys.path.insert(0, _SQLITE_DIR)
import db as _db  # noqa: E402
import loader as _loader  # noqa: E402
import hits_backfill as _hits  # noqa: E402


# 生产采集策略固定，避免调用方通过临时调参改变覆盖面、频率或筛选口径。
# 可变输入仅保留工作目录、账号、关键词和当日爆款池配额。
FIXED_REALTIME_POLICY = {
    "preset": "safe",
    "per_keyword": 0,
    "comments_count": 30,
    "speed": "safe",
    "lt": "qrcode",
    "sleep_min": 6,
    "sleep_max": 15,
    "retry_fail": 2,
    "stop_at_like_floor": True,
    "sort_by_likes": True,
    "search_sort": 1,
    "query_correct": 1,
    "headless": False,
    "kw_gap": 90,
    "max_min": 45,
    "min_likes": 1000,
    "min_replies": 50,
    "video_min_likes": 10000,
    "min_len": 20,
    "min_score": 55,
    "stale_stop_sec": 120,
}

LOCKED_REALTIME_OPTIONS = frozenset(FIXED_REALTIME_POLICY) | {
    "cookies",
    "db",
    "dry_run",
    "offline_source",
}


def _enforce_fixed_realtime_policy(args, argv):
    """Reject runtime tuning and apply the single approved crawl profile.

    唯一豁免：--offline-source 离线调试路径（SKILL 约定不作为日常入口），
    允许显式调参 + 必须显式传隔离 --db，防止调试数据污染生产库。
    """
    specified = _presets.parse_present_flags(argv)
    if args.quota <= 0:
        sys.exit("[ERR] --quota 必须为正整数")
    if args.offline_source:
        if not args.db:
            sys.exit("[ERR] 离线调试模式（--offline-source）必须同时显式传 --db 指向隔离库")
        print("[调试模式] 离线排障路径：参数锁不生效；该入口不得用于日常采集")
        args.screen_engine = "rules"   # 离线自测固定规则评分，保证确定性
        _presets.apply_preset(args, argv)
        return
    locked = sorted(name.replace("_", "-") for name in specified & LOCKED_REALTIME_OPTIONS)
    if locked:
        sys.exit(
            "[ERR] 采集参数已固化，仅允许调整 --quota；"
            f"不可传入: {', '.join('--' + name for name in locked)}"
        )
    for name, value in FIXED_REALTIME_POLICY.items():
        setattr(args, name, value)
    print(
        "[固定采集策略] safe | 最多点赞排序 | 不限翻页 | 万赞止停 | "
        "30评论/视频 | 三级筛选 | 120s熔断；仅 --quota 可调"
    )
    for name, value in FIXED_REALTIME_POLICY.items():
        setattr(args, name, value)
    print(
        "[固定采集策略] safe | 最多点赞排序 | 不限翻页 | 万赞止停 | "
        "30评论/视频 | 三级筛选 | 120s熔断；仅 --quota 可调"
    )


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
    with open(source, encoding="utf-8") as f:
        records = json.load(f)
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


def _screen_engine():
    """③可成文性判定引擎：配置了 API → api；否则 agent 队列（由值班 Agent 评审）。"""
    if getattr(_ai, "available", lambda: False)():
        return "api"
    return "agent"


def _emit_judge_queue(a, keyword, cands):
    """Agent 判分模式：把过①②门槛的候选落到队列文件，供值班 Agent 评审入池。"""
    if not cands:
        return None
    path = os.path.join(a.root, f".hcp-judge-{a.account}.jsonl")
    rows = [{
        "comment_id": c["comment_id"], "aweme_id": c["aweme_id"],
        "content": c["content"], "like_count": c["like_count"],
        "sub_comment_count": c["sub_comment_count"], "keyword": keyword,
    } for c in cands[:50]]
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, path)
    print(f"[AI判分] 已落 Agent 评审队列 {len(rows)} 条（过①②门槛待③判定）→ {path}")
    return path


def _screen(blobs, args):
    """三级门槛：①②固定规则；③可成文性按引擎判定（api 自动 / rules 确定性 / agent 延迟）。

    agent 模式下候选 score=None，不自动入池，只进评审队列；quota 由 Agent 写 hits 后驱动。
    """
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
        out.append({
            "aweme_id": str(r.get("aweme_id") or ""),
            "comment_id": str(r.get("comment_id") or ""),
            "nickname": r.get("nickname") or "",
            "content": content,
            "like_count": likes,
            "sub_comment_count": replies,
            "create_time": r.get("create_time"),
            "len": elen, "score": None, "score_breakdown": {}, "reasons": [],
        })

    engine = getattr(args, "screen_engine", None) or _screen_engine()
    if engine == "rules":
        for c in out:
            score, bd, why = filter_pool._score(c["content"])
            c.update(score=score, score_breakdown=bd, reasons=why)
        out = [c for c in out if c["score"] >= args.min_score]
        out.sort(key=lambda c: (c["score"], c["like_count"]), reverse=True)
    elif engine == "api" and out:
        try:
            judged = _ai.judge(out)
            for c, (score, reason) in zip(out, judged):
                c.update(score=score,
                         score_breakdown=_ai.breakdown(),
                         reasons=[reason])
            out = [c for c in out if c["score"] >= args.min_score]
            out.sort(key=lambda c: (c["score"], c["like_count"]), reverse=True)
        except Exception as e:
            print(f"[AI判分] API 不可用({e})，本轮降级规则评分")
            for c in out:
                score, bd, why = filter_pool._score(c["content"])
                c.update(score=score, score_breakdown=bd, reasons=why)
            out = [c for c in out if c["score"] >= args.min_score]
            out.sort(key=lambda c: (c["score"], c["like_count"]), reverse=True)
    else:   # agent
        out.sort(key=lambda c: (c["like_count"], c["sub_comment_count"]), reverse=True)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--account", default="pool")
    ap.add_argument("--keywords", required=True, help="关键词，分号分隔；实时 MediaCrawler 采集+筛选为主路径")
    ap.add_argument("--preset", default="safe", choices=["safe", "ultra", "fast"],
                    help="采集档位：safe=慢档(并发1/延时6-15s，稳)，ultra=超稳档(并发1/延时12-28s+词间长休息，风控期/长跑用)，fast=快档(并发3/延时2-6s，提速但风控面大)。默认 safe。")
    ap.add_argument("--per-keyword", type=int, default=0,
                    help="每关键词最多采样视频数；0=不限（配合 --stop-at-like-floor，按视频1万赞门槛自然结束，不再固定15条）")
    ap.add_argument("--comments-count", type=int, default=30,
                    help="单视频最多一级评论数（改小加快）")
    ap.add_argument("--speed", default="safe", choices=["safe", "normal", "fast"],
                    help="并发 level: safe=1 / normal=2 / fast=3（被显式指定时优先于 --preset）")
    ap.add_argument("--lt", default="qrcode")
    ap.add_argument("--cookies", default=None)
    ap.add_argument("--sleep-min", type=float, default=3)
    ap.add_argument("--sleep-max", type=float, default=3)
    ap.add_argument("--retry-fail", type=int, default=2)
    ap.add_argument("--stop-at-like-floor", action="store_true",
                    help="按赞排序抓到首个低于1万门槛的视频即结束本词（自动启用 --sort-by-likes；替代固定 per-keyword 条数）")
    ap.add_argument("--sort-by-likes", action="store_true",
                    help="搜索结果按最多点赞排序（实测严格赞降序；配合万赞门槛几乎零浪费。注意：同词结果固定，重复跑依赖跳过去重）")
    ap.add_argument("--search-sort", dest="search_sort", type=int, default=None,
                    help="显式搜索排序 0=综合 1=最多点赞 2=最新发布（默认 None=跟随约定）。注意：需配 --per-keyword>0，否则 per-keyword<=0 会自动切回最多点赞")
    ap.add_argument("--query-correct", dest="query_correct", type=int, default=None, choices=[0, 1],
                    help="搜索纠错开关（透传 collect）：0=关闭纠错（组合词空结果可试）1=允许纠错（默认）")
    ap.add_argument("--headless", action="store_true",
                    help="后台无痕采集（默认关闭=有头浏览，绑定指纹防空响应）。默认即有头，加此参数才无头")
    ap.add_argument("--kw-gap", dest="kw_gap", type=float, default=90,
                    help="关键词轮次之间的休息秒数（加大采集间隔、降风控面；0=关闭。ultra 档默认 180）")
    ap.add_argument("--max-min", type=float, default=45)
    ap.add_argument("--min-likes", type=int, default=1000)
    ap.add_argument("--min-replies", type=int, default=50)
    ap.add_argument("--video-min-likes", type=int, default=10000,
                    help="抓取级视频点赞门槛：低于该赞跳过评论深挖（透传给 collect_search）")
    ap.add_argument("--min-len", type=int, default=30)
    ap.add_argument("--min-score", type=float, default=55)
    ap.add_argument("--quota", type=int, default=5)
    ap.add_argument("--stale-stop-sec", type=float, default=120,
                    help="实时采集连续无视频/评论/候选新增超过此秒数后自动停止；0=关闭")
    ap.add_argument("--db", default=None,
                    help="SQLite 库路径（默认技能内置 sqlite/douyin_hotpool.db；测试可传隔离库）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--offline-source", default=None,
                    help="仅调试：喂存量聚合 JSON 做筛选（跳过采集），日常主路径不要用")
    a = ap.parse_args()
    _enforce_fixed_realtime_policy(a, sys.argv)

    conn = _open_db(a.db)  # 采集数据默认入库 SQLite
    # 主路径：逐关键词 实时采集→实时入库→内存筛选→hits→清理 JSON→达标即停→报告
    if a.offline_source:
        if not os.path.isfile(a.offline_source):
            sys.exit(f"[ERR] --offline-source 不存在: {a.offline_source}（仅为调试用，正常应实时采集）")
        print("[提示] 你正在用离线模式（--offline-source），仅适用于调试；主流程应走实时 MediaCrawler 采集。")
        result = _run_offline(conn, a.offline_source, a.account, a.quota, a, a.dry_run)
        conn.close()
        return result

    try:
        result = _run_realtime(conn, a)
        conn.close()
        return result
    except KeyboardInterrupt:
        # Keep interruption observable and remove only this account's controls.
        conn.execute("""UPDATE batches SET status='failed', phase='collector_interrupted',
                       error='keyboard_interrupt', finished_at=datetime('now','localtime')
                       WHERE account=? AND status IN ('running','collecting','ingesting','screening','cleaning')""",
                     (a.account,))
        conn.commit()
        for leftover in (os.path.join(a.root, f".douyin-crawl-current-{a.account}.json"),
                         os.path.join(a.root, f".hcp-skip-{a.account}.txt")):
            try:
                os.remove(leftover)
            except OSError:
                pass
        print("[中断收尾] 已标记批次并清理控制文件；已落盘数据保留供下次幂等导入")
        conn.close()
        return 130


def _crawl_keyword(a, kw, skip_file=None, conn=None, batch_id=None):
    """调 collect_search 采集单个关键词。返回 (returncode, run_root|None)。

    skip_file：已采视频ID列表文件，命中的视频在评论阶段被跳过（降重复抓取）。
    """
    sub_args = ["--root", a.root, "--account", a.account, "--keywords", kw,
                "--per-keyword", str(a.per_keyword), "--comments-count", str(a.comments_count),
                "--speed", a.speed, "--lt", a.lt, "--sleep-min", str(a.sleep_min),
                "--sleep-max", str(a.sleep_max), "--retry-fail", str(a.retry_fail),
                "--max-min", str(a.max_min)]
    if a.cookies:
        sub_args += ["--cookies", a.cookies]
    if skip_file:
        sub_args += ["--skip-file", skip_file]
    if a.headless:
        sub_args += ["--headless"]
    else:
        sub_args += ["--no-headless"]
    sub_args += ["--min-likes", str(a.min_likes), "--min-replies", str(a.min_replies)]
    # 排序取值：显式 --search-sort 优先；否则按赞排序开关/止停/不限条数时取最多点赞
    sort = a.search_sort
    if sort is None:
        if getattr(a, "sort_by_likes", False) or getattr(a, "stop_at_like_floor", False) \
                or a.per_keyword <= 0:
            sort = 1
    if a.search_sort is not None and a.per_keyword <= 0:
        print("[提示] --search-sort 显式指定需配 --per-keyword>0；当前 per-keyword<=0 会强制切回最多点赞，可能覆盖预期排序")
        sort = 1
    # per_keyword<=0（不限条数）自动启用按赞止停，避免无界翻页
    if getattr(a, "stop_at_like_floor", False) or a.per_keyword <= 0:
        sub_args += ["--stop-at-like-floor"]
    if sort is not None:
        sub_args += ["--search-sort", str(sort)]
    if getattr(a, "query_correct", None) is not None:
        sub_args += ["--query-correct", str(a.query_correct)]
    if getattr(a, "video_min_likes", 10000) != 10000:
        sub_args += ["--video-min-likes", str(a.video_min_likes)]
    cmd = [sys.executable, os.path.join(TOOLS, "collect_search.py")] + sub_args
    if getattr(a, "live_screen", True):
        return _crawl_keyword_live(a, kw, cmd, conn, batch_id)
    r = subprocess.run(cmd)
    run_root = None
    pointer = os.path.join(a.root, f".douyin-crawl-current-{a.account}.json")
    if os.path.isfile(pointer):
        try:
            run_root = json.load(open(pointer, encoding="utf-8")).get("run_root")
        except Exception:
            pass
    return r.returncode, run_root


def _crawl_keyword_live(a, kw, cmd, conn, batch_id):
    """流式采集并周期性增量筛选；配额满时立即停止子进程。"""
    proc = subprocess.Popen(cmd, cwd=TOOLS, stdout=None, stderr=None)
    pointer = os.path.join(a.root, f".douyin-crawl-current-{a.account}.json")
    last_probe = 0.0
    last_report = 0.0
    last_activity = time.time()
    last_signature = (0, 0, 0, 0)
    stopped_for_stale = False
    stopped_for_quota = False
    state = {"videos": 0, "comments": 0, "candidates": 0, "hits": 0,
             "sample": "", "last_video": "", "last_comment": ""}
    try:
        while proc.poll() is None:
            now = time.time()
            if now - last_probe >= 4:
                run_root = None
                try:
                    with open(pointer, encoding="utf-8") as f:
                        run_root = json.load(f).get("run_root")
                except (OSError, ValueError, TypeError):
                    pass
                if run_root:
                    reached = _incremental_screen(conn=conn, args=a, keyword=kw,
                                                  batch_id=batch_id, run_root=run_root, state=state)
                    if reached:
                        print(f"[实时达标] 关键词「{kw}」命中已达 {a.quota} 条，停止后续采集")
                        proc.terminate()
                        stopped_for_quota = True
                        break
                last_probe = now
                signature = (state["videos"], state["comments"], state["candidates"], state["hits"])
                if signature != last_signature:
                    last_signature = signature
                    last_activity = now
            if now - last_report >= 60:
                _print_live_report(conn, a, kw, state)
                last_report = now
            if len(_today_ids(conn)) >= a.quota:
                print(f"[实时达标] 关键词「{kw}」命中已达 {a.quota} 条，停止后续采集")
                proc.terminate()
                stopped_for_quota = True
                break
            if (a.stale_stop_sec > 0 and last_signature != (0, 0, 0, 0)
                    and now - last_activity >= a.stale_stop_sec):
                print(f"[无新增熔断] 关键词「{kw}」连续 {now - last_activity:.0f}s 无视频/评论/候选新增，停止采集")
                proc.terminate()
                stopped_for_stale = True
                break
            time.sleep(0.5)
        rc = proc.wait()
    except KeyboardInterrupt:
        print(f"[中断收尾] 关键词「{kw}」收到停止信号，终止采集子进程并保留已落盘数据")
        if proc.poll() is None:
            proc.terminate()
        rc = proc.wait()
        if batch_id:
            _db.update_batch(conn, batch_id, status="failed", phase="collector_interrupted",
                             error="keyboard_interrupt")
    finally:
        # 最后一轮由收尾导入负责，避免截断时漏掉已落盘的尾部数据。
        pass
    try:
        run_root = json.load(open(pointer, encoding="utf-8")).get("run_root")
    except (OSError, ValueError, TypeError):
        run_root = None
    return (0 if stopped_for_quota or stopped_for_stale else rc), run_root


def _print_live_report(conn, args, keyword, state):
    """输出可直接转发的 Markdown 实时进度，数据全部来自当前 SQLite 快照。"""
    today_hits = len(_today_ids(conn))
    print("\n### 抖音爆款评论采集进度")
    print(f"- **关键词**：`{keyword}`")
    print("- **阶段**：实时采集 / 增量入库 / 三级筛选")
    print(f"- **累计漏斗**：视频 **{state['videos']}** | 评论 **{state['comments']}** | "
          f"候选 **{state['candidates']}** | 新命中 **{state['hits']}** | "
          f"当日配额 **{today_hits}/{args.quota}**")
    print("\n| 最新视频 | 作者 | 视频点赞 | 视频评论 |")
    print("|---|---|---:|---:|")
    video_meta = state.get("last_video_meta", ("-", "-", "-"))
    print(f"| {state.get('last_video') or '暂无新视频记录'} | {video_meta[0]} | "
          f"{video_meta[1]} | {video_meta[2]} |")
    print("\n| 最新评论 | 评论点赞 | 回复数 | 筛选结果 |")
    print("|---|---:|---:|---|")
    comment_meta = state.get("last_comment_meta", ("-", "-"))
    print(f"| {state.get('last_comment') or '暂无新评论记录'} | {comment_meta[0]} | "
          f"{comment_meta[1]} | 已入库 |")
    if state.get("sample"):
        print(f"\n**最新候选**：{state['sample'][:180]}")
    print("\n- **下一检查点**：约 60 秒后；连续无视频、评论或候选新增将触发停滞熔断。")


def _refresh_live_examples(conn, batch_id, state):
    """从本批次库内数据刷新最新视频与评论摘要，避免只汇报计数。"""
    video = conn.execute(
        "SELECT title, nickname, liked_count, comment_count FROM videos "
        "WHERE first_seen_batch=? ORDER BY rowid DESC LIMIT 1", (batch_id,)).fetchone()
    comment = conn.execute(
        "SELECT content, like_count, sub_comment_count FROM comments "
        "WHERE batch_id=? ORDER BY rowid DESC LIMIT 1", (batch_id,)).fetchone()
    if video:
        title = (video["title"] or "").replace("|", "\\|").replace("\n", " ")[:80]
        state["last_video"] = f"{title or '无标题'}"
        state["last_video_meta"] = (video["nickname"] or "未知作者", video["liked_count"] or 0,
                                     video["comment_count"] or 0)
    if comment:
        content = (comment["content"] or "").replace("|", "\\|").replace("\n", " ")[:100]
        state["last_comment"] = content or "空文本评论"
        state["last_comment_meta"] = (comment["like_count"] or 0, comment["sub_comment_count"] or 0)


def _sort_cands(cands):
    """按评分降序；未判分(agent 模式 score=None)排最后，仅作展示排序。"""
    cands.sort(key=lambda c: (-1 if c.get("score") is None else c["score"],
                              c["like_count"]), reverse=True)


def _incremental_screen(conn, args, keyword, batch_id, run_root, state):
    """对当前 JSONL 快照增量入库和筛选；所有操作幂等，可重复调用。"""
    crawl_dir = os.path.join(run_root, "crawl_" + args.account)
    files = glob.glob(os.path.join(crawl_dir, "**", "search_comments_*.jsonl"), recursive=True)
    if not files:
        return False
    try:
        res = _loader.import_batch(conn, crawl_dir, keyword=keyword,
                                   account=args.account, batch_id=batch_id or "")
        state["videos"] = max(state["videos"], res["items"]["videos"])
        state["comments"] = max(state["comments"], res["items"]["comments"])
        _refresh_live_examples(conn, batch_id, state)
        by_aweme, summary = aggregate_comments.aggregate_paths(files, max_n=100)
        cands = _screen(list(filter_pool.iter_records({"summary": summary, "by_aweme": by_aweme})), args)
        _sort_cands(cands)
        state["candidates"] = len(cands)
        if cands:
            top = cands[0]
            state["sample"] = (f"{top['like_count']}赞/{top['sub_comment_count']}回/{top['score']}分 "
                               f"{top['content']}")
        ids = _today_ids(conn)
        done_ids, done_texts = _dup_guard(conn)
        picks = [c for c in cands if c.get("score") is not None
                 and not _already(ids, c) and not _is_dup(c, done_ids, done_texts)]
        remaining = max(0, args.quota - len(ids))
        if picks and remaining:
            n = _hits.write_hits(conn, picks[:remaining], batch_id=batch_id or "")
            state["hits"] += n
            pick = picks[0]
            state["sample"] = (f"{pick['like_count']}赞/{pick['sub_comment_count']}回/{pick['score']}分 "
                               f"{pick['content']}")
            print(f"[实时筛选] 关键词「{keyword}」: 新候选 {len(picks)}，入池 {n}，"
                  f"累计命中 {len(_today_ids(conn))}/{args.quota}；样例={state['sample'][:80]}")
        elif _screen_engine() == "agent":
            _emit_judge_queue(args, keyword, cands)
        if batch_id:
            _db.update_batch(conn, batch_id, status="collecting", phase="live_screen",
                             videos_count=state["videos"], comments_count=state["comments"])
        return len(_today_ids(conn)) >= args.quota
    except Exception as e:
        print(f"[实时筛选] 暂不可筛选，下一轮重试: {e}")
        return False
    finally:
        conn.commit()


def _export_skip_file(conn, root, account):
    """把库内已采过评论的视频 ID 导出到临时文件（供采集进程跳过重复抓取）。

    判定口径：该视频已有 ≥1 条评论入库（说明评论抓取完成过）。
    文件落在运行指针同目录，返回路径；库为空时返回 None（不启用跳过）。
    """
    try:
        ids = [r["aweme_id"] for r in conn.execute(
            "SELECT DISTINCT aweme_id AS aweme_id FROM comments WHERE aweme_id != ''")]
    except Exception as e:
        print(f"  [daily] 已采视频导出失败（本次不启用跳过）: {e}")
        return None
    if not ids:
        return None
    p = os.path.join(root, f".hcp-skip-{account}.txt")
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(ids))
    os.replace(tmp, p)
    return p


def _begin_batch(conn, a, kw, skipped_count=0):
    bid = f"{a.account}.{kw}.{datetime.datetime.now().strftime('%H%M%S')}"
    conn.execute(
        """INSERT INTO batches(batch_id, run_root, account, keyword, started_at,
                  status, phase, skipped_count)
           VALUES(?,?,?,?,datetime('now','localtime'),'running','collecting',?)
           ON CONFLICT(batch_id) DO UPDATE SET status='running', phase='collecting',
                  skipped_count=excluded.skipped_count, error=''""",
        (bid, a.root, a.account, kw, skipped_count))
    conn.commit()
    return bid


def _ingest_keyword_run(conn, a, kw, run_root, bid=None):
    """单关键词实时入库：loader 导入→内存聚合+三级筛选→hits(配额封顶)→清理运行目录。

    返回报表行 dict（kw/videos/comments/new_hits/status/picks/cleaned）。
    清理策略：入库成功或确认无数据 → 整段删除 run_root（零 JSON 残留）；入库失败保留现场。
    """
    row = {"kw": kw, "videos": 0, "comments": 0, "new_hits": 0,
           "status": "空结果", "note": "", "picks": [], "cleaned": False}
    if not bid:
        bid = _begin_batch(conn, a, kw)
    row["new_hits"] = conn.execute(
        "SELECT COUNT(*) AS n FROM hits WHERE batch_id=? AND hit_date=?",
        (bid, datetime.date.today().isoformat())).fetchone()["n"]
    crawl_dir = os.path.join(run_root, "crawl_" + a.account)
    comm_fps = sorted(glob.glob(os.path.join(crawl_dir, "**", "search_comments_*.jsonl"),
                                recursive=True))
    if not comm_fps:
        row["note"] = ("无评论产物；多为未登录/风控空响应，可重新扫码登录后重试，"
                       "或用隔离排障工具 collect_search.py 单词探测")
        print(f"  [daily] {row['note']}")

    ingested = False
    if comm_fps:
        try:
            _db.update_batch(conn, bid, status="ingesting", phase="raw_import")
            res = _loader.import_batch(conn, crawl_dir, keyword=kw, account=a.account,
                                       batch_id=bid)
            conn.commit()
            ingested = True
            row["videos"], row["comments"] = res["items"]["videos"], res["items"]["comments"]
            print(f"  [daily] 实时入库: 视频 {row['videos']} / 评论 {row['comments']}")
        except Exception as e:
            row["status"] = "入库失败"
            row["note"] = str(e)
            _db.update_batch(conn, bid, status="failed", phase="raw_import", error=str(e), finished=True)
            print(f"  [daily] 原始采集数据入库失败（保留现场不删文件）: {e}")

    if ingested:
        _db.update_batch(conn, bid, status="screening", phase="three_gate")
        # 三级筛选：内存聚合（不落聚合 JSON）→ 打分 → 配额封顶写入 hits
        by_aweme, summary = aggregate_comments.aggregate_paths(comm_fps, max_n=100)
        cands = _screen(list(filter_pool.iter_records(
            {"summary": summary, "by_aweme": by_aweme})), a)
        _sort_cands(cands)
        ids = _today_ids(conn)
        done_ids, done_texts = _dup_guard(conn)   # 跨天去重：历史命中不再重复入选
        picks = []
        for c in cands:
            if len(ids) + len(picks) >= a.quota:
                break
            if c.get("score") is None or _already(ids, c) or _is_dup(c, done_ids, done_texts):
                continue
            picks.append(c)
        if picks:
            _hits.write_hits(conn, picks)
            conn.commit()
            row["new_hits"] += len(picks)
            row["picks"] = picks
            print(f"  [daily] +入库爆款命中 {len(picks)} 条（当日 {len(_today_ids(conn))}/{a.quota}）")
        else:
            print("  [daily] 本关键词无达标入选（原始数据已入库）")
            if _screen_engine() == "agent":
                _emit_judge_queue(a, kw, cands)
        row["status"] = "成功"
        _db.update_batch(conn, bid, status="cleaning", phase="cleanup")

    # 实时清理：数据已在 SQLite（或确认本来就没数据）→ 删除整段运行目录，零 JSON 残留
    if ingested or not comm_fps:
        shutil.rmtree(run_root, ignore_errors=True)
        row["cleaned"] = not os.path.isdir(run_root)
        if row["cleaned"]:
            print(f"  [daily] 已清理采集产物目录（数据在库，无 JSON 残留）: {run_root}")
            _db.update_batch(conn, bid, status="completed", phase="done", finished=True)
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
    skip_file = None
    for i, kw in enumerate(keyword_list, 1):
        # 达标即停（硬保证）：每个关键词开抓前复查当日配额
        if len(_today_ids(conn)) >= a.quota:
            print("[达标即停] 今日配额已满，无需继续采集，直接停止")
            break
        # 词间休息（加大采集间隔、降风控面）：仅在本词之后还有词要抓且配额未满时休息
        if i > 1 and a.kw_gap and a.kw_gap > 0:
            print(f"[词间休息] {a.kw_gap:.0f}s 后继续下一关键词（--kw-gap 可调）")
            time.sleep(a.kw_gap)
        # 已采视频跳过：每词开抓前重新导出（上一词入库后集合会变大）
        skip_file = _export_skip_file(conn, a.root, a.account)
        if skip_file:
            n_known = sum(1 for _ in open(skip_file, encoding="utf-8"))
            print(f"[daily] 已采视频 {n_known} 个启用跳过（重复视频不重抓评论）")
        n_known = n_known if skip_file else 0
        bid = _begin_batch(conn, a, kw, skipped_count=n_known)
        print(f"[daily] 采集关键词「{kw}」")
        rc, run_root = _crawl_keyword(a, kw, skip_file, conn=conn, batch_id=bid)
        if rc != 0:
            print(f"  [daily] 关键词「{kw}」采集退出码 {rc}；若有部分数据仍会实时入库")
            _db.update_batch(conn, bid, status="collecting", phase="collector_exit", error=f"collector_rc={rc}")
        if not run_root or not os.path.isdir(run_root):
            _db.update_batch(conn, bid, status="empty", phase="no_output", error=f"collector_rc={rc}", finished=True)
            rows.append({"kw": kw, "videos": 0, "comments": 0, "new_hits": 0,
                         "status": "无运行目录", "note": f"rc={rc}", "picks": [],
                         "cleaned": False})
            continue
        rows.append(_ingest_keyword_run(conn, a, kw, run_root, bid))

    # 收尾：删运行指针与已采跳过文件（数据全在 SQLite，控制文件也不留）
    pointer = os.path.join(a.root, f".douyin-crawl-current-{a.account}.json")
    for leftover in (pointer, os.path.join(a.root, f".hcp-skip-{a.account}.txt")):
        try:
            os.remove(leftover)
        except OSError:
            pass

    _print_report(conn, a, rows, t0)
    return 0


if __name__ == "__main__":
    sys.exit(main())

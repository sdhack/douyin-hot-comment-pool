# -*- coding: utf-8 -*-
"""爆款评论池·每日调度（核心编排：采样→筛选→每日配额达标即停→沉淀→断点续跑）。

单一命令完成"今天刷一堆爆款评论"，主路径为实时 MediaCrawler 采集：
  1) 关键词广撒网→ MediaCrawler 抓视频+评论（复用 douyin-crawl-report 的 crawl.py）；
  2) 各关键词评论经 aggregate_comments 聚合为 comments.json；
  3) 三级门槛筛选（感高互动→字数→可成文性，复用 filter_pool.py 函数）；
  4) 达标即停：每天最多入池 QUOTA 条（默认 5）。当日已入选数在 pool.json 的
     daily/{YYYY-MM-DD}.count 记录，达到 QUOTA 立即结束（不再继续采集）；
  5) 原子写 pool.json 断点续跑。
  --offline-source 仅排障：喂存量聚合 JSON 跳过采集。

用法（主路径-实时采集）:
  python tools/run_daily.py --root <工作根> --account <slug> --keywords "甲;乙;丙"
      [--per-keyword 30] [--comments-count 100] [--speed safe]
      [--min-likes 1000] [--min-replies 50] [--min-len 30] [--min-score 55]
      [--quota 5] [--dry-run]
用法（调试-离线）:
  python tools/run_daily.py --root <根> --account pool --keywords "<任一>" --per-keyword 0
      --offline-source <聚合 comments.json> --quota 5

产物: <root>/pool/<account>.json
      { meta, pool:[入选评论...], daily:{YYYY-MM-DD:{count, added}} }
"""
import argparse
import copy
import datetime
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import aggregate_comments  # noqa: E402
import filter_pool  # noqa: E402


def _load_pool(root, account, quota):
    path = os.path.join(root, "pool", account + ".json")
    data = {"meta": {"account": account, "quota_per_day": quota},
            "pool": [], "daily": {}}
    if os.path.isfile(path):
        try:
            data = json.load(open(path, encoding="utf-8"))
        except Exception as e:
            sys.exit(f"[ERR] 沉淀池损坏，拒绝覆盖：{path} ({e})")
    data.setdefault("meta", {"account": account, "quota_per_day": quota})
    data["meta"]["quota_per_day"] = quota
    data.setdefault("pool", [])
    data.setdefault("daily", {})
    return data, path


def _already(pool, cand):
    for p in pool.get("pool", []):
        if cand.get("comment_id") and p.get("comment_id") == cand["comment_id"]:
            return True
        if p.get("content") == cand.get("content"):
            return True
    return False


def _run_offline(root, source, account, quota, args, dry_run):
    pool, path = _load_pool(root, account, quota)
    today = datetime.date.today().isoformat()
    dd = pool["daily"].get(today, {"count": 0, "added": []})
    remaining = max(0, quota - dd["count"])
    print(f"[daily] 日期={today} 当日已入池={dd['count']} 配额={quota} 剩余={remaining}")

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
    fresh = [c for c in cands if not _already(pool, c)]
    fresh.sort(key=lambda c: (c["score"], c["like_count"]), reverse=True)
    pick = fresh[:remaining]

    if dry_run:
        print(f"[dry-run] 命中候选 {len(cands)} / 新候选 {len(fresh)} / 将入池 {len(pick)}")
        for c in pick:
            print(f"   +{c['content'][:40]}... [{c['score']}]")
        return 0

    for c in pick:
        pool["pool"].append(c)
    dd["count"] += len(pick)
    dd["added"].extend({"_": c["comment_id"] or c["content"], "s": c["score"]} for c in pick)
    pool["daily"][today] = dd
    if pick:
        pool["pool"] = pool["pool"][-2000:]  # 池上限防无限膨胀

    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(pool, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    print(f"[daily] 入池 {len(pick)} 条（今日累计 {dd['count']}/{quota}）→ {path}")
    print(f"[下一阶段] 沉淀池供文案改造：python tools/filter_pool.py --in {source} --out ... (或直接读 pool.json)")
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
    ap.add_argument("--per-keyword", type=int, default=30)
    ap.add_argument("--comments-count", type=int, default=100)
    ap.add_argument("--speed", default="safe", choices=["safe", "normal", "fast"])
    ap.add_argument("--lt", default="qrcode")
    ap.add_argument("--cookies", default=None)
    ap.add_argument("--sleep-min", type=float, default=3)
    ap.add_argument("--sleep-max", type=float, default=8)
    ap.add_argument("--retry-fail", type=int, default=2)
    ap.add_argument("--max-min", type=float, default=45)
    ap.add_argument("--min-likes", type=int, default=1000)
    ap.add_argument("--min-replies", type=int, default=50)
    ap.add_argument("--min-len", type=int, default=30)
    ap.add_argument("--min-score", type=float, default=55)
    ap.add_argument("--quota", type=int, default=5)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--offline-source", default=None,
                    help="仅调试：喂存量聚合 JSON 做筛选（跳过采集），日常主路径不要用")
    a = ap.parse_args()

    # 主路径：实时 MediaCrawler 关键词采集 → 聚合 → 三级筛选 → 入池 → 达标即停
    if a.offline_source:
        if not os.path.isfile(a.offline_source):
            sys.exit(f"[ERR] --offline-source 不存在: {a.offline_source}（仅为调试用，正常应实时采集）")
        print("[提示] 你正在用离线模式（--offline-source），仅适用于调试；主流程应走实时 MediaCrawler 采集。")
        return _run_offline(a.root, a.offline_source, a.account, a.quota, a, a.dry_run)

    return _run_offline_collect(a)


def _run_offline_collect(a):
    """实时采集一口闷：逐关键词→聚合→离线筛选入池。失败不中断，收集到配额即止。"""
    root = a.root
    account = a.account
    pool, path = _load_pool(root, account, a.quota)
    today = datetime.date.today().isoformat()
    dd = pool["daily"].get(today, {"count": 0, "added": []})

    collect_args = [sys.executable,
                    os.path.join(os.path.dirname(os.path.abspath(__file__)), "collect_search.py")]
    keyword_list = [k.strip() for k in (a.keywords or "").split(";") if k.strip()]
    if not keyword_list:
        sys.exit("[ERR] 实时采集需 --keywords（分号分隔）")
    if a.dry_run:
        print(f"[daily] dry-run：将采集 {len(keyword_list)} 个关键词，配额 {a.quota}")
        return 0

    for kw in keyword_list:
        if dd["count"] >= a.quota:
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
            print(f"  [daily] 关键词「{kw}」采集失败(exit {r.returncode})，跳过")
            continue
        # 定位最新 run 目录，找聚合产物
        run_root = None
        pointer = os.path.join(root, f".douyin-crawl-current-{account}.json")
        if os.path.isfile(pointer):
            try:
                run_root = json.load(open(pointer, encoding="utf-8")).get("run_root")
            except Exception:
                pass
        if not run_root:
            continue
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
        # 筛选并尝试入池
        recs = json.load(open(ag, encoding="utf-8"))
        cands = _screen(list(filter_pool.iter_records(recs)), a)
        cands.sort(key=lambda c: (c["score"], c["like_count"]), reverse=True)
        for c in cands:
            if dd["count"] >= a.quota:
                break
            if _already(pool, c):
                continue
            pool["pool"].append(c)
            dd["count"] += 1
            dd["added"].append({"_": c["comment_id"] or c["content"], "s": c["score"]})
            print(f"  [daily] +入池:[{c['score']}] {c['content'][:40]}...")
        pool["daily"][today] = dd

    if pool["daily"].get(today):
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(pool, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
        print(f"[daily] 实时采集完成：今日累计 {dd['count']}/{a.quota} → {path}")
    else:
        print("[daily] 今日无新增")
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
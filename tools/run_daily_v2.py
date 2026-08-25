# -*- coding: utf-8 -*-
"""run_daily_v2 —— 优化版每日调度（方案 A+B+C+D 全量落地）。

对比 run_daily.py 的关键变化：
  A 提速·单进程多词: 全部关键词合并进【一次】MediaCrawler search 启动
                    （--keywords 支持逗号分隔），消除 N 词 = N 次浏览器重启的开销；
  B 提质·两阶段: 先 search 只拉视频元数据(get_comment=false) → 本地按 liked_count
               排序、跨天去重(排除库中已有 aweme_id，fix Bug-1) → 仅对 Top-N 高赞
               视频以 detail 模式(specified_id 支持纯数字ID)定向抓评论；
  C 正确性: Bug-4/5/6/8/9/10 已在技能内修复；批次行用唯一 batch_id 不再互相覆盖；
  D 止损: 配额闸前置——启动即查当日 hits，配额满零成本退出；入池只写剩余额度条数；
          Bug-7 落地为逐请求随机延时(base_config 静态赋值移除 + 包级 __getattr__
          动态抖动 + client 评论翻页抖动)，不再整轮一个常数。

用法:
  python tools/run_daily_v2.py --root <工作根> --account <slug> --keywords "甲;乙;丙"
      [--preset fast]              # 默认 fast（并发3 / 抖动默认取档位 sleep 区间）
      [--min-video-likes 500]      # 选片门槛：liked_count 达标优先
      [--top-n 15]                 # 定向抓评论的视频数上限
      [--quota 5]                  # 当日入池配额
      [--min-likes 500 --min-replies 50 --min-len 30 --min-score 55]   # 评论三级门槛
      [--max-min 12]               # 单阶段超时(分)
      [--dry-run]

产物: 与 run_daily 完全一致（SQLite accounts/videos/comments/ancestry/batches/hits），
      另输出阶段耗时 KPI 到 stdout，供优化前后对比报告使用。
"""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import time

TOOLS = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(TOOLS)
sys.path.insert(0, TOOLS)
sys.path.insert(0, os.path.join(SKILL, "sqlite"))

import aggregate_comments        # noqa: E402
import filter_pool               # noqa: E402
import _presets                  # noqa: E402
import db as _db                 # noqa: E402
import loader as _loader         # noqa: E402
import hits_backfill as _hits    # noqa: E402
from collect_search import (     # noqa: E402
    resolve_mc, build_cmd, dedup_contents, validate_account_slug,
    _resolve_run_root, _set_pointer, _run, _SPEED_CONC,
    ensure_mc_sleep_patch, ensure_mc_comments_patch,
)
from loader import _safe_int     # noqa: E402
from run_daily import _screen, _today_ids, _already  # noqa: E402

JITTER_MARK = "MC_SLEEP_JITTER_V2"


def _log(msg):
    print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------- Bug-7 补丁
_JI_HELPER = '''

def _mc_ji(default_value):  # {mark}
    """逐请求随机延时: uniform(MC_SLEEP_MIN, MC_SLEEP_MAX)；未设 env 时回落原值。"""
    try:
        lo = float(os.getenv("MC_SLEEP_MIN", "") or 0)
        hi = float(os.getenv("MC_SLEEP_MAX", "") or 0)
    except ValueError:
        return default_value
    if hi <= 0:
        return default_value
    lo = hi * 0.5 if lo <= 0 else min(lo, hi)
    if lo >= hi:
        return round(hi, 3)
    return round(random.uniform(lo, hi), 3)
'''.format(mark=JITTER_MARK)

_INIT_GETATTR = '''

# --- {mark}: CRAWLER_MAX_SLEEP_SEC 动态化（每次访问重新抖动，防星号导入冻结快照） ---
import os as _os_ji
import random as _random_ji


def __getattr__(name):
    if name == "CRAWLER_MAX_SLEEP_SEC":
        try:
            lo = float(_os_ji.getenv("MC_SLEEP_MIN", "") or 0)
            hi = float(_os_ji.getenv("MC_SLEEP_MAX", "") or 0)
        except ValueError:
            lo = hi = 0.0
        if hi > 0:
            lo = hi * 0.5 if lo <= 0 else min(lo, hi)
            if lo >= hi:
                return round(hi, 3)
            return round(_random_ji.uniform(lo, hi), 3)
        try:
            return float(_os_ji.getenv("MC_SLEEP_SEC", "10"))
        except ValueError:
            return 10.0
    raise AttributeError(f"module {{__name__!r}} has no attribute {{name!r}}")
'''.format(mark=JITTER_MARK)


def _patch_file(path, transform, mark):
    """通用幂等补丁：transform(src)->new_src；首改备份 .bak2。返回动作名。"""
    try:
        src = open(path, encoding="utf-8-sig").read()
    except OSError:
        return f"{os.path.basename(path)}:missing"
    if mark in src:
        return f"{os.path.basename(path)}:already"
    new = transform(src)
    if new == src:
        return f"{os.path.basename(path)}:no-change"
    bak = path + ".bak2"
    if not os.path.isfile(bak):
        open(bak, "w", encoding="utf-8").write(src)
    open(path, "w", encoding="utf-8", newline="").write(new)
    return f"{os.path.basename(path)}:patched"


def ensure_mc_jitter_patches(mc_root):
    """三文件落地逐请求抖动（幂等，可回滚：删除 .bak2 同名恢复）。"""
    acts = []

    def t_base(src):  # 移除静态赋值行（含旧 MC_SLEEP_SEC 补丁），保 __getattr__ 生效
        pat = re.compile(r"(?m)^CRAWLER_MAX_SLEEP_SEC[ \t]*=.*$")
        if not pat.search(src):
            return src
        src = pat.sub("# CRAWLER_MAX_SLEEP_SEC -> package __getattr__ (jitter v2)", src, count=1)
        if not re.search(r"(?m)^import random", src):
            src = "import random\n" + src
        return src

    def t_init(src):  # 包级 PEP562 __getattr__
        return src + _INIT_GETATTR

    def t_client(src):  # 评论翻页 sleep 过抖动助手
        src = src + _JI_HELPER
        return src.replace("await asyncio.sleep(crawl_interval)",
                           "await asyncio.sleep(_mc_ji(crawl_interval))")

    acts.append(_patch_file(os.path.join(mc_root, "config", "base_config.py"), t_base, JITTER_MARK))
    acts.append(_patch_file(os.path.join(mc_root, "config", "__init__.py"), t_init, JITTER_MARK))
    acts.append(_patch_file(os.path.join(mc_root, "media_platform", "douyin", "client.py"),
                            t_client, JITTER_MARK))
    return acts


def verify_jitter(mc_py, mc_root, env):
    """连续取值验证动态性：4 次采样应出现多个不同值。"""
    code = ("import config;print(round(float(config.CRAWLER_MAX_SLEEP_SEC),3))")
    vals = []
    for _ in range(4):
        r = subprocess.run([mc_py, "-c", code], cwd=mc_root, env=env,
                           capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            return [], (r.stderr or "").strip()[-300:]
        vals.append(r.stdout.strip())
    return vals, None


# ---------------------------------------------------------------- 选片(B)
def select_videos(save_dir, conn, top_n, min_video_likes, need_comments=100):
    """读本地 jsonl → 按赞排序取 Top-N；库里评论深度达标的视频跳过，不足的可补抓深挖。"""
    _, rows = dedup_contents(save_dir)
    depth = {}
    for row in conn.execute(
            "SELECT aweme_id AS aid, COUNT(*) AS n FROM comments GROUP BY aweme_id"):
        if isinstance(row, dict):
            depth[str(row["aid"])] = int(row["n"])
        else:
            aa, cc = row
            depth[str(aa)] = int(cc)
    seen, fresh, total_uni = set(), [], 0
    for r in rows:
        aid = str(r.get("aweme_id") or "")
        if not aid or aid in seen:
            continue
        seen.add(aid)
        total_uni += 1
        if depth.get(aid, 0) >= need_comments:
            continue  # 评论已采够深度，不再重复抓；浅(<need)的补抓（Bug-1 的深化版）
        likes = _safe_int(r.get("liked_count"))
        title = (r.get("title") or r.get("desc") or "").replace("\n", " ")[:36]
        fresh.append({"aweme_id": aid, "likes": likes, "title": title})
    fresh.sort(key=lambda x: (-x["likes"], x["aweme_id"]))
    hot_n = sum(1 for x in fresh if x["likes"] >= min_video_likes)
    picks = fresh[:top_n]
    stats = {"unique": total_uni, "fresh": len(fresh), "hot_ge_threshold": hot_n,
             "picked": len(picks),
             "likes_top": [x["likes"] for x in picks[:5]],
             "likes_min": picks[-1]["likes"] if picks else 0}
    return picks, stats


# ---------------------------------------------------------------- 主流程
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--account", default="yangsheng")
    ap.add_argument("--keywords", required=True, help="分号分隔；将合并为单次 MC 多词运行")
    ap.add_argument("--preset", default="fast", choices=["safe", "fast"])
    ap.add_argument("--speed", default=None, choices=["safe", "normal", "fast"])
    ap.add_argument("--per-keyword", type=int, default=None)
    ap.add_argument("--comments-count", type=int, default=None)
    ap.add_argument("--sleep-min", type=float, default=None)
    ap.add_argument("--sleep-max", type=float, default=None)
    ap.add_argument("--top-n", type=int, default=15, help="定向抓评论的 Top 视频数")
    ap.add_argument("--min-video-likes", type=int, default=500, help="选片点赞门槛")
    ap.add_argument("--lt", default="qrcode")
    ap.add_argument("--cookies", default=None)
    ap.add_argument("--retry-fail", type=int, default=1)
    ap.add_argument("--max-min", type=float, default=15, help="单阶段超时分钟数")
    ap.add_argument("--min-likes", type=int, default=500)
    ap.add_argument("--min-replies", type=int, default=50)
    ap.add_argument("--min-len", type=int, default=30)
    ap.add_argument("--min-score", type=float, default=55)
    ap.add_argument("--quota", type=int, default=5)
    ap.add_argument("--db", default=None)
    ap.add_argument("--skip-jitter-patch", action="store_true")
    ap.add_argument("--skip-search", action="store_true",
                    help="复用当前 run_root 已抓数据，跳过搜索直接选片+定向抓评")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    _presets.apply_preset(a, sys.argv)
    # preset 未覆盖到的兜底
    a.speed = a.speed or ("fast" if a.preset == "fast" else "safe")
    a.per_keyword = a.per_keyword if a.per_keyword is not None else 10
    # detail 模式需要足够深的评论区（长优质评论在列表尾部）；显式传参则尊重用户
    if "--comments-count" not in sys.argv:
        a.comments_count = 100
    elif a.comments_count is None:
        a.comments_count = 30
    if a.sleep_min is None:
        a.sleep_min = 1 if a.preset == "fast" else 3
    if a.sleep_max is None:
        a.sleep_max = 3 if a.preset == "fast" else 8
    validate_account_slug(a.account)

    T0 = time.monotonic()
    phase_t = {}
    kws = []
    for rawk in (a.keywords or "").split(";"):
        k = rawk.strip()
        if k and k not in kws:
            kws.append(k)
    if not kws:
        sys.exit("[ERR] --keywords 不能为空（分号分隔）")

    conn = _db.open_db(a.db or _db.DEFAULT_DB)
    _db.ensure_schema(conn)
    conn.execute("DROP VIEW IF EXISTS vw_hot_comments")  # Bug-8: 重建修正后的视图
    _db.ensure_schema(conn)

    today = datetime.date.today().isoformat()
    n_today = len(_today_ids(conn))
    remaining = max(0, a.quota - n_today)
    _log(f"[D|配额闸] 今日={today} 已入池={n_today}/{a.quota} 剩余额度={remaining}")
    if remaining <= 0:
        _log("[达标即停] 今日配额已满，本次零成本退出（不启动浏览器）")
        return 0
    if a.dry_run:
        _log(f"[dry-run] 将合并 {len(kws)} 词单进程搜索 → Top{a.top_n}(≥{a.min_video_likes}赞)"
             f" 定向抓评 → 三级筛选入池≤{remaining} 条")
        return 0

    mc_py, mc_root = resolve_mc()
    mc_main = os.path.join(mc_root, "main.py")
    conc = _SPEED_CONC[a.speed]

    # Bug-7: 打补丁 + 动态性验证
    if not a.skip_jitter_patch:
        t = time.monotonic()
        acts = ensure_mc_jitter_patches(mc_root)
        env_probe = dict(os.environ)
        env_probe["MC_SLEEP_MIN"], env_probe["MC_SLEEP_MAX"] = str(a.sleep_min), str(a.sleep_max)
        vals, err = verify_jitter(mc_py, mc_root, env_probe)
        phase_t["patch+verify"] = round(time.monotonic() - t, 1)
        uniq = sorted(set(vals))
        if err:
            _log(f"[Bug-7] 补丁后自检失败: {err}")
            _log("[Bug-7] 回滚保护: 删除 config/*.bak2 对应文件可恢复；本次继续但延时会退化为常量")
        elif len(uniq) < 2:
            _log(f"[Bug-7] 异常: 4 次采样同值 {vals}（可能 env 未生效），请检查补丁")
        else:
            _log(f"[Bug-7] 逐请求抖动生效 {acts} 采样={vals}")

    # 断点续跑目录（沿用指针机制）
    run_root = _resolve_run_root(a.root, a.account)
    save_dir = os.path.join(run_root, "crawl_" + a.account)
    os.makedirs(os.path.join(save_dir, "cursor"), exist_ok=True)
    _set_pointer(a.root, a.account, run_root)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    batch_id = f"{a.account}-{stamp}"  # Bug-5: 唯一批次号

    env = dict(os.environ)
    env["MC_CURSOR_DIR"] = os.path.join(save_dir, "cursor")
    env.setdefault("PYTHONUNBUFFERED", "1")
    env["MC_SLEEP_MIN"], env["MC_SLEEP_MAX"] = str(a.sleep_min), str(a.sleep_max)
    env["MC_COMMENTS_COUNT"] = str(a.comments_count)  # 单视频评论抓取深度（默认10太浅）
    timeout = a.max_min * 60

    def mc_phase(tag, mode, target, get_comment, max_n):
        cmd = build_cmd(mc_py, mc_main, mode, target, save_dir, get_comment,
                        conc, True, max_n, a.lt, a.cookies)
        _log(f"[cmd|{tag}] " + " ".join(cmd))
        attempts = a.retry_fail + 1
        rc = None
        for att in range(1, attempts + 1):
            t = time.monotonic()
            rc = _run(cmd, mc_root, env, timeout)
            phase_t[tag] = round(time.monotonic() - t, 1)
            if rc == 0:
                break
            if att < attempts:
                _log(f"[重试] {tag} 退出码 {rc}，10s 后第 {att+1} 次")
                time.sleep(10)
        return rc

    # ---- A|阶段1: 单进程多关键词搜索（不抓评论，快速扫场）；--skip-search 复用已有数据 ----
    if a.skip_search:
        _log("[A|阶段1] --skip-search: 复用当前 run_root 已抓视频数据，跳过搜索")
        phase_t["phase1-search"] = 0.0
    else:
        _log(f"[A|阶段1] 合并 {len(kws)} 词单进程搜索: {' | '.join(kws)}")
        rc1 = mc_phase("phase1-search", "search", ",".join(kws), False, a.per_keyword)
        if rc1 != 0:
            _log(f"[FAIL] 搜索阶段退出码 {rc1}")
            return 1
        _log(f"[A|阶段1] 完成，用时 {phase_t['phase1-search']}s")

    # ---- B|阶段2: 本地选片 ----
    t = time.monotonic()
    picks, sstat = select_videos(save_dir, conn, a.top_n, a.min_video_likes,
                                 need_comments=a.comments_count)
    phase_t["phase2-select"] = round(time.monotonic() - t, 2)
    _log(f"[B|阶段2] 唯一视频={sstat['unique']} 库外新视频={sstat['fresh']} "
         f"≥{a.min_video_likes}赞={sstat['hot_ge_threshold']} → 定向Top{len(picks)} "
         f"点赞Top5={sstat['likes_top']}")
    if not picks:
        _log("[B|阶段2] 无新视频可选（全部已在库或未抓到），结束")
        return 2

    # ---- B|阶段3: detail 模式定向抓评论 ----
    _log(f"[B|阶段3] 对 {len(picks)} 个高赞视频定向抓评论（detail/specified_id）")
    rc2 = mc_phase("phase3-detail", "detail",
                   ",".join(x["aweme_id"] for x in picks), True, len(picks))
    if rc2 != 0:
        _log(f"[WARN] 评论阶段退出码 {rc2}，仍尝试聚合已抓到的部分")

    # ---- D|阶段4: 聚合→三级筛选→入库（原始+hits，配额封顶）----
    t = time.monotonic()
    crawl_stat = {"videos": 0, "comments": 0}
    try:
        res = _loader.import_batch(conn, save_dir, keyword=",".join(kws),
                                   account=a.account, batch_id=batch_id)
        conn.commit()
        crawl_stat = res["items"]
        _log(f"[入库] 批次 {batch_id}: 视频 {crawl_stat['videos']} / 评论 {crawl_stat['comments']}"
             f" / 作者 {crawl_stat.get('accounts', 0)}")
    except Exception as e:
        _log(f"[入库失败] {e}")

    ag = os.path.join(run_root, "comments_aggregated.json")
    if os.path.isfile(ag):
        try:
            os.remove(ag)
        except OSError:
            pass
    r = subprocess.run([sys.executable, os.path.abspath(aggregate_comments.__file__),
                        "--in", save_dir, "--out", ag,
                        "--max", str(max(100, a.comments_count))],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0 or not os.path.isfile(ag):
        _log(f"[FAIL] 聚合失败: {(r.stderr or '').strip()[-300:]}")
        return 1
    _log(f"[聚合] {r.stdout.strip().splitlines()[0] if r.stdout.strip() else ''}")

    recs = json.load(open(ag, encoding="utf-8"))
    blobs = list(filter_pool.iter_records(recs))
    cands = _screen(blobs, a)
    cands.sort(key=lambda c: (c["score"], c["like_count"]), reverse=True)
    ids_today = _today_ids(conn)
    picks_hits, seen_c = [], set(ids_today)
    for c in cands:
        if len(picks_hits) >= remaining:
            break
        cid = c.get("comment_id") or ""
        if not cid or cid in seen_c:
            continue
        seen_c.add(cid)
        picks_hits.append(c)
    n_hit = _hits.write_hits(conn, picks_hits, batch_id=batch_id) if picks_hits else 0
    conn.commit()
    phase_t["phase4-load+screen"] = round(time.monotonic() - t, 2)

    total = round(time.monotonic() - T0, 1)
    today_now = len(_today_ids(conn))
    _log("=" * 62)
    _log(f"[KPI] 总耗时 {total}s | 阶段: {phase_t}")
    _log(f"[KPI] 视频: 本次唯一 {sstat['unique']} / 新增定向 {len(picks)} | "
         f"评论新增 {crawl_stat['comments']}")
    _log(f"[KPI] 筛选漏斗: 读入 {len(blobs)} → 三级达标 {len(cands)} → 入池 {n_hit}"
         f"（今日 {today_now}/{a.quota}）")
    if picks_hits:
        for c in picks_hits[:5]:
            _log(f"   ★[{c['score']:>2}] 赞{c['like_count']} {c['content'][:34]}")
    _log(f"[下一阶段] python sqlite/report.py --hot --top 20")
    return 0 if n_hit else 2


if __name__ == "__main__":
    sys.exit(main())

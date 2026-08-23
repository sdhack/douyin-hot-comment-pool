# -*- coding: utf-8 -*-
"""爆款评论池·关键词采集入口（核心职责：让一组关键词采样抖音视频+评论）。

本工具把 MediaCrawler 的调度逻辑**完全内嵌**，零依赖 external 技能（如 douyin-crawl-report）：
  1) 直接解析本机已注册/安装的 MediaCrawler（env MEDIACRAWLER_PY/MC_ROOT > 全局注册指针
     runtime-registry.json > 默认缓存 ~/.cache/codex-mediacrawler/MediaCrawler）；
  2) 需让单视频评论数/随机延时生效时，给 MediaCrawler 的 base_config.py 打一次性 env 补丁
     （幂等、可回滚、回读校验）；
  3) 每个关键词以 search 模式调用 MediaCrawler main.py 采集（视频+一级评论）；
  4) 断点续跑：复用当前账号 run-root 与 cursor 目录，跨天 jsonl 合并去重；
  5) 写 <root>/.douyin-crawl-current-<account>.json 指针，供 run_daily 定位聚合。

用法:
  python tools/collect_search.py --root <工作根> --account <唯一slug> --keywords "甲;乙;丙"
      [--per-keyword 30] [--comments-count 100] [--speed safe|normal|fast]
      [--sleep-min 3 --sleep-max 8] [--retry-fail 2] [--max-min 45]
      [--headless] [--lt qrcode|cookie|phone] [--cookies] [--dry-run]
  python tools/collect_search.py --raw-crawler --root <x> --account <s> --target "词"
"""
import argparse
import datetime
import glob
import json
import os
import random
import re
import subprocess
import sys
import time

from _presets import apply_preset  # noqa: E402

PLATFORM = "dy"  # 抖音 固定
_SPEED_CONC = {"safe": 1, "normal": 2, "fast": 3}
_SAFE_ACCOUNT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")

REGISTRY = os.path.join(os.path.expanduser("~"), ".trae-cn", "runtime-registry.json")
CACHE_MC = os.path.join(os.path.expanduser("~"), ".cache", "codex-mediacrawler", "MediaCrawler")


def _reg_get_mc():
    try:
        with open(REGISTRY, encoding="utf-8") as f:
            return json.load(f).get("keys", {}).get("mediacrawler", {})
    except Exception:
        return {}


def resolve_mc():
    """返回 (mc_py, mc_root)；找不到则报错退出。顺序 env > 全局注册指针 > 默认缓存。"""
    reg = _reg_get_mc()
    env_py = os.environ.get("MEDIACRAWLER_PY", "").strip()
    env_root = os.environ.get("MC_ROOT", "").strip()
    cache_py = os.path.join(CACHE_MC, ".venv", "Scripts", "python.exe")
    py = None
    for cand in (env_py, reg.get("python"), cache_py):
        if cand and os.path.isfile(cand):
            py = cand
            break
    root = None
    for cand in (env_root, reg.get("root"), CACHE_MC):
        if cand and os.path.isfile(os.path.join(cand, "main.py")):
            root = cand
            break
    if not py or not root:
        sys.exit("[ERR] 未找到 MediaCrawler：请先安装并在 runtime-registry.json 登记，"
                 "或设置环境变量 MEDIACRAWLER_PY / MC_ROOT。")
    return py, root


def _patch_verify(p, marker, pat, new):
    """给 MediaCrawler 源码打一次性 env 补丁并回读校验（防静默假成功）。"""
    try:
        src = open(p, encoding="utf-8-sig").read()
    except OSError:
        return None
    if marker in src:
        return "already"
    try:
        bak = p + ".bak"
        if not os.path.isfile(bak):
            open(bak, "w", encoding="utf-8").write(src)
        if not re.search(r"(?m)^import os", src):
            src = "import os\n" + src
        if not re.search(pat, src):
            return "no-var"
        patched = re.sub(pat, new, src, count=1)
        old_l, new_l = src.splitlines(), patched.splitlines()
        changed = sum(1 for x, y in zip(old_l, new_l) if x != y) + abs(len(old_l) - len(new_l))
        if changed > 1:
            return "verify-failed"
        open(p, "w", encoding="utf-8", newline="").write(patched)
        return "patched" if marker in open(p, encoding="utf-8").read() else "verify-failed"
    except Exception:
        return None


def ensure_mc_sleep_patch(mc_root):
    p = os.path.join(mc_root, "config", "base_config.py")
    if not os.path.isfile(p):
        return None
    pat = r"(?m)^CRAWLER_MAX_SLEEP_SEC[ \t]*=[ \t]*\d+(?:\.\d+)?[ \t]*(?:#.*)?$"
    return _patch_verify(p, "MC_SLEEP_SEC", pat,
                         'CRAWLER_MAX_SLEEP_SEC = float(os.getenv("MC_SLEEP_SEC", "10"))')


def ensure_mc_comments_patch(mc_root):
    p = os.path.join(mc_root, "config", "base_config.py")
    if not os.path.isfile(p):
        return None
    pat = r"(?m)^CRAWLER_MAX_COMMENTS_COUNT_SINGLENOTES[ \t]*=[ \t]*\d+(?:\.\d+)?[ \t]*(?:#.*)?$"
    return _patch_verify(p, "MC_COMMENTS_COUNT", pat,
                         'CRAWLER_MAX_COMMENTS_COUNT_SINGLENOTES = int(float(os.getenv("MC_COMMENTS_COUNT", "10")))')


def _run_marker(path):
    return os.path.join(path, ".douyin-crawl-run.json")


def validate_account_slug(slug):
    if not _SAFE_ACCOUNT.fullmatch(slug) or slug in (".", ".."):
        sys.exit("[ERR] --account 仅允许 1-80 位字母、数字、点、下划线或连字符，且须以字母或数字开头。")


def _resolve_run_root(root, account):
    """断点续跑：指针指向的 run-root 可复用，否则新建 <root>/<account>-<时间戳>。返回 run_root。"""
    parent = os.path.abspath(root)
    pointer = os.path.join(parent, f".douyin-crawl-current-{account}.json")
    if os.path.isfile(pointer):
        try:
            r = os.path.abspath(json.load(open(pointer, encoding="utf-8")).get("run_root", ""))
            if os.path.isdir(r) and os.path.dirname(r) == parent:
                return r
        except Exception:
            pass
    os.makedirs(parent, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(parent, f"{account}-{stamp}")
    suffix = 2
    while os.path.exists(path):
        path = os.path.join(parent, f"{account}-{stamp}-{suffix}")
        suffix += 1
    os.makedirs(path, exist_ok=True)
    return path


def _set_pointer(parent, account, run_root):
    pointer = os.path.join(parent, f".douyin-crawl-current-{account}.json")
    tmp = pointer + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"account": account, "run_root": os.path.abspath(run_root),
                   "updated_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds")},
                  f, ensure_ascii=False, indent=2)
    os.replace(tmp, pointer)


def build_cmd(py, mc_main, mode, target, save_dir, get_comment, concurrency, headless, max_n,
              lt, cookies):
    cmd = [py, mc_main,
           "--platform", PLATFORM,
           "--type", mode,
           "--save_data_option", "jsonl",
           "--save_data_path", save_dir,
           "--get_comment", str(get_comment).lower()[:1],
           "--max_concurrency_num", str(concurrency),
           "--headless", str(headless).lower()[:1],
           "--crawler_max_notes_count", str(max_n)]
    if mode == "search":
        cmd += ["--keywords", target]
    elif mode == "creator":
        cmd += ["--creator_id", target]
    else:
        cmd += ["--specified_id", target]
    if lt:
        cmd += ["--lt", lt]
    if lt == "cookie" and cookies:
        cmd += ["--cookies", cookies]
    return cmd


def dedup_contents(save_dir):
    """合并 *contents*.jsonl（跨天），按 aweme_id 去重，返回 atot_rows_unique。"""
    raws = sorted(glob.glob(os.path.join(save_dir, "**", "*contents*.jsonl"), recursive=True),
                  key=os.path.getmtime)
    seen, rows = set(), []
    for raw in raws:
        for line in open(raw, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                j = json.loads(line)
            except Exception:
                continue
            if glob and os.path.exists(raw):
                pass
            aid = j.get("aweme_id")
            if not aid or aid in seen:
                continue
            seen.add(aid)
            rows.append(j)
    return raws, rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--account", required=True)
    ap.add_argument("--keywords", default="")
    ap.add_argument("--preset", default="safe", choices=["safe", "fast"],
                    help="采集档位：safe=慢档(并发1/延时3-8s，最稳)，fast=快档(并发3/延时1-3s，提速但风控面大)。默认 safe。")
    ap.add_argument("--per-keyword", type=int, default=10,
                    help="每关键词最多采样视频数（达标5条无需30；改小加快）")
    ap.add_argument("--comments-count", type=int, default=30,
                    help="单视频最多一级评论数（需 base_config 打补丁）")
    ap.add_argument("--speed", default="safe", choices=["safe", "normal", "fast"],
                    help="并发 level: safe=1 / normal=2 / fast=3（被显式指定时优先于 --preset）")
    ap.add_argument("--sleep-min", type=float, default=3)
    ap.add_argument("--sleep-max", type=float, default=3)
    ap.add_argument("--retry-fail", type=int, default=0)
    ap.add_argument("--max-min", type=float, default=None, help="单次抓取最大运行分钟数（防子进程挂起）")
    ap.add_argument("--lt", default="qrcode", choices=["qrcode", "cookie", "phone"])
    ap.add_argument("--cookies", default=None)
    ap.add_argument("--headless", action="store_true", default=True)
    ap.add_argument("--get-comment", dest="get_comment", action="store_true", default=True)
    ap.add_argument("--no-comment", dest="get_comment", action="store_false")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--raw-crawler", action="store_true",
                    help="透传单个关键词直接跑（测试用）")
    ap.add_argument("--target", default=None, help="--raw-crawler 时透传的搜索词")
    a = ap.parse_args()
    apply_preset(a, sys.argv)

    validate_account_slug(a.account)
    if a.raw_crawler:
        if not a.target:
            sys.exit("[ERR] --raw-crawler 需带 --target")
        keywords = [a.target]
    else:
        keywords = []
        for raw in (a.keywords or "").split(";"):
            kw = raw.strip()
            if kw:
                keywords.append(kw)
        seen = set()
        uni = []
        for kw in keywords:
            if kw not in seen:
                seen.add(kw)
                uni.append(kw)
        random.Random(0).shuffle(uni)
        keywords = uni
    if not keywords:
        sys.exit("[ERR] --keywords 不能为空（分号分隔），或 --raw-crawler 需带 --target")

    if a.sleep_min is not None or a.sleep_max is not None:
        if a.sleep_min is None or a.sleep_max is None:
            sys.exit("[ERR] --sleep-min 与 --sleep-max 需成对提供")
        if a.sleep_max < a.sleep_min:
            sys.exit("[ERR] --sleep-max 不得小于 --sleep-min")

    mc_py, mc_root = resolve_mc()
    mc_main = os.path.join(mc_root, "main.py")
    concurrency = _SPEED_CONC[a.speed]
    run_root = _resolve_run_root(a.root, a.account)
    save_dir = os.path.join(run_root, "crawl_" + a.account)
    os.makedirs(os.path.join(save_dir, "cursor"), exist_ok=True)
    _set_pointer(a.root, a.account, run_root)

    print(f"[collect] MediaCrawler={mc_py}\n[collect] mc_root={mc_root}\n"
          f"[collect] 运行目录={run_root}\n[collect] 关键词池={keywords} ({len(keywords)} 个)")

    # 补丁：随机延时 / 单视频评论数（一律打 env 覆盖）
    env = dict(os.environ)
    env["MC_CURSOR_DIR"] = os.path.join(save_dir, "cursor")
    env.setdefault("PYTHONUNBUFFERED", "1")
    if a.sleep_min is not None:
        r = ensure_mc_sleep_patch(mc_root)
        if r in ("patched", "already"):
            env["MC_SLEEP_SEC"] = str(round(random.uniform(a.sleep_min, a.sleep_max), 2))
            print(f"[延时补丁] {r}; MC_SLEEP_SEC={env['MC_SLEEP_SEC']}s")
        else:
            print(f"[延时补丁] 失败({r})，--sleep-min/--sleep-max 不生效")
    if a.get_comment and a.comments_count != 10:
        r = ensure_mc_comments_patch(mc_root)
        if r not in ("patched", "already"):
            print(f"[评论数补丁] 失败({r})，单视频评论数将取 MediaCrawler 出厂默认")
        else:
            env["MC_COMMENTS_COUNT"] = str(a.comments_count)
            print(f"[评论数补丁] {r}; MC_COMMENTS_COUNT={a.comments_count}")

    products, fail = [], 0
    for i, kw in enumerate(keywords, 1):
        print("=" * 60)
        print(f"[collect] 采集 [{i}/{len(keywords)}] 关键词「{kw}」")
        cmd = build_cmd(mc_py, mc_main, "search", kw, save_dir, a.get_comment,
                        concurrency, a.headless, a.per_keyword, a.lt, a.cookies)
        if a.dry_run:
            print("[dry-run] " + " ".join(cmd))
            continue

        attempts = a.retry_fail + 1
        rc = None
        for attempt in range(1, attempts + 1):
            if attempt > 1 and a.sleep_min is not None:
                env["MC_SLEEP_SEC"] = str(round(random.uniform(a.sleep_min, a.sleep_max), 2))
                print(f"[随机延时] 第 {attempt} 次重试重随机 MC_SLEEP_SEC={env['MC_SLEEP_SEC']}s")
            print(f"[cmd] " + " ".join(cmd))
            timeout = a.max_min * 60 if a.max_min else None
            rc = _run(cmd, mc_root, env, timeout)
            print(f"[退出码] {rc}")
            if rc == 0:
                break
            if attempt < attempts:
                wait = min(60, 5 * (2 ** (attempt - 1)))
                print(f"[重试] 退出码 {rc}，{wait}s 后重试...")
                time.sleep(wait)
        if rc != 0:
            print(f"[collect] 关键词「{kw}」采集失败（exit {rc}），继续下一个")
            fail += 1
            continue

        raws, rows = dedup_contents(save_dir)
        if not rows and not raws:
            print(f"[collect] 未发现 contents JSONL，跳过产物收集")
            fail += 1
            continue
        if a.get_comment:
            # MediaCrawler 把 jsonl 落在 <save_data_path>/douyin/jsonl/（含平台子目录），
            # 必须递归扫描；非递归只扫顶层会漏判「未发现评论」。
            cps = glob.glob(os.path.join(save_dir, "**", "search_comments*.jsonl"),
                            recursive=True)
            if not cps:
                print(f"[collect] 已开评论但未发现 search_comments_*.jsonl（含{cps}）；请检查登录态/风控")
                fail += 1
                continue
        products.append(run_root)
        print(f"[collect] 此关键词 videos 唯一数={len(rows)}")

    print("=" * 60)
    if a.dry_run:
        print("[collect] dry-run 完成，未实际采集")
        return
    print(f"[collect] 完成：成功 {len(products)} 轮 / 失败 {fail} 轮 → 运行目录 {run_root}")
    for r in products:
        print(f"[下一阶段] aggregate_comments.py --in {os.path.join(r, 'crawl_' + a.account)} "
              f"--out {os.path.join(r, 'comments_aggregated.json')}")
    sys.exit(0 if products and fail == 0 else 1)


def _run(cmd, cwd, env, timeout):
    """流式执行 MediaCrawler，逐行回显到控制台。返回退出码。"""
    try:
        proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                errors="replace", bufsize=1)
    except (FileNotFoundError, OSError) as e:
        print(f"[ERR] MediaCrawler 无法启动：{e}")
        return 127
    try:
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
    except Exception:
        pass
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.terminate()
        proc.wait()
        print(f"[超时] 抓取超过 {timeout} 秒，已终止（可用 --max-min 调大重试）")
        return -9
    return proc.returncode


if __name__ == "__main__":
    main()
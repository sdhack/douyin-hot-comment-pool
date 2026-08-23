# -*- coding: utf-8 -*-
"""爆款评论池·关键词采集入口（核心职责：让调度器能用一组关键词采样抖音视频+评论）。

本工具**薄封装**已成熟的 douyin-crawl-report 技能的 crawl.py 搜索引擎（它已处理
MediaCrawler 解释器/源码根解析、登录态、--max 硬上限、评论数补丁、断点续跑等），
本脚本只在前面做三件事：
  1) 校验/构造关键词池（去重、随机顺序打散提高采样覆盖面）；
  2) 每个关键词调 crawl.py --mode search 采集一轮（视频+一级评论）；
  3) 汇总每轮产物路径供下一阶段聚合。

用法:
  python tools/collect_search.py --root <工作根> --account <唯一slug> --keywords "甲;乙;丙"
      [--per-keyword 30] [--comments-count 100] [--speed safe|normal|fast]
      [--sleep-min 3 --sleep-max 8] [--retry-fail 2] [--max-min 45] [--headless] [--dry-run]

依赖: 已安装可用的 douyin-crawl-report 技能（在 ~/.trae-cn/skills 下，crawl.py 由其 runtime 解析）。
采集底层是 MediaCrawler 的 API 签名直抓（非浏览器 DOM 模拟）；--headless 默认开启避免弹窗，
扫码登录仅需首次（之后复用登录态缓存）。
用法二（直接透传单个关键词给 crawl.py，适合测试）:
  python tools/collect_search.py --raw-crawler --root <根> --account <slug> --target "关键词"
"""
import argparse
import json
import os
import random
import subprocess
import sys
import datetime

CR_SKILL = os.path.join(os.path.expanduser("~"), ".trae-cn", "skills",
                        "douyin-crawl-report", "tools")
CR_CRAWL = os.path.join(CR_SKILL, "crawl.py")
CR_RUNTIME = os.path.join(CR_SKILL, "runtime.py")


def _resolver_py():
    """解析运行库解释器（用于跑 douyin-crawl-report/runtime.py）。"""
    try:
        r = subprocess.run([sys.executable, CR_RUNTIME, "py"], capture_output=True,
                           text=True, timeout=60)
        p = (r.stdout or "").strip()
        if p and os.path.isfile(p):
            return p
    except Exception:
        pass
    return sys.executable


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--account", required=True)
    ap.add_argument("--keywords", default="")
    ap.add_argument("--per-keyword", type=int, default=30)
    ap.add_argument("--comments-count", type=int, default=100)
    ap.add_argument("--speed", default="safe", choices=["safe", "normal", "fast"])
    ap.add_argument("--sleep-min", type=float, default=3)
    ap.add_argument("--sleep-max", type=float, default=8)
    ap.add_argument("--retry-fail", type=int, default=2)
    ap.add_argument("--max-min", type=float, default=45)
    ap.add_argument("--lt", default="qrcode")
    ap.add_argument("--cookies", default=None)
    ap.add_argument("--headless", action="store_true", default=True,
                    help="不弹浏览器窗口（MediaCrawler API 直抓）；需先扫码登录一次")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--raw-crawler", action="store_true",
                    help="透传单个关键词直接跑 crawl.py（测试用，忽略 --keywords 拆分）")
    ap.add_argument("--target", default=None, help="--raw-crawler 时透传的搜索词")
    a = ap.parse_args()

    if not os.path.isfile(CR_CRAWL):
        sys.exit(f"[ERR] 找不到 douyin-crawl-report 技能 crawl.py: {CR_CRAWL}\n"
                 "  需先安装 douyin-crawl-report 技能（本技能依赖其搜索引擎）。")

    keywords = []
    if a.raw_crawler:
        keywords = [a.target]
    else:
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
        keywords = uni
        random.Random(0).shuffle(keywords)  # 打散关键词顺序，提高采样覆盖面
    if not keywords:
        sys.exit("[ERR] --keywords 不能为空（分号分隔多个），或 --raw-crawler 需带 --target")

    run_py = _resolver_py()
    print(f"[collect] 运行库={run_py}\n[collect] 关键词池={keywords} ({len(keywords)} 个)")

    products = []
    fail = 0
    for i, kw in enumerate(keywords, 1):
        print("=" * 60)
        print(f"[collect] 采集 [{i}/{len(keywords)}] 关键词「{kw}」")
        cmd = [run_py, CR_CRAWL,
               "--root", a.root, "--account", a.account,
               "--mode", "search", "--target", kw,
               "--max", str(a.per_keyword),
               "--comments-count", str(a.comments_count),
               "--speed", a.speed, "--lt", a.lt,
               "--sleep-min", str(a.sleep_min), "--sleep-max", str(a.sleep_max),
               "--retry-fail", str(a.retry_fail), "--max-min", str(a.max_min)]
        if a.headless:
            cmd += ["--headless"]
        if a.cookies:
            cmd += ["--cookies", a.cookies]
        if a.dry_run:
            print("[dry-run] " + " ".join(cmd))
            continue
        print("[cmd] " + " ".join(cmd))
        rc = subprocess.run(cmd).returncode
        if rc != 0:
            print(f"[collect] 关键词「{kw}」采集失败（exit {rc}），继续下一个")
            fail += 1
            continue
        # 找本账号最新一轮 run 目录下的 search_comments jsonl
        run_root = None
        pointer = os.path.join(a.root, f".douyin-crawl-current-{a.account}.json")
        if os.path.isfile(pointer):
            try:
                run_root = json.load(open(pointer, encoding="utf-8")).get("run_root")
            except Exception:
                run_root = None
        if not run_root or not os.path.isdir(run_root):
            print(f"[collect] 未定位到运行目录（{run_root}），跳过产物收集")
            continue
        products.append(run_root)

    print("=" * 60)
    if a.dry_run:
        print("[collect] dry-run 完成，未实际采集")
        return
    print(f"[collect] 完成：成功 {len(products)} 轮 / 失败 {fail} 轮")
    # 下一阶段命令
    for run_root in products:
        print(f"[下一阶段] aggregate_comments.py --in {os.path.join(run_root, 'crawl_' + a.account)} "
              f"--out {os.path.join(run_root, 'comments_aggregated.json')}")
    sys.exit(0 if products and fail == 0 else 1 if fail else 2)


if __name__ == "__main__":
    main()
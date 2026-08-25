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
    """确保延时机制就绪。兼容两种形态：
    1) jitter-v2 动态形态（base_config 含 MC_SLEEP_JITTER_V2 标记，CRAWLER_MAX_SLEEP_SEC 经
       config.__getattr__ 每次访问按 MC_SLEEP_MIN/MAX 抖动）→ 直接视为 already；
    2) 旧静态形态（CRAWLER_MAX_SLEEP_SEC = <数字>）→ 补丁为读 env 后返回。
    """
    cfg_dir = os.path.join(mc_root, "config")
    p = os.path.join(cfg_dir, "base_config.py")
    if not os.path.isfile(p):
        return None
    # jitter-v2 标记可能位于 base_config.py 或同目录 __init__.py（package __getattr__ 动态化）
    for f in ("base_config.py", "__init__.py"):
        fp = os.path.join(cfg_dir, f)
        if os.path.isfile(fp):
            try:
                if "MC_SLEEP_JITTER_V2" in open(fp, encoding="utf-8-sig").read():
                    return "already"   # 动态形态：MC_SLEEP_MIN/MAX 已被每请求消费
            except OSError:
                pass
    try:
        src = open(p, encoding="utf-8-sig").read()
    except OSError:
        return None
    pat = r"(?m)^CRAWLER_MAX_SLEEP_SEC[ \t]*=[ \t]*\d+(?:\.\d+)?[ \t]*(?:#.*)?$"
    if not re.search(pat, src):
        return "no-var"
    return _patch_verify(p, "MC_SLEEP_SEC", pat,
                         'CRAWLER_MAX_SLEEP_SEC = float(os.getenv("MC_SLEEP_SEC", "10"))')


def ensure_mc_comments_patch(mc_root):
    p = os.path.join(mc_root, "config", "base_config.py")
    if not os.path.isfile(p):
        return None
    pat = r"(?m)^CRAWLER_MAX_COMMENTS_COUNT_SINGLENOTES[ \t]*=[ \t]*\d+(?:\.\d+)?[ \t]*(?:#.*)?$"
    return _patch_verify(p, "MC_COMMENTS_COUNT", pat,
                         'CRAWLER_MAX_COMMENTS_COUNT_SINGLENOTES = int(float(os.getenv("MC_COMMENTS_COUNT", "10")))')


def ensure_mc_skip_patch(mc_root):
    """给 douyin/core.py 注入「已采视频跳过评论」钩子（幂等，带 .bak 回滚）。

    在既有 MC_OPT 定制层上扩展两处（env MC_SKIP_FILE 未设置时零行为变化，不影响其他项目）：
      1) 新增方法 _mc_opt_known_awemes：懒加载 MC_SKIP_FILE（每行一个 aweme_id）为集合并缓存；
      2) batch_get_note_comments 循环内：aweme_id 命中集合则计指标 skipped_known_awemes 并跳过评论抓取。
    返回 patched / already / no-anchor / verify-failed / None(文件缺失)。
    """
    p = os.path.join(mc_root, "media_platform", "douyin", "core.py")
    if not os.path.isfile(p):
        return None
    try:
        src = open(p, encoding="utf-8-sig").read()
    except OSError:
        return None
    if "MC_SKIP_FILE" in src:
        return "already"
    try:
        bak = p + ".bak"
        if not os.path.isfile(bak):
            open(bak, "w", encoding="utf-8").write(src)

        method_anchor = "    def _mc_opt_record_aweme(self, aweme):"
        method_code = (
            '    def _mc_opt_known_awemes(self):\n'
            '        """已采视频ID集合（env MC_SKIP_FILE 指定文件，每行一个 aweme_id；未设置则为空）。"""\n'
            '        self._mc_opt_init()\n'
            '        cached = getattr(self, "_mc_opt_skip_cache", None)\n'
            '        path = os.getenv("MC_SKIP_FILE", "")\n'
            '        if cached is None or cached[0] != path:\n'
            '            ids = set()\n'
            '            if path and os.path.isfile(path):\n'
            '                try:\n'
            '                    with open(path, encoding="utf-8") as f:\n'
            '                        ids = {line.strip() for line in f if line.strip()}\n'
            '                except OSError:\n'
            '                    pass\n'
            '            cached = (path, ids)\n'
            '            self._mc_opt_skip_cache = cached\n'
            '        return cached[1]\n\n'
        )
        guard_anchor = ("            if isinstance(candidate, dict):\n"
                        "                self._mc_opt_record_aweme(candidate)\n")
        guard_code = guard_anchor + (
            "            if str(aweme_id) in self._mc_opt_known_awemes():\n"
            '                self._mc_opt_metric("skipped_known_awemes")\n'
            '                utils.logger.info(f"[MC_OPT] skipped_known_aweme aweme_id:{aweme_id}")\n'
            "                continue\n"
        )
        if method_anchor not in src or guard_anchor not in src:
            return "no-anchor"
        patched = src.replace(method_anchor, method_code + method_anchor, 1)
        patched = patched.replace(guard_anchor, guard_code, 1)
        if patched == src:
            return "verify-failed"
        open(p, "w", encoding="utf-8", newline="").write(patched)
        now = open(p, encoding="utf-8").read()
        return "patched" if ("MC_SKIP_FILE" in now and "skipped_known_aweme" in now) else "verify-failed"
    except Exception:
        return None


def ensure_mc_search_diag_patch(mc_root):
    """给 douyin/core.py 的搜索空结果日志附加 status_code/status_msg（幂等，纯日志增强）。

    便于区分空结果根因：2483=未登录/会话失效（扫码重登后需 --no-headless 有头采集）、
    其他=风控或无匹配。返回 patched / already / no-anchor / verify-failed / None。
    """
    p = os.path.join(mc_root, "media_platform", "douyin", "core.py")
    if not os.path.isfile(p):
        return None
    try:
        src = open(p, encoding="utf-8-sig").read()
    except OSError:
        return None
    if "is empty(status=" in src:
        return "already"
    try:
        bak = p + ".diag.bak"
        if not os.path.isfile(bak):
            open(bak, "w", encoding="utf-8").write(src)
        anchor = 'page: {page} is empty,{posts_res.get(\'data\')}`"'
        new = ('page: {page} is empty(status={posts_res.get(\'status_code\')}|'
               '{posts_res.get(\'status_msg\')}) data:{posts_res.get(\'data\')}`"')
        if anchor not in src:
            return "no-anchor"
        patched = src.replace(anchor, new, 1)
        if patched == src:
            return "verify-failed"
        open(p, "w", encoding="utf-8", newline="").write(patched)
        now = open(p, encoding="utf-8").read()
        return "patched" if "is empty(status=" in now else "verify-failed"
    except Exception:
        return None


def ensure_mc_video_gate_patch(mc_root, min_likes=10000):
    """给 douyin/core.py 注入「视频级点赞门槛」（幂等，带 .bak 回滚）。

    规则（用户需求 2026-08-25 硬编码 1 万赞）：
      - 搜索结果中的视频，statistics.digg_count < MC_VIDEO_MIN_LIKES（默认 10000）→ 跳过评论抓取，
        仅保留视频信息入库；
      - 低赞被跳过的视频不进 skip 名单（无评论），**二次抓到时若点赞已涨过门槛则正常抓取**；
      - 已采过的视频仍受 MC_SKIP_FILE 去重约束。
    返回 patched/already/no-anchor/verify-failed/None。
    """
    p = os.path.join(mc_root, "media_platform", "douyin", "core.py")
    if not os.path.isfile(p):
        return None
    try:
        src = open(p, encoding="utf-8-sig").read()
    except OSError:
        return None
    if "MC_VIDEO_MIN_LIKES" in src:
        return "already"
    try:
        bak = p + ".gate.bak"
        if not os.path.isfile(bak):
            open(bak, "w", encoding="utf-8").write(src)

        # 1) record_aweme 里顺带记录 digg_count（供门槛判断）
        rec_anchor = ("    def _mc_opt_record_aweme(self, aweme):\n"
                      "        self._mc_opt_init()\n")
        rec_insert = rec_anchor + (
            "        _lk_st = aweme.get('statistics') if isinstance(aweme.get('statistics'), dict) else {}\n"
            "        _lk_v = _lk_st.get('digg_count', aweme.get('digg_count'))\n"
            "        try:\n"
            "            if _lk_v is not None and str(_lk_v).strip() != '':\n"
            "                _ld = getattr(self, '_mc_opt_liked_counts', None)\n"
            "                if _ld is None:\n"
            "                    _ld = {}\n"
            "                    self._mc_opt_liked_counts = _ld\n"
            "                _ld[str(aweme.get('aweme_id'))] = int(float(str(_lk_v)))\n"
            "        except (TypeError, ValueError):\n"
            "            pass\n")

        # 2) batch_get_note_comments 的 known-skip 之后追加低赞跳过分支
        known_anchor = ('            if str(aweme_id) in self._mc_opt_known_awemes():\n'
                        '                self._mc_opt_metric("skipped_known_awemes")\n'
                        '                utils.logger.info(f"[MC_OPT] skipped_known_aweme aweme_id:{aweme_id}")\n'
                        "                continue\n")
        gate_insert = known_anchor + (
            "            _vml = int(os.getenv('MC_VIDEO_MIN_LIKES', '" + str(int(min_likes)) + "') or 0)\n"
            "            if _vml > 0:\n"
            "                _lv = getattr(self, '_mc_opt_liked_counts', {}).get(str(aweme_id))\n"
            "                if _lv is not None and _lv < _vml:\n"
            '                    self._mc_opt_metric("skipped_low_like_videos")\n'
            '                    utils.logger.info(f"[MC_OPT] skipped_low_like_video aweme_id:{aweme_id} "\n'
            '                                      f"liked={_lv} floor={_vml}")\n'
            "                    continue\n")
        if rec_anchor not in src or known_anchor not in src:
            return "no-anchor"
        patched = src.replace(rec_anchor, rec_insert, 1)
        patched = patched.replace(known_anchor, gate_insert, 1)
        if patched == src:
            return "verify-failed"
        open(p, "w", encoding="utf-8", newline="").write(patched)
        now = open(p, encoding="utf-8").read()
        return "patched" if "MC_VIDEO_MIN_LIKES" in now else "verify-failed"
    except Exception:
        return None


def ensure_mc_stop_floor_patch(mc_root):
    """给 core.py search() 注入「按赞止停」（幂等，带 .bak 回滚）。

    配合 MOST_LIKE 排序（结果严格赞降序）：本页出现点赞低于 MC_STOP_AT_LIKE_FLOOR
    （默认取视频门槛值）的视频时——过滤掉这些视频的评论抓取并结束当前关键词的翻页
    （break 仅跳出分页 while，不影响后续关键词）。env 未设置时零行为变化。
    返回 patched/already/no-anchor/verify-failed/None。
    """
    p = os.path.join(mc_root, "media_platform", "douyin", "core.py")
    if not os.path.isfile(p):
        return None
    try:
        src = open(p, encoding="utf-8-sig").read()
    except OSError:
        return None
    if "MC_STOP_AT_LIKE_FLOOR" in src:
        return "already"
    try:
        bak = p + ".stop.bak"
        if not os.path.isfile(bak):
            open(bak, "w", encoding="utf-8").write(src)
        anchor = ('                # Batch get note comments for the current page\n'
                  "                await self.batch_get_note_comments(page_aweme_list)")
        new = ('                # Batch get note comments for the current page\n'
               '                _saf = int(os.getenv("MC_STOP_AT_LIKE_FLOOR", "0") or 0)\n'
               "                if _saf > 0 and page_aweme_list:\n"
               '                    _lk = getattr(self, "_mc_opt_liked_counts", {})\n'
               '                    _below = [str(a) for a in page_aweme_list\n'
               '                              if _lk.get(str(a)) is not None and _lk[str(a)] < _saf]\n'
               "                    if _below:\n"
               '                        utils.logger.info(f"[MC_OPT] stop_at_like_floor keyword:{keyword} "\n'
               '                                          f"below_floor_n={len(_below)} -> 按赞降序，本词到此为止")\n'
               "                        await self.batch_get_note_comments(\n"
               "                            [a for a in page_aweme_list if str(a) not in _below])\n"
               "                        break\n"
               "                await self.batch_get_note_comments(page_aweme_list)")
        if anchor not in src:
            return "no-anchor"
        patched = src.replace(anchor, new, 1)
        if patched == src:
            return "verify-failed"
        open(p, "w", encoding="utf-8", newline="").write(patched)
        now = open(p, encoding="utf-8").read()
        return "patched" if "MC_STOP_AT_LIKE_FLOOR" in now else "verify-failed"
    except Exception:
        return None


def ensure_mc_sort_patch(mc_root):
    """让搜索接口支持排序选择（幂等，带 .bak 回滚）。

    env MC_SEARCH_SORT_TYPE: 0=综合(默认) 1=最多点赞 2=最新发布。
    实测 MOST_LIKE 返回严格赞降序；默认不改变行为（综合排序保持覆盖多样性）。
    返回 patched/already/no-anchor/verify-failed/None。
    """
    p = os.path.join(mc_root, "media_platform", "douyin", "core.py")
    if not os.path.isfile(p):
        return None
    try:
        src = open(p, encoding="utf-8-sig").read()
    except OSError:
        return None
    if "MC_SEARCH_SORT_TYPE" in src:
        return "already"
    try:
        bak = p + ".sort.bak"
        if not os.path.isfile(bak):
            open(bak, "w", encoding="utf-8").write(src)
        imp_anchor = "from .field import PublishTimeType"
        imp_new = "from .field import PublishTimeType, SearchSortType"
        call_anchor = "                        publish_time=PublishTimeType(config.PUBLISH_TIME_TYPE),"
        call_new = ("                        publish_time=PublishTimeType(config.PUBLISH_TIME_TYPE),\n"
                    '                        sort_type=SearchSortType(int(os.getenv("MC_SEARCH_SORT_TYPE", "0"))),')
        if imp_anchor not in src or call_anchor not in src:
            return "no-anchor"
        patched = src.replace(imp_anchor, imp_new, 1).replace(call_anchor, call_new, 1)
        if patched == src:
            return "verify-failed"
        open(p, "w", encoding="utf-8", newline="").write(patched)
        now = open(p, encoding="utf-8").read()
        return "patched" if "MC_SEARCH_SORT_TYPE" in now else "verify-failed"
    except Exception:
        return None


def ensure_mc_heat_patch(mc_root):
    """给 client.py 评论翻页注入「热度水位早停」（幂等，带 .bak 回滚）。

    抖音评论接口为固定智能排序（实测不接受排序参数），页码越深内容越冷：
    某页最高 digg_count < MC_PAGE_HEAT_FLOOR_LIKES 且最高 reply_comment_total <
    MC_PAGE_HEAT_FLOOR_REPLIES 时立即停止该视频后续翻页（阈值默认取三级门槛
    --min-likes/--min-replies，0=关闭）。返回 patched/already/no-anchor/verify-failed/None。
    """
    p = os.path.join(mc_root, "media_platform", "douyin", "client.py")
    if not os.path.isfile(p):
        return None
    try:
        src = open(p, encoding="utf-8-sig").read()
    except OSError:
        return None
    if "MC_PAGE_HEAT_FLOOR_LIKES" in src:
        return "already"
    try:
        bak = p + ".heat.bak"
        if not os.path.isfile(bak):
            open(bak, "w", encoding="utf-8").write(src)
        anchor = ('            comments = comments_res.get("comments", [])\n'
                  "            if not comments:\n"
                  "                continue\n")
        insert = anchor + (
            '            _fl = int(os.getenv("MC_PAGE_HEAT_FLOOR_LIKES", "0") or 0)\n'
            '            _fr = int(os.getenv("MC_PAGE_HEAT_FLOOR_REPLIES", "0") or 0)\n'
            "            if (_fl or _fr) and comments:\n"
            "                try:\n"
            '                    _mx_d = max(int(float(str(c.get("digg_count") or 0))) for c in comments)\n'
            '                    _mx_r = max(int(float(str(c.get("reply_comment_total") or 0))) for c in comments)\n'
            "                    if _mx_d < _fl and _mx_r < _fr:\n"
            '                        utils.logger.info(f"[MC_OPT] early_stop_low_heat aweme_id:{aweme_id} "\n'
            '                                          f"page_max_digg={_mx_d} page_max_reply={_mx_r} floor={_fl}/{_fr}")\n'
            "                        return result\n"
            "                except Exception:\n"
            "                    pass\n")
        if anchor not in src:
            return "no-anchor"
        patched = src.replace(anchor, insert, 1)
        if patched == src:
            return "verify-failed"
        open(p, "w", encoding="utf-8", newline="").write(patched)
        now = open(p, encoding="utf-8").read()
        return "patched" if "MC_PAGE_HEAT_FLOOR_LIKES" in now else "verify-failed"
    except Exception:
        return None


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
    ap.add_argument("--preset", default="safe", choices=["safe", "ultra", "fast"],
                    help="采集档位：safe=慢档(并发1/延时6-15s，稳)，ultra=超稳档(并发1/延时12-28s，风控期/长跑用)，fast=快档(并发3/延时2-6s，提速但风控面大)。默认 safe。")
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
    ap.add_argument("--stop-at-like-floor", dest="stop_at_like_floor", action="store_true",
                    help="配合按赞排序：抓到首个点赞低于视频门槛的视频即结束本词（替代固定条数；自动启用最多点赞排序）")
    ap.add_argument("--search-sort", dest="search_sort", type=int, default=0, choices=[0, 1, 2],
                    help="搜索结果排序：0=综合(默认) 1=最多点赞(实测严格赞降序，配合万赞门槛零浪费) 2=最新发布")
    ap.add_argument("--video-min-likes", dest="video_min_likes", type=int, default=10000,
                    help="视频级点赞门槛（硬编码默认1万）：低于此值的视频跳过评论抓取，二次遇到涨过门槛则正常抓取")
    ap.add_argument("--min-likes", type=int, default=1000,
                    help="评论翻页热度水位：页内最高赞低于此值且最高回复低于 --min-replies 时提前停止翻页")
    ap.add_argument("--min-replies", type=int, default=50,
                    help="评论翻页热度水位（回复数），配合 --min-likes 使用")
    ap.add_argument("--skip-file", dest="skip_file", default=None,
                    help="已采视频ID列表文件（每行一个 aweme_id）；命中的视频跳过评论重抓，降重复率与风控面")
    ap.add_argument("--headless", default=True, action=argparse.BooleanOptionalAction,
                    help="无头模式（默认开）。扫码重登后新会话绑定有头指纹时需 --no-headless 弹窗采集")
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
        random.shuffle(uni)
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
            # 每请求区间抖动：MC_OPT 层在 get_comments/get_aweme_detail 内按 [MIN,MAX] uniform 取间隔
            env["MC_SLEEP_MIN"] = str(a.sleep_min)
            env["MC_SLEEP_MAX"] = str(a.sleep_max)
            print(f"[延时补丁] {r}; MC_SLEEP_SEC={env['MC_SLEEP_SEC']}s "
                  f"(每请求抖动 {a.sleep_min}-{a.sleep_max}s)")
        else:
            print(f"[延时补丁] 失败({r})，--sleep-min/--sleep-max 不生效")
    if a.get_comment and a.comments_count != 10:
        r = ensure_mc_comments_patch(mc_root)
        if r not in ("patched", "already"):
            print(f"[评论数补丁] 失败({r})，单视频评论数将取 MediaCrawler 出厂默认")
        else:
            env["MC_COMMENTS_COUNT"] = str(a.comments_count)
            print(f"[评论数补丁] {r}; MC_COMMENTS_COUNT={a.comments_count}")

    r_diag = ensure_mc_search_diag_patch(mc_root)
    if r_diag == "patched":
        print("[诊断补丁] 搜索空结果日志已附 status_code/msg（区分未登录2483/风控）")

    r_sort = ensure_mc_sort_patch(mc_root)
    if r_sort in ("patched", "already"):
        if a.search_sort != 0:
            env["MC_SEARCH_SORT_TYPE"] = str(a.search_sort)
            print(f"[搜索排序] {r_sort}; MC_SEARCH_SORT_TYPE={a.search_sort} "
                  f"({'最多点赞' if a.search_sort == 1 else '最新发布'})")
    else:
        print(f"[搜索排序] 补丁失败({r_sort})，固定综合排序")

    if a.stop_at_like_floor:
        if a.search_sort == 0:
            a.search_sort = 1   # 止停依赖赞降序，自动启用最多点赞
        if r_sort in ("patched", "already"):
            env["MC_SEARCH_SORT_TYPE"] = str(a.search_sort)
        r_sf = ensure_mc_stop_floor_patch(mc_root)
        if r_sf in ("patched", "already"):
            env["MC_STOP_AT_LIKE_FLOOR"] = str(a.video_min_likes)
            print(f"[按赞止停] {r_sf}; 排序=最多点赞，抓到点赞<{a.video_min_likes} 即结束本词（不再固定条数）")
        else:
            print(f"[按赞止停] 补丁失败({r_sf})，退回固定条数模式")

    if a.get_comment:
        r_gate = ensure_mc_video_gate_patch(mc_root, min_likes=a.video_min_likes)
        if r_gate in ("patched", "already"):
            env["MC_VIDEO_MIN_LIKES"] = str(a.video_min_likes)
            print(f"[视频门槛] {r_gate}; 点赞<{a.video_min_likes} 跳过评论抓取（二次遇涨过门槛正常抓）")
        else:
            print(f"[视频门槛] 补丁失败({r_gate})，不做视频级过滤")

    if a.get_comment and (a.min_likes > 0 or a.min_replies > 0):
        r_heat = ensure_mc_heat_patch(mc_root)
        if r_heat in ("patched", "already"):
            env["MC_PAGE_HEAT_FLOOR_LIKES"] = str(a.min_likes)
            env["MC_PAGE_HEAT_FLOOR_REPLIES"] = str(a.min_replies)
            print(f"[热度早停] {r_heat}; 页内最高赞<{a.min_likes} 且最高回复<{a.min_replies} 即停止翻页")
        else:
            print(f"[热度早停] 补丁失败({r_heat})，将按 max_count 翻满")

    products, fail = [], 0
    skip_state = None  # (patch_result, 是否给子进程传了 MC_SKIP_FILE)
    if a.get_comment and a.skip_file:
        r = ensure_mc_skip_patch(mc_root)
        skip_state = (r, r in ("patched", "already"))
        if skip_state[1]:
            env["MC_SKIP_FILE"] = os.path.abspath(a.skip_file)
            print(f"[已采跳过补丁] {r}; MC_SKIP_FILE={env['MC_SKIP_FILE']}")
        else:
            print(f"[已采跳过补丁] 失败({r})，本批不跳过已采视频（不影响采集，仅重复抓取）")
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
    if products:
        print("[提示] 由 run_daily 调度时：本目录 jsonl 会被实时导入 SQLite 后整体删除（磁盘零 JSON 残留）")
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
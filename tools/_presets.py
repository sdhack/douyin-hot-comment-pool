# -*- coding: utf-8 -*-
"""采集档位预设：--preset 一键速调 speed / 随机延时 / 采集面大小。

safe=慢档（最稳，风控压力最小）   fast=快档（约数倍提速，风控暴露面更大）。
独立参数（--speed / --sleep-* / --per-keyword / --comments-count）若被显式传入，
优先级高于 preset；preset 只填充未被用户显式指定的字段。
"""
PRESETS = {
    "safe": dict(speed="safe", sleep_min=3, sleep_max=8, per_keyword=10, comments_count=30),
    "fast": dict(speed="fast", sleep_min=1, sleep_max=3, per_keyword=10, comments_count=30),
}


def parse_present_flags(argv):
    """从原始 argv 提取"被显式指定"的参数 dest 集合（用于覆盖 preset 默认）。"""
    present = set()
    i = 0
    n = len(argv)
    while i < n:
        tok = argv[i]
        if tok.startswith("--"):
            flag = tok.split("=", 1)[0][2:]
            present.add(flag.replace("-", "_"))
            if "=" not in tok and i + 1 < n and not argv[i + 1].startswith("--"):
                i += 1
        i += 1
    return present


def apply_preset(a, argv):
    """把 preset 的预设值填到 namespace `a` 上，但不覆盖用户显式传入的参数。"""
    if a.preset not in PRESETS:
        a.preset = "safe"
    present = parse_present_flags(argv)
    for key, val in PRESETS[a.preset].items():
        if key not in present:
            setattr(a, key, val)
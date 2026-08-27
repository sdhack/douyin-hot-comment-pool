# -*- coding: utf-8 -*-
"""可成文性 AI 判分引擎（③门槛）。

两种执行形态：
  1) API 引擎（无人值守）：配置 OpenAI 兼容接口后自动批量判分。
     配置来源（优先级）：环境变量 AI_SCORER_API_BASE / AI_SCORER_API_KEY / AI_SCORER_MODEL
     > 技能内 tools/ai_scorer.json {"api_base":..,"api_key":..,"model":..}。
  2) Agent 引擎（当前主用）：无配置时进入「Agent 判分队列」——run_daily 把过①②门槛的候选
     落到 .hcp-judge-<account>.jsonl，由值班 Agent 按评分手册逐条评审后经 hits_backfill.write_hits
     入池；配额达标机制不受影响。

判分手册（与版本号一起固化，任何调整必须同步 bump RUBRIC_VERSION）：
  * 唯一标准：能否据此写出一条**新的、有明确观点**的文案（借鉴结构/钩子，重写表达）。
  * 加分：强钩子（悬念/立场/反常识）、情绪浓度、具体细节与数字、自足性（脱离原视频仍成立）、
    可执行清单的信息密度、高级钩子（如「在收心」「做减法」这类概括力强的观点句）。
  * 一票否决（对成文毫无意义）：水贴、纯提问、接龙/打卡、玩梗复读、广告引流、人身攻击、
    需原视频语境才能理解的内容。
"""

import json
import os
import urllib.request

RUBRIC_VERSION = "ai-rubric-v1"
CFG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ai_scorer.json")

PROMPT_TMPL = """你是爆款文案素材评审。对下列抖音评论逐条判定：能否据此写出一条【新的、有明确观点】的口播/图文文案（只借鉴结构与钩子，不照抄内容）。

评分（0-100）：
  85-100 强素材：钩子锋利 + 具体细节/数字 + 脱离原视频自成一体；
  70-84 可用：观点清晰，改写后能立住；
  55-69 勉强可用：骨架可借但需大幅补充；
  <55 弃用。

一票否决置 0：水贴寒暄、纯提问求资源、接龙打卡、玩梗复读、广告引流、攻击辱骂、离开原视频就看不懂的评论。

输出严格 JSON 数组，不加任何其他文字：
[{{"i": <序号>, "score": <0-100整数>, "reason": "<≤20字理由>"}}]

评论列表：
{items}
"""


def load_cfg():
    base = os.environ.get("AI_SCORER_API_BASE", "").strip().rstrip("/")
    key = os.environ.get("AI_SCORER_API_KEY", "").strip()
    model = os.environ.get("AI_SCORER_MODEL", "").strip()
    try:
        cfg = json.load(open(CFG_FILE, encoding="utf-8"))
        base = base or str(cfg.get("api_base") or "").rstrip("/")
        key = key or str(cfg.get("api_key") or "")
        model = model or str(cfg.get("model") or "")
    except (OSError, ValueError):
        pass
    return {"base": base, "key": key, "model": model}


def available():
    cfg = load_cfg()
    return bool(cfg["base"] and cfg["key"] and cfg["model"])


def judge(cands, timeout=60):
    """API 批量判分。cands: [{content,...}], 返回 [(score:int, reason:str)] 与 cands 等长。

    失败抛 RuntimeError，由调用方降级规则评分，绝不让采集管线中断。
    """
    cfg = load_cfg()
    if not available():
        raise RuntimeError("AI_SCORER 未配置")
    items = "\n".join(f'{i}. {c["content"][:200]}' for i, c in enumerate(cands))
    payload = {
        "model": cfg["model"],
        "temperature": 0,
        "messages": [{"role": "user", "content": PROMPT_TMPL.format(items=items)}],
    }
    req = urllib.request.Request(
        cfg["base"] + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {cfg['key']}"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.load(resp)
    text = body["choices"][0]["message"]["content"].strip()
    start, end = text.find("["), text.rfind("]")
    rows = json.loads(text[start:end + 1])
    out = [(0, "缺失项")] * len(cands)
    for row in rows:
        i = int(row.get("i", -1))
        if 0 <= i < len(cands):
            try:
                score = max(0, min(100, int(row.get("score", 0))))
            except (TypeError, ValueError):
                score = 0
            out[i] = (score, str(row.get("reason", ""))[:40])
    return out


def breakdown(model="agent"):
    """hits.score_breakdown 的元信息（可追溯判分版本）。"""
    return {"judge_engine": model, "rubric": RUBRIC_VERSION}

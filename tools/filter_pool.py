# -*- coding: utf-8 -*-
"""爆款评论三级门槛筛选（技能核心，不依赖网络，可离线自测）。

输入：逐行 JSONL 评论 或 聚合 dict（两种都兼容，自动识别）。
  单行评论字段: content(nickname), like_count, sub_comment_count, aweme_id, comment_id, create_time
  聚合 dict: {"by_aweme": {aid: {"comments":[{...}]}}, ...}  兼容 comments.py 的 comments.json

三级门槛（全部通过才成候选）：
  ① 高互动: like_count >= MIN_LIKES 或 sub_comment_count >= MIN_REPLIES
  ② 字数达标: 去符号/去空白后的有效字数 >= MIN_LEN
  ③ 可成文性: 规则评分 score >= MIN_SCORE（评分维度：长度结构/钩子词/情绪词/具体意象/
              人称/数字时间/疑问命令等；低于阈值或命中排除特征则淘汰）

用法:
  python tools/filter_pool.py --in <comments.json> --out <candidates.json>
      [--min-likes 500] [--min-replies 50] [--min-len 30] [--min-score 55]
      [--max-n 60] [--seed 0]

输出: <candidates.json>
  { meta: {...判定汇总}, candidates: [ {aweme_id, comment_id, nickname, content,
     like_count, sub_comment_count, len, score, reasons: [...], score_breakdown:{...} }, ... ] }
"""
import argparse
import json
import os
import re
import unicodedata

# 钩子词：能制造悬念/立场/冲突的表达
_HOOK_WORDS = (
    "别", "千万别", "不要", "劝", "建议", "一定要", "记住", "千万", "亲测", "实测", "真相",
    "其实", "没想到", "居然", "竟然", "原来", "终于", "终于发现", "后悔", "踩坑", "避坑",
    "避雷", "踩雷", "翻车", "别踩", "再也不会", "第一", "最", "唯一", "直接", "反而",
    "直到", "才发现", "看完", "奉劝", "试试", "趁", "钱", "白", "白白", "浪费", "智商税",
    "套路", "猫腻", "内幕", "秘密", "啥比", "不如", "更好", "更差", "伪", "假装",
)
# 情绪词：情绪浓度指示（惊喜/愤怒/遗憾/感动等口播常用情绪）
_EMO_WORDS = (
    "太太", "好想", "哭了", "笑", "搞笑", "离谱", "气死", "无语", "震撼", "惊喜", "意外",
    "感动", "破防", "心酸", "心疼", "羡慕", "嫉妒", "酸了", "yyds", "绝了", "真的", "太爽",
    "上头", "真香", "救命", "OMG", "啊啊", "嘿嘿", "哈哈哈", "无语子", "要命", "够够",
)
# 排除特征：纯口水/搬运/无骨架（命中其一直接淘汰）
_EXCLUDE_PATTERNS = (
    "哈哈哈", "哈哈哈哈", "已读", "收到", "打卡", "点赞支持", "谢谢分享", "晚安", "沙发",
    "前排", "收藏了", "码住", "插眼", "路过", "同款", "链接", "私我", "加V", "VX", "威信",
    "扣1", "888", "666", "6666", "顶一下", "学习了", "转了", "关注你",
)
_EMOJI_RE = re.compile(
    "[\U0001F600-\U0001FAFF"   # 表情/符号/动物等（不含跨入汉字区的宽范围）
    "\U0001F300-\U0001F5FF"
    "\U0001F900-\U0001F9FF"
    "\U0001F000-\U0001F0FF"
    "\U00002600-\U000027BF"
    "\U00002B00-\U00002BFF"
    "\U0000FE0F]"              # 变体选择符
)
_SYMBOL_RE = re.compile(r"[^\w\u4e00-\u9fff]")
# 入池字数统计口径：仅数字和中英文字母/汉字，不含标点、空白、emoji 等
_COUNT_RE = re.compile(r"[0-9A-Za-z\u4e00-\u9fff]")

# 评分维度权重（0-100）
_W_STR = 22   # 结构长度（30-80 字最佳）
_W_HOOK = 20  # 钩子词命中
_W_EMO = 15   # 情绪词命中
_W_IMG = 13   # 具体意象（专有名词/品牌/数字场景）
_W_NUM = 10   # 数字/时间
_W_PRS = 10   # 人称/行动主语
_W_QS = 10    # 疑问/命令/发生委托句式


def _clean(text):
    t = _EMOJI_RE.sub(" ", text or "")
    t = _SYMBOL_RE.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip()


def _eff_len(text):
    """有效字数：只计数字与中英文字符（标点/表情/空白不计入）。"""
    return len(_COUNT_RE.findall(text or ""))


def _hit(text, words):
    return [w for w in words if w in text]


def _img_terms(text):
    """具体意象计数：连续中文名词块/词，粗略统计非停用词密度。"""
    tokens = re.findall(r"[\u4e00-\u9fff]{2,}", _clean(text) or "")
    stops = {"但是", "因为", "所以", "如果", "就是", "而且", "但是", "然后", "觉得", "真的",
             "一个", "这个", "那个", "大家", "你们", "他们", "我们", "现在", "当时", "那里"}
    return [t for t in tokens if t not in stops]


def _score(text):
    """可成文性规则评分（0-100）。评分只作排序/淘汰线索，最终入选仍须过写作文本改造。"""
    bd = {"len": 0, "hook": 0, "emo": 0, "img": 0, "num": 0, "prs": 0, "qs": 0, "base": 0}
    eff = _clean(text)
    n = len(eff)
    if n >= 25:
        bd["len"] = _W_STR
    elif n >= 18:
        bd["len"] = round(_W_STR * 0.6)
    elif n >= 12:
        bd["len"] = round(_W_STR * 0.35)
    else:
        bd["len"] = 0

    hooks = _hit(eff, _HOOK_WORDS)
    bd["hook"] = min(_W_HOOK, len(hooks) * 6)
    emo = _hit(eff, _EMO_WORDS)
    bd["emo"] = min(_W_EMO, len(emo) * 5)
    img = _img_terms(text)
    bd["img"] = min(_W_IMG, len(set(img)) * 1)  # 具体意象丰富度
    num = re.findall(r"\d+|[一二两三四五六七八九十百千]", eff)
    bd["num"] = min(_W_NUM, len(set(num)) * 5)
    prs = re.findall(r"(我|你|咱|大家|我妈|我爸|我朋友|我闺蜜|家)", eff)
    bd["prs"] = min(_W_PRS, len(set(prs)) * 4)
    qs = re.findall(r"[？?]|(求|问|谁知道|有没有|怎么办|是不是|真的吗)", eff)
    bd["qs"] = min(_W_QS, len(set(qs)) * 5)
    bd["base"] = 8 if bd["hook"] + bd["emo"] + bd["img"] + bd["num"] + bd["prs"] + bd["qs"] >= 30 else 0
    total = bd["len"] + bd["hook"] + bd["emo"] + bd["img"] + bd["num"] + bd["prs"] + bd["qs"] + bd["base"]
    reasons = []
    if hooks:
        reasons.append(f"钩子词:{'/'.join(hooks[:3])}")
    if emo:
        reasons.append(f"情绪词:{'/'.join(emo[:3])}")
    if num:
        reasons.append("含数字/时间")
    if prs:
        reasons.append("含行动主体")
    if bd["len"] == _W_STR:
        reasons.append("结构完整")
    return min(100, total), bd, reasons


def _is_excluded(text):
    t = _clean(text)
    if not t:
        return "空内容"
    if len(t) < 8:
        return "过短"
    for p in _EXCLUDE_PATTERNS:
        if p in text:
            return f"口水词:{p}"
    return None


def iter_records(src):
    """输入自动识别：聚合 dict（by_aweme）→ 平铺；否则按逐行 JSONL 读。"""
    if isinstance(src, dict):
        for aid, blk in src.get("by_aweme", {}).items():
            for c in blk.get("comments", []):
                rec = dict(c)
                rec.setdefault("aweme_id", aid)
                yield rec
        return
    for line in src:
        line = line.strip()
        if line:
            try:
                yield json.loads(line)
            except Exception:
                pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--min-likes", type=int, default=500)
    ap.add_argument("--min-replies", type=int, default=50)
    ap.add_argument("--min-len", type=int, default=20)
    ap.add_argument("--min-score", type=float, default=55)
    ap.add_argument("--max-n", type=int, default=60)
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()

    raw = json.load(open(a.inp, encoding="utf-8")) if a.inp.endswith((".json", ".jsonl")) else None
    if raw is not None:
        if isinstance(raw, list):
            source = raw
        elif isinstance(raw, dict):
            source = raw
        else:
            source = raw
        records = list(iter_records(source))
    else:
        records = list(iter_records(open(a.inp, encoding="utf-8")))

    total = len(records)
    passed1 = passed2 = passed3 = 0
    excl_stats = {}
    candidates = []
    seen = set()
    for r in records:
        content = (r.get("content") or "").strip()
        if not content:
            continue
        cid = str(r.get("comment_id") or "")
        key = cid or content
        if key in seen:
            continue
        seen.add(key)
        likes = int(r.get("like_count") or 0)
        replies = int(r.get("sub_comment_count") or r.get("reply_count") or 0)
        if likes < a.min_likes and replies < a.min_replies:
            continue
        passed1 += 1
        elen = _eff_len(content)
        if elen < a.min_len:
            continue
        passed2 += 1
        excl = _is_excluded(content)
        if excl:
            excl_stats[excl] = excl_stats.get(excl, 0) + 1
            continue
        score, bd, why = _score(content)
        passed3 += 1
        candidates.append({
            "aweme_id": str(r.get("aweme_id") or ""),
            "comment_id": cid,
            "nickname": r.get("nickname") or "",
            "content": content,
            "like_count": likes,
            "sub_comment_count": replies,
            "create_time": r.get("create_time"),
            "len": elen,
            "score": score,
            "score_breakdown": bd,
            "reasons": why,
        })

    # 打分=0（纯口水结构）淘汰
    candidates = [c for c in candidates if c["score"] >= a.min_score]
    candidates.sort(key=lambda c: (c["score"], c["like_count"]), reverse=True)
    candidates = candidates[: a.max_n]

    op = a.out
    os.makedirs(os.path.dirname(os.path.abspath(op)), exist_ok=True)
    meta = {
        "input": a.inp,
        "records_read": total,
        "passed_high_engage": passed1,
        "passed_min_len": passed2,
        "passed_score": len(candidates),
        "score_class": [c["score"] for c in candidates][:5],
        "thresholds": {"min_likes": a.min_likes, "min_replies": a.min_replies,
                       "min_len": a.min_len, "min_score": a.min_score},
        "excluded_reasons_top": dict(sorted(excl_stats.items(), key=lambda x: -x[1])[:8]),
    }
    out = {"meta": meta, "candidates": candidates}
    tmp = op + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    os.replace(tmp, op)
    print(f"[filter_pool] 读取 {total} 条 | 高互动 {passed1} | 字数达标 {passed2} | 可成文候选 {len(candidates)}")
    print(f"[exclude] {excl_stats or '无'}")
    print(f"[out] {op}")
    if candidates:
        print("=== Top 候选 ===")
        for c in candidates[:5]:
            print(f"  [score {c['score']}] {c['content'][:38]}")


if __name__ == "__main__":
    main()
# -*- coding: utf-8 -*-
"""聚合 MediaCrawler 关键词搜索评论：把 search_comments_*.jsonl 按视频聚合成 top-N。

与 douyin-crawl-report/comments.py 输出结构兼容（by_aweme），从而 filter_pool.py 可直接消费。
评论原始字段: content, nickname, like_count, sub_comment_count, comment_id, create_time, aweme_id

两种用法:
  1) 纯内存（主路径，run_daily 实时调用，不落任何 JSON）:
     by_aweme, summary = aggregate_paths(fps, max_n=100)
  2) CLI 落盘（仅离线调试）:
     python tools/aggregate_comments.py --in <search_comments_*.jsonl 目录|文件> --out <聚合.json>
       [--max 100]  每视频按赞降序保留条数上限

落盘输出: { summary:{...}, by_aweme:{ aid:{ n, max, comments:[{content,nickname,like_count,sub_comment_count,comment_id,create_time}] } } }
"""
import argparse
import glob
import json
import os


def aggregate_paths(fps, max_n=100):
    """聚合多个 search_comments_*.jsonl → (by_aweme, summary)。纯内存，不写任何文件。

    每视频按 like_count 降序截取 top max_n；同 comment_id（缺省退化为同文本）去重。
    """
    by_aweme, seen, total, corrupt = {}, {}, 0, 0
    for f in fps:
        for line in open(f, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                ct = json.loads(line)
            except Exception:
                corrupt += 1
                continue
            aid = str(ct.get("aweme_id") or "")
            content = (ct.get("content") or "").strip()
            cid = str(ct.get("comment_id") or "")
            if not aid or not content:
                continue
            key = cid if cid else content
            s = seen.setdefault(aid, set())
            if key in s:
                continue
            s.add(key)
            total += 1
            by_aweme.setdefault(aid, []).append({
                "content": content,
                "nickname": ct.get("nickname") or "",
                "like_count": int(ct.get("like_count") or 0),
                "sub_comment_count": int(ct.get("sub_comment_count") or
                                        ct.get("reply_count") or 0),
                "comment_id": cid,
                "create_time": ct.get("create_time"),
            })
    # 先排序再截 top-N（旧版先切片后排序，top-N 并非按赞，已修复）
    for v in by_aweme.values():
        v.sort(key=lambda c: c["like_count"], reverse=True)
    out = {aid: {"n": len(v[:max_n]), "max": max_n, "comments": v[:max_n]}
           for aid, v in by_aweme.items()}
    summary = {"source_files": [os.path.basename(f) for f in fps],
               "total_comments": total, "corrupt": corrupt,
               "total_videos": len(by_aweme)}
    return out, summary


def find_comment_files(inp):
    """定位输入（目录/通配/单文件）对应的 search_comments_*.jsonl 文件列表。"""
    if os.path.isdir(inp):
        return sorted(glob.glob(os.path.join(inp, "**", "*_comments_*.jsonl"), recursive=True))
    if "*" in inp:
        return sorted(glob.glob(inp, recursive=True))
    return [inp]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True, help="指到 *_comments_*.jsonl 文件或含该文件的目录")
    ap.add_argument("--out", required=True)
    ap.add_argument("--max", type=int, default=100)
    a = ap.parse_args()

    fps = find_comment_files(a.inp)
    out, summary = aggregate_paths(fps, max_n=a.max)

    op = a.out
    os.makedirs(os.path.dirname(os.path.abspath(op)), exist_ok=True)
    tmp = op + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"summary": summary, "by_aweme": out}, f, ensure_ascii=False, indent=1)
    os.replace(tmp, op)
    print(f"[aggregate] {len(fps)} 文件 | {summary['total_comments']} 评论 | "
          f"{summary['total_videos']} 视频 | 每视频 top {a.max}")
    print(f"[out] {op}")


if __name__ == "__main__":
    main()

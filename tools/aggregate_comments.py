# -*- coding: utf-8 -*-
"""聚合 MediaCrawler 关键词搜索评论：把 search_comments_*.jsonl 按视频聚合成 top-N。

与 douyin-crawl-report/comments.py 输出结构兼容（by_aweme），从而 filter_pool.py 可直接消费。
评论原始字段: content, nickname, like_count, sub_comment_count, comment_id, create_time, aweme_id

用法:
  python tools/aggregate_comments.py --in <search_comments_*.jsonl 目录|文件> --out <聚合.json>
      [--max 100]  每视频按赞降序保留条数上限

输出: { summary:{...}, by_aweme:{ aid:{ n, max, comments:[{content,nickname,like_count,sub_comment_count,comment_id,create_time}] } } }
"""
import argparse
import glob
import json
import os


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True, help="指到 *_comments_*.jsonl 文件或含该文件的目录")
    ap.add_argument("--out", required=True)
    ap.add_argument("--max", type=int, default=100)
    a = ap.parse_args()

    if os.path.isdir(a.inp):
        fps = sorted(glob.glob(os.path.join(a.inp, "**", "*_comments_*.jsonl"), recursive=True))
    elif "*" in a.inp:
        fps = sorted(glob.glob(a.inp, recursive=True))
    else:
        fps = [a.inp]

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

    out = {aid: {"n": len(v[:a.max]), "max": a.max, "comments": v[:a.max]}
           for aid, v in by_aweme.items()}
    for v in by_aweme.values():
        v.sort(key=lambda c: c["like_count"], reverse=True)

    op = a.out
    os.makedirs(os.path.dirname(os.path.abspath(op)), exist_ok=True)
    summary = {"source_files": [os.path.basename(f) for f in fps],
               "total_comments": total, "corrupt": corrupt,
               "total_videos": len(by_aweme)}
    tmp = op + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"summary": summary, "by_aweme": out}, f, ensure_ascii=False, indent=1)
    os.replace(tmp, op)
    print(f"[aggregate] {len(fps)} 文件 | {total} 评论 | {len(by_aweme)} 视频 | 每视频 top {a.max}")
    print(f"[out] {op}")


if __name__ == "__main__":
    main()
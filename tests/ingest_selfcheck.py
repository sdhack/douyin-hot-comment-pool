# -*- coding: utf-8 -*-
"""实时入库管线离线自测：验证 _ingest_keyword_run 的 入库→内存筛选→配额封顶→清理 全链路。

不依赖网络/MediaCrawler：伪造 collect_search 产物目录（douyin/jsonl），直接调用 run_daily 的
单关键词处理函数，断言：① 原始数据入 SQLite；② hits 按三级门槛+配额写入；③ 运行目录被删除
（零 JSON 残留）；④ 空结果目录同样被清理；⑤ aggregate_paths 按赞降序截 top-N。
"""
import argparse
import datetime
import io
import json
import os
import shutil
import sys
import tempfile
from contextlib import redirect_stdout

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(SKILL, "tools"))
sys.path.insert(0, os.path.join(SKILL, "sqlite"))

import aggregate_comments  # noqa: E402
import run_daily  # noqa: E402
import db as dbmod  # noqa: E402

PASS, FAIL = [], []


def ok(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  " + str(detail)) if detail else ""))


GOOD = [  # 三级门槛全过的长评论样例
    ("花三个月钱买的东西，结果发现是智商税，姐妹们一定要看完这条，真的气死了", 5200, 300),
    ("在我家楼下那家店，我闺蜜说好用我就买了，现在天天后悔，真香，别买贵了", 2300, 80),
    ("第一次知道原来熬夜真会掉头发，医生都这么说，我后悔没早点看", 1500, 40),
]
BAD = [
    ("哈哈哈", 80000, 10),        # 短口水
    ("已读", 9999, 0),            # 排除词
    ("普通短评", 10, 1),          # 互动不足
]


def _args(tmp, quota, account="t"):
    return argparse.Namespace(
        root=tmp, account=account, quota=quota,
        min_likes=1000, min_replies=40, min_len=20, min_score=50)


def _mk_run(tmp, account="t"):
    """伪造一个 collect_search 运行目录：crawl_<account>/douyin/jsonl/{contents,comments}.jsonl"""
    run_root = os.path.join(tmp, f"{account}-20260823-120000")
    jl = os.path.join(run_root, f"crawl_{account}", "douyin", "jsonl")
    os.makedirs(os.path.join(run_root, f"crawl_{account}", "cursor"), exist_ok=True)
    os.makedirs(jl, exist_ok=True)
    with open(os.path.join(jl, "search_contents_2026-08-23.jsonl"), "w", encoding="utf-8") as f:
        f.write(json.dumps({"aweme_id": "111", "creator_hash": "uA", "nickname": "甲",
                            "title": "视频甲", "liked_count": 100, "comment_count": 9},
                           ensure_ascii=False) + "\n")
    with open(os.path.join(jl, "search_comments_2026-08-23.jsonl"), "w", encoding="utf-8") as f:
        for i, (content, likes, reps) in enumerate(GOOD + BAD):
            f.write(json.dumps({"aweme_id": "111", "comment_id": f"c{i}", "nickname": "评",
                                "content": content, "like_count": likes,
                                "sub_comment_count": reps, "create_time": 0},
                               ensure_ascii=False) + "\n")
    return run_root


def main():
    tmp = tempfile.mkdtemp(prefix="hcp_ingest_ck_")

    # 0) aggregate_paths：按赞降序截 top-N（回归旧版“先切片后排序”bug）
    with open(os.path.join(tmp, "raw.jsonl"), "w", encoding="utf-8") as f:
        for i, (content, likes, reps) in enumerate(GOOD + BAD):
            f.write(json.dumps({"aweme_id": "111", "comment_id": f"c{i}",
                                "content": content, "like_count": likes,
                                "sub_comment_count": reps}, ensure_ascii=False) + "\n")
    by, sm = aggregate_comments.aggregate_paths([os.path.join(tmp, "raw.jsonl")], max_n=3)
    likes_seq = [c["like_count"] for c in by["111"]["comments"]]
    ok("aggregate_paths top-N 按赞降序", likes_seq == sorted(likes_seq, reverse=True)
       and len(likes_seq) == 3 and sm["total_comments"] == 6, likes_seq)

    # 1) 全链路：入库→筛选→hits→目录清理
    dbp = os.path.join(tmp, "t1.db")
    conn = run_daily._open_db(dbp)
    rr = _mk_run(tmp)
    row = run_daily._ingest_keyword_run(conn, _args(tmp, quota=5), "测试词", rr)
    today = datetime.date.today().isoformat()
    n_hit = conn.execute("SELECT COUNT(*) AS n FROM hits WHERE hit_date=?", (today,)).fetchone()["n"]
    ok("原始数据实时入库（视频1/评论6）", row["videos"] == 1 and row["comments"] == 6,
       (row["videos"], row["comments"]))
    ok("三级门槛命中 3 条（口水/短评被剔）", n_hit == 3 and row["new_hits"] == 3,
       (n_hit, row["new_hits"]))
    ok("运行目录已删除（零 JSON 残留）", row["cleaned"] and not os.path.isdir(rr))
    ok("状态=成功", row["status"] == "成功", row["status"])

    # 2) 配额封顶：quota=1 只取最高分 1 条
    dbp2 = os.path.join(tmp, "t2.db")
    conn2 = run_daily._open_db(dbp2)
    rr2 = _mk_run(tmp, account="t2")
    row2 = run_daily._ingest_keyword_run(conn2, _args(tmp, quota=1, account="t2"), "测试词", rr2)
    n_hit2 = conn2.execute("SELECT COUNT(*) AS n FROM hits WHERE hit_date=?", (today,)).fetchone()["n"]
    ok("配额封顶：quota=1 只入 1 条", n_hit2 == 1 and row2["new_hits"] == 1,
       (n_hit2, row2["new_hits"]))

    # 3) 采集中的增量筛选：无需等待关键词结束即可命中并触发配额闸门
    dbp_live = os.path.join(tmp, "live.db")
    conn_live = run_daily._open_db(dbp_live)
    rr_live = _mk_run(tmp, account="live")
    live_args = _args(tmp, quota=1, account="live")
    live_state = {"videos": 0, "comments": 0, "candidates": 0, "hits": 0, "sample": ""}
    reached = run_daily._incremental_screen(conn_live, live_args, "测试词", "live-batch",
                                            rr_live, live_state)
    live_hits = conn_live.execute("SELECT COUNT(*) AS n FROM hits").fetchone()["n"]
    ok("增量筛选达到配额即报告停止", reached and live_hits == 1 and live_state["hits"] == 1
       and live_state["candidates"] == 3 and "赞/" in live_state["sample"],
       (reached, live_hits, live_state["sample"][:24]))
    live_state["last_video"] = "视频甲"
    live_state["last_video_meta"] = ("作者甲", 10000, 120)
    live_state["last_comment"] = "这是一条用于验证 Markdown 汇报的长评论"
    live_state["last_comment_meta"] = (5200, 300)
    report_buf = io.StringIO()
    with redirect_stdout(report_buf):
        run_daily._print_live_report(conn_live, live_args, "测试词", live_state)
    report = report_buf.getvalue()
    ok("实时汇报为 Markdown 且包含视频/评论明细",
       "### 抖音爆款评论采集进度" in report and "| 最新视频 |" in report
       and "| 最新评论 |" in report and "视频甲" in report and "5200" in report,
       report[:80])
    shutil.rmtree(rr_live)

    # 4) 空结果目录：无 jsonl → 状态空结果 + 目录仍被清理
    rr3 = os.path.join(tmp, "t3-20260823-130000")
    os.makedirs(os.path.join(rr3, "crawl_t3", "cursor"), exist_ok=True)
    row3 = run_daily._ingest_keyword_run(conn2, _args(tmp, quota=5), "测试词", rr3)
    ok("空结果目录被清理", row3["status"] == "空结果" and not os.path.isdir(rr3),
       row3["status"])

    # 5) 跨天去重：历史（非当日）已命中的评论不再重复入选，也不占用当日配额
    dbp4 = os.path.join(tmp, "t4.db")
    conn4 = run_daily._open_db(dbp4)
    rr4 = _mk_run(tmp, account="t4")
    # 只导入原始数据（不写 hits），再手动把最高分那条标为历史命中（hit_date=过去某天）
    _loader_import = __import__("loader").import_batch
    _loader_import(conn4, os.path.join(rr4, "crawl_t4"), keyword="测试词", account="t4")
    old_day = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    conn4.execute(
        "INSERT INTO hits(comment_id, aweme_id, score, passed_level, reasons, score_break, hit_date) "
        "VALUES('c0','111',90,'123','[]','{}',?) "
        "ON CONFLICT(comment_id) DO UPDATE SET hit_date=excluded.hit_date", (old_day,))
    conn4.commit()
    row4 = run_daily._ingest_keyword_run(conn4, _args(tmp, quota=5, account="t4"), "测试词", rr4)
    n_hit4 = conn4.execute("SELECT COUNT(*) AS n FROM hits WHERE hit_date=?", (today,)).fetchone()["n"]
    c0_today = conn4.execute(
        "SELECT COUNT(*) AS n FROM hits WHERE comment_id='c0' AND hit_date=?", (today,)).fetchone()["n"]
    ok("跨天去重：历史命中(c0)不再入选，其余照常", c0_today == 0 and row4["new_hits"] == 2,
       (row4["new_hits"], n_hit4, c0_today))
    conn.close(); conn2.close(); conn_live.close(); conn4.close()

    print()
    if FAIL:
        print(f"[ingest_selfcheck] 失败 {len(FAIL)} 项: {FAIL}")
        return 1
    print(f"[ingest_selfcheck] 全部 {len(PASS)} 项通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())

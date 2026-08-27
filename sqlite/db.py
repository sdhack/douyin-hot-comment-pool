# -*- coding: utf-8 -*-
"""SQLite 数据中心：建库 + schema + 连接。技能内置、随项目走、可整体移植。

设计要点：
  * 幂等：accounts/videos/comments 全部 UPSERT（唯一键 ON CONFLICT），重复采集不产生脏数据。
  * 外键关联：videos→accounts、comments→video+author、ancestry 表达父子讨论串、hits 标记爆款。
  * 大字段只入 URL 不解析：videos 的下载链、comments 的 pictures 仅存 URL 字符串，避免体积膨胀。

用法：
  from db import open_db, ensure_schema   # 或命令行 python db.py --db <path> --init
"""
import argparse
import os
import sqlite3

# 数据库目录固定在技能内，随技能整体移植（用户指定）
_SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(_SKILL_DIR, "douyin_hotpool.db")

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS accounts (
    creator_hash  TEXT PRIMARY KEY,
    nickname      TEXT DEFAULT '',
    first_seen    TEXT,            -- 首次采到(该作者相关批次)日期
    last_seen     TEXT,
    video_count   INTEGER DEFAULT 0,
    comment_count INTEGER DEFAULT 0,
    total_likes   INTEGER DEFAULT 0,
    created_at    TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS videos (
    aweme_id            TEXT PRIMARY KEY,
    creator_hash        TEXT NOT NULL,
    nickname            TEXT DEFAULT '',
    title               TEXT DEFAULT '',
    delta_desc          TEXT DEFAULT '',
    aweme_type          INTEGER,
    liked_count         INTEGER DEFAULT 0,
    collected_count     INTEGER DEFAULT 0,
    comment_count       INTEGER DEFAULT 0,
    share_count         INTEGER DEFAULT 0,
    create_time         INTEGER,
    source_keyword      TEXT DEFAULT '',
    first_seen_batch    TEXT,
    aweme_url           TEXT DEFAULT '',
    cover_url           TEXT DEFAULT '',
    video_download_url  TEXT DEFAULT '',
    music_download_url  TEXT DEFAULT '',
    note_download_url   TEXT DEFAULT '',
    created_at          TEXT DEFAULT (datetime('now','localtime')),
    FOREIGN KEY (creator_hash) REFERENCES accounts(creator_hash)
);

CREATE TABLE IF NOT EXISTS comments (
    comment_id          TEXT PRIMARY KEY,
    aweme_id            TEXT NOT NULL,
    creator_hash        TEXT DEFAULT '',   -- 评论作者
    parent_comment_id   TEXT DEFAULT '',   -- 为二级回复时指向其一父评论
    content             TEXT DEFAULT '',
    like_count          INTEGER DEFAULT 0,
    sub_comment_count   INTEGER DEFAULT 0, -- 回复数
    create_time         INTEGER,
    last_modify_ts      INTEGER,
    pictures_url        TEXT DEFAULT '',   -- 只入URL(json数组字符串)，不解析图片
    is_top_comment      INTEGER DEFAULT 0, -- 该批是否是首页热评(前N)
    batch_id            TEXT,
    FOREIGN KEY (aweme_id) REFERENCES videos(aweme_id)
);

-- 讨论串自关联：一级评论 parent 为空，二级 parent=一级 comment_id
CREATE TABLE IF NOT EXISTS ancestry (
    child_comment_id  TEXT PRIMARY KEY,
    parent_comment_id TEXT DEFAULT '',
    root_comment_id   TEXT DEFAULT '',
    depth             INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS hits (
    comment_id    TEXT PRIMARY KEY,
    aweme_id      TEXT DEFAULT '',
    score         REAL DEFAULT 0,
    passed_level  TEXT DEFAULT '',   -- "123" / "1|2|3"
    reasons       TEXT DEFAULT '',   -- json数组
    score_break   TEXT DEFAULT '',   -- json对象
    hit_date      TEXT,
    batch_id      TEXT,
    FOREIGN KEY (comment_id) REFERENCES comments(comment_id)
);

CREATE TABLE IF NOT EXISTS batches (
    batch_id       TEXT PRIMARY KEY,
    run_root       TEXT DEFAULT '',
    account        TEXT DEFAULT '',
    keyword        TEXT DEFAULT '',
    started_at     TEXT,
    finished_at    TEXT,
    videos_count   INTEGER DEFAULT 0,
    comments_count INTEGER DEFAULT 0,
    status          TEXT DEFAULT 'running', -- running/ingesting/screening/cleaning/completed/failed/empty
    phase           TEXT DEFAULT 'init',
    last_progress_at TEXT,
    retry_count     INTEGER DEFAULT 0,
    skipped_count   INTEGER DEFAULT 0,
    error          TEXT DEFAULT '',        -- 空=成功
    source_dir     TEXT DEFAULT '',
    created_at     TEXT DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_comments_aweme ON comments(aweme_id);
CREATE INDEX IF NOT EXISTS idx_comments_author ON comments(creator_hash);
CREATE INDEX IF NOT EXISTS idx_comments_parent ON comments(parent_comment_id);
CREATE INDEX IF NOT EXISTS idx_videos_author ON videos(creator_hash);
CREATE INDEX IF NOT EXISTS idx_hits_date ON hits(hit_date);
"""

# 汇总视图：爆款评论 + 视频标题 + 作者昵称 一站式
_CREATE_VIEWS = """
CREATE VIEW IF NOT EXISTS vw_hot_comments AS
SELECT h.comment_id, h.score, h.passed_level, h.hit_date,
       c.content, c.like_count, c.sub_comment_count, c.create_time,
       v.aweme_id, v.title, v.source_keyword,
       a.nickname AS author_nickname
FROM hits h
LEFT JOIN comments c ON c.comment_id = h.comment_id
LEFT JOIN videos v   ON v.aweme_id = c.aweme_id
LEFT JOIN accounts a  ON a.creator_hash = v.creator_hash;  -- fix Bug-8: 经视频关联作者（评论侧hash常为空）

-- 讨论串层级视图
CREATE VIEW IF NOT EXISTS vw_threads AS
SELECT a.root_comment_id, c.comment_id, c.parent_comment_id, a.depth,
       c.content, c.creator_hash, c.create_time
FROM ancestry a
JOIN comments c ON c.comment_id = a.child_comment_id
ORDER BY a.root_comment_id, a.depth, c.create_time;

-- 日统计累计视图
CREATE VIEW IF NOT EXISTS vw_daily_stats AS
SELECT
  date(b.started_at)              AS day,
  count(DISTINCT v.aweme_id)      AS videos,
  count(DISTINCT c.comment_id)    AS comments,
  count(DISTINCT h.comment_id)    AS hits
FROM batches b
LEFT JOIN videos v   ON v.first_seen_batch = b.batch_id
LEFT JOIN comments c ON c.batch_id = b.batch_id
LEFT JOIN hits h     ON h.batch_id = b.batch_id
GROUP BY date(b.started_at);
"""


def _dict_factory(cursor, row):
    return {col[0]: row[i] for i, col in enumerate(cursor.description)}


def open_db(path=None):
    path = path or DEFAULT_DB
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = _dict_factory
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def ensure_schema(conn, create_views=True):
    conn.executescript(_SCHEMA)
    # Keep existing user databases upgradeable without destructive rebuilds.
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(batches)")}
    migrations = {
        "status": "TEXT DEFAULT 'running'",
        "phase": "TEXT DEFAULT 'init'",
        "last_progress_at": "TEXT",
        "retry_count": "INTEGER DEFAULT 0",
        "skipped_count": "INTEGER DEFAULT 0",
    }
    for name, definition in migrations.items():
        if name not in cols:
            conn.execute(f"ALTER TABLE batches ADD COLUMN {name} {definition}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_batches_status ON batches(status, last_progress_at)")
    if create_views:
        conn.executescript(_CREATE_VIEWS)
    conn.commit()


def update_batch(conn, batch_id, status=None, phase=None, error=None,
                 videos_count=None, comments_count=None, retry_count=None,
                 skipped_count=None, finished=False):
    """Update observable batch state; safe for minute progress polling."""
    fields, values = ["last_progress_at=datetime('now','localtime')"], []
    for name, value in (("status", status), ("phase", phase), ("error", error),
                        ("videos_count", videos_count), ("comments_count", comments_count),
                        ("retry_count", retry_count), ("skipped_count", skipped_count)):
        if value is not None:
            fields.append(f"{name}=?")
            values.append(value)
    if finished:
        fields.append("finished_at=datetime('now','localtime')")
    values.append(batch_id)
    conn.execute(f"UPDATE batches SET {', '.join(fields)} WHERE batch_id=?", values)
    conn.commit()


def init_db(path=None):
    conn = open_db(path)
    ensure_schema(conn)
    tbls = [r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view') ORDER BY type,name")]
    conn.close()
    return tbls


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None)
    ap.add_argument("--init", action="store_true")
    a = ap.parse_args()
    if a.init:
        tables = init_db(a.db)
        print("已初始化库:", a.db or DEFAULT_DB)
        print("对象:", ", ".join(tables))
    else:
        print("用法: python db.py --init [--db <path>]")

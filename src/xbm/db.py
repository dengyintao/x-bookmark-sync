"""sqlite 存储：去重集合 / watermark / 运行日志。

表：
- tweets(tweet_id PK, author, screen_name, text, created_at, synced_at, has_article)
- media_files(id PK, tweet_id, kind, local_path, bytes, remote_url)
- sync_runs(id PK, started_at, finished_at, new_count, status, error)

watermark = tweets 表中 rowid 最新的一条（无需另建表）。
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any


_SCHEMA = """
CREATE TABLE IF NOT EXISTS tweets(
    tweet_id    TEXT PRIMARY KEY,
    author      TEXT,
    screen_name TEXT,
    text        TEXT,
    created_at  TEXT,
    synced_at   REAL,
    has_article INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS media_files(
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    tweet_id   TEXT,
    kind       TEXT,
    local_path TEXT,
    bytes      INTEGER,
    remote_url TEXT
);
CREATE TABLE IF NOT EXISTS sync_runs(
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at REAL,
    finished_at REAL,
    new_count  INTEGER DEFAULT 0,
    status     TEXT,
    error      TEXT
);
"""


class Store:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)

    def seen(self, tweet_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM tweets WHERE tweet_id=?", (tweet_id,)
        ).fetchone()
        return row is not None

    def record_tweet(self, info: dict[str, Any]) -> None:
        self._conn.execute(
            """INSERT OR IGNORE INTO tweets
               (tweet_id, author, screen_name, text, created_at, synced_at, has_article)
               VALUES (?,?,?,?,?,?,?)""",
            (
                info["tweet_id"],
                info.get("author", ""),
                info.get("screen_name", ""),
                info.get("text", ""),
                info.get("created_at", ""),
                time.time(),
                1 if info.get("has_article") else 0,
            ),
        )
        self._conn.commit()

    def latest_tweet_id(self) -> str | None:
        """watermark：最新入库的一条 tweet_id。"""
        row = self._conn.execute(
            "SELECT tweet_id FROM tweets ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        return row["tweet_id"] if row else None

    def count_tweets(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM tweets").fetchone()
        return row["n"]

    def start_run(self) -> int:
        cur = self._conn.execute(
            "INSERT INTO sync_runs(started_at, status) VALUES (?, 'running')",
            (time.time(),),
        )
        self._conn.commit()
        return cur.lastrowid

    def finish_run(self, run_id: int, new_count: int,
                   status: str = "ok", error: str = "") -> None:
        self._conn.execute(
            """UPDATE sync_runs
               SET finished_at=?, new_count=?, status=?, error=?
               WHERE id=?""",
            (time.time(), new_count, status, error, run_id),
        )
        self._conn.commit()

    def consecutive_failures(self) -> int:
        rows = self._conn.execute(
            "SELECT status FROM sync_runs ORDER BY id DESC LIMIT 20"
        ).fetchall()
        n = 0
        for r in rows:
            if r["status"] == "failed":
                n += 1
            else:
                break
        return n

    def close(self) -> None:
        self._conn.close()

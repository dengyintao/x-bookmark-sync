"""sqlite 存储：去重集合 / watermark / 运行日志 / 下载队列。

表：
- tweets(tweet_id PK, author, screen_name, text, created_at, synced_at, has_article, url)
- media_files(id PK, tweet_id, kind, remote_url, local_path, bytes,
              status, retries, error, next_retry_at, created_at,
              UNIQUE(tweet_id, remote_url))
- sync_runs(id PK, started_at, finished_at, new_count, status, error)

watermark = tweets 表中 rowid 最新的一条（无需另建表）。
"""
from __future__ import annotations

import sqlite3
import threading
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
    has_article INTEGER DEFAULT 0,
    url         TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS media_files(
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    tweet_id     TEXT,
    kind         TEXT,
    remote_url   TEXT,
    local_path   TEXT DEFAULT '',
    bytes        INTEGER DEFAULT 0,
    status       TEXT DEFAULT 'pending',
    retries      INTEGER DEFAULT 0,
    error        TEXT DEFAULT '',
    next_retry_at REAL DEFAULT 0,
    created_at   REAL,
    UNIQUE(tweet_id, remote_url)
);
CREATE INDEX IF NOT EXISTS idx_media_status
    ON media_files(status, next_retry_at);
CREATE TABLE IF NOT EXISTS sync_runs(
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at REAL,
    finished_at REAL,
    new_count  INTEGER DEFAULT 0,
    status     TEXT,
    error      TEXT
);
"""

# 老库迁移：media_files 缺列时补上（M1 的库只有 6 列）
_MIGRATE_MEDIA = [
    ("local_path", "TEXT DEFAULT ''"),
    ("bytes", "INTEGER DEFAULT 0"),
    ("status", "TEXT DEFAULT 'pending'"),
    ("retries", "INTEGER DEFAULT 0"),
    ("error", "TEXT DEFAULT ''"),
    ("next_retry_at", "REAL DEFAULT 0"),
    ("created_at", "REAL"),
]


class Store:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # ThreadingHTTPServer 多线程共用连接：关掉线程检查 + 全局锁串行化
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA.replace('CREATE INDEX IF NOT EXISTS idx_media_status\n    ON media_files(status, next_retry_at);', ''))
            self._migrate_media()
            self._conn.execute('CREATE INDEX IF NOT EXISTS idx_media_status ON media_files(status, next_retry_at)')
            # tweets.url 列也是 M2 新增的
            cols = {r["name"] for r in
                    self._conn.execute("PRAGMA table_info(tweets)").fetchall()}
            if "url" not in cols:
                self._conn.execute("ALTER TABLE tweets ADD COLUMN url TEXT DEFAULT ''")
            self._conn.commit()

    def _migrate_media(self) -> None:
        cols = {r["name"] for r in
                self._conn.execute("PRAGMA table_info(media_files)").fetchall()}
        for name, ddl in _MIGRATE_MEDIA:
            if name not in cols:
                self._conn.execute(f"ALTER TABLE media_files ADD COLUMN {name} {ddl}")
        # 补唯一约束：老库可能已有重复行，先清重再建唯一索引
        self._conn.execute(
            """DELETE FROM media_files WHERE id NOT IN
               (SELECT MIN(id) FROM media_files GROUP BY tweet_id, remote_url)"""
        )
        self._conn.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_media_unique
               ON media_files(tweet_id, remote_url)"""
        )

    def ingest_batch(self, tweets: list[dict[str, Any]]) -> dict[str, Any]:
        """Commit tweet metadata, media jobs and receipt as one transaction."""
        new_count = media_count = 0
        now = time.time()
        with self._lock, self._conn:
            for tweet in tweets:
                cur = self._conn.execute(
                    """INSERT OR IGNORE INTO tweets
                       (tweet_id, author, screen_name, text, created_at, synced_at, has_article, url)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (tweet["tweet_id"], tweet["author"], tweet["screen_name"],
                     tweet["text"], tweet["created_at"], now, int(tweet["has_article"]), tweet["url"]),
                )
                new_count += cur.rowcount
                for kind, url in tweet["jobs"]:
                    cur = self._conn.execute(
                        """INSERT OR IGNORE INTO media_files
                           (tweet_id, kind, remote_url, created_at) VALUES (?,?,?,?)""",
                        (tweet["tweet_id"], kind, url, now),
                    )
                    media_count += cur.rowcount
            self._conn.execute(
                """INSERT INTO sync_runs(started_at, finished_at, new_count, status)
                   VALUES (?,?,?,'ok')""", (now, now, new_count),
            )
            total = self._conn.execute('SELECT COUNT(*) FROM tweets').fetchone()[0]
        return {"ok": True, "new_count": new_count,
                "dup_count": len(tweets) - new_count, "media_queued": media_count, "total": total}

    def seen(self, tweet_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM tweets WHERE tweet_id=?", (tweet_id,)
            ).fetchone()
        return row is not None

    def record_tweet(self, info: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT OR IGNORE INTO tweets
                   (tweet_id, author, screen_name, text, created_at, synced_at,
                    has_article, url)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    info["tweet_id"],
                    info.get("author", ""),
                    info.get("screen_name", ""),
                    info.get("text", ""),
                    info.get("created_at", ""),
                    time.time(),
                    1 if info.get("has_article") else 0,
                    info.get("url", ""),
                ),
            )
            self._conn.commit()

    def get_tweet(self, tweet_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tweets WHERE tweet_id=?", (tweet_id,)
            ).fetchone()
        return dict(row) if row else None

    # ---- 下载队列 ----

    def add_media(self, tweet_id: str, kind: str, remote_url: str) -> None:
        """幂等：同一 tweet 的同一 URL 只入队一次。"""
        if not remote_url:
            return
        with self._lock:
            self._conn.execute(
                """INSERT OR IGNORE INTO media_files
                   (tweet_id, kind, remote_url, created_at)
                   VALUES (?,?,?,?)""",
                (tweet_id, kind, remote_url, time.time()),
            )
            self._conn.commit()

    def recover_interrupted_media(self) -> None:
        """Called once by the service, never by read/export clients."""
        with self._lock, self._conn:
            self._conn.execute("UPDATE media_files SET status='pending' WHERE status='downloading'")

    def claim_media(self, max_retries: int) -> dict[str, Any] | None:
        """原子领取一个待下载任务（pending，或可重试的 failed）。"""
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                """SELECT * FROM media_files
                   WHERE status IN ('pending', 'failed') AND retries<? AND next_retry_at<=?
                   ORDER BY created_at ASC LIMIT 1""",
                (max_retries, now),
            ).fetchone()
            if not row:
                return None
            self._conn.execute(
                "UPDATE media_files SET status='downloading', error='' WHERE id=?",
                (row["id"],),
            )
            self._conn.commit()
            return dict(row)

    def mark_done(self, media_id: int, local_path: str, size: int) -> None:
        with self._lock:
            self._conn.execute(
                """UPDATE media_files
                   SET status='done', local_path=?, bytes=?, error=''
                   WHERE id=?""",
                (local_path, size, media_id),
            )
            self._conn.commit()

    def mark_failed(self, media_id: int, error: str, max_retries: int) -> None:
        with self._lock:
            row = self._conn.execute(
                "SELECT retries FROM media_files WHERE id=?", (media_id,)
            ).fetchone()
            retries = (row["retries"] if row else 0) + 1
            # 指数退避：1m, 2m, 4m … 上限 1h；超上限则永久 failed
            if retries >= max_retries:
                status, next_at = "failed", 0
            else:
                status = "pending"
                next_at = time.time() + min(3600, 60 * (2 ** (retries - 1)))
            self._conn.execute(
                """UPDATE media_files
                   SET status=?, retries=?, error=?, next_retry_at=?
                   WHERE id=?""",
                (status, retries, error[:500], next_at, media_id),
            )
            self._conn.commit()

    def media_for_tweet(self, tweet_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM media_files WHERE tweet_id=? ORDER BY id",
                (tweet_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def tweet_media_done(self, tweet_id: str) -> bool:
        """该推文所有媒体都已 done（且至少有一个）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT status FROM media_files WHERE tweet_id=?", (tweet_id,)
            ).fetchall()
        return bool(rows) and all(r["status"] == "done" for r in rows)

    def media_stats(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM media_files GROUP BY status"
            ).fetchall()
        stats = {"pending": 0, "downloading": 0, "done": 0, "failed": 0}
        for r in rows:
            stats[r["status"]] = r["n"]
        return stats

    def latest_tweet_id(self) -> str | None:
        """watermark：最新入库的一条 tweet_id。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT tweet_id FROM tweets ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
        return row["tweet_id"] if row else None

    def count_tweets(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM tweets").fetchone()
        return row["n"]

    def start_run(self) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO sync_runs(started_at, status) VALUES (?, 'running')",
                (time.time(),),
            )
            self._conn.commit()
        return cur.lastrowid

    def finish_run(self, run_id: int, new_count: int,
                   status: str = "ok", error: str = "") -> None:
        with self._lock:
            self._conn.execute(
                """UPDATE sync_runs
                   SET finished_at=?, new_count=?, status=?, error=?
                   WHERE id=?""",
                (time.time(), new_count, status, error, run_id),
            )
            self._conn.commit()

    def consecutive_failures(self) -> int:
        with self._lock:
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
        with self._lock:
            self._conn.close()

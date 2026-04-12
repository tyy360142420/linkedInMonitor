"""
database.py
SQLite 数据层 — 统一管理 Publishers / Posts / StockMentions / Notes。
WAL 模式支持多线程并发读写。
"""

import sqlite3
import os
from contextlib import contextmanager
from typing import Dict, Generator, List, Optional

from paths import APP_DIR

DB_FILE: str = os.path.join(APP_DIR, "tracker.db")


@contextmanager
def _db() -> Generator[sqlite3.Connection, None, None]:
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ------------------------------------------------------------------ #
# 初始化
# ------------------------------------------------------------------ #

def init_db() -> None:
    with _db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS publishers (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            platform    TEXT NOT NULL,
            name        TEXT NOT NULL,
            handle      TEXT NOT NULL,
            profile_url TEXT DEFAULT '',
            created_at  TEXT DEFAULT (datetime('now','localtime')),
            UNIQUE(platform, handle)
        );

        CREATE TABLE IF NOT EXISTS posts (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            publisher_id INTEGER NOT NULL,
            platform     TEXT NOT NULL,
            post_id      TEXT NOT NULL UNIQUE,
            content      TEXT DEFAULT '',
            post_time    TEXT DEFAULT '',
            url          TEXT DEFAULT '',
            found_at     TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (publisher_id) REFERENCES publishers(id)
        );

        CREATE TABLE IF NOT EXISTS stock_mentions (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id      INTEGER NOT NULL,
            publisher_id INTEGER NOT NULL,
            ticker       TEXT NOT NULL,
            mentioned_at TEXT DEFAULT (datetime('now','localtime')),
            UNIQUE(post_id, ticker),
            FOREIGN KEY (post_id) REFERENCES posts(id),
            FOREIGN KEY (publisher_id) REFERENCES publishers(id)
        );

        CREATE TABLE IF NOT EXISTS notes (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker     TEXT NOT NULL,
            title      TEXT DEFAULT '',
            content    TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            updated_at TEXT DEFAULT (datetime('now','localtime'))
        );

        CREATE INDEX IF NOT EXISTS idx_sm_ticker     ON stock_mentions(ticker);
        CREATE INDEX IF NOT EXISTS idx_sm_publisher  ON stock_mentions(publisher_id);
        CREATE INDEX IF NOT EXISTS idx_posts_pub     ON posts(publisher_id);
        CREATE INDEX IF NOT EXISTS idx_notes_ticker  ON notes(ticker);
        """)


# ------------------------------------------------------------------ #
# Publishers
# ------------------------------------------------------------------ #

def upsert_publisher(platform: str, name: str, handle: str,
                     profile_url: str = "") -> int:
    with _db() as conn:
        conn.execute(
            """INSERT INTO publishers (platform, name, handle, profile_url)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(platform, handle) DO UPDATE SET
                 name        = excluded.name,
                 profile_url = excluded.profile_url""",
            (platform, name, handle, profile_url),
        )
        row = conn.execute(
            "SELECT id FROM publishers WHERE platform=? AND handle=?",
            (platform, handle),
        ).fetchone()
        return row["id"]


def get_publishers() -> List[Dict]:
    with _db() as conn:
        rows = conn.execute("""
            SELECT p.*,
                   COUNT(DISTINCT po.id)   AS post_count,
                   COUNT(DISTINCT sm.ticker) AS ticker_count
            FROM publishers p
            LEFT JOIN posts po ON po.publisher_id = p.id
            LEFT JOIN stock_mentions sm ON sm.publisher_id = p.id
            GROUP BY p.id
            ORDER BY p.platform, p.name
        """).fetchall()
        return [dict(r) for r in rows]


def get_publisher(pub_id: int) -> Optional[Dict]:
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM publishers WHERE id=?", (pub_id,)
        ).fetchone()
        return dict(row) if row else None


def delete_publisher(pub_id: int) -> None:
    with _db() as conn:
        conn.execute(
            "DELETE FROM stock_mentions WHERE publisher_id=?", (pub_id,)
        )
        conn.execute("DELETE FROM posts WHERE publisher_id=?", (pub_id,))
        conn.execute("DELETE FROM publishers WHERE id=?", (pub_id,))


# ------------------------------------------------------------------ #
# Posts
# ------------------------------------------------------------------ #

def insert_post(publisher_id: int, platform: str, post_id: str,
                content: str, post_time: str, url: str) -> Optional[int]:
    """插入新帖子。返回新行 id；重复时返回 None。"""
    try:
        with _db() as conn:
            cur = conn.execute(
                """INSERT OR IGNORE INTO posts
                   (publisher_id, platform, post_id, content, post_time, url)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (publisher_id, platform, post_id,
                 content or "", post_time or "", url or ""),
            )
            return cur.lastrowid if cur.rowcount > 0 else None
    except Exception:
        return None


def get_posts_for_publisher(publisher_id: int, limit: int = 50) -> List[Dict]:
    with _db() as conn:
        rows = conn.execute(
            """SELECT * FROM posts WHERE publisher_id=?
               ORDER BY found_at DESC LIMIT ?""",
            (publisher_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


# ------------------------------------------------------------------ #
# Stock Mentions
# ------------------------------------------------------------------ #

def insert_stock_mention(post_id: int, publisher_id: int,
                         ticker: str) -> None:
    try:
        with _db() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO stock_mentions
                   (post_id, publisher_id, ticker) VALUES (?, ?, ?)""",
                (post_id, publisher_id, ticker.upper()),
            )
    except Exception:
        pass


def get_stocks_for_publisher(publisher_id: int) -> List[Dict]:
    with _db() as conn:
        rows = conn.execute(
            """SELECT ticker,
                      COUNT(*)          AS mention_count,
                      MAX(mentioned_at) AS last_mentioned
               FROM stock_mentions WHERE publisher_id=?
               GROUP BY ticker ORDER BY mention_count DESC""",
            (publisher_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_recommendations_for_publisher(publisher_id: int) -> List[Dict]:
    """
    返回发布人层面的股票推荐基线：
    - ticker
    - mention_count
    - first_mentioned (首次提及时间，作为推荐起点)
    - last_mentioned
    """
    with _db() as conn:
        rows = conn.execute(
            """SELECT ticker,
                      COUNT(*)          AS mention_count,
                      MIN(mentioned_at) AS first_mentioned,
                      MAX(mentioned_at) AS last_mentioned
               FROM stock_mentions
               WHERE publisher_id=?
               GROUP BY ticker
               ORDER BY first_mentioned DESC""",
            (publisher_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_all_stocks() -> List[Dict]:
    with _db() as conn:
        rows = conn.execute("""
            SELECT sm.ticker,
                   COUNT(*)                    AS mention_count,
                   MAX(sm.mentioned_at)        AS last_mentioned,
                   GROUP_CONCAT(DISTINCT p.name ORDER BY p.name) AS publishers
            FROM stock_mentions sm
            JOIN publishers p ON p.id = sm.publisher_id
            GROUP BY sm.ticker
            ORDER BY mention_count DESC
        """).fetchall()
        return [dict(r) for r in rows]


def get_publishers_for_ticker(ticker: str) -> List[Dict]:
    with _db() as conn:
        rows = conn.execute("""
            SELECT p.id, p.name, p.platform, p.handle, p.profile_url,
                   COUNT(sm.id) AS mention_count,
                   MAX(sm.mentioned_at) AS last_mentioned
            FROM stock_mentions sm
            JOIN publishers p ON p.id = sm.publisher_id
            WHERE sm.ticker = ?
            GROUP BY p.id
            ORDER BY mention_count DESC
        """, (ticker.upper(),)).fetchall()
        return [dict(r) for r in rows]


def get_posts_for_ticker(ticker: str, limit: int = 30) -> List[Dict]:
    with _db() as conn:
        rows = conn.execute("""
            SELECT po.*, p.name AS publisher_name, p.platform
            FROM stock_mentions sm
            JOIN posts po ON po.id = sm.post_id
            JOIN publishers p ON p.id = po.publisher_id
            WHERE sm.ticker = ?
            ORDER BY sm.mentioned_at DESC
            LIMIT ?
        """, (ticker.upper(), limit)).fetchall()
        return [dict(r) for r in rows]


# ------------------------------------------------------------------ #
# Notes
# ------------------------------------------------------------------ #

def get_notes(ticker: Optional[str] = None) -> List[Dict]:
    with _db() as conn:
        if ticker:
            rows = conn.execute(
                "SELECT * FROM notes WHERE ticker=? ORDER BY updated_at DESC",
                (ticker.upper(),),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM notes ORDER BY updated_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]


def create_note(ticker: str, title: str, content: str) -> int:
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO notes (ticker, title, content) VALUES (?, ?, ?)",
            (ticker.upper(), title or "", content),
        )
        return cur.lastrowid


def update_note(note_id: int, title: str, content: str) -> None:
    with _db() as conn:
        conn.execute(
            """UPDATE notes SET title=?, content=?,
               updated_at=datetime('now','localtime')
               WHERE id=?""",
            (title or "", content, note_id),
        )


def delete_note(note_id: int) -> None:
    with _db() as conn:
        conn.execute("DELETE FROM notes WHERE id=?", (note_id,))

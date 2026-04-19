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

        CREATE TABLE IF NOT EXISTS twitter_account_stats (
            publisher_id    INTEGER PRIMARY KEY,
            follower_count  INTEGER DEFAULT 0,
            following_count INTEGER DEFAULT 0,
            updated_at      TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (publisher_id) REFERENCES publishers(id)
        );

        CREATE TABLE IF NOT EXISTS twitter_followings (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            publisher_id     INTEGER NOT NULL,
            following_handle TEXT NOT NULL,
            following_name   TEXT DEFAULT '',
            synced_at        TEXT DEFAULT (datetime('now','localtime')),
            UNIQUE(publisher_id, following_handle),
            FOREIGN KEY (publisher_id) REFERENCES publishers(id)
        );

        CREATE INDEX IF NOT EXISTS idx_tf_publisher ON twitter_followings(publisher_id);
        CREATE INDEX IF NOT EXISTS idx_tf_handle    ON twitter_followings(following_handle);
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


def get_first_mentioned_for_ticker(ticker: str) -> Optional[str]:
    """返回某股票在系统内的首次提及时间（本地时区字符串），不存在则返回 None。"""
    with _db() as conn:
        row = conn.execute(
            """SELECT MIN(mentioned_at) AS first_mentioned
               FROM stock_mentions
               WHERE ticker = ?""",
            (ticker.upper(),),
        ).fetchone()
        if not row:
            return None
        return row["first_mentioned"]


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


# ================================================================== #
# Twitter Following 同步
# ================================================================== #

def upsert_twitter_account_stats(
    publisher_id: int, follower_count: int, following_count: int
) -> None:
    with _db() as conn:
        conn.execute(
            """INSERT INTO twitter_account_stats
               (publisher_id, follower_count, following_count, updated_at)
               VALUES (?, ?, ?, datetime('now','localtime'))
               ON CONFLICT(publisher_id) DO UPDATE SET
                 follower_count  = excluded.follower_count,
                 following_count = excluded.following_count,
                 updated_at      = excluded.updated_at""",
            (publisher_id, follower_count, following_count),
        )


def get_twitter_account_stats(publisher_id: int) -> Optional[Dict]:
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM twitter_account_stats WHERE publisher_id=?",
            (publisher_id,),
        ).fetchone()
        return dict(row) if row else None


def replace_twitter_followings(publisher_id: int, followings: List[dict]) -> None:
    """整体替换某发布人的关注列表。"""
    with _db() as conn:
        conn.execute(
            "DELETE FROM twitter_followings WHERE publisher_id=?", (publisher_id,)
        )
        rows = []
        for f in followings:
            handle = str(f.get("handle", "")).strip().lstrip("@").lower()
            if not handle:
                continue
            rows.append((publisher_id, handle, f.get("name", "")))
        conn.executemany(
            """INSERT OR IGNORE INTO twitter_followings
               (publisher_id, following_handle, following_name)
               VALUES (?, ?, ?)""",
            rows,
        )


def get_common_followings_for_publishers(publisher_ids: List[int]) -> List[Dict]:
    """获取多个发布人共同关注的账户。"""
    if not publisher_ids:
        return []
    placeholders = ",".join("?" * len(publisher_ids))
    with _db() as conn:
        rows = conn.execute(
            f"""SELECT
                    LOWER(tf.following_handle) AS following_handle,
                    MAX(tf.following_name) AS following_name,
                    COUNT(DISTINCT tf.publisher_id) AS followed_by_count,
                    GROUP_CONCAT(DISTINCT p.handle) AS followed_by_handles
                FROM twitter_followings tf
                JOIN publishers p ON p.id = tf.publisher_id
                WHERE tf.publisher_id IN ({placeholders})
                GROUP BY LOWER(tf.following_handle)
                HAVING COUNT(DISTINCT tf.publisher_id) = ?
                ORDER BY LOWER(tf.following_handle)""",
            publisher_ids + [len(publisher_ids)],
        ).fetchall()
        return [dict(r) for r in rows]


def get_following_sync_status(publisher_ids: List[int]) -> List[Dict]:
    """获取各账户关注列表的同步状态。"""
    if not publisher_ids:
        return []
    placeholders = ",".join("?" * len(publisher_ids))
    with _db() as conn:
        rows = conn.execute(
            f"""SELECT
                    p.id AS publisher_id,
                    p.handle,
                    tas.follower_count,
                    tas.following_count,
                    tas.updated_at AS stats_updated_at,
                    COUNT(tf.id) AS synced_following_count,
                    MAX(tf.synced_at) AS last_synced_at
                FROM publishers p
                LEFT JOIN twitter_account_stats tas ON tas.publisher_id = p.id
                LEFT JOIN twitter_followings tf ON tf.publisher_id = p.id
                WHERE p.id IN ({placeholders})
                GROUP BY p.id""",
            publisher_ids,
        ).fetchall()
        return [dict(r) for r in rows]


# ================================================================== #
# Twitter 账户比较
# ================================================================== #

def get_common_stocks_for_publishers(publisher_ids: List[int]) -> List[Dict]:
    """
    获取多个发布人共同提及的股票。
    返回这些发布人都提及过的股票，按提及次数排序。
    """
    if not publisher_ids:
        return []
    
    placeholders = ','.join('?' * len(publisher_ids))
    with _db() as conn:
        rows = conn.execute(f"""
            SELECT 
                sm.ticker,
                COUNT(DISTINCT sm.publisher_id) AS publisher_count,
                COUNT(sm.id) AS total_mentions,
                MIN(sm.mentioned_at) AS first_mentioned,
                MAX(sm.mentioned_at) AS last_mentioned,
                GROUP_CONCAT(DISTINCT p.handle) AS mentioned_by
            FROM stock_mentions sm
            JOIN publishers p ON p.id = sm.publisher_id
            WHERE sm.publisher_id IN ({placeholders})
            GROUP BY sm.ticker
            HAVING COUNT(DISTINCT sm.publisher_id) = ?
            ORDER BY total_mentions DESC
        """, publisher_ids + [len(publisher_ids)]).fetchall()
        return [dict(r) for r in rows]


def get_all_stocks_for_publishers(publisher_ids: List[int]) -> List[Dict]:
    """
    获取多个发布人提及的所有股票及其在各账户中的提及情况。
    """
    if not publisher_ids:
        return []
    
    placeholders = ','.join('?' * len(publisher_ids))
    with _db() as conn:
        rows = conn.execute(f"""
            SELECT 
                sm.ticker,
                p.id AS publisher_id,
                p.handle,
                p.name,
                COUNT(sm.id) AS mention_count,
                MIN(sm.mentioned_at) AS first_mentioned,
                MAX(sm.mentioned_at) AS last_mentioned
            FROM stock_mentions sm
            JOIN publishers p ON p.id = sm.publisher_id
            WHERE sm.publisher_id IN ({placeholders})
            GROUP BY sm.ticker, p.id, p.handle, p.name
            ORDER BY sm.ticker, p.handle
        """, publisher_ids).fetchall()
        return [dict(r) for r in rows]


def get_common_mentioned_publishers_for_publishers(publisher_ids: List[int]) -> List[Dict]:
    """
    获取在这些发布人的推文内容中被提及/标记的其他发布人。
    通过在推文内容中查找 @handle 来识别。
    """
    if not publisher_ids:
        return []
    
    placeholders = ','.join('?' * len(publisher_ids))
    with _db() as conn:
        # 首先获取这些发布人的所有推文内容
        rows = conn.execute(f"""
            SELECT 
                p.id,
                p.handle,
                p.name,
                p.platform,
                COUNT(DISTINCT po.id) AS mention_count_in_posts
            FROM posts po
            JOIN publishers p ON p.id = po.publisher_id
            WHERE po.publisher_id IN ({placeholders})
            GROUP BY p.id
            ORDER BY p.handle
        """, publisher_ids).fetchall()
        return [dict(r) for r in rows]

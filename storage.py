"""
storage.py
持久化已抓取到的帖子 ID，防止重复发送通知。
"""

import json
import os
import logging
from typing import Set

logger = logging.getLogger(__name__)

_STORAGE_FILE = "seen_posts.json"


def load_seen_posts() -> Set[str]:
    """从磁盘加载已见过的帖子 ID 集合。"""
    if not os.path.exists(_STORAGE_FILE):
        return set()
    try:
        with open(_STORAGE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return set(data.get("post_ids", []))
    except (json.JSONDecodeError, IOError) as exc:
        logger.error("加载 seen_posts.json 失败: %s", exc)
        return set()


def save_seen_posts(post_ids: Set[str]) -> None:
    """将帖子 ID 集合写入磁盘。"""
    try:
        with open(_STORAGE_FILE, "w", encoding="utf-8") as f:
            json.dump({"post_ids": list(post_ids)}, f, ensure_ascii=False, indent=2)
    except IOError as exc:
        logger.error("保存 seen_posts.json 失败: %s", exc)

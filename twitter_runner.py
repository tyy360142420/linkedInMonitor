"""
twitter_runner.py
后台线程：定期抓取 Twitter/X 上被监控账号的最新推文，
提取股票代码并存入 SQLite 数据库。
"""

import logging
import os
import threading
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import config as cfg_module
import database as db
import stock_data
from paths import APP_DIR
from twitter_scraper import TwitterScraper

logger = logging.getLogger(__name__)


class TwitterRunner:
    """管理 Twitter 后台追踪线程。"""

    def __init__(self) -> None:
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._scraper: Optional[TwitterScraper] = None
        self._lock = threading.Lock()

        self.status: str = "stopped"
        self.status_message: str = "Twitter 追踪器未运行"
        self.next_check_at: Optional[datetime] = None
        self.last_check_at: Optional[datetime] = None
        self.tweets_found: List[Dict] = []
        self.last_error: str = ""
        self.cooldown_until: Optional[datetime] = None

        # Following 同步状态
        self._sync_lock = threading.Lock()
        self._sync_status: Dict[int, Dict] = {}  # publisher_id -> status dict

    # ------------------------------------------------------------------ #
    # 公开控制接口
    # ------------------------------------------------------------------ #

    def start(self) -> bool:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return False
            if self.cooldown_until and datetime.now() < self.cooldown_until:
                remaining = int((self.cooldown_until - datetime.now()).total_seconds() // 60) + 1
                self.last_error = f"Twitter 登录被风控限制，请约 {remaining} 分钟后重试。"
                self.status = "error"
                self.status_message = self.last_error
                return False
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run, daemon=True, name="twitter-tracker"
            )
            self._thread.start()
        return True

    def stop(self) -> None:
        self._stop_event.set()
        with self._lock:
            if self._scraper:
                try:
                    self._scraper.close()
                except Exception:
                    pass
                self._scraper = None
            thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=2)
        if thread and not thread.is_alive():
            self._thread = None
        self.status = "stopped"
        self.status_message = "Twitter 追踪器已停止"
        self.next_check_at = None

    def is_running(self) -> bool:
        return (
            self._thread is not None
            and self._thread.is_alive()
            and not self._stop_event.is_set()
        )

    def get_state(self) -> Dict:
        return {
            "running": self.is_running(),
            "status": self.status,
            "status_message": self.status_message,
            "next_check_at": (
                self.next_check_at.strftime("%Y-%m-%d %H:%M:%S")
                if self.next_check_at
                else None
            ),
            "last_check_at": (
                self.last_check_at.strftime("%Y-%m-%d %H:%M:%S")
                if self.last_check_at
                else None
            ),
            "tweets_found": self.tweets_found[-20:],
            "tweets_total": len(self.tweets_found),
            "last_error": self.last_error,
            "cooldown_until": (
                self.cooldown_until.strftime("%Y-%m-%d %H:%M:%S")
                if self.cooldown_until
                else None
            ),
        }

    # ------------------------------------------------------------------ #
    # 后台线程主循环
    # ------------------------------------------------------------------ #

    def _run(self) -> None:
        self.status = "starting"
        self.status_message = "正在初始化 Twitter 浏览器并登录 …"
        logger.info("Twitter 追踪器线程已启动")

        cfg = cfg_module.get_all()

        # 初始化浏览器并登录
        if not self._init_logged_in_scraper(cfg):
            return

        self.status = "running"
        logger.info("Twitter 登录成功")

        while not self._stop_event.is_set():
            accounts = self._get_twitter_accounts()  # 动态重读，支持途中增删
            if not accounts:
                cfg = cfg_module.get_all()
                interval = int(cfg.get("TWITTER_CHECK_INTERVAL_MINUTES", 60))
                self.next_check_at = datetime.now() + timedelta(minutes=interval)
                self.status = "running"
                self.status_message = "已登录，暂未配置监控账号；可先手动完成验证或稍后添加账号"
                self._wait_until_next_round(interval)
                continue

            n = len(accounts)
            self.status_message = f"已登录，正在监控 {n} 个账号"
            for idx, account in enumerate(accounts):
                if self._stop_event.is_set():
                    break
                if idx > 0:
                    # 账号之间等待，避免 X.com 连续请求风控
                    for _ in range(6):
                        if self._stop_event.is_set():
                            break
                        time.sleep(5)
                self._do_check(account)

            if self._stop_event.is_set():
                break

            cfg = cfg_module.get_all()
            interval = int(cfg.get("TWITTER_CHECK_INTERVAL_MINUTES", 60))
            self.next_check_at = datetime.now() + timedelta(minutes=interval)
            self.status = "running"
            self.status_message = f"等待下轮检查（{interval} 分钟后）"

            self._wait_until_next_round(interval)

        logger.info("Twitter 追踪器线程已退出")
        self.status = "stopped"
        self.status_message = "Twitter 追踪器已停止"
        self.next_check_at = None
        with self._lock:
            self._scraper = None
            self._thread = None

    def _init_logged_in_scraper(self, cfg: Dict) -> bool:
        """初始化 scraper 并完成登录。"""
        try:
            scraper = TwitterScraper(
                email=str(cfg.get("TWITTER_EMAIL", "")),
                username=str(cfg.get("TWITTER_USERNAME", "")),
                password=str(cfg.get("TWITTER_PASSWORD", "")),
                headless=bool(cfg.get("TWITTER_HEADLESS", False)),
                challenge_wait_minutes=int(
                    cfg.get("TWITTER_CHALLENGE_WAIT_MINUTES", 5)
                ),
                session_profile_dir=str(
                    cfg.get(
                        "TWITTER_SESSION_PROFILE_DIR",
                        os.path.join(APP_DIR, ".edge_profile_twitter"),
                    )
                ),
            )
            scraper._init_driver()
            if not scraper.login():
                specific_msg = scraper.last_login_error or "Twitter 登录失败，请检查账号密码或手动完成验证。"
                lowered = specific_msg.lower()
                if "399" in lowered or "could not log you in now" in lowered or "请稍后再试" in specific_msg:
                    self._set_cooldown_error(specific_msg, minutes=30)
                else:
                    self._set_error(specific_msg)
                scraper.close()
                return False
            with self._lock:
                self._scraper = scraper
            self.cooldown_until = None
            return True
        except Exception as exc:
            self._set_error(f"Twitter 浏览器启动失败: {exc}")
            return False

    def _wait_until_next_round(self, interval: int) -> None:
        """等待下一轮检查，同时响应停止信号。"""
        for _ in range(interval * 12):
            if self._stop_event.is_set():
                break
            time.sleep(5)

    # ------------------------------------------------------------------ #
    # 单账号检查
    # ------------------------------------------------------------------ #

    def _do_check(self, account: Dict) -> None:
        handle = account["handle"]
        publisher_id = account["id"]
        self.status = "checking"
        self.status_message = f"正在抓取 @{handle} …"
        self.last_check_at = datetime.now()
        logger.info("▶ 开始抓取 @%s", handle)

        lookback_days = int(cfg_module.get_all().get("TWITTER_LOOKBACK_DAYS", 30))
        logger.info("抓取 @%s，时间窗口：最近 %d 天", handle, lookback_days)
        try:
            tweets = self._scraper.get_recent_tweets(
                handle,
                lookback_days=lookback_days,
            )
        except Exception as exc:
            logger.error("抓取 @%s 失败: %s", handle, exc)
            self.last_error = f"抓取 @{handle} 失败: {exc}"
            return

        new_count = 0
        for tweet in tweets:
            tickers = stock_data.extract_tickers(tweet.content)
            if not tickers:
                continue  # 不含股票提及，跳过入库

            db_post_id = db.insert_post(
                publisher_id=publisher_id,
                platform="twitter",
                post_id=tweet.tweet_id,
                content=tweet.content,
                post_time=tweet.timestamp,
                url=tweet.url,
            )
            if db_post_id:  # 新推文
                new_count += 1
                for ticker in tickers:
                    db.insert_stock_mention(db_post_id, publisher_id, ticker)

                self.tweets_found.append(
                    {
                        "handle": handle,
                        "content": tweet.content[:200],
                        "timestamp": tweet.timestamp,
                        "url": tweet.url,
                        "found_at": datetime.now().strftime(
                            "%Y-%m-%d %H:%M:%S"
                        ),
                        "tickers": tickers,
                    }
                )

        self.tweets_found = self.tweets_found[-200:]
        logger.info("@%s: 共 %d 条，%d 条新推文", handle, len(tweets), new_count)

    def _get_twitter_accounts(self) -> List[Dict]:
        return [p for p in db.get_publishers() if p["platform"] == "twitter"]

    def _set_error(self, msg: str) -> None:
        self.status = "error"
        self.last_error = msg
        self.status_message = msg
        logger.error(msg)

    def _set_cooldown_error(self, reason: str, minutes: int) -> None:
        self.cooldown_until = datetime.now() + timedelta(minutes=minutes)
        msg = f"{reason} 已进入冷却期（{minutes} 分钟）。"
        self.status = "error"
        self.last_error = msg
        self.status_message = msg
        logger.error(msg)

    # ------------------------------------------------------------------ #
    # Following 同步
    # ------------------------------------------------------------------ #

    def get_sync_status(self, publisher_id: int) -> Dict:
        with self._sync_lock:
            return dict(self._sync_status.get(publisher_id, {"state": "idle"}))

    def start_following_sync(self, publisher_id: int, handle: str, min_followers: int = 20000) -> bool:
        """在后台线程中启动关注列表同步，返回是否成功启动（已在同步中返回 False）。"""
        with self._sync_lock:
            existing = self._sync_status.get(publisher_id, {})
            if existing.get("state") == "running":
                return False
            self._sync_status[publisher_id] = {
                "state": "running",
                "handle": handle,
                "message": "正在同步…",
                "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "follower_count": None,
                "synced_count": 0,
                "skipped": False,
            }

        thread = threading.Thread(
            target=self._do_following_sync,
            args=(publisher_id, handle, min_followers),
            daemon=True,
            name=f"tw-following-{handle}",
        )
        thread.start()
        return True

    def _do_following_sync(self, publisher_id: int, handle: str, min_followers: int) -> None:
        """后台线程执行体：抓取 following 列表并存库。"""
        scraper = None
        own_scraper = False

        def _update(state: str, **kwargs):
            with self._sync_lock:
                self._sync_status[publisher_id].update({"state": state, **kwargs})

        try:
            # 优先复用正在运行的 scraper
            with self._lock:
                scraper = self._scraper

            if scraper is None:
                cfg = cfg_module.get_all()
                scraper = TwitterScraper(
                    email=str(cfg.get("TWITTER_EMAIL", "")),
                    username=str(cfg.get("TWITTER_USERNAME", "")),
                    password=str(cfg.get("TWITTER_PASSWORD", "")),
                    headless=bool(cfg.get("TWITTER_HEADLESS", False)),
                    challenge_wait_minutes=int(cfg.get("TWITTER_CHALLENGE_WAIT_MINUTES", 5)),
                    session_profile_dir=str(
                        cfg.get("TWITTER_SESSION_PROFILE_DIR",
                                os.path.join(APP_DIR, ".edge_profile_twitter"))
                    ),
                )
                scraper._init_driver()
                if not scraper.login():
                    _update("error", message="Twitter 登录失败，无法同步")
                    scraper.close()
                    return
                own_scraper = True

            # 1. 获取账户统计（粉丝数）
            _update("running", message=f"正在获取 @{handle} 的粉丝数…")
            stats = scraper.get_account_stats(handle)
            follower_count = stats.get("follower_count", 0)
            following_count = stats.get("following_count", 0)
            db.upsert_twitter_account_stats(publisher_id, follower_count, following_count)

            if follower_count < min_followers:
                _update(
                    "done",
                    message=f"@{handle} 粉丝数 {follower_count:,} < {min_followers:,}，已跳过",
                    follower_count=follower_count,
                    skipped=True,
                    synced_count=0,
                )
                logger.info("@%s 粉丝数 %d < %d，跳过 following 抓取", handle, follower_count, min_followers)
                return

            # 2. 抓取 following 列表
            _update("running", message=f"@{handle} 粉丝数 {follower_count:,}，开始抓取关注列表…",
                    follower_count=follower_count)
            followings = scraper.get_following_list(handle, max_users=500)

            # 3. 存库
            db.replace_twitter_followings(publisher_id, followings)
            _update(
                "done",
                message=f"同步完成，共 {len(followings)} 人",
                follower_count=follower_count,
                synced_count=len(followings),
                skipped=False,
            )
            logger.info("@%s following 同步完成，共 %d 人", handle, len(followings))

        except Exception as exc:
            logger.error("@%s following 同步异常: %s", handle, exc)
            _update("error", message=f"同步失败: {exc}")
        finally:
            if own_scraper and scraper:
                try:
                    scraper.close()
                except Exception:
                    pass

"""
tracker_runner.py
在后台线程中运行 LinkedIn 追踪器，向 Web UI 暴露状态与控制接口。
"""

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import config as cfg_module
import database as db
import stock_data
from notifier import EmailNotifier
from scraper import LinkedInScraper
from storage import load_seen_posts, save_seen_posts

logger = logging.getLogger(__name__)


class TrackerRunner:
    """管理后台追踪线程的生命周期与可查询状态。"""

    def __init__(self) -> None:
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._scraper: Optional[LinkedInScraper] = None
        self._lock = threading.Lock()
        self._publisher_id: Optional[int] = None

        # 供 Web UI 读取的可观测状态
        self.status: str = "stopped"
        self.status_message: str = "追踪器未运行"
        self.next_check_at: Optional[datetime] = None
        self.last_check_at: Optional[datetime] = None
        self.posts_found: List[Dict] = []   # 最近发现的帖子（内存中保留最近 200 条）
        self.last_error: str = ""

    # ------------------------------------------------------------------ #
    # 公开控制接口
    # ------------------------------------------------------------------ #

    def start(self) -> bool:
        """启动后台线程；已在运行时返回 False。"""
        with self._lock:
            if self._thread and self._thread.is_alive():
                return False
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run, daemon=True, name="linkedin-tracker"
            )
            self._thread.start()
        return True

    def stop(self) -> None:
        """发出停止信号并关闭浏览器。"""
        self._stop_event.set()
        with self._lock:
            if self._scraper:
                try:
                    self._scraper.close()
                except Exception:
                    pass
                self._scraper = None
        self.status = "stopped"
        self.status_message = "追踪器已停止"
        self.next_check_at = None

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def get_state(self) -> Dict:
        """返回当前状态的可序列化快照。"""
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
            "posts_found": self.posts_found[-20:],
            "posts_total": len(self.posts_found),
            "last_error": self.last_error,
        }

    # ------------------------------------------------------------------ #
    # 后台线程主循环
    # ------------------------------------------------------------------ #

    def _run(self) -> None:
        self.status = "starting"
        self.status_message = "正在启动浏览器并登录 LinkedIn …"
        logger.info("追踪器线程已启动")

        cfg = cfg_module.get_all()

        # ---- 初始化浏览器并登录（整个运行周期只做一次）----
        try:
            scraper = LinkedInScraper(
                email=str(cfg["LINKEDIN_EMAIL"]),
                password=str(cfg["LINKEDIN_PASSWORD"]),
                headless=bool(cfg["HEADLESS"]),
                challenge_wait_minutes=int(cfg["SECURITY_CHALLENGE_WAIT_MINUTES"]),
                session_profile_dir=str(cfg["SESSION_PROFILE_DIR"]),
            )
            scraper._init_driver()
            if not scraper.login():
                self._set_error("登录失败，请检查账号密码或手动完成人机验证后重试。")
                return
            with self._lock:
                self._scraper = scraper
        except Exception as exc:
            self._set_error(f"浏览器启动失败: {exc}")
            return

        logger.info("登录成功，开始监控循环")

        # 在数据库中注册 LinkedIn 发布人
        profile_url = str(cfg["TARGET_PROFILE_URL"])
        handle = profile_url.rstrip("/").split("/")[-1] or "linkedin-target"
        name = str(cfg.get("TARGET_PROFILE_NAME") or handle)
        self._publisher_id = db.upsert_publisher(
            "linkedin", name, handle, profile_url
        )
        logger.info("LinkedIn 发布人已注册: %s (DB id=%s)", name, self._publisher_id)

        self.status = "running"
        self.status_message = "已登录，正在监控中"

        interval = int(cfg["CHECK_INTERVAL_MINUTES"])

        while not self._stop_event.is_set():
            self._do_check(cfg)

            if self._stop_event.is_set():
                break

            next_time = datetime.now() + timedelta(minutes=interval)
            self.next_check_at = next_time
            self.status = "running"
            self.status_message = f"等待下轮检查（{interval} 分钟后）"

            # 每 5 秒检查一次停止信号，避免长时间阻塞
            for _ in range(interval * 12):
                if self._stop_event.is_set():
                    break
                time.sleep(5)

        logger.info("追踪器线程已退出")
        self.status = "stopped"
        self.status_message = "追踪器已停止"
        self.next_check_at = None

    def _do_check(self, cfg: Dict) -> None:
        """执行一次抓取检查，发现新帖子时发送邮件通知。"""
        self.status = "checking"
        self.status_message = "正在抓取最新动态 …"
        self.last_check_at = datetime.now()
        logger.info("▶ 开始检查新动态")

        try:
            posts = self._scraper.get_recent_posts(str(cfg["TARGET_PROFILE_URL"]))
        except Exception as exc:
            logger.error("抓取失败: %s", exc)
            self.last_error = f"抓取失败: {exc}"
            return

        if not posts:
            logger.info("未获取到帖子（页面可能正在加载或隐私限制）")
            return

        seen_ids = load_seen_posts()

        # ---- 首次运行：仅标记，不发通知 ----
        if not seen_ids:
            logger.info("首次运行，将 %d 条帖子标记为已知，不发送通知", len(posts))
            save_seen_posts({p.post_id for p in posts})
            self._record_posts(posts, is_new=False)
            return

        new_posts = [p for p in posts if p.post_id not in seen_ids]

        if not new_posts:
            logger.info("✔ 无新动态")
            return

        logger.info("★ 发现 %d 条新帖子，准备发送邮件通知", len(new_posts))
        self._record_posts(new_posts, is_new=True)

        notifier = EmailNotifier(
            sender=str(cfg["NOTIFY_EMAIL_SENDER"]),
            app_password=str(cfg["NOTIFY_EMAIL_APP_PASSWORD"]),
            recipient=str(cfg["NOTIFY_EMAIL_RECIPIENT"]),
        )

        if notifier.send_notification(new_posts, str(cfg["TARGET_PROFILE_URL"])):
            seen_ids.update(p.post_id for p in new_posts)
            save_seen_posts(seen_ids)
        else:
            self.last_error = "邮件发送失败，将在下次检查时重试"

    def _record_posts(self, posts, is_new: bool) -> None:
        """将帖子存入数据库并追加到内存列表，最多保留最近 200 条。"""
        for p in posts:
            # 持久化到数据库
            if self._publisher_id is not None:
                db_post_id = db.insert_post(
                    publisher_id=self._publisher_id,
                    platform="linkedin",
                    post_id=p.post_id,
                    content=p.content or "",
                    post_time=p.timestamp or "",
                    url=p.url or "",
                )
                if db_post_id:
                    tickers = stock_data.extract_tickers(p.content or "")
                    for ticker in tickers:
                        db.insert_stock_mention(
                            db_post_id, self._publisher_id, ticker
                        )

            # 内存缓存（供仪表盘实时展示）
            self.posts_found.append(
                {
                    "post_id": p.post_id,
                    "content": (p.content[:300] if p.content else "（无正文）"),
                    "timestamp": p.timestamp or "",
                    "url": p.url or "",
                    "found_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "is_new": is_new,
                }
            )
        self.posts_found = self.posts_found[-200:]

    def _set_error(self, message: str) -> None:
        self.status = "error"
        self.last_error = message
        self.status_message = message
        logger.error(message)

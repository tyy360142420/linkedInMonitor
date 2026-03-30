"""
main.py
LinkedIn 动态追踪程序入口。

策略：
- 程序启动时登录一次，浏览器整个生命周期内不再重新登录。
- 每隔 CHECK_INTERVAL_MINUTES 分钟仅做页面抓取。
- 遇到人机验证：等待用户手动通过，验证期间跳过本轮抓取，
  下一轮直接复用已登录会话继续抓取，不重新登录。
- 仅当浏览器进程真正崩溃（InvalidSession）时才重启并重新登录。
"""

import logging
import os
import time
from typing import Optional

import schedule

import config
from notifier import EmailNotifier
from paths import APP_DIR
from scraper import LinkedInScraper
from storage import load_seen_posts, save_seen_posts

# ------------------------------------------------------------------ #
# 日志配置
# ------------------------------------------------------------------ #
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(APP_DIR, "tracker.log"), encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

# 全局唯一 scraper（整个进程生命周期只初始化一次）
_scraper: Optional[LinkedInScraper] = None


def _browser_alive() -> bool:
    """探测浏览器进程是否仍在运行。"""
    if _scraper is None:
        return False
    try:
        _ = _scraper._driver.current_url
        return True
    except Exception:
        return False


def _init_scraper_once() -> bool:
    """
    启动浏览器并登录，整个进程只调用一次。
    登录失败（含验证超时）时返回 False，程序退出。
    """
    global _scraper
    s = LinkedInScraper(
        email=config.LINKEDIN_EMAIL,
        password=config.LINKEDIN_PASSWORD,
        headless=config.HEADLESS,
        challenge_wait_minutes=config.SECURITY_CHALLENGE_WAIT_MINUTES,
        session_profile_dir=config.SESSION_PROFILE_DIR,
    )
    s._init_driver()
    if not s.login():
        s.close()
        logger.error("启动登录失败，程序退出。请检查账号或手动完成验证后重新运行。")
        return False
    _scraper = s
    logger.info("登录成功，浏览器将持续保持，不再重复登录")
    return True


# ------------------------------------------------------------------ #
# 核心检查逻辑
# ------------------------------------------------------------------ #

def check_for_new_posts() -> None:
    """仅做抓取 + 通知，不涉及任何登录操作。"""
    logger.info("▶ 开始检查新动态 …")

    # 仅在浏览器进程真正崩溃时才重启（极少发生）
    if not _browser_alive():
        logger.warning("浏览器进程已崩溃，正在重启并重新登录一次 …")
        global _scraper
        if _scraper:
            try:
                _scraper.close()
            except Exception:
                pass
            _scraper = None
        if not _init_scraper_once():
            logger.error("重启登录失败，跳过本轮检查")
            return

    seen_ids = load_seen_posts()

    try:
        posts = _scraper.get_recent_posts(config.TARGET_PROFILE_URL)
    except Exception as exc:
        logger.error("抓取失败: %s — 浏览器保持运行，下轮重试", exc)
        return

    if not posts:
        logger.info("未获取到帖子（页面可能正在加载或对方设了隐私）")
        return

    # ---- 首次运行：将现有帖子全部标记为"已见"，不发通知 ----
    if not seen_ids:
        logger.info("首次运行，将当前 %d 条帖子标记为已知，不发送通知", len(posts))
        save_seen_posts({p.post_id for p in posts})
        return

    # ---- 找出新帖子 ----
    new_posts = [p for p in posts if p.post_id not in seen_ids]

    if not new_posts:
        logger.info("✔ 无新动态")
        return

    logger.info("★ 发现 %d 条新帖子，准备发送邮件通知 …", len(new_posts))

    notifier = EmailNotifier(
        sender=config.NOTIFY_EMAIL_SENDER,
        app_password=config.NOTIFY_EMAIL_APP_PASSWORD,
        recipient=config.NOTIFY_EMAIL_RECIPIENT,
    )

    if notifier.send_notification(new_posts, config.TARGET_PROFILE_URL):
        seen_ids.update(p.post_id for p in new_posts)
        save_seen_posts(seen_ids)
    else:
        logger.warning("邮件发送失败，将在下次检查时重试")


# ------------------------------------------------------------------ #
# 程序入口
# ------------------------------------------------------------------ #

def main() -> None:
    try:
        config.validate()
    except ValueError as exc:
        logger.error("%s", exc)
        return

    logger.info("=" * 60)
    logger.info("LinkedIn 动态追踪器已启动")
    logger.info("监控目标: %s", config.TARGET_PROFILE_URL)
    logger.info("检查间隔: %d 分钟", config.CHECK_INTERVAL_MINUTES)
    logger.info("通知邮箱: %s", config.NOTIFY_EMAIL_RECIPIENT)
    logger.info("=" * 60)

    # 启动时登录唯一一次，失败则退出
    if not _init_scraper_once():
        return

    # 首次立即检查
    check_for_new_posts()

    schedule.every(config.CHECK_INTERVAL_MINUTES).minutes.do(check_for_new_posts)
    logger.info("定时任务已设置，按 Ctrl+C 停止程序")

    try:
        while True:
            schedule.run_pending()
            time.sleep(30)
    except KeyboardInterrupt:
        logger.info("程序已手动停止")
    finally:
        if _scraper is not None:
            _scraper.close()
            logger.info("浏览器已关闭")


if __name__ == "__main__":
    main()

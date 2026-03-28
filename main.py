"""
main.py
LinkedIn 动态追踪程序入口。
浏览器启动后保持常驻，每隔指定分钟只做页面抓取，不重复登录。
仅在浏览器崩溃时自动重新初始化。
"""

import logging
import time
from typing import Optional

import schedule

import config
from notifier import EmailNotifier
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
        logging.FileHandler("tracker.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

# 全局常驻 scraper 实例
_scraper: Optional[LinkedInScraper] = None


def _get_scraper() -> Optional[LinkedInScraper]:
    """返回已登录的 scraper，必要时重新初始化并登录。"""
    global _scraper
    # 检测浏览器是否还活着
    if _scraper is not None:
        try:
            _ = _scraper._driver.current_url  # 访问任意属性探活
        except Exception:
            logger.warning("浏览器会话已断开，正在重新初始化 …")
            _scraper.close()
            _scraper = None

    if _scraper is None:
        s = LinkedInScraper(
            email=config.LINKEDIN_EMAIL,
            password=config.LINKEDIN_PASSWORD,
            headless=config.HEADLESS,
            challenge_wait_minutes=config.SECURITY_CHALLENGE_WAIT_MINUTES,
            session_profile_dir=config.SESSION_PROFILE_DIR,
        )
        s._init_driver()
        if not s.login():
            logger.error("登录失败，本次检查跳过")
            s.close()
            return None
        _scraper = s
        logger.info("浏览器已启动并登录，后续检查将复用此会话")

    return _scraper


# ------------------------------------------------------------------ #
# 核心检查逻辑
# ------------------------------------------------------------------ #

def check_for_new_posts() -> None:
    """复用浏览器会话抓取帖子，若有新内容则发邮件通知。"""
    logger.info("▶ 开始检查新动态 …")

    seen_ids = load_seen_posts()

    scraper = _get_scraper()
    if scraper is None:
        return

    try:
        posts = scraper.get_recent_posts(config.TARGET_PROFILE_URL)
    except Exception as exc:
        logger.error("抓取过程出现未预期错误: %s", exc, exc_info=True)
        # 标记 scraper 为失效，下次重建
        global _scraper
        _scraper = None
        return

    if not posts:
        logger.info("未获取到任何帖子（可能受隐私设置限制）")
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
        # 仅在邮件发送成功后才更新已见列表，防止漏通知
        seen_ids.update(p.post_id for p in new_posts)
        save_seen_posts(seen_ids)
    else:
        logger.warning("邮件发送失败，将在下次检查时重试")


# ------------------------------------------------------------------ #
# 程序入口
# ------------------------------------------------------------------ #

def main() -> None:
    # 校验配置
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

    # 启动后立即执行一次
    check_for_new_posts()

    # 定时任务
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

"""
scraper.py
使用 Microsoft Edge 登录 LinkedIn 并抓取指定用户的最新动态。
"""

import hashlib
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional

from selenium import webdriver
from selenium.common.exceptions import (
    NoSuchElementException,
    NoSuchWindowException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.edge.options import Options as EdgeOptions
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

logger = logging.getLogger(__name__)

_EDGE_EXE = "msedge.exe"
_RELATIVE_MINUTE_UNITS = {"min", "mins", "minute", "minutes", "m", "分钟", "分"}
_RELATIVE_HOUR_UNITS = {"hour", "hours", "hr", "hrs", "h", "小时", "时"}
_RELATIVE_DAY_UNITS = {"day", "days", "d", "天"}
_RELATIVE_WEEK_UNITS = {"wk", "wks", "week", "weeks", "w", "周"}
_RELATIVE_MONTH_UNITS = {"mo", "mon", "mons", "month", "months", "个月", "月"}
_RELATIVE_YEAR_UNITS = {"yr", "yrs", "year", "years", "y"}

# LinkedIn CSS 选择器（多个备用，应对页面结构变化）
_POST_CONTAINERS = [
    ".feed-shared-update-v2",
    ".occludable-update",
    "[data-urn]",
]

_CONTENT_SELECTORS = [
    ".feed-shared-update-v2__description .break-words",
    ".update-components-text .break-words",
    ".feed-shared-text span[dir]",
    ".break-words",
]

_TIMESTAMP_SELECTORS = [
    "time",
    ".update-components-actor__sub-description",
    ".feed-shared-actor__sub-description",
    ".feed-shared-actor__sub-description span[aria-hidden='true']",
    ".update-components-actor__meta-link span[aria-hidden='true']",
]

_POST_LINK_SELECTORS = [
    "a[href*='/posts/']",
    "a[href*='/feed/update/']",
    "a[href*='activity']",
]


@dataclass
class Post:
    post_id: str
    content: str
    timestamp: str
    url: str
    raw_urn: str = field(default="")


class LinkedInScraper:
    """使用 Selenium 登录 LinkedIn 并抓取用户最新帖子。"""

    _LOGIN_URL = "https://www.linkedin.com/login"

    def __init__(
        self,
        email: str,
        password: str,
        headless: bool = False,
        challenge_wait_minutes: int = 5,
        session_profile_dir: str = ".edge_profile",
    ) -> None:
        self.email = email
        self.password = password
        self.headless = headless
        self.challenge_wait_minutes = challenge_wait_minutes
        self.session_profile_dir = session_profile_dir
        self._driver: Optional[Any] = None
        self._wait: Optional[WebDriverWait] = None
        self._primary_window_handle: Optional[str] = None

    # ------------------------------------------------------------------ #
    # Context manager
    # ------------------------------------------------------------------ #

    def __enter__(self) -> "LinkedInScraper":
        self._init_driver()
        return self

    def __exit__(self, *_) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def login(self) -> bool:
        """登录 LinkedIn，成功返回 True，失败返回 False。"""
        logger.info("正在登录 LinkedIn …")
        for attempt in (1, 2):
            try:
                return self._login_once()
            except Exception as exc:
                if self._handle_login_exception(exc, attempt):
                    continue
                return False

        return False

    def _handle_login_exception(self, exc: Exception, attempt: int) -> bool:
        if isinstance(exc, NoSuchWindowException):
            if attempt == 1:
                logger.warning("浏览器窗口意外关闭，正在重启并重试登录一次")
                self.close()
                self._init_driver()
                return True
            logger.error("浏览器窗口连续关闭，登录失败")
            return False

        if isinstance(exc, TimeoutException):
            logger.error("登录超时，请检查网络或账号信息")
            return False

        if isinstance(exc, WebDriverException):
            if self._is_detached_frame_error(exc) and attempt == 1:
                logger.warning("登录页标签失去连接，正在恢复浏览器后重试一次")
                self._recover_primary_window()
                return True
            logger.error("登录过程发生浏览器错误: %s", exc, exc_info=True)
            return False

        logger.error("登录过程发生意外错误: %s", exc, exc_info=True)
        return False

    def _login_once(self) -> bool:
        self._navigate(self._LOGIN_URL)
        time.sleep(2)

        if self._is_logged_in_url(self._driver.current_url):
            logger.info("检测到已存在登录会话，跳过账号密码输入")
            return True

        email_field = self._wait_for_login_form_or_redirect()
        if email_field == "already_logged_in":
            logger.info("检测到已存在登录会话，跳过账号密码输入")
            return True

        self._submit_login_credentials(email_field)
        return self._complete_login_after_submit()

    def _wait_for_login_form_or_redirect(self):
        def _login_form_or_redirect(driver):
            if self._is_logged_in_url(driver.current_url):
                return "already_logged_in"
            els = driver.find_elements(By.ID, "username")
            return els[0] if els else False

        return self._wait.until(_login_form_or_redirect)

    def _submit_login_credentials(self, email_field) -> None:
        email_field.clear()
        email_field.send_keys(self.email)

        pwd_field = self._driver.find_element(By.ID, "password")
        pwd_field.clear()
        pwd_field.send_keys(self.password)

        submit = self._driver.find_element(By.CSS_SELECTOR, 'button[type="submit"]')
        submit.click()

    def _complete_login_after_submit(self) -> bool:
        time.sleep(5)
        url = self._driver.current_url

        if self._is_challenge_url(url):
            if not self._wait_for_challenge_resolution():
                logger.error("安全验证未在规定时间内完成")
                return False
            url = self._driver.current_url

        if self._is_logged_in_url(url):
            logger.info("登录成功")
            return True

        logger.error("登录失败，当前页面: %s", url)
        return False

    def get_recent_posts(self, profile_url: str, lookback_days: int = 30) -> List[Post]:
        """
        抓取指定用户主页在最近时间窗口内的帖子。
        注意：可能受到对方隐私设置限制。
        """
        base = profile_url.rstrip("/")
        activity_url = f"{base}/recent-activity/all/"
        logger.info("访问用户动态页: %s", activity_url)

        try:
            self._navigate(activity_url)
            time.sleep(4)

            # 多滚动几轮，尽量覆盖整个时间窗口，而不是只拿首屏几条。
            for _ in range(5):
                self._driver.execute_script("window.scrollBy(0, 1400)")
                time.sleep(1.5)

            posts = self._extract_posts(lookback_days=lookback_days)
            logger.info("共抓取到 %d 条最近 %d 天帖子", len(posts), lookback_days)
            return posts

        except Exception as exc:
            logger.error("抓取帖子时发生错误: %s", exc, exc_info=True)
            return []

    def close(self) -> None:
        if self._driver:
            try:
                self._driver.quit()
            except Exception:
                pass
            logger.info("浏览器已关闭")

    # ------------------------------------------------------------------ #
    # Private helpers
    # ------------------------------------------------------------------ #

    def _init_driver(self) -> None:
        options = EdgeOptions()
        if self.headless:
            options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-session-crashed-bubble")
        options.add_argument("--no-first-run")
        options.add_argument("--no-default-browser-check")
        options.add_argument("--window-size=1920,1080")
        options.add_argument("--lang=zh-CN")
        # 禁用 GPU 硬件加速，防止 AMD/GPU 驱动错误导致渲染进程崩溃断连
        options.add_argument("--disable-gpu")
        options.add_argument("--disable-gpu-compositing")
        options.add_argument("--disable-software-rasterizer")
        options.add_argument("--disable-features=VizDisplayCompositor")

        edge_binary = self._detect_edge_binary()
        if edge_binary:
            options.binary_location = edge_binary

        profile_path = os.path.abspath(self.session_profile_dir)
        os.makedirs(profile_path, exist_ok=True)
        options.add_argument(f"--user-data-dir={profile_path}")
        options.add_argument("--profile-directory=Default")

        self._driver = webdriver.Edge(options=options)
        self._wait = WebDriverWait(self._driver, 30)
        self._prepare_primary_window()
        logger.info(
            "Edge 浏览器已启动（无头模式: %s，会话目录: %s）",
            self.headless,
            profile_path,
        )

    def _prepare_primary_window(self) -> None:
        """创建一个专用标签页，并关闭恢复出的无关页面。"""
        current_handles = self._driver.window_handles
        if not current_handles:
            raise RuntimeError("Edge 未创建可用窗口")

        self._driver.switch_to.new_window("tab")
        self._driver.get("about:blank")
        self._primary_window_handle = self._driver.current_window_handle
        self._close_extra_windows(self._primary_window_handle)

    def _close_extra_windows(self, keep_handle: str) -> None:
        for handle in self._driver.window_handles:
            if handle == keep_handle:
                continue
            try:
                self._driver.switch_to.window(handle)
                self._driver.close()
            except Exception:
                continue
        self._driver.switch_to.window(keep_handle)

    def _ensure_primary_window(self) -> None:
        handles = self._driver.window_handles
        if not handles:
            raise NoSuchWindowException("没有可用浏览器窗口")

        if self._primary_window_handle in handles:
            self._driver.switch_to.window(self._primary_window_handle)
            return

        self._primary_window_handle = handles[-1]
        self._driver.switch_to.window(self._primary_window_handle)

    def _recover_primary_window(self) -> None:
        self._ensure_primary_window()
        try:
            self._driver.switch_to.new_window("tab")
            self._driver.get("about:blank")
            self._primary_window_handle = self._driver.current_window_handle
            self._close_extra_windows(self._primary_window_handle)
        except Exception:
            self._ensure_primary_window()

    def _navigate(self, url: str) -> None:
        for attempt in (1, 2):
            try:
                self._ensure_primary_window()
                self._driver.get(url)
                return
            except WebDriverException as exc:
                if self._is_detached_frame_error(exc) and attempt == 1:
                    logger.warning("检测到标签页上下文丢失，正在恢复并重试: %s", url)
                    self._recover_primary_window()
                    continue
                raise

    @staticmethod
    def _is_detached_frame_error(exc: Exception) -> bool:
        return "target frame detached" in str(exc).lower()

    def _wait_for_challenge_resolution(self) -> bool:
        """检测到安全挑战后，等待用户手动完成验证。"""
        wait_seconds = max(self.challenge_wait_minutes, 1) * 60
        logger.warning(
            "检测到 LinkedIn 安全验证，请在浏览器中手动完成（最多等待 %d 分钟）",
            max(self.challenge_wait_minutes, 1),
        )
        start = time.time()
        while time.time() - start < wait_seconds:
            try:
                url = self._driver.current_url
            except Exception:
                time.sleep(2)
                continue

            if self._is_logged_in_url(url):
                logger.info("安全验证已完成，继续执行")
                return True

            if not self._is_challenge_url(url) and "login" not in url:
                # 页面离开 challenge 区域，也按通过处理，后续逻辑继续判断。
                logger.info("检测到页面已离开安全验证页面，继续执行")
                return True

            time.sleep(2)

        return False

    @staticmethod
    def _is_logged_in_url(url: str) -> bool:
        url_lower = (url or "").lower()
        if "linkedin.com" not in url_lower:
            return False
        if any(kw in url_lower for kw in ("/login", "checkpoint", "challenge", "captcha", "two-step")):
            return False
        return True

    @staticmethod
    def _is_challenge_url(url: str) -> bool:
        return any(kw in url for kw in ("checkpoint", "challenge", "captcha", "two-step"))

    @staticmethod
    def _detect_edge_binary() -> Optional[str]:
        """返回可用的 msedge 可执行文件路径，找不到时返回 None（Selenium 自动查找）。"""
        candidates = [
            os.environ.get("EDGE_BINARY"),
            shutil.which("msedge"),
            shutil.which(_EDGE_EXE),
            os.path.join(
                os.environ.get("PROGRAMW6432", ""),
                "Microsoft", "Edge", "Application", _EDGE_EXE,
            ),
            os.path.join(
                os.environ.get("PROGRAMFILES", ""),
                "Microsoft", "Edge", "Application", _EDGE_EXE,
            ),
            os.path.join(
                os.environ.get("PROGRAMFILES(X86)", ""),
                "Microsoft", "Edge", "Application", _EDGE_EXE,
            ),
            os.path.join(
                os.environ.get("LOCALAPPDATA", ""),
                "Microsoft", "Edge", "Application", _EDGE_EXE,
            ),
        ]
        for path in candidates:
            if path and os.path.isfile(path):
                return path
        return None

    def _extract_posts(self, lookback_days: int = 30, max_candidates: int = 80) -> List[Post]:
        """在当前页面中查找并解析帖子元素。"""
        posts: List[Post] = []
        cutoff = datetime.now(timezone.utc) - timedelta(days=max(lookback_days, 1))
        seen_ids = set()

        # 尝试各备用选择器，找到第一个有结果的
        container_selector = None
        for sel in _POST_CONTAINERS:
            elems = self._driver.find_elements(By.CSS_SELECTOR, sel)
            if elems:
                container_selector = sel
                break

        if container_selector is None:
            logger.warning("页面上未找到帖子容器，可能该用户设置了隐私或页面结构已变化")
            return posts

        elements = self._driver.find_elements(By.CSS_SELECTOR, container_selector)
        for elem in elements[:max_candidates]:
            try:
                post = self._parse_element(elem)
                if not post or post.post_id in seen_ids:
                    continue
                parsed_at = self._parse_timestamp(post.timestamp)
                if parsed_at and parsed_at < cutoff:
                    continue
                seen_ids.add(post.post_id)
                posts.append(post)
            except Exception as exc:
                logger.debug("解析帖子元素异常: %s", exc)

        return posts

    @staticmethod
    def _parse_timestamp(raw_value: str) -> Optional[datetime]:
        raw = (raw_value or "").strip()
        if not raw:
            return None

        normalized = raw.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            pass

        lowered = raw.lower().replace("ago", "").replace("·", " ").strip()
        compact = lowered.replace(" ", "")
        now = datetime.now(timezone.utc)

        split_index = LinkedInScraper._first_non_digit_index(compact)
        if split_index <= 0:
            return None

        num = int(compact[:split_index])
        unit = compact[split_index:]
        if unit in _RELATIVE_MINUTE_UNITS:
            return now - timedelta(minutes=num)
        if unit in _RELATIVE_HOUR_UNITS:
            return now - timedelta(hours=num)
        if unit in _RELATIVE_DAY_UNITS:
            return now - timedelta(days=num)
        if unit in _RELATIVE_WEEK_UNITS:
            return now - timedelta(weeks=num)
        if unit in _RELATIVE_MONTH_UNITS:
            return now - timedelta(days=30 * num)
        if unit in _RELATIVE_YEAR_UNITS:
            return now - timedelta(days=365 * num)
        return None

    @staticmethod
    def _first_non_digit_index(value: str) -> int:
        for index, char in enumerate(value):
            if not char.isdigit():
                return index
        return -1

    def _parse_element(self, elem) -> Optional[Post]:
        """从单个帖子 DOM 元素中提取数据。"""
        urn = elem.get_attribute("data-urn") or elem.get_attribute("data-id") or ""
        content = self._find_text(elem, _CONTENT_SELECTORS)
        timestamp = self._extract_timestamp_from_element(elem)
        post_url = self._extract_post_url(elem, urn)

        if not urn and not content:
            return None

        post_id = self._build_post_id(urn, content)

        return Post(
            post_id=post_id,
            content=content[:600],
            timestamp=timestamp,
            url=post_url,
            raw_urn=urn,
        )

    def _extract_timestamp_from_element(self, elem) -> str:
        for sel in _TIMESTAMP_SELECTORS:
            try:
                timestamp = self._normalize_timestamp_text(
                    elem.find_element(By.CSS_SELECTOR, sel)
                )
                if timestamp:
                    return timestamp
            except NoSuchElementException:
                continue
        return ""

    @staticmethod
    def _normalize_timestamp_text(element) -> str:
        raw_ts = element.get_attribute("datetime") or ""
        if raw_ts:
            return raw_ts

        for line in element.text.split("\n"):
            line = line.strip()
            if line and len(line) < 80:
                return line
        return ""

    def _extract_post_url(self, elem, urn: str) -> str:
        for sel in _POST_LINK_SELECTORS:
            try:
                href = elem.find_element(By.CSS_SELECTOR, sel).get_attribute("href") or ""
                if href:
                    return href
            except NoSuchElementException:
                continue

        if not urn:
            return ""

        import urllib.parse
        return f"https://www.linkedin.com/feed/update/{urllib.parse.quote(urn, safe=':')}/"

    @staticmethod
    def _build_post_id(urn: str, content: str) -> str:
        base = urn if urn else content
        return hashlib.sha256(base.encode()).hexdigest()[:20]

    @staticmethod
    def _find_text(parent, selectors: List[str]) -> str:
        for sel in selectors:
            try:
                elem = parent.find_element(By.CSS_SELECTOR, sel)
                text = elem.text.strip()
                if text:
                    return text
            except NoSuchElementException:
                continue
        return ""

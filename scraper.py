"""
scraper.py
使用 Microsoft Edge 登录 LinkedIn 并抓取指定用户的最新动态。
"""

import hashlib
import logging
import os
import shutil
import time
from dataclasses import dataclass, field
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
                self._navigate(self._LOGIN_URL)
                time.sleep(2)

                if self._is_logged_in_url(self._driver.current_url):
                    logger.info("检测到已存在登录会话，跳过账号密码输入")
                    return True

                wait = self._wait

                email_field = wait.until(
                    EC.presence_of_element_located((By.ID, "username"))
                )
                email_field.clear()
                email_field.send_keys(self.email)

                pwd_field = self._driver.find_element(By.ID, "password")
                pwd_field.clear()
                pwd_field.send_keys(self.password)

                submit = self._driver.find_element(By.CSS_SELECTOR, 'button[type="submit"]')
                submit.click()

                # 等待页面跳转
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

            except NoSuchWindowException:
                if attempt == 1:
                    logger.warning("浏览器窗口意外关闭，正在重启并重试登录一次")
                    self.close()
                    self._init_driver()
                    continue
                logger.error("浏览器窗口连续关闭，登录失败")
                return False
            except TimeoutException:
                logger.error("登录超时，请检查网络或账号信息")
                return False
            except WebDriverException as exc:
                if self._is_detached_frame_error(exc) and attempt == 1:
                    logger.warning("登录页标签失去连接，正在恢复浏览器后重试一次")
                    self._recover_primary_window()
                    continue
                logger.error("登录过程发生浏览器错误: %s", exc, exc_info=True)
                return False
            except Exception as exc:
                logger.error("登录过程发生意外错误: %s", exc, exc_info=True)
                return False

        return False

    def get_recent_posts(self, profile_url: str) -> List[Post]:
        """
        抓取指定用户主页的最新帖子（最多 10 条）。
        注意：可能受到对方隐私设置限制。
        """
        base = profile_url.rstrip("/")
        activity_url = f"{base}/recent-activity/all/"
        logger.info("访问用户动态页: %s", activity_url)

        try:
            self._navigate(activity_url)
            time.sleep(4)

            # 滚动触发懒加载
            self._driver.execute_script(
                "window.scrollTo(0, Math.round(document.body.scrollHeight * 0.4))"
            )
            time.sleep(2)

            posts = self._extract_posts()
            logger.info("共抓取到 %d 条帖子", len(posts))
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
        self._wait = WebDriverWait(self._driver, 15)
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
        for handle in list(self._driver.window_handles):
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
            shutil.which("msedge.exe"),
            os.path.join(
                os.environ.get("PROGRAMW6432", ""),
                "Microsoft", "Edge", "Application", "msedge.exe",
            ),
            os.path.join(
                os.environ.get("PROGRAMFILES", ""),
                "Microsoft", "Edge", "Application", "msedge.exe",
            ),
            os.path.join(
                os.environ.get("PROGRAMFILES(X86)", ""),
                "Microsoft", "Edge", "Application", "msedge.exe",
            ),
            os.path.join(
                os.environ.get("LOCALAPPDATA", ""),
                "Microsoft", "Edge", "Application", "msedge.exe",
            ),
        ]
        for path in candidates:
            if path and os.path.isfile(path):
                return path
        return None

    def _extract_posts(self) -> List[Post]:
        """在当前页面中查找并解析帖子元素。"""
        posts: List[Post] = []

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
        for elem in elements[:10]:
            try:
                post = self._parse_element(elem)
                if post:
                    posts.append(post)
            except Exception as exc:
                logger.debug("解析帖子元素异常: %s", exc)

        return posts

    def _parse_element(self, elem) -> Optional[Post]:
        """从单个帖子 DOM 元素中提取数据。"""
        # 1) 帖子唯一标识（优先用 data-urn）
        urn = elem.get_attribute("data-urn") or elem.get_attribute("data-id") or ""

        # 2) 正文内容
        content = self._find_text(elem, _CONTENT_SELECTORS)

        # 3) 时间戳 —— 取第一行非空文本，过滤含换行的作者名/关注按钮
        timestamp = ""
        for sel in _TIMESTAMP_SELECTORS:
            try:
                t = elem.find_element(By.CSS_SELECTOR, sel)
                raw_ts = t.get_attribute("datetime") or ""
                if not raw_ts:
                    # 取文本中第一行有效内容，排除多行合并噪音
                    for line in t.text.split("\n"):
                        line = line.strip()
                        if line and len(line) < 80:
                            raw_ts = line
                            break
                if raw_ts:
                    timestamp = raw_ts
                    break
            except NoSuchElementException:
                continue

        # 4) 帖子链接 —— 优先找直链，找不到则用 data-urn 构造
        post_url = ""
        for sel in _POST_LINK_SELECTORS:
            try:
                a = elem.find_element(By.CSS_SELECTOR, sel)
                post_url = a.get_attribute("href") or ""
                if post_url:
                    break
            except NoSuchElementException:
                continue
        # 从 data-urn (e.g. urn:li:activity:123) 构造 URL
        if not post_url and urn:
            import urllib.parse
            post_url = f"https://www.linkedin.com/feed/update/{urllib.parse.quote(urn, safe=':')}/"

        # 必须至少有 urn 或内容，才认为是有效帖子
        if not urn and not content:
            return None

        # 生成稳定 ID
        if urn:
            post_id = hashlib.sha256(urn.encode()).hexdigest()[:20]
        else:
            post_id = hashlib.sha256(content.encode()).hexdigest()[:20]

        return Post(
            post_id=post_id,
            content=content[:600],
            timestamp=timestamp,
            url=post_url,
            raw_urn=urn,
        )

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

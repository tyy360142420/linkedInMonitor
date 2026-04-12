"""
twitter_scraper.py
使用 Microsoft Edge (Selenium) 登录 X/Twitter 并抓取指定用户的最新推文。
注意：使用独立的 Edge profile 目录，与 LinkedIn scraper 互不干扰。
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

from selenium.webdriver.common.keys import Keys
logger = logging.getLogger(__name__)

_BUTTON_SELECTOR = 'div[role="button"]'
_EDGE_EXE = "msedge.exe"
_INPUT_NAME_TEXT_SELECTOR = 'input[name="text"]'
_INPUT_MODE_TEXT_SELECTOR = 'input[inputmode="text"]'


@dataclass
class Tweet:
    tweet_id: str
    author_handle: str
    content: str
    timestamp: str
    url: str


class TwitterScraper:
    """使用 Selenium Edge 登录 X/Twitter 并抓取用户推文。"""

    _LOGIN_URLS = [
        "https://x.com/i/flow/login",
        "https://x.com/login",
        "https://twitter.com/i/flow/login",
        "https://twitter.com/login",
    ]

    def __init__(
        self,
        email: str,
        username: str,
        password: str,
        headless: bool = False,
        session_profile_dir: str = ".edge_profile_twitter",
        challenge_wait_minutes: int = 5,
    ) -> None:
        self.email = email
        self.username = username
        self.password = password
        self.headless = headless
        self.session_profile_dir = session_profile_dir
        self.challenge_wait_minutes = challenge_wait_minutes
        self._driver: Optional[Any] = None
        self._wait: Optional[WebDriverWait] = None
        self._login_retry_used = False
        self._primary_window_handle: Optional[str] = None

    # ------------------------------------------------------------------ #
    # 驱动初始化
    # ------------------------------------------------------------------ #

    def _init_driver(self) -> None:
        opts = EdgeOptions()
        if self.headless:
            opts.add_argument("--headless=new")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--disable-session-crashed-bubble")
        opts.add_argument("--no-first-run")
        opts.add_argument("--no-default-browser-check")
        # 不使用 --disable-gpu，避免 React SPA 无法渲染内容
        opts.add_argument("--disable-gpu-compositing")
        opts.add_argument("--disable-software-rasterizer")
        opts.add_argument("--disable-notifications")
        opts.add_argument("--disable-popup-blocking")
        opts.add_argument("--disable-features=VizDisplayCompositor")
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_argument(f"--user-data-dir={self.session_profile_dir}")
        opts.add_argument("--lang=en-US,en;q=0.9")
        opts.add_argument("--window-size=1920,1080")
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        opts.add_experimental_option("useAutomationExtension", False)

        edge_binary = self._detect_edge_binary()
        if edge_binary:
            opts.binary_location = edge_binary

        profile_path = os.path.abspath(self.session_profile_dir)
        os.makedirs(profile_path, exist_ok=True)
        opts.add_argument(f"--user-data-dir={profile_path}")
        opts.add_argument("--profile-directory=Default")

        self._driver = webdriver.Edge(options=opts)
        self._driver.implicitly_wait(3)
        self._wait = WebDriverWait(self._driver, 25)
        # 隐藏 webdriver 特征，防止 X.com 检测后拒绝渲染登录表单
        self._mask_webdriver_flags()
        self._prepare_primary_window()
        logger.info(
            "Twitter Edge 已启动（无头: %s，profile: %s）",
            self.headless,
            profile_path,
        )

    # ------------------------------------------------------------------ #
    # 登录
    # ------------------------------------------------------------------ #

    def login(self) -> bool:
        """登录 X/Twitter，已有会话时直接跳过。成功返回 True。"""
        logger.info("正在登录 X/Twitter …")
        identifier = (self.email or self.username).strip()
        if not identifier or not self.password:
            logger.error("Twitter 登录信息不完整")
            return False

        try:
            return self._login_once(identifier)

        except TimeoutException:
            self._log_login_context("Twitter 登录超时")
            logger.error("Twitter 登录超时，请检查网络或账号")
            return False
        except WebDriverException as exc:
            if self._should_retry_with_fresh_profile(exc):
                return self._retry_with_fresh_profile(identifier, exc)
            self._log_login_context("Twitter 登录异常")
            logger.error("Twitter 登录异常: %s", exc)
            return False

    def _login_once(self, identifier: str) -> bool:
        if not self._open_login_entrypoint():
            logger.warning("Twitter 登录页未正常渲染，转为等待手动登录")
            self._log_login_context("登录入口加载失败")
            return self._wait_for_manual_login("等待手动登录")

        if self._is_logged_in():
            logger.info("检测到已有 Twitter 会话，跳过登录")
            return True

        if not self._submit_identifier(identifier):
            logger.warning("未找到 Twitter 登录账号输入框，转为等待手动登录")
            self._log_login_context("账号输入阶段失败")
            return self._wait_for_manual_login("等待手动登录")

        if not self._advance_login_flow(identifier):
            return self._wait_for_manual_login("等待手动完成密码输入或验证")

        if self._is_logged_in():
            logger.info("Twitter 登录成功")
            return True

        return self._wait_for_manual_login("进入人工验证等待")

    def _submit_identifier(self, identifier: str) -> bool:
        identifier_input = self._wait_for_input([
            'input[autocomplete="username"]',
            _INPUT_NAME_TEXT_SELECTOR,
            _INPUT_MODE_TEXT_SELECTOR,
        ])
        if not identifier_input:
            return False

        self._replace_input_text(identifier_input, identifier)
        time.sleep(0.8)
        return self._click_action_button(
            selector_candidates=[
                '[data-testid="LoginForm_Login_Button"]',
                '[data-testid="ocfEnterTextNextButton"]',
                _BUTTON_SELECTOR,
                'button',
            ],
            text_candidates=["Next", "下一步", "继续", "Log in", "登录"],
        )

    def _advance_login_flow(self, identifier: str) -> bool:
        secondary_identifier = (self.username or identifier).strip()
        deadline = time.time() + 35
        secondary_step_submitted = False

        while time.time() < deadline:
            if self._is_logged_in():
                return True

            if self._handle_password_step():
                continue

            if not secondary_step_submitted and self._handle_secondary_identifier_step(secondary_identifier):
                secondary_step_submitted = True
                continue

            if self._page_requires_manual_verification():
                logger.warning("检测到 Twitter 需要人工验证")
                self._log_login_context("检测到人工验证页面")
                return True

            time.sleep(1)

        logger.error("未找到 Twitter 密码输入框")
        self._log_login_context("密码输入阶段失败")
        return False

    def _open_login_entrypoint(self) -> bool:
        for login_url in self._LOGIN_URLS:
            try:
                self._navigate(login_url)
                # 给 React SPA 充足的时间完成首次渲染
                time.sleep(6)
            except Exception as exc:
                logger.debug("访问 Twitter 登录入口失败: %s (%s)", login_url, exc)
                continue

            if self._is_logged_in() or self._page_has_login_inputs():
                logger.info("Twitter 登录入口可用: %s", login_url)
                return True

            logger.warning("Twitter 登录入口未渲染输入框，尝试下一个入口: %s", login_url)

        return False

    def _page_has_login_inputs(self) -> bool:
        selectors = [
            'input[autocomplete="username"]',
            _INPUT_NAME_TEXT_SELECTOR,
            _INPUT_MODE_TEXT_SELECTOR,
            'input[name="password"]',
        ]
        for selector in selectors:
            try:
                if self._driver.find_elements(By.CSS_SELECTOR, selector):
                    return True
            except Exception:
                continue

        try:
            counts = self._driver.execute_script(
                "return {"
                "inputs: document.querySelectorAll('input').length,"
                "buttons: document.querySelectorAll('button,[role=\"button\"]').length,"
                "textLen: (document.body && document.body.innerText ? document.body.innerText.length : 0)"
                "};"
            ) or {}
            return (
                int(counts.get("inputs", 0)) > 0
                or int(counts.get("buttons", 0)) > 0
                or int(counts.get("textLen", 0)) > 80
            )
        except Exception:
            return False

    def _handle_password_step(self) -> bool:
        pwd_input = self._find_optional_input([
            'input[name="password"]',
            'input[type="password"]',
            'input[autocomplete="current-password"]',
        ])
        if not pwd_input:
            return False

        self._replace_input_text(pwd_input, self.password)
        time.sleep(0.8)
        if not self._click_action_button(
            selector_candidates=[
                '[data-testid="LoginForm_Login_Button"]',
                _BUTTON_SELECTOR,
                'button',
            ],
            text_candidates=["Log in", "登录", "Sign in"],
        ):
            logger.error("未找到 Twitter 登录提交按钮")
            self._log_login_context("密码提交阶段失败")
            raise WebDriverException("Twitter 登录提交按钮缺失")

        time.sleep(4)
        return True

    def _handle_secondary_identifier_step(self, secondary_identifier: str) -> bool:
        if not secondary_identifier:
            return False

        extra = self._find_optional_input([
            'input[data-testid="ocfEnterTextTextInput"]',
            _INPUT_NAME_TEXT_SELECTOR,
            _INPUT_MODE_TEXT_SELECTOR,
        ])
        if not extra:
            return False

        logger.info("检测到 Twitter 二次身份确认，尝试自动填写用户名/标识")
        self._replace_input_text(extra, secondary_identifier)
        time.sleep(0.8)
        if not self._click_action_button(
            selector_candidates=[
                '[data-testid="ocfEnterTextNextButton"]',
                _BUTTON_SELECTOR,
                'button',
            ],
            text_candidates=["Next", "下一步", "继续"],
        ):
            logger.error("未找到 Twitter 二次身份确认提交按钮")
            self._log_login_context("二次身份确认阶段失败")
            raise WebDriverException("Twitter 二次身份确认提交按钮缺失")

        time.sleep(3)
        return True

    def _is_logged_in(self) -> bool:
        try:
            self._ensure_primary_window()
            self._driver.find_element(
                By.CSS_SELECTOR,
                (
                    '[data-testid="SideNav_AccountSwitcher_Button"],'
                    '[data-testid="AppTabBar_Home_Link"],'
                    'a[href="/home"]'
                ),
            )
            return True
        except NoSuchElementException:
            return False

    def _wait_for_input(self, selectors: List[str], timeout: int = 45):
        deadline = time.time() + timeout
        while time.time() < deadline:
            element = self._find_optional_input(selectors)
            if element:
                return element
            time.sleep(0.8)
        return None

    def _find_optional_input(self, selectors: List[str]):
        # 先用 Selenium 原生方式查找
        for selector in selectors:
            try:
                elements = self._driver.find_elements(By.CSS_SELECTOR, selector)
                for element in elements:
                    if element.is_displayed():
                        return element
            except Exception:
                continue

        # Fallback：通过 JavaScript 直接查询（适用于 React 渲染较慢的场景）
        combined = ", ".join(selectors)
        try:
            js_el = self._driver.execute_script(
                "var els = document.querySelectorAll(arguments[0]);"
                "for (var i=0; i<els.length; i++) {"
                "  var s = els[i].style;"
                "  if (s.display !== 'none' && s.visibility !== 'hidden') return els[i];"
                "}"
                "return null;",
                combined,
            )
            if js_el:
                return js_el
        except Exception:
            pass

        return None

    def _click_action_button(
        self,
        selector_candidates: List[str],
        text_candidates: List[str],
    ) -> bool:
        lowered = {text.lower() for text in text_candidates}

        if self._click_by_selector_text(selector_candidates, lowered):
            return True
        if self._click_by_xpath_text(text_candidates):
            return True

        logger.warning("未找到可点击按钮: %s", ", ".join(text_candidates))
        return False

    def _click_by_selector_text(
        self,
        selector_candidates: List[str],
        lowered_text_candidates: set,
    ) -> bool:
        for selector in selector_candidates:
            try:
                elements = self._driver.find_elements(By.CSS_SELECTOR, selector)
            except Exception:
                continue

            for element in elements:
                text = " ".join((element.text or "").strip().lower().split())
                if not text:
                    continue
                if not any(candidate in text for candidate in lowered_text_candidates):
                    continue
                if not element.is_displayed():
                    continue
                self._driver.execute_script("arguments[0].click();", element)
                return True

        return False

    def _replace_input_text(self, element, value: str) -> None:
        element.click()
        try:
            element.send_keys(Keys.CONTROL, "a")
            element.send_keys(Keys.DELETE)
        except Exception:
            pass
        try:
            element.clear()
        except Exception:
            pass
        element.send_keys(value)

    def _page_requires_manual_verification(self) -> bool:
        try:
            body_text = (self._driver.find_element(By.TAG_NAME, "body").text or "").lower()
        except Exception:
            return False

        keywords = [
            "verification code",
            "enter the code",
            "confirm your identity",
            "suspicious login",
            "unusual activity",
            "verify",
            "captcha",
            "验证",
            "确认你的身份",
        ]
        return any(keyword in body_text for keyword in keywords)

    def _should_retry_with_fresh_profile(self, exc: WebDriverException) -> bool:
        if self._login_retry_used:
            return False
        message = str(exc).lower()
        return (
            "no such window" in message
            or "invalid session id" in message
            or "disconnected" in message
            or "renderer" in message
        )

    def _retry_with_fresh_profile(self, identifier: str, exc: WebDriverException) -> bool:
        logger.warning("Twitter 浏览器会话异常，改用全新 profile 重试一次: %s", exc)
        self._login_retry_used = True
        self.close()

        base_profile_dir = self.session_profile_dir.rstrip("\\/")
        self.session_profile_dir = f"{base_profile_dir}_retry"
        self._init_driver()

        try:
            return self._login_once(identifier)
        except TimeoutException:
            self._log_login_context("Twitter 重试登录超时")
            logger.error("Twitter 登录超时，请检查网络或账号")
            return False
        except WebDriverException as retry_exc:
            self._log_login_context("Twitter 重试登录异常")
            logger.error("Twitter 登录异常: %s", retry_exc)
            return False

    def _log_login_context(self, stage: str) -> None:
        try:
            self._ensure_primary_window()
            current_url = self._driver.current_url
        except Exception:
            current_url = "<unavailable>"

        try:
            title = self._driver.title
        except Exception:
            title = "<unavailable>"

        logger.info("%s，当前页面: %s，标题: %s", stage, current_url, title)

    def _wait_for_manual_login(self, stage: str) -> bool:
        logger.warning(
            "%s，等待 %d 分钟内手动完成登录/验证 …",
            stage,
            self.challenge_wait_minutes,
        )
        self._log_login_context(stage)
        deadline = time.time() + self.challenge_wait_minutes * 60
        next_refresh = time.time() + 30
        while time.time() < deadline:
            time.sleep(5)
            if self._is_logged_in():
                logger.info("检测到手动登录已完成，Twitter 登录成功")
                return True
            if time.time() >= next_refresh:
                try:
                    self._driver.refresh()
                except Exception:
                    pass
                next_refresh = time.time() + 30

        logger.error("Twitter 验证超时")
        self._log_login_context("人工验证超时")
        return False

    def _prepare_primary_window(self) -> None:
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
                    logger.warning("Twitter 标签页上下文丢失，正在恢复并重试: %s", url)
                    self._recover_primary_window()
                    continue
                raise

    @staticmethod
    def _is_detached_frame_error(exc: Exception) -> bool:
        return "target frame detached" in str(exc).lower()

    def _mask_webdriver_flags(self) -> None:
        """通过 CDP 注入脚本，让页面无法通过 navigator.webdriver 识别自动化。"""
        try:
            self._driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {
                    "source": """
                        Object.defineProperty(navigator, 'webdriver', {
                            get: () => undefined
                        });
                        window.chrome = { runtime: {} };
                    """
                },
            )
            logger.debug("Webdriver 标识已通过 CDP 隐藏")
        except Exception as exc:
            logger.debug("CDP 注入失败（不影响运行）: %s", exc)

    @staticmethod
    def _detect_edge_binary() -> Optional[str]:
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

    def _click_by_xpath_text(self, text_candidates: List[str]) -> bool:
        for text in text_candidates:
            try:
                xpath = (
                    "//span[normalize-space(text())=\"%s\"]"
                    "/ancestor::*[@role='button' or self::button][1]"
                ) % text
                button = self._driver.find_element(By.XPATH, xpath)
                self._driver.execute_script("arguments[0].click();", button)
                return True
            except Exception:
                continue

        return False

    # ------------------------------------------------------------------ #
    # 抓取推文
    # ------------------------------------------------------------------ #

    def get_recent_tweets(
        self, handle: str, max_tweets: int = 20
    ) -> List[Tweet]:
        """抓取指定账号的最新推文。handle 不含 @ 符号。"""
        url = f"https://x.com/{handle}"
        logger.info("访问 Twitter 用户主页: %s", url)
        try:
            self._driver.get(url)
            time.sleep(3)

            # 滚动加载更多内容
            for _ in range(3):
                self._driver.execute_script("window.scrollBy(0, 1000)")
                time.sleep(1.2)

            articles = self._driver.find_elements(
                By.CSS_SELECTOR, 'article[data-testid="tweet"]'
            )
            tweets: List[Tweet] = []
            seen_ids: set = set()

            for article in articles[:max_tweets]:
                try:
                    tweet = self._parse_tweet(article, handle)
                    if tweet and tweet.tweet_id not in seen_ids:
                        seen_ids.add(tweet.tweet_id)
                        tweets.append(tweet)
                except Exception as exc:
                    logger.debug("解析推文失败: %s", exc)

            logger.info("从 @%s 抓取到 %d 条推文", handle, len(tweets))
            return tweets

        except WebDriverException as exc:
            logger.error("抓取 @%s 失败: %s", handle, exc)
            return []

    def _parse_tweet(self, article, author_handle: str) -> Optional[Tweet]:
        # 正文
        content = ""
        try:
            content = article.find_element(
                By.CSS_SELECTOR, '[data-testid="tweetText"]'
            ).text
        except NoSuchElementException:
            pass

        # 时间戳
        timestamp = ""
        try:
            t_el = article.find_element(By.CSS_SELECTOR, "time")
            timestamp = t_el.get_attribute("datetime") or t_el.text
        except NoSuchElementException:
            pass

        # tweet URL / ID
        url = ""
        tweet_id = ""
        try:
            links = article.find_elements(
                By.CSS_SELECTOR, 'a[href*="/status/"]'
            )
            if links:
                href = links[0].get_attribute("href") or ""
                url = href
                parts = href.rstrip("/").split("/")
                if parts:
                    tweet_id = parts[-1]
        except Exception:
            pass

        if not tweet_id:
            tweet_id = hashlib.md5(
                (author_handle + content[:200]).encode()
            ).hexdigest()[:16]

        if not content and not url:
            return None

        return Tweet(
            tweet_id=tweet_id,
            author_handle=author_handle,
            content=content,
            timestamp=timestamp,
            url=url,
        )

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    def close(self) -> None:
        if self._driver:
            try:
                self._driver.quit()
            except Exception:
                pass
            self._driver = None
            self._primary_window_handle = None
        logger.info("Twitter 浏览器已关闭")

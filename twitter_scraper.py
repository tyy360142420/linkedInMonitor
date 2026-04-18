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

from selenium.webdriver.common.keys import Keys
logger = logging.getLogger(__name__)

_BUTTON_SELECTOR = 'div[role="button"]'
_EDGE_EXE = "msedge.exe"
_LOGIN_FORM_BUTTON_SELECTOR = '[data-testid="LoginForm_Login_Button"]'
_INPUT_AUTOCOMPLETE_USERNAME_SELECTOR = 'input[autocomplete="username"]'
_INPUT_NAME_TEXT_SELECTOR = 'input[name="text"]'
_INPUT_MODE_TEXT_SELECTOR = 'input[inputmode="text"]'
_LOGIN_TEXT_CANDIDATES = ["Log in", "登录", "Sign in", "登入"]
_JS_CLICK = "arguments[0].click();"


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
        self.last_login_error: str = ""

    # ------------------------------------------------------------------ #
    # 驱动初始化
    # ------------------------------------------------------------------ #

    def _build_edge_options(self, profile_path: str) -> EdgeOptions:
        opts = EdgeOptions()
        if self.headless:
            opts.add_argument("--headless=new")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--disable-session-crashed-bubble")
        opts.add_argument("--no-first-run")
        opts.add_argument("--no-default-browser-check")
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_argument("--disable-features=TranslateUI")
        opts.add_argument("--disable-popup-blocking")
        opts.add_argument("--disable-notifications")
        opts.add_argument("--lang=en-US,en;q=0.9")
        opts.add_argument("--window-size=1920,1080")
        # 禁用 GPU 渲染进程，防止 AMD/GPU 驱动错误导致 Chrome instance exited
        opts.add_argument("--disable-gpu")
        opts.add_argument("--disable-gpu-compositing")
        opts.add_argument("--disable-software-rasterizer")
        opts.add_argument("--disable-features=VizDisplayCompositor,TranslateUI")
        opts.add_experimental_option("excludeSwitches", ["enable-logging", "enable-automation"])
        opts.add_experimental_option("useAutomationExtension", False)

        edge_binary = self._detect_edge_binary()
        if edge_binary:
            opts.binary_location = edge_binary

        opts.add_argument(f"--user-data-dir={profile_path}")
        opts.add_argument("--profile-directory=Default")
        return opts

    @staticmethod
    def _remove_lock_files(profile_path: str) -> None:
        """删除 Edge/Chrome 遗留的锁文件，避免 session not created。"""
        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket", "lockfile"):
            lock = os.path.join(profile_path, name)
            try:
                if os.path.exists(lock):
                    os.remove(lock)
            except OSError:
                pass

    def _init_driver(self) -> None:
        profile_path = os.path.abspath(self.session_profile_dir)
        os.makedirs(profile_path, exist_ok=True)
        self._remove_lock_files(profile_path)

        try:
            self._driver = webdriver.Edge(options=self._build_edge_options(profile_path))
        except WebDriverException as exc:
            err_text = str(exc).lower()
            fallback_needed = (
                "session not created" in err_text
                or "chrome instance exited" in err_text
                or "user data directory is already in use" in err_text
            )
            if not fallback_needed:
                raise

            fallback_profile = os.path.abspath(f"{self.session_profile_dir}_runtime")
            os.makedirs(fallback_profile, exist_ok=True)
            self._remove_lock_files(fallback_profile)
            logger.warning(
                "Twitter Edge 启动失败，改用备用 profile 重试。原 profile: %s, 备用 profile: %s",
                profile_path,
                fallback_profile,
            )
            self._driver = webdriver.Edge(options=self._build_edge_options(fallback_profile))

        self._driver.implicitly_wait(3)
        self._wait = WebDriverWait(self._driver, 25)
        
        # 使用 headless=false 来支持手动登录
        if self.headless:
            logger.warning("Warning: headless mode 可能会导致集成登录验证失败。建议设置 headless=False。")
        
        # 隐藏 webdriver 特征
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
        self.last_login_error = ""
        logger.info("正在登录 X/Twitter …")
        identifier = (self.email or self.username).strip()
        if not identifier:
            self.last_login_error = "Twitter 登录标识不完整（请至少填写邮箱或用户名）"
            logger.error(self.last_login_error)
            return False

        # 首先检查是否已有有效的登录会话（通过特定的 cookies）
        if self._has_valid_session():
            logger.info("检测到有效的 Twitter 登录会话，直接使用")
            return True

        try:
            return self._login_once(identifier)

        except TimeoutException:
            self._log_login_context("Twitter 登录超时")
            self.last_login_error = "Twitter 登录超时，请检查网络或账号"
            logger.error(self.last_login_error)
            return False
        except WebDriverException as exc:
            if self._should_retry_with_fresh_profile(exc):
                return self._retry_with_fresh_profile(identifier, exc)
            self._log_login_context("Twitter 登录异常")
            self.last_login_error = f"Twitter 登录异常: {exc}"
            logger.error(self.last_login_error)
            return False

    def _has_valid_session(self) -> bool:
        """检查配置文件中是否有有效的登录 cookies。"""
        cookies_file = os.path.join(self.session_profile_dir, "Default", "Cookies")
        if os.path.exists(cookies_file):
            try:
                # 如果 cookies 文件存在且最近有修改，认为有有效会话
                mtime = os.path.getmtime(cookies_file)
                age_hours = (time.time() - mtime) / 3600
                if age_hours < 720:  # 30 天内
                    logger.info(f"检测到有效的 cookies（{age_hours:.1f} 小时前保存）")
                    # 尝试导航到 x.com 验证会话
                    try:
                        self._driver.get("https://x.com/home")
                        time.sleep(2)
                        current_url = (self._driver.current_url or "").lower()
                        if "/home" in current_url and "/i/flow/login" not in current_url:
                            return True
                        return self._is_logged_in()
                    except Exception as e:
                        logger.debug(f"验证会话失败: {e}")
                        return False
            except Exception as e:
                logger.debug(f"检查 cookies 文件失败: {e}")
        return False

    def _login_once(self, identifier: str) -> bool:
        """尝试自动登录，失败则转为手动登录等待。"""
        
        # 检查可用配置文件中是否已有有效登录
        if self._has_valid_session():
            logger.info("✓ 检测到有效的 Twitter 登录会话，直接使用")
            return True
        
        logger.info("━" * 70)
        logger.info("开始 Twitter 登录流程")
        logger.info("由于 X.com 的严格反自动化防护，系统将打开可见的浏览器窗口。")
        logger.info("━" * 70)
        
        try:
            self._driver.get("https://x.com")
            time.sleep(2)
        except Exception:
            pass

        if self._is_logged_in():
            logger.info("✓ 检测到已有登录会话，跳过登录")
            return True
        
        can_auto_password_login = bool((self.password or "").strip())

        # 尝试自动登录，但如果失败就进入手动模式
        if not can_auto_password_login:
            logger.warning("未配置 Twitter 密码，将跳过自动密码登录并进入手动登录（可使用 Google 账户）")
        else:
            if not self._open_login_entrypoint():
                logger.warning("⚠ 登录页面加载失败，进入手动登录模式")
            elif not self._submit_identifier(identifier):
                logger.warning("⚠ 无法自动输入账号，进入手动登录模式")
            elif not self._advance_login_flow(identifier):
                logger.warning("⚠ 自动登录失败（常见原因：服务器拒绝、需要验证），进入手动登录模式")
            elif self._is_logged_in():
                logger.info("✓ Twitter 登录成功")
                return True
            else:
                logger.warning("⚠ 登录状态未能确认，进入手动登录模式")

        if self._is_login_rate_limited_page():
            self._log_login_context("检测到 Twitter 登录风控")
            logger.warning("检测到 Twitter 登录限制（399），将继续等待你手动点击 Google 登录或稍后重试")
        
        # 自动登录失败，转入手动登录
        logger.warning("╔" + "═" * 68 + "╗")
        logger.warning("║ " + "需要手动完成 X/Twitter 登录".center(66) + " ║")
        logger.warning("╠" + "═" * 68 + "╣")
        logger.warning("║ 请在已打开的浏览器窗口中完成以下操作：                            ║")
        logger.warning("║                                                                    ║")
        logger.warning("║ 1️⃣  优先点击 Continue with Google（推荐）                         ║")
        logger.warning("║ 2️⃣  使用你配置中的邮箱完成 Google 登录与二次验证                  ║")
        logger.warning("║ 3️⃣  若 Google 不可用，再改用用户名/密码登录                       ║")
        logger.warning("║ 4️⃣  登录成功后保持页面 10~20 秒，系统会自动检测并继续运行         ║")
        logger.warning("║                                                                    ║")
        logger.warning("║ ⏱️  系统将等待 " + str(self.challenge_wait_minutes).ljust(2) + " 分钟，超时后将停止等待            ║")
        logger.warning("╚" + "═" * 68 + "╝")

        # 确保手动兜底时能回到包含 Google 入口的登录页，必要时自动点一次 Google 按钮。
        self._prepare_google_manual_login()
        
        self._log_login_context("自动登录失败，进入手动登录等待")
        return self._wait_for_manual_login(f"等待手动登录验证（{self.challenge_wait_minutes}分钟超时）")

    def _submit_identifier(self, identifier: str) -> bool:
        self._ensure_login_form_opened()
        identifier_input = self._wait_for_input([
            _INPUT_AUTOCOMPLETE_USERNAME_SELECTOR,
            _INPUT_NAME_TEXT_SELECTOR,
            _INPUT_MODE_TEXT_SELECTOR,
        ])
        if not identifier_input:
            return False

        self._replace_input_text(identifier_input, identifier)
        time.sleep(0.8)
        return self._click_action_button(
            selector_candidates=[
                _LOGIN_FORM_BUTTON_SELECTOR,
                '[data-testid="ocfEnterTextNextButton"]',
                _BUTTON_SELECTOR,
                'button',
            ],
            text_candidates=["Next", "下一步", "继续", "Log in", "登录"],
        )

    def _advance_login_flow(self, identifier: str) -> bool:
        secondary_identifier = (self.username or identifier).strip()
        deadline = time.time() + 35
        secondary_step_submitted_count = 0

        while time.time() < deadline:
            if self._is_logged_in():
                return True

            if self._handle_password_step():
                continue

            if secondary_step_submitted_count < 3 and self._handle_secondary_identifier_step(secondary_identifier):
                secondary_step_submitted_count += 1
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
                time.sleep(2)
                # 如果空白页还在加载 React 组件，持续等待
                if not self._page_has_login_inputs():
                    time.sleep(4)

                # 首次渲染失败时自动执行一次“重试”动作（等价于手动点重试）
                if not self._page_has_login_inputs() and self._auto_retry_login_render(login_url):
                    logger.info("Twitter 登录页自动重试成功: %s", login_url)
            except Exception as exc:
                logger.debug("访问 Twitter 登录入口失败: %s (%s)", login_url, exc)
                continue

            if self._is_logged_in() or self._page_has_login_inputs():
                logger.info("Twitter 登录入口可用: %s", login_url)
                return True

            logger.warning("Twitter 登录入口未渲染输入框，尝试下一个入口: %s", login_url)

        return False

    def _page_has_login_inputs(self) -> bool:
        self._wait_for_page_load_complete(timeout=3)
        
        selectors = [
            _INPUT_AUTOCOMPLETE_USERNAME_SELECTOR,
            _INPUT_NAME_TEXT_SELECTOR,
            _INPUT_MODE_TEXT_SELECTOR,
            'input[name="password"]',
        ]
        for selector in selectors:
            try:
                elements = self._driver.find_elements(By.CSS_SELECTOR, selector)
                if elements and len(elements) > 0:
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
            if isinstance(counts, dict):
                return (
                    int(counts.get("inputs", 0)) > 0
                    or int(counts.get("buttons", 0)) > 0
                    or int(counts.get("textLen", 0)) > 80
                )
        except Exception:
            pass
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
        # 给用户输入一些自然的延迟
        time.sleep(1.2)
        
        if not self._click_action_button(
            selector_candidates=[
                _LOGIN_FORM_BUTTON_SELECTOR,
                _BUTTON_SELECTOR,
                'button',
            ],
            text_candidates=_LOGIN_TEXT_CANDIDATES,
        ):
            logger.warning("未找到 Twitter 登录提交按钮，改为等待手动继续")
            self._log_login_context("密码提交阶段失败")
            return False

        # 等待认证服务器响应，增加延迟以允许后端验证
        logger.info("正在等待 X.com 认证服务器响应...")
        time.sleep(6)
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
            logger.warning("未找到 Twitter 二次身份确认提交按钮，改为等待手动继续")
            self._log_login_context("二次身份确认阶段失败")
            return False

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
            self._wait_for_page_load_complete(timeout=5)
            element = self._find_optional_input(selectors)
            if element:
                return element
            time.sleep(0.8)
        return None

    def _wait_for_page_load_complete(self, timeout: int = 3) -> None:
        """等待页面加载完成，检查加载动画消失。"""
        try:
            deadline = time.time() + timeout
            while time.time() < deadline:
                result = self._driver.execute_script(
                    "return !(document.querySelector('[role=\"progressbar\"]') != null "
                    "|| document.querySelector('.spinner') != null "
                    "|| document.querySelector('[data-testid=\"loadingIndicator\"]') != null);"
                )
                if result:
                    return
                time.sleep(0.3)
        except Exception:
            pass

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

    def _ensure_login_form_opened(self) -> None:
        """部分页面会先展示入口页，先点击一次登录入口再等待用户名输入框。
           如果点击失败，不影响流程（继续等待输入框出现）。"""
        try:
            # 输入框已经存在，不需要点按钮
            if self._find_optional_input([
                _INPUT_AUTOCOMPLETE_USERNAME_SELECTOR,
                _INPUT_NAME_TEXT_SELECTOR,
                _INPUT_MODE_TEXT_SELECTOR,
            ]):
                return

            # 最多尝试 2 次点击按钮，如果都失败，继续等待输入框出现
            for attempt in range(2):
                try:
                    if self._click_action_button(
                        selector_candidates=[
                            'a[href*="/i/flow/login"]',
                            'a[href*="/login"]',
                            '[data-testid="loginButton"]',
                            _LOGIN_FORM_BUTTON_SELECTOR,
                            _BUTTON_SELECTOR,
                            'button',
                        ],
                        text_candidates=_LOGIN_TEXT_CANDIDATES,
                    ):
                        logger.debug("成功点击登录入口按钮")
                        time.sleep(2)
                        return
                except Exception as e:
                    logger.debug("点击登录按钮异常: %s", e)
                if attempt == 0:
                    time.sleep(0.5)
            logger.debug("未找到登录按钮，继续等待输入框出现")
        except Exception as e:
            logger.debug("打开登录表单时异常: %s", e)

    def _prepare_google_manual_login(self) -> None:
        """手动兜底时尽量回到可见 Google 登录入口，并尝试自动点击一次。"""
        try:
            self._navigate("https://x.com/i/flow/login")
            time.sleep(1.5)
        except Exception as exc:
            logger.debug("返回登录入口失败: %s", exc)
            return

        clicked = self._click_action_button(
            selector_candidates=[
                '[data-testid="google_sign_in_button"]',
                _BUTTON_SELECTOR,
                'button',
                'a',
            ],
            text_candidates=[
                "Continue with Google",
                "Sign in with Google",
                "Google",
                "使用 Google 帐户登录",
                "通过 Google 继续",
                "通过google继续",
            ],
        )
        if clicked:
            logger.info("已自动点击 Google 登录入口，请在弹窗/新页面完成 Google 登录")
            time.sleep(1)
        else:
            logger.info("未自动匹配到 Google 登录按钮，请在页面手动点击 Continue with Google")

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

            if self._click_first_matching_element(
                elements,
                selector,
                lowered_text_candidates,
            ):
                return True

        return False

    def _click_first_matching_element(
        self,
        elements,
        selector: str,
        lowered_text_candidates: set,
    ) -> bool:
        for element in elements:
            if not self._is_clickable_element(element):
                continue

            if self._selector_allows_direct_click(selector):
                self._click_element(element)
                return True

            if self._element_matches_text(element, lowered_text_candidates):
                self._click_element(element)
                return True

        return False

    @staticmethod
    def _selector_allows_direct_click(selector: str) -> bool:
        # X 登录流程里的 data-testid 按钮有时没有可读文本，允许直接点击。
        return "data-testid" in selector

    @staticmethod
    def _element_matches_text(element, lowered_text_candidates: set) -> bool:
        text = " ".join((element.text or "").strip().lower().split())
        if not text:
            return False
        return any(candidate in text for candidate in lowered_text_candidates)

    @staticmethod
    def _is_clickable_element(element) -> bool:
        try:
            return bool(element.is_displayed())
        except Exception:
            return False

    def _click_element(self, element) -> None:
        self._driver.execute_script(_JS_CLICK, element)

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
            "could not log you in now",
            "please try again later",
            "verify",
            "captcha",
            "验证",
            "确认你的身份",
            "暂时无法登录",
            "请稍后再试",
        ]
        return any(keyword in body_text for keyword in keywords)

    def _is_login_rate_limited_page(self) -> bool:
        try:
            body_text = (self._driver.find_element(By.TAG_NAME, "body").text or "").lower()
        except Exception:
            return False

        rate_limit_keywords = [
            "could not log you in now",
            "please try again later",
            "暂时无法登录",
            "请稍后再试",
        ]
        return any(keyword in body_text for keyword in rate_limit_keywords)

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
        logger.warning("Twitter 浏览器会话异常，沿用当前 profile 重试一次: %s", exc)
        self._login_retry_used = True
        try:
            self.close()
        except Exception:
            pass
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
        saw_rate_limit = False
        while time.time() < deadline:
            time.sleep(5)
            if self._is_login_rate_limited_page() and not saw_rate_limit:
                saw_rate_limit = True
                logger.warning("页面显示 399 登录限制；可先尝试 Continue with Google，若仍失败请等待后重试")
            if self._is_logged_in():
                logger.info("检测到手动登录已完成，Twitter 登录成功")
                return True

        logger.error("Twitter 验证超时")
        if saw_rate_limit:
            self.last_login_error = (
                "Twitter 返回登录限制（399）：Could not log you in now。"
                "建议等待 30 分钟后再重试，优先使用 Continue with Google 登录。"
            )
        self._log_login_context("人工验证超时")
        return False

    def _prepare_primary_window(self) -> None:
        current_handles = self._driver.window_handles
        if not current_handles:
            raise RuntimeError("Edge 未创建可用窗口")

        # 直接复用当前窗口，避免新开标签页导致会话扰动
        self._primary_window_handle = current_handles[-1]
        self._driver.switch_to.window(self._primary_window_handle)

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
        handles = self._driver.window_handles
        if not handles:
            raise NoSuchWindowException("没有可用浏览器窗口")
        self._primary_window_handle = handles[-1]
        self._driver.switch_to.window(self._primary_window_handle)

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

    def _auto_retry_login_render(self, login_url: str) -> bool:
        """首次打开未渲染输入框时，自动刷新和重进一次登录页。"""
        try:
            self._driver.refresh()
            time.sleep(2)
            if self._page_has_login_inputs():
                return True
        except Exception:
            pass

        try:
            self._navigate(login_url)
            time.sleep(2)
            if self._page_has_login_inputs():
                return True
        except Exception:
            pass

        return False

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
                        // 只做最小化掩码，避免过度指纹改写触发风控
                        Object.defineProperty(navigator, 'webdriver', {
                            get: () => undefined,
                            configurable: true
                        });

                        window.chrome = window.chrome || { runtime: {} };
                    """
                },
            )
            logger.debug("Webdriver 标识已通过 CDP 隐藏")
        except Exception as exc:
            logger.debug("CDP 注入失败（不影响运行）: %s", exc)
    
    def _setup_network_interception(self) -> None:
        """启用网络拦截并添加真实浏览器的请求头。"""
        try:
            # 启用网络驱动
            self._driver.execute_cdp_cmd('Network.enable', {})
            
            # 注入脚本来拦截 XMLHttpRequest 和 fetch
            self._driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {
                    "source": """
                        // 拦截 fetch，添加必要的安全头部
                        const originalFetch = window.fetch;
                        window.fetch = function(...args) {
                            let request = args[0];
                            let options = args[1] || {};
                            
                            // 确保有 headers
                            if (!options.headers) options.headers = {};
                            
                            // 添加安全相关的 headers
                            options.headers['Sec-Fetch-Site'] = 'same-origin';
                            options.headers['Sec-Fetch-Mode'] = 'cors';
                            options.headers['Sec-Fetch-Dest'] = 'empty';
                            options.headers['Sec-Fetch-User'] = '?1';
                            options.headers['Sec-Ch-Ua'] = '"Microsoft Edge";v="120", "Chromium";v="120", ";Not A Brand";v="99"';
                            options.headers['Sec-Ch-Ua-Mobile'] = '?0';
                            options.headers['Sec-Ch-Ua-Platform'] = '"Windows"';
                            
                            // 添加 Referer
                            if (!options.headers['Referer']) {
                                options.headers['Referer'] = 'https://x.com/';
                            }
                            
                            // 确保启用 credentials
                            if (options.mode === 'cors') {
                                options.credentials = 'include';
                            }
                            
                            return originalFetch.apply(this, [request, options]);
                        };
                        
                        // 拦截 XMLHttpRequest
                        const XHROpen = XMLHttpRequest.prototype.open;
                        XMLHttpRequest.prototype.open = function(method, url, ...rest) {
                            const xhr = this;
                            const originalSetHeader = this.setRequestHeader;
                            let headersSet = false;
                            
                            this.setRequestHeader = function(header, value) {
                                // 添加安全头部
                                if (header.toLowerCase() === 'sec-fetch-site') {
                                    return originalSetHeader.call(xhr, header, 'same-origin');
                                }
                                if (header.toLowerCase() === 'sec-fetch-mode') {
                                    return originalSetHeader.call(xhr, header, 'cors');
                                }
                                if (header.toLowerCase() === 'sec-fetch-dest') {
                                    return originalSetHeader.call(xhr, header, 'empty');
                                }
                                return originalSetHeader.call(xhr, header, value);
                            };
                            
                            // 监听 send 来添加默认头部
                            const originalSend = this.send;
                            this.send = function(body) {
                                if (!headersSet) {
                                    originalSetHeader.call(xhr, 'Sec-Fetch-Site', 'same-origin');
                                    originalSetHeader.call(xhr, 'Sec-Fetch-Mode', 'cors');
                                    originalSetHeader.call(xhr, 'Sec-Fetch-Dest', 'empty');
                                    originalSetHeader.call(xhr, 'Sec-Fetch-User', '?1');
                                    headersSet = true;
                                }
                                return originalSend.call(xhr, body);
                            };
                            
                            return XHROpen.apply(this, [method, url, ...rest]);
                        };
                    """
                },
            )
            logger.debug("网络请求拦截已启用")
        except Exception as exc:
            logger.debug("网络拦截设置失败（不影响运行）: %s", exc)

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
                self._click_element(button)
                return True
            except Exception:
                continue

        return False

    # ------------------------------------------------------------------ #
    # 抓取推文
    # ------------------------------------------------------------------ #

    def get_recent_tweets(
        self,
        handle: str,
        lookback_days: int = 30,
        max_scrolls: int = 8,
        max_candidates: int = 120,
    ) -> List[Tweet]:
        """抓取指定账号在最近时间窗口内的推文。handle 不含 @ 符号。"""
        url = f"https://x.com/{handle}"
        logger.info("访问 Twitter 用户主页: %s", url)
        try:
            self._driver.get(url)
            time.sleep(3)

            # 滚动加载更多内容，尽量覆盖整个时间窗口。
            for _ in range(max_scrolls):
                self._driver.execute_script("window.scrollBy(0, 1000)")
                time.sleep(1.2)

            articles = self._driver.find_elements(
                By.CSS_SELECTOR, 'article[data-testid="tweet"]'
            )
            tweets: List[Tweet] = []
            seen_ids: set = set()
            cutoff = datetime.now(timezone.utc) - timedelta(days=max(lookback_days, 1))

            for article in articles[:max_candidates]:
                try:
                    tweet = self._parse_tweet(article, handle)
                    if not tweet or tweet.tweet_id in seen_ids:
                        continue
                    parsed_at = self._parse_timestamp(tweet.timestamp)
                    if parsed_at and parsed_at < cutoff:
                        continue
                    seen_ids.add(tweet.tweet_id)
                    tweets.append(tweet)
                except Exception as exc:
                    logger.debug("解析推文失败: %s", exc)

            logger.info("从 @%s 抓取到 %d 条最近 %d 天推文", handle, len(tweets), lookback_days)
            return tweets

        except WebDriverException as exc:
            logger.error("抓取 @%s 失败: %s", handle, exc)
            return []

    @staticmethod
    def _parse_timestamp(raw_value: str) -> Optional[datetime]:
        raw = (raw_value or "").strip()
        if not raw:
            return None

        normalized = raw.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return None

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

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
    # 账户统计 & 关注列表
    # ------------------------------------------------------------------ #

    @staticmethod
    def _parse_count_text(text: str) -> int:
        """解析 Twitter 风格的数字，如 '1.2K'、'56.8M'。"""
        text = (text or "").strip().replace(",", "").replace("\u00a0", "").replace(" ", "")
        if not text:
            return 0
        multiplier = 1
        upper = text.upper()
        if upper.endswith("K"):
            multiplier = 1_000
            text = text[:-1]
        elif upper.endswith("M"):
            multiplier = 1_000_000
            text = text[:-1]
        elif upper.endswith("B"):
            multiplier = 1_000_000_000
            text = text[:-1]
        try:
            return int(float(text) * multiplier)
        except (ValueError, TypeError):
            return 0

    def get_account_stats(self, handle: str) -> dict:
        """获取账户的粉丝数（followers）和关注数（following）。"""
        url = f"https://x.com/{handle}"
        try:
            self._navigate(url)
            time.sleep(2)
            stats = {"follower_count": 0, "following_count": 0}
            try:
                links = self._driver.find_elements(
                    By.CSS_SELECTOR,
                    'a[href$="/followers"], a[href$="/following"]',
                )
                for link in links:
                    href = (link.get_attribute("href") or "").rstrip("/")
                    spans = link.find_elements(By.TAG_NAME, "span")
                    count_text = ""
                    for span in spans:
                        t = (span.text or "").strip()
                        if t and any(c.isdigit() for c in t):
                            count_text = t
                            break
                    count = self._parse_count_text(count_text)
                    if href.endswith("/followers"):
                        stats["follower_count"] = count
                    elif href.endswith("/following"):
                        stats["following_count"] = count
            except Exception as exc:
                logger.debug("解析账户统计信息失败: %s", exc)
            logger.info(
                "@%s 粉丝数: %s，关注数: %s",
                handle,
                stats["follower_count"],
                stats["following_count"],
            )
            return stats
        except Exception as exc:
            logger.error("访问 @%s 主页失败: %s", handle, exc)
            return {"follower_count": 0, "following_count": 0}

    def get_following_list(
        self,
        handle: str,
        max_users: int = 500,
        batch_scrolls: int = 4,
        batch_pause: float = 1.5,
    ) -> List[dict]:
        """
        分批滚动抓取指定账号的关注列表（Following）。
        返回 [{"handle": str, "name": str}, ...]
        """
        url = f"https://x.com/{handle}/following"
        logger.info("开始分批抓取 @%s 的关注列表，上限 %d 人", handle, max_users)
        try:
            self._navigate(url)
            time.sleep(3)

            seen_handles: set = set()
            result: List[dict] = []
            no_new_streak = 0
            MAX_NO_NEW = 4  # 连续 4 批无新增则停止

            while len(result) < max_users and no_new_streak < MAX_NO_NEW:
                cells = self._driver.find_elements(
                    By.CSS_SELECTOR, '[data-testid="UserCell"]'
                )
                new_found = 0

                for cell in cells:
                    if len(result) >= max_users:
                        break
                    try:
                        user_handle, user_name = self._extract_user_from_cell(cell)
                        if not user_handle:
                            continue
                        key = user_handle.lower()
                        if key in seen_handles:
                            continue
                        seen_handles.add(key)
                        result.append({"handle": user_handle, "name": user_name})
                        new_found += 1
                    except Exception as exc:
                        logger.debug("解析用户单元格失败: %s", exc)

                no_new_streak = 0 if new_found > 0 else no_new_streak + 1
                if len(result) >= max_users:
                    break

                # 分批滚动加载更多
                for _ in range(batch_scrolls):
                    self._driver.execute_script("window.scrollBy(0, 800)")
                    time.sleep(0.4)
                time.sleep(batch_pause)
                logger.debug("已抓取 %d/%d，本批新增 %d", len(result), max_users, new_found)

            logger.info("@%s 关注列表抓取完毕，共 %d 人", handle, len(result))
            return result
        except WebDriverException as exc:
            logger.error("抓取 @%s 关注列表失败: %s", handle, exc)
            return []

    def _extract_user_from_cell(self, cell) -> tuple:
        """从 UserCell DOM 中提取 (handle, display_name)，失败返回 ('', '')。"""
        links = cell.find_elements(By.CSS_SELECTOR, 'a[role="link"]')
        for link in links:
            href = (link.get_attribute("href") or "").strip()
            for domain in ("https://x.com", "https://twitter.com", "http://x.com"):
                href = href.replace(domain, "")
            path = href.strip("/")
            # 排除非用户路径（含 / 或特殊前缀）
            if not path or "/" in path or path.lower().startswith("i/"):
                continue
            user_handle = path
            user_name = user_handle
            try:
                name_spans = cell.find_elements(
                    By.CSS_SELECTOR, 'div[dir="ltr"] > span'
                )
                if name_spans:
                    user_name = name_spans[0].text or user_handle
            except Exception:
                pass
            return user_handle, user_name
        return "", ""

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

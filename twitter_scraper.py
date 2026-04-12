"""
twitter_scraper.py
使用 Microsoft Edge (Selenium) 登录 X/Twitter 并抓取指定用户的最新推文。
注意：使用独立的 Edge profile 目录，与 LinkedIn scraper 互不干扰。
"""

import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any, List, Optional

from selenium import webdriver
from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.edge.options import Options as EdgeOptions
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

logger = logging.getLogger(__name__)

_BUTTON_SELECTOR = 'div[role="button"]'


@dataclass
class Tweet:
    tweet_id: str
    author_handle: str
    content: str
    timestamp: str
    url: str


class TwitterScraper:
    """使用 Selenium Edge 登录 X/Twitter 并抓取用户推文。"""

    _LOGIN_URL = "https://x.com/i/flow/login"

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

    # ------------------------------------------------------------------ #
    # 驱动初始化
    # ------------------------------------------------------------------ #

    def _init_driver(self) -> None:
        opts = EdgeOptions()
        if self.headless:
            opts.add_argument("--headless=new")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument(f"--user-data-dir={self.session_profile_dir}")
        opts.add_argument("--lang=en-US,en;q=0.9")
        opts.add_argument("--window-size=1280,900")
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        opts.add_experimental_option("useAutomationExtension", False)

        self._driver = webdriver.Edge(options=opts)
        self._driver.implicitly_wait(3)
        self._wait = WebDriverWait(self._driver, 25)
        logger.info(
            "Twitter Edge 已启动（无头: %s，profile: %s）",
            self.headless,
            self.session_profile_dir,
        )

    # ------------------------------------------------------------------ #
    # 登录
    # ------------------------------------------------------------------ #

    def login(self) -> bool:
        """登录 X/Twitter，已有会话时直接跳过。成功返回 True。"""
        logger.info("正在登录 X/Twitter …")
        try:
            identifier = (self.email or self.username).strip()
            if not identifier or not self.password:
                logger.error("Twitter 登录信息不完整")
                return False

            self._driver.get(self._LOGIN_URL)
            time.sleep(3)

            if self._is_logged_in():
                logger.info("检测到已有 Twitter 会话，跳过登录")
                return True

            identifier_input = self._wait_for_input([
                'input[autocomplete="username"]',
                'input[name="text"]',
                'input[inputmode="text"]',
            ])
            if not identifier_input:
                logger.error("未找到 Twitter 登录账号输入框")
                return False

            identifier_input.clear()
            identifier_input.send_keys(identifier)
            time.sleep(0.8)
            self._click_action_button(
                selector_candidates=[
                    '[data-testid="LoginForm_Login_Button"]',
                    '[data-testid="ocfEnterTextNextButton"]',
                    _BUTTON_SELECTOR,
                    'button',
                ],
                text_candidates=["Next", "下一步", "继续", "Log in", "登录"],
            )
            time.sleep(2.5)

            extra = self._find_optional_input([
                'input[data-testid="ocfEnterTextTextInput"]',
                'input[name="text"]',
            ])
            if extra and self.username:
                extra.clear()
                extra.send_keys(self.username)
                self._click_action_button(
                    selector_candidates=[
                        '[data-testid="ocfEnterTextNextButton"]',
                        _BUTTON_SELECTOR,
                        'button',
                    ],
                    text_candidates=["Next", "下一步", "继续"],
                )
                time.sleep(2)

            pwd_input = self._wait_for_input([
                'input[name="password"]',
                'input[type="password"]',
            ])
            if not pwd_input:
                logger.error("未找到 Twitter 密码输入框")
                return False

            pwd_input.clear()
            pwd_input.send_keys(self.password)
            time.sleep(0.8)
            self._click_action_button(
                selector_candidates=[
                    '[data-testid="LoginForm_Login_Button"]',
                    _BUTTON_SELECTOR,
                    'button',
                ],
                text_candidates=["Log in", "登录", "Sign in"],
            )
            time.sleep(4)

            if self._is_logged_in():
                logger.info("Twitter 登录成功")
                return True

            # 等待人机验证
            logger.warning(
                "Twitter 登录后未检测到主页，等待 %d 分钟手动处理验证 …",
                self.challenge_wait_minutes,
            )
            deadline = time.time() + self.challenge_wait_minutes * 60
            while time.time() < deadline:
                time.sleep(5)
                if self._is_logged_in():
                    logger.info("验证通过，Twitter 登录成功")
                    return True

            logger.error("Twitter 验证超时")
            return False

        except TimeoutException:
            logger.error("Twitter 登录超时，请检查网络或账号")
            return False
        except WebDriverException as exc:
            logger.error("Twitter 登录异常: %s", exc)
            return False

    def _is_logged_in(self) -> bool:
        try:
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

    def _wait_for_input(self, selectors: List[str], timeout: int = 25):
        deadline = time.time() + timeout
        while time.time() < deadline:
            element = self._find_optional_input(selectors)
            if element:
                return element
            time.sleep(0.5)
        return None

    def _find_optional_input(self, selectors: List[str]):
        for selector in selectors:
            try:
                elements = self._driver.find_elements(By.CSS_SELECTOR, selector)
                for element in elements:
                    if element.is_displayed():
                        return element
            except Exception:
                continue
        return None

    def _click_action_button(
        self,
        selector_candidates: List[str],
        text_candidates: List[str],
    ) -> None:
        lowered = {text.lower() for text in text_candidates}

        if self._click_by_selector_text(selector_candidates, lowered):
            return
        if self._click_by_xpath_text(text_candidates):
            return

        logger.warning("未找到可点击按钮: %s", ", ".join(text_candidates))

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
                text = (element.text or "").strip().lower()
                if not text or text not in lowered_text_candidates:
                    continue
                if not element.is_displayed():
                    continue
                self._driver.execute_script("arguments[0].click();", element)
                return True

        return False

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
        logger.info("Twitter 浏览器已关闭")

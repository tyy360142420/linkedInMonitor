"""
notifier.py
通过 Gmail SMTP（应用专用密码）向指定邮箱发送 LinkedIn 新动态通知。
"""

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import List

from scraper import Post

logger = logging.getLogger(__name__)


class EmailNotifier:
    """封装 Gmail SMTP 发信逻辑。"""

    _SMTP_HOST = "smtp.gmail.com"
    _SMTP_PORT = 465  # SSL

    def __init__(self, sender: str, app_password: str, recipient: str) -> None:
        self.sender = sender
        self.app_password = app_password
        self.recipient = recipient

    def send_notification(self, new_posts: List[Post], profile_url: str) -> bool:
        """
        发送包含新帖子详情的通知邮件。
        成功返回 True，失败返回 False。
        """
        count = len(new_posts)
        subject = f"[LinkedIn 动态] 发现 {count} 条新帖子"

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = self.sender
        msg["To"] = self.recipient

        text_body = self._build_text(new_posts, profile_url)
        html_body = self._build_html(new_posts, profile_url)

        msg.attach(MIMEText(text_body, "plain", "utf-8"))
        msg.attach(MIMEText(html_body, "html", "utf-8"))

        try:
            with smtplib.SMTP_SSL(self._SMTP_HOST, self._SMTP_PORT) as server:
                server.login(self.sender, self.app_password)
                server.sendmail(self.sender, self.recipient, msg.as_bytes())
            logger.info("通知邮件已发送至 %s", self.recipient)
            return True
        except smtplib.SMTPAuthenticationError:
            logger.error("Gmail 认证失败，请确认已使用「应用专用密码」而非登录密码")
            return False
        except smtplib.SMTPException as exc:
            logger.error("发送邮件失败: %s", exc)
            return False

    # ------------------------------------------------------------------ #
    # Templates
    # ------------------------------------------------------------------ #

    @staticmethod
    def _build_text(posts: List[Post], profile_url: str) -> str:
        lines = [
            "LinkedIn 新动态通知",
            f"监控主页: {profile_url}",
            f"新帖子数量: {len(posts)}",
            "=" * 50,
        ]
        for i, p in enumerate(posts, 1):
            lines += [
                f"\n【帖子 #{i}】",
                f"内容: {p.content or '（无法获取正文）'}",
                f"时间: {p.timestamp or '未知'}",
                f"链接: {p.url or '（无链接）'}",
            ]
        return "\n".join(lines)

    @staticmethod
    def _build_html(posts: List[Post], profile_url: str) -> str:
        cards = []
        for i, p in enumerate(posts, 1):
            link_html = (
                f'<a href="{p.url}" target="_blank">查看原帖 →</a>'
                if p.url
                else "<span>（无链接）</span>"
            )
            content_html = p.content.replace("\n", "<br>") if p.content else "<em>（无法获取正文）</em>"
            cards.append(
                f"""
                <div style="border:1px solid #e0e0e0;border-radius:8px;
                            padding:16px;margin:12px 0;background:#fafafa;">
                  <h3 style="margin-top:0;color:#0a66c2;">帖子 #{i}</h3>
                  <p style="color:#333;line-height:1.6;">{content_html}</p>
                  <p style="color:#888;font-size:0.85em;">发布时间：{p.timestamp or "未知"}</p>
                  <p>{link_html}</p>
                </div>
                """
            )

        return f"""
        <!DOCTYPE html>
        <html lang="zh">
        <head><meta charset="utf-8"><title>LinkedIn 新动态</title></head>
        <body style="font-family:Arial,sans-serif;max-width:700px;margin:auto;padding:20px;">
          <h2 style="color:#0a66c2;">🔔 LinkedIn 新动态通知</h2>
          <p>监控主页：<a href="{profile_url}">{profile_url}</a></p>
          <p>共发现 <strong>{len(posts)}</strong> 条新帖子：</p>
          {"".join(cards)}
          <hr>
          <p style="color:#aaa;font-size:0.8em;">此邮件由 LinkedIn 动态追踪程序自动发送。</p>
        </body>
        </html>
        """

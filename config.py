import os
from dotenv import load_dotenv
from paths import APP_DIR

# 从 .exe / 脚本所在目录读取 .env
load_dotenv(os.path.join(APP_DIR, ".env"))

LINKEDIN_EMAIL: str = os.getenv("LINKEDIN_EMAIL", "")
LINKEDIN_PASSWORD: str = os.getenv("LINKEDIN_PASSWORD", "")
TARGET_PROFILE_URL: str = os.getenv("TARGET_PROFILE_URL", "")

NOTIFY_EMAIL_SENDER: str = os.getenv("NOTIFY_EMAIL_SENDER", "")
NOTIFY_EMAIL_APP_PASSWORD: str = os.getenv("NOTIFY_EMAIL_APP_PASSWORD", "")
NOTIFY_EMAIL_RECIPIENT: str = os.getenv("NOTIFY_EMAIL_RECIPIENT", "")

CHECK_INTERVAL_MINUTES: int = int(os.getenv("CHECK_INTERVAL_MINUTES", "60"))
HEADLESS: bool = os.getenv("HEADLESS", "false").strip().lower() == "true"
SECURITY_CHALLENGE_WAIT_MINUTES: int = int(
    os.getenv("SECURITY_CHALLENGE_WAIT_MINUTES", "8")
)
SESSION_PROFILE_DIR: str = os.getenv(
    "SESSION_PROFILE_DIR",
    os.path.join(APP_DIR, ".edge_profile"),
)


def validate() -> None:
    """校验必填配置项，缺失时抛出 ValueError。"""
    required = {
        "LINKEDIN_EMAIL": LINKEDIN_EMAIL,
        "LINKEDIN_PASSWORD": LINKEDIN_PASSWORD,
        "TARGET_PROFILE_URL": TARGET_PROFILE_URL,
        "NOTIFY_EMAIL_SENDER": NOTIFY_EMAIL_SENDER,
        "NOTIFY_EMAIL_APP_PASSWORD": NOTIFY_EMAIL_APP_PASSWORD,
        "NOTIFY_EMAIL_RECIPIENT": NOTIFY_EMAIL_RECIPIENT,
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        raise ValueError(
            f"缺少必填配置项: {', '.join(missing)}\n"
            "请将 .env.example 复制为 .env 并填写相应信息。"
        )

"""config.py
从 config.json 加载 / 保存配置，提供与原 .env 版本相同的模块级变量名，
同时支持通过 Web UI 动态读写。
"""
import json
import os
from typing import Dict, List, Optional

from paths import APP_DIR

CONFIG_FILE: str = os.path.join(APP_DIR, "config.json")

_DEFAULTS: Dict[str, object] = {
    # LinkedIn
    "LINKEDIN_EMAIL": "",
    "LINKEDIN_PASSWORD": "",
    "TARGET_PROFILE_URL": "",
    "TARGET_PROFILE_NAME": "",          # 监控目标的显示名称（如留空则用 URL 末段）
    "NOTIFY_EMAIL_SENDER": "",
    "NOTIFY_EMAIL_APP_PASSWORD": "",
    "NOTIFY_EMAIL_RECIPIENT": "",
    "CHECK_INTERVAL_MINUTES": 60,
    "HEADLESS": False,
    "SECURITY_CHALLENGE_WAIT_MINUTES": 8,
    "SESSION_PROFILE_DIR": os.path.join(APP_DIR, ".edge_profile"),
    "LINKEDIN_ACCOUNTS": [],            # 多账户: [{"profile_url": "...", "name": "...", "check_enabled": bool}]
    # Twitter / X
    "TWITTER_EMAIL": "",
    "TWITTER_USERNAME": "",             # @ 用户名（不含 @）
    "TWITTER_PASSWORD": "",
    "TWITTER_HEADLESS": False,
    "TWITTER_CHECK_INTERVAL_MINUTES": 60,
    "TWITTER_CHALLENGE_WAIT_MINUTES": 5,
    "TWITTER_SESSION_PROFILE_DIR": os.path.join(APP_DIR, ".edge_profile_twitter"),
}

_REQUIRED: List[str] = [
    "LINKEDIN_EMAIL",
    "LINKEDIN_PASSWORD",
    "TARGET_PROFILE_URL",
    "NOTIFY_EMAIL_SENDER",
    "NOTIFY_EMAIL_APP_PASSWORD",
    "NOTIFY_EMAIL_RECIPIENT",
]


def get_all() -> Dict[str, object]:
    """读取 config.json，合并默认值后返回完整配置字典。"""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            return {**_DEFAULTS, **saved}
        except (json.JSONDecodeError, IOError):
            pass
    return dict(_DEFAULTS)


def save(data: Dict[str, object]) -> None:
    """将 data 合并到现有配置并写入 config.json。"""
    current = get_all()
    current.update(data)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(current, f, ensure_ascii=False, indent=2)


def validate(cfg: Optional[Dict[str, object]] = None) -> List[str]:
    """返回缺失的必填字段列表（空列表表示校验通过）。"""
    if cfg is None:
        cfg = get_all()
    return [k for k in _REQUIRED if not cfg.get(k)]


# ------------------------------------------------------------------ #
# 模块级变量（兼容旧版 main.py 的直接属性访问方式）
# 注意：这些值在模块首次导入时固定，修改 config.json 后需重启进程。
# ------------------------------------------------------------------ #
_cfg = get_all()

LINKEDIN_EMAIL: str = str(_cfg["LINKEDIN_EMAIL"])
LINKEDIN_PASSWORD: str = str(_cfg["LINKEDIN_PASSWORD"])
TARGET_PROFILE_URL: str = str(_cfg["TARGET_PROFILE_URL"])
NOTIFY_EMAIL_SENDER: str = str(_cfg["NOTIFY_EMAIL_SENDER"])
NOTIFY_EMAIL_APP_PASSWORD: str = str(_cfg["NOTIFY_EMAIL_APP_PASSWORD"])
NOTIFY_EMAIL_RECIPIENT: str = str(_cfg["NOTIFY_EMAIL_RECIPIENT"])
CHECK_INTERVAL_MINUTES: int = int(_cfg["CHECK_INTERVAL_MINUTES"])
HEADLESS: bool = bool(_cfg["HEADLESS"])
SECURITY_CHALLENGE_WAIT_MINUTES: int = int(_cfg["SECURITY_CHALLENGE_WAIT_MINUTES"])
SESSION_PROFILE_DIR: str = str(_cfg["SESSION_PROFILE_DIR"])


# ------------------------------------------------------------------ #
# LinkedIn 多账户管理辅助函数
# ------------------------------------------------------------------ #

def get_linkedin_accounts() -> List[Dict]:
    """获取所有 LinkedIn 监控账户列表。"""
    cfg = get_all()
    accounts = cfg.get("LINKEDIN_ACCOUNTS", [])
    if not isinstance(accounts, list):
        return []
    return accounts


def add_linkedin_account(profile_url: str, display_name: str = "", check_enabled: bool = True) -> bool:
    """添加一个新的 LinkedIn 监控账户。返回成功否。"""
    if not profile_url.strip():
        return False
    
    accounts = get_linkedin_accounts()
    # 检查重复
    if any(a.get("profile_url") == profile_url for a in accounts):
        return False
    
    accounts.append({
        "profile_url": profile_url.strip(),
        "name": display_name.strip() or profile_url.split("/")[-1],
        "check_enabled": bool(check_enabled),
    })
    save({"LINKEDIN_ACCOUNTS": accounts})
    return True


def remove_linkedin_account(profile_url: str) -> bool:
    """删除一个 LinkedIn 监控账户。"""
    accounts = get_linkedin_accounts()
    original_len = len(accounts)
    accounts = [a for a in accounts if a.get("profile_url") != profile_url]
    if len(accounts) < original_len:
        save({"LINKEDIN_ACCOUNTS": accounts})
        return True
    return False


def update_linkedin_account(profile_url: str, display_name: str = "", check_enabled: bool = True) -> bool:
    """更新一个 LinkedIn 监控账户的信息。"""
    accounts = get_linkedin_accounts()
    for account in accounts:
        if account.get("profile_url") == profile_url:
            account["name"] = display_name.strip() or account.get("name", "")
            account["check_enabled"] = bool(check_enabled)
            save({"LINKEDIN_ACCOUNTS": accounts})
            return True
    return False


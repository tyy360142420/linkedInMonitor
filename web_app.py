"""
web_app.py
Flask Web 应用入口：LinkedIn + Twitter 双平台追踪、股票看板、笔记系统。

启动方式:
    python web_app.py
然后在浏览器中访问 http://localhost:5001
"""

import logging
import os
import secrets

from flask import Flask, jsonify, redirect, render_template, request, url_for, flash

import config as cfg_module
import database as db
import stock_data as sd
from paths import APP_DIR
from storage import save_seen_posts
from tracker_runner import TrackerRunner
from twitter_runner import TwitterRunner

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

# ------------------------------------------------------------------ #
# 数据库初始化
# ------------------------------------------------------------------ #
db.init_db()

# ------------------------------------------------------------------ #
# Flask 应用初始化
# ------------------------------------------------------------------ #
app = Flask(__name__)

# 持久化 secret_key，防止重启后 session 失效
_secret_key_file = os.path.join(APP_DIR, ".flask_secret")
if os.path.exists(_secret_key_file):
    with open(_secret_key_file, "r") as _f:
        app.secret_key = _f.read().strip()
else:
    _generated_key = secrets.token_hex(32)
    with open(_secret_key_file, "w") as _f:
        _f.write(_generated_key)
    app.secret_key = _generated_key

# 全局追踪器实例（整个进程生命周期唯一）
tracker = TrackerRunner()
twitter_tracker = TwitterRunner()


# ------------------------------------------------------------------ #
# Jinja2 自定义过滤器
# ------------------------------------------------------------------ #
@app.template_filter("extract_tickers_filter")
def extract_tickers_filter(text: str):
    """供模板调用：从帖子内容中提取股票代码列表。"""
    return sd.extract_tickers(text or "")


def _build_publisher_performance(publisher_id: int) -> dict:
    """
    计算发布人的推荐表现统计：
    - 每个 ticker 从首次提及日至今的涨跌幅
    - 上涨股票占比
    - 平均涨跌幅
    """
    recs = db.get_recommendations_for_publisher(publisher_id)
    rows = []
    valid_rows = []

    for rec in recs:
        perf = sd.get_change_since_date(rec["ticker"], rec["first_mentioned"])
        merged = {**rec, **perf}
        rows.append(merged)
        if merged.get("change_pct") is not None:
            valid_rows.append(merged)

    total_recommended = len(recs)
    total_evaluable = len(valid_rows)
    up_count = sum(1 for r in valid_rows if r.get("is_up") is True)
    up_ratio_all = (
        round(up_count / total_recommended * 100, 2)
        if total_recommended
        else None
    )
    avg_change = (
        round(sum(r["change_pct"] for r in valid_rows) / total_evaluable, 2)
        if total_evaluable
        else None
    )

    return {
        "items": rows,
        "total_recommended": total_recommended,
        "total_evaluable": total_evaluable,
        "up_count": up_count,
        "up_ratio": up_ratio_all,
        "avg_change": avg_change,
    }


# ================================================================== #


@app.route("/", methods=["GET"])
def index():
    linkedin_state = tracker.get_state()
    twitter_state = twitter_tracker.get_state()
    cfg = cfg_module.get_all()
    return render_template("index.html", linkedin_state=linkedin_state, twitter_state=twitter_state, cfg=cfg)


@app.route("/config", methods=["GET", "POST"])
def config_page():
    if request.method == "POST":
        headless_val = request.form.get("HEADLESS") == "on"
        tw_headless_val = request.form.get("TWITTER_HEADLESS") == "on"
        data = {
            "LINKEDIN_EMAIL": request.form.get("LINKEDIN_EMAIL", "").strip(),
            "LINKEDIN_PASSWORD": request.form.get("LINKEDIN_PASSWORD", "").strip(),
            "TARGET_PROFILE_URL": request.form.get("TARGET_PROFILE_URL", "").strip(),
            "TARGET_PROFILE_NAME": request.form.get("TARGET_PROFILE_NAME", "").strip(),
            "NOTIFY_EMAIL_SENDER": request.form.get("NOTIFY_EMAIL_SENDER", "").strip(),
            "NOTIFY_EMAIL_APP_PASSWORD": request.form.get(
                "NOTIFY_EMAIL_APP_PASSWORD", ""
            ).strip(),
            "NOTIFY_EMAIL_RECIPIENT": request.form.get(
                "NOTIFY_EMAIL_RECIPIENT", ""
            ).strip(),
            "CHECK_INTERVAL_MINUTES": max(
                1, int(request.form.get("CHECK_INTERVAL_MINUTES", 60) or 60)
            ),
            "LINKEDIN_LOOKBACK_DAYS": max(
                1, int(request.form.get("LINKEDIN_LOOKBACK_DAYS", 30) or 30)
            ),
            "HEADLESS": headless_val,
            "SECURITY_CHALLENGE_WAIT_MINUTES": max(
                1,
                int(
                    request.form.get("SECURITY_CHALLENGE_WAIT_MINUTES", 8) or 8
                ),
            ),
            "TWITTER_EMAIL": request.form.get("TWITTER_EMAIL", "").strip(),
            "TWITTER_USERNAME": request.form.get("TWITTER_USERNAME", "").strip().lstrip("@"),
            "TWITTER_PASSWORD": request.form.get("TWITTER_PASSWORD", "").strip(),
            "TWITTER_CHECK_INTERVAL_MINUTES": max(
                1, int(request.form.get("TWITTER_CHECK_INTERVAL_MINUTES", 60) or 60)
            ),
            "TWITTER_LOOKBACK_DAYS": max(
                1, int(request.form.get("TWITTER_LOOKBACK_DAYS", 30) or 30)
            ),
            "TWITTER_CHALLENGE_WAIT_MINUTES": max(
                1,
                int(request.form.get("TWITTER_CHALLENGE_WAIT_MINUTES", 5) or 5),
            ),
            "TWITTER_HEADLESS": tw_headless_val,
        }
        cfg_module.save(data)
        flash("配置已保存！如追踪器正在运行，请重启以使新配置生效。", "success")
        return redirect(url_for("config_page"))

    cfg = cfg_module.get_all()
    return render_template("config.html", cfg=cfg)


# ================================================================== #
# API 端点（供前端 JavaScript 调用）
# ================================================================== #


@app.route("/api/start", methods=["POST"])
def api_start():
    cfg = cfg_module.get_all()
    missing = cfg_module.validate(cfg)
    if missing:
        return (
            jsonify({"ok": False, "error": f"请先填写必填配置项: {', '.join(missing)}"}),
            400,
        )
    if tracker.is_running():
        return jsonify({"ok": False, "error": "追踪器已在运行中"}), 400
    tracker.start()
    logger.info("追踪器已通过 Web UI 启动")
    return jsonify({"ok": True, "message": "追踪器已启动"})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    if not tracker.is_running():
        return jsonify({"ok": False, "error": "追踪器未在运行"}), 400
    tracker.stop()
    logger.info("追踪器已通过 Web UI 停止")
    return jsonify({"ok": True, "message": "追踪器已停止"})


@app.route("/api/status", methods=["GET"])
def api_status():
    return jsonify(tracker.get_state())


@app.route("/api/logs", methods=["GET"])
def api_logs():
    log_file = os.path.join(APP_DIR, "tracker.log")
    lines = []
    if os.path.exists(log_file):
        try:
            with open(log_file, "r", encoding="utf-8") as f:
                lines = f.readlines()[-150:]
        except IOError:
            pass
    return jsonify({"lines": [ln.rstrip() for ln in reversed(lines)]})


@app.route("/api/clear-seen", methods=["POST"])
def api_clear_seen():
    save_seen_posts(set())
    tracker.posts_found.clear()
    logger.info("已清空已见帖子记录")
    return jsonify({"ok": True, "message": "已清空已见帖子记录，下次检查将重新标记所有帖子"})


# ================================================================== #
# LinkedIn 账户管理
# ================================================================== #

@app.route("/linkedin-accounts", methods=["GET"])
def linkedin_accounts_page():
    """LinkedIn 多账户管理页面。"""
    accounts = cfg_module.get_linkedin_accounts()
    cfg = cfg_module.get_all()
    return render_template(
        "linkedin_accounts.html",
        accounts=accounts,
        cfg=cfg,
    )


@app.route("/api/linkedin-accounts", methods=["GET"])
def api_get_linkedin_accounts():
    """获取所有 LinkedIn 监控账户。"""
    accounts = cfg_module.get_linkedin_accounts()
    return jsonify({"ok": True, "accounts": accounts})


@app.route("/api/linkedin-accounts", methods=["POST"])
def api_add_linkedin_account():
    """添加新的 LinkedIn 监控账户。"""
    data = request.get_json(force=True) or {}
    profile_url = str(data.get("profile_url", "")).strip()
    display_name = str(data.get("display_name", "")).strip()
    
    if not profile_url:
        return jsonify({"ok": False, "error": "profile_url 不能为空"}), 400
    
    if cfg_module.add_linkedin_account(profile_url, display_name):
        logger.info("已添加 LinkedIn 监控账户: %s", profile_url)
        return jsonify({"ok": True, "message": "账户已添加"})
    else:
        return jsonify({"ok": False, "error": "账户已存在或格式错误"}), 400


@app.route("/api/linkedin-accounts/<path:profile_url>", methods=["PUT"])
def api_update_linkedin_account(profile_url: str):
    """更新 LinkedIn 监控账户信息。"""
    data = request.get_json(force=True) or {}
    display_name = str(data.get("display_name", "")).strip()
    check_enabled = bool(data.get("check_enabled", True))
    
    if cfg_module.update_linkedin_account(profile_url, display_name, check_enabled):
        logger.info("已更新 LinkedIn 监控账户: %s", profile_url)
        return jsonify({"ok": True, "message": "账户已更新"})
    else:
        return jsonify({"ok": False, "error": "账户不存在"}), 404


@app.route("/api/linkedin-accounts/<path:profile_url>", methods=["DELETE"])
def api_remove_linkedin_account(profile_url: str):
    """删除一个 LinkedIn 监控账户。"""
    if cfg_module.remove_linkedin_account(profile_url):
        logger.info("已删除 LinkedIn 监控账户: %s", profile_url)
        return jsonify({"ok": True, "message": "账户已删除"})
    else:
        return jsonify({"ok": False, "error": "账户不存在"}), 404


# ================================================================== #
# Twitter 路由
# ================================================================== #

@app.route("/twitter", methods=["GET"])
def twitter_page():
    state = twitter_tracker.get_state()
    cfg = cfg_module.get_all()
    accounts = [p for p in db.get_publishers() if p["platform"] == "twitter"]
    return render_template("twitter.html", state=state, cfg=cfg, accounts=accounts)


@app.route("/api/twitter/start", methods=["POST"])
def api_twitter_start():
    cfg = cfg_module.get_all()
    if not (cfg.get("TWITTER_EMAIL") or cfg.get("TWITTER_USERNAME")):
        return jsonify({"ok": False, "error": "请先在配置页填写 Twitter 登录邮箱或用户名"}), 400
    if twitter_tracker.is_running():
        return jsonify({"ok": False, "error": "Twitter 追踪器已在运行中"}), 400
    if not twitter_tracker.start():
        return jsonify({"ok": False, "error": twitter_tracker.last_error or "Twitter 追踪器启动失败"}), 400
    logger.info("Twitter 追踪器已通过 Web UI 启动")
    return jsonify({"ok": True, "message": "Twitter 追踪器已启动"})


@app.route("/api/twitter/stop", methods=["POST"])
def api_twitter_stop():
    if not twitter_tracker.is_running():
        return jsonify({"ok": False, "error": "Twitter 追踪器未在运行"}), 400
    twitter_tracker.stop()
    logger.info("Twitter 追踪器已通过 Web UI 停止")
    return jsonify({"ok": True, "message": "Twitter 追踪器已停止"})


@app.route("/api/twitter/status", methods=["GET"])
def api_twitter_status():
    return jsonify(twitter_tracker.get_state())


# ================================================================== #
# 发布人管理
# ================================================================== #

@app.route("/publishers", methods=["GET"])
def publishers_page():
    publishers = db.get_publishers()
    for pub in publishers:
        perf = _build_publisher_performance(pub["id"])
        pub["perf_total"] = perf["total_recommended"]
        pub["perf_up_count"] = perf["up_count"]
        pub["perf_up_ratio"] = perf["up_ratio"]
        pub["perf_avg_change"] = perf["avg_change"]
    return render_template("publishers.html", publishers=publishers)


@app.route("/publisher/<int:pub_id>", methods=["GET"])
def publisher_detail(pub_id: int):
    publisher = db.get_publisher(pub_id)
    if not publisher:
        return "发布人不存在", 404
    posts = db.get_posts_for_publisher(pub_id, limit=50)
    stocks = db.get_stocks_for_publisher(pub_id)
    perf = _build_publisher_performance(pub_id)
    perf_by_ticker = {r["ticker"]: r for r in perf["items"]}

    for s in stocks:
        p = perf_by_ticker.get(s["ticker"], {})
        s["change_pct"] = p.get("change_pct")
        s["is_up"] = p.get("is_up")
        s["first_mentioned"] = p.get("first_mentioned")

    notes_by_ticker = {
        s["ticker"]: db.get_notes(s["ticker"]) for s in stocks
    }
    return render_template(
        "publisher_detail.html",
        publisher=publisher,
        posts=posts,
        stocks=stocks,
        notes_by_ticker=notes_by_ticker,
        perf_summary=perf,
    )


@app.route("/api/publishers", methods=["POST"])
def api_add_publisher():
    data = request.get_json(force=True) or {}
    platform = str(data.get("platform", "twitter"))
    handle = str(data.get("handle", "")).strip().lstrip("@")
    name = str(data.get("name", "")).strip() or f"@{handle}"
    if not handle:
        return jsonify({"ok": False, "error": "handle 不能为空"}), 400
    profile_url = str(data.get("profile_url", f"https://x.com/{handle}"))
    pub_id = db.upsert_publisher(platform, name, handle, profile_url)
    logger.info("已添加发布人: @%s (id=%s)", handle, pub_id)
    return jsonify({"ok": True, "id": pub_id, "message": f"已添加 @{handle}"})


@app.route("/api/publishers/<int:pub_id>", methods=["DELETE"])
def api_delete_publisher(pub_id: int):
    pub = db.get_publisher(pub_id)
    if not pub:
        return jsonify({"ok": False, "error": "发布人不存在"}), 404
    if pub["platform"] == "linkedin":
        return jsonify({"ok": False, "error": "LinkedIn 追踪目标请在配置页修改，不支持从此处删除"}), 400
    db.delete_publisher(pub_id)
    logger.info("已删除发布人 id=%s", pub_id)
    return jsonify({"ok": True, "message": "已删除"})


# ================================================================== #
# 股票路由
# ================================================================== #

@app.route("/stocks", methods=["GET"])
def stocks_page():
    all_stocks = db.get_all_stocks()
    return render_template("stocks.html", stocks=all_stocks)


@app.route("/stock/<ticker>", methods=["GET"])
def stock_detail(ticker: str):
    ticker = ticker.upper()
    publishers = db.get_publishers_for_ticker(ticker)
    posts = db.get_posts_for_ticker(ticker, limit=30)
    notes = db.get_notes(ticker)
    return render_template(
        "stock_detail.html",
        ticker=ticker,
        publishers=publishers,
        posts=posts,
        notes=notes,
    )


@app.route("/api/stock/<ticker>/history", methods=["GET"])
def api_stock_history(ticker: str):
    period = request.args.get("period", "3mo")
    if period not in ("1mo", "3mo", "6mo", "1y"):
        period = "3mo"
    data = sd.get_price_history(ticker.upper(), period)
    include_news = request.args.get("include_news", "1") != "0"
    if include_news:
        news = sd.get_stock_news(ticker.upper(), limit=20)
        data["news"] = news
        data["news_summary"] = sd.summarize_news_impacts(news)
    else:
        data["news"] = []
        data["news_summary"] = None
    return jsonify(data)


@app.route("/api/stock/<ticker>/news", methods=["GET"])
def api_stock_news(ticker: str):
    try:
        limit = max(1, min(20, int(request.args.get("limit", 5))))
    except ValueError:
        limit = 5
    try:
        offset = max(0, int(request.args.get("offset", 0)))
    except ValueError:
        offset = 0

    # 按 offset + limit 拉取后再切片，前端可分批加载。
    all_items = sd.get_stock_news(ticker.upper(), limit=offset + limit)
    items = all_items[offset : offset + limit]
    has_more = len(all_items) > offset + limit

    return jsonify(
        {
            "items": items,
            "offset": offset,
            "limit": limit,
            "has_more": has_more,
            "summary": sd.summarize_news_impacts(all_items),
        }
    )


# ================================================================== #
# 笔记 API
# ================================================================== #

@app.route("/api/notes", methods=["GET"])
def api_get_notes():
    ticker = request.args.get("ticker", "").upper() or None
    return jsonify({"notes": db.get_notes(ticker)})


@app.route("/api/notes", methods=["POST"])
def api_create_note():
    data = request.get_json(force=True) or {}
    ticker = str(data.get("ticker", "")).strip().upper()
    title = str(data.get("title", "")).strip()
    content = str(data.get("content", "")).strip()
    if not ticker or not content:
        return jsonify({"ok": False, "error": "ticker 和 content 不能为空"}), 400
    note_id = db.create_note(ticker, title, content)
    return jsonify({"ok": True, "id": note_id})


@app.route("/api/notes/<int:note_id>", methods=["PUT"])
def api_update_note(note_id: int):
    data = request.get_json(force=True) or {}
    title = str(data.get("title", "")).strip()
    content = str(data.get("content", "")).strip()
    if not content:
        return jsonify({"ok": False, "error": "content 不能为空"}), 400
    db.update_note(note_id, title, content)
    return jsonify({"ok": True})


@app.route("/api/notes/<int:note_id>", methods=["DELETE"])
def api_delete_note(note_id: int):
    db.delete_note(note_id)
    return jsonify({"ok": True})



if __name__ == "__main__":
    host = os.getenv("WEB_HOST", "127.0.0.1")
    try:
        port = int(os.getenv("WEB_PORT", "5001"))
    except ValueError:
        port = 5001
    logger.info("LinkedIn 追踪器 Web 界面已启动，访问 http://localhost:%s", port)
    app.run(host=host, port=port, debug=False, threaded=True)

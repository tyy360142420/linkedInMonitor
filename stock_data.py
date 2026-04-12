"""
stock_data.py
两个职责：
  1. extract_tickers(text) — 从文本提取 $TICKER 格式的股票代码
  2. get_price_history(ticker, period) — 通过 yfinance 获取历史收盘价
"""

import re
import logging
from datetime import datetime, timedelta
from collections import Counter
from email.utils import parsedate_to_datetime
from typing import Dict, List

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ #
# Ticker 提取
# ------------------------------------------------------------------ #

_TICKER_RE = re.compile(r"\$([A-Z]{1,5})\b")

# 过滤掉绝对不是 ticker 的常见英文词
_STOP_WORDS = {
    "I", "A", "IT", "AT", "IN", "OR", "AND", "THE", "FOR",
    "BE", "WE", "DO", "GO", "ME", "MY", "NO", "SO", "TO",
    "US", "AM", "AN", "AS", "BY", "HE", "IF", "IS", "OF",
    "ON", "UP", "AI", "ALL", "ARE", "CAN", "GET", "HAS",
    "HIM", "HIS", "HOW", "ITS", "LET", "MAY", "NEW", "NOT",
    "NOW", "OUR", "OUT", "OWN", "SAY", "SEE", "SHE", "TOO",
    "TWO", "WAS", "WAY", "WHO", "WHY", "YOU",
}

_EVENT_RULES = [
    ("财报", ["earnings", "revenue", "eps", "guidance", "quarter", "财报", "业绩", "营收"]),
    ("评级", ["upgrade", "downgrade", "rating", "analyst", "target price", "评级", "目标价"]),
    ("并购", ["acquisition", "merger", "buyout", "takeover", "收购", "并购"]),
    ("产品", ["launch", "product", "release", "iphone", "ai", "产品", "发布"]),
    ("监管", ["sec", "lawsuit", "regulation", "fine", "investigation", "诉讼", "监管", "罚款"]),
    ("宏观", ["inflation", "fed", "interest rate", "cpi", "gdp", "利率", "通胀", "宏观"]),
]


def extract_tickers(text: str) -> List[str]:
    """从文本中提取 $XXX 格式的股票代码，去重并过滤停用词。"""
    if not text:
        return []
    seen: set = set()
    result: List[str] = []
    for match in _TICKER_RE.finditer(text.upper()):
        t = match.group(1)
        if t not in _STOP_WORDS and t not in seen:
            seen.add(t)
            result.append(t)
    return result


# ------------------------------------------------------------------ #
# 价格历史
# ------------------------------------------------------------------ #

def get_price_history(ticker: str, period: str = "3mo") -> Dict:
    """
    通过 yfinance 获取收盘价历史。

    返回结构:
    {
      "ticker": "AAPL",
      "name": "Apple Inc.",
      "currency": "USD",
      "current_price": 180.5,
      "change_pct": 1.2,       # 相对前一日涨跌幅 (%)
      "dates": ["2024-01-02", ...],
      "closes": [185.0, ...],
      "error": null             # 出错时为错误字符串
    }
    """
    ticker = ticker.upper()
    base: Dict = {
        "ticker": ticker,
        "name": ticker,
        "currency": "USD",
        "current_price": None,
        "change_pct": None,
        "dates": [],
        "closes": [],
        "error": None,
    }
    try:
        import yfinance as yf  # 延迟导入，避免启动时报错

        stock = yf.Ticker(ticker)
        hist = stock.history(period=period)

        if hist.empty:
            base["error"] = f"没有找到 {ticker} 的历史数据，请确认代码正确"
            return base

        base["dates"] = hist.index.strftime("%Y-%m-%d").tolist()
        base["closes"] = [round(float(c), 2) for c in hist["Close"].tolist()]

        # 尝试获取详细信息（可能因网络超时失败）
        try:
            info = stock.info or {}
        except Exception:
            info = {}

        base["name"] = info.get("longName") or info.get("shortName") or ticker
        base["currency"] = info.get("currency", "USD")

        cp = (
            info.get("currentPrice")
            or info.get("regularMarketPrice")
            or (base["closes"][-1] if base["closes"] else None)
        )
        base["current_price"] = cp

        pc = info.get("previousClose") or (
            base["closes"][-2] if len(base["closes"]) > 1 else None
        )
        if cp and pc and pc != 0:
            base["change_pct"] = round((cp - pc) / pc * 100, 2)

        return base

    except ImportError:
        base["error"] = "yfinance 未安装，请执行 pip install yfinance"
        return base
    except Exception as exc:
        logger.error("获取 %s 价格失败: %s", ticker, exc)
        base["error"] = str(exc)
        return base


def get_change_since_date(ticker: str, since_date: str) -> Dict:
    """
    计算股票自某日期（推荐日期）以来的涨跌幅。

    返回结构:
    {
      "ticker": "AAPL",
      "since_date": "2026-04-01",
      "start_price": 170.1,
      "current_price": 182.3,
      "change_pct": 7.17,
      "is_up": true,
      "error": null
    }
    """
    ticker = ticker.upper()
    out: Dict = {
        "ticker": ticker,
        "since_date": since_date,
        "start_price": None,
        "current_price": None,
        "change_pct": None,
        "is_up": None,
        "error": None,
    }

    try:
        import yfinance as yf

        # 仅保留 YYYY-MM-DD，避免数据库 datetime 字符串带时分秒
        since_str = (since_date or "")[:10]
        start_dt = datetime.strptime(since_str, "%Y-%m-%d")
        end_dt = datetime.now() + timedelta(days=1)

        hist = yf.Ticker(ticker).history(
            start=start_dt.strftime("%Y-%m-%d"),
            end=end_dt.strftime("%Y-%m-%d"),
        )

        if hist.empty:
            out["error"] = "无可用历史数据"
            return out

        closes = hist["Close"].dropna().tolist()
        if not closes:
            out["error"] = "无可用收盘价"
            return out

        start_price = float(closes[0])
        current_price = float(closes[-1])

        if start_price == 0:
            out["error"] = "起始价格为 0，无法计算涨跌幅"
            return out

        change_pct = round((current_price - start_price) / start_price * 100, 2)

        out["start_price"] = round(start_price, 2)
        out["current_price"] = round(current_price, 2)
        out["change_pct"] = change_pct
        out["is_up"] = change_pct > 0
        return out

    except ValueError:
        out["error"] = f"日期格式错误: {since_date}"
        return out
    except ImportError:
        out["error"] = "yfinance 未安装"
        return out
    except Exception as exc:
        logger.error("计算 %s 从 %s 以来涨跌幅失败: %s", ticker, since_date, exc)
        out["error"] = str(exc)
        return out


def _parse_news_datetime(item: Dict) -> datetime | None:
    """从 yfinance 新闻条目中解析发布时间。"""
    ts = item.get("providerPublishTime")
    if ts:
        try:
            return datetime.fromtimestamp(int(ts))
        except Exception:
            pass

    published = item.get("published")
    if published:
        try:
            return parsedate_to_datetime(str(published))
        except Exception:
            pass

    pub_date = item.get("pubDate")
    if pub_date:
        try:
            return parsedate_to_datetime(str(pub_date))
        except Exception:
            pass

    return None


def _normalize_news_item(item: Dict) -> Dict | None:
    """将原始新闻转换为标准结构，失败返回 None。"""
    title = item.get("title") or ""
    if not title:
        return None

    dt = _parse_news_datetime(item)
    if dt is None:
        return None

    link = item.get("link") or item.get("url") or ""
    source = item.get("publisher") or item.get("source") or "未知来源"

    return {
        "title": title,
        "url": link,
        "source": source,
        "published_at": dt.strftime("%Y-%m-%d %H:%M:%S"),
        "date": dt.strftime("%Y-%m-%d"),
        "event_type": classify_event_type(title),
    }


def classify_event_type(title: str) -> str:
    """根据标题关键词对新闻事件分类。"""
    t = (title or "").lower()
    for label, keywords in _EVENT_RULES:
        if any(k in t for k in keywords):
            return label
    return "其他"


def _first_close_on_or_after(rows: List[tuple], target_date: datetime.date):
    """在 (date, close) 有序序列中找到目标日期及之后首个收盘价。"""
    for d, c in rows:
        if d >= target_date:
            return d, c
    return None


def _calc_news_impacts(ticker: str, news_items: List[Dict], horizon_days: int = 3) -> List[Dict]:
    """计算每条新闻在未来 horizon_days 交易日内的涨跌幅。"""
    if not news_items:
        return news_items

    try:
        import yfinance as yf

        min_date = min(n["date"] for n in news_items)
        start_dt = datetime.strptime(min_date, "%Y-%m-%d") - timedelta(days=7)
        end_dt = datetime.now() + timedelta(days=14)

        hist = yf.Ticker(ticker).history(
            start=start_dt.strftime("%Y-%m-%d"),
            end=end_dt.strftime("%Y-%m-%d"),
        )
        if hist.empty:
            return news_items

        rows = []
        for idx, close_val in hist["Close"].dropna().items():
            rows.append((idx.date(), float(close_val)))
        rows.sort(key=lambda x: x[0])

        for n in news_items:
            event_day = datetime.strptime(n["date"], "%Y-%m-%d").date()
            start_point = _first_close_on_or_after(rows, event_day)
            end_point = _first_close_on_or_after(
                rows, event_day + timedelta(days=horizon_days)
            )

            n["impact_window_days"] = horizon_days
            n["impact_pct"] = None
            n["impact_direction"] = "unknown"

            if not start_point or not end_point:
                continue

            _, p0 = start_point
            _, p1 = end_point
            if p0 == 0:
                continue

            impact = round((p1 - p0) / p0 * 100, 2)
            n["impact_pct"] = impact
            if impact > 0:
                n["impact_direction"] = "up"
            elif impact < 0:
                n["impact_direction"] = "down"
            else:
                n["impact_direction"] = "flat"

        return news_items

    except Exception as exc:
        logger.warning("计算 %s 新闻影响失败: %s", ticker, exc)
        return news_items


def summarize_news_impacts(news_items: List[Dict]) -> Dict:
    """汇总事件类型对上涨/下跌触发的统计。"""
    up_events = [n for n in news_items if n.get("impact_direction") == "up"]
    down_events = [n for n in news_items if n.get("impact_direction") == "down"]
    evaluable = [n for n in news_items if n.get("impact_pct") is not None]

    up_counter = Counter(n.get("event_type", "其他") for n in up_events)
    down_counter = Counter(n.get("event_type", "其他") for n in down_events)

    return {
        "evaluable_count": len(evaluable),
        "up_count": len(up_events),
        "down_count": len(down_events),
        "up_ratio": round(len(up_events) / len(evaluable) * 100, 2) if evaluable else None,
        "top_up_triggers": [
            {"event_type": k, "count": v} for k, v in up_counter.most_common(5)
        ],
        "top_down_triggers": [
            {"event_type": k, "count": v} for k, v in down_counter.most_common(5)
        ],
    }


def get_stock_news(ticker: str, limit: int = 12) -> List[Dict]:
    """获取股票简讯，输出标准化字段用于时间轴展示。"""
    ticker = ticker.upper()
    try:
        import yfinance as yf

        raw_items = yf.Ticker(ticker).news or []
        result = []
        for item in raw_items:
            normalized = _normalize_news_item(item)
            if normalized is not None:
                result.append(normalized)

        result = _calc_news_impacts(ticker, result, horizon_days=3)

        # 最新在前
        result.sort(key=lambda x: x["published_at"], reverse=True)
        return result[:limit]

    except Exception as exc:
        logger.warning("获取 %s 简讯失败: %s", ticker, exc)
        return []

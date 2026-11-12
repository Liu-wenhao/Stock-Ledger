"""A-share desk figures from AkShare. Failures leave a field empty."""

from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import requests

logger = logging.getLogger(__name__)

_CACHE_SECONDS = 600
_lock = threading.Lock()
_desk_cache: dict = {"at": 0.0, "payload": None}
_stock_cache: dict[str, tuple[float, dict]] = {}


def _num(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return number


def _yi(value):
    number = _num(value)
    if number is None:
        return None
    return round(number / 1e8, 2)


def _activity() -> dict:
    import akshare as ak

    try:
        frame = ak.stock_market_activity_legu()
        items = {
            str(row["item"]).strip(): str(row["value"]).strip()
            for _, row in frame.iterrows()
            if str(row.get("item") or "").strip()
        }
        return {
            "date": items.get("统计日期"),
            "up": _count(items.get("上涨")),
            "down": _count(items.get("下跌")),
            "limit_up": _count(items.get("涨停")),
            "limit_down": _count(items.get("跌停")),
            "buckets": [],
        }
    except Exception as exc:
        logger.warning("乐咕涨跌统计失败，改读页面说明: %s", exc)
        return _activity_from_page()


def _count(value):
    if value is None:
        return None
    match = re.search(r"\d+", str(value).replace(",", ""))
    return int(match.group(0)) if match else None


def _activity_from_page() -> dict:
    empty = {"date": None, "up": None, "down": None, "limit_up": None, "limit_down": None, "buckets": []}
    try:
        from akshare.utils.cons import headers

        response = requests.get("https://legulegu.com/stockdata/market-activity", headers=headers, timeout=12)
        response.raise_for_status()
    except Exception as exc:
        logger.warning("乐咕页面没有取到: %s", exc)
        return empty
    description = ""
    match = re.search(r'<meta name="description" content="([^"]+)"', response.text)
    if match:
        description = match.group(1)
    found = {label: int(count) for count, label in re.findall(r"(\d+)家([^，。]+)", description)}
    dated = re.search(r"(\d{4}-\d{2}-\d{2})", description)
    buckets = []
    for count, label in re.findall(r"(\d+)家([^，。]+)", description):
        if label in {"涨停", "跌停", "上涨", "下跌"}:
            continue
        buckets.append({"label": label, "count": int(count)})
    return {
        "date": dated.group(1) if dated else None,
        "up": int(found["上涨"]) if "上涨" in found else None,
        "down": int(found["下跌"]) if "下跌" in found else None,
        "limit_up": int(found["涨停"]) if "涨停" in found else None,
        "limit_down": int(found["跌停"]) if "跌停" in found else None,
        "buckets": buckets,
    }


def _sse() -> dict:
    import akshare as ak

    day = date.today()
    for _ in range(6):
        while day.weekday() >= 5:
            day -= timedelta(days=1)
        try:
            frame = ak.stock_sse_deal_daily(date=day.strftime("%Y%m%d"))
        except Exception as exc:
            logger.warning("上证成交概况 %s 失败: %s", day, exc)
            frame = None
        if frame is not None and not frame.empty and "单日情况" in frame.columns and "股票" in frame.columns:
            indexed = frame.set_index("单日情况")
            column = indexed["股票"]

            def cell(name: str):
                return _num(column.get(name)) if name in column.index else None

            return {
                "date": day.isoformat(),
                "amount": cell("成交金额"),
                "volume": cell("成交量"),
                "pe": cell("平均市盈率"),
                "turnover": cell("换手率"),
            }
        day -= timedelta(days=1)
    return {"date": None, "amount": None, "volume": None, "pe": None, "turnover": None}


def _market_flow() -> dict:
    import akshare as ak

    empty = {
        "date": None,
        "main": None,
        "main_ratio": None,
        "super_order": None,
        "super_ratio": None,
        "large": None,
        "large_ratio": None,
        "mid": None,
        "mid_ratio": None,
        "small": None,
        "small_ratio": None,
    }
    try:
        frame = ak.stock_market_fund_flow()
    except Exception as exc:
        logger.warning("大盘资金流失败: %s", exc)
        return empty
    if frame is None or frame.empty:
        return empty
    row = frame.iloc[-1]
    return {
        "date": str(row.get("日期") or "")[:10] or None,
        "main": _yi(row.get("主力净流入-净额")),
        "main_ratio": _num(row.get("主力净流入-净占比")),
        "super_order": _yi(row.get("超大单净流入-净额")),
        "super_ratio": _num(row.get("超大单净流入-净占比")),
        "large": _yi(row.get("大单净流入-净额")),
        "large_ratio": _num(row.get("大单净流入-净占比")),
        "mid": _yi(row.get("中单净流入-净额")),
        "mid_ratio": _num(row.get("中单净流入-净占比")),
        "small": _yi(row.get("小单净流入-净额")),
        "small_ratio": _num(row.get("小单净流入-净占比")),
    }


def _north() -> list[dict]:
    import akshare as ak

    try:
        frame = ak.stock_hsgt_fund_flow_summary_em()
    except Exception as exc:
        logger.warning("北向资金失败: %s", exc)
        return []
    if frame is None or frame.empty:
        return []
    rows = []
    for _, row in frame.iterrows():
        if str(row.get("资金方向") or "") != "北向":
            continue
        rows.append(
            {
                "board": str(row.get("板块") or ""),
                "net_buy": _num(row.get("成交净买额")),
                "inflow": _num(row.get("资金净流入")),
                "up": _count(row.get("上涨数")),
                "down": _count(row.get("下跌数")),
                "index_name": str(row.get("相关指数") or "") or None,
                "index_change": _num(row.get("指数涨跌幅")),
            }
        )
    return rows


def cn_market_desk() -> dict:
    now = time.time()
    with _lock:
        cached = _desk_cache["payload"]
        if cached is not None and now - _desk_cache["at"] < _CACHE_SECONDS:
            return cached
    payload = {"activity": _activity(), "sse": _sse(), "flow": _market_flow(), "north": _north()}
    activity = payload["activity"]
    if activity.get("limit_up") is not None or activity.get("buckets"):
        with _lock:
            _desk_cache["at"] = time.time()
            _desk_cache["payload"] = payload
    return payload


def _listed_code(symbol: str) -> tuple[str, str, str] | None:
    from app.services.symbols import split_symbol

    exchange, ticker = split_symbol(symbol)
    if not ticker or not ticker.isdigit():
        return None
    if exchange == "SSE":
        market = "sh"
    elif exchange == "SZSE":
        market = "sz"
    elif exchange in {"BSE", "BJ"}:
        market = "bj"
    else:
        return None
    return symbol, market, ticker


def _one_stock(symbol: str, market: str, ticker: str) -> dict:
    import akshare as ak

    item = {
        "symbol": symbol,
        "main": None,
        "main_ratio": None,
        "buy_volume": None,
        "sell_volume": None,
        "neutral_volume": None,
    }
    try:
        frame = ak.stock_individual_fund_flow(stock=ticker, market=market)
        if frame is not None and not frame.empty:
            row = frame.iloc[-1]
            item["main"] = _yi(row.get("主力净流入-净额"))
            item["main_ratio"] = _num(row.get("主力净流入-净占比"))
    except Exception as exc:
        logger.warning("个股资金流失败 %s: %s", symbol, exc)
    if market == "bj":
        return item
    try:
        ticks = ak.stock_zh_a_tick_tx_js(symbol=f"{market}{ticker}")
    except Exception as exc:
        logger.warning("分时内外盘失败 %s: %s", symbol, exc)
        return item
    if ticks is None or ticks.empty or "性质" not in ticks.columns:
        return item
    volume = ticks.groupby("性质")["成交量"].sum()
    item["buy_volume"] = _num(volume.get("买盘"))
    item["sell_volume"] = _num(volume.get("卖盘"))
    item["neutral_volume"] = _num(volume.get("中性盘"))
    return item


def cn_stock_flow(symbols: list[str]) -> dict:
    wanted = []
    seen = set()
    for raw in symbols:
        coded = _listed_code(raw)
        if coded is None or coded[0] in seen:
            continue
        seen.add(coded[0])
        wanted.append(coded)
        if len(wanted) >= 12:
            break
    now = time.time()
    items = []
    missing = []
    for symbol, market, ticker in wanted:
        cached = _stock_cache.get(symbol)
        if cached and now - cached[0] < _CACHE_SECONDS:
            items.append(cached[1])
        else:
            missing.append((symbol, market, ticker))
    if missing:
        with ThreadPoolExecutor(max_workers=4) as pool:
            fresh = list(pool.map(lambda row: _one_stock(*row), missing))
        for item in fresh:
            _stock_cache[item["symbol"]] = (time.time(), item)
            items.append(item)
    order = {symbol: index for index, (symbol, _, _) in enumerate(wanted)}
    items.sort(key=lambda item: order.get(item["symbol"], 0))
    return {"items": items}

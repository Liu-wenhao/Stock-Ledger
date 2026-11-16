"""Latest A-share margin balance and broad-ETF shares. Read-only background, not a signal."""

from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)

_CACHE_SECONDS = 6 * 3600
_lock = threading.Lock()
_cache: dict = {"at": 0.0, "payload": None}

# Shanghai-listed broad ETFs. Shares come from the exchange, in 万份.
_ETFS = (
    ("510300", "沪深300"),
    ("510050", "上证50"),
    ("510500", "中证500"),
    ("512100", "中证1000"),
)
_NOTE = "只说明资金现在的位置，不能用来判断后面涨跌。融资余额在次日公布；ETF 份额是最近一个交易日相对约 4 周前的变化。"


def cn_flow_snapshot() -> dict:
    now = time.time()
    with _lock:
        cached = _cache["payload"]
        if cached and now - _cache["at"] < _CACHE_SECONDS:
            return cached
    payload = _build()
    if payload.get("margin_date") or payload.get("etfs"):
        with _lock:
            _cache["at"] = time.time()
            _cache["payload"] = payload
    return payload


def _get(url: str, params: dict, referer: str) -> dict:
    import requests

    response = requests.get(
        url,
        params=params,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": referer,
        },
        timeout=20,
        proxies={"http": None, "https": None},
    )
    response.raise_for_status()
    return response.json()


def _margin() -> list[dict]:
    data = _get(
        "https://datacenter-web.eastmoney.com/api/data/v1/get",
        {
            "reportName": "RPTA_RZRQ_LSHJ",
            "columns": "DIM_DATE,RZYE,RZYEZB",
            "source": "WEB",
            "sortColumns": "DIM_DATE",
            "sortTypes": "-1",
            "pageNumber": 1,
            "pageSize": 260,
        },
        "https://data.eastmoney.com/",
    )
    rows = ((data.get("result") or {}).get("data")) or []
    out = []
    for row in rows:
        try:
            out.append(
                {
                    "date": str(row["DIM_DATE"])[:10],
                    "balance": float(row["RZYE"]),
                    "ratio": float(row["RZYEZB"]),
                }
            )
        except (TypeError, ValueError, KeyError):
            continue
    out.sort(key=lambda item: item["date"])
    return out


def _shares(day: str) -> dict[str, float]:
    data = _get(
        "https://query.sse.com.cn/commonQuery.do",
        {
            "isPagination": "true",
            "pageHelp.pageSize": 2000,
            "pageHelp.pageNo": 1,
            "pageHelp.beginPage": 1,
            "pageHelp.cacheSize": 1,
            "pageHelp.endPage": 1,
            "sqlId": "COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L",
            "STAT_DATE": day,
        },
        "https://www.sse.com.cn/",
    )
    wanted = {code for code, _ in _ETFS}
    found = {}
    for row in data.get("result") or []:
        code = str(row.get("SEC_CODE") or "")
        if code not in wanted or not row.get("TOT_VOL"):
            continue
        try:
            found[code] = float(row["TOT_VOL"])
        except (TypeError, ValueError):
            continue
    return found


def _build() -> dict:
    empty = {
        "margin_date": None,
        "margin_balance": None,
        "margin_ratio": None,
        "margin_change_pct": None,
        "margin_rank": None,
        "etf_date": None,
        "etf_base_date": None,
        "etfs": [],
        "note": _NOTE,
    }
    try:
        rows = _margin()
    except Exception as exc:
        logger.warning("融资余额没有取到: %s", exc)
        rows = []
    if len(rows) >= 21:
        latest = rows[-1]
        earlier = rows[-21]
        window = [row["ratio"] for row in rows[-250:]]
        below = sum(1 for value in window if value < latest["ratio"])
        empty.update(
            margin_date=latest["date"],
            margin_balance=round(latest["balance"] / 1e12, 2),
            margin_ratio=round(latest["ratio"], 2),
            margin_change_pct=round((latest["balance"] / earlier["balance"] - 1) * 100, 2),
            margin_rank=round(below / len(window) * 100, 0),
        )
    if not rows:
        return empty
    # About four trading weeks back, then a few earlier days in case that date has no file.
    candidates = [rows[index]["date"] for index in (-21, -22, -20, -23, -24) if abs(index) < len(rows)]
    latest_day = rows[-1]["date"]
    try:
        now = _shares(latest_day)
    except Exception as exc:
        logger.warning("ETF 份额没有取到 %s: %s", latest_day, exc)
        now = {}
    before: dict[str, float] = {}
    base_day = None
    for day in candidates:
        try:
            before = _shares(day)
        except Exception as exc:
            logger.warning("ETF 份额没有取到 %s: %s", day, exc)
            continue
        if before:
            base_day = day
            break
    etfs = []
    for code, name in _ETFS:
        current = now.get(code)
        previous = before.get(code)
        change = None
        if current and previous:
            change = round((current / previous - 1) * 100, 2)
        if current or change is not None:
            etfs.append(
                {
                    "code": code,
                    "name": name,
                    "shares": None if current is None else round(current / 10000, 2),
                    "change_pct": change,
                }
            )
    empty["etf_date"] = latest_day if now else None
    empty["etf_base_date"] = base_day
    empty["etfs"] = etfs
    return empty

from __future__ import annotations

import csv
import io
import logging
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from functools import lru_cache

import requests
import pandas as pd
from pandas.tseries.holiday import USFederalHolidayCalendar
from sqlalchemy.orm import Session

from app.models.entities import DailyBar
from app.services.market import _fetch_sina_history
from app.services.trading_sessions import _calendar

logger = logging.getLogger(__name__)
_CACHE_SECONDS = 900
_MAX_CARRY_DAYS = 7
_fetch_cache: dict[tuple, tuple[float, list[tuple[date, float]]]] = {}
_cache_lock = threading.Lock()


@dataclass(frozen=True)
class Benchmark:
    key: str
    label: str
    provider: str
    ticker: str
    calendar: str
    source: str
    note: str
    yield_rate: bool = False


BENCHMARKS = (
    Benchmark("sp500", "标普500", "sina", ".inx", "us", "本地指数日线 / Sina · 标普500", "美元价格指数，不含股息。"),
    Benchmark("treasury", "3个月美债（无风险估算）", "treasury", "US3M", "treasury", "美国财政部 · 3 Mo 日利率", "前一日已发布的年化利率 ÷ 365，按自然日逐日复利估算；不是债券价格回报。", True),
    Benchmark("gold", "黄金（GLD ETF代理）", "sina", "gld", "us", "本地日线 / Sina · GLD", "使用美元计价的 GLD 黄金 ETF 价格代理黄金收益，不是现货金价；基金管理费已体现在价格中，未计交易税费。"),
    Benchmark("bitcoin", "比特币", "kraken", "XBTUSD", "daily", "Kraken · BTC/USD", "美元计价，使用已完成的 UTC 日日线，不含费用；接口最多返回最近720根日线，缺少区间起点时不计算。"),
    Benchmark("fx", "美元兑人民币", "ecb", "USDCNY", "ecb", "ECB / Frankfurter · USD/CNY", "USD/CNY：1 美元可兑换的人民币；正收益代表美元升值、人民币贬值。使用 ECB 参考汇率，不是银行成交价。"),
)
_DATABASE_ALIASES = {"sp500": ".inx", "gold": "GLD"}


def _bar_key(benchmark: Benchmark) -> str:
    return f"BENCH:{benchmark.provider.upper()}:{benchmark.ticker.upper()}"


def _parse_csv(payload: str, value_column: str) -> list[tuple[date, float]]:
    quotes = []
    for row in csv.DictReader(io.StringIO(payload.lstrip("\ufeff"))):
        raw_day = row.get("observation_date") or row.get("DATE") or row.get("Date")
        try:
            raw_day = str(raw_day).strip()
            day = datetime.strptime(raw_day, "%m/%d/%Y").date() if "/" in raw_day else date.fromisoformat(raw_day)
            value = float(row[value_column])
        except (ValueError, TypeError, KeyError):
            continue
        if math.isfinite(value):
            quotes.append((day, value))
    return quotes


def _parse_kraken(payload: dict) -> list[tuple[date, float]]:
    if payload.get("error"):
        return []
    records = next((rows for key, rows in (payload.get("result") or {}).items() if key != "last"), [])
    quotes = []
    for row in records:
        try:
            day = datetime.fromtimestamp(int(row[0]), timezone.utc).date()
            value = float(row[4])
        except (ValueError, TypeError, IndexError, OverflowError, OSError):
            continue
        if math.isfinite(value):
            quotes.append((day, value))
    return quotes


def _parse_forex(payload: dict) -> list[tuple[date, float]]:
    if payload.get("base") != "USD":
        return []
    quotes = []
    for raw_day, rates in (payload.get("rates") or {}).items():
        try:
            day = date.fromisoformat(raw_day)
            value = float(rates["CNY"])
        except (ValueError, TypeError, KeyError):
            continue
        if math.isfinite(value):
            quotes.append((day, value))
    return quotes


def _fetch_quotes(benchmark: Benchmark, start: date, end: date) -> list[tuple[date, float]]:
    headers = {"User-Agent": "Mozilla/5.0 stock-ledger"}
    if benchmark.provider == "sina":
        number = max(120, min(12000, (end - start).days + 30))
        raw = _fetch_sina_history(benchmark.ticker, number)
        quotes = []
        for row in raw:
            try:
                quotes.append((date.fromisoformat(str(row["d"])[:10]), float(row["c"])))
            except (ValueError, TypeError, KeyError):
                continue
        return quotes
    if benchmark.provider == "treasury":
        months = []
        month = start.replace(day=1)
        while month <= end:
            months.append(month.strftime("%Y%m"))
            month = (month.replace(day=28) + timedelta(days=4)).replace(day=1)

        def fetch_month(month_key):
            try:
                response = requests.get(
                    "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
                    f"daily-treasury-rates.csv/all/{month_key}",
                    params={"type": "daily_treasury_yield_curve", "field_tdr_date_value_month": month_key},
                    headers=headers,
                    timeout=(3, 8),
                )
                response.raise_for_status()
                return _parse_csv(response.text, "3 Mo")
            except Exception as exception:
                logger.warning("Treasury history unavailable %s: %s", month_key, exception)
                return []

        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="treasury-history") as pool:
            return [quote for quotes in pool.map(fetch_month, months) for quote in quotes]
    if benchmark.provider == "kraken":
        response = requests.get(
            "https://api.kraken.com/0/public/OHLC",
            params={"pair": benchmark.ticker, "interval": 1440, "since": int(datetime.combine(start, datetime.min.time(), timezone.utc).timestamp())},
            headers=headers,
            timeout=(3, 8),
        )
        response.raise_for_status()
        return _parse_kraken(response.json())
    if benchmark.provider == "ecb":
        response = requests.get(
            f"https://api.frankfurter.dev/v1/{start.isoformat()}..{end.isoformat()}",
            params={"base": "USD", "symbols": "CNY"},
            headers=headers,
            timeout=(3, 8),
        )
        response.raise_for_status()
        return _parse_forex(response.json())
    return []


@lru_cache(maxsize=8)
def _fetch_lock(key: str):
    return threading.Lock()


def _cached_fetch(benchmark: Benchmark, start: date, end: date) -> list[tuple[date, float]]:
    cache_key = (benchmark.key, start, end)
    with _fetch_lock(benchmark.key):
        with _cache_lock:
            cached = _fetch_cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < _CACHE_SECONDS:
            return cached[1]
        try:
            quotes = _fetch_quotes(benchmark, start, end)
        except Exception as exception:
            logger.warning("Benchmark history unavailable %s: %s", benchmark.key, exception)
            quotes = []
        with _cache_lock:
            if len(_fetch_cache) >= 128:
                oldest = min(_fetch_cache, key=lambda key: _fetch_cache[key][0])
                _fetch_cache.pop(oldest, None)
            _fetch_cache[cache_key] = (time.monotonic(), quotes)
        return quotes


def _days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


def _expected_days(benchmark: Benchmark, start: date, end: date) -> set[date]:
    if benchmark.calendar == "daily":
        return set(_days(start, end))
    if benchmark.calendar in {"us", "treasury"}:
        try:
            sessions = {stamp.date() for stamp in _calendar("us").sessions_in_range(start.isoformat(), end.isoformat())}
            if benchmark.calendar == "treasury":
                sessions -= {stamp.date() for stamp in USFederalHolidayCalendar().holidays(start, end)}
            return sessions
        except Exception:
            pass
    sessions = {day for day in _days(start, end) if day.weekday() < 5}
    if benchmark.calendar == "ecb":
        for year in range(start.year, end.year + 1):
            easter = (pd.Timestamp(year=year, month=1, day=1) + pd.offsets.Easter()).date()
            sessions -= {
                date(year, 1, 1), date(year, 5, 1), date(year, 12, 25), date(year, 12, 26),
                easter - timedelta(days=2), easter + timedelta(days=1),
            }
    return sessions


def _valid_value(benchmark: Benchmark, value: float) -> bool:
    return math.isfinite(value) and (value >= 0 if benchmark.yield_rate else value > 0)


def _latest_quote(quotes: dict[date, float], day: date) -> tuple[date | None, float | None]:
    for offset in range(_MAX_CARRY_DAYS + 1):
        candidate = day - timedelta(days=offset)
        if candidate in quotes:
            return candidate, quotes[candidate]
    return None, None


def _price_at(quotes: dict[date, float], day: date, expected: set[date]) -> tuple[date | None, float | None]:
    quote_day, value = _latest_quote(quotes, day)
    if quote_day is None or any(candidate in expected for candidate in _days(quote_day + timedelta(days=1), day)):
        return None, None
    return quote_day, value


def _series(benchmark: Benchmark, quotes: dict[date, float], dates: list[date]) -> dict:
    result = {
        "key": benchmark.key,
        "label": benchmark.label,
        "source": benchmark.source,
        "note": benchmark.note,
        "status": "unavailable",
        "as_of": max(quotes) if quotes else None,
        "return_percent": None,
        "annualized_yield": quotes[max(quotes)] if benchmark.yield_rate and quotes else None,
        "points": [],
    }
    if not dates:
        result["note"] += " 所选区间尚无已完成日线。"
        return result
    expected = _expected_days(benchmark, dates[0] - timedelta(days=_MAX_CARRY_DAYS), dates[-1])
    anchor_day, anchor = _latest_quote(quotes, dates[0]) if benchmark.yield_rate else _price_at(quotes, dates[0], expected)
    if anchor is None:
        result["note"] += " 缺少区间起点的有效报价，不能计算同期收益。"
        return result
    growth = 1.0
    accrual_complete = True
    for index, day in enumerate(dates):
        if benchmark.yield_rate:
            quote_day, annual_rate = _latest_quote(quotes, day - timedelta(days=1)) if index else (anchor_day, anchor)
            if annual_rate is None:
                accrual_complete = False
            if index and accrual_complete:
                growth *= 1 + annual_rate / 100 / 365
            percent = (growth - 1) * 100 if accrual_complete else None
        else:
            quote_day, value = _price_at(quotes, day, expected)
            percent = (value / anchor - 1) * 100 if value is not None else None
        result["points"].append({"date": day, "return_percent": round(percent, 6) if percent is not None else None, "quote_date": quote_day})
    result["return_percent"] = result["points"][-1]["return_percent"]
    result["status"] = "partial" if any(point["return_percent"] is None for point in result["points"]) else "ok"
    if result["status"] == "partial":
        result["note"] += " 缺少部分交易日数据，缺口留空，不按零收益处理。"
    return result


def _save_quotes(db: Session, values: list[dict]) -> None:
    if not values:
        return
    if db.get_bind().dialect.name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert

        statement = insert(DailyBar).on_conflict_do_nothing(index_elements=["symbol", "bar_date"])
    else:
        from sqlalchemy.dialects.mysql import insert

        statement = insert(DailyBar).prefix_with("IGNORE")
    for offset in range(0, len(values), 500):
        db.execute(statement.values(values[offset:offset + 500]))
    db.commit()


def build_benchmarks(db: Session, start: date, end: date, now: datetime | None = None) -> dict:
    if start > end:
        raise ValueError("开始日期不能晚于结束日期")
    if (end - start).days > 36600:
        raise ValueError("对比区间不能超过100年")
    clock = now or datetime.now(timezone.utc)
    clock = clock.replace(tzinfo=timezone.utc) if clock.tzinfo is None else clock.astimezone(timezone.utc)
    end = min(end, clock.date() - timedelta(days=1))
    dates = _days(start, end) if start <= end else []
    result = {
        "start_date": start,
        "end_date": end if dates else None,
        "currency": "USD",
        "dates": dates,
        "series": [],
        "note": "同一起止日、起点为0%；仅使用已完成日线。休市沿用最近有效收盘价，缺少交易日报价留空。年化美债利率不直接当作同期收益；资产基准按美元计价，汇率变动单列，未计税费。",
    }
    if not dates:
        result["series"] = [_series(benchmark, {}, []) for benchmark in BENCHMARKS]
        return result
    since = start - timedelta(days=_MAX_CARRY_DAYS + 3)
    stored = db.query(DailyBar).filter(
        DailyBar.symbol.in_([_bar_key(benchmark) for benchmark in BENCHMARKS] + list(_DATABASE_ALIASES.values())),
        DailyBar.bar_date >= since,
        DailyBar.bar_date <= end,
    ).all()
    by_symbol = {}
    for row in stored:
        by_symbol.setdefault(row.symbol, {})[row.bar_date] = float(row.close)
    histories = {
        benchmark.key: {
            day: value for day, value in {
                **by_symbol.get(_DATABASE_ALIASES.get(benchmark.key), {}),
                **by_symbol.get(_bar_key(benchmark), {}),
            }.items() if _valid_value(benchmark, value)
        }
        for benchmark in BENCHMARKS
    }
    missing = [
        benchmark for benchmark in BENCHMARKS
        if not _expected_days(benchmark, start, end).issubset(histories[benchmark.key])
        or _latest_quote(histories[benchmark.key], start)[1] is None
    ]
    values = []
    if missing:
        with ThreadPoolExecutor(max_workers=5, thread_name_prefix="benchmark-history") as pool:
            fetched = pool.map(lambda benchmark: _cached_fetch(benchmark, since, end), missing)
            for benchmark, quotes in zip(missing, fetched):
                history = histories[benchmark.key]
                for day, value in quotes:
                    if day < since or day > end or day in history or not _valid_value(benchmark, value):
                        continue
                    history[day] = value
                    values.append({"symbol": _bar_key(benchmark), "bar_date": day, "close": Decimal(str(value)).quantize(Decimal("0.000001")), "volume": 0, "dollar": 0})
    _save_quotes(db, values)
    result["series"] = [_series(benchmark, histories[benchmark.key], dates) for benchmark in BENCHMARKS]
    return result

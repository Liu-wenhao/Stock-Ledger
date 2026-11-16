"""Market-wide sentiment from public daily series.

Breadth is the advance/decline count of the largest 500 US names on the
Sina US list (a large-cap tape, not the full NYSE). VIX is the Cboe daily
history file. Index paths are the Sina daily bars for the S&P 500 and Nasdaq.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from statistics import median, mean

from app.services.bars import chart_since
from app.services.market import _request_text

logger = logging.getLogger(__name__)

_CACHE_SECONDS = 600
_LIVE_BREADTH_SECONDS = 180
_lock = threading.Lock()
_caches: dict[str, dict] = {}

_VIX_URL = "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"
_INDEXES = (("标普 500", ".inx"), ("道琼斯", ".dji"), ("纳斯达克", ".ixic"))
_ARMS_INDEX = {
    "us": ("纳斯达克 100", ".ndx"),
    "cn": ("中证全指", "SSE:000985"),
    "hk": ("恒生指数", "HKEX:HSI"),
}


def build_market_sentiment(db=None, market: str = "us") -> dict:
    market = market if market in {"us", "cn", "hk"} else "us"
    now = time.time()
    owner = False
    cached_hit = None
    event = None
    with _lock:
        slot = _caches.setdefault(market, {"at": 0.0, "payload": None, "event": None})
        cached = slot["payload"]
        if cached is not None and now - slot["at"] < _CACHE_SECONDS:
            cached_hit = cached
        else:
            event = slot["event"]
            if event is None:
                event = threading.Event()
                slot["event"] = event
                owner = True
    if cached_hit is not None:
        return _finish(cached_hit, market)
    if not owner:
        event.wait(timeout=90)
        with _lock:
            payload = _caches[market]["payload"]
        return _finish(payload or _build(db, market), market)
    try:
        payload = _build(db, market)
        with _lock:
            _caches[market]["at"] = time.time()
            _caches[market]["payload"] = payload
        return _finish(payload, market)
    finally:
        event.set()
        with _lock:
            slot = _caches[market]
            if slot["event"] is event:
                slot["event"] = None


def _finish(payload: dict | None, market: str) -> dict | None:
    if not payload:
        return payload
    if market == "us":
        payload = _overlay_open_session(payload)
    elif market in {"cn", "hk"}:
        payload = _overlay_listed_session(payload, market)
    if market != "hk":
        _attach_arms_reading(payload, market)
    return payload


def _build(db=None, market: str = "us") -> dict:
    if market == "cn":
        return _build_listed(db, market)
    if market == "hk":
        return _build_listed(db, market)
    from app.core.database import SessionLocal

    own_db = db is None
    if own_db:
        db = SessionLocal()
    try:
        vix = _vix(db)
        indexes = [_index(db, name, symbol) for name, symbol in _INDEXES]
        arms_index = _arms_index_series(db, "us", indexes)
    finally:
        if own_db:
            db.close()
    as_of = None
    for item in indexes:
        if item["history"]:
            as_of = item["history"][-1]["date"]
    if as_of is None and vix["history"]:
        as_of = vix["history"][-1]["date"]
    label, tone, note = _read(None, vix)
    return {
        "as_of": as_of,
        "label": label,
        "tone": tone,
        "note": note,
        "breadth": None,
        "vix": vix,
        "indexes": indexes,
        "arms_index": arms_index,
    }


def _read(breadth: dict | None, vix: dict) -> tuple[str, str, str]:
    ratio = None if not breadth else breadth.get("up_ratio")
    vs = vix.get("vs_ma20")
    if ratio is None:
        return "中性", "info", "涨跌家数这次没有取到，只看波动和指数。"
    if vs is not None and vs > 15 and ratio < 40:
        return "偏恐慌", "danger", "VIX 高于自己的 20 日均线，同时市值前 500 里下跌的更多。"
    if vs is not None and vs < -10 and ratio > 65:
        return "偏亢奋", "warning", "VIX 低于自己的 20 日均线，同时上涨家数明显占优。"
    if ratio < 40:
        return "偏弱", "warning", "市值前 500 里下跌家数更多。"
    if ratio > 60:
        return "偏强", "success", "市值前 500 里上涨家数更多。"
    return "中性", "info", "涨跌家数接近对半，没有单边情绪。"


def _arms_value(up: int, down: int, up_size: float, down_size: float) -> float | None:
    """TRIN = (advancers / decliners) / (advancing size / declining size)."""
    if up < 1 or down < 1 or up_size <= 0 or down_size <= 0:
        return None
    return round((up / down) / (up_size / down_size), 2)


def _breadth_payload(
    universe: str,
    up: int,
    down: int,
    flat: int,
    arms: float | None,
    *,
    changes: list[float] | None = None,
    minimum: int = 100,
) -> dict | None:
    count = up + down + flat
    if count < minimum:
        return None
    payload = {
        "universe": universe,
        "count": count,
        "advancers": up,
        "decliners": down,
        "unchanged": flat,
        "up_ratio": round(up / count * 100, 1),
        "arms": arms,
        "median_change": None,
        "strong_up": None,
        "strong_down": None,
    }
    if changes:
        payload["median_change"] = round(median(changes), 2)
        payload["strong_up"] = sum(1 for value in changes if value >= 2)
        payload["strong_down"] = sum(1 for value in changes if value <= -2)
    return payload


def _fetch_vix_bars() -> list[dict]:
    request = urllib.request.Request(_VIX_URL, headers={"User-Agent": "stock-ledger"})
    with urllib.request.urlopen(request, timeout=12) as response:
        text = response.read().decode("utf-8", errors="replace")
    bars: list[dict] = []
    for line in text.splitlines()[1:]:
        cells = [cell.strip() for cell in line.split(",")]
        if len(cells) < 5 or not cells[4]:
            continue
        parts = cells[0].split("/")
        if len(parts) != 3:
            continue
        try:
            opened, high, low, close = (float(cells[i]) for i in range(1, 5))
        except ValueError:
            continue
        month, day, year = parts
        bars.append(
            {
                "d": f"{year}-{int(month):02d}-{int(day):02d}",
                "o": opened,
                "h": high,
                "l": low,
                "c": close,
                "v": 0,
                "dol": 0,
            }
        )
    return bars


def _vix(db) -> dict:
    from app.services.bars import load_bars

    empty = {"last": None, "day_change": None, "ma20": None, "vs_ma20": None, "history": []}
    try:
        bars = load_bars(db, "VIX", _fetch_vix_bars)
    except Exception:
        return empty
    closes = [(bar["d"], bar["c"]) for bar in bars]
    if len(closes) < 2:
        return empty
    floor = chart_since().isoformat()
    tail = [(day, price) for day, price in closes if day >= floor]
    if len(tail) < 2:
        return empty
    window = [price for _, price in closes[-20:]]
    last = tail[-1][1]
    prev = tail[-2][1]
    ma20 = mean(window) if len(window) >= 2 else None
    return {
        "last": round(last, 2),
        "day_change": round((last / prev - 1) * 100, 2) if prev else None,
        "ma20": None if ma20 is None else round(ma20, 2),
        "vs_ma20": None if not ma20 else round((last / ma20 - 1) * 100, 1),
        "history": [{"date": day, "close": round(price, 2)} for day, price in tail],
    }


def _ma20_fields(closes: list[float], last: float) -> dict:
    window = closes[-20:]
    ma20 = round(mean(window), 2) if len(window) >= 20 else None
    return {
        "ma20": ma20,
        "vs_ma20": None if not ma20 else round((last / ma20 - 1) * 100, 2),
    }


def _index(db, name: str, symbol: str) -> dict:
    from app.services.bars import load_sina_bars

    empty = {"name": name, "last": None, "day_change": None, "ret_5d": None, "ma20": None, "vs_ma20": None, "history": []}
    try:
        rows = load_sina_bars(db, symbol)
    except Exception:
        return empty
    bars = [(row["d"], row["c"]) for row in rows if row.get("c")]
    if len(bars) < 2:
        return empty
    floor = chart_since().isoformat()
    tail = [(day, price) for day, price in bars if day >= floor]
    if len(tail) < 2:
        return empty
    last = bars[-1][1]
    prev = bars[-2][1]
    base = bars[-6][1] if len(bars) >= 6 else None
    return {
        "name": name,
        "last": round(last, 2),
        "day_change": round((last / prev - 1) * 100, 2) if prev else None,
        "ret_5d": None if not base else round((last / base - 1) * 100, 2),
        **_ma20_fields([price for _, price in bars], last),
        "history": [{"date": day, "close": round(price, 2)} for day, price in tail],
    }


def _push_today(item: dict, close: float, day: str) -> None:
    history = item["history"]
    point = {"date": day, "close": round(close, 2)}
    if history and history[-1]["date"] == day:
        history[-1] = point
    else:
        history.append(point)
    floor = chart_since().isoformat()
    item["history"] = [point for point in history if point["date"] >= floor]
    last = history[-1]["close"]
    prev = history[-2]["close"] if len(history) >= 2 else None
    base = history[-6]["close"] if len(history) >= 6 else None
    item["last"] = last
    item["day_change"] = round((last / prev - 1) * 100, 2) if prev else None
    if "ret_5d" in item:
        item["ret_5d"] = None if not base else round((last / base - 1) * 100, 2)
    if "ma20" in item:
        window = [point["close"] for point in item["history"][-20:]]
        item["ma20"] = round(mean(window), 2) if len(window) >= 20 else None
        item["vs_ma20"] = None if not item["ma20"] else round((last / item["ma20"] - 1) * 100, 2)


def _overlay_open_session(payload: dict | None) -> dict | None:
    """Attach today's live print for the charts. The cached body stays on completed sessions."""
    if not payload:
        return payload
    from app.services.bars import bar_key, cash_session_closed, open_session_bars

    if cash_session_closed():
        return payload
    wanted = {name: symbol for name, symbol in _INDEXES}
    wanted[_ARMS_INDEX["us"][0]] = _ARMS_INDEX["us"][1]
    bars = open_session_bars([*wanted.values(), "VIX"])
    indexes = []
    for item in payload.get("indexes") or []:
        copy = {**item, "history": [dict(point) for point in item.get("history") or []]}
        bar = bars.get(bar_key(wanted.get(item.get("name"), "")))
        if bar:
            _push_today(copy, bar["c"], bar["d"])
        indexes.append(copy)
    vix = payload.get("vix") or {}
    vix = {**vix, "history": [dict(point) for point in vix.get("history") or []]}
    vix_bar = bars.get(bar_key("VIX"))
    if vix_bar:
        _push_today(vix, vix_bar["c"], vix_bar["d"])
    arms = payload.get("arms_index")
    if arms:
        arms = {**arms, "history": [dict(point) for point in arms.get("history") or []]}
        bar = bars.get(bar_key(_ARMS_INDEX["us"][1]))
        if bar:
            _push_today(arms, bar["c"], bar["d"])
    as_of = payload.get("as_of")
    for item in indexes:
        if item.get("history"):
            as_of = item["history"][-1]["date"]
    return {**payload, "indexes": indexes, "vix": vix, "arms_index": arms, "as_of": as_of}


def _overlay_listed_session(payload: dict | None, market: str) -> dict | None:
    """Today's index print stays off the daily bars until the cash close. Lunch keeps the morning print."""
    if not payload or market not in {"cn", "hk"}:
        return payload
    from app.services.bars import _local_now, _local_session, cn_frozen, hk_frozen
    from app.services.market import _fetch_cn_hk_quotes

    if market == "cn":
        local = _local_now("Asia/Shanghai", None)
        frozen = cn_frozen()
        pairs = _CN_INDEXES
    else:
        local = _local_now("Asia/Hong_Kong", None)
        frozen = hk_frozen()
        pairs = _HK_INDEXES
    session = _local_session(local, 9 * 60 + 30)
    if frozen >= session:
        return payload
    wanted = {name: symbol for name, symbol in pairs}
    arms_name, arms_symbol = _ARMS_INDEX[market]
    wanted[arms_name] = arms_symbol
    quotes = _fetch_cn_hk_quotes(list(dict.fromkeys(wanted.values())))
    prices: dict[str, float] = {}
    for symbol, quote in quotes.items():
        stamp = quote.get("price_as_of")
        price = quote.get("price")
        if price is None or stamp is None or stamp.date() != session:
            continue
        prices[symbol] = float(price)

    def paint(item: dict, symbol: str) -> dict:
        copy = {**item, "history": [dict(point) for point in item.get("history") or []]}
        close = prices.get(symbol)
        if close:
            _push_today(copy, close, session.isoformat())
        return copy

    indexes = [paint(item, wanted.get(item.get("name"), "")) for item in payload.get("indexes") or []]
    arms = payload.get("arms_index")
    if arms:
        arms = paint(arms, arms_symbol)
    as_of = payload.get("as_of")
    for item in indexes:
        if item.get("history"):
            as_of = item["history"][-1]["date"]
    return {**payload, "indexes": indexes, "arms_index": arms, "as_of": as_of}


_CN_INDEXES = (
    ("上证指数", "SSE:000001"),
    ("深证成指", "SZSE:399001"),
    ("创业板指", "SZSE:399006"),
    ("科创板", "SSE:000688"),
)
_HK_INDEXES = (
    ("恒生指数", "HKEX:HSI"),
    ("恒生科技", "HKEX:HSTECH"),
)


def _build_listed(db, market: str) -> dict:
    from app.core.database import SessionLocal

    own_db = db is None
    if own_db:
        db = SessionLocal()
    try:
        if market == "cn":
            indexes = [_index_tencent(db, name, symbol) for name, symbol in _CN_INDEXES]
            label, tone, note = _read_ratio(None, "沪深A股")
            arms_index = _arms_index_series(db, "cn", indexes)
        else:
            indexes = [_index_tencent(db, name, symbol) for name, symbol in _HK_INDEXES]
            label, tone, note = "中性", "info", "港股不计算阿姆士和涨跌家数，只看指数。"
            arms_index = _arms_index_series(db, "hk", indexes)
    finally:
        if own_db:
            db.close()
    as_of = None
    for item in indexes:
        if item["history"]:
            as_of = item["history"][-1]["date"]
    return {
        "as_of": as_of,
        "label": label,
        "tone": tone,
        "note": note,
        "breadth": None,
        "vix": None,
        "indexes": indexes,
        "arms_index": arms_index,
    }


def _arms_index_series(db, market: str, indexes: list[dict]) -> dict:
    """Index drawn on the Arms chart. Kept off the price-page tape."""
    name, symbol = _ARMS_INDEX[market]
    found = next((item for item in indexes if item.get("name") == name and item.get("history")), None)
    if found:
        return found
    if market == "us":
        return _index(db, name, symbol)
    return _index_tencent(db, name, symbol)


def _shanghai_now(now: datetime | None = None) -> datetime:
    from zoneinfo import ZoneInfo

    zone = ZoneInfo("Asia/Shanghai")
    if now and now.tzinfo:
        return now.astimezone(zone)
    return datetime.now(zone)


def _new_york_now(now: datetime | None = None) -> datetime:
    from zoneinfo import ZoneInfo

    zone = ZoneInfo("America/New_York")
    if now and now.tzinfo:
        return now.astimezone(zone)
    return datetime.now(zone)


def _previous_weekday(day: date) -> date:
    day = day - timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def arms_due(market: str, now: datetime | None = None) -> tuple[date, str] | None:
    """Checkpoint whose quotes are a finished session. None while that session is still printing."""
    if market == "hk":
        return None
    if market == "cn":
        local = _shanghai_now(now)
        minutes = local.hour * 60 + local.minute
        if local.weekday() < 5 and 11 * 60 + 30 <= minutes < 13 * 60:
            return local.date(), "am"
        if local.weekday() < 5 and 9 * 60 + 30 <= minutes < 15 * 60:
            return None
        if local.weekday() < 5 and minutes >= 15 * 60:
            return local.date(), "pm"
        day = local.date()
        if local.weekday() < 5:
            day = _previous_weekday(day)
        else:
            while day.weekday() >= 5:
                day -= timedelta(days=1)
        return day, "pm"
    local = _new_york_now(now)
    minutes = local.hour * 60 + local.minute
    if local.weekday() < 5 and 9 * 60 + 30 <= minutes < 17 * 60 + 30:
        return None
    if local.weekday() < 5 and minutes >= 17 * 60 + 30:
        return local.date(), "pm"
    day = local.date()
    if local.weekday() < 5:
        day = _previous_weekday(day)
    else:
        while day.weekday() >= 5:
            day -= timedelta(days=1)
    return day, "pm"


def _arms_caption(phase: str) -> str:
    if phase == "am":
        return "按上午收盘计算。"
    return "按收盘计算。"


def _reading_dict(row) -> dict:
    def num(value):
        return None if value is None else float(value)

    return {
        "universe": row.universe,
        "count": row.count,
        "advancers": row.advancers,
        "decliners": row.decliners,
        "unchanged": row.unchanged,
        "up_ratio": num(row.up_ratio),
        "arms": num(row.arms),
        "median_change": num(row.median_change),
        "strong_up": row.strong_up,
        "strong_down": row.strong_down,
        "above_ma20": num(row.above_ma20),
        "ma_valid": row.ma_valid,
        "ma_total": row.ma_total,
    }


def _remember_breadth_day(db, market: str, day: date, fresh: dict, *, phase: str = "pm", book: str = "legacy") -> None:
    """One chart row per session. Am fills a blank day; pm overwrites that day's point."""
    from decimal import Decimal

    from app.models.entities import BreadthDay

    def dec(value, digits="0.01"):
        if value is None:
            return None
        return Decimal(str(value)).quantize(Decimal(digits))

    advancers = int(fresh.get("advancers") or 0)
    decliners = int(fresh.get("decliners") or 0)
    unchanged = int(fresh.get("unchanged") or 0)
    count = advancers + decliners + unchanged
    breadth = None if not count else round((advancers - decliners) / count * 100, 1)
    up_ratio = fresh.get("up_ratio")
    if up_ratio is None and count:
        up_ratio = round(advancers / count * 100, 1)
    row = (
        db.query(BreadthDay)
        .filter(BreadthDay.market == market, BreadthDay.bar_date == day, BreadthDay.book == book)
        .one_or_none()
    )
    if row is None:
        db.add(
            BreadthDay(
                market=market,
                bar_date=day,
                book=book,
                advancers=advancers,
                decliners=decliners,
                unchanged=unchanged,
                up_ratio=dec(up_ratio),
                breadth=dec(breadth),
                arms=dec(fresh.get("arms")),
                above_ma20=dec(fresh.get("above_ma20")),
            )
        )
        return
    if phase != "pm":
        if row.above_ma20 is None and fresh.get("above_ma20") is not None:
            row.above_ma20 = dec(fresh.get("above_ma20"))
        return
    row.advancers = advancers
    row.decliners = decliners
    row.unchanged = unchanged
    row.up_ratio = dec(up_ratio)
    row.breadth = dec(breadth)
    if fresh.get("arms") is not None:
        row.arms = dec(fresh.get("arms"))
    if fresh.get("above_ma20") is not None:
        row.above_ma20 = dec(fresh.get("above_ma20"))


def _sync_breadth_days_from_arms(db, market: str) -> None:
    """Pull closed Arms checkpoints into the chart table when a day is still missing."""
    from app.models.entities import ArmsReading, BreadthDay

    have = {
        (row.bar_date, row.book)
        for row in db.query(BreadthDay.bar_date, BreadthDay.book).filter(BreadthDay.market == market).all()
    }
    readings = (
        db.query(ArmsReading)
        .filter(ArmsReading.market == market, ArmsReading.book != "legacy")
        .order_by(ArmsReading.bar_date.asc(), ArmsReading.phase.asc())
        .all()
    )
    best: dict[tuple, object] = {}
    for row in readings:
        key = (row.bar_date, row.book or "legacy")
        prior = best.get(key)
        if prior is None or row.phase == "pm":
            best[key] = row
    changed = False
    for (day, book), row in best.items():
        if (day, book) in have:
            if row.above_ma20 is None:
                continue
            chart = (
                db.query(BreadthDay)
                .filter(BreadthDay.market == market, BreadthDay.bar_date == day, BreadthDay.book == book)
                .one_or_none()
            )
            if chart is not None and chart.above_ma20 is None:
                from decimal import Decimal

                chart.above_ma20 = Decimal(str(row.above_ma20))
                changed = True
            continue
        _remember_breadth_day(
            db,
            market,
            day,
            {
                "advancers": row.advancers,
                "decliners": row.decliners,
                "unchanged": row.unchanged,
                "up_ratio": None if row.up_ratio is None else float(row.up_ratio),
                "arms": None if row.arms is None else float(row.arms),
                "above_ma20": None if row.above_ma20 is None else float(row.above_ma20),
            },
            phase=row.phase,
            book=book,
        )
        changed = True
        have.add((day, book))
    if changed:
        db.commit()


def _save_arms_reading(db, market: str, day: date, phase: str, fresh: dict):
    from decimal import Decimal

    from app.models.entities import ArmsReading

    def dec(value):
        return None if value is None else Decimal(str(value))

    row = ArmsReading(
        market=market,
        bar_date=day,
        phase=phase,
        count=int(fresh.get("count") or 0),
        advancers=int(fresh.get("advancers") or 0),
        decliners=int(fresh.get("decliners") or 0),
        unchanged=int(fresh.get("unchanged") or 0),
        up_ratio=dec(fresh.get("up_ratio")),
        arms=dec(fresh.get("arms")),
        median_change=dec(fresh.get("median_change")),
        strong_up=None if fresh.get("strong_up") is None else int(fresh["strong_up"]),
        strong_down=None if fresh.get("strong_down") is None else int(fresh["strong_down"]),
        universe=str(fresh.get("universe") or ""),
        above_ma20=dec(fresh.get("above_ma20")),
        book=str(fresh.get("book") or "legacy"),
    )
    db.add(row)
    _remember_breadth_day(db, market, day, fresh, phase=phase, book=row.book)
    db.commit()
    return row


_MA_COVERAGE = 0.9
_CN_LIST = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
_LISTS = {
    "cn": _CN_LIST,
    "us": "m:105,m:106,m:107",
}


def _is_bse(code: str) -> bool:
    """Beijing Stock Exchange. These codes share Shenzhen's venue number."""
    return code.startswith(("92", "8", "4"))


def _quote_books(market: str, venue: int) -> list[str]:
    if market == "cn":
        books = ["cn_all"]
        if venue == 1:
            books.append("sse")
        elif venue == 0:
            books.append("szse")
        return books
    books = ["us_all"]
    if venue == 105:
        books.append("nyse")
    elif venue == 106:
        books.append("nasdaq")
    return books


def _live_prices(market: str) -> list[dict]:
    """Names that traded today, with the latest print. Lunch still shows the morning print."""
    url = "https://push2.eastmoney.com/api/qt/clist/get"
    fs = _LISTS.get(market)
    if not fs:
        return []
    page_size = 100

    def page(pn: int) -> dict:
        try:
            text = _request_text(
                url,
                params={
                    "pn": str(pn),
                    "pz": str(page_size),
                    "po": "1",
                    "np": "1",
                    "fltt": "2",
                    "invt": "2",
                    "fid": "f12",
                    "fs": fs,
                    "fields": "f2,f5,f12,f13",
                },
                encoding="utf-8",
                timeout=8,
            )
            return json.loads(text).get("data") or {}
        except Exception:
            return {}

    first = page(1)
    rows = list(first.get("diff") or [])
    if isinstance(rows, dict):
        rows = list(rows.values())
    try:
        total = int(first.get("total") or 0)
    except (TypeError, ValueError):
        return []
    pages = max(1, (total + page_size - 1) // page_size)
    if pages > 1:
        with ThreadPoolExecutor(max_workers=8) as pool:
            for batch in pool.map(page, range(2, min(pages, 160) + 1)):
                diff = batch.get("diff") or []
                if isinstance(diff, dict):
                    diff = list(diff.values())
                rows.extend(diff)
    prices: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        code = str(row.get("f12") or "").strip().upper()
        if not code or code in seen:
            continue
        raw_venue = row.get("f13")
        try:
            venue = int(float(raw_venue)) if raw_venue not in (None, "") else -1
            price = float(row.get("f2"))
            volume = float(row.get("f5") or 0)
        except (TypeError, ValueError):
            continue
        if price <= 0 or volume <= 0:
            continue
        if market == "cn" and _is_bse(code):
            venue = 2
        elif market == "cn" and venue not in {0, 1}:
            if code.startswith(("6", "9")):
                venue = 1
            elif code.startswith(("0", "3")):
                venue = 0
            else:
                continue
        if market == "us" and venue not in {105, 106, 107}:
            continue
        seen.add(code)
        if market == "cn":
            prefix = "sh" if venue == 1 else "bj" if venue == 2 else "sz"
            symbol = f"{prefix}{code}"
        else:
            symbol = code
        prices.append({"symbol": symbol, "code": code, "venue": venue, "price": price})
    if market == "cn" and len(prices) < 1000:
        return _cn_sina_prices()
    if market == "us" and len(prices) < 8000:
        logger.warning("US Eastmoney live list is short (%s names); using Sina", len(prices))
        return _us_sina_prices()
    return prices


def _us_sina_prices() -> list[dict]:
    """US prints when the Eastmoney list is empty or short. Same shape as `_live_prices`."""
    prices: list[dict] = []
    for item in _sina_us_listings():
        row = item["row"]
        try:
            price = float(row.get("price"))
            volume = float(row.get("volume") or 0)
        except (TypeError, ValueError):
            continue
        if price <= 0 or volume <= 0:
            continue
        prices.append(
            {
                "symbol": item["code"],
                "code": item["code"],
                "venue": item["venue"],
                "price": price,
            }
        )
    return prices


def _cn_sina_prices() -> list[dict]:
    """A-share prints when the Eastmoney list is short. Same shape as `_live_prices`.
    Sina's ticktime is only a clock, so the last trade is the current session's print.
    """
    prices: list[dict] = []
    seen: set[str] = set()
    for item in _sina_pages(_sina_cn_page, 80, 80):
        symbol = str(item.get("symbol") or "").strip().lower()
        code = str(item.get("code") or "").strip().upper()
        if not symbol or not code or code in seen:
            continue
        try:
            price = float(item.get("trade") or 0)
            volume = float(item.get("volume") or 0)
        except (TypeError, ValueError):
            continue
        if price <= 0 or volume <= 0:
            continue
        if symbol.startswith("sh"):
            venue = 1
        elif symbol.startswith("sz"):
            venue = 0
        elif symbol.startswith("bj") or _is_bse(code):
            venue = 2
        else:
            continue
        seen.add(code)
        prices.append({"symbol": symbol, "code": code, "venue": venue, "price": price})
    return prices


def _us_anchor_sum(code: str, venue: int, through: date) -> float | None:
    """Sum of the 19 completed US closes on or before `through`. Raw prices, same list as the quote."""
    try:
        text = _request_text(
            "https://push2his.eastmoney.com/api/qt/stock/kline/get",
            params={
                "secid": f"{venue}.{code}",
                "klt": "101",
                "fqt": "0",
                "beg": (through - timedelta(days=70)).strftime("%Y%m%d"),
                "end": through.strftime("%Y%m%d"),
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f53",
            },
            referer="https://quote.eastmoney.com/",
            encoding="utf-8",
            timeout=8,
        )
        klines = ((json.loads(text).get("data") or {}).get("klines")) or []
    except Exception:
        return None
    limit = through.isoformat()
    closes: list[float] = []
    for row in klines:
        parts = str(row).split(",")
        if len(parts) < 2 or parts[0] > limit:
            continue
        try:
            price = float(parts[1])
        except ValueError:
            continue
        if price > 0:
            closes.append(price)
    if len(closes) < 19:
        return None
    return sum(closes[-19:])


def _anchor_sum(code: str, through: date) -> float | None:
    """Sum of the 19 completed closes on or before `through`. Today's bar is not in the daily file yet."""
    closes = [price for day, price, _volume in _sina_closes(code) if day <= through.isoformat() and price > 0]
    if len(closes) < 19:
        return None
    return sum(closes[-19:])


def _fill_above_ma20(market: str, day: date, phase: str) -> None:
    """Per-book share of names above their own MA20. Written only at 90% coverage."""
    from decimal import Decimal

    from app.core.database import SessionLocal
    from app.models.entities import ArmsReading, BreadthDay, MaAnchor

    if market not in _LISTS:
        return
    through = _previous_weekday(day)
    prices = _live_prices(market)
    minimum = 1000 if market == "cn" else 8000
    if len(prices) < minimum:
        return
    db = SessionLocal()
    try:
        db.query(MaAnchor).filter(MaAnchor.market == market, MaAnchor.through_date != through).delete()
        stored = {
            row.symbol: float(row.close_sum)
            for row in db.query(MaAnchor).filter(MaAnchor.market == market, MaAnchor.through_date == through).all()
        }
        missing = [item for item in prices if item["symbol"] not in stored]

        def anchor(item: dict) -> tuple[str, float | None]:
            if market == "cn":
                return item["symbol"], _anchor_sum(item["symbol"], through)
            return item["symbol"], _us_anchor_sum(item["code"], item["venue"], through)

        def remember(found: list[tuple[str, float]]) -> None:
            if not found:
                return
            db.add_all(
                [
                    MaAnchor(market=market, symbol=symbol, through_date=through, close_sum=Decimal(str(round(total, 4))))
                    for symbol, total in found
                ]
            )
            db.commit()
            stored.update(found)

        def fetch(items: list[dict]) -> None:
            found: list[tuple[str, float]] = []
            with ThreadPoolExecutor(max_workers=16) as pool:
                for symbol, total in pool.map(anchor, items):
                    if total is None:
                        continue
                    found.append((symbol, total))
                    if len(found) >= 250:
                        remember(found)
                        found = []
            remember(found)

        if missing:
            fetch(missing)
            missing = [item for item in prices if item["symbol"] not in stored]
            if missing and len(stored) >= len(prices) * 0.5:
                fetch(missing)
        buckets = {
            key: {"above": 0, "valid": 0, "total": 0}
            for key, _label, _pair in _BOOKS[market]
        }
        for item in prices:
            total = stored.get(item["symbol"])
            above = total is not None and item["price"] > (total + item["price"]) / 20
            for book in _quote_books(market, item["venue"]):
                slot = buckets[book]
                slot["total"] += 1
                if total is None:
                    continue
                slot["valid"] += 1
                if above:
                    slot["above"] += 1
        for book, slot in buckets.items():
            row = (
                db.query(ArmsReading)
                .filter(
                    ArmsReading.market == market,
                    ArmsReading.bar_date == day,
                    ArmsReading.phase == phase,
                    ArmsReading.book == book,
                )
                .one_or_none()
            )
            if row is None:
                continue
            covered = slot["total"] > 0 and slot["valid"] >= 50 and slot["valid"] >= slot["total"] * _MA_COVERAGE
            if not covered:
                continue
            row.ma_valid = slot["valid"]
            row.ma_total = slot["total"]
            if row.above_ma20 is None:
                row.above_ma20 = Decimal(str(round(slot["above"] / slot["valid"] * 100, 1)))
            chart = (
                db.query(BreadthDay)
                .filter(BreadthDay.market == market, BreadthDay.bar_date == day, BreadthDay.book == book)
                .one_or_none()
            )
            if chart is not None and chart.above_ma20 is None and row.above_ma20 is not None:
                chart.above_ma20 = row.above_ma20
        db.commit()
    finally:
        db.close()


_ma20_running: set[str] = set()


def _schedule_above_ma20(market: str, day: date, phase: str) -> None:
    if market in _ma20_running:
        return
    _ma20_running.add(market)

    def work() -> None:
        try:
            _fill_above_ma20(market, day, phase)
        finally:
            _ma20_running.discard(market)

    threading.Thread(target=work, daemon=True, name=f"{market}-above-ma20").start()


_BOOKS = {
    "us": (
        ("us_all", "美股全市场", ""),
        ("nyse", "纽交所", "标普 500"),
        ("nasdaq", "纳斯达克", "纳斯达克"),
    ),
    "cn": (
        ("cn_all", "沪深全A", "中证全指"),
        ("sse", "上证", "上证指数"),
        ("szse", "深市", "深证成指"),
    ),
}


def _default_book(market: str) -> str:
    books = _BOOKS.get(market) or ()
    return books[0][0] if books else ""


def _book_meta(book: str) -> tuple[str, str]:
    for books in _BOOKS.values():
        for key, label, pair in books:
            if key == book:
                return label, pair
    return book, ""


def _clist_page(fs: str, fields: str, page: int) -> dict | None:
    try:
        text = _request_text(
            "https://push2.eastmoney.com/api/qt/clist/get",
            params={
                "pn": str(page),
                "pz": "100",
                "po": "1",
                "np": "1",
                "fltt": "2",
                "invt": "2",
                "fid": "f12",
                "fs": fs,
                "fields": fields,
            },
            referer="https://quote.eastmoney.com/",
            encoding="utf-8",
            timeout=12,
        )
        return json.loads(text).get("data") or {}
    except (OSError, json.JSONDecodeError):
        return None


def _clist_rows(fs: str, fields: str) -> list[dict]:
    """One page at a time. A failed page is skipped after three tries so a short prefix is not stored as the whole list."""
    rows: list[dict] = []
    total = None
    misses = 0
    page = 1
    while page <= 160:
        data = None
        for _attempt in range(3):
            data = _clist_page(fs, fields, page)
            if data:
                break
            time.sleep(1.5)
        if not data:
            misses += 1
            if total is None or misses >= 3:
                break
            page += 1
            continue
        if total is None:
            try:
                total = int(data.get("total") or 0)
            except (TypeError, ValueError):
                total = 0
        batch = data.get("diff") or []
        if isinstance(batch, dict):
            batch = list(batch.values())
        if not batch:
            if total and len(rows) < total:
                misses += 1
                if misses >= 3:
                    break
                page += 1
                continue
            break
        misses = 0
        rows.extend(batch)
        if total and len(rows) >= total:
            break
        if len(batch) < 100 and (not total or len(rows) >= total):
            break
        page += 1
        time.sleep(0.35)
    return rows


def _change_and_size(row: dict, amount: bool) -> tuple[float, float, float] | None:
    try:
        if "f3" in row:
            change = float(row.get("f3"))
            volume = float(row.get("f5") or 0)
            size = float(row.get("f6") or 0) if amount else volume
        else:
            raw_change = row.get("chg")
            if raw_change in (None, ""):
                raw_change = row.get("changepercent")
            change = float(raw_change)
            volume = float(row.get("volume") or 0)
            turnover = float(row.get("amount") or 0)
            size = turnover if amount and turnover > 0 else volume
    except (TypeError, ValueError):
        return None
    return change, volume, size


def _tally_rows(rows: list[dict], *, amount: bool, label: str) -> dict | None:
    up = down = flat = 0
    up_size = down_size = 0.0
    for row in rows:
        parsed = _change_and_size(row, amount)
        if parsed is None:
            continue
        change, volume, size = parsed
        if volume <= 0:
            continue
        if change > 0:
            up += 1
            up_size += size
        elif change < 0:
            down += 1
            down_size += size
        else:
            flat += 1
    return _breadth_payload(label, up, down, flat, _arms_value(up, down, up_size, down_size), minimum=80)


_SINA_US_LIST = (
    "https://stock.finance.sina.com.cn/usstock/api/jsonp.php/"
    "IO.XSRV2.CallbackList/US_CategoryService.getList"
)
_SINA_CN_LIST = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "Market_Center.getHQNodeData"
)


def _sina_us_page(page: int) -> list[dict]:
    """One page of the Sina US list. The body is JSONP, sometimes preceded by a redirect stub."""
    text = _request_text(
        _SINA_US_LIST,
        params={"page": str(page), "num": "20", "sort": "", "asc": "0", "market": "", "id": ""},
        referer="https://finance.sina.com.cn/",
        encoding="utf-8",
        timeout=8,
    )
    match = re.search(r"\((\{.*\})\)", text, flags=re.DOTALL)
    if not match:
        return []
    payload = json.loads(match.group(1))
    rows = payload.get("data") if isinstance(payload, dict) else None
    return [row for row in rows or [] if isinstance(row, dict)]


def _sina_cn_page(page: int) -> list[dict]:
    """One page of Sina's Shanghai and Shenzhen A-share list, including Beijing names."""
    text = _request_text(
        _SINA_CN_LIST,
        params={"page": str(page), "num": "80", "sort": "symbol", "asc": "1", "node": "hs_a"},
        referer="https://finance.sina.com.cn/",
        encoding="utf-8",
        timeout=8,
    )
    rows = json.loads(text)
    return [row for row in rows or [] if isinstance(row, dict)]


def _sina_pages(fetch, page_size: int, limit: int) -> list[dict]:
    rows: list[dict] = []
    for page in range(1, limit + 1):
        try:
            batch = fetch(page)
        except Exception:
            time.sleep(2)
            try:
                batch = fetch(page)
            except Exception:
                break
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < page_size:
            break
        time.sleep(0.5)
    return rows


def _eastmoney_us_venue(market: str) -> int:
    """Sina's exchange name to the Eastmoney market id used by the saved books.

    105 is Nasdaq and 106 is NYSE. The stored book keys follow those ids.
    """
    text = market.upper()
    if "NASDAQ" in text:
        return 105
    if "NYSE" in text:
        return 106
    if "AMEX" in text or "ARCA" in text:
        return 107
    return 0


def _sina_us_listings() -> list[dict]:
    """Every Sina US name, with the Eastmoney venue id. Empty when the list is short."""
    try:
        text = _request_text(
            _SINA_US_LIST,
            params={"page": "1", "num": "20", "sort": "", "asc": "0", "market": "", "id": ""},
            referer="https://finance.sina.com.cn/",
            encoding="utf-8",
            timeout=12,
        )
        match = re.search(r"\((\{.*\})\)", text, flags=re.DOTALL)
        payload = json.loads(match.group(1)) if match else {}
    except Exception:
        return []
    try:
        count = int(payload.get("count") or 0)
    except (TypeError, ValueError):
        count = 0
    pages = max(1, (count + 19) // 20) if count else 1
    rows = [row for row in payload.get("data") or [] if isinstance(row, dict)]

    def load(page: int) -> list[dict]:
        try:
            return _sina_us_page(page)
        except Exception:
            time.sleep(0.8)
            try:
                return _sina_us_page(page)
            except Exception:
                return []

    if pages > 1:
        with ThreadPoolExecutor(max_workers=8) as pool:
            for batch in pool.map(load, range(2, min(pages, 1200) + 1)):
                rows.extend(batch)
    listings = []
    seen: set[str] = set()
    for item in rows:
        code = str(item.get("symbol") or "").strip().upper()
        if not code or code in seen:
            continue
        venue = _eastmoney_us_venue(str(item.get("market") or ""))
        if venue not in {105, 106, 107}:
            continue
        seen.add(code)
        listings.append({"code": code, "venue": venue, "row": item})
    if len(listings) < 8000:
        logger.warning("Sina US list is short (%s names)", len(listings))
        return []
    return listings


def _scan_us_sina() -> dict[str, list]:
    groups = {"us_all": [], "nyse": [], "nasdaq": []}
    for item in _sina_us_listings():
        row = item["row"]
        groups["us_all"].append(row)
        if item["venue"] == 105:
            groups["nyse"].append(row)
        elif item["venue"] == 106:
            groups["nasdaq"].append(row)
    if len(groups["us_all"]) < 8000 or not groups["nyse"] or not groups["nasdaq"]:
        return {"us_all": [], "nyse": [], "nasdaq": []}
    return groups


def _scan_us_groups() -> dict[str, list]:
    rows = _clist_rows("m:105,m:106,m:107", "f3,f5,f13")
    groups = {"us_all": [], "nyse": [], "nasdaq": []}
    for row in rows:
        groups["us_all"].append(row)
        try:
            venue = int(float(row.get("f13") or 0))
        except (TypeError, ValueError):
            venue = 0
        if venue == 105:
            groups["nyse"].append(row)
        elif venue == 106:
            groups["nasdaq"].append(row)
    if len(groups["us_all"]) < 8000 or not groups["nyse"] or not groups["nasdaq"]:
        logger.warning("US Eastmoney list is short (%s names); using Sina", len(groups["us_all"]))
        return _scan_us_sina()
    return groups


def _scan_cn_sina() -> list[dict]:
    rows = []
    for item in _sina_pages(_sina_cn_page, 80, 80):
        change = item.get("changepercent")
        rows.append(
            {
                "symbol": item.get("symbol") or "",
                "code": item.get("code") or "",
                "chg": change,
                "volume": item.get("volume") or 0,
                "amount": item.get("amount") or 0,
            }
        )
    return rows


def _scan_cn_groups() -> dict[str, list]:
    rows = _clist_rows(_CN_LIST, "f3,f5,f6,f12,f13")
    if len(rows) < 1000:
        rows = _scan_cn_sina()
    groups = {"cn_all": [], "sse": [], "szse": []}
    for row in rows:
        groups["cn_all"].append(row)
        code = str(row.get("f12") or row.get("code") or row.get("symbol") or "")
        if _is_bse(code):
            continue
        raw_venue = row.get("f13")
        venue = "" if raw_venue in (None, "") else str(raw_venue)
        if venue in {"1", "1.0"} or code.startswith(("sh", "6")):
            groups["sse"].append(row)
        elif venue in {"0", "0.0"} or code.startswith(("sz", "0", "3")):
            groups["szse"].append(row)
    return groups


_book_cache: dict[str, tuple[float, list]] = {}
_scan_threads: dict[str, threading.Thread] = {}
_scan_lock = threading.Lock()


def _store_books(market: str, groups: dict[str, list]) -> list[dict]:
    amount = market == "cn"
    books = []
    for key, label, pair in _BOOKS[market]:
        payload = _tally_rows(groups.get(key) or [], amount=amount, label=label)
        if not payload:
            continue
        payload["book"] = key
        payload["key"] = key
        payload["pair"] = pair
        payload["universe"] = f"{label} {payload['count']} 家"
        books.append(payload)
    if books:
        _book_cache[market] = (time.time(), books)
    return books


def _fill_book_cache(market: str) -> None:
    try:
        groups = _scan_us_groups() if market == "us" else _scan_cn_groups()
        _store_books(market, groups)
    finally:
        with _scan_lock:
            _scan_threads.pop(market, None)


def scan_market_books(market: str) -> list[dict]:
    """Return the cached split. A cold or stale cache starts one sequential scan and does not wait on it."""
    if market not in _BOOKS:
        return []
    now = time.time()
    cached = _book_cache.get(market)
    if cached and now - cached[0] < _LIVE_BREADTH_SECONDS:
        return cached[1]
    with _scan_lock:
        if market not in _scan_threads:
            thread = threading.Thread(target=_fill_book_cache, args=(market,), daemon=True, name=f"breadth-{market}")
            _scan_threads[market] = thread
            thread.start()
    return cached[1] if cached else []


def scan_pending(market: str) -> bool:
    return market in _scan_threads


_arms_compute_lock = threading.Lock()


def _public_reading(row) -> dict:
    data = _reading_dict(row)
    label, pair = _book_meta(getattr(row, "book", "") or "")
    data["key"] = getattr(row, "book", "") or ""
    data["pair"] = pair
    if label and data.get("count"):
        data["universe"] = f"{label} {data['count']} 家"
    return data


def ensure_arms_reading(market: str, now: datetime | None = None):
    """Return the full-market close, plus the other books saved at the same checkpoint."""
    from app.core.database import SessionLocal
    from app.models.entities import ArmsReading
    from app.services.bars import cash_session_closed, remember_breadth
    from app.services.ledger import us_equity_session_date

    if market == "hk":
        return None, "", []
    db = SessionLocal()
    try:
        due = arms_due(market, now)
        rows = []
        if due:
            day, phase = due
            wanted = {key for key, _label, _pair in _BOOKS.get(market, ())}

            def saved():
                return (
                    db.query(ArmsReading)
                    .filter(
                        ArmsReading.market == market,
                        ArmsReading.bar_date == day,
                        ArmsReading.phase == phase,
                        ArmsReading.book.in_(wanted),
                    )
                    .all()
                )

            rows = saved()
            if wanted - {row.book for row in rows}:
                with _arms_compute_lock:
                    rows = saved()
                    missing = wanted - {row.book for row in rows}
                    if missing:
                        for fresh in scan_market_books(market):
                            if fresh["book"] not in missing:
                                continue
                            row = _save_arms_reading(db, market, day, phase, fresh)
                            rows.append(row)
                            if (
                                market == "us"
                                and phase == "pm"
                                and fresh["book"] == "us_all"
                                and cash_session_closed(now)
                                and us_equity_session_date(now) == day
                            ):
                                remember_breadth(db, fresh)
        if not rows:
            latest = (
                db.query(ArmsReading)
                .filter(ArmsReading.market == market, ArmsReading.book == _default_book(market))
                .order_by(ArmsReading.bar_date.desc(), ArmsReading.phase.desc())
                .first()
            )
            if latest is not None:
                rows = (
                    db.query(ArmsReading)
                    .filter(
                        ArmsReading.market == market,
                        ArmsReading.bar_date == latest.bar_date,
                        ArmsReading.phase == latest.phase,
                        ArmsReading.book != "legacy",
                    )
                    .all()
                )
        primary = next((row for row in rows if row.book == _default_book(market)), None)
        if primary is None:
            return None, "", []
        # Retry while the percentage is still blank. An earlier partial write of
        # ma_total used to stop the retry even though above_ma20 was never set.
        pending = bool(due) and any(
            row.above_ma20 is None and row.bar_date == due[0] and row.phase == due[1]
            for row in rows
        )
        if pending:
            _schedule_above_ma20(market, due[0], due[1])
        books = [_public_reading(row) for row in rows]
        if pending:
            for item in books:
                if item.get("above_ma20") is None:
                    item["ma_pending"] = True
        books.sort(key=lambda item: _book_order(market, item.get("key")))
        return _public_reading(primary), _arms_caption(primary.phase), books
    finally:
        db.close()


_close_stop = threading.Event()
_close_thread: threading.Thread | None = None
_CLOSE_MARKETS = ("cn", "us")


def _checkpoint_gaps(market: str, day: date, phase: str) -> tuple[bool, bool]:
    """Whether this close still needs a name list, and whether MA20 is still open."""
    from app.core.database import SessionLocal
    from app.models.entities import ArmsReading

    wanted = {key for key, _label, _pair in _BOOKS.get(market, ())}
    db = SessionLocal()
    try:
        rows = (
            db.query(ArmsReading.book, ArmsReading.ma_total)
            .filter(
                ArmsReading.market == market,
                ArmsReading.bar_date == day,
                ArmsReading.phase == phase,
                ArmsReading.book.in_(wanted),
            )
            .all()
        )
    finally:
        db.close()
    found = {row.book for row in rows}
    return bool(wanted - found), any(row.above_ma20 is None for row in rows)


def capture_due_close(market: str) -> None:
    """Store the finished session once. A later pass only fills MA20."""
    due = arms_due(market)
    if not due or market not in _BOOKS:
        return
    day, phase = due
    missing, ma_pending = _checkpoint_gaps(market, day, phase)
    if not missing and not ma_pending:
        return
    if missing:
        cached = _book_cache.get(market)
        fresh = bool(cached) and time.time() - cached[0] < _LIVE_BREADTH_SECONDS
        if not fresh:
            with _scan_lock:
                if market in _scan_threads:
                    return
                _scan_threads[market] = threading.current_thread()
            try:
                logger.warning("close capture scan %s %s %s", market, day.isoformat(), phase)
                groups = _scan_us_groups() if market == "us" else _scan_cn_groups()
                _store_books(market, groups)
            finally:
                with _scan_lock:
                    _scan_threads.pop(market, None)
    elif ma_pending:
        logger.warning("close capture ma20 %s %s %s", market, day.isoformat(), phase)
    ensure_arms_reading(market)


def _close_loop() -> None:
    while not _close_stop.is_set():
        for market in _CLOSE_MARKETS:
            if _close_stop.is_set():
                return
            try:
                capture_due_close(market)
            except Exception:
                logger.exception("close capture failed for %s", market)
        _close_stop.wait(60)


def start_close_capture() -> None:
    """After each cash close, store breadth, Arms, and MA20 without a page visit."""
    global _close_thread
    if _close_thread and _close_thread.is_alive():
        return
    _close_stop.clear()
    _close_thread = threading.Thread(target=_close_loop, daemon=True, name="close-capture")
    _close_thread.start()
    logger.warning("close capture started")


def stop_close_capture() -> None:
    _close_stop.set()


def _book_order(market: str, key: str | None) -> int:
    keys = [item[0] for item in _BOOKS.get(market, ())]
    try:
        return keys.index(key or "")
    except ValueError:
        return len(keys)


_open_counts_cache: dict[str, tuple[float, dict | None]] = {}


def _stored_session_counts(market: str) -> dict | None:
    """Last completed history row, without an Arms figure, when the live list fails."""
    from app.core.database import SessionLocal
    from app.models.entities import BreadthDay

    db = SessionLocal()
    try:
        row = (
            db.query(BreadthDay)
            .filter(BreadthDay.market == market, BreadthDay.book == _default_book(market))
            .order_by(BreadthDay.bar_date.desc())
            .first()
        )
    finally:
        db.close()
    if row is None:
        return None
    up, down, flat = row.advancers, row.decliners, row.unchanged
    count = up + down + flat
    if count <= 0:
        return None
    label, _pair = _book_meta(_default_book(market))
    universe = label or ("美股" if market == "us" else "沪深A股")
    return {
        "universe": universe,
        "count": count,
        "advancers": up,
        "decliners": down,
        "unchanged": flat,
        "up_ratio": None if row.up_ratio is None else float(row.up_ratio),
        "arms": None,
        "median_change": None,
        "strong_up": None,
        "strong_down": None,
        "prior_date": row.bar_date.isoformat(),
    }


def _open_session_counts(market: str) -> list[dict] | None:
    """Live advance/decline and Arms. This print is not written onto the daily chart."""
    if arms_due(market) is not None:
        return None
    now = time.time()
    live = [dict(item) for item in scan_market_books(market)]
    if live:
        return live
    cached = _open_counts_cache.get(market)
    if cached and now - cached[0] < 30:
        return cached[1]
    prior = _stored_session_counts(market)
    if prior:
        prior["key"] = _default_book(market)
        prior["pair"] = _book_meta(prior["key"])[1]
        live = [prior]
    _open_counts_cache[market] = (now if live else now - 15, live)
    return live


def _attach_arms_reading(payload: dict, market: str) -> None:
    def finish(breadth: dict | None, books: list[dict], extra: str) -> None:
        if market == "us":
            label, tone, note = _read(breadth, payload.get("vix") or {})
        else:
            label, tone, note = _read_ratio(breadth, (breadth or {}).get("universe") or "沪深A股")
        if extra:
            note = f"{note}{extra}"
        if market == "us" and breadth and breadth.get("arms") is not None:
            note = f"{note}按成分自算，不是交易所发布的 TRIN。"
        payload["breadth"] = breadth
        payload["books"] = books
        payload["label"] = label
        payload["tone"] = tone
        payload["note"] = note

    live = _open_session_counts(market)
    if live is not None:
        if not live and scan_pending(market):
            finish(None, [], "正在分批读取涨跌家数和阿姆士。")
            return
        books = [{key: value for key, value in item.items() if key != "prior_date"} for item in live]
        primary = next((item for item in books if item.get("key") == _default_book(market)), books[0] if books else None)
        extra = "这是盘中这一刻，历史图仍用收盘。"
        dated = next((item.get("prior_date") for item in live if item.get("prior_date")), None)
        if dated:
            extra = f"涨跌家数是 {dated} 的。阿姆士收盘后才写入历史。"
        finish(primary, books, extra)
        return
    breadth, caption, books = ensure_arms_reading(market)
    if breadth is None and scan_pending(market):
        extra = "正在分批读取这一套的涨跌家数。"
    else:
        extra = caption if breadth else "还没有收盘结果，阿姆士先空着。"
    finish(breadth, books, extra)


def _read_ratio(breadth: dict | None, name: str) -> tuple[str, str, str]:
    if not breadth or breadth.get("up_ratio") is None:
        return "中性", "info", f"{name}涨跌家数这次没有取到，只看指数。"
    ratio = breadth["up_ratio"]
    universe = breadth.get("universe") or name
    if ratio < 40:
        return "偏弱", "warning", f"{universe}下跌家数更多。"
    if ratio > 60:
        return "偏强", "success", f"{universe}上涨家数更多。"
    return "中性", "info", f"{universe}涨跌接近对半，没有单边。"


_HISTORY_DAYS = 45


def _sina_closes(code: str) -> list[tuple[str, float, float]]:
    try:
        text = _request_text(
            "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData",
            params={"symbol": code, "scale": 240, "ma": "no", "datalen": 48},
            referer="https://finance.sina.com.cn/",
            encoding="utf-8",
            timeout=8,
        )
        payload = json.loads(text)
    except Exception:
        return []
    if not isinstance(payload, list):
        return []
    bars: list[tuple[str, float, float]] = []
    for row in payload:
        try:
            bars.append((str(row["day"]), float(row["close"]), float(row["volume"])))
        except (KeyError, TypeError, ValueError):
            continue
    bars.sort()
    return bars


def breadth_history_payload(db, market: str, book: str = "") -> dict:
    from app.models.entities import BreadthDay

    market = market if market in {"us", "cn", "hk"} else "us"
    book = book or _default_book(market)
    _sync_breadth_days_from_arms(db, market)
    stored = (
        db.query(BreadthDay)
        .filter(BreadthDay.market == market, BreadthDay.book == book)
        .order_by(BreadthDay.bar_date.asc())
        .all()
    )
    basis = "成交量" if market == "us" else "成交额"
    label, pair = _book_meta(book)
    universe = label or {"us": "美股", "cn": "沪深A股", "hk": "港股"}[market]
    if pair:
        universe = f"{universe}，对照{pair}"
    notes = {
        "us": "按这一套成分的涨跌家数和成交股数自算，不是交易所发布的 TRIN。高于 MA20 用未复权收盘价，样本太大时这一格先空着。",
        "cn": "按这一套成分的涨跌家数和成交额自算。高于 MA20 用未复权收盘价，样本太大时这一格先空着。",
        "hk": "港股日线这会儿没有返回，历史图先空着。",
    }
    note = notes[market]
    return {
        "universe": universe,
        "basis": basis,
        "note": note,
        "points": [
            {
                "date": row.bar_date.isoformat(),
                "advancers": row.advancers,
                "decliners": row.decliners,
                "unchanged": row.unchanged,
                "up_ratio": None if row.up_ratio is None else float(row.up_ratio),
                "breadth": None if row.breadth is None else float(row.breadth),
                "arms": None if row.arms is None else float(row.arms),
                "above_ma20": None if row.above_ma20 is None else float(row.above_ma20),
            }
            for row in stored[-_HISTORY_DAYS:]
        ],
    }


def _index_tencent(db, name: str, symbol: str) -> dict:
    from app.services.bars import cn_frozen, hk_frozen, load_bars
    from app.services.market import fetch_tencent_bars
    from app.services.market_scope import market_of

    empty = {"name": name, "last": None, "day_change": None, "ret_5d": None, "history": []}
    kind = market_of(symbol)
    frozen = hk_frozen() if kind == "hk" else cn_frozen()
    try:
        rows = load_bars(db, symbol, lambda: fetch_tencent_bars(symbol, lot_size=1), frozen=frozen)
    except Exception:
        return empty
    return _index_view(name, rows)


def _index_view(name: str, rows: list[dict]) -> dict:
    empty = {"name": name, "last": None, "day_change": None, "ret_5d": None, "ma20": None, "vs_ma20": None, "history": []}
    bars = [(row["d"], row["c"]) for row in rows if row.get("c")]
    if len(bars) < 2:
        return empty
    floor = chart_since().isoformat()
    tail = [(day, price) for day, price in bars if day >= floor]
    if len(tail) < 2:
        return empty
    last = bars[-1][1]
    prev = bars[-2][1]
    base = bars[-6][1] if len(bars) >= 6 else None
    return {
        "name": name,
        "last": round(last, 2),
        "day_change": round((last / prev - 1) * 100, 2) if prev else None,
        "ret_5d": None if not base else round((last / base - 1) * 100, 2),
        **_ma20_fields([price for _, price in bars], last),
        "history": [{"date": day, "close": round(price, 2)} for day, price in tail],
    }

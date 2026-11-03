"""Market-wide sentiment from public daily series.

Breadth is the advance/decline count of the largest 500 US names on the
Sina US list (a large-cap tape, not the full NYSE). VIX is the Cboe daily
history file. Index paths are the Sina daily bars for the S&P 500 and Nasdaq.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from statistics import median, mean

from app.services.bars import chart_since
from app.services.market import _request_text

_CACHE_SECONDS = 600
_lock = threading.Lock()
_caches: dict[str, dict] = {}

_BREADTH_URL = (
    "https://stock.finance.sina.com.cn/usstock/api/jsonp.php/var/"
    "US_CategoryService.getList"
)
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


def _breadth_page(page: int) -> list[dict]:
    # The list ignores `num` and returns 20 names per page, ordered by market cap.
    try:
        text = _request_text(
            _BREADTH_URL,
            params={"page": page, "num": 20, "sort": "", "asc": 0, "market": "", "id": ""},
            referer="https://finance.sina.com.cn/",
            timeout=8,
        )
    except Exception:
        return []
    match = re.search(r"\((\{.*\})\)", text, flags=re.DOTALL)
    if not match:
        return []
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        return []
    rows = []
    for row in payload.get("data") or []:
        symbol = str(row.get("symbol") or "")
        if not symbol:
            continue
        try:
            rows.append(
                {
                    "symbol": symbol,
                    "price": float(row.get("price")),
                    "preclose": float(row.get("preclose")),
                    "chg": float(row.get("chg")),
                    "volume": float(row.get("volume") or 0),
                }
            )
        except (TypeError, ValueError):
            continue
    return rows


def _breadth() -> dict | None:
    """Largest 500 US names. Issue counts and TRIN share that list; TRIN uses share volume."""
    seen: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        for rows in pool.map(_breadth_page, range(1, 26)):
            for row in rows:
                seen.setdefault(row["symbol"], row)
    up = down = flat = 0
    up_volume = down_volume = 0.0
    changes: list[float] = []
    for row in seen.values():
        price, preclose, volume = row["price"], row["preclose"], row["volume"]
        if preclose <= 0 or price <= 0 or volume <= 0:
            continue
        changes.append(row["chg"])
        if price > preclose:
            up += 1
            up_volume += volume
        elif price < preclose:
            down += 1
            down_volume += volume
        else:
            flat += 1
    return _breadth_payload(
        "市值前 500 只美股",
        up,
        down,
        flat,
        _arms_value(up, down, up_volume, down_volume),
        changes=changes,
        minimum=50,
    )


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
    if local.weekday() < 5 and 9 * 60 + 30 <= minutes < 16 * 60:
        return None
    if local.weekday() < 5 and minutes >= 16 * 60:
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
    }


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
    )
    db.add(row)
    db.commit()
    return row


def _cn_live_prices() -> list[tuple[str, float]]:
    """Names that traded today, with the latest print. Lunch still shows the morning print."""
    url = "https://push2.eastmoney.com/api/qt/clist/get"
    fs = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
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
    try:
        total = int(first.get("total") or 0)
    except (TypeError, ValueError):
        return []
    pages = max(1, (total + page_size - 1) // page_size)
    if pages > 1:
        with ThreadPoolExecutor(max_workers=8) as pool:
            for batch in pool.map(page, range(2, pages + 1)):
                rows.extend(batch.get("diff") or [])
    prices: list[tuple[str, float]] = []
    for row in rows:
        code = str(row.get("f12") or "")
        if not code:
            continue
        prefix = "sh" if str(row.get("f13")) == "1" else "sz" if str(row.get("f13")) == "0" else ""
        if not prefix:
            continue
        try:
            price = float(row.get("f2"))
            volume = float(row.get("f5") or 0)
        except (TypeError, ValueError):
            continue
        if price <= 0 or volume <= 0:
            continue
        prices.append((f"{prefix}{code}", price))
    return prices


def _anchor_sum(code: str, through: date) -> float | None:
    """Sum of the 19 completed closes on or before `through`. Today's bar is not in the daily file yet."""
    closes = [price for day, price, _volume in _sina_closes(code) if day <= through.isoformat() and price > 0]
    if len(closes) < 19:
        return None
    return sum(closes[-19:])


def _fill_cn_above_ma20(day: date, phase: str) -> None:
    from decimal import Decimal

    from app.core.database import SessionLocal
    from app.models.entities import ArmsReading, MaAnchor

    through = _previous_weekday(day)
    prices = _cn_live_prices()
    if len(prices) < 1000:
        return
    db = SessionLocal()
    try:
        db.query(MaAnchor).filter(MaAnchor.market == "cn", MaAnchor.through_date != through).delete()
        stored = {
            row.symbol: float(row.close_sum)
            for row in db.query(MaAnchor).filter(MaAnchor.market == "cn", MaAnchor.through_date == through).all()
        }
        missing = [symbol for symbol, _price in prices if symbol not in stored]
        if missing:
            found: list[tuple[str, float]] = []
            with ThreadPoolExecutor(max_workers=16) as pool:
                for symbol, total in zip(missing, pool.map(lambda code: _anchor_sum(code, through), missing)):
                    if total is not None:
                        found.append((symbol, total))
            if found:
                db.add_all(
                    [
                        MaAnchor(market="cn", symbol=symbol, through_date=through, close_sum=Decimal(str(round(total, 4))))
                        for symbol, total in found
                    ]
                )
                db.commit()
                stored.update(found)
        above = valid = 0
        for symbol, price in prices:
            total = stored.get(symbol)
            if total is None:
                continue
            valid += 1
            if price > (total + price) / 20:
                above += 1
        if valid < 50 or valid < len(prices) * 0.6:
            return
        row = (
            db.query(ArmsReading)
            .filter(ArmsReading.market == "cn", ArmsReading.bar_date == day, ArmsReading.phase == phase)
            .one_or_none()
        )
        if row is None or row.above_ma20 is not None:
            return
        row.above_ma20 = Decimal(str(round(above / valid * 100, 1)))
        db.commit()
    finally:
        db.close()


_ma20_running = False


def _schedule_cn_above_ma20(day: date, phase: str) -> None:
    global _ma20_running
    if _ma20_running:
        return
    _ma20_running = True

    def work() -> None:
        global _ma20_running
        try:
            _fill_cn_above_ma20(day, phase)
        finally:
            _ma20_running = False

    threading.Thread(target=work, daemon=True, name="cn-above-ma20").start()


_arms_compute_lock = threading.Lock()


def ensure_arms_reading(market: str, now: datetime | None = None):
    """Return the stored close reading. Scan the market only when that close has no row yet."""
    from app.core.database import SessionLocal
    from app.models.entities import ArmsReading
    from app.services.bars import cash_session_closed, remember_breadth
    from app.services.ledger import us_equity_session_date

    if market == "hk":
        return None, ""
    db = SessionLocal()
    try:
        due = arms_due(market, now)
        row = None
        if due:
            day, phase = due
            row = (
                db.query(ArmsReading)
                .filter(ArmsReading.market == market, ArmsReading.bar_date == day, ArmsReading.phase == phase)
                .one_or_none()
            )
            if row is None:
                with _arms_compute_lock:
                    row = (
                        db.query(ArmsReading)
                        .filter(ArmsReading.market == market, ArmsReading.bar_date == day, ArmsReading.phase == phase)
                        .one_or_none()
                    )
                    if row is None:
                        fresh = _breadth_a_shares() if market == "cn" else _breadth()
                        if fresh:
                            row = _save_arms_reading(db, market, day, phase, fresh)
                            if (
                                market == "us"
                                and phase == "pm"
                                and cash_session_closed(now)
                                and us_equity_session_date(now) == day
                            ):
                                remember_breadth(db, fresh)
        if row is None:
            row = (
                db.query(ArmsReading)
                .filter(ArmsReading.market == market)
                .order_by(ArmsReading.bar_date.desc(), ArmsReading.phase.desc())
                .first()
            )
        if row is None:
            return None, ""
        if (
            market == "cn"
            and due
            and row.above_ma20 is None
            and row.bar_date == due[0]
            and row.phase == due[1]
        ):
            _schedule_cn_above_ma20(due[0], due[1])
        return _reading_dict(row), _arms_caption(row.phase)
    finally:
        db.close()


_open_counts_cache: dict[str, tuple[float, dict | None]] = {}


def _cn_board_counts() -> dict | None:
    """Advance/decline from the CSI All Share quote. One request, no Arms."""
    try:
        text = _request_text(
            "https://push2.eastmoney.com/api/qt/stock/get",
            params={"secid": "1.000985", "fltt": "2", "fields": "f113,f114,f115"},
            referer="https://quote.eastmoney.com/",
            encoding="utf-8",
            timeout=8,
        )
        data = json.loads(text).get("data") or {}
        up, down, flat = int(data["f113"]), int(data["f114"]), int(data["f115"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return _breadth_payload("中证全指", up, down, flat, None)


def _stored_session_counts(market: str) -> dict | None:
    """Last completed history row, without an Arms figure, when the live list fails."""
    from app.core.database import SessionLocal
    from app.models.entities import BreadthDay

    db = SessionLocal()
    try:
        row = (
            db.query(BreadthDay)
            .filter(BreadthDay.market == market)
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
    universe = "市值前 500 只美股" if market == "us" else "沪深A股"
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


def _open_session_counts(market: str) -> dict | None:
    """上涨、下跌、占比、宽度 while the session is still printing. Arms stays empty."""
    if arms_due(market) is not None:
        return None
    now = time.time()
    cached = _open_counts_cache.get(market)
    if cached and now - cached[0] < _CACHE_SECONDS:
        return cached[1]
    fresh = _breadth() if market == "us" else _cn_board_counts()
    prior = None
    if fresh:
        fresh = dict(fresh)
        fresh["arms"] = None
    else:
        prior = _stored_session_counts(market)
        fresh = prior
    _open_counts_cache[market] = (now if fresh else now - _CACHE_SECONDS + 30, fresh)
    return fresh


def _attach_arms_reading(payload: dict, market: str) -> None:
    live = _open_session_counts(market)
    if live:
        breadth = {key: value for key, value in live.items() if key != "prior_date"}
        if market == "us":
            label, tone, note = _read(breadth, payload.get("vix") or {})
        else:
            label, tone, note = _read_ratio(breadth, breadth.get("universe") or "沪深A股")
        if live.get("prior_date"):
            note = f"{note}涨跌家数是 {live['prior_date']} 的。"
        note = f"{note}阿姆士收盘后才计算。"
        payload["breadth"] = breadth
        payload["label"] = label
        payload["tone"] = tone
        payload["note"] = note
        return
    breadth, caption = ensure_arms_reading(market)
    if market == "us":
        label, tone, note = _read(breadth, payload.get("vix") or {})
    else:
        label, tone, note = _read_ratio(breadth, "沪深A股")
    if breadth is None:
        note = "还没有收盘结果，阿姆士先空着。"
    elif caption:
        note = f"{note}{caption}"
    payload["breadth"] = breadth
    payload["label"] = label
    payload["tone"] = tone
    payload["note"] = note


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


def _breadth_a_shares_eastmoney() -> dict | None:
    """All Shanghai and Shenzhen A-shares. TRIN uses turnover, not share volume."""
    url = "https://push2.eastmoney.com/api/qt/clist/get"
    fs = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
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
                    "fields": "f3,f5,f6",
                },
                encoding="utf-8",
                timeout=8,
            )
            return json.loads(text).get("data") or {}
        except Exception:
            return {}

    first = page(1)
    rows = list(first.get("diff") or [])
    try:
        total = int(first.get("total") or 0)
    except (TypeError, ValueError):
        return None
    pages = max(1, (total + page_size - 1) // page_size)
    if pages > 1:
        with ThreadPoolExecutor(max_workers=8) as pool:
            for batch in pool.map(page, range(2, pages + 1)):
                rows.extend(batch.get("diff") or [])
    if total < 1000 or len(rows) < total * 0.9:
        return None
    up = down = flat = 0
    up_amount = down_amount = 0.0
    for row in rows:
        try:
            change = float(row.get("f3"))
            volume = float(row.get("f5") or 0)
            amount = float(row.get("f6") or 0)
        except (TypeError, ValueError):
            continue
        if volume <= 0:
            continue
        if change > 0:
            up += 1
            up_amount += amount
        elif change < 0:
            down += 1
            down_amount += amount
        else:
            flat += 1
    return _breadth_payload("沪深A股", up, down, flat, _arms_value(up, down, up_amount, down_amount))


def _sina_a_page(page: int) -> list[dict]:
    try:
        text = _request_text(
            "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData",
            params={"page": page, "num": 100, "sort": "symbol", "asc": 1, "node": "hs_a", "symbol": ""},
            referer="https://finance.sina.com.cn/",
            encoding="utf-8",
            timeout=12,
        )
        payload = json.loads(text)
    except Exception:
        return []
    return payload if isinstance(payload, list) else []


def _breadth_a_shares_sina() -> dict | None:
    """Same Shanghai and Shenzhen pool when the Eastmoney list is unavailable."""
    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for batch in pool.map(_sina_a_page, range(1, 61)):
            rows.extend(batch)
    shsz = [row for row in rows if str(row.get("symbol") or "").startswith(("sh", "sz"))]
    if len(shsz) < 2000:
        return None
    up = down = flat = 0
    up_amount = down_amount = 0.0
    for row in shsz:
        try:
            price = float(row.get("trade") or 0)
            preclose = float(row.get("settlement") or 0)
            volume = float(row.get("volume") or 0)
            amount = float(row.get("amount") or 0)
        except (TypeError, ValueError):
            continue
        if price <= 0 or preclose <= 0 or volume <= 0:
            continue
        if price > preclose:
            up += 1
            up_amount += amount
        elif price < preclose:
            down += 1
            down_amount += amount
        else:
            flat += 1
    return _breadth_payload("沪深A股", up, down, flat, _arms_value(up, down, up_amount, down_amount))


def _breadth_a_shares() -> dict | None:
    listed = _breadth_a_shares_eastmoney()
    return listed if listed is not None else _breadth_a_shares_sina()


def _hk_page(page: int) -> list[dict]:
    try:
        text = _request_text(
            "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHKStockData",
            params={"page": page, "num": 60, "sort": "symbol", "asc": 1, "node": "qbgg_hk"},
            referer="https://finance.sina.com.cn/",
            encoding="utf-8",
            timeout=12,
        )
        payload = json.loads(text)
    except Exception:
        return []
    return payload if isinstance(payload, list) else []


def _breadth_hk() -> dict | None:
    """Hong Kong names on the Sina full list. TRIN uses turnover, not share volume."""
    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        for batch in pool.map(_hk_page, range(1, 48)):
            rows.extend(batch)
    if len(rows) < 1500:
        return None
    up = down = flat = 0
    up_amount = down_amount = 0.0
    for row in rows:
        try:
            price = float(row.get("lasttrade") or 0)
            preclose = float(row.get("prevclose") or 0)
            volume = float(row.get("volume") or 0)
            amount = float(row.get("amount") or 0)
        except (TypeError, ValueError):
            continue
        if price <= 0 or preclose <= 0 or volume <= 0:
            continue
        if price > preclose:
            up += 1
            up_amount += amount
        elif price < preclose:
            down += 1
            down_amount += amount
        else:
            flat += 1
    return _breadth_payload("港股", up, down, flat, _arms_value(up, down, up_amount, down_amount))


_HISTORY_DAYS = 22


def _history_codes(market: str) -> tuple[list[str], bool, float]:
    """Tencent kline codes, whether Arms uses turnover, and the share multiplier."""
    if market == "cn":
        rows: list[dict] = []
        with ThreadPoolExecutor(max_workers=8) as pool:
            for batch in pool.map(_sina_a_page, range(1, 61)):
                rows.extend(batch)
        codes = []
        seen = set()
        for row in rows:
            symbol = str(row.get("symbol") or "")
            if symbol.startswith(("sh", "sz")) and symbol not in seen:
                seen.add(symbol)
                codes.append(symbol)
        return codes, True, 1.0
    if market == "hk":
        rows = []
        with ThreadPoolExecutor(max_workers=6) as pool:
            for batch in pool.map(_hk_page, range(1, 48)):
                rows.extend(batch)
        codes = []
        seen = set()
        for row in rows:
            symbol = str(row.get("symbol") or "").zfill(5)
            code = f"hk{symbol}"
            if symbol.isdigit() and code not in seen:
                seen.add(code)
                codes.append(code)
        return codes, True, 1.0
    rows = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for batch in pool.map(_breadth_page, range(1, 26)):
            rows.extend(batch)
    codes = []
    seen = set()
    for row in rows:
        symbol = str(row.get("symbol") or "")
        code = f"us{symbol}"
        if symbol and code not in seen:
            seen.add(code)
            codes.append(code)
    return codes, False, 1.0


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


def _tencent_closes(code: str) -> list[tuple[str, float, float]]:
    try:
        text = _request_text(
            "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
            params={"param": f"{code},day,,,48,qfq"},
            encoding="utf-8",
            timeout=8,
        )
        payload = json.loads(text)
    except Exception:
        return []
    node = (payload.get("data") or {}).get(code) or {}
    raw = node.get("qfqday") or node.get("day") or []
    bars: list[tuple[str, float, float]] = []
    for row in raw:
        if not isinstance(row, list) or len(row) < 6:
            continue
        try:
            bars.append((str(row[0]), float(row[2]), float(row[5])))
        except (TypeError, ValueError):
            continue
    bars.sort()
    return bars[-48:]


def _points_from_series(series: list[list[tuple[str, float, float]]], amount: bool, share_lot: float, minimum: int) -> list[dict]:
    if len(series) < minimum:
        return []
    buckets: dict[str, list[float]] = {}
    above_ma20: dict[str, list[int]] = {}
    for bars in series:
        window: list[float] = []
        window_sum = 0.0
        for index, (day, close, volume) in enumerate(bars):
            window.append(close)
            window_sum += close
            if len(window) > 20:
                window_sum -= window.pop(0)
            if index == 0:
                continue
            prev = bars[index - 1][1]
            if volume <= 0 or close <= 0 or prev <= 0:
                continue
            size = volume * share_lot * close if amount else volume
            slot = buckets.setdefault(day, [0, 0, 0, 0.0, 0.0])
            if close > prev:
                slot[0] += 1
                slot[3] += size
            elif close < prev:
                slot[1] += 1
                slot[4] += size
            else:
                slot[2] += 1
            if len(window) < 20 or any(price <= 0 for price in window):
                continue
            counted = above_ma20.setdefault(day, [0, 0])
            counted[1] += 1
            if close > window_sum / 20:
                counted[0] += 1
    points = []
    for day in sorted(buckets)[-_HISTORY_DAYS:]:
        up, down, flat, up_size, down_size = buckets[day]
        up, down, flat = int(up), int(down), int(flat)
        count = up + down + flat
        above, valid = above_ma20.get(day, [0, 0])
        points.append(
            {
                "date": day,
                "advancers": up,
                "decliners": down,
                "unchanged": flat,
                "up_ratio": round(up / count * 100, 1) if count else None,
                "breadth": round((up - down) / count * 100, 1) if count else None,
                "arms": _arms_value(up, down, up_size, down_size),
                "above_ma20": round(above / valid * 100, 1) if valid >= 50 else None,
            }
        )
    return points


def _yahoo_series(codes: list[str]) -> list[list[tuple[str, float, float]]]:
    import yfinance as yf

    tickers = [code[2:] for code in codes if code.startswith("us") and len(code) > 2]
    if not tickers:
        return []
    frame = yf.download(
        tickers,
        period="3mo",
        auto_adjust=False,
        progress=False,
        threads=True,
        group_by="ticker",
    )
    series: list[list[tuple[str, float, float]]] = []
    single = len(tickers) == 1
    for ticker in tickers:
        try:
            block = frame if single else frame[ticker]
            closes = block["Close"].dropna()
            volumes = block["Volume"].reindex(closes.index)
        except (KeyError, TypeError, AttributeError):
            continue
        bars: list[tuple[str, float, float]] = []
        for day, price in closes.items():
            volume = volumes.get(day)
            try:
                price = float(price)
                volume = float(volume)
            except (TypeError, ValueError):
                continue
            bars.append((str(day)[:10], price, volume))
        bars.sort()
        if len(bars) >= 2:
            series.append(bars[-48:])
    return series


def _aggregate_arms_history(codes: list[str], amount: bool, share_lot: float) -> list[dict]:
    series: list[list[tuple[str, float, float]]] = []
    with ThreadPoolExecutor(max_workers=12) as pool:
        for bars in pool.map(_sina_closes if codes and codes[0].startswith(("sh", "sz")) else _tencent_closes, codes):
            if len(bars) >= 2:
                series.append(bars)
    return _points_from_series(series, amount, share_lot, max(50, int(len(codes) * 0.6)))


def fill_above_ma20(db, market: str) -> int:
    """Write the MA20 breadth onto stored sessions without rewriting Arms."""
    from decimal import Decimal

    from app.models.entities import BreadthDay

    fresh = build_arms_history(market)
    rows = {
        row.bar_date.isoformat(): row
        for row in db.query(BreadthDay).filter(BreadthDay.market == market).all()
    }
    changed = 0
    for point in fresh:
        row = rows.get(point["date"])
        if row is None or point.get("above_ma20") is None:
            continue
        row.above_ma20 = Decimal(str(point["above_ma20"]))
        changed += 1
    if changed:
        db.commit()
    return changed


def _session_closed(market: str) -> bool:
    from app.services.bars import cash_session_closed, cn_session_closed, hk_session_closed

    if market == "cn":
        return cn_session_closed()
    if market == "hk":
        return hk_session_closed()
    return cash_session_closed()


def build_arms_history(market: str) -> list[dict]:
    if market == "hk":
        return []
    codes, amount, share_lot = _history_codes(market)
    if market == "us":
        series = _yahoo_series(codes)
        points = _points_from_series(series, False, 1.0, max(50, int(len(codes) * 0.6)))
    else:
        points = _aggregate_arms_history(codes, amount, share_lot)
    if points and not _session_closed(market):
        points = points[:-1]
    return points


def breadth_history_payload(db, market: str) -> dict:
    from decimal import Decimal

    from app.models.entities import BreadthDay

    market = market if market in {"us", "cn", "hk"} else "us"
    stored = (
        db.query(BreadthDay)
        .filter(BreadthDay.market == market)
        .order_by(BreadthDay.bar_date.asc())
        .all()
    )
    if len(stored) < 15:
        fresh = build_arms_history(market)
        have = {row.bar_date.isoformat() for row in stored}
        for point in fresh:
            if point["date"] in have:
                continue
            db.add(
                BreadthDay(
                    market=market,
                    bar_date=point["date"],
                    advancers=point["advancers"],
                    decliners=point["decliners"],
                    unchanged=point["unchanged"],
                    up_ratio=None if point["up_ratio"] is None else Decimal(str(point["up_ratio"])),
                    breadth=None if point["breadth"] is None else Decimal(str(point["breadth"])),
                    arms=None if point["arms"] is None else Decimal(str(point["arms"])),
                    above_ma20=None if point.get("above_ma20") is None else Decimal(str(point["above_ma20"])),
                )
            )
        if fresh:
            db.commit()
            stored = (
                db.query(BreadthDay)
                .filter(BreadthDay.market == market)
                .order_by(BreadthDay.bar_date.asc())
                .all()
            )
    basis = "成交量" if market == "us" else "成交额"
    universe = {"us": "市值前 500 只美股", "cn": "沪深A股", "hk": "港股"}[market]
    notes = {
        "us": "阿姆士按成交股数计算。高于 MA20 用未复权收盘价，上市不满 20 天和当日没有成交的不计入。",
        "cn": "历史成交额按收盘价乘成交股数，和卡片上的交易所成交额会有一点差别。高于 MA20 用未复权收盘价，上市不满 20 天和当日没有成交的不计入。",
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

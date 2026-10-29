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
_INDEXES = (("标普 500", ".inx"), ("纳斯达克", ".ixic"))


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
    if market != "us":
        return payload
    return _overlay_open_session(payload)


def _build(db=None, market: str = "us") -> dict:
    if market == "cn":
        return _build_listed(db, market)
    if market == "hk":
        return _build_listed(db, market)
    from app.core.database import SessionLocal
    from app.services.bars import remember_breadth

    own_db = db is None
    if own_db:
        db = SessionLocal()
    try:
        breadth = _breadth()
        remember_breadth(db, breadth)
        vix = _vix(db)
        indexes = [_index(db, name, symbol) for name, symbol in _INDEXES]
    finally:
        if own_db:
            db.close()
    as_of = None
    for item in indexes:
        if item["history"]:
            as_of = item["history"][-1]["date"]
    if as_of is None and vix["history"]:
        as_of = vix["history"][-1]["date"]
    label, tone, note = _read(breadth, vix)
    return {
        "as_of": as_of,
        "label": label,
        "tone": tone,
        "note": note,
        "breadth": breadth,
        "vix": vix,
        "indexes": indexes,
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


def _breadth_page(page: int) -> list[tuple[str, float]]:
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
            rows.append((symbol, float(row.get("chg"))))
        except (TypeError, ValueError):
            continue
    return rows


def _breadth() -> dict | None:
    changes: dict[str, float] = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        for rows in pool.map(_breadth_page, range(1, 26)):
            for symbol, change in rows:
                changes.setdefault(symbol, change)
    values = list(changes.values())
    if len(values) < 50:
        return None
    up = sum(1 for value in values if value > 0)
    down = sum(1 for value in values if value < 0)
    flat = len(values) - up - down
    return {
        "universe": "市值前 500 只美股",
        "count": len(values),
        "advancers": up,
        "decliners": down,
        "unchanged": flat,
        "up_ratio": round(up / len(values) * 100, 1),
        "median_change": round(median(values), 2),
        "strong_up": sum(1 for value in values if value >= 2),
        "strong_down": sum(1 for value in values if value <= -2),
    }


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


def _index(db, name: str, symbol: str) -> dict:
    from app.services.bars import load_sina_bars

    empty = {"name": name, "last": None, "day_change": None, "ret_5d": None, "history": []}
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
    if "ma20" in item and item.get("ma20"):
        item["vs_ma20"] = round((last / item["ma20"] - 1) * 100, 1)


def _overlay_open_session(payload: dict | None) -> dict | None:
    """Attach today's live print for the charts. The cached body stays on completed sessions."""
    if not payload:
        return payload
    from app.services.bars import bar_key, cash_session_closed, open_session_bars

    if cash_session_closed():
        return payload
    wanted = {name: symbol for name, symbol in _INDEXES}
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
    as_of = payload.get("as_of")
    for item in indexes:
        if item.get("history"):
            as_of = item["history"][-1]["date"]
    return {**payload, "indexes": indexes, "vix": vix, "as_of": as_of}


_CN_INDEXES = (
    ("上证指数", "SSE:000001"),
    ("深证成指", "SZSE:399001"),
    ("创业板指", "SZSE:399006"),
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
            breadth = _breadth_exchanges()
            indexes = [_index_tencent(db, name, symbol) for name, symbol in _CN_INDEXES]
            label, tone, note = _read_ratio(breadth, "沪深")
        else:
            breadth = None
            indexes = [_index_tencent(db, name, symbol) for name, symbol in _HK_INDEXES]
            label, tone, note = "中性", "info", "恒生指数和恒生科技的日线。涨跌家数没有公开汇总。"
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
        "breadth": breadth,
        "vix": None,
        "indexes": indexes,
    }


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


def _breadth_exchanges() -> dict | None:
    """Shanghai plus Shenzhen advance/decline printed on the two index quotes."""
    try:
        text = _request_text(
            "https://push2.eastmoney.com/api/qt/ulist.np/get",
            params={
                "fltt": "2",
                "secids": "1.000001,0.399001",
                "fields": "f12,f104,f105,f106",
            },
            encoding="utf-8",
            timeout=8,
        )
        payload = json.loads(text)
    except Exception:
        return None
    rows = ((payload.get("data") or {}).get("diff")) or []
    up = down = flat = 0
    for row in rows:
        try:
            up += int(row.get("f104") or 0)
            down += int(row.get("f105") or 0)
            flat += int(row.get("f106") or 0)
        except (TypeError, ValueError):
            continue
    count = up + down + flat
    if count < 100:
        return None
    return {
        "universe": "沪深",
        "count": count,
        "advancers": up,
        "decliners": down,
        "unchanged": flat,
        "up_ratio": round(up / count * 100, 1),
        "median_change": None,
        "strong_up": None,
        "strong_down": None,
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
    empty = {"name": name, "last": None, "day_change": None, "ret_5d": None, "history": []}
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
        "history": [{"date": day, "close": round(price, 2)} for day, price in tail],
    }

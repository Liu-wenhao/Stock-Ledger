"""Backfill daily breadth and Arms from 2026-10-01.

Each symbol's daily bars stay in memory only long enough to add that name into
the day's counts. Nothing per symbol is written. Stored rows are one per
trading day and book.
"""

from __future__ import annotations

import json
import time
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from app.services.market import _request_text
from app.services.sentiment import _arms_value

PAGE_GAP = 0.5
START = date(2026, 10, 1)
FETCH_BEG = "20260820"
FETCH_END = "20261108"
_NY = ZoneInfo("America/New_York")
_STATE = Path(__file__).resolve().parents[2] / "data" / "breadth_backfill.json"
_CLIST = "https://push2.eastmoney.com/api/qt/clist/get"
_KLINE = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
_YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
_LISTS = {
    "cn": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048",
    "us": "m:105,m:106,m:107",
}


def _log(message: str) -> None:
    print(f"{datetime.now():%H:%M:%S} {message}", flush=True)


def _load() -> dict:
    if not _STATE.exists():
        return {"market": "cn", "phase": "list", "cursor": 0, "symbols": [], "agg": {}}
    return json.loads(_STATE.read_text(encoding="utf-8"))


def _save(state: dict) -> None:
    _STATE.parent.mkdir(parents=True, exist_ok=True)
    temporary = _STATE.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    temporary.replace(_STATE)


def _pause() -> None:
    time.sleep(PAGE_GAP)


def _clist_symbols(market: str) -> list[dict]:
    rows: list[dict] = []
    total = None
    seen: set[str] = set()
    for page in range(1, 160):
        payload = None
        for attempt in range(2):
            try:
                text = _request_text(
                    _CLIST,
                    params={
                        "pn": str(page),
                        "pz": "100",
                        "po": "1",
                        "np": "1",
                        "fltt": "2",
                        "invt": "2",
                        "fid": "f12",
                        "fs": _LISTS[market],
                        "fields": "f12,f13",
                    },
                    referer="https://quote.eastmoney.com/",
                    encoding="utf-8",
                    timeout=15,
                )
                payload = json.loads(text).get("data") or {}
                break
            except Exception:
                if attempt == 0:
                    _pause()
        if not payload:
            break
        if total is None:
            try:
                total = int(payload.get("total") or 0)
            except (TypeError, ValueError):
                total = 0
        batch = payload.get("diff") or []
        if isinstance(batch, dict):
            batch = list(batch.values())
        if not batch:
            break
        for item in batch:
            code = str(item.get("f12") or "").strip().upper()
            try:
                venue = int(float(item.get("f13") or 0))
            except (TypeError, ValueError):
                venue = 0
            if not code or code in seen:
                continue
            if market == "cn" and code.startswith(("92", "8", "4")):
                venue = 2
            seen.add(code)
            rows.append({"code": code, "venue": venue})
        _log(f"{market} 列表第 {page} 页，累计 {len(rows)} 只")
        if total and len(rows) >= total:
            break
        if len(batch) < 100:
            break
        _pause()
    return rows


def _books(market: str, venue: int) -> list[str]:
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


def _cn_bars(symbol: dict) -> list[tuple[date, float, float]]:
    text = _request_text(
        _KLINE,
        params={
            "secid": f"{symbol['venue']}.{symbol['code']}",
            "klt": "101",
            "fqt": "0",
            "beg": FETCH_BEG,
            "end": FETCH_END,
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f53,f56,f57",
        },
        referer="https://quote.eastmoney.com/",
        encoding="utf-8",
        timeout=15,
    )
    klines = ((json.loads(text).get("data") or {}).get("klines")) or []
    bars: list[tuple[date, float, float]] = []
    for row in klines:
        parts = str(row).split(",")
        if len(parts) < 4:
            continue
        try:
            bars.append((date.fromisoformat(parts[0]), float(parts[1]), float(parts[3])))
        except ValueError:
            continue
    bars.sort()
    return bars


def _us_bars(symbol: dict) -> list[tuple[date, float, float]]:
    ticker = symbol["code"].replace(".", "-")
    start = int(datetime(2026, 8, 20, tzinfo=timezone.utc).timestamp())
    end = int(datetime(2026, 11, 9, tzinfo=timezone.utc).timestamp())
    text = _request_text(
        _YAHOO.format(symbol=ticker),
        params={"period1": str(start), "period2": str(end), "interval": "1d", "includeAdjustedClose": "false"},
        referer="https://finance.yahoo.com/",
        encoding="utf-8",
        timeout=15,
    )
    result = ((json.loads(text).get("chart") or {}).get("result") or [None])[0] or {}
    stamps = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    closes = quote.get("close") or []
    volumes = quote.get("volume") or []
    bars: list[tuple[date, float, float]] = []
    for stamp, close, volume in zip(stamps, closes, volumes):
        if close is None or volume is None:
            continue
        try:
            day = datetime.fromtimestamp(int(stamp), _NY).date()
            bars.append((day, float(close), float(volume)))
        except (TypeError, ValueError, OSError):
            continue
    bars.sort()
    return bars


def _fetch_bars(market: str, symbol: dict) -> list[tuple[date, float, float]]:
    last_error = None
    for attempt in range(2):
        try:
            return _cn_bars(symbol) if market == "cn" else _us_bars(symbol)
        except Exception as exc:
            last_error = exc
            if attempt == 0:
                _pause()
    _log(f"{market} {symbol['code']} 跳过：{last_error}")
    return []


def _add(agg: dict, market: str, symbol: dict, bars: list[tuple[date, float, float]]) -> None:
    books = _books(market, int(symbol["venue"]))
    closes: list[float] = []
    previous: float | None = None
    for day, close, size in bars:
        closes.append(close)
        if day >= START and previous is not None and size > 0:
            direction = 0 if close == previous else (1 if close > previous else -1)
            ma_ready = len(closes) >= 20
            above = close > (sum(closes[-20:]) / 20) if ma_ready else False
            for book in books:
                bucket = agg.setdefault(day.isoformat(), {}).setdefault(
                    book,
                    {"up": 0, "down": 0, "flat": 0, "up_size": 0.0, "down_size": 0.0, "ma_above": 0, "ma_count": 0},
                )
                if direction > 0:
                    bucket["up"] += 1
                    bucket["up_size"] += size
                elif direction < 0:
                    bucket["down"] += 1
                    bucket["down_size"] += size
                else:
                    bucket["flat"] += 1
                if ma_ready:
                    bucket["ma_count"] += 1
                    if above:
                        bucket["ma_above"] += 1
        previous = close


def _flush(market: str, agg: dict) -> int:
    from app.core.database import SessionLocal
    from app.models.entities import BreadthDay

    db = SessionLocal()
    written = 0
    try:
        for day_text in sorted(agg):
            day = date.fromisoformat(day_text)
            if day < START:
                continue
            for book, bucket in agg[day_text].items():
                exists = (
                    db.query(BreadthDay)
                    .filter(BreadthDay.market == market, BreadthDay.bar_date == day, BreadthDay.book == book)
                    .one_or_none()
                )
                if exists is not None:
                    continue
                up, down, flat = int(bucket["up"]), int(bucket["down"]), int(bucket["flat"])
                count = up + down + flat
                if count < 80:
                    continue
                ma_count = int(bucket["ma_count"])
                above = None if ma_count < 80 else round(bucket["ma_above"] / ma_count * 100, 1)
                arms = _arms_value(up, down, float(bucket["up_size"]), float(bucket["down_size"]))
                breadth = round((up - down) / count * 100, 1)
                db.add(
                    BreadthDay(
                        market=market,
                        bar_date=day,
                        book=book,
                        advancers=up,
                        decliners=down,
                        unchanged=flat,
                        up_ratio=Decimal(str(round(up / count * 100, 1))),
                        breadth=Decimal(str(breadth)),
                        arms=None if arms is None else Decimal(str(arms)),
                        above_ma20=None if above is None else Decimal(str(above)),
                    )
                )
                written += 1
        db.commit()
    finally:
        db.close()
    return written


def main() -> None:
    state = _load()
    while state["market"] in {"cn", "us"}:
        market = state["market"]
        if state["phase"] == "list":
            _log(f"开始读取{market}成分，页与页之间停 {PAGE_GAP} 秒")
            state["symbols"] = _clist_symbols(market)
            state["phase"] = "kline"
            state["cursor"] = 0
            state["agg"] = {}
            _save(state)
            if not state["symbols"]:
                _log(f"{market} 没有取到成分，停止")
                return
        symbols = state["symbols"]
        while state["cursor"] < len(symbols):
            index = state["cursor"]
            symbol = symbols[index]
            bars = _fetch_bars(market, symbol)
            _add(state["agg"], market, symbol, bars)
            state["cursor"] = index + 1
            if state["cursor"] % 25 == 0 or state["cursor"] == len(symbols):
                _save(state)
                _log(f"{market} 日线 {state['cursor']}/{len(symbols)}")
            _pause()
        written = _flush(market, state["agg"])
        _log(f"{market} 写入 {written} 行日汇总")
        if market == "cn":
            state = {"market": "us", "phase": "list", "cursor": 0, "symbols": [], "agg": {}}
            _save(state)
            continue
        state["market"] = "done"
        _save(state)
        _log("两个市场的日汇总都已写入")
        return


if __name__ == "__main__":
    main()

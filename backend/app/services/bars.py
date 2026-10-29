"""Store daily bars once they can no longer change.

A bar is frozen when its date is before the current US session, or when it is
the current session and the cash market has already closed. Today's open
session is calculated from the live fetch and is not written.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models.entities import DailyBar, MarketBreadth
from app.services.ledger import previous_us_session_date, us_equity_session_date
from app.services.market import _fetch_sina_history, _provider_ticker


def bar_key(symbol: str) -> str:
    raw = (symbol or "").strip()
    if raw.startswith("."):
        return raw.lower()
    ticker = _provider_ticker(raw) or raw
    return ticker.upper()


def cash_session_closed(now: datetime | None = None) -> bool:
    """True when the current US cash session is no longer printing."""
    try:
        from zoneinfo import ZoneInfo

        et = now.astimezone(ZoneInfo("America/New_York")) if now and now.tzinfo else None
        if et is None:
            et = datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        et = now or datetime.utcnow()
    if et.weekday() >= 5:
        return True
    minutes = et.hour * 60 + et.minute
    return minutes >= 16 * 60 or minutes < 9 * 60 + 30


def chart_since(today: date | None = None) -> date:
    """Price charts start at April of the current year and use bars already stored."""
    today = today or date.today()
    return date(today.year, 4, 1)


def frozen_through(now: datetime | None = None) -> date:
    session = us_equity_session_date(now)
    if cash_session_closed(now):
        return session
    return previous_us_session_date(session)


def _local_now(tz_name: str, now: datetime | None) -> datetime:
    from zoneinfo import ZoneInfo

    zone = ZoneInfo(tz_name)
    if now and now.tzinfo:
        return now.astimezone(zone)
    return datetime.now(zone)


def _local_session(local: datetime, open_minute: int) -> date:
    day = local.date()
    minutes = local.hour * 60 + local.minute
    if minutes < open_minute:
        day -= timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def _local_closed(local: datetime, open_minute: int, close_minute: int) -> bool:
    if local.weekday() >= 5:
        return True
    minutes = local.hour * 60 + local.minute
    return minutes < open_minute or minutes >= close_minute


def _local_frozen(tz_name: str, open_minute: int, close_minute: int, now: datetime | None) -> date:
    local = _local_now(tz_name, now)
    session = _local_session(local, open_minute)
    if _local_closed(local, open_minute, close_minute):
        return session
    return previous_us_session_date(session)


def cn_frozen(now: datetime | None = None) -> date:
    """Last completed Shanghai/Shenzhen cash session. Lunch does not freeze the bar."""
    return _local_frozen("Asia/Shanghai", 9 * 60 + 30, 15 * 60, now)


def hk_frozen(now: datetime | None = None) -> date:
    """Last completed Hong Kong cash session."""
    return _local_frozen("Asia/Shanghai", 9 * 60 + 30, 16 * 60, now)


def _num(value: float | None, digits: str = "0.000001") -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value)).quantize(Decimal(digits))


def _read_stored(db: Session, symbol: str) -> list[dict]:
    rows = (
        db.query(DailyBar)
        .filter(DailyBar.symbol == symbol)
        .order_by(DailyBar.bar_date.asc())
        .all()
    )
    return [
        {
            "d": row.bar_date.isoformat(),
            "o": float(row.open) if row.open is not None else float(row.close),
            "h": float(row.high) if row.high is not None else float(row.close),
            "l": float(row.low) if row.low is not None else float(row.close),
            "c": float(row.close),
            "v": float(row.volume or 0),
            "dol": float(row.dollar or 0),
        }
        for row in rows
    ]


def _save_frozen(db: Session, symbol: str, bars: list[dict], frozen: date) -> None:
    existing = {
        row.bar_date
        for row in db.query(DailyBar.bar_date).filter(DailyBar.symbol == symbol).all()
    }
    fresh: list[DailyBar] = []
    for bar in bars:
        try:
            day = date.fromisoformat(str(bar["d"])[:10])
        except ValueError:
            continue
        if day > frozen or day in existing:
            continue
        close = bar.get("c")
        if close is None or close <= 0:
            continue
        existing.add(day)
        fresh.append(
            DailyBar(
                symbol=symbol,
                bar_date=day,
                open=_num(bar.get("o")),
                high=_num(bar.get("h")),
                low=_num(bar.get("l")),
                close=_num(close) or Decimal("0"),
                volume=_num(bar.get("v") or 0, "0.01") or Decimal("0"),
                dollar=_num(bar.get("dol") or 0, "0.01") or Decimal("0"),
            )
        )
    if not fresh:
        return
    db.add_all(fresh)
    db.commit()


def covers_frozen(bars: list[dict], frozen: date) -> bool:
    if not bars:
        return False
    try:
        latest = date.fromisoformat(bars[-1]["d"])
    except ValueError:
        return False
    return latest >= frozen


def load_bars(db: Session, symbol: str, fetcher, frozen: date | None = None) -> list[dict]:
    """Return daily bars, fetching only when a frozen session is still missing."""
    key = bar_key(symbol)
    if not key:
        return []
    frozen = frozen or frozen_through()
    stored = _read_stored(db, key)
    if covers_frozen(stored, frozen):
        return [bar for bar in stored if bar["d"] <= frozen.isoformat()]
    try:
        fresh = fetcher() or []
    except Exception:
        return stored
    _save_frozen(db, key, fresh, frozen)
    by_day = {bar["d"]: bar for bar in stored}
    for bar in fresh:
        day = str(bar.get("d") or "")[:10]
        if day:
            by_day[day] = bar
    return [by_day[day] for day in sorted(by_day)]


def parse_sina_bars(raw: list[dict]) -> list[dict]:
    bars: list[dict] = []
    for item in raw or []:
        day = str(item.get("d") or "")[:10]
        try:
            close = float(item["c"])
        except (TypeError, ValueError, KeyError):
            continue
        if not day or close <= 0:
            continue
        try:
            high = float(item.get("h") or close)
            low = float(item.get("l") or close)
            opened = float(item.get("o") or close)
            volume = float(item.get("v") or 0)
        except (TypeError, ValueError):
            high, low, opened, volume = close, close, close, 0.0
        amount = item.get("a")
        try:
            dollar = float(amount) if amount not in (None, "", "0") else close * volume
        except (TypeError, ValueError):
            dollar = close * volume
        bars.append(
            {"d": day, "o": opened, "h": high, "l": low, "c": close, "v": volume, "dol": dollar}
        )
    bars.sort(key=lambda bar: bar["d"])
    return bars


def load_sina_bars(db: Session, symbol: str) -> list[dict]:
    key = bar_key(symbol)

    def fetch():
        raw = _fetch_sina_history(key.lower(), 400)
        return parse_sina_bars(raw)

    return load_bars(db, key, fetch)


def load_symbol_bars(db: Session, symbol: str) -> list[dict]:
    """US bars stay on Sina. A-share and Hong Kong bars come from Tencent daily klines."""
    from app.services.market import fetch_tencent_bars
    from app.services.market_scope import market_of

    kind = market_of(symbol)
    if kind == "cn":
        return load_bars(db, symbol, lambda: fetch_tencent_bars(symbol, lot_size=100), frozen=cn_frozen())
    if kind == "hk":
        return load_bars(db, symbol, lambda: fetch_tencent_bars(symbol, lot_size=1), frozen=hk_frozen())
    return load_sina_bars(db, symbol)


def _quote_code(symbol: str) -> str:
    key = bar_key(symbol)
    return key[1:] if key.startswith(".") else key


def open_session_bars(symbols: list[str]) -> dict[str, dict]:
    """Today's still-open bar from the live quote. Not stored."""
    if cash_session_closed():
        return {}
    from app.services.ledger import _as_et, _us_session_date_from_et
    from app.services.market import _SINA_QUOTE_URL, _parse_as_of, _request_text

    session = us_equity_session_date()
    codes: dict[str, str] = {}
    for symbol in symbols:
        code = _quote_code(symbol)
        if code:
            codes[code.lower()] = bar_key(symbol)
    if not codes:
        return {}
    try:
        text = _request_text(
            _SINA_QUOTE_URL.format(symbols=",".join(f"gb_{code}" for code in codes)),
            referer="https://finance.sina.com.cn/",
        )
    except Exception:
        return {}
    found: dict[str, dict] = {}
    for code, key in codes.items():
        match = re.search(rf'var\s+hq_str_gb_{re.escape(code)}="([^"]*)"', text, flags=re.IGNORECASE)
        if not match:
            continue
        fields = match.group(1).split(",")
        if len(fields) < 27:
            continue
        stamp = _parse_as_of(fields[3])
        if stamp is None or _us_session_date_from_et(_as_et(stamp, "Asia/Shanghai")) != session:
            continue
        try:
            close = float(fields[1])
            opened = float(fields[5] or close)
            high = float(fields[6] or close)
            low = float(fields[7] or close)
            volume = float(fields[10] or 0)
        except ValueError:
            continue
        if close <= 0:
            continue
        found[key] = {
            "d": session.isoformat(),
            "o": opened,
            "h": high,
            "l": low,
            "c": close,
            "v": volume,
            "dol": close * volume,
        }
    return found


def append_open_bar(bars: list[dict], bar: dict | None) -> list[dict]:
    if not bar:
        return bars
    if bars and bars[-1]["d"] == bar["d"]:
        return [*bars[:-1], bar]
    if bars and bars[-1]["d"] > bar["d"]:
        return bars
    return [*bars, bar]


def remember_breadth(db: Session, snapshot: dict | None) -> None:
    """Keep one breadth row for a session after the cash close. Do not rewrite it."""
    if not snapshot or not cash_session_closed():
        return
    day = us_equity_session_date()
    exists = db.query(MarketBreadth.session_date).filter(MarketBreadth.session_date == day).first()
    if exists:
        return
    db.add(
        MarketBreadth(
            session_date=day,
            count=int(snapshot.get("count") or 0),
            advancers=int(snapshot.get("advancers") or 0),
            decliners=int(snapshot.get("decliners") or 0),
            unchanged=int(snapshot.get("unchanged") or 0),
            up_ratio=_num(snapshot.get("up_ratio"), "0.01"),
            median_change=_num(snapshot.get("median_change"), "0.01"),
            strong_up=int(snapshot.get("strong_up") or 0),
            strong_down=int(snapshot.get("strong_down") or 0),
        )
    )
    db.commit()

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
    return _completed_session("us", "America/New_York", now)


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


def cn_session_closed(now: datetime | None = None) -> bool:
    """Shanghai cash session is closed, including lunch and weekends."""
    local = _local_now("Asia/Shanghai", now)
    if local.weekday() >= 5:
        return True
    minutes = local.hour * 60 + local.minute
    return not (9 * 60 + 30 <= minutes < 11 * 60 + 30 or 13 * 60 <= minutes < 15 * 60)


def hk_session_closed(now: datetime | None = None) -> bool:
    """Hong Kong cash session is closed, including lunch and weekends."""
    local = _local_now("Asia/Hong_Kong", now)
    if local.weekday() >= 5:
        return True
    minutes = local.hour * 60 + local.minute
    return not (9 * 60 + 30 <= minutes < 12 * 60 or 13 * 60 <= minutes < 16 * 60)


def session_closed(symbol: str, now: datetime | None = None) -> bool:
    """True when this symbol's cash session is not printing."""
    from app.services.market_scope import market_of

    kind = market_of(symbol)
    if kind == "cn":
        return cn_session_closed(now)
    if kind == "hk":
        return hk_session_closed(now)
    return cash_session_closed(now)


def cn_frozen(now: datetime | None = None) -> date:
    """Last completed Shanghai/Shenzhen cash session. Lunch does not freeze the bar."""
    return _completed_session("cn", "Asia/Shanghai", now)


def hk_frozen(now: datetime | None = None) -> date:
    """Last completed Hong Kong cash session."""
    return _completed_session("hk", "Asia/Hong_Kong", now)


def _completed_session(market: str, zone: str, now: datetime | None) -> date:
    from app.services.trading_sessions import completed_session

    through = completed_session(market, _local_now(zone, now))
    if through is None:
        raise ValueError(f"无法确认 {market} 市场的已收盘交易日，暂不冻结日线")
    return through


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
    if db.get_bind().dialect.name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        stmt = sqlite_insert(DailyBar).on_conflict_do_nothing(index_elements=["symbol", "bar_date"])
    else:
        from sqlalchemy.dialects.mysql import insert as mysql_insert

        # Another request can insert the same symbol and date between the read and
        # this write. IGNORE keeps the row already stored and writes the rest.
        stmt = mysql_insert(DailyBar).prefix_with("IGNORE")
    stmt = stmt.values(
        [
            {
                "symbol": row.symbol,
                "bar_date": row.bar_date,
                "open": row.open,
                "high": row.high,
                "low": row.low,
                "close": row.close,
                "volume": row.volume,
                "dollar": row.dollar,
            }
            for row in fresh
        ]
    )
    db.execute(stmt)
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


def load_symbol_bars(db: Session, symbol: str, *, frozen: date | None = None) -> list[dict]:
    """US bars from YFinance. A-share and Hong Kong bars from AkShare, then the fallbacks."""
    from app.services.data_sources import fetch_daily_bars
    from app.services.market_scope import market_of

    kind = market_of(symbol)
    if frozen is None:
        frozen = cn_frozen() if kind == "cn" else hk_frozen() if kind == "hk" else None
    return load_bars(db, symbol, lambda: fetch_daily_bars(symbol), frozen=frozen)


def _quote_code(symbol: str) -> str:
    key = bar_key(symbol)
    return key[1:] if key.startswith(".") else key


def open_session_bars(symbols: list[str], *, when_closed: bool = False) -> dict[str, dict]:
    """Today's US bar from the live quote. Sina and Tencent first. Not stored.

    Closed sessions return nothing unless the caller still needs a bar the daily series missed.
    """
    if cash_session_closed() and not when_closed:
        return {}
    from app.services.ledger import _as_et, _us_session_date_from_et
    from app.services.market import _SINA_QUOTE_URL, _parse_as_of, _request_text
    from app.services.market_scope import market_of

    session = us_equity_session_date()
    us_symbols = [symbol for symbol in symbols if market_of(symbol) == "us"]
    found: dict[str, dict] = {}
    try:
        from app.services.data_sources import fetch_quotes

        for symbol, quote in fetch_quotes(us_symbols).items():
            price = quote.get("price")
            if price is None:
                continue
            close = float(price)
            if close <= 0:
                continue
            opened = float(quote["open"]) if quote.get("open") is not None else close
            high = float(quote["high"]) if quote.get("high") is not None else close
            low = float(quote["low"]) if quote.get("low") is not None else close
            volume = float(quote["volume"]) if quote.get("volume") else 0.0
            found[bar_key(symbol)] = {
                "d": session.isoformat(),
                "o": opened,
                "h": high,
                "l": low,
                "c": close,
                "v": volume,
                "dol": close * volume,
            }
    except Exception:
        found = {}
    missing = [symbol for symbol in us_symbols if bar_key(symbol) not in found]
    if not missing:
        return found
    codes: dict[str, str] = {}
    for symbol in missing:
        code = _quote_code(symbol)
        if code:
            codes[code.lower()] = bar_key(symbol)
    if not codes:
        return found
    try:
        text = _request_text(
            _SINA_QUOTE_URL.format(symbols=",".join(f"gb_{code}" for code in codes)),
            referer="https://finance.sina.com.cn/",
        )
    except Exception:
        return found
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


def cn_session_bars(symbols: list[str]) -> dict[str, dict]:
    """Today's A-share bars from one Tencent quote request. Not stored."""
    from app.services.market import _TENCENT_QUOTE_URL, _request_text
    from app.services.symbols import quote_code

    local = _local_now("Asia/Shanghai", None)
    session = _local_session(local, 9 * 60 + 30)
    codes = {quote_code(symbol): symbol for symbol in symbols}
    codes = {code: symbol for code, symbol in codes.items() if code}
    if not codes:
        return {}
    try:
        text = _request_text(_TENCENT_QUOTE_URL.format(symbols=",".join(codes)))
    except Exception:
        return {}
    found: dict[str, dict] = {}
    for code, symbol in codes.items():
        bar = _cn_bar_from_quote(text, code, session)
        if bar:
            found[bar_key(symbol)] = bar
    return found


def chart_session_bar(symbol: str, bars: list[dict]) -> dict | None:
    """Today's bar for the indicator chart when the stored series stops before that session. Not stored."""
    from app.services.market_scope import market_of

    kind = market_of(symbol)
    if kind == "hk":
        return None
    if kind == "cn":
        return _cn_chart_bar(symbol, bars)
    session = us_equity_session_date().isoformat()
    if bars and bars[-1]["d"] >= session:
        return None
    return open_session_bars([symbol], when_closed=True).get(bar_key(symbol))


def _cn_chart_bar(symbol: str, bars: list[dict]) -> dict | None:
    """A-share print for the current Shanghai session, including lunch. Not stored."""
    from app.services.market import _TENCENT_QUOTE_URL, _request_text
    from app.services.symbols import quote_code

    local = _local_now("Asia/Shanghai", None)
    session = _local_session(local, 9 * 60 + 30)
    if bars and bars[-1]["d"] >= session.isoformat():
        return None
    code = quote_code(symbol)
    if not code:
        return None
    try:
        text = _request_text(_TENCENT_QUOTE_URL.format(symbols=code))
    except Exception:
        return None
    return _cn_bar_from_quote(text, code, session)


def _cn_bar_from_quote(text: str, code: str, session: date) -> dict | None:
    from app.services.market import _parse_as_of

    match = re.search(rf'v_{re.escape(code)}="([^"]*)"', text, flags=re.IGNORECASE)
    if not match:
        return None
    fields = match.group(1).split("~")
    if len(fields) < 35:
        return None
    stamp = _parse_as_of(fields[30] if len(fields) > 30 else None)
    if stamp is None or stamp.date() != session:
        return None
    try:
        close = float(fields[3])
        opened = float(fields[5] or close)
        high = float(fields[33] or close)
        low = float(fields[34] or close)
        lots = float(fields[6] or 0)
        amount_wan = float(fields[37] or 0) if len(fields) > 37 else 0.0
    except (TypeError, ValueError):
        return None
    if close <= 0:
        return None
    volume = lots * 100
    dollar = amount_wan * 10000 if amount_wan > 0 else close * volume
    return {
        "d": session.isoformat(),
        "o": opened,
        "h": high,
        "l": low,
        "c": close,
        "v": volume,
        "dol": dollar,
    }


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

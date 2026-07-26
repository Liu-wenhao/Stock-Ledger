from __future__ import annotations

import logging
import threading
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.models.entities import PriceCache, SymbolMeta
from app.services.symbols import to_yahoo_symbol

logger = logging.getLogger(__name__)
settings = get_settings()

_yahoo_backoff_until: datetime | None = None
_refresh_lock = threading.Lock()
_refreshing = False

# US regular session close (America/New_York). Bars dated today before this
# hour are treated as incomplete and must not become 收盘价.
_US_REGULAR_CLOSE_HOUR = 16

KNOWN_NAMES = {
    "AAPL": "Apple Inc.",
    "MSFT": "Microsoft Corporation",
    "NVDA": "NVIDIA Corporation",
    "TSLA": "Tesla, Inc.",
    "GOOGL": "Alphabet Inc.",
    "LLY": "Eli Lilly and Company",
    "MU": "Micron Technology",
    "INTC": "Intel Corporation",
    "MRVL": "Marvell Technology",
    "HIMX": "Himax Technologies",
    "SOXX": "iShares Semiconductor ETF",
    "QQQ": "Invesco QQQ Trust",
    "PSQ": "ProShares Short QQQ",
    "RKLB": "Rocket Lab USA",
    "OKLO": "Oklo Inc.",
    "NOK": "Nokia",
}


def _now_eastern() -> datetime:
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        return datetime.utcnow()


def _max_completed_session_date(now_et: datetime | None = None) -> date:
    """
    Latest calendar date whose US regular session can be treated as completed.

    Before 16:00 ET, today's Yahoo daily bar (if present) is incomplete / intraday
    and must be ignored. After the close, today's bar is the official close.
    Weekends/holidays are handled by filtering against available daily bars.
    """
    now_et = now_et or _now_eastern()
    d = now_et.date()
    if getattr(now_et, "hour", 12) < _US_REGULAR_CLOSE_HOUR:
        d -= timedelta(days=1)
    return d


def _bar_date(idx: Any) -> date | None:
    try:
        if hasattr(idx, "date") and callable(idx.date):
            return idx.date()
        if isinstance(idx, date) and not isinstance(idx, datetime):
            return idx
        return datetime.fromisoformat(str(idx)[:10]).date()
    except Exception:
        return None


def _completed_closes(close_series: Any, max_date: date) -> Any:
    """Keep only daily closes on or before the last completed session date."""
    if close_series is None or getattr(close_series, "empty", True):
        return close_series
    keep_idx = []
    for idx in close_series.index:
        d = _bar_date(idx)
        if d is not None and d <= max_date:
            keep_idx.append(idx)
    if not keep_idx:
        return close_series.iloc[0:0]
    return close_series.loc[keep_idx]


def _d(v: Any) -> Decimal | None:
    if v is None:
        return None
    try:
        if isinstance(v, float) and (v != v):
            return None
        return Decimal(str(round(float(v), 6)))
    except Exception:
        return None


def _change_fields(price: Decimal | None, prev: Decimal | None) -> tuple[Decimal | None, Decimal | None]:
    if price is None or prev is None:
        return None, None
    change = (price - prev).quantize(Decimal("0.000001"))
    change_pct = None
    if prev != 0:
        change_pct = (change / prev * Decimal("100")).quantize(Decimal("0.000001"))
    return change, change_pct


def _parse_as_of(v: Any) -> date | None:
    """Parse Yahoo bar / API date. Returns None when unknown — never invent."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except Exception:
        return None


def ensure_symbol_meta(db: Session, symbol: str, fetch_remote: bool = False) -> SymbolMeta:
    row = db.query(SymbolMeta).filter(SymbolMeta.symbol == symbol).first()
    yahoo = to_yahoo_symbol(symbol) or symbol
    if row:
        if not row.name:
            row.name = KNOWN_NAMES.get(yahoo) or row.name
            db.commit()
        return row

    exchange = symbol.split(":")[0] if ":" in symbol else None
    row = SymbolMeta(
        symbol=symbol,
        yahoo_symbol=yahoo,
        name=KNOWN_NAMES.get(yahoo),
        exchange=exchange,
        currency="USD",
        logo_url=None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _cache_to_dict(cache: PriceCache | None, symbol: str) -> dict[str, Any]:
    if not cache:
        return {
            "symbol": symbol,
            "price": None,
            "prev_close": None,
            "open": None,
            "high": None,
            "low": None,
            "change": None,
            "change_percent": None,
            "price_as_of": None,
        }
    return {
        "symbol": symbol,
        "price": cache.price,
        "prev_close": cache.prev_close,
        "open": cache.open_price,
        "high": cache.high_price,
        "low": cache.low_price,
        "change": cache.change_amount,
        "change_percent": cache.change_percent,
        "price_as_of": cache.price_as_of,
    }


def get_quotes(
    db: Session,
    symbols: list[str],
    force: bool = False,
    background_refresh: bool = True,
) -> dict[str, dict[str, Any]]:
    """Return quotes from DB cache. Optionally kick off background Yahoo refresh."""
    unique = sorted({s for s in symbols if s and s.upper() not in {"CASH", "USD"}})
    result: dict[str, dict[str, Any]] = {}
    if not unique:
        return result

    # Kill-switch: serve cache only (no Yahoo overwrite)
    if not settings.yahoo_refresh_enabled:
        force = False
        background_refresh = False

    stale: list[str] = []
    now = datetime.utcnow()
    for sym in unique:
        ensure_symbol_meta(db, sym, fetch_remote=False)
        cache = db.query(PriceCache).filter(PriceCache.symbol == sym).first()
        result[sym] = _cache_to_dict(cache, sym)
        fresh = (
            cache
            and cache.price is not None
            and cache.fetched_at
            and cache.fetched_at >= now - timedelta(seconds=settings.quote_cache_seconds)
        )
        if force or not fresh:
            stale.append(sym)

    if stale and background_refresh:
        _start_background_refresh(stale)
    elif stale and force:
        # synchronous path only when explicitly forced (e.g. refresh button endpoint)
        fetched = _fetch_yahoo_batch(stale)
        _persist_quotes(stale, fetched)
        for sym in stale:
            cache = db.query(PriceCache).filter(PriceCache.symbol == sym).first()
            result[sym] = _cache_to_dict(cache, sym)

    return result


def _start_background_refresh(symbols: list[str]) -> None:
    global _refreshing, _yahoo_backoff_until
    if _yahoo_backoff_until and datetime.utcnow() < _yahoo_backoff_until:
        return
    with _refresh_lock:
        if _refreshing:
            return
        _refreshing = True

    def worker() -> None:
        global _refreshing, _yahoo_backoff_until
        try:
            fetched = _fetch_yahoo_batch(symbols)
            if not any(v.get("price") is not None for v in fetched.values()):
                _yahoo_backoff_until = datetime.utcnow() + timedelta(minutes=5)
                logger.warning("Yahoo returned no prices; backoff 5 minutes (cache unchanged)")
            else:
                _persist_quotes(symbols, fetched)
        except Exception as exc:
            logger.warning("background quote refresh failed: %s", exc)
            _yahoo_backoff_until = datetime.utcnow() + timedelta(minutes=5)
        finally:
            with _refresh_lock:
                _refreshing = False

    threading.Thread(target=worker, daemon=True).start()


def _persist_quotes(symbols: list[str], fetched: dict[str, dict[str, Any]]) -> None:
    """Write Yahoo results to cache. Skip symbols with no usable price (keep DB)."""
    db = SessionLocal()
    try:
        now = datetime.utcnow()
        for sym in symbols:
            data = fetched.get(sym, {})
            price = data.get("price")
            if price is None:
                # Fetch failed / empty for this symbol — do not overwrite cache
                continue
            cache = db.query(PriceCache).filter(PriceCache.symbol == sym).first()
            if not cache:
                cache = PriceCache(symbol=sym)
                db.add(cache)
                db.flush()

            prev = data.get("prev_close")
            if prev is None:
                # Keep cached 昨收 when Yahoo only returned one completed bar
                prev = cache.prev_close

            change, change_pct = _change_fields(
                price if isinstance(price, Decimal) else _d(price),
                prev if isinstance(prev, Decimal) or prev is None else _d(prev),
            )

            cache.price = price
            cache.prev_close = prev
            # As-of trading day for this close; None if Yahoo bar date unknown
            cache.price_as_of = _parse_as_of(
                data.get("price_as_of") if data.get("price_as_of") is not None else data.get("session_date")
            )
            if data.get("open") is not None:
                cache.open_price = data.get("open")
            if data.get("high") is not None:
                cache.high_price = data.get("high")
            if data.get("low") is not None:
                cache.low_price = data.get("low")
            if change is not None:
                cache.change_amount = change
            if change_pct is not None:
                cache.change_percent = change_pct
            cache.fetched_at = now
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.warning("persist quotes failed: %s", exc)
    finally:
        db.close()


def refresh_quotes_sync(db: Session, symbols: list[str]) -> dict[str, dict[str, Any]]:
    return get_quotes(db, symbols, force=True, background_refresh=False)


def _quote_from_frame(frame: Any, max_completed: date) -> dict[str, Any]:
    """
    Build a quote from daily OHLCV.

    price / 收盘价 = last completed trading day's close
    prev_close / 昨收 = the trading day before that (if available)
    price_as_of = that completed session's calendar date (from the bar)
    """
    if frame is None or getattr(frame, "empty", True):
        return {}
    if "Close" not in frame:
        return {}

    close = frame["Close"].dropna()
    completed = _completed_closes(close, max_completed)
    if completed is None or getattr(completed, "empty", True):
        return {}

    price = _d(completed.iloc[-1])
    if price is None:
        return {}

    prev = _d(completed.iloc[-2]) if len(completed) >= 2 else None
    change, change_pct = _change_fields(price, prev)

    last_idx = completed.index[-1]
    as_of = _bar_date(last_idx)
    open_v = high_v = low_v = None
    try:
        if "Open" in frame:
            open_v = _d(frame.loc[last_idx, "Open"])
        if "High" in frame:
            high_v = _d(frame.loc[last_idx, "High"])
        if "Low" in frame:
            low_v = _d(frame.loc[last_idx, "Low"])
    except Exception:
        pass

    return {
        "price": price,
        "prev_close": prev,
        "open": open_v,
        "high": high_v,
        "low": low_v,
        "change": change,
        "change_percent": change_pct,
        "price_as_of": as_of,
        "session_date": as_of.isoformat() if as_of else None,
    }


def _fetch_yahoo_batch(symbols: list[str]) -> dict[str, dict[str, Any]]:
    """
    Fetch previous completed US session close for each symbol.

    On any global/per-symbol failure, that symbol maps to {} so callers leave
    the DB cache unchanged.
    """
    out: dict[str, dict[str, Any]] = {s: {} for s in symbols}
    try:
        import yfinance as yf
    except Exception as exc:
        logger.error("yfinance import failed: %s", exc)
        return out

    mapping = {sym: (to_yahoo_symbol(sym) or sym) for sym in symbols}
    yahoo_list = sorted({v for v in mapping.values() if v})
    if not yahoo_list:
        return out

    max_completed = _max_completed_session_date()
    yahoo_to_data: dict[str, dict[str, Any]] = {}
    try:
        # Extra days so weekends/holidays still yield >= 2 completed bars for 昨收
        hist = yf.download(
            yahoo_list,
            period="10d",
            group_by="ticker",
            auto_adjust=True,
            threads=True,
            progress=False,
        )
        if hist is not None and not hist.empty:
            for ysym in yahoo_list:
                try:
                    frame = hist if len(yahoo_list) == 1 else hist[ysym]
                    quote = _quote_from_frame(frame, max_completed)
                    if quote.get("price") is not None:
                        yahoo_to_data[ysym] = quote
                except Exception as exc:
                    logger.debug("skip Yahoo symbol %s: %s", ysym, exc)
                    continue
    except Exception as exc:
        logger.warning("yf.download failed: %s", exc)
        return out

    for sym, ysym in mapping.items():
        out[sym] = yahoo_to_data.get(ysym, {})
    return out


def get_history_closes(
    symbols: list[str], start: datetime, end: datetime | None = None
) -> dict[str, dict[str, Decimal]]:
    global _yahoo_backoff_until
    if _yahoo_backoff_until and datetime.utcnow() < _yahoo_backoff_until:
        return {s: {} for s in symbols}

    try:
        import yfinance as yf
    except Exception:
        return {s: {} for s in symbols}

    end = end or datetime.utcnow()
    yahoo_map = {sym: (to_yahoo_symbol(sym) or sym) for sym in symbols}
    yahoo_list = sorted({v for v in yahoo_map.values() if v})
    result = {s: {} for s in symbols}
    if not yahoo_list:
        return result

    try:
        hist = yf.download(
            yahoo_list,
            start=start.date(),
            end=(end.date() + timedelta(days=1)),
            group_by="ticker",
            auto_adjust=True,
            threads=True,
            progress=False,
        )
    except Exception as exc:
        logger.warning("history download failed: %s", exc)
        _yahoo_backoff_until = datetime.utcnow() + timedelta(minutes=5)
        return result

    yahoo_closes: dict[str, dict[str, Decimal]] = {}
    if hist is not None and not hist.empty:
        for ysym in yahoo_list:
            closes: dict[str, Decimal] = {}
            try:
                frame = hist if len(yahoo_list) == 1 else hist[ysym]
                if frame is None or getattr(frame, "empty", True):
                    yahoo_closes[ysym] = {}
                    continue
                for idx, row in frame.iterrows():
                    val = _d(row.get("Close"))
                    if val is not None:
                        closes[idx.date().isoformat()] = val
            except Exception:
                closes = {}
            yahoo_closes[ysym] = closes

    if not any(yahoo_closes.values()):
        _yahoo_backoff_until = datetime.utcnow() + timedelta(minutes=5)

    for sym, ysym in yahoo_map.items():
        result[sym] = yahoo_closes.get(ysym, {})
    return result

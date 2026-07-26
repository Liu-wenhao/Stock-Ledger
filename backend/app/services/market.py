from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
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

# Manual last/close corrections when Yahoo cache is wrong (symbol -> last price).
PRICE_OVERRIDES: dict[str, Decimal] = {
    "NYSE:CRCD": Decimal("6.12"),
    "CRCD": Decimal("6.12"),
    "NYSE:PSQ": Decimal("26.99"),
    "PSQ": Decimal("26.99"),
    "NYSE:XE": Decimal("14.56"),
    "XE": Decimal("14.56"),
    "NYSE:NOK": Decimal("9.1"),
    "NOK": Decimal("9.1"),
    "NYSE:NASA": Decimal("22.14"),
    "NASA": Decimal("22.14"),
    "NYSE:SSG": Decimal("13.14"),
    "SSG": Decimal("13.14"),
    "NYSE:RKLZ": Decimal("5.43"),
    "RKLZ": Decimal("5.43"),
}

# Manual previous-close corrections (symbol -> prev_close).
# When Yahoo "last" was actually the prior session close, promote it here
# after correcting PRICE_OVERRIDES (e.g. PSQ 26.66, CRCD 6.16, XE 15.92).
PREV_CLOSE_OVERRIDES: dict[str, Decimal] = {
    "NYSE:PSQ": Decimal("26.66"),
    "PSQ": Decimal("26.66"),
    "NYSE:CRCD": Decimal("6.16"),
    "CRCD": Decimal("6.16"),
    "NYSE:XE": Decimal("15.92"),
    "XE": Decimal("15.92"),
    "NYSE:NOK": Decimal("9.73"),
    "NOK": Decimal("9.73"),
    "NYSE:NASA": Decimal("23.23"),
    "NASA": Decimal("23.23"),
    "NYSE:SSG": Decimal("12.41"),
    "SSG": Decimal("12.41"),
    "NYSE:RKLZ": Decimal("4.62"),
    "RKLZ": Decimal("4.62"),
}


def _override_for(table: dict[str, Decimal], symbol: str) -> Decimal | None:
    return table.get(symbol) or table.get(symbol.split(":")[-1])


def _apply_price_override(symbol: str, data: dict[str, Any]) -> dict[str, Any]:
    """Force corrected last / prev_close and recompute change."""
    if not data:
        return data
    data = dict(data)
    last = _override_for(PRICE_OVERRIDES, symbol)
    prev_override = _override_for(PREV_CLOSE_OVERRIDES, symbol)
    if last is not None:
        data["price"] = last
    if prev_override is not None:
        data["prev_close"] = prev_override
    price = data.get("price")
    prev = data.get("prev_close")
    if price is not None and prev is not None:
        price_d = price if isinstance(price, Decimal) else Decimal(str(price))
        prev_d = prev if isinstance(prev, Decimal) else Decimal(str(prev))
        data["change"] = (price_d - prev_d).quantize(Decimal("0.000001"))
        if prev_d != 0:
            data["change_percent"] = (data["change"] / prev_d * Decimal("100")).quantize(
                Decimal("0.000001")
            )
    return data


def _d(v: Any) -> Decimal | None:
    if v is None:
        return None
    try:
        if isinstance(v, float) and (v != v):
            return None
        return Decimal(str(round(float(v), 6)))
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
        }
    return _apply_price_override(
        symbol,
        {
            "symbol": symbol,
            "price": cache.price,
            "prev_close": cache.prev_close,
            "open": cache.open_price,
            "high": cache.high_price,
            "low": cache.low_price,
            "change": cache.change_amount,
            "change_percent": cache.change_percent,
        },
    )


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

    # Temporary kill-switch: serve cache + manual overrides only
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
                logger.warning("Yahoo returned no prices; backoff 5 minutes")
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
    db = SessionLocal()
    try:
        now = datetime.utcnow()
        for sym in symbols:
            data = fetched.get(sym, {})
            if data.get("price") is None:
                continue
            cache = db.query(PriceCache).filter(PriceCache.symbol == sym).first()
            if not cache:
                cache = PriceCache(symbol=sym)
                db.add(cache)
                db.flush()
            data = _apply_price_override(sym, data)
            cache.price = data.get("price")
            cache.prev_close = data.get("prev_close")
            cache.open_price = data.get("open")
            cache.high_price = data.get("high")
            cache.low_price = data.get("low")
            cache.change_amount = data.get("change")
            cache.change_percent = data.get("change_percent")
            cache.fetched_at = now
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.warning("persist quotes failed: %s", exc)
    finally:
        db.close()


def refresh_quotes_sync(db: Session, symbols: list[str]) -> dict[str, dict[str, Any]]:
    return get_quotes(db, symbols, force=True, background_refresh=False)


def _fetch_yahoo_batch(symbols: list[str]) -> dict[str, dict[str, Any]]:
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

    yahoo_to_data: dict[str, dict[str, Any]] = {}
    try:
        hist = yf.download(
            yahoo_list,
            period="5d",
            group_by="ticker",
            auto_adjust=True,
            threads=True,
            progress=False,
        )
        if hist is not None and not hist.empty:
            for ysym in yahoo_list:
                try:
                    frame = hist if len(yahoo_list) == 1 else hist[ysym]
                    if frame is None or getattr(frame, "empty", True):
                        continue
                    close = frame["Close"].dropna()
                    if close.empty:
                        continue
                    price = _d(close.iloc[-1])
                    prev = _d(close.iloc[-2]) if len(close) >= 2 else None
                    change = price - prev if price is not None and prev not in (None, 0) else None
                    change_pct = (change / prev * Decimal("100")) if change is not None and prev else None
                    yahoo_to_data[ysym] = {
                        "price": price,
                        "prev_close": prev,
                        "open": _d(frame["Open"].dropna().iloc[-1]) if "Open" in frame else None,
                        "high": _d(frame["High"].dropna().iloc[-1]) if "High" in frame else None,
                        "low": _d(frame["Low"].dropna().iloc[-1]) if "Low" in frame else None,
                        "change": change,
                        "change_percent": change_pct,
                    }
                except Exception:
                    continue
    except Exception as exc:
        logger.warning("yf.download failed: %s", exc)
        # mark rate limit style backoff by returning empty

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

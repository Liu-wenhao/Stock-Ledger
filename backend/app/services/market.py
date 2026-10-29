from __future__ import annotations

import json
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.models.entities import PriceCache, SymbolMeta
from app.services.market_scope import market_of
from app.services.symbols import quote_code, to_market_symbol

logger = logging.getLogger(__name__)
settings = get_settings()

_market_backoff_until: datetime | None = None
_refresh_lock = threading.Lock()
_refreshing = False

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0 Safari/537.36"
)
_SINA_QUOTE_URL = "https://hq.sinajs.cn/list={symbols}"
_TENCENT_QUOTE_URL = "https://qt.gtimg.cn/q={symbols}"
_TENCENT_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
_SINA_KLINE_URL = (
    "https://stock.finance.sina.com.cn/usstock/api/jsonp.php/var/"
    "US_MinKService.getDailyK"
)

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


def _bar_datetime(idx: Any) -> datetime | None:
    """Convert a provider timestamp to a naive America/New_York datetime."""
    try:
        if hasattr(idx, "to_pydatetime") and callable(idx.to_pydatetime):
            value = idx.to_pydatetime()
        elif isinstance(idx, datetime):
            value = idx
        elif isinstance(idx, date):
            value = datetime.combine(idx, datetime.min.time())
        else:
            value = datetime.fromisoformat(str(idx).replace("Z", "+00:00"))
        if value.tzinfo is not None:
            try:
                from zoneinfo import ZoneInfo

                value = value.astimezone(ZoneInfo("America/New_York"))
            except Exception:
                pass
            value = value.replace(tzinfo=None)
        return value
    except Exception:
        return None


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


def _parse_as_of(v: Any) -> datetime | None:
    """Parse a provider bar/API timestamp. Returns None when unknown."""
    if v is None:
        return None
    if isinstance(v, str):
        value = v.strip()
        for fmt in (
            "%Y-%m-%d %H:%M:%S",
            "%Y/%m/%d %H:%M:%S",
            "%Y%m%d%H%M%S",
            "%Y%m%d %H:%M:%S",
            "%Y-%m-%d",
        ):
            try:
                return datetime.strptime(value, fmt)
            except ValueError:
                continue
    return _bar_datetime(v)


def _positive_d(v: Any) -> Decimal | None:
    value = _d(v)
    return value if value is not None and value > 0 else None


def ensure_symbol_meta(db: Session, symbol: str, fetch_remote: bool = False) -> SymbolMeta:
    row = db.query(SymbolMeta).filter(SymbolMeta.symbol == symbol).first()
    provider_symbol = to_market_symbol(symbol) or symbol
    if row:
        if not row.name:
            row.name = KNOWN_NAMES.get(provider_symbol) or row.name
            db.commit()
        return row

    exchange = symbol.split(":")[0] if ":" in symbol else None
    kind = market_of(symbol)
    currency = {"cn": "CNY", "hk": "HKD"}.get(kind, "USD")
    row = SymbolMeta(
        symbol=symbol,
        # The legacy column also stores the normalized ticker used by the
        # global-stock-data quote providers.
        yahoo_symbol=provider_symbol,
        name=KNOWN_NAMES.get(provider_symbol),
        exchange=exchange,
        currency=currency,
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
    """Return cached quotes and optionally refresh global-stock-data providers."""
    unique = sorted({s for s in symbols if s and s.upper() not in {"CASH", "USD"}})
    result: dict[str, dict[str, Any]] = {}
    if not unique:
        return result

    # Kill-switch: serve cache only (no external quote overwrite).
    if not settings.quote_refresh_enabled:
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
    elif stale:
        # Explicit refresh endpoints wait for fresh data; ordinary reads keep
        # serving the cache while their background refresh runs.
        fetched = _fetch_global_stock_data_batch(stale)
        _persist_quotes(stale, fetched)
        for sym in stale:
            data = fetched.get(sym) or {}
            # A new symbol is written on another session. This request's
            # transaction cannot see that commit, so use the fetch itself.
            if data.get("price") is not None:
                result[sym] = {
                    "symbol": sym,
                    "price": data.get("price"),
                    "prev_close": data.get("prev_close"),
                    "open": data.get("open"),
                    "high": data.get("high"),
                    "low": data.get("low"),
                    "change": data.get("change"),
                    "change_percent": data.get("change_percent"),
                    "price_as_of": data.get("price_as_of"),
                    "name": data.get("name"),
                }
            else:
                cache = db.query(PriceCache).filter(PriceCache.symbol == sym).first()
                result[sym] = _cache_to_dict(cache, sym)
            name = data.get("name")
            if name:
                meta = db.query(SymbolMeta).filter(SymbolMeta.symbol == sym).first()
                if meta and not meta.name:
                    meta.name = str(name)[:255]
        if any((fetched.get(sym) or {}).get("name") for sym in stale):
            db.commit()

    return result


def _start_background_refresh(symbols: list[str]) -> None:
    global _refreshing, _market_backoff_until
    if _market_backoff_until and datetime.utcnow() < _market_backoff_until:
        return
    with _refresh_lock:
        if _refreshing:
            return
        _refreshing = True

    def worker() -> None:
        global _refreshing, _market_backoff_until
        try:
            fetched = _fetch_global_stock_data_batch(symbols)
            if not any(v.get("price") is not None for v in fetched.values()):
                _market_backoff_until = datetime.utcnow() + timedelta(minutes=2)
                logger.warning("market providers returned no prices; backoff 2 minutes")
            else:
                _persist_quotes(symbols, fetched)
        except Exception as exc:
            logger.warning("background quote refresh failed: %s", exc)
            _market_backoff_until = datetime.utcnow() + timedelta(minutes=2)
        finally:
            with _refresh_lock:
                _refreshing = False

    threading.Thread(target=worker, daemon=True).start()


def _persist_quotes(symbols: list[str], fetched: dict[str, dict[str, Any]]) -> None:
    """Write provider results to cache. Failed symbols keep their previous value."""
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
                # Keep cached previous close when the provider omits it.
                prev = cache.prev_close

            change, change_pct = _change_fields(
                price if isinstance(price, Decimal) else _d(price),
                prev if isinstance(prev, Decimal) or prev is None else _d(prev),
            )

            cache.price = price
            cache.prev_close = prev
            # Provider quote/bar timestamp; null when unknown.
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
            name = data.get("name")
            if name:
                meta = db.query(SymbolMeta).filter(SymbolMeta.symbol == sym).first()
                if meta and not meta.name:
                    meta.name = str(name)[:255]
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.warning("persist quotes failed: %s", exc)
    finally:
        db.close()


def refresh_quotes_sync(
    db: Session,
    symbols: list[str],
    force: bool = True,
) -> dict[str, dict[str, Any]]:
    return get_quotes(db, symbols, force=force, background_refresh=False)


def _request_text(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    referer: str | None = None,
    timeout: int = 5,
    encoding: str = "gbk",
) -> str:
    import requests

    headers = {"User-Agent": _USER_AGENT}
    if referer:
        headers["Referer"] = referer
    response = requests.get(url, params=params, headers=headers, timeout=timeout)
    response.raise_for_status()
    response.encoding = encoding
    return response.text


def _provider_ticker(symbol: str) -> str:
    return (to_market_symbol(symbol) or symbol).strip().upper()


def _quote_payload(
    *,
    price: Any,
    prev_close: Any,
    open_price: Any,
    high: Any,
    low: Any,
    timestamp: Any,
) -> dict[str, Any]:
    price_d = _d(price)
    if price_d is None or price_d <= 0:
        return {}
    prev_d = _positive_d(prev_close)
    change, change_pct = _change_fields(price_d, prev_d)
    as_of = _parse_as_of(timestamp)
    return {
        "price": price_d,
        "prev_close": prev_d,
        "open": _positive_d(open_price),
        "high": _positive_d(high),
        "low": _positive_d(low),
        "change": change,
        "change_percent": change_pct,
        "price_as_of": as_of,
        "session_date": as_of.date().isoformat() if as_of else None,
    }


def _parse_sina_quote(text: str, ticker: str) -> dict[str, Any]:
    pattern = rf'var\s+hq_str_gb_{re.escape(ticker.lower())}="([^"]*)"'
    match = re.search(pattern, text, flags=re.IGNORECASE)
    if not match:
        return {}
    fields = match.group(1).split(",")
    if len(fields) < 30:
        return {}
    return _quote_payload(
        price=fields[1],
        prev_close=fields[26],
        open_price=fields[5],
        high=fields[6],
        low=fields[7],
        timestamp=fields[3],
    )


def _parse_tencent_quote(text: str, code: str) -> dict[str, Any]:
    pattern = rf'v_{re.escape(code)}="([^"]*)"'
    match = re.search(pattern, text, flags=re.IGNORECASE)
    if not match:
        return {}
    fields = match.group(1).split("~")
    if len(fields) < 35:
        return {}
    payload = _quote_payload(
        price=fields[3],
        prev_close=fields[4],
        open_price=fields[5],
        high=fields[33],
        low=fields[34],
        timestamp=fields[30],
    )
    name = fields[1].strip() if len(fields) > 1 else ""
    if payload and name:
        payload["name"] = name
    return payload


def _fetch_cn_hk_quotes(symbols: list[str]) -> dict[str, dict[str, Any]]:
    """A-share and Hong Kong quotes. Prefixes are sh/sz/hk, not the US feed."""
    out: dict[str, dict[str, Any]] = {symbol: {} for symbol in symbols}
    codes = {symbol: quote_code(symbol) for symbol in symbols}
    codes = {symbol: code for symbol, code in codes.items() if code}
    if not codes:
        return out
    try:
        text = _request_text(_TENCENT_QUOTE_URL.format(symbols=",".join(codes.values())))
    except Exception as exc:
        logger.warning("Tencent CN/HK quote request failed: %s", exc)
        return out
    for symbol, code in codes.items():
        quote = _parse_tencent_quote(text, code)
        if quote.get("price") is not None:
            out[symbol] = quote
    return out


def _fetch_us_quotes(symbols: list[str]) -> dict[str, dict[str, Any]]:
    """Fetch US quotes using global-stock-data's Sina -> Tencent priority."""
    out: dict[str, dict[str, Any]] = {symbol: {} for symbol in symbols}
    mapping = {symbol: _provider_ticker(symbol) for symbol in symbols}
    tickers = sorted({ticker for ticker in mapping.values() if ticker})
    if not tickers:
        return out

    sina_text = ""
    try:
        sina_codes = ",".join(f"gb_{ticker.lower()}" for ticker in tickers)
        sina_text = _request_text(
            _SINA_QUOTE_URL.format(symbols=sina_codes),
            referer="https://finance.sina.com.cn/",
        )
    except Exception as exc:
        logger.warning("Sina quote request failed: %s", exc)

    by_ticker = {ticker: _parse_sina_quote(sina_text, ticker) for ticker in tickers}
    missing = [ticker for ticker in tickers if not by_ticker[ticker].get("price")]
    if missing:
        try:
            tencent_codes = ",".join(f"us{ticker}" for ticker in missing)
            text = _request_text(_TENCENT_QUOTE_URL.format(symbols=tencent_codes))
            for ticker in missing:
                quote = _parse_tencent_quote(text, f"us{ticker}")
                if quote.get("price") is not None:
                    by_ticker[ticker] = quote
        except Exception as exc:
            logger.warning("Tencent quote fallback failed: %s", exc)

    for symbol, ticker in mapping.items():
        out[symbol] = by_ticker.get(ticker, {})
    return out


def _fetch_global_stock_data_batch(
    symbols: list[str],
) -> dict[str, dict[str, Any]]:
    local = [symbol for symbol in symbols if market_of(symbol) in {"cn", "hk"}]
    us = [symbol for symbol in symbols if symbol not in local]
    out: dict[str, dict[str, Any]] = {}
    if us:
        out.update(_fetch_us_quotes(us))
    if local:
        out.update(_fetch_cn_hk_quotes(local))
    return out


def fetch_tencent_bars(symbol: str, lot_size: int = 1, limit: int = 320) -> list[dict[str, Any]]:
    """Daily bars from Tencent. A-share volume is in lots of 100 shares."""
    code = quote_code(symbol)
    if not code:
        return []
    text = _request_text(
        _TENCENT_KLINE_URL,
        params={"param": f"{code},day,,,{limit},qfq"},
        timeout=12,
        encoding="utf-8",
    )
    payload = json.loads(text)
    node = (payload.get("data") or {}).get(code) or {}
    rows = node.get("qfqday") or node.get("day") or []
    bars: list[dict[str, Any]] = []
    scale = lot_size if lot_size > 0 else 1
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            continue
        day = str(row[0])[:10]
        try:
            opened = float(row[1])
            close = float(row[2])
            high = float(row[3])
            low = float(row[4])
            volume = float(row[5]) * scale
        except (TypeError, ValueError):
            continue
        if not day or close <= 0:
            continue
        bars.append(
            {
                "d": day,
                "o": opened,
                "h": high,
                "l": low,
                "c": close,
                "v": volume,
                "dol": close * volume,
            }
        )
    bars.sort(key=lambda bar: bar["d"])
    return bars


def _fetch_sina_history(ticker: str, num: int) -> list[dict[str, Any]]:
    text = _request_text(
        _SINA_KLINE_URL,
        params={"symbol": ticker, "num": num},
        referer="https://finance.sina.com.cn/",
        timeout=15,
    )
    match = re.search(r"\((\[.+\])\)", text, flags=re.DOTALL)
    if not match:
        return []
    payload = json.loads(match.group(1))
    return payload if isinstance(payload, list) else []


def get_history_closes(
    symbols: list[str], start: datetime, end: datetime | None = None
) -> dict[str, dict[str, Decimal]]:
    global _market_backoff_until
    result = {symbol: {} for symbol in symbols}
    if _market_backoff_until and datetime.utcnow() < _market_backoff_until:
        return result

    end = end or datetime.utcnow()
    mapping = {symbol: _provider_ticker(symbol) for symbol in symbols}
    tickers = sorted({ticker for ticker in mapping.values() if ticker})
    if not tickers:
        return result

    # Calendar-day range plus a buffer for weekends and market holidays.
    num = max(120, min(12000, (end.date() - start.date()).days + 30))
    histories: dict[str, dict[str, Decimal]] = {ticker: {} for ticker in tickers}

    def load(ticker: str) -> tuple[str, dict[str, Decimal]]:
        closes: dict[str, Decimal] = {}
        for item in _fetch_sina_history(ticker, num):
            day = str(item.get("d") or "")[:10]
            value = _d(item.get("c"))
            if value is None or not day:
                continue
            try:
                day_date = date.fromisoformat(day)
            except ValueError:
                continue
            if start.date() <= day_date <= end.date():
                closes[day] = value
        return ticker, closes

    with ThreadPoolExecutor(max_workers=min(6, len(tickers))) as executor:
        futures = {executor.submit(load, ticker): ticker for ticker in tickers}
        for future in as_completed(futures):
            ticker = futures[future]
            try:
                _, closes = future.result()
                histories[ticker] = closes
            except Exception as exc:
                logger.warning("Sina history request failed for %s: %s", ticker, exc)

    if not any(histories.values()):
        _market_backoff_until = datetime.utcnow() + timedelta(minutes=2)
    for symbol, ticker in mapping.items():
        result[symbol] = histories.get(ticker, {})
    return result

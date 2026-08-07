"""Symbol helpers: normalize EXCHANGE:TICKER values for market providers."""

from __future__ import annotations

EXCHANGE_SUFFIX = {
    "HKEX": ".HK",
    "SSE": ".SS",
    "SZSE": ".SZ",
    "LSE": ".L",
    "TSE": ".T",
}


def split_symbol(symbol: str | None) -> tuple[str | None, str | None]:
    if not symbol:
        return None, None
    s = symbol.strip().upper()
    if s in {"CASH", "USD"}:
        return None, s
    if ":" in s:
        exchange, ticker = s.split(":", 1)
        return exchange, ticker
    return None, s


def to_market_symbol(symbol: str | None) -> str | None:
    exchange, ticker = split_symbol(symbol)
    if not ticker or ticker in {"CASH", "USD"}:
        return None
    if exchange and exchange in EXCHANGE_SUFFIX:
        # HK tickers often need numeric padding; leave as-is for now
        return f"{ticker}{EXCHANGE_SUFFIX[exchange]}"
    return ticker


def to_yahoo_symbol(symbol: str | None) -> str | None:
    """Backward-compatible alias for the legacy database field name."""
    return to_market_symbol(symbol)


def exchange_of(symbol: str | None) -> str | None:
    exchange, _ = split_symbol(symbol)
    return exchange

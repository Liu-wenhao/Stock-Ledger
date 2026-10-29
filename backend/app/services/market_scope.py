"""Request-scoped market filter: us, cn (SSE/SZSE), or hk (HKEX)."""

from __future__ import annotations

from contextvars import ContextVar

from app.services.symbols import split_symbol

_market: ContextVar[str] = ContextVar("ledger_market", default="us")

CN_EXCHANGES = {"SSE", "SZSE"}
HK_EXCHANGES = {"HKEX"}
CASH_TICKERS = {"CASH", "USD", "CNY", "HKD"}


def current_market() -> str:
    return _market.get()


def market_currency(market: str | None = None) -> str:
    chosen = market or current_market()
    return {"cn": "CNY", "hk": "HKD"}.get(chosen, "USD")


def market_of(symbol: str | None) -> str:
    """us, cn, hk, or cash. Bare tickers stay on the US book."""
    exchange, ticker = split_symbol(symbol)
    if not ticker or ticker in CASH_TICKERS:
        return "cash"
    if exchange in CN_EXCHANGES:
        return "cn"
    if exchange in HK_EXCHANGES:
        return "hk"
    return "us"


def in_market(symbol: str | None, market: str | None = None) -> bool:
    """Cash and deposits stay on the US book. Stock rows follow the exchange prefix."""
    chosen = market or current_market()
    kind = market_of(symbol)
    if kind == "cash":
        return chosen == "us"
    return kind == chosen


def apply_market(market: str | None):
    chosen = market if market in {"us", "cn", "hk"} else "us"
    token = _market.set(chosen)
    return token


def clear_market(token) -> None:
    _market.reset(token)

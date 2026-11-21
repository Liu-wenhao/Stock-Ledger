from __future__ import annotations

from datetime import date

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.entities import DailyBar
from app.services import market_bars
from app.services.market_scope import market_of
from app.services.symbols import split_symbol

_US_ETF_TICKERS = frozenset({"SOXX", "EWT", "CRCA", "CRCD", "NASA", "OKLS", "RKLZ", "SSG", "TSLQ"})


def uses_market_store(symbol: str) -> bool:
    """Ordinary CN/US shares use market_bar; other products keep daily_bar."""
    from app.services.review import _CORE_TICKERS
    from app.services.signals import LEVERAGE, is_etf

    ticker = split_symbol(symbol)[1]
    return market_of(symbol) in {"cn", "us"} and not (
        is_etf(symbol) or ticker in _CORE_TICKERS or ticker in LEVERAGE or ticker in _US_ETF_TICKERS
    )


def load_history_bars(db: Session, symbol: str, *, end: date | None = None) -> list[dict]:
    if uses_market_store(symbol):
        bars = market_bars.load_market_bars(db, symbol, end=end)
        if not bars:
            raise ValueError(f"全市场数据库中没有 {symbol} 的已收盘日线，请先回填数据")
        return bars
    from app.services.bars import load_symbol_bars

    return load_symbol_bars(db, symbol)


def unfinished_session(symbol: str, *, through: date | None = None) -> date | None:
    """Keep the current unfinished cash-session bar, including the CN lunch break."""
    from app.services.bars import _local_now, hk_frozen
    from app.services.trading_sessions import unfinished_session as current_session

    market = market_of(symbol)
    if market not in {"cn", "us", "hk"}:
        return None
    completed = through or (hk_frozen() if market == "hk" else market_bars.last_session(market))
    zone = {"cn": "Asia/Shanghai", "hk": "Asia/Hong_Kong", "us": "America/New_York"}[market]
    session = current_session(market, _local_now(zone, None))
    return session if session is not None and session > completed else None


def history_version(db: Session, symbol: str) -> str | None:
    market = market_of(symbol)
    if market not in {"cn", "us", "hk"}:
        return None
    if uses_market_store(symbol):
        source = "market_bar"
        through = market_bars.last_session(market)
        metadata = market_bars.data_version(db, market, [symbol])
    else:
        from app.services.bars import bar_key, cn_frozen, frozen_through, hk_frozen

        source = "daily_bar"
        through = cn_frozen() if market == "cn" else hk_frozen() if market == "hk" else frozen_through()
        metadata = str(
            db.query(func.count(DailyBar.id), func.max(DailyBar.id), func.max(DailyBar.bar_date))
            .filter(DailyBar.symbol == bar_key(symbol), DailyBar.bar_date <= through)
            .one()
        )
    live = unfinished_session(symbol, through=through)
    return f"{source}:v1:{market}:{through}:{live or ''}:{metadata}"

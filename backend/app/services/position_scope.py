from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.entities import Trade
from app.services.ledger import replay
from app.services.market_scope import market_of


def position_symbols(
    db: Session, portfolio_id: int, market: str | None = None, *, include_closed: bool = True
) -> list[str]:
    """Symbols with an open position or a completed liquidation cycle."""
    trades = db.query(Trade).filter(Trade.portfolio_id == portfolio_id).all()
    if market is not None:
        trades = [trade for trade in trades if market_of(trade.symbol) == market]
    state = replay(trades)
    return sorted(
        symbol
        for symbol, position in state.positions.items()
        if market_of(symbol) in {"us", "cn", "hk"}
        and (position.quantity > 0 or (include_closed and position.closed_cycles))
    )

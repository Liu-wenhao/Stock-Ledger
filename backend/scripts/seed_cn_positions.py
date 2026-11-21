"""Preview or initialize untraded A-share price-board positions in the local ledger."""

from __future__ import annotations

import argparse
import gzip
import json
from datetime import date, datetime, time
from decimal import Decimal, ROUND_FLOOR
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.models.entities import DailyBar, MarketBar, MarketSymbol, Portfolio, Trade
from app.services.bars import bar_key, cn_frozen
from app.services.market_bars import store_key
from app.services.signals import is_etf
from app.services.watchlist import watch_symbols


def lot_quantity(budget: Decimal, close: Decimal) -> Decimal:
    if not budget.is_finite() or budget <= 0:
        raise ValueError("每只股票的预算必须为正数")
    if not close.is_finite() or close <= 0:
        raise ValueError("收盘价必须为正数")
    quantity = (budget / (close * 100)).to_integral_value(rounding=ROUND_FLOOR) * 100
    if quantity <= 0:
        raise ValueError("预算不足以买入一手 100 股")
    return quantity


def _latest_close(db: Session, symbol: str, through: date) -> Decimal:
    if is_etf(symbol):
        row = (
            db.query(DailyBar)
            .filter(DailyBar.symbol == bar_key(symbol), DailyBar.bar_date <= through)
            .order_by(DailyBar.bar_date.desc())
            .first()
        )
    else:
        row = (
            db.query(MarketBar)
            .join(MarketSymbol, MarketSymbol.id == MarketBar.symbol_id)
            .filter(
                MarketSymbol.market == "cn",
                MarketSymbol.symbol == store_key("cn", symbol),
                MarketBar.bar_date <= through,
            )
            .order_by(MarketBar.bar_date.desc())
            .first()
        )
    if row is None or row.bar_date != through:
        raise ValueError(f"{symbol} 缺少最近已收盘日 {through} 的收盘价，请先更新日线")
    return Decimal(row.close)


def _json_value(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(type(value).__name__)


def seed_positions(
    db: Session,
    budget: Decimal = Decimal("25000"),
    *,
    apply: bool = False,
    portfolio_id: int | None = None,
    through: date | None = None,
    backup_dir: Path | None = None,
) -> dict:
    portfolio_id = portfolio_id or get_settings().default_portfolio_id
    through = through or cn_frozen()
    if not budget.is_finite() or budget <= 0:
        raise ValueError("每只股票的预算必须为正数")
    try:
        portfolio_query = db.query(Portfolio).filter(Portfolio.id == portfolio_id)
        if apply:
            portfolio_query = portfolio_query.with_for_update()
        if portfolio_query.one_or_none() is None:
            raise ValueError(f"组合 {portfolio_id} 不存在")
        existing = db.query(Trade).filter(Trade.portfolio_id == portfolio_id).order_by(Trade.id).all()
        traded = {
            (trade.symbol or "").strip().upper()
            for trade in existing
            if trade.side in {"Buy", "Sell"}
        }
        plan = []
        for symbol in dict.fromkeys(watch_symbols("cn")):
            if symbol in traded:
                continue
            close = _latest_close(db, symbol, through)
            quantity = lot_quantity(budget, close)
            plan.append({
                "symbol": symbol,
                "quantity": quantity,
                "close": close,
                "date": through,
                "cost": quantity * close,
            })
        backup_path = None
        if apply and plan:
            backup_dir = backup_dir or ROOT / "data"
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup_path = backup_dir / f"cn_positions_before_seed_{datetime.now():%Y%m%d_%H%M%S_%f}.json.gz"
            snapshot = {
                "portfolio_id": portfolio_id,
                "budget_per_symbol": budget,
                "plan": plan,
                "trades": [
                    {column.name: getattr(trade, column.name) for column in Trade.__table__.columns}
                    for trade in existing
                ],
            }
            with gzip.open(backup_path, "xt", encoding="utf-8") as backup:
                json.dump(snapshot, backup, ensure_ascii=False, default=_json_value)
            db.add_all([
                Trade(
                    portfolio_id=portfolio_id,
                    symbol=item["symbol"],
                    side="Buy",
                    qty=item["quantity"],
                    fill_price=item["close"],
                    commission=Decimal("0"),
                    closing_time=datetime.combine(through, time(23)),
                    notes=f"A股价格页初始化：预算{budget}元，{through}收盘价",
                )
                for item in plan
            ])
            db.commit()
        total_cost = sum((item["cost"] for item in plan), Decimal("0"))
        return {
            "applied": apply,
            "created": len(plan) if apply else 0,
            "budget_per_symbol": budget,
            "positions": plan,
            "total_cost": total_cost,
            "unused_budget": budget * len(plan) - total_cost,
            "backup": str(backup_path) if backup_path else None,
        }
    except Exception:
        db.rollback()
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="写入账本；默认只预览")
    parser.add_argument("--budget", type=Decimal, default=Decimal("25000"))
    args = parser.parse_args()
    with SessionLocal() as db:
        try:
            result = seed_positions(db, args.budget, apply=args.apply)
        except ValueError as exc:
            parser.exit(1, f"{exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2, default=_json_value))


if __name__ == "__main__":
    main()

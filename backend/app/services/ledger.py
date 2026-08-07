from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Iterable

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.entities import DailyNav, SymbolMeta, Trade
from app.services.market import get_history_closes, get_quotes
from app.services.symbols import exchange_of

ZERO = Decimal("0")
settings = get_settings()


@dataclass
class ClosedCycle:
    """One 建仓 → 清仓 period for a symbol."""

    started_at: datetime | None
    ended_at: datetime
    trade_count: int
    realized_pnl: Decimal
    realized_cost: Decimal
    buy_amount: Decimal = ZERO
    sell_amount: Decimal = ZERO
    buy_quantity: Decimal = ZERO
    close_price: Decimal | None = None


@dataclass
class PositionState:
    quantity: Decimal = ZERO
    avg_price: Decimal = ZERO
    cost_basis: Decimal = ZERO
    # Current open cycle (resets after each full 清仓)
    realized_pnl: Decimal = ZERO
    realized_cost: Decimal = ZERO
    cycle_trade_count: int = 0
    cycle_started_at: datetime | None = None
    cycle_buy_amount: Decimal = ZERO
    cycle_sell_amount: Decimal = ZERO
    cycle_buy_quantity: Decimal = ZERO
    # Completed cycles (all-time for this symbol)
    closed_cycles: list[ClosedCycle] = field(default_factory=list)
    closed_cycles_pnl: Decimal = ZERO
    closed_cycles_cost: Decimal = ZERO
    last_cycle_pnl: Decimal = ZERO
    last_cycle_trade_count: int = 0
    last_cycle_realized_cost: Decimal = ZERO
    last_cycle_started_at: datetime | None = None
    dividends: Decimal = ZERO


@dataclass
class PortfolioState:
    cash: Decimal = ZERO
    positions: dict[str, PositionState] = field(default_factory=dict)
    realized_pnl: Decimal = ZERO
    dividends: Decimal = ZERO
    deposits: Decimal = ZERO
    withdrawals: Decimal = ZERO
    first_cash_date: datetime | None = None

    def net_invested(self) -> Decimal:
        return self.deposits - self.withdrawals


def _dec(v) -> Decimal:
    if v is None:
        return ZERO
    if isinstance(v, Decimal):
        return v
    return Decimal(str(v))


def us_equity_session_date(now: datetime | None = None) -> date:
    """US equity session date (weekends roll back to Friday)."""
    try:
        from zoneinfo import ZoneInfo

        now = now or datetime.now(ZoneInfo("America/New_York"))
        if now.tzinfo is None:
            now = now.replace(tzinfo=ZoneInfo("America/New_York"))
        else:
            now = now.astimezone(ZoneInfo("America/New_York"))
    except Exception:
        now = now or datetime.utcnow()
    d = now.date()
    # Early morning ET: still attribute to previous session
    if getattr(now, "hour", 12) < 4:
        d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def trade_us_session_date(dt: datetime) -> date:
    """
    Map a trade's closing_time to the US equity session date.
    Naive timestamps are treated as China local (CSV / broker export convention).
    """
    try:
        from zoneinfo import ZoneInfo

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        et = dt.astimezone(ZoneInfo("America/New_York"))
        d = et.date()
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        return d
    except Exception:
        d = dt.date()
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        return d


def position_day_pnl(
    symbol: str,
    symbol_trades: list[Trade],
    last: Decimal | None,
    prev_close: Decimal | None,
    end_qty: Decimal,
    session_date: date | None = None,
) -> tuple[Decimal | None, Decimal | None]:
    """
    Day P/L for a symbol:
      end_qty * last - start_qty * prev_close - today_buys + today_sells
    Overnight shares use prev_close; same-session buys use fill price.
    Trade times are bucketed by US session date (CN naive → ET).
    """
    if last is None or prev_close is None or end_qty < ZERO:
        return None, None
    session_date = session_date or us_equity_session_date()
    sym = symbol.upper()

    start_state = PortfolioState()
    today_buy = ZERO
    today_sell = ZERO
    for t in sorted(symbol_trades, key=lambda x: (x.closing_time, x.id or 0)):
        tday = trade_us_session_date(t.closing_time)
        if tday < session_date:
            apply_trade(start_state, t)
        elif tday == session_date and t.side == "Buy":
            today_buy += _dec(t.qty) * _dec(t.fill_price)
        elif tday == session_date and t.side == "Sell":
            today_sell += _dec(t.qty) * _dec(t.fill_price)

    start_qty = start_state.positions.get(sym, PositionState()).quantity
    day_pnl = end_qty * last - start_qty * prev_close - today_buy + today_sell
    denom = start_qty * prev_close + today_buy
    day_pct = (day_pnl / denom * Decimal("100")) if denom > ZERO else None
    return day_pnl.quantize(Decimal("0.01")), (
        day_pct.quantize(Decimal("0.01")) if day_pct is not None else None
    )


def apply_trade(state: PortfolioState, trade: Trade) -> None:
    side = trade.side
    qty = _dec(trade.qty)
    price = _dec(trade.fill_price)
    commission = _dec(trade.commission)
    symbol = (trade.symbol or "").upper() or None

    if state.first_cash_date is None:
        state.first_cash_date = trade.closing_time

    if side == "Deposit":
        amount = price if price != ZERO else qty
        state.cash += amount
        state.deposits += amount
        return
    if side == "Withdrawal":
        amount = price if price != ZERO else qty
        state.cash -= amount
        state.withdrawals += amount
        return
    if side == "Dividend":
        amount = price if qty == ZERO else qty * price
        if amount == ZERO:
            amount = price
        state.cash += amount
        state.dividends += amount
        state.realized_pnl += amount
        if symbol:
            pos = state.positions.setdefault(symbol, PositionState())
            pos.dividends += amount
            # Only count into an open cycle
            if pos.quantity > ZERO:
                pos.realized_pnl += amount
                pos.cycle_trade_count += 1
            else:
                pos.closed_cycles_pnl += amount
        return
    if not symbol:
        return

    pos = state.positions.setdefault(symbol, PositionState())

    if side == "Buy":
        # New cycle after 清仓
        if pos.quantity <= ZERO:
            pos.realized_pnl = ZERO
            pos.realized_cost = ZERO
            pos.cycle_trade_count = 0
            pos.cycle_buy_amount = ZERO
            pos.cycle_sell_amount = ZERO
            pos.cycle_buy_quantity = ZERO
            pos.cycle_started_at = trade.closing_time
        # Position cost ignores commission; cash still pays the fee (手续费另算)
        notional = qty * price
        state.cash -= notional + commission
        new_qty = pos.quantity + qty
        if new_qty > 0:
            pos.cost_basis = pos.cost_basis + notional
            pos.avg_price = pos.cost_basis / new_qty
            pos.quantity = new_qty
        pos.cycle_buy_amount += notional
        pos.cycle_buy_quantity += qty
        pos.cycle_trade_count += 1
        return

    if side == "Sell":
        notional = qty * price
        state.cash += notional - commission
        sell_qty = min(qty, pos.quantity) if pos.quantity > 0 else qty
        cost_removed = pos.avg_price * sell_qty
        # Realized PnL ignores commission (手续费另算)
        if pos.quantity > 0:
            pnl = sell_qty * price - cost_removed
            if qty > pos.quantity:
                pnl += (qty - pos.quantity) * price
        else:
            pnl = notional
        pos.realized_pnl += pnl
        pos.realized_cost += cost_removed
        pos.cycle_sell_amount += notional
        state.realized_pnl += pnl
        pos.quantity -= sell_qty
        pos.cost_basis = pos.avg_price * pos.quantity
        pos.cycle_trade_count += 1
        if pos.quantity <= ZERO:
            # 清仓：锁定本轮盈亏，之后新开仓重新计算
            pos.quantity = ZERO
            pos.cost_basis = ZERO
            pos.avg_price = ZERO
            pos.last_cycle_pnl = pos.realized_pnl
            pos.last_cycle_trade_count = pos.cycle_trade_count
            pos.last_cycle_realized_cost = pos.realized_cost
            pos.last_cycle_started_at = pos.cycle_started_at
            pos.closed_cycles.append(
                ClosedCycle(
                    started_at=pos.cycle_started_at or trade.closing_time,
                    ended_at=trade.closing_time,
                    trade_count=pos.cycle_trade_count,
                    realized_pnl=pos.realized_pnl,
                    realized_cost=pos.realized_cost,
                    buy_amount=pos.cycle_buy_amount,
                    sell_amount=pos.cycle_sell_amount,
                    buy_quantity=pos.cycle_buy_quantity,
                    close_price=price,
                )
            )
            pos.closed_cycles_pnl += pos.realized_pnl
            pos.closed_cycles_cost += pos.realized_cost
            pos.realized_pnl = ZERO
            pos.realized_cost = ZERO
            pos.cycle_trade_count = 0
            pos.cycle_buy_amount = ZERO
            pos.cycle_sell_amount = ZERO
            pos.cycle_buy_quantity = ZERO
            pos.cycle_started_at = None
        return


def replay(trades: Iterable[Trade], until: datetime | None = None) -> PortfolioState:
    state = PortfolioState()
    for trade in sorted(trades, key=lambda t: (t.closing_time, t.id or 0)):
        if until and trade.closing_time > until:
            break
        apply_trade(state, trade)
    return state


def load_trades(db: Session, portfolio_id: int = 1) -> list[Trade]:
    return (
        db.query(Trade)
        .filter(Trade.portfolio_id == portfolio_id)
        .order_by(Trade.closing_time.asc(), Trade.id.asc())
        .all()
    )


def compute_holdings(
    db: Session,
    portfolio_id: int = 1,
    include_closed: bool = False,
) -> dict:
    trades = load_trades(db, portfolio_id)
    state = replay(trades)
    trades_by_symbol: dict[str, list[Trade]] = {}
    for t in trades:
        if t.symbol:
            trades_by_symbol.setdefault(t.symbol.upper(), []).append(t)
    symbols = list(state.positions.keys())
    quotes = get_quotes(db, symbols)
    metas = {
        m.symbol: m
        for m in db.query(SymbolMeta).filter(SymbolMeta.symbol.in_(symbols)).all()
    } if symbols else {}
    session_date = us_equity_session_date()

    open_items = []
    closed_items = []
    total_mv = ZERO

    for sym, pos in state.positions.items():
        q = quotes.get(sym, {})
        last = q.get("price")
        if last is None and pos.quantity > 0:
            last = pos.avg_price
        last = _dec(last) if last is not None else None
        closed = pos.quantity <= ZERO
        prev_close = _dec(q.get("prev_close")) if q.get("prev_close") is not None else None

        if not closed:
            # Open: current cycle only (after last 清仓); historical cycles go to 清仓盈亏
            mv = pos.quantity * (last if last is not None else pos.avg_price)
            cost = pos.cost_basis
            unrealized = mv - cost
            pnl = unrealized + pos.realized_pnl
            invested = cost + pos.realized_cost
            trade_count = pos.cycle_trade_count
            cycle_started_at = pos.cycle_started_at
            if invested > ZERO:
                pnl_pct = pnl / invested * Decimal("100")
            elif pos.avg_price > ZERO and last is not None:
                pnl_pct = (last / pos.avg_price - Decimal("1")) * Decimal("100")
            else:
                pnl_pct = ZERO
        else:
            # Closed: all completed cycles (lifetime). last_cycle_* kept for trade-history UX.
            mv = ZERO
            cost = pos.closed_cycles_cost
            pnl = pos.closed_cycles_pnl
            invested = cost
            trade_count = sum((c.trade_count for c in pos.closed_cycles), 0)
            cycle_started_at = (
                pos.closed_cycles[0].started_at if pos.closed_cycles else pos.last_cycle_started_at
            )
            pnl_pct = (pnl / invested * Decimal("100")) if invested > ZERO else ZERO

        day_chg, day_pct = (None, None)
        if not closed:
            day_chg, day_pct = position_day_pnl(
                sym,
                trades_by_symbol.get(sym, []),
                last,
                prev_close,
                pos.quantity,
                session_date=session_date,
            )
            if day_pct is None and q.get("change_percent") is not None:
                day_pct = _dec(q.get("change_percent"))

        row = {
            "symbol": sym,
            "name": metas.get(sym).name if metas.get(sym) else None,
            "logo_url": metas.get(sym).logo_url if metas.get(sym) else None,
            "exchange": exchange_of(sym),
            "quantity": pos.quantity,
            "avg_price": pos.avg_price,
            "last_price": last,
            "price_as_of": q.get("price_as_of"),
            "cost_basis": cost,
            "market_value": mv,
            "invested": invested,
            "total_pnl": pnl,
            "total_pnl_percent": pnl_pct.quantize(Decimal("0.01")),
            "day_change": day_chg,
            "day_change_percent": day_pct,
            "open_price": q.get("open"),
            "high_price": q.get("high"),
            "low_price": q.get("low"),
            "prev_close": q.get("prev_close"),
            "closed": closed,
            "realized_pnl": pos.realized_pnl if not closed else pos.closed_cycles_pnl,
            "dividends": pos.dividends,
            "trade_count": trade_count,
            "cycle_started_at": cycle_started_at,
            "closed_cycles_pnl": pos.closed_cycles_pnl,
        }
        if pos.quantity > ZERO:
            total_mv += mv
            open_items.append(row)
        else:
            closed_items.append(row)

    items = list(open_items)
    if include_closed:
        items.extend(closed_items)

    for row in items:
        if row["quantity"] > 0 and total_mv > 0:
            row["allocation"] = row["market_value"] / total_mv * Decimal("100")
        else:
            row["allocation"] = ZERO

    items.sort(key=lambda r: r["market_value"], reverse=True)
    return {
        "items": items,
        "total_market_value": total_mv,
        "cash": state.cash,
        "state": state,
        "quotes": quotes,
    }


def compute_closed_holdings(db: Session, portfolio_id: int = 1) -> dict:
    """
    Nested 清仓 view: parent = symbol, cycles = each 建仓→清仓 period.
    Includes symbols that later reopened.
    """
    trades = load_trades(db, portfolio_id)
    state = replay(trades)
    symbols = [s for s, p in state.positions.items() if p.closed_cycles]
    metas = {
        m.symbol: m
        for m in db.query(SymbolMeta).filter(SymbolMeta.symbol.in_(symbols)).all()
    } if symbols else {}

    items = []
    total_trade_count = 0
    total_realized_pnl = ZERO
    total_realized_cost = ZERO
    total_buy_amount = ZERO
    total_sell_amount = ZERO

    for sym in symbols:
        pos = state.positions[sym]
        cycles = []
        for idx, cyc in enumerate(pos.closed_cycles, start=1):
            pct = (
                (cyc.realized_pnl / cyc.realized_cost * Decimal("100"))
                if cyc.realized_cost > ZERO
                else ZERO
            )
            cycles.append(
                {
                    "cycle_index": idx,
                    "started_at": cyc.started_at,
                    "ended_at": cyc.ended_at,
                    "trade_count": cyc.trade_count,
                    "realized_cost": cyc.realized_cost,
                    "realized_pnl": cyc.realized_pnl,
                    "realized_pnl_percent": pct.quantize(Decimal("0.01")),
                    "buy_amount": cyc.buy_amount,
                    "sell_amount": cyc.sell_amount,
                }
            )
        trade_count = sum(c["trade_count"] for c in cycles)
        realized_cost = sum((c["realized_cost"] for c in cycles), ZERO)
        realized_pnl = sum((c["realized_pnl"] for c in cycles), ZERO)
        buy_amount = sum((c["buy_amount"] for c in cycles), ZERO)
        sell_amount = sum((c["sell_amount"] for c in cycles), ZERO)
        pnl_pct = (
            (realized_pnl / realized_cost * Decimal("100")) if realized_cost > ZERO else ZERO
        )
        items.append(
            {
                "symbol": sym,
                "name": metas.get(sym).name if metas.get(sym) else None,
                "logo_url": metas.get(sym).logo_url if metas.get(sym) else None,
                "exchange": exchange_of(sym),
                "cycle_count": len(cycles),
                "trade_count": trade_count,
                "realized_cost": realized_cost,
                "realized_pnl": realized_pnl,
                "realized_pnl_percent": pnl_pct.quantize(Decimal("0.01")),
                "buy_amount": buy_amount,
                "sell_amount": sell_amount,
                "currently_open": pos.quantity > ZERO,
                "cycles": cycles,
            }
        )
        total_trade_count += trade_count
        total_realized_pnl += realized_pnl
        total_realized_cost += realized_cost
        total_buy_amount += buy_amount
        total_sell_amount += sell_amount

    items.sort(key=lambda r: r["realized_pnl"])
    return {
        "items": items,
        "total_trade_count": total_trade_count,
        "total_realized_pnl": total_realized_pnl,
        "total_realized_cost": total_realized_cost,
        "total_buy_amount": total_buy_amount,
        "total_sell_amount": total_sell_amount,
        "cash": state.cash,
        "total_market_value": ZERO,
    }


def compute_cumulative_holdings(db: Session, portfolio_id: int = 1) -> dict:
    """Current positions with all completed cycles plus the open cycle."""
    holdings = compute_holdings(db, portfolio_id, include_closed=False)
    state: PortfolioState = holdings["state"]
    items = []
    total_trade_count = 0
    total_cost = ZERO
    total_pnl = ZERO

    for row in holdings["items"]:
        sym = row["symbol"]
        pos = state.positions[sym]
        if pos.quantity <= ZERO:
            continue

        cycles = []
        for idx, cyc in enumerate(pos.closed_cycles, start=1):
            avg_price = (
                cyc.buy_amount / cyc.buy_quantity if cyc.buy_quantity > ZERO else ZERO
            )
            pct = (
                cyc.realized_pnl / cyc.realized_cost * Decimal("100")
                if cyc.realized_cost > ZERO
                else ZERO
            )
            cycles.append(
                {
                    "cycle_index": idx,
                    "status": "清仓",
                    "started_at": cyc.started_at,
                    "ended_at": cyc.ended_at,
                    "trade_count": cyc.trade_count,
                    "avg_price": avg_price,
                    "price": cyc.close_price,
                    "cost": cyc.realized_cost,
                    "pnl": cyc.realized_pnl,
                    "pnl_percent": pct.quantize(Decimal("0.01")),
                }
            )

        open_cost = _dec(row["invested"])
        open_pnl = _dec(row["total_pnl"])
        open_pct = (
            open_pnl / open_cost * Decimal("100") if open_cost > ZERO else ZERO
        )
        cycles.append(
            {
                "cycle_index": len(cycles) + 1,
                "status": "持仓",
                "started_at": pos.cycle_started_at,
                "ended_at": None,
                "trade_count": pos.cycle_trade_count,
                "avg_price": pos.avg_price,
                "price": row["last_price"],
                "cost": open_cost,
                "pnl": open_pnl,
                "pnl_percent": open_pct.quantize(Decimal("0.01")),
            }
        )

        # Lifetime break-even average for the remaining position. Realized
        # losses raise the effective cost; realized profits lower it.
        # Invariant:
        #   (last_price - cumulative_avg_price) * current_quantity
        #   == lifetime cumulative P/L (when last_price is available).
        lifetime_realized_pnl = pos.closed_cycles_pnl + pos.realized_pnl
        effective_cost_basis = pos.cost_basis - lifetime_realized_pnl
        cumulative_avg_price = (
            effective_cost_basis / pos.quantity if pos.quantity > ZERO else ZERO
        )

        cumulative_cost = pos.closed_cycles_cost + open_cost
        cumulative_pnl = pos.closed_cycles_pnl + open_pnl
        cumulative_pct = (
            cumulative_pnl / cumulative_cost * Decimal("100")
            if cumulative_cost > ZERO
            else ZERO
        )
        trade_count = sum(c["trade_count"] for c in cycles)
        items.append(
            {
                "symbol": sym,
                "name": row.get("name"),
                "logo_url": row.get("logo_url"),
                "exchange": row.get("exchange"),
                "cycle_count": len(cycles),
                "trade_count": trade_count,
                "avg_price": cumulative_avg_price,
                "last_price": row["last_price"],
                "price_as_of": row.get("price_as_of"),
                "cost": cumulative_cost,
                "total_pnl": cumulative_pnl,
                "total_pnl_percent": cumulative_pct.quantize(Decimal("0.01")),
                "cycles": cycles,
            }
        )
        total_trade_count += trade_count
        total_cost += cumulative_cost
        total_pnl += cumulative_pnl

    items.sort(key=lambda r: r["total_pnl"])
    return {
        "items": items,
        "total_trade_count": total_trade_count,
        "total_cost": total_cost,
        "total_pnl": total_pnl,
        "total_pnl_percent": (
            total_pnl / total_cost * Decimal("100") if total_cost > ZERO else ZERO
        ).quantize(Decimal("0.01")),
        "total_market_value": holdings["total_market_value"],
        "cash": holdings["cash"],
    }


def compute_summary(db: Session, portfolio_id: int = 1) -> dict:
    data = compute_holdings(db, portfolio_id, include_closed=True)
    state: PortfolioState = data["state"]
    open_items = [i for i in data["items"] if not i.get("closed")]
    closed_items = [i for i in data["items"] if i.get("closed")]
    holdings_value = data["total_market_value"]
    cash = state.cash
    # Total equity (holdings + cash); KPI「投资组合金额」uses holdings_value only
    portfolio_value = holdings_value + cash

    # 持仓盈亏: 仍持仓标的的当前周期盈亏之和
    # 清仓盈亏: 所有已结束周期盈亏之和（含后来又重开的标的的历史清仓）
    open_pnl = sum((_dec(i["total_pnl"]) for i in open_items), ZERO)
    closed_pnl = sum((p.closed_cycles_pnl for p in state.positions.values()), ZERO)
    closed_cost = sum((p.closed_cycles_cost for p in state.positions.values()), ZERO)
    open_pct = (open_pnl / holdings_value * Decimal("100")) if holdings_value > ZERO else ZERO
    realized_pct = (closed_pnl / closed_cost * Decimal("100")) if closed_cost > ZERO else ZERO

    day_change = sum((_dec(i["day_change"]) for i in open_items if i["day_change"] is not None), ZERO)
    prev_mv = ZERO
    for i in open_items:
        prev = _dec(i["prev_close"])
        if prev > 0:
            prev_mv += prev * i["quantity"]
        else:
            prev_mv += i["market_value"]

    realized = closed_pnl

    trades = load_trades(db, portfolio_id)
    commissions = sum((_dec(t.commission) for t in trades), ZERO)

    net_invested = state.net_invested()
    if net_invested == ZERO:
        # No Deposit rows: use gross buy notional as invested-capital proxy
        buy_notional = ZERO
        for t in trades:
            if t.side == "Buy":
                buy_notional += _dec(t.qty) * _dec(t.fill_price)
        cost_open = sum((_dec(i["cost_basis"]) for i in open_items), ZERO)
        net_invested = buy_notional if buy_notional > ZERO else (cost_open if cost_open > ZERO else Decimal("1"))

    # 累计盈亏不含手续费 = 持仓盈亏 + 清仓盈亏
    # 等价：现金 + 持仓市值 - 净入金 + 手续费（现金里已扣的手续费加回）
    total_pnl = open_pnl + closed_pnl
    total_pct = (total_pnl / abs(net_invested) * Decimal("100")) if net_invested != ZERO else ZERO

    day_change_pct = (day_change / prev_mv * Decimal("100")) if prev_mv > ZERO else ZERO

    annualized = ZERO
    if state.first_cash_date and net_invested != ZERO:
        days = max((datetime.utcnow() - state.first_cash_date).days, 1)
        r = total_pnl / abs(net_invested)
        # Guard extreme values when r <= -1
        if r <= Decimal("-0.9999"):
            annualized = Decimal("-100")
        else:
            try:
                annualized = ((Decimal("1") + r) ** (Decimal("365") / Decimal(days)) - Decimal("1")) * Decimal(
                    "100"
                )
            except Exception:
                annualized = r * Decimal("365") / Decimal(days) * Decimal("100")

    return {
        "portfolio_value": portfolio_value,
        "holdings_value": holdings_value,
        "cash": cash,
        "unrealized_pnl": open_pnl,
        "unrealized_pnl_percent": open_pct,
        "day_change": day_change.quantize(Decimal("0.01")),
        "day_change_percent": day_change_pct.quantize(Decimal("0.01")),
        "realized_pnl": realized,
        "realized_pnl_percent": realized_pct.quantize(Decimal("0.01")),
        "dividends": state.dividends,
        "commissions": commissions,
        "total_pnl": total_pnl,
        "total_pnl_percent": total_pct,
        "annualized_return": annualized,
        "currency": settings.base_currency,
        "as_of": datetime.utcnow(),
        "net_invested": net_invested,
    }


RANGE_DEFS = [
    ("1W", "1周", 7),
    ("1M", "1个月", 30),
    ("3M", "3个月", 90),
    ("6M", "6个月", 180),
    ("YTD", "今年开始到现在", None),
    ("1Y", "1年", 365),
    ("ALL", "全部时间", None),
]


def range_start(key: str, first_date: date | None) -> date:
    today = date.today()
    if key == "YTD":
        return date(today.year, 1, 1)
    if key == "ALL":
        return first_date or today
    mapping = {k: d for k, _label, d in RANGE_DEFS}
    d = mapping.get(key)
    if d is None:
        return first_date or today
    return today - timedelta(days=d)


def build_nav_series(db: Session, portfolio_id: int = 1, rebuild: bool = False) -> list[DailyNav]:
    existing = (
        db.query(DailyNav)
        .filter(DailyNav.portfolio_id == portfolio_id)
        .order_by(DailyNav.nav_date.asc())
        .all()
    )
    if existing and not rebuild:
        return existing

    trades = load_trades(db, portfolio_id)
    if not trades:
        return []

    start = trades[0].closing_time.date()
    end = date.today()
    symbols = sorted(
        {
            t.symbol.upper()
            for t in trades
            if t.symbol and t.side in {"Buy", "Sell"}
        }
    )
    history = get_history_closes(
        symbols, datetime.combine(start, datetime.min.time()), datetime.utcnow()
    )

    # Clear old
    db.query(DailyNav).filter(DailyNav.portfolio_id == portfolio_id).delete()
    db.commit()

    rows: list[DailyNav] = []
    day = start
    # Pre-index trades by date
    while day <= end:
        until = datetime.combine(day, datetime.max.time())
        state = replay(trades, until=until)
        holdings_value = ZERO
        for sym, pos in state.positions.items():
            if pos.quantity <= 0:
                continue
            closes = history.get(sym, {})
            px = None
            # walk back for last available close
            cursor = day
            for _ in range(10):
                px = closes.get(cursor.isoformat())
                if px is not None:
                    break
                cursor -= timedelta(days=1)
            if px is None:
                px = pos.avg_price
            holdings_value += pos.quantity * _dec(px)

        total_value = holdings_value + state.cash
        cost_open = sum((p.cost_basis for p in state.positions.values() if p.quantity > 0), ZERO)
        unrealized = holdings_value - cost_open
        total_pnl = unrealized + state.realized_pnl
        net_invested = state.net_invested()
        if net_invested == ZERO:
            net_invested = total_value - total_pnl

        row = DailyNav(
            portfolio_id=portfolio_id,
            nav_date=day,
            total_value=total_value,
            cash=state.cash,
            holdings_value=holdings_value,
            realized_pnl=state.realized_pnl,
            total_pnl=total_pnl,
            net_invested=net_invested,
        )
        db.add(row)
        rows.append(row)
        day += timedelta(days=1)

    db.commit()
    return (
        db.query(DailyNav)
        .filter(DailyNav.portfolio_id == portfolio_id)
        .order_by(DailyNav.nav_date.asc())
        .all()
    )


def nav_response(db: Session, range_key: str = "3M", mode: str = "value") -> dict:
    rows = build_nav_series(db)
    # Align today's point with live summary so chart end == KPI cards
    summary = compute_summary(db)
    net_invested = abs(_dec(summary.get("net_invested"))) or Decimal("1")
    live_value = _dec(summary["portfolio_value"])
    live_cash = _dec(summary["cash"])
    live_pnl = _dec(summary["total_pnl"])
    live_realized = _dec(summary["realized_pnl"])
    live_return = _dec(summary["total_pnl_percent"])

    if rows:
        last = rows[-1]
        if last.nav_date == date.today():
            last.total_value = live_value
            last.cash = live_cash
            last.total_pnl = live_pnl
            last.realized_pnl = live_realized
            last.holdings_value = live_value - live_cash
            last.net_invested = _dec(summary.get("net_invested"))
            db.add(last)
            db.commit()
            db.refresh(last)
        else:
            # Ensure today exists as a live tip point in the response series
            rows = list(rows) + [
                DailyNav(
                    portfolio_id=last.portfolio_id,
                    nav_date=date.today(),
                    total_value=live_value,
                    cash=live_cash,
                    holdings_value=live_value - live_cash,
                    realized_pnl=live_realized,
                    total_pnl=live_pnl,
                    net_invested=_dec(summary.get("net_invested")),
                )
            ]

    first = rows[0].nav_date if rows else date.today()
    start = range_start(range_key, first)
    filtered = [r for r in rows if r.nav_date >= start]

    points = []
    for r in filtered:
        value = _dec(r.total_value)
        cash = _dec(r.cash)
        pnl = _dec(r.total_pnl)
        # Portfolio return % (same definition as summary); cash return always 0
        ret_pct = (pnl / net_invested * Decimal("100")) if net_invested != ZERO else ZERO
        points.append(
            {
                "date": r.nav_date,
                "total_value": value,
                "cash": cash,
                "realized_pnl": r.realized_pnl,
                "total_pnl": r.total_pnl,
                "return_percent": ret_pct,
                "cash_percent": ZERO,
            }
        )

    # Force last point to live summary (authoritative)
    if points:
        points[-1]["total_value"] = live_value
        points[-1]["cash"] = live_cash
        points[-1]["total_pnl"] = live_pnl
        points[-1]["realized_pnl"] = live_realized
        points[-1]["return_percent"] = live_return
        points[-1]["cash_percent"] = ZERO

    # Periods that require full window coverage (match TradingView empty dashes)
    strict_keys = {"6M", "YTD", "1Y"}
    ranges = []
    for key, label, _ in RANGE_DEFS:
        rs = range_start(key, first)
        subset = [r for r in rows if r.nav_date >= rs]
        if len(subset) < 1:
            ranges.append({"key": key, "label": label, "return_percent": None, "pnl": None})
            continue
        # History shorter than requested window → show "—"
        if key in strict_keys and first > rs:
            ranges.append({"key": key, "label": label, "return_percent": None, "pnl": None})
            continue
        first_ret = _dec(subset[0].total_pnl) / net_invested * Decimal("100")
        last_ret = live_return
        pnl = live_pnl - _dec(subset[0].total_pnl)
        ret = last_ret - first_ret if key != "ALL" else live_return
        if key == "ALL":
            pnl = live_pnl
        ranges.append({"key": key, "label": label, "return_percent": ret, "pnl": pnl})

    return {"mode": mode, "points": points, "ranges": ranges}


def monthly_returns(db: Session, range_key: str = "3M") -> dict:
    rows = build_nav_series(db)
    if not rows:
        return {"items": [], "benchmark": settings.benchmark_symbol}

    first = rows[0].nav_date
    start = range_start(range_key, first)
    filtered = [r for r in rows if r.nav_date >= start]

    # Portfolio monthly return from month-end values
    by_month: dict[str, list[DailyNav]] = {}
    for r in filtered:
        key = r.nav_date.strftime("%Y-%m")
        by_month.setdefault(key, []).append(r)

    # Benchmark history
    bench_hist = get_history_closes(
        [settings.benchmark_symbol],
        datetime.combine(start, datetime.min.time()),
        datetime.utcnow(),
    ).get(settings.benchmark_symbol, {})

    items = []
    months = sorted(by_month.keys())
    for i, month in enumerate(months):
        mrows = by_month[month]
        start_v = _dec(mrows[0].total_value)
        end_v = _dec(mrows[-1].total_value)
        # Prefer previous month end as start
        if i > 0:
            prev = by_month[months[i - 1]][-1]
            start_v = _dec(prev.total_value)
        port_pct = ((end_v - start_v) / abs(start_v) * Decimal("100")) if start_v != ZERO else ZERO

        # benchmark
        m_start = mrows[0].nav_date
        m_end = mrows[-1].nav_date
        b_start = bench_hist.get(m_start.isoformat())
        b_end = bench_hist.get(m_end.isoformat())
        # walk
        if b_start is None:
            for d in range(0, 10):
                b_start = bench_hist.get((m_start - timedelta(days=d)).isoformat())
                if b_start:
                    break
        if b_end is None:
            for d in range(0, 10):
                b_end = bench_hist.get((m_end - timedelta(days=d)).isoformat())
                if b_end:
                    break
        if b_start and b_end and b_start != 0:
            bench_pct = (_dec(b_end) - _dec(b_start)) / _dec(b_start) * Decimal("100")
        else:
            bench_pct = ZERO

        label = f"{int(month.split('-')[1])}月"
        items.append(
            {
                "month": label,
                "portfolio_percent": port_pct.quantize(Decimal("0.01")),
                "benchmark_percent": bench_pct.quantize(Decimal("0.01")),
            }
        )

    return {"items": items, "benchmark": settings.benchmark_symbol}


def daily_movers(db: Session, portfolio_id: int = 1) -> dict:
    data = compute_holdings(db, portfolio_id, include_closed=False)
    scored = []
    for item in data["items"]:
        pct = item.get("day_change_percent")
        amt = item.get("day_change")
        if pct is None and amt is None:
            continue
        pct_v = _dec(pct) if pct is not None else ZERO
        amt_v = _dec(amt) if amt is not None else ZERO
        scored.append(
            {
                "symbol": item["symbol"],
                "name": item["name"],
                "logo_url": item["logo_url"],
                "change": amt_v,
                "change_percent": pct_v,
                "closed": False,
            }
        )
    # Full open lists; frontend sorts by amount/% and slices daily top-N
    winners = [x for x in scored if x["change_percent"] > 0 or x["change"] > 0]
    losers = [x for x in scored if x["change_percent"] < 0 or x["change"] < 0]
    winners.sort(key=lambda x: x["change_percent"], reverse=True)
    losers.sort(key=lambda x: x["change_percent"])
    return {"winners": winners, "losers": losers}


def _period_section(items: list[dict]) -> dict:
    """Build one period P/L summary and its winner/loser lists."""
    scored = []
    total_pnl = ZERO
    total_cost = ZERO
    for item in items:
        amount = _dec(item.get("amount"))
        cost = _dec(item.get("cost"))
        if amount == ZERO and cost == ZERO:
            continue
        pct = amount / cost * Decimal("100") if cost > ZERO else ZERO
        scored.append(
            {
                "symbol": item["symbol"],
                "name": item.get("name"),
                "logo_url": item.get("logo_url"),
                "change": amount,
                "change_percent": pct.quantize(Decimal("0.01")),
                "closed": bool(item.get("closed")),
            }
        )
        total_pnl += amount
        total_cost += cost

    winners = [x for x in scored if x["change_percent"] > ZERO or x["change"] > ZERO]
    losers = [x for x in scored if x["change_percent"] < ZERO or x["change"] < ZERO]
    winners.sort(key=lambda x: x["change_percent"], reverse=True)
    losers.sort(key=lambda x: x["change_percent"])
    return {
        "pnl": total_pnl,
        "pnl_percent": (
            total_pnl / total_cost * Decimal("100") if total_cost > ZERO else ZERO
        ).quantize(Decimal("0.01")),
        "winners": winners,
        "losers": losers,
    }


def period_performance(db: Session, portfolio_id: int = 1, range_key: str = "3M") -> dict:
    """Calculate holding/closed/lifetime P/L contributed during a selected range."""
    trades = load_trades(db, portfolio_id)
    empty = {"pnl": ZERO, "pnl_percent": ZERO, "winners": [], "losers": []}
    if not trades:
        return {"range": range_key, "holding": empty, "closed": empty, "cumulative": empty}

    first_trade_date = min(t.closing_time.date() for t in trades)
    if range_key == "ALL":
        start_date = first_trade_date - timedelta(days=1)
    else:
        start_date = range_start(range_key, first_trade_date)
    start_until = datetime.combine(start_date, datetime.max.time())

    start_state = replay(trades, until=start_until)
    end_data = compute_holdings(db, portfolio_id, include_closed=True)
    end_state: PortfolioState = end_data["state"]
    end_rows = {row["symbol"]: row for row in end_data["items"]}

    start_symbols = [
        sym for sym, pos in start_state.positions.items() if pos.quantity > ZERO
    ]
    histories = (
        get_history_closes(
            start_symbols,
            datetime.combine(start_date - timedelta(days=14), datetime.min.time()),
            datetime.combine(start_date, datetime.max.time()),
        )
        if start_symbols
        else {}
    )

    def start_price(symbol: str, position: PositionState) -> Decimal:
        closes = histories.get(symbol, {})
        eligible = [day for day in closes if day <= start_date.isoformat()]
        if eligible:
            return _dec(closes[max(eligible)])
        return position.avg_price

    def open_pnl(position: PositionState, price: Decimal) -> Decimal:
        return position.quantity * price - position.cost_basis + position.realized_pnl

    holding_items = []
    closed_items = []
    cumulative_items = []
    symbols = set(start_state.positions) | set(end_state.positions)
    for symbol in symbols:
        start_pos = start_state.positions.get(symbol, PositionState())
        end_pos = end_state.positions.get(symbol, PositionState())
        row = end_rows.get(symbol) or {}
        start_px = start_price(symbol, start_pos) if start_pos.quantity > ZERO else ZERO
        start_value = start_pos.quantity * start_px
        start_open_pnl = (
            open_pnl(start_pos, start_px) if start_pos.quantity > ZERO else ZERO
        )

        # Closed cycles appended after the range boundary. If the first one
        # is the cycle that was already open at the boundary, subtract its
        # pre-period P/L so only this range's contribution remains.
        old_closed_count = len(start_pos.closed_cycles)
        new_cycles = end_pos.closed_cycles[old_closed_count:]
        same_start_cycle = bool(
            start_pos.quantity > ZERO
            and start_pos.cycle_started_at is not None
            and new_cycles
            and new_cycles[0].started_at == start_pos.cycle_started_at
        )
        raw_closed_pnl = sum((c.realized_pnl for c in new_cycles), ZERO)
        closed_amount = raw_closed_pnl - start_open_pnl if same_start_cycle else raw_closed_pnl
        closed_cost = ZERO
        for index, cycle in enumerate(new_cycles):
            if index == 0 and same_start_cycle:
                closed_cost += start_value + max(cycle.buy_amount - start_pos.cycle_buy_amount, ZERO)
            else:
                closed_cost += cycle.realized_cost
        # Dividends (or other realized entries) that do not create a cycle.
        closed_amount += (
            end_pos.closed_cycles_pnl
            - start_pos.closed_cycles_pnl
            - raw_closed_pnl
        )

        holding_amount = ZERO
        holding_cost = ZERO
        if end_pos.quantity > ZERO:
            end_px = _dec(row.get("last_price")) if row.get("last_price") is not None else end_pos.avg_price
            end_open_pnl = open_pnl(end_pos, end_px)
            same_open_cycle = bool(
                start_pos.quantity > ZERO
                and start_pos.cycle_started_at is not None
                and end_pos.cycle_started_at == start_pos.cycle_started_at
            )
            if same_open_cycle:
                holding_amount = end_open_pnl - start_open_pnl
                holding_cost = start_value + max(
                    end_pos.cycle_buy_amount - start_pos.cycle_buy_amount, ZERO
                )
            else:
                holding_amount = end_open_pnl
                holding_cost = end_pos.cycle_buy_amount

        name = row.get("name")
        logo_url = row.get("logo_url")
        holding_items.append(
            {
                "symbol": symbol,
                "name": name,
                "logo_url": logo_url,
                "amount": holding_amount,
                "cost": holding_cost,
                "closed": False,
            }
        )
        closed_items.append(
            {
                "symbol": symbol,
                "name": name,
                "logo_url": logo_url,
                "amount": closed_amount,
                "cost": closed_cost,
                "closed": True,
            }
        )
        cumulative_items.append(
            {
                "symbol": symbol,
                "name": name,
                "logo_url": logo_url,
                "amount": holding_amount + closed_amount,
                "cost": holding_cost + closed_cost,
                "closed": end_pos.quantity <= ZERO,
            }
        )

    return {
        "range": range_key,
        "holding": _period_section(holding_items),
        "closed": _period_section(closed_items),
        "cumulative": _period_section(cumulative_items),
    }


def total_movers(
    db: Session,
    portfolio_id: int = 1,
    include_closed: bool = False,
    closed_only: bool = False,
) -> dict:
    """Overall P/L winners & losers (not day change). No count limit."""
    if closed_only:
        data = compute_closed_holdings(db, portfolio_id)
        scored = []
        for item in data["items"]:
            pct_v = _dec(item.get("realized_pnl_percent"))
            amt_v = _dec(item.get("realized_pnl"))
            scored.append(
                {
                    "symbol": item["symbol"],
                    "name": item["name"],
                    "logo_url": item["logo_url"],
                    "change": amt_v,
                    "change_percent": pct_v,
                    "closed": True,
                }
            )
    elif include_closed:
        # 累计盈亏: lifetime per symbol = all closed cycles + current open cycle (if any)
        data = compute_holdings(db, portfolio_id, include_closed=True)
        state: PortfolioState = data["state"]
        by_symbol = {i["symbol"]: i for i in data["items"]}
        scored = []
        for sym, pos in state.positions.items():
            row = by_symbol.get(sym)
            has_open = pos.quantity > ZERO
            has_closed = bool(pos.closed_cycles) or pos.closed_cycles_pnl != ZERO
            if not has_open and not has_closed:
                continue
            if has_open:
                if row is None:
                    continue
                cycle_pnl = _dec(row["total_pnl"])
                cycle_cost = _dec(row["invested"])
                amt_v = cycle_pnl + pos.closed_cycles_pnl
                cost_v = cycle_cost + pos.closed_cycles_cost
            else:
                amt_v = pos.closed_cycles_pnl
                cost_v = pos.closed_cycles_cost
            pct_v = (
                (amt_v / cost_v * Decimal("100")).quantize(Decimal("0.01"))
                if cost_v > ZERO
                else ZERO
            )
            scored.append(
                {
                    "symbol": sym,
                    "name": row["name"] if row else None,
                    "logo_url": row["logo_url"] if row else None,
                    "change": amt_v,
                    "change_percent": pct_v,
                    "closed": not has_open,
                }
            )
    else:
        # 持仓盈亏: current open cycle only
        data = compute_holdings(db, portfolio_id, include_closed=False)
        scored = []
        for item in data["items"]:
            pct = item.get("total_pnl_percent")
            amt = item.get("total_pnl")
            if pct is None and amt is None:
                continue
            pct_v = _dec(pct) if pct is not None else ZERO
            amt_v = _dec(amt) if amt is not None else ZERO
            scored.append(
                {
                    "symbol": item["symbol"],
                    "name": item["name"],
                    "logo_url": item["logo_url"],
                    "change": amt_v,
                    "change_percent": pct_v,
                    "closed": bool(item.get("closed")),
                }
            )
    winners = [x for x in scored if x["change_percent"] > 0 or x["change"] > 0]
    winners.sort(key=lambda x: x["change_percent"], reverse=True)
    losers = [x for x in scored if x["change_percent"] < 0 or x["change"] < 0]
    losers.sort(key=lambda x: x["change_percent"])
    return {"winners": winners, "losers": losers}

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Iterable

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.entities import DailyNav, SymbolMeta, Trade
from app.services.market import get_history_closes, get_quotes
from app.services.market_scope import market_currency
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
    commissions: Decimal = ZERO
    first_cash_date: datetime | None = None

    def net_invested(self) -> Decimal:
        return self.deposits - self.withdrawals


def _dec(v) -> Decimal:
    if v is None:
        return ZERO
    if isinstance(v, Decimal):
        return v
    return Decimal(str(v))


def _as_et(dt: datetime, naive_tz: str) -> datetime:
    try:
        from zoneinfo import ZoneInfo

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo(naive_tz))
        return dt.astimezone(ZoneInfo("America/New_York"))
    except Exception:
        return dt


def _us_session_date_from_et(et: datetime) -> date:
    """US equity session date: pre-4am ET stays on the previous session; weekends roll to Friday."""
    d = et.date()
    if getattr(et, "hour", 12) < 4:
        d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def previous_us_session_date(day: date) -> date:
    d = day - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def us_equity_session_date(now: datetime | None = None) -> date:
    """US equity session date (weekends roll back to Friday)."""
    try:
        from zoneinfo import ZoneInfo

        now = now or datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        now = now or datetime.utcnow()
    return _us_session_date_from_et(_as_et(now, "America/New_York"))


def trade_us_session_date(dt: datetime) -> date:
    """
    Map a trade's closing_time to the US equity session date.
    Naive timestamps are treated as China local (CSV / broker export convention).
    Uses the same pre-4am ET / weekend rules as us_equity_session_date.
    """
    return _us_session_date_from_et(_as_et(dt, "Asia/Shanghai"))


def position_day_pnl(
    symbol: str,
    symbol_trades: list[Trade],
    last: Decimal | None,
    prev_close: Decimal | None,
    end_qty: Decimal,
    session_date: date | None = None,
) -> tuple[Decimal | None, Decimal | None, Decimal]:
    """
    较前一天 / 日涨跌: shares held yesterday that are still held, marked
    last vs prev_close. Same-session buys and sells do not enter this figure
    — a new lot has no prior day, and a sale's fill vs yesterday belongs to
    realized trading P/L, not remaining 持仓.
    Trade times are bucketed by US session date (CN naive → ET).
    Returns (amount, percent, overnight_qty).
    """
    session_date = session_date or us_equity_session_date()
    prev_session = previous_us_session_date(session_date)
    sym = symbol.upper()
    held_qty = end_qty if end_qty > ZERO else ZERO

    start_state = PortfolioState()
    for t in sorted(symbol_trades, key=lambda x: (x.closing_time, x.id or 0)):
        tday = trade_us_session_date(t.closing_time)
        if tday <= prev_session:
            apply_trade(start_state, t)

    start_qty = start_state.positions.get(sym, PositionState()).quantity
    overnight_qty = min(start_qty, held_qty)

    if overnight_qty <= ZERO:
        if last is None:
            return None, None, ZERO
        return Decimal("0.00"), None, ZERO
    if last is None or prev_close is None:
        return None, None, overnight_qty

    day_pnl = overnight_qty * (last - _dec(prev_close))
    denom = overnight_qty * _dec(prev_close)
    day_pct = (day_pnl / denom * Decimal("100")) if denom > ZERO else None
    return day_pnl.quantize(Decimal("0.01")), (
        day_pct.quantize(Decimal("0.01")) if day_pct is not None else None
    ), overnight_qty


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
        state.commissions += commission
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
        state.commissions += commission
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


def replay_until_session(trades: Iterable[Trade], session_day: date) -> PortfolioState:
    """Replay trades through the end of a US equity session date."""
    state = PortfolioState()
    for trade in sorted(trades, key=lambda t: (t.closing_time, t.id or 0)):
        if trade_us_session_date(trade.closing_time) <= session_day:
            apply_trade(state, trade)
    return state


def _nav_series_needs_rebuild(existing: list[DailyNav]) -> bool:
    if not existing:
        return False
    latest = existing[-1]
    session_today = us_equity_session_date()
    if any(r.nav_date.weekday() >= 5 for r in existing):
        return True
    if latest.nav_date > session_today:
        return True
    if _dec(latest.investment_total) == ZERO and _dec(latest.total_pnl) != ZERO:
        return True

    # Older NAV rows omitted commissions from cumulative P/L. Detect that cache
    # shape so the next request rebuilds history once using the account口径.
    tolerance = Decimal("0.01")
    for row in existing:
        net_invested = _dec(row.net_invested)
        if net_invested <= ZERO:
            continue
        expected_pnl = _dec(row.total_value) - net_invested
        if abs(expected_pnl - _dec(row.total_pnl)) > tolerance:
            return True
    return False


def load_trades(db: Session, portfolio_id: int = 1) -> list[Trade]:
    from app.services.market_scope import in_market

    rows = (
        db.query(Trade)
        .filter(Trade.portfolio_id == portfolio_id)
        .order_by(Trade.closing_time.asc(), Trade.id.asc())
        .all()
    )
    return [row for row in rows if in_market(row.symbol)]


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
            # Displayed 平均价格/成本值 use the break-even basis, so the row agrees
            # with 总盈亏 (which already includes this cycle's realized P/L):
            #   (last_price - display_avg) * quantity == total_pnl
            # Realized losses raise the effective cost, realized gains lower it.
            # The raw moving-average lot values stay on lot_avg_price/lot_cost_basis
            # because 投资总额 and the NAV series must keep counting money actually in.
            display_cost = cost - pos.realized_pnl
            display_avg = display_cost / pos.quantity
            # Keep the displayed rate on the same denominator as the displayed
            # 成本值. Realized P/L from this cycle is already included in total
            # P/L, so adding the sold lot cost again understates the percentage.
            pnl_pct = (pnl / display_cost * Decimal("100")) if display_cost > ZERO else ZERO
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
            # Fully closed: no open lot, so break-even collapses to the raw values.
            display_cost = cost
            display_avg = pos.avg_price

        day_chg, day_pct = (None, None)
        overnight_qty = ZERO
        if not closed:
            day_chg, day_pct, overnight_qty = position_day_pnl(
                sym,
                trades_by_symbol.get(sym, []),
                last,
                prev_close,
                pos.quantity,
                session_date=session_date,
            )
            if day_pct is None and overnight_qty > ZERO and q.get("change_percent") is not None:
                day_pct = _dec(q.get("change_percent"))
        day_change_base = (
            overnight_qty * prev_close
            if overnight_qty > ZERO and prev_close is not None
            else None
        )

        row = {
            "symbol": sym,
            "name": metas.get(sym).name if metas.get(sym) else None,
            "logo_url": metas.get(sym).logo_url if metas.get(sym) else None,
            "exchange": exchange_of(sym),
            "quantity": pos.quantity,
            "avg_price": display_avg,
            "last_price": last,
            "price_as_of": q.get("price_as_of"),
            "cost_basis": display_cost,
            "lot_avg_price": pos.avg_price,
            "lot_cost_basis": cost,
            "market_value": mv,
            "invested": invested,
            "total_pnl": pnl,
            "total_pnl_percent": pnl_pct.quantize(Decimal("0.01")),
            "day_change": day_chg,
            "day_change_percent": day_pct,
            "day_change_base": day_change_base,
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
                    "avg_price": (
                        cyc.buy_amount / cyc.buy_quantity
                        if cyc.buy_quantity > ZERO
                        else ZERO
                    ),
                }
            )
        trade_count = sum(c["trade_count"] for c in cycles)
        realized_cost = sum((c["realized_cost"] for c in cycles), ZERO)
        realized_pnl = sum((c["realized_pnl"] for c in cycles), ZERO)
        buy_amount = sum((c["buy_amount"] for c in cycles), ZERO)
        sell_amount = sum((c["sell_amount"] for c in cycles), ZERO)
        buy_quantity = sum((cyc.buy_quantity for cyc in pos.closed_cycles), ZERO)
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
                "avg_price": buy_amount / buy_quantity if buy_quantity > ZERO else ZERO,
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
                "avg_price": _dec(row["avg_price"]),
                "price": row["last_price"],
                "cost": open_cost,
                "pnl": open_pnl,
                "pnl_percent": open_pct.quantize(Decimal("0.01")),
            }
        )

        # Lifetime break-even ("diluted") basis for the remaining position.
        # Realized losses raise the effective cost; realized profits lower it.
        # Everything in this row is expressed on that one basis, so the row ties
        # out:  cost = avg_price * quantity,  total_pnl = mv - cost,  pct = pnl / cost.
        # It can go negative once banked profits exceed what is still at risk.
        lifetime_realized_pnl = pos.closed_cycles_pnl + pos.realized_pnl
        effective_cost_basis = pos.cost_basis - lifetime_realized_pnl
        cumulative_avg_price = (
            effective_cost_basis / pos.quantity if pos.quantity > ZERO else ZERO
        )

        # cost is the diluted basis itself (= net cash still in this name:
        # lifetime buys minus lifetime sells). The lifetime invested figure is
        # deliberately NOT used here: pairing it with avg_price/total_pnl above
        # would mix two bases in one row.
        cumulative_cost = effective_cost_basis
        cumulative_pnl = pos.closed_cycles_pnl + open_pnl
        # A paid-off position (net cash out <= 0) has no meaningful return rate.
        cumulative_pct = (
            (cumulative_pnl / cumulative_cost * Decimal("100")).quantize(Decimal("0.01"))
            if cumulative_cost > ZERO
            else None
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
                "total_pnl_percent": cumulative_pct,
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
    cost_open = sum((_dec(i["lot_cost_basis"]) for i in open_items), ZERO)
    investment_total = cost_open + closed_cost

    trades = load_trades(db, portfolio_id)
    commissions = sum((_dec(t.commission) for t in trades), ZERO)

    # 累计盈亏（账户口径）：期末总资产 - 累计净入金。
    # 该口径与「投资组合金额」及累计本金直接对账，并自动包含股息与手续费。
    net_invested = state.net_invested()
    if state.deposits != ZERO or state.withdrawals != ZERO:
        total_pnl = portfolio_value - net_invested
    else:
        # 缺少出入金流水时（例如仅导入买卖记录），退回交易口径。
        total_pnl = open_pnl + closed_pnl - commissions

    first_nav = _first_nav_row(db)
    if first_nav:
        initial_portfolio_base = _row_portfolio_base(first_nav)
    else:
        initial_portfolio_base = investment_total + cash
    if initial_portfolio_base <= ZERO:
        initial_portfolio_base = Decimal("1")

    # 累计收益率使用累计盈亏的绝对值作为规模项，避免亏损时负分母放大比例；
    # 分子保留累计盈亏的正负号。持仓收益率仍以当前持仓市值为分母。
    # 现金不进入累计收益率公式。
    holdings_base = holdings_value if holdings_value > ZERO else initial_portfolio_base
    cumulative_base = abs(total_pnl) + holdings_value
    total_pct = (
        total_pnl / cumulative_base * Decimal("100")
        if cumulative_base > ZERO
        else ZERO
    )
    open_pct = (
        open_pnl / holdings_base * Decimal("100")
        if holdings_base > ZERO
        else ZERO
    )
    realized_pct = (closed_pnl / closed_cost * Decimal("100")) if closed_cost > ZERO else ZERO

    day_change = sum((_dec(i["day_change"]) for i in open_items if i["day_change"] is not None), ZERO)
    prev_mv = ZERO
    for i in open_items:
        base = i.get("day_change_base")
        if base is not None and _dec(base) > ZERO:
            prev_mv += _dec(base)
            continue
        # Same-session new lots have no yesterday book; skip them.
        if i.get("day_change_percent") is None:
            continue
        chg = i.get("day_change")
        pct = i.get("day_change_percent")
        if chg is not None and pct is not None and _dec(pct) != ZERO:
            prev_mv += _dec(chg) / (_dec(pct) / Decimal("100"))
            continue
        prev = _dec(i["prev_close"])
        if prev > 0:
            prev_mv += prev * i["quantity"]
        else:
            prev_mv += i["market_value"]

    realized = closed_pnl


    day_change_pct = (day_change / prev_mv * Decimal("100")) if prev_mv > ZERO else ZERO

    # 年化收益率仍按投入本金口径，与指定的累计盈亏百分比公式分开计算。
    annualized_base = investment_total + holdings_value
    annualized = ZERO
    if state.first_cash_date and annualized_base > ZERO:
        days = max((datetime.utcnow() - state.first_cash_date).days, 1)
        r = total_pnl / annualized_base
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
        "unrealized_pnl_percent": open_pct.quantize(Decimal("0.01")),
        "day_change": day_change.quantize(Decimal("0.01")),
        "day_change_percent": day_change_pct.quantize(Decimal("0.01")),
        "realized_pnl": realized,
        "realized_pnl_percent": realized_pct.quantize(Decimal("0.01")),
        "dividends": state.dividends,
        "commissions": commissions,
        "total_pnl": total_pnl,
        "total_pnl_percent": total_pct.quantize(Decimal("0.01")),
        "annualized_return": annualized,
        "currency": market_currency(),
        "as_of": datetime.utcnow(),
        "net_invested": net_invested,
    }


RANGE_DEFS = [
    ("1W", "1周", 7),
    ("1M", "1个月", 30),
    ("3M", "3个月", 90),
    ("6M", "6个月", 180),
    ("YTD", "今年", None),
    ("1Y", "1年", 365),
    ("ALL", "全部", None),
]


def _investment_total_from_state(state: PortfolioState, cost_open: Decimal) -> Decimal:
    closed_cost = sum((p.closed_cycles_cost for p in state.positions.values()), ZERO)
    total = cost_open + closed_cost
    return total if total > ZERO else ZERO


def _live_investment_total(db: Session, portfolio_id: int = 1) -> Decimal:
    data = compute_holdings(db, portfolio_id, include_closed=True)
    state: PortfolioState = data["state"]
    cost_open = sum(
        (_dec(i["lot_cost_basis"]) for i in data["items"] if not i.get("closed")),
        ZERO,
    )
    return _investment_total_from_state(state, cost_open)


def _row_investment_total(row: DailyNav) -> Decimal:
    investment_total = _dec(row.investment_total)
    if investment_total <= ZERO:
        holdings_val = _dec(row.holdings_value)
        unrealized = _dec(row.total_pnl) - _dec(row.realized_pnl)
        cost_open = holdings_val - unrealized
        investment_total = cost_open if cost_open > ZERO else ZERO
    return investment_total


def _row_investment_base(row: DailyNav) -> Decimal:
    inv = _row_investment_total(row)
    if inv > ZERO:
        return inv
    # Pre-trade / cash-only start: use portfolio base so return stays meaningful
    return _row_portfolio_base(row)


def _row_portfolio_base(row: DailyNav) -> Decimal:
    base = _row_investment_total(row) + _dec(row.cash)
    return base if base > ZERO else Decimal("1")


def _first_nav_row(db: Session, portfolio_id: int = 1) -> DailyNav | None:
    return (
        db.query(DailyNav)
        .filter(DailyNav.portfolio_id == portfolio_id)
        .order_by(DailyNav.nav_date.asc())
        .first()
    )


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
    from app.services.market_scope import current_market

    # The stored NAV is the US book. Other markets are computed in memory so a
    # filtered request cannot overwrite that cache.
    persist = current_market() == "us"
    existing = []
    if persist:
        existing = (
            db.query(DailyNav)
            .filter(DailyNav.portfolio_id == portfolio_id)
            .order_by(DailyNav.nav_date.asc())
            .all()
        )
    if existing and not rebuild:
        if _nav_series_needs_rebuild(existing):
            rebuild = True
        else:
            return existing

    trades = load_trades(db, portfolio_id)
    if not trades:
        return []

    start = trade_us_session_date(trades[0].closing_time)
    end = us_equity_session_date()
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

    if persist:
        db.query(DailyNav).filter(DailyNav.portfolio_id == portfolio_id).delete()
        db.commit()

    rows: list[DailyNav] = []
    day = start
    while day <= end:
        if day.weekday() >= 5:
            day += timedelta(days=1)
            continue

        state = replay_until_session(trades, day)
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
        net_invested = state.net_invested()
        if state.deposits != ZERO or state.withdrawals != ZERO:
            # Account口径: cumulative P/L is NAV minus net cash flows. This
            # attributes commissions and dividends to the session incurring them.
            total_pnl = total_value - net_invested
        else:
            total_pnl = unrealized + state.realized_pnl - state.commissions
        investment_total = _investment_total_from_state(state, cost_open)
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
            investment_total=investment_total,
        )
        if persist:
            db.add(row)
        rows.append(row)
        day += timedelta(days=1)

    if not persist:
        return rows
    db.commit()
    return (
        db.query(DailyNav)
        .filter(DailyNav.portfolio_id == portfolio_id)
        .order_by(DailyNav.nav_date.asc())
        .all()
    )


def _sync_live_today_nav(db: Session, rows: list[DailyNav], portfolio_id: int = 1) -> list[DailyNav]:
    """Align the current US session NAV row with live holdings."""
    from app.services.market_scope import current_market

    persist = current_market() == "us"
    summary = compute_summary(db, portfolio_id)
    live_investment_total = _live_investment_total(db, portfolio_id)
    live_value = _dec(summary["portfolio_value"])
    live_cash = _dec(summary["cash"])
    live_pnl = _dec(summary["total_pnl"])
    live_realized = _dec(summary["realized_pnl"])
    session_today = us_equity_session_date()

    if not rows:
        return rows

    last = rows[-1]
    if last.nav_date == session_today:
        last.total_value = live_value
        last.cash = live_cash
        last.total_pnl = live_pnl
        last.realized_pnl = live_realized
        last.holdings_value = live_value - live_cash
        last.net_invested = _dec(summary.get("net_invested"))
        last.investment_total = live_investment_total
        if persist:
            db.add(last)
            db.commit()
            db.refresh(last)
        return rows

    if last.nav_date < session_today:
        row = DailyNav(
            portfolio_id=portfolio_id,
            nav_date=session_today,
            total_value=live_value,
            cash=live_cash,
            holdings_value=live_value - live_cash,
            realized_pnl=live_realized,
            total_pnl=live_pnl,
            net_invested=_dec(summary.get("net_invested")),
            investment_total=live_investment_total,
        )
        if persist:
            db.add(row)
            db.commit()
            db.refresh(row)
        return [*rows, row]

    return rows


def nav_response(db: Session, range_key: str = "3M", mode: str = "value") -> dict:
    rows = build_nav_series(db)
    rows = _sync_live_today_nav(db, rows)
    summary = compute_summary(db)
    live_value = _dec(summary["portfolio_value"])
    live_cash = _dec(summary["cash"])
    live_pnl = _dec(summary["total_pnl"])
    live_realized = _dec(summary["realized_pnl"])

    first = rows[0].nav_date if rows else date.today()
    start = range_start(range_key, first)
    filtered = [r for r in rows if r.nav_date >= start]

    start_row = filtered[0] if filtered else None
    start_pnl = _dec(start_row.total_pnl) if start_row else ZERO
    start_portfolio_base = _row_portfolio_base(start_row) if start_row else Decimal("1")
    start_investment_base = _row_investment_base(start_row) if start_row else Decimal("1")

    first_row = rows[0] if rows else None
    initial_pnl = _dec(first_row.total_pnl) if first_row else ZERO
    initial_portfolio_base = _row_portfolio_base(first_row) if first_row else Decimal("1")
    initial_investment_base = _row_investment_base(first_row) if first_row else Decimal("1")

    live_period_pnl = live_pnl - start_pnl
    live_all_pnl = live_pnl - initial_pnl
    live_return = (live_period_pnl / start_portfolio_base * Decimal("100")).quantize(Decimal("0.01"))
    live_holdings_return = (live_period_pnl / start_investment_base * Decimal("100")).quantize(
        Decimal("0.01")
    )
    live_all_return = (live_all_pnl / initial_portfolio_base * Decimal("100")).quantize(Decimal("0.01"))
    live_all_holdings_return = (live_all_pnl / initial_investment_base * Decimal("100")).quantize(
        Decimal("0.01")
    )

    points = []
    for r in filtered:
        value = _dec(r.total_value)
        cash = _dec(r.cash)
        holdings_val = _dec(r.holdings_value)
        pnl = _dec(r.total_pnl)
        period_pnl = pnl - start_pnl
        ret_pct = period_pnl / start_portfolio_base * Decimal("100")
        holdings_pct = period_pnl / start_investment_base * Decimal("100")
        points.append(
            {
                "date": r.nav_date,
                "total_value": value,
                "cash": cash,
                "holdings_value": holdings_val,
                "realized_pnl": r.realized_pnl,
                "total_pnl": r.total_pnl,
                "return_percent": ret_pct,
                "holdings_percent": holdings_pct,
                "cash_percent": ZERO,
            }
        )

    # Force last point to live summary (authoritative)
    if points:
        live_holdings = live_value - live_cash
        points[-1]["total_value"] = live_value
        points[-1]["cash"] = live_cash
        points[-1]["holdings_value"] = live_holdings
        points[-1]["total_pnl"] = live_pnl
        points[-1]["realized_pnl"] = live_realized
        points[-1]["return_percent"] = live_return
        points[-1]["holdings_percent"] = live_holdings_return
        points[-1]["cash_percent"] = ZERO

    empty_range = {
        "return_percent": None,
        "holdings_return_percent": None,
        "cash_return_percent": None,
        "pnl": None,
        "holdings_pnl": None,
        "cash_pnl": None,
    }
    # Periods that require full window coverage (match TradingView empty dashes)
    strict_keys = {"6M", "YTD", "1Y"}
    live_holdings = live_value - live_cash
    ranges = []
    for key, label, _ in RANGE_DEFS:
        rs = range_start(key, first)
        subset = [r for r in rows if r.nav_date >= rs]
        if len(subset) < 1:
            ranges.append({"key": key, "label": label, **empty_range})
            continue
        # History shorter than requested window → show "—"
        if key in strict_keys and first > rs:
            ranges.append({"key": key, "label": label, **empty_range})
            continue
        start_row = subset[0]
        start_investment = _row_investment_base(start_row)
        start_cash = _dec(start_row.cash)
        start_portfolio_base = _row_portfolio_base(start_row)
        start_pnl_row = _dec(start_row.total_pnl)
        period_pnl = live_pnl - start_pnl_row
        if key == "ALL":
            pnl = live_pnl
            ret = live_all_return
            holdings_ret = live_all_holdings_return
        else:
            pnl = period_pnl
            ret = (
                period_pnl / start_portfolio_base * Decimal("100")
                if start_portfolio_base > ZERO
                else None
            )
            holdings_ret = (
                period_pnl / start_investment * Decimal("100") if start_investment > ZERO else None
            )
        live_holdings = live_value - live_cash
        start_holdings = _dec(start_row.holdings_value)
        holdings_pnl = live_holdings - start_holdings
        cash_pnl = live_cash - start_cash
        cash_ret = ZERO
        ranges.append(
            {
                "key": key,
                "label": label,
                "return_percent": ret,
                "holdings_return_percent": holdings_ret,
                "cash_return_percent": cash_ret,
                "pnl": pnl,
                "holdings_pnl": holdings_pnl,
                "cash_pnl": cash_pnl,
            }
        )

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


def _build_daily_pnl_items(rows: list[DailyNav]) -> list[dict]:
    """Daily PnL by US equity session date (consecutive session deltas)."""
    all_items: list[dict] = []
    prev_pnl: Decimal | None = None
    prev_value: Decimal | None = None

    for row in rows:
        if row.nav_date.weekday() >= 5:
            continue

        pnl = _dec(row.total_pnl)
        row_value = _dec(row.total_value)

        if prev_pnl is None:
            # Opening session: cumulative P/L is that day's P/L. Capital already
            # in the account (deposits, before the day's result) is the base.
            day_pnl = pnl
            base_value = row_value - day_pnl
            if base_value <= ZERO:
                base_value = row_value
        else:
            day_pnl = pnl - prev_pnl
            base_value = prev_value if prev_value is not None else row_value
        day_pct = (day_pnl / base_value * Decimal("100")) if base_value > ZERO else ZERO
        all_items.append(
            {
                "date": row.nav_date,
                "pnl": day_pnl.quantize(Decimal("0.01")),
                "pnl_percent": day_pct.quantize(Decimal("0.01")),
            }
        )

        prev_pnl = pnl
        prev_value = row_value

    return all_items


def _pnl_period_summary(items: list[dict]) -> dict:
    total = sum((item["pnl"] for item in items), ZERO)
    profit_days = sum(1 for item in items if item["pnl"] > ZERO)
    loss_days = sum(1 for item in items if item["pnl"] < ZERO)
    flat_days = sum(1 for item in items if item["pnl"] == ZERO)
    pnls = [item["pnl"] for item in items if item["pnl"] != ZERO]
    best = max(pnls) if pnls else None
    worst = min(pnls) if pnls else None
    return {
        "total_pnl": total.quantize(Decimal("0.01")),
        "profit_days": profit_days,
        "loss_days": loss_days,
        "flat_days": flat_days,
        "best_day_pnl": best.quantize(Decimal("0.01")) if best is not None else None,
        "worst_day_pnl": worst.quantize(Decimal("0.01")) if worst is not None else None,
    }


def _aggregate_monthly(all_items: list[dict], rows_by_date: dict[date, DailyNav], year: int) -> list[dict]:
    buckets: dict[int, Decimal] = {}
    for item in all_items:
        if item["date"].year != year:
            continue
        m = item["date"].month
        buckets[m] = buckets.get(m, ZERO) + item["pnl"]
    items = []
    for m in sorted(buckets):
        pnl = buckets[m]
        base = _period_start_base(rows_by_date, date(year, m, 1))
        pct = (pnl / base * Decimal("100")) if base > ZERO else ZERO
        items.append(
            {
                "date": date(year, m, 1),
                "pnl": pnl.quantize(Decimal("0.01")),
                "pnl_percent": pct.quantize(Decimal("0.01")),
            }
        )
    return items


def _aggregate_quarterly(all_items: list[dict], rows_by_date: dict[date, DailyNav], year: int) -> list[dict]:
    buckets: dict[int, Decimal] = {}
    for item in all_items:
        if item["date"].year != year:
            continue
        quarter = (item["date"].month - 1) // 3 + 1
        buckets[quarter] = buckets.get(quarter, ZERO) + item["pnl"]
    items = []
    for quarter in sorted(buckets):
        pnl = buckets[quarter]
        start_month = (quarter - 1) * 3 + 1
        base = _period_start_base(rows_by_date, date(year, start_month, 1))
        pct = (pnl / base * Decimal("100")) if base > ZERO else ZERO
        items.append(
            {
                "date": date(year, start_month, 1),
                "pnl": pnl.quantize(Decimal("0.01")),
                "pnl_percent": pct.quantize(Decimal("0.01")),
            }
        )
    return items


def _aggregate_yearly(all_items: list[dict], rows_by_date: dict[date, DailyNav]) -> list[dict]:
    buckets: dict[int, Decimal] = {}
    for item in all_items:
        y = item["date"].year
        buckets[y] = buckets.get(y, ZERO) + item["pnl"]
    items = []
    for y in sorted(buckets):
        pnl = buckets[y]
        base = _period_start_base(rows_by_date, date(y, 1, 1))
        pct = (pnl / base * Decimal("100")) if base > ZERO else ZERO
        items.append(
            {
                "date": date(y, 1, 1),
                "pnl": pnl.quantize(Decimal("0.01")),
                "pnl_percent": pct.quantize(Decimal("0.01")),
            }
        )
    return items


def _period_start_base(rows_by_date: dict[date, DailyNav], period_start: date) -> Decimal:
    """Portfolio value the period's P/L is measured against.

    Same idea as the daily rate (that day's P/L / previous session's portfolio
    value): the last session before the period. Cumulative cost is not used —
    closed trades would keep inflating it. If the account opens during the
    period, use the capital already in on that first session.
    """
    prior_dates = [nav_date for nav_date in rows_by_date if nav_date < period_start]
    if prior_dates:
        base = _dec(rows_by_date[max(prior_dates)].total_value)
        return base if base > ZERO else ZERO

    inside = [nav_date for nav_date in rows_by_date if nav_date >= period_start]
    if not inside:
        return ZERO
    first = rows_by_date[min(inside)]
    base = _dec(first.total_value) - _dec(first.total_pnl)
    if base <= ZERO:
        base = _dec(first.total_value)
    return base if base > ZERO else ZERO


def daily_pnl_calendar(
    db: Session,
    portfolio_id: int = 1,
    granularity: str = "day",
    year: int | None = None,
    month: int | None = None,
) -> dict:
    today = us_equity_session_date()
    granularity = granularity if granularity in {"day", "month", "quarter", "year"} else "day"

    rows = build_nav_series(db, portfolio_id=portfolio_id)
    rows = _sync_live_today_nav(db, rows, portfolio_id=portfolio_id)
    empty_year = year or today.year
    empty_month = month or today.month
    empty = {
        "granularity": granularity,
        "year": empty_year,
        "month": empty_month if granularity == "day" else None,
        "years": [empty_year],
        "months": [empty_month],
        "items": [],
        "summary": _pnl_period_summary([]),
    }
    if not rows:
        return empty

    all_items = _build_daily_pnl_items(rows)
    rows_by_date = {row.nav_date: row for row in rows}
    years = sorted({item["date"].year for item in all_items})

    if granularity == "day":
        target_year = year if year is not None else today.year
        if years and target_year not in years:
            target_year = years[-1]
        months = sorted({item["date"].month for item in all_items if item["date"].year == target_year})
        if not months:
            months = [today.month]
        target_month = month if month is not None else today.month
        if target_month not in months:
            target_month = months[-1]
        items = [
            item
            for item in all_items
            if item["date"].year == target_year and item["date"].month == target_month
        ]
        return {
            "granularity": granularity,
            "year": target_year,
            "month": target_month,
            "years": years,
            "months": months,
            "items": items,
            "summary": _pnl_period_summary(items),
        }

    if granularity == "month":
        target_year = year if year is not None else today.year
        if years and target_year not in years:
            target_year = years[-1]
        items = _aggregate_monthly(all_items, rows_by_date, target_year)
        months = sorted({item["date"].month for item in all_items if item["date"].year == target_year})
        return {
            "granularity": granularity,
            "year": target_year,
            "month": None,
            "years": years,
            "months": months or [today.month],
            "items": items,
            "summary": _pnl_period_summary(items),
        }

    if granularity == "quarter":
        target_year = year if year is not None else today.year
        if years and target_year not in years:
            target_year = years[-1]
        items = _aggregate_quarterly(all_items, rows_by_date, target_year)
        return {
            "granularity": granularity,
            "year": target_year,
            "month": None,
            "years": years,
            "months": [],
            "items": items,
            "summary": _pnl_period_summary(items),
        }

    items = _aggregate_yearly(all_items, rows_by_date)
    target_year = year if year is not None else (years[-1] if years else today.year)
    return {
        "granularity": granularity,
        "year": target_year,
        "month": None,
        "years": years,
        "months": [],
        "items": items,
        "summary": _pnl_period_summary(items),
    }

def _close_on_or_before(closes: dict[str, Decimal], day: date) -> Decimal | None:
    if not closes:
        return None
    day_key = day.isoformat()
    candidates = [key for key in closes if key <= day_key]
    if not candidates:
        return None
    return closes[max(candidates)]


def period_holdings(db: Session, start: date, end: date, portfolio_id: int = 1) -> dict:
    """Per-symbol P/L from the session before `start` through `end`.

    Same mark as a single day: ending shares at the period's last price, minus
    shares held at the previous close, minus buys, plus sells.
    """
    if end < start:
        start, end = end, start
    today = us_equity_session_date()
    period_end = min(end, today)
    while period_end.weekday() >= 5:
        period_end -= timedelta(days=1)
    period_start = start
    while period_start.weekday() >= 5:
        period_start += timedelta(days=1)

    empty_totals = {
        "total_gross_pnl": ZERO,
        "total_commission": ZERO,
        "total_pnl": ZERO,
    }
    empty = {
        "date": period_end,
        "start_date": start,
        "end_date": period_end,
        **empty_totals,
        "items": [],
    }
    if period_start > period_end:
        return empty

    trades = load_trades(db, portfolio_id)
    trade_symbols = sorted(
        {
            t.symbol.upper()
            for t in trades
            if t.symbol and t.side in {"Buy", "Sell"}
        }
    )
    if not trade_symbols:
        return empty

    prior = previous_us_session_date(period_start)
    opening_state = replay_until_session(trades, prior)
    closing_state = replay_until_session(trades, period_end)
    trades_by_symbol: dict[str, list[Trade]] = {}
    for trade in trades:
        if trade.symbol and trade.side in {"Buy", "Sell"}:
            trades_by_symbol.setdefault(trade.symbol.upper(), []).append(trade)

    relevant: list[str] = []
    window_trades: dict[str, list[Trade]] = {}
    for symbol in trade_symbols:
        opening_qty = opening_state.positions.get(symbol, PositionState()).quantity
        closing_qty = closing_state.positions.get(symbol, PositionState()).quantity
        symbol_trades = [
            trade
            for trade in trades_by_symbol.get(symbol, [])
            if period_start <= trade_us_session_date(trade.closing_time) <= period_end
        ]
        if opening_qty > ZERO or closing_qty > ZERO or symbol_trades:
            relevant.append(symbol)
            window_trades[symbol] = symbol_trades

    if not relevant:
        return empty

    include_live = period_end == today
    quotes = get_quotes(db, relevant) if include_live else {}
    histories = get_history_closes(
        relevant,
        start=datetime.combine(prior - timedelta(days=14), datetime.min.time()),
        end=datetime.combine(period_end, datetime.max.time()),
    )
    metas = {
        meta.symbol: meta
        for meta in db.query(SymbolMeta).filter(SymbolMeta.symbol.in_(relevant)).all()
    }

    items: list[dict] = []
    total_gross_pnl = ZERO
    total_commission = ZERO
    total_pnl = ZERO
    for symbol in relevant:
        opening_qty = opening_state.positions.get(symbol, PositionState()).quantity
        closing_qty = closing_state.positions.get(symbol, PositionState()).quantity
        symbol_trades = window_trades.get(symbol, [])
        buy_amount = sum(
            (_dec(trade.qty) * _dec(trade.fill_price) for trade in symbol_trades if trade.side == "Buy"),
            ZERO,
        )
        sell_amount = sum(
            (_dec(trade.qty) * _dec(trade.fill_price) for trade in symbol_trades if trade.side == "Sell"),
            ZERO,
        )
        commission = sum((_dec(trade.commission) for trade in symbol_trades), ZERO)

        closes = histories.get(symbol, {})
        prev_close = _close_on_or_before(closes, prior)
        close_price = _close_on_or_before(closes, period_end)
        if include_live:
            quote = quotes.get(symbol, {})
            if quote.get("price") is not None:
                close_price = _dec(quote.get("price"))

        gross_pnl = None
        if close_price is not None and prev_close is not None:
            gross_pnl = closing_qty * close_price - opening_qty * prev_close - buy_amount + sell_amount
        elif close_price is not None and opening_qty == ZERO:
            gross_pnl = closing_qty * close_price - buy_amount + sell_amount
        elif opening_qty == ZERO and closing_qty == ZERO and (buy_amount or sell_amount):
            gross_pnl = sell_amount - buy_amount

        day_pnl = gross_pnl - commission if gross_pnl is not None else None
        denominator = opening_qty * prev_close + buy_amount if prev_close is not None else buy_amount
        gross_pct = (
            gross_pnl / denominator * Decimal("100")
            if gross_pnl is not None and denominator > ZERO
            else None
        )
        day_pct = (
            day_pnl / denominator * Decimal("100")
            if day_pnl is not None and denominator > ZERO
            else None
        )
        if gross_pnl is not None:
            total_gross_pnl += gross_pnl
            total_pnl += day_pnl
        total_commission += commission

        if closing_qty > ZERO:
            status = "持仓"
        elif opening_qty > ZERO:
            status = "已清仓"
        elif buy_amount > ZERO and sell_amount > ZERO:
            status = "日内交易"
        else:
            status = "已平仓"

        meta = metas.get(symbol)
        items.append(
            {
                "symbol": symbol,
                "name": meta.name if meta else None,
                "logo_url": meta.logo_url if meta else None,
                "exchange": exchange_of(symbol),
                "opening_quantity": opening_qty,
                "quantity": closing_qty,
                "prev_close": prev_close,
                "close_price": close_price,
                "buy_amount": buy_amount,
                "sell_amount": sell_amount,
                "commission": commission.quantize(Decimal("0.01")),
                "gross_pnl": gross_pnl.quantize(Decimal("0.01")) if gross_pnl is not None else None,
                "gross_pnl_percent": gross_pct.quantize(Decimal("0.01")) if gross_pct is not None else None,
                "day_pnl": day_pnl.quantize(Decimal("0.01")) if day_pnl is not None else None,
                "day_pnl_percent": day_pct.quantize(Decimal("0.01")) if day_pct is not None else None,
                "status": status,
                "trade_count": len(symbol_trades),
                "pricing_complete": gross_pnl is not None,
            }
        )

    items.sort(key=lambda item: item["symbol"])
    return {
        "date": period_end,
        "start_date": start,
        "end_date": period_end,
        "total_gross_pnl": total_gross_pnl.quantize(Decimal("0.01")),
        "total_commission": total_commission.quantize(Decimal("0.01")),
        "total_pnl": total_pnl.quantize(Decimal("0.01")),
        "items": items,
    }


def daily_holdings(db: Session, session_day: date, portfolio_id: int = 1) -> dict:
    """Return per-symbol P/L for every stock held or traded on a session date."""
    target_day = session_day
    while target_day.weekday() >= 5:
        target_day -= timedelta(days=1)

    trades = load_trades(db, portfolio_id)
    trade_symbols = sorted(
        {
            t.symbol.upper()
            for t in trades
            if t.symbol and t.side in {"Buy", "Sell"}
        }
    )
    empty_totals = {
        "total_gross_pnl": ZERO,
        "total_commission": ZERO,
        "total_pnl": ZERO,
    }
    if not trade_symbols:
        return {"date": target_day, **empty_totals, "items": []}

    opening_state = replay_until_session(trades, previous_us_session_date(target_day))
    closing_state = replay_until_session(trades, target_day)
    trades_by_symbol: dict[str, list[Trade]] = {}
    for trade in trades:
        if trade.symbol and trade.side in {"Buy", "Sell"}:
            trades_by_symbol.setdefault(trade.symbol.upper(), []).append(trade)

    relevant: list[str] = []
    day_trades: dict[str, list[Trade]] = {}
    for symbol in trade_symbols:
        opening_qty = opening_state.positions.get(symbol, PositionState()).quantity
        closing_qty = closing_state.positions.get(symbol, PositionState()).quantity
        symbol_day_trades = [
            trade
            for trade in trades_by_symbol.get(symbol, [])
            if trade_us_session_date(trade.closing_time) == target_day
        ]
        if opening_qty > ZERO or closing_qty > ZERO or symbol_day_trades:
            relevant.append(symbol)
            day_trades[symbol] = symbol_day_trades

    if not relevant:
        return {"date": target_day, **empty_totals, "items": []}

    quotes: dict[str, dict] = {}
    histories: dict[str, dict[str, Decimal]] = {}
    today = us_equity_session_date()
    if target_day == today:
        quotes = get_quotes(db, relevant)
    else:
        histories = get_history_closes(
            relevant,
            start=datetime.combine(target_day - timedelta(days=14), datetime.min.time()),
            end=datetime.combine(target_day, datetime.max.time()),
        )

    metas = {
        meta.symbol: meta
        for meta in db.query(SymbolMeta).filter(SymbolMeta.symbol.in_(relevant)).all()
    }

    items: list[dict] = []
    total_gross_pnl = ZERO
    total_commission = ZERO
    total_pnl = ZERO
    for symbol in relevant:
        opening_qty = opening_state.positions.get(symbol, PositionState()).quantity
        closing_qty = closing_state.positions.get(symbol, PositionState()).quantity
        symbol_trades = day_trades.get(symbol, [])
        buy_amount = sum(
            (
                _dec(trade.qty) * _dec(trade.fill_price)
                for trade in symbol_trades
                if trade.side == "Buy"
            ),
            ZERO,
        )
        sell_amount = sum(
            (
                _dec(trade.qty) * _dec(trade.fill_price)
                for trade in symbol_trades
                if trade.side == "Sell"
            ),
            ZERO,
        )
        commission = sum((_dec(trade.commission) for trade in symbol_trades), ZERO)

        close_price = None
        prev_close = None
        if target_day == today:
            quote = quotes.get(symbol, {})
            close_price = _dec(quote.get("price")) if quote.get("price") is not None else None
            prev_close = (
                _dec(quote.get("prev_close")) if quote.get("prev_close") is not None else None
            )
        else:
            closes = histories.get(symbol, {})
            close_price = closes.get(target_day.isoformat())
            prior_dates = [
                date.fromisoformat(day_key)
                for day_key in closes
                if day_key < target_day.isoformat()
            ]
            if prior_dates:
                prev_close = closes.get(max(prior_dates).isoformat())

        gross_pnl = None
        if close_price is not None and prev_close is not None:
            gross_pnl = (
                closing_qty * close_price
                - opening_qty * prev_close
                - buy_amount
                + sell_amount
            )
        elif close_price is not None and opening_qty == ZERO:
            # New same-session position: cost is today's fills, not yesterday's close.
            gross_pnl = closing_qty * close_price - buy_amount + sell_amount
        elif opening_qty == ZERO and closing_qty == ZERO and (buy_amount or sell_amount):
            # A same-day round trip is fully determined by its fills.
            gross_pnl = sell_amount - buy_amount

        day_pnl = gross_pnl - commission if gross_pnl is not None else None
        day_denominator = opening_qty * prev_close + buy_amount if prev_close is not None else buy_amount
        gross_pct = (
            gross_pnl / day_denominator * Decimal("100")
            if gross_pnl is not None and day_denominator > ZERO
            else None
        )
        day_pct = (
            day_pnl / day_denominator * Decimal("100")
            if day_pnl is not None and day_denominator > ZERO
            else None
        )
        if gross_pnl is not None:
            total_gross_pnl += gross_pnl
            total_pnl += day_pnl
        total_commission += commission

        if closing_qty > ZERO:
            status = "持仓"
        elif opening_qty > ZERO:
            status = "已清仓"
        elif buy_amount > ZERO and sell_amount > ZERO:
            status = "日内交易"
        else:
            status = "已平仓"

        meta = metas.get(symbol)
        items.append(
            {
                "symbol": symbol,
                "name": meta.name if meta else None,
                "logo_url": meta.logo_url if meta else None,
                "exchange": exchange_of(symbol),
                "opening_quantity": opening_qty,
                "quantity": closing_qty,
                "prev_close": prev_close,
                "close_price": close_price,
                "buy_amount": buy_amount,
                "sell_amount": sell_amount,
                "commission": commission.quantize(Decimal("0.01")),
                "gross_pnl": (
                    gross_pnl.quantize(Decimal("0.01")) if gross_pnl is not None else None
                ),
                "gross_pnl_percent": (
                    gross_pct.quantize(Decimal("0.01")) if gross_pct is not None else None
                ),
                "day_pnl": day_pnl.quantize(Decimal("0.01")) if day_pnl is not None else None,
                "day_pnl_percent": (
                    day_pct.quantize(Decimal("0.01")) if day_pct is not None else None
                ),
                "status": status,
                "trade_count": len(symbol_trades),
                "pricing_complete": gross_pnl is not None,
            }
        )

    items.sort(key=lambda item: item["symbol"])
    return {
        "date": target_day,
        "total_gross_pnl": total_gross_pnl.quantize(Decimal("0.01")),
        "total_commission": total_commission.quantize(Decimal("0.01")),
        "total_pnl": total_pnl.quantize(Decimal("0.01")),
        "items": items,
    }


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

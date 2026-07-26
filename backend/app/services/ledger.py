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
class PositionState:
    quantity: Decimal = ZERO
    avg_price: Decimal = ZERO
    cost_basis: Decimal = ZERO
    realized_pnl: Decimal = ZERO
    realized_cost: Decimal = ZERO  # cost basis of sold shares (for closed % return)
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
        return
    if not symbol:
        return

    pos = state.positions.setdefault(symbol, PositionState())

    if side == "Buy":
        cost = qty * price + commission
        state.cash -= cost
        new_qty = pos.quantity + qty
        if new_qty > 0:
            pos.cost_basis = pos.cost_basis + cost
            pos.avg_price = pos.cost_basis / new_qty
            pos.quantity = new_qty
        return

    if side == "Sell":
        proceeds = qty * price - commission
        state.cash += proceeds
        sell_qty = min(qty, pos.quantity) if pos.quantity > 0 else qty
        cost_removed = pos.avg_price * sell_qty
        pnl = proceeds - cost_removed if pos.quantity > 0 else proceeds
        # If selling more than held, treat remaining as realized at fill
        if qty > pos.quantity and pos.quantity > 0:
            pnl = (pos.quantity * price - commission * (pos.quantity / qty)) - cost_removed
            pnl += (qty - pos.quantity) * price
        pos.realized_pnl += pnl
        pos.realized_cost += cost_removed
        state.realized_pnl += pnl
        pos.quantity -= sell_qty
        pos.cost_basis = pos.avg_price * pos.quantity
        if pos.quantity <= ZERO:
            pos.quantity = ZERO
            pos.cost_basis = ZERO
            pos.avg_price = ZERO
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
    symbols = list(state.positions.keys())
    quotes = get_quotes(db, symbols)
    metas = {
        m.symbol: m
        for m in db.query(SymbolMeta).filter(SymbolMeta.symbol.in_(symbols)).all()
    } if symbols else {}

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

        if not closed:
            # Open: total P/L = unrealized + realized; % vs all capital deployed
            mv = pos.quantity * (last if last is not None else pos.avg_price)
            cost = pos.cost_basis
            unrealized = mv - cost
            pnl = unrealized + pos.realized_pnl
            invested = cost + pos.realized_cost
            if invested > ZERO:
                pnl_pct = pnl / invested * Decimal("100")
            elif pos.avg_price > ZERO and last is not None:
                pnl_pct = (last / pos.avg_price - Decimal("1")) * Decimal("100")
            else:
                pnl_pct = ZERO
        else:
            # Closed: realized P/L vs cost of sold shares
            mv = ZERO
            cost = pos.realized_cost
            pnl = pos.realized_pnl
            pnl_pct = (pnl / cost * Decimal("100")) if cost > ZERO else ZERO

        day_chg = None
        day_pct = q.get("change_percent")
        if not closed and q.get("change") is not None:
            day_chg = _dec(q.get("change")) * pos.quantity

        row = {
            "symbol": sym,
            "name": metas.get(sym).name if metas.get(sym) else None,
            "logo_url": metas.get(sym).logo_url if metas.get(sym) else None,
            "exchange": exchange_of(sym),
            "quantity": pos.quantity,
            "avg_price": pos.avg_price,
            "last_price": last,
            "cost_basis": cost,
            "market_value": mv,
            "total_pnl": pnl,
            "total_pnl_percent": pnl_pct.quantize(Decimal("0.01")),
            "day_change": day_chg,
            "day_change_percent": day_pct,
            "open_price": q.get("open"),
            "high_price": q.get("high"),
            "low_price": q.get("low"),
            "prev_close": q.get("prev_close"),
            "closed": closed,
            "realized_pnl": pos.realized_pnl,
            "dividends": pos.dividends,
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


def compute_summary(db: Session, portfolio_id: int = 1) -> dict:
    data = compute_holdings(db, portfolio_id, include_closed=False)
    state: PortfolioState = data["state"]
    holdings_value = data["total_market_value"]
    cash = state.cash
    portfolio_value = holdings_value + cash

    cost_open = sum((i["cost_basis"] for i in data["items"]), ZERO)
    unrealized = sum((i["total_pnl"] for i in data["items"]), ZERO)
    unrealized_pct = (unrealized / cost_open * Decimal("100")) if cost_open > 0 else ZERO

    day_change = sum((_dec(i["day_change"]) for i in data["items"] if i["day_change"] is not None), ZERO)
    prev_mv = ZERO
    for i in data["items"]:
        prev = _dec(i["prev_close"])
        if prev > 0:
            prev_mv += prev * i["quantity"]
        else:
            prev_mv += i["market_value"]

    realized = state.realized_pnl
    total_pnl = unrealized + realized

    trades = load_trades(db, portfolio_id)
    commissions = sum((_dec(t.commission) for t in trades), ZERO)

    net_invested = state.net_invested()
    if net_invested == ZERO:
        # No Deposit rows: use gross buy notional as invested-capital proxy
        buy_notional = ZERO
        for t in trades:
            if t.side == "Buy":
                buy_notional += _dec(t.qty) * _dec(t.fill_price) + _dec(t.commission)
        net_invested = buy_notional if buy_notional > ZERO else (cost_open if cost_open > ZERO else Decimal("1"))

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
        "cash": cash,
        "unrealized_pnl": unrealized,
        "unrealized_pnl_percent": unrealized_pct,
        "day_change": day_change.quantize(Decimal("0.01")),
        "day_change_percent": day_change_pct.quantize(Decimal("0.01")),
        "realized_pnl": realized,
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


def total_movers(
    db: Session,
    portfolio_id: int = 1,
    include_closed: bool = False,
) -> dict:
    """Overall P/L winners & losers (not day change). No count limit."""
    data = compute_holdings(db, portfolio_id, include_closed=include_closed)
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

"""Trading-behaviour review: replay the ledger and measure how the account is
actually traded, not just what it earned.

Everything here is derived from the same weighted-average cost basis that
`app.services.ledger` uses, so the numbers line up with the holdings page.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal


from sqlalchemy.orm import Session

from app.models.entities import Trade
from app.services import market
from app.services.ledger import load_trades, trade_us_session_date, us_equity_session_date

ZERO = Decimal("0")
_EPS = Decimal("0.0000001")
HARD_STOP_PCT = Decimal("0.12")
TRAILING_STOP_PCT = Decimal("0.08")
REENTRY_COOLDOWN_SESSIONS = 5
REENTRY_LOOKBACK_DAYS = 180
REENTRY_MAX_SYMBOLS = 20

# Holdings and strategy are opened back-to-back in normal use. Daily closes only
# need occasional refreshes; current quotes still update the stop on every call.
_HISTORY_CACHE_TTL = timedelta(minutes=5)
_history_cache: dict[
    tuple[tuple[str, ...], str, str], tuple[datetime, dict[str, dict[str, Decimal]]]
] = {}


@dataclass
class Cycle:
    """One 建仓 → 清仓 period for a symbol."""

    symbol: str
    started_at: datetime
    ended_at: datetime
    buy_qty: Decimal = ZERO
    sell_qty: Decimal = ZERO
    buy_amount: Decimal = ZERO
    sell_amount: Decimal = ZERO
    realized: Decimal = ZERO
    cost: Decimal = ZERO
    trades: int = 0
    # Buys made while the position was already below its running average cost.
    addon_below_avg: int = 0

    @property
    def holding_days(self) -> int:
        return max((self.ended_at - self.started_at).days, 0)

    @property
    def return_pct(self) -> Decimal:
        if self.cost <= ZERO:
            return ZERO
        return (self.realized / self.cost) * Decimal("100")


@dataclass
class _Pos:
    qty: Decimal = ZERO
    avg: Decimal = ZERO
    cost: Decimal = ZERO


def _replay(trades: list[Trade]) -> dict:
    """Walk the trades and produce closed cycles, open positions and flags."""
    positions: dict[str, _Pos] = {}
    open_cycles: dict[str, Cycle] = {}
    closed: list[Cycle] = []

    deposits = ZERO
    buy_notional = ZERO
    sell_notional = ZERO
    commissions = ZERO
    day_trades: dict[tuple[str, object], dict[str, Decimal]] = defaultdict(
        lambda: {
            "buy_qty": ZERO,
            "buy_amount": ZERO,
            "sell_qty": ZERO,
            "sell_amount": ZERO,
        }
    )

    for t in trades:
        side = (t.side or "").strip()
        symbol = (t.symbol or "").strip().upper()
        qty = Decimal(t.qty or 0)
        price = Decimal(t.fill_price or 0)
        commission = Decimal(t.commission or 0)
        commissions += commission

        if side == "Deposit":
            deposits += price if price != ZERO else qty
            continue
        if side == "Withdrawal":
            deposits -= price if price != ZERO else qty
            continue
        if side not in ("Buy", "Sell") or not symbol:
            continue

        pos = positions.setdefault(symbol, _Pos())
        notional = qty * price
        day = day_trades[(symbol, trade_us_session_date(t.closing_time))]
        if side == "Buy":
            day["buy_qty"] += qty
            day["buy_amount"] += notional
        else:
            day["sell_qty"] += qty
            day["sell_amount"] += notional

        if side == "Buy":
            if pos.qty <= _EPS:
                # Either flat, or a phantom sell left it negative: start clean.
                open_cycles[symbol] = Cycle(symbol, t.closing_time, t.closing_time)
                pos.qty = ZERO
                pos.cost = ZERO
                pos.avg = ZERO
            cyc = open_cycles[symbol]
            if pos.qty > _EPS and price < pos.avg:
                cyc.addon_below_avg += 1
            buy_notional += notional
            cyc.trades += 1
            cyc.buy_qty += qty
            cyc.buy_amount += notional
            pos.cost += notional
            pos.qty += qty
            # A phantom sell can leave qty at or below zero; never divide by it.
            pos.avg = pos.cost / pos.qty if pos.qty > _EPS else ZERO
            continue

        # Sell
        cyc = open_cycles.get(symbol)
        if cyc is None:
            cyc = Cycle(symbol, t.closing_time, t.closing_time)
            open_cycles[symbol] = cyc
        sell_qty = min(qty, pos.qty) if pos.qty > _EPS else qty
        cost_removed = pos.avg * sell_qty
        pnl = sell_qty * price - cost_removed
        if pos.qty > _EPS and qty > pos.qty:
            pnl += (qty - pos.qty) * price
        sell_notional += notional
        cyc.trades += 1
        cyc.sell_qty += sell_qty
        cyc.sell_amount += notional
        cyc.realized += pnl
        cyc.cost += cost_removed
        cyc.ended_at = t.closing_time
        pos.qty -= sell_qty
        pos.cost = pos.avg * pos.qty
        if pos.qty <= _EPS:
            pos.qty = pos.cost = pos.avg = ZERO
            closed.append(cyc)
            open_cycles.pop(symbol, None)

    same_day_flips = []
    for (sym, day), group in day_trades.items():
        buy_qty = group["buy_qty"]
        sell_qty = group["sell_qty"]
        if buy_qty <= _EPS or sell_qty <= _EPS:
            continue
        matched_qty = min(buy_qty, sell_qty)
        buy_avg = group["buy_amount"] / buy_qty
        sell_avg = group["sell_amount"] / sell_qty
        same_day_flips.append(
            {
                "symbol": sym,
                "date": day.isoformat(),
                "matched_qty": matched_qty,
                "buy_avg": buy_avg,
                "sell_avg": sell_avg,
                "pnl": (sell_avg - buy_avg) * matched_qty,
            }
        )
    same_day_flips.sort(key=lambda r: (r["date"], r["symbol"]))

    open_positions = [
        {"symbol": sym, "qty": p.qty, "avg_price": p.avg, "cost": p.cost}
        for sym, p in positions.items()
        if p.qty > _EPS
    ]
    open_positions.sort(key=lambda r: r["cost"], reverse=True)

    return {
        "closed": closed,
        "open_positions": open_positions,
        "open_cycles": list(open_cycles.values()),
        "deposits": deposits,
        "buy_notional": buy_notional,
        "sell_notional": sell_notional,
        "commissions": commissions,
        "same_day_flips": same_day_flips,
    }


def _q(value: Decimal, places: str = "0.01") -> Decimal:
    return value.quantize(Decimal(places))


def build_review(db: Session, portfolio_id: int = 1) -> dict:
    trades = load_trades(db, portfolio_id)
    state = _replay(trades)

    closed: list[Cycle] = state["closed"]
    priced = [c for c in closed if c.cost > ZERO]
    wins = [c for c in priced if c.realized > ZERO]
    losses = [c for c in priced if c.realized <= ZERO]

    t_trades = state["same_day_flips"]
    t_wins = [t for t in t_trades if t["pnl"] > ZERO]
    t_losses = [t for t in t_trades if t["pnl"] <= ZERO]
    t_pnl = sum((t["pnl"] for t in t_trades), ZERO)

    realized = sum((c.realized for c in priced), ZERO)
    win_sum = sum((c.realized for c in wins), ZERO)
    loss_sum = sum((c.realized for c in losses), ZERO)
    avg_win = win_sum / len(wins) if wins else ZERO
    avg_loss = abs(loss_sum / len(losses)) if losses else ZERO

    deposits = state["deposits"]
    buy_notional = state["buy_notional"]

    # ---- per symbol -------------------------------------------------------
    by_symbol: dict[str, dict] = defaultdict(
        lambda: {"realized": ZERO, "cost": ZERO, "cycles": 0, "days": 0,
                 "addon_below": 0, "trades": 0}
    )
    for c in priced:
        b = by_symbol[c.symbol]
        b["realized"] += c.realized
        b["cost"] += c.cost
        b["cycles"] += 1
        b["days"] += c.holding_days
        b["addon_below"] += c.addon_below_avg
    for t in trades:
        sym = (t.symbol or "").strip().upper()
        if sym and t.side in ("Buy", "Sell"):
            by_symbol[sym]["trades"] += 1

    symbols = []
    for sym, b in by_symbol.items():
        symbols.append(
            {
                "symbol": sym,
                "realized": _q(b["realized"]),
                "cost": _q(b["cost"]),
                "return_pct": _q(b["realized"] / b["cost"] * Decimal("100"))
                if b["cost"] > ZERO else ZERO,
                "cycles": b["cycles"],
                "trades": b["trades"],
                "avg_holding_days": _q(Decimal(b["days"]) / Decimal(b["cycles"]))
                if b["cycles"] else ZERO,
                "addon_below_avg": b["addon_below"],
            }
        )
    symbols.sort(key=lambda r: r["realized"])

    # ---- monthly ----------------------------------------------------------
    monthly: dict[str, dict] = defaultdict(
        lambda: {"trades": 0, "realized": ZERO, "wins": 0, "losses": 0}
    )
    for t in trades:
        sym = (t.symbol or "").strip().upper()
        if sym and t.side in ("Buy", "Sell"):
            monthly[t.closing_time.strftime("%Y-%m")]["trades"] += 1
    for c in priced:
        m = monthly[c.ended_at.strftime("%Y-%m")]
        m["realized"] += c.realized
        if c.realized > ZERO:
            m["wins"] += 1
        else:
            m["losses"] += 1

    monthly_items = [
        {
            "month": key,
            "trades": val["trades"],
            "realized": _q(val["realized"]),
            "wins": val["wins"],
            "losses": val["losses"],
        }
        for key, val in sorted(monthly.items())
    ]

    # ---- cycle level ------------------------------------------------------
    cycles = [
        {
            "symbol": c.symbol,
            "started_at": c.started_at,
            "ended_at": c.ended_at,
            "holding_days": c.holding_days,
            "realized": _q(c.realized),
            "cost": _q(c.cost),
            "return_pct": _q(c.return_pct),
            "trades": c.trades,
            "addon_below_avg": c.addon_below_avg,
        }
        for c in priced
    ]
    cycles.sort(key=lambda r: r["realized"])

    win_days = [c.holding_days for c in wins]
    loss_days = [c.holding_days for c in losses]

    return {
        "meta": {
            "trades": len([t for t in trades if t.side in ("Buy", "Sell")]),
            "symbols": len({(t.symbol or "").upper() for t in trades if t.symbol}),
            "first_trade_at": trades[0].closing_time if trades else None,
            "last_trade_at": trades[-1].closing_time if trades else None,
        },
        "capital": {
            "deposits": _q(deposits),
            "buy_notional": _q(buy_notional),
            "sell_notional": _q(state["sell_notional"]),
            "turnover_x": _q(buy_notional / deposits) if deposits > ZERO else ZERO,
            "commissions": _q(state["commissions"]),
            "realized": _q(realized),
            "realized_pct": _q(realized / deposits * Decimal("100"))
            if deposits > ZERO else ZERO,
        },
        "behavior": {
            "closed_cycles": len(priced),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": _q(Decimal(len(wins)) / Decimal(len(priced)) * Decimal("100"))
            if priced else ZERO,
            "avg_win": _q(avg_win),
            "avg_loss": _q(avg_loss),
            "payoff": _q(avg_win / avg_loss) if avg_loss > ZERO else ZERO,
            "profit_factor": _q(win_sum / abs(loss_sum)) if loss_sum < ZERO else ZERO,
            "avg_win_days": _q(Decimal(sum(win_days)) / Decimal(len(win_days)))
            if win_days else ZERO,
            "avg_loss_days": _q(Decimal(sum(loss_days)) / Decimal(len(loss_days)))
            if loss_days else ZERO,
            "addon_below_avg": sum(c.addon_below_avg for c in priced),
            "same_day_flips": len(state["same_day_flips"]),
            "t_trades": len(t_trades),
            "t_wins": len(t_wins),
            "t_losses": len(t_losses),
            "t_win_rate": _q(Decimal(len(t_wins)) / Decimal(len(t_trades)) * Decimal("100"))
            if t_trades else ZERO,
            "t_pnl": _q(t_pnl),
        },
        "symbols": symbols,
        "monthly": monthly_items,
        "cycles": cycles,
        "open_positions": [
            {
                "symbol": p["symbol"],
                "qty": _q(p["qty"], "0.0001"),
                "avg_price": _q(p["avg_price"], "0.0001"),
                "cost": _q(p["cost"]),
                "cycle_started_at": next(
                    (
                        c.started_at
                        for c in state["open_cycles"]
                        if c.symbol == p["symbol"]
                    ),
                    None,
                ),
            }
            for p in state["open_positions"]
        ],
    }


logger = logging.getLogger(__name__)


def _decimal_or_none(value) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        result = Decimal(str(value))
    except Exception:
        return None
    return result if result > ZERO else None


def _weekday_sessions_since(start: date, end: date) -> int:
    if end <= start:
        return 0
    return sum(
        1
        for offset in range(1, (end - start).days + 1)
        if (start + timedelta(days=offset)).weekday() < 5
    )


def _sessions_since_exit(
    exit_day: date,
    today: date,
    closes: dict[str, Decimal],
) -> tuple[int, bool]:
    close_days = []
    for day_text in closes:
        try:
            close_days.append(date.fromisoformat(day_text))
        except (TypeError, ValueError):
            continue
    if close_days:
        return sum(1 for day in close_days if exit_day < day <= today), False
    return _weekday_sessions_since(exit_day, today), True


def _technical_snapshot(
    closes: dict[str, Decimal], current: Decimal | None
) -> dict:
    ordered_days = sorted(closes)
    values = [closes[d] for d in ordered_days if closes[d] > ZERO]
    if current is not None:
        if values:
            values[-1] = current
        else:
            values = [current]

    latest = values[-1] if values else current
    ma10 = sum(values[-10:], ZERO) / Decimal(len(values[-10:])) if len(values) >= 10 else None
    ma10_previous = (
        sum(values[-15:-5], ZERO) / Decimal(10) if len(values) >= 15 else None
    )
    previous_20_high = max(values[-21:-1]) if len(values) >= 21 else None
    stabilized = bool(
        latest is not None
        and ma10 is not None
        and latest >= ma10
        and (ma10_previous is None or ma10 >= ma10_previous)
    )
    breakout = bool(
        latest is not None
        and previous_20_high is not None
        and latest > previous_20_high
    )
    signal = "突破" if breakout else ("企稳" if stabilized else "")
    return {
        "latest_price": latest,
        "ma10": _q(ma10) if ma10 is not None else None,
        "previous_20_high": _q(previous_20_high) if previous_20_high is not None else None,
        "stabilized": stabilized,
        "breakout": breakout,
        "technical_signal": signal,
        "technical_ready": bool(signal),
        "sample_size": len(values),
    }


def _get_history_closes_cached(
    symbols: list[str], start: datetime
) -> dict[str, dict[str, Decimal]]:
    """Reuse recent daily closes across the holdings and strategy endpoints."""
    now = datetime.utcnow()
    ordered = sorted(set(symbols))
    key = (tuple(ordered), start.date().isoformat(), now.date().isoformat())
    cached = _history_cache.get(key)
    if cached and now - cached[0] < _HISTORY_CACHE_TTL:
        return cached[1]
    data = market.get_history_closes(ordered, start)
    _history_cache[key] = (now, data)
    return data


def _position_plan(
    avg_price: Decimal,
    current_price: Decimal | None,
    closes: list[Decimal],
    holding_days: int = 0,
) -> dict:
    hard_stop = avg_price * (Decimal("1") - HARD_STOP_PCT)
    highest_close = max(closes) if closes else None
    if current_price is not None and (
        highest_close is None or current_price > highest_close
    ):
        highest_close = current_price
    trailing_stop = (
        highest_close * (Decimal("1") - TRAILING_STOP_PCT)
        if highest_close is not None else None
    )
    # The trailing stop may tighten the exit, but it never falls below the -12% cost floor.
    stop_price = max(
        hard_stop,
        trailing_stop if trailing_stop is not None else hard_stop,
    )
    stop_source = (
        "建议止损" if trailing_stop is not None and trailing_stop > hard_stop else "必须止损"
    )

    if current_price is None:
        return {
            "hard_stop_price": _q(hard_stop),
            "trailing_stop_price": _q(trailing_stop) if trailing_stop is not None else None,
            "high_water_price": _q(highest_close) if highest_close is not None else None,
            "stop_price": _q(stop_price),
            "stop_source": stop_source,
            "action": "hold",
            "action_tone": "hold",
            "action_label": "行情不足，按止损位持有",
            "action_note": f"行情未更新；跌到 {stop_source}价即全部卖出。",
        }

    if current_price <= stop_price:
        # Mandatory stop has visual and execution priority when both levels are breached.
        mandatory_exit = current_price <= hard_stop
        exit_source = "必须止损" if mandatory_exit else "建议止损"
        return {
            "hard_stop_price": _q(hard_stop),
            "trailing_stop_price": _q(trailing_stop) if trailing_stop is not None else None,
            "high_water_price": _q(highest_close) if highest_close is not None else None,
            "stop_price": _q(stop_price),
            "stop_source": exit_source,
            "action": "exit",
            "action_tone": "cut" if mandatory_exit else "warning",
            "action_label": "必须止损 · 待清仓" if mandatory_exit else "建议止损 · 待清仓",
            "action_note": f"现价已跌破{exit_source}价，卖出全部剩余仓位；清仓后记录卖出，并冷却 5 个交易日。",
        }

    if holding_days >= 40:
        return {
            "hard_stop_price": _q(hard_stop),
            "trailing_stop_price": _q(trailing_stop) if trailing_stop is not None else None,
            "high_water_price": _q(highest_close) if highest_close is not None else None,
            "stop_price": _q(stop_price),
            "stop_source": stop_source,
            "action": "trim",
            "action_tone": "trim",
            "action_label": "到时间止损 · 复核逻辑",
            "action_note": "已持有 40 天以上；没有新增基本面或趋势理由就清仓，有明确理由才保留且不加仓。",
        }

    if current_price < avg_price:
        return {
            "hard_stop_price": _q(hard_stop),
            "trailing_stop_price": _q(trailing_stop) if trailing_stop is not None else None,
            "high_water_price": _q(highest_close) if highest_close is not None else None,
            "stop_price": _q(stop_price),
            "stop_source": stop_source,
            "action": "hold",
            "action_tone": "trim",
            "action_label": "持有，守止损",
            "action_note": "当前低于持仓成本，不加仓；跌到止损价全部卖出。",
        }

    return {
        "hard_stop_price": _q(hard_stop),
        "trailing_stop_price": _q(trailing_stop) if trailing_stop is not None else None,
        "high_water_price": _q(highest_close) if highest_close is not None else None,
        "stop_price": _q(stop_price),
        "stop_source": stop_source,
        "action": "hold",
        "action_tone": "hold",
        "action_label": "持有，抬高止损",
        "action_note": f"只在盈利时分批加仓；{stop_source}价随新高上移。",
    }


def _enrich_open_positions(
    positions: list[dict],
    quotes: dict[str, dict],
    histories: dict[str, dict[str, Decimal]],
) -> list[dict]:
    today = us_equity_session_date()
    enriched = []
    for row in positions:
        symbol = row["symbol"]
        quote = quotes.get(symbol, {})
        current = _decimal_or_none(quote.get("price"))
        closes = histories.get(symbol, {})
        cycle_started_at = row.get("cycle_started_at")
        cycle_day = trade_us_session_date(cycle_started_at) if cycle_started_at else None
        relevant = [
            value
            for day_text, value in sorted(closes.items())
            if cycle_day is None or day_text >= cycle_day.isoformat()
        ]
        if current is None and relevant:
            current = relevant[-1]
        if current is None:
            current = _decimal_or_none(row.get("avg_price"))
        avg_price = Decimal(row["avg_price"])
        qty = Decimal(row["qty"])
        market_value = current * qty if current is not None else None
        pnl = (current - avg_price) * qty if current is not None else None
        pnl_pct = (
            (current / avg_price - Decimal("1")) * Decimal("100")
            if current is not None and avg_price > ZERO else None
        )
        holding_days = max((today - cycle_day).days, 0) if cycle_day else 0
        plan = _position_plan(avg_price, current, relevant, holding_days)
        enriched.append(
            {
                **row,
                "holding_days": holding_days,
                "time_stop_due": holding_days >= 40,
                "current_price": _q(current) if current is not None else None,
                "market_value": _q(market_value) if market_value is not None else None,
                "pnl": _q(pnl) if pnl is not None else None,
                "pnl_pct": _q(pnl_pct) if pnl_pct is not None else None,
                **plan,
            }
        )
    return enriched


def enrich_holding_stop_levels(items: list[dict]) -> list[dict]:
    """Attach strategy stop prices to open ledger rows for the holdings table."""
    open_rows = [
        row for row in items
        if Decimal(row.get("quantity") or 0) > ZERO
    ]
    if not open_rows:
        return items

    starts = [
        trade_us_session_date(row["cycle_started_at"])
        for row in open_rows
        if row.get("cycle_started_at")
    ]
    start_day = min(starts) if starts else us_equity_session_date()
    history_start = datetime.combine(
        start_day - timedelta(days=10), datetime.min.time()
    )
    symbols = sorted({
        str(row["symbol"]).upper() for row in open_rows if row.get("symbol")
    })
    try:
        histories = _get_history_closes_cached(symbols, history_start)
    except Exception as exc:
        logger.warning("holdings stops history unavailable: %s", exc)
        histories = {symbol: {} for symbol in symbols}

    for row in open_rows:
        symbol = str(row.get("symbol") or "").upper()
        current = _decimal_or_none(row.get("last_price"))
        avg_price = _decimal_or_none(row.get("lot_avg_price"))
        if avg_price is None or avg_price <= ZERO:
            avg_price = _decimal_or_none(row.get("avg_price"))
        cycle_started_at = row.get("cycle_started_at")
        cycle_day = (
            trade_us_session_date(cycle_started_at)
            if cycle_started_at else None
        )
        closes = histories.get(symbol, {})
        relevant = [
            value
            for day_text, value in sorted(closes.items())
            if cycle_day is None or day_text >= cycle_day.isoformat()
        ]
        if current is None and relevant:
            current = relevant[-1]
        if avg_price is None or avg_price <= ZERO:
            row["suggested_stop_price"] = None
            row["mandatory_stop_price"] = None
            continue
        plan = _position_plan(avg_price, current, relevant)
        row["suggested_stop_price"] = plan["trailing_stop_price"]
        row["mandatory_stop_price"] = plan["hard_stop_price"]
    return items


def _latest_closed_cycles(
    cycles: list[dict], open_symbols: set[str], today: date
) -> list[dict]:
    cutoff = today - timedelta(days=REENTRY_LOOKBACK_DAYS)
    latest: dict[str, dict] = {}
    for cycle in cycles:
        symbol = cycle["symbol"]
        if symbol in open_symbols:
            continue
        ended_at = cycle["ended_at"]
        ended_day = trade_us_session_date(ended_at)
        if ended_day < cutoff:
            continue
        previous = latest.get(symbol)
        if previous is None or ended_at > previous["ended_at"]:
            latest[symbol] = cycle
    rows = list(latest.values())
    rows.sort(key=lambda row: row["ended_at"], reverse=True)
    return rows[:REENTRY_MAX_SYMBOLS]


def _build_reentry_candidates(
    cycles: list[dict],
    open_symbols: set[str],
    today: date,
    quotes: dict[str, dict],
    histories: dict[str, dict[str, Decimal]],
) -> list[dict]:
    candidates = []
    for cycle in _latest_closed_cycles(cycles, open_symbols, today):
        symbol = cycle["symbol"]
        exit_day = trade_us_session_date(cycle["ended_at"])
        closes = histories.get(symbol, {})
        sessions, estimated = _sessions_since_exit(exit_day, today, closes)
        cooldown_met = sessions >= REENTRY_COOLDOWN_SESSIONS
        current = _decimal_or_none(quotes.get(symbol, {}).get("price"))
        if current is None and closes:
            current = closes[sorted(closes)[-1]]
        technical = _technical_snapshot(closes, current)

        if not cooldown_met:
            status = "cooling"
            status_label = "冷却中"
            status_note = f"还差 {REENTRY_COOLDOWN_SESSIONS - sessions} 个交易日；冷却期内不买回。"
        elif technical["technical_ready"]:
            status = "ready"
            status_label = "技术条件满足"
            status_note = "还需写下新的买入理由；确认后只做 1/3 试仓。"
        elif technical["sample_size"] < 21:
            status = "unavailable"
            status_label = "行情不足"
            status_note = "历史行情不足，无法确认企稳或突破；暂不买回。"
        else:
            status = "watching"
            status_label = "等待信号"
            status_note = "尚未站上 10 日均线，也未突破前 20 个收盘高点。"

        candidates.append(
            {
                "symbol": symbol,
                "exited_at": cycle["ended_at"],
                "sessions_since_exit": sessions,
                "sessions_estimated": estimated,
                "cooldown_sessions": REENTRY_COOLDOWN_SESSIONS,
                "cooldown_met": cooldown_met,
                "cooldown_remaining": max(REENTRY_COOLDOWN_SESSIONS - sessions, 0),
                "current_price": _q(current) if current is not None else None,
                "ma10": technical["ma10"],
                "previous_20_high": technical["previous_20_high"],
                "stabilized": technical["stabilized"],
                "breakout": technical["breakout"],
                "technical_signal": technical["technical_signal"],
                "technical_ready": technical["technical_ready"],
                "prior_realized": cycle["realized"],
                "prior_return_pct": cycle["return_pct"],
                "status": status,
                "status_label": status_label,
                "status_note": status_note,
            }
        )

    rank = {"ready": 0, "cooling": 1, "watching": 2, "unavailable": 3}
    candidates.sort(
        key=lambda row: (rank.get(row["status"], 9), -row["exited_at"].timestamp())
    )
    return candidates


def build_strategy(db: Session, portfolio_id: int = 1) -> dict:
    """Review data plus live position actions and post-exit re-entry candidates."""
    data = build_review(db, portfolio_id)
    today = us_equity_session_date()
    open_symbols = {row["symbol"] for row in data["open_positions"]}
    candidate_cycles = _latest_closed_cycles(data["cycles"], open_symbols, today)
    symbols = sorted(open_symbols | {row["symbol"] for row in candidate_cycles})

    history_starts = [today - timedelta(days=REENTRY_LOOKBACK_DAYS + 30)]
    for row in data["open_positions"]:
        started_at = row.get("cycle_started_at")
        if started_at:
            history_starts.append(trade_us_session_date(started_at))
    history_start = min(history_starts) - timedelta(days=10)
    history_start_dt = datetime.combine(history_start, datetime.min.time())

    quotes: dict[str, dict] = {}
    histories: dict[str, dict[str, Decimal]] = {symbol: {} for symbol in symbols}
    if symbols:
        try:
            quotes = market.get_quotes(db, symbols)
        except Exception as exc:
            logger.warning("strategy quotes unavailable: %s", exc)
        try:
            histories = _get_history_closes_cached(symbols, history_start_dt)
        except Exception as exc:
            logger.warning("strategy history unavailable: %s", exc)

    enriched = _enrich_open_positions(data["open_positions"], quotes, histories)
    candidates = _build_reentry_candidates(
        data["cycles"],
        open_symbols,
        today,
        quotes,
        histories,
    )
    return {
        **data,
        "open_positions": enriched,
        "reentry_candidates": candidates,
        "reentry_screened": len(candidates),
    }

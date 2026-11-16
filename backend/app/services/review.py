"""Trading-behaviour review: replay the ledger and measure how the account is
actually traded, not just what it earned.

Everything here is derived from the same weighted-average cost basis that
`app.services.ledger` uses, so the numbers line up with the holdings page.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
import calendar
from datetime import date, datetime, timedelta
from decimal import Decimal


from sqlalchemy.orm import Session

from app.models.entities import PositionStop, Trade
from app.services import market
from app.services.ledger import load_trades, period_holdings, trade_us_session_date, us_equity_session_date
from app.services.signals import _alpha_strategies, _rsi, _stop_candle, is_etf, rule_catalog, wilder_atr

ZERO = Decimal("0")
_EPS = Decimal("0.0000001")
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
    exit_price: Decimal | None = None
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
            cyc.exit_price = price
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

    open_positions = []
    for sym, p in positions.items():
        if p.qty <= _EPS:
            continue
        cycle = open_cycles.get(sym)
        open_positions.append(
            {
                "symbol": sym,
                "qty": p.qty,
                "avg_price": p.avg,
                "cost": p.cost,
                # Holdings reports the current cycle's total P/L: the open lot's
                # unrealized P/L plus realized P/L from partial sells in this cycle.
                "realized_pnl": cycle.realized if cycle else ZERO,
            }
        )
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


def _month_ago(day: date) -> date:
    month = day.month - 1 or 12
    year = day.year - (1 if day.month == 1 else 0)
    last = calendar.monthrange(year, month)[1]
    return date(year, month, min(day.day, last))


def _resolve_range(start: date | None, end: date | None) -> tuple[date, date]:
    today = us_equity_session_date()
    end = end or today
    start = start or _month_ago(end)
    if end < start:
        start, end = end, start
    return start, end


def _month_slices(start: date, end: date) -> list[tuple[date, date]]:
    cursor = date(start.year, start.month, 1)
    slices: list[tuple[date, date]] = []
    while cursor <= end:
        last = calendar.monthrange(cursor.year, cursor.month)[1]
        month_end = date(cursor.year, cursor.month, last)
        slices.append((max(start, cursor), min(end, month_end)))
        cursor = month_end + timedelta(days=1)
    return slices


def build_review(
    db: Session,
    portfolio_id: int = 1,
    start: date | None = None,
    end: date | None = None,
) -> dict:
    start, end = _resolve_range(start, end)
    trades = load_trades(db, portfolio_id)
    state = _replay(trades)

    def in_range(day: date) -> bool:
        return start <= day <= end

    closed: list[Cycle] = state["closed"]
    priced = [
        c for c in closed
        if c.cost > ZERO and in_range(trade_us_session_date(c.ended_at))
    ]
    wins = [c for c in priced if c.realized > ZERO]
    losses = [c for c in priced if c.realized <= ZERO]

    t_trades = [
        t for t in state["same_day_flips"]
        if in_range(date.fromisoformat(t["date"]))
    ]
    t_wins = [t for t in t_trades if t["pnl"] > ZERO]
    t_losses = [t for t in t_trades if t["pnl"] <= ZERO]
    t_pnl = sum((t["pnl"] for t in t_trades), ZERO)

    win_sum = sum((c.realized for c in wins), ZERO)
    loss_sum = sum((c.realized for c in losses), ZERO)
    avg_win = win_sum / len(wins) if wins else ZERO
    avg_loss = abs(loss_sum / len(losses)) if losses else ZERO

    window_trades = [
        t for t in trades
        if t.side in ("Buy", "Sell")
        and (t.symbol or "").strip()
        and in_range(trade_us_session_date(t.closing_time))
    ]
    deposits = ZERO
    for t in trades:
        if t.side not in ("Deposit", "Withdrawal"):
            continue
        if trade_us_session_date(t.closing_time) > end:
            continue
        amount = Decimal(t.fill_price or 0)
        if amount == ZERO:
            amount = Decimal(t.qty or 0)
        deposits += amount if t.side == "Deposit" else -amount
    buy_notional = sum(
        (Decimal(t.qty or 0) * Decimal(t.fill_price or 0) for t in window_trades if t.side == "Buy"),
        ZERO,
    )
    sell_notional = sum(
        (Decimal(t.qty or 0) * Decimal(t.fill_price or 0) for t in window_trades if t.side == "Sell"),
        ZERO,
    )
    commissions = sum((Decimal(t.commission or 0) for t in window_trades), ZERO)

    # Mark-to-market for the window: names still held and names closed inside it.
    slices = _month_slices(start, end)
    month_books = [period_holdings(db, left, right, portfolio_id) for left, right in slices]
    whole = month_books[0] if len(slices) == 1 else period_holdings(db, start, end, portfolio_id)
    holding_pnl = sum(
        (item["day_pnl"] for item in whole["items"] if item["status"] == "持仓" and item["day_pnl"] is not None),
        ZERO,
    )
    closed_pnl = sum(
        (item["day_pnl"] for item in whole["items"] if item["status"] != "持仓" and item["day_pnl"] is not None),
        ZERO,
    )
    realized = Decimal(whole["total_pnl"] or 0)

    cycle_stats: dict[str, dict] = defaultdict(
        lambda: {"cycles": 0, "days": 0, "addon_below": 0}
    )
    for c in priced:
        b = cycle_stats[c.symbol]
        b["cycles"] += 1
        b["days"] += c.holding_days
        b["addon_below"] += c.addon_below_avg

    symbols = []
    for item in whole["items"]:
        pnl = item["day_pnl"]
        if pnl is None:
            continue
        stats = cycle_stats[item["symbol"]]
        symbols.append(
            {
                "symbol": item["symbol"],
                "realized": _q(pnl),
                "cost": ZERO,
                "return_pct": _q(item["day_pnl_percent"]) if item["day_pnl_percent"] is not None else ZERO,
                "cycles": stats["cycles"],
                "trades": item["trade_count"],
                "avg_holding_days": _q(Decimal(stats["days"]) / Decimal(stats["cycles"]))
                if stats["cycles"] else ZERO,
                "addon_below_avg": stats["addon_below"],
                "status": item["status"],
            }
        )
    symbols.sort(key=lambda r: r["realized"])

    monthly_items = []
    for (left, _right), book in zip(slices, month_books):
        key = left.strftime("%Y-%m")
        month_cycles = [c for c in priced if c.ended_at.strftime("%Y-%m") == key]
        monthly_items.append(
            {
                "month": key,
                "trades": sum(1 for t in window_trades if trade_us_session_date(t.closing_time).strftime("%Y-%m") == key),
                "realized": _q(Decimal(book["total_pnl"] or 0)),
                "wins": sum(1 for c in month_cycles if c.realized > ZERO),
                "losses": sum(1 for c in month_cycles if c.realized <= ZERO),
            }
        )

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
            "exit_price": _q(c.exit_price) if c.exit_price is not None else None,
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
            "trades": len(window_trades),
            "symbols": len({row["symbol"] for row in symbols}),
            "first_trade_at": window_trades[0].closing_time if window_trades else None,
            "last_trade_at": window_trades[-1].closing_time if window_trades else None,
            "start_date": start,
            "end_date": end,
        },
        "capital": {
            "deposits": _q(deposits),
            "buy_notional": _q(buy_notional),
            "sell_notional": _q(sell_notional),
            "turnover_x": _q(buy_notional / deposits) if deposits > ZERO else ZERO,
            "commissions": _q(commissions),
            "realized": _q(realized),
            "realized_pct": _q(realized / deposits * Decimal("100"))
            if deposits > ZERO else ZERO,
            "closed_pnl": _q(closed_pnl),
            "holding_pnl": _q(holding_pnl),
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
            "same_day_flips": len(t_trades),
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
                "realized_pnl": _q(p["realized_pnl"]),
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
    symbol: str, closes: dict[str, Decimal], current: Decimal | None
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
    previous_20_high = max(values[-21:-1]) if len(values) >= 21 else None

    # 优先使用日线 K 线跑完整的 4 条 ATR 策略判定
    hit_rules: list[str] = []
    try:
        from app.services.signals import _load_bars
        bars = _load_bars(symbol)
        if len(bars) >= 25:
            f_closes = [float(b["c"]) for b in bars]
            packs = _alpha_strategies(bars, _rsi(f_closes), _stop_candle(bars), is_etf(symbol))
            hit_rules = [p["name"] for p in packs if p.get("ready")]
    except Exception:
        hit_rules = []

    stabilized = bool(latest is not None and ma10 is not None and latest >= ma10)
    breakout = bool(latest is not None and previous_20_high is not None and latest > previous_20_high)
    if hit_rules:
        signal = " / ".join(hit_rules)
    else:
        signal = "突破" if breakout else ("企稳" if stabilized else "")

    return {
        "latest_price": latest,
        "ma10": _q(ma10) if ma10 is not None else None,
        "previous_20_high": _q(previous_20_high) if previous_20_high is not None else None,
        "stabilized": stabilized,
        "breakout": breakout or bool(hit_rules),
        "technical_signal": signal,
        "technical_ready": bool(hit_rules) or bool(signal),
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
    symbol: str | None = None,
    entry_day: date | None = None,
) -> dict:
    # 1. 加载 K 线：atr_dec 是最新的 Wilder ATR(14)，atr0 是建仓那天的 ATR（初始止损尺度）
    atr_dec: Decimal | None = None
    atr0: Decimal | None = None
    ma20_dec: Decimal | None = None
    if symbol:
        try:
            from app.services.signals import _load_bars
            bars = _load_bars(symbol)
            if len(bars) >= 14:
                atrs = wilder_atr(bars, 14)
                if atrs:
                    atr_dec = Decimal(str(round(atrs[-1], 4)))
                    if entry_day is not None:
                        cutoff = entry_day.isoformat()
                        upto = sum(1 for b in bars if str(b.get("d") or "")[:10] <= cutoff)
                        if upto >= 14:
                            atr0 = Decimal(str(round(atrs[upto - 1], 4)))
            if len(bars) >= 20:
                ma20_val = sum(float(b["c"]) for b in bars[-20:]) / 20.0
                ma20_dec = Decimal(str(round(ma20_val, 4)))
        except Exception:
            pass
    if atr0 is None or atr0 <= ZERO:
        # 建仓日的 K 线不够时，退回最新 ATR。
        atr0 = atr_dec

    highest_close = max(closes) if closes else None
    if current_price is not None and (highest_close is None or current_price > highest_close):
        highest_close = current_price

    if atr0 is None or atr0 <= ZERO or atr_dec is None or atr_dec <= ZERO:
        # 没有 ATR 就没有止损价，不再退回固定百分比。
        return {
            "hard_stop_price": None,
            "trailing_stop_price": None,
            "high_water_price": _q(highest_close) if highest_close is not None else None,
            "stop_price": None,
            "stop_source": "ATR 不足",
            "action": "hold",
            "action_tone": "hold",
            "action_label": "日线不足 · 算不出 ATR 止损",
            "action_note": "这只的日线不够 14 根，算不出 ATR 止损价；补齐日线前请手动设止损。",
        }

    # 2. 必须止损 (1R)：成本 − 2 倍建仓日 ATR
    risk_1r = Decimal("2.0") * atr0
    hard_stop = avg_price - risk_1r

    # 3. 建议止损：浮盈到过 2.5 倍建仓日 ATR 后，最高收盘 − 2.2 倍当日 ATR（ATR 吊灯）
    trailing_stop: Decimal | None = None
    if highest_close is not None and (highest_close - avg_price) >= Decimal("2.5") * atr0:
        trailing_stop = highest_close - Decimal("2.2") * atr_dec

    if trailing_stop is not None and trailing_stop > hard_stop:
        stop_price = trailing_stop
        stop_source = "建议止损(ATR吊灯)"
    else:
        stop_price = hard_stop
        stop_source = "必须止损(初始1R)"

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

    # 4. 破位止损判定（硬止损或已激活的吊灯止盈，或持满3日跌破 MA20 - 0.3*ATR）
    ma20_broken = (
        holding_days >= 3
        and ma20_dec is not None
        and current_price < (ma20_dec - Decimal("0.3") * atr_dec)
    )
    if current_price <= stop_price or ma20_broken:
        mandatory_exit = current_price <= hard_stop
        exit_source = "必须止损" if mandatory_exit else ("MA20缓冲带破位" if ma20_broken else "ATR吊灯止盈")
        return {
            "hard_stop_price": _q(hard_stop),
            "trailing_stop_price": _q(trailing_stop) if trailing_stop is not None else None,
            "high_water_price": _q(highest_close) if highest_close is not None else None,
            "stop_price": _q(stop_price),
            "stop_source": exit_source,
            "action": "exit",
            "action_tone": "cut" if mandatory_exit else "warning",
            "action_label": "必须止损 · 待清仓" if mandatory_exit else f"{exit_source} · 待离场",
            "action_note": f"现价已触发{exit_source}（{_q(stop_price)}），按纪律卖出并强制冷却 5 个交易日。",
        }

    # 5. +1.5R 与 +2.5R 分批止盈提示
    gain = current_price - avg_price
    if risk_1r > ZERO and gain >= Decimal("2.5") * risk_1r:
        return {
            "hard_stop_price": _q(hard_stop),
            "trailing_stop_price": _q(trailing_stop) if trailing_stop is not None else None,
            "high_water_price": _q(highest_close) if highest_close is not None else None,
            "stop_price": _q(stop_price),
            "stop_source": stop_source,
            "action": "trim",
            "action_tone": "trim",
            "action_label": "已达 +2.5R · 减仓 1/3",
            "action_note": f"浮盈已超 2.5R！按纪律兑现第二个 1/3 仓位，尾仓以 {_q(stop_price)} 为吊灯止盈线让利润奔跑。",
        }
    if risk_1r > ZERO and gain >= Decimal("1.5") * risk_1r:
        return {
            "hard_stop_price": _q(hard_stop),
            "trailing_stop_price": _q(trailing_stop) if trailing_stop is not None else None,
            "high_water_price": _q(highest_close) if highest_close is not None else None,
            "stop_price": _q(stop_price),
            "stop_source": stop_source,
            "action": "trim",
            "action_tone": "hold",
            "action_label": "已达 +1.5R · 减仓 1/3",
            "action_note": f"浮盈已达 1.5R（+{_q(gain)}），可兑现首个 1/3 仓位锁定胜局，并将底仓止损上移至成本价 {_q(avg_price)}。",
        }

    # 6. 时间证伪止损（15天未达 1.5*ATR 或 40天全局复核）
    if holding_days >= 15 and gain < Decimal("1.5") * atr0:
        return {
            "hard_stop_price": _q(hard_stop),
            "trailing_stop_price": _q(trailing_stop) if trailing_stop is not None else None,
            "high_water_price": _q(highest_close) if highest_close is not None else None,
            "stop_price": _q(stop_price),
            "stop_source": stop_source,
            "action": "trim",
            "action_tone": "trim",
            "action_label": f"持满 {holding_days} 天 · 动能不足",
            "action_note": f"已持有 {holding_days} 天但浮盈仍未达 1.5 倍 ATR（僵尸仓），建议时间止损换仓。",
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
            "action_label": "满 40 天 · 复核四条规则",
            "action_note": "已持有 40 天以上；若四条买入规则均不成立则清仓。",
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
            "action_label": "水下持有 · 严禁补仓",
            "action_note": f"当前低于持仓成本 {_q(avg_price)}，严禁水下摊平！跌破 {_q(stop_price)} 无条件止损。",
        }

    return {
        "hard_stop_price": _q(hard_stop),
        "trailing_stop_price": _q(trailing_stop) if trailing_stop is not None else None,
        "high_water_price": _q(highest_close) if highest_close is not None else None,
        "stop_price": _q(stop_price),
        "stop_source": stop_source,
        "action": "hold",
        "action_tone": "hold",
        "action_label": "盈利持有 · 可分批加仓",
        "action_note": f"现价高于成本，收盘比上一批买价再高 0.25 倍 ATR 可加下一批（不超过单只上限）；防守线 {_q(stop_price)}。",
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
        realized_pnl = Decimal(row.get("realized_pnl") or 0)
        total_pnl = pnl + realized_pnl if pnl is not None else None
        # Match the holdings page: current-cycle P/L is measured against the
        # break-even cost (open-lot cost less realized P/L from this cycle).
        total_cost = Decimal(row["cost"])
        pnl_base = total_cost - realized_pnl
        total_pnl_pct = (
            (total_pnl / pnl_base) * Decimal("100")
            if total_pnl is not None and pnl_base > ZERO else None
        )
        holding_days = max((today - cycle_day).days, 0) if cycle_day else 0
        plan = _position_plan(avg_price, current, relevant, holding_days, symbol=symbol, entry_day=cycle_day)
        enriched.append(
            {
                **row,
                "holding_days": holding_days,
                "time_stop_due": holding_days >= 40,
                "current_price": _q(current) if current is not None else None,
                "market_value": _q(market_value) if market_value is not None else None,
                "pnl": _q(pnl) if pnl is not None else None,
                "pnl_pct": _q(pnl_pct) if pnl_pct is not None else None,
                "realized_pnl": _q(realized_pnl),
                "total_pnl": _q(total_pnl) if total_pnl is not None else None,
                "total_pnl_percent": _q(total_pnl_pct) if total_pnl_pct is not None else None,
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
        plan = _position_plan(avg_price, current, relevant, symbol=symbol, entry_day=cycle_day)
        row["suggested_stop_price"] = plan["trailing_stop_price"]
        row["mandatory_stop_price"] = plan["hard_stop_price"]
    return items


def _latest_closed_cycles(
    cycles: list[dict],
    open_symbols: set[str],
    today: date,
    cutoff: date | None = None,
) -> list[dict]:
    if cutoff is None:
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
    cutoff: date | None = None,
) -> list[dict]:
    candidates = []
    for cycle in _latest_closed_cycles(cycles, open_symbols, today, cutoff):
        symbol = cycle["symbol"]
        exit_day = trade_us_session_date(cycle["ended_at"])
        closes = histories.get(symbol, {})
        sessions, estimated = _sessions_since_exit(exit_day, today, closes)
        cooldown_met = sessions >= REENTRY_COOLDOWN_SESSIONS
        current = _decimal_or_none(quotes.get(symbol, {}).get("price"))
        if current is None and closes:
            current = closes[sorted(closes)[-1]]
        exit_price = cycle.get("exit_price")
        change_since_exit_pct = None
        if current is not None and exit_price is not None and exit_price > ZERO:
            change_since_exit_pct = _q((current - exit_price) / exit_price * Decimal("100"))
        technical = _technical_snapshot(symbol, closes, current)

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
                "exit_price": _q(exit_price) if exit_price is not None else None,
                "change_since_exit_pct": change_since_exit_pct,
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


# Unlevered broad index products. Single names and leveraged funds stay in the satellite sleeve.
_CORE_TICKERS = {
    "SPY", "VOO", "IVV", "SPLG", "QQQ", "QQQM", "DIA", "IWM", "VTI", "ITOT",
    "SCHB", "SCHX", "VT", "VEA", "VWO",
    "510050", "510300", "510310", "510330", "510500", "159919", "159922",
    "159915", "159949", "588000", "588080",
    "2800", "3033", "3067",
}
# Core and satellite numbers are ceilings; the cash number is a floor.
_SLEEVE_TARGET = (("core", "核心仓", 50), ("satellite", "卫星仓", 30), ("cash", "现金", 20))
# Each sleeve's ceiling is split evenly across the names actually in it.
SLEEVE_PCT = {"core": 50, "satellite": 30}
SLEEVE_MAX_NAMES = {"core": 3, "satellite": 5}
SLEEVE_LABEL = {"core": "核心", "satellite": "卫星"}


def sleeve_choices(db: Session, portfolio_id: int = 1) -> dict[str, str]:
    """Sleeves the user picked by hand, keyed by symbol."""
    rows = (
        db.query(PositionStop.symbol, PositionStop.sleeve)
        .filter(PositionStop.portfolio_id == portfolio_id, PositionStop.sleeve.isnot(None))
        .all()
    )
    return {str(symbol).upper(): sleeve for symbol, sleeve in rows if sleeve in SLEEVE_PCT}


def sleeve_of(symbol: str, chosen: dict[str, str] | None = None) -> str:
    picked = (chosen or {}).get((symbol or "").upper())
    if picked:
        return picked
    ticker = (symbol or "").split(":")[-1].upper()
    return "core" if ticker in _CORE_TICKERS else "satellite"


def _build_allocation(positions: list[dict], cash: Decimal, chosen: dict[str, str] | None = None) -> dict:
    cash_value = cash if cash > ZERO else ZERO
    buckets = {"core": ZERO, "satellite": ZERO}
    counts = {"core": 0, "satellite": 0}
    names = []
    for row in positions:
        value = Decimal(row.get("market_value") or 0)
        if value <= ZERO:
            continue
        sleeve = sleeve_of(row["symbol"], chosen)
        buckets[sleeve] += value
        counts[sleeve] += 1
        names.append(
            {
                "symbol": row["symbol"],
                "ticker": (row["symbol"] or "").split(":")[-1],
                "sleeve": SLEEVE_LABEL[sleeve],
                "sleeve_key": sleeve,
                "market_value": value,
            }
        )
    holdings_value = buckets["core"] + buckets["satellite"]
    equity = holdings_value + cash_value
    # 单只上限 = 该类型的总额 ÷ 该类型现在的只数。还没有持仓时按 1 只算。
    single_cap = equity * Decimal(SLEEVE_PCT["satellite"]) / Decimal("100") / Decimal(max(counts["satellite"], 1))
    core_single_cap = equity * Decimal(SLEEVE_PCT["core"]) / Decimal("100") / Decimal(max(counts["core"], 1))
    current = {
        "core": buckets["core"],
        "satellite": buckets["satellite"],
        "cash": cash_value,
    }
    sleeves = []
    for key, label, target_pct in _SLEEVE_TARGET:
        target_amount = equity * Decimal(target_pct) / Decimal("100")
        amount = current[key]
        sleeves.append(
            {
                "key": key,
                "label": label,
                "target_pct": target_pct,
                "target_amount": _q(target_amount),
                "current_pct": float(_q(amount / equity * Decimal("100"), "0.1")) if equity > ZERO else 0,
                "current_amount": _q(amount),
                # 核心、卫星：离上限还有多少（负数是超了）。现金：比下限多多少（负数是不够）。
                "gap": _q(amount - target_amount) if key == "cash" else _q(target_amount - amount),
            }
        )
    priced = []
    for row in names:
        weight = row["market_value"] / equity * Decimal("100") if equity > ZERO else ZERO
        cap = core_single_cap if row["sleeve"] == "核心" else single_cap
        priced.append(
            {
                **row,
                "market_value": _q(row["market_value"]),
                "weight": float(_q(weight, "0.1")),
                "room": _q(cap - row["market_value"]),
            }
        )
    priced.sort(key=lambda row: row["market_value"], reverse=True)
    over = [row["ticker"] for row in priced if row["room"] < ZERO]
    core_pct = sleeves[0]["current_pct"]
    satellite_pct = sleeves[1]["current_pct"]
    cash_pct = sleeves[2]["current_pct"]
    if equity <= ZERO:
        note = "当前账户没有持仓，也没有现金。"
    else:
        note = (
            f"金额按持仓市值加现金计算。"
            f"核心仓现在 {core_pct:.0f}%，卫星仓 {satellite_pct:.0f}%，现金 {cash_pct:.0f}%。"
        )
        note += f"核心 {counts['core']} 只，卫星 {counts['satellite']} 只，各自平分该类型的总额。"
        if over:
            note += f"{'、'.join(over)} 超过单只上限。"
        crowded = [
            f"{SLEEVE_LABEL[key]}有 {counts[key]} 只，超过 {SLEEVE_MAX_NAMES[key]} 只"
            for key in ("core", "satellite")
            if counts[key] > SLEEVE_MAX_NAMES[key]
        ]
        if crowded:
            note += "；".join(crowded) + "。"
    return {
        "core_count": counts["core"],
        "satellite_count": counts["satellite"],
        "core_max": SLEEVE_MAX_NAMES["core"],
        "satellite_max": SLEEVE_MAX_NAMES["satellite"],
        "equity": _q(equity),
        "cash": _q(cash_value),
        "holdings_value": _q(holdings_value),
        "single_cap": _q(single_cap),
        "core_single_cap": _q(core_single_cap),
        "tranche": _q(single_cap / Decimal("3")),
        "sleeves": sleeves,
        "names": priced,
        "note": note,
    }


def build_strategy(
    db: Session,
    portfolio_id: int = 1,
    start: date | None = None,
    end: date | None = None,
) -> dict:
    """Review data plus live position actions and post-exit re-entry candidates."""
    data = build_review(db, portfolio_id, start, end)
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
        cutoff=data["meta"]["start_date"],
    )
    try:
        from app.services.ledger import compute_summary

        cash = Decimal(compute_summary(db, portfolio_id)["cash"])
    except Exception as exc:
        logger.warning("strategy cash unavailable: %s", exc)
        cash = ZERO
    return {
        **data,
        "open_positions": enriched,
        "reentry_candidates": candidates,
        "reentry_screened": len(candidates),
        "allocation": _build_allocation(enriched, cash, sleeve_choices(db, portfolio_id)),
        "rules": rule_catalog(),
    }

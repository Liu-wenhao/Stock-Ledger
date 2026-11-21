"""Replay the price-page buy rules with AKQuant. Does not change the ledger."""

from __future__ import annotations

import bisect
import logging
import math
import time
from collections import defaultdict
from datetime import date, datetime, timezone

import pandas as pd
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.entities import BreadthDay, SymbolMeta
from app.services.market_bars import data_version
from app.services.market_scope import market_of
from app.services.position_scope import position_symbols
from app.services.price_history import load_history_bars, uses_market_store
from app.services.signals import (
    ALPHA_RULE_CATALOG,
    _alpha_strategies,
    _measure,
    _rsi,
    _sell_strategies,
    _stop_candle,
    is_etf,
    wilder_atr,
)

logger = logging.getLogger(__name__)
logging.getLogger("akquant").setLevel(logging.WARNING)

_CACHE_SECONDS = 600
_cache: dict[str, tuple[float, dict]] = {}
# Whole book. Core names share 50% of it, satellite names share 30%, the rest stays in cash.
CASH = 30_000.0
# The A-share book is in yuan and trades in lots of 100, so it needs a larger total.
_MARKET_CASH = {"cn": 300_000.0}
_EVEN_SHARE = 0.8

# After any full exit, wait five sessions before the next buy on that name.
_LOSS_COOLDOWN = 5
_TREND_RULES = {"ma_crossover_v1", "volatility_compression_breakout_v1"}
# A reversal buy may lose at most this share of the whole book if its ATR stop is hit.
_REVERSAL_RISK = 0.01

RULE_NAME = {
    "ma_crossover_v1": "均线交叉",
    "rsi_reversion_v1": "RSI超卖",
    "volatility_compression_breakout_v1": "收缩突破",
    "mean_reversion_oversold_v1": "超跌反弹",
}

# 同日共振时谁说了算：趋势在前，防止被反转规则的「触及 MA20」提前清仓。
_PRIORITY = (
    "volatility_compression_breakout_v1",
    "ma_crossover_v1",
    "rsi_reversion_v1",
    "mean_reversion_oversold_v1",
)
# US fills move against the order by this share of the price. The A-share broker
# profile already carries its own slippage.
_US_SLIPPAGE = 0.0002
# A-share commission on both sides, with no minimum per order.
_CN_COMMISSION = 0.0001
# A-share stamp duty, charged on sells only.
_CN_STAMP_TAX = 0.0005
# The index the US book is compared with. Its local daily bars have no gaps.
_US_INDEX = "SPY"


def _stop_multiple(rule: str) -> float:
    """ATR stop multiple. ALPHA_RULE_CATALOG is the only place this number is written."""
    for item in ALPHA_RULE_CATALOG:
        if item["id"] == rule:
            return float(item["stop_atr"])
    return 2.0


def _fresh_sells(rows: list[dict], before: set[str] | None) -> tuple[list[str], set[str]]:
    """Names of the sell rules that turned ready on the last bar, and the ids ready now.
    `before` holds the ids ready on the previous bar; None means work it out from the bars."""
    if before is None:
        before = {pack["id"] for pack in _sell_strategies(rows[:-1]) if pack["ready"]} if len(rows) > 25 else set()
    ready = [pack for pack in _sell_strategies(rows) if pack["ready"]]
    return [pack["name"] for pack in ready if pack["id"] not in before], {pack["id"] for pack in ready}


def _spendable(cash: float) -> float:
    """Cash left for shares after setting aside fees: 0.2% of the cash, at least 5."""
    return max(0.0, cash - max(cash * 0.002, 5.0))


def _series(history, current: float) -> list[float]:
    values = [float(item) for item in history]
    if not values or abs(values[-1] - current) > 1e-8:
        values.append(float(current))
    return values


def _build_strategy_class():
    from akquant import Strategy
    from akquant.backtest.fill_mode import CurrentClose

    class BookStrategy(Strategy):
        # 反转规则算单笔风险用的总本金；没给时退回这只自己的现金。
        risk_base = 0.0

        def __init__(self):
            super().__init__()
            self.pending: dict[str, str] = {}
            self.opening: dict[str, dict] = {}
            self.state: dict[str, dict] = {}
            self.cooldown: dict[str, int] = {}
            self.ticks: dict[str, int] = {}

        def _bars(self, bar):
            symbol = bar.symbol
            closes = _series(self.get_history(140, symbol, "close"), bar.close)
            opens = _series(self.get_history(140, symbol, "open"), bar.open)
            highs = _series(self.get_history(140, symbol, "high"), bar.high)
            lows = _series(self.get_history(140, symbol, "low"), bar.low)
            volumes = _series(self.get_history(140, symbol, "volume"), bar.volume)
            size = min(len(closes), len(opens), len(highs), len(lows), len(volumes))
            rows = []
            for i in range(size):
                opened = opens[-size + i]
                high = highs[-size + i]
                low = lows[-size + i]
                close = closes[-size + i]
                volume = volumes[-size + i]
                if not all(math.isfinite(value) for value in (opened, high, low, close, volume)):
                    continue
                rows.append({"o": opened, "h": high, "l": low, "c": close, "v": volume})
            return rows, [row["c"] for row in rows]

        def _primary(self, rules: list[str]) -> str:
            for rule in _PRIORITY:
                if rule in rules:
                    return rule
            return rules[0] if rules else "ma_crossover_v1"

        def _risk(self, rule: str, atr0: float) -> float:
            # 1R = 初始 ATR 止损距离（目录里的止损倍数 × 买入当日 ATR）
            return _stop_multiple(rule) * atr0

        def _broken(self, rule: str, init_entry: float, close: float, held: int, ma20: float, atr0: float, atr_t: float) -> str:
            # 初始 ATR 止损锚定首笔入场价 init_entry，防止盈利加仓摊高均价后被正常回踩误杀
            multiple = _stop_multiple(rule)
            if close <= init_entry - multiple * atr0:
                return f"跌破{multiple:.1f}倍ATR止损"
            if rule == "ma_crossover_v1" and held >= 3 and close < ma20 - 0.3 * atr_t:
                return "跌破MA20下方0.3倍ATR"
            if rule == "volatility_compression_breakout_v1" and close < ma20 - 0.3 * atr_t:
                return "跌破MA20下方0.3倍ATR"
            return ""

        def _time_stop(self, rule: str, init_entry: float, close: float, held: int, atr0: float) -> str:
            # 四条策略专属时间证伪止损（趋势策略浮盈达 1.5*ATR 自动豁免）
            gain_atr = (close - init_entry) / atr0 if atr0 > 0 else 0.0
            if rule == "ma_crossover_v1" and held >= 15 and gain_atr < 1.5:
                return "满15日浮盈未达1.5倍ATR"
            if rule == "volatility_compression_breakout_v1" and held >= 20 and gain_atr < 1.5:
                return "满20日浮盈未达1.5倍ATR"
            if rule == "rsi_reversion_v1" and held >= 10:
                return "持有满10日离场"
            if rule == "mean_reversion_oversold_v1" and held >= 8:
                return "持有满8日离场"
            return ""

        def _trail(self, rule: str, init_entry: float, close: float, high: float, ma20: float, peak: float, atr0: float, atr_t: float) -> str:
            spanned = (peak - init_entry) / atr0 if atr0 > 0 else 0.0
            if rule == "ma_crossover_v1":
                if spanned >= 2.5 and close <= peak - 2.2 * atr_t:
                    return "ATR吊灯止盈(回落2.2倍ATR)"
            elif rule == "rsi_reversion_v1":
                if close >= init_entry + 3.0 * atr0:
                    return "达到3倍ATR目标止盈"
                if high >= ma20:
                    return "最高价触及MA20止盈"
            elif rule == "volatility_compression_breakout_v1":
                if spanned >= 2.0 and close <= peak - 2.0 * atr_t:
                    return "VCP吊灯止盈(回落2倍ATR)"
            elif rule == "mean_reversion_oversold_v1":
                if high >= ma20:
                    return "最高价触及MA20止盈"
                if spanned >= 1.5 and close <= peak - 1.2 * atr_t:
                    return "超跌反弹回落1.2倍ATR止盈"
            return ""

        def _book_flat(self, symbol: str, tag: str = "清仓") -> None:
            # close_position() drops the tag in this akquant build; sell() keeps it,
            # so the exit reason survives into trades_df.exit_tag and the chart.
            held = float(self.get_position(symbol) or 0)
            if held > 0:
                # 信号当天按收盘价卖出
                self.sell(symbol=symbol, quantity=int(held), tag=tag, fill_mode=CurrentClose())
            else:
                self.close_position(symbol)
            self.cooldown[symbol] = _LOSS_COOLDOWN

        def _trim(self, symbol: str, held: float, keep_fraction: float, tag: str = "分批止盈") -> None:
            lot = 100 if market_of(symbol) == "cn" else 1
            qty = int(held)
            keep = max(lot, int(qty * keep_fraction / lot) * lot)
            if keep >= qty:
                return
            self.sell(symbol=symbol, quantity=qty - keep, tag=tag, fill_mode=CurrentClose())

        def on_bar(self, bar):
            symbol = bar.symbol
            tick = self.ticks.get(symbol, 0) + 1
            self.ticks[symbol] = tick
            held = float(self.get_position(symbol) or 0)
            rows, closes = self._bars(bar)
            if held > 0:
                state = self.state.get(symbol)
                if state is None:
                    # 建仓单在信号当天收盘成交，这里是成交后的第一根 K 线。
                    atrs = wilder_atr(rows)
                    opened = self.opening.pop(symbol, {})
                    self.pending.pop(symbol, None)
                    first_px = float(opened.get("price") or bar.open or bar.close)
                    state = {
                        "rules": opened.get("rules") or self.pending.get(symbol, ""),
                        "init_entry": first_px,
                        "entry": first_px,
                        "last_buy_price": first_px,
                        "high": max(first_px, float(bar.high)),
                        "atr0": float(opened.get("atr0") or 0) or (atrs[-1] if atrs else 0.0),
                        "held_days": max(0, tick - int(opened.get("tick") or tick)),
                        "trend": bool(opened.get("trend")),
                        "slice": int(opened.get("slice") or 0),
                        "batches": int(opened.get("batches") or 1),
                        "last_add_day": 0,
                        "shares": float(held),
                        "add_fill_price": 0.0,
                        "sold_15": False,
                        "sold_25": False,
                        "closing": False,
                    }
                    self.state[symbol] = state
                else:
                    state["held_days"] = int(state.get("held_days") or 0) + 1
                    pending_add = float(state.get("add_qty") or 0)
                    if pending_add:
                        if held + 1e-6 >= float(state.get("shares") or 0) + pending_add * 0.5:
                            old = float(state.get("shares") or 0)
                            fill = float(state.get("add_fill_price") or 0) or float(bar.open or bar.close)
                            if old > 0:
                                state["entry"] = (float(state["entry"]) * old + fill * pending_add) / (old + pending_add)
                            state["last_buy_price"] = fill
                            state["shares"] = old + pending_add
                        state["add_qty"] = 0
                        state["add_fill_price"] = 0.0
                if state["closing"]:
                    return

                # 每根持仓 K 线都记下卖出规则的状态，「刚成立」才能和前一天比。
                fresh_sells, state["sell_ready"] = _fresh_sells(rows, state.get("sell_ready"))
                state["high"] = max(state["high"], float(bar.high))
                rules = [part for part in str(state.get("rules") or "").split(",") if part]
                primary = self._primary(rules)
                init_entry = float(state.get("init_entry") or state["entry"])
                entry = float(state["entry"])
                atr0 = float(state.get("atr0") or 0)
                close = float(bar.close)
                high = float(bar.high)
                ma20 = sum(closes[-20:]) / 20 if len(closes) >= 20 else close
                atr_t = wilder_atr(rows)[-1] if rows else 0.0
                held_days = int(state["held_days"])

                # 1. 破位止损 / 策略专属时间止损 / 40天全局失效清仓
                broken_reason = self._broken(primary, init_entry, close, held_days, ma20, atr0, atr_t) if atr0 > 0 else ""
                time_reason = self._time_stop(primary, init_entry, close, held_days, atr0) if atr0 > 0 else ""
                stale_reason = ""
                if held_days >= 40 and len(rows) >= 25:
                    packs = _alpha_strategies(rows, _rsi(closes), _stop_candle(rows), is_etf(symbol))
                    if not any(pack.get("ready") for pack in packs):
                        stale_reason = "满40日且四条规则均不成立"

                exit_tag = broken_reason or time_reason or stale_reason
                if exit_tag:
                    self._book_flat(symbol, tag=exit_tag)
                    state["closing"] = True
                    return

                # 2. 吊灯移动止盈 & 反转触及MA20止盈（解除原 runner 死锁，全天候生效）
                trail_reason = self._trail(primary, init_entry, close, high, ma20, state["high"], atr0, atr_t) if atr0 > 0 else ""
                if trail_reason:
                    self._book_flat(symbol, tag=trail_reason)
                    state["closing"] = True
                    return

                # 2b. 卖出规则：四条卖出规则任一条当天刚成立（前一天还不成立），整笔清仓
                if fresh_sells:
                    self._book_flat(symbol, tag="卖出规则：" + "、".join(fresh_sells))
                    state["closing"] = True
                    return

                # 3. 趋势策略专属：+1.5R 与 +2.5R 分批减仓，尾仓交给上面的 ATR 吊灯
                risk = self._risk(primary, atr0) if atr0 > 0 else 0.0
                qty = int(held)
                lot = 100 if market_of(symbol) == "cn" else 1
                if state["trend"] and risk > 0:
                    gain = close - init_entry
                    if qty >= lot * 3:
                        if gain >= 2.5 * risk and not state["sold_25"]:
                            self._trim(symbol, held, 1 / 3 if not state["sold_15"] else 0.5, tag="+2.5R减仓1/3")
                            state["sold_15"] = True
                            state["sold_25"] = True
                            return
                        if gain >= 1.5 * risk and not state["sold_15"]:
                            self._trim(symbol, held, 2 / 3, tag="+1.5R减仓1/3")
                            state["sold_15"] = True
                            return

                # 4. 趋势策略分批加仓：收盘高于成本且比上一批抬升至少 0.25*ATR0，当天按收盘价买入
                if state["trend"] and int(state["batches"]) < 3 and not state["sold_15"]:
                    last_add_day = int(state.get("last_add_day") or 0)
                    min_step = float(state.get("last_buy_price") or entry) + 0.25 * atr0
                    if close > entry and close >= min_step and held_days > last_add_day:
                        affordable = int(_spendable(float(self.cash)) / close / lot) * lot if close > 0 else 0
                        add = min(int(state["slice"]), affordable)
                        if add >= lot:
                            label = RULE_NAME.get(primary, "趋势加仓")
                            self.buy(symbol=symbol, quantity=add, tag=label, fill_mode=CurrentClose())
                            state["add_qty"] = add
                            state["add_fill_price"] = close
                            state["batches"] = int(state["batches"]) + 1
                            state["last_add_day"] = held_days
                return

            # ==================== 空仓扫描与首笔建仓 ====================
            # 上一根的买单没有变成持仓，清掉没成交的规则，避免后面误用。
            self.state.pop(symbol, None)
            self.pending.pop(symbol, None)
            self.opening.pop(symbol, None)
            waiting = self.cooldown.get(symbol, 0)
            if waiting:
                self.cooldown[symbol] = waiting - 1
                return
            if len(rows) < 25:
                return
            packs = _alpha_strategies(rows, _rsi(closes), _stop_candle(rows), is_etf(symbol))
            ready = [pack for pack in packs if pack.get("ready")]
            if not ready:
                return
            price = float(bar.close)
            lot = 100 if market_of(symbol) == "cn" else 1
            if price <= 0:
                return
            # 这份本金全部可用，只留出手续费
            plan = int(_spendable(float(self.cash)) / price / lot) * lot
            if plan < lot:
                return
            ids = [pack["id"] for pack in ready]
            primary = self._primary(ids)
            trend = primary in _TREND_RULES
            atrs = wilder_atr(rows)
            atr0 = atrs[-1] if atrs else 0.0
            quantity = plan
            slice_qty = plan
            if trend:
                slice_qty = int(plan / 3 / lot) * lot
                quantity = slice_qty if slice_qty >= lot else plan
                trend = quantity < plan
            else:
                # 反转规则一次买入：打到 ATR 止损时的亏损不超过总本金的 1%。
                stop_distance = _stop_multiple(primary) * atr0
                if stop_distance > 0:
                    base = float(self.risk_base or 0) or float(self.cash)
                    capped = int(base * _REVERSAL_RISK / stop_distance / lot) * lot
                    quantity = min(plan, capped)
                    if quantity < lot:
                        return  # 连一手都超过风险上限，这次不买
            names = "、".join(pack["name"] for pack in ready)
            self.opening[symbol] = {
                "rules": ",".join(ids),
                "trend": trend,
                "slice": quantity if trend else plan,
                "batches": 1,
                "price": price,
                "atr0": atr0,
                "tick": tick,
            }
            # 信号当天按收盘价买入
            self.buy(symbol=symbol, quantity=quantity, tag=names, fill_mode=CurrentClose())
            self.pending[symbol] = ",".join(ids)

    return BookStrategy

_Strategy = _build_strategy_class()

_advice_cache: dict[tuple, dict[str, list[dict]]] = {}


def advice_marks(symbol: str, bars: list[dict], since: str) -> dict[str, list[dict]]:
    """Suggested buys and sells by day for the trend chart.

    One position without share counts is walked from the first day on or after `since`:
    buy at the close of a day a rule is ready, then leave on the engine's own stop, time
    and take-profit checks, and wait out the cooldown. The walk always starts on the same
    day, so paging the chart to older bars never moves a mark.
    """
    rows = []
    for bar in bars:
        close = bar.get("c")
        if not close:
            continue
        close = float(close)
        rows.append(
            {
                "d": str(bar["d"])[:10],
                "o": float(bar.get("o") or close),
                "h": float(bar.get("h") or close),
                "l": float(bar.get("l") or close),
                "c": close,
                "v": float(bar.get("v") or 0),
            }
        )
    if not rows:
        return {}
    key = (symbol, since, len(rows), rows[-1]["d"], rows[-1]["c"])
    if key in _advice_cache:
        return _advice_cache[key]

    etf = is_etf(symbol)
    marks: dict[str, list[dict]] = defaultdict(list)
    held: dict | None = None
    waiting = 0
    for index, row in enumerate(rows):
        if row["d"] < since or index + 1 < 25:
            continue
        window = rows[max(0, index - 139) : index + 1]
        closes = [item["c"] for item in window]
        close = row["c"]
        atrs = wilder_atr(window)
        atr_t = atrs[-1] if atrs else 0.0
        if held:
            held["days"] += 1
            fresh_sells, held["sell_ready"] = _fresh_sells(window, held.get("sell_ready"))
            held["high"] = max(held["high"], row["h"])
            rule, entry, atr0, days = held["rule"], held["entry"], held["atr0"], held["days"]
            ma20 = sum(closes[-20:]) / 20 if len(closes) >= 20 else close
            reason = ""
            if atr0 > 0:
                reason = _Strategy._broken(None, rule, entry, close, days, ma20, atr0, atr_t) or _Strategy._time_stop(
                    None, rule, entry, close, days, atr0
                )
            if not reason and days >= 40:
                packs = _alpha_strategies(window, _rsi(closes), _stop_candle(window), etf)
                if not any(pack.get("ready") for pack in packs):
                    reason = "满40日且四条规则均不成立"
            if not reason and atr0 > 0:
                reason = _Strategy._trail(None, rule, entry, close, row["h"], ma20, held["high"], atr0, atr_t)
            if not reason and fresh_sells:
                reason = "卖出规则：" + "、".join(fresh_sells)
            if reason:
                marks[row["d"]].append({"side": "S", "rule": RULE_NAME.get(rule, ""), "reason": reason})
                held = None
                waiting = _LOSS_COOLDOWN
                continue
            # Same partial exits as the engine's trend rules; the rest stays on.
            risk = _stop_multiple(rule) * atr0
            if rule in _TREND_RULES and risk > 0:
                gain = close - entry
                trimmed = ""
                if gain >= 2.5 * risk and not held["sold_25"]:
                    trimmed = "+2.5R减仓1/3"
                    held["sold_15"] = held["sold_25"] = True
                elif gain >= 1.5 * risk and not held["sold_15"]:
                    trimmed = "+1.5R减仓1/3"
                    held["sold_15"] = True
                if trimmed:
                    marks[row["d"]].append({"side": "S", "rule": RULE_NAME.get(rule, ""), "reason": trimmed})
            continue
        if waiting:
            waiting -= 1
            continue
        packs = _alpha_strategies(window, _rsi(closes), _stop_candle(window), etf)
        ready = [pack for pack in packs if pack.get("ready")]
        if not ready:
            continue
        ids = [pack["id"] for pack in ready]
        rule = next((item for item in _PRIORITY if item in ids), ids[0])
        held = {
            "rule": rule,
            "entry": close,
            "atr0": atr_t,
            "days": 0,
            "high": close,
            "sold_15": False,
            "sold_25": False,
        }
        marks[row["d"]].append(
            {"side": "B", "rule": RULE_NAME.get(rule, ""), "reason": "、".join(pack["name"] for pack in ready)}
        )
    if len(_advice_cache) > 64:
        _advice_cache.clear()
    _advice_cache[key] = dict(marks)
    return _advice_cache[key]


def _symbols(db: Session, market: str) -> list[str]:
    return position_symbols(db, get_settings().default_portfolio_id, market)


def _frame(symbol: str, bars: list[dict]) -> pd.DataFrame:
    rows = []
    for bar in bars:
        close = bar.get("c")
        if not close:
            continue
        close = float(close)
        rows.append(
            {
                "date": bar["d"],
                "open": float(bar.get("o") or close),
                "high": float(bar.get("h") or close),
                "low": float(bar.get("l") or close),
                "close": close,
                "volume": float(bar.get("v") or 0),
                "symbol": symbol,
            }
        )
    return pd.DataFrame(rows)


def _in_window(frame: pd.DataFrame, start: date | None, end: date | None) -> pd.DataFrame:
    if start is None and end is None:
        return frame
    dates = frame["date"].astype(str).str.slice(0, 10)
    mask = pd.Series(True, index=frame.index)
    if start is not None:
        mask &= dates >= start.isoformat()
    if end is not None:
        mask &= dates <= end.isoformat()
    return frame.loc[mask]


def _uses_market_store(symbol: str) -> bool:
    return uses_market_store(symbol)


def _backtest_bars(db: Session, symbol: str, *, end: date | None = None) -> list[dict]:
    return load_history_bars(db, symbol, end=end)


def backtest_candles(
    db: Session,
    symbol: str,
    start: date | None = None,
    end: date | None = None,
    pad_before: int = 40,
    pad_after: int = 10,
) -> dict:
    """Daily bars around one backtest cycle, padded so the B/S marks have context."""
    key = str(symbol or "").strip().upper()
    meta = db.query(SymbolMeta).filter(SymbolMeta.symbol == key).first()
    payload = {"symbol": key, "name": meta.name if meta else None, "candles": []}
    bars = _backtest_bars(db, key)
    rows = [bar for bar in bars if bar.get("c")]
    dates = [str(bar["d"])[:10] for bar in rows]
    lo = bisect.bisect_left(dates, start.isoformat()) if start else 0
    hi = bisect.bisect_right(dates, end.isoformat()) if end else len(rows)
    if lo >= hi:
        return payload
    lo = max(0, lo - pad_before)
    hi = min(len(rows), hi + pad_after)
    candles = []
    for i in range(lo, hi):
        # Evaluate every rule on the history ending at this bar, so any date — not just
        # signal days — can show which hard/optional checks passed. Labels live in the
        # rule catalog; only the booleans travel, keeping the payload small.
        window = rows[max(0, i - 139) : i + 1]
        checks: list[dict] = []
        sells: list[dict] = []
        if len(window) >= 25:
            closes = [bar["c"] for bar in window]
            packs = _alpha_strategies(
                window, _rsi(closes), _stop_candle(window), is_etf(key), with_conditions=True
            )
            checks = [
                {
                    "ready": bool(pack.get("ready")),
                    "core": [bool(item["ok"]) for item in pack.get("core", [])],
                    "opt": [bool(item["ok"]) for item in pack.get("optional", [])],
                }
                for pack in packs
            ]
            # Sell rules with at least one core hit, labels included.
            sells = [
                {key: pack[key] for key in ("name", "ready", "need", "opt_hits", "core", "optional")}
                for pack in _sell_strategies(window)
                if any(item["ok"] for item in pack["core"])
            ]
        candles.append(
            {
                "d": dates[i],
                "o": float(rows[i].get("o") or rows[i]["c"]),
                "h": float(rows[i].get("h") or rows[i]["c"]),
                "l": float(rows[i].get("l") or rows[i]["c"]),
                "c": float(rows[i]["c"]),
                "v": float(rows[i].get("v") or 0),
                "checks": checks,
                "sells": sells,
            }
        )
    payload["candles"] = candles
    return payload


def engine_source() -> str:
    """The core engine as it actually runs: rule catalog, buy checks, and the book strategy."""
    import ast
    import inspect
    import textwrap

    from app.services import signals
    from app.services.signals import _alpha_strategies as alpha_checks

    module_src = inspect.getsource(signals)
    catalog_src = ""
    for node in ast.parse(module_src).body:
        targets = node.targets if isinstance(node, ast.Assign) else [getattr(node, "target", None)]
        if any(getattr(target, "id", "") == "ALPHA_RULE_CATALOG" for target in targets):
            catalog_src = ast.get_source_segment(module_src, node) or ""
            break

    trend = ", ".join(f'"{name}"' for name in sorted(_TREND_RULES))
    header = (
        "from app.services.market_scope import market_of\n"
        "from app.services.signals import _rsi, _stop_candle, is_etf, wilder_atr\n"
        "from akquant.backtest.fill_mode import CurrentClose\n"
        "\n"
        f"_LOSS_COOLDOWN = {_LOSS_COOLDOWN}          # 清仓后强制冷却的交易日数\n"
        f"_TREND_RULES = {{{trend}}}  # 分三批建仓的趋势规则\n"
        f"_REVERSAL_RISK = {_REVERSAL_RISK}       # 反转规则单笔止损亏损占总本金的上限\n"
        f"_US_SLIPPAGE = {_US_SLIPPAGE}      # 美股每笔成交按价格的这个比例向不利方向滑点\n"
        "# 同日共振时的主导规则顺序\n"
        "_PRIORITY = (\n" + "".join(f'    "{name}",\n' for name in _PRIORITY) + ")\n"
        "\n"
        + inspect.getsource(_stop_multiple)
        + "\n"
        + inspect.getsource(_spendable)
    )
    checks_src = textwrap.dedent(inspect.getsource(alpha_checks))
    book_src = textwrap.dedent(inspect.getsource(_build_strategy_class))
    sell_catalog_src = ""
    for node in ast.parse(module_src).body:
        targets = node.targets if isinstance(node, ast.Assign) else [getattr(node, "target", None)]
        if any(getattr(target, "id", "") == "SELL_RULE_CATALOG" for target in targets):
            sell_catalog_src = ast.get_source_segment(module_src, node) or ""
            break
    sell_src = "\n\n".join(
        [sell_catalog_src.rstrip(), inspect.getsource(signals._sell_strategies).rstrip(), inspect.getsource(_fresh_sells).rstrip()]
    )
    return "\n\n".join(
        [
            "# ===== 1. 运行参数与依赖 =====\n" + header.rstrip(),
            "# ===== 2. 四条买入规则目录（硬条件 / 辅助条件 / 止损 / 离场）=====\n" + catalog_src.rstrip(),
            "# ===== 3. 买入判定：硬条件全中，辅助条件按门槛 =====\n" + checks_src.rstrip(),
            "# ===== 4. 执行引擎：信号当天收盘成交，建仓、加仓、止盈止损、清仓冷却 =====\n" + book_src.rstrip(),
            "# ===== 5. 四条卖出规则：任一条当天刚成立，持仓整笔清仓 =====\n" + sell_src,
        ]
    ) + "\n"


def _stakes(sleeves: list[str], total: float = CASH) -> list[float]:
    """Each sleeve's share of the book, split evenly across the names in that sleeve."""
    from app.services.review import SLEEVE_PCT

    counts = {key: sleeves.count(key) for key in SLEEVE_PCT}
    cents = int(round(total * 100))
    return [
        (cents * SLEEVE_PCT[sleeve] // 100 // counts[sleeve]) / 100
        for sleeve in sleeves
    ]


def _one(
    db: Session,
    symbol: str,
    name: str | None,
    market: str,
    start: date | None,
    end: date | None,
    cash: float,
    mood: dict | None = None,
    risk_base: float | None = None,
) -> dict:
    item = {
        "symbol": symbol,
        "name": name,
        "bars": 0,
        "return_pct": None,
        "max_drawdown_pct": None,
        "closed_trades": 0,
        "win_rate": None,
        "end_value": None,
        "cash": round(float(cash), 2),
        "pnl": None,
        "rule": "",
        "error": "",
        "trades": [],
        # Working data for the book-level tables; dropped before the payload is returned.
        "curve": {},
        "closes": {},
        "cycles": [],
    }
    try:
        bars = _backtest_bars(db, symbol, end=end)
    except ValueError as exc:
        item["error"] = str(exc)
        return item
    except Exception as exc:
        logger.warning("回测日线失败 %s: %s", symbol, exc)
        item["error"] = "日线没有取到"
        return item
    frame = _frame(symbol, bars)
    if frame.empty or "date" not in frame.columns:
        item["error"] = "全市场数据库中没有该标的日线，请先回填数据" if _uses_market_store(symbol) else "日线不足，没有重跑"
        return item
    window = _in_window(frame, start, end)
    item["bars"] = len(window)
    item["closes"] = {str(day)[:10]: float(close) for day, close in zip(window["date"], window["close"])}
    if len(frame) < 40:
        item["error"] = "日线不足，没有重跑"
        return item
    if (start or end) and window.empty:
        item["error"] = "这一段没有日线"
        return item
    import akquant as aq

    try:
        kwargs = {
            "data": frame,
            "strategy": type("BookStrategy", (_Strategy,), {"risk_base": float(risk_base)}) if risk_base else _Strategy,
            "initial_cash": cash,
            "symbols": symbol,
            "history_depth": 140,
            "show_progress": False,
        }
        if market == "cn":
            # 模板给出过户费、滑点、T+1 和一手 100 股；佣金买卖都收、没有最低收费，
            # 印花税只在卖出收，ETF 不收。
            kwargs["broker_profile"] = "cn_stock_t1_low_fee"
            kwargs.update(
                commission_rate=_CN_COMMISSION,
                min_commission=0.0,
                stamp_tax_rate=0.0 if is_etf(symbol) else _CN_STAMP_TAX,
            )
        else:
            kwargs.update(
                commission_rate=0.0005,
                min_commission=1.0,
                lot_size=1,
                t_plus_one=False,
                slippage={"type": "percent", "value": _US_SLIPPAGE},
            )
        if start is not None:
            kwargs["start_time"] = start.isoformat()
        if end is not None:
            kwargs["end_time"] = end.isoformat()
        result = aq.run_backtest(**kwargs)
    except Exception as exc:
        logger.warning("AKQuant 失败 %s: %s", symbol, exc)
        item["error"] = "回测没有跑完"
        return item
    metrics = result.metrics
    item["return_pct"] = _metric(metrics, "total_return_pct")
    item["max_drawdown_pct"] = _metric(metrics, "max_drawdown_pct")
    item["closed_trades"] = int(_metric(metrics, "closed_trade_count") or 0)
    item["win_rate"] = _metric(metrics, "win_rate") if item["closed_trades"] else None
    item["end_value"] = _metric(metrics, "end_market_value")
    if item["end_value"] is not None:
        item["pnl"] = round(item["end_value"] - item["cash"], 2)
    orders = result.orders_df
    if orders is not None and not orders.empty and "tag" in orders.columns:
        tags = [str(tag) for tag in orders["tag"].tolist() if tag]
        if tags:
            rule = max(set(tags), key=tags.count)
            item["rule"] = RULE_NAME.get(rule, rule)
    if not item["rule"]:
        item["rule"] = "没有触发"
    item["trades"] = _trade_rows(symbol, result, bars, mood or {})
    try:
        item["curve"] = {str(stamp)[:10]: float(value) for stamp, value in result.equity_curve.items()}
        item["cycles"] = _cycle_rows(result, frame)
    except Exception as exc:
        logger.warning("回测统计失败 %s: %s", symbol, exc)
    return item


def _primary_rule(tag: str) -> str:
    """The rule that led a cycle, read back from the first buy's tag (rule names joined by 、)."""
    by_name = {name: rule for rule, name in RULE_NAME.items()}
    rules = [by_name[part] for part in str(tag or "").split("、") if part in by_name]
    for rule in _PRIORITY:
        if rule in rules:
            return rule
    return ""


def _cycle_rows(result, frame: pd.DataFrame) -> list[dict]:
    """One row per flat-to-flat round, with its risk (1R), result in R, and how far price went
    against and for it while held. Excursions are measured from the first entry price, so adding
    to a winner does not move the reference."""
    orders = getattr(result, "orders_df", None)
    if orders is None or orders.empty:
        return []
    days = [str(day)[:10] for day in frame["date"]]
    bars = [
        {"h": float(high), "l": float(low), "c": float(close)}
        for high, low, close in zip(frame["high"], frame["low"], frame["close"])
    ]
    fills = []
    for order in orders.sort_values("created_at").itertuples(index=False):
        qty = float(getattr(order, "filled_quantity", 0) or 0)
        if qty <= 0:
            continue
        fills.append((_trade_day(order.created_at), str(order.side).lower(), qty, float(order.avg_price), str(order.tag or "")))
    cycles: list[dict] = []
    held = 0.0
    current = None
    for day, side, qty, price, tag in fills:
        if side == "buy":
            if held <= 1e-6:
                current = {"start": day, "end": "", "rule": _primary_rule(tag), "entry": price}
                cycles.append(current)
            held += qty
        elif current:
            held -= qty
            if held <= 1e-6:
                held = 0.0
                current["end"] = day
                current = None
    trades = getattr(result, "trades_df", None)
    lots = [] if trades is None or trades.empty else [
        (
            _trade_day(row.entry_time),
            float(row.pnl or 0),
            float(row.commission or 0),
            float(row.net_pnl or 0),
            float(row.quantity or 0),
        )
        for row in trades.itertuples(index=False)
    ]
    rows = []
    for cycle in cycles:
        # 还没清仓的那一轮不统计：结果没出来。
        if not cycle["end"] or not cycle["rule"] or cycle["start"] not in days:
            continue
        first = days.index(cycle["start"])
        last = bisect.bisect_right(days, cycle["end"]) - 1
        # 和引擎一样：用买入当天为止的最近 141 根算 ATR。
        atrs = wilder_atr(bars[max(0, first - 140) : first + 1])
        distance = _stop_multiple(cycle["rule"]) * (atrs[-1] if atrs else 0.0)
        if distance <= 0 or last <= first:
            continue
        mine = [lot for lot in lots if cycle["start"] <= lot[0] <= cycle["end"]]
        net = sum(lot[3] for lot in mine)
        # 买入是当天收盘成交，所以从下一根开始看；清仓那天的盘中也算在持有期内。
        span = bars[first + 1 : last + 1]
        rows.append(
            {
                "rule": cycle["rule"],
                "gross": sum(lot[1] for lot in mine),
                "commission": sum(lot[2] for lot in mine),
                "net": net,
                # 每一笔平仓各算一次：这笔净盈亏 ÷（止损距离 × 这笔的股数）。
                # 不按整轮算，否则加过仓的盈利轮分母更大，分批的规则会被系统性压低。
                "lot_r": [lot[3] / (distance * lot[4]) for lot in mine if lot[4] > 0],
                "held": last - first,
                "mae": max(0.0, cycle["entry"] - min(bar["l"] for bar in span)) / distance,
                "mfe": max(0.0, max(bar["h"] for bar in span) - cycle["entry"]) / distance,
            }
        )
    return rows


def _quantile(values: list[float], share: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    spot = (len(ordered) - 1) * share
    low = int(spot)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (spot - low)


def _rule_tables(cycles: list[dict]) -> tuple[list[dict], list[dict]]:
    """Per-rule result table and per-rule excursion table, each with a closing 全部 row."""
    groups = [(RULE_NAME[rule], _stop_multiple(rule), [c for c in cycles if c["rule"] == rule]) for rule in _PRIORITY]
    groups.append(("全部", None, cycles))
    results, excursions = [], []
    for name, stop_atr, rows in groups:
        if not rows:
            continue
        wins = [row for row in rows if row["net"] > 0]
        losses = [row for row in rows if row["net"] <= 0]
        avg_win = sum(row["net"] for row in wins) / len(wins) if wins else None
        avg_loss = sum(row["net"] for row in losses) / len(losses) if losses else None
        lot_r = [value for row in rows for value in row["lot_r"]]
        results.append(
            {
                "rule": name,
                "cycles": len(rows),
                "lots": len(lot_r),
                "win_rate": round(len(wins) / len(rows) * 100, 1),
                "payoff": round(avg_win / abs(avg_loss), 2) if avg_win is not None and avg_loss else None,
                "expect_r": round(sum(lot_r) / len(lot_r), 3) if lot_r else None,
                "held": round(sum(row["held"] for row in rows) / len(rows), 1),
                "gross": round(sum(row["gross"] for row in rows), 2),
                "commission": round(sum(row["commission"] for row in rows), 2),
                "net": round(sum(row["net"] for row in rows), 2),
            }
        )
        touched = [row for row in rows if row["mae"] >= 1]
        excursions.append(
            {
                "rule": name,
                "stop_atr": stop_atr,
                "cycles": len(rows),
                "wins": len(wins),
                "win_mae_p50": _round(_quantile([row["mae"] for row in wins], 0.5), 2),
                "win_mae_p75": _round(_quantile([row["mae"] for row in wins], 0.75), 2),
                "win_mae_max": _round(max((row["mae"] for row in wins), default=None), 2),
                "loss_mfe_p50": _round(_quantile([row["mfe"] for row in losses], 0.5), 2),
                "touched": len(touched),
                "touched_won": sum(1 for row in touched if row["net"] > 0),
                "reach_15": round(sum(1 for row in rows if row["mfe"] >= 1.5) / len(rows) * 100, 0),
                "reach_25": round(sum(1 for row in rows if row["mfe"] >= 2.5) / len(rows) * 100, 0),
            }
        )
    return results, excursions


def _carried(days: list[str], curve: dict[str, float], start: float) -> list[float]:
    """The curve on every day of the axis; days without a value keep the last one."""
    values = []
    last = start
    for day in days:
        last = curve.get(day, last)
        values.append(last)
    return values


def _hold_curve(closes: dict[str, float], stake: float, lot: int) -> dict[str, float]:
    """Buy whole lots at the first close in the range and never sell. Fees are not charged."""
    curve: dict[str, float] = {}
    shares = None
    spare = stake
    for day in sorted(closes):
        price = closes[day]
        if shares is None:
            shares = int(_spendable(stake) / price / lot) * lot if price > 0 else 0
            spare = stake - shares * price
        curve[day] = spare + shares * price
    return curve


def _compare_row(label: str, note: str, values: list[float], start: float) -> dict:
    """End value, Sharpe (risk-free rate taken as 0, daily × √252), worst drawdown and Calmar."""
    row = {
        "label": label,
        "note": note,
        "end_value": None,
        "pnl": None,
        "return_pct": None,
        "annual_pct": None,
        "sharpe": None,
        "max_drawdown_pct": None,
        "calmar": None,
    }
    if len(values) < 2 or start <= 0:
        return row
    series = [start, *values]
    moves = [after / before - 1 for before, after in zip(series, series[1:]) if before > 0]
    average = sum(moves) / len(moves)
    spread = (sum((move - average) ** 2 for move in moves) / (len(moves) - 1)) ** 0.5
    peak = series[0]
    deepest = 0.0
    for value in series:
        peak = max(peak, value)
        deepest = max(deepest, 1 - value / peak)
    annual = (values[-1] / start) ** (252 / len(values)) - 1
    row.update(
        end_value=round(values[-1], 2),
        pnl=round(values[-1] - start, 2),
        return_pct=round((values[-1] / start - 1) * 100, 2),
        annual_pct=round(annual * 100, 2),
        sharpe=round(average / spread * 252**0.5, 2) if spread > 0 else None,
        max_drawdown_pct=round(deepest * 100, 2),
        calmar=round(annual / deepest, 2) if deepest > 0 else None,
    )
    return row


def _metric(metrics, name: str):
    value = getattr(metrics, name, None)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return number


def _round(value, digits: int):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return round(number, digits)


def _mood(db: Session, market: str, start: date | None, end: date | None) -> dict[str, dict]:
    """Saved whole-market close. The same daily series as the breadth chart. Missing days stay absent."""
    book = {"us": "us_all", "cn": "cn_all"}.get(market)
    if book is None:
        return {}
    query = db.query(BreadthDay).filter(BreadthDay.market == market, BreadthDay.book == book)
    if start is not None:
        query = query.filter(BreadthDay.bar_date >= date.fromordinal(start.toordinal() - 10))
    if end is not None:
        query = query.filter(BreadthDay.bar_date <= end)
    return {
        row.bar_date.isoformat(): {
            "arms": _round(row.arms, 2),
            "above_ma20": _round(row.above_ma20, 1),
            "advancers": int(row.advancers or 0),
            "decliners": int(row.decliners or 0),
        }
        for row in query
    }


def _context(bars: list[dict], signal: str, mood: dict) -> dict:
    prefix = [bar for bar in bars if bar["d"] <= signal]
    measured = _measure(prefix, None) if len(prefix) >= 21 else None
    stats = measured or {}
    tone = mood.get(signal) or {}
    vs = stats.get("vs_ma20")
    return {
        "rsi": _round(stats.get("rsi"), 1),
        "mom": _round(stats.get("mom"), 2),
        "ma20": _round(stats.get("ma20"), 2),
        "vs_ma20": None if vs is None else round(float(vs) * 100, 1),
        "arms": tone.get("arms"),
        "above_ma20": tone.get("above_ma20"),
        "advancers": tone.get("advancers"),
        "decliners": tone.get("decliners"),
    }


def _trade_moment(value) -> datetime | None:
    text = str(value or "").strip()
    if not text or text.lower() in {"nan", "nat", "none"}:
        return None
    if text[0].isdigit() and "-" not in text[:10]:
        try:
            stamp = int(float(text))
        except ValueError:
            stamp = 0
        if stamp > 10**14:
            return datetime.fromtimestamp(stamp / 1_000_000_000, timezone.utc)
        return None
    cleaned = text.replace("T", " ")[:19]
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(cleaned[: len(datetime.now().strftime(pattern))], pattern).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _trade_day(value) -> str:
    moment = _trade_moment(value)
    if moment is not None:
        return moment.date().isoformat()
    text = str(value or "").strip()
    return "" if not text or text.lower() in {"nan", "nat", "none"} else text[:10]


def _trade_clock(value) -> str:
    moment = _trade_moment(value)
    if moment is None:
        return ""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _signal_rules(bars: list[dict], signal: str, symbol: str) -> list[dict]:
    """The buy rules ready on the signal day, with each hard/aux condition's pass-fail."""
    prefix = [bar for bar in bars if bar["d"] <= signal]
    if len(prefix) < 25:
        return []
    closes = [bar["c"] for bar in prefix]
    packs = _alpha_strategies(prefix, _rsi(closes), _stop_candle(prefix), is_etf(symbol), with_conditions=True)
    return [
        {
            "name": pack["name"],
            "need": pack.get("need", 0),
            "opt_hits": pack.get("opt_hits", 0),
            "core": pack.get("core", []),
            "optional": pack.get("optional", []),
        }
        for pack in packs
        if pack.get("ready")
    ]


def _trade_rows(symbol: str, result, bars: list[dict], mood: dict) -> list[dict]:
    frame = getattr(result, "trades_df", None)
    if frame is None or getattr(frame, "empty", True):
        return []
    rows = []
    for trade in frame.itertuples(index=False):
        fill = _trade_day(trade.entry_time)
        # 信号当天收盘成交，信号日就是成交日。
        signal = fill if any(bar["d"] == fill for bar in bars) else ""
        row = {
            "symbol": symbol,
            "entry": fill,
            "exit": _trade_day(getattr(trade, "exit_time", None)),
            "entry_at": _trade_clock(trade.entry_time),
            "exit_at": _trade_clock(getattr(trade, "exit_time", None)),
            "entry_price": _round(getattr(trade, "entry_price", None), 4),
            "exit_price": _round(getattr(trade, "exit_price", None), 4),
            "qty": _round(getattr(trade, "quantity", None), 4),
            "commission": _round(getattr(trade, "commission", None), 2),
            "pnl": _round(getattr(trade, "net_pnl", None), 2),
            "signal": signal,
            "rule": str(getattr(trade, "entry_tag", "") or ""),
            "exit_rule": str(getattr(trade, "exit_tag", "") or ""),
            "return_pct": _round(getattr(trade, "return_pct", None), 2),
            "held": int(getattr(trade, "duration_bars", 0) or 0),
        }
        if signal:
            row.update(_context(bars, signal, mood))
            row["signal_rules"] = _signal_rules(bars, signal, symbol)
        rows.append(row)
    return rows


def run_book_backtest(
    db: Session, market: str, start: date | None = None, end: date | None = None
) -> dict:
    from app.services.review import SLEEVE_LABEL, SLEEVE_PCT, sleeve_choices, sleeve_of

    now = time.time()
    symbols = _symbols(db, market)
    even_split = len(symbols) < 4
    chosen = sleeve_choices(db, get_settings().default_portfolio_id)
    sleeves = ["even"] * len(symbols) if even_split else [sleeve_of(symbol, chosen) for symbol in symbols]
    total = _MARKET_CASH.get(market, CASH)
    key = (
        f"{market}|{total}|{start.isoformat() if start else ''}|{end.isoformat() if end else ''}"
        f"|{','.join(f'{symbol}={sleeve}' for symbol, sleeve in zip(symbols, sleeves))}"
        f"|{data_version(db, market, [symbol for symbol in symbols if _uses_market_store(symbol)])}"
    )
    cached = _cache.get(key)
    if cached and now - cached[0] < _CACHE_SECONDS:
        return cached[1]
    names = {
        row.symbol: row.name
        for row in db.query(SymbolMeta).filter(SymbolMeta.symbol.in_(symbols)).all()
    } if symbols else {}
    profile = (
        f"买卖佣金各 {_CN_COMMISSION * 100:.2f}%（没有最低收费），卖出印花税 {_CN_STAMP_TAX * 100:.2f}%（ETF 不收），另有过户费、滑点 0.01%，T+1，一手 100 股"
        if market == "cn"
        else f"佣金 0.05%，每笔成交滑点 {_US_SLIPPAGE * 100:.2f}%，没有印花税，可以当日买卖"
    )
    mood = _mood(db, market, start, end) if symbols else {}
    if even_split:
        each = int(total * _EVEN_SHARE * 100) // max(len(symbols), 1) / 100
        stakes = [each] * len(symbols)
    else:
        stakes = _stakes(sleeves, total)
    items = []
    for symbol, sleeve, stake in zip(symbols, sleeves, stakes):
        item = _one(db, symbol, names.get(symbol), market, start, end, stake, mood, risk_base=total)
        item["sleeve"] = "平分" if even_split else SLEEVE_LABEL[sleeve]
        items.append(item)
    # 不分给任何标的的那部分（至少 20%）全程是现金，不赚不亏。
    idle = round(total - sum(stakes), 2)
    counts = {name: sleeves.count(name) for name in SLEEVE_PCT}
    if even_split:
        split = (
            f"持仓与清仓共 {len(symbols)} 只，不足 4 只时不分核心卫星，"
            f"平分 {_EVEN_SHARE * 100:.0f}%，每只 {stakes[0]:,.0f}" if symbols else "没有可跑的标的"
        )
    else:
        split = "；".join(
            f"{SLEEVE_LABEL[name]} {counts[name]} 只平分 {SLEEVE_PCT[name]}%，每只 {total * SLEEVE_PCT[name] / 100 / counts[name]:,.0f}"
            for name in SLEEVE_PCT
            if counts[name]
        )
    ran = [item for item in items if item.get("end_value") is not None]
    end_value = None
    pnl = None
    return_pct = None
    if items:
        end_value = round(
            idle
            + sum(
                float(item["end_value"]) if item.get("end_value") is not None else float(item["cash"])
                for item in items
            ),
            2,
        )
        pnl = round(end_value - total, 2)
        return_pct = round(pnl / total * 100, 2)

    # ---- 对照：同一批标的买入不动、指数买入不动 ----
    lot = 100 if market == "cn" else 1
    curves = [item.pop("curve") for item in items]
    closes = [item.pop("closes") for item in items]
    cycles = [cycle for item in items for cycle in item.pop("cycles")]
    index_closes: dict[str, float] = {}
    if market == "us" and symbols:
        try:
            index_closes = {
                str(bar["d"])[:10]: float(bar["c"])
                for bar in _backtest_bars(db, _US_INDEX)
                if bar.get("c")
                and (start is None or str(bar["d"])[:10] >= start.isoformat())
                and (end is None or str(bar["d"])[:10] <= end.isoformat())
            }
        except Exception as exc:
            logger.warning("基准日线失败 %s: %s", _US_INDEX, exc)
    days = sorted({day for series in closes for day in series})
    compare = []
    if len(days) >= 2:
        invested = total - idle
        cash_note = f"留 {idle:,.0f} 现金不动"

        def book(series_list: list[dict[str, float]]) -> list[float]:
            columns = [_carried(days, series, stake) for series, stake in zip(series_list, stakes)]
            return [idle + sum(column[i] for column in columns) for i in range(len(days))]

        compare.append(_compare_row("按规则买卖", f"本页的回测结果，{cash_note}", book(curves), total))
        compare.append(
            _compare_row(
                "同一批标的买入不动",
                f"每只用同样的本金，在区间第一天收盘买入后不再动，{cash_note}，不计手续费",
                book([_hold_curve(series, stake, lot) for series, stake in zip(closes, stakes)]),
                total,
            )
        )
        if index_closes:
            held = _carried(days, _hold_curve(index_closes, invested, 1), invested)
            compare.append(
                _compare_row(
                    f"{_US_INDEX} 买入不动",
                    f"用 {invested:,.0f} 在区间第一天收盘买入标普 500 ETF 后不再动，{cash_note}，不计手续费",
                    [idle + value for value in held],
                    total,
                )
            )
    rule_stats, excursions = _rule_tables(cycles)
    lots = [trade for item in items for trade in item["trades"] if trade.get("pnl") is not None]
    fees = round(sum(float(trade.get("commission") or 0) for trade in lots), 2)
    net = round(sum(float(trade["pnl"]) for trade in lots), 2)
    cost = {
        "trades": len(lots),
        "gross": round(net + fees, 2),
        "commission": fees,
        "net": net,
        "share_pct": round(fees / (net + fees) * 100, 1) if net + fees > 0 else None,
    }
    if start and end:
        span = f"{start.isoformat()} 至 {end.isoformat()}"
    elif start:
        span = f"{start.isoformat()} 起"
    elif end:
        span = f"截止 {end.isoformat()}"
    else:
        span = "全部已有日线"
    payload = {
        "engine": "AKQuant",
        "market": market,
        "profile": profile,
        "cash": total,
        "idle_cash": idle,
        "start": start,
        "end": end,
        "note": (
            f"{span}。仅回测持仓与已清仓标的，不加入观察名单。普通股票日线直接读取全市场数据库，ETF 沿用原日线数据源。总资金 {total:,.0f}。{split}；剩下 {idle:,.0f} 留作现金不动"
            + (f"，{len(items) - len(ran)} 只没有跑成，那部分本金按没动计算" if items and len(ran) < len(items) else "")
            + "。硬条件必须全中，辅助条件按各自门槛。同日共振按「收缩突破 > 均线交叉 > RSI超卖 > 超跌反弹」取主导规则。收盘满足信号，当天按收盘价买入或卖出。1R 是该规则的 ATR 止损距离，止损锚定首笔入场价。均线交叉和收缩突破分三批买，收盘高于均价且比上一批再高 0.25 倍 ATR 就加一批；另外两套一次买入，股数按「打到止损时亏损不超过总本金 1%」封顶。吊灯止盈与反转触及 MA20 全天候生效；只有趋势规则做 +1.5R / +2.5R 各减 1/3，尾仓交给 ATR 吊灯。时间止损 15/20 日（浮盈达 1.5 倍 ATR 豁免）与 10/8 日无条件。清仓后暂停 5 个交易日。这列数字不进累计盈亏。"
        ),
        "end_value": end_value,
        "pnl": pnl,
        "return_pct": return_pct,
        "compare": compare,
        "rule_stats": rule_stats,
        "excursions": excursions,
        "cost": cost,
        "items": items,
    }
    _cache[key] = (time.time(), payload)
    return payload

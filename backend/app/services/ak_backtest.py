"""Replay the price-page buy rules with AKQuant. Does not change the ledger."""

from __future__ import annotations

import logging
import math
import time
from collections import defaultdict
from datetime import date

import pandas as pd
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.entities import BreadthDay, SymbolMeta, Trade
from app.services.market_scope import market_of
from app.services.signals import _alpha_strategies, _measure, _rsi, _stop_candle
from app.services.watchlist import watch_symbols

logger = logging.getLogger(__name__)
logging.getLogger("akquant").setLevel(logging.WARNING)

_CACHE_SECONDS = 600
_cache: dict[str, tuple[float, dict]] = {}
CASH = 100_000.0

# A moving-average cross that has not already risen is the trade that stops out.
# After a losing exit, wait five sessions before the next buy on that name.
_MA_PRIOR_RISE = 0.04
_LOSS_COOLDOWN = 5

RULE_NAME = {
    "ma_crossover_v1": "均线交叉",
    "rsi_reversion_v1": "RSI超卖",
    "volatility_compression_breakout_v1": "收缩突破",
    "mean_reversion_oversold_v1": "超跌反弹",
}


def _series(history, current: float) -> list[float]:
    values = [float(item) for item in history]
    if not values or abs(values[-1] - current) > 1e-8:
        values.append(float(current))
    return values


class _BookStrategy:
    """Filled in after import so the class subclasses akquant.Strategy."""


def _build_strategy_class():
    from akquant import Strategy

    class BookStrategy(Strategy):
        def __init__(self):
            super().__init__()
            self.pending: dict[str, str] = {}
            self.state: dict[str, dict] = {}
            self.cooldown: dict[str, int] = {}

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

        def _rule_exit(self, rule: str, entry: float, close: float, high: float, held: int, ma20: float, peak: float) -> bool:
            if rule == "ma_crossover_v1":
                if close <= entry * 0.96 or close >= entry * 1.12 or held >= 15:
                    return True
                return held >= 2 and close < ma20
            if rule == "rsi_reversion_v1":
                return close <= entry * 0.95 or close >= entry * 1.10 or held >= 10
            if rule == "volatility_compression_breakout_v1":
                if close < ma20 or close <= entry * 0.95 or held >= 20:
                    return True
                return peak >= entry * 1.08 and close <= peak * 0.96
            if rule == "mean_reversion_oversold_v1":
                return close <= peak * 0.97 or high >= ma20 or held >= 8
            return held >= 15

        def _exit(self, state: dict, bar, closes: list[float]) -> bool:
            entry = state["entry"]
            if entry <= 0 or len(closes) < 20:
                return False
            close = float(bar.close)
            high = float(bar.high)
            held = self.get_holding_bars(bar.symbol)
            ma20 = sum(closes[-20:]) / 20
            rules = [part for part in str(state.get("rules") or "").split(",") if part]
            if not rules:
                return held >= 15
            return any(
                self._rule_exit(rule, entry, close, high, held, ma20, state["high"])
                for rule in rules
            )

        def on_bar(self, bar):
            symbol = bar.symbol
            held = float(self.get_position(symbol) or 0)
            rows, closes = self._bars(bar)
            if held > 0:
                state = self.state.get(symbol)
                if state is None:
                    state = {
                        "rules": self.pending.get(symbol, ""),
                        "entry": float(bar.open or bar.close),
                        "high": float(bar.high),
                        "closing": False,
                    }
                    self.state[symbol] = state
                state["high"] = max(state["high"], float(bar.high))
                if not state["closing"] and self._exit(state, bar, closes):
                    if float(bar.close) < state["entry"]:
                        self.cooldown[symbol] = _LOSS_COOLDOWN
                    self.close_position(symbol)
                    state["closing"] = True
                return
            self.state.pop(symbol, None)
            waiting = self.cooldown.get(symbol, 0)
            if waiting:
                self.cooldown[symbol] = waiting - 1
                return
            if len(rows) < 25:
                return
            packs = _alpha_strategies(rows, _rsi(closes), _stop_candle(rows))
            risen = len(closes) >= 6 and closes[-1] / closes[-6] - 1 > _MA_PRIOR_RISE
            ready = [
                pack for pack in packs
                if pack.get("missed", 0) <= 1 and (pack.get("id") != "ma_crossover_v1" or risen)
            ]
            if not ready:
                return
            price = float(bar.close)
            lot = 100 if market_of(symbol) == "cn" else 1
            if price <= 0:
                return
            quantity = int((float(self.cash) * 0.95) / price / lot) * lot
            if quantity < lot:
                return
            self.buy(symbol=symbol, quantity=quantity, tag="、".join(pack["name"] for pack in ready))
            self.pending[symbol] = ",".join(pack["id"] for pack in ready)

    return BookStrategy


_Strategy = _build_strategy_class()


def _symbols(db: Session, market: str) -> list[str]:
    portfolio_id = get_settings().default_portfolio_id
    qty: dict[str, float] = defaultdict(float)
    rows = (
        db.query(Trade.symbol, Trade.side, Trade.qty)
        .filter(Trade.portfolio_id == portfolio_id, Trade.symbol.isnot(None))
        .all()
    )
    for symbol, side, amount in rows:
        key = str(symbol).upper()
        if market_of(key) != market:
            continue
        if side == "Buy":
            qty[key] += float(amount or 0)
        elif side == "Sell":
            qty[key] -= float(amount or 0)
    chosen = [symbol for symbol, amount in qty.items() if amount > 1e-8]
    if len(chosen) < 4:
        for symbol in watch_symbols(market):
            if symbol not in chosen:
                chosen.append(symbol)
            if len(chosen) >= 6:
                break
    return chosen[:8]


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


def _one(symbol: str, name: str | None, market: str, start: date | None, end: date | None, mood: dict | None = None) -> dict:
    from app.core.database import SessionLocal
    from app.services.bars import load_symbol_bars

    item = {
        "symbol": symbol,
        "name": name,
        "bars": 0,
        "return_pct": None,
        "max_drawdown_pct": None,
        "closed_trades": 0,
        "win_rate": None,
        "end_value": None,
        "rule": "",
        "error": "",
        "trades": [],
    }
    db = SessionLocal()
    try:
        bars = load_symbol_bars(db, symbol)
    except Exception as exc:
        logger.warning("回测日线失败 %s: %s", symbol, exc)
        item["error"] = "日线没有取到"
        return item
    finally:
        db.close()
    frame = _frame(symbol, bars)
    if frame.empty or "date" not in frame.columns:
        item["error"] = "日线不足，没有重跑"
        return item
    window = _in_window(frame, start, end)
    item["bars"] = len(window)
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
            "strategy": _Strategy,
            "initial_cash": CASH,
            "symbols": symbol,
            "history_depth": 140,
            "show_progress": False,
        }
        if market == "cn":
            kwargs["broker_profile"] = "cn_stock_t1_low_fee"
        else:
            kwargs.update(commission_rate=0.0005, min_commission=1.0, lot_size=1, t_plus_one=False)
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
    orders = result.orders_df
    if orders is not None and not orders.empty and "tag" in orders.columns:
        tags = [str(tag) for tag in orders["tag"].tolist() if tag]
        if tags:
            rule = max(set(tags), key=tags.count)
            item["rule"] = RULE_NAME.get(rule, rule)
    if not item["rule"]:
        item["rule"] = "没有触发"
    item["trades"] = _trade_rows(symbol, result, bars, mood or {})
    return item


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


def _trade_rows(symbol: str, result, bars: list[dict], mood: dict) -> list[dict]:
    frame = getattr(result, "trades_df", None)
    if frame is None or getattr(frame, "empty", True):
        return []
    rows = []
    for trade in frame.itertuples(index=False):
        fill = str(trade.entry_time)[:10]
        prior = None
        for bar in bars:
            if bar["d"] >= fill:
                break
            prior = bar
        signal = prior["d"] if prior else ""
        row = {
            "symbol": symbol,
            "entry": fill,
            "signal": signal,
            "rule": str(getattr(trade, "entry_tag", "") or ""),
            "return_pct": _round(getattr(trade, "return_pct", None), 2),
            "held": int(getattr(trade, "duration_bars", 0) or 0),
        }
        if signal:
            row.update(_context(bars, signal, mood))
        rows.append(row)
    return rows


def run_book_backtest(
    db: Session, market: str, start: date | None = None, end: date | None = None
) -> dict:
    now = time.time()
    key = f"{market}|{start.isoformat() if start else ''}|{end.isoformat() if end else ''}"
    cached = _cache.get(key)
    if cached and now - cached[0] < _CACHE_SECONDS:
        return cached[1]
    symbols = _symbols(db, market)
    names = {
        row.symbol: row.name
        for row in db.query(SymbolMeta).filter(SymbolMeta.symbol.in_(symbols)).all()
    } if symbols else {}
    profile = "A股佣金、印花税、过户费、T+1、一手 100 股" if market == "cn" else "佣金 0.05%，没有印花税，可以当日买卖"
    mood = _mood(db, market, start, end)
    items = [_one(symbol, names.get(symbol), market, start, end, mood) for symbol in symbols]
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
        "cash": CASH,
        "start": start,
        "end": end,
        "note": f"{span}。每只单独用 10 万。一条规则最多缺一项即可买；均线交叉还要近 5 日涨幅超过 4%。持有满 2 日后收盘跌破 20 日线也卖。亏损卖出后，这只暂停 5 个交易日。这列数字不进累计盈亏。",
        "items": items,
    }
    _cache[key] = (time.time(), payload)
    return payload

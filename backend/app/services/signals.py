"""Institutional-style read of open holdings from daily bars.

Prices, volatility, VWAP, bandwidth and Amihud come from the exchange daily
series. Trailing PE comes from the Tencent quote when it matches price / EPS.
The US 10-year yield comes from the public FRED DGS10 file. ERP is left empty
when either input is missing.
"""

from __future__ import annotations

import math
import re
from concurrent.futures import ThreadPoolExecutor
from statistics import mean, stdev

from sqlalchemy.orm import Session

from app.models.entities import PositionStop
from app.services.ledger import compute_holdings

TARGET_VOL = 0.20

# Known leveraged products. Sign is the equity beta multiple.
LEVERAGE = {
    "QID": -2.0,
    "SQQQ": -3.0,
    "TQQQ": 3.0,
    "PSQ": -1.0,
    "RKLX": 2.0,
    "QLD": 2.0,
    "SOXL": 3.0,
    "SOXS": -3.0,
    "07709": 2.0,
}


def _f(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _px(value: float | None) -> str:
    if value is None:
        return "—"
    if abs(value) >= 1000:
        return f"{value:,.0f}"
    return f"{value:,.2f}"


def _pct(value: float | None, digits: int = 1) -> str:
    if value is None:
        return "—"
    return f"{value * 100:+.{digits}f}%"


def _ann_vol(rets: list[float]) -> float | None:
    if len(rets) < 2:
        return None
    return stdev(rets) * math.sqrt(252)


def _load_bars(symbol: str) -> list[dict]:
    from app.core.database import SessionLocal
    from app.services.bars import load_symbol_bars

    db = SessionLocal()
    try:
        return load_symbol_bars(db, symbol)
    finally:
        db.close()


def _rsi(closes: list[float], period: int = 14) -> float | None:
    """Wilder RSI via the same adjusted EMA AlphaEvo uses (alpha = 1/period)."""
    if period <= 0 or len(closes) <= period:
        return None
    decay = 1.0 - 1.0 / period
    gain_num = gain_den = loss_num = loss_den = 0.0
    avg_gain = avg_loss = None
    for index in range(1, len(closes)):
        change = closes[index] - closes[index - 1]
        gain = change if change > 0 else 0.0
        loss = -change if change < 0 else 0.0
        gain_num = gain + decay * gain_num
        gain_den = 1.0 + decay * gain_den
        loss_num = loss + decay * loss_num
        loss_den = 1.0 + decay * loss_den
        if index >= period:
            avg_gain = gain_num / gain_den
            avg_loss = loss_num / loss_den
    if avg_gain is None or avg_loss is None:
        return None
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


_CN_ETF_PREFIXES = ("159", "510", "511", "512", "513", "515", "516", "517", "518", "560", "561", "562", "563", "588")


def is_etf(symbol: str) -> bool:
    """Low-volatility parameter set. A-share ETF codes and tickers ending in ETF."""
    ticker = str(symbol or "").split(":")[-1].upper()
    code = ticker.split(".")[0]
    if code.isdigit() and len(code) == 6 and code.startswith(_CN_ETF_PREFIXES):
        return True
    return ticker.endswith("ETF")


def _percentile(values: list[float], q: float) -> float | None:
    ordered = sorted(value for value in values if value == value)
    if not ordered:
        return None
    pos = (len(ordered) - 1) * q
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    weight = pos - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


def _bandwidths(closes: list[float]) -> list[float | None]:
    """(upper − lower) / MA20. Same 20-day sample deviation the chart uses."""
    out: list[float | None] = []
    for index in range(len(closes)):
        if index < 19:
            out.append(None)
            continue
        window = closes[index - 19 : index + 1]
        mid = mean(window)
        out.append((4 * stdev(window) / mid) if mid else None)
    return out


def wilder_atr(bars: list[dict], period: int = 14) -> list[float]:
    """Wilder ATR: 前 period 根采用简单均值种子对齐 TradingView/同花顺，并加最小正数兜底防除零。"""
    if not bars:
        return []
    trs: list[float] = []
    for i, bar in enumerate(bars):
        high = float(bar["h"])
        low = float(bar["l"])
        close = float(bar["c"])
        if i == 0:
            tr = high - low
        else:
            prev_close = float(bars[i - 1]["c"])
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        # 防止一字板或停牌导致 TR=0 引发下游 (close - ma20) / atr 除零
        trs.append(max(tr, close * 1e-4))

    values: list[float] = []
    running_sum = 0.0
    for i, tr in enumerate(trs):
        running_sum += tr
        if i < period - 1:
            values.append(running_sum / (i + 1))
        elif i == period - 1:
            values.append(running_sum / period)
        else:
            values.append((values[-1] * (period - 1) + tr) / period)
    return values


def _bullish_pattern(bars: list[dict]) -> str | None:
    """识别底部锤子线 (含探底T字线) 与看涨吞没，过滤高位吊颈线与十字星假吞没。"""
    if len(bars) < 2:
        return None
    row = bars[-1]
    opened = float(row.get("o", row["c"]))
    close = float(row["c"])
    high = float(row["h"])
    low = float(row["l"])
    span = high - low
    if span <= 1e-6:
        return None

    body = abs(close - opened)
    lower_shadow = min(opened, close) - low
    upper_shadow = high - max(opened, close)

    # 位置过滤：当日最低价必须处于近5日均价下方或附近，防止把连续大涨后的高位吊颈线当成底部锤子线
    if len(bars) >= 5:
        ma5_close = mean(float(b["c"]) for b in bars[-5:])
        if low > ma5_close * 1.01:
            return None

    # 1. 锤子线（用 max(body, span*0.08) 兼容底部探底 T 字线，且要求收盘在振幅上半区）
    effective_body = max(body, span * 0.08)
    if lower_shadow >= 2.0 * effective_body and upper_shadow <= 0.15 * span and (close - low) / span >= 0.55:
        return "锤子线"

    # 2. 看涨吞没（要求昨日阴线实体至少占昨日振幅 20%，防止吞没一根十字星）
    prev = bars[-2]
    prev_open = float(prev.get("o", prev["c"]))
    prev_close = float(prev["c"])
    prev_span = float(prev["h"]) - float(prev["l"])
    prev_body = abs(prev_close - prev_open)
    is_valid_prev_yin = (prev_close < prev_open) and (prev_span > 1e-6) and (prev_body / prev_span >= 0.20)

    if is_valid_prev_yin and close > opened and opened <= prev_close and close >= prev_open and body >= prev_body:
        return "看涨吞没"
    return None


def _stop_candle(bars: list[dict]) -> str | None:
    """图表标签与 _bullish_pattern 保持一致。"""
    return _bullish_pattern(bars)


# The four buy rules in words. `_alpha_strategies` pairs these labels with its own
# live checks, so the strategy summary can never drift from what the engine trades.
ALPHA_RULE_CATALOG: tuple[dict, ...] = (
    {
        "id": "ma_crossover_v1",
        "name": "均线交叉",
        "kind": "trend",
        "sizing": "分三批：先买 1/3；之后收盘高于均价、且比上一批买价再高出至少 0.25 倍初始 ATR，当天按收盘价再买一批。",
        "core": (
            "MA5 不低于 MA10，允许差 0.08 倍 ATR",
            "近 5 日涨幅在 1.2 到 3.5 倍当日 ATR% 之间",
            "收盘在 MA20 上，但不超过 2.5 倍 ATR",
        ),
        "optional": (
            "5 日量比不低于 1.2",
            "RSI 在 50 和 72 之间",
            "MA20 比昨天高",
        ),
        "need": 2,
        "stop_atr": 2.0,
        "exit_steps": (
            "ATR 止损锚定首笔入场价：收盘 ≤ 首笔价 − 2 倍初始 ATR，整笔清仓。",
            "持有满 3 日后收盘跌破 MA20 下方 0.3 倍当日 ATR，整笔清仓。",
            "吊灯止盈：浮盈到过 2.5 倍初始 ATR 后，收盘从最高价回落 2.2 倍当日 ATR 即清仓，全天候生效、不必先分批。",
            "趋势分批：+1.5R 减 1/3，+2.5R 再减 1/3；尾仓跟随上面的吊灯止盈。",
            "时间止损：满 15 日且浮盈不到 1.5 倍初始 ATR 则离场，浮盈达标自动豁免。",
            "卖出规则：收缩跌破、均线死叉、RSI超买、超涨回落任一条当天刚成立（前一天还不成立），整笔清仓。",
            "全局失效：持仓满 40 日且四条规则都不成立，清掉。",
        ),
    },
    {
        "id": "rsi_reversion_v1",
        "name": "RSI超卖",
        "kind": "reversion",
        "sizing": "一次买入，不分批、不加仓。股数按「打到 ATR 止损时亏损不超过总本金的 1%」反推，且不超过计划仓位。",
        "core": (
            "收盘在 MA10 下",
            "RSI(6) 低于 25，或 RSI(14) 低于 35",
            "出现锤子线或看涨吞没",
        ),
        "optional": (
            "5 日量比高于 1.3",
            "收盘比 MA20 低 1 到 3 倍 ATR",
        ),
        "need": 1,
        "stop_atr": 1.8,
        "exit_steps": (
            "ATR 止损锚定首笔入场价：收盘 ≤ 首笔价 − 1.8 倍初始 ATR，整笔清仓。",
            "止盈：收盘 ≥ 首笔价 + 3 倍初始 ATR，或当日最高价触及 MA20，整笔清仓，全天候生效。",
            "反转规则不做 +1.5R / +2.5R 分批减仓，一次买入、一次清掉。",
            "时间止损：持有满 10 日无条件离场。",
            "卖出规则：收缩跌破、均线死叉、RSI超买、超涨回落任一条当天刚成立（前一天还不成立），整笔清仓。",
            "全局失效：持仓满 40 日且四条规则都不成立，清掉。",
        ),
    },
    {
        "id": "volatility_compression_breakout_v1",
        "name": "收缩突破",
        "kind": "trend",
        "sizing": "分三批：先买 1/3；之后收盘高于均价、且比上一批买价再高出至少 0.25 倍初始 ATR，当天按收盘价再买一批。",
        "core": (
            "收盘突破前 20 日高点",
            "前 3 日有一天带宽不超过 3.8 倍 ATR%，或处于近 120 日最低两成",
        ),
        "optional": (
            "20 日量比不低于 1.2",
            "阳线实体占振幅至少 45%",
            "MA20 比 5 日前高",
            "处在近 120 日高低区间的 65% 以上",
        ),
        # 突破前 20 日高点时收盘必然在 MA20 上，那一项不再单列；4 项中 3 项与原来 5 项中 4 项等价。
        "need": 3,
        "stop_atr": 1.8,
        "exit_steps": (
            "ATR 止损锚定首笔入场价：收盘 ≤ 首笔价 − 1.8 倍初始 ATR，整笔清仓。",
            "收盘跌破 MA20 下方 0.3 倍当日 ATR，整笔清仓（不设持有天数门槛）。",
            "吊灯止盈：浮盈到过 2 倍初始 ATR 后，收盘从最高价回落 2 倍当日 ATR 即清仓，全天候生效、不必先分批。",
            "趋势分批：+1.5R 减 1/3，+2.5R 再减 1/3；尾仓跟随上面的吊灯止盈。",
            "时间止损：满 20 日且浮盈不到 1.5 倍初始 ATR 则离场，浮盈达标自动豁免。",
            "卖出规则：收缩跌破、均线死叉、RSI超买、超涨回落任一条当天刚成立（前一天还不成立），整笔清仓。",
            "全局失效：持仓满 40 日且四条规则都不成立，清掉。",
        ),
    },
    {
        "id": "mean_reversion_oversold_v1",
        "name": "超跌反弹",
        "kind": "reversion",
        "sizing": "一次买入，不分批、不加仓。股数按「打到 ATR 止损时亏损不超过总本金的 1%」反推，且不超过计划仓位。",
        "core": (
            "收盘比 MA20 低至少 2.5 倍 ATR",
            "RSI(14) 低于 30，或 RSI(6) 低于 20",
        ),
        "optional": (
            "出现锤子线或看涨吞没",
            "前 5 日均量低于前 10 日的 85%，当天量比昨天多 15%",
        ),
        "need": 1,
        "stop_atr": 2.2,
        "exit_steps": (
            "ATR 止损锚定首笔入场价：收盘 ≤ 首笔价 − 2.2 倍初始 ATR，整笔清仓。",
            "止盈：当日最高价触及 MA20，或浮盈到过 1.5 倍初始 ATR 后收盘从最高价回落 1.2 倍当日 ATR，整笔清仓，全天候生效。",
            "反转规则不做 +1.5R / +2.5R 分批减仓，一次买入、一次清掉。",
            "时间止损：持有满 8 日无条件离场。",
            "卖出规则：收缩跌破、均线死叉、RSI超买、超涨回落任一条当天刚成立（前一天还不成立），整笔清仓。",
            "全局失效：持仓满 40 日且四条规则都不成立，清掉。",
        ),
    },
)


def rule_catalog() -> list[dict]:
    """The buy rules as the strategy page shows them: hard checks, optional checks, sizing, exit."""
    return [
        {
            "id": rule["id"],
            "name": rule["name"],
            "kind": rule["kind"],
            "sizing": rule["sizing"],
            "core": list(rule["core"]),
            "optional": list(rule["optional"]),
            "need": rule["need"],
            "stop_atr": rule["stop_atr"],
            "exit_steps": list(rule["exit_steps"]),
        }
        for rule in ALPHA_RULE_CATALOG
    ]


def _alpha_strategies(
    bars: list[dict],
    rsi: float | None,
    stop_candle: str | None,
    is_etf: bool = False,
    with_conditions: bool = False,
) -> list[dict]:
    """Four buy rules. Core checks must all pass. Optional checks may miss one.

    Distances and the five-day rise are measured in 14-day ATR, so a quiet ETF
    and a volatile stock do not share one fixed percent. Volume ratios use the
    previous sessions' average. `stop_candle` is the chart label and is not a buy check.
    """
    del stop_candle, is_etf
    closes = [bar["c"] for bar in bars]
    highs = [bar["h"] for bar in bars]
    lows = [bar["l"] for bar in bars]
    volumes = [bar["v"] for bar in bars]
    last = bars[-1]
    opened = last.get("o", last["c"])
    close = last["c"]
    ma5 = mean(closes[-5:]) if len(closes) >= 5 else None
    ma10 = mean(closes[-10:]) if len(closes) >= 10 else None
    ma20 = mean(closes[-20:]) if len(closes) >= 20 else None
    ma20_prev = mean(closes[-21:-1]) if len(closes) >= 21 else None
    ma20_5ago = mean(closes[-25:-5]) if len(closes) >= 25 else None
    above_ma20 = ma20 is not None and close > ma20
    below_ma10 = ma10 is not None and close < ma10
    atrs = wilder_atr(bars)
    atr = atrs[-1] if atrs else None
    atr_pct = (atr / close * 100) if atr and close else None
    atr_dist = ((close - ma20) / atr) if atr and ma20 is not None else None
    ma_near = ma5 is not None and ma10 is not None and atr is not None and ma5 >= ma10 - 0.08 * atr
    avg5 = mean(volumes[-6:-1]) if len(volumes) >= 6 else 0.0
    vol_ratio_5 = (volumes[-1] / avg5) if avg5 else 0.0
    avg20 = mean(volumes[-21:-1]) if len(volumes) >= 21 else 0.0
    vol_ratio_20 = (volumes[-1] / avg20) if avg20 else 0.0
    prior_high = max(highs[-21:-1]) if len(highs) >= 21 else None
    breakout = prior_high is not None and close > prior_high
    span = last["h"] - last["l"]
    yang_body = ((close - opened) / span) if span else 0.0
    rise_5 = ((closes[-1] / closes[-6] - 1) * 100) if len(closes) >= 6 and closes[-6] else None
    momentum_ok = (
        atr_pct is not None
        and rise_5 is not None
        and rise_5 >= 1.2 * atr_pct
        and rise_5 <= 3.5 * atr_pct
    )
    above_band = atr_dist is not None and 0 < atr_dist <= 2.5
    rsi14 = rsi if rsi is not None else _rsi(closes, 14)
    rsi6 = _rsi(closes, 6)
    pattern = _bullish_pattern(bars)
    widths = _bandwidths(closes)
    prior_widths = [value for value in widths[-4:-1] if value is not None]
    prior_width = min(prior_widths) if prior_widths else None
    width_floor = _percentile([value for value in widths[-120:] if value is not None], 0.20)
    prior_pct = prior_width * 100 if prior_width is not None else None
    floor_pct = width_floor * 100 if width_floor is not None else None
    squeezed = prior_pct is not None and atr_pct is not None and (
        prior_pct <= 3.8 * atr_pct or (floor_pct is not None and prior_pct <= floor_pct)
    )
    window = min(120, len(highs))
    low_120 = min(lows[-window:]) if window else None
    high_120 = max(highs[-window:]) if window else None
    if low_120 is None or high_120 is None or high_120 == low_120 or window < 30:
        position = None
    else:
        position = (close - low_120) / (high_120 - low_120)
    avg10_prev = mean(volumes[-11:-1]) if len(volumes) >= 11 else 0.0
    shrink_then_expand = (
        bool(avg5 and avg10_prev)
        and avg5 < avg10_prev * 0.85
        and len(volumes) >= 2
        and volumes[-1] > volumes[-2] * 1.15
    )
    touched_ma20 = ma20 is not None and last["h"] >= ma20

    def pack(
        strategy_id: str,
        name: str,
        core: list[tuple[str, bool]],
        optional: list[tuple[str, bool]],
        need: int,
        exit_rule: str,
        exit_now: str = "",
    ) -> dict:
        core_miss = [label for label, ok in core if not ok]
        opt_hits = sum(1 for _, ok in optional if ok)
        missed = list(core_miss)
        if opt_hits < need:
            missed.extend(label for label, ok in optional if not ok)
        result = {
            "id": strategy_id,
            "name": name,
            "ready": not missed,
            "missed": len(missed),
            "detail": "条件都满足" if not missed else "、".join(missed),
            "exit": exit_rule,
            "exit_now": exit_now,
        }
        if with_conditions:
            result["need"] = need
            result["opt_hits"] = opt_hits
            result["core"] = [{"label": label, "ok": bool(ok)} for label, ok in core]
            result["optional"] = [{"label": label, "ok": bool(ok)} for label, ok in optional]
        return result

    trend_rule, reversion_rule, breakout_rule, oversold_rule = ALPHA_RULE_CATALOG

    def checks(rule: dict, core_ok: list[bool], optional_ok: list[bool]):
        """Catalog labels paired with this bar's live checks, in catalog order."""
        return (
            list(zip(rule["core"], core_ok)),
            list(zip(rule["optional"], optional_ok)),
            rule["need"],
            " ".join(rule["exit_steps"]),
        )

    return [
        pack(
            trend_rule["id"],
            trend_rule["name"],
            *checks(
                trend_rule,
                [ma_near, momentum_ok, above_band],
                [
                    vol_ratio_5 >= 1.2,
                    rsi14 is not None and 50 < rsi14 < 72,
                    ma20_prev is not None and ma20 is not None and ma20 > ma20_prev,
                ],
            ),
        ),
        pack(
            reversion_rule["id"],
            reversion_rule["name"],
            *checks(
                reversion_rule,
                [
                    below_ma10,
                    (rsi6 is not None and rsi6 < 25) or (rsi14 is not None and rsi14 < 35),
                    pattern is not None,
                ],
                [
                    vol_ratio_5 > 1.3,
                    atr_dist is not None and -3 <= atr_dist <= -1,
                ],
            ),
            "当天最高已触及 MA20" if touched_ma20 else "",
        ),
        pack(
            breakout_rule["id"],
            breakout_rule["name"],
            *checks(
                breakout_rule,
                [breakout, squeezed],
                [
                    vol_ratio_20 >= 1.2,
                    yang_body >= 0.45,
                    ma20 is not None and ma20_5ago is not None and ma20 > ma20_5ago,
                    position is not None and position >= 0.65,
                ],
            ),
            "当天收盘在 MA20 下" if not above_ma20 else "",
        ),
        pack(
            oversold_rule["id"],
            oversold_rule["name"],
            *checks(
                oversold_rule,
                [
                    atr_dist is not None and atr_dist <= -2.5,
                    (rsi14 is not None and rsi14 < 30) or (rsi6 is not None and rsi6 < 20),
                ],
                [pattern is not None, shrink_then_expand],
            ),
            "当天最高已触及 MA20" if touched_ma20 else "",
        ),
    ]


def _bearish_pattern(bars: list[dict]) -> str | None:
    """Shooting star or bearish engulfing: `_bullish_pattern` turned upside down."""
    if len(bars) < 2:
        return None
    row = bars[-1]
    opened = float(row.get("o", row["c"]))
    close = float(row["c"])
    high = float(row["h"])
    low = float(row["l"])
    span = high - low
    if span <= 1e-6:
        return None
    body = abs(close - opened)
    lower_shadow = min(opened, close) - low
    upper_shadow = high - max(opened, close)
    # The high has to sit at or above the five-day average, so a long upper shadow
    # after a slide is not read as a top.
    if len(bars) >= 5 and high < mean(float(b["c"]) for b in bars[-5:]) * 0.99:
        return None
    effective_body = max(body, span * 0.08)
    if upper_shadow >= 2.0 * effective_body and lower_shadow <= 0.15 * span and (high - close) / span >= 0.55:
        return "流星线"
    prev = bars[-2]
    prev_open = float(prev.get("o", prev["c"]))
    prev_close = float(prev["c"])
    prev_span = float(prev["h"]) - float(prev["l"])
    prev_body = abs(prev_close - prev_open)
    prev_yang = prev_close > prev_open and prev_span > 1e-6 and prev_body / prev_span >= 0.20
    if prev_yang and close < opened and opened >= prev_close and close <= prev_open and body >= prev_body:
        return "看跌吞没"
    return None


# Four sell rules, each the mirror image of one buy rule with the same numbers. They
# look only at the bars, so they read the same whether or not anything is held. The
# backtest engine closes a position on the day one of them turns ready.
SELL_RULE_CATALOG: tuple[dict, ...] = (
    {
        "id": "breakdown_sell_v1",
        "name": "收缩跌破",
        "core": (
            "收盘跌破前 20 日低点",
            "前 3 日有一天带宽不超过 3.8 倍 ATR%，或处于近 120 日最低两成",
        ),
        "optional": (
            "20 日量比不低于 1.2",
            "阴线实体占振幅至少 45%",
            "MA20 比 5 日前低",
            "处在近 120 日高低区间的 35% 以下",
        ),
        "need": 3,
    },
    {
        "id": "ma_crossunder_sell_v1",
        "name": "均线死叉",
        "core": (
            "MA5 不高于 MA10，允许差 0.08 倍 ATR",
            "近 5 日跌幅在 1.2 到 3.5 倍当日 ATR% 之间",
            "收盘在 MA20 下，但不超过 2.5 倍 ATR",
        ),
        "optional": (
            "5 日量比不低于 1.2",
            "RSI 在 28 和 50 之间",
            "MA20 比昨天低",
        ),
        "need": 2,
    },
    {
        "id": "rsi_overbought_sell_v1",
        "name": "RSI超买",
        "core": (
            "收盘在 MA10 上",
            "RSI(6) 高于 75，或 RSI(14) 高于 65",
            "出现流星线或看跌吞没",
        ),
        "optional": (
            "5 日量比高于 1.3",
            "收盘比 MA20 高 1 到 3 倍 ATR",
        ),
        "need": 1,
    },
    {
        "id": "overextended_sell_v1",
        "name": "超涨回落",
        "core": (
            "收盘比 MA20 高至少 2.5 倍 ATR",
            "RSI(14) 高于 70，或 RSI(6) 高于 80",
        ),
        "optional": (
            "出现流星线或看跌吞没",
            "前 5 日均量低于前 10 日的 85%，当天量比昨天多 15%",
        ),
        "need": 1,
    },
)


def _sell_strategies(bars: list[dict]) -> list[dict]:
    """Four sell rules judged on the bars alone. Core checks must all pass and at least
    `need` optional checks."""
    closes = [bar["c"] for bar in bars]
    highs = [bar["h"] for bar in bars]
    lows = [bar["l"] for bar in bars]
    volumes = [bar["v"] for bar in bars]
    last = bars[-1]
    opened = last.get("o", last["c"])
    close = last["c"]
    ma5 = mean(closes[-5:]) if len(closes) >= 5 else None
    ma10 = mean(closes[-10:]) if len(closes) >= 10 else None
    ma20 = mean(closes[-20:]) if len(closes) >= 20 else None
    ma20_prev = mean(closes[-21:-1]) if len(closes) >= 21 else None
    ma20_5ago = mean(closes[-25:-5]) if len(closes) >= 25 else None
    atrs = wilder_atr(bars)
    atr = atrs[-1] if atrs else None
    atr_pct = (atr / close * 100) if atr and close else None
    atr_dist = ((close - ma20) / atr) if atr and ma20 is not None else None
    avg5 = mean(volumes[-6:-1]) if len(volumes) >= 6 else 0.0
    vol_ratio_5 = (volumes[-1] / avg5) if avg5 else 0.0
    avg20 = mean(volumes[-21:-1]) if len(volumes) >= 21 else 0.0
    vol_ratio_20 = (volumes[-1] / avg20) if avg20 else 0.0
    prior_low = min(lows[-21:-1]) if len(lows) >= 21 else None
    span = last["h"] - last["l"]
    yin_body = ((opened - close) / span) if span else 0.0
    fall_5 = ((1 - closes[-1] / closes[-6]) * 100) if len(closes) >= 6 and closes[-6] else None
    rsi14 = _rsi(closes, 14)
    rsi6 = _rsi(closes, 6)
    pattern = _bearish_pattern(bars)
    widths = _bandwidths(closes)
    prior_widths = [value for value in widths[-4:-1] if value is not None]
    prior_pct = min(prior_widths) * 100 if prior_widths else None
    width_floor = _percentile([value for value in widths[-120:] if value is not None], 0.20)
    floor_pct = width_floor * 100 if width_floor is not None else None
    squeezed = prior_pct is not None and atr_pct is not None and (
        prior_pct <= 3.8 * atr_pct or (floor_pct is not None and prior_pct <= floor_pct)
    )
    window = min(120, len(highs))
    low_120 = min(lows[-window:]) if window else None
    high_120 = max(highs[-window:]) if window else None
    if low_120 is None or high_120 is None or high_120 == low_120 or window < 30:
        position = None
    else:
        position = (close - low_120) / (high_120 - low_120)
    avg10_prev = mean(volumes[-11:-1]) if len(volumes) >= 11 else 0.0
    shrink_then_expand = (
        bool(avg5 and avg10_prev)
        and avg5 < avg10_prev * 0.85
        and len(volumes) >= 2
        and volumes[-1] > volumes[-2] * 1.15
    )
    live = {
        "breakdown_sell_v1": (
            [prior_low is not None and close < prior_low, squeezed],
            [
                vol_ratio_20 >= 1.2,
                yin_body >= 0.45,
                ma20 is not None and ma20_5ago is not None and ma20 < ma20_5ago,
                position is not None and position <= 0.35,
            ],
        ),
        "ma_crossunder_sell_v1": (
            [
                ma5 is not None and ma10 is not None and atr is not None and ma5 <= ma10 + 0.08 * atr,
                atr_pct is not None and fall_5 is not None and 1.2 * atr_pct <= fall_5 <= 3.5 * atr_pct,
                atr_dist is not None and -2.5 <= atr_dist < 0,
            ],
            [
                vol_ratio_5 >= 1.2,
                rsi14 is not None and 28 < rsi14 < 50,
                ma20 is not None and ma20_prev is not None and ma20 < ma20_prev,
            ],
        ),
        "rsi_overbought_sell_v1": (
            [
                ma10 is not None and close > ma10,
                (rsi6 is not None and rsi6 > 75) or (rsi14 is not None and rsi14 > 65),
                pattern is not None,
            ],
            [vol_ratio_5 > 1.3, atr_dist is not None and 1 <= atr_dist <= 3],
        ),
        "overextended_sell_v1": (
            [
                atr_dist is not None and atr_dist >= 2.5,
                (rsi14 is not None and rsi14 > 70) or (rsi6 is not None and rsi6 > 80),
            ],
            [pattern is not None, shrink_then_expand],
        ),
    }
    packs = []
    for rule in SELL_RULE_CATALOG:
        core_ok, optional_ok = live[rule["id"]]
        opt_hits = sum(1 for ok in optional_ok if ok)
        packs.append(
            {
                "id": rule["id"],
                "name": rule["name"],
                "ready": all(core_ok) and opt_hits >= rule["need"],
                "need": rule["need"],
                "opt_hits": opt_hits,
                "core": [{"label": label, "ok": bool(ok)} for label, ok in zip(rule["core"], core_ok)],
                "optional": [{"label": label, "ok": bool(ok)} for label, ok in zip(rule["optional"], optional_ok)],
            }
        )
    return packs


def _dollar_chip(day_ret: float | None, dollar: float | None, avg_dollar: float | None) -> str | None:
    """Turnover versus the last 20 sessions. Shared by the price-page note and the chart tags."""
    if day_ret is None or not avg_dollar or not dollar:
        return None
    if day_ret > 0.005 and dollar < avg_dollar * 0.8:
        return "squeeze"
    if abs(day_ret) < 0.005 and dollar > avg_dollar * 1.8:
        return "distribute"
    if day_ret < -0.005 and dollar < avg_dollar * 0.8:
        return "shrink_down"
    return None


def _flow(rets: list[float], amounts: list[float]) -> str:
    if len(rets) < 1 or len(amounts) < 20:
        return "中性"
    tag = _dollar_chip(rets[-1], amounts[-1], mean(amounts[-20:]))
    return {"squeeze": "缩量推升", "distribute": "放量滞涨", "shrink_down": "缩量下跌"}.get(tag or "", "中性")


def _ema_series(values: list[float], period: int) -> list[float | None]:
    """EMA seeded with the first `period` observations' average."""
    out: list[float | None] = [None] * len(values)
    if len(values) < period or period < 1:
        return out
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    weight = 2 / (period + 1)
    for index in range(period, len(values)):
        prev = values[index] * weight + prev * (1 - weight)
        out[index] = prev
    return out


def _macd_last(closes: list[float]) -> tuple[float | None, float | None, float | None]:
    """DIF, DEA and the histogram for 12, 26, 9. The bar is 2 × (DIF − DEA)."""
    fast = _ema_series(closes, 12)
    slow = _ema_series(closes, 26)
    difs = [fast[i] - slow[i] for i in range(len(closes)) if fast[i] is not None and slow[i] is not None]
    if len(difs) < 9:
        return (difs[-1], None, None) if difs else (None, None, None)
    dea = _ema_series(difs, 9)[-1]
    dif = difs[-1]
    if dea is None:
        return dif, None, None
    return dif, dea, 2 * (dif - dea)


def _measure(
    bars: list[dict], live: float | None, symbol: str | None = None, with_conditions: bool = False
) -> dict | None:
    if len(bars) < 21:
        return None
    closes = [bar["c"] for bar in bars]
    highs = [bar["h"] for bar in bars]
    lows = [bar["l"] for bar in bars]
    vols = [bar["v"] for bar in bars]
    amounts = [bar["dol"] for bar in bars]
    rets = [
        (closes[i] / closes[i - 1] - 1) if closes[i - 1] else 0.0
        for i in range(1, len(closes))
    ]
    window = closes[-20:]
    ma20 = mean(window)
    sd20 = stdev(window)
    bandwidth = (4 * sd20 / ma20) if ma20 else None
    bandwidths = []
    for i in range(19, len(closes)):
        chunk = closes[i - 19 : i + 1]
        mid = mean(chunk)
        bandwidths.append((4 * stdev(chunk) / mid) if mid else None)
    tail = [value for value in bandwidths[-120:] if value is not None]
    bw_min = min(tail) if tail else None
    if bandwidth is None or bw_min is None:
        squeeze = "无"
    elif bandwidth <= bw_min * 1.001:
        squeeze = "120日新低"
    elif bandwidth <= bw_min * 1.10:
        squeeze = "接近新低"
    else:
        squeeze = "未挤压"

    typical = [(highs[i] + lows[i] + closes[i]) / 3 for i in range(len(closes))]
    vol20 = sum(vols[-20:])
    vwap20 = (
        sum(price * volume for price, volume in zip(typical[-20:], vols[-20:])) / vol20
        if vol20
        else None
    )
    vol = _ann_vol(rets[-20:])
    r20 = (closes[-1] / closes[-21] - 1) if len(closes) >= 21 and closes[-21] else None
    mom20 = (r20 / vol) if (r20 is not None and vol) else None
    illiq = None
    if vols[-1] > 0 and rets:
        dollar_100m = closes[-1] * vols[-1] / 1e8
        if dollar_100m > 0:
            illiq = abs(rets[-1]) * 100 / dollar_100m
    recent = bars[-20:]
    low_bar = min(recent, key=lambda bar: (bar["l"], bar["d"]))
    anchored = [bar for bar in bars if low_bar["d"] <= bar["d"] <= bars[-1]["d"]]
    anchor_denom = sum(bar["v"] for bar in anchored)
    anchor = (
        sum((bar["h"] + bar["l"] + bar["c"]) / 3 * bar["v"] for bar in anchored) / anchor_denom
        if anchor_denom
        else None
    )
    price = live if live and live > 0 else closes[-1]
    swing_range = _range_move(bars, price)
    mom = mom20
    day = rets[-1] if rets else None
    avg_dollar = mean(amounts[-20:]) if len(amounts) >= 20 else None
    tag = _dollar_chip(day, amounts[-1] if amounts else None, avg_dollar)
    signal = tag if tag in {"squeeze", "distribute", "shrink_down"} else None
    ma5 = mean(closes[-5:])
    ma10 = mean(closes[-10:])
    macd = _macd_last(closes)
    rsi = _rsi(closes)
    stop_candle = _stop_candle(bars)

    return {
        "as_of": bars[-1]["d"],
        "open": bars[-1].get("o", closes[-1]),
        "high": highs[-1],
        "low": lows[-1],
        "close": closes[-1],
        "ma5": ma5,
        "ma10": ma10,
        "ma20": ma20,
        "ma_order": _ma_order(ma5, ma10, ma20),
        "boll_upper": ma20 + 2 * sd20,
        "boll_lower": ma20 - 2 * sd20,
        "macd_dif": macd[0],
        "macd_dea": macd[1],
        "macd": macd[2],
        "vwap20": vwap20,
        "vs_ma20": (price / ma20 - 1) if ma20 else None,
        "vs_vwap": (price / vwap20 - 1) if vwap20 else None,
        "vol": vol,
        "r20": r20,
        "mom": mom,
        "illiq": illiq,
        "volume": vols[-1],
        "turnover": amounts[-1],
        "bandwidth": bandwidth,
        "squeeze": squeeze,
        "signal": signal,
        "rsi": rsi,
        "stop_candle": stop_candle,
        "strategies": _alpha_strategies(
            bars, rsi, stop_candle, is_etf(symbol or ""), with_conditions=with_conditions
        ),
        "flow": _flow(rets, amounts),
        "day_ret": rets[-1] if rets else None,
        "oct_low": low_bar["l"] if low_bar else None,
        "oct_low_date": low_bar["d"] if low_bar else None,
        "anchor_vwap": anchor,
        "vs_anchor": (price / anchor - 1) if anchor else None,
        "price": price,
        "drawdown_pct": None if swing_range is None else swing_range["drawdown_pct"],
        "rebound_pct": None if swing_range is None else swing_range["rebound_pct"],
        "swing_high_date": None if swing_range is None else swing_range["high_date"],
        "swing_low_date": None if swing_range is None else swing_range["low_date"],
    }


def _ma_order(ma5: float | None, ma10: float | None, ma20: float | None) -> str | None:
    if ma5 is None or ma10 is None or ma20 is None:
        return None
    if ma5 > ma10 > ma20:
        return "多头"
    if ma5 < ma10 < ma20:
        return "空头"
    return "交错"


def _ma_sentence(order: str | None) -> str:
    if order == "多头":
        return "均线是多头排列，MA5 高于 MA10，MA10 高于 MA20。"
    if order == "空头":
        return "均线是空头排列，MA5 低于 MA10，MA10 低于 MA20。"
    if order == "交错":
        return "均线交错，没有排成多头或空头。"
    return ""


def _pe_sentence(pe: float | None) -> str:
    if pe is None:
        return ""
    if pe < 0:
        return f"滚动市盈率 {pe:.2f}，是负的。"
    return f"滚动市盈率 {pe:.2f}。"


def _stage(stats: dict, leverage: float | None) -> tuple[str, str]:
    if leverage is not None and leverage < 0:
        return "对冲腿", "info"
    if (
        leverage is not None
        and abs(leverage) >= 2
        and (stats.get("bandwidth") or 0) > 0.8
        and (stats.get("vs_vwap") or 0) < 0
    ):
        return "杠杆磨损", "warning"
    if (stats.get("vs_ma20") or 0) < -0.15 and (stats.get("day_ret") or 0) > 0:
        return "超跌反弹", "warning"
    if stats.get("squeeze") in {"120日新低", "接近新低"}:
        return "带宽收敛", "info"
    if (stats.get("vs_vwap") or 0) >= 0:
        return "站上成本", "success"
    return "成本下方", "neutral"


def _advice_view(stats: dict, holding: dict, leverage: float | None, bars: list[dict], pe: float | None = None) -> dict:
    """One conclusion per indicator, plus the stop the user set."""
    price = stats["price"]
    short = leverage is not None and leverage < 0
    vs_anchor = stats.get("vs_anchor")
    vs_ma = stats.get("vs_ma20")
    vs_vwap = stats.get("vs_vwap")
    mom = stats.get("mom")
    bandwidth = stats.get("bandwidth")
    flow = stats.get("flow") or "中性"
    squeeze = stats.get("squeeze") or "未挤压"
    nav_w = holding["nav_weight"]
    cap = holding.get("vol_cap")
    swing = _close_swing(bars)
    defense = _chandelier_state(bars, price, short, stats.get("anchor_vwap"))
    headline = _opening(
        short=short,
        ticker=holding["ticker"],
        vs_anchor=vs_anchor,
        mom=mom,
        swing=swing,
        chandelier_lost=defense["lost"],
        extreme_day=defense["extreme_day"],
        leverage=leverage,
        bandwidth=bandwidth,
        vs_vwap=vs_vwap,
        ma5=stats.get("ma5"),
        ma10=stats.get("ma10"),
        ma20=stats.get("ma20"),
        pe=pe,
    )
    if cap is not None and nav_w > cap + 0.02:
        headline = (
            f"按 20% 波动目标，这只单独最多占净值 {cap * 100:.0f}%，"
            f"现在 {nav_w * 100:.0f}%。先减到上限里，再谈加仓。"
        )

    sections: list[dict] = []
    order = stats.get("ma_order")
    if stats.get("ma5") is not None and stats.get("ma10") is not None and stats.get("ma20") is not None:
        sections.append(
            {
                "title": "均线",
                "trend": (
                    f"MA5 {_px(stats['ma5'])}，MA10 {_px(stats['ma10'])}，MA20 {_px(stats['ma20'])}。"
                ),
                "advice": _ma_sentence(order) or "均线还没排出来。",
            }
        )
    pe_text = _pe_sentence(pe)
    if pe_text:
        sections.append({"title": "PE", "trend": pe_text, "advice": "只作估值对照，不单独决定买卖。"})
    if stats.get("anchor_vwap") is not None:
        if vs_ma is not None and vs_ma < 0:
            ma_bit = f"20 日均线 {_px(stats['ma20'])} 还在现价上方。"
        elif stats.get("ma20") is not None:
            ma_bit = f"现价已经在 20 日均线 {_px(stats['ma20'])} 之上。"
        else:
            ma_bit = ""
        side = "上方" if (vs_anchor or 0) >= 0 else "下方"
        if short:
            defense = "反向仓收在锚定成本上方，对冲腿还有效。这不是看多信号。"
        elif (vs_anchor or 0) >= 0 and (vs_ma or 0) < 0:
            defense = "底仓可以守这条绿线。20 日均线没站上之前，不加到满仓。"
        elif (vs_anchor or 0) >= 0:
            defense = "收盘守住锚定成本，近 20 日进场的资金整体还在。跌破再减。"
        else:
            defense = "收盘已经在锚定成本下方，这段防守丢了。先减，不在这里加。"
        sections.append(
            {
                "title": "防守线",
                "trend": (
                    f"近 20 日低点 {_px(stats['oct_low'])}（{stats['oct_low_date']}）。"
                    f"从那天锚定的成本 {_px(stats['anchor_vwap'])}，现价在{side} {_pct(vs_anchor)}。"
                    f"{ma_bit}"
                ),
                "advice": defense,
            }
        )

    mom_text = "—" if mom is None else f"{mom:.2f}"
    if short:
        momentum = "反向仓的动量和多头相反。正的动量说明对冲在赚钱，不是加多头的理由。"
    elif mom is None:
        momentum = "样本不够，动量先不看。"
    elif mom > 0.15:
        momentum = "动量在正的一侧，而且不弱。顺势拿，不在加速那天追。"
    elif mom > 0:
        momentum = "动量刚到正的一侧。可以拿着，加仓等回踩锚定成本。"
    elif swing == "up":
        momentum = "20 日动量仍是负的，窗口里还有更早的下跌。这几天只持有，不把反弹加成满仓。"
    elif mom > -0.15:
        momentum = "动量在负的一侧，力度还不大。只留底仓或网格，禁止追高。"
    else:
        momentum = "动量明显为负。这是下跌段，反弹不追。"
    sections.append(
        {
            "title": "动量",
            "trend": (
                f"近 20 日 {_pct(stats['r20'])}，年化波动 {_pct(stats['vol'], 0).replace('+', '')}，"
                f"风险调整动量 {mom_text}。"
            ),
            "advice": momentum,
        }
    )

    if squeeze in {"120日新低", "接近新低"}:
        band = "带宽收到很窄。方向没选出来之前不追，收盘离开窄带再决定。"
    elif (bandwidth or 0) >= 0.40 and (vs_vwap or 0) < -0.03:
        band = "带宽偏宽，现价又在 20 日成本下方。来回空间大，不在这里加满。"
    elif (bandwidth or 0) >= 0.40:
        band = "带宽偏宽，价格来回的空间大。仓位用波动上限卡住。"
    else:
        band = "带宽不算宽。沿着现有方向拿，跌破锚定成本再减。"
    if leverage is not None and abs(leverage) >= 2 and stats.get("vol"):
        drag = 0.5 * abs(leverage) * (abs(leverage) - 1) * stats["vol"] ** 2
        band += f" {abs(leverage):.0f} 倍杠杆按当前波动粗算，年化磨损约 {drag * 100:.0f}%。"
    sections.append(
        {
            "title": "带宽",
            "trend": f"布林带宽 {_pct(bandwidth, 0).replace('+', '')}，{squeeze}。",
            "advice": band,
        }
    )

    day_ret = stats.get("day_ret")
    if day_ret is None or abs(day_ret) < 0.0005:
        day_move = "几乎平盘"
    elif day_ret > 0:
        day_move = f"涨 {_pct(day_ret)}"
    else:
        day_move = f"跌 {_pct(day_ret)}"
    if flow == "缩量推升":
        chip_trend = f"最近一个完整交易日{day_move}，成交额低于近 20 日。"
        chip = "少量买盘就把价格推上去了，抛压偏轻。持有可以，不要在放量滞涨那天追。"
    elif flow == "放量滞涨":
        chip_trend = "最近一个完整交易日放量，但几乎没涨。"
        chip = "放量却几乎没涨。不在这一天加。"
    elif flow == "缩量下跌":
        chip_trend = f"最近一个完整交易日{day_move}，成交额低于近 20 日。"
        chip = "卖盘还没放完。不抄，等放量止跌。"
    else:
        chip_trend = f"最近一个完整交易日{day_move}，成交和近 20 日差不多。"
        chip = "成交额没有明显的缩量上涨或放量滞涨。按防守线和动量决定，不因为这一天加减。"
    sections.append({"title": "筹码", "trend": chip_trend, "advice": chip})

    bits = [f"占净值 {nav_w * 100:.0f}%"]
    if cap is not None:
        bits.append(f"按 20% 波动目标最多 {cap * 100:.0f}%")
    if holding.get("rp_weight") is not None and not short:
        bits.append(
            f"多头仓里占 {holding['long_weight'] * 100:.0f}%，"
            f"按 1/波动大约 {holding['rp_weight'] * 100:.0f}%"
        )
    if cap is not None and nav_w > cap + 0.02:
        size = "单名已经超过波动上限。先减到上限里。"
    elif nav_w < 0.02:
        size = "仓位不到净值的 2%，对账户波动几乎没有影响。"
    elif holding.get("rp_weight") is not None and not short:
        gap = holding["long_weight"] - holding["rp_weight"]
        if gap > 0.08:
            size = "在多头仓里这份偏重。新增仓位先给更轻的名字。"
        elif gap < -0.08:
            size = "在多头仓里这份偏轻。要加也只加到波动平价附近，不一次补满。"
        else:
            size = "和波动平价接近，仓位可以维持。"
    else:
        size = "按现价计的仓位，没有额外的加减结论。"
    sections.append({"title": "仓位", "trend": "，".join(bits) + "。", "advice": size})

    stop = holding.get("stop_price")
    stop_hit = False
    if stop and price:
        scope = "覆盖全部仓位" if holding.get("stop_qty") is None else f"只覆盖 {holding['stop_qty']:g} 股"
        dist = (price - stop) / price
        stop_hit = dist < 0
        if stop_hit:
            stop_trend = f"现价 {_px(price)} 已经低于止损 {_px(stop)}，{scope}。"
            stop_advice = "按设定执行，不在止损下方加仓。"
        else:
            stop_trend = f"止损 {_px(stop)}，{scope}，距现价还有 {dist * 100:.1f}%。"
            stop_advice = "还没碰到。收盘跌破这个价格再执行。"
    else:
        stop = None
        stop_trend = "还没设止损。"
        stop_advice = (
            "仓位已经不是零头。先写下一个价格和覆盖股数。"
            if nav_w >= 0.02
            else "仓位很小。要留着，也写下一个价格。"
        )
    sections.append({"title": "止损", "trend": stop_trend, "advice": stop_advice})

    return {
        "headline": headline,
        "sections": sections,
        "stop_price": stop,
        "stop_hit": stop_hit,
        "lines": [f"{item['title']}：{item['trend']}{item['advice']}" for item in sections],
    }


def _book_vol(series: dict[str, dict[str, float]], weights: dict[str, float]) -> float | None:
    usable = {symbol: bars for symbol, bars in series.items() if bars and weights.get(symbol)}
    if len(usable) < 2:
        return None
    days = None
    for bars in usable.values():
        keys = set(bars)
        days = keys if days is None else days & keys
    if not days or len(days) < 12:
        return None
    ordered = sorted(days)[-21:]
    rets = []
    for prev, day in zip(ordered, ordered[1:]):
        ret = 0.0
        for symbol, bars in usable.items():
            if bars[prev] <= 0:
                continue
            ret += weights[symbol] * (bars[day] / bars[prev] - 1)
        rets.append(ret)
    return _ann_vol(rets)


def _yield_bar(day: str, value: float) -> dict:
    return {"d": day, "o": value, "h": value, "l": value, "c": value, "v": 0, "dol": 0}


def _iso_day(raw: str) -> str | None:
    text = raw.strip().strip('"')
    if len(text) >= 10 and text[4] == "-":
        return text[:10]
    parts = text.split("/")
    if len(parts) != 3:
        return None
    month, day, year = parts
    try:
        return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
    except ValueError:
        return None


def _us10y_points() -> list[dict]:
    """Completed US 10-year yields. FRED first, Treasury month file if FRED is silent."""
    import urllib.request
    from datetime import datetime

    fred = _read_text(
        urllib.request.Request(
            "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10",
            headers={"User-Agent": "stock-ledger"},
        ),
        timeout=3,
    )
    points: list[dict] = []
    if fred:
        for line in fred.splitlines()[1:]:
            parts = line.split(",")
            if len(parts) < 2 or parts[1].strip() in {"", "."}:
                continue
            day = _iso_day(parts[0])
            try:
                value = float(parts[1])
            except ValueError:
                continue
            if day:
                points.append(_yield_bar(day, value))
        if points:
            return points

    month = datetime.utcnow().strftime("%Y%m")
    treasury = _read_text(
        urllib.request.Request(
            "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
            f"daily-treasury-rates.csv/all/{month}"
            f"?type=daily_treasury_yield_curve&field_tdr_date_value_month={month}",
            headers={"User-Agent": "stock-ledger"},
        )
    )
    if not treasury:
        return []
    rows = [line for line in treasury.splitlines() if line.strip()]
    if len(rows) < 2:
        return []
    header = rows[0].split(",")
    try:
        column = next(i for i, name in enumerate(header) if "10 Yr" in name)
    except StopIteration:
        return []
    for line in rows[1:]:
        cells = line.split(",")
        if len(cells) <= column:
            continue
        day = _iso_day(cells[0])
        raw = cells[column].strip().strip('"')
        if not day or raw in {"", "N/A"}:
            continue
        try:
            points.append(_yield_bar(day, float(raw)))
        except ValueError:
            continue
    return points


def _fetch_us10y(db: Session | None = None) -> float | None:
    """Latest stored US 10-year yield. Past days are written once."""
    if db is None:
        points = _us10y_points()
        return points[-1]["c"] if points else None
    from app.services.bars import load_bars

    bars = load_bars(db, "US10Y", _us10y_points)
    return bars[-1]["c"] if bars else None


def _read_text(request, timeout: int = 8) -> str | None:
    import urllib.request

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8", errors="replace")
    except Exception:
        return None


def _pe_from_fields(fields: list[str]) -> float | None:
    if len(fields) < 48:
        return None
    try:
        price = float(fields[3])
        pe = float(fields[39])
        eps = float(fields[47])
    except ValueError:
        return None
    if price <= 0 or eps == 0 or pe == 0:
        return None
    if abs(price / eps - pe) / abs(pe) > 0.2:
        return None
    return pe


def _fetch_pe(tickers: list[str]) -> dict[str, float]:
    """Trailing PE from Tencent quotes, kept only when price / EPS matches it."""
    from app.services.market import _request_text

    symbols = [ticker for ticker in tickers if ticker]
    if not symbols:
        return {}
    try:
        text = _request_text("https://qt.gtimg.cn/q=" + ",".join(f"us{ticker}" for ticker in symbols))
    except Exception:
        return {}
    found: dict[str, float] = {}
    for ticker in symbols:
        match = re.search(rf'v_us{re.escape(ticker)}="([^"]*)"', text, flags=re.IGNORECASE)
        if not match:
            continue
        pe = _pe_from_fields(match.group(1).split("~"))
        if pe is not None:
            found[ticker.upper()] = pe
    missing = [ticker for ticker in symbols if ticker.upper() not in found]
    if missing:
        from app.services.data_sources import supplement_pe

        for ticker, (pe, eps, price) in supplement_pe(missing).items():
            if price > 0 and eps and pe and abs(price / eps - pe) / abs(pe) <= 0.2:
                found[ticker.upper()] = pe
    return found


def _fetch_listed_pe(pairs: list[tuple[str, str]]) -> dict[str, float]:
    """Same PE check for sh/sz/hk quote codes. Keyed by ticker."""
    from app.services.market import _request_text

    if not pairs:
        return {}
    try:
        text = _request_text("https://qt.gtimg.cn/q=" + ",".join(code for _, code in pairs))
    except Exception:
        return {}
    found: dict[str, float] = {}
    for ticker, code in pairs:
        match = re.search(rf'v_{re.escape(code)}="([^"]*)"', text, flags=re.IGNORECASE)
        if not match:
            continue
        pe = _pe_from_fields(match.group(1).split("~"))
        if pe is not None:
            found[ticker.upper()] = pe
    missing = [
        f"HKEX:{ticker}"
        for ticker, code in pairs
        if code.lower().startswith("hk") and ticker.upper() not in found
    ]
    if missing:
        from app.services.data_sources import supplement_pe

        for ticker, (pe, eps, price) in supplement_pe(missing).items():
            if price > 0 and eps and pe and abs(price / eps - pe) / abs(pe) <= 0.2:
                found[ticker.upper()] = pe
    return found


def _metrics(stats: dict | None, row: dict, pe: float | None, us10y: float | None) -> dict:
    empty = {
        "illiq": None,
        "illiq_date": None,
        "volume": None,
        "turnover": None,
        "anchor_date": None,
        "anchor_vwap": None,
        "vs_anchor": None,
        "ma20": None,
        "ma5": None,
        "ma10": None,
        "ma_order": None,
        "bandwidth": None,
        "vol": None,
        "mom": None,
        "vol_cap": None,
        "rp_weight": None,
        "pe": None,
        "erp": None,
        "signals": [],
    }
    if not stats:
        return empty
    erp = None
    if pe and pe > 0 and us10y is not None:
        erp = (1 / pe - us10y / 100) * 100
    return {
        "illiq": None if stats.get("illiq") is None else round(stats["illiq"], 3),
        "illiq_date": stats.get("as_of"),
        "volume": None if stats.get("volume") is None else round(stats["volume"]),
        "turnover": None if stats.get("turnover") is None else round(stats["turnover"]),
        "anchor_date": stats.get("oct_low_date"),
        "anchor_vwap": None if stats.get("anchor_vwap") is None else round(stats["anchor_vwap"], 2),
        "vs_anchor": None if stats.get("vs_anchor") is None else round(stats["vs_anchor"] * 100, 1),
        "ma20": None if stats.get("ma20") is None else round(stats["ma20"], 2),
        "ma5": None if stats.get("ma5") is None else round(stats["ma5"], 2),
        "ma10": None if stats.get("ma10") is None else round(stats["ma10"], 2),
        "ma_order": stats.get("ma_order"),
        "bandwidth": None if stats.get("bandwidth") is None else round(stats["bandwidth"] * 100, 1),
        "vol": None if stats.get("vol") is None else round(stats["vol"] * 100, 1),
        "mom": None if stats.get("mom") is None else round(stats["mom"], 2),
        "vol_cap": None if row.get("vol_cap") is None else round(row["vol_cap"] * 100, 1),
        "rp_weight": None if row.get("rp_weight") is None else round(row["rp_weight"] * 100, 1),
        "pe": None if pe is None else round(pe, 2),
        "erp": None if erp is None else round(erp, 2),
        # Only the rules whose hard checks all pass and whose optional gate is met.
        "signals": [
            {
                "id": pack["id"],
                "name": pack["name"],
                "detail": pack.get("detail") or "",
                "exit": pack.get("exit") or "",
                "exit_now": pack.get("exit_now") or "",
            }
            for pack in (stats.get("strategies") or [])
            if pack.get("ready")
        ],
    }


def build_holdings_advice(db: Session, portfolio_id: int = 1) -> dict:
    book = compute_holdings(db, portfolio_id, include_closed=True)
    stops = {
        row.symbol: row
        for row in db.query(PositionStop)
        .filter(PositionStop.portfolio_id == portfolio_id)
        .all()
    }
    holdings_value = _f(book.get("total_market_value")) or 0.0
    cash = _f(book.get("cash")) or 0.0
    nav = holdings_value + cash
    open_rows = [
        item
        for item in book.get("items", [])
        if (_f(item.get("quantity")) or 0) > 0 and (_f(item.get("market_value")) or 0) > 0
    ]
    closed_rows = [item for item in book.get("items", []) if item.get("closed")]
    from app.models.entities import SymbolMeta
    from app.services.market import get_quotes
    from app.services.market_scope import current_market, market_of
    from app.services.symbols import quote_code
    from app.services.watchlist import watch_symbols

    known = {item["symbol"].upper() for item in open_rows}
    known.update(item["symbol"].upper() for item in closed_rows)
    pending = [symbol for symbol in watch_symbols(current_market()) if symbol.upper() not in known]
    quotes = get_quotes(db, pending, background_refresh=False)
    names = {
        row.symbol: row.name
        for row in db.query(SymbolMeta).filter(SymbolMeta.symbol.in_(pending)).all()
    } if pending else {}
    for symbol, quote in quotes.items():
        if quote.get("price") is None:
            continue
        open_rows.append(
            {
                "symbol": symbol,
                "name": quote.get("name") or names.get(symbol),
                "last_price": quote.get("price"),
                "market_value": 0,
                "quantity": 0,
            }
        )
    for item in closed_rows:
        open_rows.append(
            {
                "symbol": item["symbol"],
                "name": item.get("name"),
                "last_price": item.get("last_price"),
                "market_value": 0,
                "quantity": 0,
            }
        )
    symbols = [item["symbol"] for item in open_rows]
    tickers = [item["symbol"].split(":")[-1] for item in open_rows]
    us10y = _fetch_us10y(db) if current_market() == "us" else None
    pe_by_ticker = _fetch_pe([item["symbol"].split(":")[-1] for item in open_rows if market_of(item["symbol"]) == "us"])
    pe_by_ticker.update(
        _fetch_listed_pe(
            [
                (item["symbol"].split(":")[-1], quote_code(item["symbol"]))
                for item in open_rows
                if market_of(item["symbol"]) in {"cn", "hk"} and quote_code(item["symbol"])
            ]
        )
    )
    bars_by_symbol: dict[str, list[dict]] = {}
    if symbols:
        with ThreadPoolExecutor(max_workers=min(8, len(symbols))) as pool:
            for symbol, bars in zip(symbols, pool.map(_load_bars, symbols)):
                bars_by_symbol[symbol] = bars

    # Rules read the bar series, not the `live` scalar, so today's session bar has to be
    # in the series for an intraday trigger to show up. Fetch US prints in ONE batched
    # call — a per-symbol fetch here means N network round-trips on every advice poll.
    from app.services.bars import append_open_bar, bar_key, chart_session_bar, open_session_bars
    from app.services.ledger import us_equity_session_date
    from app.services.market_scope import market_of

    session = us_equity_session_date().isoformat()
    us_pending = [
        symbol
        for symbol in symbols
        if market_of(symbol) == "us"
        and (not bars_by_symbol.get(symbol) or bars_by_symbol[symbol][-1]["d"] < session)
    ]
    session_bars = open_session_bars(sorted(set(us_pending)), when_closed=True) if us_pending else {}

    prepared = []
    for item in open_rows:
        ticker = item["symbol"].split(":")[-1]
        mv = _f(item.get("market_value")) or 0.0
        stop = stops.get(item["symbol"])
        live = _f(item.get("last_price"))
        series = bars_by_symbol.get(item["symbol"]) or []
        if market_of(item["symbol"]) == "us":
            series = append_open_bar(series, session_bars.get(bar_key(item["symbol"])))
        else:
            series = append_open_bar(series, chart_session_bar(item["symbol"], series))
        stats = _measure(series, live, item["symbol"])
        vol = stats["vol"] if stats else None
        prepared.append(
            {
                "symbol": item["symbol"],
                "ticker": ticker,
                "name": item.get("name"),
                "mv": mv,
                "nav_weight": (mv / nav) if nav else 0.0,
                "book_weight": (mv / holdings_value) if holdings_value else 0.0,
                "leverage": LEVERAGE.get(ticker),
                "vol": vol,
                "vol_cap": (TARGET_VOL / vol) if vol else None,
                "stop_price": None if stop is None or stop.stop_price is None else float(stop.stop_price),
                "stop_qty": None if stop is None or stop.stop_qty is None else float(stop.stop_qty),
                "stats": stats,
                "last": live if live else (stats["price"] if stats else None),
            }
        )

    longs = [
        row
        for row in prepared
        if row["mv"] > 0 and (row["leverage"] is None or row["leverage"] >= 0)
    ]
    long_mv = sum(row["mv"] for row in longs) or 1.0
    inv = []
    for row in longs:
        row["long_weight"] = row["mv"] / long_mv
        inv.append((1 / row["vol"]) if row["vol"] else 0.0)
    inv_sum = sum(inv) or 1.0
    for row, weight in zip(longs, inv):
        row["rp_weight"] = weight / inv_sum
    for row in prepared:
        row.setdefault("long_weight", None)
        row.setdefault("rp_weight", None)

    close_series = {
        row["symbol"]: {bar["d"]: bar["c"] for bar in bars_by_symbol.get(row["symbol"], [])}
        for row in prepared
    }
    stock_weights = {
        row["symbol"]: row["book_weight"] for row in prepared if row["stats"]
    }
    book_vol = _book_vol(close_series, stock_weights)
    equity_weight = (holdings_value / nav) if nav else None
    cash_weight = (cash / nav) if nav else None
    account_vol = (
        book_vol * equity_weight if book_vol is not None and equity_weight is not None else None
    )
    equity_cap = (TARGET_VOL / book_vol) if book_vol else None

    items = []
    as_of = None
    for row in sorted(prepared, key=lambda item: item["mv"], reverse=True):
        stats = row["stats"]
        pe = pe_by_ticker.get(row["ticker"].upper())
        if stats is None:
            items.append(
                {
                    "symbol": row["symbol"],
                    "ticker": row["ticker"],
                    "name": row["name"],
                    "last": row["last"],
                    "stage": "无日线",
                    "tone": "neutral",
                    "headline": "没有拉到足够的日线。",
                    "sections": [],
                    "stop_price": row["stop_price"],
                    "stop_hit": False,
                    "lines": ["没有拉到足够的日线，均线、波动和 VWAP 都算不了。"],
                    "metrics": _metrics(None, row, pe, us10y),
                }
            )
            continue
        as_of = max(as_of or stats["as_of"], stats["as_of"])
        stage, tone = _stage(stats, row["leverage"])
        if row["vol_cap"] is not None and row["nav_weight"] > row["vol_cap"] + 0.02:
            stage, tone = "单名超限", "danger"
        view = _advice_view(stats, row, row["leverage"], bars_by_symbol.get(row["symbol"]) or [], pe)
        items.append(
            {
                "symbol": row["symbol"],
                "ticker": row["ticker"],
                "name": row["name"],
                "last": row["last"],
                "stage": stage,
                "tone": tone,
                **view,
                "metrics": _metrics(stats, row, pe, us10y),
            }
        )

    points: list[str] = []
    heavy = [
        row
        for row in longs
        if row.get("rp_weight") is not None and row["long_weight"] - row["rp_weight"] > 0.08
    ]
    if heavy:
        names = "、".join(
            f"{row['ticker']}（多头仓 {row['long_weight'] * 100:.0f}%，波动平价约 {row['rp_weight'] * 100:.0f}%）"
            for row in sorted(heavy, key=lambda item: item["long_weight"], reverse=True)
        )
        points.append(f"多头里偏重的是 {names}。")
    under_vwap = [
        row["ticker"]
        for row in longs
        if row["stats"] and (row["stats"].get("vs_vwap") or 0) < 0
    ]
    above_vwap = [
        row["ticker"]
        for row in longs
        if row["stats"] and (row["stats"].get("vs_vwap") or 0) >= 0
    ]
    if under_vwap:
        points.append(
            f"{'、'.join(under_vwap)} 的现价还在 20 日 VWAP 下面。"
            + (f"{'、'.join(above_vwap)} 在上面。" if above_vwap else "多头没有一只站上 20 日成本。")
        )
    moms = [
        row["stats"]["mom"]
        for row in longs
        if row["stats"] and row["stats"].get("mom") is not None
    ]
    if len(moms) >= 3 and max(moms) < 0 and max(moms) - min(moms) <= 0.15:
        points.append(
            f"多头近 20 日的风险调整动量挤在 {min(moms):.2f} 到 {max(moms):.2f}，"
            "更像同一轮按波动减仓，而不是各自基本面分开走。"
        )
    hedges = [row["ticker"] for row in prepared if row["leverage"] is not None and row["leverage"] < 0]
    if hedges:
        points.append(f"{'、'.join(hedges)} 是反向工具，不放进多头的波动平价。")

    if book_vol is None or equity_weight is None or account_vol is None or equity_cap is None:
        headline = "日线不够，账户波动和仓位上限算不出来。"
    else:
        headline = (
            f"股票仓近 20 日年化波动 {book_vol * 100:.0f}%，现金 {cash_weight * 100:.0f}%，"
            f"账户大约 {account_vol * 100:.0f}%。按 20% 的波动目标，股票仓大约应占净值 "
            f"{equity_cap * 100:.0f}%（现在 {equity_weight * 100:.0f}%）。"
        )

    return {
        "as_of": as_of,
        "us10y": None if us10y is None else round(us10y, 2),
        "target_vol": TARGET_VOL,
        "headline": headline,
        "points": points,
        "account_vol": account_vol,
        "book_vol": book_vol,
        "equity_weight": equity_weight,
        "equity_cap": equity_cap,
        "cash_weight": cash_weight,
        "items": items,
    }


def _history_point(stats: dict) -> dict:
    def rounded(key: str, digits: int):
        value = stats.get(key)
        return None if value is None else round(value, digits)

    return {
        "date": stats["as_of"],
        "open": rounded("open", 2),
        "high": rounded("high", 2),
        "low": rounded("low", 2),
        "close": rounded("close", 2),
        "volume": None if stats.get("volume") is None else round(stats["volume"]),
        "turnover": None if stats.get("turnover") is None else round(stats["turnover"]),
        "illiq": rounded("illiq", 3),
        "ma20": rounded("ma20", 2),
        "boll_upper": rounded("boll_upper", 2),
        "boll_lower": rounded("boll_lower", 2),
        "macd_dif": rounded("macd_dif", 4),
        "macd_dea": rounded("macd_dea", 4),
        "macd": rounded("macd", 4),
        "ma5": rounded("ma5", 2),
        "ma10": rounded("ma10", 2),
        "ma_order": stats.get("ma_order"),
        "vs_ma20": None if stats.get("vs_ma20") is None else round(stats["vs_ma20"] * 100, 1),
        "anchor_date": stats.get("oct_low_date"),
        "anchor_vwap": rounded("anchor_vwap", 2),
        "vs_anchor": None if stats.get("vs_anchor") is None else round(stats["vs_anchor"] * 100, 1),
        "bandwidth": None if stats.get("bandwidth") is None else round(stats["bandwidth"] * 100, 1),
        "vol": None if stats.get("vol") is None else round(stats["vol"] * 100, 1),
        "mom": rounded("mom", 2),
        "signal": stats.get("signal"),
        "vol_squeeze": stats.get("squeeze") == "120日新低",
        "rsi": rounded("rsi", 1),
        "stop_candle": stats.get("stop_candle"),
        "strategies": stats.get("strategies") or [],
        "drawdown_pct": stats.get("drawdown_pct"),
        "rebound_pct": stats.get("rebound_pct"),
        "swing_high_date": stats.get("swing_high_date"),
        "swing_low_date": stats.get("swing_low_date"),
    }


def _wilder_atr(bars: list[dict], period: int = 22) -> float | None:
    """Welles Wilder ATR: first value is the mean of `period` true ranges, then smoothed."""
    if len(bars) < period + 1:
        return None
    trs: list[float] = []
    for i in range(1, len(bars)):
        high, low, prev = bars[i]["h"], bars[i]["l"], bars[i - 1]["c"]
        trs.append(max(high - low, abs(high - prev), abs(low - prev)))
    if len(trs) < period:
        return None
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def _shift(points: list[dict], key: str, back: int = 5) -> float | None:
    if len(points) <= back:
        return None
    latest, earlier = points[-1].get(key), points[-1 - back].get(key)
    if latest is None or earlier is None:
        return None
    return latest - earlier


def _delta_phrase(delta: float | None, step: float, up: str, down: str) -> str:
    if delta is None:
        return "近几日样本不够，看不出方向"
    if delta >= step:
        return up
    if delta <= -step:
        return down
    return "近 5 个交易日基本没变"


def _gap_shift(points: list[dict], key: str, back: int = 5) -> float | None:
    if len(points) <= back:
        return None
    latest, earlier = points[-1].get(key), points[-1 - back].get(key)
    if latest is None or earlier is None:
        return None
    return abs(latest) - abs(earlier)


def _swing_clause(swing: str) -> str:
    if swing == "up":
        return "较 5 个交易日前收高"
    if swing == "down":
        return "较 5 个交易日前收低"
    return "较 5 个交易日前基本持平"


def _chandelier_state(bars: list[dict], price: float | None, short: bool, avwap: float | None) -> dict:
    """22-session extreme ± 3 × Wilder ATR. The level formula stays fixed."""
    period = 22
    empty = {
        "atr": None,
        "chandelier": None,
        "lost": False,
        "defense_lost": False,
        "extreme_day": None,
        "extreme_px": None,
        "stop_price": None,
        "target_price": None,
    }
    atr = _wilder_atr(bars, period)
    if atr is None or len(bars) < period or not price:
        return empty
    window = bars[-period:]
    high_bar = _extreme_bar(window, "h")
    low_bar = _extreme_bar(window, "l")
    highest, lowest = high_bar["h"], low_bar["l"]
    extreme_bar = low_bar if short else high_bar
    extreme_px = lowest if short else highest
    chandelier = (lowest + 3 * atr) if short else (highest - 3 * atr)
    if short:
        defense_lost = avwap is not None and price > avwap
        lost = price >= chandelier
        candidates = [level for level in (chandelier, avwap) if level is not None and level > price]
    else:
        defense_lost = avwap is not None and price < avwap
        lost = price <= chandelier
        candidates = [level for level in (chandelier, avwap) if level is not None and level < price]
    stop_price = None
    if defense_lost:
        stop_price = avwap
    elif candidates:
        stop_price = min(candidates) if short else max(candidates)
    target_price = None
    risk = abs(price - stop_price) if stop_price is not None else None
    if risk and risk > 0 and not defense_lost:
        target_price = price - 2 * risk if short else price + 2 * risk
    return {
        "atr": atr,
        "chandelier": chandelier,
        "lost": lost,
        "defense_lost": defense_lost,
        "extreme_day": str(extreme_bar["d"])[:10],
        "extreme_px": extreme_px,
        "stop_price": stop_price,
        "target_price": target_price,
    }


def _regime_headline(*, short: bool, ticker: str, vs_anchor, mom, swing: str, chandelier_lost: bool, extreme_day: str | None) -> str:
    if short:
        return f"{ticker} 是反向工具。下面的止损按空头吊灯算，站上它自己的成本不是看多信号。"
    lead = _swing_clause(swing)
    above = vs_anchor is not None and vs_anchor >= 0
    mom_pos = mom is not None and mom > 0
    parts = [lead + "。"]
    if swing == "down" and chandelier_lost and extreme_day:
        parts.append(
            f"从 {extreme_day} 高点的回撤已经超过 3 倍 ATR（22 个交易日窗口），吊灯止损破了，不再加仓。"
        )
    elif chandelier_lost and extreme_day:
        parts.append(
            f"22 个交易日里的高点在 {extreme_day}，现价离那个高点仍超过 3 倍 ATR，旧吊灯还在上方。"
        )
    if above and mom_pos and swing != "down":
        parts.append("价格在锚定成本上方，动量为正。这是右侧趋势，持有可以，加仓不能超过波动上限。")
    elif above and mom_pos:
        parts.append("价格还在锚定成本上方，20 日动量仍为正。右侧还在，这几天不加仓。")
    elif above and swing == "up":
        parts.append("价格在锚定成本上方。20 日动量仍是负的，窗口里还有更早的下跌。只持有，不把这几天当成趋势转正。")
    elif above:
        parts.append("价格还在锚定成本上方，但动量已经转负。趋势在变弱，只持有，不追。")
    elif swing == "up":
        parts.append("价格还在锚定成本下方，20 日动量仍是负的，窗口里还有更早的下跌。这是下跌之后的反弹，不是趋势转多。")
    elif vs_anchor is not None and (mom is None or mom < 0):
        parts.append("防守线在下方，动量也是负的。这是下跌趋势里的左侧，先处理离场，不谈加仓。")
    else:
        parts.append("防守线已经失守。按规则先减，动量是否回正再决定还留不留底仓。")
    if above and chandelier_lost:
        parts.append("底仓的止损改看防守线。")
    return "".join(parts)


def _opening(**kwargs) -> str:
    headline = _regime_headline(
        short=kwargs["short"],
        ticker=kwargs["ticker"],
        vs_anchor=kwargs["vs_anchor"],
        mom=kwargs["mom"],
        swing=kwargs["swing"],
        chandelier_lost=kwargs["chandelier_lost"],
        extreme_day=kwargs["extreme_day"],
    )
    leverage = kwargs.get("leverage")
    bandwidth = kwargs.get("bandwidth")
    vs_vwap = kwargs.get("vs_vwap")
    if (
        not kwargs["short"]
        and leverage is not None
        and abs(leverage) >= 2
        and (bandwidth or 0) > 0.8
        and (vs_vwap or 0) < 0
    ):
        headline = (
            headline
            + "杠杆品种还在 20 日滚动成本下方，带宽又很宽。这段来回震荡会磨损，不适合当成趋势仓一直拿。"
        )
    alignment = _ma_sentence(_ma_order(kwargs.get("ma5"), kwargs.get("ma10"), kwargs.get("ma20")))
    if alignment:
        headline += alignment
    pe_text = _pe_sentence(kwargs.get("pe"))
    if pe_text:
        headline += pe_text
    return headline


def _close_swing(bars: list[dict], back: int = 5, step: float = 0.01) -> str:
    """Direction of the last few closes. Separate from the 20-day regime."""
    if len(bars) <= back or not bars[-1 - back]["c"]:
        return "flat"
    delta = bars[-1]["c"] / bars[-1 - back]["c"] - 1
    if delta >= step:
        return "up"
    if delta <= -step:
        return "down"
    return "flat"


def _range_move(bars: list[dict], price: float | None) -> dict | None:
    """Drawdown from the 22-session high and rebound from the 22-session low."""
    if not price or price <= 0 or len(bars) < 22:
        return None
    window = bars[-22:]
    high_bar = _extreme_bar(window, "h")
    low_bar = _extreme_bar(window, "l")
    high, low = high_bar["h"], low_bar["l"]
    if not high or not low or high <= 0 or low <= 0:
        return None
    return {
        "high_date": str(high_bar["d"])[:10],
        "low_date": str(low_bar["d"])[:10],
        "drawdown_pct": round((high - price) / high * 100, 1),
        "rebound_pct": round((price - low) / low * 100, 1),
    }


def _extreme_bar(window: list[dict], key: str) -> dict:
    level = max(bar[key] for bar in window) if key == "h" else min(bar[key] for bar in window)
    return [bar for bar in window if bar[key] == level][-1]


def _trend_brief(bars: list[dict], points: list[dict], ticker: str, pe: float | None = None) -> dict | None:
    """Conclusions by indicator, plus Chandelier stop and a 2R target.

    Stop: Chuck LeBeau / StockCharts Chandelier Exit, 22-day extreme minus
    (or plus) 3 × Wilder ATR(22). Target: twice the distance from the last
    price to the stop that would be hit first. The anchored VWAP remains the
    separate defense line already used on the chart.
    """
    if not points:
        return None
    last = points[-1]
    price = last.get("close")
    if not price:
        return None
    swing = _close_swing(bars)
    measured = _measure(bars, None) or {}
    leverage = LEVERAGE.get(ticker.upper())
    short = leverage is not None and leverage < 0
    mom = last.get("mom")
    vs_anchor = last.get("vs_anchor")
    bandwidth = last.get("bandwidth")
    ma20 = last.get("ma20")
    avwap = last.get("anchor_vwap")
    anchor_way = _delta_phrase(
        _gap_shift(points, "vs_anchor"),
        2.0,
        "距锚较 5 个交易日前扩大",
        "距锚较 5 个交易日前收窄",
    )
    band_way = _delta_phrase(
        _shift(points, "bandwidth"),
        3.0,
        "带宽较 5 个交易日前变宽",
        "带宽较 5 个交易日前变窄",
    )
    defense = _chandelier_state(bars, price, short, measured.get("anchor_vwap"))

    squeezes = [p["date"] for p in points if p.get("signal") == "squeeze"]
    distributes = [p["date"] for p in points if p.get("signal") == "distribute"]
    if squeezes and (not distributes or squeezes[-1] >= distributes[-1]):
        chip_trend = f"这 30 个交易日有 {len(squeezes)} 次缩量上涨，最近一次是 {squeezes[-1]}。"
        chip_advice = "缩量上涨还在，持有可以，不要在放量滞涨那天追。"
    elif distributes:
        chip_trend = f"这 30 个交易日有 {len(distributes)} 次放量几乎不涨，最近一次是 {distributes[-1]}。"
        chip_advice = "最近的放量没涨是派发，反弹减仓，不把缩量反弹当成新趋势。"
    else:
        chip_trend = "这 30 个交易日没有缩量上涨，也没有放量滞涨。"
        chip_advice = "筹码上没有极端信号，仓位跟着防守线和动量走。"

    if bandwidth is None:
        band_trend = "带宽还没算出来。"
        band_advice = "等满 20 个交易日再看是不是在蓄势。"
    elif last.get("vol_squeeze"):
        band_trend = f"带宽 {bandwidth:.1f}%，是近 120 个交易日最窄的。{band_way}。"
        band_advice = "带宽收到极窄，方向还没选出来。突破再加，跌破防守线就走，不要在窄幅里来回做。"
    elif _shift(points, "bandwidth") is not None and _shift(points, "bandwidth") <= -3:
        band_trend = f"带宽 {bandwidth:.1f}%。{band_way}。"
        band_advice = "波动在收，还没到 120 日新低。仓位维持，等它收得更窄或重新张开。"
    else:
        band_trend = f"带宽 {bandwidth:.1f}%。{band_way}。"
        if bandwidth >= 40 and mom is not None and mom > 0:
            band_advice = "带宽偏宽，价格来回的空间大。趋势还在右侧，拿着可以，不适合在中间加满。"
        elif bandwidth >= 40:
            band_advice = "带宽偏宽，价格来回的空间大。动量不在右侧，不在宽幅里加仓。"
        else:
            band_advice = "带宽不算极窄，也还没收到 120 日新低。仓位跟着防守线和动量。"

    if vs_anchor is None or avwap is None:
        anchor_trend = "锚定成本还没算出来。"
        anchor_advice = "没有防守线之前，不按这套规则加仓。"
    elif short and vs_anchor >= 0:
        anchor_trend = f"收盘在锚定成本 {_px(avwap)} 上方 {vs_anchor:+.1f}%。{anchor_way}。"
        anchor_advice = "反向仓的防守线在成本上方。收盘站上这条线，空头失效，减对冲，不是看多加仓。"
    elif short:
        anchor_trend = f"收盘在锚定成本 {_px(avwap)} 下方 {vs_anchor:+.1f}%。{anchor_way}。"
        anchor_advice = "价格还在这条线下面，反向仓的防守还在。收盘重新站上就减。"
    elif vs_anchor >= 0:
        anchor_trend = f"收盘在锚定成本 {_px(avwap)} 上方 {vs_anchor:+.1f}%。{anchor_way}。"
        anchor_advice = "收盘守住这条绿线才算机构持仓还在。收盘跌破就按防守线离场。"
    else:
        anchor_trend = f"收盘在锚定成本 {_px(avwap)} 下方 {vs_anchor:+.1f}%。{anchor_way}。"
        anchor_advice = "防守线已经失守。这是离场或只留底仓，不是等反弹加仓。"

    if mom is None:
        mom_trend = "动量还没算出来。"
        mom_advice = "没有 20 日风险调整动量之前，不区分左右侧。"
    swing_text = _swing_clause(swing)
    if mom is None:
        mom_trend = "动量还没算出来。"
        mom_advice = "没有 20 日风险调整动量之前，不区分左右侧。"
    elif mom > 0 and swing == "down":
        mom_trend = f"风险调整动量 {mom:+.2f}，20 日还在正的一侧。{swing_text}。"
        mom_advice = "20 日动量还没转负。这几天先不加，等收盘重新抬高。"
    elif mom > 0:
        mom_trend = f"风险调整动量 {mom:+.2f}，还在正的一侧。{swing_text}。"
        mom_advice = "动量允许顺着右侧拿。动量转负之前，不加到波动上限以上。"
    elif swing == "up":
        mom_trend = f"风险调整动量 {mom:+.2f}，20 日仍在负的一侧。{swing_text}。"
        mom_advice = "负的是 20 日窗口，里面还装着更早的下跌。这几天只持有，不把反弹加成满仓。"
    else:
        mom_trend = f"风险调整动量 {mom:+.2f}，在负的一侧。{swing_text}。"
        mom_advice = "这是左侧。只留底仓或网格，禁止追高。"

    atr = defense["atr"]
    stop_price = defense["stop_price"]
    target_price = defense["target_price"]
    defense_lost = defense["defense_lost"]
    chandelier_lost = defense["lost"]
    extreme_day = defense["extreme_day"]
    level_note = "止损用 Chuck LeBeau 的吊灯出场：22 个交易日的极值减去或加上 3 倍 Wilder ATR。止盈是现价到先执行止损的距离再乘 2。这是离场价，不是预测能涨到那里。"
    if atr is None:
        stop_text = "日线不够 22 根，吊灯止损算不出来。有锚定成本的话，收盘跌破那条线就走。"
        target_text = "止损还没算出来，先不给 2 倍风险的止盈。"
    else:
        chandelier = defense["chandelier"]
        extreme_px = defense["extreme_px"]
        side = "空头" if short else "多头"
        recovering = (short and swing == "down") or (not short and swing == "up")
        stop_bits = [
            f"{side}吊灯止损 {_px(chandelier)}（{extreme_day} 的{'低点' if short else '高点'} {_px(extreme_px)}，"
            f"22 个交易日窗口，{'+' if short else '−'} 3×ATR {_px(atr)}）。"
        ]
        if avwap is not None:
            stop_bits.append(f"锚定防守线 {_px(avwap)}。")
        if defense_lost:
            stop_bits.append("收盘已经在防守线外侧，规则是先离场，不再等吊灯，也不设新的止盈。")
        elif chandelier_lost and stop_price is not None and recovering:
            stop_bits.append(
                f"{swing_text}。吊灯还在现价{'下方' if short else '上方'}，"
                f"是 {extreme_day} 那个{'低点' if short else '高点'}留下的，不是这几天走出来的。"
                f"底仓还没破防守线，止损改看 {_px(stop_price)}，收盘过去就走。"
            )
        elif chandelier_lost and stop_price is not None:
            stop_bits.append(
                f"吊灯已经在现价{'下方' if short else '上方'}，这段趋势的跟踪止损触发了，不再加仓。"
                f"底仓还没破防守线，止损改看 {_px(stop_price)}，收盘过去就走。"
            )
        elif stop_price is not None:
            if avwap is not None and abs(stop_price - avwap) <= abs(stop_price - chandelier):
                which = "防守线"
            else:
                which = "吊灯"
            stop_bits.append(f"两道都在现价{'上方' if short else '下方'}。先执行更近的{which} {_px(stop_price)}，收盘过去就走。")
        stop_text = "".join(stop_bits)
        if target_price is not None:
            nearer = ""
            if ma20 is not None and (
                (not short and ma20 > price and ma20 < target_price)
                or (short and ma20 < price and ma20 > target_price)
            ):
                nearer = f"20 日均线 {_px(ma20)} 更近，到均线先减一档，剩下的再看到 {_px(target_price)}。"
            target_text = f"止盈按现价到止损的 2 倍风险放在 {_px(target_price)}。{nearer}"
        else:
            target_text = "当前不新开止盈。先处理已经碰到的止损。"

    headline = _opening(
        short=short,
        ticker=ticker,
        vs_anchor=measured.get("vs_anchor"),
        mom=measured.get("mom"),
        swing=swing,
        chandelier_lost=chandelier_lost,
        extreme_day=extreme_day,
        leverage=leverage,
        bandwidth=measured.get("bandwidth"),
        vs_vwap=measured.get("vs_vwap"),
        ma5=measured.get("ma5"),
        ma10=measured.get("ma10"),
        ma20=measured.get("ma20"),
        pe=pe,
    )
    if measured.get("drawdown_pct") is not None and measured.get("rebound_pct") is not None:
        headline += (
            f"近 22 个交易日从 {measured.get('swing_high_date')} 高点回撤 {measured['drawdown_pct']:.1f}%，"
            f"从 {measured.get('swing_low_date')} 低点反弹 {measured['rebound_pct']:.1f}%。"
        )

    ma_trend = (
        f"MA5 {_px(measured.get('ma5'))}，MA10 {_px(measured.get('ma10'))}，MA20 {_px(ma20)}。"
        if measured.get("ma5") is not None
        else "短均线还没算出来。"
    )
    pe_text = _pe_sentence(pe)
    sections = [
        {"title": "均线", "trend": ma_trend, "advice": _ma_sentence(measured.get("ma_order")) or "均线还没排出来。"},
    ]
    if pe_text:
        sections.append({"title": "PE", "trend": pe_text, "advice": "只作估值对照，不单独决定买卖。"})
    sections.extend(
        [
            {"title": "防守线", "trend": anchor_trend, "advice": anchor_advice},
            {"title": "动量", "trend": mom_trend, "advice": mom_advice},
            {"title": "带宽", "trend": band_trend, "advice": band_advice},
            {"title": "筹码", "trend": chip_trend, "advice": chip_advice},
            {"title": "止损止盈", "trend": stop_text, "advice": target_text},
        ]
    )

    return {
        "headline": headline,
        "sections": sections,
        "stop_price": None if stop_price is None else round(stop_price, 2),
        "stop_hit": bool(defense_lost) if atr is not None else False,
        "target_price": None if target_price is None else round(target_price, 2),
        "level_note": level_note,
    }


def _symbol_pe(symbol: str) -> float | None:
    from app.services.market_scope import market_of
    from app.services.symbols import quote_code

    ticker = (symbol or "").split(":")[-1]
    if not ticker:
        return None
    try:
        if market_of(symbol) == "us":
            found = _fetch_pe([ticker])
        else:
            code = quote_code(symbol)
            found = _fetch_listed_pe([(ticker, code)]) if code else {}
    except Exception:
        return None
    return found.get(ticker.upper())


def build_indicator_history(symbol: str, before: str | None = None, limit: int = 40) -> dict:
    """One page of daily indicators from stored bars, newest page first.

    The window starts on 1 April of the current year. Callers walk backward
    with `before` (exclusive) so a long history is not built in one response.
    Today's session bar is attached on the newest page when stored bars stop
    before that session, and that print is not written.
    """
    from app.services.bars import append_open_bar, chart_session_bar, chart_since

    limit = max(20, min(int(limit or 40), 80))
    since = chart_since().isoformat()
    cutoff = str(before)[:10] if before else ""
    newest = not cutoff

    bars = _load_bars(symbol)
    if newest:
        bars = append_open_bar(bars, chart_session_bar(symbol, bars))
    eligible = [
        index
        for index, bar in enumerate(bars)
        if index >= 20 and since <= str(bar["d"])[:10] and (not cutoff or str(bar["d"])[:10] < cutoff)
    ]
    page = eligible[-limit:]
    points = []
    for index in page:
        stats = _measure(bars[: index + 1], None, symbol, with_conditions=True)
        if stats:
            points.append(_history_point(stats))
    # Imported here because ak_backtest imports this module.
    from app.services.ak_backtest import advice_marks

    advice = advice_marks(symbol, bars, since)
    day_index = {str(bar["d"])[:10]: index for index, bar in enumerate(bars)}

    def sells_at(index: int) -> list[dict]:
        return _sell_strategies(bars[max(0, index - 139) : index + 1]) if index >= 24 else []

    for point in points:
        index = day_index.get(point["date"])
        sells = sells_at(index) if index is not None else []
        before_ready = {pack["id"] for pack in sells_at(index - 1) if pack["ready"]} if index else set()
        # A sell rule is marked on the day it turns ready. The day before is read from the
        # stored bars, so the mark is the same on every page.
        fresh = [pack["name"] for pack in sells if pack["ready"] and pack["id"] not in before_ready]
        point["sells"] = sells
        point["advice"] = list(advice.get(point["date"], []))
        if fresh:
            point["advice"].append({"side": "S", "kind": "rule", "rule": "、".join(fresh), "reason": "卖出规则成立"})
    ticker = symbol.split(":")[-1]
    recent = points[-30:]
    return {
        "symbol": symbol,
        "ticker": ticker,
        "since": since,
        "before": points[0]["date"] if points else None,
        "has_more": len(eligible) > len(page),
        "points": points,
        "brief": _trend_brief(bars, recent, ticker, _symbol_pe(symbol)) if newest else None,
    }


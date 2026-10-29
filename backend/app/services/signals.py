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
from statistics import mean, median, stdev

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


def _flow(rets: list[float], amounts: list[float]) -> str:
    if len(rets) < 5:
        return "中性"
    illiq: list[float | None] = []
    for i, ret in enumerate(rets):
        dollar = amounts[i + 1]
        illiq.append(abs(ret) / dollar if dollar > 0 else None)
    last = illiq[-1]
    base_vals = [x for x in illiq[-60:-20] if x is not None]
    base = median(base_vals) if base_vals else None
    avg_dollar = mean(amounts[-20:]) if amounts else 0
    last_dollar = amounts[-1]
    last_ret = rets[-1]
    if (
        last
        and base
        and last_ret > 0.005
        and last > base * 2
        and avg_dollar
        and last_dollar < avg_dollar * 0.8
    ):
        return "缩量推升"
    if abs(last_ret) < 0.005 and avg_dollar and last_dollar > avg_dollar * 1.8:
        return "放量滞涨"
    if (
        last
        and base
        and last_ret < -0.005
        and last > base * 2
        and avg_dollar
        and last_dollar < avg_dollar * 0.8
    ):
        return "缩量下跌"
    return "中性"


def _measure(bars: list[dict], live: float | None) -> dict | None:
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
    mom = mom20
    vol_ma20 = mean(vols[-20:]) if len(vols) >= 20 else None
    day = rets[-1] if rets else None
    signal = None
    if day is not None and vol_ma20 and illiq is not None and vols[-1] > 0:
        if day > 0.03 and vols[-1] < vol_ma20 and illiq > 1.0:
            signal = "squeeze"
        elif vols[-1] > 2 * vol_ma20 and -0.01 < day < 0.015 and illiq < 0.1:
            signal = "distribute"

    return {
        "as_of": bars[-1]["d"],
        "open": bars[-1].get("o", closes[-1]),
        "high": highs[-1],
        "low": lows[-1],
        "close": closes[-1],
        "ma20": ma20,
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
        "flow": _flow(rets, amounts),
        "day_ret": rets[-1] if rets else None,
        "oct_low": low_bar["l"] if low_bar else None,
        "oct_low_date": low_bar["d"] if low_bar else None,
        "anchor_vwap": anchor,
        "vs_anchor": (price / anchor - 1) if anchor else None,
        "price": price,
    }


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


def _advice_view(stats: dict, holding: dict, leverage: float | None) -> dict:
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

    if short:
        headline = "这是反向仓。它站上自己的成本，只说明对冲腿在起作用，不是看多加仓信号。"
    elif leverage is not None and abs(leverage) >= 2 and (bandwidth or 0) > 0.8 and (vs_vwap or 0) < 0:
        headline = "杠杆品种还在均线下方，带宽又很宽。这段来回震荡会磨损，不适合当成趋势仓一直拿。"
    elif (vs_ma or 0) < -0.15 and (stats.get("day_ret") or 0) > 0:
        headline = "离 20 日均线还远，今天的上涨只是超跌反弹，不是趋势确认。"
    elif squeeze in {"120日新低", "接近新低"}:
        headline = "带宽收到近 120 日低位。方向还没选，不追，等收盘离开这条窄带再动。"
    elif (vs_vwap or 0) >= 0 and (mom or 0) > 0:
        headline = "现价在 20 日成本上方，动量也在正的一侧。守住锚定成本就可以拿，跌破再减。"
    elif (vs_vwap or 0) >= 0:
        headline = "现价还在 20 日成本上方，但动量不强。只留底仓，不加满。"
    elif (vs_anchor or 0) >= 0:
        headline = "20 日滚动成本还在现价上方，锚定成本已经守住。只留底仓，均线没站上之前不加满。"
    else:
        headline = "现价还在 20 日成本下方，锚定成本也没守住。这是反弹，不适合追高加满。"
    if cap is not None and nav_w > cap + 0.02:
        headline = (
            f"按 20% 波动目标，这只单独最多占净值 {cap * 100:.0f}%，"
            f"现在 {nav_w * 100:.0f}%。先减到上限里，再谈加仓。"
        )

    sections: list[dict] = []
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
        chip = "更像换手派发，不是锁仓。不在这一天加。"
    elif flow == "缩量下跌":
        chip_trend = f"最近一个完整交易日{day_move}，成交额低于近 20 日。"
        chip = "卖盘还没放完。不抄，等放量止跌。"
    else:
        chip_trend = f"最近一个完整交易日{day_move}，成交和近 20 日差不多。"
        chip = "筹码没有明显的锁仓或派发。按防守线和动量决定，不因为这一天加减。"
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
        "bandwidth": None,
        "vol": None,
        "mom": None,
        "vol_cap": None,
        "rp_weight": None,
        "pe": None,
        "erp": None,
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
        "bandwidth": None if stats.get("bandwidth") is None else round(stats["bandwidth"] * 100, 1),
        "vol": None if stats.get("vol") is None else round(stats["vol"] * 100, 1),
        "mom": None if stats.get("mom") is None else round(stats["mom"], 2),
        "vol_cap": None if row.get("vol_cap") is None else round(row["vol_cap"] * 100, 1),
        "rp_weight": None if row.get("rp_weight") is None else round(row["rp_weight"] * 100, 1),
        "pe": None if pe is None else round(pe, 2),
        "erp": None if erp is None else round(erp, 2),
    }


def build_holdings_advice(db: Session, portfolio_id: int = 1) -> dict:
    book = compute_holdings(db, portfolio_id)
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
    from app.models.entities import SymbolMeta
    from app.services.market import get_quotes
    from app.services.market_scope import current_market, market_of
    from app.services.symbols import quote_code
    from app.services.watchlist import watch_symbols

    pending = [symbol for symbol in watch_symbols(current_market()) if symbol.upper() not in {item["symbol"].upper() for item in open_rows}]
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

    prepared = []
    for item in open_rows:
        ticker = item["symbol"].split(":")[-1]
        mv = _f(item.get("market_value")) or 0.0
        stop = stops.get(item["symbol"])
        live = _f(item.get("last_price"))
        stats = _measure(bars_by_symbol.get(item["symbol"]) or [], live)
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
        view = _advice_view(stats, row, row["leverage"])
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
        "vs_ma20": None if stats.get("vs_ma20") is None else round(stats["vs_ma20"] * 100, 1),
        "anchor_date": stats.get("oct_low_date"),
        "anchor_vwap": rounded("anchor_vwap", 2),
        "vs_anchor": None if stats.get("vs_anchor") is None else round(stats["vs_anchor"] * 100, 1),
        "bandwidth": None if stats.get("bandwidth") is None else round(stats["bandwidth"] * 100, 1),
        "vol": None if stats.get("vol") is None else round(stats["vol"] * 100, 1),
        "mom": rounded("mom", 2),
        "signal": stats.get("signal"),
        "vol_squeeze": stats.get("squeeze") == "120日新低",
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


def _way(delta: float | None, step: float) -> str:
    if delta is None:
        return "近几日样本不够，看不出方向"
    if delta >= step:
        return "近 5 个交易日在走高"
    if delta <= -step:
        return "近 5 个交易日在走低"
    return "近 5 个交易日基本横着"


def _trend_brief(bars: list[dict], points: list[dict], ticker: str) -> dict | None:
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
    leverage = LEVERAGE.get(ticker.upper())
    short = leverage is not None and leverage < 0
    mom = last.get("mom")
    vs_anchor = last.get("vs_anchor")
    bandwidth = last.get("bandwidth")
    ma20 = last.get("ma20")
    avwap = last.get("anchor_vwap")
    mom_way = _way(_shift(points, "mom"), 0.03)
    anchor_way = _way(_shift(points, "vs_anchor"), 2.0)
    band_way = _way(_shift(points, "bandwidth"), 3.0)

    squeezes = [p["date"] for p in points if p.get("signal") == "squeeze"]
    distributes = [p["date"] for p in points if p.get("signal") == "distribute"]
    if squeezes and (not distributes or squeezes[-1] >= distributes[-1]):
        chip_trend = f"这 30 个交易日有 {len(squeezes)} 次锁仓逼空，最近一次是 {squeezes[-1]}。"
        chip_advice = "缩量上涨还在，持有可以，不要在放量滞涨那天追。"
    elif distributes:
        chip_trend = f"这 30 个交易日有 {len(distributes)} 次天量滞涨，最近一次是 {distributes[-1]}。"
        chip_advice = "最近的放量没涨是派发，反弹减仓，不把缩量反弹当成新趋势。"
    else:
        chip_trend = "这 30 个交易日没有锁仓逼空，也没有天量滞涨。"
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
    elif mom > 0:
        mom_trend = f"风险调整动量 {mom:+.2f}，还在正的一侧。{mom_way}。"
        mom_advice = "动量允许顺着右侧拿。动量转负之前，不加到波动上限以上。"
    else:
        mom_trend = f"风险调整动量 {mom:+.2f}，在负的一侧。{mom_way}。"
        mom_advice = "这是左侧。只留底仓或网格，禁止追高。"

    period = 22
    atr = _wilder_atr(bars, period)
    stop_price = None
    target_price = None
    defense_lost = False
    chandelier_lost = False
    level_note = "止损用 Chuck LeBeau 的吊灯出场：22 日极值减去或加上 3 倍 Wilder ATR。止盈是现价到先执行止损的距离再乘 2。这是离场价，不是预测能涨到那里。"
    if atr is None or len(bars) < period:
        stop_text = "日线不够 22 根，吊灯止损算不出来。有锚定成本的话，收盘跌破那条线就走。"
        target_text = "止损还没算出来，先不给 2 倍风险的止盈。"
    else:
        window = bars[-period:]
        highest = max(bar["h"] for bar in window)
        lowest = min(bar["l"] for bar in window)
        chandelier = (lowest + 3 * atr) if short else (highest - 3 * atr)
        side = "空头" if short else "多头"
        if short:
            defense_lost = avwap is not None and price > avwap
            chandelier_lost = price >= chandelier
            candidates = [level for level in (chandelier, avwap) if level is not None and level > price]
        else:
            defense_lost = avwap is not None and price < avwap
            chandelier_lost = price <= chandelier
            candidates = [level for level in (chandelier, avwap) if level is not None and level < price]
        if defense_lost:
            stop_price = avwap
        elif candidates:
            stop_price = min(candidates) if short else max(candidates)
        risk = abs(price - stop_price) if stop_price is not None else None
        if risk and risk > 0 and not defense_lost:
            target_price = price - 2 * risk if short else price + 2 * risk
        stop_bits = [
            f"{side}吊灯止损 {_px(chandelier)}（22 日{'低点' if short else '高点'} {'+' if short else '−'} 3×ATR {_px(atr)}）。"
        ]
        if avwap is not None:
            stop_bits.append(f"锚定防守线 {_px(avwap)}。")
        if defense_lost:
            stop_bits.append("收盘已经在防守线外侧，规则是先离场，不再等吊灯，也不设新的止盈。")
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

    if short:
        headline = f"{ticker} 是反向工具。下面的止损按空头吊灯算，站上它自己的成本不是看多信号。"
    elif chandelier_lost and vs_anchor is not None and vs_anchor >= 0:
        headline = "从 22 日高点的回撤已经超过 3 倍 ATR，吊灯止损破了，不再加仓。价格还在锚定成本上方，底仓的止损改看防守线。"
    elif vs_anchor is not None and vs_anchor < 0 and (mom is None or mom < 0):
        headline = "防守线在下方，动量也是负的。这是下跌趋势里的左侧，先处理离场，不谈加仓。"
    elif vs_anchor is not None and vs_anchor >= 0 and mom is not None and mom > 0:
        headline = "价格在锚定成本上方，动量为正。这是右侧趋势，持有可以，加仓不能超过波动上限。"
    elif vs_anchor is not None and vs_anchor >= 0:
        headline = "价格还在锚定成本上方，但动量已经转负。趋势在变弱，只持有，不追。"
    else:
        headline = "防守线已经失守。按规则先减，动量是否回正再决定还留不留底仓。"

    return {
        "headline": headline,
        "sections": [
            {"title": "防守线", "trend": anchor_trend, "advice": anchor_advice},
            {"title": "动量", "trend": mom_trend, "advice": mom_advice},
            {"title": "带宽", "trend": band_trend, "advice": band_advice},
            {"title": "筹码", "trend": chip_trend, "advice": chip_advice},
            {"title": "止损止盈", "trend": stop_text, "advice": target_text},
        ],
        "stop_price": None if stop_price is None else round(stop_price, 2),
        "stop_hit": bool(defense_lost) if atr is not None else False,
        "target_price": None if target_price is None else round(target_price, 2),
        "level_note": level_note,
    }


def build_indicator_history(symbol: str, before: str | None = None, limit: int = 40) -> dict:
    """One page of daily indicators from stored bars, newest page first.

    The window starts on 1 April of the current year. Callers walk backward
    with `before` (exclusive) so a long history is not built in one response.
    Today's open bar is attached only on the newest page and is not stored.
    """
    from app.services.bars import append_open_bar, bar_key, chart_since, open_session_bars

    limit = max(20, min(int(limit or 40), 80))
    since = chart_since().isoformat()
    cutoff = str(before)[:10] if before else ""
    newest = not cutoff
    from app.services.market_scope import market_of

    bars = _load_bars(symbol)
    if newest and market_of(symbol) not in {"cn", "hk"}:
        bars = append_open_bar(bars, open_session_bars([symbol]).get(bar_key(symbol)))
    eligible = [
        index
        for index, bar in enumerate(bars)
        if index >= 20 and since <= str(bar["d"])[:10] and (not cutoff or str(bar["d"])[:10] < cutoff)
    ]
    page = eligible[-limit:]
    points = []
    for index in page:
        stats = _measure(bars[: index + 1], None)
        if stats:
            points.append(_history_point(stats))
    ticker = symbol.split(":")[-1]
    recent = points[-30:]
    return {
        "symbol": symbol,
        "ticker": ticker,
        "since": since,
        "before": points[0]["date"] if points else None,
        "has_more": len(eligible) > len(page),
        "points": points,
        "brief": _trend_brief(bars, recent, ticker) if newest else None,
    }


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
    from app.services.bars import load_sina_bars

    db = SessionLocal()
    try:
        return load_sina_bars(db, symbol)
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


def _lines(stats: dict, holding: dict, leverage: float | None) -> list[str]:
    price = stats["price"]
    mom_text = "—" if stats["mom"] is None else f"{stats['mom']:.2f}"
    lines = [
        (
            f"现价 {_px(price)}，20 日均线 {_px(stats['ma20'])}"
            f"（{_pct(stats['vs_ma20'])}），20 日 VWAP {_px(stats['vwap20'])}"
            f"（{_pct(stats['vs_vwap'])}）。"
        ),
        (
            f"近 20 日 {_pct(stats['r20'])}，20 日年化波动 "
            f"{_pct(stats['vol'], 0).replace('+', '')}，"
            f"风险调整动量 {mom_text}（20 日收益 / 波动），"
            f"布林带宽 {_pct(stats['bandwidth'], 0).replace('+', '')}。"
        ),
    ]
    if stats.get("oct_low") is not None and stats.get("anchor_vwap") is not None:
        if stats["vs_ma20"] is not None and stats["vs_ma20"] < 0:
            pressure = f"20 日均线 {_px(stats['ma20'])} 是上方第一压力。"
        else:
            pressure = f"现价已经在 20 日均线 {_px(stats['ma20'])} 之上。"
        lines.append(
            f"近 20 日低点 {_px(stats['oct_low'])}（{stats['oct_low_date']}）。"
            f"从那天锚定的 VWAP 是 {_px(stats['anchor_vwap'])}"
            f"（现价 {_pct(stats['vs_anchor'])}）。"
            f"{pressure}"
        )
    if stats["flow"] == "缩量推升":
        lines.append(
            f"最近一个完整交易日涨 {_pct(stats['day_ret'])}，成交额低于近 20 日，"
            "少量买盘就把价格推上去了，抛压偏轻。"
        )
    elif stats["flow"] == "放量滞涨":
        lines.append("最近一个完整交易日放量但几乎没涨，更像换手派发，而不是锁仓。")
    elif stats["flow"] == "缩量下跌":
        lines.append("最近一个完整交易日缩量下跌，卖盘还没放完。")

    nav_w = holding["nav_weight"]
    cap = holding["vol_cap"]
    if leverage is not None and leverage < 0:
        lines.append("方向和多头相反。它站上自己的成本，只说明对冲腿在起作用，不是看多加仓信号。")
    elif (stats["vs_vwap"] or 0) < -0.03 and (stats["bandwidth"] or 0) > 0.40:
        lines.append("现价还在 20 日成本下方，带宽也还张着。这是反弹，不是趋势确认，不适合追高加满。")
    elif (stats["vs_anchor"] or 0) > 0.02 and stats["flow"] == "缩量推升":
        lines.append("已经离开近 20 日低点锚定的成本，锁仓反弹成立。碰到 20 日均线再考虑减一点。")
    elif (stats["vs_vwap"] or 0) >= 0:
        lines.append("现价在 20 日 VWAP 之上，近 20 日进场的资金整体还在成本上方。")

    if leverage is not None and abs(leverage) >= 2 and stats["vol"]:
        drag = 0.5 * abs(leverage) * (abs(leverage) - 1) * stats["vol"] ** 2
        chop = ""
        if (stats["vs_vwap"] or 0) < 0 or (stats["bandwidth"] or 0) > 0.8:
            chop = "均线下方或带宽很宽时来回震荡，不适合当成趋势仓一直拿。"
        lines.append(
            f"{abs(leverage):.0f} 倍杠杆按当前波动粗算，年化磨损约 {drag * 100:.0f}%。{chop}"
        )

    if cap is not None and nav_w > cap + 0.02:
        lines.append(
            f"按 20% 波动目标，这只单独最多占净值 {cap * 100:.0f}%，"
            f"现在 {nav_w * 100:.0f}%，单名已经超了。"
        )
    elif holding.get("rp_weight") is not None and (leverage is None or leverage >= 0):
        gap = holding["long_weight"] - holding["rp_weight"]
        if nav_w < 0.02:
            lines.append("仓位不到净值的 2%，对账户波动几乎没有影响。")
        elif gap > 0.08:
            lines.append(
                f"在多头仓里占 {holding['long_weight'] * 100:.0f}%，"
                f"按 1/波动 大约 {holding['rp_weight'] * 100:.0f}%，这份偏重。"
            )
        elif gap < -0.08:
            lines.append(
                f"在多头仓里占 {holding['long_weight'] * 100:.0f}%，"
                f"按 1/波动 大约 {holding['rp_weight'] * 100:.0f}%，这份偏轻。"
            )

    stop = holding.get("stop_price")
    if stop and price:
        scope = "覆盖全部仓位" if holding.get("stop_qty") is None else f"只覆盖 {holding['stop_qty']:g} 股"
        dist = (price - stop) / price
        if dist < 0:
            lines.append(f"现价已经低于止损 {_px(stop)}（{scope}）。")
        else:
            lines.append(f"止损 {_px(stop)} {scope}，距现价还有 {dist * 100:.1f}%。")
    elif nav_w >= 0.02:
        lines.append("这只还没设止损。")
    return lines


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
        fields = match.group(1).split("~")
        if len(fields) < 48:
            continue
        try:
            price = float(fields[3])
            pe = float(fields[39])
            eps = float(fields[47])
        except ValueError:
            continue
        if price <= 0 or eps == 0 or pe == 0:
            continue
        if abs(price / eps - pe) / abs(pe) > 0.2:
            continue
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
    symbols = [item["symbol"] for item in open_rows]
    tickers = [item["symbol"].split(":")[-1] for item in open_rows]
    us10y = _fetch_us10y(db)
    pe_by_ticker = _fetch_pe(tickers)
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

    longs = [row for row in prepared if row["leverage"] is None or row["leverage"] >= 0]
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
                    "lines": ["没有拉到足够的日线，均线、波动和 VWAP 都算不了。"],
                    "metrics": _metrics(None, row, pe, us10y),
                }
            )
            continue
        as_of = max(as_of or stats["as_of"], stats["as_of"])
        stage, tone = _stage(stats, row["leverage"])
        if row["vol_cap"] is not None and row["nav_weight"] > row["vol_cap"] + 0.02:
            stage, tone = "单名超限", "danger"
        items.append(
            {
                "symbol": row["symbol"],
                "ticker": row["ticker"],
                "name": row["name"],
                "last": row["last"],
                "stage": stage,
                "tone": tone,
                "lines": _lines(stats, row, row["leverage"]),
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


def build_indicator_history(symbol: str, days: int = 30) -> dict:
    """Recompute the daily-bar indicators on each of the last `days` sessions."""
    days = max(5, min(int(days or 30), 60))
    from app.services.bars import append_open_bar, bar_key, open_session_bars

    bars = append_open_bar(_load_bars(symbol), open_session_bars([symbol]).get(bar_key(symbol)))
    points = []
    if len(bars) >= 21:
        first = max(21, len(bars) - days + 1)
        for end in range(first, len(bars) + 1):
            stats = _measure(bars[:end], None)
            if stats:
                points.append(_history_point(stats))
    return {
        "symbol": symbol,
        "ticker": symbol.split(":")[-1],
        "days": days,
        "points": points,
    }

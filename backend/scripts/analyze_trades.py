"""Trade journal analysis: replay trades_backup.csv with the same weighted-average
cost basis as app/services/ledger.py and emit metrics for the strategy report.

Usage: python scripts/analyze_trades.py [csv_path] > analysis.json
"""
from __future__ import annotations

import csv
import json
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

CSV_DEFAULT = Path(__file__).resolve().parents[1] / "trades_backup.csv"


@dataclass
class Cycle:
    symbol: str
    started: datetime
    ended: datetime
    buys: int = 0
    sells: int = 0
    buy_qty: float = 0.0
    sell_qty: float = 0.0
    buy_amount: float = 0.0
    sell_amount: float = 0.0
    realized: float = 0.0
    cost: float = 0.0
    # buys made while the position was already underwater vs its running average
    addon_below_avg: int = 0
    peak_qty: float = 0.0

    @property
    def days(self) -> int:
        return max((self.ended - self.started).days, 0)

    @property
    def ret_pct(self) -> float:
        return (self.realized / self.cost * 100) if self.cost else 0.0


@dataclass
class Pos:
    qty: float = 0.0
    avg: float = 0.0
    cost: float = 0.0


def parse_symbol(raw: str) -> str:
    s = (raw or "").strip()
    for p in ("NYSE:", "NASDAQ:", "AMEX:"):
        if s.upper().startswith(p):
            s = s[len(p):]
    return s.upper()


def load(path: Path):
    rows = []
    with path.open(encoding="utf-8-sig", newline="") as fh:
        for i, r in enumerate(csv.DictReader(fh), start=2):
            side = (r["Side"] or "").strip()
            if not side:
                continue
            ts = datetime.strptime(r["Closing Time"].strip(), "%Y/%m/%d %H:%M")
            rows.append(
                dict(
                    id=i,
                    symbol=parse_symbol(r["Symbol"]),
                    side=side,
                    qty=float(r["Qty"]),
                    price=float(r["Fill Price"]),
                    comm=float(r["Commission"] or 0),
                    ts=ts,
                    note=(r.get("Notes") or "").strip(),
                )
            )
    rows.sort(key=lambda x: (x["ts"], x["id"]))
    return rows


def replay(rows):
    state: dict[str, Pos] = {}
    cycles: list[Cycle] = []
    open_cycle: dict[str, Cycle] = {}
    deposits = 0.0
    buy_notional = sell_notional = commissions = 0.0
    same_day_flip = []  # symbols where a buy and a sell share a calendar day
    day_sides: dict[tuple, set] = defaultdict(set)

    for t in rows:
        sym, side = t["symbol"], t["side"]
        commissions += t["comm"]
        if side == "Deposit":
            deposits += t["price"] if t["price"] else t["qty"]
            continue
        if not sym:
            continue
        pos = state.setdefault(sym, Pos())
        notional = t["qty"] * t["price"]

        if side == "Buy":
            if pos.qty <= 0:
                open_cycle[sym] = Cycle(sym, t["ts"], t["ts"])
            c = open_cycle[sym]
            if pos.qty > 0 and t["price"] < pos.avg:
                c.addon_below_avg += 1
            buy_notional += notional
            c.buys += 1
            c.buy_qty += t["qty"]
            c.buy_amount += notional
            new_qty = pos.qty + t["qty"]
            pos.cost += notional
            pos.qty = new_qty
            pos.avg = pos.cost / new_qty if new_qty else 0.0
            c.peak_qty = max(c.peak_qty, pos.qty)
            day_sides[(sym, t["ts"].date())].add("B")

        elif side == "Sell":
            c = open_cycle.get(sym)
            if c is None:  # sell with no position (phantom) - skip cost handling
                c = Cycle(sym, t["ts"], t["ts"])
                open_cycle[sym] = c
            sell_qty = min(t["qty"], pos.qty) if pos.qty > 0 else t["qty"]
            cost_removed = pos.avg * sell_qty
            pnl = sell_qty * t["price"] - cost_removed
            if pos.qty > 0 and t["qty"] > pos.qty:
                pnl += (t["qty"] - pos.qty) * t["price"]
            sell_notional += notional
            c.sells += 1
            c.sell_qty += sell_qty
            c.sell_amount += notional
            c.realized += pnl
            c.cost += cost_removed
            c.ended = t["ts"]
            pos.qty -= sell_qty
            pos.cost = pos.avg * pos.qty
            day_sides[(sym, t["ts"].date())].add("S")
            if pos.qty <= 1e-9:
                pos.qty = pos.cost = pos.avg = 0.0
                cycles.append(c)
                open_cycle.pop(sym, None)

    for entry in day_sides.items():
        (sym, day), sides = entry
        if len(sides) == 2:
            same_day_flip.append({"symbol": sym, "date": day.isoformat()})

    open_positions = [
        {"symbol": s, "qty": p.qty, "avg": p.avg, "cost": p.cost}
        for s, p in state.items() if p.qty > 1e-9
    ]
    return dict(
        cycles=cycles, open_positions=open_positions, deposits=deposits,
        buy_notional=buy_notional, sell_notional=sell_notional,
        commissions=commissions, same_day_flip=same_day_flip,
        open_cycles=list(open_cycle.values()),
    )


def summarize(rows, r):
    closed = [c for c in r["cycles"] if c.cost > 0]
    wins = [c for c in closed if c.realized > 0]
    losses = [c for c in closed if c.realized <= 0]
    realized = sum(c.realized for c in closed)
    win_sum = sum(c.realized for c in wins)
    loss_sum = sum(c.realized for c in losses)
    avg_win = win_sum / len(wins) if wins else 0
    avg_loss = abs(loss_sum / len(losses)) if losses else 0

    by_symbol = defaultdict(lambda: dict(realized=0.0, cost=0.0, cycles=0, days=0.0,
                                         addon_below=0, trades=0))
    for c in closed:
        b = by_symbol[c.symbol]
        b["realized"] += c.realized
        b["cost"] += c.cost
        b["cycles"] += 1
        b["days"] += c.days
        b["addon_below"] += c.addon_below_avg
    for t in rows:
        if t["symbol"] and t["side"] in ("Buy", "Sell"):
            by_symbol[t["symbol"]]["trades"] += 1

    monthly = defaultdict(lambda: dict(trades=0, realized=0.0, wins=0, losses=0))
    for t in rows:
        if t["side"] in ("Buy", "Sell") and t["symbol"]:
            monthly[t["ts"].strftime("%Y-%m")]["trades"] += 1
    for c in closed:
        m = monthly[c.ended.strftime("%Y-%m")]
        m["realized"] += c.realized
        if c.realized > 0:
            m["wins"] += 1
        else:
            m["losses"] += 1

    symbols = []
    for s, b in by_symbol.items():
        symbols.append(dict(
            symbol=s, realized=round(b["realized"], 2), cost=round(b["cost"], 2),
            ret_pct=round(b["realized"] / b["cost"] * 100, 2) if b["cost"] else 0.0,
            cycles=b["cycles"], trades=b["trades"],
            avg_days=round(b["days"] / b["cycles"], 1) if b["cycles"] else 0.0,
            addon_below=b["addon_below"],
        ))
    symbols.sort(key=lambda x: x["realized"])

    # win/loss holding-duration profile
    win_days = [c.days for c in wins]
    loss_days = [c.days for c in losses]

    return dict(
        meta=dict(
            rows=len(rows),
            first=rows[0]["ts"].strftime("%Y-%m-%d"),
            last=rows[-1]["ts"].strftime("%Y-%m-%d"),
            symbols=len({t["symbol"] for t in rows if t["symbol"]}),
        ),
        capital=dict(
            deposits=round(r["deposits"], 2),
            buy_notional=round(r["buy_notional"], 2),
            sell_notional=round(r["sell_notional"], 2),
            turnover_x=round(r["buy_notional"] / r["deposits"], 2) if r["deposits"] else 0,
            commissions=round(r["commissions"], 2),
            realized=round(realized, 2),
            realized_pct=round(realized / r["deposits"] * 100, 2) if r["deposits"] else 0,
        ),
        behavior=dict(
            closed_cycles=len(closed),
            wins=len(wins), losses=len(losses),
            win_rate=round(len(wins) / len(closed) * 100, 1) if closed else 0,
            avg_win=round(avg_win, 2), avg_loss=round(avg_loss, 2),
            payoff=round(avg_win / avg_loss, 2) if avg_loss else 0,
            profit_factor=round(win_sum / abs(loss_sum), 2) if loss_sum else 0,
            win_sum=round(win_sum, 2), loss_sum=round(loss_sum, 2),
            avg_win_days=round(sum(win_days) / len(win_days), 1) if win_days else 0,
            avg_loss_days=round(sum(loss_days) / len(loss_days), 1) if loss_days else 0,
            addon_below_total=sum(c.addon_below_avg for c in closed),
            same_day_flips=len(r["same_day_flip"]),
            same_day_flip_symbols=sorted({x["symbol"] for x in r["same_day_flip"]}),
        ),
        symbols=symbols,
        monthly=[dict(month=m, **v) for m, v in sorted(monthly.items())],
        open_positions=[
            dict(symbol=p["symbol"], qty=round(p["qty"], 4), avg=round(p["avg"], 4),
                 cost=round(p["cost"], 2))
            for p in sorted(r["open_positions"], key=lambda x: -x["cost"])
        ],
        open_cycles=[
            dict(symbol=c.symbol, realized=round(c.realized, 2),
                 addon_below=c.addon_below_avg, days=c.days)
            for c in r["open_cycles"]
        ],
    )


def main():
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else CSV_DEFAULT
    rows = load(path)
    result = summarize(rows, replay(rows))
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()

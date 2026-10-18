"""Import the broker export rows (2026-08-07 .. 2026-10-09) into the trade table.

Dry-run by default; nothing is written until --apply.

    python scripts/import_broker_rows.py                 # review only
    python scripts/import_broker_rows.py --apply         # write

Commission is 0 on every row (as requested). Times are pinned to 23:00:00
unless --keep-broker-time is given: ledger.trade_us_session_date() reads a
naive closing_time as China local, so the time of day has to stay in the
evening for the row to land on the broker's US session date.
Output is ASCII on purpose (Windows consoles here default to GBK).
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.models.entities import DailyNav, SymbolMeta, Trade
from app.services.csv_io import export_csv
from app.services.ledger import trade_us_session_date
from app.services.market import ensure_symbol_meta

# (no, date, broker time, name, ticker, side, price, qty, amount, status, order type)
# Copied verbatim from the broker export the user pasted; row no. matches its first column.
ROWS: list[tuple] = [
    (2, "2026-08-07", None, "XE X-ENERGY", "XE", "买入", "22.65", "13", "294.45", "全部成交", "普通委托"),
    (3, "2026-08-07", None, "DEFIANCE DT 2X LONG RKLB", "RKLX", "买入", "25.1", "16", "401.6", "全部成交", "普通委托"),
    (4, "2026-08-07", "23:10:41", "微软", "MSFT", "买入", "502.65", "0.1949", "97.97", "部成已撤", "定投"),
    (5, "2026-08-07", "23:10:42", "诺基亚", "NOK", "买入", "9.365", "5", "46.83", "全部成交", "定投"),
    (6, "2026-08-07", "23:10:42", "Rocket Lab", "RKLB", "买入", "80.36", "1", "80.36", "全部成交", "定投"),
    (7, "2026-08-10", "23:15:10", "微软", "MSFT", "买入", "508.89", "0.1925", "97.96", "部成已撤", "定投"),
    (8, "2026-08-10", "23:15:10", "Rocket Lab", "RKLB", "买入", "81.8295", "1", "81.83", "全部成交", "定投"),
    (9, "2026-08-10", "23:15:11", "诺基亚", "NOK", "买入", "9.2295", "5", "46.15", "全部成交", "定投"),
    (10, "2026-08-11", "23:10:35", "微软", "MSFT", "买入", "503.22", "0.1947", "97.98", "部成已撤", "定投"),
    (11, "2026-08-11", "23:10:36", "诺基亚", "NOK", "买入", "9.41", "5", "47.05", "全部成交", "定投"),
    (12, "2026-08-11", "23:10:36", "Rocket Lab", "RKLB", "买入", "78.73", "1", "78.73", "全部成交", "定投"),
    (13, "2026-08-12", None, "MARVELL TECHNOLO", "MRVL", "买入", "220.79", "1", "220.79", "全部成交", "普通委托"),
    (14, "2026-08-12", "23:10:56", "诺基亚", "NOK", "买入", "10.3895", "5", "51.95", "全部成交", "定投"),
    (15, "2026-08-12", "23:10:56", "Rocket Lab", "RKLB", "买入", "80.3395", "1", "80.34", "全部成交", "定投"),
    (16, "2026-08-12", "23:10:56", "微软", "MSFT", "买入", "493.73", "0.1985", "98.01", "部成已撤", "定投"),
    (17, "2026-08-13", "23:10:34", "微软", "MSFT", "买入", "496.13", "0.1975", "97.99", "部成已撤", "定投"),
    (18, "2026-08-13", "23:10:35", "诺基亚", "NOK", "买入", "10.83", "5", "54.15", "全部成交", "定投"),
    (19, "2026-08-13", "23:10:35", "Rocket Lab", "RKLB", "买入", "81.8995", "1", "81.9", "全部成交", "定投"),
    (20, "2026-08-25", None, "XE X-ENERGY", "XE", "买入", "18.79", "11", "206.69", "全部成交", "普通委托"),
    (21, "2026-08-25", None, "ROCKET LAB CORP", "RKLB", "买入", "67.55", "3", "202.65", "全部成交", "普通委托"),
    (22, "2026-08-25", None, "DEFIANCE DT 2X LONG RKLB", "RKLX", "买入", "17.01", "12", "204.12", "全部成交", "普通委托"),
    (23, "2026-08-25", None, "COHERENT CORP", "COHR", "买入", "286.21", "1", "286.21", "全部成交", "普通委托"),
    (24, "2026-08-26", None, "XE X-ENERGY", "XE", "买入", "20.02", "10", "200.2", "全部成交", "普通委托"),
    (25, "2026-08-26", None, "XE X-ENERGY", "XE", "买入", "18.74", "16", "299.84", "全部成交", "普通委托"),
    (26, "2026-08-27", None, "MARVELL TECHNOLO", "MRVL", "买入", "246.61", "1", "246.61", "全部成交", "普通委托"),
    (27, "2026-08-27", None, "ROCKET LAB CORP", "RKLB", "买入", "66.23", "3", "198.69", "全部成交", "普通委托"),
    (28, "2026-08-27", None, "COHERENT CORP", "COHR", "买入", "298.07", "1", "298.07", "全部成交", "普通委托"),
    (29, "2026-08-27", None, "DEFIANCE DT 2X LONG RKLB", "RKLX", "买入", "16.26", "12", "195.12", "全部成交", "普通委托"),
    (30, "2026-08-28", None, "COHERENT CORP", "COHR", "卖出", "281.31", "2", "562.62", "全部成交", "普通委托"),
    (31, "2026-08-28", None, "ROCKET LAB CORP", "RKLB", "卖出", "63.91", "6", "383.46", "全部成交", "普通委托"),
    (32, "2026-08-28", None, "MARVELL TECHNOLO", "MRVL", "卖出", "217.13", "3", "651.39", "全部成交", "普通委托"),
    (33, "2026-09-08", None, "DEFIANCE DT 2X LONG RKLB", "RKLX", "买入", "16.29", "18", "293.22", "全部成交", "普通委托"),
    (34, "2026-09-08", None, "XE X-ENERGY", "XE", "买入", "19.01", "10", "190.1", "全部成交", "普通委托"),
    (35, "2026-09-08", None, "ROCKET LAB CORP", "RKLB", "买入", "66.77", "6", "400.62", "全部成交", "普通委托"),
    (36, "2026-09-08", None, "COHERENT CORP", "COHR", "买入", "311.5", "1", "311.5", "全部成交", "普通委托"),
    (37, "2026-09-08", None, "MARVELL TECHNOLO", "MRVL", "买入", "230.6", "2", "461.2", "全部成交", "普通委托"),
    (38, "2026-09-17", None, "DEFIANCE DT 2X LONG RKLB", "RKLX", "买入", "15.95", "16", "255.2", "全部成交", "普通委托"),
    (39, "2026-09-17", None, "MARVELL TECHNOLO", "MRVL", "买入", "240.64", "1", "240.64", "全部成交", "普通委托"),
    (40, "2026-09-17", None, "COHERENT CORP", "COHR", "买入", "298.73", "1", "298.73", "全部成交", "普通委托"),
    (41, "2026-09-18", None, "DEFIANCE DT 2X LONG RKLB", "RKLX", "买入", "15.34", "16", "245.44", "全部成交", "普通委托"),
    (42, "2026-09-18", None, "XE X-ENERGY", "XE", "买入", "15.87", "15", "238.05", "全部成交", "普通委托"),
    (43, "2026-09-18", None, "MARVELL TECHNOLO", "MRVL", "买入", "240.4", "1", "240.4", "全部成交", "普通委托"),
    (44, "2026-09-18", None, "DEFIANCE DT 2X LONG RKLB", "RKLX", "买入", "15.13", "16", "242.08", "全部成交", "普通委托"),
    (45, "2026-09-21", None, "DEFIANCE DT 2X LONG RKLB", "RKLX", "卖出", "17.29", "57", "985.53", "全部成交", "普通委托"),
    (46, "2026-09-22", None, "COHERENT CORP", "COHR", "买入", "315.97", "1", "315.97", "全部成交", "普通委托"),
    (47, "2026-09-22", None, "MARVELL TECHNOLO", "MRVL", "买入", "262.15", "1", "262.15", "全部成交", "普通委托"),
    (48, "2026-09-24", None, "MARVELL TECHNOLO", "MRVL", "卖出", "256.02", "4", "1024.08", "全部成交", "普通委托"),
    (49, "2026-09-24", None, "DEFIANCE DT 2X LONG RKLB", "RKLX", "卖出", "18.12", "59", "1069.08", "全部成交", "普通委托"),
    (50, "2026-09-25", None, "NOKIA CORP ADR", "NOK", "买入", "10.39", "19", "197.41", "全部成交", "普通委托"),
    (51, "2026-09-25", None, "COHERENT CORP", "COHR", "买入", "300.31", "1", "300.31", "全部成交", "普通委托"),
    (52, "2026-09-25", None, "XE X-ENERGY", "XE", "买入", "15.51", "30", "465.3", "全部成交", "普通委托"),
    (53, "2026-09-28", None, "ROCKET LAB CORP", "RKLB", "卖出", "72.37", "16", "1157.92", "全部成交", "普通委托"),
    (54, "2026-10-01", None, "MARVELL TECHNOLO", "MRVL", "买入", "265.66", "4", "1062.64", "全部成交", "普通委托"),
    (55, "2026-10-02", None, "XE X-ENERGY", "XE", "买入", "14.01", "36", "504.36", "全部成交", "普通委托"),
    (56, "2026-10-02", None, "NOKIA CORP ADR", "NOK", "买入", "10.51", "29", "304.79", "全部成交", "普通委托"),
    (57, "2026-10-06", None, "MARVELL TECHNOLO", "MRVL", "买入", "284.11", "2", "568.22", "全部成交", "普通委托"),
    (58, "2026-10-06", None, "ROCKET LAB CORP", "RKLB", "买入", "73.93", "14", "1035.02", "全部成交", "普通委托"),
    (59, "2026-10-06", None, "XE X-ENERGY", "XE", "买入", "15.15", "17", "257.55", "全部成交", "普通委托"),
    (60, "2026-10-07", None, "ROCKET LAB CORP", "RKLB", "买入", "71.1", "6", "426.6", "全部成交", "普通委托"),
    (61, "2026-10-07", None, "XE X-ENERGY", "XE", "买入", "14.18", "16", "226.88", "全部成交", "普通委托"),
    (62, "2026-10-09", None, "DEFIANCE DT 2X LONG RKLB", "RKLX", "买入", "15.82", "9", "142.38", "全部成交", "普通委托"),
    (63, "2026-10-09", None, "COHERENT CORP", "COHR", "买入", "306.74", "1", "306.74", "全部成交", "普通委托"),
]

SIDE_MAP = {"买入": "Buy", "卖出": "Sell"}
DEFAULT_TIME = time(23, 0, 0)
# Ledger symbols carry an EXCHANGE: prefix (the CSV export convention); these are
# the listings the broker exported. An existing row's spelling always wins.
EXCHANGE = {
    "XE": "NASDAQ",
    "RKLX": "NASDAQ",
    "RKLB": "NASDAQ",
    "MRVL": "NASDAQ",
    "COHR": "NASDAQ",
    "MSFT": "NASDAQ",
    "NOK": "NYSE",
}
# Notes keep the broker's wording (the ledger has no column for it); the console
# prints the ASCII form because the Windows console here is GBK.
NOTE_ASCII = {"定投": "DCA", "部成已撤": "partial"}


def ticker_of(symbol: str | None) -> str:
    raw = (symbol or "").strip().upper()
    return raw.split(":")[-1] if raw else ""


def dkey(value) -> str:
    """Canonical decimal text, so 13 and 13.000000 compare equal."""
    return f"{Decimal(str(value)).normalize():f}"


def notes_for(status: str, order_type: str) -> str | None:
    parts = []
    if order_type == "定投":
        parts.append("定投")
    if status != "全部成交":
        parts.append(status)
    return " · ".join(parts) or None


def ascii_notes(notes: str | None) -> str:
    if not notes:
        return "-"
    return "+".join(NOTE_ASCII.get(p, p) for p in notes.split(" · "))


def resolve_symbols(db, tickers: set[str]) -> dict[str, str]:
    """Reuse the exact spelling an existing row uses for a ticker, else prefix it."""
    known: dict[str, str] = {}
    for sym in db.scalars(select(Trade.symbol).where(Trade.symbol.is_not(None)).distinct()):
        known.setdefault(ticker_of(sym), sym)
    for sym in db.scalars(select(SymbolMeta.symbol).distinct()):
        known.setdefault(ticker_of(sym), sym)
    out = {}
    for ticker in tickers:
        out[ticker] = known.get(ticker) or (
            f"{EXCHANGE[ticker]}:{ticker}" if ticker in EXCHANGE else ticker
        )
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Import the pasted broker rows into trade")
    parser.add_argument("--apply", action="store_true", help="write to the DB; default is dry-run")
    parser.add_argument(
        "--keep-broker-time",
        action="store_true",
        help="keep the broker's HH:MM:SS instead of pinning every row to 23:00:00",
    )
    parser.add_argument("--no-notes", action="store_true", help="leave notes empty")
    parser.add_argument(
        "--include-duplicates",
        action="store_true",
        help="also insert rows that already exist (same ticker/session/side/qty/price)",
    )
    parser.add_argument("--portfolio-id", type=int, default=None, help="defaults to settings")
    parser.add_argument("--backup", metavar="PATH", help="dump the current trade table to CSV first")
    args = parser.parse_args()

    settings = get_settings()
    portfolio_id = args.portfolio_id or settings.default_portfolio_id

    db = SessionLocal()
    try:
        tickers = {r[4] for r in ROWS}
        symbols = resolve_symbols(db, tickers)
        exact: set[tuple] = set()
        loose: dict[tuple, list[tuple[int, date]]] = {}
        for t in db.scalars(select(Trade).where(Trade.portfolio_id == portfolio_id)):
            if not t.symbol:
                continue
            ticker = ticker_of(t.symbol)
            session = trade_us_session_date(t.closing_time)
            key = (ticker, t.side, dkey(t.qty), dkey(t.fill_price))
            exact.add((session, key))
            loose.setdefault(key, []).append((t.id, session))

        first, last = date.fromisoformat(ROWS[0][1]), date.fromisoformat(ROWS[-1][1])
        nearby = sorted(
            (
                (trade_us_session_date(t.closing_time), t.id, t.symbol, t.side, t.qty, t.fill_price)
                for t in db.scalars(select(Trade).where(Trade.portfolio_id == portfolio_id))
                if t.symbol and ticker_of(t.symbol) in tickers
                and first <= trade_us_session_date(t.closing_time) <= last
            )
        )

        planned = []
        for no, day, broker_time, _broker_name, ticker, cn_side, price, qty, amount, status, order_type in ROWS:
            d = date.fromisoformat(day)
            if args.keep_broker_time and broker_time:
                closing_time = datetime.combine(d, time.fromisoformat(broker_time))
            else:
                closing_time = datetime.combine(d, DEFAULT_TIME)
            side = SIDE_MAP[cn_side]
            notes = None if args.no_notes else notes_for(status, order_type)
            session = trade_us_session_date(closing_time)
            key = (ticker, side, dkey(qty), dkey(price))
            # Same row already stored -> DUP. Same ticker/side/qty/price one day off
            # -> DUP+DATE, which is what an import made at 00:00:00 would have left
            # behind (the session date shifts back a day).
            near = [tid for tid, s in loose.get(key, []) if abs((s - session).days) <= 1]
            dup = (session, key) in exact
            planned.append(
                {
                    "no": no,
                    "ticker": ticker,
                    "symbol": symbols[ticker],
                    "side": side,
                    "price": Decimal(price),
                    "qty": Decimal(qty),
                    "amount": Decimal(amount),
                    "closing_time": closing_time,
                    "session": session,
                    "notes": notes,
                    "dup": dup,
                    "near": [] if dup else near,
                    "off": abs(Decimal(price) * Decimal(qty) - Decimal(amount)) > Decimal("0.015"),
                }
            )

        to_write = [p for p in planned if args.include_duplicates or not (p["dup"] or p["near"])]
        written = {p["no"] for p in to_write}
        skipped = [p for p in planned if p["no"] not in written]

        print(f"portfolio {portfolio_id} | {len(ROWS)} broker row(s) | commission 0 | "
              f"time {'as exported' if args.keep_broker_time else DEFAULT_TIME}")
        blank = sum(1 for r in ROWS if not r[2])
        if args.keep_broker_time:
            print(f"time: {len(ROWS) - blank} row(s) keep the broker's HH:MM:SS, "
                  f"{blank} row(s) had no time -> {DEFAULT_TIME}")
        else:
            print(f"time: all {len(ROWS)} row(s) -> {DEFAULT_TIME} "
                  f"({blank} had no time in the export, "
                  f"{len(ROWS) - blank} carried a broker HH:MM:SS and are flattened too)")
        print(f"{len(nearby)} existing trade(s) for these tickers between {first} and {last}")
        for session, tid, symbol, side, qty, price in nearby[:50]:
            print(f"      #{tid:<6} {session} {symbol:<13} {side:<4} {dkey(qty):>9} @ {dkey(price)}")
        if len(nearby) > 50:
            print(f"      ... {len(nearby) - 50} more")
        print()
        print(f"{'no':>3}  {'closing_time':<19} {'symbol':<13} {'side':<4} {'price':>9} {'qty':>9} "
              f"{'amount':>10} {'session':<10} {'notes':<12} flag")
        for p in planned:
            flags = []
            if p["off"]:
                flags.append("AMOUNT!")
            if p["dup"]:
                flags.append("DUP")
            elif p["near"]:
                flags.append(f"DUP+DATE(existing #{p['near'][0]})")
            if p["session"].isoformat() != next(
                r[1] for r in ROWS if r[0] == p["no"]
            ):
                flags.append("SESSION-SHIFT")
            print(
                f"{p['no']:>3}  {p['closing_time'].isoformat(sep=' '):<19} {p['symbol']:<13} "
                f"{p['side']:<4} {p['price']:>9f} {p['qty']:>9f} {p['amount']:>10f} "
                f"{p['session'].isoformat():<10} {ascii_notes(p['notes']):<12} {' '.join(flags)}"
            )

        per_ticker: dict[str, list[Decimal]] = {}
        for p in planned:
            per_ticker.setdefault(p["ticker"], []).append(p["amount"])
        print()
        for ticker, amounts in sorted(per_ticker.items()):
            print(f"  {symbols[ticker]:<13} {len(amounts):>2} row(s)  total {sum(amounts):>10f}")

        buys = sum(p["amount"] for p in planned if p["side"] == "Buy")
        sells = sum(p["amount"] for p in planned if p["side"] == "Sell")
        print(f"\n  buy  {buys:>12f}\n  sell {sells:>12f}\n  net cash change {sells - buys:>12f}")

        if any(p["off"] for p in planned):
            print("\nWARNING: AMOUNT! = price * qty does not match the broker's 成交金额 column; "
                  "check the transcription before --apply.")
        if skipped:
            print(f"\n{len(skipped)} row(s) look like they are already stored "
                  f"(row {', '.join(str(p['no']) for p in skipped)}); they stay out unless "
                  f"--include-duplicates is given.")

        if args.backup:
            Path(args.backup).write_text(export_csv(db, portfolio_id), encoding="utf-8")
            print(f"\nbackup written: {args.backup}")

        if not args.apply:
            print(f"\ndry-run: would insert {len(to_write)} row(s), skip {len(skipped)} duplicate(s); "
                  f"re-run with --apply")
            return

        for p in to_write:
            db.add(
                Trade(
                    portfolio_id=portfolio_id,
                    symbol=p["symbol"],
                    side=p["side"],
                    qty=p["qty"],
                    fill_price=p["price"],
                    commission=Decimal("0"),
                    closing_time=p["closing_time"],
                    notes=p["notes"],
                )
            )
        # Same cache invalidation the trade API does, so the daily NAV recomputes.
        cleared = db.query(DailyNav).filter(DailyNav.portfolio_id == portfolio_id).delete()
        db.commit()
        for symbol in sorted({p["symbol"] for p in to_write}):
            ensure_symbol_meta(db, symbol, fetch_remote=False)
        print(f"\ninserted {len(to_write)} row(s); skipped {len(skipped)} duplicate(s); "
              f"cleared {cleared} daily_nav row(s)")
    finally:
        db.close()


if __name__ == "__main__":
    main()

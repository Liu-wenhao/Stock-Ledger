"""Normalize the time part of trade rows to 23:00:00.

Dry-run by default; pass --apply to write.

    python scripts/set_trade_time.py INTC 2026-10-07 2026-10-09
    python scripts/set_trade_time.py INTC 2026-10-07 2026-10-09 --apply
    python scripts/set_trade_time.py --all-symbols 2026-10-09 --apply

Symbols are matched by ticker, so NASDAQ:INTC matches INTC.
Output is ASCII on purpose (Windows consoles here default to GBK).
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, time, timedelta
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import select

from app.core.database import SessionLocal
from app.models.entities import Trade

TARGET = time(23, 0, 0)


def ticker_of(symbol: str | None) -> str:
    raw = (symbol or "").strip().upper()
    return raw.split(":")[-1] if raw else ""


def main() -> None:
    parser = argparse.ArgumentParser(description="Normalize trade time to 23:00:00")
    parser.add_argument("symbol", nargs="?", help="ticker, e.g. INTC (any EXCHANGE: prefix is ignored)")
    parser.add_argument("dates", nargs="*", help="trading day(s), YYYY-MM-DD")
    parser.add_argument("--all-symbols", action="store_true", help="ignore the symbol filter")
    parser.add_argument("--all-dates", action="store_true", help="ignore the date filter")
    parser.add_argument("--apply", action="store_true", help="write to DB; default is dry-run")
    args = parser.parse_args()

    # `--all-symbols 2026-10-07 2026-10-09` makes argparse put the first date in
    # `symbol`; move it back to the date list.
    if args.symbol:
        try:
            date.fromisoformat(args.symbol)
        except ValueError:
            pass
        else:
            args.dates.insert(0, args.symbol)
            args.symbol = None

    if args.all_symbols:
        wanted_ticker = None
    elif args.symbol:
        wanted_ticker = ticker_of(args.symbol)
    else:
        parser.error("give a ticker, or pass --all-symbols")

    if args.all_dates:
        wanted: list[date] | None = None
    elif args.dates:
        try:
            wanted = sorted({date.fromisoformat(d) for d in args.dates})
        except ValueError:
            parser.error("dates must be YYYY-MM-DD")
    else:
        parser.error("give trading day(s), or pass --all-dates")

    stmt = select(Trade).order_by(Trade.closing_time, Trade.id)
    if wanted:
        stmt = stmt.where(
            Trade.closing_time >= datetime.combine(wanted[0], time.min),
            Trade.closing_time < datetime.combine(wanted[-1] + timedelta(days=1), time.min),
        )

    db = SessionLocal()
    try:
        rows = db.scalars(stmt).all()

        matched = [
            t
            for t in rows
            if (wanted is None or t.closing_time.date() in wanted)
            and (wanted_ticker is None or ticker_of(t.symbol) == wanted_ticker)
        ]

        if not matched:
            scope = "all symbols" if wanted_ticker is None else f"ticker={wanted_ticker}"
            days = "all dates" if wanted is None else f"dates={[str(d) for d in wanted]}"
            print(f"no match: {scope} {days}")
            print(f"({len(rows)} trade(s) in scope)")
            return

        changed = 0
        for t in matched:
            new_time = datetime.combine(t.closing_time.date(), TARGET)
            detail = f"{t.fill_price} x {t.qty}"
            if t.closing_time == new_time:
                print(f"#{t.id:<6} {t.side:<9} {t.symbol:<16} {t.closing_time} = (already ok)  {detail}")
                continue
            changed += 1
            print(f"#{t.id:<6} {t.side:<9} {t.symbol:<16} {t.closing_time} -> {new_time}  {detail}")
            if args.apply:
                t.closing_time = new_time

        if args.apply:
            db.commit()
            print(f"updated {changed} of {len(matched)} matched row(s)")
        else:
            print(f"dry-run: {len(matched)} matched, {changed} to change; re-run with --apply")
    finally:
        db.close()


if __name__ == "__main__":
    main()

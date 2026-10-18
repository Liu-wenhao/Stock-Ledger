"""Smoke test: build the trading review straight from the database.

Run from backend/:  python -c "import scripts.test_review as t; t.main()"
or simply:          python scripts/test_review.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.database import SessionLocal  # noqa: E402
from app.services.review import build_review  # noqa: E402


def main() -> None:
    with SessionLocal() as db:
        data = build_review(db)

    meta = data["meta"]
    cap = data["capital"]
    beh = data["behavior"]

    print(f"trades={meta['trades']} symbols={meta['symbols']}")
    print(f"first={meta['first_trade_at']} last={meta['last_trade_at']}")
    print(
        f"deposits={cap['deposits']} realized={cap['realized']} "
        f"({cap['realized_pct']}%) turnover={cap['turnover_x']}x"
    )
    print(
        f"cycles={beh['closed_cycles']} win_rate={beh['win_rate']}% "
        f"payoff={beh['payoff']} avg_win={beh['avg_win']} avg_loss={beh['avg_loss']}"
    )
    print(
        f"hold days win={beh['avg_win_days']} loss={beh['avg_loss_days']} "
        f"addon_below={beh['addon_below_avg']} same_day={beh['same_day_flips']}"
    )
    print(
        f"day trades={beh['t_trades']} wins={beh['t_wins']} losses={beh['t_losses']} "
        f"win_rate={beh['t_win_rate']}% pnl={beh['t_pnl']}"
    )
    print("\nworst symbols:")
    for row in data["symbols"][:5]:
        print(
            f"  {row['symbol']:<6} {row['realized']:>10} "
            f"{row['return_pct']:>7}% addon={row['addon_below_avg']}"
        )
    print("\nbest symbols:")
    for row in data["symbols"][-3:]:
        print(f"  {row['symbol']:<6} {row['realized']:>10} {row['return_pct']:>7}%")
    print("\nmonthly:")
    for row in data["monthly"]:
        print(f"  {row['month']}  trades={row['trades']:<4} realized={row['realized']}")
    print("\nopen positions:")
    for row in data["open_positions"]:
        print(f"  {row['symbol']:<6} qty={row['qty']} avg={row['avg_price']}")


if __name__ == "__main__":
    main()

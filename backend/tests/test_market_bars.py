from datetime import date, timedelta
import unittest
from unittest.mock import patch

import pandas as pd
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.orm import sessionmaker

from app.models.entities import MarketBar, MarketSymbol
from app.services import market_bars


class MarketBarStoreTest(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite://")
        self.engine = engine
        MarketSymbol.__table__.create(engine)
        MarketBar.__table__.create(engine)
        self.db = sessionmaker(bind=engine)()

    def tearDown(self):
        self.db.close()

    def _name(self, market: str, symbol: str, closes: list[float], last: date) -> int:
        row = MarketSymbol(market=market, symbol=symbol, venue=1, fetch_state="ok", bars_through=last)
        self.db.add(row)
        self.db.flush()
        days = [last - timedelta(days=offset) for offset in range(len(closes) - 1, -1, -1)]
        market_bars.write_bars(self.db, row.id, [(day, close, 100, None) for day, close in zip(days, closes)])
        self.db.commit()
        return row.id

    def test_class_share_codes_share_one_key(self):
        self.assertEqual(market_bars.store_key("us", "brk_b"), "BRK.B")
        self.assertEqual(market_bars.store_key("cn", "SH601012"), "sh601012")
        self.assertEqual(market_bars.store_key("us", "NASDAQ:INTC"), "INTC")
        self.assertEqual(market_bars.store_key("us", "NYSE:BRK_B"), "BRK.B")
        self.assertEqual(market_bars.store_key("cn", "SSE:601012"), "sh601012")
        self.assertEqual(market_bars.store_key("cn", "SZSE:000001"), "sz000001")

    def test_existing_close_only_schema_is_upgraded_without_rewriting_prices(self):
        engine = create_engine("sqlite://")
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE market_bar (symbol_id INTEGER, bar_date DATE, close DECIMAL(14,4))"))
            connection.execute(text("INSERT INTO market_bar VALUES (1, '2026-11-20', 12.5)"))

        market_bars.ensure_ohlc_schema(engine)
        market_bars.ensure_ohlc_schema(engine)

        self.assertTrue({"open", "high", "low"}.issubset({column["name"] for column in inspect(engine).get_columns("market_bar")}))
        with engine.connect() as connection:
            self.assertEqual(connection.execute(text("SELECT close, open, high, low FROM market_bar")).one(), (12.5, None, None, None))
        engine.dispose()

    def test_history_keeps_provider_ohlc_and_share_volume(self):
        day = date(2026, 11, 20)
        frame = pd.DataFrame([{
            "date": day, "open": 10.2, "high": 11.8, "low": 9.6,
            "close": 11.1, "volume": 350, "amount": 3885,
        }])
        with patch("akshare.stock_zh_a_daily", return_value=frame) as history:
            rows = market_bars._history("cn", "sh601012", day, day)

        history.assert_called_once_with(symbol="sh601012", start_date="20261120", end_date="20261120", adjust="qfq")
        self.assertEqual(rows, [(day, 11.1, 350, 3885.0, 10.2, 11.8, 9.6)])

    def test_reader_keeps_real_ohlc_and_excludes_future_bars(self):
        day = date(2026, 11, 20)
        ident = self._name("us", "INTC", [], day)
        market_bars.write_bars(self.db, ident, [
            (day, 11.0, 100, None, 10.0, 12.0, 9.0),
            (day + timedelta(days=1), 99.0, 200, None, 90.0, 100.0, 80.0),
        ])
        self.db.commit()
        with patch.object(market_bars, "last_session", return_value=day):
            bars = market_bars.load_market_bars(self.db, "NASDAQ:INTC")

        self.assertEqual(bars, [{"d": "2026-11-20", "o": 10.0, "h": 12.0, "l": 9.0, "c": 11.0, "v": 100.0, "dol": 1100.0}])

    def test_reader_rejects_close_only_history(self):
        day = date(2026, 11, 20)
        self._name("cn", "sh601012", [10.0], day)
        with patch.object(market_bars, "last_session", return_value=day):
            with self.assertRaisesRegex(ValueError, "缺少完整 OHLC"):
                market_bars.load_market_bars(self.db, "SSE:601012")

    def test_snapshot_preserves_ohlc_when_fields_are_absent(self):
        day = date(2026, 11, 20)
        ident = self._name("cn", "sh601012", [], day)
        market_bars.write_bars(self.db, ident, [(day, 11.0, 100, None, 10.0, 12.0, 9.0)])
        self.db.commit()

        market_bars.append_session(self.db, "cn", day, [{"symbol": "sh601012", "venue": 1, "price": 11.0, "volume": 200}])

        bar = self.db.get(MarketBar, (ident, day))
        self.assertEqual(tuple(float(value) for value in (bar.open, bar.high, bar.low)), (10.0, 12.0, 9.0))
        self.assertEqual(bar.volume, 200)

    def test_close_snapshot_saves_real_ohlc(self):
        day = date(2026, 11, 20)
        ident = self._name("cn", "sh601012", [10.0], day - timedelta(days=1))
        market_bars.append_session(self.db, "cn", day, [{
            "symbol": "sh601012", "venue": 1, "price": 11.0,
            "open": 10.0, "high": 12.0, "low": 9.0, "volume": 200,
        }])

        bar = self.db.get(MarketBar, (ident, day))
        self.assertEqual(tuple(float(value) for value in (bar.open, bar.high, bar.low, bar.close)), (10.0, 12.0, 9.0, 11.0))

    def test_scoped_backfill_cannot_clear_the_entire_market(self):
        day = date(2026, 11, 20)
        self._name("us", "INTC", [10.0], day)
        self._name("us", "RKLB", [20.0], day)

        with self.assertRaisesRegex(ValueError, "--replace"):
            market_bars.backfill("us", symbols=["INTC"], replace=True, relist=False)

        self.assertEqual(self.db.execute(select(func.count()).select_from(MarketBar)).scalar_one(), 2)

    def test_ohlc_backfill_reads_only_requested_names_from_their_original_start(self):
        day = date(2026, 11, 20)
        first = date(2017, 1, 3)
        target_id = self._name("us", "INTC", [10.0], day)
        other_id = self._name("us", "RKLB", [20.0], day)
        target = self.db.get(MarketSymbol, target_id)
        target.bars_from = first
        market_bars.write_bars(self.db, target_id, [(first, 8.0, 50, None)])
        self.db.commit()
        jobs_seen = []

        def read(jobs):
            jobs_seen.extend(jobs)
            return [("ok", [
                (first, 8.0, 50, None, 7.5, 8.5, 7.0),
                (day, 10.0, 100, None, 9.0, 11.0, 8.0),
            ], "") for job in jobs]

        with (
            patch("app.core.database.SessionLocal", sessionmaker(bind=self.engine)),
            patch.object(market_bars, "last_session", return_value=day),
            patch.object(market_bars, "_Readers") as readers,
            patch.object(market_bars, "_log"),
        ):
            readers.return_value.run.side_effect = read
            readers.return_value.crashes = 0
            stats = market_bars.backfill("us", ohlc=True, symbols=["NASDAQ:INTC"], relist=False)

        self.db.expire_all()
        self.assertEqual([(job[1], job[2]) for job in jobs_seen], [("INTC", first)])
        self.assertEqual(stats["left"], 0)
        self.assertEqual(float(self.db.get(MarketBar, (target_id, first)).open), 7.5)
        self.assertIsNone(self.db.get(MarketBar, (other_id, day)).open)

    def test_close_sums_use_the_last_19_closes_before_the_day(self):
        today = date(2026, 11, 20)
        self._name("cn", "sh600000", [float(value) for value in range(1, 26)], today - timedelta(days=1))
        self._name("cn", "sh600001", [10.0] * 18, today - timedelta(days=1))
        self._name("us", "AAPL", [5.0] * 25, today - timedelta(days=1))

        sums = market_bars.close_sums(self.db, "cn", today)

        # Closes 7..25 are the last 19. The 18-bar name and the US name are left out.
        self.assertEqual(sums, {"sh600000": float(sum(range(7, 26)))})

    def test_close_snapshot_overwrites_same_day(self):
        day = date(2026, 11, 20)
        self._name("cn", "sh600000", [10.0, 10.5], day - timedelta(days=1))
        prices = [
            {"symbol": "sh600000", "venue": 1, "price": 11.0, "volume": 1200, "amount": 13200.0, "prev_close": 10.5},
            {"symbol": "sz000001", "venue": 0, "price": 9.0, "volume": 500, "amount": None, "prev_close": None},
        ]

        self.assertEqual(market_bars.append_session(self.db, "cn", day, prices), 2)
        prices[0]["price"] = 99.0
        market_bars.append_session(self.db, "cn", day, prices)

        ids = market_bars.symbol_ids(self.db, "cn")
        bar = self.db.get(MarketBar, (ids["sh600000"], day))
        self.assertEqual(float(bar.close), 99.0)
        self.assertEqual(bar.volume, 1200)
        self.assertEqual(self.db.execute(select(func.count()).select_from(MarketBar)).scalar_one(), 4)
        known = self.db.get(MarketSymbol, ids["sh600000"])
        fresh = self.db.get(MarketSymbol, ids["sz000001"])
        self.assertEqual(known.bars_through, day)
        # A name first seen in the snapshot still needs its history read.
        self.assertEqual(fresh.fetch_state, "")

    def test_prev_close_mismatch_reloads_adjusted_history(self):
        day = date(2026, 11, 20)
        self._name("cn", "sh600000", [10.0, 10.5], day - timedelta(days=1))
        seen: list[list[str]] = []

        def fake_refresh(db, market, symbols, **kwargs):
            seen.append(list(symbols))
            return len(symbols)

        original = market_bars.refresh_symbols
        market_bars.refresh_symbols = fake_refresh
        try:
            market_bars.append_session(
                self.db,
                "cn",
                day,
                [
                    {
                        "symbol": "sh600000",
                        "venue": 1,
                        "price": 11.0,
                        "volume": 100,
                        "amount": None,
                        "prev_close": 9.8,
                    }
                ],
            )
            market_bars.append_session(
                self.db,
                "cn",
                day,
                [
                    {
                        "symbol": "sh600000",
                        "venue": 1,
                        "price": 11.0,
                        "volume": 100,
                        "amount": None,
                        "prev_close": 10.5,
                    }
                ],
            )
        finally:
            market_bars.refresh_symbols = original

        self.assertEqual(seen, [["sh600000"]])

    def test_replace_bars_overwrites_history(self):
        day = date(2026, 11, 20)
        ident = self._name("cn", "sh600000", [10.0, 11.0], day)
        market_bars.replace_bars(
            self.db,
            ident,
            [(day - timedelta(days=1), 9.5, 50, 100.0), (day, 12.0, 60, 120.0)],
        )
        self.db.commit()

        rows = self.db.execute(
            select(MarketBar.bar_date, MarketBar.close, MarketBar.volume)
            .where(MarketBar.symbol_id == ident)
            .order_by(MarketBar.bar_date)
        ).all()
        self.assertEqual([(d, float(c), v) for d, c, v in rows], [
            (day - timedelta(days=1), 9.5, 50),
            (day, 12.0, 60),
        ])

    def test_filter_writable_bars_drops_prices_outside_column_range(self):
        day = date(2026, 11, 20)
        kept = (day, 12.5, 100, None, 12.0, 13.0, 11.0)
        huge = (day - timedelta(days=1), 12.5, 100, None, 2.2e11, 3.3e11, 2.0e11)
        self.assertEqual(market_bars.filter_writable_bars([kept, huge]), [kept])


if __name__ == "__main__":
    unittest.main()

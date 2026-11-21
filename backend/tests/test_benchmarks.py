from datetime import date, datetime, timezone
import math
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import portfolio
from app.models.entities import DailyBar
from app.schemas.portfolio import BenchmarkComparisonResponse
from app.services import benchmarks


class BenchmarksTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        DailyBar.__table__.create(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.start = date(2026, 11, 20)
        self.end = date(2026, 11, 24)
        self.now = datetime(2026, 11, 25, 3, tzinfo=timezone.utc)
        self.days = [date(2026, 11, day) for day in (20, 21, 22, 23, 24)]
        self.quotes = {
            "sp500": [(self.days[0], 100), (self.days[3], 105), (self.days[4], 108)],
            "treasury": [(self.days[0], 3.65), (self.days[3], 7.30), (self.days[4], 10.95)],
            "gold": [(self.days[0], 200), (self.days[3], 210), (self.days[4], 220)],
            "bitcoin": list(zip(self.days, (300, 320, 310, 330, 340))),
            "fx": [(self.days[0], 7.0), (self.days[3], 7.1), (self.days[4], 6.9)],
        }
        benchmarks._fetch_cache.clear()
        network = patch("app.services.benchmarks.requests.get", side_effect=AssertionError("Tests must not access the network"))
        network.start()
        self.addCleanup(network.stop)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()
        benchmarks._fetch_cache.clear()

    def _build(self, quotes=None, end=None):
        histories = self.quotes if quotes is None else quotes
        with patch("app.services.benchmarks._fetch_quotes", side_effect=lambda benchmark, start, through: histories[benchmark.key]):
            return benchmarks.build_benchmarks(self.db, self.start, end or self.end, now=self.now)

    def _series(self, result, key):
        return next(series for series in result["series"] if series["key"] == key)

    def test_price_returns_use_one_common_start_and_end(self):
        result = self._build()
        BenchmarkComparisonResponse.model_validate(result)
        self.assertEqual(result["dates"], self.days)
        for key, expected in (("sp500", 8), ("gold", 10), ("bitcoin", 40 / 3), ("fx", -10 / 7)):
            series = self._series(result, key)
            self.assertEqual(series["status"], "ok")
            self.assertEqual(series["points"][0]["return_percent"], 0)
            self.assertAlmostEqual(series["return_percent"], expected, places=5)
        self.assertIn("美元升值", self._series(result, "fx")["note"])

    def test_treasury_accrues_natural_days_without_future_yields(self):
        result = self._build()
        series = self._series(result, "treasury")
        expected = (math.pow(1.0001, 3) * 1.0002 - 1) * 100
        self.assertAlmostEqual(series["return_percent"], expected, places=6)
        self.assertEqual(series["points"][3]["quote_date"], self.days[0])
        self.assertEqual(series["points"][4]["quote_date"], self.days[3])
        self.assertEqual(series["annualized_yield"], 10.95)
        self.assertLess(series["return_percent"], series["annualized_yield"])

    def test_zero_treasury_yields_are_real_data_not_missing(self):
        quotes = {**self.quotes, "treasury": [(day, 0) for day, value in self.quotes["treasury"]]}
        result = self._build(quotes)
        series = self._series(result, "treasury")
        self.assertEqual(series["status"], "ok")
        self.assertEqual(series["return_percent"], 0)
        key = benchmarks._bar_key(benchmarks.BENCHMARKS[1])
        self.assertEqual(self.db.query(DailyBar).filter(DailyBar.symbol == key).count(), 3)

    def test_later_yield_does_not_repair_an_unknown_accrual_gap(self):
        benchmark = benchmarks.BENCHMARKS[1]
        dates = benchmarks._days(self.start, date(2026, 12, 2))
        quotes = {self.start: 3.65, date(2026, 11, 30): 7.3}
        series = benchmarks._series(benchmark, quotes, dates)
        self.assertEqual(series["status"], "partial")
        self.assertIsNone(series["return_percent"])
        self.assertIsNone(series["points"][-1]["return_percent"])

    def test_missing_origin_never_rebases_to_a_future_price(self):
        quotes = {**self.quotes, "sp500": self.quotes["sp500"][1:]}
        series = self._series(self._build(quotes), "sp500")
        self.assertEqual(series["status"], "unavailable")
        self.assertIsNone(series["return_percent"])
        self.assertEqual(series["points"], [])

    def test_missing_trading_day_is_null_not_zero_or_interpolated(self):
        quotes = {**self.quotes, "sp500": [self.quotes["sp500"][0], self.quotes["sp500"][2]]}
        series = self._series(self._build(quotes), "sp500")
        self.assertEqual(series["status"], "partial")
        self.assertIsNone(series["points"][3]["return_percent"])
        self.assertEqual(series["points"][4]["return_percent"], 8)

    def test_weekend_does_not_hide_a_missing_friday_close(self):
        benchmark = benchmarks.BENCHMARKS[2]
        days = [date(2026, 11, day) for day in (19, 20, 21)]
        series = benchmarks._series(benchmark, {days[0]: 200}, days)
        self.assertIsNone(series["points"][1]["return_percent"])
        self.assertIsNone(series["points"][2]["return_percent"])

    def test_bitcoin_missing_weekend_is_not_forward_filled(self):
        quotes = {**self.quotes, "bitcoin": [point for point in self.quotes["bitcoin"] if point[0] != self.days[1]]}
        series = self._series(self._build(quotes), "bitcoin")
        self.assertIsNone(series["points"][1]["return_percent"])
        self.assertEqual(series["status"], "partial")

    def test_only_real_completed_observations_are_saved_without_fake_ohlc(self):
        quotes = {key: values + [(date(2026, 11, 25), 999)] for key, values in self.quotes.items()}
        result = self._build(quotes, end=date(2026, 12, 1))
        rows = self.db.query(DailyBar).all()
        self.assertEqual(result["end_date"], self.end)
        self.assertEqual(len(rows), 17)
        self.assertTrue(all(row.bar_date <= self.end for row in rows))
        self.assertTrue(all(row.open is None and row.high is None and row.low is None for row in rows))
        self.assertTrue(all(row.symbol.startswith("BENCH:") for row in rows))
        gold = [row for row in rows if row.symbol == benchmarks._bar_key(benchmarks.BENCHMARKS[2])]
        self.assertEqual(len(gold), 3)

    def test_complete_database_history_does_not_call_providers(self):
        self._build()
        benchmarks._fetch_cache.clear()
        with patch("app.services.benchmarks._fetch_quotes", side_effect=AssertionError("Unexpected provider call")) as provider:
            result = benchmarks.build_benchmarks(self.db, self.start, self.end, now=self.now)
        provider.assert_not_called()
        self.assertTrue(all(series["status"] == "ok" for series in result["series"]))

    def test_provider_failure_keeps_cache_and_exposes_missing_tail(self):
        self._build()
        key = benchmarks._bar_key(benchmarks.BENCHMARKS[2])
        self.db.query(DailyBar).filter(DailyBar.symbol == key, DailyBar.bar_date == self.end).delete()
        self.db.commit()
        benchmarks._fetch_cache.clear()
        with patch("app.services.benchmarks._fetch_quotes", side_effect=RuntimeError("offline")):
            result = benchmarks.build_benchmarks(self.db, self.start, self.end, now=self.now)
        series = self._series(result, "gold")
        self.assertEqual(series["status"], "partial")
        self.assertIsNone(series["return_percent"])
        self.assertEqual(series["as_of"], self.days[3])
        self.assertEqual(self.db.query(DailyBar).filter(DailyBar.symbol == key).count(), 2)

    def test_failed_requests_have_a_retry_cooldown(self):
        with patch("app.services.benchmarks._fetch_quotes", side_effect=RuntimeError("offline")) as provider:
            first = benchmarks._cached_fetch(benchmarks.BENCHMARKS[0], self.start, self.end)
            second = benchmarks._cached_fetch(benchmarks.BENCHMARKS[0], self.start, self.end)
        self.assertEqual(first, [])
        self.assertEqual(second, [])
        self.assertEqual(provider.call_count, 1)

    def test_insert_ignore_preserves_frozen_prices(self):
        self._build()
        key = benchmarks._bar_key(benchmarks.BENCHMARKS[0])
        benchmarks._save_quotes(self.db, [{"symbol": key, "bar_date": self.start, "close": 999, "volume": 0, "dollar": 0}])
        row = self.db.query(DailyBar).filter(DailyBar.symbol == key, DailyBar.bar_date == self.start).one()
        self.assertEqual(float(row.close), 100)

    def test_csv_parser_ignores_missing_nonfinite_and_invalid_dates(self):
        payload = "observation_date,DGS3MO\n2026-11-20,3.65\n2026-11-21,.\n2026-11-22,NaN\n2026-11-23,inf\ninvalid,7.3\n2026-11-24,0\n"
        self.assertEqual(benchmarks._parse_csv(payload, "DGS3MO"), [(self.start, 3.65), (self.end, 0)])
        spot = "Date,Open,High,Low,Close\n2026-11-20,190,210,180,200\n"
        self.assertEqual(benchmarks._parse_csv(spot, "Close"), [(self.start, 200)])

    def test_kraken_parser_uses_crypto_utc_closes_and_skips_nulls(self):
        timestamp = int(datetime(2026, 11, 20, tzinfo=timezone.utc).timestamp())
        payload = {"error": [], "result": {"XXBTZUSD": [[timestamp, 290, 320, 280, 300], [timestamp + 86400, 300, 330, 290, None], [timestamp + 172800, 300, 330, 290, float("nan")], []], "last": timestamp}}
        self.assertEqual(benchmarks._parse_kraken(payload), [(self.start, 300)])
        self.assertEqual(benchmarks._parse_kraken({"error": ["Unknown asset pair"]}), [])

    def test_ecb_parser_checks_base_and_skips_invalid_prices(self):
        payload = {"base": "USD", "rates": {"2026-11-20": {"CNY": 7}, "invalid": {"CNY": 8}, "2026-11-24": {"CNY": None}}}
        self.assertEqual(benchmarks._parse_forex(payload), [(self.start, 7)])
        self.assertEqual(benchmarks._parse_forex({**payload, "base": "EUR"}), [])

    def test_treasury_month_csv_and_source_specific_holidays(self):
        payload = 'Date,"1 Mo","3 Mo","10 Yr"\n11/20/2026,4.19,4.32,4.92\n'
        self.assertEqual(benchmarks._parse_csv(payload, "3 Mo"), [(self.start, 4.32)])
        veteran_day = date(2026, 11, 11)
        self.assertIn(veteran_day, benchmarks._expected_days(benchmarks.BENCHMARKS[0], veteran_day, veteran_day))
        self.assertNotIn(veteran_day, benchmarks._expected_days(benchmarks.BENCHMARKS[1], veteran_day, veteran_day))
        easter_monday = date(2026, 4, 6)
        self.assertNotIn(easter_monday, benchmarks._expected_days(benchmarks.BENCHMARKS[4], easter_monday, easter_monday))

    def test_existing_local_index_and_gld_rows_are_reused_without_overwriting(self):
        for key, symbol in benchmarks._DATABASE_ALIASES.items():
            for day, value in self.quotes[key]:
                self.db.add(DailyBar(symbol=symbol, bar_date=day, close=value, open=value - 1, high=value + 1, low=value - 2))
        self.db.commit()
        with patch("app.services.benchmarks._fetch_quotes", side_effect=lambda benchmark, start, through: self.quotes[benchmark.key]) as provider:
            result = benchmarks.build_benchmarks(self.db, self.start, self.end, now=self.now)
        self.assertEqual({call.args[0].key for call in provider.call_args_list}, {"treasury", "bitcoin", "fx"})
        self.assertEqual(self._series(result, "sp500")["return_percent"], 8)
        self.assertEqual(self._series(result, "gold")["return_percent"], 10)
        row = self.db.query(DailyBar).filter(DailyBar.symbol == "GLD", DailyBar.bar_date == self.start).one()
        self.assertEqual(float(row.open), 199)

    def test_unfinished_only_window_does_not_fetch_or_show_zero(self):
        day = self.now.date()
        with patch("app.services.benchmarks._fetch_quotes") as provider:
            result = benchmarks.build_benchmarks(self.db, day, day, now=self.now)
        provider.assert_not_called()
        self.assertIsNone(result["end_date"])
        self.assertEqual(result["dates"], [])
        self.assertTrue(all(series["return_percent"] is None for series in result["series"]))

    def test_api_response_and_invalid_range_without_starting_workers(self):
        app = FastAPI()
        app.include_router(portfolio.router)
        app.dependency_overrides[portfolio.get_db] = lambda: self.db
        response = self._build()
        with TestClient(app) as client:
            with patch("app.services.benchmarks.build_benchmarks", return_value=response):
                result = client.get("/api/analysis/benchmarks?start=2026-11-20&end=2026-11-24")
            invalid = client.get("/api/analysis/benchmarks?start=2026-11-25&end=2026-11-20")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["currency"], "USD")
        self.assertEqual(len(result.json()["series"]), 5)
        self.assertEqual(invalid.status_code, 422)


if __name__ == "__main__":
    unittest.main()

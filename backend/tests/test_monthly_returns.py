from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import portfolio
from app.schemas.portfolio import MonthlyReturnsResponse
from app.services import ledger


class MonthlyReturnsTest(unittest.TestCase):
    def setUp(self):
        self.days = [date(2026, 1, 16), date(2026, 1, 30), date(2026, 2, 2), date(2026, 2, 27)]
        self.rows = self._rows(zip(self.days, (100, 110, 112, 121)))
        self.prices = dict(zip((day.isoformat() for day in self.days), (200, 220, 224, 242)))
        self.symbol = ledger.settings.benchmark_symbol

    def _rows(self, values):
        return [SimpleNamespace(nav_date=day, total_value=Decimal(str(value))) for day, value in values]

    def _returns(self, rows=None, prices=None, start=None, through=None):
        nav_rows = self.rows if rows is None else rows
        history = self.prices if prices is None else prices
        first = nav_rows[0].nav_date if nav_rows else self.days[0]
        with (
            patch("app.services.ledger.build_nav_series", return_value=nav_rows),
            patch("app.services.ledger.range_start", return_value=start or first),
            patch("app.services.ledger.get_history_closes", return_value={self.symbol: history}) as provider,
        ):
            result = ledger.monthly_returns(None, range_key="ALL", through=through)
        MonthlyReturnsResponse.model_validate(result)
        return result, provider

    def test_partial_first_month_uses_actual_nav_origin_for_both_returns(self):
        result, provider = self._returns()
        first = result["items"][0]
        self.assertEqual(first["month_key"], "2026-01")
        self.assertEqual(first["start_date"], self.days[0])
        self.assertEqual(first["end_date"], self.days[1])
        self.assertEqual(first["portfolio_percent"], Decimal("10.00"))
        self.assertEqual(first["benchmark_percent"], Decimal("10.00"))

    def test_later_month_uses_previous_month_end_for_both_returns(self):
        result, provider = self._returns()
        second = result["items"][1]
        self.assertEqual(second["start_date"], self.days[1])
        self.assertEqual(second["end_date"], self.days[3])
        self.assertEqual(second["portfolio_percent"], Decimal("10.00"))
        self.assertEqual(second["benchmark_percent"], Decimal("10.00"))

    def test_range_filter_does_not_include_earlier_nav_as_origin(self):
        result, provider = self._returns(start=self.days[1])
        first = result["items"][0]
        self.assertEqual(first["start_date"], self.days[1])
        self.assertEqual(first["end_date"], self.days[1])
        self.assertEqual(first["portfolio_percent"], Decimal("0.00"))
        self.assertEqual(first["benchmark_percent"], Decimal("0.00"))

    def test_through_excludes_newer_nav_and_uses_matching_partial_month_end(self):
        result, provider = self._returns(through=self.days[2])
        second = result["items"][1]
        self.assertEqual(second["end_date"], self.days[2])
        self.assertEqual(second["portfolio_percent"], Decimal("1.82"))
        self.assertEqual(second["benchmark_percent"], Decimal("1.82"))

    def test_history_request_has_lookback_and_stops_at_cutoff_day_end(self):
        result, provider = self._returns(through=self.days[2])
        provider.assert_called_once_with(
            [self.symbol],
            datetime.combine(self.days[0] - timedelta(days=10), datetime.min.time()),
            datetime.combine(self.days[2], datetime.max.time()),
        )

    def test_weekend_nav_uses_prior_close_without_using_a_future_price(self):
        rows = self._rows(((date(2026, 1, 17), 100), (self.days[1], 110)))
        prices = {self.days[0].isoformat(): 200, date(2026, 1, 19).isoformat(): 999, self.days[1].isoformat(): 220}
        result, provider = self._returns(rows=rows, prices=prices)
        self.assertEqual(result["items"][0]["benchmark_percent"], Decimal("10.00"))

    def test_same_calendar_month_in_different_years_has_distinct_keys(self):
        days = [date(2025, 1, 31), date(2025, 12, 31), date(2026, 1, 30)]
        result, provider = self._returns(rows=self._rows(zip(days, (100, 110, 121))), prices={})
        self.assertEqual([item["month_key"] for item in result["items"]], ["2025-01", "2025-12", "2026-01"])
        self.assertEqual(result["items"][0]["month"], result["items"][2]["month"])
        self.assertNotEqual(result["items"][0]["start_date"], result["items"][2]["start_date"])

    def test_missing_benchmark_quotes_are_null_not_zero(self):
        result, provider = self._returns(prices={})
        self.assertTrue(all(item["benchmark_percent"] is None for item in result["items"]))
        self.assertEqual(result["items"][0]["portfolio_percent"], Decimal("10.00"))

    def test_empty_nav_does_not_request_history(self):
        result, provider = self._returns(rows=[])
        self.assertEqual(result["items"], [])
        provider.assert_not_called()

    def test_empty_filtered_range_does_not_request_history(self):
        result, provider = self._returns(start=date(2026, 3, 1))
        self.assertEqual(result["items"], [])
        provider.assert_not_called()

    def test_cutoff_before_first_nav_does_not_request_history(self):
        result, provider = self._returns(through=date(2026, 1, 1))
        self.assertEqual(result["items"], [])
        provider.assert_not_called()

    def _client(self):
        app = FastAPI()
        app.include_router(portfolio.router)
        app.dependency_overrides[portfolio.get_db] = lambda: None
        return TestClient(app)

    def test_api_passes_cutoff_and_serializes_actual_month_dates(self):
        result, provider = self._returns(through=self.days[2])
        with self._client() as client, patch("app.services.ledger.monthly_returns", return_value=result) as monthly:
            response = client.get("/api/analysis/monthly-returns?range=ALL&end=2026-02-02")
        self.assertEqual(response.status_code, 200)
        monthly.assert_called_once_with(None, range_key="ALL", through=self.days[2])
        self.assertEqual(response.json()["items"][0]["start_date"], self.days[0].isoformat())
        self.assertEqual(response.json()["items"][1]["end_date"], self.days[2].isoformat())

    def test_api_without_cutoff_keeps_existing_call_behavior(self):
        result = {"items": [], "benchmark": self.symbol}
        with self._client() as client, patch("app.services.ledger.monthly_returns", return_value=result) as monthly:
            response = client.get("/api/analysis/monthly-returns?range=3M")
        self.assertEqual(response.status_code, 200)
        monthly.assert_called_once_with(None, range_key="3M", through=None)

    def test_api_rejects_invalid_cutoff_without_calling_service(self):
        with self._client() as client, patch("app.services.ledger.monthly_returns") as monthly:
            response = client.get("/api/analysis/monthly-returns?end=not-a-date")
        self.assertEqual(response.status_code, 422)
        monthly.assert_not_called()


if __name__ == "__main__":
    unittest.main()

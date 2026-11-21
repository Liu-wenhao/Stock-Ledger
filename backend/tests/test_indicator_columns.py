from datetime import date, timedelta
import unittest
from unittest.mock import MagicMock, patch

from app.schemas.portfolio import IndicatorHistory, IndicatorPoint
from app.services import signals


class IndicatorColumnsTest(unittest.TestCase):
    def _bars(self):
        first = date(2026, 5, 1)
        return [
            {
                "d": (first + timedelta(days=offset)).isoformat(),
                "o": 100 + offset,
                "h": 102 + offset,
                "l": 99 + offset,
                "c": 101 + offset,
                "v": 1000 + offset,
                "dol": (101 + offset) * (1000 + offset),
            }
            for offset in range(100)
        ]

    def test_daily_columns_use_the_previous_bar(self):
        bars = self._bars()
        stats = signals._measure(bars, None, "NASDAQ:INTC")
        point = signals._history_point(stats)
        self.assertEqual(point["prev_close"], bars[-2]["c"])
        self.assertEqual(point["day_change"], 1)
        self.assertEqual(point["day_change_percent"], round(100 / bars[-2]["c"], 2))
        self.assertEqual(point["vol_cap"], round(signals.TARGET_VOL / stats["vol"] * 100, 1))
        public = IndicatorPoint(**point).model_dump()
        for field in ("prev_close", "day_change", "day_change_percent", "vol_cap", "anchor_date"):
            self.assertEqual(public[field], point[field])

    def test_missing_previous_close_and_zero_volatility_are_not_fabricated(self):
        stats = {
            "as_of": "2026-11-20",
            "close": 10,
            "prev_close": None,
            "day_change": None,
            "day_ret": None,
            "vol": 0,
        }
        point = signals._history_point(stats)
        for field in ("prev_close", "day_change", "day_change_percent", "vol_cap"):
            self.assertIsNone(point[field])

    def test_negative_and_unchanged_daily_moves(self):
        bars = self._bars()
        previous = bars[-2]["c"]
        for change in (-2, 0):
            with self.subTest(change=change):
                bars[-1]["c"] = previous + change
                point = signals._history_point(signals._measure(bars, None, "NASDAQ:INTC"))
                self.assertEqual(point["day_change"], change)
                self.assertEqual(point["day_change_percent"], round(change / previous * 100, 2))

    def test_page_boundary_keeps_previous_close_and_columns(self):
        bars = self._bars()
        with (
            patch("app.core.database.SessionLocal", return_value=MagicMock()),
            patch("app.services.signals._load_bars", return_value=bars),
            patch("app.services.price_history.history_version", return_value="market:test"),
            patch("app.services.price_history.unfinished_session", return_value=None),
            patch("app.services.bars.chart_since", return_value=date(2026, 4, 1)),
            patch("app.services.signals._symbol_pe", return_value=None),
            patch("app.services.signals._trend_brief", return_value=None),
            patch("app.services.ak_backtest.advice_marks", return_value={}),
            patch("app.services.signals._sell_strategies", return_value=[]),
        ):
            newest = signals.build_indicator_history("NASDAQ:INTC", limit=20)
            older = signals.build_indicator_history("NASDAQ:INTC", before=newest["before"], limit=20)
        self.assertEqual(newest["_indicator_columns_version"], signals.INDICATOR_COLUMNS_VERSION)
        for payload, start in ((newest, 80), (older, 60)):
            public = IndicatorHistory(**payload)
            first = public.points[0]
            self.assertEqual(first.date, bars[start]["d"])
            self.assertEqual(first.prev_close, bars[start - 1]["c"])
            self.assertEqual(first.day_change, 1)
            self.assertEqual(first.day_change_percent, round(100 / bars[start - 1]["c"], 2))


if __name__ == "__main__":
    unittest.main()

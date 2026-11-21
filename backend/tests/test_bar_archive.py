from datetime import date, datetime, timezone
import threading
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.entities import DailyBar, IndicatorTrendCache, Portfolio, Trade
from app.services import bar_archive, bars, indicator_monitor, sentiment


class BarArchiveTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)
        self.db = self.sessions()
        self.db.add(Portfolio(id=1, name="Default"))
        self.db.add_all([
            self._trade("NYSE:SSG", "Buy"),
            self._trade("NASDAQ:QQQ", "Buy"),
            self._trade("NASDAQ:QQQ", "Sell", month=2),
            self._trade("SZSE:159934", "Buy"),
            self._trade("HKEX:00005", "Buy"),
            self._trade("NASDAQ:INTC", "Buy"),
            self._trade("SSE:601012", "Buy"),
            self._trade("NASDAQ:SOXX", "Dividend"),
        ])
        self.db.add(IndicatorTrendCache(symbol="NYSE:SSG", payload_blob=indicator_monitor._encode({"points": []}), notified_state_json="{}"))
        self.db.commit()
        self.day = date(2026, 11, 20)
        self.history = [
            {"d": day, "o": 10, "h": 12, "l": 9, "c": 11, "v": 100, "dol": 1100}
            for day in ("2026-11-19", "2026-11-20", "2026-11-23")
        ]
        bar_archive._retry_at.clear()
        replacement = patch.object(bar_archive, "SessionLocal", self.sessions)
        replacement.start()
        self.addCleanup(replacement.stop)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()
        bar_archive._retry_at.clear()

    def _trade(self, symbol, side, month=1):
        return Trade(portfolio_id=1, symbol=symbol, side=side, qty=100, fill_price=10, closing_time=datetime(2026, month, 1))

    def test_held_and_closed_legacy_symbols_are_archived_without_live_bars_or_alerts(self):
        trade_count = self.db.query(Trade).count()
        with (
            patch.object(bar_archive, "completed_session", return_value=self.day),
            patch("app.services.data_sources.fetch_daily_bars", return_value=self.history) as fetch,
            patch("app.services.bars.chart_session_bar") as live,
            patch("app.services.dingtalk.send_markdown") as notify,
        ):
            self.assertEqual(bar_archive.archive_completed_position_bars(), 4)
            self.assertEqual(bar_archive.archive_completed_position_bars(), 0)
        self.assertEqual({call.args[0] for call in fetch.call_args_list}, {"NYSE:SSG", "NASDAQ:QQQ", "SZSE:159934", "HKEX:00005"})
        self.assertEqual(fetch.call_count, 4)
        self.db.expire_all()
        rows = self.db.query(DailyBar).all()
        self.assertEqual(len(rows), 8)
        self.assertTrue(all(row.bar_date <= self.day for row in rows))
        self.assertTrue(all(float(row.open) == 10 and float(row.high) == 12 and float(row.low) == 9 and float(row.close) == 11 for row in rows))
        self.assertEqual(self.db.query(Trade).count(), trade_count)
        cached = self.db.get(IndicatorTrendCache, "NYSE:SSG")
        self.assertIsNone(indicator_monitor.cached_indicator_history(self.db, "NYSE:SSG"))
        self.assertEqual(cached.notified_state_json, "{}")
        live.assert_not_called()
        notify.assert_not_called()

    def test_missing_close_retries_after_ten_minutes(self):
        with (
            patch.object(bar_archive, "position_symbols", return_value=["NYSE:SSG"]),
            patch.object(bar_archive, "completed_session", return_value=self.day),
            patch("app.services.data_sources.fetch_daily_bars", side_effect=[RuntimeError("not published"), self.history]) as fetch,
        ):
            with patch.object(bar_archive.time, "monotonic", return_value=100):
                self.assertEqual(bar_archive.archive_completed_position_bars(), 0)
            with patch.object(bar_archive.time, "monotonic", return_value=200):
                self.assertEqual(bar_archive.archive_completed_position_bars(), 0)
            self.assertEqual(fetch.call_count, 1)
            with patch.object(bar_archive.time, "monotonic", return_value=701):
                self.assertEqual(bar_archive.archive_completed_position_bars(), 1)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(self.db.query(DailyBar).count(), 2)
        self.assertEqual(bar_archive._retry_at, {})

    def test_calendar_failure_does_not_freeze_any_daily_bar(self):
        with (
            patch.object(bar_archive, "completed_session", return_value=None),
            patch("app.services.data_sources.fetch_daily_bars") as fetch,
        ):
            self.assertEqual(bar_archive.archive_completed_position_bars(), 0)
        fetch.assert_not_called()
        self.assertEqual(self.db.query(DailyBar).count(), 0)

    def test_completed_daily_bars_are_written_once(self):
        bars._save_frozen(self.db, "SSG", self.history, self.day)
        changed = [{**bar, "c": 99} for bar in self.history]
        bars._save_frozen(self.db, "SSG", changed, self.day)
        rows = self.db.query(DailyBar).all()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(float(row.close) == 11 for row in rows))

    def test_background_archive_runs_even_if_breadth_capture_fails(self):
        stop = threading.Event()
        with (
            patch.object(bar_archive, "_stop", stop),
            patch.object(stop, "wait", side_effect=lambda seconds: stop.set()),
            patch.object(sentiment, "capture_due_close", side_effect=RuntimeError("breadth unavailable")) as capture,
            patch.object(bar_archive, "archive_completed_position_bars", return_value=0) as archive,
        ):
            bar_archive._loop()
        capture.assert_not_called()
        archive.assert_called_once_with()


class ArchiveCalendarTest(unittest.TestCase):
    def test_only_finished_exchange_sessions_are_eligible(self):
        cases = (
            ("cn", datetime(2026, 11, 20, 3, 45, tzinfo=timezone.utc), date(2026, 11, 19)),
            ("cn", datetime(2026, 11, 20, 7, 1, tzinfo=timezone.utc), date(2026, 11, 20)),
            ("hk", datetime(2026, 11, 20, 4, 30, tzinfo=timezone.utc), date(2026, 11, 19)),
            ("hk", datetime(2026, 11, 20, 8, 1, tzinfo=timezone.utc), date(2026, 11, 20)),
            ("us", datetime(2026, 11, 20, 14, tzinfo=timezone.utc), date(2026, 11, 19)),
            ("us", datetime(2026, 11, 20, 21, 1, tzinfo=timezone.utc), date(2026, 11, 20)),
            ("us", datetime(2026, 11, 21, 12, tzinfo=timezone.utc), date(2026, 11, 20)),
            ("us", datetime(2026, 11, 26, 22, tzinfo=timezone.utc), date(2026, 11, 25)),
            ("us", datetime(2026, 11, 27, 17, 59, tzinfo=timezone.utc), date(2026, 11, 25)),
            ("us", datetime(2026, 11, 27, 18, 1, tzinfo=timezone.utc), date(2026, 11, 27)),
        )
        for market, stamp, expected in cases:
            with self.subTest(market=market, stamp=stamp):
                self.assertEqual(bar_archive.completed_session(market, stamp), expected)
                freeze = {"cn": bars.cn_frozen, "hk": bars.hk_frozen, "us": bars.frozen_through}[market]
                self.assertEqual(freeze(stamp), expected)


if __name__ == "__main__":
    unittest.main()

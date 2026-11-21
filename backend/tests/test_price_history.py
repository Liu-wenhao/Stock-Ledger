from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.entities import DailyBar, IndicatorTrendCache, MarketBar, MarketSymbol, Trade
from app.services import ak_backtest, indicator_monitor, market_bars, price_history, signals


class PriceHistoryTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)
        self.db = self.sessions()
        self.day = date(2026, 11, 20)
        for target, value in (
            ("app.core.database.SessionLocal", self.sessions),
            ("app.services.market_bars.last_session", self.day),
            ("app.services.ledger.us_equity_session_date", self.day),
            ("app.services.bars.chart_since", date(2026, 4, 1)),
            ("app.services.bars.frozen_through", self.day),
            ("app.services.signals._symbol_pe", None),
        ):
            replacement = patch(target, new=value) if target.endswith("SessionLocal") else patch(target, return_value=value)
            replacement.start()
            self.addCleanup(replacement.stop)
        local_clock = patch("app.services.bars._local_now", side_effect=lambda zone, now: datetime(2026, 11, 20, 16, tzinfo=ZoneInfo(zone)))
        local_clock.start()
        self.addCleanup(local_clock.stop)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _stock(self, market="us", key="INTC", complete=True):
        stock = MarketSymbol(market=market, symbol=key, fetch_state="ok", bars_through=self.day)
        self.db.add(stock)
        self.db.flush()
        rows = [
            (self.day - timedelta(days=offset), 11.0, 100, None, 10.0 if complete else None, 12.0, 9.0)
            for offset in range(44, -1, -1)
        ]
        market_bars.write_bars(self.db, stock.id, rows)
        self.db.add(DailyBar(symbol=key, bar_date=self.day, open=100, high=110, low=90, close=105, volume=300))
        self.db.commit()
        return stock

    def _daily_bars(self):
        return [
            {"d": (self.day - timedelta(days=offset)).isoformat(), "o": 100, "h": 110, "l": 90, "c": 105, "v": 300, "dol": 31500}
            for offset in range(44, -1, -1)
        ]

    def test_cn_and_us_trends_match_backtest_database_candles(self):
        for market, key, symbol in (("us", "INTC", "NASDAQ:INTC"), ("cn", "sh601012", "SSE:601012")):
            with self.subTest(market=market):
                self._stock(market, key)
                with (
                    patch("app.services.bars.load_symbol_bars", side_effect=AssertionError("stock history fetched daily bars")) as daily,
                    patch("app.services.bars.chart_session_bar", side_effect=AssertionError("completed stock history fetched a quote")) as live,
                ):
                    payload = signals.build_indicator_history(symbol)
                    candles = ak_backtest.backtest_candles(self.db, symbol)["candles"]
                point = payload["points"][-1]
                self.assertEqual([point[field] for field in ("open", "high", "low", "close")], [10, 12, 9, 11])
                self.assertEqual([candles[-1][field] for field in ("o", "h", "l", "c")], [10, 12, 9, 11])
                self.assertEqual(point["date"], candles[-1]["d"])
                self.assertTrue(payload["_history_source_version"].startswith("market_bar:v1:"))
                daily.assert_not_called()
                live.assert_not_called()

    def test_missing_market_history_never_uses_a_daily_fallback(self):
        with (
            patch("app.services.bars.load_symbol_bars") as daily,
            patch("app.services.bars.chart_session_bar") as live,
        ):
            with self.assertRaisesRegex(ValueError, "全市场数据库中没有"):
                signals.build_indicator_history("NASDAQ:INTC")
        daily.assert_not_called()
        live.assert_not_called()

    def test_missing_ohlc_never_uses_a_daily_or_live_fallback(self):
        self._stock(complete=False)
        with (
            patch("app.services.bars.load_symbol_bars") as daily,
            patch("app.services.bars.chart_session_bar") as live,
        ):
            with self.assertRaisesRegex(ValueError, "缺少完整 OHLC"):
                signals.build_indicator_history("NASDAQ:INTC")
        daily.assert_not_called()
        live.assert_not_called()

    def test_unfinished_us_candle_is_retained_without_being_written(self):
        self._stock()
        live_bar = {"d": "2026-11-23", "o": 11, "h": 13, "l": 10, "c": 12, "v": 200, "dol": 2400}
        with (
            patch("app.services.ledger.us_equity_session_date", return_value=date(2026, 11, 23)),
            patch("app.services.bars._local_now", return_value=datetime(2026, 11, 23, 10, tzinfo=ZoneInfo("America/New_York"))),
            patch("app.services.bars.chart_session_bar", return_value=live_bar) as live,
        ):
            payload = signals.build_indicator_history("NASDAQ:INTC")
        self.assertEqual(payload["points"][-1]["date"], "2026-11-23")
        self.assertEqual(payload["points"][-1]["close"], 12)
        self.assertEqual(self.db.query(MarketBar).count(), 45)
        self.assertEqual(self.db.query(DailyBar).count(), 1)
        live.assert_called_once()

    def test_cn_lunch_keeps_the_unfinished_session(self):
        self._stock("cn", "sh601012")
        lunch = datetime(2026, 11, 23, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
        live_bar = {"d": "2026-11-23", "o": 11, "h": 13, "l": 10, "c": 12, "v": 200, "dol": 2400}
        with patch("app.services.bars._local_now", return_value=lunch):
            payload = signals.build_indicator_history("SSE:601012", session_bar=live_bar, fetch_session_bar=False)
        self.assertEqual(payload["points"][-1]["date"], "2026-11-23")
        self.assertEqual(self.db.query(MarketBar).count(), 45)

    def test_wrong_day_quote_is_not_used_as_stock_history(self):
        self._stock()
        for day in ("2026-11-20", "2026-11-24"):
            with self.subTest(day=day):
                live_bar = {"d": day, "o": 90, "h": 110, "l": 80, "c": 100, "v": 200}
                with patch("app.services.bars._local_now", return_value=datetime(2026, 11, 23, 10, tzinfo=ZoneInfo("America/New_York"))):
                    payload = signals.build_indicator_history("NASDAQ:INTC", session_bar=live_bar, fetch_session_bar=False)
                self.assertEqual(payload["points"][-1]["date"], "2026-11-20")
                self.assertEqual(payload["points"][-1]["close"], 11)

    def test_completed_session_does_not_replace_a_database_bar(self):
        self._stock()
        live_bar = {"d": "2026-11-20", "o": 90, "h": 110, "l": 80, "c": 100, "v": 200}
        payload = signals.build_indicator_history("NASDAQ:INTC", session_bar=live_bar, fetch_session_bar=False)
        self.assertEqual(payload["points"][-1]["close"], 11)

    def test_older_page_never_requests_an_unfinished_candle(self):
        self._stock()
        with (
            patch("app.services.ledger.us_equity_session_date", return_value=date(2026, 11, 23)),
            patch("app.services.bars._local_now", return_value=datetime(2026, 11, 23, 10, tzinfo=ZoneInfo("America/New_York"))),
            patch("app.services.bars.chart_session_bar") as live,
        ):
            payload = signals.build_indicator_history("NASDAQ:INTC", before="2026-11-15")
        self.assertLess(payload["points"][-1]["date"], "2026-11-15")
        live.assert_not_called()

    def test_etf_and_hk_trends_keep_the_original_daily_source(self):
        for symbol in ("NASDAQ:QQQ", "SSE:512480", "NYSE:SSG", "HKEX:00005"):
            with self.subTest(symbol=symbol):
                with (
                    patch("app.services.bars.load_symbol_bars", return_value=self._daily_bars()) as daily,
                    patch.object(market_bars, "load_market_bars", side_effect=AssertionError("legacy product read market bars")) as market,
                ):
                    payload = signals.build_indicator_history(symbol, fetch_session_bar=False)
                self.assertEqual(payload["points"][-1]["close"], 105)
                self.assertTrue(payload["_history_source_version"].startswith("daily_bar:v1:"))
                self.assertEqual(daily.call_args.args[1], symbol)
                market.assert_not_called()

    def test_price_advice_default_loader_is_unchanged(self):
        with (
            patch("app.services.bars.load_symbol_bars", return_value=self._daily_bars()) as daily,
            patch.object(price_history, "load_history_bars") as market,
        ):
            bars = signals._load_bars("NASDAQ:INTC")
        self.assertEqual(bars[-1]["c"], 105)
        self.assertEqual(daily.call_args.args[1], "NASDAQ:INTC")
        market.assert_not_called()

    def test_stock_cache_rejects_legacy_source_refills_and_session_changes(self):
        stock = self._stock()
        symbol = "NASDAQ:INTC"
        payload = signals.build_indicator_history(symbol, fetch_session_bar=False)
        cached = IndicatorTrendCache(symbol=symbol, payload_blob=indicator_monitor._encode(payload))
        self.db.add(cached)
        self.db.commit()
        self.assertEqual(indicator_monitor.cached_indicator_history(self.db, symbol), payload)

        legacy = {key: value for key, value in payload.items() if key != "_history_source_version"}
        cached.payload_blob = indicator_monitor._encode(legacy)
        self.db.commit()
        self.assertIsNone(indicator_monitor.cached_indicator_history(self.db, symbol))

        cached.payload_blob = indicator_monitor._encode(payload)
        stock.fetched_at = datetime(2026, 11, 21, 10)
        self.db.commit()
        self.assertIsNone(indicator_monitor.cached_indicator_history(self.db, symbol))

        payload["_history_source_version"] = price_history.history_version(self.db, symbol)
        cached.payload_blob = indicator_monitor._encode(payload)
        self.db.commit()
        with patch("app.services.bars._local_now", return_value=datetime(2026, 11, 23, 10, tzinfo=ZoneInfo("America/New_York"))):
            self.assertIsNone(indicator_monitor.cached_indicator_history(self.db, symbol))
        with patch.object(market_bars, "last_session", return_value=date(2026, 11, 23)):
            self.assertIsNone(indicator_monitor.cached_indicator_history(self.db, symbol))

    def test_priming_rebuilds_legacy_stock_cache_without_notifications(self):
        self._stock()
        symbol = "NASDAQ:INTC"
        self.db.add(Trade(symbol=symbol, side="Buy", qty=1, closing_time=datetime(2026, 1, 1)))
        self.db.add(IndicatorTrendCache(symbol=symbol, payload_blob=indicator_monitor._encode({"symbol": symbol, "points": []})))
        self.db.commit()
        with (
            patch.object(indicator_monitor, "SessionLocal", self.sessions),
            patch.object(indicator_monitor._stop, "is_set", return_value=False),
            patch("app.services.bars.chart_session_bar") as live,
            patch("app.services.dingtalk.send_markdown") as notify,
        ):
            self.assertEqual(indicator_monitor.prime_indicator_caches(), 1)
        self.db.expire_all()
        payload = indicator_monitor.cached_indicator_history(self.db, symbol)
        self.assertEqual(payload["points"][-1]["close"], 11)
        live.assert_not_called()
        notify.assert_not_called()

    def test_etf_cache_expires_when_a_completed_bar_is_archived_or_the_market_closes(self):
        symbol = "NYSE:SSG"
        payload = {
            "symbol": symbol,
            "points": [],
            "_history_source_version": price_history.history_version(self.db, symbol),
            "_indicator_columns_version": signals.INDICATOR_COLUMNS_VERSION,
        }
        cached = IndicatorTrendCache(symbol=symbol, payload_blob=indicator_monitor._encode(payload))
        self.db.add(cached)
        self.db.commit()
        self.assertEqual(indicator_monitor.cached_indicator_history(self.db, symbol), payload)
        self.db.add(DailyBar(symbol="SSG", bar_date=self.day, open=10, high=12, low=9, close=11, volume=100))
        self.db.commit()
        self.assertIsNone(indicator_monitor.cached_indicator_history(self.db, symbol))

        payload["_history_source_version"] = price_history.history_version(self.db, symbol)
        cached.payload_blob = indicator_monitor._encode(payload)
        self.db.commit()
        with patch("app.services.bars.frozen_through", return_value=date(2026, 11, 23)):
            self.assertIsNone(indicator_monitor.cached_indicator_history(self.db, symbol))

    def test_nontrading_days_and_early_close_never_request_a_stock_live_candle(self):
        self._stock()
        for stamp in (
            datetime(2026, 11, 26, 12, tzinfo=ZoneInfo("America/New_York")),
            datetime(2026, 11, 27, 13, 1, tzinfo=ZoneInfo("America/New_York")),
            datetime(2026, 11, 21, 12, tzinfo=ZoneInfo("America/New_York")),
        ):
            with self.subTest(stamp=stamp):
                with (
                    patch("app.services.bars._local_now", return_value=stamp),
                    patch("app.services.bars.chart_session_bar") as live,
                ):
                    payload = signals.build_indicator_history("NASDAQ:INTC")
                self.assertEqual(payload["points"][-1]["date"], "2026-11-20")
                live.assert_not_called()

    def test_etf_source_version_includes_bars_saved_by_its_first_history_load(self):
        with patch("app.services.data_sources.fetch_daily_bars", return_value=self._daily_bars()):
            payload = signals.build_indicator_history("NASDAQ:QQQ", fetch_session_bar=False)
        self.assertEqual(self.db.query(DailyBar).count(), 45)
        self.assertEqual(payload["_history_source_version"], price_history.history_version(self.db, "NASDAQ:QQQ"))


if __name__ == "__main__":
    unittest.main()

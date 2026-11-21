from datetime import date, datetime, timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.entities import DailyBar, MarketBar, MarketSymbol, Trade
from app.services import ak_backtest, market_bars


class BacktestDataTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.day = date(2026, 11, 20)
        ak_backtest._cache.clear()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()
        ak_backtest._cache.clear()

    def _stock(self, market="us", key="INTC", count=45, complete=True):
        row = MarketSymbol(market=market, symbol=key, fetch_state="ok", bars_through=self.day)
        self.db.add(row)
        self.db.flush()
        rows = []
        for offset in range(count - 1, -1, -1):
            history = (self.day - timedelta(days=offset), 11.0, 100, None)
            rows.append(history + (10.0, 12.0, 9.0) if complete else history)
        market_bars.write_bars(self.db, row.id, rows)
        self.db.commit()
        return row

    def test_stock_backtest_uses_market_prices_even_when_daily_cache_differs(self):
        self._stock()
        self.db.add(DailyBar(symbol="INTC", bar_date=self.day, open=100, high=110, low=90, close=105, volume=300))
        self.db.commit()
        result = SimpleNamespace(
            metrics=SimpleNamespace(end_market_value=1000, total_return_pct=0, max_drawdown_pct=0, closed_trade_count=0),
            orders_df=pd.DataFrame(), trades_df=pd.DataFrame(), equity_curve=pd.Series(dtype=float),
        )
        with (
            patch.object(market_bars, "last_session", return_value=self.day),
            patch("app.services.bars.load_symbol_bars", side_effect=AssertionError("stock backtest fetched daily bars")) as daily,
            patch("akquant.run_backtest", return_value=result) as run,
            patch.object(ak_backtest, "_cycle_rows", return_value=[]),
        ):
            item = ak_backtest._one(self.db, "NASDAQ:INTC", None, "us", None, self.day, 1000)

        daily.assert_not_called()
        self.assertEqual(item["error"], "")
        self.assertEqual(item["bars"], 45)
        frame = run.call_args.kwargs["data"]
        self.assertEqual(frame.iloc[-1][["open", "high", "low", "close"]].tolist(), [10.0, 12.0, 9.0, 11.0])

    def test_missing_stock_ohlc_does_not_fetch_or_run_on_fabricated_prices(self):
        self._stock(complete=False)
        with (
            patch.object(market_bars, "last_session", return_value=self.day),
            patch("app.services.bars.load_symbol_bars") as daily,
            patch("akquant.run_backtest") as run,
        ):
            item = ak_backtest._one(self.db, "NASDAQ:INTC", None, "us", None, self.day, 1000)

        self.assertIn("缺少完整 OHLC", item["error"])
        daily.assert_not_called()
        run.assert_not_called()

    def test_missing_stock_is_reported_without_daily_fallback(self):
        with (
            patch.object(market_bars, "last_session", return_value=self.day),
            patch("app.services.bars.load_symbol_bars") as daily,
        ):
            item = ak_backtest._one(self.db, "NASDAQ:INTC", None, "us", None, self.day, 1000)

        self.assertIn("全市场数据库中没有", item["error"])
        daily.assert_not_called()

    def test_etfs_keep_the_original_daily_bar_source(self):
        for symbol in (
            "SSE:512480", "SZSE:159934", "SPY", "NASDAQ:QQQ", "RKLX",
            "NASDAQ:SOXX", "NYSE:EWT", "NYSE:CRCA", "NYSE:CRCD", "NYSE:NASA",
            "NYSE:OKLS", "NYSE:RKLZ", "NYSE:SSG", "NYSE:TSLQ",
        ):
            with self.subTest(symbol=symbol):
                with (
                    patch("app.services.bars.load_symbol_bars", return_value=[{"d": "2026-11-20", "c": 15}]) as daily,
                    patch.object(market_bars, "load_market_bars", side_effect=AssertionError("ETF read market store")),
                ):
                    bars = ak_backtest._backtest_bars(self.db, symbol)
                self.assertEqual(bars[0]["c"], 15)
                daily.assert_called_once_with(self.db, symbol)

    def test_spcx_current_equity_uses_market_store_not_its_historical_etf_type(self):
        self.assertTrue(ak_backtest._uses_market_store("NYSE:SPCX"))

    def test_stock_candles_read_the_same_market_ohlc(self):
        self._stock(market="cn", key="sh601012", count=1)
        with (
            patch.object(market_bars, "last_session", return_value=self.day),
            patch("app.services.bars.load_symbol_bars", side_effect=AssertionError("stock candles fetched daily bars")),
        ):
            payload = ak_backtest.backtest_candles(self.db, "SSE:601012", self.day, self.day)

        candle = payload["candles"][0]
        self.assertEqual([candle[field] for field in ("o", "h", "l", "c")], [10.0, 12.0, 9.0, 11.0])

    def test_etf_candles_keep_the_original_daily_source(self):
        bar = {"d": "2026-11-20", "o": 1.0, "h": 1.2, "l": 0.9, "c": 1.1, "v": 100}
        with patch("app.services.bars.load_symbol_bars", return_value=[bar]) as daily:
            payload = ak_backtest.backtest_candles(self.db, "SSE:512480", self.day, self.day)

        daily.assert_called_once_with(self.db, "SSE:512480")
        self.assertEqual(payload["candles"][0]["h"], 1.2)

    def test_stock_pool_only_contains_holdings_and_closed_positions(self):
        self._stock()
        self._stock(key="RKLB")
        self.db.add_all([
            Trade(symbol="NASDAQ:INTC", side="Buy", qty=1, closing_time=datetime(2026, 1, 1)),
            Trade(symbol="NASDAQ:RKLB", side="Buy", qty=1, closing_time=datetime(2026, 1, 1)),
            Trade(symbol="NASDAQ:RKLB", side="Sell", qty=1, closing_time=datetime(2026, 2, 1)),
            Trade(symbol="SSE:601012", side="Buy", qty=1, closing_time=datetime(2026, 1, 1)),
        ])
        self.db.commit()

        self.assertEqual(ak_backtest._symbols(self.db, "us"), ["NASDAQ:INTC", "NASDAQ:RKLB"])
        self.assertEqual(ak_backtest._symbols(self.db, "cn"), ["SSE:601012"])

    def test_empty_ledger_does_not_backtest_watchlist_or_fetch_benchmark(self):
        with (
            patch.object(ak_backtest, "_one") as run,
            patch.object(ak_backtest, "_backtest_bars") as bars,
            patch.object(ak_backtest, "_mood") as mood,
        ):
            payload = ak_backtest.run_book_backtest(self.db, "us", end=self.day)
            cn_payload = ak_backtest.run_book_backtest(self.db, "cn", end=self.day)
        self.assertEqual(payload["items"], [])
        self.assertEqual(cn_payload["items"], [])
        run.assert_not_called()
        bars.assert_not_called()
        mood.assert_not_called()

    def test_backtest_cache_changes_after_market_history_is_refilled(self):
        stock = self._stock()
        self.db.add(Trade(symbol="NASDAQ:INTC", side="Buy", qty=1, closing_time=datetime(2026, 1, 1)))
        self.db.commit()

        def run_symbol(db, symbol, name, market, start, end, cash, mood, **kwargs):
            return {"symbol": symbol, "cash": cash, "end_value": cash, "trades": [], "curve": {}, "closes": {}, "cycles": []}

        with (
            patch.object(ak_backtest, "_one", side_effect=run_symbol) as run,
            patch.object(ak_backtest, "_backtest_bars", return_value=[]) as benchmark,
        ):
            ak_backtest.run_book_backtest(self.db, "us", end=self.day)
            ak_backtest.run_book_backtest(self.db, "us", end=self.day)
            self.assertEqual(run.call_count, 1)
            stock.fetched_at = datetime(2026, 11, 21, 10, 0)
            self.db.commit()
            ak_backtest.run_book_backtest(self.db, "us", end=self.day)
            self.assertEqual(run.call_count, 2)

        self.assertEqual(run.call_args.args[1], "NASDAQ:INTC")
        self.assertEqual(benchmark.call_args.args[1], "SPY")


if __name__ == "__main__":
    unittest.main()

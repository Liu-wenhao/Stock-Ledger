from datetime import datetime
from decimal import Decimal
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import portfolio
from app.core.database import Base, get_db
from app.main import MarketScopeMiddleware
from app.models.entities import IndicatorTrendCache, Portfolio, Trade
from app.services import ak_backtest, indicator_monitor
from app.services.position_scope import position_symbols


class PositionScopeTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.db.add_all([Portfolio(id=1, name="Default"), Portfolio(id=2, name="Other")])
        self.db.add_all([
            self._trade("NASDAQ:INTC", "Buy", "0.000001"),
            self._trade("NASDAQ:RKLB", "Buy", "5"),
            self._trade("NASDAQ:RKLB", "Sell", "5", month=2),
            self._trade("SSE:601012", "Buy", "100"),
            self._trade("SSE:600362", "Buy", "100"),
            self._trade("SSE:600362", "Sell", "100", month=2),
            self._trade("HKEX:03121", "Buy", "100"),
            self._trade("NASDAQ:AMD", "Dividend", "0"),
            self._trade("SZSE:159934", "Dividend", "0"),
            self._trade("NASDAQ:OTHER", "Buy", "1", portfolio_id=2),
            self._trade("CASH", "Deposit", "0"),
        ])
        self.db.add(IndicatorTrendCache(symbol="SSE:512480", payload_blob=indicator_monitor._encode({"symbol": "SSE:512480", "points": []})))
        self.db.commit()
        api = FastAPI()
        api.add_middleware(MarketScopeMiddleware)
        api.include_router(portfolio.router)
        api.dependency_overrides[get_db] = lambda: self.db
        self.client = TestClient(api)

    def tearDown(self):
        self.client.close()
        self.db.close()
        self.engine.dispose()

    def _trade(self, symbol, side, quantity, month=1, portfolio_id=1):
        return Trade(
            portfolio_id=portfolio_id,
            symbol=symbol,
            side=side,
            qty=Decimal(quantity),
            fill_price=Decimal("10"),
            closing_time=datetime(2026, month, 1),
        )

    def test_scope_includes_held_and_closed_but_not_watch_dividend_or_cash_rows(self):
        self.assertEqual(position_symbols(self.db, 1, "us"), ["NASDAQ:INTC", "NASDAQ:RKLB"])
        self.assertEqual(position_symbols(self.db, 1, "cn"), ["SSE:600362", "SSE:601012"])
        self.assertEqual(position_symbols(self.db, 1, "hk"), ["HKEX:03121"])
        self.assertEqual(position_symbols(self.db, 2, "us"), ["NASDAQ:OTHER"])
        self.assertEqual(position_symbols(self.db, 1, "us", include_closed=False), ["NASDAQ:INTC"])
        self.assertEqual(indicator_monitor._monitored_symbols(self.db), position_symbols(self.db, 1, include_closed=False))
        self.assertEqual(ak_backtest._symbols(self.db, "cn"), position_symbols(self.db, 1, "cn"))

    def test_reopened_position_only_appears_once(self):
        self.db.add(self._trade("NASDAQ:RKLB", "Buy", "2", month=3))
        self.db.commit()
        self.assertEqual(position_symbols(self.db, 1, "us"), ["NASDAQ:INTC", "NASDAQ:RKLB"])

    def test_history_rejects_ineligible_symbols_before_cache_or_provider_access(self):
        with (
            patch.object(indicator_monitor, "cached_indicator_history") as cache,
            patch.object(portfolio, "build_indicator_history") as history,
        ):
            for symbol, market in (
                ("SSE:512480", "cn"),
                ("SZSE:159934", "cn"),
                ("NASDAQ:INTC", "cn"),
                ("NASDAQ:OTHER", "us"),
                ("NASDAQ:AMD", "us"),
            ):
                with self.subTest(symbol=symbol, market=market):
                    response = self.client.get("/api/holdings/indicator-history", params={"symbol": symbol, "market": market})
                    self.assertEqual(response.status_code, 403)
                    self.assertIn("仅持仓或已清仓", response.json()["detail"])
            response = self.client.get("/api/holdings/indicator-history", params={"symbol": "SSE:512480", "market": "cn", "before": "2026-11-01"})
            self.assertEqual(response.status_code, 403)
        cache.assert_not_called()
        history.assert_not_called()

    def test_history_allows_held_and_closed_symbols_and_normalizes_case(self):
        closed_payload = {"symbol": "SSE:600362", "ticker": "600362", "points": []}
        with (
            patch.object(indicator_monitor, "cached_indicator_history", side_effect=lambda db, symbol: {"symbol": symbol, "ticker": symbol.split(":")[-1], "points": []}) as cache,
            patch.object(portfolio, "build_indicator_history", return_value=closed_payload) as history,
        ):
            for symbol in (" sse:601012 ", "sse:600362"):
                response = self.client.get("/api/holdings/indicator-history", params={"symbol": symbol, "market": "cn"})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["symbol"], symbol.strip().upper())
        self.assertEqual(cache.call_count, 1)
        history.assert_called_once_with("SSE:600362", before=None, limit=40)

    def test_closed_history_does_not_return_stale_monitor_cache(self):
        stale = {"symbol": "SSE:600362", "ticker": "600362", "points": []}
        self.db.add(IndicatorTrendCache(symbol="SSE:600362", payload_blob=indicator_monitor._encode(stale)))
        self.db.commit()
        with (
            patch.object(indicator_monitor, "cached_indicator_history") as cache,
            patch.object(portfolio, "build_indicator_history", return_value={**stale, "since": "2026-11-20"}) as history,
        ):
            response = self.client.get("/api/holdings/indicator-history", params={"symbol": "SSE:600362", "market": "cn"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["since"], "2026-11-20")
        cache.assert_not_called()
        history.assert_called_once()

    def test_closed_history_can_load_older_pages(self):
        payload = {"symbol": "SSE:600362", "ticker": "600362", "points": []}
        with patch.object(portfolio, "build_indicator_history", return_value=payload) as history:
            response = self.client.get("/api/holdings/indicator-history", params={"symbol": "sse:600362", "market": "cn", "before": "2026-11-01", "limit": 60})
        self.assertEqual(response.status_code, 200)
        history.assert_called_once_with("SSE:600362", before="2026-11-01", limit=60)

    def test_candles_reject_ineligible_symbols_before_data_access(self):
        with patch.object(portfolio, "backtest_candles") as candles:
            for symbol in ("SSE:512480", "SZSE:159934", "NASDAQ:INTC"):
                response = self.client.get("/api/analysis/backtest/candles", params={"symbol": symbol, "market": "cn"})
                self.assertEqual(response.status_code, 403)
        candles.assert_not_called()

    def test_candles_allow_closed_symbols(self):
        with patch.object(portfolio, "backtest_candles", return_value={"symbol": "SSE:600362", "candles": []}) as candles:
            response = self.client.get("/api/analysis/backtest/candles", params={"symbol": " sse:600362 ", "market": "cn"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(candles.call_args.args[1], "SSE:600362")

    def test_history_surfaces_missing_database_ohlc_as_422(self):
        problem = "全市场数据库 sh600362 缺少完整 OHLC，请先回填日线"
        with patch.object(portfolio, "build_indicator_history", side_effect=ValueError(problem)):
            response = self.client.get("/api/holdings/indicator-history", params={"symbol": "SSE:600362", "market": "cn"})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"], problem)

    def test_candles_surface_missing_database_ohlc_as_422(self):
        problem = "全市场数据库 sh600362 缺少完整 OHLC，请先回填日线"
        with patch.object(portfolio, "backtest_candles", side_effect=ValueError(problem)):
            response = self.client.get("/api/analysis/backtest/candles", params={"symbol": "SSE:600362", "market": "cn"})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"], problem)

    def test_history_source_marker_is_not_exposed_by_the_api(self):
        payload = {"symbol": "SSE:600362", "ticker": "600362", "points": [], "_history_source_version": "internal"}
        with patch.object(portfolio, "build_indicator_history", return_value=payload):
            response = self.client.get("/api/holdings/indicator-history", params={"symbol": "SSE:600362", "market": "cn"})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_history_source_version", response.json())


if __name__ == "__main__":
    unittest.main()

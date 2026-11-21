from datetime import date, datetime, timedelta
from decimal import Decimal
import gzip
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.entities import DailyBar, DailyNav, MarketBar, MarketSymbol, Portfolio, Trade
from app.services.ledger import replay
from scripts.seed_cn_positions import lot_quantity, seed_positions


class SeedCnPositionsTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.temp = TemporaryDirectory()
        self.backup_dir = Path(self.temp.name)
        self.day = date(2026, 11, 20)
        self.symbols = ["SSE:601012", "SSE:513400"]
        self.watch_patch = patch("scripts.seed_cn_positions.watch_symbols", return_value=self.symbols)
        self.watch_patch.start()
        self.db.add_all([Portfolio(id=1, name="Default"), Portfolio(id=2, name="Other")])
        stock = MarketSymbol(market="cn", symbol="sh601012")
        etf = MarketSymbol(market="cn", symbol="sh513400")
        self.db.add_all([stock, etf])
        self.db.flush()
        self.db.add_all([
            MarketBar(symbol_id=stock.id, bar_date=self.day, close=Decimal("10.89")),
            MarketBar(symbol_id=etf.id, bar_date=self.day, close=Decimal("50")),
            DailyBar(symbol="601012.SS", bar_date=self.day, close=Decimal("100")),
            DailyBar(symbol="513400.SS", bar_date=self.day, close=Decimal("1.208")),
            DailyBar(symbol="513400.SS", bar_date=self.day + timedelta(days=1), close=Decimal("2")),
            Trade(symbol="NASDAQ:INTC", side="Buy", qty=1, fill_price=50, closing_time=datetime(2026, 1, 1)),
            DailyNav(portfolio_id=1, nav_date=self.day, total_value=50, holdings_value=50),
        ])
        self.db.commit()

    def tearDown(self):
        self.watch_patch.stop()
        self.db.close()
        self.engine.dispose()
        self.temp.cleanup()

    def _seed(self, apply=False):
        return seed_positions(self.db, apply=apply, portfolio_id=1, through=self.day, backup_dir=self.backup_dir)

    def test_lots_use_decimal_floor_and_never_exceed_budget(self):
        for price, expected in (("1.208", 20600), ("10.89", 2200), ("28.4", 800), ("250", 100)):
            with self.subTest(price=price):
                close = Decimal(price)
                quantity = lot_quantity(Decimal("25000"), close)
                self.assertEqual(quantity, Decimal(expected))
                self.assertEqual(quantity % 100, 0)
                self.assertLessEqual(quantity * close, Decimal("25000"))
                self.assertGreater((quantity + 100) * close, Decimal("25000"))

    def test_invalid_budget_price_and_insufficient_lot_are_rejected(self):
        for budget in ("0", "-1", "NaN", "Infinity"):
            with self.subTest(budget=budget), self.assertRaises(ValueError):
                lot_quantity(Decimal(budget), Decimal("1"))
        for close in ("0", "-1", "NaN", "Infinity", "250.01"):
            with self.subTest(close=close), self.assertRaises(ValueError):
                lot_quantity(Decimal("25000"), Decimal(close))

    def test_preview_uses_correct_sources_and_completed_close_without_writing(self):
        result = self._seed()
        self.assertFalse(result["applied"])
        self.assertEqual(result["created"], 0)
        self.assertEqual([(item["symbol"], item["close"], item["quantity"]) for item in result["positions"]], [
            ("SSE:601012", Decimal("10.89"), Decimal("2200")),
            ("SSE:513400", Decimal("1.208"), Decimal("20600")),
        ])
        self.assertEqual(self.db.query(Trade).count(), 1)
        self.assertEqual(list(self.backup_dir.iterdir()), [])

    def test_apply_backs_up_and_preserves_us_trades_and_nav(self):
        result = self._seed(apply=True)
        self.assertEqual(result["created"], 2)
        with gzip.open(result["backup"], "rt", encoding="utf-8") as backup:
            snapshot = json.load(backup)
        self.assertEqual(len(snapshot["trades"]), 1)
        self.assertEqual(snapshot["trades"][0]["symbol"], "NASDAQ:INTC")
        trades = self.db.query(Trade).filter(Trade.symbol.in_(self.symbols)).all()
        state = replay(trades)
        for item in result["positions"]:
            position = state.positions[item["symbol"]]
            self.assertEqual(position.quantity, item["quantity"])
            self.assertEqual(position.avg_price, item["close"])
            self.assertEqual(position.cost_basis, item["cost"])
        self.assertTrue(all(trade.closing_time == datetime(2026, 11, 20, 23) and trade.commission == 0 for trade in trades))
        us = self.db.query(Trade).filter(Trade.symbol == "NASDAQ:INTC").one()
        self.assertEqual((us.qty, us.fill_price), (Decimal("1"), Decimal("50")))
        self.assertEqual(self.db.query(DailyNav).one().total_value, Decimal("50"))
        self.assertEqual(result["total_cost"] + result["unused_budget"], Decimal("50000"))

    def test_rerun_does_not_add_to_or_reopen_seeded_positions(self):
        first = self._seed(apply=True)
        self.db.add(Trade(symbol="SSE:601012", side="Sell", qty=2200, fill_price=11, closing_time=datetime(2026, 11, 21, 23)))
        self.db.commit()
        second = self._seed(apply=True)
        self.assertEqual(second["created"], 0)
        self.assertEqual(second["positions"], [])
        self.assertIsNone(second["backup"])
        self.assertEqual(self.db.query(Trade).count(), 4)
        self.assertEqual(len(list(self.backup_dir.iterdir())), 1)
        self.assertIsNotNone(first["backup"])

    def test_existing_manual_position_is_not_overwritten(self):
        self.db.add(Trade(symbol="SSE:601012", side="Buy", qty=100, fill_price=8, closing_time=datetime(2026, 1, 1)))
        self.db.commit()
        result = self._seed(apply=True)
        self.assertEqual(result["created"], 1)
        self.assertEqual(result["positions"][0]["symbol"], "SSE:513400")
        manual = self.db.query(Trade).filter(Trade.symbol == "SSE:601012").one()
        self.assertEqual((manual.qty, manual.fill_price), (Decimal("100"), Decimal("8")))

    def test_other_portfolio_history_does_not_skip_default_portfolio(self):
        self.db.add(Trade(portfolio_id=2, symbol="SSE:601012", side="Buy", qty=500, fill_price=8, closing_time=datetime(2026, 1, 1)))
        self.db.commit()
        result = self._seed(apply=True)
        self.assertEqual(result["created"], 2)
        other = self.db.query(Trade).filter(Trade.portfolio_id == 2).one()
        self.assertEqual((other.qty, other.fill_price), (Decimal("500"), Decimal("8")))

    def test_missing_latest_etf_close_aborts_entire_plan(self):
        self.db.query(DailyBar).filter(DailyBar.symbol == "513400.SS", DailyBar.bar_date == self.day).delete()
        self.db.commit()
        with self.assertRaisesRegex(ValueError, "缺少最近已收盘日"):
            self._seed(apply=True)
        self.assertEqual(self.db.query(Trade).count(), 1)
        self.assertEqual(list(self.backup_dir.iterdir()), [])

    def test_invalid_close_aborts_without_partial_writes(self):
        self.db.query(DailyBar).filter(DailyBar.symbol == "513400.SS", DailyBar.bar_date == self.day).update({"close": 0})
        self.db.commit()
        with self.assertRaisesRegex(ValueError, "收盘价必须为正数"):
            self._seed(apply=True)
        self.assertEqual(self.db.query(Trade).count(), 1)
        self.assertEqual(list(self.backup_dir.iterdir()), [])

    def test_failed_commit_rolls_back_new_trades(self):
        with patch.object(self.db, "commit", side_effect=RuntimeError("write failed")):
            with self.assertRaisesRegex(RuntimeError, "write failed"):
                self._seed(apply=True)
        self.assertEqual(self.db.query(Trade).count(), 1)
        self.assertEqual(len(list(self.backup_dir.iterdir())), 1)


if __name__ == "__main__":
    unittest.main()

from datetime import date, datetime
import unittest
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.entities import StockNews, SymbolMeta
from app.schemas.news import NewsBatchIn, NewsItemIn
from app.services.news import ingest_news


class NewsIngestTest(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite://")
        SymbolMeta.__table__.create(engine)
        StockNews.__table__.create(engine)
        self.db = sessionmaker(bind=engine)()
        self.db.add(SymbolMeta(symbol="NASDAQ:RKLB", yahoo_symbol="RKLB"))
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def payload(self, url: str) -> NewsBatchIn:
        return NewsBatchIn(
            items=[
                NewsItemIn(
                    symbols=["RKLB", "NASDAQ:RKLB"],
                    title="Rocket Lab announces a launch",
                    source="Grok Bot",
                    url=url,
                    published_at=datetime(2026, 11, 18, 16, 30, tzinfo=ZoneInfo("America/New_York")),
                )
            ]
        )

    def test_aliases_and_tracking_urls_are_idempotent(self):
        first = ingest_news(self.db, self.payload("https://example.com/story?utm_source=x"))
        second = ingest_news(self.db, self.payload("https://example.com/story?utm_source=y"))

        self.assertEqual(first, {"articles": 1, "rows": 2, "inserted": 1, "duplicates": 1})
        self.assertEqual(second, {"articles": 1, "rows": 2, "inserted": 0, "duplicates": 2})
        row = self.db.query(StockNews).one()
        self.assertEqual(row.symbol, "NASDAQ:RKLB")
        self.assertEqual(row.session_date, date(2026, 11, 19))


if __name__ == "__main__":
    unittest.main()

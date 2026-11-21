from contextlib import asynccontextmanager
import logging

from urllib.parse import parse_qs

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api import market_api, news, portfolio, trades
from app.core.config import get_settings
from app.core.database import Base, SessionLocal, engine
from app.models.entities import Portfolio

settings = get_settings()
logger = logging.getLogger(__name__)


def _ensure_schema() -> None:
    """Additive column patches for existing DBs (create_all does not ALTER)."""
    from app.services.market_bars import ensure_ohlc_schema

    ensure_ohlc_schema(engine)
    stmts = [
        "ALTER TABLE price_cache ADD COLUMN price_as_of DATETIME NULL",
        "ALTER TABLE price_cache MODIFY COLUMN price_as_of DATETIME NULL",
        "ALTER TABLE daily_nav ADD COLUMN investment_total DECIMAL(18, 6) NOT NULL DEFAULT 0",
        "ALTER TABLE position_stop ADD COLUMN target_qty DECIMAL(18, 6) NULL",
        "ALTER TABLE position_stop ADD COLUMN max_buy_amount DECIMAL(18, 6) NULL",
        "ALTER TABLE position_stop ADD COLUMN take_profit_price DECIMAL(18, 6) NULL",
        "ALTER TABLE position_stop ADD COLUMN sleeve VARCHAR(16) NULL",
        "ALTER TABLE arms_reading ADD COLUMN above_ma20 DECIMAL(8, 2) NULL",
        "ALTER TABLE arms_reading ADD COLUMN ma_valid INT NULL",
        "ALTER TABLE arms_reading ADD COLUMN ma_total INT NULL",
        "ALTER TABLE ma_anchor MODIFY COLUMN symbol VARCHAR(32) NOT NULL",
        "ALTER TABLE arms_reading ADD COLUMN book VARCHAR(16) NOT NULL DEFAULT 'legacy'",
        "ALTER TABLE breadth_day ADD COLUMN book VARCHAR(16) NOT NULL DEFAULT 'legacy'",
        "ALTER TABLE arms_reading DROP PRIMARY KEY, ADD PRIMARY KEY (market, bar_date, phase, book)",
        "ALTER TABLE breadth_day DROP PRIMARY KEY, ADD PRIMARY KEY (market, bar_date, book)",
        (
            "UPDATE price_cache SET price_as_of = fetched_at "
            "WHERE price_as_of IS NOT NULL "
            "AND TIME(price_as_of) = '00:00:00' "
            "AND fetched_at IS NOT NULL"
        ),
    ]
    with engine.begin() as conn:
        for sql in stmts:
            try:
                conn.execute(text(sql))
            except Exception as exc:
                # Duplicate column / already migrated — ignore
                msg = str(exc).lower()
                if "duplicate" in msg or "exists" in msg or "1060" in msg:
                    continue
                logger.debug("schema patch skipped: %s (%s)", sql, exc)


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    _ensure_schema()
    db: Session = SessionLocal()
    try:
        existing = db.query(Portfolio).filter(Portfolio.id == settings.default_portfolio_id).first()
        if not existing:
            db.add(
                Portfolio(
                    id=settings.default_portfolio_id,
                    name=settings.default_portfolio_name,
                    base_currency=settings.base_currency,
                )
            )
            db.commit()
    finally:
        db.close()


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    from app.services.bar_archive import start_bar_archive, stop_bar_archive
    from app.services.indicator_monitor import start_indicator_monitor, stop_indicator_monitor
    from app.services.sentiment import start_close_capture, stop_close_capture

    start_bar_archive()
    start_close_capture()
    start_indicator_monitor()
    try:
        yield
    finally:
        stop_indicator_monitor()
        stop_bar_archive()
        stop_close_capture()


class MarketScopeMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        from app.services.market_scope import apply_market, clear_market

        raw = parse_qs(scope.get("query_string", b"").decode())
        token = apply_market((raw.get("market") or ["us"])[0])
        try:
            await self.app(scope, receive, send)
        finally:
            clear_market(token)


app = FastAPI(title="Stock Ledger API", version="1.0.0", lifespan=lifespan)

app.add_middleware(MarketScopeMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(trades.router)
app.include_router(portfolio.router)
app.include_router(market_api.router)
app.include_router(news.router)


@app.get("/api/health")
def health():
    return {"status": "ok"}

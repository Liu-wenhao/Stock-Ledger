from __future__ import annotations

import logging
import threading
import time
from datetime import date, datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.models.entities import DailyBar
from app.services.bars import bar_key, load_symbol_bars
from app.services.market_scope import market_of
from app.services.position_scope import position_symbols
from app.services.price_history import uses_market_store
from app.services.trading_sessions import completed_session

logger = logging.getLogger(__name__)
_RETRY_SECONDS = 600
_retry_at: dict[tuple[str, date], float] = {}
_stop = threading.Event()
_thread: threading.Thread | None = None


def _stored_through(db: Session, key: str, through: date) -> date | None:
    return (
        db.query(func.max(DailyBar.bar_date))
        .filter(DailyBar.symbol == key, DailyBar.bar_date <= through)
        .scalar()
    )


def archive_completed_position_bars(now: datetime | None = None) -> int:
    """Persist completed legacy-source bars for held and liquidated positions without alerts."""
    db = SessionLocal()
    archived = 0
    try:
        symbols = position_symbols(db, get_settings().default_portfolio_id, include_closed=True)
        dates: dict[str, date | None] = {}
        targets: dict[tuple[str, date], str] = {}
        for symbol in symbols:
            if uses_market_store(symbol):
                continue
            market = market_of(symbol)
            if market not in dates:
                dates[market] = completed_session(market, now)
            through = dates[market]
            if through is not None:
                targets[(symbol, through)] = bar_key(symbol)
        for retry_key in list(_retry_at):
            if retry_key not in targets:
                _retry_at.pop(retry_key)
        for (symbol, through), key in targets.items():
            if _stop.is_set():
                break
            stored = _stored_through(db, key, through)
            if stored is not None and stored >= through:
                _retry_at.pop((symbol, through), None)
                continue
            if time.monotonic() < _retry_at.get((symbol, through), 0):
                continue
            try:
                load_symbol_bars(db, symbol, frozen=through)
                stored = _stored_through(db, key, through)
                if stored is None or stored < through:
                    _retry_at[(symbol, through)] = time.monotonic() + _RETRY_SECONDS
                    logger.warning("daily archive waiting for %s through %s", symbol, through)
                    continue
                _retry_at.pop((symbol, through), None)
                archived += 1
            except Exception:
                db.rollback()
                _retry_at[(symbol, through)] = time.monotonic() + _RETRY_SECONDS
                logger.exception("daily archive failed for %s through %s", symbol, through)
    finally:
        db.close()
    return archived


def _loop() -> None:
    while not _stop.is_set():
        started = time.monotonic()
        try:
            archived = archive_completed_position_bars()
            if archived:
                logger.info("daily archive refreshed %s position symbols", archived)
        except Exception:
            logger.exception("position daily archive failed")
        _stop.wait(max(1, 60 - (time.monotonic() - started)))


def start_bar_archive() -> None:
    global _thread

    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, daemon=True, name="position-bar-archive")
    _thread.start()
    logger.warning("position daily archive started")


def stop_bar_archive() -> None:
    _stop.set()

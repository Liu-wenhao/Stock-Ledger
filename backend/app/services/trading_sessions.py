from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from functools import lru_cache

import pandas as pd

logger = logging.getLogger(__name__)
_CALENDAR_NAMES = {"us": "XNYS", "cn": "XSHG", "hk": "XHKG"}


@lru_cache(maxsize=3)
def _calendar(market: str):
    import exchange_calendars as xcals

    return xcals.get_calendar(_CALENDAR_NAMES[market])


def _stamp(now: datetime | None) -> pd.Timestamp:
    stamp = pd.Timestamp(now or datetime.now(timezone.utc))
    return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")


def completed_session(market: str, now: datetime | None = None) -> date | None:
    """Last actually closed exchange session, excluding lunch, holidays and premarket."""
    try:
        stamp = _stamp(now)
        calendar = _calendar(market)
        session = calendar.date_to_session(stamp.tz_convert(calendar.tz).date().isoformat(), direction="previous")
        if calendar.session_close(session) > stamp:
            session = calendar.previous_session(session)
        return session.date()
    except Exception:
        logger.exception("completed-session calendar failed for %s", market)
        return None


def unfinished_session(market: str, now: datetime | None = None) -> date | None:
    """Current unfinished trading day; lunch is part of the same daily candle."""
    try:
        stamp = _stamp(now)
        calendar = _calendar(market)
        day = stamp.tz_convert(calendar.tz).date()
        if not calendar.is_session(day.isoformat()):
            return None
        if calendar.session_open(day.isoformat()) <= stamp < calendar.session_close(day.isoformat()):
            return day
    except Exception:
        logger.exception("unfinished-session calendar failed for %s", market)
    return None

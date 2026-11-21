from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.models.entities import StockNews, SymbolMeta
from app.schemas.news import NewsBatchIn

_TRACKING_PARAMS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "ref",
    "ref_src",
    "spm",
}


def _canonical_url(raw: str | None) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    try:
        parts = urlsplit(text)
        query = [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if not key.lower().startswith("utm_") and key.lower() not in _TRACKING_PARAMS
        ]
        path = parts.path.rstrip("/") or "/"
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), ""))
    except ValueError:
        return text


def _dedup_key(*, source: str, external_id: str | None, url: str | None, title: str, published_at: datetime) -> str:
    if external_id:
        identity = f"id|{source.strip().lower()}|{external_id.strip()}"
    elif url:
        identity = f"url|{_canonical_url(url)}"
    else:
        identity = (
            f"title|{source.strip().lower()}|{' '.join(title.lower().split())}|"
            f"{published_at.astimezone(timezone.utc).date().isoformat()}"
        )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _symbol_aliases(db: Session) -> tuple[set[str], dict[str, list[str]]]:
    symbols = {str(value).upper() for (value,) in db.query(SymbolMeta.symbol).all()}
    by_ticker: dict[str, list[str]] = {}
    for symbol in symbols:
        ticker = symbol.split(":", 1)[-1]
        by_ticker.setdefault(ticker, []).append(symbol)
    return symbols, by_ticker


def _canonical_symbol(raw: str, symbols: set[str], by_ticker: dict[str, list[str]]) -> str:
    value = raw.strip().upper()
    if value in symbols:
        return value
    if ":" in value:
        # Canonical symbols do not have to be in symbol_meta before the first article arrives.
        exchange, ticker = value.split(":", 1)
        if exchange and ticker:
            return value
    matches = by_ticker.get(value, [])
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError(f"股票代码 {value} 对应多个市场，请传 EXCHANGE:TICKER")
    if value.isalpha():
        # A few existing US holdings use a bare ticker as their canonical symbol.
        return value
    raise ValueError(f"无法识别股票代码 {value}，请传 EXCHANGE:TICKER")


def _next_weekday(value: date) -> date:
    value += timedelta(days=1)
    while value.weekday() >= 5:
        value += timedelta(days=1)
    return value


def _session_date(symbol: str, published_at: datetime, explicit: date | None) -> date:
    if explicit is not None:
        return explicit
    exchange = symbol.split(":", 1)[0] if ":" in symbol else ""
    if exchange in {"SSE", "SZSE"}:
        local = published_at.astimezone(ZoneInfo("Asia/Shanghai"))
        cutoff = time(15, 0)
    elif exchange == "HKEX":
        local = published_at.astimezone(ZoneInfo("Asia/Hong_Kong"))
        cutoff = time(16, 0)
    else:
        local = published_at.astimezone(ZoneInfo("America/New_York"))
        cutoff = time(16, 0)
    day = local.date()
    if day.weekday() >= 5:
        while day.weekday() >= 5:
            day += timedelta(days=1)
        return day
    return _next_weekday(day) if local.time() >= cutoff else day


def ingest_news(db: Session, payload: NewsBatchIn) -> dict:
    known, by_ticker = _symbol_aliases(db)
    candidates: list[StockNews] = []
    seen: set[tuple[str, str]] = set()
    duplicates = 0
    rows_requested = 0

    for item in payload.items:
        key = _dedup_key(
            source=item.source,
            external_id=item.external_id,
            url=item.url,
            title=item.title,
            published_at=item.published_at,
        )
        metadata = (
            json.dumps(item.metadata, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            if item.metadata is not None
            else None
        )
        published_utc = item.published_at.astimezone(timezone.utc).replace(tzinfo=None)
        for raw_symbol in item.symbols:
            rows_requested += 1
            symbol = _canonical_symbol(raw_symbol, known, by_ticker)
            identity = (symbol, key)
            if identity in seen:
                duplicates += 1
                continue
            seen.add(identity)
            candidates.append(
                StockNews(
                    symbol=symbol,
                    dedup_key=key,
                    external_id=item.external_id,
                    title=item.title.strip(),
                    summary=item.summary.strip() if item.summary else None,
                    source=item.source.strip(),
                    url=item.url.strip() if item.url else None,
                    published_at=published_utc,
                    session_date=_session_date(symbol, item.published_at, item.session_date),
                    category=item.category.strip() if item.category else None,
                    sentiment=item.sentiment,
                    metadata_json=metadata,
                )
            )

    if candidates:
        candidate_symbols = {row.symbol for row in candidates}
        candidate_keys = {row.dedup_key for row in candidates}
        existing = {
            (row.symbol, row.dedup_key)
            for row in db.query(StockNews.symbol, StockNews.dedup_key)
            .filter(StockNews.symbol.in_(candidate_symbols), StockNews.dedup_key.in_(candidate_keys))
            .all()
        }
        fresh = [row for row in candidates if (row.symbol, row.dedup_key) not in existing]
        duplicates += len(candidates) - len(fresh)
        db.add_all(fresh)
        db.commit()
    else:
        fresh = []

    return {
        "articles": len(payload.items),
        "rows": rows_requested,
        "inserted": len(fresh),
        "duplicates": duplicates,
    }

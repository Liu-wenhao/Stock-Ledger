"""Full-market daily bars, one row per listed name and session.

The backfill reads each name's forward-adjusted (qfq) history once through
AkShare (Sina). After that the close snapshot adds one row per name, so the
share of names above MA20 comes from stored closes and needs no request per name.

Sina's list `settlement` / `preclose` is also forward-adjusted. When that value
disagrees with the stored previous close, that name's history is rewritten —
usually a new ex-dividend or split. The backfill starts from today's list, so
names delisted earlier are absent.
"""

from __future__ import annotations

import argparse
import math
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import delete, func, insert, inspect, or_, select, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.models.entities import BreadthDay, MarketBar, MarketSymbol
from app.services.market_scope import market_of
from app.services.symbols import quote_code, split_symbol

MARKETS = ("cn", "us")
_PROBE = {"cn": "sh601012", "us": "AAPL"}
_STREAK_LIMIT = 40
_COOLDOWN_SECONDS = 600
_MAX_COOLDOWNS = 3
# MarketBar open/high/low/close are Numeric(14, 4); drop source bars that cannot fit.
_PRICE_LIMIT = 10**10
HistoryBar = (
    tuple[date, float, int, float | None]
    | tuple[date, float, int, float | None, float | None, float | None, float | None]
)


def _price_fits(value: float | None) -> bool:
    if value is None:
        return True
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and abs(number) < _PRICE_LIMIT


def _bar_fits(row: HistoryBar) -> bool:
    if not _price_fits(row[1]):
        return False
    if len(row) == 7:
        return _price_fits(row[4]) and _price_fits(row[5]) and _price_fits(row[6])
    return True


def filter_writable_bars(rows: list[HistoryBar]) -> list[HistoryBar]:
    """Keep only bars whose OHLC fits the market_bar price columns."""
    return [row for row in rows if _bar_fits(row)]


def _log(message: str) -> None:
    print(f"{datetime.now():%H:%M:%S} {message}", flush=True)


def store_key(market: str, symbol: str) -> str:
    """The list sources disagree on class shares (BRK_B, BRK.B). Sina's dotted form is stored."""
    text = (symbol or "").strip()
    if market == "cn":
        return (quote_code(text) or text).lower()
    _exchange, ticker = split_symbol(text)
    return (ticker or "").upper().replace("_", ".")


def ensure_ohlc_schema(engine: Engine) -> None:
    """Add nullable OHLC columns without changing existing close-only history."""
    with engine.begin() as connection:
        columns = {column["name"] for column in inspect(connection).get_columns("market_bar")}
        for column in ("open", "high", "low"):
            if column not in columns:
                connection.execute(text(f"ALTER TABLE market_bar ADD COLUMN {column} DECIMAL(14, 4) NULL"))


def _price(value) -> float | None:
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(price) or price <= 0:
        return None
    return round(price, 4) or None


def load_market_bars(db: Session, symbol: str, *, end: date | None = None) -> list[dict]:
    """Read completed, genuine OHLCV from the full-market database without fetching."""
    market = market_of(symbol)
    if market not in MARKETS:
        return []
    key = store_key(market, symbol)
    through = last_session(market)
    if end is not None:
        through = min(through, end)
    rows = db.execute(
        select(MarketBar)
        .join(MarketSymbol, MarketSymbol.id == MarketBar.symbol_id)
        .where(MarketSymbol.market == market, MarketSymbol.symbol == key, MarketBar.bar_date <= through)
        .order_by(MarketBar.bar_date)
    ).scalars().all()
    bars = []
    for row in rows:
        prices = [_price(value) for value in (row.open, row.high, row.low, row.close)]
        if any(value is None for value in prices):
            raise ValueError(f"全市场数据库 {key} 在 {row.bar_date} 缺少完整 OHLC，请先回填日线")
        opening, high, low, close = prices
        if high < max(opening, close) or low > min(opening, close):
            raise ValueError(f"全市场数据库 {key} 在 {row.bar_date} 的 OHLC 不一致，请重新回填日线")
        volume = float(row.volume or 0)
        bars.append({
            "d": row.bar_date.isoformat(),
            "o": opening,
            "h": high,
            "l": low,
            "c": close,
            "v": volume,
            "dol": float(row.amount) if row.amount is not None else close * volume,
        })
    return bars


def data_version(db: Session, market: str, symbols: list[str]) -> str:
    keys = list(dict.fromkeys(store_key(market, symbol) for symbol in symbols))
    rows = db.execute(
        select(MarketSymbol.symbol, MarketSymbol.bars_from, MarketSymbol.bars_through, MarketSymbol.fetched_at)
        .where(MarketSymbol.market == market, MarketSymbol.symbol.in_(keys))
        .order_by(MarketSymbol.symbol)
    ).all()
    return str(rows)


def last_session(market: str) -> date:
    """Last session whose bar can no longer change."""
    from app.services.bars import cn_frozen, frozen_through

    return cn_frozen() if market == "cn" else frozen_through()


def _years_before(day: date, years: int) -> date:
    try:
        return day.replace(year=day.year - years)
    except ValueError:
        return day.replace(year=day.year - years, day=28)


def _same_close(stored: float, quoted: float) -> bool:
    return abs(stored - quoted) <= max(0.011, abs(stored) * 0.0005)


def _upsert_bars(db: Session):
    """Insert or overwrite one bar. Used for the daily close snapshot."""
    dialect = db.get_bind().dialect.name
    if dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        stmt = sqlite_insert(MarketBar)
        return stmt.on_conflict_do_update(
            index_elements=["symbol_id", "bar_date"],
            set_={
                "open": func.coalesce(stmt.excluded.open, MarketBar.open),
                "high": func.coalesce(stmt.excluded.high, MarketBar.high),
                "low": func.coalesce(stmt.excluded.low, MarketBar.low),
                "close": stmt.excluded.close,
                "volume": stmt.excluded.volume,
                "amount": stmt.excluded.amount,
            },
        )
    from sqlalchemy.dialects.mysql import insert as mysql_insert

    stmt = mysql_insert(MarketBar)
    return stmt.on_duplicate_key_update(
        open=func.coalesce(stmt.inserted.open, MarketBar.open),
        high=func.coalesce(stmt.inserted.high, MarketBar.high),
        low=func.coalesce(stmt.inserted.low, MarketBar.low),
        close=stmt.inserted.close,
        volume=stmt.inserted.volume,
        amount=stmt.inserted.amount,
    )


def symbol_ids(db: Session, market: str) -> dict[str, int]:
    rows = db.execute(select(MarketSymbol.symbol, MarketSymbol.id).where(MarketSymbol.market == market)).all()
    return {symbol: ident for symbol, ident in rows}


def sync_symbols(db: Session, market: str, prices: list[dict]) -> dict[str, int]:
    """Add names the store has not seen. Existing rows keep their read state."""
    known = symbol_ids(db, market)
    fresh: dict[str, int] = {}
    for item in prices:
        key = store_key(market, item.get("symbol") or "")
        if key and key not in known and key not in fresh:
            fresh[key] = int(item.get("venue") or 0)
    if fresh:
        db.add_all([MarketSymbol(market=market, symbol=key, venue=venue) for key, venue in fresh.items()])
        db.commit()
        known = symbol_ids(db, market)
    return known


def _history(market: str, symbol: str, start: date, end: date) -> list[HistoryBar]:
    """Completed sessions: date, qfq close, shares, turnover, open, high, low."""
    import akshare as ak
    import pandas as pd

    if market == "cn":
        frame = ak.stock_zh_a_daily(
            symbol=symbol,
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
            adjust="qfq",
        )
    else:
        # Sina's US file has no date filter. It returns the whole listing history.
        frame = ak.stock_us_daily(symbol=symbol, adjust="qfq")
    if frame is None or len(frame) == 0:
        return []
    days = pd.to_datetime(frame["date"]).dt.date.tolist()
    closes = pd.to_numeric(frame["close"], errors="coerce").tolist()
    volumes = pd.to_numeric(frame["volume"], errors="coerce").tolist()
    amounts = pd.to_numeric(frame["amount"], errors="coerce").tolist() if "amount" in frame.columns else None
    ohlc = [
        pd.to_numeric(frame[column], errors="coerce").tolist() if column in frame.columns else [None] * len(frame)
        for column in ("open", "high", "low")
    ]
    rows: list[HistoryBar] = []
    for index, day in enumerate(days):
        close = closes[index]
        if day < start or day > end or close != close or close <= 0:
            continue
        volume = volumes[index]
        shares = 0 if volume != volume or volume < 0 else int(volume)
        amount = None
        if amounts is not None:
            value = amounts[index]
            amount = None if value != value or value < 0 else round(float(value), 2)
        opening, high, low = (_price(values[index]) for values in ohlc)
        rows.append((day, round(float(close), 4), shares, amount, opening, high, low))
    return rows


def _read(market: str, symbol: str, start: date, end: date, gap: float) -> tuple[str, list, str]:
    """ok with rows, empty when the source has nothing, fail when the request itself broke."""
    try:
        rows = _history(market, symbol, start, end)
        outcome = ("ok" if rows else "empty", rows, "")
    except OSError as exc:
        outcome = ("fail", [], f"{type(exc).__name__}: {str(exc)[:80]}")
    except Exception as exc:
        outcome = ("empty", [], f"{type(exc).__name__}: {str(exc)[:80]}")
    if gap > 0:
        time.sleep(gap)
    return outcome


def _read_job(args: tuple) -> tuple[str, list, str]:
    return _read(*args)


class _Readers:
    """Worker processes for the history reads.

    AkShare decodes Sina's files in an embedded JS engine that can take the whole
    interpreter down. A crash costs one worker here, and the name is tried again.
    """

    def __init__(self, workers: int):
        self.workers = workers
        self.pool = ProcessPoolExecutor(max_workers=workers)
        self.crashes = 0

    def _restart(self) -> None:
        self.crashes += 1
        self.pool.shutdown(wait=False, cancel_futures=True)
        self.pool = ProcessPoolExecutor(max_workers=self.workers)

    def run(self, jobs: list[tuple]) -> list[tuple[str, list, str]]:
        try:
            return list(self.pool.map(_read_job, jobs))
        except BrokenProcessPool:
            self._restart()
        results = []
        for job in jobs:
            try:
                results.append(self.pool.submit(_read_job, job).result())
            except BrokenProcessPool:
                self._restart()
                results.append(("fail", [], "worker crashed"))
        return results

    def close(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)


def write_bars(db: Session, symbol_id: int, rows: list[HistoryBar]) -> None:
    """Insert bars. Caller must have cleared conflicting rows when a full rewrite is needed."""
    if not rows:
        return
    values = []
    for row in rows:
        day, close, shares, amount = row[:4]
        opening, high, low = row[4:] if len(row) == 7 else (None, None, None)
        values.append({
            "symbol_id": symbol_id, "bar_date": day, "close": close, "volume": shares, "amount": amount,
            "open": opening, "high": high, "low": low,
        })
    db.execute(insert(MarketBar), values)


def replace_bars(db: Session, symbol_id: int, rows: list[HistoryBar]) -> None:
    """Drop every stored bar for the name, then write the new forward-adjusted history."""
    db.execute(delete(MarketBar).where(MarketBar.symbol_id == symbol_id))
    write_bars(db, symbol_id, rows)


def previous_closes(db: Session, market: str, before: date) -> dict[str, float]:
    """Each name's latest stored close strictly before `before`."""
    latest = (
        select(MarketBar.symbol_id, func.max(MarketBar.bar_date).label("bar_date"))
        .join(MarketSymbol, MarketSymbol.id == MarketBar.symbol_id)
        .where(MarketSymbol.market == market, MarketBar.bar_date < before)
        .group_by(MarketBar.symbol_id)
        .subquery()
    )
    rows = db.execute(
        select(MarketSymbol.symbol, MarketBar.close)
        .join(MarketBar, MarketBar.symbol_id == MarketSymbol.id)
        .join(
            latest,
            (latest.c.symbol_id == MarketBar.symbol_id) & (latest.c.bar_date == MarketBar.bar_date),
        )
        .where(MarketSymbol.market == market)
    ).all()
    return {symbol: float(close) for symbol, close in rows}


def refresh_symbols(db: Session, market: str, symbols: list[str], *, years: int = 6, workers: int = 2) -> int:
    """Re-read forward-adjusted history for the given names and overwrite their bars."""
    keys = [store_key(market, symbol) for symbol in symbols]
    keys = list(dict.fromkeys(key for key in keys if key))
    if not keys:
        return 0
    end = last_session(market)
    start = _years_before(end, years)
    ids = symbol_ids(db, market)
    jobs = [(market, key, start, end, 0.2) for key in keys if key in ids]
    if not jobs:
        return 0
    readers = _Readers(max(1, min(workers, len(jobs))))
    refreshed = 0
    try:
        results = readers.run(jobs)
        for (market_name, symbol, _start, _end, _gap), (state, rows, _error) in zip(jobs, results):
            if state != "ok" or not rows:
                continue
            ident = ids[symbol]
            replace_bars(db, ident, rows)
            row = db.get(MarketSymbol, ident)
            row.fetch_state = "ok"
            row.bars_from = rows[0][0]
            row.bars_through = rows[-1][0]
            row.fetched_at = datetime.utcnow()
            refreshed += 1
        db.commit()
    finally:
        readers.close()
    return refreshed


def clear_market(db: Session, market: str) -> int:
    """Drop stored bars and mark every name as still to read. Used when switching to qfq."""
    ids = list(symbol_ids(db, market).values())
    removed = 0
    for start in range(0, len(ids), 2000):
        part = ids[start : start + 2000]
        result = db.execute(delete(MarketBar).where(MarketBar.symbol_id.in_(part)))
        removed += result.rowcount or 0
    db.execute(
        update(MarketSymbol)
        .where(MarketSymbol.market == market)
        .values(fetch_state="", bars_from=None, bars_through=None, fetched_at=None)
    )
    db.commit()
    return removed


def backfill(
    market: str,
    *,
    years: int = 6,
    workers: int = 2,
    gap: float = 0.3,
    limit: int | None = None,
    stale: bool = False,
    relist: bool = True,
    replace: bool = False,
    ohlc: bool = False,
    symbols: list[str] | None = None,
) -> dict:
    """Read history for every name still pending. Safe to stop and run again."""
    if replace and symbols is not None:
        raise ValueError("--replace 不能与 --symbols 一起使用，请用 --ohlc 补齐指定股票")
    from app.core.database import SessionLocal
    from app.services import sentiment

    end = last_session(market)
    start = _years_before(end, years)
    db = SessionLocal()
    stats = {"ok": 0, "empty": 0, "fail": 0, "rows": 0, "left": 0, "stopped": ""}
    try:
        prices = sentiment._live_prices(market) if relist else []
        if not relist:
            _log(f"{market} 沿用库里已有的名单")
        elif prices:
            sync_symbols(db, market, prices)
            _log(f"{market} 名单 {len(prices)} 只")
        else:
            _log(f"{market} 这次没有取到名单，只处理库里已有的待读股票")
        if replace:
            cleared = clear_market(db, market)
            _log(f"{market} 已清空旧行情 {cleared} 行，按前复权重新读取")
        query = select(MarketSymbol.id, MarketSymbol.symbol, MarketSymbol.bars_through, MarketSymbol.bars_from).where(
            MarketSymbol.market == market
        )
        if symbols is not None:
            query = query.where(MarketSymbol.symbol.in_([store_key(market, symbol) for symbol in symbols]))
        if ohlc:
            incomplete = select(MarketBar.symbol_id).where(
                MarketBar.symbol_id == MarketSymbol.id,
                or_(MarketBar.open.is_(None), MarketBar.high.is_(None), MarketBar.low.is_(None)),
            ).exists()
            query = query.where(or_(incomplete, MarketSymbol.fetch_state.in_(["", "fail"])))
        elif stale:
            query = query.where(MarketSymbol.fetch_state == "ok", MarketSymbol.bars_through < end)
        else:
            query = query.where(MarketSymbol.fetch_state.in_(["", "fail"]))
        query = query.order_by(MarketSymbol.id)
        cooldowns = 0
        # A second round picks up names that failed or were set back during a pause.
        for round_number in (1, 2):
            todo = db.execute(query).all()
            if limit:
                todo = todo[:limit]
            total = len(todo)
            if not total:
                break
            _log(
                f"{market} 第 {round_number} 轮待读 {total} 只，区间 {start} 至 {end}，"
                f"{workers} 个读取进程，每次间隔 {gap} 秒"
            )
            started = time.monotonic()
            streak: list[int] = []
            position = 0
            chunk = max(workers * 8, 8)
            readers = _Readers(workers)
            try:
                while position < total:
                    batch = todo[position : position + chunk]
                    jobs = [
                        (
                            market,
                            symbol,
                            min(start, first or start) if ohlc else start if through is None else max(start, through + timedelta(days=1)),
                            end,
                            gap,
                        )
                        for _ident, symbol, through, first in batch
                    ]
                    results = readers.run(jobs)
                    if readers.crashes > 50:
                        stats["stopped"] = "读取进程反复崩溃，已停止。重新运行会从断点继续。"
                        _log(f"{market} {stats['stopped']}")
                        break
                    for (ident, symbol, through, _first), (state, rows, _error) in zip(batch, results):
                        row = db.get(MarketSymbol, ident)
                        row.fetched_at = datetime.utcnow()
                        if state == "ok" and ohlc and any(len(bar) != 7 or None in bar[4:] for bar in rows):
                            state = "fail"
                        if state == "ok":
                            kept = filter_writable_bars(rows)
                            dropped = len(rows) - len(kept)
                            if dropped:
                                # Serial reverse-split names (e.g. SMX) inflate early qfq prices.
                                _log(f"{market} {symbol} 滤掉 {dropped} 根超出库列的前复权K线")
                            rows = kept
                            if not rows:
                                state = "empty"
                        if state == "ok":
                            if through is None or ohlc:
                                replace_bars(db, ident, rows)
                            else:
                                write_bars(db, ident, rows)
                            row.fetch_state = "ok"
                            first, last = rows[0][0], rows[-1][0]
                            row.bars_from = first if row.bars_from is None else min(row.bars_from, first)
                            row.bars_through = last if row.bars_through is None else max(row.bars_through, last)
                            stats["rows"] += len(rows)
                            streak = []
                        elif through is None:
                            row.fetch_state = state
                            streak.append(ident)
                        stats[state] += 1
                    db.commit()
                    position += len(batch)
                    if len(streak) >= _STREAK_LIMIT:
                        probe = (market, _PROBE[market], max(start, end - timedelta(days=20)), end, 0)
                        if readers.run([probe])[0][0] == "ok":
                            _log(f"{market} 连续 {len(streak)} 只没有数据，但数据源仍正常，继续")
                        else:
                            # Nothing is answering, so those names were not really empty.
                            cooldowns += 1
                            db.execute(
                                update(MarketSymbol).where(MarketSymbol.id.in_(streak)).values(fetch_state="")
                            )
                            db.commit()
                            if cooldowns > _MAX_COOLDOWNS:
                                stats["stopped"] = "数据源连续不可用，已停止。重新运行会从断点继续。"
                                _log(f"{market} {stats['stopped']}")
                                break
                            _log(
                                f"{market} 数据源没有响应，暂停 {_COOLDOWN_SECONDS // 60} 分钟"
                                f"（第 {cooldowns} 次）"
                            )
                            time.sleep(_COOLDOWN_SECONDS)
                        streak = []
                    if (position // chunk) % 5 == 0 or position >= total:
                        elapsed = time.monotonic() - started
                        rate = position / elapsed if elapsed > 0 else 0
                        eta = (total - position) / rate / 60 if rate > 0 else 0
                        _log(
                            f"{market} {position}/{total}  有数据 {stats['ok']}  无数据 {stats['empty']}  "
                            f"失败 {stats['fail']}  已写 {stats['rows']} 行  约剩 {eta:.0f} 分钟"
                        )
            finally:
                readers.close()
            if stats["stopped"] or limit or stale:
                break
        if ohlc or symbols is not None:
            stats["left"] = db.execute(select(func.count()).select_from(query.subquery())).scalar_one()
        else:
            stats["left"] = db.execute(
                select(func.count())
                .select_from(MarketSymbol)
                .where(MarketSymbol.market == market, MarketSymbol.fetch_state.in_(["", "fail"]))
            ).scalar_one()
        return stats
    finally:
        db.close()


def coverage(db: Session, market: str) -> tuple[int, date | None]:
    """How many names have history, and the latest stored session."""
    count, latest = db.execute(
        select(func.count(), func.max(MarketSymbol.bars_through)).where(
            MarketSymbol.market == market, MarketSymbol.fetch_state == "ok"
        )
    ).one()
    return int(count or 0), latest


def close_sums(db: Session, market: str, before: date, count: int = 19, window: int = 70) -> dict[str, float]:
    """Sum of each name's last `count` closes before `before`. Names with fewer closes are left out."""
    rows = db.execute(
        select(MarketSymbol.symbol, MarketBar.bar_date, MarketBar.close)
        .join(MarketBar, MarketBar.symbol_id == MarketSymbol.id)
        .where(
            MarketSymbol.market == market,
            MarketBar.bar_date >= before - timedelta(days=window),
            MarketBar.bar_date < before,
        )
    ).all()
    closes: dict[str, list[tuple[date, float]]] = defaultdict(list)
    for symbol, day, close in rows:
        closes[symbol].append((day, float(close)))
    sums: dict[str, float] = {}
    for symbol, items in closes.items():
        if len(items) < count:
            continue
        items.sort()
        sums[symbol] = sum(price for _day, price in items[-count:])
    return sums


def append_session(db: Session, market: str, day: date, prices: list[dict]) -> int:
    """Store one finished session from the close snapshot.

    Today's last print equals the forward-adjusted close for the latest day, so it
    is written as-is. When the list's previous close disagrees with the stored bar,
    that name's full qfq history is rewritten first.
    """
    ids = sync_symbols(db, market, prices)
    stored_prev = previous_closes(db, market, day)
    reload: list[str] = []
    rows = []
    touched = []
    for item in prices:
        key = store_key(market, item.get("symbol") or "")
        ident = ids.get(key)
        price = item.get("price")
        if ident is None or not price or price <= 0:
            continue
        prev = item.get("prev_close")
        known = stored_prev.get(key)
        if prev and known is not None and not _same_close(known, float(prev)):
            reload.append(key)
        amount = item.get("amount")
        rows.append(
            {
                "symbol_id": ident,
                "bar_date": day,
                "open": _price(item.get("open")),
                "high": _price(item.get("high")),
                "low": _price(item.get("low")),
                "close": round(float(price), 4),
                "volume": int(item.get("volume") or 0),
                "amount": None if amount in (None, "") else round(float(amount), 2),
            }
        )
        touched.append(ident)
    if reload:
        unique = list(dict.fromkeys(reload))
        _log(f"{market} {len(unique)} 只昨收与库中不一致，重拉前复权历史")
        refresh_symbols(db, market, unique)
    if not rows:
        return 0
    db.execute(_upsert_bars(db), rows)
    for start in range(0, len(touched), 2000):
        db.execute(
            update(MarketSymbol)
            .where(
                MarketSymbol.id.in_(touched[start : start + 2000]),
                MarketSymbol.fetch_state == "ok",
                MarketSymbol.bars_through < day,
            )
            .values(bars_through=day)
        )
    db.commit()
    return len(rows)


def rebuild_breadth(market: str) -> int:
    """Daily advance/decline, Arms, and MA20 breadth from stored bars. Existing days are overwritten."""
    from app.core.database import SessionLocal
    from app.services.sentiment import _arms_value, _quote_books

    db = SessionLocal()
    try:
        names = db.execute(
            select(MarketSymbol.id, MarketSymbol.venue, MarketSymbol.symbol).where(
                MarketSymbol.market == market, MarketSymbol.fetch_state == "ok"
            )
        ).all()
        meta = {ident: (venue, symbol) for ident, venue, symbol in names}
        ids = sorted(meta)
        # Smaller boards still get a history once a few dozen names trade.
        min_names = {"cn_star": 40, "cn_cyb": 40, "cn_hs300": 40}
        agg: dict[date, dict[str, dict]] = {}
        for start in range(0, len(ids), 400):
            part = ids[start : start + 400]
            rows = db.execute(
                select(MarketBar.symbol_id, MarketBar.bar_date, MarketBar.close, MarketBar.volume, MarketBar.amount)
                .where(MarketBar.symbol_id.in_(part))
                .order_by(MarketBar.symbol_id, MarketBar.bar_date)
            ).all()
            current = None
            books: list[str] = []
            closes: list[float] = []
            previous = None
            for ident, day, close, volume, amount in rows:
                if ident != current:
                    current = ident
                    venue, symbol = meta[ident]
                    books = _quote_books(market, venue, symbol)
                    closes = []
                    previous = None
                price = float(close)
                closes.append(price)
                if len(closes) > 20:
                    closes.pop(0)
                if previous is not None and volume and volume > 0:
                    size = float(volume)
                    if market == "cn":
                        size = float(amount) if amount else price * float(volume)
                    ready = len(closes) >= 20
                    above = ready and price > sum(closes) / 20
                    for book in books:
                        bucket = agg.setdefault(day, {}).setdefault(
                            book,
                            {"up": 0, "down": 0, "flat": 0, "up_size": 0.0, "down_size": 0.0, "above": 0, "ma": 0},
                        )
                        if price > previous:
                            bucket["up"] += 1
                            bucket["up_size"] += size
                        elif price < previous:
                            bucket["down"] += 1
                            bucket["down_size"] += size
                        else:
                            bucket["flat"] += 1
                        if ready:
                            bucket["ma"] += 1
                            if above:
                                bucket["above"] += 1
                previous = price
            _log(f"{market} 汇总 {min(start + 400, len(ids))}/{len(ids)} 只")
        existing = {
            (row.bar_date, row.book): row
            for row in db.query(BreadthDay).filter(BreadthDay.market == market).all()
        }
        written = 0
        for day in sorted(agg):
            for book, bucket in agg[day].items():
                up, down, flat = bucket["up"], bucket["down"], bucket["flat"]
                total = up + down + flat
                need = min_names.get(book, 80)
                if total < need:
                    continue
                ma_need = min(need, 80)
                above = None if bucket["ma"] < ma_need else Decimal(str(round(bucket["above"] / bucket["ma"] * 100, 1)))
                arms = _arms_value(up, down, bucket["up_size"], bucket["down_size"])
                up_ratio = Decimal(str(round(up / total * 100, 1)))
                breadth = Decimal(str(round((up - down) / total * 100, 1)))
                arms_dec = None if arms is None else Decimal(str(arms))
                row = existing.get((day, book))
                if row is not None:
                    row.advancers = up
                    row.decliners = down
                    row.unchanged = flat
                    row.up_ratio = up_ratio
                    row.breadth = breadth
                    row.arms = arms_dec
                    row.above_ma20 = above
                else:
                    db.add(
                        BreadthDay(
                            market=market,
                            bar_date=day,
                            book=book,
                            advancers=up,
                            decliners=down,
                            unchanged=flat,
                            up_ratio=up_ratio,
                            breadth=breadth,
                            arms=arms_dec,
                            above_ma20=above,
                        )
                    )
                written += 1
        db.commit()
        return written
    finally:
        db.close()


def _status() -> None:
    from app.core.database import SessionLocal

    db = SessionLocal()
    try:
        for market in MARKETS:
            states = dict(
                db.execute(
                    select(MarketSymbol.fetch_state, func.count())
                    .where(MarketSymbol.market == market)
                    .group_by(MarketSymbol.fetch_state)
                ).all()
            )
            count, latest = coverage(db, market)
            _log(
                f"{market}: 有数据 {states.get('ok', 0)}  待读 {states.get('', 0)}  "
                f"无数据 {states.get('empty', 0)}  失败 {states.get('fail', 0)}  最新一日 {latest}"
            )
    finally:
        db.close()


def main() -> None:
    from app.core.database import Base, engine

    parser = argparse.ArgumentParser(description="Full-market daily bar store")
    parser.add_argument("action", choices=["backfill", "rebuild", "status"])
    parser.add_argument("--market", choices=MARKETS)
    parser.add_argument("--years", type=int, default=6)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--gap", type=float, default=0.3)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--stale", action="store_true", help="re-read names whose bars stop before the last session")
    parser.add_argument("--ohlc", action="store_true", help="re-read close-only history with complete OHLC")
    parser.add_argument("--symbols", nargs="+", help="read only these existing names, accepting EXCHANGE:TICKER")
    parser.add_argument(
        "--replace",
        action="store_true",
        help="drop stored bars and re-read every name with forward-adjusted prices",
    )
    parser.add_argument("--no-rebuild", action="store_true")
    parser.add_argument("--no-list", action="store_true", help="keep the stored name list instead of reading it again")
    args = parser.parse_args()

    Base.metadata.create_all(bind=engine, tables=[MarketSymbol.__table__, MarketBar.__table__])
    ensure_ohlc_schema(engine)
    if args.action == "status":
        _status()
        return
    for market in [args.market] if args.market else list(MARKETS):
        if args.action == "backfill":
            stats = backfill(
                market,
                years=args.years,
                workers=args.workers,
                gap=args.gap,
                limit=args.limit,
                stale=args.stale,
                relist=not args.no_list and not args.symbols,
                replace=args.replace,
                ohlc=args.ohlc,
                symbols=args.symbols,
            )
            _log(f"{market} 回填结束：{stats}")
            if stats["left"] or stats["stopped"] or args.limit or args.no_rebuild or args.symbols:
                continue
        written = rebuild_breadth(market)
        _log(f"{market} 宽度历史写入 {written} 行")


if __name__ == "__main__":
    main()

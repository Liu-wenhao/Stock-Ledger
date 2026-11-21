from __future__ import annotations

import json
import logging
import math
import threading
import time
import zlib
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.models.entities import IndicatorTrendCache, PositionStop, SymbolMeta
from app.services import dingtalk
from app.services.bars import bar_key, chart_session_bar, cn_session_bars, open_session_bars
from app.services.market_scope import market_of
from app.services.position_scope import position_symbols
from app.services.price_history import history_version
from app.services.signals import INDICATOR_COLUMNS_VERSION, _load_bars, build_indicator_history

logger = logging.getLogger(__name__)
settings = get_settings()

_stop = threading.Event()
_thread: threading.Thread | None = None
_calendars: dict[str, object] = {}
_CALENDAR_NAMES = {"us": "XNYS", "cn": "XSHG", "hk": "XHKG"}


def _encode(payload: dict) -> bytes:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return zlib.compress(raw, level=6)


def _decode(blob: bytes | None) -> dict | None:
    if not blob:
        return None
    try:
        return json.loads(zlib.decompress(blob).decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def cached_indicator_history(db: Session, symbol: str) -> dict | None:
    symbol = symbol.strip().upper()
    row = db.get(IndicatorTrendCache, symbol)
    payload = _decode(row.payload_blob) if row else None
    if payload is not None:
        if payload.get("_indicator_columns_version") != INDICATOR_COLUMNS_VERSION:
            return None
        if payload.get("_history_source_version") != history_version(db, symbol):
            return None
    return payload


def _monitored_symbols(db: Session) -> list[str]:
    return position_symbols(db, settings.default_portfolio_id, include_closed=False)


def _market_open(market: str, now: datetime | None = None) -> bool:
    """Exchange-calendar check avoids collecting on weekends, holidays and lunch breaks."""
    try:
        import exchange_calendars as xcals
        import pandas as pd

        calendar = _calendars.get(market)
        if calendar is None:
            calendar = xcals.get_calendar(_CALENDAR_NAMES[market])
            _calendars[market] = calendar
        stamp = pd.Timestamp(now or datetime.now(timezone.utc))
        if stamp.tzinfo is None:
            stamp = stamp.tz_localize("UTC")
        else:
            stamp = stamp.tz_convert("UTC")
        return bool(calendar.is_open_on_minute(stamp.floor("min"), ignore_breaks=False))
    except Exception as exc:
        logger.warning("indicator monitor calendar failed for %s: %s", market, type(exc).__name__)
        return False


def _valid_bar(bar: dict | None) -> tuple[bool, str]:
    if not bar:
        return False, "当前交易分钟没有取得行情"
    try:
        opened = float(bar["o"])
        high = float(bar["h"])
        low = float(bar["l"])
        close = float(bar["c"])
        volume = float(bar.get("v") or 0)
    except (KeyError, TypeError, ValueError):
        return False, "行情字段缺失或不是数字"
    if not all(math.isfinite(value) for value in (opened, high, low, close, volume)):
        return False, "行情包含非有限数字"
    if min(opened, high, low, close) <= 0 or volume < 0:
        return False, "行情包含非正价格或负成交量"
    if low > min(opened, close) or high < max(opened, close) or low > high:
        return False, "行情的开高低收关系异常"
    return True, ""


def _ready_state(
    payload: dict,
    stop: PositionStop | None,
    previous: IndicatorTrendCache | None,
) -> tuple[dict[str, str], list[str]]:
    points = payload.get("points") or []
    if not points:
        return {}, []
    point = points[-1]
    current: dict[str, str] = {}
    lines: list[str] = []
    for rule in point.get("strategies") or []:
        if rule.get("ready"):
            key = f"buy:{rule.get('id') or rule.get('name')}"
            current[key] = f"买入/加仓信号：{rule.get('name')}"
    for rule in point.get("sells") or []:
        if rule.get("ready"):
            key = f"sell:{rule.get('id') or rule.get('name')}"
            current[key] = f"卖出信号：{rule.get('name')}"
    stop_candle = str(point.get("stop_candle") or "").strip()
    if stop_candle:
        current[f"candle:stop:{stop_candle}"] = f"止损K线：{stop_candle}"
    volume_signal = {
        "squeeze": "缩量推升",
        "distribute": "放量滞涨",
        "shrink_down": "缩量下跌",
    }.get(str(point.get("signal") or ""))
    if volume_signal:
        current[f"volume:{point['signal']}"] = f"量能信号：{volume_signal}"
    if point.get("vol_squeeze"):
        current["volatility:squeeze"] = "波动率信号：布林带宽创120日新低，进入变盘蓄势"
    price = float(point.get("close") or 0)
    if stop and stop.stop_price is not None and price <= float(stop.stop_price):
        current["risk:stop"] = f"触及自设止损 {float(stop.stop_price):.2f}"
    if stop and stop.take_profit_price is not None and price >= float(stop.take_profit_price):
        current["risk:take-profit"] = f"触及自设止盈 {float(stop.take_profit_price):.2f}"
    if (
        previous
        and previous.last_price is not None
        and previous.snapshot_at is not None
        and previous.session_date is not None
        and str(previous.session_date) == str(point.get("date") or "")
    ):
        old = float(previous.last_price)
        minutes = (datetime.utcnow() - previous.snapshot_at).total_seconds() / 60
        if old > 0 and minutes <= 5 and abs(price / old - 1) >= 0.05:
            move = (price / old - 1) * 100
            current["anomaly:minute-jump"] = f"分钟价格跳变 {move:+.1f}%（{old:.2f} → {price:.2f}）"
    lines.extend(current.values())
    return current, lines


def _state(raw: str | None) -> dict[str, str]:
    try:
        value = json.loads(raw or "{}")
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        return {}


def _transition_labels(current: dict[str, str], notified: dict[str, str]) -> list[str]:
    labels = [current[key] for key in current if key not in notified]
    for key, old_label in notified.items():
        if key in current or not key.startswith(("buy:", "sell:")):
            continue
        name = old_label.split("：", 1)[-1]
        side = "买入/加仓规则" if key.startswith("buy:") else "卖出规则"
        labels.append(f"{side}由成立变为消失：{name}")
    return labels


def _signal_message(symbol: str, name: str | None, payload: dict, labels: list[str]) -> tuple[str, str]:
    point = (payload.get("points") or [{}])[-1]
    title = f"{name or symbol} 指标信号"
    content = [
        f"**{name or symbol}** `{symbol}`",
        f"- 时间：{datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}",
        f"- 最新价：{float(point.get('close') or 0):.2f}",
    ]
    if point.get("rsi") is not None:
        content.append(f"- RSI(14)：{point['rsi']}")
    if point.get("ma20") is not None:
        content.append(f"- MA20：{point['ma20']}（偏离 {point.get('vs_ma20')}%）")
    content.extend(f"- **{label}**" for label in labels)
    content.append("\n仅作规则提醒，不自动下单。")
    return title, "\n".join(content)


def _record_error(symbol: str, message: str) -> None:
    db = SessionLocal()
    try:
        row = db.get(IndicatorTrendCache, symbol)
        if row is None:
            row = IndicatorTrendCache(symbol=symbol)
            db.add(row)
        if row.last_error != message:
            row.error_count = 1
            row.error_notified = 0
        else:
            row.error_count = int(row.error_count or 0) + 1
        row.last_error = message[:512]
        db.commit()
        if row.error_count >= 3 and not row.error_notified and dingtalk.configured():
            sent = dingtalk.send_markdown(
                f"{symbol} 指标采集异常",
                f"**{symbol}** 连续 {row.error_count} 次采集失败：{message}\n\n系统会继续每分钟重试。",
            )
            if sent:
                row.error_notified = 1
                db.commit()
    finally:
        db.close()


def _refresh_symbol(symbol: str, session_bar: dict | None) -> None:
    valid, problem = _valid_bar(session_bar)
    if not valid:
        _record_error(symbol, problem)
        return
    try:
        payload = build_indicator_history(
            symbol,
            limit=40,
            session_bar=session_bar,
            fetch_session_bar=False,
        )
        points = payload.get("points") or []
        if not points:
            _record_error(symbol, "指标计算没有返回数据点")
            return
        point = points[-1]
        price = float(point.get("close") or 0)
        db = SessionLocal()
        try:
            row = db.get(IndicatorTrendCache, symbol)
            was_error = bool(row and row.error_notified)
            old_notified = _state(row.notified_state_json if row else None)
            stop = (
                db.query(PositionStop)
                .filter(
                    PositionStop.portfolio_id == settings.default_portfolio_id,
                    PositionStop.symbol == symbol,
                )
                .one_or_none()
            )
            current, _all_labels = _ready_state(payload, stop, row)
            transition_labels = _transition_labels(current, old_notified)
            if row is None:
                row = IndicatorTrendCache(symbol=symbol)
                db.add(row)
            row.payload_blob = _encode(payload)
            row.session_date = datetime.strptime(point["date"], "%Y-%m-%d").date()
            row.snapshot_at = datetime.utcnow()
            row.last_price = Decimal(str(price))
            row.signal_state_json = json.dumps(current, ensure_ascii=False, separators=(",", ":"))
            row.error_count = 0
            row.last_error = None
            row.error_notified = 0
            db.commit()

            meta = db.query(SymbolMeta).filter(SymbolMeta.symbol == symbol).one_or_none()
            if was_error and dingtalk.configured():
                dingtalk.send_markdown(
                    f"{meta.name if meta and meta.name else symbol} 指标采集恢复",
                    f"`{symbol}` 已恢复每分钟指标采集，最新价 {price:.2f}。",
                )
            if transition_labels and dingtalk.configured():
                title, content = _signal_message(
                    symbol,
                    meta.name if meta else None,
                    payload,
                    transition_labels,
                )
                if dingtalk.send_markdown(title, content):
                    row.notified_state_json = json.dumps(current, ensure_ascii=False, separators=(",", ":"))
                    db.commit()
            else:
                # Track signals disappearing so the next genuine transition can alert again.
                row.notified_state_json = json.dumps(
                    {key: value for key, value in old_notified.items() if key in current},
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                if not dingtalk.configured():
                    row.notified_state_json = row.signal_state_json
                db.commit()
        finally:
            db.close()
    except Exception as exc:
        logger.exception("indicator monitor failed for %s", symbol)
        _record_error(symbol, f"{type(exc).__name__}: {str(exc)[:420]}")


def prime_indicator_caches() -> int:
    """Rebuild missing or obsolete latest pages without requesting a live quote."""
    db = SessionLocal()
    try:
        held = _monitored_symbols(db)
        missing = []
        for symbol in held:
            if cached_indicator_history(db, symbol) is None:
                missing.append(symbol)
    finally:
        db.close()
    built = 0
    for symbol in missing:
        if _stop.is_set():
            break
        try:
            payload = build_indicator_history(symbol, limit=40, fetch_session_bar=False)
            points = payload.get("points") or []
            if not points:
                continue
            point = points[-1]
            db = SessionLocal()
            try:
                row = db.get(IndicatorTrendCache, symbol) or IndicatorTrendCache(symbol=symbol)
                stop = (
                    db.query(PositionStop)
                    .filter(
                        PositionStop.portfolio_id == settings.default_portfolio_id,
                        PositionStop.symbol == symbol,
                    )
                    .one_or_none()
                )
                current, _labels = _ready_state(payload, stop, row)
                row.payload_blob = _encode(payload)
                row.session_date = datetime.strptime(point["date"], "%Y-%m-%d").date()
                row.snapshot_at = datetime.utcnow()
                row.last_price = Decimal(str(float(point.get("close") or 0)))
                row.signal_state_json = json.dumps(current, ensure_ascii=False, separators=(",", ":"))
                # Priming historical state must not emit stale signals on the next minute.
                row.notified_state_json = row.signal_state_json
                row.error_count = 0
                row.last_error = None
                row.error_notified = 0
                db.add(row)
                db.commit()
                built += 1
            finally:
                db.close()
        except Exception:
            logger.exception("indicator cache prime failed for %s", symbol)
    return built


def run_indicator_cycle(now: datetime | None = None) -> int:
    db = SessionLocal()
    try:
        held = _monitored_symbols(db)
    finally:
        db.close()
    active = [symbol for symbol in held if _market_open(market_of(symbol), now)]
    if not active:
        return 0

    live: dict[str, dict] = {}
    us_symbols = [symbol for symbol in active if market_of(symbol) == "us"]
    cn_symbols = [symbol for symbol in active if market_of(symbol) == "cn"]
    if us_symbols:
        live.update(open_session_bars(us_symbols))
    if cn_symbols:
        live.update(cn_session_bars(cn_symbols))
    for symbol in active:
        key = bar_key(symbol)
        if key in live:
            continue
        try:
            live[key] = chart_session_bar(symbol, _load_bars(symbol, full_market=True))
        except Exception:
            live[key] = None
    for symbol in active:
        if _stop.is_set():
            break
        _refresh_symbol(symbol, live.get(bar_key(symbol)))
    return len(active)


def _loop() -> None:
    interval = max(30, int(settings.indicator_monitor_seconds or 60))
    primed = prime_indicator_caches()
    if primed:
        logger.info("indicator monitor primed %s held symbols", primed)
    while not _stop.is_set():
        started = time.monotonic()
        try:
            count = run_indicator_cycle()
            if count:
                logger.info("indicator monitor refreshed %s held symbols", count)
        except Exception:
            logger.exception("indicator monitor cycle failed")
        elapsed = time.monotonic() - started
        _stop.wait(max(1, interval - elapsed))


def start_indicator_monitor() -> None:
    global _thread
    if not settings.indicator_monitor_enabled:
        logger.warning("indicator monitor is disabled")
        return
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, daemon=True, name="indicator-monitor")
    _thread.start()
    logger.warning("indicator monitor started (%ss)", max(30, int(settings.indicator_monitor_seconds or 60)))


def stop_indicator_monitor() -> None:
    _stop.set()

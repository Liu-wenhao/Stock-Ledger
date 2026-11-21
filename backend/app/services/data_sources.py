"""Quote and daily-bar routing.

US live quotes use one Sina request, then Tencent. YFinance fills only the names
those two missed, because a Yahoo download can sit for the full timeout.
US history still uses YFinance. A-shares and Hong Kong use AkShare.
A-share bars fall through to Tushare when a token is set, otherwise Baostock.
Longbridge is created only when credentials exist, and only to fill a US or
Hong Kong quote the faster sources left incomplete.
A-share requests never call Longbridge.
"""

from __future__ import annotations

import base64
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from app.core.config import get_settings
from app.services.market_scope import market_of
from app.services.symbols import split_symbol

logger = logging.getLogger(__name__)

_settings_lock = threading.Lock()
_ak_gate: threading.BoundedSemaphore | None = None
_eastmoney_down_until = 0.0
_yahoo_down_until = 0.0
_YAHOO_TIMEOUT_SECONDS = 12
_YAHOO_COOLDOWN_SECONDS = 60
_longbridge_down_until = 0.0
_longbridge_ctx: Any = None
_longbridge_skip_logged = False
_patch_applied = False


def _settings():
    return get_settings()


def _semaphore() -> threading.BoundedSemaphore:
    global _ak_gate
    workers = max(1, int(_settings().max_workers or 1))
    with _settings_lock:
        if _ak_gate is None:
            _ak_gate = threading.BoundedSemaphore(workers)
        return _ak_gate


def _apply_eastmoney_patch(force: bool = False) -> None:
    global _patch_applied
    if _patch_applied or not (force or _settings().enable_eastmoney_patch):
        return
    import requests

    original = requests.Session.request

    def request(self, method, url, **kwargs):
        if "eastmoney.com" in str(url):
            self.trust_env = False
            headers = dict(kwargs.get("headers") or {})
            headers.setdefault(
                "User-Agent",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/138.0 Safari/537.36",
            )
            kwargs["headers"] = headers
            kwargs.setdefault("timeout", 12)
        return original(self, method, url, **kwargs)

    requests.Session.request = request
    _patch_applied = True


def _eastmoney_error(exc: Exception) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return "remotedisconnected" in text or "eastmoney" in text or "connection aborted" in text


def _run_eastmoney(action: Callable[[], Any]) -> Any:
    """One AkShare call. After a disconnect, later calls skip until the cooldown ends."""
    global _eastmoney_down_until
    if time.time() < _eastmoney_down_until:
        raise RuntimeError("eastmoney cooldown")
    _apply_eastmoney_patch()
    attempts = 3 if _settings().enable_eastmoney_patch else 1
    patched_retry = False
    last: Exception | None = None
    attempt = 0
    while attempt < attempts:
        try:
            with _semaphore():
                return action()
        except Exception as exc:
            last = exc
            if not _eastmoney_error(exc):
                raise
            if not _patch_applied:
                _apply_eastmoney_patch(force=True)
                patched_retry = True
                logger.warning("东财走了系统代理并断开，已改成直连后重试一次。")
                continue
            attempt += 1
            if attempt < attempts:
                time.sleep(0.4 * attempt)
                continue
            _eastmoney_down_until = time.time() + 15
            if not patched_retry:
                logger.warning(
                    "东财接口失败（%s）。可在 .env 设置 ENABLE_EASTMONEY_PATCH=true、"
                    "MAX_WORKERS=1；A 股日线不足时会改走 Baostock。",
                    exc,
                )
            raise
    if last:
        raise last
    raise RuntimeError("eastmoney call failed")
    if last:
        raise last
    raise RuntimeError("eastmoney call failed")


def _yahoo_symbol(symbol: str) -> str | None:
    if market_of(symbol) != "us":
        return None
    _exchange, ticker = split_symbol(symbol)
    return ticker or None


def _hk_ticker(symbol: str) -> str | None:
    exchange, ticker = split_symbol(symbol)
    if exchange != "HKEX" or not ticker:
        return None
    return ticker.zfill(5) if ticker.isdigit() else ticker


def _cn_code(symbol: str) -> tuple[str, str] | None:
    exchange, ticker = split_symbol(symbol)
    if exchange == "SSE" and ticker:
        return ticker, "SH"
    if exchange == "SZSE" and ticker:
        return ticker, "SZ"
    return None


def _bars_from_frame(frame, volume_scale: float = 1.0) -> list[dict]:
    if frame is None or getattr(frame, "empty", True):
        return []
    columns = {str(column): column for column in frame.columns}

    def col(*names: str):
        for name in names:
            if name in columns:
                return columns[name]
        return None

    date_col = col("日期", "date", "trade_date", "Date")
    open_col = col("开盘", "open", "Open")
    close_col = col("收盘", "close", "Close")
    high_col = col("最高", "high", "High")
    low_col = col("最低", "low", "Low")
    volume_col = col("成交量", "volume", "vol", "Volume")
    amount_col = col("成交额", "amount", "Amount")
    if close_col is None:
        return []
    bars: list[dict] = []
    for _, row in frame.iterrows():
        raw_day = row.name if date_col is None else row[date_col]
        day = str(raw_day)[:10].replace("/", "-")
        if len(day) == 8 and day.isdigit():
            day = f"{day[:4]}-{day[4:6]}-{day[6:8]}"
        try:
            close = float(row[close_col])
        except (TypeError, ValueError):
            continue
        if not day or close <= 0:
            continue

        def num(column, default: float) -> float:
            if column is None:
                return default
            try:
                value = float(row[column])
            except (TypeError, ValueError):
                return default
            return value

        volume = num(volume_col, 0.0) * volume_scale
        amount = num(amount_col, 0.0)
        dollar = amount if amount > 0 else close * volume
        bars.append(
            {
                "d": day,
                "o": num(open_col, close),
                "h": num(high_col, close),
                "l": num(low_col, close),
                "c": close,
                "v": volume,
                "dol": dollar,
            }
        )
    bars.sort(key=lambda bar: bar["d"])
    return bars


def _mark_yahoo_down() -> None:
    global _yahoo_down_until
    _yahoo_down_until = time.time() + _YAHOO_COOLDOWN_SECONDS


def _run_yahoo(fn: Callable, label: str):
    """Yahoo first. A timeout or a failed call skips it for a minute."""
    if time.time() < _yahoo_down_until:
        return None
    holder: dict[str, Any] = {}

    def work() -> None:
        try:
            holder["value"] = fn()
        except Exception as exc:
            holder["error"] = exc

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    thread.join(_YAHOO_TIMEOUT_SECONDS)
    if thread.is_alive():
        _mark_yahoo_down()
        logger.warning(
            "Yahoo 超过 %s 秒没有返回（%s），改走下一级数据源。",
            _YAHOO_TIMEOUT_SECONDS,
            label,
        )
        return None
    if "error" in holder:
        _mark_yahoo_down()
        logger.warning("Yahoo 失败（%s）: %s", label, holder["error"])
        return None
    return holder.get("value")


def _yf_bars(symbol: str) -> list[dict]:
    yahoo = _yahoo_symbol(symbol)
    if not yahoo:
        return []
    import yfinance as yf

    frame = _run_yahoo(
        lambda: yf.Ticker(yahoo).history(period="2y", auto_adjust=False, actions=False),
        symbol,
    )
    if frame is None:
        return []
    return _bars_from_frame(frame)


def _ak_bars(symbol: str) -> list[dict]:
    import akshare as ak

    kind = market_of(symbol)
    start = (datetime.now() - timedelta(days=800)).strftime("%Y%m%d")
    end = datetime.now().strftime("%Y%m%d")
    if kind == "hk":
        ticker = _hk_ticker(symbol)
        if not ticker:
            return []
        frame = _run_eastmoney(
            lambda: ak.stock_hk_hist(symbol=ticker, period="daily", start_date=start, end_date=end, adjust="qfq")
        )
        return _bars_from_frame(frame)
    coded = _cn_code(symbol)
    if not coded:
        return []
    ticker, _suffix = coded
    frame = _run_eastmoney(
        lambda: ak.stock_zh_a_hist(symbol=ticker, period="daily", start_date=start, end_date=end, adjust="qfq")
    )
    bars = _bars_from_frame(frame)
    return _scale_cn_lots(bars)


def _scale_cn_lots(bars: list[dict]) -> list[dict]:
    """AkShare A-share volume is sometimes lots of 100. Amount reveals which."""
    ratios = []
    for bar in bars[-20:]:
        if bar["v"] > 0 and bar["dol"] > 0 and bar["c"] > 0:
            ratios.append(bar["dol"] / bar["c"] / bar["v"])
    if ratios and (sum(ratios) / len(ratios)) > 50:
        for bar in bars:
            bar["v"] *= 100
            if bar["dol"] <= 0:
                bar["dol"] = bar["c"] * bar["v"]
    return bars


def _baostock_bars(symbol: str) -> list[dict]:
    holder: dict[str, Any] = {}

    def work() -> None:
        try:
            holder["bars"] = _baostock_bars_inner(symbol)
        except Exception as exc:
            holder["error"] = exc

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    thread.join(12)
    if thread.is_alive():
        logger.warning("Baostock 超过 12 秒没有返回 %s，改走下一级数据源。", symbol)
        return []
    if "error" in holder:
        raise holder["error"]
    return holder.get("bars") or []


def _baostock_bars_inner(symbol: str) -> list[dict]:
    coded = _cn_code(symbol)
    if not coded:
        return []
    import baostock as bs

    ticker, suffix = coded
    prefix = "sh" if suffix == "SH" else "sz"
    login = bs.login()
    if getattr(login, "error_code", "1") != "0":
        return []
    try:
        start = (datetime.now() - timedelta(days=800)).strftime("%Y-%m-%d")
        end = datetime.now().strftime("%Y-%m-%d")
        rs = bs.query_history_k_data_plus(
            f"{prefix}.{ticker}",
            "date,open,high,low,close,volume,amount",
            start_date=start,
            end_date=end,
            frequency="d",
            adjustflag="2",
        )
        rows = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())
    finally:
        bs.logout()
    if not rows:
        return []
    import pandas as pd

    frame = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume", "amount"])
    return _bars_from_frame(frame)


def _legacy_bars(symbol: str) -> list[dict]:
    kind = market_of(symbol)
    if kind == "cn":
        from app.services.market import fetch_tencent_bars

        return fetch_tencent_bars(symbol, lot_size=100)
    if kind == "hk":
        from app.services.market import fetch_tencent_bars

        return fetch_tencent_bars(symbol, lot_size=1)
    from app.services.bars import parse_sina_bars
    from app.services.market import _fetch_sina_history

    key = (_yahoo_symbol(symbol) or "").lower()
    if not key:
        return []
    return parse_sina_bars(_fetch_sina_history(key, 400))


def fetch_daily_bars(symbol: str) -> list[dict]:
    """Primary source for one symbol, then the documented fallback, then Sina/Tencent."""
    kind = market_of(symbol)
    if kind == "us":
        try:
            bars = _yf_bars(symbol)
            if len(bars) >= 30:
                return bars
        except Exception as exc:
            logger.warning("YFinance 日线失败 %s: %s", symbol, exc)
            bars = []
        if len(bars) < 30:
            extra = _longbridge_bars(symbol)
            if len(extra) > len(bars):
                return extra
        if bars:
            return bars
        return _legacy_bars(symbol)

    try:
        bars = _ak_bars(symbol)
        if len(bars) >= 30:
            return bars
    except Exception as exc:
        logger.warning("AkShare 日线失败 %s: %s", symbol, exc)
        bars = []
    if kind == "cn" and len(bars) < 30:
        try:
            replacement = _baostock_bars(symbol)
            if len(replacement) > len(bars):
                bars = replacement
        except Exception as exc:
            logger.warning("Baostock 日线失败 %s: %s", symbol, exc)
    if kind == "hk" and len(bars) < 30:
        extra = _longbridge_bars(symbol)
        if len(extra) > len(bars):
            bars = extra
    if bars:
        return bars
    try:
        return _legacy_bars(symbol)
    except Exception as exc:
        logger.warning("日线兜底失败 %s: %s", symbol, exc)
        return []


def _quote_from_ohlc(
    *,
    close: float,
    prev: float | None,
    opened: float | None,
    high: float | None,
    low: float | None,
    volume: float | None = None,
    name: str | None = None,
) -> dict[str, Any]:
    from app.services.market import _quote_payload

    payload = _quote_payload(
        price=close,
        prev_close=prev,
        open_price=opened,
        high=high,
        low=low,
        timestamp=None,
    )
    if payload and name:
        payload["name"] = name
    if payload and volume:
        payload["volume"] = volume
    return payload


def _yf_quotes(symbols: list[str]) -> dict[str, dict[str, Any]]:
    import yfinance as yf

    mapping = {symbol: _yahoo_symbol(symbol) for symbol in symbols}
    mapping = {symbol: yahoo for symbol, yahoo in mapping.items() if yahoo}
    out = {symbol: {} for symbol in symbols}
    if not mapping:
        return out
    tickers = list(dict.fromkeys(mapping.values()))
    frame = _run_yahoo(
        lambda: yf.download(
            tickers,
            period="5d",
            interval="1d",
            auto_adjust=False,
            progress=False,
            threads=False,
        ),
        ",".join(tickers),
    )
    if frame is None or getattr(frame, "empty", True):
        return out
    for symbol, yahoo in mapping.items():
        try:
            if getattr(frame.columns, "nlevels", 1) > 1:
                level0 = set(frame.columns.get_level_values(0))
                if "Close" in level0:
                    series = frame.xs(yahoo, axis=1, level=1)
                else:
                    series = frame[yahoo]
            else:
                series = frame
            closes = series["Close"].dropna()
            if closes.empty:
                continue
            close = float(closes.iloc[-1])
            prev = float(closes.iloc[-2]) if len(closes) > 1 else None
            last = series.dropna(subset=["Close"]).iloc[-1]

            def cell(field: str) -> float | None:
                value = last.get(field)
                try:
                    number = float(value)
                except (TypeError, ValueError):
                    return None
                if number != number:
                    return None
                return number

            out[symbol] = _quote_from_ohlc(
                close=close,
                prev=prev,
                opened=cell("Open"),
                high=cell("High"),
                low=cell("Low"),
                volume=cell("Volume"),
            )
        except Exception as exc:
            logger.warning("YFinance 行情解析失败 %s: %s", symbol, exc)
    return out


def _ak_quote(symbol: str) -> dict[str, Any]:
    import akshare as ak

    kind = market_of(symbol)
    start = (datetime.now() - timedelta(days=40)).strftime("%Y%m%d")
    end = datetime.now().strftime("%Y%m%d")
    if kind == "hk":
        ticker = _hk_ticker(symbol)
        if not ticker:
            return {}
        frame = _run_eastmoney(
            lambda: ak.stock_hk_hist(symbol=ticker, period="daily", start_date=start, end_date=end, adjust="qfq")
        )
    else:
        coded = _cn_code(symbol)
        if not coded:
            return {}
        ticker, _suffix = coded
        frame = _run_eastmoney(
            lambda: ak.stock_zh_a_hist(symbol=ticker, period="daily", start_date=start, end_date=end, adjust="qfq")
        )
    bars = _bars_from_frame(frame)
    if kind == "cn":
        bars = _scale_cn_lots(bars)
    if not bars:
        return {}
    last = bars[-1]
    prev = bars[-2]["c"] if len(bars) > 1 else None
    return _quote_from_ohlc(close=last["c"], prev=prev, opened=last["o"], high=last["h"], low=last["l"])


def _longbridge_client_id() -> str:
    settings = _settings()
    if (settings.longbridge_oauth_client_id or "").strip():
        return settings.longbridge_oauth_client_id.strip()
    if not (settings.longbridge_access_token or "").strip():
        return (settings.longbridge_app_key or "").strip()
    return ""


def _longbridge_configured() -> bool:
    settings = _settings()
    if _longbridge_client_id() and (
        (settings.longbridge_oauth_token_cache_b64 or "").strip()
        or _token_cache_path(_longbridge_client_id()).exists()
    ):
        return True
    return bool(
        (settings.longbridge_app_key or "").strip()
        and (settings.longbridge_app_secret or "").strip()
        and (settings.longbridge_access_token or "").strip()
    )


def _token_cache_path(client_id: str) -> Path:
    return Path.home() / ".longbridge" / "openapi" / "tokens" / client_id


def _write_token_cache(client_id: str) -> None:
    raw = (_settings().longbridge_oauth_token_cache_b64 or "").strip()
    if not raw or not client_id:
        return
    path = _token_cache_path(client_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(base64.b64decode(raw))


def _connection_closed(exc: Exception) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return "connection" in text or "closed" in text or "reset" in text or "timeout" in text


def _longbridge_context():
    """Build the quote client once. Missing SDK or credentials returns None."""
    global _longbridge_ctx, _longbridge_skip_logged, _longbridge_down_until
    if time.time() < _longbridge_down_until:
        return None
    if _longbridge_ctx is not None:
        return _longbridge_ctx
    if not _longbridge_configured():
        return None
    settings = _settings()
    client_id = _longbridge_client_id()
    try:
        from longbridge.openapi import Config, QuoteContext
    except ImportError:
        if not _longbridge_skip_logged:
            logger.warning(
                "当前环境没有可用的 Longbridge SDK（需要 OAuthBuilder / Config.from_oauth 或旧版 API Key）。"
                "已跳过 Longbridge，不影响 YFinance / AkShare。"
            )
            _longbridge_skip_logged = True
        return None
    try:
        config = None
        if client_id and not (settings.longbridge_access_token or "").strip():
            try:
                from longbridge.openapi import OAuthBuilder
            except ImportError:
                if not _longbridge_skip_logged:
                    logger.warning(
                        "Longbridge SDK 没有 OAuthBuilder / Config.from_oauth，跳过 OAuth，继续用 YFinance / AkShare。"
                    )
                    _longbridge_skip_logged = True
                return None
            _write_token_cache(client_id)
            oauth = OAuthBuilder(client_id).build()
            if not hasattr(Config, "from_oauth"):
                if not _longbridge_skip_logged:
                    logger.warning("Longbridge SDK 没有 Config.from_oauth，跳过 Longbridge。")
                    _longbridge_skip_logged = True
                return None
            config = Config.from_oauth(oauth)
        else:
            kwargs = {}
            if settings.longbridge_http_url:
                kwargs["http_url"] = settings.longbridge_http_url
            if settings.longbridge_quote_ws_url:
                kwargs["quote_ws_url"] = settings.longbridge_quote_ws_url
            if settings.longbridge_trade_ws_url:
                kwargs["trade_ws_url"] = settings.longbridge_trade_ws_url
            config = Config(
                settings.longbridge_app_key,
                settings.longbridge_app_secret,
                settings.longbridge_access_token,
                **kwargs,
            )
        _longbridge_ctx = QuoteContext(config)
        return _longbridge_ctx
    except Exception as exc:
        _longbridge_ctx = None
        if _connection_closed(exc):
            _longbridge_down_until = time.time() + max(1, int(settings.longbridge_connection_cooldown_seconds or 15))
            logger.warning("Longbridge 连接关闭，冷却 %s 秒内跳过。", settings.longbridge_connection_cooldown_seconds)
        else:
            logger.warning("Longbridge 初始化失败，已跳过: %s", exc)
        return None


def _lb_symbol(symbol: str) -> str | None:
    kind = market_of(symbol)
    if kind == "cn":
        return None
    if kind == "hk":
        ticker = _hk_ticker(symbol)
        return f"{ticker}.HK" if ticker else None
    ticker = _yahoo_symbol(symbol)
    return f"{ticker}.US" if ticker else None


def _lb_number(quote, *names: str) -> float | None:
    for name in names:
        value = quote.get(name) if isinstance(quote, dict) else getattr(quote, name, None)
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number > 0:
            return number
    return None


def _longbridge_quotes(symbols: list[str]) -> dict[str, dict[str, Any]]:
    global _longbridge_down_until, _longbridge_ctx
    ctx = _longbridge_context()
    out = {symbol: {} for symbol in symbols}
    if ctx is None:
        return out
    mapping = {symbol: _lb_symbol(symbol) for symbol in symbols}
    mapping = {symbol: code for symbol, code in mapping.items() if code}
    if not mapping:
        return out
    try:
        rows = ctx.quote(list(mapping.values()))
    except Exception as exc:
        _longbridge_ctx = None
        if _connection_closed(exc):
            _longbridge_down_until = time.time() + max(1, int(_settings().longbridge_connection_cooldown_seconds or 15))
            logger.warning("Longbridge 连接关闭，冷却期内跳过。")
        else:
            logger.warning("Longbridge 行情失败: %s", exc)
        return out
    by_code = {}
    for row in rows or []:
        code = str(getattr(row, "symbol", "") or (row.get("symbol") if isinstance(row, dict) else ""))
        by_code[code.upper()] = row
    for symbol, code in mapping.items():
        row = by_code.get(code.upper())
        if row is None:
            continue
        close = _lb_number(row, "last_done", "last_price", "price")
        if close is None:
            continue
        out[symbol] = _quote_from_ohlc(
            close=close,
            prev=_lb_number(row, "prev_close", "prev_close_price"),
            opened=_lb_number(row, "open"),
            high=_lb_number(row, "high"),
            low=_lb_number(row, "low"),
            name=str(getattr(row, "name", "") or ""),
        )
    return out


def _longbridge_bars(symbol: str) -> list[dict]:
    if market_of(symbol) == "cn" or _lb_symbol(symbol) is None:
        return []
    ctx = _longbridge_context()
    if ctx is None or not hasattr(ctx, "history_candlesticks_by_offset"):
        return []
    try:
        from longbridge.openapi import AdjustType, Period

        candles = ctx.history_candlesticks_by_offset(
            _lb_symbol(symbol),
            Period.Day,
            AdjustType.NoAdjust,
            False,
            400,
        )
    except Exception as exc:
        logger.warning("Longbridge 日线失败 %s: %s", symbol, exc)
        return []
    bars = []
    for candle in candles or []:
        day = getattr(candle, "timestamp", None)
        if hasattr(day, "astimezone"):
            day = day.astimezone(timezone.utc).date().isoformat()
        else:
            day = str(day)[:10]
        close = _lb_number(candle, "close")
        if not day or close is None:
            continue
        volume = _lb_number(candle, "volume") or 0
        bars.append(
            {
                "d": day,
                "o": _lb_number(candle, "open") or close,
                "h": _lb_number(candle, "high") or close,
                "l": _lb_number(candle, "low") or close,
                "c": close,
                "v": volume,
                "dol": close * volume,
            }
        )
    bars.sort(key=lambda bar: bar["d"])
    return bars


def _incomplete(quote: dict | None) -> bool:
    return not quote or quote.get("price") is None


def fetch_quotes(symbols: list[str]) -> dict[str, dict[str, Any]]:
    """US live quotes from Sina then Tencent. YFinance only fills the misses."""
    out: dict[str, dict[str, Any]] = {symbol: {} for symbol in symbols}
    us = [symbol for symbol in symbols if market_of(symbol) == "us"]
    local = [symbol for symbol in symbols if market_of(symbol) in {"cn", "hk"}]
    if us:
        try:
            from app.services.market import _fetch_us_quotes

            for symbol, quote in _fetch_us_quotes(us).items():
                if not _incomplete(quote):
                    out[symbol] = quote
        except Exception as exc:
            logger.warning("新浪/腾讯美股行情失败: %s", exc)
        missing = [symbol for symbol in us if _incomplete(out.get(symbol))]
        if missing:
            try:
                for symbol, quote in _yf_quotes(missing).items():
                    if not _incomplete(quote):
                        out[symbol] = quote
            except Exception as exc:
                logger.warning("YFinance 行情失败: %s", exc)
    for symbol in local:
        try:
            out[symbol] = _ak_quote(symbol)
        except Exception as exc:
            logger.warning("AkShare 行情失败 %s: %s", symbol, exc)
            out[symbol] = {}
    gaps = [
        symbol
        for symbol in symbols
        if market_of(symbol) in {"us", "hk"} and _incomplete(out.get(symbol))
    ]
    if gaps and _longbridge_configured():
        filled = _longbridge_quotes(gaps)
        for symbol, quote in filled.items():
            if not _incomplete(quote):
                out[symbol] = quote
    return out


def supplement_pe(symbols: list[str]) -> dict[str, tuple[float, float, float]]:
    """Longbridge PE, EPS, and price for US/HK names whose primary PE was empty.

    A-share symbols are ignored. Callers keep the existing price/EPS check.
    """
    wanted = [symbol for symbol in symbols if market_of(symbol) in {"us", "hk"}]
    if not wanted or not _longbridge_configured():
        return {}
    ctx = _longbridge_context()
    if ctx is None or not hasattr(ctx, "quote"):
        return {}
    mapping = {symbol: _lb_symbol(symbol) for symbol in wanted}
    codes = [code for code in mapping.values() if code]
    if not codes:
        return {}
    global _longbridge_down_until, _longbridge_ctx
    try:
        rows = ctx.quote(codes)
    except Exception as exc:
        _longbridge_ctx = None
        if _connection_closed(exc):
            _longbridge_down_until = time.time() + max(1, int(_settings().longbridge_connection_cooldown_seconds or 15))
        return {}
    by_code = {}
    for row in rows or []:
        code = str(getattr(row, "symbol", "") or "")
        by_code[code.upper()] = row
    found: dict[str, tuple[float, float, float]] = {}
    for symbol, code in mapping.items():
        row = by_code.get((code or "").upper())
        if row is None:
            continue
        pe = _lb_number(row, "pe_ttm_ratio", "pe_ratio", "pe")
        eps = _lb_number(row, "eps_ttm", "eps")
        price = _lb_number(row, "last_done", "last_price", "price")
        ticker = (split_symbol(symbol)[1] or "").upper()
        if pe and eps and price and ticker:
            found[ticker] = (pe, eps, price)
    return found

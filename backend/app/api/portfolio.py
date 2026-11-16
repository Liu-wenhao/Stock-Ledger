from datetime import date
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_db
from app.models.entities import PositionStop
from app.schemas.portfolio import (
    ClosedHoldingRow,
    ClosedHoldingsResponse,
    CumulativeCycleRow,
    CumulativeHoldingRow,
    CumulativeHoldingsResponse,
    DailyHoldingsPnlResponse,
    DailyMoversResponse,
    DailyPnlCalendarResponse,
    HoldingRow,
    HoldingsResponse,
    MonthlyReturnsResponse,
    NavSeriesResponse,
    PeriodPerformanceResponse,
    PortfolioSummary,
    PositionLimitUpdate,
    HoldingsAdvice,
    IndicatorHistory,
    PositionSleeveOut,
    PositionSleeveUpdate,
    PositionStopOut,
    PositionStopUpdate,
    ProfitSeriesResponse,
    ReviewResponse,
    StrategyEngineResponse,
    StrategyResponse,
    BacktestCandlesResponse,
    BacktestResponse,
)
from app.services import ledger, review
from app.services.ak_backtest import backtest_candles, engine_source, run_book_backtest
from app.services.market_scope import current_market
from app.services.signals import build_holdings_advice, build_indicator_history

router = APIRouter(prefix="/api", tags=["portfolio"])
settings = get_settings()

ZERO = Decimal("0")
SORTABLE_HOLDING_FIELDS = {"total_pnl", "total_pnl_percent"}


def _filter_rows(items: list[dict], q: str | None) -> list[dict]:
    """Keep the rows whose 商品代码/名称 match the `q` search box."""
    ql = (q or "").strip().upper()
    if not ql:
        return items
    return [
        i for i in items if ql in i["symbol"].upper() or (i.get("name") and ql in i["name"].upper())
    ]


def _sum(items: list[dict], key: str) -> Decimal:
    return sum((i[key] for i in items), ZERO)


def _dec_value(value) -> Decimal:
    if value is None or value == "":
        return ZERO
    try:
        return Decimal(str(value))
    except Exception:
        return ZERO


def _order_price_board(items: list[dict]) -> list[dict]:
    """Open names by position size, then flat names by realized loss."""
    held = []
    closed = []
    rest = []
    for item in items:
        if item.get("closed"):
            last = item.get("last_price")
            prev = item.get("prev_close")
            if last is not None and prev is not None:
                last_d = _dec_value(last)
                prev_d = _dec_value(prev)
                if prev_d != 0 and last_d > 0:
                    change = last_d - prev_d
                    item["day_change"] = change
                    item["day_change_percent"] = (change / prev_d * Decimal("100")).quantize(Decimal("0.01"))
            closed.append(item)
        elif _dec_value(item.get("quantity")) > 0:
            held.append(item)
        else:
            rest.append(item)
    held.sort(key=lambda item: str(item.get("symbol") or ""))
    held.sort(key=lambda item: _dec_value(item.get("market_value")), reverse=True)
    closed.sort(key=lambda item: str(item.get("symbol") or ""))
    closed.sort(key=lambda item: _dec_value(item.get("realized_pnl")))
    return [*held, *closed, *rest]


def _attach_position_stops(db: Session, items: list[dict], portfolio_id: int) -> list[dict]:
    if not items:
        return items
    rows = db.query(PositionStop).filter(PositionStop.portfolio_id == portfolio_id).all()
    by_symbol = {row.symbol: row for row in rows}
    for item in items:
        stop = by_symbol.get(str(item.get("symbol") or "").upper())
        item["stop_price"] = stop.stop_price if stop else None
        item["stop_qty"] = stop.stop_qty if stop else None
        item["take_profit_price"] = stop.take_profit_price if stop else None
        item["target_qty"] = stop.target_qty if stop else None
        item["max_buy_amount"] = stop.max_buy_amount if stop else None
    return items


def _merge_watch_rows(db: Session, items: list[dict]) -> list[dict]:
    """Add the A-share or Hong Kong quote list. These rows are not positions."""
    from app.services.market import ensure_symbol_meta, get_quotes
    from app.services.market_scope import current_market
    from app.services.symbols import exchange_of
    from app.services.watchlist import watch_symbols

    market = current_market()
    symbols = watch_symbols(market)
    if not symbols:
        return items
    present = {str(item.get("symbol") or "").upper() for item in items}
    missing = [symbol for symbol in symbols if symbol.upper() not in present]
    if not missing:
        return items
    quotes = get_quotes(db, missing, background_refresh=False)
    currency = "CNY" if market == "cn" else "HKD"
    extra = []
    for symbol in missing:
        meta = ensure_symbol_meta(db, symbol)
        quote = quotes.get(symbol) or {}
        name = quote.get("name") or meta.name
        extra.append(
            {
                "symbol": symbol,
                "name": name,
                "logo_url": meta.logo_url,
                "exchange": exchange_of(symbol),
                "currency": currency,
                "quantity": ZERO,
                "avg_price": ZERO,
                "last_price": quote.get("price"),
                "cost_basis": ZERO,
                "lot_avg_price": ZERO,
                "lot_cost_basis": ZERO,
                "market_value": ZERO,
                "invested": ZERO,
                "allocation": ZERO,
                "total_pnl": ZERO,
                "total_pnl_percent": ZERO,
                "day_change": quote.get("change"),
                "day_change_percent": quote.get("change_percent"),
                "open_price": quote.get("open"),
                "high_price": quote.get("high"),
                "low_price": quote.get("low"),
                "prev_close": quote.get("prev_close"),
                "price_as_of": quote.get("price_as_of"),
                "closed": False,
                "trade_count": 0,
            }
        )
    return [*items, *extra]


def _sort_holding_rows(
    items: list[dict],
    sort_by: Literal["total_pnl", "total_pnl_percent"] | None,
    sort_order: Literal["asc", "desc"],
) -> list[dict]:
    """Sort numeric holding columns while keeping rows without a rate at the end."""
    if sort_by not in SORTABLE_HOLDING_FIELDS:
        return items

    ranked = [item for item in items if item.get(sort_by) is not None]
    missing = [item for item in items if item.get(sort_by) is None]
    ranked.sort(key=lambda item: item["symbol"])
    ranked.sort(key=lambda item: item[sort_by], reverse=sort_order == "desc")
    return [*ranked, *missing]


@router.get("/portfolio/summary", response_model=PortfolioSummary)
def summary(db: Session = Depends(get_db)):
    return ledger.compute_summary(db)


@router.get("/portfolio/nav", response_model=NavSeriesResponse)
def nav(
    range: str = Query("3M", alias="range"),
    mode: str = Query("value"),
    rebuild: bool = False,
    db: Session = Depends(get_db),
):
    if rebuild:
        ledger.build_nav_series(db, rebuild=True)
    return ledger.nav_response(db, range_key=range, mode=mode)


@router.get("/portfolio/daily-movers", response_model=DailyMoversResponse)
def movers(db: Session = Depends(get_db)):
    return ledger.daily_movers(db)


@router.get("/portfolio/daily-pnl", response_model=DailyPnlCalendarResponse)
def daily_pnl(
    granularity: str = Query("day"),
    year: int | None = None,
    month: int | None = None,
    db: Session = Depends(get_db),
):
    return ledger.daily_pnl_calendar(db, granularity=granularity, year=year, month=month)


@router.get("/portfolio/daily-holdings", response_model=DailyHoldingsPnlResponse)
def daily_holdings(
    date: date = Query(...),
    db: Session = Depends(get_db),
):
    """Per-stock P/L for a selected US trading session."""
    return ledger.daily_holdings(db, session_day=date)


@router.get("/portfolio/period-holdings", response_model=DailyHoldingsPnlResponse)
def period_holdings(
    start_date: date = Query(...),
    end_date: date = Query(...),
    db: Session = Depends(get_db),
):
    """Per-stock P/L for a month, quarter, or year."""
    return ledger.period_holdings(db, start_date, end_date)


@router.get("/portfolio/total-movers", response_model=DailyMoversResponse)
def total_movers(
    include_closed: bool = False,
    closed_only: bool = False,
    db: Session = Depends(get_db),
):
    return ledger.total_movers(db, include_closed=include_closed, closed_only=closed_only)


@router.get("/portfolio/period-performance", response_model=PeriodPerformanceResponse)
def period_performance(
    range: str = Query("3M", alias="range"),
    db: Session = Depends(get_db),
):
    return ledger.period_performance(db, range_key=range)


@router.get("/holdings")
def holdings(
    view: str = Query("positions"),
    include_closed: bool = False,
    watch: bool = Query(False),
    q: str | None = None,
    sort_by: Literal["total_pnl", "total_pnl_percent"] | None = None,
    sort_order: Literal["asc", "desc"] = "desc",
    db: Session = Depends(get_db),
):
    # Do not use a Union response_model here: FastAPI/Pydantic may coerce
    # open holdings into ClosedHoldingsResponse and drop total_pnl / market_value.
    if view == "closed":
        data = ledger.compute_closed_holdings(db)
        items = _filter_rows(data["items"], q)
        # 汇总行必须汇总它下面的行：筛选后重新合计。否则 周期数（前端按行加）
        # 会跟着筛选，而金额/盈亏/盈亏% 还是全量，汇总行自相矛盾。
        return ClosedHoldingsResponse(
            items=[ClosedHoldingRow(**i) for i in items],
            total_trade_count=sum(i["trade_count"] for i in items),
            total_realized_pnl=_sum(items, "realized_pnl"),
            total_realized_cost=_sum(items, "realized_cost"),
            total_buy_amount=_sum(items, "buy_amount"),
            total_sell_amount=_sum(items, "sell_amount"),
            total_market_value=data["total_market_value"],
            cash=data["cash"],
        )
    if view == "cumulative":
        data = ledger.compute_cumulative_holdings(db)
        items = _sort_holding_rows(_filter_rows(data["items"], q), sort_by, sort_order)
        total_cost = _sum(items, "cost")
        total_pnl = _sum(items, "total_pnl")
        return CumulativeHoldingsResponse(
            items=[
                CumulativeHoldingRow(
                    **{
                        **{k: v for k, v in item.items() if k != "cycles"},
                        "cycles": [CumulativeCycleRow(**cycle) for cycle in item.get("cycles", [])],
                    }
                )
                for item in items
            ],
            total_trade_count=sum(i["trade_count"] for i in items),
            total_cost=total_cost,
            total_pnl=total_pnl,
            total_pnl_percent=(
                (total_pnl / total_cost * Decimal("100")).quantize(Decimal("0.01"))
                if total_cost > ZERO
                else ZERO
            ),
            total_market_value=data["total_market_value"],
            cash=data["cash"],
        )

    data = ledger.compute_holdings(db, include_closed=include_closed or watch)
    items = _merge_watch_rows(db, data["items"]) if watch else data["items"]
    items = _filter_rows(items, q)
    items = _order_price_board(items) if watch and not sort_by else _sort_holding_rows(items, sort_by, sort_order)
    items = review.enrich_holding_stop_levels(items)
    items = _attach_position_stops(db, items, settings.default_portfolio_id)
    return HoldingsResponse(
        items=[HoldingRow(**{k: v for k, v in i.items() if k in HoldingRow.model_fields}) for i in items],
        total_market_value=data["total_market_value"],
        cash=data["cash"],
    )


def _position_stop_row(db: Session, symbol: str) -> PositionStop | None:
    return (
        db.query(PositionStop)
        .filter(
            PositionStop.portfolio_id == settings.default_portfolio_id,
            PositionStop.symbol == symbol,
        )
        .first()
    )


def _position_stop_out(row: PositionStop | None, symbol: str) -> PositionStopOut:
    if row is None:
        return PositionStopOut(symbol=symbol)
    return PositionStopOut(
        symbol=row.symbol,
        stop_price=row.stop_price,
        stop_qty=row.stop_qty,
        take_profit_price=row.take_profit_price,
        target_qty=row.target_qty,
        max_buy_amount=row.max_buy_amount,
    )


def _position_stop_empty(row: PositionStop) -> bool:
    return (
        row.stop_price is None
        and row.stop_qty is None
        and row.take_profit_price is None
        and row.target_qty is None
        and row.max_buy_amount is None
        and row.sleeve is None
    )


@router.put("/holdings/sleeve", response_model=PositionSleeveOut)
def update_holding_sleeve(payload: PositionSleeveUpdate, db: Session = Depends(get_db)):
    """Mark a held name as core or satellite. Core holds at most 3 names, satellite at most 5."""
    symbol = (payload.symbol or "").strip().upper()
    if not symbol:
        raise HTTPException(status_code=400, detail="缺少商品代码")
    chosen = review.sleeve_choices(db, settings.default_portfolio_id)
    held = {str(item["symbol"]).upper() for item in ledger.compute_holdings(db)["items"]}
    others = sum(
        1 for name in held if name != symbol and review.sleeve_of(name, chosen) == payload.sleeve
    )
    limit = review.SLEEVE_MAX_NAMES[payload.sleeve]
    if others >= limit:
        raise HTTPException(
            status_code=400,
            detail=f"{review.SLEEVE_LABEL[payload.sleeve]}最多 {limit} 只，先把其中一只改成另一类",
        )
    row = _position_stop_row(db, symbol)
    if row is None:
        row = PositionStop(portfolio_id=settings.default_portfolio_id, symbol=symbol)
        db.add(row)
    row.sleeve = payload.sleeve
    db.commit()
    return PositionSleeveOut(symbol=symbol, sleeve=payload.sleeve)


@router.put("/holdings/stop", response_model=PositionStopOut)
def update_holding_stop(payload: PositionStopUpdate, db: Session = Depends(get_db)):
    symbol = (payload.symbol or "").strip().upper()
    if not symbol:
        raise HTTPException(status_code=400, detail="缺少商品代码")
    fields = payload.model_fields_set - {"symbol"}
    if "stop_price" in fields and payload.stop_price is not None and payload.stop_price <= ZERO:
        raise HTTPException(status_code=400, detail="止损价格必须大于 0")
    if "stop_qty" in fields and payload.stop_qty is not None and payload.stop_qty <= ZERO:
        raise HTTPException(status_code=400, detail="止损数量必须大于 0")
    if (
        "take_profit_price" in fields
        and payload.take_profit_price is not None
        and payload.take_profit_price <= ZERO
    ):
        raise HTTPException(status_code=400, detail="止盈价格必须大于 0")

    row = _position_stop_row(db, symbol)
    if row is None:
        if not any(getattr(payload, name) is not None for name in fields):
            return _position_stop_out(None, symbol)
        row = PositionStop(portfolio_id=settings.default_portfolio_id, symbol=symbol)
        db.add(row)
    for name in ("stop_price", "stop_qty", "take_profit_price"):
        if name in fields:
            setattr(row, name, getattr(payload, name))
    if _position_stop_empty(row):
        db.delete(row)
        db.commit()
        return _position_stop_out(None, symbol)
    db.commit()
    db.refresh(row)
    return _position_stop_out(row, symbol)


@router.put("/holdings/limit", response_model=PositionStopOut)
def update_holding_limit(payload: PositionLimitUpdate, db: Session = Depends(get_db)):
    symbol = (payload.symbol or "").strip().upper()
    if not symbol:
        raise HTTPException(status_code=400, detail="缺少商品代码")
    if payload.target_qty is not None and payload.target_qty <= ZERO:
        raise HTTPException(status_code=400, detail="仓位必须大于 0")
    if payload.max_buy_amount is not None and payload.max_buy_amount <= ZERO:
        raise HTTPException(status_code=400, detail="最大买入金额必须大于 0")

    row = _position_stop_row(db, symbol)
    if payload.target_qty is None and payload.max_buy_amount is None:
        if row is not None:
            row.target_qty = None
            row.max_buy_amount = None
            if _position_stop_empty(row):
                db.delete(row)
                db.commit()
                return _position_stop_out(None, symbol)
            db.commit()
            db.refresh(row)
        return _position_stop_out(row, symbol)

    if row is None:
        row = PositionStop(
            portfolio_id=settings.default_portfolio_id,
            symbol=symbol,
        )
        db.add(row)
    row.target_qty = payload.target_qty
    row.max_buy_amount = payload.max_buy_amount
    db.commit()
    db.refresh(row)
    return _position_stop_out(row, symbol)


@router.get("/holdings/advice", response_model=HoldingsAdvice)
def holdings_advice(db: Session = Depends(get_db)):
    """Position notes from daily bars: VWAP, momentum, bandwidth, vol targeting."""
    return build_holdings_advice(db, settings.default_portfolio_id)


@router.get("/holdings/indicator-history", response_model=IndicatorHistory)
def holdings_indicator_history(
    symbol: str = Query(...),
    before: str | None = Query(None),
    limit: int = Query(40, ge=20, le=80),
):
    """One page of daily indicators, walking back from the newest stored bar toward April."""
    return build_indicator_history(symbol, before=before, limit=limit)


@router.get("/analysis/monthly-returns", response_model=MonthlyReturnsResponse)
def monthly_returns(range: str = Query("3M"), db: Session = Depends(get_db)):
    return ledger.monthly_returns(db, range_key=range)


@router.get("/analysis/profit-series", response_model=ProfitSeriesResponse)
def profit_series(range: str = Query("3M"), db: Session = Depends(get_db)):
    data = ledger.nav_response(db, range_key=range, mode="profit")
    return {"points": data["points"], "ranges": data["ranges"]}


@router.get("/analysis/review", response_model=ReviewResponse)
def trading_review(
    start: date | None = Query(None),
    end: date | None = Query(None),
    db: Session = Depends(get_db),
):
    """Trading-behaviour review for a date range. Defaults to the last month."""
    return review.build_review(db, start=start, end=end)


@router.get("/analysis/strategy", response_model=StrategyResponse)
def trading_strategy(
    start: date | None = Query(None),
    end: date | None = Query(None),
    db: Session = Depends(get_db),
):
    """Position actions and post-exit re-entry candidates for the strategy page."""
    return review.build_strategy(db, start=start, end=end)


@router.get("/analysis/strategy/engine", response_model=StrategyEngineResponse)
def trading_strategy_engine():
    """Core engine source for the strategy page's copy action. Read straight from the modules."""
    return {"engine_code": engine_source()}


@router.get("/analysis/backtest", response_model=BacktestResponse)
def analysis_backtest(
    start: date | None = Query(None),
    end: date | None = Query(None),
    db: Session = Depends(get_db),
):
    """AKQuant replay of the price-page rules. Separate from ledger P&L."""
    if start and end and start > end:
        raise HTTPException(status_code=400, detail="开始日期不能晚于结束日期")
    return run_book_backtest(db, current_market(), start, end)


@router.get("/analysis/backtest/candles", response_model=BacktestCandlesResponse)
def analysis_backtest_candles(
    symbol: str = Query(..., min_length=1),
    start: date | None = Query(None),
    end: date | None = Query(None),
    db: Session = Depends(get_db),
):
    """Daily bars around one backtest cycle for the B/S candlestick chart."""
    if start and end and start > end:
        raise HTTPException(status_code=400, detail="开始日期不能晚于结束日期")
    try:
        return backtest_candles(db, symbol, start, end)
    except Exception as exc:
        raise HTTPException(status_code=502, detail="日线没有取到") from exc

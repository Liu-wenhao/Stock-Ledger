from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.schemas.portfolio import (
    ClosedHoldingRow,
    ClosedHoldingsResponse,
    CumulativeCycleRow,
    CumulativeHoldingRow,
    CumulativeHoldingsResponse,
    DailyMoversResponse,
    DailyPnlCalendarResponse,
    HoldingRow,
    HoldingsResponse,
    MonthlyReturnsResponse,
    NavSeriesResponse,
    PeriodPerformanceResponse,
    PortfolioSummary,
    ProfitSeriesResponse,
    ReviewResponse,
    StrategyResponse,
)
from app.services import ledger, review

router = APIRouter(prefix="/api", tags=["portfolio"])

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

    data = ledger.compute_holdings(db, include_closed=include_closed)
    items = _sort_holding_rows(_filter_rows(data["items"], q), sort_by, sort_order)
    items = review.enrich_holding_stop_levels(items)
    return HoldingsResponse(
        items=[HoldingRow(**{k: v for k, v in i.items() if k in HoldingRow.model_fields}) for i in items],
        total_market_value=data["total_market_value"],
        cash=data["cash"],
    )


@router.get("/analysis/monthly-returns", response_model=MonthlyReturnsResponse)
def monthly_returns(range: str = Query("3M"), db: Session = Depends(get_db)):
    return ledger.monthly_returns(db, range_key=range)


@router.get("/analysis/profit-series", response_model=ProfitSeriesResponse)
def profit_series(range: str = Query("3M"), db: Session = Depends(get_db)):
    data = ledger.nav_response(db, range_key=range, mode="profit")
    return {"points": data["points"], "ranges": data["ranges"]}


@router.get("/analysis/review", response_model=ReviewResponse)
def trading_review(db: Session = Depends(get_db)):
    """Trading-behaviour review: win rate, payoff, chases, hold times."""
    return review.build_review(db)


@router.get("/analysis/strategy", response_model=StrategyResponse)
def trading_strategy(db: Session = Depends(get_db)):
    """Position actions and post-exit re-entry candidates for the strategy page."""
    return review.build_strategy(db)

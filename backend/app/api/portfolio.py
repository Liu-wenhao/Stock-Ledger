from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.schemas.portfolio import (
    ClosedHoldingRow,
    ClosedHoldingsResponse,
    DailyMoversResponse,
    HoldingRow,
    HoldingsResponse,
    MonthlyReturnsResponse,
    NavSeriesResponse,
    PortfolioSummary,
    ProfitSeriesResponse,
)
from app.services import ledger

router = APIRouter(prefix="/api", tags=["portfolio"])


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


@router.get("/portfolio/total-movers", response_model=DailyMoversResponse)
def total_movers(
    include_closed: bool = False,
    closed_only: bool = False,
    db: Session = Depends(get_db),
):
    return ledger.total_movers(db, include_closed=include_closed, closed_only=closed_only)


@router.get("/holdings")
def holdings(
    view: str = Query("positions"),
    include_closed: bool = False,
    q: str | None = None,
    db: Session = Depends(get_db),
):
    # Do not use a Union response_model here: FastAPI/Pydantic may coerce
    # open holdings into ClosedHoldingsResponse and drop total_pnl / market_value.
    if view == "closed":
        data = ledger.compute_closed_holdings(db)
        items = data["items"]
        if q:
            ql = q.strip().upper()
            items = [
                i
                for i in items
                if ql in i["symbol"].upper() or (i.get("name") and ql in i["name"].upper())
            ]
        return ClosedHoldingsResponse(
            items=[ClosedHoldingRow(**i) for i in items],
            total_trade_count=data["total_trade_count"],
            total_realized_pnl=data["total_realized_pnl"],
            total_realized_cost=data["total_realized_cost"],
            total_buy_amount=data["total_buy_amount"],
            total_sell_amount=data["total_sell_amount"],
            total_market_value=data["total_market_value"],
            cash=data["cash"],
        )

    data = ledger.compute_holdings(db, include_closed=include_closed)
    items = data["items"]
    if q:
        ql = q.strip().upper()
        items = [
            i
            for i in items
            if ql in i["symbol"].upper() or (i.get("name") and ql in i["name"].upper())
        ]
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

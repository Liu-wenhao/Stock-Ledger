from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field


class SummaryCard(BaseModel):
    label: str
    value: Decimal
    currency: str = "USD"
    percent: Decimal | None = None
    secondary_label: str | None = None
    secondary_value: Decimal | None = None
    secondary_percent: Decimal | None = None
    secondary_currency: str | None = None


class PortfolioSummary(BaseModel):
    portfolio_value: Decimal
    holdings_value: Decimal = Decimal("0")
    cash: Decimal
    unrealized_pnl: Decimal
    unrealized_pnl_percent: Decimal
    day_change: Decimal
    day_change_percent: Decimal
    realized_pnl: Decimal
    dividends: Decimal
    commissions: Decimal = Decimal("0")
    total_pnl: Decimal
    total_pnl_percent: Decimal
    annualized_return: Decimal
    currency: str = "USD"
    as_of: datetime


class HoldingRow(BaseModel):
    symbol: str
    name: str | None = None
    logo_url: str | None = None
    exchange: str | None = None
    quantity: Decimal
    avg_price: Decimal
    last_price: Decimal | None = None
    cost_basis: Decimal
    market_value: Decimal
    invested: Decimal = Decimal("0")
    allocation: Decimal
    total_pnl: Decimal
    total_pnl_percent: Decimal
    day_change: Decimal | None = None
    day_change_percent: Decimal | None = None
    open_price: Decimal | None = None
    high_price: Decimal | None = None
    low_price: Decimal | None = None
    prev_close: Decimal | None = None
    closed: bool = False
    dividends: Decimal = Decimal("0")
    realized_pnl: Decimal = Decimal("0")


class HoldingsResponse(BaseModel):
    items: list[HoldingRow]
    total_market_value: Decimal
    cash: Decimal


class NavPoint(BaseModel):
    date: date
    total_value: Decimal
    cash: Decimal = Decimal("0")
    realized_pnl: Decimal
    total_pnl: Decimal
    return_percent: Decimal | None = None
    cash_percent: Decimal | None = None


class RangeStat(BaseModel):
    key: str
    label: str
    return_percent: Decimal | None = None
    pnl: Decimal | None = None


class NavSeriesResponse(BaseModel):
    mode: str
    points: list[NavPoint]
    ranges: list[RangeStat]


class MoverItem(BaseModel):
    symbol: str
    name: str | None = None
    logo_url: str | None = None
    change: Decimal = Decimal("0")
    change_percent: Decimal
    closed: bool = False


class DailyMoversResponse(BaseModel):
    winners: list[MoverItem]
    losers: list[MoverItem]


class MonthlyReturnPoint(BaseModel):
    month: str
    portfolio_percent: Decimal
    benchmark_percent: Decimal


class MonthlyReturnsResponse(BaseModel):
    items: list[MonthlyReturnPoint]
    benchmark: str = "QQQ"


class ProfitSeriesResponse(BaseModel):
    points: list[NavPoint]
    ranges: list[RangeStat]


class PerformanceItem(BaseModel):
    symbol: str
    name: str | None = None
    logo_url: str | None = None
    value: Decimal
    percent: Decimal
    closed: bool = False


class QuoteOut(BaseModel):
    symbol: str
    price: Decimal | None = None
    change_percent: Decimal | None = None
    name: str | None = None


class SymbolSearchItem(BaseModel):
    symbol: str
    name: str | None = None
    logo_url: str | None = None

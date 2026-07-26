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
    realized_pnl_percent: Decimal = Decimal("0")
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
    # US trading-day date for last_price / 收盘价; omit/null when unknown
    price_as_of: date | None = None
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
    trade_count: int = 0
    cycle_started_at: datetime | None = None
    closed_cycles_pnl: Decimal = Decimal("0")


class ClosedCycleRow(BaseModel):
    cycle_index: int
    started_at: datetime | None = None
    ended_at: datetime | None = None
    trade_count: int = 0
    realized_cost: Decimal = Decimal("0")
    realized_pnl: Decimal = Decimal("0")
    realized_pnl_percent: Decimal = Decimal("0")
    buy_amount: Decimal = Decimal("0")
    sell_amount: Decimal = Decimal("0")


class ClosedHoldingRow(BaseModel):
    symbol: str
    name: str | None = None
    logo_url: str | None = None
    exchange: str | None = None
    cycle_count: int = 0
    trade_count: int = 0
    realized_cost: Decimal = Decimal("0")
    realized_pnl: Decimal = Decimal("0")
    realized_pnl_percent: Decimal = Decimal("0")
    buy_amount: Decimal = Decimal("0")
    sell_amount: Decimal = Decimal("0")
    currently_open: bool = False
    # Named cycles (not children) so ant-design-vue won't treat rows as tree data
    cycles: list[ClosedCycleRow] = Field(default_factory=list)


class HoldingsResponse(BaseModel):
    items: list[HoldingRow]
    total_market_value: Decimal
    cash: Decimal


class ClosedHoldingsResponse(BaseModel):
    items: list[ClosedHoldingRow]
    total_trade_count: int = 0
    total_realized_pnl: Decimal = Decimal("0")
    total_realized_cost: Decimal = Decimal("0")
    total_buy_amount: Decimal = Decimal("0")
    total_sell_amount: Decimal = Decimal("0")
    total_market_value: Decimal = Decimal("0")
    cash: Decimal = Decimal("0")


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

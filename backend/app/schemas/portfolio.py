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
    # Break-even basis: includes this cycle's realized P/L, so that
    # (last_price - avg_price) * quantity == total_pnl
    avg_price: Decimal
    last_price: Decimal | None = None
    # Strategy stop levels for the open lot: trailing high-water -8% and cost -12%.
    suggested_stop_price: Decimal | None = None
    mandatory_stop_price: Decimal | None = None
    # US market quote/bar time for last_price / 价格; omit/null when unknown
    price_as_of: datetime | None = None
    cost_basis: Decimal
    # Raw moving-average cost of the open lot (money actually still in it),
    # before realized P/L is folded in. Used for 投资总额.
    lot_avg_price: Decimal = Decimal("0")
    lot_cost_basis: Decimal = Decimal("0")
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
    # Weighted average buy price over the cycle: buy_amount / buy_quantity
    avg_price: Decimal = Decimal("0")


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
    # Weighted average buy price across all closed cycles of this symbol
    avg_price: Decimal = Decimal("0")
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


class CumulativeCycleRow(BaseModel):
    cycle_index: int
    status: str
    started_at: datetime | None = None
    ended_at: datetime | None = None
    trade_count: int = 0
    avg_price: Decimal = Decimal("0")
    price: Decimal | None = None
    cost: Decimal = Decimal("0")
    pnl: Decimal = Decimal("0")
    pnl_percent: Decimal = Decimal("0")


class CumulativeHoldingRow(BaseModel):
    symbol: str
    name: str | None = None
    logo_url: str | None = None
    exchange: str | None = None
    cycle_count: int = 0
    trade_count: int = 0
    avg_price: Decimal = Decimal("0")
    last_price: Decimal | None = None
    price_as_of: datetime | None = None
    cost: Decimal = Decimal("0")
    total_pnl: Decimal = Decimal("0")
    # None when the diluted cost basis is <= 0 (nothing left at risk)
    total_pnl_percent: Decimal | None = None
    cycles: list[CumulativeCycleRow] = Field(default_factory=list)


class CumulativeHoldingsResponse(BaseModel):
    items: list[CumulativeHoldingRow]
    total_trade_count: int = 0
    total_cost: Decimal = Decimal("0")
    total_pnl: Decimal = Decimal("0")
    total_pnl_percent: Decimal = Decimal("0")
    total_market_value: Decimal = Decimal("0")
    cash: Decimal = Decimal("0")


class NavPoint(BaseModel):
    date: date
    total_value: Decimal
    cash: Decimal = Decimal("0")
    holdings_value: Decimal = Decimal("0")
    realized_pnl: Decimal
    total_pnl: Decimal
    return_percent: Decimal | None = None
    holdings_percent: Decimal | None = None
    cash_percent: Decimal | None = None


class RangeStat(BaseModel):
    key: str
    label: str
    return_percent: Decimal | None = None
    holdings_return_percent: Decimal | None = None
    cash_return_percent: Decimal | None = None
    pnl: Decimal | None = None
    holdings_pnl: Decimal | None = None
    cash_pnl: Decimal | None = None


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


class PeriodPerformanceSection(BaseModel):
    pnl: Decimal = Decimal("0")
    pnl_percent: Decimal = Decimal("0")
    winners: list[MoverItem] = Field(default_factory=list)
    losers: list[MoverItem] = Field(default_factory=list)


class PeriodPerformanceResponse(BaseModel):
    range: str
    holding: PeriodPerformanceSection
    closed: PeriodPerformanceSection
    cumulative: PeriodPerformanceSection


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


class DailyPnlItem(BaseModel):
    date: date
    pnl: Decimal
    pnl_percent: Decimal = Decimal("0")


class DailyPnlSummary(BaseModel):
    total_pnl: Decimal = Decimal("0")
    profit_days: int = 0
    loss_days: int = 0
    flat_days: int = 0
    best_day_pnl: Decimal | None = None
    worst_day_pnl: Decimal | None = None


class DailyPnlCalendarResponse(BaseModel):
    granularity: str = "day"
    year: int
    month: int | None = None
    years: list[int]
    months: list[int] = Field(default_factory=list)
    items: list[DailyPnlItem]
    summary: DailyPnlSummary


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


class ReviewMeta(BaseModel):
    trades: int = 0
    symbols: int = 0
    first_trade_at: datetime | None = None
    last_trade_at: datetime | None = None


class ReviewCapital(BaseModel):
    deposits: Decimal = Decimal("0")
    buy_notional: Decimal = Decimal("0")
    sell_notional: Decimal = Decimal("0")
    turnover_x: Decimal = Decimal("0")
    commissions: Decimal = Decimal("0")
    realized: Decimal = Decimal("0")
    realized_pct: Decimal = Decimal("0")


class ReviewBehavior(BaseModel):
    closed_cycles: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: Decimal = Decimal("0")
    avg_win: Decimal = Decimal("0")
    avg_loss: Decimal = Decimal("0")
    payoff: Decimal = Decimal("0")
    profit_factor: Decimal = Decimal("0")
    avg_win_days: Decimal = Decimal("0")
    avg_loss_days: Decimal = Decimal("0")
    addon_below_avg: int = 0
    same_day_flips: int = 0
    t_trades: int = 0
    t_wins: int = 0
    t_losses: int = 0
    t_win_rate: Decimal = Decimal("0")
    t_pnl: Decimal = Decimal("0")


class ReviewSymbolRow(BaseModel):
    symbol: str
    realized: Decimal = Decimal("0")
    cost: Decimal = Decimal("0")
    return_pct: Decimal = Decimal("0")
    cycles: int = 0
    trades: int = 0
    avg_holding_days: Decimal = Decimal("0")
    addon_below_avg: int = 0


class ReviewMonthRow(BaseModel):
    month: str
    trades: int = 0
    realized: Decimal = Decimal("0")
    wins: int = 0
    losses: int = 0


class ReviewCycleRow(BaseModel):
    symbol: str
    started_at: datetime
    ended_at: datetime
    holding_days: int = 0
    realized: Decimal = Decimal("0")
    cost: Decimal = Decimal("0")
    return_pct: Decimal = Decimal("0")
    trades: int = 0
    addon_below_avg: int = 0


class ReviewOpenPosition(BaseModel):
    symbol: str
    qty: Decimal = Decimal("0")
    avg_price: Decimal = Decimal("0")
    cost: Decimal = Decimal("0")
    cycle_started_at: datetime | None = None


class ReviewResponse(BaseModel):
    meta: ReviewMeta
    capital: ReviewCapital
    behavior: ReviewBehavior
    symbols: list[ReviewSymbolRow] = Field(default_factory=list)
    monthly: list[ReviewMonthRow] = Field(default_factory=list)
    cycles: list[ReviewCycleRow] = Field(default_factory=list)
    open_positions: list[ReviewOpenPosition] = Field(default_factory=list)


class StrategyOpenPosition(ReviewOpenPosition):
    current_price: Decimal | None = None
    market_value: Decimal | None = None
    pnl: Decimal | None = None
    pnl_pct: Decimal | None = None
    holding_days: int = 0
    time_stop_due: bool = False
    hard_stop_price: Decimal | None = None
    trailing_stop_price: Decimal | None = None
    high_water_price: Decimal | None = None
    stop_price: Decimal | None = None
    stop_source: str = "硬止损"
    action: str = "hold"
    action_tone: str = "hold"
    action_label: str = ""
    action_note: str = ""


class ReentryCandidate(BaseModel):
    symbol: str
    exited_at: datetime
    sessions_since_exit: int = 0
    sessions_estimated: bool = False
    cooldown_sessions: int = 5
    cooldown_met: bool = False
    cooldown_remaining: int = 5
    current_price: Decimal | None = None
    ma10: Decimal | None = None
    previous_20_high: Decimal | None = None
    stabilized: bool = False
    breakout: bool = False
    technical_signal: str = ""
    technical_ready: bool = False
    prior_realized: Decimal = Decimal("0")
    prior_return_pct: Decimal = Decimal("0")
    status: str = "watching"
    status_label: str = ""
    status_note: str = ""


class StrategyResponse(ReviewResponse):
    open_positions: list[StrategyOpenPosition] = Field(default_factory=list)
    reentry_candidates: list[ReentryCandidate] = Field(default_factory=list)
    reentry_screened: int = 0

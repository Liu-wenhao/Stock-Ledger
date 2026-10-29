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
    currency: str | None = None
    quantity: Decimal
    # Break-even basis: includes this cycle's realized P/L, so that
    # (last_price - avg_price) * quantity == total_pnl
    avg_price: Decimal
    last_price: Decimal | None = None
    # Strategy stop levels for the open lot: trailing high-water -8% and cost -12%.
    suggested_stop_price: Decimal | None = None
    mandatory_stop_price: Decimal | None = None
    # User-set stop on the holdings page; quantity is how many shares to sell.
    stop_price: Decimal | None = None
    stop_qty: Decimal | None = None
    take_profit_price: Decimal | None = None
    # Planned position: share count and/or max cash still in this name.
    target_qty: Decimal | None = None
    max_buy_amount: Decimal | None = None
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


class PositionStopUpdate(BaseModel):
    symbol: str
    stop_price: Decimal | None = None
    stop_qty: Decimal | None = None
    take_profit_price: Decimal | None = None


class PositionLimitUpdate(BaseModel):
    symbol: str
    target_qty: Decimal | None = None
    max_buy_amount: Decimal | None = None


class PositionStopOut(BaseModel):
    symbol: str
    stop_price: Decimal | None = None
    stop_qty: Decimal | None = None
    take_profit_price: Decimal | None = None
    target_qty: Decimal | None = None
    max_buy_amount: Decimal | None = None


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


class DailyHoldingPnlRow(BaseModel):
    symbol: str
    name: str | None = None
    logo_url: str | None = None
    exchange: str | None = None
    opening_quantity: Decimal = Decimal("0")
    quantity: Decimal = Decimal("0")
    prev_close: Decimal | None = None
    close_price: Decimal | None = None
    buy_amount: Decimal = Decimal("0")
    sell_amount: Decimal = Decimal("0")
    commission: Decimal = Decimal("0")
    gross_pnl: Decimal | None = None
    gross_pnl_percent: Decimal | None = None
    day_pnl: Decimal | None = None
    day_pnl_percent: Decimal | None = None
    status: str
    trade_count: int = 0
    pricing_complete: bool = False


class DailyHoldingsPnlResponse(BaseModel):
    date: date
    start_date: date | None = None
    end_date: date | None = None
    total_gross_pnl: Decimal = Decimal("0")
    total_commission: Decimal = Decimal("0")
    total_pnl: Decimal = Decimal("0")
    items: list[DailyHoldingPnlRow] = Field(default_factory=list)


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
    realized_pnl: Decimal = Decimal("0")
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
    # Open-lot P/L remains available for stop-sensitive displays.
    pnl: Decimal | None = None
    pnl_pct: Decimal | None = None
    # Current cycle total P/L, matching the holdings page's cumulative P/L.
    total_pnl: Decimal | None = None
    total_pnl_percent: Decimal | None = None
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
    exit_price: Decimal | None = None
    change_since_exit_pct: Decimal | None = None
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


class AdviceMetrics(BaseModel):
    illiq: float | None = None
    illiq_date: str | None = None
    volume: float | None = None
    turnover: float | None = None
    anchor_date: str | None = None
    anchor_vwap: float | None = None
    vs_anchor: float | None = None
    ma20: float | None = None
    bandwidth: float | None = None
    vol: float | None = None
    mom: float | None = None
    vol_cap: float | None = None
    rp_weight: float | None = None
    pe: float | None = None
    erp: float | None = None


class TrendSection(BaseModel):
    title: str
    trend: str
    advice: str


class AdviceItem(BaseModel):
    symbol: str
    ticker: str
    name: str | None = None
    last: float | None = None
    stage: str
    tone: str = "neutral"
    headline: str = ""
    sections: list[TrendSection] = Field(default_factory=list)
    stop_price: float | None = None
    stop_hit: bool = False
    lines: list[str] = Field(default_factory=list)
    metrics: AdviceMetrics = Field(default_factory=AdviceMetrics)


class HoldingsAdvice(BaseModel):
    as_of: str | None = None
    us10y: float | None = None
    target_vol: float = 0.20
    headline: str
    points: list[str] = Field(default_factory=list)
    account_vol: float | None = None
    book_vol: float | None = None
    equity_weight: float | None = None
    equity_cap: float | None = None
    cash_weight: float | None = None
    items: list[AdviceItem] = Field(default_factory=list)


class IndicatorPoint(BaseModel):
    date: str
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    volume: float | None = None
    turnover: float | None = None
    illiq: float | None = None
    ma20: float | None = None
    vs_ma20: float | None = None
    anchor_date: str | None = None
    anchor_vwap: float | None = None
    vs_anchor: float | None = None
    bandwidth: float | None = None
    vol: float | None = None
    mom: float | None = None
    signal: str | None = None
    vol_squeeze: bool = False


class TrendBrief(BaseModel):
    headline: str
    sections: list[TrendSection] = Field(default_factory=list)
    stop_price: float | None = None
    stop_hit: bool = False
    target_price: float | None = None
    level_note: str = ""


class IndicatorHistory(BaseModel):
    symbol: str
    ticker: str
    since: str | None = None
    before: str | None = None
    has_more: bool = False
    points: list[IndicatorPoint] = Field(default_factory=list)
    brief: TrendBrief | None = None


class SentimentBreadth(BaseModel):
    universe: str
    count: int = 0
    advancers: int = 0
    decliners: int = 0
    unchanged: int = 0
    up_ratio: float | None = None
    median_change: float | None = None
    strong_up: int | None = None
    strong_down: int | None = None


class SentimentSeriesPoint(BaseModel):
    date: str
    close: float


class SentimentVix(BaseModel):
    last: float | None = None
    day_change: float | None = None
    ma20: float | None = None
    vs_ma20: float | None = None
    history: list[SentimentSeriesPoint] = Field(default_factory=list)


class SentimentIndex(BaseModel):
    name: str
    last: float | None = None
    day_change: float | None = None
    ret_5d: float | None = None
    history: list[SentimentSeriesPoint] = Field(default_factory=list)


class MarketSentiment(BaseModel):
    as_of: str | None = None
    label: str
    tone: str = "info"
    note: str
    breadth: SentimentBreadth | None = None
    vix: SentimentVix | None = None
    indexes: list[SentimentIndex] = Field(default_factory=list)


class StrategyResponse(ReviewResponse):
    open_positions: list[StrategyOpenPosition] = Field(default_factory=list)
    reentry_candidates: list[ReentryCandidate] = Field(default_factory=list)
    reentry_screened: int = 0

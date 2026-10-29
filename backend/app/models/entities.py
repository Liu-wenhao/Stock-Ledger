from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Index, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class Portfolio(Base):
    __tablename__ = "portfolio"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    base_currency: Mapped[str] = mapped_column(String(16), nullable=False, default="USD")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    trades: Mapped[list["Trade"]] = relationship(back_populates="portfolio")


class Trade(Base):
    __tablename__ = "trade"
    __table_args__ = (Index("ix_trade_portfolio_time", "portfolio_id", "closing_time"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolio.id"), nullable=False, default=1)
    symbol: Mapped[str | None] = mapped_column(String(64), nullable=True)
    side: Mapped[str] = mapped_column(String(32), nullable=False)
    qty: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False, default=0)
    fill_price: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False, default=0)
    commission: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False, default=0)
    closing_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    notes: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    portfolio: Mapped["Portfolio"] = relationship(back_populates="trades")


class SymbolMeta(Base):
    __tablename__ = "symbol_meta"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    yahoo_symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    exchange: Mapped[str | None] = mapped_column(String(64), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(16), nullable=True)
    logo_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class PriceCache(Base):
    __tablename__ = "price_cache"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    price: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    prev_close: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    open_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    high_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    low_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    change_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    change_percent: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    # Quote/bar time in the US market session. Null when unknown — never invent.
    price_as_of: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class PositionStop(Base):
    __tablename__ = "position_stop"
    __table_args__ = (
        UniqueConstraint("portfolio_id", "symbol", name="uq_position_stop_portfolio_symbol"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolio.id"), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    stop_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    stop_qty: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    take_profit_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    # Planned size: share count and/or max cash still allowed in this name.
    target_qty: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    max_buy_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class DailyBar(Base):
    """Completed daily OHLCV. Rows are written once and never updated."""

    __tablename__ = "daily_bar"
    __table_args__ = (
        UniqueConstraint("symbol", "bar_date", name="uq_daily_bar_symbol_date"),
        Index("ix_daily_bar_symbol_date", "symbol", "bar_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    bar_date: Mapped[date] = mapped_column(Date, nullable=False)
    open: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    high: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    low: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    close: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    volume: Mapped[Decimal] = mapped_column(Numeric(24, 2), nullable=False, default=0)
    dollar: Mapped[Decimal] = mapped_column(Numeric(24, 2), nullable=False, default=0)


class MarketBreadth(Base):
    """Large-cap advance/decline after the US cash session has closed."""

    __tablename__ = "market_breadth"

    session_date: Mapped[date] = mapped_column(Date, primary_key=True)
    count: Mapped[int] = mapped_column(nullable=False, default=0)
    advancers: Mapped[int] = mapped_column(nullable=False, default=0)
    decliners: Mapped[int] = mapped_column(nullable=False, default=0)
    unchanged: Mapped[int] = mapped_column(nullable=False, default=0)
    up_ratio: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)
    median_change: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)
    strong_up: Mapped[int] = mapped_column(nullable=False, default=0)
    strong_down: Mapped[int] = mapped_column(nullable=False, default=0)


class DailyNav(Base):
    __tablename__ = "daily_nav"
    __table_args__ = (UniqueConstraint("portfolio_id", "nav_date", name="uq_daily_nav"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolio.id"), nullable=False)
    nav_date: Mapped[date] = mapped_column(Date, nullable=False)
    total_value: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False, default=0)
    cash: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False, default=0)
    holdings_value: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False, default=0)
    realized_pnl: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False, default=0)
    total_pnl: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False, default=0)
    net_invested: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False, default=0)
    investment_total: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False, default=0)

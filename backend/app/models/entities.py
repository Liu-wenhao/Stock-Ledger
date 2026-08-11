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

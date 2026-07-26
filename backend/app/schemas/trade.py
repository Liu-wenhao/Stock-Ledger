from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

SideType = Literal["Buy", "Sell", "Dividend", "Withdrawal", "Deposit"]
VALID_SIDES = {"Buy", "Sell", "Dividend", "Withdrawal", "Deposit"}


class TradeBase(BaseModel):
    symbol: str | None = None
    side: SideType
    qty: Decimal = Field(default=Decimal("0"))
    fill_price: Decimal = Field(default=Decimal("0"))
    commission: Decimal = Field(default=Decimal("0"))
    closing_time: datetime
    notes: str | None = Field(default=None, max_length=128)

    @field_validator("side")
    @classmethod
    def validate_side(cls, v: str) -> str:
        if v not in VALID_SIDES:
            raise ValueError(f"Invalid side: {v}")
        return v

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, v: str | None) -> str | None:
        if v is None:
            return None
        s = v.strip().upper()
        return s or None


class TradeCreate(TradeBase):
    pass


class TradeUpdate(TradeBase):
    pass


class TradeOut(TradeBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    portfolio_id: int
    total: Decimal | None = None
    name: str | None = None
    logo_url: str | None = None


class TradeImportResult(BaseModel):
    imported: int
    deleted: int

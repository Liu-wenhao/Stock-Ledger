from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class NewsItemIn(BaseModel):
    symbols: list[str] = Field(min_length=1, max_length=50)
    title: str = Field(min_length=1, max_length=512)
    published_at: datetime
    source: str = Field(min_length=1, max_length=128)
    url: str | None = Field(default=None, max_length=4096)
    summary: str | None = Field(default=None, max_length=20_000)
    external_id: str | None = Field(default=None, max_length=255)
    session_date: date | None = None
    category: str | None = Field(default=None, max_length=64)
    sentiment: Literal["positive", "neutral", "negative"] | None = None
    metadata: dict[str, Any] | None = None

    @field_validator("published_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("published_at 必须包含时区，例如 2026-11-19T09:30:00-05:00")
        return value

    @field_validator("symbols")
    @classmethod
    def clean_symbols(cls, values: list[str]) -> list[str]:
        cleaned = list(dict.fromkeys(value.strip().upper() for value in values if value.strip()))
        if not cleaned:
            raise ValueError("symbols 不能为空")
        return cleaned


class NewsBatchIn(BaseModel):
    items: list[NewsItemIn] = Field(min_length=1, max_length=100)


class NewsIngestResult(BaseModel):
    articles: int
    rows: int
    inserted: int
    duplicates: int

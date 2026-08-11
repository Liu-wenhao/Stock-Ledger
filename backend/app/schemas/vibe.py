from typing import Any

from pydantic import BaseModel, Field


class VibeStatusOut(BaseModel):
    configured: bool
    available: bool
    url: str
    detail: str | None = None


class VibeAnalyzeRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=5000)
    include_trades: bool = True


class VibeAnalyzeOut(BaseModel):
    session_id: str
    status: str
    title: str
    upload: dict[str, Any] | None = None
    goal: dict[str, Any] | None = None
    dispatch: dict[str, Any] | None = None


class VibeSessionOut(BaseModel):
    session_id: str
    title: str = ""
    status: str = "unknown"
    created_at: str | None = None
    updated_at: str | None = None
    last_attempt_id: str | None = None


class VibeMessageOut(BaseModel):
    message_id: str | None = None
    session_id: str | None = None
    role: str
    content: str
    created_at: str | None = None
    linked_attempt_id: str | None = None
    metadata: dict[str, Any] | None = None
    tool_trail: list[dict[str, Any]] = Field(default_factory=list)

from __future__ import annotations

from datetime import datetime
import re
from typing import NoReturn

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_db
from app.models.entities import Trade
from app.schemas.vibe import (
    VibeAnalyzeOut,
    VibeAnalyzeRequest,
    VibeMessageOut,
    VibeSessionOut,
    VibeStatusOut,
)
from app.services.csv_io import export_csv
from app.services.vibe_trading import VibeTradingClient, VibeTradingError

router = APIRouter(prefix="/api/vibe", tags=["vibe-trading"])
settings = get_settings()
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _client() -> VibeTradingClient:
    return VibeTradingClient(settings)


def _valid_session_id(session_id: str) -> str:
    if not _SESSION_ID_RE.fullmatch(session_id):
        raise HTTPException(400, "无效的 Vibe-Trading 会话 ID")
    return session_id


def _raise_vibe_error(exc: VibeTradingError) -> NoReturn:
    raise HTTPException(exc.status_code, exc.detail) from exc


@router.get("/status", response_model=VibeStatusOut)
async def vibe_status():
    client = _client()
    try:
        await client.list_sessions()
    except VibeTradingError as exc:
        return VibeStatusOut(
            configured=bool(settings.vibe_trading_url),
            available=False,
            url=client.base_url,
            detail=exc.detail,
        )
    return VibeStatusOut(configured=True, available=True, url=client.base_url)


@router.post("/analyze", response_model=VibeAnalyzeOut)
async def analyze_trades(payload: VibeAnalyzeRequest, db: Session = Depends(get_db)):
    client = _client()
    upload = None
    prompt = payload.prompt.strip()

    if payload.include_trades:
        trade_count = (
            db.query(Trade)
            .filter(Trade.portfolio_id == settings.default_portfolio_id)
            .count()
        )
        if trade_count == 0:
            raise HTTPException(400, "当前没有可分析的交易记录")
        csv_text = export_csv(db, portfolio_id=settings.default_portfolio_id)
        try:
            upload = await client.upload_csv("stock-ledger-trades.csv", csv_text.encode("utf-8-sig"))
        except VibeTradingError as exc:
            _raise_vibe_error(exc)

        file_path = upload.get("file_path") if isinstance(upload, dict) else None
        if not file_path:
            raise HTTPException(502, "Vibe-Trading 上传成功但未返回文件路径")
        prompt = (
            f'请读取交易记录文件 "{file_path}"，基于文件中的全部实际交易完成以下研究：\n'
            f"{prompt}\n\n"
            "要求：区分事实、推断和建议；给出关键指标、证据与风险；"
            "仅进行研究、行为诊断和回测，不连接券商，不执行任何交易。"
        )

    title = f"Stock Ledger 研究 {datetime.now().strftime('%m-%d %H:%M')}"
    try:
        session = await client.create_session(title)
        session_id = str(session.get("session_id") or "")
        if not session_id:
            raise VibeTradingError("创建会话后未返回 session_id")
        goal = await client.create_goal(
            session_id,
            objective=prompt,
            ui_summary=payload.prompt.strip(),
        )
        dispatch = await client.send_message(session_id, prompt)
    except VibeTradingError as exc:
        _raise_vibe_error(exc)

    return VibeAnalyzeOut(
        session_id=session_id,
        status=str(session.get("status") or "running"),
        title=str(session.get("title") or title),
        upload=upload,
        goal=goal if isinstance(goal, dict) else None,
        dispatch=dispatch if isinstance(dispatch, dict) else None,
    )


@router.get("/sessions/{session_id}", response_model=VibeSessionOut)
async def get_vibe_session(session_id: str):
    try:
        return await _client().get_session(_valid_session_id(session_id))
    except VibeTradingError as exc:
        _raise_vibe_error(exc)


@router.get("/sessions/{session_id}/messages", response_model=list[VibeMessageOut])
async def get_vibe_messages(session_id: str):
    try:
        return await _client().get_messages(_valid_session_id(session_id))
    except VibeTradingError as exc:
        _raise_vibe_error(exc)


@router.post("/sessions/{session_id}/cancel")
async def cancel_vibe_session(session_id: str):
    try:
        return await _client().cancel(_valid_session_id(session_id))
    except VibeTradingError as exc:
        _raise_vibe_error(exc)

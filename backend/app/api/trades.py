from decimal import Decimal

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_db
from app.models.entities import DailyNav, SymbolMeta, Trade
from app.schemas.trade import TradeCreate, TradeImportResult, TradeOut, TradeUpdate
from app.services.csv_io import export_csv, import_csv
from app.services.market import ensure_symbol_meta

router = APIRouter(prefix="/api/trades", tags=["trades"])
settings = get_settings()


def _trade_total(t: Trade) -> Decimal:
    qty = Decimal(t.qty or 0)
    price = Decimal(t.fill_price or 0)
    commission = Decimal(t.commission or 0)
    if t.side == "Buy":
        return qty * price + commission
    if t.side == "Sell":
        return qty * price - commission
    return price


def _to_out(db: Session, t: Trade) -> TradeOut:
    meta = None
    if t.symbol:
        meta = db.query(SymbolMeta).filter(SymbolMeta.symbol == t.symbol).first()
    return TradeOut(
        id=t.id,
        portfolio_id=t.portfolio_id,
        symbol=t.symbol,
        side=t.side,
        qty=t.qty,
        fill_price=t.fill_price,
        commission=t.commission,
        closing_time=t.closing_time,
        notes=t.notes,
        total=_trade_total(t),
        name=meta.name if meta else None,
        logo_url=meta.logo_url if meta else None,
    )


@router.get("", response_model=list[TradeOut])
def list_trades(
    side_group: str = Query("trades", pattern="^(trades|dividends|all)$"),
    side: str | None = Query(None, description="Exact Side filter, e.g. Buy/Deposit"),
    q: str | None = None,
    db: Session = Depends(get_db),
):
    query = db.query(Trade).filter(Trade.portfolio_id == settings.default_portfolio_id)
    if side_group == "trades":
        query = query.filter(Trade.side.in_(["Buy", "Sell", "Deposit", "Withdrawal"]))
    elif side_group == "dividends":
        query = query.filter(Trade.side == "Dividend")
    if side:
        query = query.filter(Trade.side == side)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(Trade.symbol.ilike(like))
    rows = query.order_by(Trade.closing_time.desc(), Trade.id.desc()).all()
    return [_to_out(db, t) for t in rows]


@router.post("", response_model=TradeOut)
def create_trade(payload: TradeCreate, db: Session = Depends(get_db)):
    t = Trade(portfolio_id=settings.default_portfolio_id, **payload.model_dump())
    db.add(t)
    if t.symbol:
        ensure_symbol_meta(db, t.symbol)
    db.query(DailyNav).filter(DailyNav.portfolio_id == settings.default_portfolio_id).delete()
    db.commit()
    db.refresh(t)
    return _to_out(db, t)


@router.put("/{trade_id}", response_model=TradeOut)
def update_trade(trade_id: int, payload: TradeUpdate, db: Session = Depends(get_db)):
    t = db.query(Trade).filter(Trade.id == trade_id).first()
    if not t:
        raise HTTPException(404, "Trade not found")
    for k, v in payload.model_dump().items():
        setattr(t, k, v)
    if t.symbol:
        ensure_symbol_meta(db, t.symbol)
    db.query(DailyNav).filter(DailyNav.portfolio_id == settings.default_portfolio_id).delete()
    db.commit()
    db.refresh(t)
    return _to_out(db, t)


@router.delete("/{trade_id}")
def delete_trade(trade_id: int, db: Session = Depends(get_db)):
    t = db.query(Trade).filter(Trade.id == trade_id).first()
    if not t:
        raise HTTPException(404, "Trade not found")
    db.delete(t)
    db.query(DailyNav).filter(DailyNav.portfolio_id == settings.default_portfolio_id).delete()
    db.commit()
    return {"ok": True}


@router.post("/import", response_model=TradeImportResult)
async def import_trades(file: UploadFile = File(...), db: Session = Depends(get_db)):
    content = await file.read()
    try:
        result = import_csv(db, content, portfolio_id=settings.default_portfolio_id, replace=True)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return result


@router.get("/export")
def export_trades(db: Session = Depends(get_db)):
    text = export_csv(db, portfolio_id=settings.default_portfolio_id)
    return PlainTextResponse(
        text,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=trades.csv"},
    )

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_db
from app.models.entities import SymbolMeta
from app.schemas.portfolio import BreadthHistory, CnFlowSnapshot, CnMarketDesk, CnStockFlowBook, MarketSentiment, QuoteOut, SymbolSearchItem
from app.services.cn_desk import cn_market_desk, cn_stock_flow
from app.services.cn_flow import cn_flow_snapshot
from app.services.market import get_quotes, refresh_quotes_sync
from app.services.market_scope import current_market, in_market
from app.services.sentiment import breadth_history_payload, build_market_sentiment

router = APIRouter(prefix="/api", tags=["market"])
settings = get_settings()


@router.get("/market/sentiment", response_model=MarketSentiment)
def market_sentiment(db: Session = Depends(get_db)):
    """Breadth and index paths for the selected market."""
    return build_market_sentiment(db, current_market())


@router.get("/market/breadth-history", response_model=BreadthHistory)
def market_breadth_history(book: str = Query(""), db: Session = Depends(get_db)):
    """Recent advance/decline and Arms readings for one book."""
    return breadth_history_payload(db, current_market(), book)


@router.get("/market/cn-flow", response_model=CnFlowSnapshot)
def market_cn_flow():
    """Latest A-share margin balance and broad-ETF share changes. Not a forecast."""
    return cn_flow_snapshot()


@router.get("/market/cn-desk", response_model=CnMarketDesk)
def market_cn_desk():
    """A-share breadth, turnover, fund flow, and northbound summary."""
    return cn_market_desk()


@router.get("/market/cn-stock-flow", response_model=CnStockFlowBook)
def market_cn_stock_flow(symbols: str = Query("")):
    """Latest main-force flow and inner/outer volume for the given A-share symbols."""
    return cn_stock_flow([part.strip() for part in symbols.split(",") if part.strip()])


@router.get("/market/quote", response_model=list[QuoteOut])
def quotes(symbols: str = Query(...), db: Session = Depends(get_db)):
    syms = [s.strip() for s in symbols.split(",") if s.strip()]
    data = get_quotes(db, syms, background_refresh=settings.quote_refresh_enabled)
    metas = {
        m.symbol: m
        for m in db.query(SymbolMeta).filter(SymbolMeta.symbol.in_(syms)).all()
    }
    return [
        QuoteOut(
            symbol=s,
            price=data.get(s, {}).get("price"),
            change_percent=data.get(s, {}).get("change_percent"),
            name=metas.get(s).name if metas.get(s) else None,
        )
        for s in syms
    ]


@router.post("/market/refresh")
def refresh_market(force: bool = Query(True), db: Session = Depends(get_db)):
    if not settings.quote_refresh_enabled:
        return {
            "symbols": 0,
            "priced": 0,
            "disabled": True,
            "message": "Market refresh disabled (MARKET_REFRESH_ENABLED=false)",
        }
    from app.services import ledger

    from app.services.market_scope import current_market
    from app.services.watchlist import watch_symbols

    state = ledger.replay(ledger.load_trades(db, settings.default_portfolio_id))
    syms = sorted(
        {
            *(sym for sym, position in state.positions.items() if position.quantity > 0),
            *watch_symbols(current_market()),
        }
    )
    data = refresh_quotes_sync(db, syms, force=force)
    priced = sum(1 for v in data.values() if v.get("price") is not None)
    return {"symbols": len(syms), "priced": priced, "disabled": False}


@router.get("/symbols/search", response_model=list[SymbolSearchItem])
def search_symbols(q: str = Query("", min_length=0), db: Session = Depends(get_db)):
    query = db.query(SymbolMeta)
    if q.strip():
        like = f"%{q.strip()}%"
        query = query.filter(
            (SymbolMeta.symbol.ilike(like)) | (SymbolMeta.name.ilike(like))
        )
    rows = query.order_by(SymbolMeta.symbol.asc()).limit(80).all()
    rows = [row for row in rows if in_market(row.symbol)][:30]
    return [
        SymbolSearchItem(symbol=r.symbol, name=r.name, logo_url=r.logo_url) for r in rows
    ]

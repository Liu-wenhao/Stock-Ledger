from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from app.api import market_api, portfolio, trades
from app.core.config import get_settings
from app.core.database import Base, SessionLocal, engine
from app.models.entities import Portfolio

settings = get_settings()


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    db: Session = SessionLocal()
    try:
        existing = db.query(Portfolio).filter(Portfolio.id == settings.default_portfolio_id).first()
        if not existing:
            db.add(
                Portfolio(
                    id=settings.default_portfolio_id,
                    name=settings.default_portfolio_name,
                    base_currency=settings.base_currency,
                )
            )
            db.commit()
    finally:
        db.close()


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="Stock Ledger API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(trades.router)
app.include_router(portfolio.router)
app.include_router(market_api.router)


@app.get("/api/health")
def health():
    return {"status": "ok"}

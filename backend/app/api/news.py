import secrets

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_db
from app.schemas.news import NewsBatchIn, NewsIngestResult
from app.services.news import ingest_news

router = APIRouter(prefix="/api/news", tags=["news"])
settings = get_settings()


def _authorize(x_news_token: str | None = Header(default=None, alias="X-News-Token")) -> None:
    expected = settings.news_ingest_token.strip()
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="NEWS_INGEST_TOKEN 尚未配置",
        )
    supplied = str(x_news_token or "")
    if not supplied or not secrets.compare_digest(supplied, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="新闻写入令牌无效",
        )


@router.post(
    "/ingest",
    response_model=NewsIngestResult,
    dependencies=[Depends(_authorize)],
)
def ingest_stock_news(payload: NewsBatchIn, db: Session = Depends(get_db)):
    """Receive a deduplicated batch of stock news from Grok Bot."""
    try:
        return ingest_news(db, payload)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

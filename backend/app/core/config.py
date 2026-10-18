from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "mysql+pymysql://stock:stockledger123@127.0.0.1:3306/stock_ledger"
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    quote_cache_seconds: int = 60
    # Backward-compatible environment flag. MARKET_REFRESH_ENABLED takes
    # precedence when set; YAHOO_REFRESH_ENABLED remains supported.
    market_refresh_enabled: bool | None = None
    yahoo_refresh_enabled: bool = True
    benchmark_symbol: str = "QQQ"
    default_portfolio_id: int = 1
    default_portfolio_name: str = "我的投资组合"
    base_currency: str = "USD"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def quote_refresh_enabled(self) -> bool:
        if self.market_refresh_enabled is not None:
            return self.market_refresh_enabled
        return self.yahoo_refresh_enabled


@lru_cache
def get_settings() -> Settings:
    return Settings()

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
    # AkShare is the default A-share/HK source. US history and live quotes use YFinance.
    tushare_token: str = ""
    enable_eastmoney_patch: bool = False
    max_workers: int = 4
    longbridge_oauth_client_id: str = ""
    longbridge_oauth_token_cache_b64: str = ""
    longbridge_app_key: str = ""
    longbridge_app_secret: str = ""
    longbridge_access_token: str = ""
    longbridge_connection_cooldown_seconds: int = 15
    longbridge_http_url: str = ""
    longbridge_quote_ws_url: str = ""
    longbridge_trade_ws_url: str = ""
    longbridge_region: str = ""

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

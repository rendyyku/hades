from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    HELIUS_API_KEY: str = ""
    DATABASE_URL: str = "sqlite:///./hades.db"
    HADES_API_KEY: str = ""
    HELIUS_WEBHOOK_SECRET: str = ""
    HELIUS_WEBHOOK_ID: str = ""
    PUBLIC_WEBHOOK_URL: str = ""
    HELIUS_MAX_PAGES_PER_ADDRESS: int = 20
    EARLY_BUYERS_N: int = 100
    SIG_SCAN_MAX_PAGES: int = 30
    RATE_LIMIT_RPS: int = 8
    # live webhook also follows the top-N non-bot wallets with score >= ALERT_MIN_SCORE
    WEBHOOK_TOP_SCORE_MAX: int = 25


@lru_cache
def get_settings() -> Settings:
    return Settings()

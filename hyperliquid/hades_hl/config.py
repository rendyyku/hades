"""Settings for the Hyperliquid head: env prefix ``HL_``, read from ``.env`` / ``../.env``."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="HL_",
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # X-Hades-Key for this head's API. repr=False: never printed or logged.
    API_KEY: str = Field(default="", repr=False)
    DATABASE_URL: str = "sqlite:///hades_hl.db"
    INFO_URL: str = "https://api.hyperliquid.xyz/info"
    WS_URL: str = "wss://api.hyperliquid.xyz/ws"
    # Unofficial: the JSON the HL web app's leaderboard page loads (verified live 2026-11-20).
    LEADERBOARD_URL: str = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
    # Unofficial: the vault list the HL app's Vaults page loads (verified live 2026-11-20).
    VAULTS_URL: str = "https://stats-data.hyperliquid.xyz/Mainnet/vaults"
    MAX_FOLLOWS: int = 25
    AUTO_FOLLOW_N: int = 10
    # "REST requests share an aggregated weight limit of 1200 per minute" (per IP).
    # Source: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits
    RATE_WEIGHT_PER_MIN: int = 1200


@lru_cache
def get_settings() -> Settings:
    return Settings()

"""Settings (secrets from .env) and AppConfig (everything else, from config.yaml)."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    GITHUB_TOKEN: str = ""
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""
    COINGECKO_API_KEY: str = ""
    HADES_API_KEY: str = ""
    DATABASE_URL: str = "sqlite:///./hades.db"
    HADES_CONFIG: str = "config.yaml"


@lru_cache
def get_settings() -> Settings:
    return Settings()


class ConfigError(Exception):
    """config.yaml is missing, not YAML, or has an invalid field (message names the field path)."""


class _Cfg(BaseModel):
    model_config = {"extra": "forbid"}


class ChainCfg(_Cfg):
    geckoterminal: str | None = None
    dexscreener: str | None = None
    evm: bool = True
    priority: bool = False


class RepoTarget(_Cfg):
    repo: str
    path: str
    mode: Literal["dir_added", "json_tokenlist", "regex_entries"]
    chains: list[str] = []
    # repo's chain folder name / chainId -> our chain key
    chain_map: dict[str, str] = {}
    regex: str | None = None


class RepoDiffCfg(_Cfg):
    interval_s: int = 900
    targets: list[RepoTarget] = []


# Shared hosting / social / docs hosts: a token "website" on one of these says nothing about the project,
# and watching it on crt.sh would fire on everyone else's subdomains.
DOMAIN_DENYLIST = [
    "linktr.ee", "x.com", "twitter.com", "t.me", "telegram.org", "telegram.me", "medium.com", "github.com",
    "github.io", "gitbook.io", "gitbook.com", "discord.gg", "discord.com", "notion.site", "notion.so",
    "google.com", "youtube.com", "facebook.com", "instagram.com", "linkedin.com", "reddit.com", "tiktok.com",
    "coingecko.com", "coinmarketcap.com", "etherscan.io", "dexscreener.com", "vercel.app", "netlify.app",
    "pages.dev", "carrd.co", "mirror.xyz", "substack.com", "wixsite.com", "webflow.io", "beacons.ai",
]

HOT_LABELS = ["claim", "airdrop", "stake", "staking", "points", "tge", "token", "bridge", "rewards", "app"]


class CtCfg(_Cfg):
    interval_s: int = 21600
    domains: list[str] = []
    hot_labels: list[str] = Field(default_factory=lambda: list(HOT_LABELS))
    domain_denylist: list[str] = Field(default_factory=lambda: list(DOMAIN_DENYLIST))


class GroundTruthCfg(_Cfg):
    interval_s: int = 600
    exchanges: list[str] = Field(default_factory=lambda: ["coinbase", "kraken", "okx", "binance", "bybit"])


class EnrichCfg(_Cfg):
    min_liquidity_usd: float = 1000
    low_liquidity_usd: float = 50000
    recheck_after_s: int = 86400


class ScoringCfg(_Cfg):
    base: dict[str, int] = Field(
        default_factory=lambda: {"pr_opened": 40, "merged": 30, "ct_high": 30, "ct_low": 10}
    )
    priority_chain: int = 15
    earliness: int = 15
    fusion_per_source: int = 20
    fusion_max: int = 40
    fusion_window_days: int = 7


class AlertsCfg(_Cfg):
    min_score: int = 40
    digest_hour_wib: int = 8


class AppConfig(_Cfg):
    chains: dict[str, ChainCfg] = {}
    repo_diff: RepoDiffCfg = RepoDiffCfg()
    ct: CtCfg = CtCfg()
    ground_truth: GroundTruthCfg = GroundTruthCfg()
    enrich: EnrichCfg = EnrichCfg()
    scoring: ScoringCfg = ScoringCfg()
    alerts: AlertsCfg = AlertsCfg()


def load_config(path: str | Path) -> AppConfig:
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        raise ConfigError(f"{path}: {e}") from e
    try:
        return AppConfig.model_validate(data)
    except ValidationError as e:
        msgs = [f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors()]
        raise ConfigError(f"{path}: " + "; ".join(msgs)) from e

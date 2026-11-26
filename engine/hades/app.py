"""Process wiring: one asyncio process runs the watchers, rechecks, digest, bot and the API."""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
import uvicorn

from hades.api import create_app
from hades.bot import poll_loop
from hades.bus import Bus
from hades.config import AppConfig, Settings
from hades.github import GitHub
from hades.http import Http
from hades.notify import Notifier, digest_loop
from hades.pipeline import Pipeline
from hades.runner import Runner
from hades.store import Store
from hades.watchers.base import Watcher
from hades.watchers.ct import CtWatcher
from hades.watchers.ground_truth import GroundTruthWatcher
from hades.watchers.repo_diff import build_repo_watchers

log = logging.getLogger(__name__)

RATES = {
    "api.github.com": 1.0,
    "crt.sh": 0.2,
    "api.geckoterminal.com": 0.5,
    "api.dexscreener.com": 2.0,
    "api.telegram.org": 5.0,
}
RECHECK_EVERY_S = 600


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx logs full request URLs at INFO; the Telegram bot token is part of the URL
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


@dataclass
class Services:
    settings: Settings
    cfg: AppConfig
    client: httpx.AsyncClient
    store: Store
    http: Http
    bus: Bus
    notifier: Notifier
    pipeline: Pipeline
    watchers: list[Watcher]
    runner: Runner


def build_watchers(cfg: AppConfig, store: Store, gh: GitHub) -> list[Watcher]:
    watchers: list[Watcher] = []
    for t in cfg.repo_diff.targets:
        watchers += build_repo_watchers(t, gh, cfg.repo_diff.interval_s, cfg.chains)
    watchers.append(CtWatcher(store, cfg.ct.hot_labels, cfg.ct.interval_s))
    watchers += [GroundTruthWatcher(ex, cfg.ground_truth.interval_s) for ex in cfg.ground_truth.exchanges]
    return watchers


def build(settings: Settings, cfg: AppConfig) -> Services:
    store = Store(settings.DATABASE_URL)
    for d in cfg.ct.domains:
        store.add_domain(d.lower(), "manual")
    client = httpx.AsyncClient(timeout=20.0, follow_redirects=True)
    http = Http(client, RATES)
    bus = Bus()
    notifier = Notifier(http, store, settings.TELEGRAM_BOT_TOKEN, settings.TELEGRAM_CHAT_ID)
    pipeline = Pipeline(store, http, cfg, notifier, bus, coingecko_key=settings.COINGECKO_API_KEY)
    watchers = build_watchers(cfg, store, GitHub(http, settings.GITHUB_TOKEN))
    runner = Runner(store, http, watchers, pipeline, on_listing=pipeline.match_listing)
    return Services(settings, cfg, client, store, http, bus, notifier, pipeline, watchers, runner)


async def _recheck_loop(pipeline: Pipeline, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await pipeline.recheck_due(datetime.now(timezone.utc))
        except Exception:  # noqa: BLE001
            log.exception("recheck failed")
        try:
            await asyncio.wait_for(stop.wait(), RECHECK_EVERY_S)
        except asyncio.TimeoutError:
            pass


async def serve(s: Services, host: str = "127.0.0.1", port: int = 8000) -> None:
    stop = asyncio.Event()
    api = create_app(s.store, s.bus, s.settings)
    server = uvicorn.Server(uvicorn.Config(api, host=host, port=port, log_level="warning"))
    tasks = [
        asyncio.create_task(s.runner.run_forever(stop)),
        asyncio.create_task(_recheck_loop(s.pipeline, stop)),
        asyncio.create_task(digest_loop(s.notifier, s.store, s.cfg, stop)),
        asyncio.create_task(poll_loop(s.http, s.notifier, s.store, s.settings.TELEGRAM_BOT_TOKEN,
                                      s.settings.TELEGRAM_CHAT_ID, stop)),
    ]
    log.info("HADES v2 running: %d watchers, API on http://%s:%d", len(s.watchers), host, port)
    try:
        await server.serve()
    finally:
        stop.set()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await s.client.aclose()

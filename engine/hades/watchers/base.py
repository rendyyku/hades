"""Watcher plugin interface (spec 5): fetch -> parse -> diff."""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

from hades.models import WatchResult


class Watcher(ABC):
    name: str
    interval_s: int

    @abstractmethod
    async def fetch(self, http: Any, state: dict | None) -> Any:
        """Network I/O only. Raise on failure; the runner records it."""

    @abstractmethod
    def parse(self, raw: Any) -> Any:
        """Pure: raw response -> items."""

    @abstractmethod
    def diff(self, state: dict | None, parsed: Any, now: datetime) -> WatchResult:
        """Pure: emit only NEW things. state None = first run: baseline, no signals/listings."""

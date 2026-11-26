from datetime import datetime, timezone

import pytest

from hades.models import Signal
from hades.store import Store

NOW = datetime(2026, 11, 27, 7, 5, tzinfo=timezone.utc)


@pytest.fixture
def store():
    return Store("sqlite://")


def make_signal(**kw) -> Signal:
    base = dict(
        source="repo_diff", kind="merged", project_key="monad:0xabc",
        evidence_url="https://github.com/a/b/commit/1", observed_at=NOW,
        dedupe_key="repo_diff:a/b:monad:0xabc", chain="monad", address="0xabc", symbol="CAT", name="Cash Cat",
    )
    base.update(kw)
    return Signal(**base)

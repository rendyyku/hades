from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from hades_core.events import TradeEvent

T0 = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


def make(**kw):
    base = dict(
        head="hyperliquid",
        wallet="0xabc",
        market_id="BTC",
        side="open",
        direction="long",
        size=Decimal("1.5"),
        price=Decimal("60000"),
        notional_usd=Decimal("90000"),
        fee_usd=Decimal("4.5"),
        leader_time=T0,
        observed_time=T0 + timedelta(seconds=2),
        source_ref="0xhash:1",
    )
    base.update(kw)
    return TradeEvent(**base)


def test_event_requires_utc():
    naive = datetime(2026, 11, 1, 12, 0)
    with pytest.raises(ValueError):
        make(leader_time=naive)
    with pytest.raises(ValueError):
        make(observed_time=naive)


def test_event_rejects_non_utc_offset():
    plus7 = timezone(timedelta(hours=7))
    with pytest.raises(ValueError):
        make(leader_time=datetime(2026, 11, 1, 19, 0, tzinfo=plus7))


def test_event_is_hashable_and_frozen():
    ev = make(leverage=Decimal("5"), position_id="p1")
    assert hash(ev) == hash(make(leverage=Decimal("5"), position_id="p1"))
    assert len({ev, make(leverage=Decimal("5"), position_id="p1")}) == 1
    with pytest.raises(FrozenInstanceError):
        ev.size = Decimal("2")  # type: ignore[misc]


def test_event_optional_fields_default_none():
    ev = make()
    assert ev.leverage is None
    assert ev.position_id is None

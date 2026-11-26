from datetime import datetime, timezone

import pytest

from hades.models import Signal


def _sig(**kw):
    base = dict(
        source="ct", kind="ct_high", project_key="dom:cat.xyz", evidence_url="https://crt.sh/?id=1",
        observed_at=datetime(2026, 11, 27, tzinfo=timezone.utc), dedupe_key="ct:claim.cat.xyz",
    )
    base.update(kw)
    return Signal(**base)


def test_signal_ok():
    assert _sig().severity == "high"


def test_signal_requires_evidence():
    with pytest.raises(ValueError):
        _sig(evidence_url="")


def test_signal_requires_aware_time():
    with pytest.raises(ValueError):
        _sig(observed_at=datetime(2026, 11, 27))

from decimal import Decimal

import pytest

from hades_core.gate import GateResult
from hades_core.ladder import gap, next_ladder


def gr(passed, **checks):
    return GateResult(passed=passed, checks=checks, values={})


FULL_PASS = gr(True, days=True, closed_trades=True, net_pnl=True, max_dd=True, top_share=True)
FULL_FAIL = gr(False, days=False, closed_trades=True, net_pnl=True, max_dd=True, top_share=True)
ROLL_OK = gr(True, net_pnl=True, max_dd=True)
ROLL_NET_BAD = gr(False, net_pnl=False, max_dd=True)
ROLL_DD_BAD = gr(False, net_pnl=True, max_dd=False)


def test_ladder_promote():
    assert next_ladder("PAPER", FULL_PASS, ROLL_OK) == "NOTIF"
    assert next_ladder("PAPER", FULL_FAIL, ROLL_OK) == "PAPER"


def test_ladder_stays_notif_when_rolling_ok_even_if_full_fails():
    assert next_ladder("NOTIF", FULL_FAIL, ROLL_OK) == "NOTIF"


@pytest.mark.parametrize("rolling", [ROLL_NET_BAD, ROLL_DD_BAD])
def test_ladder_demote_rolling(rolling):
    assert next_ladder("NOTIF", FULL_PASS, rolling) == "PAPER"
    assert next_ladder("COPY", FULL_PASS, rolling) == "PAPER"


def test_copy_never_automatic():
    assert next_ladder("NOTIF", FULL_PASS, ROLL_OK) == "NOTIF"
    assert next_ladder("PAPER", FULL_PASS, ROLL_OK) == "NOTIF"
    assert next_ladder("COPY", FULL_PASS, ROLL_OK) == "COPY"


def test_unknown_state_rejected():
    with pytest.raises(ValueError):
        next_ladder("LIVE", FULL_PASS, ROLL_OK)  # type: ignore[arg-type]


def test_gap_is_leader_minus_paper():
    assert gap(Decimal("1000"), Decimal("650.5")) == Decimal("349.5")
    assert gap(Decimal("-10"), Decimal("5")) == Decimal("-15")


# -- final review I1: no flapping ------------------------------------------------------

@pytest.mark.parametrize("rolling", [ROLL_NET_BAD, ROLL_DD_BAD])
def test_no_promotion_while_rolling_gate_fails(rolling):
    # a wallet the rolling rule would demote at once is never promoted
    assert next_ladder("PAPER", FULL_PASS, rolling) == "PAPER"


def test_demote_then_rerun_stays_paper():
    state = next_ladder("NOTIF", FULL_PASS, ROLL_NET_BAD)
    assert state == "PAPER"
    for _ in range(3):  # the next runs see the same gates
        state = next_ladder(state, FULL_PASS, ROLL_NET_BAD)
        assert state == "PAPER"


def test_cooling_down_blocks_promotion_only():
    assert next_ladder("PAPER", FULL_PASS, ROLL_OK, cooling_down=True) == "PAPER"
    assert next_ladder("PAPER", FULL_PASS, ROLL_OK, cooling_down=False) == "NOTIF"
    # a cool-down never delays a demotion and never demotes
    assert next_ladder("NOTIF", FULL_PASS, ROLL_NET_BAD, cooling_down=True) == "PAPER"
    assert next_ladder("NOTIF", FULL_FAIL, ROLL_OK, cooling_down=True) == "NOTIF"
    assert next_ladder("COPY", FULL_PASS, ROLL_OK, cooling_down=True) == "COPY"

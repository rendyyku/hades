from datetime import datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from hades_core.contract import Alert, HeadHealth, PaperSummary, WalletRow

T0 = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


def wallet(**kw):
    base = dict(
        address="0xabc",
        score=71.5,
        net_pnl_usd=Decimal("1234.56"),
        trades=40,
        winrate=0.6,
        n=40,
        tags=["fresh_whale"],
        is_bot=False,
        extra={"avg_leverage": "4.2"},
    )
    base.update(kw)
    return WalletRow(**base)


def test_walletrow_winrate_requires_n():
    with pytest.raises(ValidationError):
        wallet(winrate=0.5, n=0)
    # winrate absent with n == 0 is fine
    assert wallet(winrate=None, n=0).winrate is None


def test_contract_roundtrip_json():
    models = [
        HeadHealth(head="hyperliquid", ok=True, last_ingest_at=T0, version="0.1.0"),
        HeadHealth(head="polymarket", ok=False, last_ingest_at=None, version="0.1.0"),
        wallet(),
        PaperSummary(
            address="0xabc",
            ladder="NOTIF",
            equity_usd=Decimal("10500.25"),
            net_pnl_usd=Decimal("500.25"),
            max_dd=Decimal("0.12"),
            closed_trades=31,
            days_followed=29,
            gap_usd=Decimal("-42.10"),
            origin="auto",
        ),
        Alert(
            id=7,
            head="hyperliquid",
            kind="paper_open",
            wallet="0xabc",
            market_id="BTC",
            payload={"size": "1.5"},
            created_at=T0,
        ),
    ]
    for m in models:
        again = type(m).model_validate_json(m.model_dump_json())
        assert again == m


def test_paper_summary_rejects_unknown_ladder():
    with pytest.raises(ValidationError):
        PaperSummary(
            address="0xabc",
            ladder="LIVE",
            equity_usd=Decimal("1"),
            net_pnl_usd=Decimal("0"),
            max_dd=Decimal("0"),
            closed_trades=0,
            days_followed=0,
            gap_usd=Decimal("0"),
            origin="manual",
        )

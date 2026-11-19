from datetime import datetime, timezone

from sqlalchemy import func, select, text

from hades.config import Settings, get_settings
from hades.db import (
    Alert,
    Swap,
    Token,
    Wallet,
    WalletStats,
    WalletTag,
    Watchlist,
    init_db,
    make_engine,
    session_scope,
)

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)


def _swap(**kw):
    base = dict(
        signature="sig1",
        wallet="W1",
        mint="M1",
        slot=100,
        block_time=NOW,
        side="buy",
        token_amount=1000.0,
        sol_amount=1.5,
        source="backfill",
        dex="raydium",
    )
    base.update(kw)
    return Swap(**base)


def test_swap_pk_dedup(session):
    session.merge(_swap())
    session.commit()
    session.merge(_swap())
    session.commit()
    assert session.scalar(select(func.count()).select_from(Swap)) == 1


def test_swap_distinct_mint_not_deduped(session):
    session.merge(_swap())
    session.merge(_swap(mint="M2"))
    session.commit()
    assert session.scalar(select(func.count()).select_from(Swap)) == 2


def test_wal_enabled(tmp_path):
    eng = make_engine(f"sqlite:///{(tmp_path / 'h.db').as_posix()}")
    try:
        with eng.connect() as conn:
            mode = conn.execute(text("PRAGMA journal_mode")).scalar()
        assert mode.lower() == "wal"
    finally:
        eng.dispose()


def test_all_models_roundtrip(session):
    session.add_all(
        [
            Token(mint="M1", symbol="MEME", name="Meme", deployer="D1", is_runner=True,
                  first_seen_at=NOW, pool_created_at=NOW, pool_created_slot=99),
            Wallet(address="W1", first_funder="F1", funded_at=NOW, is_bot=False),
            WalletStats(address="W1", window_days=30, realized_pnl_sol=1.0,
                        unrealized_pnl_sol=0.5, total_pnl_sol=1.5, tokens_traded=3,
                        tokens_closed=2, win_rate=0.5, median_hold_sec=600.0,
                        trades_per_day=2.0, score=55.0, computed_at=NOW),
            WalletTag(address="W1", tag="sniper", mint="M1",
                      evidence_json={"slot_delta": 1}, created_at=NOW),
            WalletTag(address="W1", tag="bot", mint=None, evidence_json={}, created_at=NOW),
            Watchlist(address="W1", note="watch", added_at=NOW),
            Alert(wallet="W1", mint="M1", side="buy", sol_amount=1.5,
                  signature="sig1", block_time=NOW, created_at=NOW),
        ]
    )
    session.commit()
    tags = session.scalars(select(WalletTag).order_by(WalletTag.id)).all()
    assert [t.id for t in tags] == [1, 2]
    assert tags[0].evidence_json == {"slot_delta": 1}
    alert = session.scalars(select(Alert)).one()
    assert alert.id == 1
    assert alert.delivered_tg is False
    assert session.get(Token, "M1").is_runner is True


def test_session_scope_commits_and_rolls_back(engine):
    with session_scope(engine) as s:
        s.add(Watchlist(address="W1", note="a", added_at=NOW))
    try:
        with session_scope(engine) as s:
            s.add(Watchlist(address="W2", note="b", added_at=NOW))
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    with session_scope(engine) as s:
        addrs = s.scalars(select(Watchlist.address)).all()
    assert addrs == ["W1"]


def test_settings_defaults(monkeypatch):
    for k in ("DATABASE_URL", "HELIUS_MAX_PAGES_PER_ADDRESS", "EARLY_BUYERS_N",
              "SIG_SCAN_MAX_PAGES", "RATE_LIMIT_RPS", "HELIUS_WEBHOOK_ID",
              "PUBLIC_WEBHOOK_URL"):
        monkeypatch.delenv(k, raising=False)
    s = Settings(_env_file=None)
    assert s.DATABASE_URL == "sqlite:///./hades.db"
    assert s.HELIUS_MAX_PAGES_PER_ADDRESS == 20
    assert s.EARLY_BUYERS_N == 100
    assert s.SIG_SCAN_MAX_PAGES == 30
    assert s.RATE_LIMIT_RPS == 8
    assert s.HELIUS_WEBHOOK_ID == ""
    assert s.PUBLIC_WEBHOOK_URL == ""


def test_settings_env_override(monkeypatch):
    monkeypatch.setenv("EARLY_BUYERS_N", "50")
    monkeypatch.setenv("HADES_API_KEY", "k")
    s = Settings(_env_file=None)
    assert s.EARLY_BUYERS_N == 50
    assert s.HADES_API_KEY == "k"


def test_get_settings_cached():
    assert get_settings() is get_settings()


def test_init_db_idempotent(engine):
    init_db(engine)


def test_datetimes_roundtrip_utc_aware(engine):
    with session_scope(engine) as s:
        s.add(Watchlist(address="W1", note=None, added_at=NOW))
    with session_scope(engine) as s:
        got = s.get(Watchlist, "W1").added_at
    assert got.tzinfo is not None
    assert got == NOW

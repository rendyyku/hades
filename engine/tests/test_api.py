from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from hades.api.main import create_app
from hades.config import Settings
from hades.db import Alert, Swap, Token, Wallet, WalletStats, WalletTag, Watchlist

KEY = "k"
H = {"X-Hades-Key": KEY}

# valid base58, 32-44 chars
W_A = "A" * 32 + "a1"
W_B = "B" * 32 + "b2"
W_C = "C" * 32 + "c3"
W_BOT = "D" * 32 + "d4"
W_NOSTAT = "E" * 32 + "e5"
FUNDER1 = "F" * 32 + "f6"
FUNDER2 = "G" * 32 + "g7"
FUNDER3 = "H" * 32 + "h8"
FUNDER4 = "J" * 32 + "j9"
MINT = "M" * 32 + "pump"
MINT2 = "N" * 32 + "pump"

T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)


def _settings(key: str = KEY) -> Settings:
    return Settings(DATABASE_URL="sqlite://", HADES_API_KEY=key, _env_file=None)


def _swap(sig, wallet, mint, slot, minutes, side, tokens, sol):
    return Swap(signature=sig, wallet=wallet, mint=mint, slot=slot,
                block_time=T0 + timedelta(minutes=minutes), side=side,
                token_amount=tokens, sol_amount=sol, source="backfill", dex="pump")


def _seed(engine) -> None:
    with Session(engine) as s:
        s.add_all([
            Token(mint=MINT, symbol="AAA", name="Alpha", deployer=FUNDER4,
                  pool_created_slot=100, is_runner=True, peak_mcap_usd=1e6),
            Token(mint=MINT2, symbol=None, is_runner=False),
            Wallet(address=W_A, first_funder=FUNDER1, funding_signature="fsA", funding_slot=50),
            Wallet(address=FUNDER1, first_funder=FUNDER2, funding_signature="fs1", funding_slot=40),
            Wallet(address=FUNDER2, first_funder=FUNDER3, funding_signature="fs2", funding_slot=30),
            Wallet(address=FUNDER3, first_funder=FUNDER4, funding_signature="fs3", funding_slot=20),
            Wallet(address=W_B),
            Wallet(address=W_C),
            Wallet(address=W_BOT, is_bot=True),
            WalletStats(address=W_A, score=0.9, total_pnl_sol=5.0, realized_pnl_sol=5.0,
                        win_rate=0.5, tokens_closed=2, tokens_traded=2),
            WalletStats(address=W_B, score=0.5, total_pnl_sol=10.0, realized_pnl_sol=10.0,
                        win_rate=0.8, tokens_closed=1, tokens_traded=1),
            WalletStats(address=W_C, score=None, total_pnl_sol=1.0, realized_pnl_sol=1.0,
                        win_rate=None, tokens_closed=0, tokens_traded=1),
            WalletStats(address=W_BOT, score=0.99, total_pnl_sol=100.0, realized_pnl_sol=100.0,
                        win_rate=0.9, tokens_closed=5, tokens_traded=5),
            # W_A: MINT buy 1 SOL / sell 3 SOL -> +2 ; MINT2 buy 2 / sell 1 -> -1
            _swap("s1", W_A, MINT, 101, 0, "buy", 1000, 1.0),
            _swap("s2", W_A, MINT, 110, 10, "sell", 1000, 3.0),
            _swap("s3", W_A, MINT2, 120, 20, "buy", 500, 2.0),
            _swap("s4", W_A, MINT2, 130, 30, "sell", 500, 1.0),
            _swap("s5", W_B, MINT, 101, 0, "buy", 2000, 2.0),
            _swap("s6", W_C, MINT, 102, 1, "buy", 100, 0.1),
            _swap("s7", W_A, MINT, 103, 2, "buy", 10, 0.01),  # repeat buyer, not unique
            _swap("s8", W_NOSTAT, MINT2, 140, 40, "buy", 5, 0.5),
            WalletTag(address=W_A, tag="sniper", mint=MINT, evidence_json={"signatures": ["s1"]}),
            WalletTag(address=W_A, tag="bundle", mint=MINT,
                      evidence_json={"cluster": "c1", "funder": FUNDER1, "signatures": ["s1", "fsA"]}),
            WalletTag(address=W_B, tag="bundle", mint=MINT,
                      evidence_json={"cluster": "c1", "funder": FUNDER1, "signatures": ["s5", "x"]}),
            WalletTag(address=W_A, tag="repeat_early", mint=None, evidence_json={}),
            WalletTag(address=W_A, tag="sniper", mint=MINT2, evidence_json={}),
            WalletTag(address=W_C, tag="deployer_link", mint=MINT2, evidence_json={}),
            Alert(wallet=W_A, mint=MINT, side="buy", signature="a1",
                  created_at=datetime.now(timezone.utc) - timedelta(hours=1)),
            Alert(wallet=W_A, mint=MINT, side="buy", signature="a2",
                  created_at=datetime.now(timezone.utc) - timedelta(hours=30)),
        ])
        s.commit()


@pytest.fixture
def client():
    app = create_app(_settings())
    _seed(app.state.engine)
    with TestClient(app) as c:
        yield c
    app.state.engine.dispose()


# -- auth ----------------------------------------------------------------------

def test_requires_key(client):
    r = client.get("/api/overview")
    assert r.status_code == 401


def test_wrong_key_401(client):
    assert client.get("/api/wallets", headers={"X-Hades-Key": "nope"}).status_code == 401


def test_empty_key_503():
    app = create_app(_settings(key=""))
    with TestClient(app) as c:
        for h in ({}, {"X-Hades-Key": ""}, {"X-Hades-Key": "k"}):
            r = c.get("/api/overview", headers=h)
            assert r.status_code == 503
            assert r.json() == {"detail": "api key not configured"}
        assert c.get("/healthz").status_code == 200


def test_healthz_no_auth(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


# -- overview / wallets -----------------------------------------------------------

def test_overview(client):
    r = client.get("/api/overview", headers=H)
    assert r.status_code == 200
    d = r.json()
    assert d["tracked_wallets"] == 4
    assert d["runner_tokens"] == 1
    assert d["alerts_24h"] == 1
    assert d["last_swap_at"].startswith("2026-11-01T00:40:00")
    assert [w["address"] for w in d["top"]] == [W_A, W_B]


def test_wallets_sorted_and_hide_bots(client):
    r = client.get("/api/wallets", headers=H)
    assert r.status_code == 200
    d = r.json()
    assert d["total"] == 3
    assert [w["address"] for w in d["items"]] == [W_A, W_B, W_C]  # NULL score last
    a = d["items"][0]
    assert a == {
        "address": W_A, "score": 0.9, "total_pnl_sol": 5.0, "realized_pnl_sol": 5.0,
        "win_rate": 0.5, "tokens_closed": 2,
        "tags": ["bundle", "repeat_early", "sniper"], "is_bot": False,
    }

    d = client.get("/api/wallets?sort=total_pnl_sol", headers=H).json()
    assert [w["address"] for w in d["items"]] == [W_B, W_A, W_C]
    d = client.get("/api/wallets?sort=win_rate", headers=H).json()
    assert [w["address"] for w in d["items"]] == [W_B, W_A, W_C]

    d = client.get("/api/wallets?hide_bots=false", headers=H).json()
    assert d["total"] == 4
    assert d["items"][0]["address"] == W_BOT and d["items"][0]["is_bot"] is True

    d = client.get("/api/wallets?limit=1&offset=1", headers=H).json()
    assert d["total"] == 3
    assert [w["address"] for w in d["items"]] == [W_B]


def test_invalid_sort_422(client):
    assert client.get("/api/wallets?sort=address", headers=H).status_code == 422
    assert client.get("/api/wallets?limit=0", headers=H).status_code == 422
    assert client.get("/api/wallets?limit=201", headers=H).status_code == 422
    assert client.get("/api/wallets?offset=-1", headers=H).status_code == 422


def test_tag_filter(client):
    d = client.get("/api/wallets?tag=bundle", headers=H).json()
    assert d["total"] == 2
    assert [w["address"] for w in d["items"]] == [W_A, W_B]
    d = client.get("/api/wallets?tag=deployer_link", headers=H).json()
    assert [w["address"] for w in d["items"]] == [W_C]
    d = client.get("/api/wallets?tag=nothing", headers=H).json()
    assert d == {"items": [], "total": 0}


# -- wallet detail ----------------------------------------------------------------

def test_wallet_404(client):
    r = client.get("/api/wallets/" + "Z" * 40, headers=H)
    assert r.status_code == 404


def test_wallet_with_swaps_but_no_row(client):
    r = client.get("/api/wallets/" + W_NOSTAT, headers=H)
    assert r.status_code == 200
    d = r.json()
    assert d["stats"] is None
    assert d["funding_chain"] == [{"address": W_NOSTAT, "funding_signature": None, "funding_slot": None}]
    assert d["positions"][0]["closed"] is False
    assert d["pnl_curve"] == []


def test_wallet_detail_positions_and_curve(client):
    r = client.get("/api/wallets/" + W_A, headers=H)
    assert r.status_code == 200
    d = r.json()
    assert d["stats"]["address"] == W_A
    assert d["stats"]["score"] == 0.9
    pos = d["positions"]
    assert [p["mint"] for p in pos] == [MINT2, MINT]  # last activity desc
    p2, p1 = pos
    assert p2["symbol"] is None
    assert p2["realized_pnl_sol"] == pytest.approx(-1.0)
    assert p2["closed"] is True
    assert p1["symbol"] == "AAA"
    assert p1["bought_tokens"] == pytest.approx(1010)
    assert p1["cost_sol"] == pytest.approx(1.01)
    assert p1["proceeds_sol"] == pytest.approx(3.0)
    # avg cost 1.01/1010 per token, 1000 sold -> cost_out 1.0 -> +2.0
    assert p1["realized_pnl_sol"] == pytest.approx(2.0)
    assert p1["remaining_tokens"] == pytest.approx(10)
    assert p1["closed"] is True
    assert p1["first_buy_time"].startswith("2026-11-01T00:00:00")
    assert set(p1) == {"mint", "symbol", "bought_tokens", "sold_tokens", "cost_sol",
                       "proceeds_sol", "realized_pnl_sol", "remaining_tokens",
                       "untracked_sell_tokens", "closed", "first_buy_time", "last_sell_time"}

    curve = d["pnl_curve"]
    assert [c["t"][:19] for c in curve] == ["2026-11-01T00:10:00", "2026-11-01T00:30:00"]
    assert [c["cum_pnl_sol"] for c in curve] == [pytest.approx(2.0), pytest.approx(1.0)]

    tags = d["tags"]
    assert len(tags) == 4
    assert {(t["tag"], t["mint"]) for t in tags} == {
        ("sniper", MINT), ("bundle", MINT), ("repeat_early", None), ("sniper", MINT2)}
    b = next(t for t in tags if t["tag"] == "bundle")
    assert b["evidence"]["cluster"] == "c1"
    assert b["created_at"]


def test_funding_chain_from_db(client):
    d = client.get("/api/wallets/" + W_A, headers=H).json()
    assert d["funding_chain"] == [
        {"address": W_A, "funding_signature": "fsA", "funding_slot": 50},
        {"address": FUNDER1, "funding_signature": "fs1", "funding_slot": 40},
        {"address": FUNDER2, "funding_signature": "fs2", "funding_slot": 30},
        {"address": FUNDER3, "funding_signature": "fs3", "funding_slot": 20},
    ]  # 3 hops max: FUNDER4 is not reached


# -- token forensics --------------------------------------------------------------

def test_token_forensics_shape(client):
    r = client.get("/api/tokens/" + MINT, headers=H)
    assert r.status_code == 200
    d = r.json()
    assert d["token"]["mint"] == MINT
    assert d["token"]["symbol"] == "AAA"
    assert d["token"]["is_runner"] is True
    eb = d["early_buyers"]
    assert [b["address"] for b in eb] == [W_A, W_B, W_C]  # (slot, signature), unique
    assert eb[0] == {"address": W_A, "slot": 101, "signature": "s1", "sol_amount": 1.0,
                     "tags": ["bundle", "repeat_early", "sniper"],
                     "funder": FUNDER1, "funder_is_hub": False}
    assert eb[2]["tags"] == []  # W_C's tag is for MINT2
    assert d["bundles"] == [{"cluster": "c1", "funder": FUNDER1, "wallets": [W_A, W_B]}]

    assert client.get("/api/tokens/" + "Q" * 40, headers=H).status_code == 404


# -- watchlist --------------------------------------------------------------------

def test_watchlist_validation_422(client):
    for bad in ("short", "0" * 40, "O" * 40, "l" * 40, "I" * 40, "A" * 45, ""):
        r = client.post("/api/watchlist", json={"address": bad}, headers=H)
        assert r.status_code == 422, bad
    assert client.post("/api/watchlist", json={}, headers=H).status_code == 422


def test_watchlist_crud(client):
    assert client.get("/api/watchlist", headers=H).json() == []
    r = client.post("/api/watchlist", json={"address": W_A, "note": "alpha"}, headers=H)
    assert r.status_code == 201
    row = r.json()
    assert row["address"] == W_A and row["note"] == "alpha" and row["added_at"]

    r = client.post("/api/watchlist", json={"address": W_A, "note": "beta"}, headers=H)
    assert r.status_code == 201
    assert r.json()["note"] == "beta"
    assert r.json()["added_at"] == row["added_at"]
    r = client.post("/api/watchlist", json={"address": W_B}, headers=H)
    assert r.status_code == 201 and r.json()["note"] is None

    lst = client.get("/api/watchlist", headers=H).json()
    assert {w["address"]: w["note"] for w in lst} == {W_A: "beta", W_B: None}

    assert client.delete("/api/watchlist/" + W_A, headers=H).status_code == 204
    assert client.delete("/api/watchlist/" + W_A, headers=H).status_code == 404
    assert [w["address"] for w in client.get("/api/watchlist", headers=H).json()] == [W_B]
    assert client.delete("/api/watchlist/" + W_B).status_code == 401


# -- alerts (Task 12) ------------------------------------------------------------

def test_alerts_requires_key(client):
    assert client.get("/api/alerts").status_code == 401
    assert client.get("/api/alerts", headers={"X-Hades-Key": "nope"}).status_code == 401


def test_alerts_newest_first_with_symbol(client):
    with Session(client.app.state.engine) as s:
        s.add(Alert(wallet=W_B, mint=MINT2, side="sell", sol_amount=1.5, signature="a3",
                    block_time=T0))
        s.commit()
    r = client.get("/api/alerts", headers=H)
    assert r.status_code == 200
    rows = r.json()
    assert [a["signature"] for a in rows] == ["a3", "a2", "a1"]
    ids = [a["id"] for a in rows]
    assert ids == sorted(ids, reverse=True)
    assert set(rows[0]) == {"id", "wallet", "mint", "symbol", "side", "sol_amount", "signature", "block_time"}
    assert rows[0]["symbol"] is None  # MINT2 has no symbol
    assert rows[0]["sol_amount"] == 1.5 and rows[0]["side"] == "sell"
    assert rows[1]["symbol"] == "AAA"  # joined from tokens
    assert rows[1]["wallet"] == W_A and rows[1]["mint"] == MINT


def test_alerts_limit(client):
    r = client.get("/api/alerts?limit=1", headers=H)
    assert r.status_code == 200
    assert [a["signature"] for a in r.json()] == ["a2"]
    assert client.get("/api/alerts?limit=200", headers=H).status_code == 200


@pytest.mark.parametrize("bad", ["0", "201", "-1", "x"])
def test_alerts_limit_bounds_422(client, bad):
    assert client.get(f"/api/alerts?limit={bad}", headers=H).status_code == 422


def test_alerts_unknown_mint_symbol_null(client):
    with Session(client.app.state.engine) as s:
        s.add(Alert(wallet=W_C, mint="Z" * 32 + "pump", side="buy", signature="a9"))
        s.commit()
    rows = client.get("/api/alerts?limit=1", headers=H).json()
    assert rows[0]["signature"] == "a9" and rows[0]["symbol"] is None


# -- token forensics: funders (Task 12b) ------------------------------------------

HUB = "K" * 32 + "k1"
DEP_FUNDER = "L" * 32 + "l2"


def _count_queries(engine):
    from sqlalchemy import event

    stmts: list[str] = []

    def _on(conn, cursor, statement, params, context, executemany):
        stmts.append(statement)

    event.listen(engine, "before_cursor_execute", _on)
    return stmts, lambda: event.remove(engine, "before_cursor_execute", _on)


def test_token_buyer_funder_present_and_null(client):
    d = client.get("/api/tokens/" + MINT, headers=H).json()
    eb = {b["address"]: b for b in d["early_buyers"]}
    assert eb[W_A]["funder"] == FUNDER1
    assert eb[W_A]["funder_is_hub"] is False
    assert eb[W_B]["funder"] is None  # wallet row, no first_funder
    assert eb[W_B]["funder_is_hub"] is False
    assert d["token"]["deployer_funder"] is None  # FUNDER4 has no wallet row


def test_token_buyer_hub_funder_and_deployer_funder(client):
    engine = client.app.state.engine
    with Session(engine) as s:
        s.add(Wallet(address=HUB, label="hub"))
        s.add(Wallet(address=FUNDER4, first_funder=DEP_FUNDER))
        s.get(Wallet, W_B).first_funder = HUB
        s.commit()
    d = client.get("/api/tokens/" + MINT, headers=H).json()
    eb = {b["address"]: b for b in d["early_buyers"]}
    assert eb[W_B]["funder"] == HUB
    assert eb[W_B]["funder_is_hub"] is True
    assert eb[W_A]["funder_is_hub"] is False
    assert d["token"]["deployer_funder"] == DEP_FUNDER


def test_token_buyer_without_wallet_row_has_null_funder(client):
    engine = client.app.state.engine
    W_X = "X" * 32 + "x1"
    with Session(engine) as s:
        s.add(_swap("sx", W_X, MINT, 105, 3, "buy", 10, 0.2))
        s.commit()
    d = client.get("/api/tokens/" + MINT, headers=H).json()
    b = next(b for b in d["early_buyers"] if b["address"] == W_X)
    assert b["funder"] is None and b["funder_is_hub"] is False


def test_token_funders_no_n_plus_one(client):
    engine = client.app.state.engine
    with Session(engine) as s:
        for i in range(20):
            addr = f"{i:02d}".replace("0", "Z") + "W" * 32
            s.add(Wallet(address=addr, first_funder=FUNDER1))
            s.add(_swap(f"n{i}", addr, MINT, 200 + i, 5, "buy", 1, 0.1))
        s.commit()
    stmts, stop = _count_queries(engine)
    try:
        small = client.get("/api/tokens/" + MINT2, headers=H)
        n_small = len(stmts)
        stmts.clear()
        big = client.get("/api/tokens/" + MINT, headers=H)
        n_big = len(stmts)
    finally:
        stop()
    assert small.status_code == big.status_code == 200
    assert len(big.json()["early_buyers"]) == 23
    # Query count is independent of the number of buyers (no per-buyer lookups).
    assert n_big <= n_small
    assert n_big <= 8

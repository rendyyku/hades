"""Insider detection tests (snipers, bundles, deployer links, repeat early buyers)."""

from datetime import datetime, timezone

from sqlalchemy import func, select

from hades import insider
from hades.db import Swap, Token, Wallet, WalletTag
from hades.insider import (
    Tag,
    detect_bundles,
    detect_deployer_link,
    detect_snipers,
    is_hub,
    persist_tags,
    tag_repeat_early,
    tag_token,
    trace_funder,
    trace_funder_detailed,
)

MINT = "MeMeMint1111111111111111111111111111111pump"
DEPLOYER = "DePLoyer1111111111111111111111111111111111"
T0 = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)


class FakeHelius:
    """funders: address -> (funder, sig, slot); hubs: addresses with >= 3000 sigs."""

    def __init__(self, funders=None, hubs=()):
        self.funders = dict(funders or {})
        self.hubs = set(hubs)
        self.fund_calls: list[str] = []
        self.sig_calls: list[str] = []

    async def first_incoming_sol(self, address, max_pages=5):
        self.fund_calls.append(address)
        return self.funders.get(address)

    async def signatures_oldest(self, address, max_pages):
        self.sig_calls.append(address)
        n = insider.HUB_SIG_THRESHOLD if address in self.hubs else 1
        return [f"{address}-s{i}" for i in range(n)]


def buy(sig, wallet, slot, mint=MINT, side="buy"):
    return Swap(
        signature=sig,
        wallet=wallet,
        mint=mint,
        slot=slot,
        block_time=T0,
        side=side,
        token_amount=1000.0,
        sol_amount=1.0,
        source="backfill",
    )


def token(**kw):
    base = dict(mint=MINT, deployer=DEPLOYER, pool_created_slot=1000, is_runner=False)
    base.update(kw)
    return Token(**base)


def assert_evidence(tag: Tag):
    ev = tag.evidence
    assert isinstance(ev["signatures"], list) and isinstance(ev["slots"], list)
    assert ev["signatures"], tag
    assert len(ev["signatures"]) == len(ev["slots"])
    assert all(isinstance(s, str) and s for s in ev["signatures"])
    assert all(isinstance(s, int) and s >= 0 for s in ev["slots"])


# -- snipers -------------------------------------------------------------------


def test_sniper_within_2_slots():
    tok = token()
    swaps = [
        buy("a1", "A", 1001),
        buy("a2", "A", 1002),
        buy("b1", "B", 1002),
        buy("c1", "C", 1003),
        buy("d1", "D", 999),
        buy("e1", "E", 1000, side="sell"),
        buy("f1", "F", 1000, mint="OTHER"),
    ]
    tags = detect_snipers(tok, swaps)
    by = {t.address: t for t in tags}
    assert set(by) == {"A", "B"}
    assert by["A"].tag == "sniper" and by["A"].mint == MINT
    assert by["A"].evidence == {"signatures": ["a1"], "slots": [1001], "delta_slots": 1}
    assert by["B"].evidence["delta_slots"] == 2
    assert detect_snipers(token(pool_created_slot=None), swaps) == []


# -- bundles -------------------------------------------------------------------


def test_bundle_needs_3_same_funder():
    tok = token()
    swaps = [
        # slot 500: 3 wallets, same funder F -> bundle
        buy("w1", "W1", 500), buy("w2", "W2", 500), buy("w3", "W3", 500),
        # slot 600: only 2 wallets with funder G -> none
        buy("w4", "W4", 600), buy("w5", "W5", 600),
        # slot 700: 3 wallets, different funders -> none
        buy("w6", "W6", 700), buy("w7", "W7", 700), buy("w8", "W8", 700),
        # slot 800: 3 buys but only 2 distinct wallets -> none
        buy("w9a", "W9", 800), buy("w9b", "W9", 800), buy("w10", "W10", 800),
    ]
    funders = {
        "W1": "FunderF111", "W2": "FunderF111", "W3": "FunderF111",
        "W4": "FunderG111", "W5": "FunderG111",
        "W6": "X1", "W7": "X2", "W8": "X3",
        "W9": "FunderH111", "W10": "FunderH111",
    }
    funding = {w: (f"fund-{w}", 100 + i) for i, w in enumerate(funders)}
    tags = detect_bundles(tok, swaps, funders, set(), funding=funding)
    assert sorted(t.address for t in tags) == ["W1", "W2", "W3"]
    clusters = {t.evidence["cluster"] for t in tags}
    assert clusters == {f"{MINT[:6]}-500-Funder"}
    for t in tags:
        assert t.tag == "bundle" and t.mint == MINT
        assert t.evidence["funder"] == "FunderF111"
        # buy sig/slot + funder->wallet funding sig/slot
        sig, fslot = funding[t.address]
        assert t.evidence["signatures"] == [t.address.lower(), sig]
        assert t.evidence["slots"] == [500, fslot]
        assert_evidence(t)


def test_bundle_excludes_wallet_without_funding_sig():
    tok = token()
    swaps = [buy("w1", "W1", 500), buy("w2", "W2", 500), buy("w3", "W3", 500)]
    funders = {"W1": "FunderF111", "W2": "FunderF111", "W3": "FunderF111"}
    # W3's funding transfer is unknown -> only 2 provable wallets -> no bundle
    funding = {"W1": ("fw1", 1), "W2": ("fw2", 2)}
    assert detect_bundles(tok, swaps, funders, set(), funding=funding) == []
    assert detect_bundles(tok, swaps, funders, set()) == []


# -- deployer link -------------------------------------------------------------


async def test_deployer_link_two_hops(session):
    tok = token()
    helius = FakeHelius(
        {
            "buyer": ("X", "sigB", 10),
            "X": (DEPLOYER, "sigX", 5),
            "other": ("Y", "sigO", 11),
            "Y": ("Z", "sigY", 4),
            "buyer2": ("S", "sigB2", 12),
            DEPLOYER: ("S", "sigD", 3),
        }
    )
    tags = await detect_deployer_link(session, helius, tok, ["buyer", "other", "buyer2"])
    by = {t.address: t for t in tags}
    assert set(by) == {"buyer", "buyer2"}
    assert by["buyer"].tag == "deployer_link"
    assert by["buyer"].evidence["chain"] == ["buyer", "X", DEPLOYER]
    assert by["buyer"].evidence["signatures"] == ["sigB", "sigX"]
    assert by["buyer"].evidence["slots"] == [10, 5]
    # shared (non-hub) funder S: buyer2 <- S -> deployer
    assert by["buyer2"].evidence["chain"] == ["buyer2", "S", DEPLOYER]
    assert by["buyer2"].evidence["signatures"] == ["sigB2", "sigD"]
    assert by["buyer2"].evidence["slots"] == [12, 3]
    assert await detect_deployer_link(session, helius, token(deployer=None), ["buyer"]) == []


async def test_deployer_self_buy_tagged(session):
    tok = token()
    helius = FakeHelius()
    swaps = [buy("d2", DEPLOYER, 1005), buy("d1", DEPLOYER, 1003)]
    tags = await detect_deployer_link(session, helius, tok, [DEPLOYER], swaps=swaps)
    assert len(tags) == 1
    assert tags[0].evidence == {"signatures": ["d1"], "slots": [1003], "chain": [DEPLOYER]}


async def test_deployer_self_buy_skipped_without_sig(session):
    helius = FakeHelius()
    assert await detect_deployer_link(session, helius, token(), [DEPLOYER]) == []
    other = [buy("x1", "Someone", 1003)]
    assert await detect_deployer_link(session, helius, token(), [DEPLOYER], swaps=other) == []


# -- repeat early --------------------------------------------------------------


def test_repeat_early_3_runners(session):
    runners = ["R1", "R2", "R3"]
    for m in runners:
        session.add(Token(mint=m, is_runner=True))
    session.add(Token(mint="N1", is_runner=False))
    # W early in all 3 runners; V early in only 2; L is 51st in R1.
    for i, m in enumerate(runners):
        session.add(buy(f"{m}-w", "W", 10 + i, mint=m))
    session.add(buy("R1-v", "V", 20, mint="R1"))
    session.add(buy("R2-v", "V", 20, mint="R2"))
    session.add(buy("N1-v", "V", 20, mint="N1"))  # non-runner doesn't count
    for j in range(49):
        session.add(buy(f"R1-f{j}", f"F{j}", 30 + j, mint="R1"))
    session.add(buy("R1-l", "L", 500, mint="R1"))  # 52nd unique buyer in R1
    session.add(buy("R2-l", "L", 500, mint="R2"))
    session.add(buy("R3-l", "L", 500, mint="R3"))
    session.add(buy("R1-w-again", "W", 900, mint="R1"))
    session.add(buy("R1-v-sell", "V", 1, mint="R1", side="sell"))
    session.commit()

    tags = tag_repeat_early(session)
    assert [t.address for t in tags] == ["W"]
    t = tags[0]
    assert t.tag == "repeat_early" and t.mint is None
    assert t.evidence["mints"] == ["R1", "R2", "R3"]
    assert t.evidence["signatures"] == ["R1-w", "R2-w", "R3-w"]
    assert t.evidence["slots"] == [10, 11, 12]
    assert session.scalar(select(func.count()).select_from(WalletTag)) == 0


# -- evidence ------------------------------------------------------------------


async def test_evidence_has_signatures(session):
    tok = token()
    swaps = [
        buy("s1", "S1", 1001),
        buy("b1", "B1", 1010), buy("b2", "B2", 1010), buy("b3", "B3", 1010),
        buy("l1", "L", 1020),
        buy("d1", DEPLOYER, 1005),
    ]
    helius = FakeHelius({"L": ("X", "sigL", 7), "X": (DEPLOYER, "sigX", 6)})
    tags = detect_snipers(tok, swaps)
    tags += detect_bundles(
        tok, swaps, {"B1": "F", "B2": "F", "B3": "F"}, set(),
        funding={"B1": ("fb1", 1), "B2": ("fb2", 2), "B3": ("fb3", 3)},
    )
    tags += await detect_deployer_link(session, helius, tok, ["L", DEPLOYER], swaps=swaps)
    for m in ("R1", "R2", "R3"):
        session.add(Token(mint=m, is_runner=True))
        session.add(buy(f"{m}-w", "W", 1, mint=m))
    session.flush()
    tags += tag_repeat_early(session)
    assert {t.tag for t in tags} == {"sniper", "bundle", "deployer_link", "repeat_early"}
    for t in tags:
        assert_evidence(t)
    for t in tags:
        if t.tag == "deployer_link":
            assert t.evidence["chain"]
        if t.tag == "bundle":
            assert t.evidence["cluster"] and t.evidence["funder"]


# -- hubs ----------------------------------------------------------------------


async def test_hub_funder_not_bundle_or_link(session):
    tok = token()
    session.add(tok)
    for i, w in enumerate(["B1", "B2", "B3"]):
        session.add(buy(f"b{i}", w, 1010))
    session.add(buy("l1", "L", 1020))
    session.commit()
    hub = "HubExchange1111"
    helius = FakeHelius(
        {
            "B1": (hub, "f1", 1), "B2": (hub, "f2", 1), "B3": (hub, "f3", 1),
            "L": (hub, "f4", 1), DEPLOYER: (hub, "f5", 1),
            hub: ("Upstream", "fh", 0),
        },
        hubs={hub},
    )
    tags = await tag_token(session, helius, tok)
    assert [t for t in tags if t.tag in ("bundle", "deployer_link")] == []
    assert session.get(Wallet, hub).label == "hub"
    # tracing stops at the hub: its own funder is never looked up
    assert hub not in helius.fund_calls
    # hub status is now cached: no further signature scans for it
    n = helius.sig_calls.count(hub)
    assert await is_hub(session, helius, hub) is True
    assert helius.sig_calls.count(hub) == n


# -- persistence ---------------------------------------------------------------


def test_persist_tags_idempotent(session):
    tags = [
        Tag("A", "sniper", MINT, {"signatures": ["a1"], "slots": [1001], "delta_slots": 1}),
        Tag("W", "repeat_early", None, {"signatures": ["x"], "slots": [1], "mints": ["R1"]}),
    ]
    assert persist_tags(session, tags) == 2
    assert persist_tags(session, tags) == 2
    session.commit()
    rows = session.scalars(select(WalletTag)).all()
    assert len(rows) == 2
    updated = [Tag("W", "repeat_early", None, {"signatures": ["y"], "slots": [2], "mints": ["R2"]})]
    assert persist_tags(session, updated) == 1
    session.commit()
    rows = {(r.address, r.tag, r.mint): r for r in session.scalars(select(WalletTag))}
    assert len(rows) == 2
    assert rows[("W", "repeat_early", None)].evidence_json["signatures"] == ["y"]
    assert rows[("A", "sniper", MINT)].evidence_json["delta_slots"] == 1
    assert session.get(Wallet, "A") is not None and session.get(Wallet, "W") is not None


def test_persist_tags_refuses_bad_evidence(session, caplog):
    good = Tag("A", "sniper", MINT, {"signatures": ["a1"], "slots": [1001]})
    assert persist_tags(session, [good]) == 1
    session.commit()
    bad = [
        Tag("A", "sniper", MINT, {"signatures": [], "slots": []}),
        Tag("B", "deployer_link", MINT, {"signatures": ["x", ""], "slots": [1, 2]}),
        Tag("C", "bundle", MINT, {"signatures": ["x", "y"], "slots": [1]}),
        Tag("D", "sniper", MINT, {"slots": [1]}),
    ]
    ok = Tag("E", "sniper", MINT, {"signatures": ["e1"], "slots": [1002]})
    with caplog.at_level("WARNING", logger="hades.insider"):
        assert persist_tags(session, bad + [ok]) == 1
    session.commit()
    rows = {(r.address, r.tag): r for r in session.scalars(select(WalletTag))}
    assert set(rows) == {("A", "sniper"), ("E", "sniper")}
    # refused tag must not wipe the existing good row
    assert rows[("A", "sniper")].evidence_json["signatures"] == ["a1"]
    assert sum("refus" in r.getMessage() for r in caplog.records) == 4


# -- tracing -------------------------------------------------------------------


async def test_trace_uses_cache(session):
    session.add(Wallet(address="A", first_funder="B", funding_signature="sigA", funding_slot=9))
    session.commit()
    helius = FakeHelius({"A": ("WRONG", "x", 1), "B": ("C", "sigC", 3)})
    chain, sigs, slots = await trace_funder_detailed(session, helius, "A", hops=2)
    assert chain == ["A", "B", "C"]
    assert sigs == ["sigA", "sigC"]
    assert slots == [9, 3]
    assert helius.fund_calls == ["B"]
    b = session.get(Wallet, "B")
    assert (b.first_funder, b.funding_signature, b.funding_slot) == ("C", "sigC", 3)

    fresh = FakeHelius({"A": ("WRONG", "x", 1), "B": ("WRONG", "y", 1)})
    assert await trace_funder_detailed(session, fresh, "A", hops=2) == (
        ["A", "B", "C"], ["sigA", "sigC"], [9, 3]
    )
    assert fresh.fund_calls == []

    # no funder -> chain stops
    none = FakeHelius()
    assert await trace_funder(session, none, "Q", hops=2) == ["Q"]

    # label 'hub' short-circuits with no RPC
    session.add(Wallet(address="H", label="hub"))
    session.flush()
    assert await is_hub(session, none, "H") is True
    assert none.sig_calls == []
    # not-hub result cached in-process
    assert await is_hub(session, none, "N") is False
    assert await is_hub(session, none, "N") is False
    assert none.sig_calls == ["N"]


async def test_trace_refetches_legacy_row_without_sig(session):
    # legacy row: first_funder known but no funding sig/slot stored
    session.add(Wallet(address="A", first_funder="B"))
    session.commit()
    helius = FakeHelius({"A": ("B", "sigA", 4)})
    chain, sigs, slots = await trace_funder_detailed(session, helius, "A", hops=1)
    assert (chain, sigs, slots) == (["A", "B"], ["sigA"], [4])
    assert helius.fund_calls == ["A"]
    a = session.get(Wallet, "A")
    assert (a.first_funder, a.funding_signature, a.funding_slot) == ("B", "sigA", 4)

    # legacy row whose re-fetch finds nothing: no sentinel hop is returned
    session.add(Wallet(address="Q", first_funder="R"))
    session.commit()
    assert await trace_funder_detailed(session, FakeHelius(), "Q", hops=2) == (["Q"], [], [])


# -- end to end ----------------------------------------------------------------


async def test_tag_token_end_to_end(session):
    tok = token()
    session.add(tok)
    for s in [
        buy("s1", "S1", 1001),
        buy("d1", DEPLOYER, 1005),
        buy("b1", "B1", 1010), buy("b2", "B2", 1010), buy("b3", "B3", 1010),
        buy("l1", "L", 1020),
        buy("o1", "O", 1030),
        buy("s1-sell", "S1", 1040, side="sell"),
    ]:
        session.add(s)
    session.commit()
    helius = FakeHelius(
        {
            "B1": ("Fund", "f1", 900), "B2": ("Fund", "f2", 901), "B3": ("Fund", "f3", 902),
            "L": ("X", "sigL", 800), "X": (DEPLOYER, "sigX", 700),
            "O": ("Rand", "sigO", 600),
        }
    )
    tags = await tag_token(session, helius, tok)
    counts: dict[str, int] = {}
    for t in tags:
        counts[t.tag] = counts.get(t.tag, 0) + 1
        assert_evidence(t)
    assert counts == {"sniper": 1, "bundle": 3, "deployer_link": 2}
    link = {t.address: t for t in tags if t.tag == "deployer_link"}
    assert link["L"].evidence["chain"] == ["L", "X", DEPLOYER]
    assert link["L"].evidence["signatures"] == ["sigL", "sigX"]
    assert link[DEPLOYER].evidence["chain"] == [DEPLOYER]
    bundle = {t.address: t for t in tags if t.tag == "bundle"}
    assert bundle["B1"].evidence["signatures"] == ["b1", "f1"]
    assert bundle["B1"].evidence["slots"] == [1010, 900]
    assert session.scalar(select(func.count()).select_from(WalletTag)) == 6

    # rerun: idempotent, no duplicate rows
    await tag_token(session, helius, tok)
    assert session.scalar(select(func.count()).select_from(WalletTag)) == 6
    assert session.get(Wallet, "B1").first_funder == "Fund"


def _persisted(session):
    session.expire_all()
    return {
        (r.address, r.tag, r.mint): r.evidence_json
        for r in session.scalars(select(WalletTag).order_by(WalletTag.address, WalletTag.tag))
    }


async def test_tag_token_rerun_with_fresh_client_keeps_evidence(session):
    tok = token()
    session.add(tok)
    for s in [
        buy("d1", DEPLOYER, 1005),
        buy("b1", "B1", 1010), buy("b2", "B2", 1010), buy("b3", "B3", 1010),
        buy("l1", "L", 1020),
        buy("m1", "M", 1030),
    ]:
        session.add(s)
    session.commit()
    funders = {
        "B1": ("Fund", "f1", 900), "B2": ("Fund", "f2", 901), "B3": ("Fund", "f3", 902),
        "L": ("X", "sigL", 800), "X": (DEPLOYER, "sigX", 700),
        "M": ("S", "sigM", 650), DEPLOYER: ("S", "sigD", 640),
    }
    await tag_token(session, FakeHelius(funders), tok)
    first = _persisted(session)
    links = {k: v for k, v in first.items() if k[1] == "deployer_link"}
    assert set(k[0] for k in links) == {"L", "M", DEPLOYER}

    # new process: fresh client, same DB. Must not lose or blank any evidence.
    fresh = FakeHelius(funders)
    tags2 = await tag_token(session, fresh, tok)
    second = _persisted(session)
    assert second == first
    for t in tags2:
        assert_evidence(t)
    for k, ev in second.items():
        assert_evidence(Tag(k[0], k[1], k[2], ev))
    assert second[("L", "deployer_link", MINT)]["signatures"] == ["sigL", "sigX"]
    assert second[("M", "deployer_link", MINT)]["signatures"] == ["sigM", "sigD"]
    assert second[("B1", "bundle", MINT)]["signatures"] == ["b1", "f1"]
    # every funded hop came from the DB: no re-fetch of known funders
    assert not set(fresh.fund_calls) & set(funders)


# -- stale tags & persisted-only return (Task 7b, R20a / W4) -------------------


def _store(session, address, tag, mint, sigs, slots):
    session.add(Wallet(address=address)) if session.get(Wallet, address) is None else None
    session.add(WalletTag(address=address, tag=tag, mint=mint,
                          evidence_json={"signatures": sigs, "slots": slots}))


async def test_tag_token_removes_stale_tags(session):
    tok = token()
    session.add(tok)
    session.add(buy("s1", "S1", 1001))
    _store(session, "Stale", "sniper", MINT, ["old1"], [1000])
    _store(session, "Stale", "bundle", MINT, ["old2", "old3"], [1010, 900])
    _store(session, "Stale", "deployer_link", MINT, ["old4"], [800])
    _store(session, "Stale", "sniper", "OtherMint", ["x1"], [5])  # other token: kept
    _store(session, "Stale", "repeat_early", None, ["r1"], [7])  # global tag: kept
    _store(session, "Stale", "bot", MINT, ["b1"], [7])  # not a per-token insider tag: kept
    session.commit()

    tags = await tag_token(session, FakeHelius(), tok)

    assert [(t.address, t.tag) for t in tags] == [("S1", "sniper")]
    assert set(_persisted(session)) == {
        ("S1", "sniper", MINT),
        ("Stale", "sniper", "OtherMint"),
        ("Stale", "repeat_early", None),
        ("Stale", "bot", MINT),
    }


async def test_tag_token_keeps_old_row_when_new_refused(session, monkeypatch):
    tok = token()
    session.add(tok)
    session.add(buy("s1", "S1", 1001))
    _store(session, "S1", "sniper", MINT, ["good-old"], [1001])
    session.commit()
    monkeypatch.setattr(
        insider, "detect_snipers",
        lambda token, swaps: [Tag("S1", "sniper", MINT, {"signatures": [], "slots": []})],
    )

    tags = await tag_token(session, FakeHelius(), tok)

    assert tags == []
    assert _persisted(session) == {
        ("S1", "sniper", MINT): {"signatures": ["good-old"], "slots": [1001]},
    }


async def test_tag_token_returns_only_persisted(session, monkeypatch):
    tok = token()
    session.add(tok)
    session.add(buy("s1", "S1", 1001))
    session.commit()
    good = Tag("S1", "sniper", MINT, {"signatures": ["s1"], "slots": [1001], "delta_slots": 1})
    bad = Tag("S2", "sniper", MINT, {"signatures": ["s2"], "slots": []})
    monkeypatch.setattr(insider, "detect_snipers", lambda token, swaps: [good, bad])

    tags = await tag_token(session, FakeHelius(), tok)

    assert tags == [good]
    assert set(_persisted(session)) == {("S1", "sniper", MINT)}

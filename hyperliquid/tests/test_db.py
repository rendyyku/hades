from datetime import datetime, timezone

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError

from hades_hl.db import (
    Alert,
    Fill,
    Funding,
    Kv,
    PaperEvent,
    Wallet,
    fill_row,
    init_db,
    insert_fills,
    make_engine,
    session_scope,
)

EXPECTED = {
    "wallets": {"address", "first_seen", "is_vault", "is_bot", "score", "stats_json", "scored_at"},
    "fills": {"tid", "address", "coin", "px", "sz", "side", "dir", "start_position", "closed_pnl",
              "fee", "time", "hash", "crossed", "liquidation", "liquidated_user", "builder_fee"},
    "funding": {"address", "coin", "time", "usdc"},
    "tags": {"address", "tag", "evidence_json", "created_at"},
    "follows": {"address", "origin", "started_at", "removed_at", "removed_manually", "ladder",
                "ladder_changed_at"},
    "ladder_history": {"id", "address", "from_", "to", "gate_json", "at"},
    "prices": {"coin", "time", "mid"},
    "paper_events": {"source_ref", "address", "json", "observed_time", "delay_ms"},
    "alerts": {"id", "kind", "address", "coin", "source_ref", "payload_json", "created_at"},
    "kv": {"key", "value"},
}

ADDR = "0x" + "cd" * 20


@pytest.fixture
def engine():
    e = make_engine("sqlite://")
    init_db(e)
    return e


def test_schema_tables_and_columns(engine):
    insp = inspect(engine)
    assert set(insp.get_table_names()) >= set(EXPECTED)
    for table, cols in EXPECTED.items():
        assert {c["name"] for c in insp.get_columns(table)} >= cols, table
    assert {c for c in insp.get_pk_constraint("funding")["constrained_columns"]} == {"address", "coin", "time"}
    assert set(insp.get_pk_constraint("tags")["constrained_columns"]) == {"address", "tag"}
    assert set(insp.get_pk_constraint("prices")["constrained_columns"]) == {"coin", "time"}


def test_init_db_idempotent(engine):
    init_db(engine)
    init_db(engine)


def test_duplicate_tid_insert_is_ignored(engine, fixture):
    rows = fixture("user_fills_by_time.json")[:50]
    with session_scope(engine) as s:
        assert insert_fills(s, ADDR, rows) == 50
    with session_scope(engine) as s:
        # same tids again (plus one new): only the new one lands, no IntegrityError
        new = dict(rows[0], tid=rows[0]["tid"] + 10**12)
        assert insert_fills(s, ADDR, rows + [new]) == 1
    with session_scope(engine) as s:
        assert s.scalar(select(text("count(*)")).select_from(Fill)) == 51


def test_duplicate_tid_within_one_batch_ignored(engine, fixture):
    row = fixture("user_fills_by_time.json")[0]
    with session_scope(engine) as s:
        assert insert_fills(s, ADDR, [row, row]) == 1


def test_fill_row_maps_hl_fields_with_decimals(fixture):
    raw = fixture("user_fills_by_time.json")[0]
    r = fill_row(ADDR, raw)
    assert r["tid"] == raw["tid"] and r["address"] == ADDR
    assert r["start_position"] == raw["startPosition"]
    assert r["closed_pnl"] == raw["closedPnl"]
    assert r["liquidation"] is False
    liq = fill_row(ADDR, dict(raw, liquidation={"liquidatedUser": ADDR, "markPx": "1", "method": "market"}))
    assert liq["liquidation"] is True


def test_decimal_round_trip(engine, fixture):
    from decimal import Decimal

    raw = fixture("user_fills_by_time.json")[0]
    with session_scope(engine) as s:
        insert_fills(s, ADDR, [raw])
    with session_scope(engine) as s:
        f = s.get(Fill, raw["tid"])
        assert f.px == Decimal(raw["px"]) and isinstance(f.px, Decimal)
        assert f.closed_pnl == Decimal(raw["closedPnl"])
        assert f.crossed is raw["crossed"]


def test_alerts_unique_kind_address_source_ref(engine):
    with session_scope(engine) as s:
        s.add(Alert(kind="open", address=ADDR, coin="BTC", source_ref="hl:1", payload_json={}))
    with pytest.raises(IntegrityError):
        with session_scope(engine) as s:
            s.add(Alert(kind="open", address=ADDR, coin="BTC", source_ref="hl:1", payload_json={}))


def test_basic_rows(engine):
    now = datetime.now(timezone.utc)
    with session_scope(engine) as s:
        s.add(Wallet(address=ADDR, first_seen=now, stats_json={"n": 1}))
        s.add(Funding(address=ADDR, coin="BTC", time=1, usdc="-1.5"))
        s.add(PaperEvent(source_ref="hl:1", address=ADDR, json={}, observed_time=now, delay_ms=900))
        s.add(Kv(key="k", value="v"))
    with session_scope(engine) as s:
        w = s.get(Wallet, ADDR)
        assert w.is_vault is False and w.is_bot is False
        assert w.first_seen.tzinfo is not None
        assert s.get(PaperEvent, "hl:1").observed_time.tzinfo is not None

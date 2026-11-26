import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from conftest import NOW, make_signal
from hades.http import Http
from hades.models import Enrichment
from hades.notify import (
    Notifier, format_digest, format_signal, format_usd, next_digest_at, to_wib,
)

TG = "https://api.telegram.org/botTOKEN/sendMessage"


async def _nosleep(_):
    return None


@pytest.fixture
async def http():
    async with httpx.AsyncClient() as c:
        yield Http(c, rates={"api.telegram.org": 10_000}, sleep=_nosleep)


def _row(**kw):
    row = {"id": 1, "source": "repo_diff", "kind": "pr_opened", "chain": "monad", "symbol": "CAT",
           "name": "Cash Cat", "address": "0xabcdef1234567890abcdef1234567890abcdef12", "project_key": "monad:0x",
           "evidence_url": "https://github.com/trustwallet/assets/pull/1234", "observed_at": NOW,
           "score": 72, "score_detail": {"total": 72}, "severity": "high"}
    row.update(kw)
    return row


def test_to_wib():
    assert to_wib(datetime(2026, 11, 27, 7, 5, tzinfo=timezone.utc)) == "27 Nov 2026 14:05 WIB"


def test_format_usd():
    assert [format_usd(x) for x in (800, 12500, 1_250_000, None)] == ["$800", "$12.5k", "$1.25M", "—"]


def test_format_not_buyable_line():
    text = format_signal(_row(), {"buyable": False, "pool_count": 0, "verified": True}, [])
    assert "72" in text and "repo_diff · pr_opened" in text and "monad" in text
    assert "CAT — Cash Cat" in text
    assert "0xabcd…ef12" in text
    assert "Belum bisa dibeli onchain (0 pool)" in text
    assert '<a href="https://github.com/trustwallet/assets/pull/1234">github.com/trustwallet/assets/pull/1234</a>' in text
    assert "27 Nov 2026 14:05 WIB" in text


def test_format_buyable_line():
    enr = {"buyable": True, "liquidity_usd": 12500.0, "dex": "kuru", "pool_url": "https://x/p", "verified": True}
    assert 'Bisa dibeli · likuiditas $12.5k · kuru · <a href="https://x/p">pool</a>' in format_signal(_row(), enr, [])


def test_format_candidates_and_failed():
    cand = {"buyable": False, "verified": False, "candidates": [{"chain": "monad", "liquidity_usd": 900.0}]}
    assert "Kandidat (belum terverifikasi): 1 pool" in format_signal(_row(), cand, [])
    assert "Cek likuiditas gagal" in format_signal(_row(), {"buyable": None, "verified": True}, [])
    assert "Cek likuiditas gagal" in format_signal(_row(), None, [])


def test_format_fusion_and_prefix():
    sib = _row(id=2, source="ct", kind="ct_high", name="claim.cat.xyz", symbol=None,
               observed_at=NOW - timedelta(days=2))
    text = format_signal(_row(), None, [sib], prefix="Sekarang bisa dibeli", now=NOW)
    assert text.startswith("<b>Sekarang bisa dibeli</b>")
    assert "Fusion: ct claim.cat.xyz (2 hari lalu)" in text


def test_format_escapes_html():
    text = format_signal(_row(name="<b>A&B_*", symbol="X<"), None, [])
    assert "&lt;b&gt;A&amp;B_*" in text and "X&lt;" in text
    assert "<b>A&B" not in text


def test_format_truncates_4096():
    assert len(format_signal(_row(name="x" * 5000), None, [])) <= 4096


def test_is_muted_by_chain(store):
    n = Notifier(None, store, "", "")
    store.add_mute("monad", NOW + timedelta(hours=1))
    assert n.is_muted(make_signal(), NOW) is True
    assert n.is_muted(make_signal(chain="base", project_key="base:0x1"), NOW) is False
    assert n.is_muted(make_signal(), NOW + timedelta(hours=2)) is False


async def test_send_without_token_returns_none(store):
    sid = store.insert_signal(make_signal())
    assert await Notifier(None, store, "", "").send_signal(sid) is None


@respx.mock
async def test_send_posts_html(store, http):
    sid = store.insert_signal(make_signal())
    store.add_enrichment(sid, Enrichment(provider="gt", checked_at=NOW, buyable=False))
    route = respx.post(TG).mock(return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 55}}))
    assert await Notifier(http, store, "TOKEN", "999").send_signal(sid) == 55
    body = json.loads(route.calls[0].request.content)
    assert body["parse_mode"] == "HTML" and body["chat_id"] == "999"
    assert "Belum bisa dibeli" in body["text"]


@respx.mock
async def test_send_error_returns_none(store, http):
    sid = store.insert_signal(make_signal())
    respx.post(TG).mock(return_value=httpx.Response(400, json={"ok": False}))
    assert await Notifier(http, store, "TOKEN", "999").send_signal(sid) is None


def test_digest_none_when_empty():
    assert format_digest([], [], NOW) is None


def test_digest_lists_low_signals_and_rechecks():
    low = [_row(source="ct", kind="ct_low", name="docs.cat.xyz", symbol=None, evidence_url="https://crt.sh/?id=5")]
    re = [(_row(symbol="DOG"), {"buyable": True, "liquidity_usd": 3000.0})]
    text = format_digest(low, re, NOW)
    assert "Ringkasan harian" in text and "27 Nov 2026" in text
    assert 'docs.cat.xyz — <a href="https://crt.sh/?id=5">bukti</a>' in text
    assert "DOG: Bisa dibeli · $3k" in text


def test_next_digest_at():
    # NOW = 14:05 WIB -> next 08:00 WIB is tomorrow 01:00 UTC
    assert next_digest_at(NOW, 8) == datetime(2026, 11, 28, 1, 0, tzinfo=timezone.utc)
    early = datetime(2026, 11, 27, 0, 30, tzinfo=timezone.utc)  # 07:30 WIB
    assert next_digest_at(early, 8) == datetime(2026, 11, 27, 1, 0, tzinfo=timezone.utc)


def test_digest_rows(store):
    from hades.notify import digest_rows
    low = store.insert_signal(make_signal(dedupe_key="low", severity="low"))
    old_low = store.insert_signal(make_signal(dedupe_key="old", severity="low", observed_at=NOW - timedelta(days=2)))
    re = store.insert_signal(make_signal(dedupe_key="re", observed_at=NOW - timedelta(days=1, hours=1)))
    store.add_enrichment(re, Enrichment(provider="gt", checked_at=NOW - timedelta(days=1, hours=1), buyable=False))
    store.add_enrichment(re, Enrichment(provider="gt", checked_at=NOW - timedelta(hours=1), buyable=True))
    lows, rechecks = digest_rows(store, NOW)
    assert [r["id"] for r in lows] == [low]
    assert old_low not in [r["id"] for r in lows]
    assert [(r["id"], e["buyable"]) for r, e in rechecks] == [(re, True)]

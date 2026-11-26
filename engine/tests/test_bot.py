import json
from datetime import timedelta

import httpx
import pytest
import respx

from conftest import NOW, make_signal
from hades.bot import MAX_MUTE_H, handle_command, normalize_domain, poll_once
from hades.http import Http
from hades.notify import Notifier

OWNER = "999"


def _cmd(store, text, chat=OWNER):
    return handle_command(text, chat, owner_chat_id=OWNER, store=store, now=NOW)


def test_foreign_chat_ignored(store):
    assert _cmd(store, "/status", chat="1") is None


def test_non_command_ignored(store):
    assert _cmd(store, "halo") is None


def test_status_lists_watchers(store):
    store.record_ok("ct", NOW - timedelta(minutes=3))
    for _ in range(4):
        store.record_error("ground_truth:okx", "SourceError: <down>", NOW)
    store.insert_signal(make_signal())
    text = _cmd(store, "/status@HadesBot")
    assert "✅ ct · ok 3 menit lalu" in text
    assert "⚠️ ground_truth:okx · gagal ×4: SourceError: &lt;down&gt;" in text
    assert "24 jam: 1 sinyal, 0 alert, 0 listing" in text


def test_normalize_domain():
    assert normalize_domain("https://Cat.XYZ/path?x=1") == "cat.xyz"
    assert normalize_domain("www.cat.xyz") == "www.cat.xyz"
    assert normalize_domain("not a domain") is None
    assert normalize_domain("localhost") is None
    assert normalize_domain("") is None


def test_watchlist_add_normalizes(store):
    assert "cat.xyz" in _cmd(store, "/watchlist add https://Cat.XYZ/path")
    assert store.domains()[0]["domain"] == "cat.xyz"
    assert store.domains()[0]["origin"] == "manual"
    listing = _cmd(store, "/watchlist")
    assert "cat.xyz (manual)" in listing


def test_watchlist_add_rejects_garbage(store):
    reply = _cmd(store, "/watchlist add not a domain")
    assert "tidak valid" in reply
    assert store.domains() == []


def test_watchlist_rm(store):
    store.add_domain("cat.xyz", "auto")
    assert "Dihapus" in _cmd(store, "/watchlist rm cat.xyz")
    assert store.domains() == []
    assert "tidak ada" in _cmd(store, "/watchlist rm cat.xyz")


def test_mute_default_24h_and_cap(store):
    _cmd(store, "/mute monad")
    [m] = store.active_mutes(NOW)
    assert m["until"] == NOW + timedelta(hours=24)
    _cmd(store, "/mute ct 99999")
    ct = [m for m in store.active_mutes(NOW) if m["target"] == "ct"][0]
    assert ct["until"] == NOW + timedelta(hours=MAX_MUTE_H)
    assert "monad" in _cmd(store, "/mute list")
    assert "dibuka" in _cmd(store, "/unmute monad")
    assert [m["target"] for m in store.active_mutes(NOW)] == ["ct"]


def test_mute_bad_hours(store):
    assert "jam" in _cmd(store, "/mute monad abc")
    assert store.active_mutes(NOW) == []


def test_unknown_shows_help(store):
    text = _cmd(store, "/foo")
    for c in ("/status", "/watchlist", "/mute", "/unmute"):
        assert c in text


async def _nosleep(_):
    return None


@respx.mock
async def test_poll_once_replies_and_saves_offset(store):
    async with httpx.AsyncClient() as c:
        http = Http(c, rates={"api.telegram.org": 10_000}, sleep=_nosleep)
        respx.get("https://api.telegram.org/botT/getUpdates").mock(return_value=httpx.Response(200, json={
            "ok": True, "result": [
                {"update_id": 10, "message": {"chat": {"id": 999}, "text": "/watchlist add cat.xyz"}},
                {"update_id": 11, "message": {"chat": {"id": 5}, "text": "/status"}},
            ]}))
        send = respx.post("https://api.telegram.org/botT/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 1}}))
        n = Notifier(http, store, "T", OWNER)
        assert await poll_once(http, n, store, "T", OWNER, NOW, timeout=0) == 1
        assert send.call_count == 1
        assert "cat.xyz" in json.loads(send.calls[0].request.content)["text"]
        assert store.get_state("telegram_bot") == {"offset": 12}

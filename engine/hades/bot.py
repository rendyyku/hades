"""Telegram bot commands (spec 9): /status, /watchlist, /mute, /unmute. Owner chat only."""

import asyncio
import html
import logging
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from hades.http import Http, SourceError
from hades.notify import TG_API, Notifier, rel_time, to_wib
from hades.store import Store

log = logging.getLogger(__name__)

DEFAULT_MUTE_H = 24
MAX_MUTE_H = 720
STATE_NAME = "telegram_bot"
_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
_TLD = re.compile(r"^[a-z][a-z0-9-]{1,62}$")

HELP = (
    "Perintah HADES:\n"
    "/status — kesehatan watcher dan hitungan 24 jam\n"
    "/watchlist — daftar domain CT\n"
    "/watchlist add &lt;domain&gt; · /watchlist rm &lt;domain&gt;\n"
    "/mute &lt;project|source|chain&gt; [jam=24] · /mute list\n"
    "/unmute &lt;target&gt;"
)


def normalize_domain(text: str) -> str | None:
    """`https://Cat.XYZ/path` -> `cat.xyz`; None if it is not a plausible public hostname."""
    text = (text or "").strip().lower()
    if not text or " " in text:
        return None
    host = urlsplit(text if "//" in text else f"//{text}").hostname or ""
    labels = host.rstrip(".").split(".")
    if len(labels) < 2 or not all(_LABEL.match(lb) for lb in labels) or not _TLD.match(labels[-1]):
        return None
    return ".".join(labels)


def _e(v: object) -> str:
    return html.escape(str(v))


def _status(store: Store, now: datetime, intervals: dict[str, int] | None = None) -> str:
    lines = ["<b>Status HADES</b>"]
    for w in store.watchers(now=now, intervals=intervals):
        if w["stale"]:
            lines.append(f"⚠️ {_e(w['name'])} · macet, sukses terakhir {rel_time(w['last_ok'], now)}")
        elif w["fail_count"]:
            lines.append(f"⚠️ {_e(w['name'])} · gagal ×{w['fail_count']}: {_e(w['last_err'] or '')}")
        else:
            ok = rel_time(w["last_ok"], now) if w["last_ok"] else "belum pernah"
            lines.append(f"✅ {_e(w['name'])} · ok {ok}")
    if len(lines) == 1:
        lines.append("Belum ada watcher yang jalan.")
    c = store.counts_since(now - timedelta(days=1))
    lines.append(f"24 jam: {c['signals']} sinyal, {c['alerts']} alert, {c['listings']} listing")
    return "\n".join(lines)


def _watchlist(store: Store, args: list[str]) -> str:
    if not args:
        rows = store.domains()
        if not rows:
            return "Watchlist kosong. Tambah: /watchlist add &lt;domain&gt;"
        return "<b>Watchlist CT</b>\n" + "\n".join(f"• {_e(r['domain'])} ({_e(r['origin'])})" for r in rows)
    if args[0] in ("add", "rm") and len(args) >= 2:
        d = normalize_domain(" ".join(args[1:]))
        if d is None:
            return "Domain tidak valid."
        if args[0] == "add":
            return f"Ditambahkan: {_e(d)}" if store.add_domain(d, "manual") else f"Sudah ada: {_e(d)}"
        return f"Dihapus: {_e(d)}" if store.remove_domain(d) else f"{_e(d)} tidak ada di watchlist."
    return HELP


def _mute(store: Store, args: list[str], now: datetime) -> str:
    if not args:
        return HELP
    if args[0] == "list":
        rows = store.active_mutes(now)
        if not rows:
            return "Tidak ada mute aktif."
        return "<b>Mute aktif</b>\n" + "\n".join(f"• {_e(m['target'])} sampai {to_wib(m['until'])}" for m in rows)
    hours = DEFAULT_MUTE_H
    if len(args) >= 2:
        try:
            hours = int(args[1])
        except ValueError:
            return "Jumlah jam harus angka, contoh: /mute monad 48"
    hours = max(1, min(hours, MAX_MUTE_H))
    until = now + timedelta(hours=hours)
    store.add_mute(args[0], until)
    return f"Mute {_e(args[0])} selama {hours} jam (sampai {to_wib(until)})."


def handle_command(text: str, chat_id: str, *, owner_chat_id: str, store: Store, now: datetime,
                   intervals: dict[str, int] | None = None) -> str | None:
    if not owner_chat_id or str(chat_id) != str(owner_chat_id):
        return None
    parts = (text or "").strip().split()
    if not parts or not parts[0].startswith("/"):
        return None
    cmd, args = parts[0].split("@", 1)[0].lower(), parts[1:]
    if cmd == "/status":
        return _status(store, now, intervals)
    if cmd == "/watchlist":
        return _watchlist(store, args)
    if cmd == "/mute":
        return _mute(store, args, now)
    if cmd == "/unmute" and args:
        return f"Mute {_e(args[0])} dibuka." if store.remove_mute(args[0]) else f"{_e(args[0])} tidak sedang di-mute."
    return HELP


async def poll_once(http: Http, notifier: Notifier, store: Store, token: str, owner_chat_id: str,
                    now: datetime, timeout: int = 30, intervals: dict[str, int] | None = None) -> int:
    """One getUpdates long-poll. Returns how many commands were answered."""
    offset = (store.get_state(STATE_NAME) or {}).get("offset", 0)
    res = await http.get_json(f"{TG_API}/bot{token}/getUpdates", params={"offset": offset, "timeout": timeout},
                              timeout=timeout + 10, max_retries=1)
    answered = 0
    for u in res.get("result", []) if isinstance(res, dict) else []:
        offset = max(offset, u.get("update_id", 0) + 1)
        msg = u.get("message") or {}
        reply = handle_command(msg.get("text", ""), str((msg.get("chat") or {}).get("id", "")),
                               owner_chat_id=owner_chat_id, store=store, now=now, intervals=intervals)
        if reply:
            await notifier.send_text(reply)
            answered += 1
    store.save_state(STATE_NAME, {"offset": offset})
    return answered


async def poll_loop(http: Http, notifier: Notifier, store: Store, token: str, owner_chat_id: str,
                    stop: asyncio.Event, intervals: dict[str, int] | None = None) -> None:
    if not token or not owner_chat_id:
        log.info("telegram bot disabled (no TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)")
        return
    while not stop.is_set():
        try:
            await poll_once(http, notifier, store, token, owner_chat_id, datetime.now(timezone.utc),
                            intervals=intervals)
        except SourceError as e:
            log.warning("telegram getUpdates failed: %s", e)
            await asyncio.sleep(10)
        except Exception:  # noqa: BLE001 - the bot must outlive one bad update
            log.exception("telegram bot iteration failed")
            await asyncio.sleep(10)

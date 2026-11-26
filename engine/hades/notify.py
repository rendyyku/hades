"""Telegram alerts and the daily digest (spec 9). HTML parse mode; every dynamic field is escaped."""

import asyncio
import html
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from hades.config import AppConfig
from hades.http import Http, SourceError
from hades.models import Signal
from hades.store import Store

log = logging.getLogger(__name__)

WIB = timezone(timedelta(hours=7), "WIB")
TG_API = "https://api.telegram.org"
TG_MAX = 4096
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
DIGEST_MAX_LINES = 30
FUSION_MAX = 3
FIELD_MAX = 100  # names/symbols come from third-party files (PR info.json): clip before escaping


def to_wib(dt: datetime) -> str:
    w = dt.astimezone(WIB)
    return f"{w.day} {MONTHS[w.month - 1]} {w.year} {w:%H:%M} WIB"


def format_usd(v: float | None) -> str:
    if v is None:
        return "—"
    if v >= 1_000_000:
        return f"${v / 1_000_000:.2f}".rstrip("0").rstrip(".") + "M"
    if v >= 1_000:
        return f"${v / 1_000:.1f}".rstrip("0").rstrip(".") + "k"
    return f"${v:.0f}"


def rel_time(then: datetime, now: datetime) -> str:
    s = max(0, int((now - then).total_seconds()))
    if s < 3600:
        return f"{max(1, s // 60)} menit lalu"
    if s < 86400:
        return f"{s // 3600} jam lalu"
    return f"{s // 86400} hari lalu"


def short_addr(a: str) -> str:
    return a if len(a) <= 12 else f"{a[:6]}…{a[-4:]}"


def _clip(v: Any, n: int = FIELD_MAX) -> str:
    v = str(v)
    return v if len(v) <= n else v[: n - 1] + "…"


def _e(v: Any) -> str:
    return html.escape(str(v), quote=True)


def _ec(v: Any) -> str:
    """Clip, then escape: never cuts through an entity or a tag."""
    return _e(_clip(v))


def buyable_line(enr: dict | None) -> str:
    if enr is None or (enr.get("buyable") is None and not enr.get("candidates")):
        return "Cek likuiditas gagal"
    if enr.get("verified") is False:
        return f"Kandidat (belum terverifikasi): {len(enr.get('candidates') or [])} pool"
    if enr.get("buyable"):
        parts = [f"Bisa dibeli · likuiditas {format_usd(enr.get('liquidity_usd'))}"]
        if enr.get("dex"):
            parts.append(_e(enr["dex"]))
        if enr.get("pool_url"):
            parts.append(f'<a href="{_e(enr["pool_url"])}">pool</a>')
        return " · ".join(parts)
    return f"Belum bisa dibeli onchain ({enr.get('pool_count') or 0} pool)"


def _label(row: dict) -> str:
    raw = row.get("name") if row.get("source") == "ct" else (row.get("symbol") or row.get("name"))
    return _clip(raw or "?")


def format_signal(row: dict, enr: dict | None, fusion: list[dict], prefix: str = "",
                  now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    score = row.get("score") or 0
    icon = "🔥" if score >= 70 else "⚡" if score >= 40 else "•"
    lines = []
    if prefix:
        lines.append(f"<b>{_e(prefix)}</b>")
    head = f"{icon} <b>{score}</b>  {_e(row['source'])} · {_e(row['kind'])}"
    if row.get("chain"):
        head += f"  ·  {_e(row['chain'])}"
    lines.append(head)
    if row.get("source") == "ct":
        ident = _ec(row.get("name") or "")
    else:
        ident = " — ".join(_ec(x) for x in (row.get("symbol"), row.get("name")) if x)
        if row.get("address"):
            ident += f"   <code>{_e(short_addr(row['address']))}</code>"
    lines.append(ident)
    lines.append(buyable_line(enr))
    url = row["evidence_url"]
    lines.append(f'Bukti: <a href="{_e(url)}">{_e(_clip(url.split("://", 1)[-1], 200))}</a>')
    fusion_lines = [f"Fusion: {_e(f['source'])} {_e(_label(f))} ({rel_time(f['observed_at'], now)})"
                    for f in fusion[:FUSION_MAX]]
    footer = [to_wib(row["observed_at"])]
    # every field is clipped, so this fits; fusion lines are the first thing to go if it ever does not
    while fusion_lines and len("\n".join(lines + fusion_lines + footer)) > TG_MAX:
        fusion_lines.pop()
    return "\n".join(lines + fusion_lines + footer)


def _section(title: str, items: list[str], budget: int) -> list[str]:
    """Header + as many whole item lines as fit in `budget` chars, then `…dan N lainnya`."""
    out = [f"\n<b>{title} ({len(items)})</b>"]
    used = len(out[0]) + 40  # room for the "lainnya" line
    shown = 0
    for line in items[:DIGEST_MAX_LINES]:
        if used + len(line) + 1 > budget:
            break
        out.append(line)
        used += len(line) + 1
        shown += 1
    if shown < len(items):
        out.append(f"…dan {len(items) - shown} lainnya")
    return out


def format_digest(low: list[dict], rechecks: list[tuple[dict, dict]], now: datetime) -> str | None:
    if not low and not rechecks:
        return None
    w = now.astimezone(WIB)
    head = f"<b>Ringkasan harian HADES</b> · {w.day} {MONTHS[w.month - 1]} {w.year}"
    low_items = [f'• {_e(r["kind"])} {_e(_label(r))} — <a href="{_e(r["evidence_url"])}">bukti</a>' for r in low]
    re_items = []
    for r, e in rechecks:
        state = (f"Bisa dibeli · {format_usd(e.get('liquidity_usd'))}" if e.get("buyable")
                 else "Belum bisa dibeli" if e.get("buyable") is False else "Cek gagal")
        re_items.append(f"• {_e(_label(r))}: {state}")
    budget = (TG_MAX - len(head)) // (bool(low_items) + bool(re_items))
    lines = [head]
    if low_items:
        lines += _section("Sinyal lemah", low_items, budget)
    if re_items:
        lines += _section("Cek ulang 24 jam", re_items, budget)
    return "\n".join(lines)


def next_digest_at(now: datetime, hour_wib: int) -> datetime:
    w = now.astimezone(WIB)
    target = w.replace(hour=hour_wib, minute=0, second=0, microsecond=0)
    if target <= w:
        target += timedelta(days=1)
    return target.astimezone(timezone.utc)


class Notifier:
    def __init__(self, http: Http | None, store: Store, token: str, chat_id: str,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self.http = http
        self.store = store
        self.token = token
        self.chat_id = chat_id
        self.clock = clock

    def is_muted(self, sig: Signal | dict, now: datetime) -> bool:
        get = sig.get if isinstance(sig, dict) else lambda k: getattr(sig, k)
        targets = {m["target"] for m in self.store.active_mutes(now)}
        keys = {get("project_key"), self.store.resolve(get("project_key")), get("source"), get("chain")}
        return bool(targets & keys)

    async def send_text(self, text: str) -> int | None:
        if not self.token or not self.chat_id or self.http is None:
            log.info("telegram not configured; alert not sent:\n%s", text)
            return None
        try:
            res = await self.http.post_json(f"{TG_API}/bot{self.token}/sendMessage", json={
                "chat_id": self.chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True,
            })
        except SourceError as e:
            log.warning("telegram send failed: %s", e)
            return None
        return (res.get("result") or {}).get("message_id") if isinstance(res, dict) else None

    async def send_signal(self, signal_id: int, prefix: str = "") -> int | None:
        row = self.store.get_signal(signal_id)
        if row is None:
            return None
        enrs = self.store.enrichments(signal_id)
        fusion = [r for r in self.store.signals_by_project(row["project_key"], exclude_id=signal_id)
                  if r["source"] != row["source"]]
        return await self.send_text(format_signal(row, enrs[-1] if enrs else None, fusion, prefix, self.clock()))


def digest_rows(store: Store, now: datetime) -> tuple[list[dict], list[tuple[dict, dict]]]:
    since = now - timedelta(days=1)
    low = store.signals_since(since, severity="low")
    rechecks = []
    for row in store.signals_since(now - timedelta(days=3)):
        enrs = store.enrichments(row["id"])
        if len(enrs) >= 2 and enrs[-1]["checked_at"] >= since:
            rechecks.append((row, enrs[-1]))
    return low, rechecks


async def digest_loop(notifier: Notifier, store: Store, cfg: AppConfig, stop: asyncio.Event,
                      sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep) -> None:
    while not stop.is_set():
        now = notifier.clock()
        await sleep((next_digest_at(now, cfg.alerts.digest_hour_wib) - now).total_seconds())
        if stop.is_set():
            return
        try:
            now = notifier.clock()
            text = format_digest(*digest_rows(store, now), now)
            if text:
                await notifier.send_text(text)
        except Exception:  # noqa: BLE001 - one bad digest must not stop tomorrow's
            log.exception("daily digest failed")

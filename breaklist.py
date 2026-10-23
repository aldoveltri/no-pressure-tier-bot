"""Guild tools: breaklist, GEXP requirement / debt, excuses, automatic inactivity notices and Road to #6.

Everything here is independent from the 1v1 tier-testing part of the bot. Settings come from environment
variables (see README), so channels and roles can change without touching the code.
"""
import asyncio
import io
import logging
import math
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiohttp
import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import dotenv_values
from PIL import Image, ImageDraw

import gexpcalc as gc
import graphics as g
from database import DB_PATH, backup_db
from mojang import get_uuid, get_name

log = logging.getLogger("tierbot")
UTC = timezone.utc
NO_MENTIONS = discord.AllowedMentions.none()
USER_MENTIONS = discord.AllowedMentions(users=True)


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _int_env(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


GUILD_NAME_DISPLAY = _env("GUILD_DISPLAY_NAME", "NO PRESSURE")
GUILD_STAFF_ROLE = _env("GUILD_STAFF_ROLE", "Guild Staff")
HYPIXEL_GUILD_NAME = _env("HYPIXEL_GUILD_NAME", "No Pressure")
VETERAN_DAYS = _int_env("VETERAN_DAYS", 180)
FRIDAY_MAX_GEXP = _int_env("FRIDAY_NOTICE_MAX_GEXP", 20000)
FINAL_HOURS_BEFORE_PURGE = _int_env("FINAL_NOTICE_HOURS_BEFORE_PURGE", 12)
ROAD_POST_TIME = _env("ROAD_POST_TIME_ET", "00:10")      # HH:MM, Eastern Time
AUTO_BACKUP_KEEP = _int_env("AUTO_BACKUP_KEEP", 14)
REQ = gc.WEEKLY_REQ

REMINDER = ("Stay guildless while you are on the breaklist. Joining another guild will "
            "permanently remove your ability to use the breaklist.")


# ======================================================================================
# small helpers
# ======================================================================================

def chan_id(env_name: str):
    raw = _env(env_name)
    return int(raw) if raw.isdigit() else None


def n(value) -> str:
    return f"{int(value):,}"


def esc(text: str) -> str:
    return discord.utils.escape_markdown(text)


def is_guild_staff(member) -> bool:
    if not isinstance(member, discord.Member):
        return False
    if member.guild_permissions.administrator:
        return True
    return any(r.name.lower() == GUILD_STAFF_ROLE.lower() for r in member.roles)


async def staff_check(interaction: discord.Interaction) -> bool:
    return is_guild_staff(interaction.user)


def _read_key():
    """The Hypixel API key, from the .env file or from the environment variable."""
    key = dotenv_values(".env").get("HYPIXEL_API_KEY") or os.getenv("HYPIXEL_API_KEY")
    return key.strip() if key else None


def now_utc() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def from_iso(text) -> Optional[datetime]:
    if not text:
        return None
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


# ======================================================================================
# database (its own tables inside the bot's SQLite file)
# ======================================================================================

SCHEMA = """
CREATE TABLE IF NOT EXISTS bl_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid TEXT NOT NULL,
    ign TEXT NOT NULL,
    added_at TEXT NOT NULL,
    added_by TEXT,
    joined_at TEXT,
    left_at TEXT NOT NULL,
    days_total REAL NOT NULL DEFAULT 0,
    gexp INTEGER NOT NULL DEFAULT 0,
    required INTEGER NOT NULL DEFAULT 0,
    owed INTEGER NOT NULL DEFAULT 0,
    reason TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    returned_at TEXT
);
CREATE TABLE IF NOT EXISTS player_days (
    uuid TEXT PRIMARY KEY, ign TEXT, banked REAL NOT NULL DEFAULT 0, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS debts (
    uuid TEXT PRIMARY KEY, ign TEXT, amount INTEGER NOT NULL DEFAULT 0, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS excuses (
    uuid TEXT NOT NULL, week TEXT NOT NULL, ign TEXT, reason TEXT, added_by TEXT, added_at TEXT,
    PRIMARY KEY (uuid, week)
);
CREATE TABLE IF NOT EXISTS gexp_history (
    uuid TEXT NOT NULL, day TEXT NOT NULL, gexp INTEGER NOT NULL, PRIMARY KEY (uuid, day)
);
CREATE TABLE IF NOT EXISTS guild_seen (
    uuid TEXT PRIMARY KEY, joined_at TEXT, rank TEXT, last_seen TEXT
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS road_history (
    day TEXT NOT NULL, guild TEXT NOT NULL, exp INTEGER NOT NULL, PRIMARY KEY (day, guild)
);
"""


async def setup_tables():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()


async def q_one(sql, params=()):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(sql, params)
        row = await cur.fetchone()
        return dict(row) if row else None


async def q_all(sql, params=()):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(sql, params)
        return [dict(r) for r in await cur.fetchall()]


async def q_run(sql, params=()):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(sql, params)
        await db.commit()
        return cur.lastrowid


async def kv_get(key):
    row = await q_one("SELECT value FROM kv WHERE key = ?", (key,))
    return row["value"] if row else None


async def kv_set(key, value):
    await q_run("INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)))


# ======================================================================================
# Hypixel guild API
# ======================================================================================

async def api_guild(params: dict):
    """The 'guild' object of the Hypixel API, or None (no key, not found, error)."""
    key = _read_key()
    if not key:
        return None
    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get("https://api.hypixel.net/v2/guild", params=params,
                                   headers={"API-Key": key}) as r:
                if r.status != 200:
                    log.warning("Hypixel guild API answered with status %s", r.status)
                    return None
                data = await r.json()
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        log.warning("Hypixel guild API failed: %s", e)
        return None
    return data.get("guild")


# ======================================================================================
# cards
# ======================================================================================

def _wrap_lines(text, font, width):
    tmp = ImageDraw.Draw(Image.new("RGB", (g.WIDTH, 10)))
    return g._wrap(tmp, text, font, width)


def _box(d, x, y, w, h, label, value, color=None, size=32, sub=None, sub_color=None):
    d.rounded_rectangle([x, y, x + w, y + h], radius=14, fill=g.ROW_BG)
    d.text((x + w // 2, y + 28), label, font=g.get_font(14, True), fill=g.MUTED, anchor="mm")
    d.text((x + w // 2, y + (62 if sub else 70)), value, font=g.get_font(size, True),
           fill=color or g.TEXT, anchor="mm")
    if sub:
        d.text((x + w // 2, y + 92), sub, font=g.get_font(15, True), fill=sub_color or g.MUTED, anchor="mm")


def _identity(img, d, y, name, head, badge_text, badge_color, extra=None, h=130):
    d.rounded_rectangle([g.PAD, y, g.WIDTH - g.PAD, y + h], radius=16, fill=g.ROW_BG)
    d.rounded_rectangle([g.PAD, y, g.PAD + 8, y + h], radius=4, fill=badge_color)
    g._paste_head(img, d, head, g.PAD + 34, y + 20, 90, radius=12)
    nx = g.PAD + 34 + 90 + 24
    d.text((nx, y + 44), name, font=g.get_font(38, True), fill=g.TEXT, anchor="lm")
    bw = max(150, int(d.textlength(badge_text, font=g.get_font(16, True))) + 40)
    g._badge(d, nx + bw // 2, y + 94, badge_text, badge_color, w=bw, h=34, font_size=16)
    if extra:
        d.text((g.WIDTH - g.PAD - 30, y + h // 2), extra, font=g.get_font(18, True), fill=g.MUTED, anchor="rm")


def _text_panel(d, y, title, text, color=None, font_size=19, max_lines=6, tcolor=None):
    """A rounded panel with a small title and wrapped text. Returns the y after the panel."""
    font = g.get_font(font_size)
    inner = g.WIDTH - 2 * (g.PAD + 28)
    lines = _wrap_lines(text, font, inner)[:max_lines]
    h = 24 + 28 + len(lines) * 28 + 14
    d.rounded_rectangle([g.PAD, y, g.WIDTH - g.PAD, y + h], radius=16, fill=g.ROW_BG)
    x = g.PAD + 28
    d.text((x, y + 22), title, font=g.get_font(15, True), fill=color or g.ACCENT, anchor="lm")
    ty = y + 46
    for line in lines:
        d.text((x, ty), line, font=font, fill=tcolor or g.SOFT, anchor="lt")
        ty += 28
    return y + h


def _panel_height(text, font_size=19, max_lines=6):
    font = g.get_font(font_size)
    inner = g.WIDTH - 2 * (g.PAD + 28)
    return 24 + 28 + min(max_lines, len(_wrap_lines(text, font, inner))) * 28 + 14


# ---------- breaklist notice / view ----------

def draw_breaklist_card(e, head, title, count):
    status = e["status"]
    active = status == "active"
    days = e["days_total"]
    reason = e.get("reason") or "No reason given."
    notice = REMINDER if active else "This player is no longer on the breaklist."
    id_h, box_h, gap = 130, 110, 14
    top = g.HEADER_H + 16
    height = (top + id_h + gap + box_h + gap + box_h + gap + _panel_height(reason, max_lines=3) + gap
              + _panel_height(notice, max_lines=3) + 16 + g.FOOTER_H)
    img = Image.new("RGB", (g.WIDTH, height), g.BG)
    d = ImageDraw.Draw(img)
    g._header(d, title, f"{GUILD_NAME_DISPLAY} · breaklist")

    label = {"active": "ON BREAKLIST", "returned": "RETURNED"}.get(status, status.upper())
    extra = f"Breaklisted {count} time{'s' if count != 1 else ''}"
    _identity(img, d, top, e["ign"], head, label, g.ACCENT if active else g.MUTED, extra, id_h)

    bw = (g.WIDTH - 2 * g.PAD - 2 * gap) // 3
    y = top + id_h + gap
    owed = e["owed"]
    _box(d, g.PAD, y, bw, box_h, "GEXP THIS WEEK", n(e["gexp"]))
    _box(d, g.PAD + bw + gap, y, bw, box_h, "REQUIRED AT BREAKLIST", n(e["required"]))
    _box(d, g.PAD + 2 * (bw + gap), y, bw, box_h, "EXTRA GEXP OWED", n(owed),
         g.RED if owed > 0 else g.GREEN)

    y += box_h + gap
    if days >= VETERAN_DAYS:
        sub, sub_color = "VETERAN REACHED", g.GREEN
    else:
        sub, sub_color = f"{VETERAN_DAYS - int(days)} to veteran", g.MUTED
    _box(d, g.PAD, y, bw, box_h, "DAYS IN GUILD", f"{int(days)} / {VETERAN_DAYS}", size=30,
         sub=sub, sub_color=sub_color)
    joined = from_iso(e.get("joined_at"))
    _box(d, g.PAD + bw + gap, y, bw, box_h, "JOINED THE GUILD", gc.fmt_date(joined) if joined else "-", size=28)
    left = from_iso(e["left_at"])
    _box(d, g.PAD + 2 * (bw + gap), y, bw, box_h, "BREAKLISTED", gc.fmt_date(left), size=28)

    y += box_h + gap
    y = _text_panel(d, y, "REASON", reason, max_lines=3)
    y += gap
    _text_panel(d, y, "NOTICE", notice, color=g.ACCENT if active else g.MUTED, max_lines=3,
                tcolor=g.TEXT if active else g.SOFT)
    g._footer(d, height, f"Added {gc.fmt_date(from_iso(e['added_at']))}")
    return g._png(img)


# ---------- list of everyone on the breaklist ----------

def draw_breaklist_list(rows, heads, page, pages, total):
    top = g.HEADER_H + 40
    height = top + len(rows) * g.ROW_H + g.FOOTER_H
    img = Image.new("RGB", (g.WIDTH, height), g.BG)
    d = ImageDraw.Draw(img)
    g._header(d, "BREAKLIST", f"{GUILD_NAME_DISPLAY} · {total} players on the breaklist")

    cols = [("ADDED", 520), ("DAYS", 640), ("OWED", 780)]
    sy = g.HEADER_H + 22
    for label, x in cols:
        d.text((x, sy), label, font=g.get_font(15, True), fill=g.MUTED, anchor="mm")
    for i, e in enumerate(rows):
        y0 = top + i * g.ROW_H
        cy = y0 + (g.ROW_H - 8) // 2
        d.rounded_rectangle([g.PAD, y0, g.WIDTH - g.PAD, y0 + g.ROW_H - 8], radius=14, fill=g.ROW_BG)
        d.rounded_rectangle([g.PAD, y0, g.PAD + 8, y0 + g.ROW_H - 8], radius=4, fill=g.ACCENT)
        hx = g.PAD + 28
        g._paste_head(img, d, heads.get(e["uuid"]), hx, cy - g.HEAD // 2, g.HEAD)
        d.text((hx + g.HEAD + 12, cy), e["ign"], font=g.get_font(24, True), fill=g.TEXT, anchor="lm")
        d.text((520, cy), gc.fmt_date(from_iso(e["added_at"])), font=g.get_font(18, True), fill=g.TEXT, anchor="mm")
        d.text((640, cy), str(int(e["days_total"])), font=g.get_font(20, True), fill=g.TEXT, anchor="mm")
        owed = e["owed"]
        d.text((780, cy), n(owed), font=g.get_font(20, True), fill=g.RED if owed > 0 else g.GREEN, anchor="mm")
    g._footer(d, height, f"Page {page}/{pages}")
    return g._png(img)


# ---------- breaklist history of one player ----------

def draw_history(name, head, entries, total, page, pages):
    row_h = 104
    top = g.HEADER_H + 16
    id_h = 110
    height = top + id_h + 14 + len(entries) * (row_h + 10) + 6 + g.FOOTER_H
    img = Image.new("RGB", (g.WIDTH, height), g.BG)
    d = ImageDraw.Draw(img)
    g._header(d, "BREAKLIST HISTORY", f"{GUILD_NAME_DISPLAY} · {name}")

    d.rounded_rectangle([g.PAD, top, g.WIDTH - g.PAD, top + id_h], radius=16, fill=g.ROW_BG)
    d.rounded_rectangle([g.PAD, top, g.PAD + 8, top + id_h], radius=4, fill=g.ACCENT)
    g._paste_head(img, d, head, g.PAD + 34, top + 15, 80, radius=12)
    d.text((g.PAD + 34 + 80 + 22, top + 55), name, font=g.get_font(36, True), fill=g.TEXT, anchor="lm")
    d.text((g.WIDTH - g.PAD - 30, top + 40), str(total), font=g.get_font(44, True), fill=g.ACCENT, anchor="rm")
    d.text((g.WIDTH - g.PAD - 30, top + 82), "TIMES ON BREAKLIST", font=g.get_font(14, True),
           fill=g.MUTED, anchor="rm")

    y = top + id_h + 14
    for e in entries:
        left = from_iso(e["left_at"])
        back = from_iso(e.get("returned_at"))
        length = gc.fmt_duration(((back or now_utc()) - left).total_seconds())
        active = e["status"] == "active"
        color = g.ACCENT if active else g.MUTED
        d.rounded_rectangle([g.PAD, y, g.WIDTH - g.PAD, y + row_h], radius=14, fill=g.ROW_BG)
        d.rounded_rectangle([g.PAD, y, g.PAD + 8, y + row_h], radius=4, fill=color)
        x = g.PAD + 28
        end_txt = gc.fmt_date(back) if back else "now"
        d.text((x, y + 24), f"{gc.fmt_date(left)} to {end_txt}", font=g.get_font(20, True), fill=g.TEXT, anchor="lm")
        d.text((x + 330, y + 24), f"Break length: {length}" + ("  (ongoing)" if active else ""),
               font=g.get_font(16, True), fill=g.SOFT, anchor="lm")
        reason = e.get("reason") or "No reason given."
        for i, line in enumerate(_wrap_lines("Reason: " + reason, g.get_font(17), 600)[:2]):
            d.text((x, y + 54 + i * 24), line, font=g.get_font(17), fill=g.SOFT, anchor="lm")
        owed = e["owed"]
        d.text((g.WIDTH - g.PAD - 24, y + 36), n(owed), font=g.get_font(26, True),
               fill=g.RED if owed > 0 else g.GREEN, anchor="rm")
        d.text((g.WIDTH - g.PAD - 24, y + 66), "GEXP OWED", font=g.get_font(13, True), fill=g.MUTED, anchor="rm")
        d.text((g.WIDTH - g.PAD - 24, y + 86), f"{int(e['days_total'])} days in guild",
               font=g.get_font(13), fill=g.MUTED, anchor="rm")
        y += row_h + 10
    g._footer(d, height, f"Page {page}/{pages}")
    return g._png(img)


# ---------- GEXP of the last 7 days ----------

def draw_eligibility(name, head, days, total, owed, source):
    """days: list of (day 'YYYY-MM-DD', gexp), oldest first."""
    daily = REQ / 7
    ok = total >= REQ
    id_h = 130
    rows_h = 60 + len(days) * 38 + 12
    info_h = 72 if owed is not None else 0
    top = g.HEADER_H + 16
    height = top + id_h + 14 + rows_h + ((14 + info_h) if info_h else 0) + 16 + g.FOOTER_H
    img = Image.new("RGB", (g.WIDTH, height), g.BG)
    d = ImageDraw.Draw(img)
    g._header(d, "GEXP ELIGIBILITY", f"{GUILD_NAME_DISPLAY} · last 7 days")

    y = top
    d.rounded_rectangle([g.PAD, y, g.WIDTH - g.PAD, y + id_h], radius=16, fill=g.ROW_BG)
    d.rounded_rectangle([g.PAD, y, g.PAD + 8, y + id_h], radius=4, fill=g.GREEN if ok else g.RED)
    g._paste_head(img, d, head, g.PAD + 34, y + 20, 90, radius=12)
    nx = g.PAD + 34 + 90 + 24
    d.text((nx, y + 44), name, font=g.get_font(36, True), fill=g.TEXT, anchor="lm")
    label = "QUALIFIES" if ok else f"MISSING {n(REQ - total)}"
    g._badge(d, nx + 100, y + 96, label, g.GREEN if ok else g.RED, w=200, h=34, font_size=16)
    d.text((g.WIDTH - g.PAD - 30, y + 52), n(total), font=g.get_font(44, True), fill=g.TEXT, anchor="rm")
    d.text((g.WIDTH - g.PAD - 30, y + 98), f"of {n(REQ)} GEXP", font=g.get_font(18), fill=g.MUTED, anchor="rm")

    y += id_h + 14
    d.rounded_rectangle([g.PAD, y, g.WIDTH - g.PAD, y + rows_h], radius=16, fill=g.ROW_BG)
    d.text((g.PAD + 24, y + 28), "GEXP PER DAY (the white line is the daily share of the requirement)",
           font=g.get_font(15, True), fill=g.MUTED, anchor="lm")
    scale = max([v for _, v in days] + [daily, 1])
    bar_x0 = g.PAD + 24 + 90
    bar_x1 = g.WIDTH - g.PAD - 24 - 110
    bar_w = bar_x1 - bar_x0
    for i, (day, v) in enumerate(days):
        cy = y + 62 + i * 38
        try:
            day_label = datetime.strptime(day, "%Y-%m-%d").strftime("%m/%d")
        except ValueError:
            day_label = day
        d.text((g.PAD + 24, cy), day_label, font=g.get_font(18, True), fill=g.TEXT, anchor="lm")
        d.rounded_rectangle([bar_x0, cy - 10, bar_x1, cy + 10], radius=10, fill=g.TRACK)
        if v > 0:
            length = max(12, int(bar_w * v / scale))
            d.rounded_rectangle([bar_x0, cy - 10, bar_x0 + length, cy + 10], radius=10,
                                fill=g.GREEN if v >= daily else g.RED)
        tick = bar_x0 + int(bar_w * daily / scale)
        d.line([(tick, cy - 14), (tick, cy + 14)], fill=g.TEXT, width=2)
        d.text((g.WIDTH - g.PAD - 24, cy), n(v), font=g.get_font(18, True), fill=g.TEXT, anchor="rm")

    if owed is not None:
        y += rows_h + 14
        d.rounded_rectangle([g.PAD, y, g.WIDTH - g.PAD, y + info_h], radius=16, fill=g.ROW_BG)
        d.text((g.PAD + 24, y + 24), "EXTRA GEXP DEBT", font=g.get_font(15, True), fill=g.ACCENT, anchor="lm")
        d.text((g.PAD + 24, y + 50), f"{n(owed)} GEXP owed on top of the normal requirement",
               font=g.get_font(20, True), fill=g.TEXT, anchor="lm")
    g._footer(d, height, source)
    return g._png(img)


# ---------- /gexp missing ----------

def draw_missing(name, head, joined, purge, r, start, current, extra_note):
    id_h, box_h, gap = 130, 110, 14
    top = g.HEADER_H + 16
    if r["all_clear"]:
        status, color = "ALL CLEAR", g.GREEN
    elif r["normal_met"]:
        status, color = "REQUIREMENT MET", g.ACCENT
    else:
        status, color = "KEEP GOING", g.RED
    lines = []
    if r["normal_met"]:
        lines.append(f"The normal requirement of {n(r['normal'])} GEXP is met.")
        if r["debt"]:
            lines.append(f"{n(r['paid'])} GEXP of the {n(r['debt'])} GEXP debt were paid. "
                         + (f"{n(r['remaining_debt'])} GEXP of debt carry over to the next weeks."
                            if r["remaining_debt"] else "The debt is fully paid."))
    else:
        lines.append(f"{n(r['still_normal'])} GEXP are still needed to meet the normal requirement of "
                     f"{n(r['normal'])} GEXP.")
        if r["debt"]:
            lines.append(f"The extra debt of {n(r['debt'])} GEXP only starts to be paid after that. "
                         f"{n(r['still_total'])} GEXP are needed in total to clear everything.")
    if extra_note:
        lines.append(extra_note)
    text = " ".join(lines)
    height = top + id_h + gap + box_h + gap + box_h + gap + _panel_height(text, max_lines=5) + 16 + g.FOOTER_H
    img = Image.new("RGB", (g.WIDTH, height), g.BG)
    d = ImageDraw.Draw(img)
    g._header(d, "GEXP REQUIREMENT", f"{GUILD_NAME_DISPLAY} · weekly requirement {n(REQ)} GEXP")
    _identity(img, d, top, name, head, status, color,
              f"Joined {gc.fmt_et(joined)}", id_h)
    d.text((g.WIDTH - g.PAD - 30, top + id_h - 28), f"Purge {gc.fmt_et(purge)}", font=g.get_font(16),
           fill=g.MUTED, anchor="rm")

    bw = (g.WIDTH - 2 * g.PAD - 2 * gap) // 3
    y = top + id_h + gap
    _box(d, g.PAD, y, bw, box_h, "NORMAL REQUIREMENT", n(r["normal"]),
         sub=f"{gc.fmt_duration(gc.secs(joined, purge))} until the purge")
    _box(d, g.PAD + bw + gap, y, bw, box_h, "STARTING GEXP", n(start))
    _box(d, g.PAD + 2 * (bw + gap), y, bw, box_h, "CURRENT GEXP", n(current),
         sub=f"+{n(r['progress'])} since joining", sub_color=g.GREEN if r["progress"] else g.MUTED)
    y += box_h + gap
    _box(d, g.PAD, y, bw, box_h, "EXTRA GEXP DEBT", n(r["debt"]), g.RED if r["debt"] else g.TEXT)
    _box(d, g.PAD + bw + gap, y, bw, box_h, "TOTAL GEXP TARGET", n(r["target"]),
         sub="normal + debt")
    _box(d, g.PAD + 2 * (bw + gap), y, bw, box_h, "REMAINING DEBT", n(r["remaining_debt"]),
         g.RED if r["remaining_debt"] else g.GREEN,
         sub=f"{n(r['still_total'])} GEXP still to earn" if r["still_total"] else "nothing left to earn",
         sub_color=g.MUTED)
    y += box_h + gap
    _text_panel(d, y, "SUMMARY", text, max_lines=5)
    g._footer(d, height, "Normal requirement first, then the extra debt")
    return g._png(img)


# ---------- inactivity notice ----------

def draw_notice(it, head):
    final = it["kind"] == "final"
    title = "FINAL GEXP REMINDER" if final else "GEXP REMINDER"
    if final:
        why = (f"The weekly purge is in {gc.fmt_duration(it['seconds_left'])}. "
               f"You have {n(it['weekly'])} GEXP and your requirement for this week is {n(it['required'])} GEXP. "
               f"Players under the requirement are kicked or breaklisted at the purge.")
    else:
        why = (f"You are under {n(FRIDAY_MAX_GEXP)} GEXP this week with {gc.fmt_duration(it['seconds_left'])} "
               f"left before the purge. The weekly requirement is {n(it['required'])} GEXP.")
    need_line = (f"You still need {n(it['needed'])} GEXP, about {n(it['per_day'])} GEXP per day.")
    if it.get("debt"):
        need_line += f" You also owe {n(it['debt'])} extra GEXP from before."
    how = "Use the buttons below: Breaklist Me puts you on the breaklist, Excuse Me excuses you for this week."
    text = f"{why} {need_line} {how}"

    id_h, box_h, gap = 130, 110, 14
    top = g.HEADER_H + 16
    bar_h = 86
    height = top + id_h + gap + bar_h + gap + box_h + gap + _panel_height(text, max_lines=7) + 16 + g.FOOTER_H
    img = Image.new("RGB", (g.WIDTH, height), g.BG)
    d = ImageDraw.Draw(img)
    g._header(d, title, f"{GUILD_NAME_DISPLAY} · purge {gc.fmt_et(it['purge'])}")
    badge = "FINAL NOTICE" if final else f"UNDER {n(FRIDAY_MAX_GEXP)} GEXP"
    _identity(img, d, top, it["ign"], head, badge, g.RED if final else g.ACCENT,
              f"{gc.fmt_duration(it['seconds_left'])} left", id_h)

    y = top + id_h + gap
    d.rounded_rectangle([g.PAD, y, g.WIDTH - g.PAD, y + bar_h], radius=16, fill=g.ROW_BG)
    bx0, bx1 = g.PAD + 28, g.WIDTH - g.PAD - 28
    by = y + 52
    d.rounded_rectangle([bx0, by - 11, bx1, by + 11], radius=11, fill=g.TRACK)
    frac = min(1.0, it["weekly"] / max(1, it["required"]))
    if frac > 0:
        d.rounded_rectangle([bx0, by - 11, bx0 + max(14, int((bx1 - bx0) * frac)), by + 11], radius=11,
                            fill=g.GREEN if frac >= 1 else g.ACCENT)
    d.text((bx0, y + 22), "PROGRESS THIS WEEK", font=g.get_font(14, True), fill=g.MUTED, anchor="lm")
    d.text((bx1, y + 22), f"{frac * 100:.0f}%", font=g.get_font(16, True), fill=g.TEXT, anchor="rm")

    bw = (g.WIDTH - 2 * g.PAD - 2 * gap) // 3
    y += bar_h + gap
    _box(d, g.PAD, y, bw, box_h, "GEXP THIS WEEK", n(it["weekly"]))
    _box(d, g.PAD + bw + gap, y, bw, box_h, "REQUIRED", n(it["required"]))
    _box(d, g.PAD + 2 * (bw + gap), y, bw, box_h, "STILL NEEDED", n(it["needed"]),
         g.RED if it["needed"] else g.GREEN, sub=f"~{n(it['per_day'])} per day")
    y += box_h + gap
    _text_panel(d, y, "WHY YOU ARE SEEING THIS", text, max_lines=7)
    g._footer(d, height, "Not affiliated with Hypixel")
    return g._png(img)


# ---------- Road to #N ----------

CHART_COLORS = [None, (167, 112, 255), (74, 144, 255)]   # the first one is the accent


def draw_road(ours, targets, series, title):
    """ours = {'name','exp','rank'}; targets = [{'rank','name','exp','gap','eta','rate_us','rate_them'}];
    series = {target name: [(day, gap), ...]}"""
    row_h = 92
    chart_h = 300
    top = g.HEADER_H + 16
    height = top + 14 + len(targets) * (row_h + 10) + chart_h + 16 + g.FOOTER_H
    img = Image.new("RGB", (g.WIDTH, height), g.BG)
    d = ImageDraw.Draw(img)
    g._header(d, title, f"{GUILD_NAME_DISPLAY} · currently #{ours['rank']} with {n(ours['exp'])} GEXP")

    colors = [g.ACCENT, (167, 112, 255), (74, 144, 255)]
    y = top
    for i, t in enumerate(targets):
        c = colors[i % 3]
        d.rounded_rectangle([g.PAD, y, g.WIDTH - g.PAD, y + row_h], radius=14, fill=g.ROW_BG)
        d.rounded_rectangle([g.PAD, y, g.PAD + 8, y + row_h], radius=4, fill=c)
        g._badge(d, g.PAD + 70, y + row_h // 2, f"#{t['rank']}", c, w=80, h=40, font_size=22)
        d.text((g.PAD + 130, y + 24), t["name"], font=g.get_font(24, True), fill=g.TEXT, anchor="lm")
        if t["rate_us"] is not None and t["rate_them"] is not None:
            line1 = f"You {n(t['rate_us'])}/day  ·  Them {n(t['rate_them'])}/day"
            if t["eta"] is not None:
                line2 = f"Catch up in about {n(math.ceil(t['eta']))} days"
            elif t["rate_us"] <= t["rate_them"]:
                line2 = "The gap is not closing"
            else:
                line2 = ""
        else:
            line1, line2 = "Daily stats appear after a few days of data", ""
        d.text((g.PAD + 130, y + 54), line1, font=g.get_font(15), fill=g.SOFT, anchor="lm")
        if line2:
            d.text((g.PAD + 130, y + 75), line2, font=g.get_font(15, True), fill=g.GREEN if "Catch" in line2 else g.RED,
                   anchor="lm")
        d.text((g.WIDTH - g.PAD - 24, y + 36), n(t["gap"]), font=g.get_font(34, True), fill=c, anchor="rm")
        d.text((g.WIDTH - g.PAD - 24, y + 68), f"GEXP FROM #{t['rank']} GUILD", font=g.get_font(13, True),
               fill=g.MUTED, anchor="rm")
        y += row_h + 10

    # chart: gap over time
    d.rounded_rectangle([g.PAD, y, g.WIDTH - g.PAD, y + chart_h], radius=16, fill=g.ROW_BG)
    d.text((g.PAD + 24, y + 26), "GAP OVER TIME (GEXP)", font=g.get_font(15, True), fill=g.MUTED, anchor="lm")
    px0, px1 = g.PAD + 110, g.WIDTH - g.PAD - 40
    py0, py1 = y + 60, y + chart_h - 80
    series = {k: v for k, v in series.items() if len(v) >= 2}      # a single point makes no line
    all_pts = [v for pts in series.values() for _, v in pts]
    days_all = sorted({day for pts in series.values() for day, _ in pts})
    if len(days_all) < 2:
        d.text(((px0 + px1) // 2, (py0 + py1) // 2), "The chart appears after two days of data",
               font=g.get_font(18), fill=g.MUTED, anchor="mm")
    else:
        lo, hi = min(all_pts), max(all_pts)
        if hi == lo:
            hi = lo + 1
        pad = (hi - lo) * 0.1
        lo, hi = max(0, lo - pad), hi + pad
        for k in range(4):
            gy = py0 + (py1 - py0) * k / 3
            d.line([(px0, gy), (px1, gy)], fill=g.TRACK, width=1)
            val = hi - (hi - lo) * k / 3
            d.text((px0 - 10, gy), f"{val / 1_000_000:.1f}M", font=g.get_font(13), fill=g.MUTED, anchor="rm")
        idx = {day: i for i, day in enumerate(days_all)}

        def xy(day, v):
            return (px0 + (px1 - px0) * idx[day] / (len(days_all) - 1),
                    py1 - (py1 - py0) * (v - lo) / (hi - lo))
        for i, t in enumerate(targets):
            pts = [xy(day, v) for day, v in series.get(t["name"], [])]
            if len(pts) >= 2:
                d.line(pts, fill=colors[i % 3], width=4)
            for p in pts:
                d.ellipse([p[0] - 4, p[1] - 4, p[0] + 4, p[1] + 4], fill=colors[i % 3])
        d.text((px0, py1 + 20), days_all[0][5:].replace("-", "/"), font=g.get_font(14), fill=g.MUTED, anchor="lm")
        d.text((px1, py1 + 20), days_all[-1][5:].replace("-", "/"), font=g.get_font(14), fill=g.MUTED, anchor="rm")
    lx = g.PAD + 24
    for i, t in enumerate(targets):
        d.rounded_rectangle([lx, y + chart_h - 29, lx + 14, y + chart_h - 15], radius=3, fill=colors[i % 3])
        d.text((lx + 22, y + chart_h - 22), f"#{t['rank']} {t['name']}", font=g.get_font(14), fill=g.SOFT, anchor="lm")
        lx += 40 + int(d.textlength(f"#{t['rank']} {t['name']}", font=g.get_font(14))) + 20
    g._footer(d, height, "GEXP = total guild experience")
    return g._png(img)


# ======================================================================================
# core logic shared by the commands, the buttons and the schedule
# ======================================================================================

class Core:
    def __init__(self, bot):
        self.bot = bot
        self._members = {}
        self._guild = None
        self._stamp = 0.0
        self._lock = asyncio.Lock()

    # ----- guild data -----

    async def guild_members(self, force=False):
        """{uuid: member} of the guild (cached 2 minutes). Saves the daily GEXP and the join dates too."""
        async with self._lock:
            if self._members and not force and time.time() - self._stamp < 120:
                return self._members
            guild = await api_guild({"name": HYPIXEL_GUILD_NAME}) if HYPIXEL_GUILD_NAME else None
            if not guild:
                return self._members
            self._guild = guild
            self._members = {m["uuid"]: m for m in guild.get("members", [])}
            self._stamp = time.time()
        await self._save_snapshot(self._members)
        return self._members

    async def _save_snapshot(self, members):
        stamp = iso(now_utc())
        async with aiosqlite.connect(DB_PATH) as db:
            for uuid, m in members.items():
                joined = m.get("joined")
                joined_iso = iso(datetime.fromtimestamp(joined / 1000, tz=UTC)) if joined else None
                await db.execute(
                    "INSERT INTO guild_seen (uuid, joined_at, rank, last_seen) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(uuid) DO UPDATE SET joined_at = COALESCE(excluded.joined_at, guild_seen.joined_at), "
                    "rank = excluded.rank, last_seen = excluded.last_seen",
                    (uuid, joined_iso, m.get("rank"), stamp))
                for day, value in (m.get("expHistory") or {}).items():
                    await db.execute(
                        "INSERT INTO gexp_history (uuid, day, gexp) VALUES (?, ?, ?) "
                        "ON CONFLICT(uuid, day) DO UPDATE SET gexp = excluded.gexp", (uuid, day, int(value)))
            await db.commit()

    async def joined_for(self, uuid):
        m = self._members.get(uuid)
        if m and m.get("joined"):
            return datetime.fromtimestamp(m["joined"] / 1000, tz=UTC)
        row = await q_one("SELECT joined_at FROM guild_seen WHERE uuid = ?", (uuid,))
        return from_iso(row["joined_at"]) if row else None

    async def history_days(self, uuid):
        """{'YYYY-MM-DD': gexp}: live from Hypixel if the player is in the guild, else the saved copy."""
        m = self._members.get(uuid)
        if m and m.get("expHistory"):
            return {d: int(v) for d, v in m["expHistory"].items()}, "Live from Hypixel"
        rows = await q_all("SELECT day, gexp FROM gexp_history WHERE uuid = ? ORDER BY day DESC LIMIT 7", (uuid,))
        return {r["day"]: r["gexp"] for r in rows}, "Saved history"

    async def window_sum(self, uuid, first: datetime, last: datetime):
        a = first.astimezone(gc.ET).date().isoformat()
        b = last.astimezone(gc.ET).date().isoformat()
        row = await q_one("SELECT COALESCE(SUM(gexp), 0) AS total, COUNT(*) AS count FROM gexp_history "
                          "WHERE uuid = ? AND day >= ? AND day <= ?", (uuid, a, b))
        return row["total"], row["count"]

    async def name_for(self, uuid):
        try:
            name = await get_name(uuid)
            if name:
                return name
        except Exception:
            pass
        row = await q_one("SELECT ign FROM bl_entries WHERE uuid = ? ORDER BY id DESC LIMIT 1", (uuid,))
        return row["ign"] if row else uuid[:8]

    async def linked_discord(self, uuid):
        try:
            row = await q_one("SELECT discord_id FROM links WHERE uuid = ?", (uuid,))
        except Exception:
            return None
        return int(row["discord_id"]) if row else None

    async def head_for(self, uuid, name):
        heads = await g._fetch_heads([(uuid, name)])
        return heads.get(uuid)

    # ----- posting -----

    async def post_to(self, env_name, *, content=None, data=None, filename="card.png", view=None,
                      thread_name=None, thread_key=None, mentions=None):
        """Sends to the channel set in the environment variable (text channel, thread or forum).
        Returns the channel mention, None when no channel is configured, False when posting failed."""
        cid = chan_id(env_name)
        if cid is None:
            return None
        try:
            ch = self.bot.get_channel(cid) or await self.bot.fetch_channel(cid)
            kwargs = {"allowed_mentions": mentions or NO_MENTIONS}
            if content:
                kwargs["content"] = content
            if data:
                kwargs["file"] = discord.File(io.BytesIO(data), filename=filename)
            if view is not None:
                kwargs["view"] = view
            if isinstance(ch, discord.ForumChannel):
                if thread_key:
                    tid = await kv_get(thread_key)
                    if tid and tid.isdigit():
                        try:
                            thread = self.bot.get_channel(int(tid)) or await self.bot.fetch_channel(int(tid))
                            await thread.send(**kwargs)
                            return thread.mention
                        except discord.NotFound:
                            pass
                if "content" not in kwargs and "file" not in kwargs:
                    kwargs["content"] = thread_name or "Post"
                created = await ch.create_thread(name=(thread_name or "Post")[:100], **kwargs)
                if thread_key:
                    await kv_set(thread_key, created.thread.id)
                return created.thread.mention
            await ch.send(**kwargs)
            return ch.mention
        except (discord.HTTPException, discord.Forbidden) as e:
            log.warning("Could not post in %s (%s): %s", env_name, cid, e)
            return False

    async def post_or_here(self, interaction, env_name, **kwargs):
        """Posts in the configured channel; if there is none (tests) or it fails, in the current channel."""
        posted = await self.post_to(env_name, **kwargs)
        if posted:
            return posted
        send = dict(allowed_mentions=kwargs.get("mentions") or NO_MENTIONS)
        if kwargs.get("content"):
            send["content"] = kwargs["content"]
        if kwargs.get("data"):
            send["file"] = discord.File(io.BytesIO(kwargs["data"]), filename=kwargs.get("filename", "card.png"))
        if kwargs.get("view") is not None:
            send["view"] = kwargs["view"]
        if interaction.response.is_done():
            await interaction.followup.send(**send)
        else:
            await interaction.response.send_message(**send)
        return None if posted is None else False

    # ----- days in guild (veteran at 180) -----

    async def days_info(self, uuid):
        row = await q_one("SELECT banked FROM player_days WHERE uuid = ?", (uuid,))
        banked = row["banked"] if row else 0.0
        if await q_one("SELECT id FROM bl_entries WHERE uuid = ? AND status = 'active'", (uuid,)):
            return banked, "breaklist"
        joined = await self.joined_for(uuid)
        if joined:
            return banked + max(0.0, (now_utc() - joined).total_seconds() / 86400), "guild"
        return banked, "unknown"

    # ----- breaklist -----

    async def do_breaklist(self, uuid, name, *, reason, added_by, start=None, end=None, gexp=None,
                           days=None, debt=None):
        """Adds a player to the breaklist. Returns {'error': text} or {'entry': row, 'count': n}."""
        if await q_one("SELECT id FROM bl_entries WHERE uuid = ? AND status = 'active'", (uuid,)):
            return {"error": f"**{esc(name)}** is already on the breaklist. Use `/breaklist remove` first."}
        await self.guild_members()
        end = end or now_utc()
        joined = start or await self.joined_for(uuid)
        ws = gc.week_start(end)
        stay_start = max(joined, ws) if joined else ws
        required = gc.required_elapsed(stay_start, end)
        if gexp is None:
            total, count = await self.window_sum(uuid, stay_start, end)
            if count == 0:
                return {"error": "I have no saved GEXP for this player this week. Give it with the `gexp` option."}
            gexp = total
        prev = await q_one("SELECT banked FROM player_days WHERE uuid = ?", (uuid,))
        banked_old = prev["banked"] if prev else 0.0
        if days is None:
            days = banked_old + (max(0.0, (end - joined).total_seconds() / 86400) if joined else 0.0)
        if debt is None:
            row = await q_one("SELECT amount FROM debts WHERE uuid = ?", (uuid,))
            debt = row["amount"] if row else 0
        owed_week = max(0, required - gexp)
        total_debt = debt + owed_week
        stamp = iso(now_utc())
        entry_id = await q_run(
            "INSERT INTO bl_entries (uuid, ign, added_at, added_by, joined_at, left_at, days_total, gexp, required, "
            "owed, reason, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')",
            (uuid, name, stamp, added_by, iso(joined) if joined else None, iso(end), days, gexp, required,
             total_debt, reason or None))
        await q_run("INSERT INTO player_days (uuid, ign, banked, updated_at) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(uuid) DO UPDATE SET ign = excluded.ign, banked = excluded.banked, "
                    "updated_at = excluded.updated_at", (uuid, name, days, stamp))
        await q_run("INSERT INTO debts (uuid, ign, amount, updated_at) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(uuid) DO UPDATE SET ign = excluded.ign, amount = excluded.amount, "
                    "updated_at = excluded.updated_at", (uuid, name, total_debt, stamp))
        entry = await q_one("SELECT * FROM bl_entries WHERE id = ?", (entry_id,))
        count = (await q_one("SELECT COUNT(*) AS c FROM bl_entries WHERE uuid = ?", (uuid,)))["c"]
        return {"entry": entry, "count": count}

    async def breaklist_card(self, entry, title, count=None):
        if count is None:
            count = (await q_one("SELECT COUNT(*) AS c FROM bl_entries WHERE uuid = ?", (entry["uuid"],)))["c"]
        head = await self.head_for(entry["uuid"], entry["ign"])
        return await asyncio.to_thread(draw_breaklist_card, entry, head, title, count)

    # ----- excuses -----

    async def add_excuse(self, uuid, name, *, weeks=1, reason=None, added_by=None):
        ws = gc.week_start(now_utc())
        stamp = iso(now_utc())
        keys = []
        for i in range(weeks):
            key = (ws + timedelta(days=7 * i)).date().isoformat()
            await q_run("INSERT INTO excuses (uuid, week, ign, reason, added_by, added_at) VALUES (?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(uuid, week) DO UPDATE SET reason = excluded.reason, added_by = excluded.added_by, "
                        "added_at = excluded.added_at, ign = excluded.ign",
                        (uuid, key, name, reason, added_by, stamp))
            keys.append(key)
        until = gc.week_end(ws + timedelta(days=7 * (weeks - 1)))
        return until

    async def excused_now(self):
        key = gc.week_start(now_utc()).date().isoformat()
        rows = await q_all("SELECT uuid FROM excuses WHERE week = ?", (key,))
        return {r["uuid"] for r in rows}

    # ----- inactivity notices -----

    async def notice_item(self, uuid, kind, members=None):
        """Numbers for one player's notice, or None when there is no data."""
        members = members if members is not None else self._members
        m = members.get(uuid)
        history = {d: int(v) for d, v in (m.get("expHistory") or {}).items()} if m else None
        if history is None:
            history, _src = await self.history_days(uuid)
            if not history:
                return None
        now = now_utc()
        ws = gc.week_start(now)
        purge = gc.week_end(now)
        weekly = gc.weekly_sum(history, ws)
        joined = await self.joined_for(uuid)
        required = gc.required_from(max(joined, ws)) if joined and joined > ws else REQ
        seconds_left = max(0, gc.secs(now, purge))
        needed = max(0, required - weekly)
        per_day = math.ceil(needed / max(seconds_left / 86400, 1 / 24)) if needed else 0
        row = await q_one("SELECT amount FROM debts WHERE uuid = ?", (uuid,))
        return {"uuid": uuid, "kind": kind, "weekly": weekly, "required": required, "needed": needed,
                "per_day": per_day, "seconds_left": seconds_left, "purge": purge, "joined": joined,
                "debt": row["amount"] if row and row["amount"] > 0 else 0}

    def notice_view(self, uuid):
        view = discord.ui.View(timeout=None)
        view.add_item(NoticeButton("bl", uuid))
        view.add_item(NoticeButton("ex", uuid))
        return view

    async def send_notice(self, it, interaction=None):
        name = await self.name_for(it["uuid"])
        it = dict(it, ign=name)
        head = await self.head_for(it["uuid"], name)
        data = await asyncio.to_thread(draw_notice, it, head)
        did = await self.linked_discord(it["uuid"])
        who = f"<@{did}>" if did else f"**{esc(name)}**"
        label = "Final reminder" if it["kind"] == "final" else "Reminder"
        content = f"{who} {label}: you need {n(it['needed'])} more GEXP this week."
        kwargs = dict(content=content, data=data, filename="notice.png", view=self.notice_view(it["uuid"]),
                      mentions=USER_MENTIONS)
        if interaction is not None:
            return await self.post_or_here(interaction, "INACTIVITY_CHANNEL_ID", **kwargs)
        return await self.post_to("INACTIVITY_CHANNEL_ID", **kwargs)

    async def run_notices(self, kind):
        """The automatic notices. Returns how many were sent."""
        if chan_id("INACTIVITY_CHANNEL_ID") is None:
            log.info("INACTIVITY_CHANNEL_ID is not set: the %s notices were skipped", kind)
            return 0
        members = await self.guild_members(force=True)
        if not members:
            log.warning("No guild data: the %s notices were skipped", kind)
            return 0
        now = now_utc()
        excused = await self.excused_now()
        on_bl = {r["uuid"] for r in await q_all("SELECT uuid FROM bl_entries WHERE status = 'active'")}
        sent = 0
        for uuid, m in members.items():
            if uuid in excused or uuid in on_bl:
                continue
            joined = await self.joined_for(uuid)
            if kind == "friday" and joined and now - joined < timedelta(days=7):
                continue
            it = await self.notice_item(uuid, kind, members)
            if it is None:
                continue
            if kind == "friday" and it["weekly"] >= FRIDAY_MAX_GEXP:
                continue
            if kind == "final" and it["weekly"] >= it["required"]:
                continue
            try:
                await self.send_notice(it)
                sent += 1
            except Exception:
                log.exception("Could not send the notice of %s", uuid)
            await asyncio.sleep(1.5)
        log.info("%d %s notices sent", sent, kind)
        return sent

    # ----- Road to #N -----

    async def road_status(self):
        """Returns (data, error). data = {'ours', 'targets', 'series', 'title', 'text'}."""
        names = [x.strip() for x in _env("ROAD_GUILDS").split(",") if x.strip()]
        if not names:
            return None, "ROAD_GUILDS is not set (the names of the guilds ranked above you, comma separated)."
        ours_guild = await api_guild({"name": HYPIXEL_GUILD_NAME})
        if not ours_guild:
            return None, "I couldn't read our guild from Hypixel (check the API key and HYPIXEL_GUILD_NAME)."
        results = await asyncio.gather(*(api_guild({"name": nm}) for nm in names))
        others = [(g_["name"], int(g_.get("exp", 0))) for g_ in results if g_]
        missing = [nm for nm, r in zip(names, results) if not r]
        our_exp = int(ours_guild.get("exp", 0))
        our_name = ours_guild["name"]

        day = datetime.now(gc.ET).date().isoformat()
        for nm, exp in others + [(our_name, our_exp)]:
            await q_run("INSERT INTO road_history (day, guild, exp) VALUES (?, ?, ?) "
                        "ON CONFLICT(day, guild) DO UPDATE SET exp = excluded.exp", (day, nm, exp))

        above = sorted([o for o in others if o[1] > our_exp], key=lambda o: o[1])
        rank = len(above) + 1
        if not above:
            return None, "No guild in ROAD_GUILDS is above us: we are #1 of the list!"
        targets = []
        hist = await q_all("SELECT day, guild, exp FROM road_history WHERE day >= ? ORDER BY day",
                           ((datetime.now(gc.ET) - timedelta(days=14)).date().isoformat(),))
        by_guild = {}
        for r in hist:
            by_guild.setdefault(r["guild"], {})[r["day"]] = r["exp"]

        def rate(guild):
            days_ = sorted(by_guild.get(guild, {}))
            if len(days_) < 2:
                return None
            a, b = days_[max(0, len(days_) - 8)], days_[-1]
            span = (datetime.fromisoformat(b) - datetime.fromisoformat(a)).days
            return max(0, (by_guild[guild][b] - by_guild[guild][a]) // span) if span else None

        rate_us = rate(our_name)
        series = {}
        for i, (nm, exp) in enumerate(above[:3]):
            gap = exp - our_exp
            rate_them = rate(nm)
            eta = None
            if rate_us is not None and rate_them is not None and rate_us > rate_them:
                eta = gap / (rate_us - rate_them)
            targets.append({"rank": rank - 1 - i, "name": nm, "exp": exp, "gap": gap, "eta": eta,
                            "rate_us": rate_us, "rate_them": rate_them})
            pts = []
            for dd, ours_exp in sorted(by_guild.get(our_name, {}).items()):
                if dd in by_guild.get(nm, {}):
                    pts.append((dd, by_guild[nm][dd] - ours_exp))
            series[nm] = pts
        title = f"ROAD TO #{rank - 1} GUILD"
        text = "\n".join(f"{n(t['gap'])} gexp from #{t['rank']} guild" for t in targets)
        if missing:
            text += "\n(Not found on Hypixel: " + ", ".join(missing) + ")"
        return {"ours": {"name": our_name, "exp": our_exp, "rank": rank}, "targets": targets,
                "series": series, "title": title, "text": text}, None

    async def road_card(self, data):
        return await asyncio.to_thread(draw_road, data["ours"], data["targets"], data["series"], data["title"])

    async def post_road(self):
        if chan_id("ROAD_CHANNEL_ID") is None:
            log.info("ROAD_CHANNEL_ID is not set: the Road post was skipped")
            return False
        data, err = await self.road_status()
        if err:
            log.warning("Road post skipped: %s", err)
            return False
        card = await self.road_card(data)
        top = data["targets"][0]["rank"]
        return bool(await self.post_to("ROAD_CHANNEL_ID", content=data["text"], data=card, filename="road.png",
                                       thread_name=f"Road to #{top} guild", thread_key="road_thread"))


# ======================================================================================
# the Breaklist Me / Excuse Me buttons (they keep working after a restart)
# ======================================================================================

class NoticeButton(discord.ui.DynamicItem[discord.ui.Button], template=r"np:(?P<act>bl|ex):(?P<uuid>[0-9a-f]{32})"):
    def __init__(self, act: str, uuid: str):
        super().__init__(discord.ui.Button(
            label="Breaklist Me" if act == "bl" else "Excuse Me",
            style=discord.ButtonStyle.danger if act == "bl" else discord.ButtonStyle.secondary,
            custom_id=f"np:{act}:{uuid}"))
        self.act = act
        self.uuid = uuid

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match, /):
        return cls(match["act"], match["uuid"])

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        core: Core = interaction.client.np_core
        if is_guild_staff(interaction.user):
            return True
        if await core.linked_discord(self.uuid) == interaction.user.id:
            return True
        await interaction.response.send_message(
            "Only the staff and the player this notice is for can use these buttons "
            "(the player must have linked their account with `/link`).", ephemeral=True)
        return False

    async def callback(self, interaction: discord.Interaction):
        core: Core = interaction.client.np_core
        await interaction.response.defer(ephemeral=True)
        name = await core.name_for(self.uuid)
        by = f"{interaction.user} ({interaction.user.id})"
        if self.act == "bl":
            res = await core.do_breaklist(self.uuid, name, reason="Requested with the notice button",
                                          added_by=str(interaction.user.id))
            if "error" in res:
                await interaction.followup.send(res["error"], ephemeral=True)
                return
            card = await core.breaklist_card(res["entry"], "BREAKLISTED", res["count"])
            posted = await core.post_to("BREAKLIST_CHANNEL_ID", content=f"✅ **{esc(name)}** was added to the breaklist.",
                                        data=card, filename="breaklist.png", thread_name=name)
            if not posted:
                await interaction.channel.send(content=f"✅ **{esc(name)}** was added to the breaklist.",
                                               file=discord.File(io.BytesIO(card), filename="breaklist.png"),
                                               allowed_mentions=NO_MENTIONS)
            msg = f"You were added to the breaklist" + (f" (posted in {posted})." if posted else ".")
        else:
            until = await core.add_excuse(self.uuid, name, weeks=1, reason="Requested with the notice button",
                                          added_by=by)
            text = f"✅ **{esc(name)}** has been excused until the purge on {gc.fmt_et(until)}."
            posted = await core.post_to("EXCUSE_CHANNEL_ID", content=text)
            if not posted:
                await interaction.channel.send(content=text, allowed_mentions=NO_MENTIONS)
            msg = "You were excused for this week."
        try:
            await interaction.message.edit(view=None)
        except discord.HTTPException:
            pass
        await interaction.followup.send(msg, ephemeral=True)


# ======================================================================================
# commands
# ======================================================================================

async def lookup(interaction: discord.Interaction, ign: str):
    try:
        result = await get_uuid(ign)
    except (RuntimeError, aiohttp.ClientError, asyncio.TimeoutError):
        await interaction.followup.send("Mojang is not responding right now. Please try again in a moment.")
        return None
    if result is None:
        await interaction.followup.send(
            f"`{ign}` is not a valid Minecraft username. Check the spelling "
            f"(3-16 characters: letters, numbers and underscores).")
        return None
    return result


def send_file(data, name):
    return discord.File(io.BytesIO(data), filename=name)


class Breaklist(commands.GroupCog, name="breaklist", description="Guild breaklist"):
    def __init__(self, core: Core):
        self.core = core
        super().__init__()

    @app_commands.command(name="add", description="Add a player to the breaklist and post the notice (guild staff)")
    @app_commands.describe(
        ign="Minecraft username of the player",
        reason="Why the player is being breaklisted (everyone can read it)",
        start="When they joined the guild (Eastern Time): 10/03/2026 or 10/03/2026 6:00 PM. Default: from Hypixel",
        end="When they were breaklisted (Eastern Time). Default: now",
        gexp="GEXP earned this week before the breaklist. Default: taken from the saved Hypixel data",
        days="Total days the player has spent in the guild (use it for players breaklisted before the bot)",
        debt="Extra GEXP debt from previous weeks. Default: the saved debt")
    @app_commands.check(staff_check)
    async def add(self, interaction: discord.Interaction, ign: str, reason: Optional[str] = None,
                  start: Optional[str] = None, end: Optional[str] = None,
                  gexp: Optional[app_commands.Range[int, 0, 100_000_000]] = None,
                  days: Optional[app_commands.Range[float, 0, 5000]] = None,
                  debt: Optional[app_commands.Range[int, 0, 100_000_000]] = None):
        await interaction.response.defer()
        r = await lookup(interaction, ign)
        if r is None:
            return
        uuid, name = r
        start_dt = end_dt = None
        if start:
            start_dt = gc.parse_et(start)
            if start_dt is None:
                await interaction.followup.send("I couldn't read the start date. Use `10/03/2026` or `10/03/2026 6:00 PM` (Eastern Time).")
                return
        if end:
            end_dt = gc.parse_et(end)
            if end_dt is None:
                await interaction.followup.send("I couldn't read the end date. Use `10/03/2026` or `10/03/2026 6:00 PM` (Eastern Time).")
                return
        if start_dt and end_dt and end_dt <= start_dt:
            await interaction.followup.send("The end date must be after the start date.")
            return
        res = await self.core.do_breaklist(uuid, name, reason=reason, added_by=str(interaction.user.id),
                                           start=start_dt, end=end_dt, gexp=gexp, days=days, debt=debt)
        if "error" in res:
            await interaction.followup.send(res["error"])
            return
        card = await self.core.breaklist_card(res["entry"], "BREAKLISTED", res["count"])
        posted = await self.core.post_to("BREAKLIST_CHANNEL_ID", content=f"✅ **{esc(name)}** was added to the breaklist.",
                                         data=card, filename="breaklist.png", thread_name=name)
        if posted:
            await interaction.followup.send(f"✅ **{esc(name)}** was added to the breaklist. Notice posted in {posted}.")
        else:
            note = "" if posted is None else "\n(I couldn't post in the breaklist channel, check its id and my permissions.)"
            await interaction.followup.send(content=f"✅ **{esc(name)}** was added to the breaklist.{note}",
                                            file=send_file(card, "breaklist.png"))

    @app_commands.command(name="view", description="Show a player's latest breaklist entry, reason and days in guild")
    @app_commands.describe(ign="Minecraft username of the player")
    async def view(self, interaction: discord.Interaction, ign: str):
        await interaction.response.defer()
        r = await lookup(interaction, ign)
        if r is None:
            return
        uuid, name = r
        entry = await q_one("SELECT * FROM bl_entries WHERE uuid = ? ORDER BY id DESC LIMIT 1", (uuid,))
        if entry is None:
            days, state = await self.core.days_info(uuid)
            extra = f" They have {int(days)} days in the guild." if state != "unknown" and days else ""
            await interaction.followup.send(f"**{esc(name)}** has never been on the breaklist.{extra}")
            return
        if entry["status"] == "active":
            days, _ = await self.core.days_info(uuid)
            entry = dict(entry, days_total=days)
        else:
            days, state = await self.core.days_info(uuid)
            entry = dict(entry, days_total=days)     # current total (days keep growing after they return)
        card = await self.core.breaklist_card(entry, "BREAKLIST ENTRY")
        await interaction.followup.send(file=send_file(card, "breaklist.png"))

    @app_commands.command(name="history", description="Every time a player was on the breaklist: reason and length of the break")
    @app_commands.describe(ign="Minecraft username of the player", page="Page number")
    async def history(self, interaction: discord.Interaction, ign: str, page: app_commands.Range[int, 1, 100] = 1):
        await interaction.response.defer()
        r = await lookup(interaction, ign)
        if r is None:
            return
        uuid, name = r
        rows = await q_all("SELECT * FROM bl_entries WHERE uuid = ? ORDER BY id DESC", (uuid,))
        if not rows:
            await interaction.followup.send(f"**{esc(name)}** has never been on the breaklist.")
            return
        per = 4
        pages = max(1, math.ceil(len(rows) / per))
        page = min(page, pages)
        chunk = rows[(page - 1) * per: page * per]
        head = await self.core.head_for(uuid, name)
        card = await asyncio.to_thread(draw_history, name, head, chunk, len(rows), page, pages)
        await interaction.followup.send(file=send_file(card, "history.png"))

    @app_commands.command(name="guild", description="List everyone who is on the breaklist")
    @app_commands.describe(page="Page number")
    async def list_guild(self, interaction: discord.Interaction, page: app_commands.Range[int, 1, 100] = 1):
        await interaction.response.defer()
        rows = await q_all("SELECT * FROM bl_entries WHERE status = 'active' ORDER BY added_at DESC")
        if not rows:
            await interaction.followup.send("Nobody is on the breaklist.")
            return
        per = 8
        pages = max(1, math.ceil(len(rows) / per))
        page = min(page, pages)
        chunk = rows[(page - 1) * per: page * per]
        heads = await g._fetch_heads([(e["uuid"], e["ign"]) for e in chunk])
        card = await asyncio.to_thread(draw_breaklist_list, chunk, heads, page, pages, len(rows))
        await interaction.followup.send(file=send_file(card, "breaklist_guild.png"))

    @app_commands.command(name="remove", description="Take a player off the breaklist when they rejoin (guild staff)")
    @app_commands.describe(ign="Minecraft username of the player",
                           when="When they rejoined (Eastern Time). Default: now")
    @app_commands.check(staff_check)
    async def remove(self, interaction: discord.Interaction, ign: str, when: Optional[str] = None):
        await interaction.response.defer()
        r = await lookup(interaction, ign)
        if r is None:
            return
        uuid, name = r
        entry = await q_one("SELECT id FROM bl_entries WHERE uuid = ? AND status = 'active'", (uuid,))
        if entry is None:
            await interaction.followup.send(f"**{esc(name)}** is not on the breaklist.")
            return
        back = gc.parse_et(when) if when else now_utc()
        if back is None:
            await interaction.followup.send("I couldn't read the date. Use `10/03/2026` or `10/03/2026 6:00 PM` (Eastern Time).")
            return
        await q_run("UPDATE bl_entries SET status = 'returned', returned_at = ? WHERE id = ?", (iso(back), entry["id"]))
        days, _ = await self.core.days_info(uuid)
        debt = await q_one("SELECT amount FROM debts WHERE uuid = ?", (uuid,))
        owed = debt["amount"] if debt else 0
        await interaction.followup.send(
            f"✅ **{esc(name)}** is off the breaklist. Their {int(days)} days in the guild are kept and keep growing "
            f"from now on. Extra GEXP debt carried over: **{n(owed)}**. "
            f"Use `/gexp missing` to calculate what they need to earn this week.")

    @app_commands.command(name="setdays", description="Set the days a player has already spent in the guild (guild staff)")
    @app_commands.describe(ign="Minecraft username of the player",
                           days="Days banked: for someone on the breaklist, their total days in guild; "
                                "for someone in the guild, the days from their previous stays")
    @app_commands.check(staff_check)
    async def setdays(self, interaction: discord.Interaction, ign: str, days: app_commands.Range[float, 0, 5000]):
        await interaction.response.defer()
        r = await lookup(interaction, ign)
        if r is None:
            return
        uuid, name = r
        await q_run("INSERT INTO player_days (uuid, ign, banked, updated_at) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(uuid) DO UPDATE SET ign = excluded.ign, banked = excluded.banked, "
                    "updated_at = excluded.updated_at", (uuid, name, days, iso(now_utc())))
        await q_run("UPDATE bl_entries SET days_total = ? WHERE uuid = ? AND status = 'active'", (days, uuid))
        total, state = await self.core.days_info(uuid)
        await interaction.followup.send(
            f"✅ **{esc(name)}**: {days:g} days banked. Total days in guild now: **{int(total)}** "
            f"(veteran at {VETERAN_DAYS}).")

    @app_commands.command(name="debt", description="Set a player's extra GEXP debt by hand (guild staff)")
    @app_commands.describe(ign="Minecraft username of the player", amount="Extra GEXP owed (0 clears the debt)")
    @app_commands.check(staff_check)
    async def debt(self, interaction: discord.Interaction, ign: str, amount: app_commands.Range[int, 0, 100_000_000]):
        await interaction.response.defer()
        r = await lookup(interaction, ign)
        if r is None:
            return
        uuid, name = r
        await q_run("INSERT INTO debts (uuid, ign, amount, updated_at) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(uuid) DO UPDATE SET ign = excluded.ign, amount = excluded.amount, "
                    "updated_at = excluded.updated_at", (uuid, name, amount, iso(now_utc())))
        await q_run("UPDATE bl_entries SET owed = ? WHERE uuid = ? AND status = 'active'", (amount, uuid))
        await interaction.followup.send(f"✅ **{esc(name)}** owes **{n(amount)}** extra GEXP.")

    @app_commands.command(name="eligibility", description="The last 7 days of GEXP and whether a player meets the requirement")
    @app_commands.describe(ign="Minecraft username of the player")
    async def eligibility(self, interaction: discord.Interaction, ign: str):
        await interaction.response.defer()
        r = await lookup(interaction, ign)
        if r is None:
            return
        uuid, name = r
        await self.core.guild_members()
        history, source = await self.core.history_days(uuid)
        if not history:
            await interaction.followup.send(
                "I have no GEXP data for this player. The bot needs the Hypixel key and the guild name "
                "(`HYPIXEL_GUILD_NAME`), and the player must have been in the guild while the bot was saving data.")
            return
        days = sorted(history.items())[-7:]
        total = sum(v for _, v in days)
        debt = await q_one("SELECT amount FROM debts WHERE uuid = ?", (uuid,))
        owed = debt["amount"] if debt and debt["amount"] > 0 else None
        head = await self.core.head_for(uuid, name)
        card = await asyncio.to_thread(draw_eligibility, name, head, days, total, owed, source)
        await interaction.followup.send(file=send_file(card, "gexp.png"))

    @app_commands.command(name="notice", description="Post the GEXP notice of one player now, with its buttons (guild staff)")
    @app_commands.describe(ign="Minecraft username of the player",
                           kind="friday = the under-20k reminder, final = the last reminder before the purge")
    @app_commands.choices(kind=[app_commands.Choice(name="Friday reminder", value="friday"),
                                app_commands.Choice(name="Final reminder", value="final")])
    @app_commands.check(staff_check)
    async def notice(self, interaction: discord.Interaction, ign: str,
                     kind: Optional[app_commands.Choice[str]] = None):
        await interaction.response.defer()
        r = await lookup(interaction, ign)
        if r is None:
            return
        uuid, name = r
        await self.core.guild_members(force=True)
        it = await self.core.notice_item(uuid, kind.value if kind else "friday")
        if it is None:
            await interaction.followup.send("I have no GEXP data for this player (is the Hypixel key set?).")
            return
        posted = await self.core.send_notice(it, interaction)
        if posted:
            await interaction.followup.send(f"Notice posted in {posted}.")
        elif posted is False:
            await interaction.followup.send("I couldn't post in the inactivity channel (check its id and my permissions).")


class Excuse(commands.GroupCog, name="excuse", description="Excuse players from the GEXP requirement"):
    def __init__(self, core: Core):
        self.core = core
        super().__init__()

    @app_commands.command(name="add", description="Excuse a player: they won't be kicked or breaklisted this week (guild staff)")
    @app_commands.describe(ign="Minecraft username of the player", weeks="How many weeks (default 1)",
                           reason="Why they are excused")
    @app_commands.check(staff_check)
    async def add(self, interaction: discord.Interaction, ign: str,
                  weeks: app_commands.Range[int, 1, 12] = 1, reason: Optional[str] = None):
        await interaction.response.defer()
        r = await lookup(interaction, ign)
        if r is None:
            return
        uuid, name = r
        until = await self.core.add_excuse(uuid, name, weeks=weeks, reason=reason,
                                           added_by=f"{interaction.user} ({interaction.user.id})")
        text = (f"✅ **{esc(name)}** has been excused until the purge on {gc.fmt_et(until)}."
                + (f" Reason: {reason}" if reason else ""))
        posted = await self.core.post_to("EXCUSE_CHANNEL_ID", content=text)
        if posted:
            await interaction.followup.send(f"✅ **{esc(name)}** was excused. Posted in {posted}.")
        else:
            await interaction.followup.send(text, allowed_mentions=NO_MENTIONS)

    @app_commands.command(name="remove", description="Remove this week's excuse of a player (guild staff)")
    @app_commands.describe(ign="Minecraft username of the player")
    @app_commands.check(staff_check)
    async def remove(self, interaction: discord.Interaction, ign: str):
        await interaction.response.defer()
        r = await lookup(interaction, ign)
        if r is None:
            return
        uuid, name = r
        key = gc.week_start(now_utc()).date().isoformat()
        row = await q_one("SELECT week FROM excuses WHERE uuid = ? AND week >= ?", (uuid, key))
        if row is None:
            await interaction.followup.send(f"**{esc(name)}** has no current excuse.")
            return
        await q_run("DELETE FROM excuses WHERE uuid = ? AND week >= ?", (uuid, key))
        await interaction.followup.send(f"✅ The excuse of **{esc(name)}** was removed.")

    @app_commands.command(name="list", description="Players excused this week or later (guild staff)")
    @app_commands.check(staff_check)
    async def list_excuses(self, interaction: discord.Interaction):
        await interaction.response.defer()
        key = gc.week_start(now_utc()).date().isoformat()
        rows = await q_all("SELECT * FROM excuses WHERE week >= ? ORDER BY ign COLLATE NOCASE, week", (key,))
        if not rows:
            await interaction.followup.send("Nobody is excused.")
            return
        by = {}
        for r in rows:
            by.setdefault(r["ign"], []).append(r)
        lines = []
        for ign, items in list(by.items())[:40]:
            last = gc.week_end(datetime.fromisoformat(items[-1]["week"]).replace(tzinfo=gc.ET))
            why = items[0]["reason"]
            lines.append(f"**{esc(ign)}**: until {gc.fmt_date(last)}" + (f" ({why})" if why else ""))
        await interaction.followup.send("**Excused players**\n" + "\n".join(lines), allowed_mentions=NO_MENTIONS)


class GexpTools(commands.GroupCog, name="gexp", description="GEXP requirement tools"):
    def __init__(self, core: Core):
        self.core = core
        super().__init__()

    @app_commands.command(name="missing", description="How much GEXP a player must earn this week, extra debt included (guild staff)")
    @app_commands.describe(
        ign="Minecraft username of the player",
        joined="When they joined or rejoined, Eastern Time: 10/21/2026 6:00 PM",
        start_gexp="The GEXP they had at that moment (default 0)",
        current_gexp="Their GEXP now (default: taken from Hypixel)",
        debt="Extra GEXP debt (default: the saved debt)",
        save="Save the remaining debt as the player's new debt")
    @app_commands.check(staff_check)
    async def missing(self, interaction: discord.Interaction, ign: str, joined: str,
                      start_gexp: app_commands.Range[int, 0, 100_000_000] = 0,
                      current_gexp: Optional[app_commands.Range[int, 0, 100_000_000]] = None,
                      debt: Optional[app_commands.Range[int, 0, 100_000_000]] = None,
                      save: bool = False):
        await interaction.response.defer()
        join_dt = gc.parse_et(joined)
        if join_dt is None:
            await interaction.followup.send(
                "I couldn't read the join time. Use month/day/year and the time in Eastern Time, "
                "for example `10/21/2026 6:00 PM` or `10/21/2026 18:00`.")
            return
        r = await lookup(interaction, ign)
        if r is None:
            return
        uuid, name = r
        now = now_utc()
        if join_dt.astimezone(UTC) > now:
            await interaction.followup.send("The join time is in the future.")
            return
        await self.core.guild_members()
        note = None
        if current_gexp is None:
            history, _src = await self.core.history_days(uuid)
            if not history:
                await interaction.followup.send(
                    "I can't read this player's current GEXP from Hypixel. Give it with `current_gexp`.")
                return
            current_gexp = gc.weekly_sum(history, gc.week_start(join_dt))
            note = "Current GEXP was read from Hypixel (the player's GEXP since the start of the week)."
        if debt is None:
            row = await q_one("SELECT amount FROM debts WHERE uuid = ?", (uuid,))
            debt = row["amount"] if row else 0
        normal = gc.required_from(join_dt)
        res = gc.settle_debt(normal, debt, current_gexp - start_gexp)
        purge = gc.week_end(join_dt)
        if save:
            if not is_guild_staff(interaction.user):
                save = False
            else:
                await q_run("INSERT INTO debts (uuid, ign, amount, updated_at) VALUES (?, ?, ?, ?) "
                            "ON CONFLICT(uuid) DO UPDATE SET ign = excluded.ign, amount = excluded.amount, "
                            "updated_at = excluded.updated_at", (uuid, name, res["remaining_debt"], iso(now)))
                note = ((note + " ") if note else "") + f"Saved: remaining debt is now {n(res['remaining_debt'])} GEXP."
        head = await self.core.head_for(uuid, name)
        card = await asyncio.to_thread(draw_missing, name, head, join_dt, purge, res, start_gexp, current_gexp, note)
        await interaction.followup.send(file=send_file(card, "gexp_missing.png"))


class RoadCog(commands.Cog):
    def __init__(self, core: Core):
        self.core = core

    @app_commands.command(name="road", description="How much GEXP we need to pass the guilds above us")
    async def road(self, interaction: discord.Interaction):
        await interaction.response.defer()
        data, err = await self.core.road_status()
        if err:
            await interaction.followup.send(err)
            return
        card = await self.core.road_card(data)
        await interaction.followup.send(content=data["text"], file=send_file(card, "road.png"))

    @app_commands.command(name="test", description="Send a test message to every channel set in the variables (administrators)")
    async def test(self, interaction: discord.Interaction):
        if not (isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.administrator):
            await interaction.response.send_message("Only server administrators can use this command.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        names = [
            ("BREAKLIST_CHANNEL_ID", "Breaklist forum"),
            ("INACTIVITY_CHANNEL_ID", "Inactivity notices"),
            ("EXCUSE_CHANNEL_ID", "Excuse list"),
            ("ROAD_CHANNEL_ID", "Road to #6 forum"),
            ("LOGS_1V1_CHANNEL_ID", "1v1 logs"),
        ]
        lines = []
        for env_name, label in names:
            cid = chan_id(env_name)
            if cid is None:
                lines.append(f"- {label} (`{env_name}`): NOT SET (messages go in the channel where the command is used)")
                continue
            ch = self.core.bot.get_channel(cid)
            if ch is None:
                try:
                    ch = await self.core.bot.fetch_channel(cid)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    lines.append(f"- {label} (`{env_name}`): FAILED, channel {cid} not found or the bot cannot see it (wrong ID, or the bot is not in that server)")
                    continue
            res = await self.core.post_to(
                env_name, content=f"Channel test from {interaction.user.display_name}: {label}. You can delete this.",
                thread_name="Channel test (delete me)")
            if res:
                lines.append(f"- {label} (`{env_name}`): OK -> {res}")
            else:
                lines.append(f"- {label} (`{env_name}`): FAILED, the bot sees the channel but cannot post. Give it View Channel, Send Messages, Attach Files "
                             "(forum: also Create Posts and Send Messages in Posts)")
        await interaction.followup.send("**Channel test**\n" + "\n".join(lines), ephemeral=True)


# ======================================================================================
# schedule: notices, Road to #N, GEXP snapshot, automatic backup
# ======================================================================================

class Scheduler(commands.Cog):
    def __init__(self, core: Core):
        self.core = core
        self.loop.start()

    def cog_unload(self):
        self.loop.cancel()

    async def _due(self, key: str, when: datetime, window_hours: float) -> bool:
        """True once per key, when 'when' has passed (and not more than window_hours ago)."""
        now = now_utc()
        if not (when.astimezone(UTC) <= now < when.astimezone(UTC) + timedelta(hours=window_hours)):
            return False
        if await kv_get(key):
            return False
        await kv_set(key, iso(now))
        return True

    @tasks.loop(minutes=2)
    async def loop(self):
        now = now_utc()
        ws = gc.week_start(now)
        wkey = ws.date().isoformat()
        try:
            # Friday 12:00 AM Mountain Time
            friday = (ws + timedelta(days=3)).date()
            friday_mt = datetime(friday.year, friday.month, friday.day, 0, 0, tzinfo=gc.MT)
            if await self._due(f"notice_friday:{wkey}", friday_mt, 12):
                await self.core.run_notices("friday")
            # a few hours before the purge
            final = gc.week_end(now) - timedelta(hours=FINAL_HOURS_BEFORE_PURGE)
            if await self._due(f"notice_final:{wkey}", final, FINAL_HOURS_BEFORE_PURGE - 0.5):
                await self.core.run_notices("final")
        except Exception:
            log.exception("The inactivity notices failed")
        try:
            hh, mm = (int(x) for x in ROAD_POST_TIME.split(":"))
            today = datetime.now(gc.ET).date()
            road_et = datetime(today.year, today.month, today.day, hh, mm, tzinfo=gc.ET)
            if await self._due(f"road:{today.isoformat()}", road_et, 6):
                await self.core.post_road()
        except Exception:
            log.exception("The Road post failed")
        try:
            last = await kv_get("snapshot_at")
            if not last or (now - from_iso(last)).total_seconds() > 6 * 3600:
                await kv_set("snapshot_at", iso(now))
                members = await self.core.guild_members(force=True)
                log.info("GEXP snapshot saved for %d guild members", len(members))
            day = now.date().isoformat()
            if await self._due(f"backup:{day}", datetime(now.year, now.month, now.day, 4, 0, tzinfo=UTC), 20):
                path = await asyncio.to_thread(backup_db, "auto")
                log.info("Automatic backup saved: %s", path)
                await asyncio.to_thread(prune_auto_backups, AUTO_BACKUP_KEEP)
        except Exception:
            log.exception("The snapshot or the backup failed")

    @loop.before_loop
    async def _before(self):
        await self.core.bot.wait_until_ready()
        await asyncio.sleep(30)


def prune_auto_backups(keep: int):
    """Keeps only the newest 'keep' automatic backups (manual ones, from /endseason etc., are never deleted)."""
    root = os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "backups")
    files = []
    for folder, _dirs, names in os.walk(root):
        files += [os.path.join(folder, x) for x in names if x.endswith("_auto.db")]
    files.sort()
    for old in files[:-keep] if keep > 0 else []:
        try:
            os.remove(old)
        except OSError:
            pass


# ======================================================================================

async def setup(bot: commands.Bot):
    await setup_tables()
    core = Core(bot)
    bot.np_core = core
    bot.add_dynamic_items(NoticeButton)
    await bot.add_cog(Breaklist(core))
    await bot.add_cog(Excuse(core))
    await bot.add_cog(GexpTools(core))
    await bot.add_cog(RoadCog(core))
    await bot.add_cog(Scheduler(core))

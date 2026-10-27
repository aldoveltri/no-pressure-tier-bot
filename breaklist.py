"""Guild tools: breaklist, GEXP requirement / debt, excuses, automatic inactivity notices and Road to #6.

Everything here is independent from the 1v1 tier-testing part of the bot. Settings come from environment
variables (see README), so channels and roles can change without touching the code.
"""
import asyncio
import io
import logging
import math
import os
import re
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
ROAD_POST_TIME = _env("ROAD_POST_TIME_ET", "23:59")      # HH:MM, Eastern Time (11:59 PM: the day is complete)
AUTO_BACKUP_KEEP = _int_env("AUTO_BACKUP_KEEP", 14)
ROAD_START_RANK = _int_env("ROAD_START_RANK", 7)             # our guild rank today (the guilds in ROAD_GUILDS are above us)

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
CREATE TABLE IF NOT EXISTS bl_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid TEXT NOT NULL, ign TEXT, kind TEXT NOT NULL, reason TEXT, duration TEXT,
    guildless INTEGER, guild TEXT, weeks INTEGER DEFAULT 1,
    requester_id TEXT, status TEXT NOT NULL DEFAULT 'pending', decided_by TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS countries (
    uuid TEXT PRIMARY KEY, ign TEXT, code TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mutes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid TEXT NOT NULL, ign TEXT, reason TEXT, start_at TEXT NOT NULL, end_at TEXT,
    status TEXT NOT NULL DEFAULT 'active', added_by TEXT, ended_at TEXT
);
CREATE TABLE IF NOT EXISTS warnings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid TEXT NOT NULL, ign TEXT, reason TEXT, at TEXT NOT NULL, added_by TEXT, discord_id TEXT
);
CREATE TABLE IF NOT EXISTS day_sets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid TEXT NOT NULL, ign TEXT, days REAL NOT NULL, note TEXT, added_by TEXT, added_at TEXT NOT NULL,
    removed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS logins (
    uuid TEXT NOT NULL, ign TEXT, login_at TEXT NOT NULL, logout_at TEXT, PRIMARY KEY (uuid, login_at)
);
CREATE TABLE IF NOT EXISTS road_history (
    day TEXT NOT NULL, guild TEXT NOT NULL, exp INTEGER NOT NULL, at TEXT, PRIMARY KEY (day, guild)
);
CREATE TABLE IF NOT EXISTS guild_left (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid TEXT NOT NULL, ign TEXT, joined_at TEXT, left_at TEXT NOT NULL, detected_at TEXT, rejoined_at TEXT
);
"""


async def setup_tables():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        for table, col, kind in (("bl_requests", "channel_id", "TEXT"), ("bl_requests", "message_id", "TEXT"),
                                 ("road_history", "at", "TEXT"), ("guild_seen", "in_guild", "INTEGER DEFAULT 1"),
                                 ("mutes", "discord_id", "TEXT"), ("bl_entries", "ord", "INTEGER"),
                                 ("guild_seen", "login_polled_at", "TEXT"), ("guild_seen", "online", "INTEGER"), ("warnings", "expires_at", "TEXT")):
            try:
                await db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {kind}")
            except aiosqlite.OperationalError:
                pass                                   # the column already exists
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


async def api_player(uuid: str):
    """The 'player' object of the Hypixel API, None (no data) or False (rate limit / error: stop polling)."""
    key = _read_key()
    if not key:
        return False
    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get("https://api.hypixel.net/v2/player", params={"uuid": uuid},
                                   headers={"API-Key": key}) as r:
                if r.status != 200:
                    log.warning("Hypixel player API answered with status %s", r.status)
                    return False
                data = await r.json()
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        log.warning("Hypixel player API failed: %s", e)
        return False
    return data.get("player")


# ======================================================================================
# cards
# ======================================================================================

def _wrap_lines(text, font, width):
    tmp = ImageDraw.Draw(Image.new("RGB", (g.WIDTH, 10)))
    return g._wrap(tmp, text, font, width)


def _box(d, x, y, w, h, label, value, color=None, size=32, sub=None, sub_color=None, sub2=None):
    d.rounded_rectangle([x, y, x + w, y + h], radius=14, fill=g.ROW_BG)
    d.text((x + w // 2, y + 24), label, font=g.get_font(14, True), fill=g.MUTED, anchor="mm")
    if sub2:
        d.text((x + w // 2, y + 52), value, font=g.get_font(size, True), fill=color or g.TEXT, anchor="mm")
        d.text((x + w // 2, y + 80), sub, font=g.get_font(15, True), fill=sub_color or g.MUTED, anchor="mm")
        d.text((x + w // 2, y + 97), sub2, font=g.get_font(14, True), fill=g.MUTED, anchor="mm")
        return
    d.text((x + w // 2, y + (62 if sub else 70)), value, font=g.get_font(size, True),
           fill=color or g.TEXT, anchor="mm")
    if sub:
        d.text((x + w // 2, y + 92), sub, font=g.get_font(15, True), fill=sub_color or g.MUTED, anchor="mm")


def clock_text(dt):
    """'4:01 PM ET' in the time zone chosen with /timezone."""
    local = dt.astimezone(gc.TZ)
    return f"{(local.strftime('%I').lstrip('0') or '12')}:{local.strftime('%M %p')} {gc.TZ_NAME}"


def into_week(e):
    """How long into the GEXP week the player was breaklisted (since the week start, or since they joined)."""
    left = from_iso(e["left_at"])
    ws = gc.week_start(left)
    joined = from_iso(e.get("joined_at"))
    start = max(joined, ws) if joined else ws
    return gc.fmt_duration((left - start).total_seconds())


# ---------- country flags next to the player name ----------

COUNTRY_NAMES = {
    "argentina": "AR", "australia": "AU", "austria": "AT", "belgium": "BE", "bolivia": "BO", "brazil": "BR",
    "bulgaria": "BG", "canada": "CA", "chile": "CL", "china": "CN", "colombia": "CO", "croatia": "HR",
    "cuba": "CU", "cyprus": "CY", "czechia": "CZ", "czech republic": "CZ", "denmark": "DK",
    "dominican republic": "DO", "ecuador": "EC", "egypt": "EG", "estonia": "EE", "finland": "FI",
    "france": "FR", "germany": "DE", "greece": "GR", "hong kong": "HK", "hungary": "HU", "iceland": "IS",
    "india": "IN", "indonesia": "ID", "iran": "IR", "iraq": "IQ", "ireland": "IE", "israel": "IL",
    "italy": "IT", "japan": "JP", "jordan": "JO", "kazakhstan": "KZ", "kuwait": "KW", "latvia": "LV",
    "lebanon": "LB", "lithuania": "LT", "luxembourg": "LU", "malaysia": "MY", "malta": "MT", "mexico": "MX",
    "morocco": "MA", "netherlands": "NL", "holland": "NL", "new zealand": "NZ", "nigeria": "NG",
    "norway": "NO", "pakistan": "PK", "paraguay": "PY", "peru": "PE", "philippines": "PH", "poland": "PL",
    "portugal": "PT", "puerto rico": "PR", "qatar": "QA", "romania": "RO", "russia": "RU",
    "saudi arabia": "SA", "serbia": "RS", "singapore": "SG", "slovakia": "SK", "slovenia": "SI",
    "south africa": "ZA", "south korea": "KR", "korea": "KR", "spain": "ES", "sweden": "SE",
    "switzerland": "CH", "taiwan": "TW", "thailand": "TH", "tunisia": "TN", "turkey": "TR", "turkiye": "TR",
    "ukraine": "UA", "united arab emirates": "AE", "uae": "AE", "united kingdom": "GB", "uk": "GB",
    "england": "GB", "scotland": "GB", "wales": "GB", "great britain": "GB",
    "united states": "US", "usa": "US", "america": "US", "uruguay": "UY", "venezuela": "VE", "vietnam": "VN",
}


def parse_country(text):
    """'Italy', 'italy' or 'IT' -> 'IT'. None when unknown."""
    t = (text or "").strip().lower()
    if t in COUNTRY_NAMES:
        return COUNTRY_NAMES[t]
    if re.fullmatch(r"[a-z]{2}", t):
        return "GB" if t == "uk" else t.upper()
    return None


def flag_emoji(code):
    return "".join(chr(0x1F1E6 + ord(c) - 65) for c in code.upper())


if not hasattr(g, "name_flag"):
    # An OLD graphics.py is installed: the bot still starts, but flags will not show on the 1v1 cards.
    # Replace graphics.py with the new one to get the flags everywhere.
    log.warning("graphics.py is the old version: replace it with the new one to see the flags on every card")
    g.FLAGS = {}
    g.FLAG_LOADER = None

    def _old_name_flag(img, d, x, y, name, font, h=34, **_):
        if isinstance(font, int):
            font = g.get_font(font, True)
        d.text((x, y), name, font=font, fill=g.TEXT, anchor="lm")
        fx = x + int(d.textlength(name, font=font)) + 18
        entry = g.FLAGS.get(name.lower())
        flag = entry[1] if entry else None
        if flag is not None:
            w = int(flag.width * h / flag.height)
            img.paste(flag.resize((w, h)), (fx, y - h // 2))
    g.name_flag = _old_name_flag

FLAGS = g.FLAGS      # player name (lower case) -> (country code, PIL image or None); shared with graphics.py
_FLAG_IMAGES = {}   # country code -> PIL image or None


async def _flag_image(code):
    if code in _FLAG_IMAGES:
        return _FLAG_IMAGES[code]
    img = None
    try:
        timeout = aiohttp.ClientTimeout(total=6)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"https://flagcdn.com/w80/{code.lower()}.png") as resp:
                if resp.status == 200:
                    img = Image.open(io.BytesIO(await resp.read())).convert("RGBA")
    except Exception:
        img = None
    _FLAG_IMAGES[code] = img
    return img


async def load_flag(uuid, name):
    """Looks up the country of a player so the card drawn next shows the flag by the name."""
    try:
        row = await q_one("SELECT code FROM countries WHERE uuid = ?", (uuid,))
    except Exception:
        row = None
    if not row:
        FLAGS.pop(name.lower(), None)
        return
    FLAGS[name.lower()] = (row["code"], await _flag_image(row["code"]))


def _name_flag(img, d, x, y, name, font):
    """Name + flag (the name only when the country is not set). The drawing lives in graphics.py."""
    g.name_flag(img, d, x, y, name, font)


async def _load_flags(items):
    """Used by graphics.py: loads the flags of [(uuid, name), ...] before a card is drawn."""
    await asyncio.gather(*(load_flag(u, n) for u, n in items))


def _identity(img, d, y, name, head, badge_text, badge_color, extra=None, h=130):
    d.rounded_rectangle([g.PAD, y, g.WIDTH - g.PAD, y + h], radius=16, fill=g.ROW_BG)
    d.rounded_rectangle([g.PAD, y, g.PAD + 8, y + h], radius=4, fill=badge_color)
    g._paste_head(img, d, head, g.PAD + 34, y + 20, 90, radius=12)
    nx = g.PAD + 34 + 90 + 24
    _name_flag(img, d, nx, y + 44, name, g.get_font(38, True))
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
    _box(d, g.PAD + bw + gap, y, bw, box_h, "REQUIRED AT BREAKLIST", n(e["required"]),
         sub=f"after {into_week(e)}", sub_color=g.TEXT, sub2="into the GEXP week")
    _box(d, g.PAD + 2 * (bw + gap), y, bw, box_h, "EXTRA GEXP OWED", n(owed),
         g.RED if owed > 0 else g.GREEN)

    y += box_h + gap
    if days >= VETERAN_DAYS:
        sub, sub_color = "VETERAN REACHED", g.GREEN
    else:
        sub, sub_color = f"{VETERAN_DAYS - int(days)} to veteran", g.MUTED
    _box(d, g.PAD, y, bw, box_h, "DAYS IN GUILD", f"{int(days)} / {VETERAN_DAYS}", size=28,
         sub=gc.fmt_duration(days * 86400), sub_color=g.TEXT, sub2=sub)
    joined = from_iso(e.get("joined_at"))
    _box(d, g.PAD + bw + gap, y, bw, box_h, "JOINED THE GUILD", gc.fmt_date(joined) if joined else "-", size=26,
         sub=clock_text(joined) if joined else "", sub2="" if joined else None)
    left = from_iso(e["left_at"])
    _box(d, g.PAD + 2 * (bw + gap), y, bw, box_h, "BREAKLISTED", gc.fmt_date(left), size=26,
         sub=clock_text(left), sub_color=g.TEXT)

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
        _name_flag(img, d, hx + g.HEAD + 12, cy, e["ign"], g.get_font(24, True))
        d.text((520, cy), gc.fmt_date(from_iso(e["added_at"])), font=g.get_font(18, True), fill=g.TEXT, anchor="mm")
        d.text((640, cy), str(int(e["days_total"])), font=g.get_font(20, True), fill=g.TEXT, anchor="mm")
        owed = e["owed"]
        d.text((780, cy), n(owed), font=g.get_font(20, True), fill=g.RED if owed > 0 else g.GREEN, anchor="mm")
    g._footer(d, height, f"Page {page}/{pages}")
    return g._png(img)


def draw_breaklist_all(rows, heads, page, pages, total):
    """Every breaklist entry of the guild (also the players who are back), newest first."""
    row_h = 96
    top = g.HEADER_H + 40
    height = top + max(1, len(rows)) * row_h + g.FOOTER_H
    img = Image.new("RGB", (g.WIDTH, height), g.BG)
    d = ImageDraw.Draw(img)
    g._header(d, "BREAKLIST HISTORY", f"{GUILD_NAME_DISPLAY} · {total} entries of everyone")
    sy = g.HEADER_H + 22
    for label, x in (("ADDED", 640), ("BACK", 736), ("DAYS", 828)):
        d.text((x, sy), label, font=g.get_font(15, True), fill=g.MUTED, anchor="mm")
    for i, e in enumerate(rows):
        y0 = top + i * row_h
        cy = y0 + (row_h - 8) // 2
        active = e["status"] == "active"
        color = g.RED if active else g.GREEN
        d.rounded_rectangle([g.PAD, y0, g.WIDTH - g.PAD, y0 + row_h - 8], radius=14, fill=g.ROW_BG)
        d.rounded_rectangle([g.PAD, y0, g.PAD + 8, y0 + row_h - 8], radius=4, fill=color)
        hx = g.PAD + 28
        g._paste_head(img, d, heads.get(e["uuid"]), hx, cy - 28, 56)
        nx = hx + 56 + 14
        g.name_flag(img, d, nx, cy - 16, e["ign"], 24, h=24, max_w=230)
        g._badge(d, 478, cy - 16, "ON BREAKLIST" if active else "BACK", color, w=130, h=28, font_size=14)
        reason = (e["reason"] or "No reason").replace("\n", " ")
        d.text((nx, cy + 18), reason[:52] + ("…" if len(reason) > 52 else ""), font=g.get_font(14),
               fill=g.MUTED, anchor="lm")
        d.text((640, cy), gc.fmt_date(from_iso(e["added_at"])), font=g.get_font(17, True), fill=g.TEXT, anchor="mm")
        back = gc.fmt_date(from_iso(e["returned_at"])) if e.get("returned_at") else "-"
        d.text((736, cy), back, font=g.get_font(17, True), fill=g.TEXT, anchor="mm")
        d.text((828, cy), str(int(e["days_total"])), font=g.get_font(20, True), fill=g.TEXT, anchor="mm")
    g._footer(d, height, f"Page {page}/{pages}")
    return g._png(img)


# ---------- purge evasion check ----------

PURGE_STATUS = {"suspect": ("SUSPECT", g.RED), "breaklist": ("BREAKLISTED", (241, 196, 15)),
                "excused": ("EXCUSED", (241, 196, 15)), "met": ("MET IT", g.GREEN)}


def short_et(dt):
    local = dt.astimezone(gc.TZ)
    return f"{local.strftime('%m/%d')} {(local.strftime('%I').lstrip('0') or '12')}:{local.strftime('%M %p')}"


def draw_purge(rows, heads, title, subtitle, page, pages):
    row_h = 96
    top = g.HEADER_H + 40
    height = top + max(1, len(rows)) * row_h + g.FOOTER_H
    img = Image.new("RGB", (g.WIDTH, height), g.BG)
    d = ImageDraw.Draw(img)
    g._header(d, title, subtitle)
    sy = g.HEADER_H + 22
    for label, x in (("EARNED", 640), ("NEEDED", 736), ("SHORT", 828)):
        d.text((x, sy), label, font=g.get_font(15, True), fill=g.MUTED, anchor="mm")
    if not rows:
        d.text((g.WIDTH // 2, top + row_h // 2), "Nobody left the guild in this period.",
               font=g.get_font(22), fill=g.MUTED, anchor="mm")
    for i, e in enumerate(rows):
        y0 = top + i * row_h
        cy = y0 + (row_h - 8) // 2
        label, color = PURGE_STATUS[e["status"]]
        d.rounded_rectangle([g.PAD, y0, g.WIDTH - g.PAD, y0 + row_h - 8], radius=14, fill=g.ROW_BG)
        d.rounded_rectangle([g.PAD, y0, g.PAD + 8, y0 + row_h - 8], radius=4, fill=color)
        hx = g.PAD + 28
        g._paste_head(img, d, heads.get(e["uuid"]), hx, cy - 28, 56)
        nx = hx + 56 + 14
        g.name_flag(img, d, nx, cy - 16, e["ign"], 24, h=24, max_w=230)
        g._badge(d, 478, cy - 16, label, color, w=118, h=28, font_size=14)
        detail = f"In {short_et(e['start'])} · Out {short_et(e['left'])} · {gc.fmt_duration(e['stay'])}"
        if e["rejoined"]:
            detail += " · rejoined"
        d.text((nx, cy + 18), detail, font=g.get_font(14), fill=g.MUTED, anchor="lm")
        d.text((640, cy), n(e["earned"]), font=g.get_font(20, True), fill=g.TEXT, anchor="mm")
        d.text((736, cy), n(e["required"]), font=g.get_font(20, True), fill=g.TEXT, anchor="mm")
        short = e["short"]
        d.text((828, cy), f"-{n(short)}" if short else "0", font=g.get_font(22, True),
               fill=(g.RED if e["status"] == "suspect" else g.MUTED) if short else g.GREEN, anchor="mm")
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
    _name_flag(img, d, g.PAD + 34 + 80 + 22, top + 55, name, g.get_font(36, True))
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


# ---------- GEXP per day (breaklist) and pasted guild history ----------

def draw_gexp_days(name, head, rows, total, required):
    """rows: [(date, gexp, position, members)], oldest first."""
    row_h = 62
    top = g.HEADER_H + 20
    id_h = 110
    height = top + id_h + 16 + 52 + len(rows) * (row_h + 8) + 70 + g.FOOTER_H
    img = Image.new("RGB", (g.WIDTH, height), g.BG)
    d = ImageDraw.Draw(img)
    g._header(d, "GEXP THIS WEEK", f"{GUILD_NAME_DISPLAY} · only the days that count")
    d.rounded_rectangle([g.PAD, top, g.WIDTH - g.PAD, top + id_h], radius=16, fill=g.ROW_BG)
    d.rounded_rectangle([g.PAD, top, g.PAD + 8, top + id_h], radius=4, fill=g.ACCENT)
    g._paste_head(img, d, head, g.PAD + 34, top + 15, 80, radius=12)
    _name_flag(img, d, g.PAD + 34 + 80 + 22, top + 55, name, g.get_font(36, True))
    d.text((g.WIDTH - g.PAD - 30, top + 42), n(total), font=g.get_font(40, True),
           fill=g.GREEN if total >= required else g.RED, anchor="rm")
    d.text((g.WIDTH - g.PAD - 30, top + 84), f"OF {n(required)} REQUIRED", font=g.get_font(14, True), fill=g.MUTED, anchor="rm")
    y = top + id_h + 16
    cols = (g.PAD + 190, g.WIDTH // 2 + 20, g.WIDTH - g.PAD - 150)
    for x, label in zip(cols, ("DATE", "GEXP", "POSITION")):
        d.text((x, y + 24), label, font=g.get_font(16, True), fill=g.MUTED, anchor="mm")
    y += 52
    for date, gexp, pos, members in rows:
        d.rounded_rectangle([g.PAD, y, g.WIDTH - g.PAD, y + row_h], radius=12, fill=g.ROW_BG)
        d.text((cols[0], y + row_h // 2), date, font=g.get_font(24, True), fill=g.TEXT, anchor="mm")
        d.text((cols[1], y + row_h // 2), n(gexp), font=g.get_font(26, True), fill=g.GREEN, anchor="mm")
        d.text((cols[2], y + row_h // 2), f"#{pos}/{members}" if pos else "-", font=g.get_font(24, True),
               fill=g.TEXT, anchor="mm")
        y += row_h + 8
    d.text((g.WIDTH // 2, y + 28), f"Total {n(total)} GEXP", font=g.get_font(22, True), fill=g.SOFT, anchor="mm")
    g._footer(d, height, "Position = rank among guild members that day")
    return g._png(img)


def draw_guild_text(name, head, lines):
    """A guild history pasted as text, drawn as a card."""
    row_h = 50
    top = g.HEADER_H + 20
    id_h = 110
    rows = []
    for line in lines[:24]:
        rows += _wrap_lines(line, g.get_font(22), g.WIDTH - 2 * g.PAD - 60)[:2]
    height = top + id_h + 16 + len(rows) * row_h + 30 + g.FOOTER_H
    img = Image.new("RGB", (g.WIDTH, height), g.BG)
    d = ImageDraw.Draw(img)
    g._header(d, "GUILD HISTORY", GUILD_NAME_DISPLAY)
    d.rounded_rectangle([g.PAD, top, g.WIDTH - g.PAD, top + id_h], radius=16, fill=g.ROW_BG)
    d.rounded_rectangle([g.PAD, top, g.PAD + 8, top + id_h], radius=4, fill=g.ACCENT)
    g._paste_head(img, d, head, g.PAD + 34, top + 15, 80, radius=12)
    _name_flag(img, d, g.PAD + 34 + 80 + 22, top + 55, name, g.get_font(36, True))
    y = top + id_h + 16
    d.rounded_rectangle([g.PAD, y, g.WIDTH - g.PAD, y + len(rows) * row_h + 30], radius=16, fill=g.ROW_BG)
    for i, line in enumerate(rows):
        d.text((g.PAD + 30, y + 30 + i * row_h), line, font=g.get_font(22), fill=g.TEXT, anchor="lm")
    g._footer(d, height)
    return g._png(img)


# ---------- mute / warning card ----------

def draw_punish_card(kind, name, head, reason, rows, by, number):
    """kind: 'MUTE' or 'VERBAL WARNING'. rows: [(label, value)] shown as boxes (length, expires, totals...)."""
    color = g.RED if kind == "MUTE" else (241, 196, 15)
    lines = _wrap_lines(reason or "No reason given.", g.get_font(22), g.WIDTH - 2 * g.PAD - 70)[:5]
    top = g.HEADER_H + 20
    id_h = 130
    box_h = 112
    reason_h = 70 + 30 * len(lines)
    height = top + id_h + 16 + box_h + 16 + reason_h + 16 + g.FOOTER_H
    img = Image.new("RGB", (g.WIDTH, height), g.BG)
    d = ImageDraw.Draw(img)
    g._header(d, kind, f"{GUILD_NAME_DISPLAY} · #{number}")
    d.rounded_rectangle([g.PAD, top, g.WIDTH - g.PAD, top + id_h], radius=16, fill=g.ROW_BG)
    d.rounded_rectangle([g.PAD, top, g.PAD + 8, top + id_h], radius=4, fill=color)
    g._paste_head(img, d, head, g.PAD + 34, top + 15, 100, radius=12)
    _name_flag(img, d, g.PAD + 34 + 100 + 24, top + 65, name, g.get_font(40, True))
    y = top + id_h + 16
    gap = 14
    w = (g.WIDTH - 2 * g.PAD - gap * (len(rows) - 1)) // max(1, len(rows))
    for i, (label, value) in enumerate(rows):
        _box(d, g.PAD + i * (w + gap), y, w, box_h, label, value, size=28 if len(value) < 13 else 22)
    y += box_h + 16
    d.rounded_rectangle([g.PAD, y, g.WIDTH - g.PAD, y + reason_h], radius=16, fill=g.ROW_BG)
    d.text((g.PAD + 28, y + 30), "REASON", font=g.get_font(15, True), fill=g.MUTED, anchor="lm")
    for i, line in enumerate(lines):
        d.text((g.PAD + 28, y + 64 + i * 30), line, font=g.get_font(22), fill=g.TEXT, anchor="lm")
    g._footer(d, height, f"By {by}")
    return g._png(img)


# ---------- GEXP of the last 7 days ----------

def draw_eligibility(name, head, days, total, owed, source):
    """days: list of (day 'YYYY-MM-DD', gexp), oldest first."""
    daily = gc.WEEKLY_REQ / 7
    ok = total >= gc.WEEKLY_REQ
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
    _name_flag(img, d, nx, y + 44, name, g.get_font(36, True))
    label = "QUALIFIES" if ok else f"MISSING {n(gc.WEEKLY_REQ - total)}"
    g._badge(d, nx + 100, y + 96, label, g.GREEN if ok else g.RED, w=200, h=34, font_size=16)
    d.text((g.WIDTH - g.PAD - 30, y + 52), n(total), font=g.get_font(44, True), fill=g.TEXT, anchor="rm")
    d.text((g.WIDTH - g.PAD - 30, y + 98), f"of {n(gc.WEEKLY_REQ)} GEXP", font=g.get_font(18), fill=g.MUTED, anchor="rm")

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
    g._header(d, "GEXP REQUIREMENT", f"{GUILD_NAME_DISPLAY} · weekly requirement {n(gc.WEEKLY_REQ)} GEXP")
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
    how = ("Use the buttons below: Breaklist Me and Excuse Me send a request to the staff (you can cancel it "
           "until they decide), Ignore tells the staff you will reach the requirement or don't intend to act.")
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

ROAD_COLORS = [(255, 200, 61), (255, 143, 74), (167, 112, 255), (74, 144, 255), (80, 220, 190), (255, 105, 140)]
# a colour per place ahead of us: nearest guild first


def draw_road(ours, targets, series, title):
    """ours = {'name','exp','rank'}; targets = [{'rank','name','exp','gap','eta','rate_us','rate_them'}] (nearest first,
    up to 6, down to #1); series = {target name: [(day, gap), ...]}"""
    n_t = len(targets)
    row_h = 84 if n_t > 3 else 92
    chart_h = 330
    top = g.HEADER_H + 16
    leg_rows = 1 + (n_t > 3)
    height = top + 14 + n_t * (row_h + 10) + chart_h + 16 + g.FOOTER_H
    img = Image.new("RGB", (g.WIDTH, height), g.BG)
    d = ImageDraw.Draw(img)
    g._header(d, title, f"{GUILD_NAME_DISPLAY} · currently #{ours['rank']} with {n(ours['exp'])} GEXP")

    def color(i):
        return ROAD_COLORS[i % len(ROAD_COLORS)]

    y = top
    for i, t in enumerate(targets):
        c = color(i)
        d.rounded_rectangle([g.PAD, y, g.WIDTH - g.PAD, y + row_h], radius=14, fill=g.ROW_BG)
        d.rounded_rectangle([g.PAD, y, g.PAD + 8, y + row_h], radius=4, fill=c)
        g._badge(d, g.PAD + 70, y + row_h // 2, f"#{t['rank']}", c, w=80, h=40, font_size=22)
        d.text((g.PAD + 130, y + 22), t["name"], font=g.get_font(24, True), fill=g.TEXT, anchor="lm")
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
        d.text((g.PAD + 130, y + row_h - 34), line1, font=g.get_font(15), fill=g.SOFT, anchor="lm")
        if line2:
            d.text((g.PAD + 130, y + row_h - 14), line2, font=g.get_font(15, True),
                   fill=g.GREEN if "Catch" in line2 else g.RED, anchor="lm")
        d.text((g.WIDTH - g.PAD - 24, y + row_h // 2 - 8), n(t["gap"]), font=g.get_font(32, True), fill=c, anchor="rm")
        d.text((g.WIDTH - g.PAD - 24, y + row_h // 2 + 24), f"GEXP FROM #{t['rank']} GUILD", font=g.get_font(13, True),
               fill=g.MUTED, anchor="rm")
        y += row_h + 10

    # chart: gap over time
    d.rounded_rectangle([g.PAD, y, g.WIDTH - g.PAD, y + chart_h], radius=16, fill=g.ROW_BG)
    d.text((g.PAD + 24, y + 26), "GAP OVER TIME (GEXP)", font=g.get_font(15, True), fill=g.MUTED, anchor="lm")
    px0, px1 = g.PAD + 110, g.WIDTH - g.PAD - 40
    py0, py1 = y + 60, y + chart_h - 70 - 24 * (leg_rows - 1)
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
                d.line(pts, fill=color(i), width=4)
            for p in pts:
                d.ellipse([p[0] - 4, p[1] - 4, p[0] + 4, p[1] + 4], fill=color(i))
        d.text((px0, py1 + 20), days_all[0][5:].replace("-", "/"), font=g.get_font(14), fill=g.MUTED, anchor="lm")
        d.text((px1, py1 + 20), days_all[-1][5:].replace("-", "/"), font=g.get_font(14), fill=g.MUTED, anchor="rm")
    # legend, wrapped on two lines when there are many guilds
    lx, ly = g.PAD + 24, y + chart_h - 22 - 24 * (leg_rows - 1)
    for i, t in enumerate(targets):
        label = f"#{t['rank']} {t['name']}"
        w = 22 + int(d.textlength(label, font=g.get_font(14))) + 20
        if lx + w > g.WIDTH - g.PAD - 20 and lx > g.PAD + 24:
            lx, ly = g.PAD + 24, ly + 24
        d.rounded_rectangle([lx, ly - 7, lx + 14, ly + 7], radius=3, fill=color(i))
        d.text((lx + 22, ly), label, font=g.get_font(14), fill=g.SOFT, anchor="lm")
        lx += w + 6
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
        self.last_error = ""
        self.last_message = None

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
            cur = await db.execute("SELECT uuid FROM guild_seen WHERE COALESCE(in_guild, 1) = 1")
            before = {r[0] for r in await cur.fetchall()}
            for uuid, m in members.items():
                joined = m.get("joined")
                joined_iso = iso(datetime.fromtimestamp(joined / 1000, tz=UTC)) if joined else None
                await db.execute(
                    "INSERT INTO guild_seen (uuid, joined_at, rank, last_seen, in_guild) VALUES (?, ?, ?, ?, 1) "
                    "ON CONFLICT(uuid) DO UPDATE SET joined_at = COALESCE(excluded.joined_at, guild_seen.joined_at), "
                    "rank = excluded.rank, last_seen = excluded.last_seen, in_guild = 1",
                    (uuid, joined_iso, m.get("rank"), stamp))
                for day, value in (m.get("expHistory") or {}).items():
                    await db.execute(
                        "INSERT INTO gexp_history (uuid, day, gexp) VALUES (?, ?, ?) "
                        "ON CONFLICT(uuid, day) DO UPDATE SET gexp = excluded.gexp", (uuid, day, int(value)))
            await self._track_departures(db, before, set(members), stamp)
            await db.commit()

    async def _track_departures(self, db, before, current, stamp):
        """Members that were in the guild at the last snapshot and are not now have left: remember when
        (Hypixel does not say, so the time is the last snapshot where they were still here)."""
        if before:
            cur = await db.execute("SELECT value FROM kv WHERE key = 'left_tracking'")
            started = await cur.fetchone()
            gone = before - current
            if gone and not started:
                # first run of this feature: whoever is already missing left at an unknown time, do not guess
                await db.execute("UPDATE guild_seen SET in_guild = 0 WHERE uuid IN (%s)" % ",".join("?" * len(gone)),
                                 tuple(gone))
            elif gone and len(current) < 0.5 * len(before):
                log.warning("The guild list looks incomplete (%d of %d members): departures not recorded",
                            len(current), len(before))
            else:
                for uuid in gone:
                    cur = await db.execute("SELECT joined_at, last_seen FROM guild_seen WHERE uuid = ?", (uuid,))
                    row = await cur.fetchone()
                    await db.execute(
                        "INSERT INTO guild_left (uuid, joined_at, left_at, detected_at) VALUES (?, ?, ?, ?)",
                        (uuid, row[0] if row else None, (row[1] if row and row[1] else stamp), stamp))
                    await db.execute("UPDATE guild_seen SET in_guild = 0 WHERE uuid = ?", (uuid,))
        await db.execute("INSERT OR IGNORE INTO kv (key, value) VALUES ('left_tracking', ?)", (stamp,))
        if current:
            await db.execute("UPDATE guild_left SET rejoined_at = ? WHERE rejoined_at IS NULL AND uuid IN (%s)"
                             % ",".join("?" * len(current)), (stamp, *current))

    async def purge_rows(self, *, cycle=None, uuid=None):
        """Members that left the guild, each with what they should have earned until they left.
        cycle: 'this' (the week that ends with the next purge) or 'last' (already purged); uuid: one player, any week.
        A member only owes GEXP in proportion to the time they were in the cycle: the requirement grows
        continuously (55,000 over the whole week), so leaving after 1 day and 1 minute needs a little more
        than leaving after exactly 1 day."""
        if uuid:
            left_rows = await q_all("SELECT * FROM guild_left WHERE uuid = ? ORDER BY left_at DESC LIMIT 10", (uuid,))
        else:
            ws = gc.week_start(now_utc())
            if cycle == "last":
                ws = ws - timedelta(days=7)
            first, last = iso(ws), iso(ws + timedelta(days=7))
            left_rows = await q_all("SELECT * FROM guild_left WHERE left_at >= ? AND left_at < ? ORDER BY left_at",
                                    (first, last))
        out = []
        for r in left_rows:
            left = from_iso(r["left_at"])
            joined = from_iso(r["joined_at"])
            ws_l = gc.week_start(left)
            start = max(joined, ws_l) if joined else ws_l
            required = gc.required_elapsed(start, left)
            earned, _count = await self.window_sum(r["uuid"], start, left)
            short = max(0, required - earned)
            status = "met" if short == 0 else "suspect"
            if await q_one("SELECT id FROM bl_entries WHERE uuid = ? AND added_at >= ?",
                           (r["uuid"], iso(left - timedelta(days=1)))):
                status = "breaklist"
            elif await q_one("SELECT uuid FROM excuses WHERE uuid = ? AND week = ?",
                             (r["uuid"], ws_l.date().isoformat())):
                status = "excused"
            ign = r["ign"]
            if not ign:
                ign = await self.name_for(r["uuid"])
                await q_run("UPDATE guild_left SET ign = ? WHERE id = ?", (ign, r["id"]))
            out.append({"uuid": r["uuid"], "ign": ign, "start": start, "left": left, "stay": max(0, gc.secs(start, left)),
                        "required": required, "earned": earned, "short": short, "status": status,
                        "rejoined": bool(r["rejoined_at"])})
        order = {"suspect": 0, "breaklist": 1, "excused": 1, "met": 2}
        out.sort(key=lambda e: (order[e["status"]], -e["short"], e["left"]))
        return out

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
                      thread_name=None, thread_key=None, mentions=None, embed=None, embeds=None, media=None):
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
            if media:
                kwargs["files"] = [discord.File(io.BytesIO(data), filename=name) for name, data in media]
            if embeds:
                kwargs["embeds"] = embeds
            elif embed is not None:
                kwargs["embed"] = embed
            if isinstance(ch, discord.ForumChannel):
                if thread_key:
                    tid = await kv_get(thread_key)
                    if tid and tid.isdigit():
                        try:
                            thread = self.bot.get_channel(int(tid)) or await self.bot.fetch_channel(int(tid))
                            if getattr(thread, "archived", False):
                                await thread.edit(archived=False)
                            self.last_message = await thread.send(**kwargs)
                            return thread.mention
                        except discord.HTTPException as e:
                            # deleted, locked or archived without permission: a new post is created instead
                            log.warning("The saved thread %s can't be used (%s): creating a new one", tid, e)
                            for f in [kwargs.get("file"), *kwargs.get("files", [])]:
                                if f is not None:
                                    f.reset()
                if not any(k in kwargs for k in ("content", "file", "files", "embed", "embeds")):
                    kwargs["content"] = thread_name or "Post"
                created = await ch.create_thread(name=(thread_name or "Post")[:100], **kwargs)
                self.last_message = created.message
                if thread_key:
                    await kv_set(thread_key, created.thread.id)
                return created.thread.mention
            self.last_message = await ch.send(**kwargs)
            return ch.mention
        except (discord.HTTPException, discord.Forbidden) as e:
            log.warning("Could not post in %s (%s): %s", env_name, cid, e)
            self.last_error = f"{type(e).__name__}: {getattr(e, 'status', '')} code {getattr(e, 'code', '')} - {getattr(e, 'text', e)}"
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

    async def add_day_set(self, uuid, name, days, note, by):
        """Adds (or, with a negative number, takes away) days and remembers them as a set that can be removed."""
        row = await q_one("SELECT banked FROM player_days WHERE uuid = ?", (uuid,))
        banked = (row["banked"] if row else 0.0)
        new = max(0.0, banked + days)
        stamp = iso(now_utc())
        sid = await q_run("INSERT INTO day_sets (uuid, ign, days, note, added_by, added_at) VALUES (?, ?, ?, ?, ?, ?)",
                          (uuid, name, new - banked, note, str(by) if by else None, stamp))
        await q_run("INSERT INTO player_days (uuid, ign, banked, updated_at) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(uuid) DO UPDATE SET ign = excluded.ign, banked = excluded.banked, "
                    "updated_at = excluded.updated_at", (uuid, name, new, stamp))
        await q_run("UPDATE bl_entries SET days_total = ? WHERE uuid = ? AND status = 'active'", (new, uuid))
        return sid, new

    async def gexp_days_card(self, entry):
        """Card with the GEXP of each day of the guild week they were breaklisted (only days that count)."""
        end = from_iso(entry["left_at"])
        ws = gc.week_start(end)
        joined = from_iso(entry.get("joined_at"))
        first = max(joined, ws) if joined else ws
        day = first.astimezone(gc.ET).date()
        last = end.astimezone(gc.ET).date()
        rows = []
        while day <= last:
            key = day.isoformat()
            mine = await q_one("SELECT gexp FROM gexp_history WHERE uuid = ? AND day = ?", (entry["uuid"], key))
            if mine is not None:
                total = (await q_one("SELECT COUNT(*) AS c FROM gexp_history WHERE day = ?", (key,)))["c"]
                better = (await q_one("SELECT COUNT(*) AS c FROM gexp_history WHERE day = ? AND gexp > ?",
                                      (key, mine["gexp"])))["c"]
                rows.append((day.strftime("%m/%d/%y"), mine["gexp"], better + 1, total))
            day += timedelta(days=1)
        if not rows:
            return None
        head = await self.head_for(entry["uuid"], entry["ign"])
        await load_flag(entry["uuid"], entry["ign"])
        return await asyncio.to_thread(draw_gexp_days, entry["ign"], head, rows, sum(r[1] for r in rows),
                                       entry["required"])

    async def post_breaklist(self, entry, name, text, card):
        """Posts the breaklist card, the daily GEXP card under it (same thread) and the staff button for the guild
        history. Returns the channel/thread mention, None when no channel is set, False when posting failed."""
        key = f"bl_thread:{entry['id']}"
        view = discord.ui.View(timeout=None)
        view.add_item(GuildHistoryButton(entry["id"]))
        posted = await self.post_to("BREAKLIST_CHANNEL_ID", content=text, data=card, filename="breaklist.png",
                                    thread_name=name, thread_key=key, view=view)
        if posted:
            try:
                second = await self.gexp_days_card(entry)
                if second:
                    await self.post_to("BREAKLIST_CHANNEL_ID", data=second, filename="gexp_week.png",
                                       thread_name=name, thread_key=key)
            except Exception:
                log.exception("The daily GEXP card failed")
        return posted

    async def guild_history_text(self, uuid):
        """Short text with the guilds we know about for a player: our own stays and the guild they are in now.
        (Hypixel's API does not give the guilds a player was in before, only the current one.)"""
        lines = []
        try:
            stays = await q_all("SELECT joined_at, left_at FROM guild_left WHERE uuid = ? ORDER BY left_at DESC LIMIT 5",
                                (uuid,))
            for x in stays:
                a, b = from_iso(x["joined_at"]), from_iso(x["left_at"])
                span = f"{max(0, int((b - a).total_seconds() // 86400))} days" if a and b else "unknown length"
                lines.append(f"• **{esc(HYPIXEL_GUILD_NAME or 'This guild')}**: "
                             f"{gc.fmt_date(a) if a else '?'} → {gc.fmt_date(b)} ({span})")
            other = await api_guild({"player": uuid})
            if other:
                m = next((x for x in other.get("members", []) if x.get("uuid") == uuid), None)
                since = (datetime.fromtimestamp(m["joined"] / 1000, tz=UTC) if m and m.get("joined") else None)
                if other.get("name", "").lower() != (HYPIXEL_GUILD_NAME or "").lower():
                    lines.insert(0, f"• **Now in {esc(other.get('name', '?'))}**"
                                 + (f" since {gc.fmt_date(since)}" if since else ""))
                    lines.append("⚠️ They joined another guild: that removes the breaklist.")
        except Exception:
            log.exception("The guild history failed")
        if not lines:
            return ""
        return "\n**Guild history we know:**\n" + "\n".join(lines)

    # ----- logins (Hypixel only gives the last login/logout, so we keep every one we see) -----

    async def poll_logins(self, batch=20):
        """Looks at the oldest-checked guild members and stores each new login. Called every 2 minutes."""
        members = await self.guild_members()
        if not members:
            return 0
        if not await kv_get("logins_since"):
            await kv_set("logins_since", iso(now_utc()))
        rows = await q_all(
            "SELECT uuid FROM guild_seen WHERE COALESCE(in_guild, 1) = 1 "
            "ORDER BY login_polled_at IS NOT NULL, login_polled_at LIMIT ?", (batch * 3,))
        todo = [r["uuid"] for r in rows if r["uuid"] in members][:batch]
        done = 0
        for uuid in todo:
            player = await api_player(uuid)
            if player is False:
                break
            await q_run("UPDATE guild_seen SET login_polled_at = ? WHERE uuid = ?", (iso(now_utc()), uuid))
            done += 1
            if not player:
                continue
            ms_in, ms_out = player.get("lastLogin"), player.get("lastLogout")
            if not ms_in:
                continue
            await q_run("UPDATE guild_seen SET online = ? WHERE uuid = ?", (1 if ms_in > (ms_out or 0) else 0, uuid))
            login = iso(datetime.fromtimestamp(ms_in / 1000, tz=UTC))
            name = player.get("displayname") or ""
            await q_run("INSERT OR IGNORE INTO logins (uuid, ign, login_at) VALUES (?, ?, ?)", (uuid, name, login))
            if ms_out and ms_out >= ms_in:
                await q_run("UPDATE logins SET logout_at = ? WHERE uuid = ? AND login_at = ?",
                            (iso(datetime.fromtimestamp(ms_out / 1000, tz=UTC)), uuid, login))
            await asyncio.sleep(0.4)
        return done

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
        if days - banked_old > 0.01:
            await q_run("INSERT INTO day_sets (uuid, ign, days, note, added_by, added_at) VALUES (?, ?, ?, ?, ?, ?)",
                        (uuid, name, days - banked_old, "added with the breaklist", str(added_by), stamp))
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
        await load_flag(entry["uuid"], entry["ign"])
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

    # ----- requests (Breaklist Me / Excuse Me) -----

    async def post_request_channel(self, interaction, *, content=None, embed=None, view=None):
        """Posts in REQUESTS_CHANNEL_ID; without it (tests) or if it fails, in the channel of the interaction."""
        self.last_message = None
        posted = await self.post_to("REQUESTS_CHANNEL_ID", content=content, embed=embed, view=view,
                                    mentions=discord.AllowedMentions(users=True, roles=True))
        if posted:
            return posted
        kwargs = {"allowed_mentions": discord.AllowedMentions(users=True, roles=True)}
        if content:
            kwargs["content"] = content
        if embed is not None:
            kwargs["embed"] = embed
        if view is not None:
            kwargs["view"] = view
        self.last_message = await interaction.channel.send(**kwargs)
        return None

    async def pending_request(self, uuid):
        return await q_one("SELECT * FROM bl_requests WHERE uuid = ? AND status = 'pending' ORDER BY id DESC LIMIT 1",
                           (uuid,))

    async def cancel_request(self, rid, by_user):
        """Cancels a pending request (by the requester or by the staff). Returns the text to show."""
        row = await q_one("SELECT * FROM bl_requests WHERE id = ?", (rid,))
        if not row:
            return "I can't find this request any more."
        if row["status"] != "pending":
            return f"This request was already {row['status']}."
        await q_run("UPDATE bl_requests SET status = 'cancelled', decided_by = ? WHERE id = ?", (str(by_user.id), rid))
        try:
            if row.get("channel_id") and row.get("message_id"):
                ch = self.bot.get_channel(int(row["channel_id"])) or await self.bot.fetch_channel(int(row["channel_id"]))
                msg = await ch.fetch_message(int(row["message_id"]))
                embed = msg.embeds[0]
                embed.color = 0x95A5A6
                embed.set_footer(text=f"Cancelled by {by_user.display_name}")
                await msg.edit(embed=embed, view=None)
                await ch.send(f"Request #{rid} for **{esc(row['ign'] or '?')}** was cancelled by "
                              f"{esc(by_user.display_name)}.", allowed_mentions=NO_MENTIONS)
        except Exception:
            log.warning("Could not update the cancelled request message", exc_info=True)
        return f"Your {row['kind']} request was cancelled. You can send a different one now."

    async def create_request(self, interaction, kind, uuid, *, reason, duration, guildless, guild):
        name = await self.name_for(uuid)
        weeks = _weeks_from(duration) if kind == "excuse" else 1
        rid = await q_run(
            "INSERT INTO bl_requests (uuid, ign, kind, reason, duration, guildless, guild, weeks, requester_id, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
            (uuid, name, kind, reason, duration, None if guildless is None else int(guildless), guild, weeks,
             str(interaction.user.id), iso(now_utc())))
        embed = discord.Embed(title=("Breaklist request" if kind == "breaklist" else "Excuse request"),
                              color=0x00C8E6, timestamp=now_utc())
        embed.add_field(name="Player", value=f"**{esc(name)}**", inline=True)
        embed.add_field(name="Requested by", value=f"<@{interaction.user.id}>", inline=True)
        embed.add_field(name="Reason", value=reason or "-", inline=False)
        if kind == "breaklist":
            embed.add_field(name="Inactive for", value=duration or "-", inline=True)
            embed.add_field(name="Staying guildless", value="Yes" if guildless else f"No - joining **{esc(guild or '?')}**",
                            inline=True)
        else:
            embed.add_field(name="Excusal length", value=f"{weeks} week(s) (asked: {duration})", inline=True)
        embed.set_footer(text=f"Request #{rid}")
        view = discord.ui.View(timeout=None)
        view.add_item(RequestButton("ok", rid))
        view.add_item(RequestButton("no", rid))
        view.add_item(RequestButton("cx", rid))
        role = _env("REQUESTS_PING_ROLE_ID")
        content = f"<@&{role}>" if role.isdigit() else None
        await self.post_request_channel(interaction, content=content, embed=embed, view=view)
        if self.last_message is not None:
            await q_run("UPDATE bl_requests SET channel_id = ?, message_id = ? WHERE id = ?",
                        (str(self.last_message.channel.id), str(self.last_message.id), rid))
        return rid

    async def announce_breaklist(self, name, res, channel):
        card = await self.breaklist_card(res["entry"], "BREAKLISTED", res["count"])
        text = f"✅ **{esc(name)}** was added to the breaklist." + await self.guild_history_text(res["entry"]["uuid"])
        posted = await self.post_breaklist(res["entry"], name, text, card)
        if not posted:
            await channel.send(content=text, file=discord.File(io.BytesIO(card), filename="breaklist.png"),
                               allowed_mentions=NO_MENTIONS)
        return posted

    async def ignored_now(self):
        key = gc.week_start(now_utc()).date().isoformat()
        rows = await q_all("SELECT key FROM kv WHERE key LIKE ?", (f"ignore:%:{key}",))
        return {r["key"].split(":")[1] for r in rows}

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
        required = gc.required_from(max(joined, ws)) if joined and joined > ws else gc.WEEKLY_REQ
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
        view.add_item(NoticeButton("ig", uuid))
        return view

    async def send_notice(self, it, interaction=None):
        name = await self.name_for(it["uuid"])
        it = dict(it, ign=name)
        head = await self.head_for(it["uuid"], name)
        await load_flag(it["uuid"], name)
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
        excused = await self.excused_now() | await self.ignored_now()
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
            return None, "ROAD_GUILDS is not set (the names of the guilds ranked above you, e.g. the #6, #5 and #4 guilds, comma separated)."
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
            await q_run("INSERT INTO road_history (day, guild, exp, at) VALUES (?, ?, ?, ?) "
                        "ON CONFLICT(day, guild) DO UPDATE SET exp = excluded.exp, at = excluded.at",
                        (day, nm, exp, iso(now_utc())))

        above = sorted([o for o in others if o[1] > our_exp], key=lambda o: o[1])
        # We start at ROAD_START_RANK (7) with every listed guild above us; each listed guild we pass moves us up one place.
        rank = ROAD_START_RANK - (len(others) - len(above))
        if not above:
            return None, "No guild in ROAD_GUILDS is above us: we are #1!"
        targets = []
        hist = await q_all("SELECT day, guild, exp, at FROM road_history WHERE day >= ? ORDER BY day",
                           ((datetime.now(gc.ET) - timedelta(days=14)).date().isoformat(),))
        by_guild, taken = {}, {}
        for r in hist:
            by_guild.setdefault(r["guild"], {})[r["day"]] = r["exp"]
            taken.setdefault(r["guild"], {})[r["day"]] = r["at"]

        def rate(guild):
            """GEXP per day. It uses the real time between the two readings (so a reading taken at 11 PM and one
            taken at 12:10 AM are not both counted as a whole day); old readings without a time use the days."""
            days_ = sorted(by_guild.get(guild, {}))
            if len(days_) < 2:
                return None
            a, b = days_[max(0, len(days_) - 8)], days_[-1]
            gain = by_guild[guild][b] - by_guild[guild][a]
            at_a, at_b = taken[guild].get(a), taken[guild].get(b)
            if at_a and at_b:
                span = (from_iso(at_b) - from_iso(at_a)).total_seconds() / 86400
                if span >= 0.5:
                    return max(0, int(gain / span))
            span = (datetime.fromisoformat(b) - datetime.fromisoformat(a)).days
            return max(0, gain // span) if span else None

        rate_us = rate(our_name)
        series = {}
        for i, (nm, exp) in enumerate(above[:6]):
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
        """Posts the Road to #N card. Returns True on success; the reason of a failure is in self.road_error."""
        self.road_error = None
        if chan_id("ROAD_CHANNEL_ID") is None:
            self.road_error = "ROAD_CHANNEL_ID is not set on the host."
            log.info(self.road_error)
            return False
        try:
            data, err = await self.road_status()
            if err:
                self.road_error = err
                log.warning("Road post skipped: %s", err)
                return False
            card = await self.road_card(data)
        except Exception as e:
            log.exception("The Road card failed")
            self.road_error = f"Error while building the card: {type(e).__name__}: {e}"
            return False
        top = data["targets"][0]["rank"]
        posted = await self.post_to("ROAD_CHANNEL_ID", content=data["text"], data=card, filename="road.png",
                                    thread_name=f"Road to #{top} guild", thread_key="road_thread")
        if not posted:
            self.road_error = ("I couldn't post in the Road channel (check ROAD_CHANNEL_ID and my permissions there: "
                               f"View Channel, Send Messages, Create Posts, Send Messages in Posts, Attach Files). {self.last_error or ''}")
            return False
        return True


# ======================================================================================
# the Breaklist Me / Excuse Me buttons (they keep working after a restart)
# ======================================================================================

class GuildHistoryModal(discord.ui.Modal, title="Guild history"):
    def __init__(self, eid):
        super().__init__()
        self.eid = eid
        self.files = discord.ui.FileUpload(required=True, min_values=1, max_values=5)
        self.note = discord.ui.TextInput(style=discord.TextStyle.paragraph, required=False, max_length=1000,
                                         placeholder="Optional note")
        self.add_item(discord.ui.Label(text="Screenshot of the guild history", component=self.files))
        self.add_item(discord.ui.Label(text="Note", component=self.note))

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        files = [discord.File(io.BytesIO(await a.read()), filename=a.filename) for a in self.files.values]
        text = f"📜 **Guild history** (added by {interaction.user.display_name})" + (f"\n{self.note.value}" if self.note.value else "")
        await interaction.channel.send(content=text, files=files, allowed_mentions=NO_MENTIONS)
        await interaction.followup.send("Guild history posted.", ephemeral=True)


class GuildHistoryButton(discord.ui.DynamicItem[discord.ui.Button], template=r"bl:gh:(?P<eid>\d+)"):
    def __init__(self, eid):
        super().__init__(discord.ui.Button(label="Add guild history (staff)", style=discord.ButtonStyle.secondary,
                                           emoji="📜", custom_id=f"bl:gh:{eid}"))
        self.eid = int(eid)

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["eid"]))

    async def callback(self, interaction: discord.Interaction):
        if not is_guild_staff(interaction.user):
            await interaction.response.send_message("Only guild staff can use this.", ephemeral=True)
            return
        await interaction.response.send_modal(GuildHistoryModal(self.eid))


class NoticeButton(discord.ui.DynamicItem[discord.ui.Button], template=r"np:(?P<act>bl|ex|ig):(?P<uuid>[0-9a-f]{32})"):
    """Breaklist Me / Excuse Me / Ignore on a GEXP notice. The first two only send a request to the staff."""
    LABELS = {"bl": "Breaklist Me", "ex": "Excuse Me", "ig": "Ignore"}
    STYLES = {"bl": discord.ButtonStyle.danger, "ex": discord.ButtonStyle.secondary, "ig": discord.ButtonStyle.secondary}

    def __init__(self, act: str, uuid: str):
        super().__init__(discord.ui.Button(label=self.LABELS[act], style=self.STYLES[act],
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
        pending = await core.pending_request(self.uuid)
        if pending:
            view = discord.ui.View(timeout=120)
            view.add_item(CancelPendingButton(pending["id"]))
            await interaction.response.send_message(
                f"There is already a **{pending['kind']}** request waiting for the staff (#{pending['id']}). "
                f"You can send another one only after this one is closed. Changed your mind? Cancel it here.",
                view=view, ephemeral=True)
            return
        if self.act == "bl":
            await interaction.response.send_modal(BreaklistRequestModal(self.uuid))
        elif self.act == "ex":
            await interaction.response.send_modal(ExcuseRequestModal(self.uuid))
        else:
            await interaction.response.defer(ephemeral=True)
            name = await core.name_for(self.uuid)
            wkey = gc.week_start(now_utc()).date().isoformat()
            if await kv_get(f"ignore:{self.uuid}:{wkey}"):
                await interaction.followup.send("You already told us this week: no more notices until the next one.",
                                                ephemeral=True)
                return
            await kv_set(f"ignore:{self.uuid}:{wkey}", iso(now_utc()))
            text = (f"**{esc(name)}** (<@{interaction.user.id}>) has acknowledged the GEXP notice and has indicated "
                    f"that they expect to reach the requirement this week, or that they do not intend to act on it. "
                    f"No further notices will be sent for them until the next week.")
            await core.post_request_channel(interaction, content=text)
            await interaction.followup.send(
                "Noted. You will not get more notices this week. The staff can see your answer.", ephemeral=True)


class CancelPendingButton(discord.ui.Button):
    """'Cancel my request' shown to the player when they press a notice button with a request still open."""

    def __init__(self, rid: int):
        super().__init__(label="Cancel my request", style=discord.ButtonStyle.danger)
        self.rid = rid

    async def callback(self, interaction: discord.Interaction):
        core: Core = interaction.client.np_core
        text = await core.cancel_request(self.rid, interaction.user)
        await interaction.response.edit_message(content=text + " Press a button on the notice again.", view=None)


def _yes_no(text):
    t = (text or "").strip().lower()
    if t in ("yes", "y", "si", "sì", "true", "1"):
        return True
    if t in ("no", "n", "false", "0"):
        return False
    return None


class BreaklistRequestModal(discord.ui.Modal, title="Breaklist request"):
    reason = discord.ui.TextInput(label="Reason", style=discord.TextStyle.paragraph, max_length=300,
                                  placeholder="Why do you need the breaklist?")
    duration = discord.ui.TextInput(label="How long will you be inactive?", max_length=60,
                                    placeholder="e.g. 2 weeks")
    guildless = discord.ui.TextInput(label="Will you stay guildless? (yes / no)", max_length=3, default="yes")
    guild = discord.ui.TextInput(label="If no: which guild will you join?", required=False, max_length=40)

    def __init__(self, uuid: str):
        super().__init__()
        self.uuid = uuid

    async def on_submit(self, interaction: discord.Interaction):
        stay = _yes_no(self.guildless.value)
        if stay is None:
            await interaction.response.send_message("Answer `yes` or `no` to \"Will you stay guildless?\". Press the button again.", ephemeral=True)
            return
        if stay is False and not self.guild.value.strip():
            await interaction.response.send_message("You said you will not stay guildless: write the guild you will join. Press the button again.", ephemeral=True)
            return
        core: Core = interaction.client.np_core
        if await core.pending_request(self.uuid):
            await interaction.response.send_message("You already have a request waiting for the staff.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await core.create_request(interaction, "breaklist", self.uuid, reason=self.reason.value.strip(),
                                  duration=self.duration.value.strip(), guildless=stay,
                                  guild=self.guild.value.strip() if stay is False else None)
        await interaction.followup.send("Your breaklist request was sent to the staff. You will be pinged when they decide. Press a notice button again if you want to cancel it.", ephemeral=True)


class ExcuseRequestModal(discord.ui.Modal, title="Excuse request"):
    reason = discord.ui.TextInput(label="Reason", style=discord.TextStyle.paragraph, max_length=300,
                                  placeholder="Why can't you reach the requirement?")
    duration = discord.ui.TextInput(label="For how many weeks?", max_length=30, default="1")

    def __init__(self, uuid: str):
        super().__init__()
        self.uuid = uuid

    async def on_submit(self, interaction: discord.Interaction):
        core: Core = interaction.client.np_core
        if await core.pending_request(self.uuid):
            await interaction.response.send_message("You already have a request waiting for the staff.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await core.create_request(interaction, "excuse", self.uuid, reason=self.reason.value.strip(),
                                  duration=self.duration.value.strip(), guildless=None, guild=None)
        await interaction.followup.send("Your excusal request was sent to the staff. You will be pinged when they decide.", ephemeral=True)


def _weeks_from(text):
    m = re.search(r"\d+", text or "")
    w = int(m.group()) if m else 1
    if "month" in (text or "").lower():
        w *= 4
    return max(1, min(w, 8))


class RequestButton(discord.ui.DynamicItem[discord.ui.Button], template=r"np:rq:(?P<act>ok|no|cx):(?P<rid>\d+)"):
    """Accept / Decline (staff only) and Cancel (the requester or the staff) under a request."""
    LABELS = {"ok": "Accept", "no": "Decline", "cx": "Cancel request"}
    STYLES = {"ok": discord.ButtonStyle.success, "no": discord.ButtonStyle.danger,
              "cx": discord.ButtonStyle.secondary}

    def __init__(self, act: str, rid: int):
        super().__init__(discord.ui.Button(label=self.LABELS[act], style=self.STYLES[act],
                                           custom_id=f"np:rq:{act}:{rid}"))
        self.act = act
        self.rid = int(rid)

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match, /):
        return cls(match["act"], int(match["rid"]))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if is_guild_staff(interaction.user):
            return True
        if self.act == "cx":
            row = await q_one("SELECT requester_id FROM bl_requests WHERE id = ?", (self.rid,))
            if row and str(row["requester_id"]) == str(interaction.user.id):
                return True
            await interaction.response.send_message("Only the person who sent this request (or the staff) can cancel it.",
                                                    ephemeral=True)
            return False
        await interaction.response.send_message("Only the guild staff can accept or decline requests.", ephemeral=True)
        return False

    async def callback(self, interaction: discord.Interaction):
        core: Core = interaction.client.np_core
        if self.act == "cx":
            await interaction.response.defer(ephemeral=True)
            await interaction.followup.send(await core.cancel_request(self.rid, interaction.user), ephemeral=True)
            return
        row = await q_one("SELECT * FROM bl_requests WHERE id = ?", (self.rid,))
        if not row:
            await interaction.response.send_message("I can't find this request any more.", ephemeral=True)
            return
        if row["status"] != "pending":
            await interaction.response.send_message(f"This request was already {row['status']}.", ephemeral=True)
            return
        await interaction.response.defer()
        name = row["ign"] or await core.name_for(row["uuid"])
        staff = interaction.user
        if self.act == "ok":
            if row["kind"] == "breaklist":
                reason = row["reason"] or "Requested by the player"
                if row["duration"]:
                    reason = f"{reason} (away: {row['duration']})"
                res = await core.do_breaklist(row["uuid"], name, reason=reason[:300], added_by=str(staff.id))
                if "error" in res:
                    await interaction.followup.send(res["error"], ephemeral=True)
                    return
                await core.announce_breaklist(name, res, interaction.channel)
            else:
                until = await core.add_excuse(row["uuid"], name, weeks=int(row["weeks"] or 1), reason=row["reason"],
                                              added_by=f"{staff} ({staff.id})")
                text = f"✅ **{esc(name)}** has been excused until {gc.fmt_et(until)}."
                if not await core.post_to("EXCUSE_CHANNEL_ID", content=text):
                    await interaction.channel.send(content=text, allowed_mentions=NO_MENTIONS)
            status, color, word = "accepted", 0x2ECC71, "accepted"
        else:
            status, color, word = "declined", 0xE74C3C, "declined"
        await q_run("UPDATE bl_requests SET status = ?, decided_by = ? WHERE id = ?", (status, str(staff.id), self.rid))
        try:
            embed = interaction.message.embeds[0]
            embed.color = color
            embed.set_footer(text=f"{status.capitalize()} by {staff.display_name}")
            await interaction.message.edit(embed=embed, view=None)
        except (IndexError, discord.HTTPException):
            pass
        # the player cannot see the staff request channel: the answer goes to the inactivity notices
        text = (f"<@{row['requester_id']}> your {row['kind']} request for **{esc(name)}** was {word} "
                f"by {esc(staff.display_name)}.")
        posted = await core.post_to("INACTIVITY_CHANNEL_ID", content=text, mentions=USER_MENTIONS,
                                    thread_name="Requests", thread_key="inactivity_requests_thread")
        if not posted:
            await interaction.channel.send(text, allowed_mentions=USER_MENTIONS)


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
    await load_flag(result[0], result[1])
    return result


def send_file(data, name):
    return discord.File(io.BytesIO(data), filename=name)


BL_SORTS = {
    "manual": ("Manual order (/breaklist move)", "ord IS NOT NULL, ord, added_at DESC"),
    "added_desc": ("Newest added first", "added_at DESC"),
    "added_asc": ("Oldest added first", "added_at ASC"),
    "days_desc": ("Most days in guild", "days_total DESC"),
    "days_asc": ("Fewest days in guild", "days_total ASC"),
    "joined": ("First joined the guild", "joined_at IS NULL, joined_at ASC"),
    "owed_desc": ("Most GEXP owed", "owed DESC"),
    "name": ("Name A-Z", "ign COLLATE NOCASE ASC"),
}


class GuildListView(discord.ui.View):
    """Sort menu and page buttons under /breaklist guild."""

    def __init__(self, cog, sort, page, pages):
        super().__init__(timeout=600)
        self.cog, self.sort, self.page, self.pages = cog, sort, page, pages
        self.select = discord.ui.Select(
            placeholder="Sort by: " + BL_SORTS[sort][0],
            options=[discord.SelectOption(label=v[0], value=k, default=(k == sort)) for k, v in BL_SORTS.items()])
        self.select.callback = self.on_sort
        self.add_item(self.select)
        self.prev = discord.ui.Button(label="Previous", style=discord.ButtonStyle.secondary, disabled=page <= 1)
        self.nxt = discord.ui.Button(label="Next", style=discord.ButtonStyle.secondary, disabled=page >= pages)
        self.prev.callback, self.nxt.callback = self.on_prev, self.on_next
        self.add_item(self.prev)
        self.add_item(self.nxt)

    async def refresh(self, interaction):
        await interaction.response.defer()
        res = await self.cog.guild_card(self.sort, self.page)
        if res is None:
            await interaction.edit_original_response(content="Nobody is on the breaklist.", attachments=[], view=None)
            return
        card, page, pages = res
        view = GuildListView(self.cog, self.sort, page, pages)
        await interaction.edit_original_response(attachments=[send_file(card, "breaklist_guild.png")], view=view)

    async def on_sort(self, interaction):
        self.sort, self.page = self.select.values[0], 1
        await self.refresh(interaction)

    async def on_prev(self, interaction):
        self.page -= 1
        await self.refresh(interaction)

    async def on_next(self, interaction):
        self.page += 1
        await self.refresh(interaction)


class Breaklist(commands.GroupCog, name="breaklist", description="Guild breaklist"):
    def __init__(self, core: Core):
        self.core = core
        super().__init__()

    @app_commands.command(name="add", description="Add a player to the breaklist and post the notice (guild staff)")
    @app_commands.describe(
        ign="Minecraft username of the player",
        reason="Why the player is being breaklisted (everyone can read it)",
        start="When they joined the guild (ET or MT, see /timezone): 2026/10/03 or 2026/10/03 6:00 PM (the year is optional: 10/03 6:00 PM). Default: from Hypixel",
        end="When they were breaklisted (ET or MT, see /timezone). Default: now",
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
                await interaction.followup.send("I couldn't read the start date. Use `2026/10/03`, `2026/10/03 6:00 PM` or without year `10/03 6:00 PM` (ET or MT, see /timezone).")
                return
        if end:
            end_dt = gc.parse_et(end)
            if end_dt is None:
                await interaction.followup.send("I couldn't read the end date. Use `2026/10/03`, `2026/10/03 6:00 PM` or without year `10/03 6:00 PM` (ET or MT, see /timezone).")
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
        history = await self.core.guild_history_text(uuid)
        posted = await self.core.post_breaklist(res["entry"], name,
                                                f"✅ **{esc(name)}** was added to the breaklist." + history, card)
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

    @app_commands.command(name="details", description="Exact times of a player's latest breaklist entry")
    @app_commands.describe(ign="Minecraft username of the player")
    async def details(self, interaction: discord.Interaction, ign: str):
        await interaction.response.defer()
        r = await lookup(interaction, ign)
        if r is None:
            return
        uuid, name = r
        e = await q_one("SELECT * FROM bl_entries WHERE uuid = ? ORDER BY id DESC LIMIT 1", (uuid,))
        if not e:
            await interaction.followup.send(f"**{esc(name)}** has never been on the breaklist.")
            return
        e = dict(e)
        joined, left = from_iso(e.get("joined_at")), from_iso(e["left_at"])
        in_guild = gc.fmt_duration(e["days_total"] * 86400)
        lines = [f"**{esc(name)}** · latest breaklist entry ({e['status']})",
                 f"Time in the guild: **{in_guild}**",
                 f"Joined: **{gc.fmt_date(joined)} {clock_text(joined)}**" if joined else "Joined: unknown",
                 f"Breaklisted: **{gc.fmt_date(left)} {clock_text(left)}**",
                 f"Into the GEXP week when breaklisted: **{into_week(e)}**",
                 f"GEXP that week: **{n(e['gexp'])}** / required **{n(e['required'])}** (owed {n(e['owed'])})"]
        await interaction.followup.send("\n".join(lines))

    async def guild_card(self, sort, page):
        """(png, page, pages, total) of /breaklist guild in the chosen order, or None when the list is empty."""
        order = BL_SORTS[sort][1]
        rows = await q_all(f"SELECT * FROM bl_entries WHERE status = 'active' ORDER BY {order}, id DESC")
        if not rows:
            return None
        per = 8
        pages = max(1, math.ceil(len(rows) / per))
        page = max(1, min(page, pages))
        chunk = rows[(page - 1) * per: page * per]
        heads = await g._fetch_heads([(e["uuid"], e["ign"]) for e in chunk])
        for e in chunk:
            await load_flag(e["uuid"], e["ign"])
        card = await asyncio.to_thread(draw_breaklist_list, chunk, heads, page, pages, len(rows))
        return card, page, pages

    @app_commands.command(name="guild", description="List everyone who is on the breaklist (with a menu to sort it)")
    @app_commands.describe(page="Page number")
    async def list_guild(self, interaction: discord.Interaction, page: app_commands.Range[int, 1, 100] = 1):
        await interaction.response.defer()
        res = await self.guild_card("manual", page)
        if res is None:
            await interaction.followup.send("Nobody is on the breaklist.")
            return
        card, page, pages = res
        view = GuildListView(self, "manual", page, pages)
        await interaction.followup.send(file=send_file(card, "breaklist_guild.png"), view=view)

    @app_commands.command(name="all", description="Everyone who has ever been on the breaklist, also who is back (guild staff)")
    @app_commands.describe(page="Page number", only="Show only who is still on it or only who is back")
    @app_commands.choices(only=[app_commands.Choice(name="Still on the breaklist", value="active"),
                                app_commands.Choice(name="Back in the guild", value="returned")])
    @app_commands.check(staff_check)
    async def all_history(self, interaction: discord.Interaction, page: app_commands.Range[int, 1, 200] = 1,
                          only: Optional[app_commands.Choice[str]] = None):
        await interaction.response.defer()
        if only:
            rows = await q_all("SELECT * FROM bl_entries WHERE status = ? ORDER BY id DESC", (only.value,))
        else:
            rows = await q_all("SELECT * FROM bl_entries ORDER BY id DESC")
        if not rows:
            await interaction.followup.send("There is nothing in the breaklist history.")
            return
        per = 8
        pages = max(1, math.ceil(len(rows) / per))
        page = min(page, pages)
        chunk = rows[(page - 1) * per: page * per]
        heads = await g._fetch_heads([(e["uuid"], e["ign"]) for e in chunk])
        for e in chunk:
            await load_flag(e["uuid"], e["ign"])
        card = await asyncio.to_thread(draw_breaklist_all, chunk, heads, page, pages, len(rows))
        await interaction.followup.send(file=send_file(card, "breaklist_all.png"))

    @app_commands.command(name="reset", description="Delete the WHOLE breaklist (administrators). A backup is saved first")
    @app_commands.describe(confirm="Type exactly: RESET BREAKLIST", include_days="Also delete the saved days and the GEXP debts")
    async def reset_all(self, interaction: discord.Interaction, confirm: str, include_days: bool = False):
        if not (isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.administrator):
            await interaction.response.send_message("Only administrators can reset the whole breaklist.", ephemeral=True)
            return
        if confirm.strip() != "RESET BREAKLIST":
            await interaction.response.send_message("Nothing was deleted. To confirm, write exactly `RESET BREAKLIST`.",
                                                    ephemeral=True)
            return
        await interaction.response.defer()
        path = await asyncio.to_thread(backup_db, "breaklist_reset")
        count = (await q_one("SELECT COUNT(*) AS c FROM bl_entries"))["c"]
        await q_run("DELETE FROM bl_entries")
        await q_run("DELETE FROM bl_requests WHERE status = 'pending'")
        if include_days:
            await q_run("DELETE FROM player_days")
            await q_run("DELETE FROM day_sets")
            await q_run("DELETE FROM debts")
        await interaction.followup.send(f"🗑️ The breaklist was reset: **{count}** entries deleted"
                                        + (", with the saved days and debts" if include_days else " (days and debts kept)")
                                        + f". Backup: `{path}`.")

    @app_commands.command(name="resetplayer", description="Delete the whole breaklist history of one player (guild staff)")
    @app_commands.describe(ign="Minecraft username", include_days="Also delete their saved days and GEXP debt")
    @app_commands.check(staff_check)
    async def reset_player(self, interaction: discord.Interaction, ign: str, include_days: bool = False):
        await interaction.response.defer()
        r = await lookup(interaction, ign)
        if r is None:
            return
        uuid, name = r
        count = (await q_one("SELECT COUNT(*) AS c FROM bl_entries WHERE uuid = ?", (uuid,)))["c"]
        if not count and not include_days:
            await interaction.followup.send(f"**{esc(name)}** has no breaklist history.", allowed_mentions=NO_MENTIONS)
            return
        await asyncio.to_thread(backup_db, "breaklist_player_reset")
        await q_run("DELETE FROM bl_entries WHERE uuid = ?", (uuid,))
        await q_run("DELETE FROM bl_requests WHERE uuid = ? AND status = 'pending'", (uuid,))
        if include_days:
            await q_run("DELETE FROM player_days WHERE uuid = ?", (uuid,))
            await q_run("DELETE FROM day_sets WHERE uuid = ?", (uuid,))
            await q_run("DELETE FROM debts WHERE uuid = ?", (uuid,))
        await interaction.followup.send(
            f"🗑️ Breaklist history of **{esc(name)}** deleted ({count} entries"
            + (", saved days and debt too" if include_days else "") + ").", allowed_mentions=NO_MENTIONS)

    @app_commands.command(name="import", description="Add to the bot everyone who has a post in the breaklist forum (guild staff)")
    @app_commands.describe(dry_run="Only show who would be added: nothing is saved")
    @app_commands.check(staff_check)
    async def import_forum(self, interaction: discord.Interaction, dry_run: bool = False):
        await interaction.response.defer()
        cid = chan_id("BREAKLIST_CHANNEL_ID")
        if cid is None:
            await interaction.followup.send("BREAKLIST_CHANNEL_ID is not set.")
            return
        try:
            ch = self.core.bot.get_channel(cid) or await self.core.bot.fetch_channel(cid)
            threads = list(getattr(ch, "threads", []))
            async for t in ch.archived_threads(limit=None):
                threads.append(t)
        except (discord.HTTPException, AttributeError) as e:
            await interaction.followup.send(f"I couldn't read the breaklist channel: {e}")
            return
        seen, added, skipped, failed = set(), [], [], []
        for t in sorted(threads, key=lambda x: x.created_at or now_utc()):
            token = re.match(r"\W*([A-Za-z0-9_]{3,16})", t.name or "")
            if not token:
                failed.append(t.name)
                continue
            try:
                res = await get_uuid(token.group(1))
            except Exception:
                res = None
            if res is None:
                failed.append(t.name)
                continue
            uuid, name = res
            if uuid in seen or await q_one("SELECT id FROM bl_entries WHERE uuid = ? AND status = 'active'", (uuid,)):
                skipped.append(name)
                continue
            seen.add(uuid)
            added.append(name)
            if not dry_run:
                when = iso(t.created_at or now_utc())
                prev = await q_one("SELECT banked FROM player_days WHERE uuid = ?", (uuid,))
                await q_run(
                    "INSERT INTO bl_entries (uuid, ign, added_at, added_by, joined_at, left_at, days_total, gexp, required, "
                    "owed, reason, status) VALUES (?, ?, ?, 'import', NULL, ?, ?, 0, 0, 0, ?, 'active')",
                    (uuid, name, when, when, prev["banked"] if prev else 0, "Imported from the breaklist forum"))
            await asyncio.sleep(0.05)
        head = (f"{'Would add' if dry_run else 'Added'} **{len(added)}** players to the breaklist."
                f" Already there: {len(skipped)}. Names I couldn't read: {len(failed)}.")
        body = head + ("\n" + ", ".join(added)[:1500] if added else "")
        if failed:
            body += "\nNot understood: " + ", ".join(f"`{x}`" for x in failed[:15])
        if added and not dry_run:
            body += ("\nTheir days are 0: set them one by one with `/breaklist setdays` or `/breaklist adddays` "
                     "(the date they were added is the date of their post).")
        await interaction.followup.send(body[:1900], allowed_mentions=NO_MENTIONS)

    @app_commands.command(name="remove", description="Take a player off the breaklist when they rejoin (guild staff)")
    @app_commands.describe(ign="Minecraft username of the player",
                           when="When they rejoined (ET or MT, see /timezone): 2026/10/03 6:00 PM, year optional. Default: now")
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
            await interaction.followup.send("I couldn't read the date. Use `2026/10/03`, `2026/10/03 6:00 PM` or without year `10/03 6:00 PM` (ET or MT, see /timezone).")
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
        old = await q_one("SELECT banked FROM player_days WHERE uuid = ?", (uuid,))
        await self.core.add_day_set(uuid, name, days - (old["banked"] if old else 0.0), "set with /breaklist setdays",
                                    interaction.user.id)
        total, state = await self.core.days_info(uuid)
        await interaction.followup.send(
            f"✅ **{esc(name)}**: {days:g} days banked. Total days in guild now: **{int(total)}** "
            f"(veteran at {VETERAN_DAYS}).")

    @app_commands.command(name="adddays", description="Add a set of days to a player: it can be removed later (guild staff)")
    @app_commands.describe(ign="Minecraft username of the player", days="Days to add",
                           note="What these days are for (optional)")
    @app_commands.check(staff_check)
    async def adddays(self, interaction: discord.Interaction, ign: str, days: app_commands.Range[float, 0.01, 5000.0],
                      note: Optional[str] = None):
        await interaction.response.defer()
        r = await lookup(interaction, ign)
        if r is None:
            return
        uuid, name = r
        sid, new = await self.core.add_day_set(uuid, name, days, note or "added with /breaklist adddays",
                                               interaction.user.id)
        total, _ = await self.core.days_info(uuid)
        await interaction.followup.send(
            f"✅ Set **#{sid}** added to **{esc(name)}**: +{days:g} days. Total days in guild now: **{int(total)}**. "
            f"Remove this set with `/breaklist removedays ign:{name} set:{sid}`.", allowed_mentions=NO_MENTIONS)

    @app_commands.command(name="days", description="Every set of days added to a player (guild staff)")
    @app_commands.describe(ign="Minecraft username of the player")
    @app_commands.check(staff_check)
    async def days_sets(self, interaction: discord.Interaction, ign: str):
        await interaction.response.defer()
        r = await lookup(interaction, ign)
        if r is None:
            return
        uuid, name = r
        rows = await q_all("SELECT * FROM day_sets WHERE uuid = ? ORDER BY id DESC", (uuid,))
        total, _ = await self.core.days_info(uuid)
        banked = (await q_one("SELECT banked FROM player_days WHERE uuid = ?", (uuid,)) or {"banked": 0})["banked"]
        lines = []
        for x in rows[:20]:
            mark = "~~" if x["removed"] else ""
            lines.append(f"{mark}**Set #{x['id']}** · {x['days']:+g} days · {gc.fmt_date(from_iso(x['added_at']))} · "
                         f"{x['note'] or '-'}{mark}{' (removed)' if x['removed'] else ''}")
        live = sum(x["days"] for x in rows if not x["removed"])
        if abs(banked - live) > 0.01:
            lines.append(f"Days from before the sets were recorded: **{banked - live:g}**")
        await interaction.followup.send(
            f"**{esc(name)}** · banked days: **{banked:g}** · total days in guild: **{int(total)}**\n"
            + ("\n".join(lines) if lines else "No sets recorded yet."), allowed_mentions=NO_MENTIONS)

    @app_commands.command(name="removedays", description="Remove one set of days from a player (guild staff)")
    @app_commands.describe(ign="Minecraft username of the player", set="Set number (see /breaklist days)")
    @app_commands.check(staff_check)
    async def removedays(self, interaction: discord.Interaction, ign: str, set: int):
        await interaction.response.defer()
        r = await lookup(interaction, ign)
        if r is None:
            return
        uuid, name = r
        row = await q_one("SELECT * FROM day_sets WHERE id = ? AND uuid = ?", (set, uuid))
        if row is None or row["removed"]:
            await interaction.followup.send(f"**{esc(name)}** has no active set #{set}. See `/breaklist days`.",
                                            allowed_mentions=NO_MENTIONS)
            return
        prev = await q_one("SELECT banked FROM player_days WHERE uuid = ?", (uuid,))
        new = max(0.0, (prev["banked"] if prev else 0.0) - row["days"])
        await q_run("UPDATE day_sets SET removed = 1 WHERE id = ?", (set,))
        await q_run("UPDATE player_days SET banked = ?, updated_at = ? WHERE uuid = ?", (new, iso(now_utc()), uuid))
        await q_run("UPDATE bl_entries SET days_total = ? WHERE uuid = ? AND status = 'active'", (new, uuid))
        total, _ = await self.core.days_info(uuid)
        await interaction.followup.send(
            f"✅ Set **#{set}** ({row['days']:+g} days) removed from **{esc(name)}**. "
            f"Total days in guild now: **{int(total)}**.", allowed_mentions=NO_MENTIONS)

    @app_commands.command(name="move", description="Move a player to another position in /breaklist guild (guild staff)")
    @app_commands.describe(ign="Minecraft username of the player on the breaklist", position="New position (1 = first)")
    @app_commands.check(staff_check)
    async def move(self, interaction: discord.Interaction, ign: str, position: app_commands.Range[int, 1, 500]):
        await interaction.response.defer()
        r = await lookup(interaction, ign)
        if r is None:
            return
        uuid, name = r
        rows = await q_all("SELECT id, uuid FROM bl_entries WHERE status = 'active' "
                           "ORDER BY ord IS NOT NULL, ord, added_at DESC")
        mine = next((x for x in rows if x["uuid"] == uuid), None)
        if mine is None:
            await interaction.followup.send(f"**{esc(name)}** is not on the breaklist.", allowed_mentions=NO_MENTIONS)
            return
        rows.remove(mine)
        position = min(position, len(rows) + 1)
        rows.insert(position - 1, mine)
        for i, x in enumerate(rows, 1):
            await q_run("UPDATE bl_entries SET ord = ? WHERE id = ?", (i, x["id"]))
        await interaction.followup.send(f"✅ **{esc(name)}** is now number **{position}** of {len(rows)} in "
                                        f"`/breaklist guild`.", allowed_mentions=NO_MENTIONS)

    @app_commands.command(name="guildhistory", description="Add the guild history of a breaklisted player: screenshot and/or text (guild staff)")
    @app_commands.describe(ign="Minecraft username", image="Screenshot of the guild history (for example from /member)",
                           image2="Another screenshot (optional)",
                           text="Or paste the history as text: one guild per line")
    @app_commands.check(staff_check)
    async def guildhistory(self, interaction: discord.Interaction, ign: str, image: Optional[discord.Attachment] = None,
                           image2: Optional[discord.Attachment] = None, text: Optional[str] = None):
        await interaction.response.defer()
        r = await lookup(interaction, ign)
        if r is None:
            return
        uuid, name = r
        entry = await q_one("SELECT * FROM bl_entries WHERE uuid = ? ORDER BY id DESC LIMIT 1", (uuid,))
        files = [discord.File(io.BytesIO(await a.read()), filename=a.filename) for a in (image, image2) if a]
        if text:
            lines = [x.strip() for x in re.split(r"\n|;", text) if x.strip()]
            head = await self.core.head_for(uuid, name)
            await load_flag(uuid, name)
            files.append(discord.File(io.BytesIO(await asyncio.to_thread(draw_guild_text, name, head, lines)),
                                      filename="guild_history.png"))
        if not files:
            await interaction.followup.send("Give an image or the text.")
            return
        content = f"📜 **Guild history** of **{esc(name)}** (added by {interaction.user.display_name})"
        tid = await kv_get(f"bl_thread:{entry['id']}") if entry else None
        target = None
        if tid and tid.isdigit():
            target = self.core.bot.get_channel(int(tid))
        if target is not None:
            await target.send(content=content, files=files, allowed_mentions=NO_MENTIONS)
            await interaction.followup.send(f"Posted in {target.mention}.")
        else:
            await interaction.followup.send(content=content, files=files, allowed_mentions=NO_MENTIONS)

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

    @app_commands.command(name="setreq", description="Change the weekly GEXP requirement (guild staff)")
    @app_commands.describe(amount="New weekly requirement, for example 55000")
    @app_commands.check(staff_check)
    async def setreq(self, interaction: discord.Interaction, amount: app_commands.Range[int, 1000, 1_000_000]):
        old = gc.WEEKLY_REQ
        gc.set_weekly_req(amount)
        await kv_set("weekly_req", amount)
        await interaction.response.send_message(
            f"Weekly GEXP requirement changed from **{n(old)}** to **{n(amount)}**. "
            f"It applies to every new calculation, card and notice from now on.")

    @app_commands.command(name="remind", description="Remind one player about the GEXP requirement now, with the buttons (guild staff)")
    @app_commands.describe(ign="Minecraft username of the player",
                           kind="friday = the under-20k reminder, final = the last reminder before the purge")
    @app_commands.choices(kind=[app_commands.Choice(name="Friday reminder", value="friday"),
                                app_commands.Choice(name="Final reminder", value="final")])
    @app_commands.check(staff_check)
    async def remind(self, interaction: discord.Interaction, ign: str, kind: Optional[app_commands.Choice[str]] = None):
        await interaction.response.defer()
        r = await lookup(interaction, ign)
        if r is None:
            return
        uuid, name = r
        await self.core.guild_members(force=True)
        it = await self.core.notice_item(uuid, kind.value if kind else "final")
        if it is None:
            await interaction.followup.send("I have no GEXP data for this player (is the Hypixel key set?).")
            return
        posted = await self.core.send_notice(it, interaction)
        if posted:
            await interaction.followup.send(f"Reminder posted in {posted}.")
        elif posted is False:
            await interaction.followup.send("I couldn't post in the inactivity channel (check its id and my permissions).")

    @app_commands.command(name="missing", description="How much GEXP a player must earn this week, extra debt included (guild staff)")
    @app_commands.describe(
        ign="Minecraft username of the player",
        joined="Join time (ET or MT, see /timezone): 2026/10/21 6:00 PM (year optional). Empty = their join time in the guild",
        start_gexp="The GEXP they had at that moment (default 0)",
        current_gexp="Their GEXP now (default: taken from Hypixel)",
        debt="Extra GEXP debt (default: the saved debt)",
        save="Save the remaining debt as the player's new debt")
    @app_commands.check(staff_check)
    async def missing(self, interaction: discord.Interaction, ign: str, joined: Optional[str] = None,
                      start_gexp: app_commands.Range[int, 0, 100_000_000] = 0,
                      current_gexp: Optional[app_commands.Range[int, 0, 100_000_000]] = None,
                      debt: Optional[app_commands.Range[int, 0, 100_000_000]] = None,
                      save: bool = False):
        await interaction.response.defer()
        join_dt = gc.parse_et(joined) if joined else None
        if joined and join_dt is None:
            await interaction.followup.send(
                "I couldn't read the join time. Use year/month/day and the time in your timezone (see /timezone), "
                "for example `2026/10/21 6:00 PM`, `2026/10/21 18:00` or without year `10/21 6:00 PM`.")
            return
        r = await lookup(interaction, ign)
        if r is None:
            return
        uuid, name = r
        now = now_utc()
        auto_join = None
        if join_dt is None:
            # no time given: the exact moment they joined the guild (Hypixel), and never before this week's start
            await self.core.guild_members()
            real_join = await self.core.joined_for(uuid)
            if real_join is None:
                await interaction.followup.send(
                    f"I don't know when **{esc(name)}** joined the guild (they are not a member). "
                    "Give the time with `joined`, for example `2026/10/21 6:00 PM`.", allowed_mentions=NO_MENTIONS)
                return
            join_dt = max(real_join, gc.week_start(now))
            auto_join = real_join
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
        if auto_join is not None:
            where = ("joined before this week: the full week counts" if auto_join < gc.week_start(now)
                     else "join time taken from the guild")
            note = ((note + " ") if note else "") + f"No time given: {where} ({gc.fmt_et(auto_join)})."
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


class PurgeCog(commands.GroupCog, name="purge", description="Purge evasion checks (guild staff)"):
    PER_PAGE = 7

    def __init__(self, core: Core):
        self.core = core
        super().__init__()

    @app_commands.command(name="evaders", description="Members who left the guild without earning the GEXP they owed (guild staff)")
    @app_commands.describe(
        cycle="Which weekly cycle (each one ends with the Tuesday 12:00 AM ET purge). Default: this one",
        ign="Check only this player (all the times they left)",
        page="Page of the list")
    @app_commands.choices(cycle=[app_commands.Choice(name="This cycle (until the next purge)", value="this"),
                                 app_commands.Choice(name="Last cycle (already purged)", value="last")])
    @app_commands.check(staff_check)
    async def evaders(self, interaction: discord.Interaction, cycle: str = "this", ign: Optional[str] = None,
                      page: app_commands.Range[int, 1, 50] = 1):
        await interaction.response.defer()
        uuid = None
        if ign:
            r = await lookup(interaction, ign)
            if r is None:
                return
            uuid = r[0]
        rows = await self.core.purge_rows(cycle=cycle, uuid=uuid)
        if uuid and not rows:
            await interaction.followup.send(f"I have no record of **{esc(r[1])}** leaving the guild.",
                                            allowed_mentions=NO_MENTIONS)
            return
        pages = max(1, math.ceil(len(rows) / self.PER_PAGE))
        page = min(page, pages)
        chunk = rows[(page - 1) * self.PER_PAGE: page * self.PER_PAGE]
        heads = await g._fetch_heads([(e["uuid"], e["ign"]) for e in chunk])
        ws = gc.week_start(now_utc()) - (timedelta(days=7) if cycle == "last" and not uuid else timedelta(0))
        subtitle = (f"{GUILD_NAME_DISPLAY} · {r[1]}" if uuid else
                    f"{GUILD_NAME_DISPLAY} · {'last' if cycle == 'last' else 'this'} cycle from {gc.fmt_date(ws)}")
        title = "PURGE CHECK"
        card = await asyncio.to_thread(draw_purge, chunk, heads, title, subtitle, page, pages)
        suspects = sum(1 for e in rows if e["status"] == "suspect")
        text = f"**{suspects}** of {len(rows)} member(s) who left are short of the GEXP they owed for the time they were in."
        await interaction.followup.send(content=text, file=send_file(card, "purge.png"))


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
            ("REQUESTS_CHANNEL_ID", "Breaklist and excuse requests"),
            ("MUTE_LOG_CHANNEL_ID", "Mute logs"),
            ("BLACKLIST_CHANNEL_ID", "Guild blacklist"),
            ("EVIDENCE_CHANNEL_ID", "Evidence"),
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
            kind = type(ch).__name__
            where = getattr(getattr(ch, "guild", None), "name", "?")
            self.core.last_error = ""
            res = await self.core.post_to(
                env_name, content=f"Channel test from {interaction.user.display_name}: {label}. You can delete this.",
                thread_name="Channel test (delete me)")
            if res:
                lines.append(f"- {label} (`{env_name}`): OK -> {res} [{kind}]")
            else:
                extra = ""
                if isinstance(ch, discord.Thread):
                    extra = f" Thread state: archived={ch.archived}, locked={ch.locked} (an ID of a single post, not of the forum?)."
                lines.append(f"- {label} (`{env_name}`): FAILED [{kind} in server '{where}'] Discord says: {self.core.last_error}.{extra}")
        await interaction.followup.send("**Channel test**\n" + "\n".join(lines), ephemeral=True)


# ======================================================================================
# country flags, mute logs
# ======================================================================================

class CountryCog(commands.GroupCog, name="country", description="Country flags shown next to player names"):
    def __init__(self, core: Core):
        self.core = core
        super().__init__()

    @app_commands.command(name="set", description="Assign a country to a player: its flag shows next to the name (guild staff)")
    @app_commands.describe(ign="Minecraft username", country="Country name (Italy) or 2-letter code (IT)")
    @app_commands.check(staff_check)
    async def set(self, interaction: discord.Interaction, ign: str, country: str):
        await interaction.response.defer()
        code = parse_country(country)
        if code is None:
            await interaction.followup.send("I don't know that country. Write its name (for example `Italy`) or its 2-letter code (`IT`).")
            return
        r = await lookup(interaction, ign)
        if r is None:
            return
        uuid, name = r
        await q_run("INSERT INTO countries (uuid, ign, code) VALUES (?, ?, ?) "
                    "ON CONFLICT(uuid) DO UPDATE SET ign = excluded.ign, code = excluded.code", (uuid, name, code))
        await interaction.followup.send(f"{flag_emoji(code)} **{esc(name)}** now has the country **{code}**. "
                                        "The flag shows on their cards.")

    @app_commands.command(name="remove", description="Remove the country of a player (guild staff)")
    @app_commands.check(staff_check)
    async def remove(self, interaction: discord.Interaction, ign: str):
        await interaction.response.defer()
        r = await lookup(interaction, ign)
        if r is None:
            return
        uuid, name = r
        await q_run("DELETE FROM countries WHERE uuid = ?", (uuid,))
        FLAGS.pop(name.lower(), None)
        await interaction.followup.send(f"Country removed from **{esc(name)}**.")

    @app_commands.command(name="view", description="Show the country of a player")
    async def view(self, interaction: discord.Interaction, ign: str):
        await interaction.response.defer()
        r = await lookup(interaction, ign)
        if r is None:
            return
        uuid, name = r
        row = await q_one("SELECT code FROM countries WHERE uuid = ?", (uuid,))
        await interaction.followup.send(f"{flag_emoji(row['code'])} **{esc(name)}**: {row['code']}" if row
                                        else f"**{esc(name)}** has no country set.")


def parse_mute_length(text):
    """'3d', '12h', '1d12h', '2w', '30m', 'perm' -> timedelta, 'perm' for permanent, None if unreadable."""
    t = (text or "").strip().lower().replace(" ", "")
    if t in ("perm", "permanent", "forever"):
        return "perm"
    if not re.fullmatch(r"(\d+[wdhm])+", t):
        return None
    units = {"w": 7 * 86400, "d": 86400, "h": 3600, "m": 60}
    total = sum(int(v) * units[u] for v, u in re.findall(r"(\d+)([wdhm])", t))
    return timedelta(seconds=total) if total > 0 else None


def mute_length_text(row):
    if not row["end_at"]:
        return "Permanent"
    return gc.fmt_duration((from_iso(row["end_at"]) - from_iso(row["start_at"])).total_seconds())


class AppealModal(discord.ui.Modal):
    """Feedback or appeal about a mute / verbal warning. The text goes to the staff channel (APPEALS_CHANNEL_ID)."""

    def __init__(self, mode, kind, pid):
        super().__init__(title="Appeal" if mode == "ap" else "Feedback")
        self.mode, self.kind, self.pid = mode, kind, pid
        self.text = discord.ui.TextInput(style=discord.TextStyle.paragraph, max_length=1500, required=True,
                                         placeholder="Why do you think it is unjust?" if mode == "ap"
                                         else "What do you think about it?")
        self.add_item(discord.ui.Label(text="Your message to the staff", component=self.text))

    async def on_submit(self, interaction: discord.Interaction):
        table = "mutes" if self.kind == "m" else "warnings"
        row = await q_one(f"SELECT * FROM {table} WHERE id = ?", (self.pid,))
        if row is None:
            await interaction.response.send_message("This punishment doesn't exist any more.", ephemeral=True)
            return
        if row.get("discord_id") and str(interaction.user.id) != str(row["discord_id"]):
            await interaction.response.send_message("Only the member who got it can use this button.", ephemeral=True)
            return
        core = interaction.client.np_core
        what = "Mute" if self.kind == "m" else "Verbal warning"
        embed = discord.Embed(title=("🛑 Appeal" if self.mode == "ap" else "💬 Feedback") + f": {what} #{self.pid}",
                              color=0xE74C3C if self.mode == "ap" else 0x00C8E6, timestamp=now_utc())
        embed.add_field(name="Player", value=f"**{esc(row['ign'])}**", inline=True)
        embed.add_field(name="From", value=interaction.user.mention, inline=True)
        embed.add_field(name="Punishment reason", value=(row["reason"] or "-")[:500], inline=False)
        embed.add_field(name="Message", value=self.text.value[:1000], inline=False)
        view = discord.ui.View(timeout=None)
        view.add_item(AppealReviewButton("ok", interaction.user.id))
        view.add_item(AppealReviewButton("tk", interaction.user.id))
        posted = await core.post_to("APPEALS_CHANNEL_ID", embed=embed, view=view, thread_name="Appeals and feedback",
                                    thread_key="appeals_thread")
        if not posted:
            await interaction.response.send_message(
                "I couldn't send it to the staff (the appeals channel is not set or I can't write there). "
                "Please contact a staff member.", ephemeral=True)
            return
        await interaction.response.send_message("✅ Your message was sent to the staff. They may contact you.",
                                                ephemeral=True)


class AppealButton(discord.ui.DynamicItem[discord.ui.Button], template=r"apl:(?P<mode>fb|ap):(?P<kind>m|w):(?P<pid>\d+)"):
    def __init__(self, mode, kind, pid):
        super().__init__(discord.ui.Button(
            label="Appeal" if mode == "ap" else "Give feedback",
            style=discord.ButtonStyle.danger if mode == "ap" else discord.ButtonStyle.secondary,
            custom_id=f"apl:{mode}:{kind}:{pid}"))
        self.mode, self.kind, self.pid = mode, kind, int(pid)

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["mode"], match["kind"], int(match["pid"]))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(AppealModal(self.mode, self.kind, self.pid))


class AppealReviewButton(discord.ui.DynamicItem[discord.ui.Button], template=r"apr:(?P<act>ok|tk):(?P<uid>\d+)"):
    def __init__(self, act, uid):
        super().__init__(discord.ui.Button(
            label="Mark as reviewed" if act == "ok" else "Open ticket",
            style=discord.ButtonStyle.success if act == "ok" else discord.ButtonStyle.primary,
            custom_id=f"apr:{act}:{uid}"))
        self.act, self.uid = act, int(uid)

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["act"], int(match["uid"]))

    async def callback(self, interaction: discord.Interaction):
        if not is_guild_staff(interaction.user):
            await interaction.response.send_message("Only guild staff can do this.", ephemeral=True)
            return
        msg = interaction.message
        if self.act == "tk":
            try:
                thread = await msg.create_thread(name=f"Ticket {msg.embeds[0].fields[0].value.strip('*')}"[:100])
                await thread.send(f"<@{self.uid}> the staff would like to talk about your message. {interaction.user.mention}")
                await interaction.response.send_message(f"Ticket opened: {thread.mention}", ephemeral=True)
            except (discord.HTTPException, IndexError, AttributeError):
                await interaction.response.send_message("I couldn't open a thread here: open a ticket by hand.",
                                                        ephemeral=True)
            return
        embed = msg.embeds[0]
        embed.set_footer(text=f"Reviewed by {interaction.user.display_name}")
        await interaction.response.edit_message(embed=embed, view=None)


def appeal_view(kind, pid):
    view = discord.ui.View(timeout=None)
    view.add_item(AppealButton("fb", kind, pid))
    view.add_item(AppealButton("ap", kind, pid))
    return view


def mute_ping():
    uid, rid = _env("MUTE_PING_USER_ID"), _env("MUTE_PING_ROLE_ID")
    if uid.isdigit():
        return f"<@{uid}>"
    if rid.isdigit():
        return f"<@&{rid}>"
    return ""


MUTE_MENTIONS = discord.AllowedMentions(users=True, roles=True)
WARN_EXPIRY_DAYS = _int_env("WARN_EXPIRY_DAYS", 60)       # 0 = verbal warnings never expire


def warn_state(w):
    """Text with a live Discord timestamp: when a verbal warning expires (or expired)."""
    if not w.get("expires_at"):
        return "never expires"
    end = from_iso(w["expires_at"])
    ts = int(end.timestamp())
    return f"expired <t:{ts}:R>" if end <= now_utc() else f"expires <t:{ts}:F> (<t:{ts}:R>)"


class MuteCog(commands.GroupCog, name="mute", description="Guild mute logs"):
    def __init__(self, core: Core):
        self.core = core
        super().__init__()

    async def target_id(self, uuid, user):
        """Who is pinged and gets the role: the member given with `user`, else the linked Discord account."""
        if user is not None:
            return user.id
        return await self.core.linked_discord(uuid)

    async def bridge_role(self, did, give: bool):
        """Gives (mute) or removes (mute over) the 'No Guild Bridge' role. The role is MUTE_ROLE_ID, or the role
        named 'No Guild Bridge'. Returns True when it worked, False when it could not be done."""
        if not did:
            return False
        rid = _env("MUTE_ROLE_ID")
        for guild in self.core.bot.guilds:
            role = guild.get_role(int(rid)) if rid.isdigit() else \
                next((r for r in guild.roles if r.name.lower() == "no guild bridge"), None)
            if role is None:
                continue
            try:
                member = guild.get_member(int(did)) or await guild.fetch_member(int(did))
            except discord.HTTPException:
                continue
            try:
                await (member.add_roles(role, reason="Guild bridge mute") if give
                       else member.remove_roles(role, reason="Guild bridge mute ended"))
                return True
            except discord.HTTPException as e:
                log.warning("Could not change the No Guild Bridge role of %s: %s", did, e)
                return False
        return False

    @app_commands.command(name="add", description="Mute a player from the guild bridge: role, ping and log (guild staff)")
    @app_commands.describe(ign="Minecraft username", length="3d, 12h, 1d12h, 2w, 30m or perm", reason="Why they were muted",
                           user="The Discord member to ping and give the role to (only if they are not linked)")
    @app_commands.check(staff_check)
    async def add(self, interaction: discord.Interaction, ign: str, length: str, reason: str,
                  user: Optional[discord.Member] = None):
        await interaction.response.defer()
        td = parse_mute_length(length)
        if td is None:
            await interaction.followup.send("I couldn't read the length. Use `3d`, `12h`, `1d12h`, `2w`, `30m` or `perm`.")
            return
        r = await lookup(interaction, ign)
        if r is None:
            return
        uuid, name = r
        if await q_one("SELECT id FROM mutes WHERE uuid = ? AND status = 'active'", (uuid,)):
            await interaction.followup.send(f"**{esc(name)}** already has an active mute. Use `/mute end` first.")
            return
        now = now_utc()
        end = None if td == "perm" else now + td
        did = await self.target_id(uuid, user)
        mid = await q_run("INSERT INTO mutes (uuid, ign, reason, start_at, end_at, status, added_by, discord_id) "
                          "VALUES (?, ?, ?, ?, ?, 'active', ?, ?)",
                          (uuid, name, reason, iso(now), iso(end) if end else None, str(interaction.user.id),
                           str(did) if did else None))
        gave = await self.bridge_role(did, give=True)
        total_mutes = (await q_one("SELECT COUNT(*) AS c FROM mutes WHERE uuid = ?", (uuid,)))["c"]
        head = await self.core.head_for(uuid, name)
        await load_flag(uuid, name)
        card = await asyncio.to_thread(
            draw_punish_card, "MUTE", name, head, reason,
            [("LENGTH", "Permanent" if end is None else gc.fmt_duration(td.total_seconds())),
             ("EXPIRES", "Never" if end is None else gc.fmt_et(end)), ("MUTES SO FAR", str(total_mutes))],
            interaction.user.display_name, mid)
        if did:
            when = "This mute does not expire." if end is None else f"It expires <t:{int(end.timestamp())}:F> (<t:{int(end.timestamp())}:R>)."
            text = (f"<@{did}> you have been muted in the guild bridge. {when} "
                    + ("The **No Guild Bridge** role was added: you can't write there until the mute ends."
                       if gave else "Staff will remove your guild bridge permissions."))
        else:
            text = (f"**{esc(name)}** has been muted in the guild bridge, but I don't know their Discord account: "
                    "link it with `/link` or give it with the `user` option.")
        if not gave:
            text += "\nThe **No Guild Bridge** role could not be added automatically: please add it manually."
        content = f"{mute_ping()} {text}".strip()
        posted = await self.core.post_to("MUTE_LOG_CHANNEL_ID", content=content, data=card, filename="mute.png",
                                         mentions=MUTE_MENTIONS, view=appeal_view("m", mid))
        if not posted:
            await interaction.followup.send(content=content, file=send_file(card, "mute.png"), allowed_mentions=MUTE_MENTIONS,
                                            view=appeal_view("m", mid))
        else:
            await interaction.followup.send(f"Mute logged in {posted}.")

    @app_commands.command(name="warn", description="Log a verbal warning: it changes nothing, it is only recorded (guild staff)")
    @app_commands.describe(ign="Minecraft username", reason="Why they got the warning",
                           user="The Discord member to ping (only if they are not linked)")
    @app_commands.check(staff_check)
    async def warn(self, interaction: discord.Interaction, ign: str, reason: str, user: Optional[discord.Member] = None):
        await interaction.response.defer()
        r = await lookup(interaction, ign)
        if r is None:
            return
        uuid, name = r
        did = await self.target_id(uuid, user)
        now = now_utc()
        expires = now + timedelta(days=WARN_EXPIRY_DAYS) if WARN_EXPIRY_DAYS > 0 else None
        wid = await q_run("INSERT INTO warnings (uuid, ign, reason, at, added_by, discord_id, expires_at) "
                          "VALUES (?, ?, ?, ?, ?, ?, ?)",
                          (uuid, name, reason, iso(now), str(interaction.user.id), str(did) if did else None,
                           iso(expires) if expires else None))
        count = (await q_one("SELECT COUNT(*) AS c FROM warnings WHERE uuid = ?", (uuid,)))["c"]
        head = await self.core.head_for(uuid, name)
        await load_flag(uuid, name)
        card = await asyncio.to_thread(
            draw_punish_card, "VERBAL WARNING", name, head, reason,
            [("WARNINGS SO FAR", str(count)), ("EXPIRES", gc.fmt_date(expires) if expires else "Never")],
            interaction.user.display_name, wid)
        who = f"<@{did}> " if did else ""
        content = f"{who}you received a verbal warning. Nothing else happens, it is only recorded.".strip()
        posted = await self.core.post_to("MUTE_LOG_CHANNEL_ID", content=content, data=card, filename="warning.png",
                                         mentions=MUTE_MENTIONS, view=appeal_view("w", wid))
        if not posted:
            await interaction.followup.send(content=content, file=send_file(card, "warning.png"), allowed_mentions=MUTE_MENTIONS,
                                            view=appeal_view("w", wid))
        else:
            await interaction.followup.send(f"Verbal warning logged in {posted}.")

    @app_commands.command(name="end", description="Lift a mute now (guild staff)")
    @app_commands.check(staff_check)
    async def end(self, interaction: discord.Interaction, ign: str):
        await interaction.response.defer()
        r = await lookup(interaction, ign)
        if r is None:
            return
        uuid, name = r
        row = await q_one("SELECT id FROM mutes WHERE uuid = ? AND status = 'active'", (uuid,))
        if not row:
            await interaction.followup.send(f"**{esc(name)}** has no active mute.")
            return
        await q_run("UPDATE mutes SET status = 'ended', ended_at = ? WHERE id = ?", (iso(now_utc()), row["id"]))
        await self.announce_end(uuid, name, row["id"], by=interaction.user.display_name, interaction=interaction)

    async def announce_end(self, uuid, name, mid, *, by, interaction=None):
        row = await q_one("SELECT discord_id FROM mutes WHERE id = ?", (mid,))
        did = (row["discord_id"] if row and row["discord_id"] else None) or await self.core.linked_discord(uuid)
        removed = await self.bridge_role(did, give=False)
        if did:
            text = (f"<@{did}> your mute is over ({by}). "
                    + ("The **No Guild Bridge** role was removed: you can write in the guild bridge again."
                       if removed else "Staff will give you back your guild bridge permissions."))
        else:
            text = f"The mute of **{esc(name)}** (#{mid}) is over ({by})."
        if not removed:
            text += "\nThe **No Guild Bridge** role could not be removed automatically: please remove it manually."
        text = f"{mute_ping()} {text}".strip()
        posted = await self.core.post_to("MUTE_LOG_CHANNEL_ID", content=text, mentions=MUTE_MENTIONS)
        if interaction is not None:
            if posted:
                await interaction.followup.send(f"Mute lifted. Notice sent in {posted}.")
            else:
                await interaction.followup.send(text, allowed_mentions=MUTE_MENTIONS)

    @app_commands.command(name="view", description="Punishment history of a player: every mute and verbal warning")
    @app_commands.describe(ign="Minecraft username")
    async def view(self, interaction: discord.Interaction, ign: str):
        await interaction.response.defer()
        r = await lookup(interaction, ign)
        if r is None:
            return
        uuid, name = r
        rows = await q_all("SELECT * FROM mutes WHERE uuid = ? ORDER BY id DESC", (uuid,))
        warns = await q_all("SELECT * FROM warnings WHERE uuid = ? ORDER BY id DESC", (uuid,))
        if not rows and not warns:
            await interaction.followup.send(f"**{esc(name)}** has no mutes or warnings.", allowed_mentions=NO_MENTIONS)
            return
        events = [(from_iso(x["start_at"]), "mute", x) for x in rows] + [(from_iso(x["at"]), "warn", x) for x in warns]
        events.sort(key=lambda e: e[0], reverse=True)
        lines = []
        for when, kind, x in events:
            day = gc.fmt_date(when)
            reason = (x["reason"] or "-").replace("\n", " ")[:160]
            if kind == "mute":
                state = "🔴 active" if x["status"] == "active" else "ended"
                lines.append(f"**Mute #{x['id']}** · {day} · {mute_length_text(x)} · {state}\n> {reason}")
            else:
                lines.append(f"**Verbal warning #{x['id']}** · {day} · {warn_state(x)}\n> {reason}")
        text, shown = "", 0
        for line in lines:
            if len(text) + len(line) + 1 > 3900:
                break
            text += line + "\n"
            shown += 1
        active_w = sum(1 for w in warns if not w.get("expires_at") or from_iso(w["expires_at"]) > now_utc())
        embed = discord.Embed(title=f"Punishment history of {name}", color=0x00C8E6)
        embed.description = text
        embed.set_thumbnail(url=f"https://mc-heads.net/head/{uuid}")
        embed.add_field(name="Mutes", value=str(len(rows)), inline=True)
        embed.add_field(name="Verbal warnings", value=f"{len(warns)} ({active_w} not expired)", inline=True)
        if shown < len(lines):
            embed.set_footer(text=f"Showing the latest {shown} of {len(lines)} entries")
        await interaction.followup.send(embed=embed, allowed_mentions=NO_MENTIONS)

    @app_commands.command(name="warnexpiry", description="Change when a verbal warning expires (guild staff)")
    @app_commands.describe(warning="Warning number (shown in /mute view)",
                           when="New expiry: 2026/11/30 (ET or MT, see /timezone), or 'never', or a length like 30d")
    @app_commands.check(staff_check)
    async def warnexpiry(self, interaction: discord.Interaction, warning: int, when: str):
        await interaction.response.defer()
        w = await q_one("SELECT * FROM warnings WHERE id = ?", (warning,))
        if w is None:
            await interaction.followup.send(f"Warning #{warning} does not exist.")
            return
        text = when.strip().lower()
        if text in ("never", "none", "perm"):
            end = None
        else:
            td = parse_mute_length(text)
            end = (from_iso(w["at"]) + td) if isinstance(td, timedelta) else gc.parse_et(when)
            if end is None:
                await interaction.followup.send("I couldn't read it. Use `2026/11/30`, `30d` (days from the warning) or `never`.")
                return
        await q_run("UPDATE warnings SET expires_at = ? WHERE id = ?", (iso(end) if end else None, warning))
        w["expires_at"] = iso(end) if end else None
        await interaction.followup.send(f"✅ Warning #{warning} of **{esc(w['ign'])}**: {warn_state(w)}.",
                                        allowed_mentions=NO_MENTIONS)

    @app_commands.command(name="removewarn", description="Delete a verbal warning (guild staff)")
    @app_commands.describe(warning="Warning number (shown in /mute view)")
    @app_commands.check(staff_check)
    async def removewarn(self, interaction: discord.Interaction, warning: int):
        await interaction.response.defer()
        w = await q_one("SELECT * FROM warnings WHERE id = ?", (warning,))
        if w is None:
            await interaction.followup.send(f"Warning #{warning} does not exist.")
            return
        await q_run("DELETE FROM warnings WHERE id = ?", (warning,))
        await interaction.followup.send(f"🗑️ Warning #{warning} of **{esc(w['ign'])}** was deleted.",
                                        allowed_mentions=NO_MENTIONS)

    @app_commands.command(name="removemute", description="Delete a mute from the history, lifting it first if active (guild staff)")
    @app_commands.describe(mute="Mute number (shown in /mute view)")
    @app_commands.check(staff_check)
    async def removemute(self, interaction: discord.Interaction, mute: int):
        await interaction.response.defer()
        m = await q_one("SELECT * FROM mutes WHERE id = ?", (mute,))
        if m is None:
            await interaction.followup.send(f"Mute #{mute} does not exist.")
            return
        note = ""
        if m["status"] == "active":
            did = m["discord_id"] or await self.core.linked_discord(m["uuid"])
            ok = await self.bridge_role(did, give=False)
            note = " The No Guild Bridge role was removed." if ok else " Remove the No Guild Bridge role by hand if they have it."
        await q_run("DELETE FROM mutes WHERE id = ?", (mute,))
        await interaction.followup.send(f"🗑️ Mute #{mute} of **{esc(m['ign'])}** was deleted.{note}",
                                        allowed_mentions=NO_MENTIONS)

    @app_commands.command(name="list", description="Players with an active mute (guild staff)")
    @app_commands.check(staff_check)
    async def list(self, interaction: discord.Interaction):
        await interaction.response.defer()
        rows = await q_all("SELECT * FROM mutes WHERE status = 'active' ORDER BY id")
        if not rows:
            await interaction.followup.send("No active mutes.")
            return
        lines = []
        for r in rows:
            ends = "never" if not r["end_at"] else gc.fmt_et(from_iso(r["end_at"]))
            lines.append(f"**{esc(r['ign'])}** · {mute_length_text(r)} · ends {ends}\n{r['reason'] or '-'}")
        await interaction.followup.send("\n\n".join(lines)[:1900])


# ======================================================================================
# logins
# ======================================================================================

def _clock(minutes):
    h, m = divmod(int(round(minutes)) % 1440, 60)
    return f"{(h % 12) or 12}:{m:02d} {'AM' if h < 12 else 'PM'}"


def _span(seconds):
    m = int(seconds // 60)
    return f"{m // 60}h {m % 60:02d}m" if m >= 60 else f"{m}m"


class LoginsCog(commands.Cog):
    def __init__(self, core):
        self.core = core

    @app_commands.command(name="logins", description="When a player logs in: earliest, latest, every day, average time on (guild staff)")
    @app_commands.describe(ign="Minecraft username", days="How many days back (default 14)")
    @app_commands.check(staff_check)
    async def logins(self, interaction: discord.Interaction, ign: str, days: app_commands.Range[int, 1, 120] = 14):
        await interaction.response.defer()
        r = await lookup(interaction, ign)
        if r is None:
            return
        uuid, name = r
        since_all = await kv_get("logins_since")
        cutoff = now_utc() - timedelta(days=days)
        rows = await q_all("SELECT * FROM logins WHERE uuid = ? AND login_at >= ? ORDER BY login_at", (uuid, iso(cutoff)))
        if not rows:
            tracked = f" (tracking started {gc.fmt_date(from_iso(since_all))})" if since_all else ""
            await interaction.followup.send(
                f"No logins saved for **{esc(name)}** in the last {days} days{tracked}. The bot saves the logins of "
                "guild members every few minutes; players who hide their online status in Hypixel may give no data.",
                allowed_mentions=NO_MENTIONS)
            return
        by_day = {}
        sessions = []
        for x in rows:
            t = from_iso(x["login_at"]).astimezone(gc.TZ)
            by_day.setdefault(t.date(), []).append((t, x))
            if x["logout_at"]:
                length = (from_iso(x["logout_at"]) - from_iso(x["login_at"])).total_seconds()
                if 0 < length < 86400:
                    sessions.append((t.date(), length))
        mins = [t.hour * 60 + t.minute for lst in by_day.values() for t, _ in lst]
        firsts = [min(t.hour * 60 + t.minute for t, _ in lst) for lst in by_day.values()]
        earliest = min(((t, x) for lst in by_day.values() for t, x in lst), key=lambda p: p[0].hour * 60 + p[0].minute)[0]
        latest = max(((t, x) for lst in by_day.values() for t, x in lst), key=lambda p: p[0].hour * 60 + p[0].minute)[0]
        per_day = {}
        for d, ln in sessions:
            per_day[d] = per_day.get(d, 0) + ln
        embed = discord.Embed(title=f"Logins of {name}", color=0x00C8E6)
        embed.description = (
            f"Last {days} days · active on **{len(by_day)}** days · **{len(rows)}** logins (times are {gc.TZ_NAME})")
        embed.add_field(name="Earliest login", value=f"{_clock(earliest.hour * 60 + earliest.minute)}\n{earliest.strftime('%m/%d')}")
        embed.add_field(name="Latest login", value=f"{_clock(latest.hour * 60 + latest.minute)}\n{latest.strftime('%m/%d')}")
        embed.add_field(name="Usual first login", value=_clock(sum(firsts) / len(firsts)))
        if sessions:
            embed.add_field(name="Average session", value=_span(sum(l for _, l in sessions) / len(sessions)))
            embed.add_field(name="Average time on per active day", value=_span(sum(per_day.values()) / len(per_day)))
        embed.add_field(name="Logins per active day", value=f"{len(rows) / len(by_day):.1f}")
        lines = []
        for d in sorted(by_day, reverse=True)[:20]:
            times = ", ".join(_clock(t.hour * 60 + t.minute) for t, _ in by_day[d])
            on = f" · on {_span(per_day[d])}" if d in per_day else ""
            lines.append(f"**{d.strftime('%a %m/%d')}**: {times}{on}")
        embed.add_field(name="Every day", value="\n".join(lines)[:1000], inline=False)
        notes = []
        if since_all and from_iso(since_all) > cutoff:
            notes.append(f"the bot has only been saving logins since {gc.fmt_date(from_iso(since_all))}")
        notes.append("logins are saved when the bot checks the player (about every 15 minutes): two logins in between can be merged")
        embed.set_footer(text="Note: " + "; ".join(notes))
        await interaction.followup.send(embed=embed, allowed_mentions=NO_MENTIONS)


# ======================================================================================
# schedule: notices, Road to #N, GEXP snapshot, automatic backup
# ======================================================================================

class Scheduler(commands.Cog):
    def __init__(self, core: Core):
        self.core = core
        self.loop.start()
        self.road_loop.start()
        log.info("The Road to guild post is scheduled every day at %s ET", ROAD_POST_TIME)

    def cog_unload(self):
        self.loop.cancel()
        self.road_loop.cancel()

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
            mutes = getattr(self.core, "mute_cog", None)
            for row in await q_all("SELECT * FROM mutes WHERE status = 'active' AND end_at IS NOT NULL"):
                if from_iso(row["end_at"]) <= now and mutes is not None:
                    await q_run("UPDATE mutes SET status = 'expired', ended_at = ? WHERE id = ?", (iso(now), row["id"]))
                    await mutes.announce_end(row["uuid"], row["ign"], row["id"], by="expired")
        except Exception:
            log.exception("The mute expiry check failed")
        try:
            await self.core.poll_logins()
        except Exception:
            log.exception("The login tracking failed")
        try:
            last = await kv_get("snapshot_at")
            if not last or (now - from_iso(last)).total_seconds() > 5 * 60:
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

    @tasks.loop(seconds=15)
    async def road_loop(self):
        """The Road post is checked every 15 seconds so it goes out at the exact minute (default 11:59 PM ET)."""
        try:
            hh, mm = (int(x) for x in ROAD_POST_TIME.split(":"))
            now = now_utc()
            et_now = datetime.now(gc.ET)
            for day in (et_now.date() - timedelta(days=1), et_now.date()):
                road_et = datetime(day.year, day.month, day.day, hh, mm, tzinfo=gc.ET)
                key = f"road:{day.isoformat()}"
                if not (road_et.astimezone(UTC) <= now < road_et.astimezone(UTC) + timedelta(minutes=30)):
                    continue
                if await kv_get(key):
                    continue
                last_try = await kv_get(f"road_try:{day.isoformat()}")
                if last_try and (now - from_iso(last_try)).total_seconds() < 300:
                    continue                                   # a failed post is tried again every 5 minutes (for 30 minutes)
                await kv_set(f"road_try:{day.isoformat()}", iso(now))
                if await self.core.post_road():
                    await kv_set(key, iso(now))
                else:
                    log.warning("The Road post of %s failed: it will be tried again in 5 minutes", day)
        except Exception:
            log.exception("The Road post failed")

    @road_loop.before_loop
    async def _road_before(self):
        await self.core.bot.wait_until_ready()


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
    saved = await kv_get("weekly_req")
    if saved and saved.isdigit():
        gc.set_weekly_req(int(saved))
    core = Core(bot)
    bot.np_core = core
    g.FLAG_LOADER = _load_flags          # every card with heads (1v1 cards too) shows the flags
    bot.add_dynamic_items(NoticeButton, RequestButton, GuildHistoryButton, AppealButton, AppealReviewButton)
    await bot.add_cog(Breaklist(core))
    await bot.add_cog(Excuse(core))
    await bot.add_cog(GexpTools(core))
    await bot.add_cog(RoadCog(core))
    await bot.add_cog(PurgeCog(core))
    await bot.add_cog(CountryCog(core))
    core.mute_cog = MuteCog(core)
    await bot.add_cog(core.mute_cog)
    await bot.add_cog(LoginsCog(core))
    await bot.add_cog(Scheduler(core))

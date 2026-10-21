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
from PIL import Image, ImageDraw

import graphics as g
from config import STAFF_ROLE, GUILD_NAME, GEXP_WEEKLY_REQ, HYPIXEL_GUILD_NAME
from database import DB_PATH
from dotenv import dotenv_values
from mojang import get_uuid

log = logging.getLogger("tierbot")

def _read_key():
    """The Hypixel API key, read from the .env file or from the environment variable."""
    key = dotenv_values(".env").get("HYPIXEL_API_KEY") or os.getenv("HYPIXEL_API_KEY")
    return key.strip() if key else None

CYCLE_DAYS = 7
ROWS_PER_PAGE = 8
REMINDER = ("Stay guildless while you are on the breaklist. Joining another guild will "
            "permanently remove your ability to use the breaklist.")
NO_MENTIONS = discord.AllowedMentions.none()


# ---------- small helpers ----------

def _is_staff(member) -> bool:
    if not isinstance(member, discord.Member):
        return False
    if member.guild_permissions.administrator:
        return True
    return any(r.name == STAFF_ROLE for r in member.roles)


async def _staff_check(interaction: discord.Interaction) -> bool:
    return _is_staff(interaction.user)


def n(value) -> str:
    return f"{int(value):,}"


def fmt_date(dt) -> str:
    return dt.strftime("%m/%d/%Y")


def fmt_days(days: float) -> str:
    total = int(round(days * 1440))
    d, rest = divmod(total, 1440)
    h, m = divmod(rest, 60)
    return f"{d}d {h}h {m}m"


def fmt_days_short(days: float) -> str:
    total = int(round(days * 1440))
    d, rest = divmod(total, 1440)
    return f"{d}d {rest // 60}h"


def parse_when(text):
    """'MM/DD/YYYY', 'MM/DD/YYYY HH:MM' (also YYYY-MM-DD) -> datetime in UTC, or None."""
    text = (text or "").strip()
    for fmt in ("%m/%d/%Y %H:%M", "%m/%d/%Y", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def calc_required(days: float):
    """Time into the current 7-day cycle and the GEXP required for that time."""
    cycle = days % CYCLE_DAYS
    return cycle, math.ceil(GEXP_WEEKLY_REQ / 7 * cycle)


# ---------- database ----------

async def _setup_tables():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
        CREATE TABLE IF NOT EXISTS breaklist (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uuid TEXT NOT NULL,
            ign TEXT NOT NULL,
            added_at TEXT NOT NULL,
            added_by TEXT,
            joined_at TEXT NOT NULL,
            left_at TEXT NOT NULL,
            days REAL NOT NULL,
            gexp INTEGER NOT NULL,
            required INTEGER NOT NULL,
            missing INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'active'
        );
        CREATE TABLE IF NOT EXISTS gexp_history (
            uuid TEXT NOT NULL,
            day TEXT NOT NULL,
            gexp INTEGER NOT NULL,
            PRIMARY KEY (uuid, day)
        );
        CREATE TABLE IF NOT EXISTS guild_seen (
            uuid TEXT PRIMARY KEY,
            joined_at TEXT,
            rank TEXT,
            last_seen TEXT
        );
        """)
        await db.commit()


async def _one(sql, params=()):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(sql, params)
        row = await cur.fetchone()
        return dict(row) if row else None


async def _all(sql, params=()):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(sql, params)
        return [dict(r) for r in await cur.fetchall()]


async def _run(sql, params=()):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(sql, params)
        await db.commit()
        return cur.lastrowid


# ---------- Hypixel guild API ----------

async def api_guild(params: dict):
    """Returns the 'guild' object of the Hypixel API, or None (no key, not found, error)."""
    key = _read_key()
    if not key:
        return None
    try:
        timeout = aiohttp.ClientTimeout(total=15)
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


async def _lookup(interaction: discord.Interaction, ign: str):
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


# ---------- cards ----------

def _box(d, x, y, w, h, label, value, color=None, size=32):
    d.rounded_rectangle([x, y, x + w, y + h], radius=14, fill=g.ROW_BG)
    d.text((x + w // 2, y + 30), label, font=g.get_font(15, True), fill=g.MUTED, anchor="mm")
    d.text((x + w // 2, y + 72), value, font=g.get_font(size, True), fill=color or g.TEXT, anchor="mm")


def _draw_notice(e, head, title):
    tmp = ImageDraw.Draw(Image.new("RGB", (g.WIDTH, 10)))
    font = g.get_font(19)
    inner_w = g.WIDTH - 2 * (g.PAD + 28)
    cycle = e["days"] % CYCLE_DAYS
    p1 = (f"Time in the guild: {fmt_days(e['days'])}. Time into the current {CYCLE_DAYS}-day cycle: "
          f"{fmt_days(cycle)}, which is {n(e['required'])} GEXP of the {n(GEXP_WEEKLY_REQ)} weekly requirement.")
    lines1 = g._wrap(tmp, p1, font, inner_w)
    lines2 = g._wrap(tmp, REMINDER, font, inner_w)

    id_h, box_h, gap = 150, 110, 14
    note_h = 24 + 30 + len(lines1) * 28 + 10 + len(lines2) * 28 + 20
    top = g.HEADER_H + 16
    height = top + id_h + gap + box_h + gap + box_h + gap + note_h + 16 + g.FOOTER_H
    img = Image.new("RGB", (g.WIDTH, height), g.BG)
    d = ImageDraw.Draw(img)
    g._header(d, title, f"{GUILD_NAME} · breaklist")

    y = top
    d.rounded_rectangle([g.PAD, y, g.WIDTH - g.PAD, y + id_h], radius=16, fill=g.ROW_BG)
    d.rounded_rectangle([g.PAD, y, g.PAD + 8, y + id_h], radius=4, fill=g.ACCENT)
    g._paste_head(img, d, head, g.PAD + 34, y + 25, 100, radius=12)
    nx = g.PAD + 34 + 100 + 26
    d.text((nx, y + 56), e["ign"], font=g.get_font(42, True), fill=g.TEXT, anchor="lm")
    active = e["status"] == "active"
    g._badge(d, nx + 85, y + 112, "ON BREAKLIST" if active else e["status"].upper(),
             g.ACCENT if active else g.MUTED, w=170, h=34, font_size=16)

    bw = (g.WIDTH - 2 * g.PAD - 2 * gap) // 3
    y += id_h + gap
    missing = e["missing"]
    boxes1 = [("GEXP EARNED", n(e["gexp"]), None),
              ("GEXP REQUIRED", n(e["required"]), None),
              ("MISSING (OWED ON REJOIN)", n(missing), g.RED if missing > 0 else g.GREEN)]
    for i, (label, value, color) in enumerate(boxes1):
        _box(d, g.PAD + i * (bw + gap), y, bw, box_h, label, value, color)

    y += box_h + gap
    joined = datetime.fromisoformat(e["joined_at"])
    left = datetime.fromisoformat(e["left_at"])
    boxes2 = [("JOINED THE GUILD", fmt_date(joined)), ("BREAKLISTED", fmt_date(left)),
              ("TIME IN GUILD", fmt_days(e["days"]))]
    for i, (label, value) in enumerate(boxes2):
        _box(d, g.PAD + i * (bw + gap), y, bw, box_h, label, value, size=28)

    y += box_h + gap
    d.rounded_rectangle([g.PAD, y, g.WIDTH - g.PAD, y + note_h], radius=16, fill=g.ROW_BG)
    x = g.PAD + 28
    ty = y + 24
    d.text((x, ty), "NOTICE", font=g.get_font(18, True), fill=g.ACCENT, anchor="lt")
    ty += 30
    for line in lines1:
        d.text((x, ty), line, font=font, fill=g.SOFT, anchor="lt")
        ty += 28
    ty += 10
    for line in lines2:
        d.text((x, ty), line, font=font, fill=g.TEXT, anchor="lt")
        ty += 28

    g._footer(d, height, f"Added {fmt_date(datetime.fromisoformat(e['added_at']))}")
    return g._png(img)


def _draw_list(rows, heads, page, pages, total):
    top = g.HEADER_H + 40
    height = top + len(rows) * g.ROW_H + g.FOOTER_H
    img = Image.new("RGB", (g.WIDTH, height), g.BG)
    d = ImageDraw.Draw(img)
    g._header(d, "BREAKLIST", f"{GUILD_NAME} · {total} players on the breaklist")

    cols = [("ADDED", 500), ("IN GUILD", 612), ("GEXP", 708), ("MISSING", 800)]
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

        values = [(fmt_date(datetime.fromisoformat(e["added_at"])), g.TEXT),
                  (fmt_days_short(e["days"]), g.TEXT),
                  (n(e["gexp"]), g.TEXT),
                  (n(e["missing"]), g.RED if e["missing"] > 0 else g.GREEN)]
        for (_label, x), (text, color) in zip(cols, values):
            d.text((x, cy), text, font=g.get_font(18, True), fill=color, anchor="mm")

    g._footer(d, height, f"Page {page}/{pages}")
    return g._png(img)


def _draw_elig(name, head, days, total, owed, source):
    """days: list of (day 'YYYY-MM-DD', gexp), oldest first."""
    req = GEXP_WEEKLY_REQ
    daily = req / 7
    ok = total >= req
    id_h = 130
    rows_h = 60 + len(days) * 38 + 12
    info_h = 72 if owed is not None else 0
    top = g.HEADER_H + 16
    height = top + id_h + 14 + rows_h + ((14 + info_h) if info_h else 0) + 16 + g.FOOTER_H
    img = Image.new("RGB", (g.WIDTH, height), g.BG)
    d = ImageDraw.Draw(img)
    g._header(d, "GEXP ELIGIBILITY", f"{GUILD_NAME} · last 7 days")

    y = top
    d.rounded_rectangle([g.PAD, y, g.WIDTH - g.PAD, y + id_h], radius=16, fill=g.ROW_BG)
    d.rounded_rectangle([g.PAD, y, g.PAD + 8, y + id_h], radius=4, fill=g.GREEN if ok else g.RED)
    g._paste_head(img, d, head, g.PAD + 34, y + 20, 90, radius=12)
    nx = g.PAD + 34 + 90 + 24
    d.text((nx, y + 44), name, font=g.get_font(36, True), fill=g.TEXT, anchor="lm")
    label = "QUALIFIES" if ok else f"MISSING {n(req - total)}"
    g._badge(d, nx + 100, y + 96, label, g.GREEN if ok else g.RED, w=200, h=34, font_size=16)
    d.text((g.WIDTH - g.PAD - 30, y + 52), n(total), font=g.get_font(44, True), fill=g.TEXT, anchor="rm")
    d.text((g.WIDTH - g.PAD - 30, y + 98), f"of {n(req)} GEXP", font=g.get_font(18), fill=g.MUTED, anchor="rm")

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
        d.text((g.PAD + 24, y + 24), "ON THE BREAKLIST", font=g.get_font(15, True), fill=g.ACCENT, anchor="lm")
        d.text((g.PAD + 24, y + 50), f"{n(owed)} GEXP owed when rejoining the guild",
               font=g.get_font(20, True), fill=g.TEXT, anchor="lm")

    g._footer(d, height, source)
    return g._png(img)


# ---------- the cog ----------

class Breaklist(commands.GroupCog, name="breaklist", description="Guild breaklist and GEXP tools"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._cache = (0.0, {})       # (time, {uuid: member}) of the guild
        super().__init__()
        self.snapshot_loop.start()

    async def cog_load(self):
        await _setup_tables()

    def cog_unload(self):
        self.snapshot_loop.cancel()

    # ----- GEXP data -----

    async def guild_members(self, force: bool = False):
        """{uuid: member} of the guild (cached for 2 minutes). Also saves the daily GEXP and join dates."""
        stamp, cached = self._cache
        if cached and not force and time.time() - stamp < 120:
            return cached
        if not HYPIXEL_GUILD_NAME:
            return {}
        guild = await api_guild({"name": HYPIXEL_GUILD_NAME})
        if not guild:
            return cached or {}
        members = {m["uuid"]: m for m in guild.get("members", [])}
        self._cache = (time.time(), members)
        await self._save_snapshot(members)
        return members

    async def _save_snapshot(self, members):
        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(DB_PATH) as db:
            for uuid, m in members.items():
                joined = m.get("joined")
                joined_iso = (datetime.fromtimestamp(joined / 1000, tz=timezone.utc).isoformat()
                              if joined else None)
                await db.execute(
                    "INSERT INTO guild_seen (uuid, joined_at, rank, last_seen) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(uuid) DO UPDATE SET "
                    "joined_at = COALESCE(excluded.joined_at, guild_seen.joined_at), "
                    "rank = excluded.rank, last_seen = excluded.last_seen",
                    (uuid, joined_iso, m.get("rank"), now))
                for day, value in (m.get("expHistory") or {}).items():
                    await db.execute(
                        "INSERT INTO gexp_history (uuid, day, gexp) VALUES (?, ?, ?) "
                        "ON CONFLICT(uuid, day) DO UPDATE SET gexp = excluded.gexp",
                        (uuid, day, int(value)))
            await db.commit()

    @tasks.loop(hours=6)
    async def snapshot_loop(self):
        try:
            members = await self.guild_members(force=True)
            log.info("GEXP snapshot saved for %d guild members", len(members))
        except Exception:
            log.exception("The GEXP snapshot failed")

    @snapshot_loop.before_loop
    async def _before_snapshot(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(120)

    async def joined_for(self, uuid):
        m = (self._cache[1] or {}).get(uuid)
        if m and m.get("joined"):
            return datetime.fromtimestamp(m["joined"] / 1000, tz=timezone.utc)
        row = await _one("SELECT joined_at FROM guild_seen WHERE uuid = ?", (uuid,))
        if row and row["joined_at"]:
            return datetime.fromisoformat(row["joined_at"])
        return None

    async def window_sum(self, uuid, first_day, last_day):
        row = await _one(
            "SELECT COALESCE(SUM(gexp), 0) AS total, COUNT(*) AS count FROM gexp_history "
            "WHERE uuid = ? AND day >= ? AND day <= ?", (uuid, first_day, last_day))
        return row["total"], row["count"]

    async def history_for(self, uuid):
        """The last 7 days of GEXP: live from Hypixel if the player is in the guild, else saved."""
        members = await self.guild_members()
        m = members.get(uuid)
        if m and m.get("expHistory"):
            rows = sorted(((day, int(v)) for day, v in m["expHistory"].items()), reverse=True)[:7]
            return rows, "Live from Hypixel"
        rows = await _all("SELECT day, gexp FROM gexp_history WHERE uuid = ? ORDER BY day DESC LIMIT 7", (uuid,))
        return [(r["day"], r["gexp"]) for r in rows], "Saved history"

    # ----- posting -----

    async def _post(self, interaction, env_name, data, filename, *, content=None,
                    thread_name=None, mentions=NO_MENTIONS):
        """Sends the card to the channel set in the environment variable (a channel, a thread, or a forum).
        With no channel configured (tests), it is sent where the command was used. Returns the channel
        mention, or None when it was sent here."""
        raw = os.getenv(env_name, "").strip()
        if raw.isdigit():
            try:
                channel = await self.bot.fetch_channel(int(raw))
                file = discord.File(io.BytesIO(data), filename=filename)
                if isinstance(channel, discord.ForumChannel):
                    await channel.create_thread(name=(thread_name or "Breaklist")[:100],
                                                content=content, file=file)
                else:
                    await channel.send(content=content, file=file, allowed_mentions=mentions)
                return channel.mention
            except discord.HTTPException as e:
                log.warning("Could not post in channel %s: %s", raw, e)
                await interaction.followup.send(
                    "I couldn't post in the configured channel (check its id and the bot's permissions). "
                    "Showing it here instead.")
        file = discord.File(io.BytesIO(data), filename=filename)
        await interaction.followup.send(content=content, file=file, allowed_mentions=mentions)
        return None

    # ----- commands -----

    @app_commands.command(name="add", description="Add a player to the breaklist and post the notice (staff only)")
    @app_commands.describe(
        ign="Minecraft username of the player",
        gexp="GEXP earned in the current cycle (default: taken from the saved Hypixel data)",
        missing="GEXP owed when rejoining (default: calculated from the requirement)",
        start="When they joined the guild, MM/DD/YYYY or MM/DD/YYYY HH:MM (default: from Hypixel)",
        end="When they were breaklisted, MM/DD/YYYY or MM/DD/YYYY HH:MM (default: now)")
    @app_commands.check(_staff_check)
    async def add(self, interaction: discord.Interaction, ign: str,
                  gexp: Optional[app_commands.Range[int, 0, 100_000_000]] = None,
                  missing: Optional[app_commands.Range[int, 0, 100_000_000]] = None,
                  start: Optional[str] = None, end: Optional[str] = None):
        await interaction.response.defer()
        r = await _lookup(interaction, ign)
        if r is None:
            return
        uuid, name = r
        safe = discord.utils.escape_markdown(name)

        if await _one("SELECT id FROM breaklist WHERE uuid = ? AND status = 'active'", (uuid,)):
            await interaction.followup.send(
                f"**{safe}** is already on the breaklist. Use `/breaklist remove` first to add them again.")
            return

        await self.guild_members()    # refreshes the saved GEXP and join dates when possible

        if start:
            start_dt = parse_when(start)
            if start_dt is None:
                await interaction.followup.send("I couldn't read the start date. Use MM/DD/YYYY or MM/DD/YYYY HH:MM.")
                return
        else:
            start_dt = await self.joined_for(uuid)
            if start_dt is None:
                await interaction.followup.send(
                    "I couldn't find when they joined the guild. Give it with the start option (MM/DD/YYYY).")
                return
        if end:
            end_dt = parse_when(end)
            if end_dt is None:
                await interaction.followup.send("I couldn't read the end date. Use MM/DD/YYYY or MM/DD/YYYY HH:MM.")
                return
        else:
            end_dt = datetime.now(timezone.utc)

        days = (end_dt - start_dt).total_seconds() / 86400
        if days <= 0:
            await interaction.followup.send("The end date must be after the start date.")
            return
        cycle, required = calc_required(days)

        if gexp is None:
            cycle_start = start_dt + timedelta(days=CYCLE_DAYS * int(days // CYCLE_DAYS))
            total, count = await self.window_sum(uuid, cycle_start.date().isoformat(), end_dt.date().isoformat())
            if count == 0:
                await interaction.followup.send(
                    "I have no saved GEXP for this player in that period. Give it with the gexp option.")
                return
            gexp = total
        if missing is None:
            missing = max(0, required - gexp)

        entry_id = await _run(
            "INSERT INTO breaklist (uuid, ign, added_at, added_by, joined_at, left_at, days, gexp, required, missing) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (uuid, name, datetime.now(timezone.utc).isoformat(), str(interaction.user.id),
             start_dt.isoformat(), end_dt.isoformat(), days, gexp, required, missing))
        entry = await _one("SELECT * FROM breaklist WHERE id = ?", (entry_id,))

        heads = await g._fetch_heads([(uuid, name)])
        data = await asyncio.to_thread(_draw_notice, entry, heads.get(uuid), "BREAKLISTED")
        posted = await self._post(interaction, "BREAKLIST_CHANNEL_ID", data, "breaklist.png", thread_name=name)
        if posted:
            await interaction.followup.send(f"✅ **{safe}** was added to the breaklist. The notice was posted in {posted}.")

    @app_commands.command(name="view", description="Show a player's breaklist entry")
    @app_commands.describe(ign="Minecraft username of the player")
    async def view(self, interaction: discord.Interaction, ign: str):
        await interaction.response.defer()
        r = await _lookup(interaction, ign)
        if r is None:
            return
        uuid, name = r
        entry = await _one("SELECT * FROM breaklist WHERE uuid = ? ORDER BY id DESC LIMIT 1", (uuid,))
        if entry is None:
            await interaction.followup.send(f"**{discord.utils.escape_markdown(name)}** is not on the breaklist.")
            return
        heads = await g._fetch_heads([(uuid, entry["ign"])])
        data = await asyncio.to_thread(_draw_notice, entry, heads.get(uuid), "BREAKLIST ENTRY")
        await interaction.followup.send(file=discord.File(io.BytesIO(data), filename="breaklist.png"))

    @app_commands.command(name="guild", description="List everyone who is on the breaklist")
    @app_commands.describe(page="Page number")
    async def guild(self, interaction: discord.Interaction, page: app_commands.Range[int, 1, 100] = 1):
        await interaction.response.defer()
        entries = await _all("SELECT * FROM breaklist WHERE status = 'active' ORDER BY added_at DESC")
        if not entries:
            await interaction.followup.send("Nobody is on the breaklist.")
            return
        pages = max(1, math.ceil(len(entries) / ROWS_PER_PAGE))
        page = min(page, pages)
        chunk = entries[(page - 1) * ROWS_PER_PAGE: page * ROWS_PER_PAGE]
        heads = await g._fetch_heads([(e["uuid"], e["ign"]) for e in chunk])
        data = await asyncio.to_thread(_draw_list, chunk, heads, page, pages, len(entries))
        await interaction.followup.send(file=discord.File(io.BytesIO(data), filename="breaklist_guild.png"))

    @app_commands.command(name="remove", description="Take a player off the breaklist, for example when they rejoin (staff only)")
    @app_commands.describe(ign="Minecraft username of the player")
    @app_commands.check(_staff_check)
    async def remove(self, interaction: discord.Interaction, ign: str):
        await interaction.response.defer()
        r = await _lookup(interaction, ign)
        if r is None:
            return
        uuid, name = r
        entry = await _one("SELECT id FROM breaklist WHERE uuid = ? AND status = 'active'", (uuid,))
        if entry is None:
            await interaction.followup.send(f"**{discord.utils.escape_markdown(name)}** is not on the breaklist.")
            return
        await _run("UPDATE breaklist SET status = 'removed' WHERE id = ?", (entry["id"],))
        await interaction.followup.send(f"✅ **{discord.utils.escape_markdown(name)}** was removed from the breaklist.")

    async def _gexp_card(self, uuid, name):
        """Builds the eligibility card. Returns (png bytes, total) or (None, None) when there is no data."""
        rows, source = await self.history_for(uuid)
        if not rows:
            return None, None
        days = sorted(rows)
        total = sum(v for _, v in days)
        entry = await _one("SELECT missing FROM breaklist WHERE uuid = ? AND status = 'active'", (uuid,))
        owed = entry["missing"] if entry else None
        heads = await g._fetch_heads([(uuid, name)])
        data = await asyncio.to_thread(_draw_elig, name, heads.get(uuid), days, total, owed, source)
        return data, total

    @app_commands.command(name="eligibility", description="Show the last 7 days of GEXP and whether a player meets the requirement")
    @app_commands.describe(ign="Minecraft username of the player")
    async def eligibility(self, interaction: discord.Interaction, ign: str):
        await interaction.response.defer()
        r = await _lookup(interaction, ign)
        if r is None:
            return
        uuid, name = r
        data, _total = await self._gexp_card(uuid, name)
        if data is None:
            await interaction.followup.send(
                "I have no GEXP data for this player. The bot needs the Hypixel key and the guild name "
                "(HYPIXEL_GUILD_NAME in config.py), and the player must have been in the guild while "
                "the bot was saving the data.")
            return
        await interaction.followup.send(file=discord.File(io.BytesIO(data), filename="gexp.png"))

    @app_commands.command(name="reminder", description="Post a GEXP reminder for a player in the inactivity channel (staff only)")
    @app_commands.describe(ign="Minecraft username of the player")
    @app_commands.check(_staff_check)
    async def reminder(self, interaction: discord.Interaction, ign: str):
        await interaction.response.defer()
        r = await _lookup(interaction, ign)
        if r is None:
            return
        uuid, name = r
        data, total = await self._gexp_card(uuid, name)
        if data is None:
            await interaction.followup.send("I have no GEXP data for this player.")
            return
        link = await _one("SELECT discord_id FROM links WHERE uuid = ?", (uuid,))
        if link:
            content = f"<@{link['discord_id']}> here is your GEXP of the last 7 days."
        else:
            content = (f"**{discord.utils.escape_markdown(name)}**, here is your GEXP of the last 7 days. "
                       f"(Their Discord account is not linked: link it with `/link` to ping them.)")
        posted = await self._post(interaction, "INACTIVITY_CHANNEL_ID", data, "gexp.png", content=content,
                                  mentions=discord.AllowedMentions(users=True))
        if posted:
            await interaction.followup.send(f"✅ Reminder posted in {posted}.")


async def setup(bot: commands.Bot):
    await bot.add_cog(Breaklist(bot))

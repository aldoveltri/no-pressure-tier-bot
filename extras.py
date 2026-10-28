"""Extra commands: days in guild, application waiting list, timezone, linked accounts, 1v1 map statistics."""
import asyncio
import io
import logging
from datetime import timedelta
from typing import Optional

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands

import breaklist as bl
import gexpcalc as gc
from breaklist import q_one, q_all, q_run, kv_get, kv_set, esc, now_utc, iso, from_iso, staff_check, lookup, n

log = logging.getLogger("extras")
NO_MENTIONS = discord.AllowedMentions.none()

SCHEMA = """
CREATE TABLE IF NOT EXISTS waitlist (
    uuid TEXT PRIMARY KEY, ign TEXT, added_at TEXT NOT NULL, added_by TEXT,
    status TEXT NOT NULL DEFAULT 'waiting', removed_at TEXT, removed_by TEXT
);
CREATE TABLE IF NOT EXISTS waitlist_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT, uuid TEXT NOT NULL, note TEXT, by TEXT, at TEXT
);
CREATE TABLE IF NOT EXISTS match_maps (match_id INTEGER PRIMARY KEY, map TEXT NOT NULL);
"""


# ======================================================================================
# days in guild
# ======================================================================================

async def days_board(core, page=1):
    """(embed, page, pages) with the members ordered by total days in the guild, or None without the member list."""
    members = await core.guild_members()
    if not members:
        return None
    banked = {r["uuid"]: r["banked"] for r in await q_all("SELECT uuid, banked FROM player_days")}
    now = now_utc()
    rows = []
    for uuid, m in members.items():
        joined = m.get("joined")
        stay = (now.timestamp() - joined / 1000) / 86400 if joined else 0
        rows.append((uuid, banked.get(uuid, 0.0) + max(0.0, stay)))
    rows.sort(key=lambda x: -x[1])
    per = 15
    pages = max(1, -(-len(rows) // per))
    page = min(page, pages)
    chunk = rows[(page - 1) * per: page * per]
    names = await asyncio.gather(*(core.name_for(u) for u, _ in chunk))
    lines = []
    flags = {}
    for r in await q_all("SELECT uuid, code FROM countries"):
        code = (r["code"] or "").strip().upper()
        if len(code) == 2 and code.isalpha():
            flags[r["uuid"]] = " " + "".join(chr(0x1F1E6 + ord(c) - 65) for c in code)
    for i, ((uuid, days), name) in enumerate(zip(chunk, names), (page - 1) * per + 1):
        vet = " ⭐" if days >= bl.VETERAN_DAYS else ""
        lines.append(f"`{i:>3}.` **{esc(name)}**{flags.get(uuid, '')} · {int(days)} days{vet}")
    embed = discord.Embed(title="Days in guild", description="\n".join(lines), color=0x00C8E6)
    embed.set_footer(text=f"Page {page}/{pages} · saved days from the breaklist are included · ⭐ = veteran")
    return embed, page, pages


class DaysView(discord.ui.View):
    """Previous / Next buttons under /days leaderboard."""

    def __init__(self, core, page, pages):
        super().__init__(timeout=900)
        self.core, self.page = core, page
        self.prev = discord.ui.Button(label="◀ Previous", style=discord.ButtonStyle.primary, disabled=page <= 1)
        self.nxt = discord.ui.Button(label="Next ▶", style=discord.ButtonStyle.primary, disabled=page >= pages)
        self.prev.callback, self.nxt.callback = self.go(-1), self.go(1)
        self.add_item(self.prev)
        self.add_item(self.nxt)

    def go(self, delta):
        async def cb(interaction):
            await interaction.response.defer()
            res = await days_board(self.core, self.page + delta)
            if res is None:
                await interaction.edit_original_response(content="I can't read the guild member list right now.", embed=None, view=None)
                return
            embed, page, pages = res
            await interaction.edit_original_response(embed=embed, view=DaysView(self.core, page, pages))
        return cb


class DaysCog(commands.GroupCog, name="days", description="Days spent in the guild"):
    def __init__(self, core):
        self.core = core
        super().__init__()


    @app_commands.command(name="leaderboard", description="Members of the guild ordered by total days in the guild")
    @app_commands.describe(page="Page number")
    async def leaderboard(self, interaction: discord.Interaction, page: app_commands.Range[int, 1, 50] = 1):
        await interaction.response.defer()
        res = await days_board(self.core, page)
        if res is None:
            await interaction.followup.send("I can't read the guild member list right now.")
            return
        await interaction.followup.send(embed=res[0], view=DaysView(self.core, res[1], res[2]))


# ======================================================================================
# waiting list
# ======================================================================================

class WaitlistCog(commands.GroupCog, name="waitlist", description="Guild application waiting list (guild staff)"):
    def __init__(self, core):
        self.core = core
        super().__init__()

    @app_commands.command(name="add", description="Put an applicant on the waiting list")
    @app_commands.describe(ign="Minecraft username", note="Optional note")
    @app_commands.check(staff_check)
    async def add(self, interaction: discord.Interaction, ign: str, note: Optional[str] = None):
        await interaction.response.defer()
        r = await lookup(interaction, ign)
        if r is None:
            return
        uuid, name = r
        row = await q_one("SELECT status FROM waitlist WHERE uuid = ?", (uuid,))
        if row and row["status"] == "waiting":
            await interaction.followup.send(f"**{esc(name)}** is already on the waiting list. Use `/waitlist note`.",
                                            allowed_mentions=NO_MENTIONS)
            return
        await q_run("INSERT INTO waitlist (uuid, ign, added_at, added_by, status) VALUES (?, ?, ?, ?, 'waiting') "
                    "ON CONFLICT(uuid) DO UPDATE SET ign = excluded.ign, added_at = excluded.added_at, "
                    "added_by = excluded.added_by, status = 'waiting', removed_at = NULL, removed_by = NULL",
                    (uuid, name, iso(now_utc()), str(interaction.user.id)))
        if note:
            await q_run("INSERT INTO waitlist_notes (uuid, note, by, at) VALUES (?, ?, ?, ?)",
                        (uuid, note, interaction.user.display_name, iso(now_utc())))
        pos = (await q_one("SELECT COUNT(*) AS c FROM waitlist WHERE status = 'waiting'"))["c"]
        await interaction.followup.send(f"✅ **{esc(name)}** is on the waiting list (number {pos}).",
                                        allowed_mentions=NO_MENTIONS)

    @app_commands.command(name="note", description="Add a note to someone on the waiting list")
    @app_commands.describe(ign="Minecraft username", note="What happened while they waited")
    @app_commands.check(staff_check)
    async def note(self, interaction: discord.Interaction, ign: str, note: str):
        await interaction.response.defer()
        r = await lookup(interaction, ign)
        if r is None:
            return
        uuid, name = r
        if not await q_one("SELECT uuid FROM waitlist WHERE uuid = ?", (uuid,)):
            await interaction.followup.send(f"**{esc(name)}** is not on the waiting list.", allowed_mentions=NO_MENTIONS)
            return
        await q_run("INSERT INTO waitlist_notes (uuid, note, by, at) VALUES (?, ?, ?, ?)",
                    (uuid, note, interaction.user.display_name, iso(now_utc())))
        await interaction.followup.send(f"📝 Note added to **{esc(name)}**.", allowed_mentions=NO_MENTIONS)

    @app_commands.command(name="remove", description="Take someone off the waiting list (accepted, declined, left...)")
    @app_commands.describe(ign="Minecraft username", note="Why they were removed (optional)",
                           image="Optional image (for example a screenshot)")
    @app_commands.check(staff_check)
    async def remove(self, interaction: discord.Interaction, ign: str, note: Optional[str] = None,
                     image: Optional[discord.Attachment] = None):
        await interaction.response.defer()
        r = await lookup(interaction, ign)
        if r is None:
            return
        uuid, name = r
        row = await q_one("SELECT * FROM waitlist WHERE uuid = ? AND status = 'waiting'", (uuid,))
        if row is None:
            await interaction.followup.send(f"**{esc(name)}** is not on the waiting list.", allowed_mentions=NO_MENTIONS)
            return
        await q_run("UPDATE waitlist SET status = 'removed', removed_at = ?, removed_by = ? WHERE uuid = ?",
                    (iso(now_utc()), str(interaction.user.id), uuid))
        if note:
            await q_run("INSERT INTO waitlist_notes (uuid, note, by, at) VALUES (?, ?, ?, ?)",
                        (uuid, f"Removed: {note}", interaction.user.display_name, iso(now_utc())))
        waited = gc.fmt_duration((now_utc() - from_iso(row["added_at"])).total_seconds())
        embed = discord.Embed(title=f"Removed from the waiting list: {name}", color=0xE74C3C)
        embed.set_thumbnail(url=f"https://mc-heads.net/head/{uuid}")
        embed.add_field(name="Waited", value=waited, inline=True)
        embed.add_field(name="By", value=interaction.user.display_name, inline=True)
        if note:
            embed.add_field(name="Note", value=note[:1000], inline=False)
        kwargs = {"embed": embed, "allowed_mentions": NO_MENTIONS}
        if image is not None:
            data = await image.read()
            embed.set_image(url=f"attachment://{image.filename}")
            kwargs["file"] = discord.File(io.BytesIO(data), filename=image.filename)
        await interaction.followup.send(**kwargs)

    @app_commands.command(name="list", description="Everyone on the waiting list, with their latest note")
    @app_commands.check(staff_check)
    async def list(self, interaction: discord.Interaction):
        await interaction.response.defer()
        rows = await q_all("SELECT * FROM waitlist WHERE status = 'waiting' ORDER BY added_at")
        if not rows:
            await interaction.followup.send("The waiting list is empty.")
            return
        lines = []
        for i, w in enumerate(rows, 1):
            last = await q_one("SELECT note FROM waitlist_notes WHERE uuid = ? ORDER BY id DESC LIMIT 1", (w["uuid"],))
            ts = int(from_iso(w["added_at"]).timestamp())
            lines.append(f"**{i}. {esc(w['ign'])}** · since <t:{ts}:D> (<t:{ts}:R>)"
                         + (f"\n> {esc(last['note'][:150])}" if last else ""))
        embed = discord.Embed(title=f"Waiting list ({len(rows)})", description="\n".join(lines)[:4000], color=0x00C8E6)
        await interaction.followup.send(embed=embed, allowed_mentions=NO_MENTIONS)

    @app_commands.command(name="view", description="All notes of an applicant")
    @app_commands.describe(ign="Minecraft username")
    @app_commands.check(staff_check)
    async def view(self, interaction: discord.Interaction, ign: str):
        await interaction.response.defer()
        r = await lookup(interaction, ign)
        if r is None:
            return
        uuid, name = r
        w = await q_one("SELECT * FROM waitlist WHERE uuid = ?", (uuid,))
        if w is None:
            await interaction.followup.send(f"**{esc(name)}** was never on the waiting list.", allowed_mentions=NO_MENTIONS)
            return
        notes = await q_all("SELECT * FROM waitlist_notes WHERE uuid = ? ORDER BY id", (uuid,))
        ts = int(from_iso(w["added_at"]).timestamp())
        text = f"Status: **{w['status']}** · added <t:{ts}:D> (<t:{ts}:R>)\n\n"
        text += "\n".join(f"• <t:{int(from_iso(x['at']).timestamp())}:d> {esc(x['by'] or '')}: {esc(x['note'] or '')}"
                          for x in notes) or "No notes."
        embed = discord.Embed(title=f"Waiting list: {name}", description=text[:4000], color=0x00C8E6)
        embed.set_thumbnail(url=f"https://mc-heads.net/head/{uuid}")
        await interaction.followup.send(embed=embed, allowed_mentions=NO_MENTIONS)


# ======================================================================================
# timezone, linked accounts, map stats
# ======================================================================================

class ToolsCog(commands.Cog):
    def __init__(self, core):
        self.core = core

    @app_commands.command(name="timezone", description="Choose the time zone shown and typed in the commands: ET or MT (guild staff)")
    @app_commands.describe(zone="ET (Eastern) or MT (Mountain). Empty: show the current one")
    @app_commands.choices(zone=[app_commands.Choice(name="ET (Eastern)", value="ET"),
                                app_commands.Choice(name="MT (Mountain)", value="MT")])
    @app_commands.check(staff_check)
    async def timezone(self, interaction: discord.Interaction, zone: Optional[str] = None):
        if zone is None:
            await interaction.response.send_message(f"The time zone is **{gc.TZ_NAME}**.", ephemeral=True)
            return
        gc.set_tz(zone)
        await kv_set("tz", zone)
        await interaction.response.send_message(
            f"✅ Times are now shown and typed in **{gc.TZ_NAME}**. The GEXP week and the purge still follow "
            f"Eastern Time (Tuesday 12:00 AM ET).")

    @app_commands.command(name="links", description="Every linked Discord account, and who linked it (guild staff)")
    @app_commands.describe(page="Page number")
    @app_commands.check(staff_check)
    async def links(self, interaction: discord.Interaction, page: app_commands.Range[int, 1, 100] = 1):
        await interaction.response.defer(ephemeral=True)
        rows = await q_all("SELECT * FROM links ORDER BY ign COLLATE NOCASE")
        if not rows:
            await interaction.followup.send("No linked accounts.", ephemeral=True)
            return
        per = 20
        pages = max(1, -(-len(rows) // per))
        page = min(page, pages)
        lines = []
        for r in rows[(page - 1) * per: page * per]:
            by = r.get("linked_by")
            if not by:
                how = "linked before this was recorded"
            elif r.get("forced"):
                how = f"forced by <@{by}>"
            else:
                how = "linked by themselves (verified)"
            lines.append(f"**{esc(r['ign'])}** ← <@{r['discord_id']}> · {how}")
        embed = discord.Embed(title=f"Linked accounts ({len(rows)})", description="\n".join(lines), color=0x00C8E6)
        embed.set_footer(text=f"Page {page}/{pages}")
        await interaction.followup.send(embed=embed, ephemeral=True, allowed_mentions=NO_MENTIONS)

    # ----- 1v1 map statistics -----

    async def map_choices(self, interaction, current):
        rows = await q_all("SELECT DISTINCT map FROM match_maps ORDER BY map COLLATE NOCASE")
        cur = current.lower()
        return [app_commands.Choice(name=r["map"], value=r["map"]) for r in rows if cur in r["map"].lower()][:25]

    @app_commands.command(name="mapstats", description="Wins, losses and win rate of a player on each 1v1 map")
    @app_commands.describe(ign="Minecraft username (empty: your linked account)", map="Only this map")
    async def mapstats(self, interaction: discord.Interaction, ign: Optional[str] = None, map: Optional[str] = None):
        await interaction.response.defer()
        if ign:
            r = await lookup(interaction, ign)
            if r is None:
                return
            uuid, name = r
        else:
            row = await q_one("SELECT uuid, ign FROM links WHERE discord_id = ?", (str(interaction.user.id),))
            if not row:
                await interaction.followup.send("Link your account with `/link` or type a username.")
                return
            uuid, name = row["uuid"], row["ign"]
        rows = await q_all(
            "SELECT mm.map AS map, "
            "SUM(CASE WHEN m.winner_uuid = ? THEN 1 ELSE 0 END) AS w, "
            "SUM(CASE WHEN m.loser_uuid = ? THEN 1 ELSE 0 END) AS l "
            "FROM matches m JOIN match_maps mm ON mm.match_id = m.id "
            "WHERE m.cancelled = 0 AND m.mode_id IS NULL AND (m.winner_uuid = ? OR m.loser_uuid = ?) "
            "GROUP BY mm.map ORDER BY (w + l) DESC", (uuid, uuid, uuid, uuid))
        if map:
            rows = [r for r in rows if r["map"].lower() == map.lower()]
        if not rows:
            await interaction.followup.send(f"No matches with a map saved for **{esc(name)}**.", allowed_mentions=NO_MENTIONS)
            return
        lines = []
        for r in rows[:20]:
            tot = r["w"] + r["l"]
            lines.append(f"**{esc(r['map'])}** · {r['w']}W {r['l']}L · WLR {r['w'] / max(1, r['l']):.2f} · {r['w'] / tot:.0%}")
        embed = discord.Embed(title=f"Map stats: {name}", description="\n".join(lines), color=0x00C8E6)
        embed.set_thumbnail(url=f"https://mc-heads.net/head/{uuid}")
        embed.set_footer(text="WLR = wins / losses · only matches logged with a map")
        await interaction.followup.send(embed=embed, allowed_mentions=NO_MENTIONS)

    @mapstats.autocomplete("map")
    async def _mapstats_ac(self, interaction, current):
        return await self.map_choices(interaction, current)

    @app_commands.command(name="mapleaderboard", description="Best players on a 1v1 map")
    @app_commands.describe(map="The map", sort="What to order by", page="Page number")
    @app_commands.choices(sort=[app_commands.Choice(name="Most wins", value="wins"),
                                app_commands.Choice(name="Most losses", value="losses"),
                                app_commands.Choice(name="Win rate (min. 3 matches)", value="rate"),
                                app_commands.Choice(name="Most matches", value="matches")])
    async def mapleaderboard(self, interaction: discord.Interaction, map: str, sort: str = "wins",
                             page: app_commands.Range[int, 1, 50] = 1):
        await interaction.response.defer()
        rows = await q_all(
            "SELECT u.uuid AS uuid, COALESCE(p.ign, u.uuid) AS ign, "
            "SUM(u.win) AS w, SUM(1 - u.win) AS l FROM ("
            " SELECT m.winner_uuid AS uuid, 1 AS win FROM matches m JOIN match_maps mm ON mm.match_id = m.id "
            "  WHERE m.cancelled = 0 AND m.mode_id IS NULL AND mm.map = ? COLLATE NOCASE"
            " UNION ALL SELECT m.loser_uuid, 0 FROM matches m JOIN match_maps mm ON mm.match_id = m.id "
            "  WHERE m.cancelled = 0 AND m.mode_id IS NULL AND mm.map = ? COLLATE NOCASE) u "
            "LEFT JOIN players p ON p.uuid = u.uuid GROUP BY u.uuid", (map, map))
        if not rows:
            await interaction.followup.send(f"No matches saved on **{esc(map)}**.", allowed_mentions=NO_MENTIONS)
            return
        keys = {"wins": lambda r: (-r["w"], r["l"]), "losses": lambda r: (-r["l"], r["w"]),
                "matches": lambda r: (-(r["w"] + r["l"]), -r["w"])}
        if sort == "rate":
            rows = [r for r in rows if r["w"] + r["l"] >= 3]
            rows.sort(key=lambda r: (-(r["w"] / (r["w"] + r["l"])), -(r["w"] + r["l"])))
        else:
            rows.sort(key=keys[sort])
        per = 10
        pages = max(1, -(-len(rows) // per))
        page = min(page, pages)
        lines = []
        for i, r in enumerate(rows[(page - 1) * per: page * per], (page - 1) * per + 1):
            lines.append(f"`{i:>2}.` **{esc(r['ign'])}** · {r['w']}W {r['l']}L · {r['w'] / max(1, r['l']):.2f} WLR · "
                         f"{r['w'] / (r['w'] + r['l']):.0%}")
        embed = discord.Embed(title=f"Map leaderboard: {map}", description="\n".join(lines) or "Nobody yet.", color=0x00C8E6)
        embed.set_footer(text=f"Page {page}/{pages} · ordered by {sort}")
        await interaction.followup.send(embed=embed, allowed_mentions=NO_MENTIONS)

    @mapleaderboard.autocomplete("map")
    async def _mapleaderboard_ac(self, interaction, current):
        return await self.map_choices(interaction, current)


async def setup(bot: commands.Bot):
    async with aiosqlite.connect(bl.DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()
    saved = await kv_get("tz")
    if saved:
        gc.set_tz(saved)
    core = bot.np_core
    await bot.add_cog(DaysCog(core))
    await bot.add_cog(WaitlistCog(core))
    await bot.add_cog(ToolsCog(core))

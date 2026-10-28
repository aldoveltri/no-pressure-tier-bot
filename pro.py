"""Pro rank upkeep: a pro must beat a C+ (or higher) player in a 1v1 every 30 days.
Reminders at day 15 and day 23 (to the player) and at day 30 (to the guild master: demote)."""
import asyncio
import logging
from datetime import timedelta
from typing import Optional

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands, tasks

import alerts
import breaklist as bl
from breaklist import q_one, q_all, q_run, kv_get, kv_set, esc, now_utc, iso, from_iso, lookup, staff_check
from database import DB_PATH

log = logging.getLogger("pro")
CPLUS_ELO = 130                                   # C+ starts at 130 elo
LIMIT_DAYS = 30
STAGES = (30, 23, 15)                             # highest first
STAGE_KEYS = {15: "pro_day15", 23: "pro_day23", 30: "pro_day30"}
SCHEMA = "CREATE TABLE IF NOT EXISTS pro_players (uuid TEXT PRIMARY KEY, ign TEXT NOT NULL, added_at TEXT NOT NULL);"


async def last_cplus_win(uuid):
    """played_at of the latest non-cancelled normal-mode win against someone who was C+ or higher."""
    row = await q_one("SELECT MAX(played_at) AS t FROM matches WHERE cancelled = 0 AND winner_uuid = ? "
                      "AND mode_id IS NULL AND loser_elo_before >= ?", (uuid, CPLUS_ELO))
    return from_iso(row["t"]) if row and row["t"] else None


async def anchor_of(p):
    """The 30 days run from the last C+ win, or from the day the player was added if there is none."""
    added = from_iso(p["added_at"])
    win = await last_cplus_win(p["uuid"])
    return max(added, win) if win else added


def stage_due(days):
    return next((s for s in STAGES if days >= s), None)


class ProCog(commands.GroupCog, name="pro"):
    def __init__(self, bot):
        self.bot = bot
        self.loop.start()

    def cog_unload(self):
        self.loop.cancel()

    async def ping_for(self, uuid):
        row = await q_one("SELECT discord_id FROM links WHERE uuid = ?", (uuid,))
        return f"<@{row['discord_id']}>" if row else None

    @tasks.loop(minutes=30)
    async def loop(self):
        try:
            await self.run_checks()
        except Exception:
            log.exception("The pro reminders failed")

    @loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(120)

    async def run_checks(self):
        now = now_utc()
        for p in await q_all("SELECT * FROM pro_players"):
            anchor = await anchor_of(p)
            days = (now - anchor).total_seconds() / 86400
            top = stage_due(days)
            if top is None:
                continue
            base = f"pro:{p['uuid']}:{iso(anchor)}"
            if await kv_get(f"{base}:{top}"):
                continue
            for s in STAGES:                                 # lower stages are skipped once a higher one is due
                if s <= top:
                    await kv_set(f"{base}:{s}", iso(now))
            if not await alerts.enabled(STAGE_KEYS[top]):
                continue
            await self.send_reminder(p, top, days, anchor)

    async def send_reminder(self, p, stage, days, anchor):
        name = esc(p["ign"])
        until = int((anchor + timedelta(days=LIMIT_DAYS)).timestamp())
        if stage == 30:
            user, role = bl._env("PRO_PING_USER_ID") or bl._env("BETRAYAL_PING_USER_ID"), bl._env("PRO_PING_ROLE_ID")
            ping = f"<@{user}>" if user.isdigit() else f"<@&{role}>" if role.isdigit() else ""
            text = (f"{ping} ⬇️ **{name}** has not beaten a C+ tier player in {LIMIT_DAYS} days. "
                    "Time to demote them from Pro.").strip()
        else:
            ping = await self.ping_for(p["uuid"]) or f"**{name}**"
            text = (f"{ping} ⏰ Reminder: you have to beat a C+ tier player (or higher) in a 1v1 to keep your Pro rank. "
                    f"It has been {int(days)} days since your last one. Deadline: <t:{until}:F> (<t:{until}:R>).")
        env = "PRO_CHANNEL_ID" if bl.chan_id("PRO_CHANNEL_ID") else "BETRAYAL_CHANNEL_ID"
        res = await self.bot.np_core.post_to(env, content=text, thread_name="Pro reminders", thread_key="pro_thread",
                                             mentions=discord.AllowedMentions(users=True, roles=True))
        if res is None:
            log.warning("Pro reminder not posted: set PRO_CHANNEL_ID")

    # ---------------- commands ----------------
    @app_commands.command(name="add", description="Add a player to the Pro list, or change how they got it (staff)")
    @app_commands.describe(ign="Minecraft username",
                           how="How they got Pro: tier testing, Bedwars stats requirement, other (free text)")
    @app_commands.check(staff_check)
    async def add(self, interaction: discord.Interaction, ign: str, how: Optional[str] = None):
        await interaction.response.defer(ephemeral=True)
        r = await lookup(interaction, ign)
        if r is None:
            return
        uuid, name = r
        how = (how or "").strip()[:200] or None
        await q_run("INSERT INTO pro_players (uuid, ign, added_at, how) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(uuid) DO UPDATE SET ign = excluded.ign, how = COALESCE(excluded.how, pro_players.how)",
                    (uuid, name, iso(now_utc()), how))
        await interaction.followup.send(f"**{esc(name)}** is on the Pro list" + (f" ({esc(how)})" if how else "") +
                                        f". The {LIMIT_DAYS} days count from their last win against a C+ player "
                                        "(or from when they were added if there is none). "
                                        "Use `/pro add` again to change how they got it.", ephemeral=True)

    @app_commands.command(name="remove", description="Remove a player from the Pro list (staff)")
    @app_commands.describe(ign="Minecraft username")
    @app_commands.check(staff_check)
    async def remove(self, interaction: discord.Interaction, ign: str):
        await interaction.response.defer(ephemeral=True)
        r = await lookup(interaction, ign)
        if r is None:
            return
        uuid, name = r
        await q_run("DELETE FROM pro_players WHERE uuid = ?", (uuid,))
        await interaction.followup.send(f"**{esc(name)}** removed from the Pro list.", ephemeral=True)

    @app_commands.command(name="list", description="Pros, how each one got the rank, and the days since their last C+ win")
    async def list_(self, interaction: discord.Interaction):
        await interaction.response.defer()
        rows = await q_all("SELECT * FROM pro_players")
        if not rows:
            await interaction.followup.send("The Pro list is empty. Staff can add players with `/pro add`.")
            return
        now, out = now_utc(), []
        for p in rows:
            win, anchor = await last_cplus_win(p["uuid"]), await anchor_of(p)
            days = (now - anchor).total_seconds() / 86400
            out.append((days, f"**{esc(p['ign'])}** · {int(days)}d since "
                              f"{'their last C+ win' if win else 'being added'} · "
                              f"{'**demote!**' if days >= LIMIT_DAYS else f'{LIMIT_DAYS - int(days)}d left'}"
                              f"\n-# Got Pro: {esc(p['how']) if p['how'] else 'not specified (use /pro add how:...)'}"))
        out.sort(key=lambda x: -x[0])
        embed = discord.Embed(title=f"Pro list · {len(out)}", color=0x00C8E6,
                              description="\n".join(t for _, t in out)[:4000])
        await interaction.followup.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="reminders", description="Turn the Pro reminders on or off (staff). No options: show the state")
    @app_commands.describe(reminder="Which reminder", state="on or off")
    @app_commands.choices(
        reminder=[app_commands.Choice(name="Day 15 (to the player)", value="pro_day15"),
                  app_commands.Choice(name="Day 23 (to the player)", value="pro_day23"),
                  app_commands.Choice(name="Day 30 (demote ping to the guild master)", value="pro_day30"),
                  app_commands.Choice(name="All of them", value="all")],
        state=[app_commands.Choice(name="on", value="on"), app_commands.Choice(name="off", value="off")])
    @app_commands.check(staff_check)
    async def reminders(self, interaction: discord.Interaction, reminder: Optional[str] = None, state: Optional[str] = None):
        await interaction.response.defer(ephemeral=True)
        if reminder and state:
            for k in (STAGE_KEYS.values() if reminder == "all" else [reminder]):
                await alerts.set_enabled(k, state == "on")
        elif reminder or state:
            await interaction.followup.send("Give both the reminder and the state, or nothing to see the list.", ephemeral=True)
            return
        rows = [f"{'🟢 ON ' if await alerts.enabled(k) else '🔴 OFF'} **{alerts.ALERTS[k][0]}**" for k in STAGE_KEYS.values()]
        await interaction.followup.send("\n".join(rows), ephemeral=True)


async def setup(bot: commands.Bot):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(SCHEMA)
        try:
            await db.execute("ALTER TABLE pro_players ADD COLUMN how TEXT")
        except aiosqlite.OperationalError:
            pass
        await db.commit()
    await bot.add_cog(ProCog(bot))

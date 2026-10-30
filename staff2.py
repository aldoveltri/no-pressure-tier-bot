"""More staff tools: wipe a cheater's matches, upcoming purge timestamp, breaklist betrayal alerts."""
import asyncio
import logging
import re
from datetime import timedelta
from typing import Optional

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands, tasks

import breaklist as bl
import gexpcalc as gc
from breaklist import q_one, q_all, q_run, kv_get, kv_set, esc, now_utc, iso, from_iso, staff_check, lookup, n
from config import tier_from_elo, STAFF_ROLE
from database import DB_PATH, backup_db, get_mode_by_name, remove_player

log = logging.getLogger("staff2")
NO_MENTIONS = discord.AllowedMentions.none()


# ======================================================================================
# wipe a cheater: cancel every match and give back the elo of the opponents
# ======================================================================================

async def cheater_matches(db, uuid, mode_id):
    db.row_factory = aiosqlite.Row
    cond, params = ("mode_id IS NULL", ()) if mode_id is None else ("mode_id = ?", (mode_id,))
    cur = await db.execute(
        f"SELECT * FROM matches WHERE cancelled = 0 AND (winner_uuid = ? OR loser_uuid = ?) AND {cond} ORDER BY id DESC",
        (uuid, uuid, *params))
    return [dict(r) for r in await cur.fetchall()]


async def current_elo(db, uuid, mode_id):
    if mode_id is None:
        cur = await db.execute("SELECT elo FROM players WHERE uuid = ?", (uuid,))
    else:
        cur = await db.execute("SELECT elo FROM mode_players WHERE mode_id = ? AND uuid = ?", (mode_id, uuid))
    row = await cur.fetchone()
    return row[0] if row else None


async def plan_wipe(uuid, mode_id):
    """{opponent uuid: (name, elo now, elo after the wipe)} and the matches that will be cancelled."""
    async with aiosqlite.connect(DB_PATH) as db:
        matches = await cheater_matches(db, uuid, mode_id)
        changes = {}
        for m in matches:                                      # newest first
            if m["winner_uuid"] == uuid:
                opp, delta = m["loser_uuid"], m["loser_elo_after"] - m["loser_elo_before"]
            else:
                opp, delta = m["winner_uuid"], m["winner_elo_after"] - m["winner_elo_before"]
            if opp not in changes:
                cur = await db.execute("SELECT ign FROM players WHERE uuid = ?", (opp,))
                row = await cur.fetchone()
                name = row[0] if row else opp[:8]
                now = await current_elo(db, opp, mode_id)
                changes[opp] = [name, now, now]
            if changes[opp][1] is not None:
                changes[opp][2] = max(0, changes[opp][2] - delta)
        return matches, changes


async def apply_wipe(uuid, mode_id, matches, changes):
    async with aiosqlite.connect(DB_PATH) as db:
        for opp, (_name, _now, new) in changes.items():
            if new is None:
                continue
            if mode_id is None:
                await db.execute("UPDATE players SET elo = ?, tier = ? WHERE uuid = ?", (new, tier_from_elo(new), opp))
            else:
                await db.execute("UPDATE mode_players SET elo = ?, tier = ? WHERE mode_id = ? AND uuid = ?",
                                 (new, tier_from_elo(new), mode_id, opp))
        for m in matches:
            await db.execute("UPDATE matches SET cancelled = 1 WHERE id = ?", (m["id"],))
        await db.commit()
    await remove_player(uuid, mode_id)


class WipeCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="wipeplayer", description="Cheater: cancel ALL their matches and give back the elo to who they fought (staff)")
    @app_commands.describe(ign="Minecraft username of the cheater", mode="Game mode (leave empty for the normal mode)",
                           confirm="Leave False to see what would change; True to do it")
    async def wipeplayer(self, interaction: discord.Interaction, ign: str, mode: Optional[str] = None, confirm: bool = False):
        # same staff rule as the other 1v1 staff commands: the Tier Tester role
        user = interaction.user
        if not (isinstance(user, discord.Member) and (user.guild_permissions.administrator
                                                      or any(r.name == STAFF_ROLE for r in user.roles))):
            await interaction.response.send_message("Only staff can use this command.", ephemeral=True)
            return
        await interaction.response.defer()
        md = None
        if mode:
            md = await get_mode_by_name(mode)
            if md is None:
                await interaction.followup.send(f"I don't know the mode `{mode}`.")
                return
        mode_id = md["id"] if md else None
        r = await lookup(interaction, ign)
        if r is None:
            return
        uuid, name = r
        matches, changes = await plan_wipe(uuid, mode_id)
        if not matches:
            if confirm:
                await asyncio.to_thread(backup_db, "wipe_player")
                await remove_player(uuid, mode_id)
                await interaction.followup.send(f"**{esc(name)}** has no matches to cancel: removed from the leaderboard.",
                                                allowed_mentions=NO_MENTIONS)
            else:
                await interaction.followup.send(f"**{esc(name)}** has no matches to cancel. Run it again with confirm:True "
                                                "to remove them from the leaderboard anyway.", allowed_mentions=NO_MENTIONS)
            return
        lines = [f"**{esc(v[0])}**: {v[1]} → **{v[2]}** ({v[2] - v[1]:+d})" for v in changes.values() if v[1] is not None]
        embed = discord.Embed(title=("Wiped " if confirm else "Would wipe ") + name,
                              color=0xE74C3C if confirm else 0xF5A623)
        embed.description = (f"**{len(matches)}** matches cancelled, **{len(changes)}** opponents.\n\n"
                             + "\n".join(lines))[:4000]
        if confirm:
            await asyncio.to_thread(backup_db, "wipe_player")
            await apply_wipe(uuid, mode_id, matches, changes)
            embed.set_footer(text="The player is removed from the leaderboard. A backup was saved.")
        else:
            embed.set_footer(text="Nothing changed yet: run the command again with confirm:True")
        await interaction.followup.send(embed=embed, allowed_mentions=NO_MENTIONS)


# ======================================================================================
# purge timestamp
# ======================================================================================

def stamp_lines(dt):
    """The timestamp of a moment: each format shown, then the same text in a box to copy."""
    ts = int(dt.timestamp())
    fmts = [(f"<t:{ts}:F>", f"<t:{ts}:F>"), (f"<t:{ts}:R>", f"<t:{ts}:R>"), (f"<t:{ts}:D> <t:{ts}:t>", f"<t:{ts}:D> <t:{ts}:t>")]
    return "\n".join(f"`{i}.` {show}\n```{raw}```" for i, (show, raw) in enumerate(fmts, 1))


class PurgeTimeCog(commands.Cog):
    @app_commands.command(name="purgetime", description="Discord timestamps of the last and the upcoming purge, or of a time you type, ready to copy")
    @app_commands.describe(when="Optional: another date/time, e.g. 2026/11/03 12:00 AM (the time zone of /timezone)")
    async def purgetime(self, interaction: discord.Interaction, when: Optional[str] = None):
        if when:
            end = gc.parse_et(when)
            if end is None:
                await interaction.response.send_message(
                    "I couldn't read it. Use `2026/11/03 12:00 AM` (time zone: see /timezone).", ephemeral=True)
                return
            embed = discord.Embed(title="Timestamp", color=0x00C8E6, description=f"{gc.fmt_et(end)}\n\n{stamp_lines(end)}")
        else:
            now = now_utc()
            nxt, last = gc.week_end(now), gc.week_start(now)
            embed = discord.Embed(title="Purge timestamps", color=0x00C8E6)
            embed.add_field(name=f"Upcoming purge · {gc.fmt_et(nxt)}", value=stamp_lines(nxt), inline=False)
            embed.add_field(name=f"Last purge · {gc.fmt_et(last)}", value=stamp_lines(last), inline=False)
        await interaction.response.send_message(embed=embed)


# ======================================================================================
# breaklist betrayal: someone on the breaklist joined another guild
# ======================================================================================

class Betrayal(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.loop.start()

    def cog_unload(self):
        self.loop.cancel()

    @tasks.loop(minutes=30)
    async def loop(self):
        try:
            ours = bl.HYPIXEL_GUILD_NAME.lower()
            for e in await q_all("SELECT * FROM bl_entries WHERE status = 'active'"):
                key = f"betrayal:{e['id']}"
                guild = await bl.api_guild({"player": e["uuid"]})
                await asyncio.sleep(0.5)
                if not guild or guild.get("name", "").lower() == ours:
                    continue
                approved = [x.strip().lower() for x in re.split(r"[,;]", e.get("visiting_guild") or "") if x.strip()]
                if guild.get("name", "").lower() in approved:
                    continue                                   # a guild the staff approved: not a betrayal
                await kv_set(key, iso(now_utc()))
                # the breaklist is over and the saved days go with it
                await q_run("UPDATE bl_entries SET status = 'betrayed', returned_at = ? WHERE id = ?",
                            (iso(now_utc()), e["id"]))
                row = await q_one("SELECT banked FROM player_days WHERE uuid = ?", (e["uuid"],))
                if row and row["banked"] > 0:
                    await q_run("INSERT INTO day_sets (uuid, ign, days, note, added_by, added_at) VALUES (?, ?, ?, ?, ?, ?)",
                                (e["uuid"], e["ign"], -row["banked"], "joined another guild while breaklisted", None,
                                 iso(now_utc())))
                    await q_run("UPDATE player_days SET banked = 0, updated_at = ? WHERE uuid = ?",
                                (iso(now_utc()), e["uuid"]))
                m = next((x for x in guild.get("members", []) if x.get("uuid") == e["uuid"]), None)
                since = f" since <t:{int(m['joined'] / 1000)}:D>" if m and m.get("joined") else ""
                ping_user, ping_role = bl._env("BETRAYAL_PING_USER_ID"), bl._env("BETRAYAL_PING_ROLE_ID")
                ping = (f"<@{ping_user}>" if ping_user.isdigit() else f"<@&{ping_role}>" if ping_role.isdigit() else "")
                text = (f"{ping} 🚨 **{esc(e['ign'])}** is on the breaklist but joined **{esc(guild.get('name', '?'))}**{since}. "
                        "They agreed to stay guildless: the breaklist no longer applies and their saved days were reset to 0.").strip()
                env = "BETRAYAL_CHANNEL_ID" if bl.chan_id("BETRAYAL_CHANNEL_ID") else "BREAKLIST_CHANNEL_ID"
                await self.bot.np_core.post_to(env, content=text, thread_name="Breaklist betrayals",
                                               thread_key="betrayal_thread",
                                               mentions=discord.AllowedMentions(users=True, roles=True))
        except Exception:
            log.exception("The betrayal check failed")

    @loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(180)


async def setup(bot: commands.Bot):
    await bot.add_cog(WipeCog(bot))
    await bot.add_cog(PurgeTimeCog())
    await bot.add_cog(Betrayal(bot))

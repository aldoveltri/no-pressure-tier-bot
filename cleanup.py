"""/cleanup: delete test data by type (or everything), for the whole guild or for one player.
Counters are reset too, so ids start again from 1. A backup is saved before anything is deleted."""
import asyncio
import logging
from typing import Optional

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands

import breaklist as bl
from breaklist import lookup, esc
from database import DB_PATH, backup_db

log = logging.getLogger("cleanup")

# type -> (label, tables deleted for everybody, kv keys, tables with a uuid column (for one player))
CATS = {
    "matches": ("All 1v1s: matches, elo, tiers, seasons, years, maps",
                ["matches", "players", "paused_matches", "season_players", "seasons", "years", "year_players",
                 "mode_players", "mode_year_players", "match_maps"], [], []),
    "punish": ("Mutes and verbal warnings", ["mutes", "warnings"], [],
               ["mutes", "warnings"]),
    "breaklist": ("Breaklist: entries, saved days, debts, excuses, history",
                  ["bl_entries", "bl_requests", "player_days", "day_sets", "debts", "excuses", "guild_left"],
                  ["guild_history_reset", "ignore:%", "daysanchor:%"],
                  ["bl_entries", "bl_requests", "player_days", "day_sets", "debts", "excuses", "guild_left"]),
    "blacklist": ("Blacklist", ["blacklist"], [], ["blacklist"]),
    "reports": ("Cheating reports and Urchin memory", ["cheat_reports", "ev_votes", "urchin_seen"],
                ["urchin_baseline"], ["cheat_reports", "urchin_seen"]),
    "gexp": ("GEXP history, logins, join dates, road history",
             ["gexp_history", "guild_seen", "logins", "road_history"],
             ["snapshot_at", "logins_since", "road_try:%"], ["gexp_history", "guild_seen", "logins"]),
    "pro": ("Pro list and waiting list", ["pro_players", "waitlist", "waitlist_notes"], ["pro:%"],
            ["pro_players", "waitlist", "waitlist_notes"]),
    "community": ("Parties and tag votes", ["parties", "party_votes", "tag_props", "tag_ballots", "tag_polls", "tag_answers", "tag_options", "tag_choices"], [], []),
    "chat": ("Saved guild chat", ["bridge_chat"], [], []),
    "links": ("Linked accounts and countries", ["links", "countries"], [], ["links", "countries"]),
}
ORDER = list(CATS)


def plan(what, uuid=None):
    """[(table, kv?)]: what will be deleted. For one player only the tables that have a uuid."""
    keys = ORDER if what == "all" else [what]
    tables, kvs = [], []
    for k in keys:
        _, whole, kv, per = CATS[k]
        tables += per if uuid else whole
        if not uuid:
            kvs += kv
    seen = set()
    return [t for t in tables if not (t in seen or seen.add(t))], kvs


async def count(db, table, uuid=None):
    try:
        if uuid and table == "ev_votes":
            cur = await db.execute("SELECT COUNT(*) FROM ev_votes WHERE report_id IN "
                                   "(SELECT id FROM cheat_reports WHERE uuid = ?)", (uuid,))
        elif uuid:
            cur = await db.execute(f"SELECT COUNT(*) FROM {table} WHERE uuid = ?", (uuid,))
        else:
            cur = await db.execute(f"SELECT COUNT(*) FROM {table}")
        return (await cur.fetchone())[0]
    except aiosqlite.OperationalError:
        return None          # the table does not exist


async def counts(what, uuid=None):
    tables, kvs = plan(what, uuid)
    out = []
    async with aiosqlite.connect(DB_PATH) as db:
        for t in tables:
            c = await count(db, t, uuid)
            if c is not None:
                out.append((t, c))
        kv_n = 0
        for k in kvs:
            try:
                cur = await db.execute("SELECT COUNT(*) FROM kv WHERE key LIKE ?", (k,))
                kv_n += (await cur.fetchone())[0]
            except aiosqlite.OperationalError:
                pass
    return out, kv_n


async def run(what, uuid=None):
    """Deletes the data and resets the counters. Returns the number of rows deleted."""
    tables, kvs = plan(what, uuid)
    total = 0
    async with aiosqlite.connect(DB_PATH) as db:
        for t in tables:
            try:
                if uuid and t == "ev_votes":
                    cur = await db.execute("DELETE FROM ev_votes WHERE report_id IN "
                                           "(SELECT id FROM cheat_reports WHERE uuid = ?)", (uuid,))
                elif uuid:
                    cur = await db.execute(f"DELETE FROM {t} WHERE uuid = ?", (uuid,))
                else:
                    cur = await db.execute(f"DELETE FROM {t}")
                total += max(getattr(cur, "rowcount", 0) or 0, 0)
            except aiosqlite.OperationalError:
                continue
        if not uuid:
            for t in tables:
                try:
                    await db.execute("DELETE FROM sqlite_sequence WHERE name = ?", (t,))
                except aiosqlite.OperationalError:
                    pass
            for k in kvs:
                try:
                    cur = await db.execute("DELETE FROM kv WHERE key LIKE ?", (k,))
                    total += max(getattr(cur, "rowcount", 0) or 0, 0)
                except aiosqlite.OperationalError:
                    pass
        await db.commit()
    if not uuid and (what in ("all", "matches")):
        try:
            from database import init_seasons
            await init_seasons()          # year 1 / season 1 again, as on a fresh start
        except Exception:
            log.exception("Could not recreate the first season")
    return total


class Confirm(discord.ui.View):
    def __init__(self, owner_id, what, uuid, name):
        super().__init__(timeout=120)
        self.owner_id, self.what, self.uuid, self.name = owner_id, what, uuid, name

    async def interaction_check(self, interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Only who typed the command can confirm.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger)
    async def delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        path = await asyncio.to_thread(backup_db, f"cleanup_{self.what}")
        total = await run(self.what, self.uuid)
        self.stop()
        await interaction.edit_original_response(
            content=f"🗑️ Done: **{total}** rows deleted, counters reset. Backup before the cleanup: `{path}`.\n"
                    "Messages already posted on Discord stay there: delete them by hand.",
            embed=None, view=None)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content="Nothing was deleted.", embed=None, view=None)


class CleanupCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="cleanup", description="Delete test data by type, with counters reset (administrators)")
    @app_commands.describe(what="What to delete", ign="Only this player (not for 1v1s and parties: use /wipeplayer)")
    @app_commands.choices(what=[app_commands.Choice(name=v[0][:100], value=k) for k, v in CATS.items()]
                          + [app_commands.Choice(name="EVERYTHING above", value="all")])
    async def cleanup(self, interaction: discord.Interaction, what: app_commands.Choice[str], ign: Optional[str] = None):
        if not (isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.administrator):
            await interaction.response.send_message("Only administrators can use this command.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        uuid = name = None
        if ign:
            if what.value == "matches":
                await interaction.followup.send("To delete the 1v1s of one player use `/wipeplayer`.")
                return
            r = await lookup(interaction, ign)
            if r is None:
                return
            uuid, name = r
        rows, kv_n = await counts(what.value, uuid)
        if what.value == "community" and uuid:
            await interaction.followup.send("Parties and tag votes are not tied to a player.")
            return
        lines = [f"`{t}`: **{c}**" for t, c in rows if c]
        if kv_n:
            lines.append(f"counters and settings of the section: **{kv_n}**")
        if not lines:
            await interaction.followup.send("There is nothing to delete there.")
            return
        target = f"of **{esc(name)}**" if name else "of the whole bot"
        label = "Everything" if what.value == "all" else CATS[what.value][0]
        embed = discord.Embed(title="Delete this data?", color=0xE74C3C,
                              description=f"{label} {target}\n\n" + "\n".join(lines))
        embed.set_footer(text="A backup is saved first. Counters restart from 1.")
        await interaction.followup.send(embed=embed, view=Confirm(interaction.user.id, what.value, uuid, name),
                                        allowed_mentions=discord.AllowedMentions.none())


async def setup(bot: commands.Bot):
    await bot.add_cog(CleanupCog(bot))

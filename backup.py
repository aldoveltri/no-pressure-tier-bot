"""/backup: list, create and restore database backups (everything, or only the breaklist, the 1v1s, ...)."""
import asyncio
import logging
import os
import sqlite3
from datetime import datetime
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from database import DB_PATH, backup_db

log = logging.getLogger("backup")
ROOT = os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "backups")
CONFIRM = "RESTORE"
GROUPS = {
    "breaklist": ("Breaklist (entries, requests, days, debts, excuses)",
                  ["bl_entries", "bl_requests", "player_days", "day_sets", "debts", "excuses"]),
    "1v1": ("1v1s (players, matches, seasons, years, modes, map stats)",
            ["players", "matches", "paused_matches", "seasons", "season_players", "years", "year_players", "modes",
             "mode_players", "mode_year_players", "match_maps"]),
    "mutes": ("Mutes and verbal warnings", ["mutes", "warnings"]),
    "blacklist": ("Blacklist and evidence", ["blacklist", "cheat_reports", "ev_votes", "urchin_seen"]),
    "links": ("Linked accounts and countries", ["links", "countries"]),
    "all": ("EVERYTHING in the backup", None),
}
_TMP = "(file)"


def list_backups():
    """[(relative path, datetime, size)] newest first."""
    out = []
    for folder, _dirs, names in os.walk(ROOT):
        for x in names:
            if x.endswith(".db"):
                p = os.path.join(folder, x)
                try:
                    out.append((os.path.relpath(p, ROOT), datetime.fromtimestamp(os.path.getmtime(p)), os.path.getsize(p)))
                except OSError:
                    pass
    out.sort(key=lambda r: r[1], reverse=True)
    return out


def resolve(rel):
    """The absolute path of a backup, only inside the backups folder (None otherwise)."""
    path = os.path.realpath(os.path.join(ROOT, rel))
    return path if path.startswith(os.path.realpath(ROOT) + os.sep) and os.path.isfile(path) else None


def tables_of(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")}


def inspect(path, group):
    """[(table, rows in the backup, rows now)] for the tables of the group that exist in the backup."""
    src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    cur = sqlite3.connect(DB_PATH)
    try:
        have = tables_of(src)
        wanted = sorted(have) if GROUPS[group][1] is None else [t for t in GROUPS[group][1] if t in have]
        now = tables_of(cur)
        return [(t, src.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0],
                 cur.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] if t in now else None) for t in wanted]
    finally:
        src.close()
        cur.close()


def restore(path, group):
    """Replaces the tables of the group with the ones of the backup. Returns [(table, rows restored)]."""
    conn = sqlite3.connect(DB_PATH, timeout=60)
    try:
        conn.execute("ATTACH DATABASE ? AS bk", (path,))
        have = {r[0] for r in conn.execute("SELECT name FROM bk.sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")}
        mine = {r[0] for r in conn.execute("SELECT name FROM main.sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")}
        wanted = sorted(have) if GROUPS[group][1] is None else [t for t in GROUPS[group][1] if t in have]
        done = []
        conn.execute("BEGIN IMMEDIATE")
        for t in wanted:
            if t not in mine:
                continue
            a = [r[1] for r in conn.execute(f'PRAGMA main.table_info("{t}")')]
            b = {r[1] for r in conn.execute(f'PRAGMA bk.table_info("{t}")')}
            cols = [c for c in a if c in b]
            if not cols:
                continue
            names = ", ".join(f'"{c}"' for c in cols)
            conn.execute(f'DELETE FROM main."{t}"')
            conn.execute(f'INSERT INTO main."{t}" ({names}) SELECT {names} FROM bk."{t}"')
            done.append((t, conn.execute(f'SELECT COUNT(*) FROM main."{t}"').fetchone()[0]))
        conn.commit()
        conn.execute("DETACH DATABASE bk")
        return done
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def is_admin(interaction):
    return isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.administrator


class BackupCog(commands.GroupCog, name="backup", description="Database backups (administrators)"):
    def __init__(self, bot):
        self.bot = bot
        super().__init__()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if is_admin(interaction):
            return True
        await interaction.response.send_message("Only administrators can use the backups.", ephemeral=True)
        return False

    @app_commands.command(name="now", description="Save a backup of the database right now")
    async def now(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        path = await asyncio.to_thread(backup_db, "manual")
        await interaction.followup.send(f"✅ Backup saved: `{path}`", ephemeral=True)

    @app_commands.command(name="list", description="The saved backups, newest first")
    @app_commands.describe(page="Page number")
    async def list_(self, interaction: discord.Interaction, page: app_commands.Range[int, 1, 100] = 1):
        await interaction.response.defer(ephemeral=True)
        rows = await asyncio.to_thread(list_backups)
        if not rows:
            await interaction.followup.send("No backups yet.", ephemeral=True)
            return
        per = 15
        pages = max(1, -(-len(rows) // per))
        page = min(page, pages)
        lines = [f"`{when:%Y-%m-%d %H:%M}` · {size / 1024:,.0f} KB · `{rel}`"
                 for rel, when, size in rows[(page - 1) * per: page * per]]
        await interaction.followup.send(f"**Backups** ({len(rows)}) · page {page}/{pages}\n" + "\n".join(lines)[:1800],
                                        ephemeral=True)

    async def source_choices(self, interaction: discord.Interaction, current: str):
        rows = await asyncio.to_thread(list_backups)
        cur = current.lower()
        return [app_commands.Choice(name=f"{when:%Y-%m-%d %H:%M} · {rel.split(os.sep)[-1].split('_', 2)[-1][:-3]}"[:100], value=rel[:100])
                for rel, when, _ in rows if cur in rel.lower()][:25]

    @app_commands.command(name="restore", description="Restore the breaklist, the 1v1s... from a backup (administrators)")
    @app_commands.describe(what="What to restore", backup="Pick a saved backup", file="Or upload a .db backup file",
                           confirm=f"Write {CONFIRM} to really restore. Empty = only show what would change")
    @app_commands.choices(what=[app_commands.Choice(name=v[0][:100], value=k) for k, v in GROUPS.items()])
    @app_commands.autocomplete(backup=source_choices)
    async def restore_cmd(self, interaction: discord.Interaction, what: str, backup: Optional[str] = None,
                          file: Optional[discord.Attachment] = None, confirm: Optional[str] = None):
        await interaction.response.defer(ephemeral=True)
        if what not in GROUPS or (backup is None) == (file is None):
            await interaction.followup.send("Give exactly one of `backup` (pick one) or `file` (upload a .db).", ephemeral=True)
            return
        if file is not None:
            os.makedirs(os.path.join(ROOT, "uploaded"), exist_ok=True)
            path = os.path.join(ROOT, "uploaded", f"{datetime.now():%Y%m%d_%H%M%S}_uploaded.db")
            with open(path, "wb") as f:
                f.write(await file.read())
        else:
            path = resolve(backup)
            if path is None:
                await interaction.followup.send("I can't find that backup. See `/backup list`.", ephemeral=True)
                return
        try:
            info = await asyncio.to_thread(inspect, path, what)
        except sqlite3.DatabaseError:
            await interaction.followup.send("That file is not a valid database backup.", ephemeral=True)
            return
        if not info:
            await interaction.followup.send("That backup has none of the tables for this choice.", ephemeral=True)
            return
        table = "\n".join(f"`{t}`: backup {a} rows · now {'-' if b is None else b}" for t, a, b in info)
        if (confirm or "").strip() != CONFIRM:
            await interaction.followup.send(
                f"**Preview: {GROUPS[what][0]}**\n{table}\n\nNothing was changed. To restore, run the same command with "
                f"`confirm:{CONFIRM}`. A backup of the current data is saved first.", ephemeral=True)
            return
        before = await asyncio.to_thread(backup_db, "before_restore")
        try:
            done = await asyncio.to_thread(restore, path, what)
        except Exception as e:
            log.exception("Restore failed")
            await interaction.followup.send(f"❌ The restore failed and nothing was changed: {e}", ephemeral=True)
            return
        await interaction.followup.send(
            f"✅ **{GROUPS[what][0]}** restored: " + ", ".join(f"{t} ({c})" for t, c in done)
            + f".\nThe data before the restore is saved in `{before}`."
            + ("\nRestart the bot so everything reloads cleanly." if what in ("all", "1v1") else ""), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(BackupCog(bot))

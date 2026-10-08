import asyncio
import logging

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import STAFF_ROLE
from database import get_all_player_names, rename_player
from mojang import get_name

log = logging.getLogger("tierbot")

CHECK_EVERY_HOURS = 6
DELAY_BETWEEN_REQUESTS = 1.5   # seconds, to stay well under Mojang's limits


def _is_staff(member) -> bool:
    if not isinstance(member, discord.Member):
        return False
    if member.guild_permissions.administrator:
        return True
    return any(r.name == STAFF_ROLE for r in member.roles)


class NameSync(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.lock = asyncio.Lock()
        self.refresh_loop.start()

    def cog_unload(self):
        self.refresh_loop.cancel()

    async def run_check(self):
        """Checks every stored player against Mojang.
        Returns (total, checked, changes) where changes is a list of (old_name, new_name)."""
        players = await get_all_player_names()
        checked, changes = 0, []
        for p in players:
            try:
                current = await get_name(p["uuid"])
            except (RuntimeError, aiohttp.ClientError, asyncio.TimeoutError) as e:
                log.warning("Name check stopped early: %s", e)
                break
            checked += 1
            if current and current != p["ign"]:
                await rename_player(p["uuid"], current)
                changes.append((p["ign"], current))
                log.info("Player renamed: %s -> %s", p["ign"], current)
            await asyncio.sleep(DELAY_BETWEEN_REQUESTS)
        return len(players), checked, changes

    @tasks.loop(hours=CHECK_EVERY_HOURS)
    async def refresh_loop(self):
        if self.lock.locked():
            return
        async with self.lock:
            try:
                total, checked, changes = await self.run_check()
                log.info("Name check done: %d/%d players checked, %d renamed",
                         checked, total, len(changes))
            except Exception:
                log.exception("The automatic name check failed")

    @refresh_loop.before_loop
    async def before_refresh_loop(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(300)   # first check 5 minutes after the bot starts

    @app_commands.command(name="refreshnames",
                          description="Check Mojang for changed usernames right now (staff only)")
    async def refreshnames(self, interaction: discord.Interaction):
        if not _is_staff(interaction.user):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True)
            return
        if self.lock.locked():
            await interaction.response.send_message(
                "A name check is already running. Try again in a few minutes.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        async with self.lock:
            total, checked, changes = await self.run_check()

        text = f"Checked {checked}/{total} players. {len(changes)} username(s) updated."
        if checked < total:
            text += "\nMojang is limiting requests, so the check stopped early. Try again later."
        for old, new in changes[:15]:
            text += f"\n• {discord.utils.escape_markdown(old)} → {discord.utils.escape_markdown(new)}"
        await interaction.followup.send(text)


async def setup(bot: commands.Bot):
    await bot.add_cog(NameSync(bot))
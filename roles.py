"""Guild member role: given automatically when a linked player is in the guild, removed when they leave it.
Set GUILD_MEMBER_ROLE_ID (the bot needs the Manage Roles permission and its role must be above that role)."""
import asyncio
import logging

import discord
from discord.ext import commands, tasks

import breaklist as bl
from breaklist import q_all, q_one

log = logging.getLogger("roles")


class GuildRole(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.warned = set()
        self.loop.start()

    def cog_unload(self):
        self.loop.cancel()

    def role_id(self):
        return bl.chan_id("GUILD_MEMBER_ROLE_ID")

    def warn_once(self, key, text):
        if key not in self.warned:
            self.warned.add(key)
            log.warning(text)

    async def sync_member(self, discord_id, members=None):
        """Adds or removes the role of one linked member. members: the Hypixel guild member list (None = ask)."""
        rid = self.role_id()
        if rid is None:
            return
        link = await q_one("SELECT uuid FROM links WHERE discord_id = ?", (str(discord_id),))
        if members is None:
            members = await self.bot.np_core.guild_members()
        if not members:
            return                                         # no list from Hypixel: change nothing
        want = bool(link) and link["uuid"] in members
        for guild in self.bot.guilds:
            role = guild.get_role(rid)
            if role is None:
                continue
            try:
                member = guild.get_member(int(discord_id)) or await guild.fetch_member(int(discord_id))
            except discord.NotFound:
                continue
            except discord.HTTPException:
                continue
            has = role in member.roles
            try:
                if want and not has:
                    await member.add_roles(role, reason="Linked and in the guild")
                elif not want and has:
                    await member.remove_roles(role, reason="No longer in the guild or unlinked")
            except discord.Forbidden:
                self.warn_once("forbidden", "Can't change the guild member role: give the bot Manage Roles and "
                                            "put its role above the member role")

    @tasks.loop(minutes=15)
    async def loop(self):
        if self.role_id() is None:
            return
        try:
            members = await self.bot.np_core.guild_members(force=True)
            if not members:
                return
            for row in await q_all("SELECT discord_id FROM links"):
                await self.sync_member(row["discord_id"], members)
                await asyncio.sleep(0.5)
        except Exception:
            log.exception("The guild member role sync failed")

    @loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(150)

    @commands.Cog.listener()
    async def on_np_linked(self, discord_id):
        try:
            await self.sync_member(discord_id)
        except Exception:
            log.exception("Role after linking failed")

    @commands.Cog.listener()
    async def on_np_unlinked(self, discord_id):
        try:
            await self.sync_member(discord_id)
        except Exception:
            log.exception("Role after unlinking failed")

    @commands.Cog.listener()
    async def on_member_join(self, member):
        try:
            await self.sync_member(member.id)
        except Exception:
            log.exception("Role for a new member failed")


async def setup(bot: commands.Bot):
    await bot.add_cog(GuildRole(bot))

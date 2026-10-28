"""Purge summary: "A total of `25 guild members` were purged putting us at `120 guild members`".
Posted by itself some time after the purge (Tuesday 12:00 AM Eastern Time) and with /purge summary."""
import asyncio
import logging
from datetime import timedelta

import discord
from discord.ext import commands, tasks

import alerts
import breaklist as bl
import gexpcalc as gc
from breaklist import q_one, q_all, kv_get, kv_set, iso, from_iso, now_utc, esc

log = logging.getLogger("purgesum")
AFTER_KICKS_HOURS = 12            # kicks done by hand just after midnight still count for the purge that just ended


def delay_minutes():
    return bl._int_env("PURGE_SUMMARY_DELAY_MINUTES", 180)


async def purged_members(core, ws):
    """Who was purged by the purge that happened at ws (a Tuesday 12:00 AM ET): members that left the guild during
    the week before it (or in the first hours after, when the kicks are done) short of the GEXP they owed.
    Not counted: players on the breaklist, excused, blacklisted, or back in the guild."""
    prev = ws - timedelta(days=7)
    rows = await q_all("SELECT * FROM guild_left WHERE left_at >= ? AND left_at < ? ORDER BY left_at",
                       (iso(prev), iso(ws + timedelta(hours=AFTER_KICKS_HOURS))))
    members = await core.guild_members() or {}
    out, skipped = [], {"breaklist": 0, "excused": 0, "blacklist": 0, "met": 0, "back": 0}
    for r in rows:
        uuid = r["uuid"]
        if r["rejoined_at"] or uuid in members:
            skipped["back"] += 1
            continue
        left = min(from_iso(r["left_at"]), ws)                    # a kick right after midnight belongs to the week that ended
        joined = from_iso(r["joined_at"]) if r["joined_at"] else None
        start = max(joined, prev) if joined else prev
        required = gc.required_elapsed(start, left)
        earned, _ = await core.window_sum(uuid, start, left)
        if await q_one("SELECT id FROM bl_entries WHERE uuid = ? AND added_at >= ? AND added_at < ?",
                       (uuid, iso(prev), iso(ws + timedelta(days=1)))):
            skipped["breaklist"] += 1
        elif await q_one("SELECT uuid FROM excuses WHERE uuid = ? AND week = ?", (uuid, prev.date().isoformat())):
            skipped["excused"] += 1
        elif await q_one("SELECT id FROM blacklist WHERE uuid = ? AND (status = 'active' OR added_at >= ?)",
                         (uuid, iso(prev))):
            skipped["blacklist"] += 1
        elif earned >= required:
            skipped["met"] += 1
        else:
            out.append({"uuid": uuid, "ign": r["ign"] or await core.name_for(uuid), "short": required - earned})
    return out, skipped, len(members)


def summary_text(purged, total):
    return (f"A total of `{len(purged)} guild members` were purged putting us at `{total} guild members`")


class PurgeSummary(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.loop.start()

    def cog_unload(self):
        self.loop.cancel()

    async def build(self, ws):
        purged, skipped, total = await purged_members(self.bot.np_core, ws)
        return purged, skipped, total

    @tasks.loop(minutes=10)
    async def loop(self):
        try:
            now = now_utc()
            ws = gc.week_start(now)
            due = ws + timedelta(minutes=delay_minutes())
            if not (due <= now < due + timedelta(hours=24)):
                return
            key = f"purge_summary:{ws.date().isoformat()}"
            if await kv_get(key) or not await alerts.enabled("purge_summary"):
                return
            members = await self.bot.np_core.guild_members(force=True)
            if not members:
                return                                         # try again in 10 minutes
            purged, skipped, total = await self.build(ws)
            await kv_set(key, iso(now))
            env = "PURGE_SUMMARY_CHANNEL_ID" if bl.chan_id("PURGE_SUMMARY_CHANNEL_ID") else "INACTIVITY_CHANNEL_ID"
            await self.bot.np_core.post_to(env, content=summary_text(purged, total))
        except Exception:
            log.exception("The purge summary failed")

    @loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(90)


async def setup(bot: commands.Bot):
    await bot.add_cog(PurgeSummary(bot))

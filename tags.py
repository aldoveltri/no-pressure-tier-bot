"""Custom guild tag voting: an active player suggests a tag (6 characters or less), staff picks which active
players online vote, everyone picked must say yes, then the bot pings the guild master to change the tag by hand."""
import json
import logging
import re
from typing import Optional

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands

import breaklist as bl
from breaklist import q_one, q_all, q_run, esc, now_utc, iso

log = logging.getLogger("tags")
SCHEMA = """
CREATE TABLE IF NOT EXISTS tag_props (
    id INTEGER PRIMARY KEY AUTOINCREMENT, tag TEXT NOT NULL, proposer_id TEXT, proposer_name TEXT,
    voters TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL DEFAULT 'open', note TEXT,
    channel_id TEXT, message_id TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS tag_ballots (
    prop_id INTEGER NOT NULL, discord_id TEXT NOT NULL, name TEXT, vote TEXT NOT NULL, PRIMARY KEY (prop_id, discord_id)
);
"""
TAG_RE = re.compile(r"^[A-Za-z0-9]{1,6}$")


def active_rank() -> str:
    return (bl._env("ACTIVE_RANK_NAME") or "Active").strip().lower()


def tally(voters, ballots):
    """('open'|'accepted'|'rejected', waiting list). One 'no' rejects; all picked voters 'yes' accepts."""
    votes = {b["discord_id"]: b["vote"] for b in ballots}
    if any(votes.get(v) == "n" for v in voters):
        return "rejected", []
    waiting = [v for v in voters if votes.get(v) != "y"]
    if voters and not waiting:
        return "accepted", []
    return "open", waiting


async def is_active(core, discord_id):
    """(True/False, reason): the Discord account is linked to a guild member with the Active rank."""
    row = await q_one("SELECT uuid, ign FROM links WHERE discord_id = ?", (str(discord_id),))
    if not row:
        return False, "your Discord account is not linked to a Minecraft account (ask the staff to link it)"
    await core.guild_members()
    seen = await q_one("SELECT rank FROM guild_seen WHERE uuid = ? AND COALESCE(in_guild, 1) = 1", (row["uuid"],))
    if not seen:
        return False, f"{row['ign']} is not in the guild"
    if (seen["rank"] or "").lower() != active_rank():
        return False, f"{row['ign']} doesn't have the {bl._env('ACTIVE_RANK_NAME') or 'Active'} rank"
    return True, row["ign"]


async def prop_embed(p):
    voters = json.loads(p["voters"])
    ballots = await q_all("SELECT * FROM tag_ballots WHERE prop_id = ?", (p["id"],))
    votes = {b["discord_id"]: b["vote"] for b in ballots}
    color = {"open": 0x9B7BE8, "accepted": 0x2ECC71, "rejected": 0xE74C3C, "cancelled": 0x808080}[p["status"]]
    e = discord.Embed(title=f"Custom tag vote: [{p['tag']}]", color=color)
    lines = [f"Suggested by **{esc(p['proposer_name'] or '?')}**", ""]
    if not voters:
        lines.append("⏳ Waiting for the staff to choose which active players vote.")
    else:
        lines.append("Everyone picked has to vote yes:")
        for v in voters:
            mark = {"y": "✅ yes", "n": "❌ no"}.get(votes.get(v), "⏳ waiting")
            lines.append(f"<@{v}> · {mark}")
    status = {"open": "Vote open", "accepted": "ACCEPTED: the guild master will change the tag",
              "rejected": "REJECTED", "cancelled": "Cancelled"}[p["status"]]
    if p["note"]:
        status += f" · {p['note']}"
    e.description = "\n".join(lines)
    e.set_footer(text=f"Proposal #{p['id']} · {status}")
    return e


def prop_view(p):
    view = discord.ui.View(timeout=None)
    if p["status"] == "open":
        pid = p["id"]
        view.add_item(TagButton("y", pid))
        view.add_item(TagButton("n", pid))
        view.add_item(TagButton("sv", pid))
        view.add_item(TagButton("cx", pid))
    return view


async def refresh(interaction, pid, announce=True):
    """Re-reads the proposal, closes it when decided, edits the message."""
    p = await q_one("SELECT * FROM tag_props WHERE id = ?", (pid,))
    if p["status"] == "open":
        ballots = await q_all("SELECT * FROM tag_ballots WHERE prop_id = ?", (pid,))
        state, _ = tally(json.loads(p["voters"]), ballots)
        if state != "open":
            note = None
            if state == "rejected":
                no = next((b for b in ballots if b["vote"] == "n" and b["discord_id"] in json.loads(p["voters"])), None)
                note = f"no by {no['name']}" if no else None
            await q_run("UPDATE tag_props SET status = ?, note = ? WHERE id = ?", (state, note, pid))
            p = await q_one("SELECT * FROM tag_props WHERE id = ?", (pid,))
            if state == "accepted" and announce:
                ping = bl._env("TAG_PING_USER_ID") or bl._env("BETRAYAL_PING_USER_ID")
                text = (f"<@{ping}> " if ping.isdigit() else "") + (
                    f"✅ All the active players picked voted yes: please change the guild tag to **[{p['tag']}]**.")
                try:
                    await interaction.channel.send(text, allowed_mentions=discord.AllowedMentions(users=True))
                except discord.HTTPException:
                    pass
    return p


class VoterSelect(discord.ui.UserSelect):
    def __init__(self, pid):
        super().__init__(placeholder="Pick the active players online who vote", min_values=1, max_values=25)
        self.pid = pid

    async def callback(self, interaction: discord.Interaction):
        picked = [u for u in self.values if not u.bot]
        await q_run("UPDATE tag_props SET voters = ? WHERE id = ?", (json.dumps([str(u.id) for u in picked]), self.pid))
        core = interaction.client.np_core
        warn = []
        for u in picked:
            ok, why = await is_active(core, u.id)
            if not ok:
                warn.append(f"{u.mention}: {why}")
        p = await refresh(interaction, self.pid)
        # the voting message is the one the staff pressed the button on
        ch = interaction.client.get_channel(int(p["channel_id"])) or await interaction.client.fetch_channel(int(p["channel_id"]))
        msg = await ch.fetch_message(int(p["message_id"]))
        await msg.edit(embed=await prop_embed(p), view=prop_view(p))
        ping = " ".join(u.mention for u in picked)
        await ch.send(f"{ping} you were picked to vote on the custom tag **[{p['tag']}]**: press Yes or No on the message above.",
                      allowed_mentions=discord.AllowedMentions(users=True))
        text = f"✅ {len(picked)} voters set."
        if warn:
            text += "\n⚠️ Not active or not linked (they can still vote): " + "; ".join(warn)
        await interaction.response.edit_message(content=text, view=None)


class TagButton(discord.ui.DynamicItem[discord.ui.Button], template=r"tg:(?P<act>y|n|sv|cx):(?P<pid>\d+)"):
    def __init__(self, act, pid):
        labels = {"y": ("Yes", discord.ButtonStyle.success), "n": ("No", discord.ButtonStyle.danger),
                  "sv": ("Pick voters (staff)", discord.ButtonStyle.primary), "cx": ("Cancel (staff)", discord.ButtonStyle.secondary)}
        label, style = labels[act]
        super().__init__(discord.ui.Button(label=label, style=style, custom_id=f"tg:{act}:{pid}"))
        self.act, self.pid = act, int(pid)

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["act"], int(match["pid"]))

    async def callback(self, interaction: discord.Interaction):
        p = await q_one("SELECT * FROM tag_props WHERE id = ?", (self.pid,))
        if p is None or p["status"] != "open":
            await interaction.response.send_message("This vote is closed.", ephemeral=True)
            return
        if self.act in ("sv", "cx"):
            if not bl.is_guild_staff(interaction.user):
                await interaction.response.send_message("Only guild staff can do this.", ephemeral=True)
                return
            if self.act == "sv":
                view = discord.ui.View(timeout=300)
                view.add_item(VoterSelect(self.pid))
                await interaction.response.send_message("Choose the active players who are online now:", view=view,
                                                        ephemeral=True)
            else:
                await q_run("UPDATE tag_props SET status = 'cancelled', note = ? WHERE id = ?",
                            (f"by {interaction.user.display_name}", self.pid))
                p = await q_one("SELECT * FROM tag_props WHERE id = ?", (self.pid,))
                await interaction.response.edit_message(embed=await prop_embed(p), view=prop_view(p))
            return
        voters = json.loads(p["voters"])
        if not voters:
            await interaction.response.send_message("The staff hasn't chosen the voters yet.", ephemeral=True)
            return
        if str(interaction.user.id) not in voters:
            await interaction.response.send_message("You are not one of the players picked for this vote.", ephemeral=True)
            return
        name, _uuid = (await q_one("SELECT ign AS a, uuid AS b FROM links WHERE discord_id = ?", (str(interaction.user.id),))
                       or {"a": interaction.user.display_name, "b": None}).values()
        await q_run("INSERT INTO tag_ballots (prop_id, discord_id, name, vote) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(prop_id, discord_id) DO UPDATE SET vote = excluded.vote, name = excluded.name",
                    (self.pid, str(interaction.user.id), name, self.act))
        p = await refresh(interaction, self.pid)
        await interaction.response.edit_message(embed=await prop_embed(p), view=prop_view(p))


class TagCog(commands.GroupCog, name="tag", description="Custom guild tag votes"):
    def __init__(self, bot):
        self.bot = bot
        super().__init__()

    @app_commands.command(name="suggest", description="Suggest a custom guild tag (6 characters or less) and start a vote (active players)")
    @app_commands.describe(tag="The tag without brackets, up to 6 letters or numbers")
    async def suggest(self, interaction: discord.Interaction, tag: str):
        await interaction.response.defer(ephemeral=True)
        tag = tag.strip().strip("[]")
        if not TAG_RE.match(tag):
            await interaction.followup.send("The tag can have at most 6 characters, only letters and numbers.", ephemeral=True)
            return
        ok, why = await is_active(self.bot.np_core, interaction.user.id)
        if not ok and not bl.is_guild_staff(interaction.user):
            await interaction.followup.send(f"Only active players can suggest a tag: {why}.", ephemeral=True)
            return
        if await q_one("SELECT id FROM tag_props WHERE status = 'open' AND LOWER(tag) = LOWER(?)", (tag,)):
            await interaction.followup.send(f"A vote for **[{tag}]** is already open.", ephemeral=True)
            return
        if await q_one("SELECT id FROM tag_props WHERE status = 'open' AND proposer_id = ?", (str(interaction.user.id),)):
            await interaction.followup.send("You already have an open vote: wait for it to end.", ephemeral=True)
            return
        name = why if ok else interaction.user.display_name
        pid = await q_run("INSERT INTO tag_props (tag, proposer_id, proposer_name, created_at) VALUES (?, ?, ?, ?)",
                          (tag.upper(), str(interaction.user.id), name, iso(now_utc())))
        p = await q_one("SELECT * FROM tag_props WHERE id = ?", (pid,))
        cid = bl.chan_id("TAG_CHANNEL_ID")
        ch = (self.bot.get_channel(cid) or await self.bot.fetch_channel(cid)) if cid else interaction.channel
        msg = await ch.send(embed=await prop_embed(p), view=prop_view(p))
        await q_run("UPDATE tag_props SET channel_id = ?, message_id = ? WHERE id = ?", (str(ch.id), str(msg.id), pid))
        await interaction.followup.send(f"Vote for **[{tag.upper()}]** started in {ch.mention}. The staff will pick who votes.",
                                        ephemeral=True)

    @app_commands.command(name="list", description="Open and recent custom tag votes (guild staff)")
    @app_commands.check(bl.staff_check)
    async def list(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        rows = await q_all("SELECT * FROM tag_props ORDER BY id DESC LIMIT 15")
        if not rows:
            await interaction.followup.send("No tag votes yet.", ephemeral=True)
            return
        await interaction.followup.send(
            "\n".join(f"#{r['id']} **[{r['tag']}]** · {r['status']} · by {esc(r['proposer_name'] or '?')}" for r in rows),
            ephemeral=True)


async def setup(bot: commands.Bot):
    async with aiosqlite.connect(bl.DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()
    bot.add_dynamic_items(TagButton)
    await bot.add_cog(TagCog(bot))

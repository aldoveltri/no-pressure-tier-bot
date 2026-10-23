"""Guild blacklist, cheating reports (evidence) and the Urchin tag watcher.

Extension of the bot: main.py loads it with  await bot.load_extension("evidence")  (after "breaklist").

Permissions: while EVIDENCE_STAFF_ONLY is empty / 0 everybody can use these commands and buttons (test mode).
Set EVIDENCE_STAFF_ONLY=1 in the host variables to make them staff only. Voting is always open to everyone.
"""
import asyncio
import hashlib
import io
import json
import logging
import os
from typing import Optional

import aiohttp
import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands, tasks

import breaklist as bl
from breaklist import (q_one, q_all, q_run, kv_get, kv_set, esc, now_utc, iso, is_guild_staff, lookup,
                       NO_MENTIONS, _env)

log = logging.getLogger("tierbot")

SCHEMA = """
CREATE TABLE IF NOT EXISTS blacklist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid TEXT NOT NULL, ign TEXT NOT NULL, reason TEXT, duration TEXT,
    added_by TEXT, added_at TEXT, updated_at TEXT, status TEXT NOT NULL DEFAULT 'active'
);
CREATE TABLE IF NOT EXISTS cheat_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid TEXT NOT NULL, ign TEXT NOT NULL, replays TEXT, reason TEXT,
    status TEXT NOT NULL DEFAULT 'open', verdict TEXT, source TEXT,
    created_by TEXT, created_at TEXT, channel_id TEXT, message_id TEXT
);
CREATE TABLE IF NOT EXISTS ev_votes (
    report_id INTEGER NOT NULL, user_id TEXT NOT NULL, vote TEXT NOT NULL,
    PRIMARY KEY (report_id, user_id)
);
CREATE TABLE IF NOT EXISTS urchin_seen (
    uuid TEXT PRIMARY KEY, sig TEXT, seen_at TEXT
);
"""

CYAN, GREEN, RED, GREY, GOLD = 0x00C8E6, 0x2ECC71, 0xE74C3C, 0x95A5A6, 0xF1C40F


# ======================================================================================
# permissions
# ======================================================================================

def staff_only_mode() -> bool:
    return _env("EVIDENCE_STAFF_ONLY").strip().lower() in ("1", "true", "yes", "on")


def allowed(member) -> bool:
    """True when this member may use the blacklist / evidence tools (everybody in test mode)."""
    return (not staff_only_mode()) or is_guild_staff(member)


async def perm_check(interaction: discord.Interaction) -> bool:
    return allowed(interaction.user)


async def deny(interaction: discord.Interaction):
    text = "Only the guild staff can do this."
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)


# ======================================================================================
# helpers
# ======================================================================================

def head_url(uuid, size=128):
    return f"https://mc-heads.net/avatar/{uuid}/{size}"


def body_url(uuid):
    return f"https://mc-heads.net/body/{uuid}/right"


def core_of(bot):
    return bot.np_core


async def send_embed(core, interaction, env_name, *, embed, content=None, view=None, thread_name=None,
                     thread_key=None):
    """Posts in the channel of the variable; without one (tests) or on failure, in the current channel.
    Returns the channel mention or None."""
    core.last_message = None
    posted = await core.post_to(env_name, content=content, embed=embed, view=view, thread_name=thread_name,
                                thread_key=thread_key)
    if posted:
        return posted
    kwargs = {"embed": embed}
    if content:
        kwargs["content"] = content
    if view is not None:
        kwargs["view"] = view
    core.last_message = await interaction.channel.send(**kwargs)
    return None


# ======================================================================================
# blacklist
# ======================================================================================

def blacklist_embed(row, title="Guild blacklist"):
    e = discord.Embed(title=title, color=RED, timestamp=now_utc())
    e.set_thumbnail(url=head_url(row["uuid"]))
    e.add_field(name="Player", value=f"**{esc(row['ign'])}**", inline=True)
    e.add_field(name="Duration", value=row["duration"] or "Not specified", inline=True)
    e.add_field(name="Reason", value=row["reason"] or "Not specified", inline=False)
    e.set_footer(text=f"Entry #{row['id']}")
    return e


async def blacklist_upsert(uuid, ign, reason, duration, by):
    """Adds or updates the active entry of a player. Returns (row, created)."""
    row = await q_one("SELECT * FROM blacklist WHERE uuid = ? AND status = 'active'", (uuid,))
    stamp = iso(now_utc())
    if row:
        await q_run("UPDATE blacklist SET ign = ?, reason = ?, duration = ?, updated_at = ? WHERE id = ?",
                    (ign, reason or row["reason"], duration or row["duration"], stamp, row["id"]))
        return await q_one("SELECT * FROM blacklist WHERE id = ?", (row["id"],)), False
    rid = await q_run("INSERT INTO blacklist (uuid, ign, reason, duration, added_by, added_at, updated_at, status) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?, 'active')", (uuid, ign, reason, duration, str(by), stamp, stamp))
    return await q_one("SELECT * FROM blacklist WHERE id = ?", (rid,)), True


class BlacklistCog(commands.GroupCog, name="blacklist", description="Guild blacklist"):
    def __init__(self, bot):
        self.bot = bot
        super().__init__()

    @property
    def core(self):
        return core_of(self.bot)

    async def announce(self, interaction, row, title):
        embed = blacklist_embed(row, title)
        posted = await send_embed(self.core, interaction, "BLACKLIST_CHANNEL_ID", embed=embed, thread_name=row["ign"])
        return posted

    @app_commands.command(name="add", description="Add a player to the guild blacklist")
    @app_commands.describe(ign="Minecraft username", reason="Why (free text)", duration="For how long (free text)")
    @app_commands.check(perm_check)
    async def add(self, interaction: discord.Interaction, ign: str, reason: str, duration: str):
        await interaction.response.defer()
        r = await lookup(interaction, ign)
        if r is None:
            return
        uuid, name = r
        row, created = await blacklist_upsert(uuid, name, reason, duration, interaction.user.id)
        posted = await self.announce(interaction, row, "Added to the guild blacklist" if created else "Blacklist entry updated")
        await interaction.followup.send(
            (f"**{esc(name)}** was added to the blacklist" if created else f"**{esc(name)}** was already blacklisted: entry updated")
            + (f" ({posted})." if posted else "."), allowed_mentions=NO_MENTIONS)

    @app_commands.command(name="edit", description="Change the reason and/or the duration of a blacklist entry")
    @app_commands.describe(ign="Minecraft username", reason="New reason (leave empty to keep it)",
                           duration="New duration (leave empty to keep it)")
    @app_commands.check(perm_check)
    async def edit(self, interaction: discord.Interaction, ign: str, reason: Optional[str] = None,
                   duration: Optional[str] = None):
        await interaction.response.defer()
        r = await lookup(interaction, ign)
        if r is None:
            return
        uuid, name = r
        row = await q_one("SELECT * FROM blacklist WHERE uuid = ? AND status = 'active'", (uuid,))
        if not row:
            await interaction.followup.send(f"**{esc(name)}** is not on the blacklist.", allowed_mentions=NO_MENTIONS)
            return
        if not reason and not duration:
            await interaction.followup.send("Give a new reason and/or a new duration.")
            return
        row, _ = await blacklist_upsert(uuid, name, reason, duration, interaction.user.id)
        posted = await self.announce(interaction, row, "Blacklist entry updated")
        await interaction.followup.send(f"Entry of **{esc(name)}** updated" + (f" ({posted})." if posted else "."),
                                        allowed_mentions=NO_MENTIONS)

    @app_commands.command(name="remove", description="Remove a player from the guild blacklist")
    @app_commands.check(perm_check)
    async def remove(self, interaction: discord.Interaction, ign: str):
        await interaction.response.defer()
        r = await lookup(interaction, ign)
        if r is None:
            return
        uuid, name = r
        row = await q_one("SELECT * FROM blacklist WHERE uuid = ? AND status = 'active'", (uuid,))
        if not row:
            await interaction.followup.send(f"**{esc(name)}** is not on the blacklist.", allowed_mentions=NO_MENTIONS)
            return
        await q_run("UPDATE blacklist SET status = 'removed', updated_at = ? WHERE id = ?", (iso(now_utc()), row["id"]))
        embed = blacklist_embed(row, "Removed from the guild blacklist")
        embed.color = GREY
        posted = await send_embed(self.core, interaction, "BLACKLIST_CHANNEL_ID", embed=embed, thread_name=row["ign"])
        await interaction.followup.send(f"**{esc(name)}** was removed from the blacklist" + (f" ({posted})." if posted else "."),
                                        allowed_mentions=NO_MENTIONS)

    @app_commands.command(name="list", description="Everyone on the guild blacklist, with reason and duration")
    @app_commands.check(perm_check)
    async def list(self, interaction: discord.Interaction):
        await interaction.response.defer()
        rows = await q_all("SELECT * FROM blacklist WHERE status = 'active' ORDER BY id")
        if not rows:
            await interaction.followup.send("The blacklist is empty.")
            return
        embed = discord.Embed(title="Guild blacklist", color=RED, description=f"{len(rows)} player(s)")
        for r in rows[:24]:
            embed.add_field(name=f"{r['ign']} · {r['duration'] or 'no duration'}", value=r["reason"] or "-", inline=False)
        if len(rows) > 24:
            embed.set_footer(text=f"Showing 24 of {len(rows)}")
        await interaction.followup.send(embed=embed)


# ======================================================================================
# cheating reports
# ======================================================================================

async def vote_counts(rid):
    rows = await q_all("SELECT vote, COUNT(*) AS c FROM ev_votes WHERE report_id = ? GROUP BY vote", (rid,))
    d = {r["vote"]: r["c"] for r in rows}
    return d.get("yes", 0), d.get("no", 0)


async def report_embed(row, footer=None):
    yes, no = await vote_counts(row["id"])
    colors = {"open": CYAN, "blacklisted": RED, "insufficient": GREY, "cancelled": GREY}
    e = discord.Embed(title=f"Cheating report #{row['id']}", color=colors.get(row["status"], CYAN),
                      description=f"Suspect: **{esc(row['ign'])}**")
    e.set_thumbnail(url=body_url(row["uuid"]))
    e.add_field(name="1 · Replay / media",
                value=row["replays"] or "*Not added yet. Staff: press **Replay / media**.*", inline=False)
    e.add_field(name="2 · Reason", value=row["reason"] or "*Not added yet. Staff: press **Reason**.*", inline=False)
    e.add_field(name="Votes", value=f"✅ Sufficient evidence: **{yes}**   ·   ❌ Insufficient evidence: **{no}**",
                inline=False)
    if row["source"]:
        e.add_field(name="Source", value=row["source"], inline=False)
    state = {"open": "Open", "blacklisted": "Closed: blacklisted", "insufficient": "Closed: insufficient evidence",
             "cancelled": "Cancelled"}.get(row["status"], row["status"])
    e.set_footer(text=footer or f"{state} · opened by {row['created_by'] or '?'}")
    return e


class EvButton(discord.ui.DynamicItem[discord.ui.Button],
               template=r"np:ev:(?P<act>rp|rs|ys|ns|end|cx|bl|ie):(?P<rid>\d+)"):
    LABELS = {"rp": "Replay / media", "rs": "Reason", "ys": "Sufficient evidence", "ns": "Insufficient evidence",
              "end": "End cheating report", "cx": "Cancel", "bl": "Add to blacklist", "ie": "Insufficient evidence"}
    STYLES = {"rp": discord.ButtonStyle.primary, "rs": discord.ButtonStyle.primary,
              "ys": discord.ButtonStyle.success, "ns": discord.ButtonStyle.danger,
              "end": discord.ButtonStyle.secondary, "cx": discord.ButtonStyle.secondary,
              "bl": discord.ButtonStyle.danger, "ie": discord.ButtonStyle.secondary}
    ROWS = {"rp": 0, "rs": 0, "ys": 1, "ns": 1, "end": 2, "cx": 2, "bl": 0, "ie": 0}

    def __init__(self, act: str, rid: int):
        super().__init__(discord.ui.Button(label=self.LABELS[act], style=self.STYLES[act],
                                           custom_id=f"np:ev:{act}:{rid}", row=self.ROWS[act]))
        self.act = act
        self.rid = int(rid)

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(match["act"], int(match["rid"]))

    async def callback(self, interaction: discord.Interaction):
        bot = interaction.client
        row = await q_one("SELECT * FROM cheat_reports WHERE id = ?", (self.rid,))
        if not row:
            await interaction.response.send_message("I can't find this report any more.", ephemeral=True)
            return
        if row["status"] != "open":
            await interaction.response.send_message("This report is already closed.", ephemeral=True)
            return
        if self.act in ("ys", "ns"):                       # voting: everybody
            vote = "yes" if self.act == "ys" else "no"
            await q_run("INSERT INTO ev_votes (report_id, user_id, vote) VALUES (?, ?, ?) "
                        "ON CONFLICT(report_id, user_id) DO UPDATE SET vote = excluded.vote",
                        (self.rid, str(interaction.user.id), vote))
            await interaction.response.edit_message(embed=await report_embed(row))
            return
        if not allowed(interaction.user):
            await deny(interaction)
            return
        if self.act == "rp":
            await interaction.response.send_modal(ReplayModal(self.rid))
        elif self.act == "rs":
            await interaction.response.send_modal(ReasonModal(self.rid, row["reason"] or ""))
        elif self.act == "cx":
            await interaction.response.send_modal(CancelModal(self.rid))
        elif self.act == "end":
            view = discord.ui.View(timeout=None)
            view.add_item(EvButton("bl", self.rid))
            view.add_item(EvButton("ie", self.rid))
            await interaction.response.edit_message(
                embed=await report_embed(row, footer="Choose the verdict: add to the blacklist or insufficient evidence"),
                view=view)
        elif self.act == "bl":
            await interaction.response.send_modal(VerdictBlacklistModal(self.rid, row["reason"] or ""))
        elif self.act == "ie":
            await close_report(interaction, row, "insufficient", "Insufficient evidence", by=interaction.user)


def report_view(rid):
    view = discord.ui.View(timeout=None)
    for act in ("rp", "rs", "ys", "ns", "end", "cx"):
        view.add_item(EvButton(act, rid))
    return view


class ReplayModal(discord.ui.Modal, title="Replay codes / media"):
    text = discord.ui.TextInput(label="Replay codes and/or media links", style=discord.TextStyle.paragraph,
                                max_length=1500, placeholder="One per line: replay codes, YouTube / Medal / image links")

    def __init__(self, rid):
        super().__init__()
        self.rid = rid

    async def on_submit(self, interaction: discord.Interaction):
        row = await q_one("SELECT * FROM cheat_reports WHERE id = ?", (self.rid,))
        lines = [x.strip() for x in self.text.value.splitlines() if x.strip()]
        merged = ((row["replays"] + "\n") if row["replays"] else "") + "\n".join(f"• {x}" for x in lines)
        await q_run("UPDATE cheat_reports SET replays = ? WHERE id = ?", (merged[:1000], self.rid))
        row = await q_one("SELECT * FROM cheat_reports WHERE id = ?", (self.rid,))
        await interaction.response.edit_message(embed=await report_embed(row))


class ReasonModal(discord.ui.Modal, title="Reason"):
    reason = discord.ui.TextInput(label="Why is this player suspected?", style=discord.TextStyle.paragraph,
                                  max_length=600, placeholder="Cheating, boosting, ...")

    def __init__(self, rid, current=""):
        super().__init__()
        self.rid = rid
        self.reason.default = current

    async def on_submit(self, interaction: discord.Interaction):
        await q_run("UPDATE cheat_reports SET reason = ? WHERE id = ?", (self.reason.value.strip(), self.rid))
        row = await q_one("SELECT * FROM cheat_reports WHERE id = ?", (self.rid,))
        await interaction.response.edit_message(embed=await report_embed(row))


class CancelModal(discord.ui.Modal, title="Cancel the report"):
    reason = discord.ui.TextInput(label="Reason (optional)", required=False, max_length=300)

    def __init__(self, rid):
        super().__init__()
        self.rid = rid

    async def on_submit(self, interaction: discord.Interaction):
        row = await q_one("SELECT * FROM cheat_reports WHERE id = ?", (self.rid,))
        await close_report(interaction, row, "cancelled", "Report cancelled", by=interaction.user,
                           note=self.reason.value.strip() or None)


class VerdictBlacklistModal(discord.ui.Modal, title="Add to the blacklist"):
    reason = discord.ui.TextInput(label="Reason", style=discord.TextStyle.paragraph, max_length=600)
    duration = discord.ui.TextInput(label="Duration (free text)", max_length=60, placeholder="e.g. permanent, 6 months")

    def __init__(self, rid, current=""):
        super().__init__()
        self.rid = rid
        self.reason.default = current

    async def on_submit(self, interaction: discord.Interaction):
        row = await q_one("SELECT * FROM cheat_reports WHERE id = ?", (self.rid,))
        entry, _ = await blacklist_upsert(row["uuid"], row["ign"], self.reason.value.strip(),
                                          self.duration.value.strip(), interaction.user.id)
        core = core_of(interaction.client)
        await send_embed(core, interaction, "BLACKLIST_CHANNEL_ID", embed=blacklist_embed(entry, "Added to the guild blacklist"),
                         thread_name=entry["ign"])
        await close_report(interaction, row, "blacklisted", "Added to the blacklist", by=interaction.user,
                           note=f"Reason: {entry['reason']}\nDuration: {entry['duration']}")


async def close_report(interaction, row, status, verdict_text, *, by, note=None):
    """Closes the report: buttons removed, final verdict message in the evidence channel."""
    await q_run("UPDATE cheat_reports SET status = ?, verdict = ? WHERE id = ?", (status, verdict_text, row["id"]))
    row = await q_one("SELECT * FROM cheat_reports WHERE id = ?", (row["id"],))
    yes, no = await vote_counts(row["id"])
    embed = await report_embed(row, footer=f"{verdict_text} · by {by.display_name}")
    if interaction.response.is_done():
        await interaction.message.edit(embed=embed, view=None)
    else:
        await interaction.response.edit_message(embed=embed, view=None)
    icon = {"blacklisted": "✅", "insufficient": "❌", "cancelled": "🚫"}[status]
    final = discord.Embed(title=f"Final verdict · report #{row['id']}",
                          color={"blacklisted": RED, "insufficient": GREY, "cancelled": GREY}[status],
                          description=f"Suspect: **{esc(row['ign'])}**\n\n{icon} **{verdict_text}**")
    final.set_thumbnail(url=head_url(row["uuid"]))
    if note:
        final.add_field(name="Details", value=note, inline=False)
    if status != "cancelled":
        final.add_field(name="Votes", value=f"Sufficient: **{yes}** · Insufficient: **{no}**", inline=False)
    final.set_footer(text=f"Closed by {by.display_name}")
    await interaction.channel.send(embed=final)


async def create_report(core, interaction, uuid, ign, *, created_by, reason=None, source=None):
    """Creates the report message (interactive). Returns (report id, channel mention or None)."""
    rid = await q_run("INSERT INTO cheat_reports (uuid, ign, reason, status, source, created_by, created_at) "
                      "VALUES (?, ?, ?, 'open', ?, ?, ?)", (uuid, ign, reason, source, created_by, iso(now_utc())))
    row = await q_one("SELECT * FROM cheat_reports WHERE id = ?", (rid,))
    posted = await send_embed(core, interaction, "EVIDENCE_CHANNEL_ID", embed=await report_embed(row),
                              view=report_view(rid), thread_name="Evidence", thread_key="evidence_thread")
    if core.last_message is not None:
        await q_run("UPDATE cheat_reports SET channel_id = ?, message_id = ? WHERE id = ?",
                    (str(core.last_message.channel.id), str(core.last_message.id), rid))
    return rid, posted


class EvidenceCog(commands.GroupCog, name="evidence", description="Cheating reports"):
    def __init__(self, bot):
        self.bot = bot
        super().__init__()

    @app_commands.command(name="add", description="Open a cheating report for a player")
    @app_commands.describe(ign="Minecraft username of the suspect")
    @app_commands.check(perm_check)
    async def add(self, interaction: discord.Interaction, ign: str):
        await interaction.response.defer(ephemeral=True)
        r = await lookup(interaction, ign)
        if r is None:
            return
        uuid, name = r
        open_row = await q_one("SELECT id FROM cheat_reports WHERE uuid = ? AND status = 'open'", (uuid,))
        if open_row:
            await interaction.followup.send(f"There is already an open report for **{esc(name)}** (#{open_row['id']}). "
                                            "Close it first.", allowed_mentions=NO_MENTIONS)
            return
        rid, posted = await create_report(core_of(self.bot), interaction, uuid, name,
                                          created_by=interaction.user.display_name)
        await interaction.followup.send(f"Report #{rid} opened" + (f" in {posted}." if posted else "."), ephemeral=True)


# ======================================================================================
# Urchin (community cheater tags)
# ======================================================================================
# The exact API address and key format are set with variables, so they can be adjusted without touching the code:
#   URCHIN_API_KEY      your key (from the Urchin Discord)
#   URCHIN_URL          address template, {uuid} {name} {key} are replaced. Default below.
#   URCHIN_KEY_HEADER   if the key goes in an HTTP header instead of the address (for example Authorization)

URCHIN_DEFAULT_URL = "https://urchin.ws/player/{uuid}?sources=MANUAL&key={key}"


class UrchinError(Exception):
    pass


def parse_tags(data):
    """Understands the usual shapes: {'tags': [...]}, [...] or {uuid: [...]}."""
    tags = None
    if isinstance(data, dict):
        tags = data.get("tags")
        if tags is None:
            for v in data.values():
                if isinstance(v, list):
                    tags = v
                    break
    elif isinstance(data, list):
        tags = data
    out = []
    for t in tags or []:
        if isinstance(t, dict):
            kind = t.get("type") or t.get("tag_type") or t.get("tag") or "tag"
            out.append((str(kind), str(t.get("reason") or "").strip()))
        elif isinstance(t, str):
            out.append((t, ""))
    return out


async def urchin_fetch(uuid, name):
    """Returns (http status, parsed JSON or text). Raises UrchinError when it is not configured."""
    key = _env("URCHIN_API_KEY")
    if not key:
        raise UrchinError("URCHIN_API_KEY is not set.")
    url = _env("URCHIN_URL", URCHIN_DEFAULT_URL).format(uuid=uuid, name=name, key=key)
    headers = {"User-Agent": "NoPressureBot"}
    if _env("URCHIN_KEY_HEADER"):
        headers[_env("URCHIN_KEY_HEADER")] = key
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.get(url) as resp:
            text = await resp.text()
            try:
                body = json.loads(text)
            except ValueError:
                body = text
            return resp.status, body


async def urchin_tags(uuid, name):
    status, body = await urchin_fetch(uuid, name)
    if status == 404:
        return []
    if status in (401, 403):
        raise UrchinError("The Urchin key was rejected (401/403).")
    if status == 429:
        raise UrchinError("Urchin rate limit reached (429).")
    if status >= 400:
        raise UrchinError(f"Urchin answered HTTP {status}.")
    return parse_tags(body)


def tags_signature(tags):
    return hashlib.sha1(json.dumps(sorted(tags)).encode()).hexdigest()


def urchin_embed(uuid, name, tags):
    e = discord.Embed(title="Urchin: tagged player in the guild", color=GOLD,
                      description=f"**{esc(name)}** is tagged on Urchin.")
    e.set_thumbnail(url=head_url(uuid))
    for kind, reason in tags[:10]:
        e.add_field(name=kind, value=reason or "No reason given", inline=False)
    e.set_footer(text="Press the button to open a cheating report")
    return e


class UrchinReportButton(discord.ui.DynamicItem[discord.ui.Button], template=r"np:ur:(?P<uuid>[0-9a-f]{32})"):
    def __init__(self, uuid):
        super().__init__(discord.ui.Button(label="Create cheating report", style=discord.ButtonStyle.danger,
                                           custom_id=f"np:ur:{uuid}"))
        self.uuid = uuid

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(match["uuid"])

    async def callback(self, interaction: discord.Interaction):
        if not allowed(interaction.user):
            await deny(interaction)
            return
        await interaction.response.defer(ephemeral=True)
        core = core_of(interaction.client)
        open_row = await q_one("SELECT id FROM cheat_reports WHERE uuid = ? AND status = 'open'", (self.uuid,))
        if open_row:
            await interaction.followup.send(f"There is already an open report (#{open_row['id']}) for this player.",
                                            ephemeral=True)
            return
        name = await core.name_for(self.uuid)
        reason, source = None, "Urchin tag"
        try:
            tags = await urchin_tags(self.uuid, name)
            if tags:
                reason = "Urchin: " + "; ".join(f"{k} ({r})" if r else k for k, r in tags)[:550]
        except Exception:
            pass
        rid, posted = await create_report(core, interaction, self.uuid, name, created_by=interaction.user.display_name,
                                          reason=reason, source=source)
        await interaction.followup.send(f"Report #{rid} opened" + (f" in {posted}." if posted else "."), ephemeral=True)


async def post_urchin_alert(core, uuid, name, tags):
    embed = urchin_embed(uuid, name, tags)
    view = discord.ui.View(timeout=None)
    view.add_item(UrchinReportButton(uuid))
    return await core.post_to("EVIDENCE_CHANNEL_ID", embed=embed, view=view, thread_name="Evidence",
                              thread_key="evidence_thread")


class UrchinCog(commands.GroupCog, name="urchin", description="Urchin cheater tags"):
    def __init__(self, bot):
        self.bot = bot
        super().__init__()
        self.watch.start()

    def cog_unload(self):
        self.watch.cancel()

    @property
    def core(self):
        return core_of(self.bot)

    @app_commands.command(name="check", description="Show the Urchin tags of a player")
    @app_commands.check(perm_check)
    async def check(self, interaction: discord.Interaction, ign: str):
        await interaction.response.defer()
        r = await lookup(interaction, ign)
        if r is None:
            return
        uuid, name = r
        try:
            tags = await urchin_tags(uuid, name)
        except UrchinError as e:
            await interaction.followup.send(str(e))
            return
        except Exception as e:
            await interaction.followup.send(f"Could not reach Urchin: {e}")
            return
        if not tags:
            await interaction.followup.send(f"**{esc(name)}** has no tags on Urchin.", allowed_mentions=NO_MENTIONS)
            return
        view = discord.ui.View(timeout=None)
        view.add_item(UrchinReportButton(uuid))
        await interaction.followup.send(embed=urchin_embed(uuid, name, tags), view=view)

    @app_commands.command(name="test", description="Debug: show the raw answer of the Urchin API for a player")
    @app_commands.check(perm_check)
    async def test(self, interaction: discord.Interaction, ign: str):
        await interaction.response.defer(ephemeral=True)
        r = await lookup(interaction, ign)
        if r is None:
            return
        uuid, name = r
        try:
            status, body = await urchin_fetch(uuid, name)
        except Exception as e:
            await interaction.followup.send(f"Error: {e}", ephemeral=True)
            return
        shown = body if isinstance(body, str) else json.dumps(body)[:900]
        await interaction.followup.send(f"HTTP {status}\n```{shown[:900]}```\nTags understood: {parse_tags(body)}",
                                        ephemeral=True)

    @app_commands.command(name="scan", description="Post now every guild member that is tagged on Urchin")
    @app_commands.check(perm_check)
    async def scan(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            found = await self.scan_guild(post=True, only_new=False)
        except UrchinError as e:
            await interaction.followup.send(str(e), ephemeral=True)
            return
        await interaction.followup.send(f"Scan finished: {found} tagged member(s) posted.", ephemeral=True)

    async def scan_guild(self, *, post, only_new):
        members = await self.core.guild_members(force=True)
        found = 0
        for uuid in list(members):
            name = await self.core.name_for(uuid)
            tags = await urchin_tags(uuid, name)
            sig = tags_signature(tags) if tags else ""
            seen = await q_one("SELECT sig FROM urchin_seen WHERE uuid = ?", (uuid,))
            changed = (seen is None and tags) or (seen is not None and seen["sig"] != sig and tags)
            await q_run("INSERT INTO urchin_seen (uuid, sig, seen_at) VALUES (?, ?, ?) "
                        "ON CONFLICT(uuid) DO UPDATE SET sig = excluded.sig, seen_at = excluded.seen_at",
                        (uuid, sig, iso(now_utc())))
            if tags and (changed or not only_new) and post:
                if await post_urchin_alert(self.core, uuid, name, tags):
                    found += 1
            await asyncio.sleep(1.2)
        return found

    @tasks.loop(minutes=30)
    async def watch(self):
        if not _env("URCHIN_API_KEY") or bl.chan_id("EVIDENCE_CHANNEL_ID") is None:
            return
        try:
            baseline = await kv_get("urchin_baseline")
            # the very first pass only remembers who is already tagged (no flood of old tags)
            await self.scan_guild(post=bool(baseline), only_new=True)
            if not baseline:
                await kv_set("urchin_baseline", iso(now_utc()))
        except UrchinError as e:
            log.warning("Urchin watch stopped: %s", e)
        except Exception:
            log.exception("The Urchin watch failed")

    @watch.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(90)


# ======================================================================================

async def setup(bot: commands.Bot):
    async with aiosqlite.connect(bl.DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()
    bot.add_dynamic_items(EvButton, UrchinReportButton)
    await bot.add_cog(BlacklistCog(bot))
    await bot.add_cog(EvidenceCog(bot))
    await bot.add_cog(UrchinCog(bot))

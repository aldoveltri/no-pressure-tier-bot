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
import re
import time
from typing import Optional

import aiohttp
import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands, tasks

import alerts
import breaklist as bl
import evidence_cards as ec
import graphics as g
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
    created_by TEXT, created_at TEXT, channel_id TEXT, message_id TEXT,
    confirmed_at TEXT, confirmed_by TEXT, closed_by TEXT
);
CREATE TABLE IF NOT EXISTS ev_votes (
    report_id INTEGER NOT NULL, user_id TEXT NOT NULL, vote TEXT NOT NULL, name TEXT,
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


async def send_embed(core, interaction, env_name, *, embed=None, embeds=None, content=None, view=None,
                     thread_name=None, thread_key=None, media=None):
    """Posts in the channel of the variable; without one (tests) or on failure, in the current channel.
    Returns the channel mention or None."""
    core.last_message = None
    posted = await core.post_to(env_name, content=content, embed=embed, embeds=embeds, view=view,
                                thread_name=thread_name, thread_key=thread_key, media=media)
    if posted:
        return posted
    kwargs = {}
    if embeds:
        kwargs["embeds"] = embeds
    elif embed is not None:
        kwargs["embed"] = embed
    if content:
        kwargs["content"] = content
    if view is not None:
        kwargs["view"] = view
    if media:
        kwargs["files"] = [discord.File(io.BytesIO(data), filename=name) for name, data in media]
    core.last_message = await interaction.channel.send(**kwargs)
    return None


# ======================================================================================
# blacklist
# ======================================================================================

URL_RE = re.compile(r"https?://[^\s<>()\"']+")


def urls_in(*texts):
    """The links written in the texts (links inside an embed are never previewed: they go in the message text)."""
    seen = []
    for t in texts:
        for u in URL_RE.findall(t or ""):
            if u not in seen:
                seen.append(u)
    return seen


def video_text(row):
    links = urls_in(row["reason"], row["duration"])
    return ("**Videos / links**\n" + "\n".join(links))[:1800] if links else None


def blacklist_embed(row, title="Guild blacklist"):
    e = discord.Embed(title=title, color=RED, timestamp=now_utc(), url=f"https://namemc.com/profile/{row['uuid']}")
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

    async def announce(self, interaction, row, title, media=None):
        """Posts the entry in the blacklist channel. media = [(file name, bytes)]: images are shown in the post."""
        if not await alerts.enabled("blacklist"):
            return None                                 # /alerts: blacklist posts are off, the entry is only saved
        embed = blacklist_embed(row, title)
        embeds = [embed]
        pics = [f"attachment://{n}" for n, _ in (media or []) if n.lower().endswith(IMG_EXT)]
        if pics:
            embed.set_image(url=pics[0])
            for pic in pics[1:4]:                       # same url = Discord shows them as one gallery
                extra = discord.Embed(url=embed.url, color=RED)
                extra.set_image(url=pic)
                embeds.append(extra)
        return await send_embed(self.core, interaction, "BLACKLIST_CHANNEL_ID", embeds=embeds, media=media or None,
                                content=video_text(row), thread_name=row["ign"])

    async def read_media(self, interaction, attachments):
        """The files given to the command -> ([(name, bytes)], [skipped texts])."""
        media, skipped = [], []
        limit = getattr(interaction.guild, "filesize_limit", None) or 10 * 1024 * 1024
        for att in attachments:
            if att is None:
                continue
            if att.size > limit:
                skipped.append(f"{att.filename} (too big)")
                continue
            media.append((f"bl{len(media) + 1}-{safe_name(att.filename)}", await att.read()))
        return media, skipped

    @app_commands.command(name="add", description="Add a player to the guild blacklist")
    @app_commands.describe(ign="Minecraft username", reason="Why (free text)", duration="For how long (free text)",
                           image="Screenshot or video (optional)", image2="Another screenshot or video (optional)",
                           image3="Another screenshot or video (optional)")
    @app_commands.check(perm_check)
    async def add(self, interaction: discord.Interaction, ign: str, reason: str, duration: str,
                  image: Optional[discord.Attachment] = None, image2: Optional[discord.Attachment] = None,
                  image3: Optional[discord.Attachment] = None):
        await interaction.response.defer()
        r = await lookup(interaction, ign)
        if r is None:
            return
        uuid, name = r
        media, skipped = await self.read_media(interaction, (image, image2, image3))
        row, created = await blacklist_upsert(uuid, name, reason, duration, interaction.user.id)
        posted = await self.announce(interaction, row, "Added to the guild blacklist" if created else "Blacklist entry updated",
                                     media)
        await interaction.followup.send(
            (f"**{esc(name)}** was added to the blacklist" if created else f"**{esc(name)}** was already blacklisted: entry updated")
            + (f" ({posted})." if posted else ".")
            + (f"\nNot added: {', '.join(skipped)}" if skipped else ""), allowed_mentions=NO_MENTIONS)

    @app_commands.command(name="edit", description="Change the reason and/or the duration of a blacklist entry")
    @app_commands.describe(ign="Minecraft username", reason="New reason (leave empty to keep it)",
                           duration="New duration (leave empty to keep it)",
                           image="Screenshot or video (optional)", image2="Another screenshot or video (optional)",
                           image3="Another screenshot or video (optional)")
    @app_commands.check(perm_check)
    async def edit(self, interaction: discord.Interaction, ign: str, reason: Optional[str] = None,
                   duration: Optional[str] = None, image: Optional[discord.Attachment] = None,
                   image2: Optional[discord.Attachment] = None, image3: Optional[discord.Attachment] = None):
        await interaction.response.defer()
        r = await lookup(interaction, ign)
        if r is None:
            return
        uuid, name = r
        row = await q_one("SELECT * FROM blacklist WHERE uuid = ? AND status = 'active'", (uuid,))
        if not row:
            await interaction.followup.send(f"**{esc(name)}** is not on the blacklist.", allowed_mentions=NO_MENTIONS)
            return
        media, skipped = await self.read_media(interaction, (image, image2, image3))
        if not reason and not duration and not media:
            await interaction.followup.send("Give a new reason, a new duration and/or images.")
            return
        row, _ = await blacklist_upsert(uuid, name, reason, duration, interaction.user.id)
        posted = await self.announce(interaction, row, "Blacklist entry updated", media)
        await interaction.followup.send(f"Entry of **{esc(name)}** updated" + (f" ({posted})." if posted else ".")
                                        + (f"\nNot added: {', '.join(skipped)}" if skipped else ""),
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
        posted = (await send_embed(self.core, interaction, "BLACKLIST_CHANNEL_ID", embed=embed, thread_name=row["ign"])
                  if await alerts.enabled("blacklist") else None)
        await interaction.followup.send(f"**{esc(name)}** was removed from the blacklist" + (f" ({posted})." if posted else "."),
                                        allowed_mentions=NO_MENTIONS)

    @app_commands.command(name="view", description="Show one player's blacklist entry, with the videos and links playable")
    @app_commands.describe(ign="Minecraft username")
    async def view(self, interaction: discord.Interaction, ign: str):
        await interaction.response.defer()
        r = await lookup(interaction, ign)
        if r is None:
            return
        uuid, name = r
        row = await q_one("SELECT * FROM blacklist WHERE uuid = ? AND status = 'active'", (uuid,))
        if not row:
            await interaction.followup.send(f"**{esc(name)}** is not on the blacklist.", allowed_mentions=NO_MENTIONS)
            return
        await interaction.followup.send(content=video_text(row), embed=blacklist_embed(row), allowed_mentions=NO_MENTIONS)

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
            by = str(r["added_by"] or "")
            by = f"<@{by}>" if by.isdigit() else (by or "unknown")
            embed.add_field(name=f"{r['ign']} · {r['duration'] or 'no duration'}",
                            value=f"{r['reason'] or '-'}\n**Added by:** {by}", inline=False)
        if len(rows) > 24:
            embed.set_footer(text=f"Showing 24 of {len(rows)}")
        else:
            embed.set_footer(text="Videos and links: /blacklist view <player>")
        await interaction.followup.send(embed=embed)


# ======================================================================================
# cheating reports
# ======================================================================================

async def vote_lists(rid):
    """Who voted 'sufficient' and 'insufficient': lists of Discord ids (the votes are public)."""
    rows = await q_all("SELECT vote, user_id FROM ev_votes WHERE report_id = ? ORDER BY rowid", (rid,))
    return ([r["user_id"] for r in rows if r["vote"] == "yes"], [r["user_id"] for r in rows if r["vote"] == "no"])


async def vote_names(rid):
    """Names of who voted 'sufficient' and 'insufficient' (shown on the card)."""
    rows = await q_all("SELECT vote, name FROM ev_votes WHERE report_id = ? ORDER BY rowid", (rid,))
    return ([r["name"] or "?" for r in rows if r["vote"] == "yes"], [r["name"] or "?" for r in rows if r["vote"] == "no"])


def phase_of(row, ending=False):
    """draft (staff fill in the evidence) -> voting (confirmed, everybody votes) -> verdict (staff decide) -> closed."""
    if row["status"] != "open":
        return row["status"]
    if not row["confirmed_at"]:
        return "draft"
    return "verdict" if ending else "voting"


STATE_TEXT = {"draft": "Draft · staff are adding the evidence", "voting": "Voting open",
              "verdict": "Waiting for the staff verdict", "blacklisted": "Closed · blacklisted",
              "insufficient": "Closed · insufficient evidence", "cancelled": "Cancelled"}
STATE_COLOR = {"draft": GOLD, "voting": CYAN, "verdict": GOLD, "blacklisted": RED, "insufficient": GREY,
               "cancelled": GREY}
IMG_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp")
IMG_HOSTS = ("cdn.discordapp.com", "media.discordapp.net", "i.imgur.com", "i.ibb.co", "files.catbox.moe")
UUID_LIKE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-", re.I)


def classify(line):
    """One saved line -> (kind, text). kind: replay | image | link | file (uploaded to the report)."""
    line = line.lstrip("•-* ").strip()
    if line.startswith("📎"):
        return "file", line[1:].strip()
    m = re.search(r"https?://\S+", line)
    if not m:
        code = line
        if UUID_LIKE.match(code) or code.lower().startswith("/replay"):
            code = code if code.lower().startswith("/replay") else f"/replay {code}"
        return "replay", code
    url = m.group(0)
    host = re.sub(r"^https?://", "", url).split("/")[0].lower()
    if url.lower().split("?")[0].endswith(IMG_EXT) or host in IMG_HOSTS:
        return "image", url
    return "link", url


def display_name(stored):
    """'ev3-2-clip.mp4' -> 'clip.mp4'."""
    return re.sub(r"^ev\d+-\d+-", "", stored)


def short_url(url):
    return re.sub(r"^https?://(www\.)?", "", url)[:60]


async def flag_text(uuid):
    row = await q_one("SELECT code FROM countries WHERE uuid = ?", (uuid,))
    return bl.flag_emoji(row["code"]) if row else "🏳️"


def mention_list(ids, limit=40):
    if not ids:
        return "*nobody yet*"
    shown = "\n".join(f"<@{i}>" for i in ids[:limit])
    return shown + (f"\n+{len(ids) - limit} more" if len(ids) > limit else "")


def votes_fields(embed, yes, no):
    embed.add_field(name=f"✅ Sufficient evidence [{len(yes)}]", value=mention_list(yes), inline=True)
    embed.add_field(name=f"❌ Insufficient evidence [{len(no)}]", value=mention_list(no), inline=True)


async def build_report_text(row, phase=None, *, title=None):
    """The report as plain Discord embeds (used for the copy of the evidence in the blacklist channel)."""
    phase = phase or phase_of(row)
    yes, no = await vote_lists(row["id"])
    url = f"https://namemc.com/profile/{row['uuid']}"
    e = discord.Embed(title=title or f"Cheating Report #{row['id']}", url=url, color=STATE_COLOR[phase])
    e.set_thumbnail(url=head_url(row["uuid"], 160))
    desc = f"**IGN** · `{row['ign']}` {await flag_text(row['uuid'])}"
    if row["source"]:
        desc += f"\n**Source** · {esc(row['source'])}"
    e.description = desc

    lines = [x for x in (row["replays"] or "").splitlines() if x.strip()]
    images, links, replays, files = [], [], [], []
    for line in lines:
        kind, text = classify(line)
        if kind == "file":
            files.append(text)
        else:
            (images if kind == "image" else links if kind == "link" else replays).append(text)
    file_images = [f"attachment://{n}" for n in files if n.lower().endswith(IMG_EXT)]
    ev = [f"• `{t}`" for t in replays] + [f"• [{short_url(u)}]({u})" for u in links + images]
    value = "\n".join(ev)[:1000] if ev else ("*Screenshots / videos are attached.*" if files
                                             else "*Nothing added yet. Staff: press **Replay / media**.*")
    e.add_field(name="Evidence", value=value, inline=False)
    reason = (row["reason"] or "").strip()
    e.add_field(name="Reason", value=("> " + reason.replace("\n", "\n> "))[:1000] if reason
                else "*Nothing added yet. Staff: press **Reason**.*", inline=False)
    if phase == "draft":
        e.add_field(name="Votes", value="🔒 Voting opens when staff confirm the evidence.", inline=False)
    else:
        votes_fields(e, yes, no)
    state = STATE_TEXT[phase]
    if row["confirmed_by"] and phase != "draft":
        state += f" · confirmed by {row['confirmed_by']}"
    e.set_footer(text=f"{state} · opened by {row['created_by'] or '?'}")

    embeds = [e]
    images = file_images + images                     # the files uploaded to the report come first
    if images:
        e.set_image(url=images[0])
        for img in images[1:4]:                      # same url = Discord shows them as one gallery
            extra = discord.Embed(url=url, color=STATE_COLOR[phase])
            extra.set_image(url=img)
            embeds.append(extra)
    content = None
    if links:                                         # raw links: Discord shows the video previews
        content = ("**Videos / links**\n" + "\n".join(links))[:1800]
    return embeds, content


async def build_report(row, phase=None):
    """The report message: a picture with the summary (state, suspect, reason, public votes) on top, then the
    evidence as text (copyable replay codes, links) and the uploaded screenshots in a gallery.
    Returns (embeds, message text with the videos / links, png of the card, file name of the card)."""
    phase = phase or phase_of(row)
    yes, no = await vote_names(row["id"])
    heads = await g._fetch_heads([(row["uuid"], row["ign"])])
    lines = [x for x in (row["replays"] or "").splitlines() if x.strip()]
    images, links, replays, files = [], [], [], []
    for line in lines:
        kind, text = classify(line)
        if kind == "file":
            files.append(text)
        else:
            (images if kind == "image" else links if kind == "link" else replays).append(text)
    png = await asyncio.to_thread(ec.draw_report, row, yes, no, heads.get(row["uuid"]), phase)
    name = f"card-{row['id']}-{int(time.time() * 1000)}.png"
    url = f"https://namemc.com/profile/{row['uuid']}"
    ev = [f"• `{t}`" for t in replays] + [f"• [{short_url(u)}]({u})" for u in links + images]
    if ev:
        text = ("**Evidence**\n" + "\n".join(ev))[:3800]
    elif files:
        text = None                                 # only uploaded files: they speak for themselves
    else:
        text = "*No evidence yet. Staff: press **Replay / media** to add replay codes, screenshots or videos.*"
    main = discord.Embed(url=url, color=STATE_COLOR[phase], description=text)
    main.set_image(url=f"attachment://{name}")
    embeds = [main]
    gallery = [f"attachment://{n}" for n in files if n.lower().endswith(IMG_EXT)] + images
    for img in gallery[:3]:                         # same url = one gallery: the card first, then the screenshots
        extra = discord.Embed(url=url, color=STATE_COLOR[phase])
        extra.set_image(url=img)
        embeds.append(extra)
    content = ("**Videos / links**\n" + "\n".join(links))[:1800] if links else None
    return embeds, content, png, name


class EvButton(discord.ui.DynamicItem[discord.ui.Button],
               template=r"np:ev:(?P<act>rp|rs|cf|ys|ns|end|cx|bl|ie|bk):(?P<rid>\d+)"):
    LABELS = {"rp": "Replay / media", "rs": "Reason", "cf": "Confirm & open voting",
              "ys": "Sufficient evidence", "ns": "Insufficient evidence",
              "end": "End cheating report", "cx": "Cancel", "bl": "Add to blacklist",
              "ie": "Insufficient evidence", "bk": "Back to voting"}
    STYLES = {"rp": discord.ButtonStyle.primary, "rs": discord.ButtonStyle.primary,
              "cf": discord.ButtonStyle.success,
              "ys": discord.ButtonStyle.success, "ns": discord.ButtonStyle.danger,
              "end": discord.ButtonStyle.secondary, "cx": discord.ButtonStyle.secondary,
              "bl": discord.ButtonStyle.danger, "ie": discord.ButtonStyle.secondary,
              "bk": discord.ButtonStyle.secondary}
    ROWS = {"rp": 0, "rs": 0, "cf": 1, "ys": 0, "ns": 0, "end": 1, "cx": 1, "bl": 0, "ie": 0, "bk": 1}

    def __init__(self, act: str, rid: int, disabled: bool = False, row: Optional[int] = None):
        super().__init__(discord.ui.Button(label=self.LABELS[act], style=self.STYLES[act],
                                           custom_id=f"np:ev:{act}:{rid}", row=self.ROWS[act] if row is None else row,
                                           disabled=disabled))
        self.act = act
        self.rid = int(rid)

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(match["act"], int(match["rid"]))

    async def callback(self, interaction: discord.Interaction):
        row = await q_one("SELECT * FROM cheat_reports WHERE id = ?", (self.rid,))
        if not row:
            await interaction.response.send_message("I can't find this report any more.", ephemeral=True)
            return
        if row["status"] != "open":
            await interaction.response.send_message("This report is already closed.", ephemeral=True)
            return
        phase = phase_of(row)
        if self.act in ("ys", "ns"):                       # voting: everybody, but only once the report is confirmed
            if phase != "voting":
                await interaction.response.send_message("Voting is not open (yet).", ephemeral=True)
                return
            vote = "yes" if self.act == "ys" else "no"
            await q_run("INSERT INTO ev_votes (report_id, user_id, vote, name) VALUES (?, ?, ?, ?) "
                        "ON CONFLICT(report_id, user_id) DO UPDATE SET vote = excluded.vote, name = excluded.name",
                        (self.rid, str(interaction.user.id), vote, interaction.user.display_name))
            await refresh(interaction, self.rid)
            return
        if not allowed(interaction.user):
            await deny(interaction)
            return
        if self.act in ("rp", "rs"):                      # staff can add or change evidence while drafting AND while voting
            if phase not in ("draft", "voting"):
                await interaction.response.send_message("The evidence can't be changed now.", ephemeral=True)
            elif self.act == "rp":
                await interaction.response.send_modal(ReplayModal(self.rid, text_lines(row["replays"])))
            else:
                await interaction.response.send_modal(ReasonModal(self.rid, row["reason"] or ""))
        elif self.act == "cf":
            if phase != "draft":
                await interaction.response.send_message("This report is already confirmed.", ephemeral=True)
            elif not ((row["replays"] or "").strip() and (row["reason"] or "").strip()):
                await interaction.response.send_message("Add the replay / media AND the reason first.", ephemeral=True)
            else:
                await q_run("UPDATE cheat_reports SET confirmed_at = ?, confirmed_by = ? WHERE id = ?",
                            (iso(now_utc()), interaction.user.display_name, self.rid))
                await refresh(interaction, self.rid)
        elif self.act == "cx":
            await interaction.response.send_modal(CancelModal(self.rid))
        elif self.act == "end":
            if phase != "voting":
                await interaction.response.send_message("Confirm the report first.", ephemeral=True)
            else:
                await refresh(interaction, self.rid, ending=True)
        elif self.act == "bk":
            await refresh(interaction, self.rid)
        elif self.act == "bl":
            await interaction.response.send_modal(VerdictBlacklistModal(self.rid, row["reason"] or ""))
        elif self.act == "ie":
            await close_report(interaction, row, "insufficient", "Insufficient evidence", by=interaction.user)


def view_for(row, ending=False):
    phase = phase_of(row, ending)
    view = discord.ui.View(timeout=None)
    rid = row["id"]
    if phase == "draft":
        ready = bool((row["replays"] or "").strip() and (row["reason"] or "").strip())   # files count as lines too
        view.add_item(EvButton("rp", rid))
        view.add_item(EvButton("rs", rid))
        view.add_item(EvButton("cf", rid, disabled=not ready))
        view.add_item(EvButton("cx", rid))
    elif phase == "voting":
        view.add_item(EvButton("ys", rid))
        view.add_item(EvButton("ns", rid))
        view.add_item(EvButton("rp", rid, row=1))          # staff can still add / change the evidence
        view.add_item(EvButton("rs", rid, row=1))
        view.add_item(EvButton("end", rid, row=2))
        view.add_item(EvButton("cx", rid, row=2))
    elif phase == "verdict":
        for act in ("bl", "ie", "bk"):
            view.add_item(EvButton(act, rid))
    else:
        return None
    return view


async def refresh(interaction, rid, *, ending=False, phase=None, new_files=None):
    """Redraws the report in the message that was clicked. The uploaded files stay; new_files = [(name, bytes)]."""
    row = await q_one("SELECT * FROM cheat_reports WHERE id = ?", (rid,))
    embeds, content, png, card_name = await build_report(row, phase or phase_of(row, ending))
    if not interaction.response.is_done():
        await interaction.response.defer()
    keep = [a for a in (getattr(getattr(interaction, "message", None), "attachments", None) or [])
            if not a.filename.startswith("card-")]            # the old picture is replaced by the new one
    attachments = (keep + [discord.File(io.BytesIO(data), filename=name) for name, data in (new_files or [])]
                   + [discord.File(io.BytesIO(png), filename=card_name)])
    await interaction.edit_original_response(content=content, embeds=embeds, attachments=attachments,
                                             view=view_for(row, ending))


MAX_FILES_PER_REPORT = 8


def text_lines(replays):
    """The codes / links of a report as plain text, one per line (the uploaded files are not part of it)."""
    out = []
    for x in (replays or "").splitlines():
        x = x.strip()
        if x and not x.lstrip("•-* ").startswith("📎"):
            out.append(x.lstrip("•-* ").strip())
    return "\n".join(out)


def safe_name(name):
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)[-50:] or "file"


class ReplayModal(discord.ui.Modal, title="Replay codes / media"):
    """Replay codes and links as text, and screenshots / videos uploaded straight into the report."""

    def __init__(self, rid, current=""):
        super().__init__()
        self.rid = rid
        self.codes = discord.ui.TextInput(style=discord.TextStyle.paragraph, max_length=1500, required=False,
                                          default=(current[:1500] or None),
                                          placeholder="One per line: replay codes, YouTube / Medal links")
        self.files = discord.ui.FileUpload(required=False, min_values=0, max_values=5)
        self.add_item(discord.ui.Label(text="Replay codes and/or links", component=self.codes))
        self.add_item(discord.ui.Label(text="Screenshots / videos", description="Optional: they are shown in the report",
                                       component=self.files))

    async def on_submit(self, interaction: discord.Interaction):
        row = await q_one("SELECT * FROM cheat_reports WHERE id = ?", (self.rid,))
        phase = phase_of(row)
        if phase not in ("draft", "voting"):
            await interaction.response.send_message("The evidence can't be changed now.", ephemeral=True)
            return
        # the text box shows the current codes / links: what is written now REPLACES them (so they can be edited or removed);
        # the files already uploaded stay, new ones are added
        lines = [x.strip() for x in (self.codes.value or "").splitlines() if x.strip()]
        old_files = [x for x in (row["replays"] or "").splitlines() if x.lstrip("•-* ").startswith("📎")]
        text_part = [f"• {x}" for x in lines]
        new_files, new_file_lines, skipped = [], [], []
        have = len(old_files)
        limit = getattr(interaction.guild, "filesize_limit", None) or 10 * 1024 * 1024
        await interaction.response.defer()
        for att in (self.files.values or []):
            if have + len(new_files) >= MAX_FILES_PER_REPORT:
                skipped.append(f"{att.filename} (max {MAX_FILES_PER_REPORT} files)")
                continue
            if att.size > limit:
                skipped.append(f"{att.filename} (too big: paste a YouTube / Medal link instead)")
                continue
            name = f"ev{self.rid}-{have + len(new_files) + 1}-{safe_name(att.filename)}"
            new_files.append((name, await att.read()))
            new_file_lines.append(f"📎 {name}")
        merged = "\n".join(text_part + old_files + new_file_lines)
        if not merged:
            await interaction.followup.send("The report needs at least one replay, link or file.", ephemeral=True)
            return
        if merged == (row["replays"] or "") and not new_files:
            await interaction.followup.send("Nothing changed.", ephemeral=True)
            return
        await q_run("UPDATE cheat_reports SET replays = ? WHERE id = ?", (merged[:3500], self.rid))
        await refresh(interaction, self.rid, new_files=new_files)
        if phase == "voting":
            await interaction.channel.send(f"✏️ The evidence of report #{self.rid} was updated by "
                                           f"{esc(interaction.user.display_name)}. Check it before you vote.",
                                           allowed_mentions=NO_MENTIONS)
        if skipped:
            await interaction.followup.send("Not added: " + "; ".join(skipped), ephemeral=True)


class ReasonModal(discord.ui.Modal, title="Reason"):
    reason = discord.ui.TextInput(label="Why is this player suspected?", style=discord.TextStyle.paragraph,
                                  max_length=600, placeholder="Cheating, boosting, ...")

    def __init__(self, rid, current=""):
        super().__init__()
        self.rid = rid
        self.reason.default = current

    async def on_submit(self, interaction: discord.Interaction):
        row = await q_one("SELECT * FROM cheat_reports WHERE id = ?", (self.rid,))
        phase = phase_of(row)
        if phase not in ("draft", "voting"):
            await interaction.response.send_message("The reason can't be changed now.", ephemeral=True)
            return
        new = self.reason.value.strip()
        if not new:
            await interaction.response.send_message("The reason can't be empty.", ephemeral=True)
            return
        await q_run("UPDATE cheat_reports SET reason = ? WHERE id = ?", (new, self.rid))
        await refresh(interaction, self.rid)
        if phase == "voting" and new != (row["reason"] or ""):
            await interaction.channel.send(f"✏️ The reason of report #{self.rid} was changed by "
                                           f"{esc(interaction.user.display_name)}. Check it before you vote.",
                                           allowed_mentions=NO_MENTIONS)


class CancelModal(discord.ui.Modal, title="Cancel the report"):
    reason = discord.ui.TextInput(label="Reason (optional)", required=False, max_length=300)

    def __init__(self, rid):
        super().__init__()
        self.rid = rid

    async def on_submit(self, interaction: discord.Interaction):
        row = await q_one("SELECT * FROM cheat_reports WHERE id = ?", (self.rid,))
        if row["status"] != "open":
            await interaction.response.send_message("This report is already closed.", ephemeral=True)
            return
        note = self.reason.value.strip() or None
        await close_report(interaction, row, "cancelled", "Report cancelled", by=interaction.user,
                           details=[("Reason", note)] if note else [])


class VerdictBlacklistModal(discord.ui.Modal, title="Add to the blacklist"):
    reason = discord.ui.TextInput(label="Reason", style=discord.TextStyle.paragraph, max_length=600)
    duration = discord.ui.TextInput(label="Duration (free text)", max_length=60, placeholder="e.g. permanent, 6 months")

    def __init__(self, rid, current=""):
        super().__init__()
        self.rid = rid
        self.reason.default = current

    async def on_submit(self, interaction: discord.Interaction):
        row = await q_one("SELECT * FROM cheat_reports WHERE id = ?", (self.rid,))
        if row["status"] != "open":
            await interaction.response.send_message("This report is already closed.", ephemeral=True)
            return
        await interaction.response.defer()
        entry, _ = await blacklist_upsert(row["uuid"], row["ign"], self.reason.value.strip(),
                                          self.duration.value.strip(), interaction.user.id)
        core = core_of(interaction.client)
        # the blacklist post carries the evidence too: replay codes, links, screenshots / videos and the votes
        media = []
        for att in (getattr(getattr(interaction, "message", None), "attachments", None) or []):
            if att.filename.startswith("card-"):
                continue                                  # the summary picture is not evidence
            try:
                media.append((att.filename, await att.read()))
            except Exception:
                log.warning("Could not copy %s into the blacklist post", att.filename)
        ev_embeds, ev_content = await build_report_text(row, "blacklisted", title=f"Evidence · report #{row['id']}")
        extra_links = [u for u in urls_in(entry["reason"]) if u not in (ev_content or "")]
        if extra_links:
            ev_content = ((ev_content or "**Videos / links**") + "\n" + "\n".join(extra_links))[:1900]
        await send_embed(core, interaction, "BLACKLIST_CHANNEL_ID",
                         embeds=[blacklist_embed(entry, "Added to the guild blacklist")] + ev_embeds,
                         content=ev_content, media=media, thread_name=entry["ign"])
        await close_report(interaction, row, "blacklisted", "Added to the blacklist", by=interaction.user,
                           details=[("Reason", entry["reason"]), ("Duration", entry["duration"])])


async def close_report(interaction, row, status, verdict_text, *, by, details=None):
    """Closes the report: the report shows the final state without buttons, and a final verdict message is posted."""
    await q_run("UPDATE cheat_reports SET status = ?, verdict = ?, closed_by = ? WHERE id = ?",
                (status, verdict_text, by.display_name, row["id"]))
    row = await q_one("SELECT * FROM cheat_reports WHERE id = ?", (row["id"],))
    await refresh(interaction, row["id"], phase=status)
    if details is None:
        details = [("Reported for", row["reason"])]
    yes_names, no_names = await vote_names(row["id"])
    heads = await g._fetch_heads([(row["uuid"], row["ign"])])
    png = await asyncio.to_thread(ec.draw_verdict, row, status, verdict_text, details, yes_names, no_names,
                                  heads.get(row["uuid"]), by.display_name)
    await interaction.channel.send(file=discord.File(io.BytesIO(png), filename=f"verdict-{row['id']}.png"))


async def create_report(core, interaction, uuid, ign, *, created_by, reason=None, source=None):
    """Creates the report message (draft). Returns (report id, channel mention or None)."""
    rid = await q_run("INSERT INTO cheat_reports (uuid, ign, reason, status, source, created_by, created_at) "
                      "VALUES (?, ?, ?, 'open', ?, ?, ?)", (uuid, ign, reason, source, created_by, iso(now_utc())))
    row = await q_one("SELECT * FROM cheat_reports WHERE id = ?", (rid,))
    embeds, content, png, card_name = await build_report(row)
    posted = await send_embed(core, interaction, "EVIDENCE_CHANNEL_ID", embeds=embeds, content=content,
                              media=[(card_name, png)], view=view_for(row), thread_name="Evidence",
                              thread_key="evidence_thread")
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
# Official Urchin API (v3). The key is sent in the X-API-Key header. You get it with /dashboard in the Urchin Discord.
#   URCHIN_API_KEY      your key
#   URCHIN_URL          optional, the API address (default https://api.urchin.gg/v3)
#   URCHIN_KEY_HEADER   optional, the header name of the key (default X-API-Key)

URCHIN_DEFAULT_URL = "https://api.urchin.gg/v3"


class UrchinError(Exception):
    pass


def _tag_pair(t):
    if isinstance(t, str):
        return t, ""
    kind = t.get("tag_type") or t.get("type") or t.get("tag") or "tag"
    reason = str(t.get("reason") or "").strip()
    by = t.get("added_by_username")
    if by and not t.get("hide_username"):
        reason = (reason + f"\nAdded by {by}").strip()
    return str(kind), reason


def parse_tags(data):
    """{'tags': [{'tag_type', 'reason', 'added_by_username', ...}]}  (also accepts a bare list)."""
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
    return [_tag_pair(t) for t in (tags or []) if isinstance(t, (dict, str))]


async def urchin_request(method, path, *, params=None, body=None):
    """Calls the Urchin API. Returns (http status, parsed JSON or text). Raises UrchinError when not configured."""
    key = _env("URCHIN_API_KEY")
    if not key:
        raise UrchinError("URCHIN_API_KEY is not set.")
    base = _env("URCHIN_URL", URCHIN_DEFAULT_URL).rstrip("/")
    headers = {"User-Agent": "NoPressureBot", "Accept": "application/json",
               _env("URCHIN_KEY_HEADER", "X-API-Key"): key}
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.request(method, f"{base}/{path.lstrip('/')}", params=params, json=body) as resp:
            text = await resp.text()
            try:
                data = json.loads(text)
            except ValueError:
                data = text
            return resp.status, data


def _check_status(status):
    if status in (401, 403):
        raise UrchinError("The Urchin key was rejected (401/403): check URCHIN_API_KEY.")
    if status == 429:
        raise UrchinError("Urchin rate limit reached (429): wait a bit.")
    if status >= 400 and status != 404:
        raise UrchinError(f"Urchin answered HTTP {status}.")


async def urchin_fetch(uuid, name):
    """Raw answer for one player: GET /player/tags?player=<name>."""
    return await urchin_request("GET", "player/tags", params={"player": name or uuid})


async def urchin_tags(uuid, name):
    status, body = await urchin_fetch(uuid, name)
    if status == 404:
        return []
    _check_status(status)
    return parse_tags(body)


async def urchin_batch(uuids):
    """Tags of many players with POST /players (100 at a time). Returns {uuid without dashes: [(kind, reason)]}."""
    out = {}
    uuids = list(uuids)
    for i in range(0, len(uuids), 100):
        status, body = await urchin_request("POST", "players", body={"uuids": uuids[i:i + 100]})
        _check_status(status)
        players = body.get("players", {}) if isinstance(body, dict) else {}
        for k, val in players.items():
            tags = parse_tags(val if isinstance(val, list) else (val or {}))
            if tags:
                out[str(k).replace("-", "").lower()] = tags
    return out


def tags_signature(tags):
    return hashlib.sha1(json.dumps(sorted(tags)).encode()).hexdigest()


async def urchin_card(core, uuid, name, tags):
    head = await core.head_for(uuid, name)
    return await asyncio.to_thread(ec.draw_urchin, name, tags, head)


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
    png = await urchin_card(core, uuid, name, tags)
    view = discord.ui.View(timeout=None)
    view.add_item(UrchinReportButton(uuid))
    return await core.post_to("EVIDENCE_CHANNEL_ID", data=png, filename="urchin.png", view=view,
                              thread_name="Evidence", thread_key="evidence_thread")


class UrchinCog(commands.GroupCog, name="urchin", description="Urchin cheater tags"):
    def __init__(self, bot):
        self.bot = bot
        super().__init__()
        try:
            seconds = max(30, int(_env("URCHIN_INTERVAL_SECONDS") or 60))
        except ValueError:
            seconds = 60
        self.watch.change_interval(seconds=seconds)      # a new tag is posted within about a minute
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
        png = await urchin_card(self.core, uuid, name, tags)
        await interaction.followup.send(file=discord.File(io.BytesIO(png), filename="urchin.png"), view=view)


    @app_commands.command(name="scan", description="Post now every guild member that is tagged on Urchin")
    @app_commands.check(perm_check)
    async def scan(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            found = await self.scan_guild(post=True, only_new=False, force=True)
        except UrchinError as e:
            await interaction.followup.send(str(e), ephemeral=True)
            return
        await interaction.followup.send(f"Scan finished: {found} tagged member(s) posted.", ephemeral=True)

    async def scan_guild(self, *, post, only_new, force=False):
        members = await self.core.guild_members(force=force)
        uuids = [u.replace("-", "").lower() for u in members]
        tagged = await urchin_batch(uuids)                       # one request every 100 members
        found = 0
        for uuid in uuids:
            tags = tagged.get(uuid, [])
            sig = tags_signature(tags) if tags else ""
            seen = await q_one("SELECT sig FROM urchin_seen WHERE uuid = ?", (uuid,))
            changed = (seen is None and tags) or (seen is not None and seen["sig"] != sig and tags)
            await q_run("INSERT INTO urchin_seen (uuid, sig, seen_at) VALUES (?, ?, ?) "
                        "ON CONFLICT(uuid) DO UPDATE SET sig = excluded.sig, seen_at = excluded.seen_at",
                        (uuid, sig, iso(now_utc())))
            if tags and (changed or not only_new) and post:
                name = await self.core.name_for(uuid)
                if await post_urchin_alert(self.core, uuid, name, tags):
                    found += 1
        return found

    @tasks.loop(seconds=60)
    async def watch(self):
        if not _env("URCHIN_API_KEY") or bl.chan_id("EVIDENCE_CHANNEL_ID") is None:
            return
        try:
            baseline = await kv_get("urchin_baseline")
            # the very first pass only remembers who is already tagged (no flood of old tags)
            await self.scan_guild(post=bool(baseline) and await alerts.enabled("urchin"), only_new=True)
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
        for table, col in (("cheat_reports", "confirmed_at"), ("cheat_reports", "confirmed_by"),
                           ("cheat_reports", "closed_by"), ("ev_votes", "name")):
            try:
                await db.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")
            except aiosqlite.OperationalError:
                pass                                   # the column already exists
        await db.commit()
    bot.add_dynamic_items(EvButton, UrchinReportButton)
    await bot.add_cog(BlacklistCog(bot))
    await bot.add_cog(EvidenceCog(bot))
    await bot.add_cog(UrchinCog(bot))

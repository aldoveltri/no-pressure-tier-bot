"""Community features: forum applications cross-post, guild bridge channel topic, guild party announcements."""
import asyncio
import html as htmllib
import io
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiohttp
import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands, tasks

import breaklist as bl
import gexpcalc as gc
from breaklist import q_one, q_all, q_run, kv_get, kv_set, esc, now_utc, iso, from_iso, staff_check, lookup, n
from config import tier_from_elo
from database import get_player, get_stats

log = logging.getLogger("community")
NO_MENTIONS = discord.AllowedMentions.none()
UTC = timezone.utc

SCHEMA = """
CREATE TABLE IF NOT EXISTS parties (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT, host TEXT, host_uuid TEXT, description TEXT, start_at TEXT NOT NULL,
    image BLOB, image_name TEXT, channel_id TEXT, message_id TEXT,
    status TEXT NOT NULL DEFAULT 'active', created_by TEXT
);
CREATE TABLE IF NOT EXISTS party_votes (
    party_id INTEGER NOT NULL, discord_id TEXT NOT NULL, name TEXT, uuid TEXT, vote TEXT NOT NULL,
    PRIMARY KEY (party_id, discord_id)
);
"""

VOTES = {"y": ("Showing Up", "✅"), "m": ("Maybe Showing Up", "❔"), "n": ("Not Showing Up", "❌")}


# ======================================================================================
# guild party
# ======================================================================================

def header_line(start: datetime) -> str:
    t = start.astimezone(gc.TZ)
    hour = t.strftime("%I").lstrip("0") or "12"
    clock = f"{hour} {t.strftime('%p')}" if t.minute == 0 else f"{hour}:{t.strftime('%M %p')}"
    return f"{t.strftime('%A, %B')} {t.day} @ {clock} {gc.TZ_NAME}"


async def voter_identity(discord_id, fallback):
    """(display name, uuid or None): the Minecraft name when the Discord account is linked."""
    try:
        row = await q_one("SELECT uuid, ign FROM links WHERE discord_id = ?", (str(discord_id),))
    except Exception:
        row = None
    if row:
        return row["ign"], row["uuid"]
    return fallback, None


async def tier_tag(uuid):
    if not uuid:
        return ""
    try:
        p = await get_player(uuid)
    except Exception:
        p = None
    return f" ({tier_from_elo(p['elo'])})" if p else ""


async def party_embed(party):
    start = from_iso(party["start_at"])
    embed = discord.Embed(color=0x9B7BE8)
    embed.set_author(name=party["title"] or "Guild Party")
    embed.title = header_line(start)
    ts = int(start.timestamp())
    text = (party["description"] or "").strip()
    if party["host"]:
        text = text or f"Join as {party['host']} hosts the guild party."
    text += f"\n\nStarts <t:{ts}:F> (<t:{ts}:R>)"
    votes = await q_all("SELECT * FROM party_votes WHERE party_id = ? ORDER BY rowid", (party["id"],))
    for key in ("y", "m", "n"):
        label = VOTES[key][0]
        names = []
        for v in votes:
            if v["vote"] == key:
                names.append(f"{esc(v['name'])}{await tier_tag(v['uuid'])}")
        text += f"\n\n__{label}:__\n" + (", ".join(names) if names else "-")
    embed.description = text[:4000]
    if party["host_uuid"]:
        embed.set_thumbnail(url=f"https://mc-heads.net/head/{party['host_uuid']}")
    if party["image"]:
        embed.set_image(url=f"attachment://{party['image_name']}")
    embed.set_footer(text=f"Party #{party['id']} · the tier is the 1v1 tier")
    return embed


def party_view(pid):
    view = discord.ui.View(timeout=None)
    view.add_item(PartyButton("y", pid))
    view.add_item(PartyButton("m", pid))
    view.add_item(PartyButton("n", pid))
    view.add_item(PartyButton("st", pid))
    view.add_item(PartyButton("ad", pid))
    return view


class AddGuestModal(discord.ui.Modal, title="Add a player to the party"):
    def __init__(self, pid):
        super().__init__()
        self.pid = pid
        self.ign = discord.ui.TextInput(max_length=16, placeholder="Minecraft username (not in the guild is fine)")
        self.answer = discord.ui.TextInput(max_length=1, default="y", placeholder="y = showing up, m = maybe, n = not")
        self.add_item(discord.ui.Label(text="Minecraft username", component=self.ign))
        self.add_item(discord.ui.Label(text="Answer: y / m / n", component=self.answer))

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        ans = self.answer.value.strip().lower()[:1] or "y"
        if ans not in ("y", "m", "n"):
            await interaction.followup.send("The answer has to be y, m or n.", ephemeral=True)
            return
        try:
            res = await bl.get_uuid(self.ign.value.strip())
        except Exception:
            res = None
        if not res:
            await interaction.followup.send("That is not a valid Minecraft username.", ephemeral=True)
            return
        uuid, name = res
        await q_run("INSERT INTO party_votes (party_id, discord_id, name, uuid, vote) VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(party_id, discord_id) DO UPDATE SET name = excluded.name, uuid = excluded.uuid, "
                    "vote = excluded.vote", (self.pid, f"guest:{uuid}", name, uuid, ans))
        party = await q_one("SELECT * FROM parties WHERE id = ?", (self.pid,))
        try:
            await interaction.message.edit(embed=await party_embed(party), view=party_view(self.pid))
        except Exception:
            pass
        await interaction.followup.send(f"✅ **{esc(name)}** was added to the party.", ephemeral=True)


class PartyButton(discord.ui.DynamicItem[discord.ui.Button], template=r"pt:(?P<act>y|m|n|st|ad):(?P<pid>\d+)"):
    def __init__(self, act, pid):
        labels = {"y": ("Showing up", discord.ButtonStyle.success), "m": ("Maybe", discord.ButtonStyle.secondary),
                  "n": ("Not showing up", discord.ButtonStyle.danger), "st": ("Attendee stats (staff)", discord.ButtonStyle.primary),
                  "ad": ("Add a player (staff)", discord.ButtonStyle.secondary)}
        label, style = labels[act]
        super().__init__(discord.ui.Button(label=label, style=style, custom_id=f"pt:{act}:{pid}"))
        self.act, self.pid = act, int(pid)

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["act"], int(match["pid"]))

    async def callback(self, interaction: discord.Interaction):
        party = await q_one("SELECT * FROM parties WHERE id = ?", (self.pid,))
        if party is None or party["status"] != "active":
            await interaction.response.send_message("This party is over or was cancelled.", ephemeral=True)
            return
        if self.act == "st":
            if not bl.is_guild_staff(interaction.user):
                await interaction.response.send_message("Only guild staff can see this.", ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True)
            core = interaction.client.np_core
            embeds = await attendee_stats(core, party)
            await interaction.followup.send(embeds=embeds[:4], ephemeral=True)
            return
        if self.act == "ad":
            if not bl.is_guild_staff(interaction.user):
                await interaction.response.send_message("Only guild staff can add players.", ephemeral=True)
                return
            await interaction.response.send_modal(AddGuestModal(self.pid))
            return
        name, uuid = await voter_identity(interaction.user.id, interaction.user.display_name)
        row = await q_one("SELECT vote FROM party_votes WHERE party_id = ? AND discord_id = ?",
                          (self.pid, str(interaction.user.id)))
        if row and row["vote"] == self.act:                  # same button again: take the answer back
            await q_run("DELETE FROM party_votes WHERE party_id = ? AND discord_id = ?", (self.pid, str(interaction.user.id)))
        else:
            await q_run("INSERT INTO party_votes (party_id, discord_id, name, uuid, vote) VALUES (?, ?, ?, ?, ?) "
                        "ON CONFLICT(party_id, discord_id) DO UPDATE SET name = excluded.name, uuid = excluded.uuid, "
                        "vote = excluded.vote", (self.pid, str(interaction.user.id), name, uuid, self.act))
        await interaction.response.edit_message(embed=await party_embed(party), view=party_view(self.pid))


async def attendee_stats(core, party):
    """One embed per answer (showing / maybe / not): tier, 1v1 record, GEXP, days in guild, last login."""
    votes = await q_all("SELECT * FROM party_votes WHERE party_id = ? ORDER BY rowid", (party["id"],))
    now = now_utc()
    embeds = []
    for key in ("y", "m", "n"):
        lines = []
        for v in [x for x in votes if x["vote"] == key]:
            if not v["uuid"]:
                lines.append(f"**{esc(v['name'])}** · Discord account not linked: no stats")
                continue
            p = await get_player(v["uuid"])
            wins, losses = await get_stats(v["uuid"])
            gexp, cnt = await core.window_sum(v["uuid"], now - timedelta(days=6), now)
            days, _ = await core.days_info(v["uuid"])
            last = await q_one("SELECT login_at FROM logins WHERE uuid = ? ORDER BY login_at DESC LIMIT 1", (v["uuid"],))
            tier = f"{tier_from_elo(p['elo'])} ({p['elo']})" if p else "no tier"
            lines.append(f"**{esc(v['name'])}** · {tier} · 1v1 {wins}W-{losses}L · GEXP 7d {n(gexp)} · "
                         f"{int(days)} days in guild" + (f" · last login {gc.fmt_date(from_iso(last['login_at']))}" if last else ""))
        e = discord.Embed(title=f"{VOTES[key][1]} {VOTES[key][0]} ({len(lines)})", color=0x9B7BE8)
        e.description = ("\n".join(lines) or "-")[:4000]
        embeds.append(e)
    return embeds


async def send_party(bot, party, ping=None):
    """Posts the party message (a new one) in its channel and remembers it. Returns the message or None."""
    cid = bl.chan_id("PARTY_CHANNEL_ID") or (int(party["channel_id"]) if party["channel_id"] else None)
    if cid is None:
        return None
    ch = bot.get_channel(cid) or await bot.fetch_channel(cid)
    kwargs = {"embed": await party_embed(party), "view": party_view(party["id"]),
              "allowed_mentions": discord.AllowedMentions(roles=True)}
    if ping:
        kwargs["content"] = ping
    if party["image"]:
        kwargs["file"] = discord.File(io.BytesIO(party["image"]), filename=party["image_name"])
    msg = await ch.send(**kwargs)
    if party["message_id"] and party["channel_id"]:             # the old message loses its buttons
        try:
            old = await (bot.get_channel(int(party["channel_id"])) or await bot.fetch_channel(int(party["channel_id"]))
                         ).fetch_message(int(party["message_id"]))
            await old.edit(view=None)
        except discord.HTTPException:
            pass
    await q_run("UPDATE parties SET channel_id = ?, message_id = ? WHERE id = ?", (str(ch.id), str(msg.id), party["id"]))
    return msg


class PartyCog(commands.GroupCog, name="party", description="Guild party announcements"):
    def __init__(self, bot):
        self.bot = bot
        super().__init__()
        self.loop.start()

    def cog_unload(self):
        self.loop.cancel()

    @app_commands.command(name="create", description="Announce a guild party with Showing up / Maybe / Not buttons (guild staff)")
    @app_commands.describe(when="Start (ET or MT, see /timezone): 2026/08/29 7:00 PM", description="What the party is about",
                           host="Minecraft username of the host (optional)", title="Default: Guild Party",
                           image="Picture shown under the text (optional)")
    @app_commands.check(staff_check)
    async def create(self, interaction: discord.Interaction, when: str, description: str, host: Optional[str] = None,
                     title: Optional[str] = None, image: Optional[discord.Attachment] = None):
        await interaction.response.defer(ephemeral=True)
        start = gc.parse_et(when)
        if start is None or not re.search(r"\d:\d|am|pm", when, re.I):
            await interaction.followup.send("I couldn't read the start. Use the date and the time, for example "
                                            "`2026/08/29 7:00 PM` (ET or MT, see /timezone).", ephemeral=True)
            return
        if start.astimezone(UTC) <= now_utc():
            await interaction.followup.send("That time is in the past.", ephemeral=True)
            return
        host_uuid = host_name = None
        if host:
            r = await lookup(interaction, host)
            if r is None:
                return
            host_uuid, host_name = r
        data = name = None
        if image is not None:
            data = await image.read()
            name = "party" + (os.path.splitext(image.filename or "")[1] or ".png")
        pid = await q_run(
            "INSERT INTO parties (title, host, host_uuid, description, start_at, image, image_name, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (title or "Guild Party", host_name, host_uuid, description, iso(start), data, name, str(interaction.user.id)))
        party = await q_one("SELECT * FROM parties WHERE id = ?", (pid,))
        ping = bl._env("PARTY_PING_ROLE_ID")
        msg = await send_party(self.bot, party, ping=f"<@&{ping}>" if ping.isdigit() else None)
        if msg is None:
            await interaction.followup.send("PARTY_CHANNEL_ID is not set (or I can't post there): the party is saved, "
                                            "use `/party resend` after fixing it.", ephemeral=True)
            return
        await interaction.followup.send(f"Party #{pid} posted in {msg.channel.mention}. The bot reposts it by itself "
                                        f"before the start (default 24 and 1 hours).", ephemeral=True)

    async def _pick(self, pid):
        if pid is not None:
            return await q_one("SELECT * FROM parties WHERE id = ?", (pid,))
        return await q_one("SELECT * FROM parties WHERE status = 'active' ORDER BY start_at LIMIT 1")

    @app_commands.command(name="resend", description="Post the party message again with the current lists (guild staff)")
    @app_commands.describe(party_id="Default: the next party")
    @app_commands.check(staff_check)
    async def resend(self, interaction: discord.Interaction, party_id: Optional[int] = None):
        await interaction.response.defer(ephemeral=True)
        party = await self._pick(party_id)
        if party is None or party["status"] != "active":
            await interaction.followup.send("There is no active party.", ephemeral=True)
            return
        msg = await send_party(self.bot, party)
        await interaction.followup.send(f"Posted in {msg.channel.mention}." if msg else "I couldn't post it (PARTY_CHANNEL_ID).",
                                        ephemeral=True)

    @app_commands.command(name="stats", description="Tier, 1v1 record, GEXP and last login of everyone who answered (guild staff)")
    @app_commands.describe(party_id="Default: the next party")
    @app_commands.check(staff_check)
    async def stats(self, interaction: discord.Interaction, party_id: Optional[int] = None):
        await interaction.response.defer(ephemeral=True)
        party = await self._pick(party_id)
        if party is None:
            await interaction.followup.send("There is no party.", ephemeral=True)
            return
        await interaction.followup.send(embeds=(await attendee_stats(self.bot.np_core, party))[:4], ephemeral=True)

    @app_commands.command(name="cancel", description="Cancel a party: its buttons stop working (guild staff)")
    @app_commands.describe(party_id="Default: the next party")
    @app_commands.check(staff_check)
    async def cancel(self, interaction: discord.Interaction, party_id: Optional[int] = None):
        await interaction.response.defer(ephemeral=True)
        party = await self._pick(party_id)
        if party is None or party["status"] != "active":
            await interaction.followup.send("There is no active party.", ephemeral=True)
            return
        await q_run("UPDATE parties SET status = 'cancelled' WHERE id = ?", (party["id"],))
        await self._close_message(party, "Party cancelled")
        await interaction.followup.send(f"Party #{party['id']} cancelled.", ephemeral=True)

    async def _close_message(self, party, note):
        if not (party["channel_id"] and party["message_id"]):
            return
        try:
            ch = self.bot.get_channel(int(party["channel_id"])) or await self.bot.fetch_channel(int(party["channel_id"]))
            msg = await ch.fetch_message(int(party["message_id"]))
            embed = msg.embeds[0]
            embed.set_footer(text=f"Party #{party['id']} · {note}")
            await msg.edit(embed=embed, view=None)
        except (discord.HTTPException, IndexError):
            pass

    @tasks.loop(minutes=2)
    async def loop(self):
        try:
            hours = [float(x) for x in bl._env("PARTY_REMINDER_HOURS", "24,1").split(",") if x.strip()]
            now = now_utc()
            for party in await q_all("SELECT * FROM parties WHERE status = 'active'"):
                start = from_iso(party["start_at"])
                if now > start + timedelta(hours=3):
                    await q_run("UPDATE parties SET status = 'done' WHERE id = ?", (party["id"],))
                    await self._close_message(party, "Party over")
                    continue
                for h in sorted(hours, reverse=True):
                    key = f"party:{party['id']}:{h:g}"
                    due = start - timedelta(hours=h)
                    # only when the moment passed less than 30 minutes ago (a party created later skips old reminders)
                    if due <= now < min(start, due + timedelta(minutes=30)) and not await kv_get(key):
                        await kv_set(key, iso(now))
                        ping = bl._env("PARTY_PING_ROLE_ID")
                        await send_party(self.bot, party, ping=f"<@&{ping}>" if ping.isdigit() else None)
                        break
        except Exception:
            log.exception("The party reminders failed")

    @loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()


# ======================================================================================
# applications cross-post and bridge channel topic
# ======================================================================================

class CommunityEvents(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.topic_loop.start()
        self._topic_warned = False

    def cog_unload(self):
        self.topic_loop.cancel()

    @commands.Cog.listener()
    async def on_thread_create(self, thread: discord.Thread):
        forum = bl.chan_id("APPLICATIONS_FORUM_ID")
        target = bl.chan_id("APPLICATIONS_REVIEW_CHANNEL_ID")
        if forum is None or target is None or thread.parent_id != forum:
            return
        await asyncio.sleep(2)                               # let Discord create the first message
        text = ""
        try:
            first = await thread.fetch_message(thread.id)
            text = first.content or ""
        except discord.HTTPException:
            pass
        owner = f"<@{thread.owner_id}>" if thread.owner_id else "?"
        embed = discord.Embed(title=thread.name[:250], url=thread.jump_url, color=0x00C8E6)
        embed.description = (text[:1800] if text else "Open the post to read the application.")
        embed.add_field(name="Applicant", value=owner, inline=True)
        tags = ", ".join(t.name for t in getattr(thread, "applied_tags", []))
        if tags:
            embed.add_field(name="Tags", value=tags, inline=True)
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="Open application", url=thread.jump_url))
        try:
            ch = self.bot.get_channel(target) or await self.bot.fetch_channel(target)
            role = bl._env("APPLICATIONS_PING_ROLE_ID")
            await ch.send(content=f"<@&{role}> New guild application" if role.isdigit() else "New guild application",
                          embed=embed, view=view, allowed_mentions=discord.AllowedMentions(roles=True))
        except discord.HTTPException as e:
            log.warning("Could not cross-post the application: %s", e)

    @tasks.loop(minutes=10)
    async def topic_loop(self):
        cid = bl.chan_id("GUILD_BRIDGE_CHANNEL_ID")
        if cid is None:
            return
        try:
            core = self.bot.np_core
            members = await core.guild_members()
            if not members:
                return
            online = (await q_one("SELECT COUNT(*) AS c FROM guild_seen WHERE COALESCE(in_guild, 1) = 1 "
                                  "AND COALESCE(online, 0) = 1"))["c"]
            prefix = bl._env("BRIDGE_TOPIC_PREFIX")
            topic = (f"{prefix + ' | ' if prefix else ''}Guild members: {len(members)} | Online now: {online}")[:1000]
            ch = self.bot.get_channel(cid) or await self.bot.fetch_channel(cid)
            if ch.topic != topic:
                await ch.edit(topic=topic, reason="Guild member count")
        except discord.Forbidden:
            if not self._topic_warned:
                log.warning("I can't change the topic of the guild bridge channel: give me Manage Channels there")
                self._topic_warned = True
        except Exception:
            log.exception("The channel topic update failed")

    @topic_loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(120)


# ======================================================================================
# applications on the Hypixel forums thread (hypixel.net)
# ======================================================================================

HX_HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/124.0 Safari/537.36", "Accept-Language": "en-US,en;q=0.9"}


def parse_thread_page(page_html: str):
    """Posts of a XenForo thread page: [{'id', 'author', 'text'}]."""
    posts = []
    for chunk in re.split(r'(?=<article[^>]*\bjs-post\b)', page_html):
        m = re.match(r'<article[^>]*', chunk)
        if not m:
            continue
        head = m.group(0)
        pid = re.search(r'js-post-(\d+)', head) or re.search(r'id="post-(\d+)"', head)
        author = re.search(r'data-author="([^"]*)"', head)
        if not pid or not author:
            continue
        end = chunk.find("</article>")
        body = chunk[: end if end > 0 else len(chunk)]
        b = body.find("bbWrapper")
        text = body[b:] if b >= 0 else ""
        text = text.split("js-selectToQuoteEnd")[0]
        text = re.sub(r"<[^>]*$", "", text)
        text = re.sub(r"<blockquote.*?</blockquote>", "", text, flags=re.S)
        text = re.sub(r"<br\s*/?>|</p>|</div>", "\n", text)
        text = re.sub(r"<img[^>]*alt=\"([^\"]*)\"[^>]*>", r"\1", text)
        text = htmllib.unescape(re.sub(r"<[^>]+>", "", text))
        text = re.sub(r'^bbWrapper">', "", text).strip()
        text = re.sub(r"\n{3,}", "\n\n", text)
        posts.append({"id": int(pid.group(1)), "author": htmllib.unescape(author.group(1)), "text": text})
    return posts


async def fetch_thread_posts(url: str, page: Optional[int] = None):
    """(status, posts, last_page). page=None: the last page."""
    base = re.sub(r"/page-\d+/?$", "", url.rstrip("/"))
    target = f"{base}/page-{page}" if page else f"{base}/page-99999"
    timeout = aiohttp.ClientTimeout(total=25)
    async with aiohttp.ClientSession(timeout=timeout, headers=HX_HEADERS) as session:
        async with session.get(target, allow_redirects=True) as r:
            status = r.status
            body = await r.text()
            final = str(r.url)
    if status != 200:
        return status, [], None
    m = re.search(r"/page-(\d+)", final)
    return status, parse_thread_page(body), int(m.group(1)) if m else 1


class ApplicationsCog(commands.GroupCog, name="applications", description="Guild applications on the Hypixel forums"):
    def __init__(self, bot):
        self.bot = bot
        super().__init__()
        self.watch.start()

    def cog_unload(self):
        self.watch.cancel()

    def url(self):
        return bl._env("HYPIXEL_THREAD_URL")

    async def new_posts(self):
        """Posts newer than the last one seen (None the first time: it only remembers where we are)."""
        status, posts, page = await fetch_thread_posts(self.url())
        if status != 200:
            raise RuntimeError(f"the forum answered HTTP {status}")
        if not posts:
            raise RuntimeError("I could not read any post on the page (the site may have changed or blocked the bot)")
        last = await kv_get("hx_last_post")
        if page and page > 1 and last and posts[0]["id"] > int(last):
            _, before, _ = await fetch_thread_posts(self.url(), page - 1)
            posts = before + posts
        newest = max(p["id"] for p in posts)
        if last is None:
            await kv_set("hx_last_post", newest)
            return None
        fresh = [p for p in posts if p["id"] > int(last)]
        if fresh:
            await kv_set("hx_last_post", newest)
        return fresh

    async def post_application(self, p):
        target = bl.chan_id("APPLICATIONS_REVIEW_CHANNEL_ID")
        if target is None:
            return
        link = f"{self.url().rstrip('/')}/post-{p['id']}".replace("/page-", "/page-")
        link = re.sub(r"/page-\d+/", "/", link)
        embed = discord.Embed(title=f"Reply by {p['author']}", url=link, color=0xF5A623)
        embed.description = (p["text"][:1800] or "(no text: probably images only)")
        embed.set_footer(text="From the guild thread on the Hypixel forums")
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="Open on the forums", url=link))
        ch = self.bot.get_channel(target) or await self.bot.fetch_channel(target)
        role = bl._env("APPLICATIONS_PING_ROLE_ID")
        await ch.send(content=f"<@&{role}> New reply in the guild thread" if role.isdigit() else "New reply in the guild thread",
                      embed=embed, view=view, allowed_mentions=discord.AllowedMentions(roles=True))

    @tasks.loop(minutes=5)
    async def watch(self):
        if not self.url() or bl.chan_id("APPLICATIONS_REVIEW_CHANNEL_ID") is None:
            return
        try:
            fresh = await self.new_posts()
            ignore = {x.strip().lower() for x in bl._env("HYPIXEL_THREAD_IGNORE_AUTHORS", "zCqmbo").split(",") if x.strip()}
            for p in fresh or []:
                if p["author"].lower() not in ignore:
                    await self.post_application(p)
        except Exception as e:
            if not getattr(self, "_warned", False):
                log.warning("The forum thread check failed: %s", e)
                self._warned = True
            return
        self._warned = False

    @watch.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(60)

async def setup(bot: commands.Bot):
    async with aiosqlite.connect(bl.DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()
    bot.add_dynamic_items(PartyButton)
    await bot.add_cog(PartyCog(bot))
    await bot.add_cog(CommunityEvents(bot))
    await bot.add_cog(ApplicationsCog(bot))

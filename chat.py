"""Guild chat history: /chat shows what a player wrote in the guild bridge, with page buttons and a search.
Needs BRIDGE_CHANNEL_ID (the channel where the bridge posts) and the Message Content intent: switch it on in the
Discord developer portal (Bot > Privileged Gateway Intents) and set MESSAGE_CONTENT_INTENT=1 in the host variables.
Only messages written after the feature is on are saved."""
import logging
import re
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import breaklist as bl
from breaklist import q_all, q_one, q_run, esc, staff_check, iso, now_utc, from_iso

log = logging.getLogger("chat")
SCHEMA = ("CREATE TABLE IF NOT EXISTS bridge_chat (id TEXT PRIMARY KEY, ign TEXT, text TEXT NOT NULL, at TEXT NOT NULL);"
          "CREATE INDEX IF NOT EXISTS bridge_chat_ign ON bridge_chat (ign COLLATE NOCASE, at);")
LINE = re.compile(r"^(?:Guild\s*>\s*)?(?:\[[^\]]{1,20}\]\s*)*([A-Za-z0-9_]{3,16})(?:\s*\[[^\]]{1,20}\])*\s*[:»>]\s*(.+)$", re.S)
PER = 12


def parse(message):
    """(ign, text) of a bridge message: plain text 'Name: text' or an embed with the name as author."""
    text = (message.content or "").strip()
    if not text and message.embeds:
        e = message.embeds[0]
        if e.author and e.author.name and (e.description or "").strip():
            return e.author.name.strip().split()[-1], e.description.strip()
        text = (e.description or e.title or "").strip()
    text = re.sub(r"\*\*|__|\\(?=[_*])", "", text)
    m = LINE.match(text)
    if m:
        return m.group(1), m.group(2).strip()
    return None, text


async def fetch(ign, query, page):
    cond, par = [], []
    if ign:
        cond.append("ign = ? COLLATE NOCASE")
        par.append(ign)
    if query:
        cond.append("text LIKE ?")
        par.append(f"%{query}%")
    where = ("WHERE " + " AND ".join(cond)) if cond else ""
    total = (await q_one(f"SELECT COUNT(*) AS c FROM bridge_chat {where}", tuple(par)))["c"]
    pages = max(1, -(-total // PER))
    page = max(1, min(page, pages))
    rows = await q_all(f"SELECT * FROM bridge_chat {where} ORDER BY at DESC LIMIT ? OFFSET ?",
                       (*par, PER, (page - 1) * PER))
    return rows, page, pages, total


def make_embed(ign, query, rows, page, pages, total):
    title = "Guild chat" + (f" · {ign}" if ign else "") + (f" · “{query}”" if query else "")
    lines = []
    for r in rows:
        ts = int(from_iso(r["at"]).timestamp())
        who = f"**{esc(r['ign'])}** " if (r["ign"] and not ign) else ""
        lines.append(f"<t:{ts}:d> <t:{ts}:t> {who}{esc(r['text'])[:300]}")
    embed = discord.Embed(title=title, description="\n".join(lines) or "No messages found.", color=0x00C8E6)
    embed.set_footer(text=f"Page {page}/{pages} · {total} message{'s' if total != 1 else ''} · newest first")
    return embed


class SearchModal(discord.ui.Modal, title="Search the chat"):
    text = discord.ui.TextInput(label="Word or sentence (empty = no search)", required=False, max_length=60)

    def __init__(self, view):
        super().__init__()
        self.chat_view = view

    async def on_submit(self, interaction):
        v = self.chat_view
        v.query = str(self.text).strip() or None
        v.page = 1
        await v.update(interaction)


class ChatView(discord.ui.View):
    def __init__(self, owner_id, ign, query, page, pages):
        super().__init__(timeout=900)
        self.owner_id, self.ign, self.query, self.page, self.pages = owner_id, ign, query, page, pages
        prev = discord.ui.Button(label="◀ Previous", style=discord.ButtonStyle.primary, disabled=page <= 1)
        nxt = discord.ui.Button(label="Next ▶", style=discord.ButtonStyle.primary, disabled=page >= pages)
        find = discord.ui.Button(label="🔍 Search", style=discord.ButtonStyle.secondary)
        prev.callback, nxt.callback, find.callback = self.go(-1), self.go(1), self.search
        for b in (prev, nxt, find):
            self.add_item(b)

    async def interaction_check(self, interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Use /chat yourself to get your own buttons.", ephemeral=True)
            return False
        return True

    def go(self, delta):
        async def cb(interaction):
            self.page += delta
            await self.update(interaction)
        return cb

    async def search(self, interaction):
        await interaction.response.send_modal(SearchModal(self))

    async def update(self, interaction):
        await interaction.response.defer()
        rows, page, pages, total = await fetch(self.ign, self.query, self.page)
        await interaction.edit_original_response(
            embed=make_embed(self.ign, self.query, rows, page, pages, total),
            view=ChatView(self.owner_id, self.ign, self.query, page, pages))


class ChatCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message):
        cid = bl.chan_id("BRIDGE_CHANNEL_ID")
        if not cid or message.channel.id != cid:
            return
        try:
            ign, text = parse(message)
            if text:
                await q_run("INSERT OR IGNORE INTO bridge_chat (id, ign, text, at) VALUES (?, ?, ?, ?)",
                            (str(message.id), ign, text[:1000], iso(message.created_at)))
        except Exception:
            log.exception("Could not save a bridge message")

    @app_commands.command(name="chat", description="What a player wrote in the guild chat, with pages and search (guild staff)")
    @app_commands.describe(ign="Minecraft username (empty: everybody)", search="Only messages that contain this text")
    @app_commands.check(staff_check)
    async def chat(self, interaction: discord.Interaction, ign: Optional[str] = None, search: Optional[str] = None):
        await interaction.response.defer(ephemeral=True)
        if not bl.chan_id("BRIDGE_CHANNEL_ID"):
            await interaction.followup.send("The guild chat is not being saved: set BRIDGE_CHANNEL_ID (the bridge channel) "
                                            "and MESSAGE_CONTENT_INTENT=1 (and turn on the Message Content intent in the "
                                            "Discord developer portal).", ephemeral=True)
            return
        rows, page, pages, total = await fetch(ign, search, 1)
        await interaction.followup.send(embed=make_embed(ign, search, rows, page, pages, total),
                                        view=ChatView(interaction.user.id, ign, search, page, pages), ephemeral=True,
                                        allowed_mentions=discord.AllowedMentions.none())


async def setup(bot: commands.Bot):
    import aiosqlite
    async with aiosqlite.connect(bl.DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()
    await bot.add_cog(ChatCog(bot))

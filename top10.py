"""/topgexp: the top 10 GEXP of the week as copyable text (with promotions and flags) and as an image."""
import asyncio
import logging
from datetime import timedelta
from typing import Optional

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands
from PIL import Image, ImageDraw

import breaklist as bl
import gexpcalc as gc
import graphics as g
from breaklist import q_all, q_one, esc, now_utc, staff_check, n, send_file, load_flag, _name_flag
from database import DB_PATH

log = logging.getLogger("top10")
MEDALS = [(255, 215, 0), (200, 205, 215), (205, 127, 50)]


def flag_emoji(code):
    code = (code or "").strip().upper()
    if len(code) == 2 and code.isalpha():
        return "".join(chr(0x1F1E6 + ord(c) - 65) for c in code)
    return ""


def promotion_text(rank):
    recruit = (bl._env("RECRUIT_RANK_NAME") or "Recruit").strip()
    active = (bl._env("ACTIVE_RANK_NAME") or "Active").strip()
    rank = (rank or "").strip()
    if rank.lower() == recruit.lower():
        return f"promoted from {recruit} to {active}"
    return f"remains as {rank or active}"


async def locate(core, week, query):
    """(position, uuid, name, gexp) of the player that matches the typed name in the week's ranking, or None."""
    rows, _, _ = await ranking(core, week)
    q = (query or "").strip().lower()
    if not q:
        return None
    names = await asyncio.gather(*(core.name_for(r["uuid"]) for r in rows))
    best = None
    for i, (r, nm) in enumerate(zip(rows, names), 1):
        low = (nm or "").lower()
        if low == q:
            return i, r["uuid"], nm, r["total"]
        if best is None and q in low:
            best = (i, r["uuid"], nm, r["total"])
    return best


async def ranking(core, week):
    members = await core.guild_members(force=True)
    now = now_utc()
    ws = gc.week_start(now)
    if week == "this":
        first, last = ws, now
    else:
        first, last = ws - timedelta(days=7), ws - timedelta(seconds=1)
    a, b = first.astimezone(gc.ET).date().isoformat(), last.astimezone(gc.ET).date().isoformat()
    rows = await q_all("SELECT uuid, SUM(gexp) AS total FROM gexp_history WHERE day >= ? AND day <= ? "
                       "GROUP BY uuid ORDER BY total DESC", (a, b))
    return [r for r in rows if not members or r["uuid"] in members], a, b


async def top_rows(core, week, page=1, per=10):
    """[{uuid, ign, gexp, rank, code}] of the best members, and the (first, last) days of the week."""
    members = await core.guild_members()
    out, a, b = await ranking(core, week)
    total = len(out)
    pages = max(1, -(-total // per))
    page = max(1, min(page, pages))
    chunk = []
    for r in out[(page - 1) * per: page * per]:
        seen = await q_one("SELECT rank FROM guild_seen WHERE uuid = ?", (r["uuid"],))
        country = await q_one("SELECT code FROM countries WHERE uuid = ?", (r["uuid"],))
        live = (members.get(r["uuid"]) or {}).get("rank") if members else None      # the rank on Hypixel right now
        chunk.append({"uuid": r["uuid"], "ign": await core.name_for(r["uuid"]), "gexp": r["total"],
                      "rank": live or (seen or {}).get("rank") or "", "code": (country or {}).get("code")})
    return chunk, a, b, page, pages


def top_text(rows, start=1):
    lines = []
    for i, r in enumerate(rows, start):
        flag = flag_emoji(r["code"])
        what = f" ▶️ {promotion_text(r['rank'])}" if i <= 10 else ""          # only the top 10 are promoted
        lines.append(f"`#{i}` {esc(r['ign'])} {n(r['gexp'])}{what}" + (f" {flag}" if flag else ""))
    return "\n".join(lines)


def draw_top10(rows, heads, subtitle, start=1, mark=None):
    row_h = 84
    top = g.HEADER_H + 20
    height = top + len(rows) * row_h + g.FOOTER_H
    img = Image.new("RGB", (g.WIDTH, height), g.BG)
    d = ImageDraw.Draw(img)
    g._header(d, "TOP 10 GEXP", subtitle)
    for i, r in enumerate(rows):
        y0 = top + i * row_h
        cy = y0 + (row_h - 8) // 2
        color = MEDALS[start + i - 1] if start + i <= 3 else g.ACCENT
        d.rounded_rectangle([g.PAD, y0, g.WIDTH - g.PAD, y0 + row_h - 8], radius=14, fill=g.ROW_BG,
                            outline=g.ACCENT if mark and r["uuid"] == mark else None, width=3)
        d.rounded_rectangle([g.PAD, y0, g.PAD + 8, y0 + row_h - 8], radius=4, fill=color)
        d.text((g.PAD + 54, cy), f"#{start + i}", font=g.get_font(26 if start + i < 100 else 20, True), fill=color, anchor="mm")
        hx = g.PAD + 100
        g._paste_head(img, d, heads.get(r["uuid"]), hx, cy - g.HEAD // 2, g.HEAD)
        _name_flag(img, d, hx + g.HEAD + 14, cy - 11, r["ign"], g.get_font(26, True))
        d.text((hx + g.HEAD + 14, cy + 19), promotion_text(r["rank"]) if start + i <= 10 else (r["rank"] or ""),
               font=g.get_font(15), fill=g.MUTED, anchor="lm")
        d.text((g.WIDTH - g.PAD - 24, cy), n(r["gexp"]), font=g.get_font(28, True), fill=g.TEXT, anchor="rm")
    counts = {}
    for r in rows:
        counts[r["rank"] or "?"] = counts.get(r["rank"] or "?", 0) + 1
    g._footer(d, height, "Ranks on Hypixel: " + " · ".join(f"{k} {v}" for k, v in counts.items()))
    return g._png(img)


async def build_page(core, week, page, mark=None):
    """(text, png, page, pages) of one page of the top, or None when there is no data."""
    rows, a, b, page, pages = await top_rows(core, week, page)
    if not rows:
        return None
    for r in rows:
        await load_flag(r["uuid"], r["ign"])
    heads = await g._fetch_heads([(r["uuid"], r["ign"]) for r in rows])
    start = (page - 1) * 10 + 1
    sub = (f"{bl.GUILD_NAME_DISPLAY} · {a[5:].replace('-', '/')} to {b[5:].replace('-', '/')}"
           + (f" · page {page}/{pages}" if pages > 1 else ""))
    card = await asyncio.to_thread(draw_top10, rows, heads, sub, start, mark)
    return top_text(rows, start), card, page, pages


class SearchModal(discord.ui.Modal, title="Find a player in the top"):
    """Asks for a name and jumps to the page where that player is, with the row highlighted."""
    ign = discord.ui.TextInput(label="Minecraft username", max_length=16, required=True)

    def __init__(self, view):
        super().__init__()
        self.view = view

    async def on_submit(self, interaction):
        found = await locate(self.view.core, self.view.week, str(self.ign))
        if found is None:
            await interaction.response.send_message(f"I can't find **{esc(str(self.ign))}** in this top.", ephemeral=True)
            return
        pos, uuid, name, gexp = found
        self.view.page, self.view.mark = (pos - 1) // 10 + 1, uuid
        await interaction.response.defer()
        res = await build_page(self.view.core, self.view.week, self.view.page, uuid)
        text, card, page, pages = res
        await interaction.edit_original_response(
            content=f"🔍 **{esc(name)}** is **#{pos}** with **{n(gexp)}** GEXP.\n" + text, attachments=[send_file(card, "top10.png")],
            view=TopView(self.view.core, self.view.week, page, pages, uuid),
            allowed_mentions=discord.AllowedMentions.none())


class TopView(discord.ui.View):
    """Previous / Next buttons under /topgexp."""

    def __init__(self, core, week, page, pages, mark=None):
        super().__init__(timeout=900)
        self.core, self.week, self.page, self.pages, self.mark = core, week, page, pages, mark
        self.prev = discord.ui.Button(label="◀ Previous", style=discord.ButtonStyle.primary, disabled=page <= 1)
        self.nxt = discord.ui.Button(label="Next ▶", style=discord.ButtonStyle.primary, disabled=page >= pages)
        self.prev.callback, self.nxt.callback = self.on_prev, self.on_next
        self.find = discord.ui.Button(label="🔍 Search", style=discord.ButtonStyle.primary)
        self.find.callback = self.on_find
        self.add_item(self.prev)
        self.add_item(self.nxt)
        self.add_item(self.find)

    async def on_find(self, interaction):
        await interaction.response.send_modal(SearchModal(self))

    async def show(self, interaction):
        await interaction.response.defer()
        res = await build_page(self.core, self.week, self.page, self.mark)
        if res is None:
            await interaction.edit_original_response(content="Nothing to show.", attachments=[], view=None)
            return
        text, card, page, pages = res
        await interaction.edit_original_response(content=text, attachments=[send_file(card, "top10.png")],
                                                 view=TopView(self.core, self.week, page, pages, self.mark),
                                                 allowed_mentions=discord.AllowedMentions.none())

    async def on_prev(self, interaction):
        self.page -= 1
        await self.show(interaction)

    async def on_next(self, interaction):
        self.page += 1
        await self.show(interaction)


class TopGexp(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="topgexp", description="Top 10 GEXP of the week: text to copy and an image (guild staff)")
    @app_commands.describe(week="Which week (default: the last one, already purged)",
                           page="Start from this page (the buttons move between pages)")
    @app_commands.choices(week=[app_commands.Choice(name="Last week", value="last"),
                                app_commands.Choice(name="This week so far", value="this")])
    @app_commands.check(staff_check)
    async def topgexp(self, interaction: discord.Interaction, week: Optional[str] = "last",
                      page: app_commands.Range[int, 1, 20] = 1):
        await interaction.response.defer()
        core = self.bot.np_core
        res = await build_page(core, week or "last", page)
        if res is None:
            await interaction.followup.send("I have no saved GEXP for that week.")
            return
        text, card, page, pages = res
        await interaction.followup.send(content=text, file=send_file(card, "top10.png"),
                                        view=TopView(core, week or "last", page, pages),
                                        allowed_mentions=discord.AllowedMentions.none())


async def setup(bot: commands.Bot):
    await bot.add_cog(TopGexp(bot))

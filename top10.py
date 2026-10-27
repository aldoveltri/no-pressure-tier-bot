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


async def top_rows(core, week, limit=10):
    """[{uuid, ign, gexp, rank, code}] of the best members, and the (first, last) days of the week."""
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
    out = []
    for r in rows:
        if members and r["uuid"] not in members:
            continue
        seen = await q_one("SELECT rank FROM guild_seen WHERE uuid = ?", (r["uuid"],))
        country = await q_one("SELECT code FROM countries WHERE uuid = ?", (r["uuid"],))
        out.append({"uuid": r["uuid"], "ign": await core.name_for(r["uuid"]), "gexp": r["total"],
                    "rank": (seen or {}).get("rank") or (members.get(r["uuid"], {}).get("rank") if members else ""),
                    "code": (country or {}).get("code")})
        if len(out) >= limit:
            break
    return out, a, b


def top_text(rows):
    lines = []
    for i, r in enumerate(rows, 1):
        flag = flag_emoji(r["code"])
        lines.append(f"`#{i}` {esc(r['ign'])} {n(r['gexp'])} ▶️ {promotion_text(r['rank'])}" + (f" {flag}" if flag else ""))
    return "\n".join(lines)


def draw_top10(rows, heads, subtitle):
    row_h = 84
    top = g.HEADER_H + 20
    height = top + len(rows) * row_h + g.FOOTER_H
    img = Image.new("RGB", (g.WIDTH, height), g.BG)
    d = ImageDraw.Draw(img)
    g._header(d, "TOP 10 GEXP", subtitle)
    for i, r in enumerate(rows):
        y0 = top + i * row_h
        cy = y0 + (row_h - 8) // 2
        color = MEDALS[i] if i < 3 else g.ACCENT
        d.rounded_rectangle([g.PAD, y0, g.WIDTH - g.PAD, y0 + row_h - 8], radius=14, fill=g.ROW_BG)
        d.rounded_rectangle([g.PAD, y0, g.PAD + 8, y0 + row_h - 8], radius=4, fill=color)
        d.text((g.PAD + 54, cy), f"#{i + 1}", font=g.get_font(26, True), fill=color, anchor="mm")
        hx = g.PAD + 100
        g._paste_head(img, d, heads.get(r["uuid"]), hx, cy - g.HEAD // 2, g.HEAD)
        _name_flag(img, d, hx + g.HEAD + 14, cy - 11, r["ign"], g.get_font(26, True))
        d.text((hx + g.HEAD + 14, cy + 19), promotion_text(r["rank"]), font=g.get_font(15), fill=g.MUTED, anchor="lm")
        d.text((g.WIDTH - g.PAD - 24, cy), n(r["gexp"]), font=g.get_font(28, True), fill=g.TEXT, anchor="rm")
    g._footer(d, height, "GEXP of the week")
    return g._png(img)


class TopGexp(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="topgexp", description="Top 10 GEXP of the week: text to copy and an image (guild staff)")
    @app_commands.describe(week="Which week (default: the last one, already purged)")
    @app_commands.choices(week=[app_commands.Choice(name="Last week", value="last"),
                                app_commands.Choice(name="This week so far", value="this")])
    @app_commands.check(staff_check)
    async def topgexp(self, interaction: discord.Interaction, week: Optional[str] = "last"):
        await interaction.response.defer()
        core = self.bot.np_core
        rows, a, b = await top_rows(core, week or "last")
        if not rows:
            await interaction.followup.send("I have no saved GEXP for that week.")
            return
        for r in rows:
            await load_flag(r["uuid"], r["ign"])
        heads = await g._fetch_heads([(r["uuid"], r["ign"]) for r in rows])
        sub = f"{bl.GUILD_NAME_DISPLAY} · {a[5:].replace('-', '/')} to {b[5:].replace('-', '/')}"
        card = await asyncio.to_thread(draw_top10, rows, heads, sub)
        await interaction.followup.send(content=top_text(rows), file=send_file(card, "top10.png"),
                                        allowed_mentions=discord.AllowedMentions.none())


async def setup(bot: commands.Bot):
    await bot.add_cog(TopGexp(bot))

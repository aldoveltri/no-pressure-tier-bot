"""/profile and /info: interactive hubs. A summary card of everything the bot knows about a player (or about the guild)
and one button per section that opens the details: 1v1s, matches, guild and GEXP, breaklist, punishments, blacklist..."""
import asyncio
import io
import logging
from collections import Counter
from datetime import timedelta
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from PIL import Image, ImageDraw

import breaklist as bl
import gexpcalc as gc
import graphics as g
from breaklist import q_one, q_all, esc, n, now_utc, iso, from_iso, load_flag, _name_flag, send_file

log = logging.getLogger("hub")
NO_MENTIONS = discord.AllowedMentions.none()
GOLD = (241, 196, 15)


async def Q1(sql, *params):
    try:
        return await q_one(sql, params)
    except Exception:
        return None


async def QA(sql, *params):
    try:
        return await q_all(sql, params)
    except Exception:
        return []


async def safe(coro, default=None):
    try:
        return await coro
    except Exception:
        log.exception("A section of the hub failed")
        return default


class Out:
    """What a section shows: text above, an embed and/or an image, and how many pages it has."""

    def __init__(self, content=None, embed=None, png=None, name="hub.png", pages=1, page=1):
        self.content, self.embed, self.png, self.name, self.pages, self.page = content, embed, png, name, pages, page


# ======================================================================================
# drawing: one card for the player hub and for the guild hub
# ======================================================================================

def fit(d, text, font, width):
    text = str(text)
    if d.textlength(text, font=font) <= width:
        return text
    while text and d.textlength(text + "…", font=font) > width:
        text = text[:-1]
    return text + "…"


def fit_font(d, text, width, size, bold=True, least=11):
    """The biggest font (from `size` down to `least`) in which the text fits the width."""
    while size > least and d.textlength(str(text), font=g.get_font(size, bold)) > width:
        size -= 1
    return g.get_font(size, bold)


def wrap2(d, text, font, width):
    """Splits the text in two lines that fit the width (the second one is cut with ... when needed)."""
    words, l1 = text.split(" "), ""
    i = 0
    while i < len(words) and d.textlength((l1 + " " + words[i]).strip(), font=font) <= width:
        l1 = (l1 + " " + words[i]).strip()
        i += 1
    if not l1:                              # a single huge word
        return fit(d, text, font, width), ""
    return l1, fit(d, " ".join(words[i:]), font, width)


def draw_hub(title, subtitle, head, name, chips, panels, footer, tag=None, flag=True):
    """panels: [(label, big text, small text, color)] in two columns. chips: [(text, color)]."""
    gap, ph = 14, 112
    top = g.HEADER_H + 16
    id_h = 130
    rows = (len(panels) + 1) // 2
    height = top + id_h + gap + rows * (ph + gap) + g.FOOTER_H - 4
    img = Image.new("RGB", (g.WIDTH, height), g.BG)
    d = ImageDraw.Draw(img)
    g._header(d, title, subtitle)
    d.rounded_rectangle([g.PAD, top, g.WIDTH - g.PAD, top + id_h], radius=16, fill=g.ROW_BG)
    d.rounded_rectangle([g.PAD, top, g.PAD + 8, top + id_h], radius=4, fill=g.ACCENT)
    if tag is None:
        g._paste_head(img, d, head, g.PAD + 34, top + 15, 100, radius=12)
        nx = g.PAD + 34 + 100 + 24
    else:                                   # the guild: no picture, the tag becomes the first pill
        nx = g.PAD + 34
        chips = [(tag, g.ACCENT)] + list(chips)
    if tag is None and flag:
        _name_flag(img, d, nx, top + 42, name, g.get_font(38, True))
    else:
        d.text((nx, top + 42), fit(d, name, g.get_font(38, True), g.WIDTH - nx - g.PAD - 20),
               font=g.get_font(38, True), fill=g.TEXT, anchor="lm")
    cx, cy = nx, top + 82
    font = g.get_font(15, True)
    for text, color in chips:
        w = int(d.textlength(text, font=font)) + 26
        if cx + w > g.WIDTH - g.PAD - 16:
            cx, cy = nx, cy + 34
        d.rounded_rectangle([cx, cy, cx + w, cy + 28], radius=10, fill=color)
        d.text((cx + w // 2, cy + 14), text, font=font, fill=g.BG, anchor="mm")
        cx += w + 8
    pw = (g.WIDTH - 2 * g.PAD - gap) // 2
    y0 = top + id_h + gap
    for i, (label, big, small, color) in enumerate(panels):
        x = g.PAD + (i % 2) * (pw + gap)
        y = y0 + (i // 2) * (ph + gap)
        d.rounded_rectangle([x, y, x + pw, y + ph], radius=14, fill=g.ROW_BG)
        d.text((x + 22, y + 24), label, font=g.get_font(14, True), fill=g.MUTED, anchor="lm")
        w = pw - 44
        bf = fit_font(d, big, w, 26, True, 16)
        if d.textlength(str(big), font=bf) > w:                     # still too long: two small lines
            bf = g.get_font(15, True)
            l1, l2 = wrap2(d, str(big), bf, w)
            d.text((x + 22, y + 46), l1, font=bf, fill=color or g.TEXT, anchor="lm")
            d.text((x + 22, y + 66), l2, font=bf, fill=color or g.TEXT, anchor="lm")
        else:
            d.text((x + 22, y + 56), fit(d, big, bf, w), font=bf, fill=color or g.TEXT, anchor="lm")
        sf = fit_font(d, small, w, 16, False, 11)
        d.text((x + 22, y + 91), fit(d, small, sf, w), font=sf, fill=g.MUTED, anchor="lm")
    g._footer(d, height, footer)
    return g._png(img)


# ======================================================================================
# player data
# ======================================================================================

def day_text(iso_date):
    """'2026-11-15' -> '11/15/2026' (a calendar date, no time zone shift)."""
    from datetime import datetime
    return gc.fmt_date(datetime.fromisoformat(iso_date[:10]).replace(tzinfo=gc.ET))


def plural(count, word):
    return f"{count} {word}" + ("" if count == 1 else "s")


def tier_chip(tier):
    return (tier, g.ACCENT)


async def gather_player(bot, uuid, name):
    from database import get_player, get_stats, get_rank, count_forfeits
    core = bot.np_core
    d = {"uuid": uuid, "name": name}
    p = await safe(get_player(uuid))
    d["p"] = p
    if p:
        w, l = await safe(get_stats(uuid), (0, 0))
        d.update(wins=w, losses=l, rank=await safe(get_rank(p["elo"])),
                 forfeits=await safe(count_forfeits(uuid), 0))
    d["link"] = await Q1("SELECT discord_id FROM links WHERE uuid = ?", uuid)
    d["country"] = await Q1("SELECT code FROM countries WHERE uuid = ?", uuid)
    d["seen"] = await Q1("SELECT * FROM guild_seen WHERE uuid = ?", uuid)
    members = await safe(core.guild_members(), {}) or {}
    d["in_guild"] = uuid in members
    d["guild_rank"] = (members.get(uuid) or {}).get("rank") or (d["seen"] or {}).get("rank")
    d["days"] = await safe(core.days_info(uuid), (0.0, "unknown"))
    ws = gc.week_start(now_utc())
    d["week"] = await safe(core.window_sum(uuid, ws, now_utc()), (0, 0))
    d["bl"] = await QA("SELECT * FROM bl_entries WHERE uuid = ? ORDER BY (status = 'active') DESC, left_at DESC, id DESC", uuid)
    d["excuse_now"] = await Q1("SELECT week FROM excuses WHERE uuid = ? AND week >= ? LIMIT 1", uuid, ws.date().isoformat())
    d["excuses"] = (await Q1("SELECT COUNT(*) AS c FROM excuses WHERE uuid = ?", uuid) or {"c": 0})["c"]
    d["left"] = await QA("SELECT * FROM guild_left WHERE uuid = ? ORDER BY left_at DESC", uuid)
    d["debt"] = (await Q1("SELECT amount FROM debts WHERE uuid = ?", uuid) or {"amount": 0})["amount"]
    d["mutes"] = await QA("SELECT * FROM mutes WHERE uuid = ? ORDER BY id DESC", uuid)
    d["warns"] = await QA("SELECT * FROM warnings WHERE uuid = ? ORDER BY id DESC", uuid)
    d["blacklist"] = await QA("SELECT * FROM blacklist WHERE uuid = ? ORDER BY id DESC", uuid)
    d["pro"] = await Q1("SELECT * FROM pro_players WHERE uuid = ?", uuid)
    d["wait"] = await Q1("SELECT status FROM waitlist WHERE uuid = ?", uuid)
    d["login"] = await Q1("SELECT login_at FROM logins WHERE uuid = ? ORDER BY login_at DESC LIMIT 1", uuid)
    d["veteran_days"] = bl.VETERAN_DAYS
    return d


def player_panels(d):
    now = now_utc()
    panels, chips = [], []
    p = d["p"]
    # 1v1
    if p:
        total = d["wins"] + d["losses"]
        wr = f"{d['wins'] / total * 100:.0f}%" if total else "-"
        panels.append(("1V1S", f"{p['tier']} · {p['elo']} elo",
                       f"#{d['rank'] or '?'} · {d['wins']}W {d['losses']}L · {wr}" + (f" · {d['forfeits']} forfeits" if d.get("forfeits") else ""),
                       g.TEXT))
        chips.append(tier_chip(p["tier"]))
    else:
        panels.append(("1V1S", "No tier yet", "Has not played any 1v1", g.MUTED))
    # guild
    days, state = d["days"]
    if d["in_guild"]:
        joined = from_iso((d["seen"] or {}).get("joined_at"))
        panels.append(("GUILD", d["guild_rank"] or "Member",
                       f"{int(days)} / {d['veteran_days']} days" + (f" · joined {gc.fmt_date(joined)}" if joined else ""), g.TEXT))
        chips.append((d["guild_rank"] or "In the guild", (0, 200, 120)))
    elif state == "breaklist":
        panels.append(("GUILD", "On the breaklist", f"{int(days)} days saved · not in the guild now", GOLD))
    else:
        panels.append(("GUILD", "Not in the guild", f"{int(days)} days saved" if days else "No data", g.MUTED))
    # gexp
    total_w, count_w = d["week"]
    if d["in_guild"] or count_w:
        req = gc.WEEKLY_REQ
        pct = total_w * 100 // req if req else 0
        panels.append(("GEXP THIS WEEK", f"{n(total_w)} / {n(req)}",
                       f"{pct}% of the requirement" + (" · excused this week" if d["excuse_now"] else ""),
                       g.GREEN if total_w >= req else g.TEXT))
    else:
        panels.append(("GEXP THIS WEEK", "-", "No GEXP saved", g.MUTED))
    # breaklist
    active = next((e for e in d["bl"] if e["status"] == "active"), None)
    if d["bl"]:
        small = ""
        if active:
            small = f"On it since {gc.fmt_date(from_iso(active['left_at']))}"
            if active.get("expected_return"):
                small += f" · back {day_text(active['expected_return'])}"
        else:
            small = f"Last: {gc.fmt_date(from_iso(d['bl'][0]['left_at']))}"
        panels.append(("BREAKLIST", f"{len(d['bl'])} time{'s' if len(d['bl']) != 1 else ''}"
                       + (" · ON IT NOW" if active else ""), small, g.RED if active else g.TEXT))
        if active:
            chips.append(("ON BREAKLIST", g.RED))
    else:
        panels.append(("BREAKLIST", "Never breaklisted", "Clean", g.GREEN))
    # excuses and purge
    panels.append(("EXCUSES", f"{d['excuses']} week{'s' if d['excuses'] != 1 else ''}" if d["excuses"] else "Never excused",
                   "Excused now" if d["excuse_now"] else "Not excused now", g.TEXT if d["excuses"] else g.MUTED))
    left = d["left"]
    panels.append(("PURGE", f"Left {len(left)} time{'s' if len(left) != 1 else ''}" if left else "Never left",
                   f"Extra GEXP debt: {n(d['debt'])}" if d["debt"] else (f"Last: {gc.fmt_date(from_iso(left[0]['left_at']))}" if left else "No debt"),
                   g.RED if d["debt"] else (g.TEXT if left else g.GREEN)))
    # punishments
    mutes, warns = d["mutes"], d["warns"]
    active_m = sum(1 for m in mutes if m["status"] == "active")
    active_w = sum(1 for w in warns if not w.get("expires_at") or from_iso(w["expires_at"]) > now)
    if mutes or warns:
        panels.append(("PUNISHMENTS", f"{len(mutes)} mute{'s' if len(mutes) != 1 else ''} · {len(warns)} warn{'s' if len(warns) != 1 else ''}",
                       f"{active_m} mute active · {active_w} warn not expired", g.RED if active_m else g.TEXT))
        if active_m:
            chips.append(("MUTED", g.RED))
    else:
        panels.append(("PUNISHMENTS", "No mutes or warns", "Clean", g.GREEN))
    # blacklist
    bl_now = next((b for b in d["blacklist"] if b["status"] == "active"), None)
    if bl_now:
        panels.append(("BLACKLIST", "BLACKLISTED", (bl_now["reason"] or "").replace("\n", " ") or "No reason", g.RED))
        chips.append(("BLACKLISTED", g.RED))
    elif d["blacklist"]:
        panels.append(("BLACKLIST", "Was blacklisted", f"{len(d['blacklist'])} past entr{'ies' if len(d['blacklist']) != 1 else 'y'}", GOLD))
    else:
        panels.append(("BLACKLIST", "Not blacklisted", "Clean", g.GREEN))
    # activity and links
    login = from_iso((d["login"] or {}).get("login_at"))
    panels.append(("ACTIVITY", f"Last login {gc.fmt_date(login)}" if login else "No logins saved",
                   "Discord linked" if d["link"] else "Discord not linked", g.TEXT if login else g.MUTED))
    # extras
    extra = []
    if d["pro"]:
        extra.append("Pro" + (f" ({d['pro']['how']})" if d["pro"].get("how") else ""))
        chips.append(("PRO", GOLD))
    if d["wait"] and d["wait"]["status"] == "waiting":
        extra.append("Waiting list")
    if d["country"]:
        extra.append(d['country']['code'].upper())
    panels.append(("EXTRAS", " · ".join(extra) if extra else "Nothing special", "Pro list · waiting list · country",
                   g.TEXT if extra else g.MUTED))
    return panels, chips


# ======================================================================================
# player sections
# ======================================================================================

PLAYER_SECTIONS = [("summary", "Summary"), ("stats", "1v1 stats"), ("matches", "Matches"), ("guild", "Guild & GEXP"),
                   ("breaklist", "Breaklist"), ("excuses", "Excuses"), ("punish", "Punishments"),
                   ("blacklist", "Blacklist"), ("logins", "Logins")]


async def player_section(ctx, key, page):
    bot, uuid, name = ctx.bot, ctx.uuid, ctx.name
    core = bot.np_core
    hooks = bot.np_hooks
    if key == "summary":
        d = await gather_player(bot, uuid, name)
        panels, chips = player_panels(d)
        await load_flag(uuid, name)
        head = await core.head_for(uuid, name)
        png = await asyncio.to_thread(draw_hub, "PLAYER PROFILE", f"{bl.GUILD_NAME_DISPLAY} · everything about {name}",
                                      head, name, chips, panels, "Use the buttons for the details")
        return Out(png=png, name="profile.png")
    if key == "stats":
        spec = None
        if ctx.spec and ctx.spec != "-":
            k, _, v = ctx.spec.partition(":")
            spec = ({"m": "mode", "s": "season", "y": "year"}[k], int(v) if k in "sy" else v)
        png, err = await hooks["profile_card"](uuid, name, spec)
        return Out(content=err, png=png, name="stats.png")
    if key == "matches":
        res = await hooks["history_page"](uuid, name, page)
        if res is None:
            return Out(content=f"**{esc(name)}** hasn't played any 1v1 yet.")
        png, pages, total = res
        return Out(png=png, name="matches.png", pages=pages, page=min(page, pages))
    if key == "guild":
        d = await gather_player(bot, uuid, name)
        days, state = d["days"]
        sets = await QA("SELECT * FROM day_sets WHERE uuid = ? AND removed = 0 ORDER BY id DESC LIMIT 5", uuid)
        joined = from_iso((d["seen"] or {}).get("joined_at"))
        lines = [f"**Rank** {d['guild_rank'] or ('not in the guild' if not d['in_guild'] else '-')} · "
                 f"**Days in guild** {int(days)} / {bl.VETERAN_DAYS}" + (" ⭐ veteran" if days >= bl.VETERAN_DAYS else "")
                 + (f" · **Joined** {gc.fmt_date(joined)}" if joined else "")]
        if sets:
            lines.append("**Saved days:** " + " · ".join(f"{x['days']:+g} ({x['note'] or '-'})" for x in sets))
        png, err = await core.eligibility_card(uuid, name)
        if err:
            lines.append(err)
        return Out(content="\n".join(lines)[:1900], png=png, name="gexp.png")
    if key == "breaklist":
        d = await gather_player(bot, uuid, name)
        entries = d["bl"]
        lines = []
        if d["excuses"]:
            rows = await QA("SELECT week, reason FROM excuses WHERE uuid = ? ORDER BY week DESC LIMIT 4", uuid)
            lines.append("**Excused:** " + " · ".join(f"week of {day_text(r['week'])}"
                                                       + (f" ({r['reason']})" if r["reason"] else "") for r in rows))
        if d["left"]:
            lines.append("**Left the guild:** " + " · ".join(gc.fmt_date(from_iso(x["left_at"])) for x in d["left"][:4]))
        if d["debt"]:
            lines.append(f"**Extra GEXP debt:** {n(d['debt'])}")
        if not entries:
            lines.insert(0, f"**{esc(name)}** has never been on the breaklist.")
            return Out(content="\n".join(lines))
        per = 4
        pages = max(1, -(-len(entries) // per))
        page = max(1, min(page, pages))
        head = await core.head_for(uuid, name)
        await load_flag(uuid, name)
        png = await asyncio.to_thread(bl.draw_history, name, head, entries[(page - 1) * per: page * per],
                                      len(entries), page, pages)
        return Out(content="\n".join(lines) or None, png=png, name="breaklist.png", pages=pages, page=page)
    if key == "punish":
        embed = await bl.punishment_embed(uuid, name)
        return Out(content=None if embed else f"**{esc(name)}** has no mutes or warnings.", embed=embed)
    if key == "blacklist":
        rows = await QA("SELECT * FROM blacklist WHERE uuid = ? ORDER BY id DESC", uuid)
        if not rows:
            return Out(content=f"**{esc(name)}** has never been on the blacklist.")
        import evidence
        active = next((r for r in rows if r["status"] == "active"), None)
        embed = evidence.blacklist_embed(active or rows[0], "On the blacklist now" if active else "Blacklist (removed)")
        past = [r for r in rows if r is not active][:6]
        if past:
            embed.add_field(name="Earlier entries", inline=False, value="\n".join(
                f"#{r['id']} · {gc.fmt_date(from_iso(r['added_at']))} · {r['status']} · {(r['reason'] or '-')[:80]}" for r in past))
        return Out(content=evidence.video_text(active or rows[0]), embed=embed)
    if key == "excuses":
        rows = await QA("SELECT * FROM excuses WHERE uuid = ? ORDER BY week DESC", uuid)
        if not rows:
            return Out(content=f"**{esc(name)}** has no excuses saved by this bot.")
        embed = discord.Embed(title=f"Excuses of {name}", color=0x00C8E6,
                              description="\n".join(f"`{day_text(r['week'])}` week · {(r['reason'] or 'no reason')[:120]}"
                                                    for r in rows[:15]))
        embed.set_footer(text=f"{len(rows)} excuse{'s' if len(rows) != 1 else ''} saved by this bot")
        return Out(embed=embed)
    if key == "logins":
        embed, text = await bl.logins_embed(uuid, name, 14)
        return Out(content=text, embed=embed)
    return Out(content="Unknown section.")


# ======================================================================================
# guild data and sections
# ======================================================================================

async def gather_guild(bot):
    core = bot.np_core
    d = {}
    members = await safe(core.guild_members(), {}) or {}
    d["members"] = members
    d["guild"] = getattr(core, "_guild", None) or {}
    d["ranks"] = Counter((m.get("rank") or "?") for m in members.values())
    ws = gc.week_start(now_utc())
    first = ws.astimezone(gc.ET).date().isoformat()
    rows = await QA("SELECT uuid, SUM(gexp) AS t FROM gexp_history WHERE day >= ? GROUP BY uuid ORDER BY t DESC", first)
    rows = [r for r in rows if not members or r["uuid"] in members]
    d["gexp_total"] = sum(r["t"] for r in rows)
    d["gexp_top"] = rows[0] if rows else None
    d["gexp_top_name"] = await safe(core.name_for(rows[0]["uuid"])) if rows else None
    d["gexp_n"] = len(rows)
    d["bl_active"] = (await Q1("SELECT COUNT(*) AS c FROM bl_entries WHERE status = 'active'") or {"c": 0})["c"]
    d["bl_total"] = (await Q1("SELECT COUNT(*) AS c FROM bl_entries") or {"c": 0})["c"]
    d["visiting"] = (await Q1("SELECT COUNT(*) AS c FROM bl_entries WHERE status = 'active' AND visiting_guild IS NOT NULL") or {"c": 0})["c"]
    d["excused"] = (await Q1("SELECT COUNT(DISTINCT uuid) AS c FROM excuses WHERE week >= ?", ws.date().isoformat()) or {"c": 0})["c"]
    purge = await safe(core.purge_rows(cycle="this"), []) or []
    d["purge_left"] = len(purge)
    d["purge_suspects"] = sum(1 for e in purge if e.get("status") == "suspect")
    d["mutes"] = (await Q1("SELECT COUNT(*) AS c FROM mutes WHERE status = 'active'") or {"c": 0})["c"]
    d["warns"] = (await Q1("SELECT COUNT(*) AS c FROM warnings WHERE expires_at IS NULL OR expires_at > ?", iso(now_utc())) or {"c": 0})["c"]
    d["blacklist"] = (await Q1("SELECT COUNT(*) AS c FROM blacklist WHERE status = 'active'") or {"c": 0})["c"]
    d["pros"] = (await Q1("SELECT COUNT(*) AS c FROM pro_players") or {"c": 0})["c"]
    d["waiting"] = (await Q1("SELECT COUNT(*) AS c FROM waitlist WHERE status = 'waiting'") or {"c": 0})["c"]
    d["info"] = await safe(bot.np_hooks["info_data"](), {}) or {}
    return d


def guild_panels(d):
    g_ = d["guild"]
    members = d["members"]
    ranks = ", ".join(f"{k} {v}" for k, v in d["ranks"].most_common(4))
    top = d["gexp_top"]
    info = d["info"]
    panels = [
        ("MEMBERS", plural(len(members), "member"), ranks or "No data", g.TEXT),
        ("GEXP THIS WEEK", n(d["gexp_total"]), (f"{n(d['gexp_total'] // max(1, d['gexp_n']))} average per member" if d["gexp_n"] else "No data"), g.TEXT),
        ("BREAKLIST", f"{d['bl_active']} on it now", f"{plural(d['bl_total'], 'entry') if d['bl_total'] == 1 else str(d['bl_total']) + ' entries'} ever" + (f" · {d['visiting']} visiting a guild" if d["visiting"] else ""),
         g.RED if d["bl_active"] else g.GREEN),
        ("EXCUSED THIS WEEK", plural(d["excused"], "member"), "Not kicked at the purge", g.TEXT if d["excused"] else g.MUTED),
        ("PURGE (THIS CYCLE)", f"{d['purge_left']} left the guild", f"{d['purge_suspects']} short of the GEXP they owed",
         g.RED if d["purge_suspects"] else g.TEXT),
        ("PUNISHMENTS", f"{d['mutes']} muted now", f"{plural(d['warns'], 'verbal warning')} not expired", g.RED if d["mutes"] else g.TEXT),
        ("BLACKLIST", f"{d['blacklist']} blacklisted", "Players refused by the guild", g.RED if d["blacklist"] else g.GREEN),
        ("1V1S", f"{info.get('players', 0)} players · {info.get('matches', 0)} matches",
         f"{info.get('recent', 0)} in the last 7 days" + (f" · top {info['top'][0]} ({info['top'][1]})" if info.get("top") else ""), g.TEXT),
        ("TOP GEXP", f"{d['gexp_top_name'] or '?'} · {top['t']:,}" if top else "-", "The best of the week so far", g.TEXT),
        ("ROSTER", f"{d['pros']} pro · {d['waiting']} waiting", "Pro list · applicants on the waiting list", g.TEXT),
    ]
    chips = [(plural(len(members), "member"), g.ACCENT)]
    created = g_.get("created")
    if created:
        from datetime import datetime, timezone
        chips.append((f"Created {gc.fmt_date(datetime.fromtimestamp(created / 1000, tz=timezone.utc))}", (120, 130, 150)))
    return panels, chips


GUILD_SECTIONS = [("summary", "Summary"), ("top", "Top GEXP"), ("ranks", "Ranks"), ("breaklist", "Breaklist"),
                  ("excused", "Excused"), ("purge", "Purge"), ("punish", "Punishments"), ("blacklist", "Blacklist"),
                  ("days", "Days"), ("roster", "Pro & waitlist"), ("road", "Road"), ("stats", "1v1 stats")]


async def guild_section(ctx, key, page):
    bot = ctx.bot
    core = bot.np_core
    if key == "summary":
        d = await gather_guild(bot)
        panels, chips = guild_panels(d)
        g_ = d["guild"]
        png = await asyncio.to_thread(
            draw_hub, "GUILD INFO", f"{bl.GUILD_NAME_DISPLAY} · everything about the guild", None,
            g_.get("name") or bl.GUILD_NAME_DISPLAY, chips, panels, "Use the buttons for the details",
            tag=(f"[{g_['tag']}]" if g_.get("tag") else "GUILD"))
        return Out(png=png, name="info.png")
    if key == "top":
        import top10
        res = await top10.build_page(core, ctx.week or "last", page, ctx.mark)
        if res is None:
            return Out(content="I have no saved GEXP for that week.")
        text, card, page, pages = res
        if ctx.found:
            text = ctx.found + "\n" + text
        return Out(content=text, png=card, name="top10.png", pages=pages, page=page)
    if key == "ranks":
        d = await gather_guild(bot)
        embed = discord.Embed(title=f"{bl.GUILD_NAME_DISPLAY}: ranks", color=0x00C8E6)
        embed.description = "\n".join(f"**{k}** · {v}" for k, v in d["ranks"].most_common()) or "No data."
        embed.set_footer(text=f"{len(d['members'])} members")
        return Out(embed=embed)
    if key == "breaklist":
        cog = bot.get_cog("breaklist") or bot.get_cog("Breaklist")
        res = await cog.guild_card("manual", page) if cog else None
        if res is None:
            return Out(content="Nobody is on the breaklist.")
        card, page, pages = res
        return Out(png=card, name="breaklist.png", pages=pages, page=page)
    if key == "excused":
        wk = gc.week_start(now_utc()).date().isoformat()
        rows = await QA("SELECT * FROM excuses WHERE week >= ? ORDER BY ign COLLATE NOCASE, week", wk)
        if not rows:
            return Out(content="Nobody is excused.")
        by = {}
        for r in rows:
            by.setdefault(r["ign"], []).append(r)
        lines = [f"**{esc(i)}**: " + (items[0]["reason"] or "no reason") + f" ({len(items)} week{'s' if len(items) != 1 else ''})"
                 for i, items in list(by.items())[:40]]
        return Out(embed=discord.Embed(title="Excused players", description="\n".join(lines)[:3900], color=0x00C8E6))
    if key == "purge":
        rows = await core.purge_rows(cycle="this")
        per = 7
        pages = max(1, -(-len(rows) // per))
        page = max(1, min(page, pages))
        chunk = rows[(page - 1) * per: page * per]
        heads = await g._fetch_heads([(e["uuid"], e["ign"]) for e in chunk])
        ws = gc.week_start(now_utc())
        card = await asyncio.to_thread(bl.draw_purge, chunk, heads, "PURGE CHECK",
                                       f"{bl.GUILD_NAME_DISPLAY} · this cycle from {gc.fmt_date(ws)}", page, pages)
        suspects = sum(1 for e in rows if e["status"] == "suspect")
        return Out(content=f"**{suspects}** of {len(rows)} member(s) who left are short of the GEXP they owed.",
                   png=card, name="purge.png", pages=pages, page=page)
    if key == "punish":
        mutes = await QA("SELECT * FROM mutes WHERE status = 'active' ORDER BY id DESC LIMIT 15")
        warns = await QA("SELECT * FROM warnings ORDER BY id DESC LIMIT 12")
        lines = [f"🔴 **{esc(m['ign'])}** · mute #{m['id']} · {bl.mute_length_text(m)} · {(m['reason'] or '-')[:80]}" for m in mutes]
        lines += [f"**{esc(w['ign'])}** · warn #{w['id']} · {gc.fmt_date(from_iso(w['at']))} · {(w['reason'] or '-')[:80]}" for w in warns]
        embed = discord.Embed(title="Punishments", description="\n".join(lines)[:3900] or "Nobody was punished.", color=0x00C8E6)
        embed.set_footer(text="Active mutes first, then the latest verbal warnings")
        return Out(embed=embed)
    if key == "blacklist":
        rows = await QA("SELECT * FROM blacklist WHERE status = 'active' ORDER BY id")
        if not rows:
            return Out(content="The blacklist is empty.")
        embed = discord.Embed(title="Guild blacklist", color=0xE74C3C, description=f"{len(rows)} player(s)")
        for r in rows[:24]:
            embed.add_field(name=f"{r['ign']} · {r['duration'] or 'no duration'}", value=(r["reason"] or "-")[:300], inline=False)
        return Out(embed=embed)
    if key == "days":
        import extras
        res = await extras.days_board(core, page)
        if res is None:
            return Out(content="I can't read the guild member list right now.")
        embed, page, pages = res
        return Out(embed=embed, pages=pages, page=page)
    if key == "roster":
        import pro
        lines = []
        now = now_utc()
        for p in await QA("SELECT * FROM pro_players ORDER BY ign COLLATE NOCASE"):
            anchor = await pro.anchor_of(p)
            lines.append(f"**{esc(p['ign'])}** · {int((now - anchor).total_seconds() // 86400)}d since the last C+ win"
                         + (f" · {esc(p['how'])}" if p["how"] else ""))
        wait = await QA("SELECT ign FROM waitlist WHERE status = 'waiting' ORDER BY added_at")
        embed = discord.Embed(title="Pro list and waiting list", color=0x00C8E6)
        embed.add_field(name=f"Pro ({len(lines)})", value="\n".join(lines)[:1000] or "Nobody.", inline=False)
        embed.add_field(name=f"Waiting list ({len(wait)})", value=", ".join(esc(w["ign"]) for w in wait)[:1000] or "Nobody.", inline=False)
        return Out(embed=embed)
    if key == "road":
        data, err = await core.road_status()
        if err:
            return Out(content=err)
        card = await core.road_card(data)
        return Out(content=data["text"], png=card, name="road.png")
    if key == "stats":
        png = await bot.np_hooks["info_image"]()
        return Out(png=png, name="info.png")
    return Out(content="Unknown section.")


# ======================================================================================
# the interactive view
# ======================================================================================

class Ctx:
    def __init__(self, bot, owner_id, kind, uuid=None, name=None):
        self.bot, self.owner_id, self.kind, self.uuid, self.name = bot, owner_id, kind, uuid, name
        self.spec = "-"        # 1v1 section: lifetime / mode / season / year
        self.week = "last"     # top GEXP section
        self.mark = None       # uuid highlighted after a search in the top
        self.found = None      # "🔍 name is #N" line shown above the top


class SearchModal(discord.ui.Modal, title="Find a player in the top"):
    ign = discord.ui.TextInput(label="Minecraft username", max_length=16, required=True)

    def __init__(self, hub_view):
        super().__init__()
        self.hub_view = hub_view

    async def on_submit(self, interaction):
        import top10
        ctx = self.hub_view.ctx
        await interaction.response.defer()               # looking up the names can take more than 3 seconds
        try:
            found = await top10.locate(ctx.bot.np_core, ctx.week or "last", str(self.ign))
            if found is None:
                await interaction.followup.send(f"**{esc(str(self.ign))}** is not in this top (not a guild member, no GEXP that week, or not a valid username).",
                                                ephemeral=True, allowed_mentions=NO_MENTIONS)
                return
            pos, uuid, name, gexp = found
            ctx.mark = uuid
            ctx.found = f"🔍 **{esc(name)}** is **#{pos}** with **{n(gexp)}** GEXP."
            out = await render(ctx, "top", (pos - 1) // 10 + 1)
            await interaction.edit_original_response(**await message_kwargs(ctx, "top", out))
        except Exception as e:
            log.exception("Search in the top failed")
            await interaction.followup.send(f"⚠️ The search failed: `{type(e).__name__}: {e}`"[:1800], ephemeral=True)

    async def on_error(self, interaction, error):
        log.exception("Search modal error", exc_info=error)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(f"⚠️ {type(error).__name__}: {error}"[:1800], ephemeral=True)
            else:
                await interaction.response.send_message(f"⚠️ {type(error).__name__}: {error}"[:1800], ephemeral=True)
        except Exception:
            pass


class HubView(discord.ui.View):
    def __init__(self, ctx, key, page=1, pages=1, options=None):
        super().__init__(timeout=900)
        self.ctx, self.key, self.page, self.pages = ctx, key, page, pages
        sections = PLAYER_SECTIONS if ctx.kind == "player" else GUILD_SECTIONS
        for k, label in sections:
            btn = discord.ui.Button(label=label, style=discord.ButtonStyle.success if k == key else discord.ButtonStyle.secondary)
            btn.callback = self._section_cb(k)
            self.add_item(btn)
        if pages > 1:
            prev = discord.ui.Button(label="◀ Previous", style=discord.ButtonStyle.primary, disabled=page <= 1, row=4)
            nxt = discord.ui.Button(label="Next ▶", style=discord.ButtonStyle.primary, disabled=page >= pages, row=4)
            prev.callback, nxt.callback = self._page_cb(-1), self._page_cb(1)
            self.add_item(prev)
            self.add_item(nxt)
        if ctx.kind == "player" and key == "blacklist":
            rst = discord.ui.Button(label="Reset blacklist history", style=discord.ButtonStyle.danger, row=4)
            rst.callback = self._reset_cb
            self.add_item(rst)
        if ctx.kind == "guild" and key == "top":
            find = discord.ui.Button(label="🔍 Search", style=discord.ButtonStyle.primary, row=4)
            find.callback = self._find_cb
            self.add_item(find)
        if options:
            sel = discord.ui.Select(placeholder=options["placeholder"], row=3,
                                    options=[discord.SelectOption(label=l[:100], value=v, default=(v == options["current"]))
                                             for l, v in options["items"]])
            sel.callback = self._select_cb(sel, options["kind"])
            self.add_item(sel)

    async def _reset_cb(self, interaction):
        import evidence
        if interaction.user.id != self.ctx.owner_id or not evidence.allowed(interaction.user):
            await interaction.response.send_message("Only the guild staff can do this.", ephemeral=True)
            return
        await evidence.ask_reset(interaction, self.ctx.uuid, self.ctx.name)

    async def _find_cb(self, interaction):
        if interaction.user.id != self.ctx.owner_id:
            await interaction.response.send_message("Use /info yourself to get your own buttons.", ephemeral=True)
            return
        await interaction.response.send_modal(SearchModal(self))

    def _section_cb(self, key):
        async def cb(interaction):
            self.ctx.mark = self.ctx.found = None
            await self.show(interaction, key, 1)
        return cb

    def _page_cb(self, delta):
        async def cb(interaction):
            await self.show(interaction, self.key, self.page + delta)
        return cb

    def _select_cb(self, sel, kind):
        async def cb(interaction):
            if kind == "spec":
                self.ctx.spec = sel.values[0]
            else:
                self.ctx.week = sel.values[0]
                self.ctx.mark = self.ctx.found = None
            await self.show(interaction, self.key, 1)
        return cb

    async def show(self, interaction, key, page):
        if interaction.user.id != self.ctx.owner_id:
            await interaction.response.send_message("Use /profile or /info yourself to get your own buttons.", ephemeral=True)
            return
        await interaction.response.defer()
        out = await render(self.ctx, key, page)
        await interaction.edit_original_response(**await message_kwargs(self.ctx, key, out))


async def section_options(ctx, key):
    if ctx.kind == "player" and key == "stats":
        items = await ctx.bot.np_hooks["profile_options"]()
        return {"placeholder": "Mode, season or year", "items": items, "current": ctx.spec, "kind": "spec"}
    if ctx.kind == "guild" and key == "top":
        return {"placeholder": "Which week", "items": [("Last week", "last"), ("This week so far", "this")],
                "current": ctx.week, "kind": "week"}
    return None


async def render(ctx, key, page):
    fn = player_section if ctx.kind == "player" else guild_section
    try:
        return await fn(ctx, key, page)
    except Exception:
        log.exception("Section %s failed", key)
        return Out(content="⚠️ I couldn't build this section. Try again in a moment.")


async def message_kwargs(ctx, key, out):
    view = HubView(ctx, key, out.page, out.pages, await section_options(ctx, key))
    kw = {"content": out.content, "embeds": [out.embed] if out.embed else [], "view": view,
          "attachments": [send_file(out.png, out.name)] if out.png else [], "allowed_mentions": NO_MENTIONS}
    return kw


async def resolve_player(interaction, ign):
    """(uuid, name): the typed Minecraft username, or the linked account of the caller. None after an error."""
    if ign:
        return await bl.lookup(interaction, ign)
    row = await q_one("SELECT uuid, ign FROM links WHERE discord_id = ?", (str(interaction.user.id),))
    if row is None:
        await interaction.followup.send("You haven't linked a Minecraft account yet. Use `/link` or type a username.")
        return None
    return row["uuid"], row["ign"]


class HubCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="profile", description="Everything about a player: 1v1s, guild, breaklist, punishments and more")
    @app_commands.describe(player="Minecraft username (leave empty for your linked account)",
                           hidden="Only you see the answer")
    async def profile(self, interaction: discord.Interaction, player: Optional[str] = None, hidden: bool = False):
        await interaction.response.defer(ephemeral=hidden)
        r = await resolve_player(interaction, player)
        if r is None:
            return
        ctx = Ctx(self.bot, interaction.user.id, "player", r[0], r[1])
        out = await render(ctx, "summary", 1)
        kw = await message_kwargs(ctx, "summary", out)
        await interaction.followup.send(content=kw["content"], file=kw["attachments"][0] if kw["attachments"] else discord.utils.MISSING,
                                        view=kw["view"], allowed_mentions=NO_MENTIONS, ephemeral=hidden)

    @profile.autocomplete("player")
    async def _profile_ac(self, interaction: discord.Interaction, current: str):
        return await player_choices(current)

    @app_commands.command(name="info", description="Everything about the guild: members, GEXP, breaklist, punishments and more")
    @app_commands.describe(hidden="Only you see the answer")
    async def info(self, interaction: discord.Interaction, hidden: bool = False):
        await interaction.response.defer(ephemeral=hidden)
        ctx = Ctx(self.bot, interaction.user.id, "guild")
        out = await render(ctx, "summary", 1)
        kw = await message_kwargs(ctx, "summary", out)
        await interaction.followup.send(content=kw["content"], file=kw["attachments"][0] if kw["attachments"] else discord.utils.MISSING,
                                        view=kw["view"], allowed_mentions=NO_MENTIONS, ephemeral=hidden)


NAME_TABLES = ("links", "players", "countries", "bl_entries", "pro_players", "blacklist", "mutes", "warnings",
               "waitlist", "guild_left", "excuses")


async def player_choices(current, limit=25):
    """Names to suggest while typing a player: every name the bot already knows (linked, 1v1s, breaklist, mutes...)."""
    cur = (current or "").strip().replace("%", "").replace("_", r"\_")
    found = {}
    for pattern in (f"{cur}%", f"%{cur}%"):
        for table in NAME_TABLES:
            if len(found) >= limit:
                break
            for r in await QA(f"SELECT DISTINCT ign FROM {table} WHERE ign LIKE ? ESCAPE '\\' AND ign != '' LIMIT ?",
                              pattern, limit):
                if r["ign"] and r["ign"].lower() not in found:
                    found[r["ign"].lower()] = r["ign"]
        if len(found) >= limit:
            break
    names = sorted(found.values(), key=lambda x: (not x.lower().startswith(cur.lower()), x.lower()))
    return [app_commands.Choice(name=x[:100], value=x[:100]) for x in names[:limit]]


async def setup(bot: commands.Bot):
    await bot.add_cog(HubCog(bot))

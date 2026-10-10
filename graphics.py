import asyncio
import base64
import io
import json
import time

import aiohttp
from PIL import Image, ImageDraw, ImageFont

from config import GUILD_NAME, CREDIT, TIERS, tier_from_elo

WIDTH = 900
PAD = 28
HEADER_H = 120
FOOTER_H = 56
ROW_H = 72
HEAD = 48

BG = (22, 23, 27)
HEADER_BG = (30, 32, 38)
ROW_BG = (38, 40, 47)
TEXT = (242, 243, 247)
MUTED = (140, 146, 160)
GOLD = (255, 199, 44)
GREEN = (67, 201, 130)
RED = (240, 98, 98)
INK = (25, 25, 30)
PLACEHOLDER = (60, 63, 72)
TRACK = (52, 55, 64)

TIER_COLORS = {
    "S": GOLD,
    "A": (167, 112, 255),
    "B": (74, 144, 255),
    "C": (67, 201, 130),
    "D": (140, 145, 155),
}
PODIUM = [GOLD, (196, 202, 214), (205, 127, 50)]

_fonts = {}


def get_font(size, bold=False):
    key = (size, bold)
    if key in _fonts:
        return _fonts[key]
    names = (["segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf"] if bold
             else ["segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"])
    font = None
    for name in names:
        try:
            font = ImageFont.truetype(name, size)
            break
        except OSError:
            continue
    if font is None:
        try:
            font = ImageFont.load_default(size)
        except TypeError:
            font = ImageFont.load_default()
    _fonts[key] = font
    return font


# ---------- player heads ----------
# 1) the skin is downloaded straight from Mojang and the face is cropped here
# 2) if that fails, a few public head services are tried in order
# Results are cached for one hour, keyed by UUID.

_head_cache = {}   # uuid -> (timestamp, png bytes)
HEAD_TTL = 3600


async def _get_bytes(session, url):
    try:
        async with session.get(url) as r:
            if r.status == 200:
                return await r.read()
    except Exception:
        pass
    return None


def _face_from_skin(skin_bytes):
    skin = Image.open(io.BytesIO(skin_bytes)).convert("RGBA")
    s = max(1, skin.width // 64)          # supports HD skins too
    face = skin.crop((8 * s, 8 * s, 16 * s, 16 * s))
    hat = skin.crop((40 * s, 8 * s, 48 * s, 16 * s))
    face.alpha_composite(hat)
    face = face.resize((64, 64), Image.NEAREST)
    buf = io.BytesIO()
    face.save(buf, "PNG")
    return buf.getvalue()


async def _head_from_mojang(session, uuid):
    try:
        async with session.get(
                f"https://sessionserver.mojang.com/session/minecraft/profile/{uuid}") as r:
            if r.status != 200:
                return None
            profile = await r.json()
        for prop in profile.get("properties", []):
            if prop.get("name") == "textures":
                textures = json.loads(base64.b64decode(prop["value"]))
                skin = textures.get("textures", {}).get("SKIN")
                if not skin:
                    return None
                url = skin["url"].replace("http://", "https://")
                skin_bytes = await _get_bytes(session, url)
                if not skin_bytes:
                    return None
                return await asyncio.to_thread(_face_from_skin, skin_bytes)
    except Exception:
        return None
    return None


async def _fetch_head(session, uuid, name):
    cached = _head_cache.get(uuid)
    if cached and time.time() - cached[0] < HEAD_TTL:
        return uuid, cached[1]

    data = await _head_from_mojang(session, uuid)
    if data is None:
        for url in (f"https://crafatar.com/avatars/{uuid}?size=64&overlay",
                    f"https://mc-heads.net/avatar/{uuid}/64",
                    f"https://minotar.net/helm/{name}/64.png"):
            data = await _get_bytes(session, url)
            if data:
                break
    if data:
        _head_cache[uuid] = (time.time(), data)
    return uuid, data


async def _fetch_heads(items):
    """items = list of (uuid, name). Returns {uuid: png bytes or None}."""
    unique = list({u: n for u, n in items}.items())
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        results = await asyncio.gather(*(_fetch_head(session, u, n) for u, n in unique))
    return dict(results)


# ---------- drawing helpers ----------

def _tier_color(tier):
    return TIER_COLORS.get((tier or "-")[0], MUTED)


def _header(d, title, subtitle):
    d.rectangle([0, 0, WIDTH, HEADER_H], fill=HEADER_BG)
    d.rectangle([0, HEADER_H - 5, WIDTH, HEADER_H], fill=GOLD)
    d.text((PAD, 38), title, font=get_font(40, True), fill=TEXT, anchor="lm")
    d.text((PAD, 82), subtitle, font=get_font(20), fill=MUTED, anchor="lm")


def _footer(d, height, text=None):
    fy = height - FOOTER_H // 2
    if text:
        d.text((WIDTH // 2, fy), text, font=get_font(20), fill=MUTED, anchor="mm")
    d.text((WIDTH - PAD, fy + 8), CREDIT, font=get_font(16), fill=MUTED, anchor="rm")


def _paste_head(img, d, data, x, y, size, radius=8):
    if data:
        try:
            head = Image.open(io.BytesIO(data)).convert("RGBA").resize((size, size), Image.NEAREST)
            mask = Image.new("L", (size, size), 0)
            ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
            img.paste(head, (x, y), mask)
            return
        except Exception:
            pass
    d.rounded_rectangle([x, y, x + size, y + size], radius=radius, fill=PLACEHOLDER)


def _badge(d, cx, cy, text, color, w=78, h=36, font_size=22):
    d.rounded_rectangle([cx - w // 2, cy - h // 2, cx + w // 2, cy + h // 2], radius=10, fill=color)
    d.text((cx, cy), text, font=get_font(font_size, True), fill=INK, anchor="mm")


def _png(img):
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


# ---------- leaderboard ----------

def _draw_leaderboard(chunk, heads, start_rank, page, pages, total):
    height = HEADER_H + 10 + len(chunk) * ROW_H + FOOTER_H
    img = Image.new("RGB", (WIDTH, height), BG)
    d = ImageDraw.Draw(img)
    _header(d, "GLOBAL LEADERBOARD", f"{GUILD_NAME} · {total} players · ranked by elo")

    for i, row in enumerate(chunk):
        rank = start_rank + i
        y0 = HEADER_H + 10 + i * ROW_H
        cy = y0 + (ROW_H - 8) // 2

        d.rounded_rectangle([PAD, y0, WIDTH - PAD, y0 + ROW_H - 8], radius=14, fill=ROW_BG)
        accent = PODIUM[rank - 1] if rank <= 3 else None
        if accent:
            d.rounded_rectangle([PAD, y0, PAD + 8, y0 + ROW_H - 8], radius=4, fill=accent)

        rx = PAD + 50
        if accent:
            d.ellipse([rx - 20, cy - 20, rx + 20, cy + 20], fill=accent)
            d.text((rx, cy), str(rank), font=get_font(24, True), fill=INK, anchor="mm")
        else:
            d.text((rx, cy), f"#{rank}", font=get_font(24, True), fill=MUTED, anchor="mm")

        hx = PAD + 100
        _paste_head(img, d, heads.get(row["uuid"]), hx, cy - HEAD // 2, HEAD)
        d.text((hx + HEAD + 18, cy), row["ign"], font=get_font(28, True), fill=TEXT, anchor="lm")

        tier = row["tier"] or "-"
        _badge(d, 599, cy, tier, _tier_color(tier))

        ex = WIDTH - PAD - 28
        d.text((ex, cy), "elo", font=get_font(18), fill=MUTED, anchor="rm")
        w = d.textlength("elo", font=get_font(18))
        d.text((ex - w - 8, cy), str(row["elo"]), font=get_font(30, True), fill=TEXT, anchor="rm")

    _footer(d, height, f"Page {page + 1}/{pages}")
    return _png(img)


async def build_leaderboard_image(rows, page, per_page):
    start = page * per_page
    chunk = rows[start:start + per_page]
    pages = max(1, (len(rows) + per_page - 1) // per_page)
    heads = await _fetch_heads([(r["uuid"], r["ign"]) for r in chunk])
    return await asyncio.to_thread(_draw_leaderboard, chunk, heads, start + 1, page, pages, len(rows))


# ---------- match result (side by side) ----------

PANEL_W = 360
PANEL_H = 340


def _match_panel(img, d, x0, y0, p, accent, label, head_data):
    cx = x0 + PANEL_W // 2
    d.rounded_rectangle([x0, y0, x0 + PANEL_W, y0 + PANEL_H], radius=18,
                        fill=ROW_BG, outline=accent, width=3)
    _badge(d, cx, y0, label, accent, w=116, h=30, font_size=16)
    _paste_head(img, d, head_data, cx - 60, y0 + 32, 120, radius=14)
    d.text((cx, y0 + 184), p["name"], font=get_font(32, True), fill=TEXT, anchor="mm")

    ty = y0 + 224
    if p["tier_before"] != p["tier_after"]:
        _badge(d, cx - 50, ty, p["tier_before"], _tier_color(p["tier_before"]), w=60, h=30, font_size=18)
        d.text((cx, ty), "→", font=get_font(20, True), fill=MUTED, anchor="mm")
        _badge(d, cx + 50, ty, p["tier_after"], _tier_color(p["tier_after"]), w=60, h=30, font_size=18)
    else:
        _badge(d, cx, ty, p["tier_after"], _tier_color(p["tier_after"]), w=60, h=30, font_size=18)

    delta_color = GREEN if p["delta"] >= 0 else RED
    d.text((cx, y0 + 274), f"{p['delta']:+d}", font=get_font(46, True), fill=delta_color, anchor="mm")
    d.text((cx, y0 + 314), f"{p['before']} → {p['after']} elo", font=get_font(20), fill=MUTED, anchor="mm")


def _draw_match(w, l, score, match_id, date, heads, title, forfeit):
    top = HEADER_H + 24
    height = top + PANEL_H + 20 + FOOTER_H
    img = Image.new("RGB", (WIDTH, height), BG)
    d = ImageDraw.Draw(img)
    if title == "MATCH RESULT":
        subtitle = f"{GUILD_NAME} · " + ("forfeit" if forfeit else "first to 3")
    else:
        subtitle = f"{GUILD_NAME} · elo restored"
    _header(d, title, subtitle)

    _match_panel(img, d, PAD, top, w, GOLD, "WINNER", heads.get(w["uuid"]))
    _match_panel(img, d, WIDTH - PAD - PANEL_W, top, l, RED,
                 "FORFEIT" if forfeit else "LOSER", heads.get(l["uuid"]))

    mid = top + PANEL_H // 2
    d.text((WIDTH // 2, mid - 40), "VS", font=get_font(34, True), fill=MUTED, anchor="mm")
    d.text((WIDTH // 2, mid + 20), score, font=get_font(54, True), fill=GOLD, anchor="mm")
    if forfeit:
        d.text((WIDTH // 2, mid + 62), "by forfeit", font=get_font(16, True), fill=RED, anchor="mm")

    _footer(d, height, f"Match #{match_id} · {date}")
    return _png(img)


async def build_match_image(winner, loser, score, match_id, date, title="MATCH RESULT", forfeit=False):
    heads = await _fetch_heads([(winner["uuid"], winner["name"]), (loser["uuid"], loser["name"])])
    return await asyncio.to_thread(_draw_match, winner, loser, score, match_id, date, heads, title, forfeit)


# ---------- paused match (side by side) ----------

def _pause_panel(img, d, x0, y0, p, head_data):
    cx = x0 + PANEL_W // 2
    d.rounded_rectangle([x0, y0, x0 + PANEL_W, y0 + PANEL_H], radius=18,
                        fill=ROW_BG, outline=MUTED, width=3)
    _paste_head(img, d, head_data, cx - 60, y0 + 32, 120, radius=14)
    d.text((cx, y0 + 184), p["name"], font=get_font(32, True), fill=TEXT, anchor="mm")
    _badge(d, cx, y0 + 224, p["tier"], _tier_color(p["tier"]), w=60, h=30, font_size=18)
    d.text((cx, y0 + 252), "GAMES WON", font=get_font(14, True), fill=MUTED, anchor="mm")
    d.text((cx, y0 + 284), str(p["games"]), font=get_font(40, True), fill=GOLD, anchor="mm")
    d.text((cx, y0 + 318), f"{p['elo']} elo", font=get_font(18), fill=MUTED, anchor="mm")


def _draw_pause(p1, p2, pause_id, date, heads):
    top = HEADER_H + 24
    height = top + PANEL_H + 20 + FOOTER_H
    img = Image.new("RGB", (WIDTH, height), BG)
    d = ImageDraw.Draw(img)
    _header(d, "MATCH PAUSED", f"{GUILD_NAME} · to be continued · first to 3")

    _pause_panel(img, d, PAD, top, p1, heads.get(p1["uuid"]))
    _pause_panel(img, d, WIDTH - PAD - PANEL_W, top, p2, heads.get(p2["uuid"]))

    mid = top + PANEL_H // 2
    d.text((WIDTH // 2, mid - 40), "VS", font=get_font(34, True), fill=MUTED, anchor="mm")
    _badge(d, WIDTH // 2, mid + 20, "PAUSED", GOLD, w=104, h=34, font_size=18)

    _footer(d, height, f"Paused #{pause_id} · {date}")
    return _png(img)


async def build_pause_image(p1, p2, pause_id, date):
    heads = await _fetch_heads([(p1["uuid"], p1["name"]), (p2["uuid"], p2["name"])])
    return await asyncio.to_thread(_draw_pause, p1, p2, pause_id, date, heads)


# ---------- list of paused matches ----------

def _draw_paused_list(entries, total, heads):
    height = HEADER_H + 10 + len(entries) * ROW_H + FOOTER_H
    img = Image.new("RGB", (WIDTH, height), BG)
    d = ImageDraw.Draw(img)
    _header(d, "PAUSED MATCHES", f"{GUILD_NAME} · {total} waiting to be continued")

    for i, e in enumerate(entries):
        y0 = HEADER_H + 10 + i * ROW_H
        cy = y0 + (ROW_H - 8) // 2

        d.rounded_rectangle([PAD, y0, WIDTH - PAD, y0 + ROW_H - 8], radius=14, fill=ROW_BG)
        d.rounded_rectangle([PAD, y0, PAD + 8, y0 + ROW_H - 8], radius=4, fill=GOLD)

        d.text((PAD + 24, cy - 10), f"Paused #{e['id']}", font=get_font(18, True), fill=TEXT, anchor="lm")
        d.text((PAD + 24, cy + 14), e["date"], font=get_font(15), fill=MUTED, anchor="lm")

        _paste_head(img, d, heads.get(e["p1_uuid"]), 178, cy - 20, 40)
        d.text((226, cy), e["p1"], font=get_font(20, True), fill=TEXT, anchor="lm")

        d.text((450, cy), f"{e['g1']} - {e['g2']}", font=get_font(22, True), fill=GOLD, anchor="mm")

        _paste_head(img, d, heads.get(e["p2_uuid"]), 494, cy - 20, 40)
        d.text((542, cy), e["p2"], font=get_font(20, True), fill=TEXT, anchor="lm")

        d.text((WIDTH - PAD - 28, cy), "to continue", font=get_font(15), fill=MUTED, anchor="rm")

    note = f"Showing the latest {len(entries)}" if total > len(entries) else None
    _footer(d, height, note)
    return _png(img)


async def build_paused_list_image(entries, total):
    items = []
    for e in entries:
        items.append((e["p1_uuid"], e["p1"]))
        items.append((e["p2_uuid"], e["p2"]))
    heads = await _fetch_heads(items)
    return await asyncio.to_thread(_draw_paused_list, entries, total, heads)


# ---------- player profile ----------

def _draw_profile(uuid, name, tier, elo, rank, wins, losses, winrate, forfeits, heads):
    id_h, box_h, gap = 170, 110, 14
    height = HEADER_H + 16 + id_h + gap + box_h + 16 + FOOTER_H
    img = Image.new("RGB", (WIDTH, height), BG)
    d = ImageDraw.Draw(img)
    _header(d, "PLAYER PROFILE", GUILD_NAME)

    y0 = HEADER_H + 16
    color = _tier_color(tier)
    d.rounded_rectangle([PAD, y0, WIDTH - PAD, y0 + id_h], radius=16, fill=ROW_BG)
    d.rounded_rectangle([PAD, y0, PAD + 8, y0 + id_h], radius=4, fill=color)
    _paste_head(img, d, heads.get(uuid), PAD + 34, y0 + 20, 130, radius=14)
    nx = PAD + 34 + 130 + 28
    d.text((nx, y0 + 62), name, font=get_font(46, True), fill=TEXT, anchor="lm")
    _badge(d, nx + 48, y0 + 120, tier, color, w=96, h=42, font_size=26)

    # forfeits counter (right side of the identity panel)
    fx = WIDTH - PAD - 40
    d.text((fx, y0 + 62), "FORFEITS", font=get_font(16, True), fill=MUTED, anchor="rm")
    d.text((fx, y0 + 108), str(forfeits), font=get_font(44, True),
           fill=RED if forfeits > 0 else TEXT, anchor="rm")

    by = y0 + id_h + gap
    bw = (WIDTH - 2 * PAD - 3 * gap) // 4
    stats = [("ELO", str(elo)), ("GLOBAL RANK", f"#{rank}"),
             ("RECORD", f"{wins}W - {losses}L"), ("WIN RATE", winrate)]
    for i, (label, value) in enumerate(stats):
        bx = PAD + i * (bw + gap)
        d.rounded_rectangle([bx, by, bx + bw, by + box_h], radius=14, fill=ROW_BG)
        d.text((bx + bw // 2, by + 36), label, font=get_font(16, True), fill=MUTED, anchor="mm")
        d.text((bx + bw // 2, by + 74), value, font=get_font(34, True), fill=TEXT, anchor="mm")

    _footer(d, height)
    return _png(img)


async def build_profile_image(uuid, name, tier, elo, rank, wins, losses, winrate, forfeits):
    heads = await _fetch_heads([(uuid, name)])
    return await asyncio.to_thread(_draw_profile, uuid, name, tier, elo, rank, wins, losses,
                                   winrate, forfeits, heads)


# ---------- player match history (paged, cancelled matches included) ----------

def _draw_history(name, entries, heads, page, pages, total):
    height = HEADER_H + 10 + len(entries) * ROW_H + FOOTER_H
    img = Image.new("RGB", (WIDTH, height), BG)
    d = ImageDraw.Draw(img)
    _header(d, "MATCH HISTORY", f"{GUILD_NAME} · {name} · {total} matches")

    for i, e in enumerate(entries):
        y0 = HEADER_H + 10 + i * ROW_H
        cy = y0 + (ROW_H - 8) // 2
        cancelled = e.get("cancelled", False)
        forfeit = e.get("forfeit", False)
        color = MUTED if cancelled else (GREEN if e["win"] else RED)
        label = "CANCELLED" if cancelled else ("WIN" if e["win"] else "LOSS")

        d.rounded_rectangle([PAD, y0, WIDTH - PAD, y0 + ROW_H - 8], radius=14, fill=ROW_BG)
        d.rounded_rectangle([PAD, y0, PAD + 8, y0 + ROW_H - 8], radius=4, fill=color)
        _badge(d, PAD + 80, cy, label, color, w=100 if cancelled else 84, h=36,
               font_size=14 if cancelled else 20)

        hx = PAD + 140
        _paste_head(img, d, heads.get(e["opp_uuid"]), hx, cy - HEAD // 2, HEAD)
        tx = hx + HEAD + 18
        d.text((tx, cy - 11), f"vs {e['opp']}", font=get_font(26, True),
               fill=MUTED if cancelled else TEXT, anchor="lm")
        d.text((tx, cy + 17), f"Match #{e['id']} · {e['date']}", font=get_font(16), fill=MUTED, anchor="lm")

        d.text((620, cy - 7 if forfeit else cy), e["score"], font=get_font(24, True),
               fill=MUTED, anchor="mm")
        if forfeit:
            d.text((620, cy + 16), "FORFEIT", font=get_font(13, True),
                   fill=RED if (not e["win"] and not cancelled) else MUTED, anchor="mm")

        ex = WIDTH - PAD - 28
        d.text((ex, cy), "elo", font=get_font(18), fill=MUTED, anchor="rm")
        w = d.textlength("elo", font=get_font(18))
        delta_text = "—" if cancelled else f"{e['delta']:+d}"
        d.text((ex - w - 8, cy), delta_text, font=get_font(30, True), fill=color, anchor="rm")

    _footer(d, height, f"Page {page + 1}/{pages}")
    return _png(img)


async def build_history_image(name, entries, page, pages, total):
    heads = await _fetch_heads([(e["opp_uuid"], e["opp"]) for e in entries])
    return await asyncio.to_thread(_draw_history, name, entries, heads, page, pages, total)


# ---------- guild-wide match log (paged, cancelled matches included) ----------

def _draw_matchlog(entries, heads, page, pages, total):
    height = HEADER_H + 10 + len(entries) * ROW_H + FOOTER_H
    img = Image.new("RGB", (WIDTH, height), BG)
    d = ImageDraw.Draw(img)
    _header(d, "MATCH LOG", f"{GUILD_NAME} · latest {total} matches")

    for i, e in enumerate(entries):
        y0 = HEADER_H + 10 + i * ROW_H
        cy = y0 + (ROW_H - 8) // 2
        cancelled = e.get("cancelled", False)
        forfeit = e.get("forfeit", False)

        d.rounded_rectangle([PAD, y0, WIDTH - PAD, y0 + ROW_H - 8], radius=14, fill=ROW_BG)
        d.rounded_rectangle([PAD, y0, PAD + 8, y0 + ROW_H - 8], radius=4, fill=MUTED if cancelled else GOLD)

        d.text((PAD + 24, cy - 10), f"Match #{e['id']}", font=get_font(18, True), fill=TEXT, anchor="lm")
        d.text((PAD + 24, cy + 14), e["date"], font=get_font(15), fill=MUTED, anchor="lm")

        _paste_head(img, d, heads.get(e["winner_uuid"]), 178, cy - 20, 40)
        d.text((226, cy), e["winner"], font=get_font(20, True),
               fill=MUTED if cancelled else TEXT, anchor="lm")

        d.text((450, cy - 7 if forfeit else cy), e["score"], font=get_font(22, True),
               fill=MUTED if cancelled else GOLD, anchor="mm")
        if forfeit:
            d.text((450, cy + 16), "FORFEIT", font=get_font(13, True),
                   fill=MUTED if cancelled else RED, anchor="mm")

        _paste_head(img, d, heads.get(e["loser_uuid"]), 494, cy - 20, 40)
        d.text((542, cy), e["loser"], font=get_font(20, True), fill=MUTED, anchor="lm")

        ex = WIDTH - PAD - 28
        if cancelled:
            _badge(d, ex - 50, cy, "CANCELLED", MUTED, w=100, h=26, font_size=14)
        else:
            d.text((ex, cy - 12), f"{e['w_delta']:+d}", font=get_font(20, True), fill=GREEN, anchor="rm")
            d.text((ex, cy + 12), f"{e['l_delta']:+d}", font=get_font(20, True), fill=RED, anchor="rm")

    _footer(d, height, f"Page {page + 1}/{pages}")
    return _png(img)


async def build_matchlog_image(entries, page, pages, total):
    items = []
    for e in entries:
        items.append((e["winner_uuid"], e["winner"]))
        items.append((e["loser_uuid"], e["loser"]))
    heads = await _fetch_heads(items)
    return await asyncio.to_thread(_draw_matchlog, entries, heads, page, pages, total)


# ---------- tier assigned ----------

def _draw_settier(uuid, name, old_tier, new_tier, elo, heads):
    p_h = 150
    height = HEADER_H + 16 + p_h + 16 + FOOTER_H
    img = Image.new("RGB", (WIDTH, height), BG)
    d = ImageDraw.Draw(img)
    _header(d, "TIER ASSIGNED", GUILD_NAME)

    y0 = HEADER_H + 16
    color = _tier_color(new_tier)
    d.rounded_rectangle([PAD, y0, WIDTH - PAD, y0 + p_h], radius=16, fill=ROW_BG)
    d.rounded_rectangle([PAD, y0, PAD + 8, y0 + p_h], radius=4, fill=color)
    _paste_head(img, d, heads.get(uuid), PAD + 34, y0 + 25, 100, radius=12)

    nx = PAD + 34 + 100 + 26
    d.text((nx, y0 + 52), name, font=get_font(38, True), fill=TEXT, anchor="lm")
    by = y0 + 106
    if old_tier and old_tier != new_tier:
        _badge(d, nx + 32, by, old_tier, _tier_color(old_tier), w=64, h=34, font_size=20)
        d.text((nx + 76, by), "→", font=get_font(22, True), fill=MUTED, anchor="mm")
        _badge(d, nx + 120, by, new_tier, color, w=64, h=34, font_size=20)
    else:
        _badge(d, nx + 32, by, new_tier, color, w=64, h=34, font_size=20)

    d.text((WIDTH - PAD - 30, y0 + 62), str(elo), font=get_font(52, True), fill=TEXT, anchor="rm")
    d.text((WIDTH - PAD - 30, y0 + 106), "ELO", font=get_font(18, True), fill=MUTED, anchor="rm")

    _footer(d, height)
    return _png(img)


async def build_settier_image(uuid, name, old_tier, new_tier, elo):
    heads = await _fetch_heads([(uuid, name)])
    return await asyncio.to_thread(_draw_settier, uuid, name, old_tier, new_tier, elo, heads)


# ---------- elo changed by hand ----------

def _draw_setelo(uuid, name, old_elo, new_elo, heads):
    p_h = 150
    height = HEADER_H + 16 + p_h + 16 + FOOTER_H
    img = Image.new("RGB", (WIDTH, height), BG)
    d = ImageDraw.Draw(img)
    _header(d, "ELO UPDATED", GUILD_NAME)

    new_tier = tier_from_elo(new_elo)
    old_tier = tier_from_elo(old_elo) if old_elo is not None else None
    color = _tier_color(new_tier)

    y0 = HEADER_H + 16
    d.rounded_rectangle([PAD, y0, WIDTH - PAD, y0 + p_h], radius=16, fill=ROW_BG)
    d.rounded_rectangle([PAD, y0, PAD + 8, y0 + p_h], radius=4, fill=color)
    _paste_head(img, d, heads.get(uuid), PAD + 34, y0 + 25, 100, radius=12)

    nx = PAD + 34 + 100 + 26
    d.text((nx, y0 + 52), name, font=get_font(38, True), fill=TEXT, anchor="lm")
    by = y0 + 106
    if old_tier and old_tier != new_tier:
        _badge(d, nx + 32, by, old_tier, _tier_color(old_tier), w=64, h=34, font_size=20)
        d.text((nx + 76, by), "→", font=get_font(22, True), fill=MUTED, anchor="mm")
        _badge(d, nx + 120, by, new_tier, color, w=64, h=34, font_size=20)
    else:
        _badge(d, nx + 32, by, new_tier, color, w=64, h=34, font_size=20)

    if old_elo is not None:
        delta = new_elo - old_elo
        d.text((WIDTH - PAD - 30, y0 + 62), f"{delta:+d}", font=get_font(48, True),
               fill=GREEN if delta >= 0 else RED, anchor="rm")
        d.text((WIDTH - PAD - 30, y0 + 108), f"{old_elo} → {new_elo} elo",
               font=get_font(20), fill=MUTED, anchor="rm")
    else:
        d.text((WIDTH - PAD - 30, y0 + 62), str(new_elo), font=get_font(52, True), fill=TEXT, anchor="rm")
        d.text((WIDTH - PAD - 30, y0 + 108), "new player · elo", font=get_font(20), fill=MUTED, anchor="rm")

    _footer(d, height)
    return _png(img)


async def build_setelo_image(uuid, name, old_elo, new_elo):
    heads = await _fetch_heads([(uuid, name)])
    return await asyncio.to_thread(_draw_setelo, uuid, name, old_elo, new_elo, heads)


# ---------- server info ----------

def _stat_box(d, x, y, w, h, label, value, sub=None, value_size=34):
    d.rounded_rectangle([x, y, x + w, y + h], radius=14, fill=ROW_BG)
    d.text((x + w // 2, y + 30), label, font=get_font(16, True), fill=MUTED, anchor="mm")
    d.text((x + w // 2, y + (62 if sub else 70)), value, font=get_font(value_size, True),
           fill=TEXT, anchor="mm")
    if sub:
        d.text((x + w // 2, y + 92), sub, font=get_font(16), fill=MUTED, anchor="mm")


def _draw_info(info):
    box_h, gap = 110, 14
    panel_h = 62 + (len(TIERS) - 1) * 36 + 24
    height = HEADER_H + 16 + box_h + gap + box_h + gap + panel_h + 16 + FOOTER_H
    img = Image.new("RGB", (WIDTH, height), BG)
    d = ImageDraw.Draw(img)
    _header(d, "SERVER INFO", f"{GUILD_NAME} · overview")

    y = HEADER_H + 16
    bw4 = (WIDTH - 2 * PAD - 3 * gap) // 4
    avg = f"{info['avg_elo']:.0f}" if info["avg_elo"] is not None else "-"
    row1 = [("PLAYERS", str(info["players"])), ("MATCHES", str(info["matches"])),
            ("LAST 7 DAYS", str(info["recent"])), ("AVG ELO", avg)]
    for i, (label, value) in enumerate(row1):
        _stat_box(d, PAD + i * (bw4 + gap), y, bw4, box_h, label, value)

    y += box_h + gap
    bw3 = (WIDTH - 2 * PAD - 2 * gap) // 3
    top = info["top"]
    row2 = [("TOP PLAYER", top[0] if top else "-", f"{top[1]} elo" if top else None, 28),
            ("LINKED ACCOUNTS", str(info["linked"]), None, 34),
            ("VERIFIED", str(info["verified"]), None, 34)]
    for i, (label, value, sub, size) in enumerate(row2):
        _stat_box(d, PAD + i * (bw3 + gap), y, bw3, box_h, label, value, sub, size)

    y += box_h + gap
    d.rounded_rectangle([PAD, y, WIDTH - PAD, y + panel_h], radius=16, fill=ROW_BG)
    d.text((PAD + 24, y + 28), "PLAYERS PER TIER", font=get_font(16, True), fill=MUTED, anchor="lm")

    counts = info["tiers"]
    max_n = max([counts.get(t, 0) for t in TIERS] + [1])
    bar_x0 = PAD + 24 + 76
    bar_max = (WIDTH - PAD - 24 - 60) - bar_x0
    for i, t in enumerate(TIERS):
        cy = y + 62 + i * 36
        _badge(d, PAD + 24 + 30, cy, t, _tier_color(t), w=60, h=26, font_size=16)
        d.rounded_rectangle([bar_x0, cy - 9, bar_x0 + bar_max, cy + 9], radius=9, fill=TRACK)
        n = counts.get(t, 0)
        if n:
            length = max(18, int(bar_max * n / max_n))
            d.rounded_rectangle([bar_x0, cy - 9, bar_x0 + length, cy + 9], radius=9, fill=_tier_color(t))
        d.text((WIDTH - PAD - 24, cy), str(n), font=get_font(18, True), fill=TEXT, anchor="rm")

    _footer(d, height)
    return _png(img)


async def build_info_image(info):
    return await asyncio.to_thread(_draw_info, info)

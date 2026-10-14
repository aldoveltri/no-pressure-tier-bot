import asyncio
import base64
import io
import json
import os
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
SOFT = (210, 214, 224)
MUTED = (140, 146, 160)
ACCENT = (0, 208, 230)        # the accent color (titles, scores, outlines...): change it here
GREEN = (67, 201, 130)
RED = (240, 98, 98)
INK = (25, 25, 30)
PLACEHOLDER = (60, 63, 72)
TRACK = (52, 55, 64)

TIER_COLORS = {
    "S": ACCENT,
    "A": (167, 112, 255),
    "B": (74, 144, 255),
    "C": (67, 201, 130),
    "D": (140, 145, 155),
}
PODIUM = [ACCENT, (196, 202, 214), (205, 127, 50)]
_CELL_COLORS = {"text": TEXT, "green": GREEN, "red": RED, "accent": ACCENT, "muted": MUTED}

# Optional: put DejaVuSans.ttf and DejaVuSans-Bold.ttf in a "fonts" folder next to this file
# to get the same look on every machine.
FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")

_fonts = {}


def get_font(size, bold=False):
    key = (size, bold)
    if key in _fonts:
        return _fonts[key]
    if bold:
        names = [os.path.join(FONT_DIR, "DejaVuSans-Bold.ttf"), "segoeuib.ttf", "arialbd.ttf",
                 "DejaVuSans-Bold.ttf"]
    else:
        names = [os.path.join(FONT_DIR, "DejaVuSans.ttf"), "segoeui.ttf", "arial.ttf",
                 "DejaVuSans.ttf"]
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
    d.rectangle([0, HEADER_H - 5, WIDTH, HEADER_H], fill=ACCENT)
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


def _arrow(d, cx, cy, color, w=22, h=14, thick=3):
    """A right-pointing arrow centered on (cx, cy). It is drawn with shapes, so it never depends
    on the font (the arrow character shows up as an empty square in some fonts)."""
    x0 = cx - w // 2
    x1 = cx + w // 2
    head = max(6, h // 2 + 2)
    d.line([(x0, cy), (x1 - head + 1, cy)], fill=color, width=thick)
    d.polygon([(x1, cy), (x1 - head, cy - h // 2), (x1 - head, cy + h // 2)], fill=color)


def _text_with_arrow(d, x, cy, left, right, font, color, align="center"):
    """Draws '<left> [arrow] <right>'. align='center': x is the center; align='right': x is the right edge."""
    gap, aw = 10, 24
    lw = d.textlength(left, font=font)
    rw = d.textlength(right, font=font)
    total = lw + gap + aw + gap + rw
    start = x - total / 2 if align == "center" else x - total
    d.text((start, cy), left, font=font, fill=color, anchor="lm")
    _arrow(d, int(start + lw + gap + aw / 2), cy, color, w=aw, h=12, thick=2)
    d.text((start + lw + gap + aw + gap, cy), right, font=font, fill=color, anchor="lm")


def _png(img):
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


# ---------- leaderboard (general, season or archived year) ----------

# (sort key, column label, CENTER x). Labels and values are centered on the same x,
# and the last column keeps free space from the right edge of the row (872).
SORT_COLUMNS = [("elo", "ELO", 540), ("wins", "W", 610), ("losses", "L", 680),
                ("winrate", "WR", 750), ("matches", "MP", 820)]
TIER_X = 458


def _draw_leaderboard(chunk, heads, start_rank, page, pages, total, sort, title, scope, dual=False):
    top = HEADER_H + 40
    height = top + len(chunk) * ROW_H + FOOTER_H
    img = Image.new("RGB", (WIDTH, height), BG)
    d = ImageDraw.Draw(img)
    parts = [GUILD_NAME] + ([scope] if scope else []) + [f"{total} players"]
    _header(d, title, " · ".join(parts))

    # column labels (the column used for sorting is highlighted)
    sy = HEADER_H + 22
    for key, label, x in SORT_COLUMNS:
        d.text((x, sy), label, font=get_font(15, True), fill=ACCENT if key == sort else MUTED, anchor="mm")
    d.text((TIER_X, sy), "TIER", font=get_font(12, True), fill=MUTED, anchor="mm")

    for i, row in enumerate(chunk):
        rank = start_rank + i
        y0 = top + i * ROW_H
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
        d.text((hx + HEAD + 10, cy), row["ign"], font=get_font(24, True), fill=TEXT, anchor="lm")

        tier = row["tier"] or "-"
        _badge(d, TIER_X, cy, tier, _tier_color(tier), w=64, h=32, font_size=20)

        values = {
            "elo": str(row["elo"]),
            "wins": str(row["wins"]),
            "losses": str(row["losses"]),
            "winrate": f"{row['winrate'] * 100:.0f}%" if row["matches"] else "-",
            "matches": str(row["matches"]),
        }
        for key, _label, x in SORT_COLUMNS:
            d.text((x, cy), values[key], font=get_font(22, True),
                   fill=ACCENT if key == sort else TEXT, anchor="mm")

    _footer(d, height, f"Page {page + 1}/{pages}")
    return _png(img)


async def build_leaderboard_image(rows, page, per_page, sort="elo", title="GLOBAL LEADERBOARD",
                                  scope="", dual=False):
    """rows must already be sorted. 'dual' is kept only for compatibility and is ignored."""
    start = page * per_page
    chunk = rows[start:start + per_page]
    pages = max(1, (len(rows) + per_page - 1) // per_page)
    heads = await _fetch_heads([(r["uuid"], r["ign"]) for r in chunk])
    return await asyncio.to_thread(_draw_leaderboard, chunk, heads, start + 1, page, pages,
                                   len(rows), sort, title, scope)


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
        _arrow(d, cx, ty, MUTED, w=22, h=14, thick=3)
        _badge(d, cx + 50, ty, p["tier_after"], _tier_color(p["tier_after"]), w=60, h=30, font_size=18)
    else:
        _badge(d, cx, ty, p["tier_after"], _tier_color(p["tier_after"]), w=60, h=30, font_size=18)

    delta_color = GREEN if p["delta"] >= 0 else RED
    d.text((cx, y0 + 274), f"{p['delta']:+d}", font=get_font(46, True), fill=delta_color, anchor="mm")
    _text_with_arrow(d, cx, y0 + 314, str(p["before"]), f"{p['after']} elo", get_font(20), MUTED)


def _draw_match(w, l, score, match_id, date, heads, title, forfeit, first_to=3):
    top = HEADER_H + 24
    height = top + PANEL_H + 20 + FOOTER_H
    img = Image.new("RGB", (WIDTH, height), BG)
    d = ImageDraw.Draw(img)
    if title == "MATCH RESULT":
        subtitle = f"{GUILD_NAME} · " + ("forfeit" if forfeit else f"first to {first_to}")
    else:
        subtitle = f"{GUILD_NAME} · elo restored"
    _header(d, title, subtitle)

    _match_panel(img, d, PAD, top, w, ACCENT, "WINNER", heads.get(w["uuid"]))
    _match_panel(img, d, WIDTH - PAD - PANEL_W, top, l, RED,
                 "FORFEIT" if forfeit else "LOSER", heads.get(l["uuid"]))

    mid = top + PANEL_H // 2
    d.text((WIDTH // 2, mid - 40), "VS", font=get_font(34, True), fill=MUTED, anchor="mm")
    d.text((WIDTH // 2, mid + 20), score, font=get_font(54, True), fill=ACCENT, anchor="mm")
    if forfeit:
        d.text((WIDTH // 2, mid + 62), "by forfeit", font=get_font(16, True), fill=RED, anchor="mm")

    _footer(d, height, f"Match #{match_id} · {date}")
    return _png(img)


async def build_match_image(winner, loser, score, match_id, date, title="MATCH RESULT", forfeit=False,
                            first_to=3):
    heads = await _fetch_heads([(winner["uuid"], winner["name"]), (loser["uuid"], loser["name"])])
    return await asyncio.to_thread(_draw_match, winner, loser, score, match_id, date, heads, title,
                                   forfeit, first_to)


# ---------- paused match (side by side) ----------

def _pause_panel(img, d, x0, y0, p, head_data):
    cx = x0 + PANEL_W // 2
    d.rounded_rectangle([x0, y0, x0 + PANEL_W, y0 + PANEL_H], radius=18,
                        fill=ROW_BG, outline=MUTED, width=3)
    _paste_head(img, d, head_data, cx - 60, y0 + 32, 120, radius=14)
    d.text((cx, y0 + 184), p["name"], font=get_font(32, True), fill=TEXT, anchor="mm")
    _badge(d, cx, y0 + 224, p["tier"], _tier_color(p["tier"]), w=60, h=30, font_size=18)
    d.text((cx, y0 + 252), "GAMES WON", font=get_font(14, True), fill=MUTED, anchor="mm")
    d.text((cx, y0 + 284), str(p["games"]), font=get_font(40, True), fill=ACCENT, anchor="mm")
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
    _badge(d, WIDTH // 2, mid + 20, "PAUSED", ACCENT, w=104, h=34, font_size=18)

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
        d.rounded_rectangle([PAD, y0, PAD + 8, y0 + ROW_H - 8], radius=4, fill=ACCENT)

        d.text((PAD + 24, cy - 10), f"Paused #{e['id']}", font=get_font(18, True), fill=TEXT, anchor="lm")
        d.text((PAD + 24, cy + 14), e["date"], font=get_font(15), fill=MUTED, anchor="lm")

        _paste_head(img, d, heads.get(e["p1_uuid"]), 178, cy - 20, 40)
        d.text((226, cy), e["p1"], font=get_font(20, True), fill=TEXT, anchor="lm")

        d.text((450, cy), f"{e['g1']} - {e['g2']}", font=get_font(22, True), fill=ACCENT, anchor="mm")

        _paste_head(img, d, heads.get(e["p2_uuid"]), 494, cy - 20, 40)
        d.text((542, cy), e["p2"], font=get_font(20, True), fill=TEXT, anchor="lm")

        d.text((WIDTH - PAD - 28, cy), "to continue", font=get_font(15), fill=MUTED, anchor="rm")

    note = f"Showing the latest {len(entries)}" if total > len(entries) else None
    _footer(d, height, note)
    return _png(img)


async def build_paused_list_image(entries, total):
    items = []
    for e in entries:

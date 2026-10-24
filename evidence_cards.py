"""Pictures for the cheating reports (summary card), the final verdict and the Urchin alerts.

Only drawing, no Discord. Same colours, fonts and header as the other cards (graphics.py).
The summary card has: state, progress stepper, suspect (skin, name, flag), reason and the votes
with the names of who voted. The evidence itself (replay codes, links, screenshots, videos) stays
in the message, so it can be copied and viewed.
"""
from datetime import datetime

from PIL import Image, ImageDraw

import graphics as g
from config import GUILD_NAME

GOLD = (241, 196, 15)
GREY = (118, 124, 138)
PANEL2 = (50, 53, 62)

W = g.WIDTH
PADX = g.PAD
INNER = W - 2 * PADX

STATES = {
    "draft": ("DRAFT", GOLD),
    "voting": ("VOTING OPEN", g.ACCENT),
    "verdict": ("AWAITING VERDICT", GOLD),
    "blacklisted": ("BLACKLISTED", g.RED),
    "insufficient": ("INSUFFICIENT", GREY),
    "cancelled": ("CANCELLED", GREY),
}
STEPS = ["Evidence", "Confirmed", "Voting", "Verdict"]


def clean(text):
    """Removes emoji and symbols the card font cannot draw."""
    out = "".join(c for c in str(text) if ord(c) <= 0xFFFF and not 0x2600 <= ord(c) <= 0x27BF)
    return out.strip() or "?"


def fit(d, text, font, width):
    if d.textlength(text, font=font) <= width:
        return text
    while text and d.textlength(text + "...", font=font) > width:
        text = text[:-1]
    return text + "..."


def wrap(d, text, font, width):
    """Word wrap that also cuts words longer than the line (links)."""
    lines = []
    for para in str(text).splitlines() or [""]:
        cur = ""
        for word in para.split(" "):
            while d.textlength(word, font=font) > width:
                cut = len(word)
                while cut > 1 and d.textlength(word[:cut], font=font) > width:
                    cut -= 1
                if cur:
                    lines.append(cur)
                    cur = ""
                lines.append(word[:cut])
                word = word[cut:]
            trial = f"{cur} {word}".strip() if cur else word
            if cur and d.textlength(trial, font=font) > width:
                lines.append(cur)
                cur = word
            else:
                cur = trial
        lines.append(cur)
    return lines


def pill(d, right_x, cy, text, color, font_size=16, pad=18, h=34):
    font = g.get_font(font_size, True)
    w = int(d.textlength(text, font=font)) + 2 * pad
    d.rounded_rectangle([right_x - w, cy - h // 2, right_x, cy + h // 2], radius=h // 2, fill=color)
    d.text((right_x - w // 2, cy), text, font=font, fill=g.INK, anchor="mm")
    return w


def check(d, cx, cy, color, s=1.0):
    d.line([(cx - 6 * s, cy), (cx - 2 * s, cy + 5 * s), (cx + 7 * s, cy - 6 * s)], fill=color,
           width=max(2, int(3 * s)), joint="curve")


def cross(d, cx, cy, color, s=1.0):
    k = 6 * s
    d.line([(cx - k, cy - k), (cx + k, cy + k)], fill=color, width=max(2, int(3 * s)))
    d.line([(cx - k, cy + k), (cx + k, cy - k)], fill=color, width=max(2, int(3 * s)))


def lock(d, cx, cy, color):
    d.rounded_rectangle([cx - 9, cy - 2, cx + 9, cy + 10], radius=3, fill=color)
    d.arc([cx - 6, cy - 12, cx + 6, cy + 4], 180, 360, fill=color, width=3)
    d.line([(cx - 6, cy - 5), (cx - 6, cy - 1)], fill=color, width=3)
    d.line([(cx + 6, cy - 5), (cx + 6, cy - 1)], fill=color, width=3)


def panel(d, y, h, color=None):
    d.rounded_rectangle([PADX, y, W - PADX, y + h], radius=16, fill=g.ROW_BG)
    if color:
        d.rounded_rectangle([PADX, y, PADX + 8, y + h], radius=4, fill=color)


def label(d, x, y, text, color=g.ACCENT):
    d.rounded_rectangle([x, y - 10, x + 5, y + 10], radius=2, fill=color)
    d.text((x + 16, y), text, font=g.get_font(15, True), fill=g.MUTED, anchor="lm")


def short_date(iso):
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%m/%d/%Y")
    except Exception:
        return ""


def stepper(d, y, phase, draw):
    h = 74
    if not draw:
        return y + h + 14
    current = {"draft": 0, "voting": 2, "verdict": 3}.get(phase)
    done_upto = {"draft": 0, "voting": 2, "verdict": 3, "blacklisted": 4, "insufficient": 4, "cancelled": 0}[phase]
    cancelled = phase == "cancelled"
    x0, x1 = PADX + 70, W - PADX - 70
    cy = y + 22
    xs = [x0 + i * (x1 - x0) // 3 for i in range(4)]
    for i in range(3):
        on = i < done_upto and not cancelled
        d.line([(xs[i] + 22, cy), (xs[i + 1] - 22, cy)], fill=g.ACCENT if on else g.TRACK, width=4)
    for i, cx in enumerate(xs):
        is_done = i < done_upto and not cancelled
        is_cur = i == current and not cancelled
        if is_done:
            d.ellipse([cx - 16, cy - 16, cx + 16, cy + 16], fill=g.ACCENT)
            check(d, cx, cy, g.INK, 0.9)
        elif is_cur:
            d.ellipse([cx - 16, cy - 16, cx + 16, cy + 16], fill=g.ROW_BG, outline=g.ACCENT, width=4)
            d.ellipse([cx - 5, cy - 5, cx + 5, cy + 5], fill=g.ACCENT)
        else:
            d.ellipse([cx - 16, cy - 16, cx + 16, cy + 16], fill=g.ROW_BG, outline=g.TRACK, width=3)
            d.text((cx, cy), str(i + 1), font=g.get_font(15, True), fill=g.MUTED, anchor="mm")
        d.text((cx, cy + 34), STEPS[i], font=g.get_font(15, True),
               fill=g.TEXT if (is_done or is_cur) else g.MUTED, anchor="mm")
    return y + h + 14


def suspect(img, d, y, ign, head, color, sub, tags, draw):
    """tags = [(text, color)] small badges under the name."""
    h = 176
    if draw:
        panel(d, y, h, color)
        g._paste_head(img, d, head, PADX + 34, y + 23, 130, radius=16)
        d.rounded_rectangle([PADX + 34, y + 23, PADX + 34 + 130, y + 23 + 130], radius=16, outline=color, width=3)
        nx = PADX + 34 + 130 + 30
        g.name_flag(img, d, nx, y + 58, clean(ign), 46, h=38, max_w=W - PADX - 30 - nx)
        d.text((nx, y + 100), fit(d, sub, g.get_font(18), W - PADX - 30 - nx), font=g.get_font(18),
               fill=g.MUTED, anchor="lm")
        bx = nx
        for text, col in tags:
            f = g.get_font(14, True)
            w = int(d.textlength(text, font=f)) + 28
            d.rounded_rectangle([bx, y + 122, bx + w, y + 148], radius=13, fill=col)
            d.text((bx + w // 2, y + 135), text, font=f, fill=g.INK if col != PANEL2 else g.MUTED, anchor="mm")
            bx += w + 10
    return y + h + 14


def reason_section(d, y, reason, draw):
    f = g.get_font(19)
    lines = wrap(d, clean(reason), f, INNER - 56) if reason else []
    if len(lines) > 7:
        lines = lines[:7]
        lines[-1] = fit(d, lines[-1] + " ...", f, INNER - 56)
    h = 58 + max(len(lines), 1) * 28 + 14
    if draw:
        panel(d, y, h)
        label(d, PADX + 24, y + 28, "REASON")
        if not lines:
            d.rounded_rectangle([PADX + 24, y + 52, W - PADX - 24, y + 90], radius=10, outline=g.TRACK, width=2)
            d.text((W // 2, y + 71), "Nothing added yet", font=g.get_font(17), fill=g.MUTED, anchor="mm")
        for i, line in enumerate(lines):
            d.text((PADX + 28, y + 56 + i * 28), line, font=f, fill=g.SOFT, anchor="lt")
    return y + h + 14


def chips(d, names, x, y, width, color, draw):
    font = g.get_font(15, True)
    h = 30
    shown = names[:24]
    items = [clean(n) for n in shown] + ([f"+{len(names) - len(shown)} more"] if len(names) > len(shown) else [])
    cx, cy = x, y
    for t in items:
        t = fit(d, t, font, width - 26)
        w = int(d.textlength(t, font=font)) + 26
        if cx > x and cx + w > x + width:
            cx, cy = x, cy + h + 8
        if draw:
            d.rounded_rectangle([cx, cy, cx + w, cy + h], radius=h // 2, fill=PANEL2, outline=color, width=2)
            d.text((cx + w // 2, cy + h // 2), t, font=font, fill=g.TEXT, anchor="mm")
        cx += w + 8
    return cy + h


def votes_section(d, y, yes, no, locked, draw, final=False):
    """Votes with the names of the voters (votes are public). Locked until the report is confirmed."""
    if locked:
        h = 92
        if draw:
            panel(d, y, h)
            label(d, PADX + 24, y + 28, "VOTES")
            lock(d, PADX + 42, y + 64, g.MUTED)
            d.text((PADX + 66, y + 64), "Voting opens when staff confirm the evidence and the reason.",
                   font=g.get_font(17), fill=g.MUTED, anchor="lm")
        return y + h + 14
    col_w = (INNER - 48 - 24) // 2
    ny, nn = len(yes), len(no)
    body = max(chips(d, yes, 0, 0, col_w, g.GREEN, False) if yes else 24,
               chips(d, no, 0, 0, col_w, g.RED, False) if no else 24)
    h = 140 + body + 18
    if draw:
        panel(d, y, h)
        label(d, PADX + 24, y + 28, "VOTES" + (" (FINAL)" if final else ""))
        total = ny + nn
        d.text((W - PADX - 24, y + 28), f"{total} vote{'s' if total != 1 else ''} · names are public",
               font=g.get_font(15), fill=g.MUTED, anchor="rm")
        bx0, bx1, by = PADX + 24, W - PADX - 24, y + 52
        d.rounded_rectangle([bx0, by, bx1, by + 22], radius=11, fill=g.TRACK)
        if total:
            split = bx0 + int((bx1 - bx0) * ny / total)
            if ny:
                d.rounded_rectangle([bx0, by, max(bx0 + 22, split), by + 22], radius=11, fill=g.GREEN)
            if nn:
                d.rounded_rectangle([min(split, bx1 - 22), by, bx1, by + 22], radius=11, fill=g.RED)
            if ny and nn:
                d.rectangle([split - 1, by, split + 1, by + 22], fill=g.ROW_BG)
        cy = y + 96
        for i, (names, color, text, n) in enumerate(((yes, g.GREEN, "SUFFICIENT", ny), (no, g.RED, "INSUFFICIENT", nn))):
            cx = PADX + 24 + i * (col_w + 24)
            (check if i == 0 else cross)(d, cx + 10, cy + 12, color, 0.9)
            d.text((cx + 30, cy + 12), text, font=g.get_font(17, True), fill=color, anchor="lm")
            d.text((cx + col_w, cy + 12), str(n), font=g.get_font(30, True), fill=g.TEXT, anchor="rm")
            if names:
                chips(d, names, cx, cy + 44, col_w, color, True)
            else:
                d.text((cx, cy + 58), "No votes", font=g.get_font(16), fill=g.MUTED, anchor="lm")
    return y + h + 14


def _finish(run, title, subtitle, pill_text, pill_color, footer):
    scratch = Image.new("RGB", (W, 10))
    height = run(scratch, ImageDraw.Draw(scratch), False) + g.FOOTER_H - 6
    img = Image.new("RGB", (W, height), g.BG)
    d = ImageDraw.Draw(img)
    g._header(d, title, subtitle)
    if pill_text:
        pill(d, W - PADX, 38, pill_text, pill_color)
    run(img, d, True)
    g._footer(d, height, footer)
    return g._png(img)


def draw_report(row, yes, no, head, phase, evidence_count=0):
    """The summary card. yes / no = names of who voted. phase: draft|voting|verdict|blacklisted|insufficient|cancelled."""
    text, color = STATES[phase]
    sub = f"Reported by {clean(row['created_by'] or '?')} · {evidence_count} evidence item{'s' if evidence_count != 1 else ''}"
    tags = [("SUSPECT", PANEL2)] + ([(clean(row["source"]).upper(), GOLD)] if row["source"] else [])

    def run(img, d, draw):
        y = g.HEADER_H + 20
        y = stepper(d, y, phase, draw)
        y = suspect(img, d, y, row["ign"], head, color, sub, tags, draw)
        y = reason_section(d, y, row["reason"], draw)
        if phase != "cancelled" or yes or no:
            y = votes_section(d, y, yes, no, phase == "draft", draw,
                              final=phase in ("blacklisted", "insufficient", "cancelled"))
        return y
    date = short_date(row["created_at"] or "")
    return _finish(run, f"CHEATING REPORT #{row['id']}", f"{GUILD_NAME} · evidence", text, color,
                   f"Opened {date}" if date else None)


def draw_verdict(row, phase, verdict_text, details, yes, no, head, by):
    """The final verdict. details = [(label, text)]."""
    color = STATES[phase][1]

    def run(img, d, draw):
        y = g.HEADER_H + 20
        h = 176
        if draw:
            panel(d, y, h, color)
            g._paste_head(img, d, head, PADX + 34, y + 23, 130, radius=16)
            d.rounded_rectangle([PADX + 34, y + 23, PADX + 34 + 130, y + 23 + 130], radius=16, outline=color, width=3)
            nx = PADX + 34 + 130 + 30
            g.name_flag(img, d, nx, y + 52, clean(row["ign"]), 44, h=36, max_w=W - PADX - 30 - nx)
            vf = g.get_font(34, True)
            d.text((nx, y + 108), fit(d, verdict_text.upper(), vf, W - PADX - 30 - nx), font=vf, fill=color,
                   anchor="lm")
            d.text((nx, y + 148), f"Decided by {clean(by)}", font=g.get_font(17), fill=g.MUTED, anchor="lm")
        y += h + 14
        det = [(k, v) for k, v in details if v]
        if det:
            f = g.get_font(19)
            blocks = [(k, wrap(d, clean(v), f, INNER - 56)[:6]) for k, v in det]
            dh = 24 + sum(34 + len(ls) * 28 for _, ls in blocks) + 4
            if draw:
                panel(d, y, dh)
                yy = y + 24
                for k, ls in blocks:
                    label(d, PADX + 24, yy + 4, k.upper())
                    for i, line in enumerate(ls):
                        d.text((PADX + 28, yy + 26 + i * 28), line, font=f, fill=g.SOFT, anchor="lt")
                    yy += 34 + len(ls) * 28
            y += dh + 14
        if phase != "cancelled" or yes or no:
            y = votes_section(d, y, yes, no, False, draw, final=True)
        return y
    return _finish(run, "FINAL VERDICT", f"{GUILD_NAME} · report #{row['id']}", STATES[phase][0], color, None)


def draw_urchin(name, tags, head):
    """Alert for a tagged guild member. tags = [(kind, reason)]."""
    f = g.get_font(19)

    def run(img, d, draw):
        y = g.HEADER_H + 20
        h = 176
        if draw:
            panel(d, y, h, GOLD)
            g._paste_head(img, d, head, PADX + 34, y + 23, 130, radius=16)
            d.rounded_rectangle([PADX + 34, y + 23, PADX + 34 + 130, y + 23 + 130], radius=16, outline=GOLD, width=3)
            nx = PADX + 34 + 130 + 30
            g.name_flag(img, d, nx, y + 58, clean(name), 46, h=38, max_w=W - PADX - 30 - nx)
            d.text((nx, y + 100), "Guild member tagged by the community", font=g.get_font(18), fill=g.MUTED,
                   anchor="lm")
            t = f"{len(tags)} TAG{'S' if len(tags) != 1 else ''}"
            tw = int(d.textlength(t, font=g.get_font(14, True))) + 28
            d.rounded_rectangle([nx, y + 122, nx + tw, y + 148], radius=13, fill=GOLD)
            d.text((nx + tw // 2, y + 135), t, font=g.get_font(14, True), fill=g.INK, anchor="mm")
        y += h + 14
        blocks = [(clean(k), wrap(d, clean(r) if r else "No reason given", f, INNER - 56)[:4])
                  for k, r in tags[:6]]
        ph = 52 + sum(34 + len(ls) * 27 for _, ls in blocks) + 6
        if draw:
            panel(d, y, ph)
            label(d, PADX + 24, y + 28, "TAGS", GOLD)
            yy = y + 54
            for k, ls in blocks:
                kw = int(d.textlength(k.upper(), font=g.get_font(14, True))) + 28
                d.rounded_rectangle([PADX + 24, yy, PADX + 24 + kw, yy + 26], radius=13, fill=GOLD)
                d.text((PADX + 24 + kw // 2, yy + 13), k.upper(), font=g.get_font(14, True), fill=g.INK, anchor="mm")
                for i, line in enumerate(ls):
                    d.text((PADX + 28, yy + 34 + i * 27), line, font=f, fill=g.SOFT, anchor="lt")
                yy += 34 + len(ls) * 27
        y += ph + 14
        return y
    return _finish(run, "URCHIN ALERT", f"{GUILD_NAME} · tagged player", "TAGGED", GOLD,
                   "Press the button below to open a cheating report")

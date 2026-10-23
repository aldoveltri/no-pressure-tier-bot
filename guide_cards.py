"""Draws a page of /guide as an image: clear sections, command cards, tables.

Uses the colours and fonts of graphics.py, so it always matches the other cards.
A page is a list of blocks: heading, paragraph, bullet, command, table (see guide.py).
"""
from PIL import Image, ImageDraw

import graphics as g

GROUPS = {"/breaklist", "/gexp", "/excuse", "/country", "/mute", "/blacklist", "/evidence", "/urchin"}

SIDE = 30                      # inner padding of the cards
GAP = 14                       # space between blocks


def _wrap(d, text, font, width):
    lines, cur = [], ""
    for word in text.split():
        trial = f"{cur} {word}".strip()
        if cur and d.textlength(trial, font=font) > width:
            lines.append(cur)
            cur = word
        else:
            cur = trial
    if cur:
        lines.append(cur)
    return lines or [""]


def _split_command(cmd):
    """'/breaklist add ign [reason]' -> ('/breaklist add', 'ign [reason]')."""
    parts = cmd.split(" ")
    take = 2 if parts[0] in GROUPS and len(parts) > 1 else 1
    return " ".join(parts[:take]), " ".join(parts[take:])


def render_page(title, subtitle, blocks, footer=""):
    scratch = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    full_w = g.WIDTH - 2 * g.PAD
    f_head, f_text, f_small = g.get_font(20, True), g.get_font(18), g.get_font(17)
    f_cmd, f_args = g.get_font(20, True), g.get_font(17)
    f_th, f_td = g.get_font(14, True), g.get_font(18, True)

    items = []                  # (kind, data, height)
    for i, b in enumerate(blocks):
        kind = b["type"]
        if kind == "heading":
            items.append(("heading", b["text"], 58 if i else 40))
        elif kind == "paragraph":
            lines = _wrap(scratch, b["text"], f_text, full_w - 8)
            items.append(("paragraph", lines, len(lines) * 27 + 4))
        elif kind == "bullet":
            lines = _wrap(scratch, b["text"], f_text, full_w - 44)
            items.append(("bullet", lines, len(lines) * 27 + 4))
        elif kind == "command":
            name, args = _split_command(b["cmd"])
            avail = full_w - 2 * SIDE - 8
            lines = _wrap(scratch, b["text"], f_small, avail)
            args_lines = None
            if args and scratch.textlength(name, font=f_cmd) + 12 + scratch.textlength(args, font=f_args) > avail:
                args_lines = _wrap(scratch, args.replace(" ", " "), f_args, avail)
            extra = len(args_lines) * 23 if args_lines else 0
            items.append(("command", (name, args, lines, args_lines), 20 + 30 + extra + len(lines) * 25 + 18))
        elif kind == "table":
            items.append(("table", b, 46 + len(b["rows"]) * 46 + 6))
    body = sum(h + GAP for _, _, h in items)
    top = g.HEADER_H + 26
    height = max(top + body + g.FOOTER_H, 520)

    img = Image.new("RGB", (g.WIDTH, height), g.BG)
    d = ImageDraw.Draw(img)
    g._header(d, title, subtitle)
    y = top
    colors = {"text": g.TEXT, "green": g.GREEN, "red": g.RED, "accent": g.ACCENT}

    for kind, data, h in items:
        if kind == "heading":
            hy = y + (h - 36 if h > 40 else 6)
            d.rounded_rectangle([g.PAD, hy, g.PAD + 6, hy + 24], radius=3, fill=g.ACCENT)
            d.text((g.PAD + 20, hy + 12), data.upper(), font=f_head, fill=g.ACCENT, anchor="lm")
            d.line([(g.PAD, hy + 34), (g.WIDTH - g.PAD, hy + 34)], fill=g.TRACK, width=1)
        elif kind == "paragraph":
            for k, line in enumerate(data):
                d.text((g.PAD + 4, y + k * 27), line, font=f_text, fill=g.SOFT, anchor="lt")
        elif kind == "bullet":
            d.ellipse([g.PAD + 10, y + 9, g.PAD + 18, y + 17], fill=g.ACCENT)
            for k, line in enumerate(data):
                d.text((g.PAD + 36, y + k * 27), line, font=f_text, fill=g.SOFT, anchor="lt")
        elif kind == "command":
            name, args, lines, args_lines = data
            d.rounded_rectangle([g.PAD, y, g.WIDTH - g.PAD, y + h], radius=14, fill=g.ROW_BG)
            d.rounded_rectangle([g.PAD, y, g.PAD + 7, y + h], radius=4, fill=g.ACCENT)
            x = g.PAD + SIDE
            d.text((x, y + 30), name, font=f_cmd, fill=g.ACCENT, anchor="lm")
            ty = y + 56
            if args_lines:
                for k, line in enumerate(args_lines):
                    d.text((x, y + 56 + k * 23), line, font=f_args, fill=g.MUTED, anchor="lt")
                ty += len(args_lines) * 23
            elif args:
                ax = x + int(d.textlength(name, font=f_cmd)) + 12
                d.text((ax, y + 30), args, font=f_args, fill=g.MUTED, anchor="lm")
            for k, line in enumerate(lines):
                d.text((x, ty + k * 25), line, font=f_small, fill=g.SOFT, anchor="lt")
        elif kind == "table":
            cols, rows, widths = data["cols"], data["rows"], data["widths"]
            ccolors = data.get("colors") or ["text"] * len(cols)
            xs, x = [], g.PAD
            for w in widths:
                xs.append(x)
                x += int(full_w * w)
            for c, label in enumerate(cols):
                d.text((xs[c] + 18, y + 18), label.upper(), font=f_th, fill=g.MUTED, anchor="lm")
            ry = y + 38
            for r, row in enumerate(rows):
                d.rounded_rectangle([g.PAD, ry, g.WIDTH - g.PAD, ry + 40], radius=10,
                                    fill=g.ROW_BG if r % 2 == 0 else g.BG)
                for c, cell in enumerate(row):
                    d.text((xs[c] + 18, ry + 20), str(cell), font=f_td, fill=colors.get(ccolors[c], g.TEXT), anchor="lm")
                ry += 46
        y += h + GAP
    g._footer(d, height, footer)
    return g._png(img)

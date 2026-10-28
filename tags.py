"""Active tag vote, in two steps, with an image that updates itself.
1) The staff picks the active players online who vote. They answer: do we change the active tag? (majority wins)
2) If yes, they suggest tags (as many as they like) and vote for the ones they want (as many as they like).
   The staff closes the vote: the tag with the most votes wins and the guild master is pinged to change it."""
import asyncio
import json
import logging
import re
from typing import Optional

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands
from PIL import Image, ImageDraw

import breaklist as bl
import graphics as g
from breaklist import q_one, q_all, q_run, esc, now_utc, iso

log = logging.getLogger("tags")
SCHEMA = """
CREATE TABLE IF NOT EXISTS tag_props (
    id INTEGER PRIMARY KEY AUTOINCREMENT, tag TEXT NOT NULL, proposer_id TEXT, proposer_name TEXT,
    voters TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL DEFAULT 'open', note TEXT,
    channel_id TEXT, message_id TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS tag_ballots (
    prop_id INTEGER NOT NULL, discord_id TEXT NOT NULL, name TEXT, vote TEXT NOT NULL, PRIMARY KEY (prop_id, discord_id)
);
CREATE TABLE IF NOT EXISTS tag_polls (
    id INTEGER PRIMARY KEY AUTOINCREMENT, status TEXT NOT NULL DEFAULT 'picking', voters TEXT NOT NULL DEFAULT '[]',
    started_by TEXT, started_name TEXT, channel_id TEXT, message_id TEXT, created_at TEXT, winner TEXT, note TEXT
);
CREATE TABLE IF NOT EXISTS tag_answers (
    poll_id INTEGER NOT NULL, discord_id TEXT NOT NULL, answer TEXT NOT NULL, PRIMARY KEY (poll_id, discord_id)
);
CREATE TABLE IF NOT EXISTS tag_options (
    poll_id INTEGER NOT NULL, tag TEXT NOT NULL, by_id TEXT, by_name TEXT, PRIMARY KEY (poll_id, tag)
);
CREATE TABLE IF NOT EXISTS tag_choices (
    poll_id INTEGER NOT NULL, discord_id TEXT NOT NULL, tag TEXT NOT NULL, PRIMARY KEY (poll_id, discord_id, tag)
);
"""
TAG_RE = re.compile(r"^[A-Za-z0-9]{1,6}$")
MAX_OPTIONS = 25


def active_rank() -> str:
    return (bl._env("ACTIVE_RANK_NAME") or "Active").strip().lower()


async def is_active(core, discord_id):
    """(True/False, reason or name): the Discord account is linked to a guild member with the Active rank."""
    row = await q_one("SELECT uuid, ign FROM links WHERE discord_id = ?", (str(discord_id),))
    if not row:
        return False, "your Discord account is not linked to a Minecraft account (ask the staff to link it)"
    await core.guild_members()
    seen = await q_one("SELECT rank FROM guild_seen WHERE uuid = ? AND COALESCE(in_guild, 1) = 1", (row["uuid"],))
    if not seen:
        return False, f"{row['ign']} is not in the guild"
    if (seen["rank"] or "").lower() != active_rank():
        return False, f"{row['ign']} doesn't have the {bl._env('ACTIVE_RANK_NAME') or 'Active'} rank"
    return True, row["ign"]


# ---------------------------------------------------------------- the numbers

def phase1_state(voters, answers):
    """('open'|'yes'|'no', yes, no): the majority of the picked voters decides, a tie is a no."""
    n_ = len(voters)
    ans = {a["discord_id"]: a["answer"] for a in answers if a["discord_id"] in voters}
    yes = sum(1 for v in ans.values() if v == "y")
    no = sum(1 for v in ans.values() if v == "n")
    left = n_ - yes - no
    if n_ and yes * 2 > n_:
        return "yes", yes, no
    if n_ and (yes + left) * 2 <= n_:
        return "no", yes, no
    return "open", yes, no


async def option_counts(pid):
    """[(tag, votes, by_name)] sorted by votes (then by the order the tags were suggested)."""
    opts = await q_all("SELECT rowid AS r, tag, by_name FROM tag_options WHERE poll_id = ? ORDER BY rowid", (pid,))
    rows = await q_all("SELECT tag, COUNT(*) AS c FROM tag_choices WHERE poll_id = ? GROUP BY tag", (pid,))
    count = {r["tag"]: r["c"] for r in rows}
    out = [(o["tag"], count.get(o["tag"], 0), o["by_name"] or "?") for o in opts]
    out.sort(key=lambda x: -x[1])
    return out


def leaders(options):
    if not options or options[0][1] == 0:
        return []
    return [o for o in options if o[1] == options[0][1]]


async def names_of(bot, ids):
    """{discord id: name}: the linked Minecraft name, else the Discord name."""
    out = {}
    for i in ids:
        row = await q_one("SELECT ign FROM links WHERE discord_id = ?", (str(i),))
        if row and row["ign"]:
            out[i] = row["ign"]
            continue
        u = bot.get_user(int(i))
        out[i] = (u.display_name if u else None) or f"user {str(i)[-4:]}"
    return out


# ---------------------------------------------------------------- the image

def _fit(d, text, font, width):
    text = str(text)
    if d.textlength(text, font=font) <= width:
        return text
    while text and d.textlength(text + "…", font=font) > width:
        text = text[:-1]
    return text + "…"


def _pills(d, x0, y, width, items, font):
    """Draws (text, color) pills in rows. Returns the y below the last row."""
    cx, cy = x0, y
    for text, color in items:
        w = int(d.textlength(text, font=font)) + 34
        if cx + w > x0 + width:
            cx, cy = x0, cy + 40
        d.rounded_rectangle([cx, cy, cx + w, cy + 32], radius=12, fill=g.TRACK)
        d.ellipse([cx + 10, cy + 11, cx + 20, cy + 21], fill=color)
        d.text((cx + 28, cy + 16), text, font=font, fill=g.TEXT, anchor="lm")
        cx += w + 8
    return cy + 40


def _pill_height(d, width, items, font):
    cx, rows = 0, 1
    for text, _ in items:
        w = int(d.textlength(text, font=font)) + 34
        if cx + w > width:
            cx, rows = 0, rows + 1
        cx += w + 8
    return rows * 40


def draw_poll(p, voters, yes, no, options, who, final_text=None):
    """p: the poll row, voters: [(name, state)] state in 'y','n','w' (phase 1) or 'v','w' (phase 2)."""
    status = p["status"]
    probe = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    pill_font = g.get_font(16, True)
    width = g.WIDTH - 2 * g.PAD - 44
    colors = {"y": g.GREEN, "n": g.RED, "w": g.MUTED, "v": g.GREEN}
    pills = [(name, colors[state]) for name, state in voters]
    pills_h = _pill_height(probe, width, pills, pill_font) if pills else 0
    shown = options[:10]
    rows_h = {"phase1": 2 * 64, "phase2": max(1, len(shown)) * 64}.get(status, 0)
    banner_h = 100 if status in ("accepted", "rejected", "cancelled") else 0
    top = g.HEADER_H + 16
    height = top + 100 + 14 + (rows_h + 28 if rows_h else 0) + (banner_h + 14 if banner_h else 0) + \
        (pills_h + 62 if pills else 0) + 16 + g.FOOTER_H
    img = Image.new("RGB", (g.WIDTH, height), g.BG)
    d = ImageDraw.Draw(img)
    sub = {"picking": "Waiting for the staff to pick who votes", "phase1": "Step 1 of 2 · do we change the active tag?",
           "phase2": "Step 2 of 2 · suggest and vote for the tags", "accepted": "Vote closed",
           "rejected": "Vote closed", "cancelled": "Vote cancelled"}[status]
    g._header(d, "ACTIVE TAG VOTE", f"{bl.GUILD_NAME_DISPLAY} · {sub}")

    d.rounded_rectangle([g.PAD, top, g.WIDTH - g.PAD, top + 100], radius=16, fill=g.ROW_BG)
    d.rounded_rectangle([g.PAD, top, g.PAD + 8, top + 100], radius=4, fill=g.ACCENT)
    question = {"picking": "Who votes?", "phase1": "Should we change the active tag?",
                "phase2": "Which tag do you want?", "accepted": "Vote closed", "rejected": "Vote closed",
                "cancelled": "Vote cancelled"}[status]
    d.text((g.PAD + 34, top + 36), question, font=g.get_font(34, True), fill=g.TEXT, anchor="lm")
    d.text((g.PAD + 34, top + 76), f"Started by {who} · vote #{p['id']}", font=g.get_font(16), fill=g.MUTED, anchor="lm")
    y = top + 114

    if status == "phase1":
        total = max(1, len(voters))
        d.rounded_rectangle([g.PAD, y, g.WIDTH - g.PAD, y + rows_h + 14], radius=16, fill=g.ROW_BG)
        for i, (label, count, color) in enumerate((("YES", yes, g.GREEN), ("NO", no, g.RED))):
            by = y + 20 + i * 64
            d.text((g.PAD + 28, by + 16), label, font=g.get_font(22, True), fill=color, anchor="lm")
            bx0, bx1 = g.PAD + 110, g.WIDTH - g.PAD - 110
            d.rounded_rectangle([bx0, by + 4, bx1, by + 28], radius=12, fill=g.TRACK)
            if count:
                d.rounded_rectangle([bx0, by + 4, bx0 + max(24, int((bx1 - bx0) * count / total)), by + 28], radius=12, fill=color)
            d.text((g.WIDTH - g.PAD - 28, by + 16), f"{count}", font=g.get_font(24, True), fill=g.TEXT, anchor="rm")
        y += rows_h + 28
    elif status == "phase2":
        d.rounded_rectangle([g.PAD, y, g.WIDTH - g.PAD, y + rows_h + 14], radius=16, fill=g.ROW_BG)
        if not shown:
            d.text((g.PAD + 28, y + 36), "No tags suggested yet: press Suggest a tag", font=g.get_font(20), fill=g.MUTED, anchor="lm")
        best = max([c for _, c, _ in shown] + [1])
        lead = {t for t, c, _ in leaders(options)}
        for i, (tag, count, by) in enumerate(shown):
            by_y = y + 20 + i * 64
            color = g.GREEN if tag in lead else g.ACCENT
            d.text((g.PAD + 28, by_y + 16), f"[{tag}]", font=g.get_font(24, True), fill=color, anchor="lm")
            bx0, bx1 = g.PAD + 190, g.WIDTH - g.PAD - 110
            d.rounded_rectangle([bx0, by_y + 4, bx1, by_y + 28], radius=12, fill=g.TRACK)
            if count:
                d.rounded_rectangle([bx0, by_y + 4, bx0 + max(24, int((bx1 - bx0) * count / best)), by_y + 28], radius=12, fill=color)
            d.text((bx0, by_y + 46), _fit(d, f"suggested by {by}", g.get_font(13), bx1 - bx0), font=g.get_font(13), fill=g.MUTED, anchor="lm")
            d.text((g.WIDTH - g.PAD - 28, by_y + 16), f"{count}", font=g.get_font(24, True), fill=g.TEXT, anchor="rm")
        y += rows_h + 28
        if len(options) > len(shown):
            d.text((g.WIDTH // 2, y - 8), f"+{len(options) - len(shown)} more tags", font=g.get_font(14), fill=g.MUTED, anchor="mm")

    if banner_h:
        color = {"accepted": g.GREEN, "rejected": g.RED, "cancelled": g.MUTED}[status]
        d.rounded_rectangle([g.PAD, y, g.WIDTH - g.PAD, y + banner_h], radius=16, fill=color)
        d.text((g.WIDTH // 2, y + banner_h // 2), final_text or status.upper(),
               font=g.get_font(34, True), fill=g.BG, anchor="mm")
        y += banner_h + 14

    if pills:
        label = {"phase1": "VOTERS (green yes · red no · grey waiting)", "phase2": "VOTERS (green already voted)",
                 "picking": "VOTERS"}.get(status, "VOTERS")
        d.rounded_rectangle([g.PAD, y, g.WIDTH - g.PAD, y + pills_h + 48], radius=16, fill=g.ROW_BG)
        d.text((g.PAD + 22, y + 24), label, font=g.get_font(14, True), fill=g.MUTED, anchor="lm")
        _pills(d, g.PAD + 22, y + 42, width, pills, pill_font)
    g._footer(d, height, "Voters are the active players picked by the staff")
    return g._png(img)


# ---------------------------------------------------------------- building the message

async def poll_message(bot, pid):
    """(content, file, view) of the poll message in its current state."""
    p = await q_one("SELECT * FROM tag_polls WHERE id = ?", (pid,))
    voters = json.loads(p["voters"])
    names = await names_of(bot, voters)
    answers = await q_all("SELECT * FROM tag_answers WHERE poll_id = ?", (pid,))
    options = await option_counts(pid)
    state, yes, no = phase1_state(voters, answers)
    pills = []
    if p["status"] in ("picking", "phase1", "accepted", "rejected", "cancelled") and p["status"] != "phase2":
        said = {a["discord_id"]: a["answer"] for a in answers}
        pills = [(names[v], (said.get(v) or "w")) for v in voters]
    if p["status"] == "phase2" or (p["status"] in ("accepted",) and options):
        chose = {r["discord_id"] for r in await q_all("SELECT DISTINCT discord_id FROM tag_choices WHERE poll_id = ?", (pid,))}
        pills = [(names[v], "v" if v in chose else "w") for v in voters]
    final = None
    if p["status"] == "accepted":
        final = f"NEW TAG: [{p['winner']}]" if p["winner"] else "ACCEPTED"
    elif p["status"] == "rejected":
        final = p["note"] or "KEEP THE CURRENT TAG"
    started = p["started_name"] or "?"
    png = await asyncio.to_thread(draw_poll, p, pills, yes, no, options, started, final)
    view = discord.ui.View(timeout=None)
    for act in {"picking": ("pick", "cx"), "phase1": ("yes", "no", "cx"),
                "phase2": ("sug", "vote", "close", "cx")}.get(p["status"], ()):
        view.add_item(PollButton(act, pid))
    return None, discord.File(__import__("io").BytesIO(png), filename="tagvote.png"), view


async def update_main(bot, pid):
    p = await q_one("SELECT * FROM tag_polls WHERE id = ?", (pid,))
    ch = bot.get_channel(int(p["channel_id"])) or await bot.fetch_channel(int(p["channel_id"]))
    msg = await ch.fetch_message(int(p["message_id"]))
    content, file, view = await poll_message(bot, pid)
    await msg.edit(content=content, attachments=[file], view=view)
    return ch


async def advance(bot, pid):
    """Moves the poll on when phase 1 is decided."""
    p = await q_one("SELECT * FROM tag_polls WHERE id = ?", (pid,))
    if p["status"] != "phase1":
        return
    voters = json.loads(p["voters"])
    answers = await q_all("SELECT * FROM tag_answers WHERE poll_id = ?", (pid,))
    state, yes, no = phase1_state(voters, answers)
    if state == "open":
        return
    ch = bot.get_channel(int(p["channel_id"])) or await bot.fetch_channel(int(p["channel_id"]))
    if state == "no":
        await q_run("UPDATE tag_polls SET status = 'rejected', note = ? WHERE id = ?",
                    (f"NO CHANGE ({no} no, {yes} yes)", pid))
        await update_main(bot, pid)
        return
    await q_run("UPDATE tag_polls SET status = 'phase2' WHERE id = ?", (pid,))
    await update_main(bot, pid)
    ping = " ".join(f"<@{v}>" for v in voters)
    await ch.send(f"{ping} the majority wants to change the active tag. **Step 2:** press **Suggest a tag** on the vote above "
                  "(as many as you like) and **Vote** for the tags you want (you can pick several).",
                  allowed_mentions=discord.AllowedMentions(users=True))


# ---------------------------------------------------------------- buttons and menus

class VoterSelect(discord.ui.UserSelect):
    def __init__(self, pid):
        super().__init__(placeholder="Pick the active players online who vote", min_values=1, max_values=25)
        self.pid = pid

    async def callback(self, interaction: discord.Interaction):
        picked = [u for u in self.values if not u.bot]
        await q_run("UPDATE tag_polls SET voters = ?, status = 'phase1' WHERE id = ?",
                    (json.dumps([str(u.id) for u in picked]), self.pid))
        await q_run("DELETE FROM tag_answers WHERE poll_id = ?", (self.pid,))
        core = interaction.client.np_core
        warn = []
        for u in picked:
            ok, why = await is_active(core, u.id)
            if not ok:
                warn.append(f"{u.mention}: {why}")
        ch = await update_main(interaction.client, self.pid)
        await ch.send(" ".join(u.mention for u in picked) + " you were picked to vote: should the **active tag** be changed? "
                      "Press **Yes** or **No** on the vote above.", allowed_mentions=discord.AllowedMentions(users=True))
        text = f"✅ {len(picked)} voters set."
        if warn:
            text += "\n⚠️ Not active or not linked (they can still vote): " + "; ".join(warn)
        await interaction.response.edit_message(content=text, view=None)


class SuggestModal(discord.ui.Modal, title="Suggest an active tag"):
    tag = discord.ui.TextInput(label="Tag (no brackets, up to 6 letters or numbers)", max_length=8, min_length=1)

    def __init__(self, pid):
        super().__init__()
        self.pid = pid

    async def on_submit(self, interaction: discord.Interaction):
        tag = str(self.tag).strip().strip("[]").upper()
        if not TAG_RE.match(tag):
            await interaction.response.send_message("The tag can have at most 6 characters, only letters and numbers.", ephemeral=True)
            return
        p = await q_one("SELECT * FROM tag_polls WHERE id = ?", (self.pid,))
        if p is None or p["status"] != "phase2":
            await interaction.response.send_message("This vote is closed.", ephemeral=True)
            return
        uid = str(interaction.user.id)
        if await q_one("SELECT 1 AS x FROM tag_options WHERE poll_id = ? AND tag = ?", (self.pid, tag)):
            await q_run("INSERT OR IGNORE INTO tag_choices (poll_id, discord_id, tag) VALUES (?, ?, ?)", (self.pid, uid, tag))
            note = f"**[{tag}]** was already suggested: your vote was added to it."
        else:
            count = (await q_one("SELECT COUNT(*) AS c FROM tag_options WHERE poll_id = ?", (self.pid,)))["c"]
            if count >= MAX_OPTIONS:
                await interaction.response.send_message(f"There are already {MAX_OPTIONS} tags: vote for one of them.", ephemeral=True)
                return
            name = (await names_of(interaction.client, [uid]))[uid]
            await q_run("INSERT INTO tag_options (poll_id, tag, by_id, by_name) VALUES (?, ?, ?, ?)", (self.pid, tag, uid, name))
            await q_run("INSERT OR IGNORE INTO tag_choices (poll_id, discord_id, tag) VALUES (?, ?, ?)", (self.pid, uid, tag))
            note = f"**[{tag}]** suggested, and you voted for it. You can suggest more."
        await interaction.response.send_message(note, ephemeral=True)
        await update_main(interaction.client, self.pid)


class TagPick(discord.ui.Select):
    def __init__(self, pid, options, mine):
        super().__init__(placeholder="Pick the tags you want (several allowed)", min_values=0,
                         max_values=max(1, len(options)),
                         options=[discord.SelectOption(label=f"[{t}]", value=t, default=t in mine) for t, _, _ in options[:25]])
        self.pid = pid

    async def callback(self, interaction: discord.Interaction):
        uid = str(interaction.user.id)
        p = await q_one("SELECT * FROM tag_polls WHERE id = ?", (self.pid,))
        if p is None or p["status"] != "phase2":
            await interaction.response.edit_message(content="This vote is closed.", view=None)
            return
        await q_run("DELETE FROM tag_choices WHERE poll_id = ? AND discord_id = ?", (self.pid, uid))
        for t in self.values:
            await q_run("INSERT OR IGNORE INTO tag_choices (poll_id, discord_id, tag) VALUES (?, ?, ?)", (self.pid, uid, t))
        await interaction.response.edit_message(
            content="✅ Your vote: " + (", ".join(f"**[{t}]**" for t in self.values) or "nothing") + ". You can change it any time.",
            view=None)
        await update_main(interaction.client, self.pid)


class PollButton(discord.ui.DynamicItem[discord.ui.Button], template=r"tv:(?P<act>pick|yes|no|sug|vote|close|cx):(?P<pid>\d+)"):
    LABELS = {"pick": ("Pick voters (staff)", discord.ButtonStyle.primary), "yes": ("Yes", discord.ButtonStyle.success),
              "no": ("No", discord.ButtonStyle.danger), "sug": ("Suggest a tag", discord.ButtonStyle.primary),
              "vote": ("Vote", discord.ButtonStyle.success), "close": ("Close and count (staff)", discord.ButtonStyle.secondary),
              "cx": ("Cancel (staff)", discord.ButtonStyle.secondary)}

    def __init__(self, act, pid):
        label, style = self.LABELS[act]
        super().__init__(discord.ui.Button(label=label, style=style, custom_id=f"tv:{act}:{pid}"))
        self.act, self.pid = act, int(pid)

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["act"], int(match["pid"]))

    async def callback(self, interaction: discord.Interaction):
        bot = interaction.client
        p = await q_one("SELECT * FROM tag_polls WHERE id = ?", (self.pid,))
        if p is None or p["status"] not in ("picking", "phase1", "phase2"):
            await interaction.response.send_message("This vote is closed.", ephemeral=True)
            return
        uid = str(interaction.user.id)
        voters = json.loads(p["voters"])
        if self.act in ("pick", "close", "cx") and not bl.is_guild_staff(interaction.user):
            await interaction.response.send_message("Only guild staff can do this.", ephemeral=True)
            return
        if self.act == "pick":
            view = discord.ui.View(timeout=300)
            view.add_item(VoterSelect(self.pid))
            await interaction.response.send_message("Choose the active players who are online now:", view=view, ephemeral=True)
            return
        if self.act == "cx":
            await q_run("UPDATE tag_polls SET status = 'cancelled' WHERE id = ?", (self.pid,))
            await interaction.response.defer()
            await update_main(bot, self.pid)
            return
        if self.act == "close":
            opts = await option_counts(self.pid)
            top = leaders(opts)
            if not top:
                await interaction.response.send_message("Nobody has voted for a tag yet.", ephemeral=True)
                return
            if len(top) > 1:
                await interaction.response.send_message(
                    "Tie: " + ", ".join(f"[{t}]" for t, _, _ in top) + f" have {top[0][1]} votes each. "
                    "Ask the voters to vote again, then close.", ephemeral=True)
                return
            await q_run("UPDATE tag_polls SET status = 'accepted', winner = ? WHERE id = ?", (top[0][0], self.pid))
            await interaction.response.defer()
            ch = await update_main(bot, self.pid)
            ping = bl._env("TAG_PING_USER_ID") or bl._env("BETRAYAL_PING_USER_ID")
            await ch.send((f"<@{ping}> " if ping.isdigit() else "") +
                          f"✅ The active players chose **[{top[0][0]}]** ({top[0][1]} votes): please change the active tag.",
                          allowed_mentions=discord.AllowedMentions(users=True))
            return
        # sug / vote / yes / no: only the voters
        if uid not in voters:
            await interaction.response.send_message("You are not one of the players picked for this vote.", ephemeral=True)
            return
        if self.act in ("yes", "no"):
            if p["status"] != "phase1":
                await interaction.response.send_message("Step 1 is over.", ephemeral=True)
                return
            await q_run("INSERT INTO tag_answers (poll_id, discord_id, answer) VALUES (?, ?, ?) "
                        "ON CONFLICT(poll_id, discord_id) DO UPDATE SET answer = excluded.answer",
                        (self.pid, uid, "y" if self.act == "yes" else "n"))
            await interaction.response.defer()
            await update_main(bot, self.pid)
            await advance(bot, self.pid)
            return
        if p["status"] != "phase2":
            await interaction.response.send_message("Step 2 has not started.", ephemeral=True)
            return
        if self.act == "sug":
            await interaction.response.send_modal(SuggestModal(self.pid))
            return
        opts = await option_counts(self.pid)
        if not opts:
            await interaction.response.send_message("No tags yet: press Suggest a tag first.", ephemeral=True)
            return
        mine = {r["tag"] for r in await q_all("SELECT tag FROM tag_choices WHERE poll_id = ? AND discord_id = ?", (self.pid, uid))}
        view = discord.ui.View(timeout=300)
        view.add_item(TagPick(self.pid, opts, mine))
        await interaction.response.send_message("Pick the tags you want:", view=view, ephemeral=True)


class TagCog(commands.GroupCog, name="tag", description="Active tag vote"):
    def __init__(self, bot):
        self.bot = bot
        super().__init__()

    @app_commands.command(name="vote", description="Start the active tag vote: first yes or no, then the tags (active players and staff)")
    async def vote(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        ok, why = await is_active(self.bot.np_core, interaction.user.id)
        if not ok and not bl.is_guild_staff(interaction.user):
            await interaction.followup.send(f"Only active players and staff can start the vote: {why}.", ephemeral=True)
            return
        if await q_one("SELECT id FROM tag_polls WHERE status IN ('picking', 'phase1', 'phase2')"):
            await interaction.followup.send("A tag vote is already open: wait for it to end (the staff can cancel it).", ephemeral=True)
            return
        name = why if ok else interaction.user.display_name
        pid = await q_run("INSERT INTO tag_polls (started_by, started_name, created_at) VALUES (?, ?, ?)",
                          (str(interaction.user.id), name, iso(now_utc())))
        cid = bl.chan_id("TAG_CHANNEL_ID")
        ch = (self.bot.get_channel(cid) or await self.bot.fetch_channel(cid)) if cid else interaction.channel
        content, file, view = await poll_message(self.bot, pid)
        msg = await ch.send(content=content, file=file, view=view)
        await q_run("UPDATE tag_polls SET channel_id = ?, message_id = ? WHERE id = ?", (str(ch.id), str(msg.id), pid))
        await interaction.followup.send(f"Vote started in {ch.mention}. The staff picks who votes.", ephemeral=True)

    @app_commands.command(name="list", description="Recent active tag votes (guild staff)")
    @app_commands.check(bl.staff_check)
    async def list(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        rows = await q_all("SELECT * FROM tag_polls ORDER BY id DESC LIMIT 15")
        if not rows:
            await interaction.followup.send("No tag votes yet.", ephemeral=True)
            return
        await interaction.followup.send(
            "\n".join(f"#{r['id']} · {r['status']}" + (f" · **[{r['winner']}]**" if r["winner"] else "")
                      + f" · by {esc(r['started_name'] or '?')}" for r in rows), ephemeral=True)


async def setup(bot: commands.Bot):
    async with aiosqlite.connect(bl.DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()
    bot.add_dynamic_items(PollButton)
    await bot.add_cog(TagCog(bot))

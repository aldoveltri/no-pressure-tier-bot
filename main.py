import asyncio
import io
import logging
import os
import re
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from database import (init_db, init_extras, init_seasons, init_modes, upsert_player, get_player, save_match,
                      get_stats, count_forfeits, get_rank, get_leaderboard,
                      get_history, count_history, get_all_matches, count_all_matches,
                      count_cancelled_matches, get_cancelled_matches,
                      get_link, remove_link, link_account,
                      get_match, last_match_id, cancel_match, set_match_date,
                      backup_db, remove_player, get_info,
                      create_pause, get_pause, list_open_pauses, count_open_pauses, close_pause,
                      get_current, list_seasons, list_years,
                      get_season_leaderboard, get_year_leaderboard, sort_rows,
                      end_season, end_year,
                      get_mode_by_name, get_mode, seed_modes, reset_all_data)
from mojang import get_uuid
import gexpcalc as gc
from hypixel import get_hypixel_discord, normalize_handle, NoApiKey, KeyRejected, HypixelError
from config import TIERS, STAFF_ROLE, TIER_MIN, MATCH_LOG_LIMIT, GUILD_NAME, RULES, MODES, tier_from_elo
from elo import calcola, FIRST_TO
import alerts
from guide import GUIDE_PAGES, GUIDE_CATEGORIES
from guide_cards import render_page
from graphics import (build_leaderboard_image, build_match_image, build_profile_image,
                      build_history_image, build_settier_image, build_matchlog_image,
                      build_setelo_image, build_info_image,
                      build_pause_image, build_paused_list_image, build_text_image)

def admin_only():
    async def pred(interaction: discord.Interaction) -> bool:
        if interaction.user.guild_permissions.administrator:
            return True
        raise app_commands.CheckFailure("Only server administrators can use this command.")
    return app_commands.check(pred)

load_dotenv()

# ---------- logging: errors are also written to bot_errors.log ----------
_handler = RotatingFileHandler("bot_errors.log", maxBytes=500_000, backupCount=2, encoding="utf-8")
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
log = logging.getLogger("tierbot")
log.setLevel(logging.INFO)
log.addHandler(_handler)
log.addHandler(logging.StreamHandler())

_intents = discord.Intents.default()
if os.getenv("MESSAGE_CONTENT_INTENT", "").strip() == "1":
    _intents.message_content = True      # only after switching it on in the Developer Portal (Bot > Privileged intents)
bot = commands.Bot(command_prefix="!", intents=_intents)

NO_MENTIONS = discord.AllowedMentions.none()
SEASON_ELO_LOSS = 100
GENERIC_ERROR = ("Something went wrong while running this command. "
                 "Please try again in a moment, and tell the staff if it keeps happening.")

SCORE_CHOICES = [   # used by /resume (normal mode, first to 3)
    app_commands.Choice(name="3-0", value=0),
    app_commands.Choice(name="3-1", value=1),
    app_commands.Choice(name="3-2", value=2),
]

SORT_OPTIONS = [("elo", "Elo"), ("wins", "Wins"), ("losses", "Losses"),
                ("winrate", "Win rate"), ("matches", "Matches played")]
SORT_CHOICES = [app_commands.Choice(name=label, value=key) for key, label in SORT_OPTIONS]

SCORE_RE = re.compile(r"^\s*(\d+)\s*[-–:]\s*(\d+)\s*$")


# ---------- small helpers ----------

def png_file(data: bytes, name: str) -> discord.File:
    return discord.File(io.BytesIO(data), filename=name)


def fmt_date(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%m/%d/%Y")
    except Exception:
        return "-"


def esc(text: str) -> str:
    return discord.utils.escape_markdown(text)


def match_number(m) -> int:
    """The number shown for a match (a new match can take the number of a cancelled one)."""
    try:
        return m["num"] if m["num"] is not None else m["id"]
    except (IndexError, KeyError):
        return m["id"]


def is_staff(member) -> bool:
    if not isinstance(member, discord.Member):
        return False
    if member.guild_permissions.administrator:
        return True
    return any(r.name == STAFF_ROLE for r in member.roles)


def staff_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        return is_staff(interaction.user)
    return app_commands.check(predicate)


async def send_card(interaction: discord.Interaction, data: bytes, filename: str, md=None, **kwargs):
    """Sends an image card. For a game mode, the mode name is written above the image."""
    content = f"**{esc(md['name'])}**" if md else None
    return await interaction.followup.send(content=content, file=png_file(data, filename), **kwargs)


async def post_1v1_log(interaction, png_bytes, filename="match.png", text=None, kind="1v1_logs"):
    """Posts in the 1v1 logs channel, unless the alert is switched off with /alerts (data is saved anyway)."""
    if not await alerts.enabled(kind):
        return
    cid = os.getenv("LOGS_1V1_CHANNEL_ID", "").strip()
    if not cid.isdigit():
        return
    ch = interaction.client.get_channel(int(cid))
    if ch is None or ch.id == interaction.channel_id:
        return
    try:
        kwargs = {"content": text, "allowed_mentions": NO_MENTIONS}
        if png_bytes:
            kwargs["file"] = discord.File(io.BytesIO(png_bytes), filename=filename)
        await ch.send(**kwargs)
    except discord.HTTPException:
        pass


async def lookup_ign(interaction: discord.Interaction, ign: str):
    """Username -> (uuid, name). Sends a clear error message and returns None on failure."""
    try:
        result = await get_uuid(ign)
    except (RuntimeError, aiohttp.ClientError, asyncio.TimeoutError):
        await interaction.followup.send("Mojang is not responding right now. Please try again in a moment.")
        return None
    if result is None:
        await interaction.followup.send(
            f"`{ign}` is not a valid Minecraft username. Check the spelling "
            f"(3-16 characters: letters, numbers and underscores).")
        return None
    return result


# ---------- game modes (fixed list in config.py) ----------

def find_config_mode(text):
    """Finds a configured mode from its name or its short key (also partial text)."""
    t = (text or "").strip().lower()
    if not t:
        return None
    for m in MODES:
        if t == m["key"] or t == m["name"].lower():
            return m
    for m in MODES:
        if t in m["name"].lower() or t in m["key"]:
            return m
    return None


def mode_system(md) -> str:
    """'ft3' or 'ft5' for a mode row (None = the normal mode = ft3)."""
    if md is not None:
        cfg = find_config_mode(md["name"])
        if cfg:
            return cfg["system"]
    return "ft3"


def parse_score(text, first_to: int):
    """'5-3' -> 3 (the games won by the loser), or None if the score isn't valid."""
    m = SCORE_RE.match(text or "")
    if not m:
        return None
    w, l = int(m.group(1)), int(m.group(2))
    if w != first_to or not (0 <= l < first_to):
        return None
    return l


async def resolve_mode(interaction: discord.Interaction, mode_name):
    """Returns (ok, mode row). No mode name = the normal mode (None). Sends an error if not found."""
    if not mode_name:
        return True, None
    cfg = find_config_mode(mode_name)
    md = await get_mode_by_name(cfg["name"]) if cfg else None
    if md is None:
        names = ", ".join(m["name"] for m in MODES)
        await interaction.followup.send(
            f"The mode `{mode_name}` does not exist. Available modes: {names}. "
            f"Leave it empty for the normal mode.")
        return False, None
    return True, md


async def mode_autocomplete(interaction: discord.Interaction, current: str):
    cur = current.strip().lower()
    return [app_commands.Choice(name=m["name"], value=m["name"]) for m in MODES
            if not cur or cur in m["name"].lower() or cur in m["key"]]


async def score_autocomplete(interaction: discord.Interaction, current: str):
    """Suggests the valid scores of the selected mode."""
    mode_name = getattr(interaction.namespace, "mode", None)
    cfg = find_config_mode(mode_name) if mode_name else None
    first_to = FIRST_TO[cfg["system"]] if cfg else 3
    scores = [f"{first_to}-{k}" for k in range(first_to)]
    return [app_commands.Choice(name=s, value=s) for s in scores if current.strip() in s]


async def resolve_target(interaction: discord.Interaction, ign):
    """Works out which player a command is about: the typed Minecraft username, or the
    caller's linked account when no username is given.
    Returns (uuid, name, fresh) or None (an error message has already been sent).
    'fresh' is True when the name comes straight from Mojang."""
    if ign:
        result = await lookup_ign(interaction, ign)
        if result is None:
            return None
        return result[0], result[1], True

    link_row = await get_link(interaction.user.id)
    if link_row is None:
        await interaction.followup.send(
            "You haven't linked a Minecraft account yet. Use `/link` or type a username.")
        return None
    return link_row["uuid"], link_row["ign"], False


def parse_match_date(text):
    """'2026/10/21', '2026/10/21 6:00 PM' or '10/21' (Eastern Time) -> (aware datetime, None) or (None, error).
    No time given: midday, so the date never shifts. Future dates are refused."""
    new = gc.parse_et(text)
    if new is None:
        return None, ("I couldn't read the date. Use `2026/10/21`, `2026/10/21 6:00 PM` or without the year "
                      "`10/21` (Eastern Time).")
    if not re.search(r"\d:\d|am|pm", text, re.I):
        new = new.replace(hour=12)
    if new.astimezone(timezone.utc) > datetime.now(timezone.utc):
        return None, "That date is in the future."
    return new, None


async def log_match(interaction: discord.Interaction, uuid_w, name_w, uuid_l, name_l,
                    score_name: str, loser_games: int, forfeit: bool = False, md=None, played=None) -> bool:
    """Registers a finished match, updates the elo and sends the result card.
    Returns False if the match could not be logged (a message has been sent)."""
    mode_id = md["id"] if md else None
    system = mode_system(md)
    p_w = await get_player(uuid_w, mode_id=mode_id)
    p_l = await get_player(uuid_l, mode_id=mode_id)
    if p_w is None or p_l is None:
        missing = esc(name_w if p_w is None else name_l)
        if md:
            await interaction.followup.send(
                f"**{missing}** doesn't have a tier in **{esc(md['name'])}** yet: "
                f"use `/settier` with `mode:{md['name']}` first.")
        else:
            await interaction.followup.send(
                f"**{missing}** doesn't have a tier yet: use `/settier` first.")
        return False

    gain, loss = calcola(p_w["elo"], p_l["elo"], loser_games, system)
    new_w, new_l = p_w["elo"] + gain, p_l["elo"] + loss

    now = played or datetime.now(timezone.utc)
    match_id = await save_match(uuid_w, uuid_l, p_w["elo"], new_w, p_l["elo"], new_l,
                                score_name, played_at=now.isoformat(), forfeit=forfeit, mode_id=mode_id)

    w = {"uuid": uuid_w, "name": name_w, "before": p_w["elo"], "after": new_w, "delta": gain,
         "tier_before": tier_from_elo(p_w["elo"]), "tier_after": tier_from_elo(new_w)}
    l = {"uuid": uuid_l, "name": name_l, "before": p_l["elo"], "after": new_l,
         "delta": new_l - p_l["elo"],
         "tier_before": tier_from_elo(p_l["elo"]), "tier_after": tier_from_elo(new_l)}

    date = (now.astimezone(gc.ET) if played else now).strftime("%m/%d/%Y") + (f" · {md['name']}" if md else "")
    data = await build_match_image(w, l, score_name, match_id, date, forfeit=forfeit,
                                   first_to=FIRST_TO[system])
    await send_card(interaction, data, "match.png", md)
    await post_1v1_log(interaction, data, filename="match.png", text=f"**{esc(md['name'])}**" if md else None)
    return True


# ---------- views ----------

class BaseView(discord.ui.View):
    message = None   # set after sending, so the buttons can be removed on timeout

    async def on_timeout(self):
        if self.message is not None:
            try:
                await self.message.edit(view=None)
            except Exception:
                pass

    async def on_error(self, interaction: discord.Interaction, error: Exception, item) -> None:
        log.error("Error in a button", exc_info=error)
        text = "Something went wrong. Please run the command again."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)
        except Exception:
            pass


class ConfirmView(BaseView):
    """Confirm / Cancel buttons. 'action' is an async function called with the button interaction."""

    def __init__(self, owner_id: int, action):
        super().__init__(timeout=60)
        self.owner_id = owner_id
        self.action = action

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the staff member who ran the command can use these buttons.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content="Working...", view=None)
        await self.action(interaction)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content="Cancelled. Nothing was changed.", view=None)


class PagedView(BaseView):
    """Base for ◀ ▶ paging. Subclasses implement make_file() and set self.pages."""
    pages = 1
    page = 0

    def update_buttons(self):
        self.prev.disabled = self.page == 0
        self.next.disabled = self.page >= self.pages - 1

    async def change_page(self, interaction: discord.Interaction, delta: int):
        await interaction.response.defer()
        self.page += delta
        self.update_buttons()
        await interaction.edit_original_response(attachments=[await self.make_file()], view=self)

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary, row=0)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.change_page(interaction, -1)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary, row=0)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.change_page(interaction, 1)


# ---------- startup and error handling ----------

async def setup_hook():
    await init_db()
    await init_extras()
    await init_seasons()
    await init_modes()
    await seed_modes([m["name"] for m in MODES])
    await bot.load_extension("namesync")
    await bot.load_extension("breaklist")
    await bot.load_extension("evidence")
    await bot.load_extension("community")

bot.setup_hook = setup_hook


@bot.event
async def on_ready():
    for guild in bot.guilds:
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
    # remove old global commands (registered with old text)
    bot.tree.clear_commands(guild=None)
    await bot.tree.sync()
    log.info("Online as %s", bot.user)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        text = "You don't have permission to use this command."
    else:
        original = getattr(error, "original", error)
        name = interaction.command.name if interaction.command else "?"
        log.error("Error in /%s", name, exc_info=original)
        text = GENERIC_ERROR
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
    except Exception:
        log.exception("Could not send the error message")


# ---------- utility ----------

@bot.tree.command(name="ping", description="Check if the bot is online")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("Pong!")


# ---------- rules ----------

@bot.tree.command(name="rules", description="Show the rules for 1v1s and tier tests")
async def rules(interaction: discord.Interaction):
    await interaction.response.defer()
    blocks = []
    for heading, lines in RULES:
        blocks.append({"type": "heading", "text": heading})
        for line in lines:
            blocks.append({"type": "bullet", "text": line})
    data = await build_text_image("1V1 & TIER TEST RULES", GUILD_NAME, blocks)
    await interaction.followup.send(file=png_file(data, "rules.png"))


# ---------- guide ----------

class GuideView(discord.ui.View):
    def __init__(self, user_id, cat=0, page=0):
        super().__init__(timeout=300)
        self.user_id, self.cat, self.page = user_id, cat, page
        self.cache = {}
        self.add_item(self._cat_select())
        self.add_item(self._page_select())

    def _cat_select(self):
        sel = discord.ui.Select(placeholder="Category", options=[
            discord.SelectOption(label=f"{i+1}. {c['name']}", description=c["description"][:100], value=str(i),
                                 default=(i == self.cat)) for i, c in enumerate(GUIDE_CATEGORIES)])
        async def cb(it):
            self.cat, self.page = int(sel.values[0]), 0
            await self.refresh(it, rebuild=True)
        sel.callback = cb
        return sel

    def _page_select(self):
        pages = GUIDE_CATEGORIES[self.cat]["pages"]
        sel = discord.ui.Select(placeholder="Page", options=[
            discord.SelectOption(label=p["title"][:100], value=str(i), default=(i == self.page))
            for i, p in enumerate(pages[:25])])
        async def cb(it):
            self.page = int(sel.values[0])
            await self.refresh(it)
        sel.callback = cb
        return sel

    async def interaction_check(self, it):
        if it.user.id != self.user_id:
            await it.response.send_message("This is not your guide. Use /guide.", ephemeral=True)
            return False
        return True

    async def refresh(self, it, rebuild=False):
        if rebuild:
            self.clear_items()
            self.add_item(self._cat_select())
            self.add_item(self._page_select())
        key = (self.cat, self.page)
        if key not in self.cache:
            cat_obj = GUIDE_CATEGORIES[self.cat]
            p = cat_obj["pages"][self.page]
            self.cache[key] = await asyncio.to_thread(
                render_page, p["title"].upper(), f"{GUILD_NAME} · {cat_obj['name']}", p["blocks"],
                f"Page {self.page + 1}/{len(cat_obj['pages'])}")
        f = discord.File(io.BytesIO(self.cache[key]), filename="guide.png")
        await it.response.edit_message(attachments=[f], view=self)


@bot.tree.command(name="guide", description="A complete guide to the bot and all its commands")
async def guide(interaction: discord.Interaction):
    await interaction.response.defer()
    cat, page = 0, 0
    key = (cat, page)
    cat_obj = GUIDE_CATEGORIES[cat]
    p = cat_obj["pages"][page]
    data = await asyncio.to_thread(
        render_page, p["title"].upper(), f"{GUILD_NAME} · {cat_obj['name']}", p["blocks"],
        f"Page {page + 1}/{len(cat_obj['pages'])}")
    view = GuideView(interaction.user.id, cat, page)
    view.cache[key] = data
    view.message = await interaction.followup.send(file=png_file(data, "guide.png"), view=view)


# ---------- game modes ----------

@bot.tree.command(name="modes", description="Show the game modes")
async def modes(interaction: discord.Interaction):
    await interaction.response.defer()
    blocks = [
        {"type": "heading", "text": "Normal (default)"},
        {"type": "bullet", "text":
         "The base mode: first to 3, with seasons and years. It is used when you don't choose a mode."},
        {"type": "heading", "text": "Other modes"},
    ]
    for m in MODES:
        blocks.append({"type": "bullet", "text": f"{m['name']}: first to {FIRST_TO[m['system']]}"})
    blocks.append({"type": "paragraph", "text":
                   "To use a mode, choose it in the mode option of /profile, /history, /leaderboard and the "
                   "other commands. Each mode has its own elo, tiers and leaderboard, only lifetime "
                   "(no seasons), and resets at the end of the year like the normal one."})
    data = await build_text_image("GAME MODES", GUILD_NAME, blocks)
    await interaction.followup.send(file=png_file(data, "modes.png"))


# ---------- season / year suggestions for the commands ----------

async def season_choices(current: str):
    out = []
    for r in await list_seasons():
        if current and current not in str(r["id"]):
            continue
        label = f"Season {r['id']}" + (" (current)" if r["ended_at"] is None else "")
        out.append(app_commands.Choice(name=label, value=r["id"]))
    return out[:25]


async def year_choices(current: str):
    out = []
    for r in await list_years():
        if current and current not in str(r["id"]):
            continue
        out.append(app_commands.Choice(name=f"Year {r['id']}", value=r["id"]))
    return out[:25]


# ---------- linking (always verified on Hypixel) ----------

async def check_hypixel_ownership(interaction: discord.Interaction, uuid: str, name: str) -> bool:
    """Checks on Hypixel that this account has the caller's Discord username set.
    Sends the reason and returns False if the check fails."""
    try:
        found, handle = await get_hypixel_discord(uuid)
    except NoApiKey:
        await interaction.followup.send(
            "Linking is not available yet (the bot has no Hypixel API key). Please ask the staff to link you.")
        return False
    except KeyRejected:
        log.error("The Hypixel API key was rejected: it has probably expired and needs renewing.")
        await interaction.followup.send(
            "Linking is temporarily unavailable. Please try again later or ask the staff to link you.")
        return False
    except (HypixelError, aiohttp.ClientError, asyncio.TimeoutError) as e:
        log.warning("Hypixel check failed: %s", e)
        await interaction.followup.send("Hypixel is not responding right now. Please try again in a moment.")
        return False

    my_name = interaction.user.name
    if not found:
        await interaction.followup.send(
            f"Hypixel has no data for **{name}**. Have you ever joined the server with this account?")
        return False
    if handle is None:
        await interaction.followup.send(
            f"**{name}** has no Discord name set on Hypixel, so it can't be verified.\n"
            f"On Hypixel, open your profile (the player head in the lobby), go to "
            f"**Social Media → Discord** and enter your Discord username: `{my_name}`.\n"
            f"Then run `/link` again (it can take a minute to update).")
        return False
    if normalize_handle(handle) != normalize_handle(my_name):
        await interaction.followup.send(
            f"The Discord name set on Hypixel for **{name}** (`{handle}`) doesn't match your Discord "
            f"username (`{my_name}`). Fix it on Hypixel and run `/link` again.")
        return False
    return True


@bot.tree.command(name="link", description="Link your Discord account to your Minecraft account (verified on Hypixel)")
@app_commands.describe(ign="Your Minecraft username", user="Staff only: link another member without verification")
async def link(interaction: discord.Interaction, ign: str, user: Optional[discord.Member] = None):
    by_staff = user is not None
    if by_staff and not is_staff(interaction.user):
        await interaction.response.send_message("Only staff can link other members.", ephemeral=True)
        return
    target = user or interaction.user

    await interaction.response.defer(ephemeral=True)
    result = await lookup_ign(interaction, ign)
    if result is None:
        return
    uuid, name = result

    if not by_staff:
        if not await check_hypixel_ownership(interaction, uuid, name):
            return

    await link_account(target.id, uuid, name)
    if by_staff:
        msg = f"✅ **{target.display_name}** is now linked to **{name}** (linked by staff)."
    else:
        msg = f"✅ Verified! **{name}** is now linked to your Discord account."
    await interaction.followup.send(msg, allowed_mentions=NO_MENTIONS)


@bot.tree.command(name="unlink", description="Remove the link between your Discord account and your username")
async def unlink(interaction: discord.Interaction):
    removed = await remove_link(interaction.user.id)
    if removed:
        await interaction.response.send_message("Your account has been unlinked.", ephemeral=True)
    else:
        await interaction.response.send_message("You don't have a linked account.", ephemeral=True)


# ---------- staff commands ----------

@bot.tree.command(name="settier", description="Assign a tier to a player (staff only)")
@app_commands.describe(ign="Minecraft username of the player", tier="Tier to assign",
                       mode="Game mode (leave empty for the normal mode)")
@app_commands.choices(tier=[app_commands.Choice(name=t, value=t) for t in TIERS])
@staff_only()
async def settier(interaction: discord.Interaction, ign: str, tier: app_commands.Choice[str],
                  mode: Optional[str] = None):
    await interaction.response.defer()
    ok, md = await resolve_mode(interaction, mode)
    if not ok:
        return
    mode_id = md["id"] if md else None
    result = await lookup_ign(interaction, ign)
    if result is None:
        return
    uuid, name = result

    old = await get_player(uuid, mode_id=mode_id)
    old_tier = old["tier"] if old else None
    await upsert_player(uuid, name, TIER_MIN[tier.value], mode_id=mode_id)

    data = await build_settier_image(uuid, name, old_tier, tier.value, TIER_MIN[tier.value])
    await send_card(interaction, data, "tier.png", md)


@bot.tree.command(name="setelo", description="Set a player's elo manually (staff only)")
@app_commands.describe(ign="Minecraft username of the player", elo="New elo (0-5000)",
                       mode="Game mode (leave empty for the normal mode)")
@staff_only()
async def setelo(interaction: discord.Interaction, ign: str, elo: app_commands.Range[int, 0, 5000],
                 mode: Optional[str] = None):
    await interaction.response.defer()
    ok, md = await resolve_mode(interaction, mode)
    if not ok:
        return
    mode_id = md["id"] if md else None
    result = await lookup_ign(interaction, ign)
    if result is None:
        return
    uuid, name = result

    old = await get_player(uuid, mode_id=mode_id)
    old_elo = old["elo"] if old else None
    await upsert_player(uuid, name, elo, mode_id=mode_id)

    data = await build_setelo_image(uuid, name, old_elo, elo)
    await send_card(interaction, data, "elo.png", md)


@bot.tree.command(name="match", description="Log a 1v1 result (staff only)")
@app_commands.describe(winner="Username of the winner", loser="Username of the loser",
                       score="Final score, for example 3-1 (in Classic 1v1 a match is first to 5: 5-3)",
                       mode="Game mode (leave empty for the normal mode)",
                       date="Optional: when it was played, 2026/10/21 or 2026/10/21 6:00 PM (Eastern Time). Default: now")
@staff_only()
async def match(interaction: discord.Interaction, winner: str, loser: str, score: str,
                mode: Optional[str] = None, date: Optional[str] = None):
    await interaction.response.defer()
    played = None
    if date:
        played, err = parse_match_date(date)
        if err:
            await interaction.followup.send(err)
            return
    ok, md = await resolve_mode(interaction, mode)
    if not ok:
        return
    first_to = FIRST_TO[mode_system(md)]
    loser_games = parse_score(score, first_to)
    if loser_games is None:
        await interaction.followup.send(
            f"The score must be {first_to}-0 up to {first_to}-{first_to - 1}: "
            f"in this mode a match is first to {first_to}.")
        return

    r_w = await lookup_ign(interaction, winner)
    if r_w is None:
        return
    r_l = await lookup_ign(interaction, loser)
    if r_l is None:
        return

    uuid_w, name_w = r_w
    uuid_l, name_l = r_l
    if uuid_w == uuid_l:
        await interaction.followup.send("The winner and the loser can't be the same player.")
        return

    await log_match(interaction, uuid_w, name_w, uuid_l, name_l,
                    f"{first_to}-{loser_games}", loser_games, md=md, played=played)


@bot.tree.command(name="forfeit", description="Log a forfeit: the player who left loses (staff only)")
@app_commands.describe(player="Username of the player who left the match",
                       opponent="Username of the opponent (wins by forfeit)",
                       player_games="Games the player who left had won before leaving (default 0)",
                       opponent_games="Games the opponent had won before the forfeit (default 0)",
                       pause_id="Optional: the paused match this forfeit closes (its score is used)",
                       mode="Game mode (leave empty for the normal mode)")
@staff_only()
async def forfeit_cmd(interaction: discord.Interaction, player: str, opponent: str,
                      player_games: app_commands.Range[int, 0, 4] = 0,
                      opponent_games: app_commands.Range[int, 0, 4] = 0,
                      pause_id: Optional[int] = None, mode: Optional[str] = None):
    await interaction.response.defer()
    ok, md = await resolve_mode(interaction, mode)
    if not ok:
        return
    if md and pause_id is not None:
        await interaction.followup.send("Paused matches only exist in the normal mode.")
        return
    first_to = FIRST_TO[mode_system(md)]
    if pause_id is None and (player_games >= first_to or opponent_games >= first_to):
        await interaction.followup.send(
            f"In this mode a match is first to {first_to}, so before a forfeit nobody can have "
            f"more than {first_to - 1} games.")
        return

    r_l = await lookup_ign(interaction, player)
    if r_l is None:
        return
    r_w = await lookup_ign(interaction, opponent)
    if r_w is None:
        return

    uuid_l, name_l = r_l
    uuid_w, name_w = r_w
    if uuid_l == uuid_w:
        await interaction.followup.send("The player and the opponent can't be the same person.")
        return

    pm = None
    if pause_id is not None:
        pm = await get_pause(pause_id)
        if pm is None:
            await interaction.followup.send(f"Paused match #{pause_id} does not exist.")
            return
        if pm["status"] != "open":
            await interaction.followup.send(f"Paused match #{pause_id} was already {pm['status']}.")
            return
        if {pm["p1_uuid"], pm["p2_uuid"]} != {uuid_l, uuid_w}:
            await interaction.followup.send(
                f"Paused match #{pause_id} is not between these two players.")
            return
        # the score of the paused match is the score before the forfeit
        if pm["p1_uuid"] == uuid_l:
            player_games, opponent_games = pm["p1_games"], pm["p2_games"]
        else:
            player_games, opponent_games = pm["p2_games"], pm["p1_games"]

    # the elo is always that of the maximum loss (3-0, or 5-0); the score shown is the real one
    shown = f"{opponent_games}-{player_games}"
    done = await log_match(interaction, uuid_w, name_w, uuid_l, name_l, shown, 0,
                           forfeit=True, md=md)
    if done and pm is not None:
        await close_pause(pause_id, "forfeited")


@bot.tree.command(name="undo", description="Cancel a logged match (staff only)")
@app_commands.describe(match_id="Match number (the # shown on the match card)")
@staff_only()
async def undo(interaction: discord.Interaction, match_id: int):
    await interaction.response.defer()
    m = await get_match(match_id)
    if m is None:
        await interaction.followup.send(f"Match #{match_id} does not exist.")
        return
    if m["cancelled"]:
        await interaction.followup.send(f"Match #{match_id} is already cancelled.")
        return

    mode_id = m["mode_id"]
    md = await get_mode(mode_id) if mode_id is not None else None
    current_year, current_season = await get_current()
    if mode_id is None:
        if m["season_id"] != current_season:
            await interaction.followup.send(
                f"Match #{match_id} belongs to a season that is already over, so it can't be cancelled.")
            return
    elif m["mode_year_id"] != current_year:
        await interaction.followup.send(
            f"Match #{match_id} belongs to a year that is already over, so it can't be cancelled.")
        return

    # the match must be the most recent one for both players (in the same mode)
    for uuid in (m["winner_uuid"], m["loser_uuid"]):
        last_id, last_num = await last_match_id(uuid, mode_id)
        if last_id != m["id"]:
            await interaction.followup.send(
                f"Match #{match_id} can't be cancelled yet: one of the players has a newer "
                f"match (#{last_num}). Cancel the newer one first.")
            return

    # and their elo must not have changed since (new /settier, /setelo...)
    p_w = await get_player(m["winner_uuid"], include_removed=True, mode_id=mode_id)
    p_l = await get_player(m["loser_uuid"], include_removed=True, mode_id=mode_id)
    if p_w["elo"] != m["winner_elo_after"] or p_l["elo"] != m["loser_elo_after"]:
        await interaction.followup.send(
            f"Match #{match_id} can't be cancelled: the elo of one of the players changed "
            f"after it (for example a manual change).")
        return

    await cancel_match(m)

    w = {"uuid": m["winner_uuid"], "name": m["winner_ign"],
         "before": m["winner_elo_after"], "after": m["winner_elo_before"],
         "delta": m["winner_elo_before"] - m["winner_elo_after"],
         "tier_before": tier_from_elo(m["winner_elo_after"]),
         "tier_after": tier_from_elo(m["winner_elo_before"])}
    l = {"uuid": m["loser_uuid"], "name": m["loser_ign"],
         "before": m["loser_elo_after"], "after": m["loser_elo_before"],
         "delta": m["loser_elo_before"] - m["loser_elo_after"],
         "tier_before": tier_from_elo(m["loser_elo_after"]),
         "tier_after": tier_from_elo(m["loser_elo_before"])}

    date = fmt_date(m["played_at"]) + (f" · {md['name']}" if md else "")
    data = await build_match_image(w, l, m["score"] or "-", match_id, date,
                                   title="MATCH CANCELLED", forfeit=bool(m["forfeit"]))
    await send_card(interaction, data, "undo.png", md)
    await post_1v1_log(interaction, data, filename="undo.png", text=f"**{esc(md['name'])}**" if md else None)


async def match_card(m, md, date_iso):
    """The result card of a stored match row, drawn again (used when its date changes)."""
    w = {"uuid": m["winner_uuid"], "name": m["winner_ign"], "before": m["winner_elo_before"],
         "after": m["winner_elo_after"], "delta": m["winner_elo_after"] - m["winner_elo_before"],
         "tier_before": tier_from_elo(m["winner_elo_before"]), "tier_after": tier_from_elo(m["winner_elo_after"])}
    l = {"uuid": m["loser_uuid"], "name": m["loser_ign"], "before": m["loser_elo_before"],
         "after": m["loser_elo_after"], "delta": m["loser_elo_after"] - m["loser_elo_before"],
         "tier_before": tier_from_elo(m["loser_elo_before"]), "tier_after": tier_from_elo(m["loser_elo_after"])}
    date = fmt_date(date_iso) + (f" · {md['name']}" if md else "")
    return await build_match_image(w, l, m["score"] or "-", m["num"], date,
                                   title="MATCH CANCELLED" if m["cancelled"] else "MATCH RESULT",
                                   forfeit=bool(m["forfeit"]), first_to=FIRST_TO[mode_system(md)])


@bot.tree.command(name="setdate", description="Change the date of a logged match: a new card is made (staff only)")
@app_commands.describe(match_id="Match number (the # shown on the match card)",
                       date="New date: 2026/10/21, or 2026/10/21 6:00 PM, or without the year: 10/21 (Eastern Time)")
@staff_only()
async def setdate(interaction: discord.Interaction, match_id: int, date: str):
    await interaction.response.defer()
    m = await get_match(match_id)
    if m is None:
        await interaction.followup.send(f"Match #{match_id} does not exist.")
        return
    new, err = parse_match_date(date)
    if err:
        await interaction.followup.send(err)
        return
    old_text = fmt_date(m["played_at"])
    await set_match_date(m["id"], new.isoformat())
    md = await get_mode(m["mode_id"]) if m["mode_id"] is not None else None
    text = (f"📅 Match **#{match_id}** ({esc(m['winner_ign'])} vs {esc(m['loser_ign'])}): "
            f"date changed from **{old_text}** to **{new.strftime('%m/%d/%Y')}**.")
    data = await match_card(m, md, new.isoformat())
    await interaction.followup.send(content=text, file=png_file(data, "match.png"), allowed_mentions=NO_MENTIONS)
    if await alerts.enabled("match_dates"):
        await post_1v1_log(interaction, data, filename="match.png", text=text, kind="match_dates")


@bot.tree.command(name="elocalc", description="What if: the elo each player would gain or lose in a match (nothing is saved)")
@app_commands.describe(player1_elo="Elo of player 1", player2_elo="Elo of player 2",
                       mode="Game mode (leave empty for the normal mode)")
async def elocalc(interaction: discord.Interaction, player1_elo: app_commands.Range[int, 0, 100000],
                  player2_elo: app_commands.Range[int, 0, 100000], mode: Optional[str] = None):
    await interaction.response.defer()
    ok, md = await resolve_mode(interaction, mode)
    if not ok:
        return
    system = mode_system(md)
    first_to = FIRST_TO[system]

    def lines(elo_w, elo_l):
        out = []
        for lg in range(first_to):
            gain, loss = calcola(elo_w, elo_l, lg, system)
            out.append(f"**{first_to}-{lg}**: winner {gain:+d} → {elo_w + gain} · loser {loss:+d} → {elo_l + loss}")
        return "\n".join(out)

    embed = discord.Embed(title="Hypothetical match" + (f" · {md['name']}" if md else ""), color=0x00C8E6)
    embed.description = (f"**Player 1**: {player1_elo} ({tier_from_elo(player1_elo)})\n"
                         f"**Player 2**: {player2_elo} ({tier_from_elo(player2_elo)})")
    embed.add_field(name="If player 1 wins", value=lines(player1_elo, player2_elo), inline=False)
    embed.add_field(name="If player 2 wins", value=lines(player2_elo, player1_elo), inline=False)
    embed.set_footer(text="Nothing is saved: it is only a calculation.")
    await interaction.followup.send(embed=embed)


ALERT_CHOICES = [app_commands.Choice(name=v[0], value=k) for k, v in alerts.ALERTS.items()]


@bot.tree.command(name="alerts", description="Switch the automatic posts on or off (staff only). No options: show the state")
@app_commands.describe(alert="Which automatic post (or all of them)", state="on or off")
@app_commands.choices(alert=ALERT_CHOICES + [app_commands.Choice(name="All of them", value="all")],
                      state=[app_commands.Choice(name="on", value="on"), app_commands.Choice(name="off", value="off")])
@staff_only()
async def alerts_cmd(interaction: discord.Interaction, alert: Optional[str] = None, state: Optional[str] = None):
    await interaction.response.defer(ephemeral=True)
    if alert and state:
        names = list(alerts.ALERTS) if alert == "all" else [alert]
        for k in names:
            await alerts.set_enabled(k, state == "on")
    elif alert or state:
        await interaction.followup.send("Give both the alert and the state, or nothing to see the list.", ephemeral=True)
        return
    rows = []
    for k, (label, what) in alerts.ALERTS.items():
        rows.append(f"{'🟢 ON ' if await alerts.enabled(k) else '🔴 OFF'} **{label}** - {what}")
    await interaction.followup.send(
        "\n".join(rows) + "\n\nChange one with `/alerts alert:<name> state:on|off`. "
        "While an alert is off the data is still saved: only the automatic post is skipped.", ephemeral=True)


@bot.tree.command(name="removeplayer", description="Remove a player from the leaderboard and profiles (staff only)")
@app_commands.describe(ign="Minecraft username of the player to remove",
                       mode="Game mode (leave empty for the normal mode)")
@staff_only()
async def removeplayer(interaction: discord.Interaction, ign: str, mode: Optional[str] = None):
    await interaction.response.defer(ephemeral=True)
    ok, md = await resolve_mode(interaction, mode)
    if not ok:
        return
    mode_id = md["id"] if md else None
    result = await lookup_ign(interaction, ign)
    if result is None:
        return
    uuid, name = result

    player = await get_player(uuid, mode_id=mode_id)
    if player is None:
        where = f"the {esc(md['name'])} leaderboard" if md else "the leaderboard"
        await interaction.followup.send(f"**{name}** is not in {where}.")
        return
    safe_name = esc(name)

    async def do_remove(i: discord.Interaction):
        kept = await remove_player(uuid, mode_id=mode_id)
        await i.edit_original_response(content="Done.")
        where = f" ({esc(md['name'])})" if md else ""
        await i.followup.send(
            f"🗑️ **{safe_name}** was removed from the leaderboard and profiles{where}. "
            f"Their {kept} matches were kept in the history.")

    if md:
        text = (f"⚠️ This will remove **{safe_name}** (elo {player['elo']}) from the **{esc(md['name'])}** "
                f"leaderboard and `/profile`. Their matches **stay** in the history. "
                f"You can bring them back later with `/settier` or `/setelo`. Do you want to continue?")
    else:
        text = (f"⚠️ This will remove **{safe_name}** (elo {player['elo']}) from the leaderboard and "
                f"`/profile`, and unlink their Discord account. Their matches **stay** in the history and "
                f"match log. You can bring them back later with `/settier` or `/setelo`. "
                f"Do you want to continue?")
    await interaction.followup.send(
        text, view=ConfirmView(interaction.user.id, do_remove), ephemeral=True)


# ---------- end of season / end of year ----------

@bot.tree.command(name="endseason", description="End the current season and start a new one (staff only)")
@staff_only()
@admin_only()
async def endseason(interaction: discord.Interaction):
    async def do_season(i: discord.Interaction):
        backup = backup_db()
        closed, new, count = await end_season(SEASON_ELO_LOSS)
        await i.edit_original_response(content="Done.")
        await i.followup.send(
            f"🏁 **Season {closed} ended.** Season {new} has started. "
            f"The {count} players lost {SEASON_ELO_LOSS} elo in the general leaderboard.\n"
            f"Backup saved as `{backup}`.")
        res = await get_season_leaderboard(closed)
        if res and res[0]:
            lb = LeaderboardView(res[0], "elo", f"SEASON {closed} LEADERBOARD",
                                 f"Season {closed} (ended)", True)
            lb.message = await i.followup.send(file=await lb.make_file(), view=lb)

    current_year, current_season = await get_current()
    await interaction.response.send_message(
        f"⚠️ This will end **season {current_season}**: its leaderboard is saved, a new season starts, "
        f"and **every player loses {SEASON_ELO_LOSS} general elo** (minimum 0). "
        f"A backup of the database is saved first.\nDo you want to continue?",
        view=ConfirmView(interaction.user.id, do_season), ephemeral=True)


@bot.tree.command(name="endyear", description="End the year: save everything and start from scratch (staff only)")
@staff_only()
@admin_only()
async def endyear(interaction: discord.Interaction):
    async def do_year(i: discord.Interaction):
        backup = backup_db()
        old_year, new_year, new_season, count = await end_year()
        await i.edit_original_response(content="Done.")
        await i.followup.send(
            f"🎆 **Year {old_year} ended.** The final leaderboard and the profiles of {count} players "
            f"were saved: see them with `/leaderboard year:{old_year}` and `/profile year:{old_year}`. "
            f"The game modes were saved too (use the `mode` option.)\n"
            f"Year {new_year} (season {new_season}) has started: everyone is back to 0 elo (tier D), "
            f"in every mode. Discord links were kept.\nBackup saved as `{backup}`.")
        rows = await get_year_leaderboard(old_year)
        if rows:
            lb = LeaderboardView(rows, "elo", f"YEAR {old_year} FINAL LEADERBOARD",
                                 f"Year {old_year} (final)", False)
            lb.message = await i.followup.send(file=await lb.make_file(), view=lb)

    current_year, current_season = await get_current()
    await interaction.response.send_message(
        f"⚠️ **This will end year {current_year}.** The final leaderboards and the profiles are saved "
        f"(normal mode and every game mode), then **every player goes back to 0 elo (tier D)** in all of "
        f"them, the current season ends, open paused matches are discarded and this year's matches "
        f"disappear from `/history` and `/matches`. Discord links are kept. A backup of the database "
        f"is saved first.\nAre you sure you want to continue?",
        view=ConfirmView(interaction.user.id, do_year), ephemeral=True)


# ---------- paused matches (normal mode only) ----------

@bot.tree.command(name="pause", description="Save an interrupted match to be continued later (staff only)")
@app_commands.describe(player1="Username of player 1", player2="Username of player 2",
                       games1="Games won so far by player 1 (0-2)",
                       games2="Games won so far by player 2 (0-2)")
@staff_only()
async def pause_cmd(interaction: discord.Interaction, player1: str, player2: str,
                    games1: app_commands.Range[int, 0, 2], games2: app_commands.Range[int, 0, 2]):
    await interaction.response.defer()
    r1 = await lookup_ign(interaction, player1)
    if r1 is None:
        return
    r2 = await lookup_ign(interaction, player2)
    if r2 is None:
        return
    uuid1, name1 = r1
    uuid2, name2 = r2

    if uuid1 == uuid2:
        await interaction.followup.send("The two players can't be the same person.")
        return
    if games1 + games2 == 0:
        await interaction.followup.send("No games have been played yet, so there is nothing to save.")
        return

    p1 = await get_player(uuid1)
    p2 = await get_player(uuid2)
    if p1 is None or p2 is None:
        missing = esc(name1 if p1 is None else name2)
        await interaction.followup.send(f"**{missing}** doesn't have a tier yet: use `/settier` first.")
        return

    pause_id = await create_pause(uuid1, uuid2, games1, games2)
    a = {"uuid": uuid1, "name": name1, "tier": p1["tier"], "elo": p1["elo"], "games": games1}
    b = {"uuid": uuid2, "name": name2, "tier": p2["tier"], "elo": p2["elo"], "games": games2}
    data = await build_pause_image(a, b, pause_id, datetime.now(timezone.utc).strftime("%m/%d/%Y"))
    await interaction.followup.send(file=png_file(data, "paused.png"))


@bot.tree.command(name="paused", description="Show the matches waiting to be continued")
async def paused(interaction: discord.Interaction):
    await interaction.response.defer()
    total = await count_open_pauses()
    if total == 0:
        await interaction.followup.send("There are no paused matches.")
        return
    rows = await list_open_pauses(10)
    entries = [{"id": r["id"], "date": fmt_date(r["created_at"]),
                "p1": r["p1_ign"], "p1_uuid": r["p1_uuid"], "g1": r["p1_games"],
                "p2": r["p2_ign"], "p2_uuid": r["p2_uuid"], "g2": r["p2_games"]} for r in rows]
    data = await build_paused_list_image(entries, total)
    await interaction.followup.send(file=png_file(data, "paused_list.png"))


@bot.tree.command(name="resume", description="Finish a paused match and log the final result (staff only)")
@app_commands.describe(pause_id="Paused match number (see /paused)",
                       winner="Username of the winner", score="Final score")
@app_commands.choices(score=SCORE_CHOICES)
@staff_only()
async def resume_cmd(interaction: discord.Interaction, pause_id: int, winner: str,
                     score: app_commands.Choice[int]):
    await interaction.response.defer()
    pm = await get_pause(pause_id)
    if pm is None:
        await interaction.followup.send(f"Paused match #{pause_id} does not exist.")
        return
    if pm["status"] != "open":
        await interaction.followup.send(f"Paused match #{pause_id} was already {pm['status']}.")
        return

    result = await lookup_ign(interaction, winner)
    if result is None:
        return
    uuid_w, name_w = result

    if uuid_w == pm["p1_uuid"]:
        uuid_l, name_l, loser_games_before = pm["p2_uuid"], pm["p2_ign"], pm["p2_games"]
    elif uuid_w == pm["p2_uuid"]:
        uuid_l, name_l, loser_games_before = pm["p1_uuid"], pm["p1_ign"], pm["p1_games"]
    else:
        await interaction.followup.send(
            f"**{esc(name_w)}** is not part of paused match #{pause_id}.")
        return

    if score.value < loser_games_before:
        await interaction.followup.send(
            f"**{esc(name_l)}** already had {loser_games_before} games, "
            f"so the final score can't be {score.name}.")
        return

    if await log_match(interaction, uuid_w, name_w, uuid_l, name_l, score.name, score.value):
        await close_pause(pause_id, "resumed")


@bot.tree.command(name="droppause", description="Discard a paused match that won't be continued (staff only)")
@app_commands.describe(pause_id="Paused match number (see /paused)")
@staff_only()
async def droppause(interaction: discord.Interaction, pause_id: int):
    await interaction.response.defer()
    pm = await get_pause(pause_id)
    if pm is None:
        await interaction.followup.send(f"Paused match #{pause_id} does not exist.")
        return
    if pm["status"] != "open":
        await interaction.followup.send(f"Paused match #{pause_id} was already {pm['status']}.")
        return
    await close_pause(pause_id, "dropped")
    await interaction.followup.send(
        f"🗑️ Paused match #{pause_id} ({esc(pm['p1_ign'])} "
        f"{pm['p1_games']}-{pm['p2_games']} {esc(pm['p2_ign'])}) was discarded.")


# ---------- profile ----------

@bot.tree.command(name="profile", description="Show a player's profile (general, or of a season / past year / mode)")
@app_commands.describe(ign="Minecraft username (leave empty for your linked account)",
                       season="Show the profile of a season",
                       year="Show the profile of a past year",
                       mode="Game mode (leave empty for the normal mode)")
async def profile(interaction: discord.Interaction, ign: Optional[str] = None,
                  season: Optional[int] = None, year: Optional[int] = None,
                  mode: Optional[str] = None):
    await interaction.response.defer()
    if season is not None and year is not None:
        await interaction.followup.send("Choose either a season or a year, not both.")
        return
    ok, md = await resolve_mode(interaction, mode)
    if not ok:
        return
    if md and season is not None:
        await interaction.followup.send("Game modes don't have seasons.")
        return
    mode_id = md["id"] if md else None
    mode_up = md["name"].upper() if md else None

    target = await resolve_target(interaction, ign)
    if target is None:
        return
    uuid, name, fresh = target

    # ----- season profile (normal mode) -----
    if season is not None:
        res = await get_season_leaderboard(season)
        if res is None:
            await interaction.followup.send(f"Season {season} does not exist.")
            return
        by_elo = sort_rows(res[0], "elo")
        idx = next((i for i, r in enumerate(by_elo) if r["uuid"] == uuid), None)
        if idx is None:
            await interaction.followup.send(
                f"**{esc(name)}** didn't play any match in season {season}.")
            return
        r = by_elo[idx]
        forfeits = await count_forfeits(uuid, season_id=season)
        winrate = f"{r['wins'] / r['matches'] * 100:.0f}%" if r["matches"] else "-"
        data = await build_profile_image(uuid, r["ign"], r["tier"], r["elo"], idx + 1,
                                         r["wins"], r["losses"], winrate, forfeits,
                                         title=f"SEASON {season} PROFILE", scope=f"Season {season}")
        await interaction.followup.send(file=png_file(data, "profile.png"))
        return

    # ----- archived year profile -----
    if year is not None:
        rows = await get_year_leaderboard(year, mode_id=mode_id)
        if rows is None:
            await interaction.followup.send(f"Year {year} does not exist or hasn't ended yet.")
            return
        by_elo = sort_rows(rows, "elo")
        idx = next((i for i, r in enumerate(by_elo) if r["uuid"] == uuid), None)
        if idx is None:
            await interaction.followup.send(
                f"**{esc(name)}** is not in the final leaderboard of year {year}.")
            return
        r = by_elo[idx]
        winrate = f"{r['wins'] / r['matches'] * 100:.0f}%" if r["matches"] else "-"
        title = f"{mode_up} YEAR {year} PROFILE" if md else f"YEAR {year} PROFILE"
        scope = f"{md['name']} · Year {year} (final)" if md else f"Year {year} (final)"
        data = await build_profile_image(uuid, r["ign"], r["tier"], r["elo"], idx + 1,
                                         r["wins"], r["losses"], winrate, r["forfeits"],
                                         title=title, scope=scope)
        await interaction.followup.send(file=png_file(data, "profile.png"))
        return

    # ----- current profile (normal mode or a game mode) -----
    p = await get_player(uuid, mode_id=mode_id)
    if p is None:
        where = f" in **{esc(md['name'])}**" if md else ""
        await interaction.followup.send(f"**{esc(name)}** doesn't have a tier{where} yet.")
        return

    if fresh:
        await upsert_player(uuid, name, mode_id=mode_id)   # keeps the stored name up to date
    else:
        name = p["ign"]

    wins, losses = await get_stats(uuid, mode_id=mode_id)
    forfeits = await count_forfeits(uuid, mode_id=mode_id)
    total = wins + losses
    winrate = f"{wins / total * 100:.0f}%" if total else "-"
    rank = await get_rank(p["elo"], mode_id=mode_id)

    data = await build_profile_image(uuid, name, p["tier"], p["elo"], rank, wins, losses,
                                     winrate, forfeits,
                                     title=f"{mode_up} PROFILE" if md else "PLAYER PROFILE",
                                     scope=md["name"] if md else "")
    await interaction.followup.send(file=png_file(data, "profile.png"))


@profile.autocomplete("season")
async def profile_season_ac(interaction: discord.Interaction, current: str):
    return await season_choices(current)


@profile.autocomplete("year")
async def profile_year_ac(interaction: discord.Interaction, current: str):
    return await year_choices(current)


# ---------- player match history ----------

def history_entries(rows, uuid):
    entries = []
    for m in rows:
        cancelled = bool(m["cancelled"])
        base = {"id": match_number(m), "date": fmt_date(m["played_at"]), "score": m["score"] or "-",
                "cancelled": cancelled, "forfeit": bool(m["forfeit"])}
        if m["winner_uuid"] == uuid:
            delta = 0 if cancelled else m["winner_elo_after"] - m["winner_elo_before"]
            entries.append({**base, "win": True, "opp": m["loser_ign"], "opp_uuid": m["loser_uuid"],
                            "delta": delta})
        else:
            delta = 0 if cancelled else m["loser_elo_after"] - m["loser_elo_before"]
            entries.append({**base, "win": False, "opp": m["winner_ign"], "opp_uuid": m["winner_uuid"],
                            "delta": delta})
    return entries


class HistoryView(PagedView):
    PER_PAGE = 10

    def __init__(self, uuid, name, total, mode_id=None):
        super().__init__(timeout=180)
        self.uuid = uuid
        self.name = name
        self.total = total
        self.mode_id = mode_id
        self.pages = max(1, (total + self.PER_PAGE - 1) // self.PER_PAGE)
        self.update_buttons()

    async def make_file(self) -> discord.File:
        rows = await get_history(self.uuid, self.PER_PAGE, self.page * self.PER_PAGE, mode_id=self.mode_id)
        entries = history_entries(rows, self.uuid)
        data = await build_history_image(self.name, entries, self.page, self.pages, self.total)
        return png_file(data, "history.png")


@bot.tree.command(name="history", description="Show a player's match history (current year)")
@app_commands.describe(ign="Minecraft username (leave empty for your linked account)",
                       mode="Game mode (leave empty for the normal mode)")
async def history(interaction: discord.Interaction, ign: Optional[str] = None,
                  mode: Optional[str] = None):
    await interaction.response.defer()
    ok, md = await resolve_mode(interaction, mode)
    if not ok:
        return
    mode_id = md["id"] if md else None
    target = await resolve_target(interaction, ign)
    if target is None:
        return
    uuid, name, fresh = target

    p = await get_player(uuid, include_removed=True, mode_id=mode_id)
    if p is None:
        where = f" in **{esc(md['name'])}**" if md else ""
        await interaction.followup.send(f"**{esc(name)}** doesn't have a tier{where} yet.")
        return
    if not fresh:
        name = p["ign"]

    total = await count_history(uuid, mode_id=mode_id)
    if total == 0:
        await interaction.followup.send(f"**{esc(name)}** hasn't played any matches yet.")
        return

    shown = f"{name} ({md['name']})" if md else name
    view = HistoryView(uuid, shown, total, mode_id)
    file = await view.make_file()
    content = f"**{esc(md['name'])}**" if md else None
    if view.pages > 1:
        view.message = await interaction.followup.send(content=content, file=file, view=view)
    else:
        await interaction.followup.send(content=content, file=file)


# ---------- guild-wide match log and cancelled matches ----------

def log_entries(rows):
    entries = []
    for m in rows:
        cancelled = bool(m["cancelled"])
        entries.append({
            "id": match_number(m), "date": fmt_date(m["played_at"]), "score": m["score"] or "-",
            "cancelled": cancelled, "forfeit": bool(m["forfeit"]),
            "winner": m["winner_ign"], "winner_uuid": m["winner_uuid"],
            "loser": m["loser_ign"], "loser_uuid": m["loser_uuid"],
            "w_delta": 0 if cancelled else m["winner_elo_after"] - m["winner_elo_before"],
            "l_delta": 0 if cancelled else m["loser_elo_after"] - m["loser_elo_before"],
        })
    return entries


class MatchLogView(PagedView):
    PER_PAGE = 10

    def __init__(self, total, cancelled=False, mode_id=None):
        super().__init__(timeout=180)
        self.total = total          # already capped by MATCH_LOG_LIMIT
        self.cancelled = cancelled
        self.mode_id = mode_id
        self.pages = max(1, (total + self.PER_PAGE - 1) // self.PER_PAGE)
        self.update_buttons()

    async def make_file(self) -> discord.File:
        offset = self.page * self.PER_PAGE
        limit = min(self.PER_PAGE, self.total - offset)
        if self.cancelled:
            rows = await get_cancelled_matches(limit, offset, mode_id=self.mode_id)
        else:
            rows = await get_all_matches(limit, offset, mode_id=self.mode_id)
        data = await build_matchlog_image(log_entries(rows), self.page, self.pages, self.total,
                                          cancelled_view=self.cancelled)
        return png_file(data, "cancelled.png" if self.cancelled else "matches.png")


async def send_match_list(interaction: discord.Interaction, cancelled: bool, mode: Optional[str]):
    await interaction.response.defer()
    ok, md = await resolve_mode(interaction, mode)
    if not ok:
        return
    mode_id = md["id"] if md else None
    counter = count_cancelled_matches if cancelled else count_all_matches
    total = min(await counter(mode_id=mode_id), MATCH_LOG_LIMIT)
    if total == 0:
        await interaction.followup.send(
            "No matches have been cancelled." if cancelled else "No matches have been logged yet.")
        return
    view = MatchLogView(total, cancelled=cancelled, mode_id=mode_id)
    file = await view.make_file()
    content = f"**{esc(md['name'])}**" if md else None
    if view.pages > 1:
        view.message = await interaction.followup.send(content=content, file=file, view=view)
    else:
        await interaction.followup.send(content=content, file=file)


@bot.tree.command(name="matches", description="Show the latest matches of the guild")
@app_commands.describe(mode="Game mode (leave empty for the normal mode)")
async def matches(interaction: discord.Interaction, mode: Optional[str] = None):
    await send_match_list(interaction, False, mode)


@bot.tree.command(name="cancelled", description="Show the latest cancelled matches")
@app_commands.describe(mode="Game mode (leave empty for the normal mode)")
async def cancelled_cmd(interaction: discord.Interaction, mode: Optional[str] = None):
    await send_match_list(interaction, True, mode)


# ---------- leaderboard (general, season, archived year, game modes) ----------

class SortSelect(discord.ui.Select):
    def __init__(self, current: str):
        options = [discord.SelectOption(label=label, value=key, default=(key == current))
                   for key, label in SORT_OPTIONS]
        super().__init__(placeholder="Sort by...", options=options, row=1)

    async def callback(self, interaction: discord.Interaction):
        view: "LeaderboardView" = self.view
        await interaction.response.defer()
        view.set_sort(self.values[0])
        await interaction.edit_original_response(attachments=[await view.make_file()], view=view)


class LeaderboardView(PagedView):
    PER_PAGE = 10

    def __init__(self, rows, sort="elo", title="GLOBAL LEADERBOARD", scope="", dual=False):
        super().__init__(timeout=300)
        self.base_rows = rows
        self.sort = sort
        self.title = title
        self.scope = scope
        self.dual = dual
        self.rows = sort_rows(rows, sort)
        self.pages = max(1, (len(self.rows) + self.PER_PAGE - 1) // self.PER_PAGE)
        self.select = SortSelect(sort)
        self.add_item(self.select)
        self.update_buttons()

    def set_sort(self, sort: str):
        self.sort = sort
        self.rows = sort_rows(self.base_rows, sort)
        self.page = 0
        for option in self.select.options:
            option.default = (option.value == sort)
        self.update_buttons()

    async def make_file(self) -> discord.File:
        data = await build_leaderboard_image(self.rows, self.page, self.PER_PAGE, sort=self.sort,
                                             title=self.title, scope=self.scope, dual=self.dual)
        return png_file(data, "leaderboard.png")


@bot.tree.command(name="leaderboard", description="Show the leaderboard (general, season, past year or game mode)")
@app_commands.describe(sort="What to sort by (you can also change it with the menu)",
                       season="Show the leaderboard of a season",
                       year="Show the final leaderboard of a past year",
                       mode="Game mode (leave empty for the normal mode)")
@app_commands.choices(sort=SORT_CHOICES)
async def leaderboard(interaction: discord.Interaction,
                      sort: Optional[app_commands.Choice[str]] = None,
                      season: Optional[int] = None, year: Optional[int] = None,
                      mode: Optional[str] = None):
    await interaction.response.defer()
    if season is not None and year is not None:
        await interaction.followup.send("Choose either a season or a year, not both.")
        return
    ok, md = await resolve_mode(interaction, mode)
    if not ok:
        return
    if md and season is not None:
        await interaction.followup.send("Game modes don't have seasons.")
        return
    mode_id = md["id"] if md else None
    mode_up = md["name"].upper() if md else None
    key = sort.value if sort else "elo"

    if season is not None:
        res = await get_season_leaderboard(season)
        if res is None:
            await interaction.followup.send(f"Season {season} does not exist.")
            return
        rows, is_current = res
        if not rows:
            await interaction.followup.send(f"No matches have been played in season {season} yet.")
            return
        scope = f"Season {season} " + ("(current)" if is_current else "(ended)")
        view = LeaderboardView(rows, key, f"SEASON {season} LEADERBOARD", scope, True)
    elif year is not None:
        rows = await get_year_leaderboard(year, mode_id=mode_id)
        if rows is None:
            await interaction.followup.send(f"Year {year} does not exist or hasn't ended yet.")
            return
        if not rows:
            await interaction.followup.send(f"Year {year} has no players in its final leaderboard.")
            return
        title = f"{mode_up} LEADERBOARD" if md else f"YEAR {year} FINAL LEADERBOARD"
        view = LeaderboardView(rows, key, title, f"Year {year} (final)", False)
    else:
        rows = await get_leaderboard(mode_id=mode_id)
        if not rows:
            await interaction.followup.send(
                f"There are no players in **{esc(md['name'])}** yet." if md else "There are no players yet.")
            return
        title = f"{mode_up} LEADERBOARD" if md else "GLOBAL LEADERBOARD"
        view = LeaderboardView(rows, key, title, "", False)

    view.message = await interaction.followup.send(file=await view.make_file(), view=view)


@leaderboard.autocomplete("season")
async def leaderboard_season_ac(interaction: discord.Interaction, current: str):
    return await season_choices(current)


@leaderboard.autocomplete("year")
async def leaderboard_year_ac(interaction: discord.Interaction, current: str):
    return await year_choices(current)


@bot.tree.command(name="info", description="Show general stats about players, matches and tiers")
async def info(interaction: discord.Interaction):
    await interaction.response.defer()
    data = await build_info_image(await get_info())
    await interaction.followup.send(file=png_file(data, "info.png"))

@bot.tree.command(name="resetall", description="TEST ONLY: delete ALL data and start from scratch (administrators)")
@app_commands.describe(confirm="Type RESET EVERYTHING to confirm")
async def resetall(interaction: discord.Interaction, confirm: str):
    member = interaction.user
    if not (isinstance(member, discord.Member) and member.guild_permissions.administrator):
        await interaction.response.send_message("Only server administrators can use this command.", ephemeral=True)
        return
    if confirm.strip() != "RESET EVERYTHING":
        await interaction.response.send_message("Type exactly `RESET EVERYTHING` in the confirm option.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    backup = backup_db()
    await reset_all_data()
    await interaction.followup.send(f"🗑️ Everything was deleted. A backup was saved as `{backup}`.")

# the "mode" option suggests the game modes; the score of /match suggests the valid scores
for _cmd in (settier, setelo, match, forfeit_cmd, removeplayer, profile, history,
             matches, cancelled_cmd, leaderboard):
    _cmd.autocomplete("mode")(mode_autocomplete)
match.autocomplete("score")(score_autocomplete)


bot.run(os.getenv("TOKEN"))
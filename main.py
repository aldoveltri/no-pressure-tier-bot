import asyncio
import io
import logging
import os
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from database import (init_db, init_extras, upsert_player, get_player, save_match,
                      get_stats, count_forfeits, get_rank, get_leaderboard,
                      get_history, count_history, get_all_matches, count_all_matches,
                      count_cancelled_matches, get_cancelled_matches,
                      get_link, remove_link, link_account,
                      get_match, last_match_id, cancel_match,
                      backup_db, apply_season_reset, remove_player, get_info,
                      create_pause, get_pause, list_open_pauses, count_open_pauses, close_pause)
from mojang import get_uuid
from hypixel import get_hypixel_discord, normalize_handle, NoApiKey, KeyRejected, HypixelError
from config import TIERS, STAFF_ROLE, TIER_MIN, MATCH_LOG_LIMIT, tier_from_elo
from elo import calcola
from graphics import (build_leaderboard_image, build_match_image, build_profile_image,
                      build_history_image, build_settier_image, build_matchlog_image,
                      build_setelo_image, build_info_image,
                      build_pause_image, build_paused_list_image)

load_dotenv()

# ---------- logging: errors are also written to bot_errors.log ----------
_handler = RotatingFileHandler("bot_errors.log", maxBytes=500_000, backupCount=2, encoding="utf-8")
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
log = logging.getLogger("tierbot")
log.setLevel(logging.INFO)
log.addHandler(_handler)
log.addHandler(logging.StreamHandler())

bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())

NO_MENTIONS = discord.AllowedMentions.none()
SEASON_ELO_LOSS = 100
GENERIC_ERROR = ("Something went wrong while running this command. "
                 "Please try again in a moment, and tell the staff if it keeps happening.")

SCORE_CHOICES = [
    app_commands.Choice(name="3-0", value=0),
    app_commands.Choice(name="3-1", value=1),
    app_commands.Choice(name="3-2", value=2),
]


# ---------- small helpers ----------

def png_file(data: bytes, name: str) -> discord.File:
    return discord.File(io.BytesIO(data), filename=name)


def fmt_date(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%m/%d/%Y")
    except Exception:
        return "-"


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


async def resolve_target(interaction: discord.Interaction, ign, user):
    """Works out which player a command is about.
    Priority: typed username > linked account of the mentioned member > caller's linked account.
    Returns (uuid, name, fresh) or None (an error message has already been sent).
    'fresh' is True when the name comes straight from Mojang."""
    if ign:
        result = await lookup_ign(interaction, ign)
        if result is None:
            return None
        return result[0], result[1], True

    target = user or interaction.user
    link_row = await get_link(target.id)
    if link_row is None:
        if user:
            msg = f"{user.display_name} hasn't linked a Minecraft account yet."
        else:
            msg = "You haven't linked a Minecraft account yet. Use `/link` or type a username."
        await interaction.followup.send(msg, allowed_mentions=NO_MENTIONS)
        return None
    return link_row["uuid"], link_row["ign"], False


async def log_match(interaction: discord.Interaction, uuid_w, name_w, uuid_l, name_l,
                    score_name: str, loser_games: int, forfeit: bool = False) -> bool:
    """Registers a finished match, updates the elo and sends the result card.
    Returns False if the match could not be logged (a message has been sent)."""
    p_w = await get_player(uuid_w)
    p_l = await get_player(uuid_l)
    if p_w is None or p_l is None:
        missing = discord.utils.escape_markdown(name_w if p_w is None else name_l)
        await interaction.followup.send(
            f"**{missing}** doesn't have a tier yet: use `/settier` first.")
        return False

    gain, loss = calcola(p_w["elo"], p_l["elo"], loser_games)
    new_w, new_l = p_w["elo"] + gain, p_l["elo"] + loss

    now = datetime.now(timezone.utc)
    match_id = await save_match(uuid_w, uuid_l, p_w["elo"], new_w, p_l["elo"], new_l,
                                score_name, played_at=now.isoformat(), forfeit=forfeit)

    w = {"uuid": uuid_w, "name": name_w, "before": p_w["elo"], "after": new_w, "delta": gain,
         "tier_before": tier_from_elo(p_w["elo"]), "tier_after": tier_from_elo(new_w)}
    l = {"uuid": uuid_l, "name": name_l, "before": p_l["elo"], "after": new_l,
         "delta": new_l - p_l["elo"],
         "tier_before": tier_from_elo(p_l["elo"]), "tier_after": tier_from_elo(new_l)}

    data = await build_match_image(w, l, score_name, match_id, now.strftime("%m/%d/%Y"),
                                   forfeit=forfeit)
    await interaction.followup.send(file=png_file(data, "match.png"))
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

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.change_page(interaction, -1)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.change_page(interaction, 1)


# ---------- startup and error handling ----------

async def setup_hook():
    await init_db()
    await init_extras()
    await bot.load_extension("namesync")

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
@app_commands.describe(ign="Minecraft username of the player", tier="Tier to assign")
@app_commands.choices(tier=[app_commands.Choice(name=t, value=t) for t in TIERS])
@staff_only()
async def settier(interaction: discord.Interaction, ign: str, tier: app_commands.Choice[str]):
    await interaction.response.defer()
    result = await lookup_ign(interaction, ign)
    if result is None:
        return
    uuid, name = result

    old = await get_player(uuid)
    old_tier = old["tier"] if old else None
    await upsert_player(uuid, name, TIER_MIN[tier.value])

    data = await build_settier_image(uuid, name, old_tier, tier.value, TIER_MIN[tier.value])
    await interaction.followup.send(file=png_file(data, "tier.png"))


@bot.tree.command(name="setelo", description="Set a player's elo manually (staff only)")
@app_commands.describe(ign="Minecraft username of the player", elo="New elo (0-5000)")
@staff_only()
async def setelo(interaction: discord.Interaction, ign: str, elo: app_commands.Range[int, 0, 5000]):
    await interaction.response.defer()
    result = await lookup_ign(interaction, ign)
    if result is None:
        return
    uuid, name = result

    old = await get_player(uuid)
    old_elo = old["elo"] if old else None
    await upsert_player(uuid, name, elo)

    data = await build_setelo_image(uuid, name, old_elo, elo)
    await interaction.followup.send(file=png_file(data, "elo.png"))


@bot.tree.command(name="match", description="Log a 1v1 result (staff only)")
@app_commands.describe(winner="Username of the winner", loser="Username of the loser",
                       score="Match score")
@app_commands.choices(score=SCORE_CHOICES)
@staff_only()
async def match(interaction: discord.Interaction, winner: str, loser: str,
                score: app_commands.Choice[int]):
    await interaction.response.defer()
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

    await log_match(interaction, uuid_w, name_w, uuid_l, name_l, score.name, score.value)


@bot.tree.command(name="forfeit", description="Log a forfeit: the player who left loses 3-0 (staff only)")
@app_commands.describe(player="Username of the player who left the match",
                       opponent="Username of the opponent (wins 3-0)",
                       pause_id="Optional: number of the paused match this forfeit closes")
@staff_only()
async def forfeit_cmd(interaction: discord.Interaction, player: str, opponent: str,
                      pause_id: Optional[int] = None):
    await interaction.response.defer()
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

    # whatever the situation was, the player who left loses 3-0
    ok = await log_match(interaction, uuid_w, name_w, uuid_l, name_l, "3-0", 0, forfeit=True)
    if ok and pm is not None:
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

    # the match must be the most recent one for both players
    for uuid in (m["winner_uuid"], m["loser_uuid"]):
        last = await last_match_id(uuid)
        if last != match_id:
            await interaction.followup.send(
                f"Match #{match_id} can't be cancelled yet: one of the players has a newer "
                f"match (#{last}). Cancel the newer one first.")
            return

    # and their elo must not have changed since (new /settier, /setelo, season reset...)
    p_w = await get_player(m["winner_uuid"], include_removed=True)
    p_l = await get_player(m["loser_uuid"], include_removed=True)
    if p_w["elo"] != m["winner_elo_after"] or p_l["elo"] != m["loser_elo_after"]:
        await interaction.followup.send(
            f"Match #{match_id} can't be cancelled: the elo of one of the players changed "
            f"after it (for example a manual change or a season reset).")
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

    data = await build_match_image(w, l, m["score"] or "-", match_id,
                                   fmt_date(m["played_at"]), title="MATCH CANCELLED",
                                   forfeit=bool(m["forfeit"]))
    await interaction.followup.send(file=png_file(data, "undo.png"))


@bot.tree.command(name="removeplayer", description="Remove a player from the leaderboard and profiles (staff only)")
@app_commands.describe(ign="Minecraft username of the player to remove")
@staff_only()
async def removeplayer(interaction: discord.Interaction, ign: str):
    await interaction.response.defer(ephemeral=True)
    result = await lookup_ign(interaction, ign)
    if result is None:
        return
    uuid, name = result

    player = await get_player(uuid)
    if player is None:
        await interaction.followup.send(f"**{name}** is not in the leaderboard.")
        return
    safe_name = discord.utils.escape_markdown(name)

    async def do_remove(i: discord.Interaction):
        kept = await remove_player(uuid)
        await i.edit_original_response(content="Done.")
        await i.followup.send(
            f"🗑️ **{safe_name}** was removed from the leaderboard and profiles. "
            f"Their {kept} matches were kept in the history.")

    await interaction.followup.send(
        f"⚠️ This will remove **{safe_name}** (elo {player['elo']}) from the leaderboard and `/profile`, "
        f"and unlink their Discord account. Their matches **stay** in the history and match log. "
        f"You can bring them back later with `/settier` or `/setelo`. Do you want to continue?",
        view=ConfirmView(interaction.user.id, do_remove), ephemeral=True)


@bot.tree.command(name="endseason", description="End the season: every player loses 100 elo (staff only)")
@staff_only()
async def endseason(interaction: discord.Interaction):
    async def do_season(i: discord.Interaction):
        backup = backup_db()
        count = await apply_season_reset(SEASON_ELO_LOSS)
        await i.edit_original_response(content="Done.")
        await i.followup.send(
            f"🏁 **Season ended.** {count} players lost {SEASON_ELO_LOSS} elo.\n"
            f"Backup saved as `{backup}`.")
        rows = await get_leaderboard()
        if rows:
            lb = LeaderboardView(rows)
            lb.message = await i.followup.send(file=await lb.make_file(), view=lb)

    await interaction.response.send_message(
        f"⚠️ This will remove **{SEASON_ELO_LOSS} elo** from **every player** (minimum 0) "
        f"and update their tiers. A backup of the database is saved first.\n"
        f"Do you want to continue?",
        view=ConfirmView(interaction.user.id, do_season), ephemeral=True)


# ---------- paused matches ----------

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
        missing = discord.utils.escape_markdown(name1 if p1 is None else name2)
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
            f"**{discord.utils.escape_markdown(name_w)}** is not part of paused match #{pause_id}.")
        return

    if score.value < loser_games_before:
        await interaction.followup.send(
            f"**{discord.utils.escape_markdown(name_l)}** already had {loser_games_before} games, "
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
        f"🗑️ Paused match #{pause_id} ({discord.utils.escape_markdown(pm['p1_ign'])} "
        f"{pm['p1_games']}-{pm['p2_games']} {discord.utils.escape_markdown(pm['p2_ign'])}) was discarded.")


# ---------- player commands ----------

@bot.tree.command(name="profile", description="Show a player's tier, elo and record")
@app_commands.describe(ign="Minecraft username (leave empty for your linked account)",
                       user="Show the linked account of another member")
async def profile(interaction: discord.Interaction, ign: Optional[str] = None,
                  user: Optional[discord.Member] = None):
    await interaction.response.defer()
    target = await resolve_target(interaction, ign, user)
    if target is None:
        return
    uuid, name, fresh = target

    p = await get_player(uuid)
    if p is None:
        await interaction.followup.send(f"**{discord.utils.escape_markdown(name)}** doesn't have a tier yet.")
        return

    if fresh:
        await upsert_player(uuid, name)   # keeps the stored name up to date
    else:
        name = p["ign"]

    wins, losses = await get_stats(uuid)
    forfeits = await count_forfeits(uuid)
    total = wins + losses
    winrate = f"{wins / total * 100:.0f}%" if total else "-"
    rank = await get_rank(p["elo"])

    data = await build_profile_image(uuid, name, p["tier"], p["elo"], rank, wins, losses,
                                     winrate, forfeits)
    await interaction.followup.send(file=png_file(data, "profile.png"))


def history_entries(rows, uuid):
    entries = []
    for m in rows:
        cancelled = bool(m["cancelled"])
        base = {"id": m["id"], "date": fmt_date(m["played_at"]), "score": m["score"] or "-",
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

    def __init__(self, uuid, name, total):
        super().__init__(timeout=180)
        self.uuid = uuid
        self.name = name
        self.total = total
        self.pages = max(1, (total + self.PER_PAGE - 1) // self.PER_PAGE)
        self.update_buttons()

    async def make_file(self) -> discord.File:
        rows = await get_history(self.uuid, self.PER_PAGE, self.page * self.PER_PAGE)
        entries = history_entries(rows, self.uuid)
        data = await build_history_image(self.name, entries, self.page, self.pages, self.total)
        return png_file(data, "history.png")


@bot.tree.command(name="history", description="Show a player's match history")
@app_commands.describe(ign="Minecraft username (leave empty for your linked account)",
                       user="Show the linked account of another member")
async def history(interaction: discord.Interaction, ign: Optional[str] = None,
                  user: Optional[discord.Member] = None):
    await interaction.response.defer()
    target = await resolve_target(interaction, ign, user)
    if target is None:
        return
    uuid, name, fresh = target

    p = await get_player(uuid, include_removed=True)
    if p is None:
        await interaction.followup.send(f"**{discord.utils.escape_markdown(name)}** doesn't have a tier yet.")
        return
    if not fresh:
        name = p["ign"]

    total = await count_history(uuid)
    if total == 0:
        await interaction.followup.send(f"**{discord.utils.escape_markdown(name)}** hasn't played any matches yet.")
        return

    view = HistoryView(uuid, name, total)
    file = await view.make_file()
    if view.pages > 1:
        view.message = await interaction.followup.send(file=file, view=view)
    else:
        await interaction.followup.send(file=file)


# ---------- guild-wide match log ----------

def log_entries(rows):
    entries = []
    for m in rows:
        cancelled = bool(m["cancelled"])
        entries.append({
            "id": m["id"], "date": fmt_date(m["played_at"]), "score": m["score"] or "-",
            "cancelled": cancelled, "forfeit": bool(m["forfeit"]),
            "winner": m["winner_ign"], "winner_uuid": m["winner_uuid"],
            "loser": m["loser_ign"], "loser_uuid": m["loser_uuid"],
            "w_delta": 0 if cancelled else m["winner_elo_after"] - m["winner_elo_before"],
            "l_delta": 0 if cancelled else m["loser_elo_after"] - m["loser_elo_before"],
        })
    return entries


class MatchLogView(PagedView):
    PER_PAGE = 10

    def __init__(self, total, cancelled=False):
        super().__init__(timeout=180)
        self.total = total          # already capped by MATCH_LOG_LIMIT
        self.cancelled = cancelled
        self.pages = max(1, (total + self.PER_PAGE - 1) // self.PER_PAGE)
        self.update_buttons()

    async def make_file(self) -> discord.File:
        offset = self.page * self.PER_PAGE
        limit = min(self.PER_PAGE, self.total - offset)
        if self.cancelled:
            rows = await get_cancelled_matches(limit, offset)
        else:
            rows = await get_all_matches(limit, offset)
        data = await build_matchlog_image(log_entries(rows), self.page, self.pages, self.total,
                                          cancelled_view=self.cancelled)
        return png_file(data, "cancelled.png" if self.cancelled else "matches.png")


@bot.tree.command(name="matches", description="Show the latest matches of the guild")
async def matches(interaction: discord.Interaction):
    await interaction.response.defer()
    total = min(await count_all_matches(), MATCH_LOG_LIMIT)
    if total == 0:
        await interaction.followup.send("No matches have been logged yet.")
        return
    view = MatchLogView(total)
    file = await view.make_file()
    if view.pages > 1:
        view.message = await interaction.followup.send(file=file, view=view)
    else:
        await interaction.followup.send(file=file)


@bot.tree.command(name="cancelled", description="Show the latest cancelled matches")
async def cancelled_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    total = min(await count_cancelled_matches(), MATCH_LOG_LIMIT)
    if total == 0:
        await interaction.followup.send("No matches have been cancelled.")
        return
    view = MatchLogView(total, cancelled=True)
    file = await view.make_file()
    if view.pages > 1:
        view.message = await interaction.followup.send(file=file, view=view)
    else:
        await interaction.followup.send(file=file)

class LeaderboardView(PagedView):
    PER_PAGE = 10

    def __init__(self, rows):
        super().__init__(timeout=180)
        self.rows = rows
        self.pages = max(1, (len(rows) + self.PER_PAGE - 1) // self.PER_PAGE)
        self.update_buttons()

    async def make_file(self) -> discord.File:
        data = await build_leaderboard_image(self.rows, self.page, self.PER_PAGE)
        return png_file(data, "leaderboard.png")


@bot.tree.command(name="leaderboard", description="Show the global leaderboard")
async def leaderboard(interaction: discord.Interaction):
    await interaction.response.defer()
    rows = await get_leaderboard()
    if not rows:
        await interaction.followup.send("There are no players yet.")
        return
    view = LeaderboardView(rows)
    view.message = await interaction.followup.send(file=await view.make_file(), view=view)


@bot.tree.command(name="info", description="Show general stats about players, matches and tiers")
async def info(interaction: discord.Interaction):
    await interaction.response.defer()
    data = await build_info_image(await get_info())
    await interaction.followup.send(file=png_file(data, "info.png"))


bot.run(os.getenv("TOKEN"))

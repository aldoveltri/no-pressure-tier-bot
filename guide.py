from config import TIER_RANGES, MODES
from elo import WIN, LOSS, WIN5, LOSS5, FIRST_TO


def _fmt(values):
    return " / ".join(f"{v:+d}" for v in values)


def _fmt5(values):
    return "/".join(f"{v:+d}" for v in values)


def _tiers_text():
    parts = []
    for i, (name, minimum) in enumerate(TIER_RANGES):
        if i == 0:
            parts.append(f"{name} {minimum}+")
        else:
            parts.append(f"{name} {minimum}-{TIER_RANGES[i - 1][1] - 1}")
    return "   ·   ".join(parts)


def _h(text):
    return {"type": "heading", "text": text}


def _p(text):
    return {"type": "paragraph", "text": text}


def _b(text):
    return {"type": "bullet", "text": text}


def _c(cmd, text):
    return {"type": "command", "cmd": cmd, "text": text}


def _modes_bullets():
    out = [_b("Normal: the base mode, used when you don't choose a mode. First to 3, with seasons and years.")]
    for m in MODES:
        out.append(_b(f"{m['name']}: first to {FIRST_TO[m['system']]}."))
    return out


_ft3_names = ", ".join(["Normal"] + [m["name"] for m in MODES if m["system"] == "ft3"])

GUIDE_PAGES = [
    {
        "title": "Getting started",
        "blocks": [
            _p("This bot keeps track of the guild's 1v1 tier tests. Every player has an elo and a tier, "
               "and every match changes them automatically. Use the menu or the arrows below to read "
               "the other sections of this guide."),
            _h("1. Link your account"),
            _c("/link ign:YourName",
               "Links your Discord account to your Minecraft account. It is verified with Hypixel: on "
               "Hypixel open your profile, go to Social Media > Discord and enter your Discord username, "
               "then run /link. You only need to do this once."),
            _c("/unlink", "Removes the link between your Discord and Minecraft accounts."),
            _p("After linking, /profile and /history work without typing your name. "
               "If linking doesn't work, ask the staff."),
            _h("2. Know the rules"),
            _c("/rules", "Shows the rules for 1v1s and tier tests."),
            _h("3. Check how you are doing"),
            _c("/profile", "Your tier, elo, rank and record."),
            _c("/leaderboard", "The ranking of all players."),
        ],
    },
    {
        "title": "Your stats",
        "blocks": [
            _c("/profile [ign] [season] [year] [mode]",
               "Shows tier, elo, rank, record (wins and losses), win rate and number of forfeits. "
               "Leave ign empty to see yourself. Use season:N for the stats of one season, year:N "
               "for the final profile of a past year, or the mode option for another game mode."),
            _c("/history [ign] [mode]",
               "Match history of the current year, 10 per page, with match number, date, score and the "
               "elo gained or lost. Cancelled matches appear with a grey CANCELLED label."),
            _c("/matches [mode]", "The latest matches of the whole guild."),
            _c("/cancelled [mode]", "The latest cancelled matches."),
            _c("/paused", "Matches that were interrupted and will be continued later."),
            _c("/info", "General stats: players, matches, average elo, top player and players per tier."),
            _c("/ping", "Checks that the bot is online."),
        ],
    },
    {
        "title": "Leaderboards",
        "blocks": [
            _c("/leaderboard",
               "The general leaderboard of the current year. Use the Sort by menu under the image to sort "
               "by Elo, Wins, Losses, Win rate or Matches played, and the arrows to change page. "
               "You can also choose the sorting directly with sort:."),
            _c("/leaderboard season:N",
               "The leaderboard of one season. Elo and tier are the normal ones, but W, L, WR and MP only "
               "count the matches of that season. While you type, the bot suggests the existing seasons."),
            _c("/leaderboard year:N", "The final leaderboard of a year that is already over."),
            _c("/leaderboard mode:NAME", "The leaderboard of another game mode (see the next section)."),
            _h("Columns"),
            _b("ELO: current elo"),
            _b("W: wins   ·   L: losses"),
            _b("WR: win rate   ·   MP: matches played"),
            _p("The column used for sorting is highlighted in gold."),
        ],
    },
    {
        "title": "Game modes",
        "blocks": [
            _p("Besides the normal mode, the guild plays other game modes. Each mode has its own elo, "
               "tiers, matches and leaderboard, so you can be S in one mode and C in another."),
            _h("The modes"),
            *_modes_bullets(),
            _h("How to use them"),
            _b("Choose the mode in the mode option of /profile, /history, /leaderboard, /matches and "
               "/cancelled. Leave it empty to use the normal mode. The bot suggests the modes while you type."),
            _b("The other modes only have a lifetime leaderboard: there are no seasons."),
            _b("At the end of the year (/endyear) every mode resets too and its final leaderboard is "
               "saved: /leaderboard mode:NAME year:N."),
            _b("Paused matches (/pause) only exist in the normal mode."),
            _c("/modes", "Shows the game modes."),
        ],
    },
    {
        "title": "Elo & tiers",
        "blocks": [
            _p(f"{_ft3_names}: every match is first to 3. The points depend on the score and on the elo "
               f"difference with your opponent: beating a stronger player gives more points, losing to a "
               f"weaker one costs more. Elo never goes below 0."),
            {
                "type": "table",
                "cols": ["Opponent's elo vs yours", "You win (3-0 / 3-1 / 3-2)", "You lose (3-0 / 3-1 / 3-2)"],
                "widths": [0.30, 0.35, 0.35],
                "colors": ["text", "green", "red"],
                "rows": [
                    ["100+ higher", _fmt(WIN["molto_alto"]), _fmt(LOSS["molto_alto"])],
                    ["35 to 99 higher", _fmt(WIN["alto"]), _fmt(LOSS["alto"])],
                    ["within 34", _fmt(WIN["normale"]), _fmt(LOSS["normale"])],
                    ["35 to 99 lower", _fmt(WIN["basso"]), _fmt(LOSS["basso"])],
                    ["100+ lower", _fmt(WIN["molto_basso"]), _fmt(LOSS["molto_basso"])],
                ],
            },
            _h("Tiers"),
            _p("Your tier is decided by your elo (the same tiers in every mode):"),
            _p(_tiers_text()),
        ],
    },
]

if any(m["system"] == "ft5" for m in MODES):
    _ft5_names = ", ".join(m["name"] for m in MODES if m["system"] == "ft5")
    GUIDE_PAGES.append({
        "title": "Classic 1v1 elo (FT5)",
        "blocks": [
            _p(f"{_ft5_names}: every match is first to 5. The points depend on the score (the games won "
               f"by the loser: 0, 1, 2, 3 or 4) and on the elo difference with your opponent."),
            {
                "type": "table",
                "cols": ["Opponent's elo vs yours", "You win (5-0/5-1/5-2/5-3/5-4)",
                         "You lose (0-5/1-5/2-5/3-5/4-5)"],
                "widths": [0.26, 0.38, 0.36],
                "colors": ["text", "green", "red"],
                "rows": [
                    ["100+ higher", _fmt5(WIN5["molto_alto"]), _fmt5(LOSS5["molto_alto"])],
                    ["35 to 99 higher", _fmt5(WIN5["alto"]), _fmt5(LOSS5["alto"])],
                    ["within 34", _fmt5(WIN5["normale"]), _fmt5(LOSS5["normale"])],
                    ["35 to 99 lower", _fmt5(WIN5["basso"]), _fmt5(LOSS5["basso"])],
                    ["100+ lower", _fmt5(WIN5["molto_basso"]), _fmt5(LOSS5["molto_basso"])],
                ],
            },
            _p("Elo never goes below 0."),
        ],
    })

GUIDE_PAGES += [
    {
        "title": "Seasons & years",
        "blocks": [
            _h("Seasons"),
            _p("A season is a period of play, numbered (Season 1, Season 2...). When the staff ends it with "
               "/endseason, the season leaderboard is saved forever, a new season starts, and every player "
               "loses 100 elo in the general leaderboard (minimum 0). Only the normal mode has seasons."),
            _b("See an old season: /leaderboard season:N and /profile season:N."),
            _h("Years"),
            _p("At the end of the year the staff uses /endyear: the final leaderboards and the profiles "
               "(normal mode and every other mode) are saved, then everyone goes back to 0 elo (tier D) and "
               "a new year starts. Discord links are kept."),
            _b("See a past year: /leaderboard year:N and /profile year:N (use the mode option for another mode)."),
            _b("The matches of past years are hidden from /history and /matches."),
        ],
    },
    {
        "title": "Staff: matches",
        "blocks": [
            _p("The commands below have a mode option to work on another game mode "
               "(except /pause, /resume and /droppause, which are only for the normal mode)."),
            _c("/settier ign tier",
               "Assigns a tier after a tier test. The elo is set to the minimum of that tier."),
            _c("/match winner loser score",
               "Logs a finished 1v1. The score is for example 3-1 (5-3 in Classic 1v1): the bot suggests "
               "the valid scores. Both players need a tier in that mode. The elo is calculated "
               "automatically and the result card shows the match number."),
            _c("/forfeit player opponent [player_games] [opponent_games] [pause_id]",
               "The player who left loses by forfeit: the elo changes like the biggest loss (3-0, or 5-0 in "
               "Classic 1v1), but the card shows the real score before the forfeit if you give the games "
               "of each player (or the pause_id). It is counted in the profile as a forfeit."),
            _c("/pause player1 player2 games1 games2",
               "Saves an interrupted match (for example 2-1) to be continued later. The elo doesn't change yet."),
            _c("/resume pause_id winner score", "Finishes a paused match with the final score and logs it."),
            _c("/droppause pause_id", "Discards a paused match that won't be continued."),
            _c("/undo match_id",
               "Cancels a match (of any mode) and restores the elo. It only works on the latest match of "
               "both players, in the current season or year, and if their elo wasn't changed by hand since."),
        ],
    },
    {
        "title": "Staff: admin",
        "blocks": [
            _c("/setelo ign elo", "Sets a player's elo by hand (0 to 5000). It has a mode option."),
            _c("/removeplayer ign",
               "Removes a player from the leaderboard and /profile (normal mode: also unlinks their Discord "
               "account). Their matches stay in the history. /settier or /setelo brings them back. "
               "It has a mode option."),
            _c("/link ign user:@member", "Links a member without the Hypixel verification."),
            _c("/endseason",
               "Ends the season: asks for confirmation and saves a backup of the database first."),
            _c("/endyear",
               "Ends the year: saves the final leaderboards and profiles (all modes), then resets everyone "
               "to 0 elo. Asks for confirmation and saves a backup first."),
            _c("/refreshnames",
               "Checks Mojang for changed usernames right now. It also runs automatically every 6 hours."),
        ],
    },
]
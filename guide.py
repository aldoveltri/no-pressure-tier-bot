from config import TIER_RANGES
from elo import WIN, LOSS


def _fmt(values):
    return " / ".join(f"{v:+d}" for v in values)


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
               "Leave ign empty to see yourself. Add season:N for the stats of one season, year:N "
               "for the final profile of a past year, or mode:NAME for another game mode."),
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
            _p("Besides the main mode (side rush), the guild can have other game modes, for example "
               "Bridge, Classic Duels or Crossmap. Each mode has its own elo, tiers, matches and leaderboard, "
               "so you can be S in one mode and C in another."),
            _h("How modes work"),
            _b("Add mode:NAME to /profile, /history, /leaderboard, /matches and /cancelled. "
               "Leave it empty to use the main mode."),
            _b("Modes only have a lifetime leaderboard: there are no seasons."),
            _b("At the end of the year (/endyear) modes reset too, and their final leaderboards are "
               "saved: /leaderboard mode:NAME year:N."),
            _b("The elo points are calculated with the same table as the main mode."),
            _b("Paused matches (/pause) only exist in the main mode."),
            _c("/modes", "Shows the available game modes."),
        ],
    },
    {
        "title": "Elo & tiers",
        "blocks": [
            _p("Every match is first to 3. The points depend on the score and on the elo difference with "
               "your opponent: beating a stronger player gives more points, losing to a weaker one costs "
               "more. Elo never goes below 0."),
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
            _p("Your tier is decided by your elo:"),
            _p(_tiers_text()),
        ],
    },
    {
        "title": "Seasons & years",
        "blocks": [
            _h("Seasons"),
            _p("A season is a period of play, numbered (Season 1, Season 2...). When the staff ends it with "
               "/endseason, the season leaderboard is saved forever, a new season starts, and every player "
               "loses 100 elo in the general leaderboard (minimum 0). Only the main mode has seasons."),
            _b("See an old season: /leaderboard season:N and /profile season:N."),
            _h("Years"),
            _p("At the end of the year the staff uses /endyear: the final leaderboards and the profiles "
               "(main mode and every other mode) are saved, then everyone goes back to 0 elo (tier D) and "
               "a new year starts. Discord links are kept."),
            _b("See a past year: /leaderboard year:N and /profile year:N (add mode:NAME for another mode)."),
            _b("The matches of past years are hidden from /history and /matches."),
        ],
    },
    {
        "title": "Staff: matches",
        "blocks": [
            _p("All the commands below accept mode:NAME to work on another game mode "
               "(except /pause, /resume and /droppause, which are only for the main mode)."),
            _c("/settier ign tier",
               "Assigns a tier after a tier test. The elo is set to the minimum of that tier."),
            _c("/match winner loser score",
               "Logs a finished 1v1 (3-0, 3-1 or 3-2). Both players need a tier. The elo is calculated "
               "automatically and the result card shows the match number."),
            _c("/forfeit player opponent [pause_id]",
               "The player who left loses 3-0, whatever the score was. It is marked as FORFEIT and counted "
               "in the profile. Add pause_id to close a paused match."),
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
            _c("/setelo ign elo", "Sets a player's elo by hand (0 to 5000). Accepts mode:NAME."),
            _c("/removeplayer ign",
               "Removes a player from the leaderboard and /profile (main mode: also unlinks their Discord "
               "account). Their matches stay in the history. /settier or /setelo brings them back. "
               "Accepts mode:NAME."),
            _c("/addmode name", "Creates a new game mode (for example Bridge). Max 20 characters."),
            _c("/removemode name", "Hides a game mode. Its data is kept."),
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

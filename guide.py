"""Guide (/guide): every command that members can use. Staff commands are only in the private wiki; the long
version of the old guide is kept in guide_full.py, which feeds the wiki."""

from config import TIER_RANGES, MODES


def _h(text):
    return {"type": "heading", "text": text}


def _p(text):
    return {"type": "paragraph", "text": text}


def _b(text):
    return {"type": "bullet", "text": text}


def _c(cmd, text):
    return {"type": "command", "cmd": cmd, "text": text}

START_PAGES = [
    {"title": "Welcome", "blocks": [
        _p("This bot runs the No Pressure guild: 1v1 tier tests, the weekly GEXP requirement, the breaklist, "
           "mute logs and more. Most things happen by themselves or with buttons. This guide lists every command "
           "that members can use."),
        _h("Start here"),
        _c("/link ign", "Links your Discord to your Minecraft account (checked on Hypixel). Do this first: the bot then knows who you are."),
        _c("/unlink", "Removes the link between your Discord and your Minecraft account."),
        _c("/profile [player] [hidden]", "Everything about a player in one card with buttons: 1v1 stats, GEXP, breaklist, punishments. Leave the name empty for yourself."),
        _c("/info [hidden]", "The same for the whole guild: top GEXP, the requirement, breaklist and more."),
        _c("/guide", "This guide."),
    ]},
    {"title": "Guild & GEXP", "blocks": [
        _p("Every guild member needs the weekly GEXP requirement. The week ends with the purge, every Tuesday at "
           "12:00 AM Eastern Time. People who joined during the week need a proportional part. If you are far "
           "from it, the bot sends you a reminder with buttons: you can ask for a breaklist or an excuse, or tell "
           "the staff that you plan to reach it. Your GEXP this week is on your /profile."),
        _c("/purgetime [when]", "The last and the next purge, as times that show in your own time zone."),
        _c("/road", "How much GEXP the guild needs to pass the guilds above us."),
        _c("/days leaderboard", "Members ordered by total days in the guild."),
        _c("/country view [player]", "The country of a player, with their 1v1s and days in the guild."),
        _c("/blacklist view ign", "One entry of the guild blacklist, with the proof."),
        _c("/tag vote", "Active players: starts the vote on the active guild tag (first yes or no, then the tags)."),
    ]},
    {"title": "Breaklist", "blocks": [
        _p("The breaklist is for members who leave the guild for a while: they keep their days in the guild and "
           "stay on good terms. The staff adds you after a request. While on it, stay guildless unless the staff "
           "approved another guild."),
        _c("/breaklist guild", "Everyone on the breaklist, with a menu to sort the list."),
        _c("/rejoin [note]", "On the breaklist: asks the staff to let you back into the guild. Your days are saved."),
        _c("/otherguild guild [reason]", "On the breaklist: asks the staff to approve another guild for you."),
    ]},
    {"title": "1v1 tier tests", "blocks": [
        _p("1v1 matches are logged by the Tier Testers. Your tier comes from your elo: winning against stronger "
           "players gives more elo."),
        _c("/leaderboard", "The leaderboard: general, season, a past year or a game mode."),
        _c("/history [player]", "The matches of a player this year."),
        _c("/matches", "The latest matches of the guild."),
        _c("/cancelled", "The latest cancelled matches."),
        _c("/paused", "The matches that were interrupted and wait to be continued."),
        _c("/mapstats [ign]", "Wins, losses and win rate of a player on each map."),
        _c("/mapleaderboard map", "The best players on one map."),
        _c("/pro list [page]", "The Pro list: how each one got the rank and the days since their last C+ win."),
        _c("/rules", "The rules for 1v1s and tier tests."),
        _c("/elocalc", "What-if calculator: the elo two players would win or lose in a match. Nothing is saved."),
    ]},
]

def _elo_pages():
    """The elo tables and the tiers, from the same numbers the bot uses (elo.py and config.py)."""
    from elo import WIN, LOSS, WIN5, LOSS5
    def f3(v):
        return " / ".join(f"{x:+d}" for x in v)
    def f5(v):
        return "/".join(f"{x:+d}" for x in v)
    tiers = []
    for i, (name, minimum) in enumerate(TIER_RANGES):
        tiers.append(f"{name} {minimum}+" if i == 0 else f"{name} {minimum}-{TIER_RANGES[i - 1][1] - 1}")
    keys = [("100+ higher", "molto_alto"), ("35 to 99 higher", "alto"), ("within 34", "normale"),
            ("35 to 99 lower", "basso"), ("100+ lower", "molto_basso")]
    names3 = ", ".join(["Normal"] + [m["name"] for m in MODES if m["system"] == "ft3"])
    pages = [{"title": "Elo & tiers", "blocks": [
        _p(f"{names3}: every match is first to 3. The points depend on the score and on the elo difference with "
           "your opponent: beating a stronger player gives more points, losing to a weaker one costs more. "
           "Elo never goes below 0."),
        {"type": "table", "cols": ["Opponent's elo vs yours", "You win (3-0 / 3-1 / 3-2)", "You lose (3-0 / 3-1 / 3-2)"],
         "widths": [0.30, 0.35, 0.35], "colors": ["text", "green", "red"],
         "rows": [[t, f3(WIN[k]), f3(LOSS[k])] for t, k in keys]},
        _h("Tiers"),
        _p("Your tier is decided by your elo (the same tiers in every mode):"),
        _p("   ·   ".join(tiers)),
    ]}]
    if any(m["system"] == "ft5" for m in MODES):
        names5 = ", ".join(m["name"] for m in MODES if m["system"] == "ft5")
        pages.append({"title": "Classic 1v1 elo (FT5)", "blocks": [
            _p(f"{names5}: every match is first to 5. The points depend on the score (the games won by the loser: "
               "0, 1, 2, 3 or 4) and on the elo difference with your opponent."),
            {"type": "table", "cols": ["Opponent's elo vs yours", "You win (5-0/5-1/5-2/5-3/5-4)",
                                       "You lose (0-5/1-5/2-5/3-5/4-5)"],
             "widths": [0.26, 0.38, 0.36], "colors": ["text", "green", "red"],
             "rows": [[t, f5(WIN5[k]), f5(LOSS5[k])] for t, k in keys]},
            _p("Elo never goes below 0."),
        ]})
    return pages


try:
    START_PAGES[4:4] = _elo_pages()          # right after "1v1 tier tests"
except Exception as _e:
    import logging
    logging.getLogger(__name__).exception("The elo pages of /guide could not be built: %s", _e)

TESTER_PAGES = [
    {"title": "Logging matches", "blocks": [
        _p("For the Tier Tester role. You can set tiers and elo and log, fix or cancel matches. You cannot end "
           "seasons or years. Most commands have a mode option for the other game modes (not /pause, /resume, "
           "/droppause)."),
        _h("After a tier test"),
        _c("/settier ign tier", "Gives a tier after a tier test. The elo is set to the minimum of that tier."),
        _c("/setelo ign elo", "Sets the elo of a player by hand (0 to 5000)."),
        _h("Finished matches"),
        _c("/match winner loser score [mode] [date]", "Logs a finished 1v1. The score is like 3-1 (5-3 in Classic 1v1): the bot suggests the valid ones. Both players need a tier in that mode. The elo is calculated by itself and the result card is posted in the 1v1 logs channel. The optional date (2026/10/21 or 2026/10/21 6:00 PM, Eastern Time) logs a match played in the past."),
        _c("/forfeit player opponent [player_games] [opponent_games] [pause_id]", "The player who left loses by forfeit: the elo changes like the biggest loss (3-0, or 5-0 in Classic 1v1). Give the games each one had won (or the pause_id) so the card shows the real score."),
    ]},
    {"title": "Interrupted & fixes", "blocks": [
        _h("Interrupted matches"),
        _c("/pause player1 player2 games1 games2", "Saves an interrupted match (for example 2-1) to continue it later. The elo does not change yet."),
        _c("/resume pause_id winner score", "Finishes a paused match with the final score and logs it."),
        _c("/droppause pause_id", "Throws away a paused match that will not be continued."),
        _h("Fixing mistakes"),
        _c("/undo match_id", "Cancels a match and gives the elo back. It only works on the latest match of both players, and only if their elo was not changed by hand after it."),
        _c("/setdate match_id date", "Changes the date of a logged match and makes a new card. Write 2026/10/21 (or 2026/10/21 6:00 PM)."),
        _c("/removeplayer ign", "Removes a player from the leaderboard and /profile. Their matches stay in the history. /settier or /setelo brings them back."),
        _h("Other"),
        _c("/alerts [alert] [state]", "Switches the automatic posts on or off. Without options it shows what is on. The data is always saved."),
        _c("/link ign user:@member", "Links a member without the Hypixel verification."),
        _c("/linkall [dry_run]", "Links every guild member whose Discord name on Hypixel matches a member of this server. Use dry_run first."),
        _c("/refreshnames", "Checks Mojang for changed usernames right now (it also runs every 6 hours)."),
    ]},
]

GUIDE_CATEGORIES = [
    {"name": "Members", "description": "Every command members can use", "pages": START_PAGES},
    {"name": "Tier Testers", "description": "Commands of the Tier Tester role", "pages": TESTER_PAGES},
]

GUIDE_PAGES = START_PAGES

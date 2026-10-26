from math import ceil

from config import TIER_RANGES, MODES
from elo import WIN, LOSS, WIN5, LOSS5, FIRST_TO

TESTER_ROLE = "Tier Tester"       # only for the 1v1 tier testing
GUILD_ROLE = "Guild Staff"        # breaklist, GEXP, excuses (the real names are set in the host variables)


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

# ======================================================================================
# CATEGORY 1: 1v1 tier testing
# ======================================================================================

TIER_PAGES = [
    {
        "title": "Getting started",
        "blocks": [
            _p("This part of the bot keeps track of the guild's 1v1 tier tests. Every player has an elo and a "
               "tier, and every match changes them automatically. Use the menus or the arrows below to read "
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
            _p("The column used for sorting is highlighted."),
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
            _b("At the end of the year every mode resets too and its final leaderboard is "
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
    TIER_PAGES.append({
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

TIER_PAGES += [
    {
        "title": "Seasons & years",
        "blocks": [
            _h("Seasons"),
            _p("A season is a period of play, numbered (Season 1, Season 2...). When an administrator ends it, "
               "the season leaderboard is saved forever, a new season starts, and every player "
               "loses 100 elo in the general leaderboard (minimum 0). Only the normal mode has seasons."),
            _b("See an old season: /leaderboard season:N and /profile season:N."),
            _h("Years"),
            _p("At the end of the year an administrator closes it: the final leaderboards and the profiles "
               "(normal mode and every other mode) are saved, then everyone goes back to 0 elo (tier D) and "
               "a new year starts. Discord links are kept."),
            _b("See a past year: /leaderboard year:N and /profile year:N (use the mode option for another mode)."),
            _b("The matches of past years are hidden from /history and /matches."),
        ],
    },
    {
        "title": f"{TESTER_ROLE} commands",
        "blocks": [
            _p(f"For the {TESTER_ROLE} role (and administrators). A {TESTER_ROLE} can add, edit and remove "
               f"tiers, elo and matches, but cannot end seasons or years, and has nothing to do with the "
               f"breaklist. The commands below have a mode option for the other game modes "
               f"(except /pause, /resume and /droppause)."),
            _c("/settier ign tier",
               "Assigns a tier after a tier test. The elo is set to the minimum of that tier."),
            _c("/setelo ign elo", "Sets a player's elo by hand (0 to 5000)."),
            _c("/match winner loser score [mode] [date]",
               "Logs a finished 1v1. The score is for example 3-1 (5-3 in Classic 1v1): the bot suggests "
               "the valid scores. Both players need a tier in that mode. The elo is calculated "
               "automatically, the result card shows the match number and it is also posted in the 1v1 logs channel "
               "(unless /alerts has switched that off). The optional date (2026/10/21 or 2026/10/21 6:00 PM, "
               "Eastern Time) logs a match played in the past."),
            _c("/elocalc player1_elo player2_elo [mode]",
               "What if: the elo each player would gain or lose for every score, whoever wins. Nothing is saved."),
            _c("/alerts [alert] [state]",
               "Staff. Switches the automatic posts on or off: 1v1 logs, match date changes, blacklist posts, "
               "Urchin alerts (or all). Without options it shows what is on. The data is always saved."),
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
            _c("/setdate match_id date",
               "Changes the date of a logged match and makes a new card with the new date. Write 2026/10/21 (or 2026/10/21 6:00 PM); the year is "
               "optional (10/21). Cards still show MM/DD/YYYY. A new match takes the number of a cancelled one."),
            _c("/removeplayer ign",
               "Removes a player from the leaderboard and /profile. Their matches stay in the history. "
               "/settier or /setelo brings them back."),
            _c("/link ign user:@member", "Links a member without the Hypixel verification."),
            _c("/refreshnames",
               "Checks Mojang for changed usernames right now. It also runs automatically every 6 hours."),
        ],
    },
    {
        "title": "Administrators",
        "blocks": [
            _p("These commands are only for server administrators. The Tier Tester role cannot use them."),
            _c("/endseason",
               "Ends the season: asks for confirmation and saves a backup of the database first."),
            _c("/endyear",
               "Ends the year: saves the final leaderboards and profiles (all modes), then resets everyone "
               "to 0 elo. Asks for confirmation and saves a backup first."),
            _c("/resetall",
               "TEST ONLY. Deletes all the 1v1 data after saving a backup. You must type RESET EVERYTHING. "
               "It is removed before the official launch."),
            _h("Backups"),
            _p("Every backup is saved in the backups folder next to the database, in a folder for each month: "
               "backups/2026-10/20261022_043000_auto.db. The name tells the date, the time and the reason "
               "(auto, season, year, reset). The bot also makes one automatic backup every day and keeps the "
               "latest 14 of them; the others are never deleted."),
        ],
    },
]

# ======================================================================================
# CATEGORY 2: breaklist
# ======================================================================================

_veteran = 180

BREAKLIST_PAGES = [
    {
        "title": "What is the breaklist",
        "blocks": [
            _p("The breaklist is the list of players who left the guild (or were removed) and are allowed to "
               "come back later. It saves what they owe and how many days they had in the guild."),
            _h("The rules"),
            _b("A player on the breaklist must stay guildless. Joining another guild permanently removes their "
               "ability to use the breaklist."),
            _b("Every breaklist has a reason that everybody can read."),
            _b("The days in the guild are saved: at 180 days a player reaches the veteran rank. While a player "
               "is on the breaklist the days do not grow and are not lost. If someone has 60 days and is "
               "breaklisted for 20, they come back with 60 days and the count restarts from the day they rejoin."),
            _b("If the player had less GEXP than required when they were breaklisted, the missing GEXP becomes "
               "an extra debt that they pay when they come back (see the GEXP & notices category)."),
            _h("How it is posted"),
            _p("When a player is added, the bot generates a notice card and posts it in the No Pressure "
               "Breaklist post of the guild breaklist forum: player, GEXP this week, GEXP required, extra GEXP "
               "owed, days in guild, join and breaklist dates, reason and the reminder to stay guildless."),
        ],
    },
    {
        "title": "Everyone: view the breaklist",
        "blocks": [
            _c("/breaklist view ign",
               "The latest breaklist entry of a player as a card: when they were added, reason, GEXP owed, "
               "total days in the guild (and how many are left to veteran) and how many times they have been "
               "breaklisted. If they were never breaklisted, it says how many days they have in the guild."),
            _c("/breaklist history ign [page]",
               "Every time the player was on the breaklist: the dates, the length of each break, the reason "
               "and the GEXP owed. 4 entries per page, newest first."),
            _c("/breaklist guild [page]",
               "The list of everyone who is on the breaklist now, with the date they were added, their days "
               "in the guild and the GEXP they owe."),
            _c("/breaklist eligibility ign",
               "The last 7 days of GEXP of a player, day by day, and whether they reach the 55,000 GEXP "
               "requirement (or how much they miss). It also shows the extra debt, if any."),
            _p("The GEXP data comes live from Hypixel for players who are in the guild. For players who "
               "already left, the bot uses the copy it saved while they were in the guild (it saves one every "
               "6 hours)."),
        ],
    },
    {
        "title": "Staff: add a player",
        "blocks": [
            _p(f"Breaklist commands are for the {GUILD_ROLE} role and administrators. The {TESTER_ROLE} role "
               f"cannot use them. Dates are in Eastern Time (ET), written year/month/day: 2026/10/03 or 2026/10/03 6:00 PM. The year is optional (10/03 6:00 PM)."),
            _c("/breaklist add ign [reason] [start] [end] [gexp] [days] [debt]",
               "Adds the player and posts the notice card."),
            _h("The options"),
            _b("ign: the Minecraft username."),
            _b("reason: why the player is breaklisted. Everybody can read it."),
            _b("start: when the player joined the guild, for example 2026/10/03 or 2026/10/03 6:00 PM. "
               "Default: the join date from Hypixel."),
            _b("end: when the player was breaklisted. Default: now."),
            _b("gexp: the GEXP they earned this week before the breaklist. Default: read from the Hypixel data "
               "the bot saved."),
            _b("days: the total days the player had in the guild. Default: counted from the start date."),
            _b("debt: extra GEXP debt from previous weeks. Default: the debt the bot already has saved."),
            _h("Example: a player leaving now"),
            _c("/breaklist add ign:Notch reason:Exams until the 5th",
               "The bot reads the join date and the GEXP from Hypixel, works out how much GEXP should have "
               "been earned so far this week, and saves the difference as GEXP owed."),
            _h("Example: someone breaklisted before the bot existed"),
            _c("/breaklist add ign:Notch reason:Vacation start:2026/08/15 end:2026/09/10 gexp:12000 days:23 debt:40",
               "Enter the numbers by hand: the days in guild, the GEXP of that week and the old debt. "
               "Do it once for every player who is already on the breaklist, so that their days are saved."),
            _p("What the owed GEXP is: the requirement is proportional to the time spent in the guild during "
               "the week, down to the minute. Someone breaklisted 2 days 7 hours 3 minutes after the purge "
               "should have 55,000 / 7 x 2.1271 = 18,023 GEXP. If they had 12,000, they owe 6,023, plus any "
               "old debt."),
        ],
    },
    {
        "title": "Staff: rejoin, days & debt",
        "blocks": [
            _c("/breaklist remove ign [when]",
               "Takes the player off the breaklist when they rejoin. The days are kept and keep growing from "
               "the rejoin date. The extra GEXP debt stays saved. The entry stays in /breaklist history."),
            _c("/breaklist setdays ign days",
               "Sets the days a player already has banked. For someone on the breaklist, it is their total days "
               "in the guild. For someone who is in the guild, it is the days from their previous stays (the "
               "days of the current stay are added automatically from the Hypixel join date)."),
            _c("/breaklist adddays ign days [note]",
               "Adds a set of days to a player. Every set gets a number, so a mistake can be undone."),
            _c("/breaklist days ign", "Lists every set of days added to a player."),
            _c("/breaklist removedays ign set",
               "Removes one set of days (the others stay). Use the number shown by /breaklist days."),
            _c("/breaklist move ign position",
               "Moves a player to another position in /breaklist guild (1 = first)."),
            _c("/breaklist debt ign amount",
               "Sets the extra GEXP debt of a player by hand. 0 clears it."),
            _c("/breaklist notice ign [kind]",
               "Posts the GEXP notice of one player right now, with its buttons. Useful to test, or to remind "
               "someone outside of the automatic schedule."),
            _h("Tip: starting with players who are already on the breaklist"),
            _b("1. /breaklist add for each of them with start, end, gexp, days and debt, as in the example."),
            _b("2. /breaklist guild to check the list."),
            _b("3. When one rejoins: /breaklist remove, then /gexp missing to calculate what they must earn."),
        ],
    },
]

# ======================================================================================
# CATEGORY 3: GEXP & notices
# ======================================================================================

_daily = ["Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday", "Monday"]

GEXP_PAGES = [
    {
        "title": "The weekly requirement",
        "blocks": [
            _p("The guild requires 55,000 GEXP every week. The week ends with the purge, every Tuesday at "
               "12:00 AM Eastern Time (ET). Members under the requirement are kicked or breaklisted."),
            _p("Your requirement depends on exactly when you join: the later you join during the week, the "
               "less GEXP you need before the next purge."),
            {
                "type": "table",
                "cols": ["Day of the week", "Cumulative GEXP"],
                "widths": [0.5, 0.5],
                "colors": ["text", "accent"],
                "rows": [[f"Day {i + 1} ({name})", f"{ceil(55000 * (i + 1) / 7):,}"] for i, name in enumerate(_daily)],
            },
            _p("These are reference amounts. The real requirement is adjusted to your exact joining time."),
            _h("Exact joining time"),
            _p("The 55,000 are divided across the whole week (168 hours), so the requirement is "
               "55,000 x (time left until the purge) / (length of the week), rounded up. It changes every "
               "minute: joining at 6:30 PM ET gives a different requirement than joining at 6:31 PM ET."),
            _p("Example: joining on Friday at 6:00 PM ET leaves 78 hours until the purge, so the requirement is "
               "55,000 x 78 / 168 = 25,536 GEXP."),
        ],
    },
    {
        "title": "Extra GEXP debt",
        "blocks": [
            _p("Players on the breaklist may owe extra GEXP from previous weeks. When they rejoin, the normal "
               "requirement is recalculated from their exact joining time, and the debt is tracked separately."),
            _b("The normal requirement must be met first. Only the GEXP earned beyond it pays the debt."),
            _b("Any unpaid debt carries over to the next weeks until it is paid, also when the player goes on "
               "the breaklist again."),
            _h("Example: the requirement is 9,286 and the player owes 40"),
            _b("Earns 9,286: the requirement is met, but the 40 are still owed."),
            _b("Earns 9,326: the requirement is met and the debt is paid completely."),
            _b("Earns 9,300: the requirement is met and the debt goes down by 14, so 26 are still owed."),
            _c("/gexp missing ign [joined] [start_gexp] [current_gexp] [debt] [save]",
               f"({GUILD_ROLE}) Calculates what a player must earn this week. Write the day and exact time "
               f"they joined, in Eastern Time (for example 2026/10/21 6:00 PM, the year is optional), and the GEXP they had at that "
               f"moment. The card shows the normal requirement, the starting GEXP, the current GEXP, the extra "
               f"debt, the total target and the remaining debt after the progress."),
            _b("joined: if you leave it empty, the bot uses the exact time the player joined the guild (from "
               "Hypixel). If they joined before this week, the full week counts."),
            _b("current_gexp: if you leave it empty, the bot reads it from Hypixel (the GEXP since the start "
               "of the week)."),
            _b("debt: if you leave it empty, the bot uses the saved debt of the player."),
            _b("save:True saves the remaining debt as the player's new debt, so it carries over."),
        ],
    },
    {
        "title": "Purge evasion",
        "blocks": [
            _p("The bot notes when a member leaves the guild (it checks the guild every 15 minutes: Hypixel does not "
               "tell the exact time, so the time is the last check where the member was still there). "
               "A member who leaves only owes the GEXP in proportion to the time they were in the cycle."),
            _b("The requirement grows continuously, to the second: 55,000 over the whole week (Tuesday 12:00 AM ET "
               "to the next Tuesday). Leaving after 1 day and 1 minute needs a little more than leaving after "
               "exactly 1 day, and nobody owes the full 55,000 just for leaving during the week."),
            _b("Members who joined during the week count from their exact join time."),
            _c("/purge evaders [cycle] [ign] [page]", f"({GUILD_ROLE}) Lists who left the guild, what they earned, "
               "what they needed and what is missing. SUSPECT = short of the GEXP; BREAKLISTED / EXCUSED = a "
               "legitimate break; MET IT = they earned enough. cycle: this cycle (default) or the last one (after the "
               "purge). With ign it shows every time that player left."),
            _b("The tracking starts when the feature is installed: earlier departures are not known."),
        ],
    },
    {
        "title": "Automatic notices",
        "blocks": [
            _p("The bot posts reminders by itself in the inactivity notices channel. Each reminder is a card "
               "with the GEXP of the player this week, what is required, what is missing, the reason and the "
               "time left, and it pings the player if their account is linked with /link."),
            _h("When they are posted"),
            _b("Friday at 12:00 AM Mountain Time: for members who have less than 20,000 GEXP this week and have "
               "been in the guild for more than 7 days."),
            _b("12 hours before the purge: the final reminder, for every member under their requirement."),
            _p("Players who are excused for the week or on the breaklist do not get a reminder."),
            _h("The buttons"),
            _b("Breaklist Me and Excuse Me open a short form and send a REQUEST to the staff. Nobody is added "
               "automatically: the staff accepts or declines (see Breaklist > Requests)."),
            _b("Ignore: tells the staff that you will reach the requirement or don't intend to act. No more "
               "reminders for you until the next week."),
            _p("Only the staff and the player the notice is for can click them (the player must have linked "
               "their Discord account with /link; staff can link a member by hand with /link ign user:@member)."),
            _c("/breaklist notice ign [kind]", f"({GUILD_ROLE}) Posts the notice of one player right now."),
        ],
    },
    {
        "title": "Excuses",
        "blocks": [
            _p("An excuse means that the player is excused for the week: they will not be kicked or breaklisted, "
               "and they get no reminders. It is not a breaklist."),
            _c("/excuse add ign [weeks] [reason]",
               f"({GUILD_ROLE}) Excuses a player for 1 week (or up to 12) and posts it in the excused list "
               f"channel with the date of the purge when it ends."),
            _c("/excuse remove ign", f"({GUILD_ROLE}) Removes the current excuse of a player."),
            _c("/excuse list", f"({GUILD_ROLE}) Everybody who is excused now or later, with the last date."),
            _p("Players can ask for an excuse with the Excuse Me button of the reminder: the staff decides."),
        ],
    },
]

# ======================================================================================
# CATEGORY 4: Road to #N
# ======================================================================================

ROAD_PAGES = [
    {
        "title": "Road to #6",
        "blocks": [
            _p("Every day the bot posts how much GEXP the guild needs to pass the guilds above it on Hypixel, "
               "with a card, the daily numbers of each guild and a chart of the gap over time."),
            _p("The card lists every guild above us, from the nearest one up to #1: for example "
               "6,662,105 gexp from #6 guild, 10,007,334 gexp from #5 guild, and so on."),
            _h("How it works"),
            _b("The bot compares the total guild GEXP of our guild with the guilds listed by the staff in the "
               "host settings (ROAD_GUILDS)."),
            _b("It posts every day at 11:59 PM Eastern Time (so the day is complete), in the Road to #6 guild "
               "forum. The GEXP per day and the ETA use the real time between two readings."),
            _b("From the second day it shows how much GEXP per day we and they earn, and how many days it would "
               "take to catch up. The chart needs two days of data."),
            _b("When we pass a guild, it leaves the card and we take its place: the title goes from #6 to #5, "
               "then #4... If that guild passes us again, it comes back."),
            _c("/road", "Shows the card right now, without posting it in the forum."),
        ],
    },
]

# ======================================================================================


REQUEST_PAGES = [
    {"title": "Requests: the notice buttons", "blocks": [
        _p("Every GEXP notice has three buttons. Only the staff and the player the notice is for (with a "
           "linked account) can press them."),
        _h("The three buttons"),
        _c("Breaklist Me", "Opens a form: reason, how long you will be inactive, will you stay guildless "
           "(yes / no) and, if not, which guild you will join. It sends a REQUEST to the staff. Nobody is "
           "added by themselves."),
        _c("Excuse Me", "Opens a form with the reason and the number of weeks. It is a request too."),
        _c("Ignore", "Tells the staff you will reach the requirement or don't intend to act. No more "
           "notices for you until the next week."),
        _h("What happens next"),
        _b("The request appears in the requests channel with Accept and Decline (staff only)."),
        _b("The player is pinged with the decision. On Accept they are breaklisted or excused as usual."),
        _b("You can send only one request at a time. Changed your mind? Press any notice button: you get a "
           "Cancel my request button. Or press Cancel request under the request itself."),
        _b("After it is closed or cancelled you can send a different one (for example Excuse Me instead of "
           "Breaklist Me)."),
    ]},
]

BLACKLIST_PAGES = [
    {"title": "Overview", "blocks": [
        _p("The guild blacklist is the list of players who are not welcome (cheating, boosting, ...). It "
           "lives in the blacklist channel. Cheating reports are discussed in the evidence thread, where "
           "everybody can vote (after the staff confirm the evidence) and the staff decides."),
        _h("The three parts"),
        _b("Blacklist: add, edit, remove and list. Reason and duration are free text."),
        _b("Evidence: a cheating report with replay codes / media, a reason, votes and a final verdict."),
        _b("Urchin: when a guild member is tagged on Urchin the bot posts it in the evidence channel."),
    ]},
    {"title": "Blacklist commands", "blocks": [
        _c("/blacklist add ign reason duration [image] [image2] [image3]", "Adds a player and posts the notice (with their skin) "
           "in the blacklist channel. Reason and duration are free text. You can attach up to 3 screenshots or "
           "videos: they are shown in the post."),
        _c("/blacklist edit ign [reason] [duration] [image]", "Changes the reason and/or the duration, and can add "
           "screenshots or videos."),
        _c("/blacklist remove ign", "Takes the player off the blacklist and posts it."),
        _c("/blacklist list", "Everyone on the blacklist, with reason and duration."),
    ]},
    {"title": "Cheating reports", "blocks": [
        _c("/evidence add ign", "Opens a report in the evidence thread: a message with the suspect's skin, "
           "flag, replay / media, reason and votes."),
        _h("Step 1: staff fill in the evidence"),
        _b("Replay / media: replay codes and/or links, and you can upload screenshots or videos straight from "
           "your device (up to 5 per time, 8 per report): they are shown inside the report as if you had posted "
           "them. The text box shows what is already there: change or delete lines, and the new files are added."),
        _b("Reason: why the player is suspected."),
        _b("Confirm & open voting: it works only when both fields are filled. Staff can still add or change evidence "
           "and the reason while people vote: the bot posts a note so the voters know."),
        _h("Step 2: everybody votes"),
        _b("Sufficient evidence / Insufficient evidence: voting opens only after the confirmation. One vote "
           "each, you can change it. Votes are public: the report shows the name of who voted."),
        _h("Step 3: staff decide"),
        _b("End cheating report: shows Add to blacklist (asks reason and duration, posts in the blacklist "
           "channel, together with the evidence: replay codes, links, screenshots, videos and the votes) or "
           "Insufficient evidence. Back to voting undoes it."),
        _b("Cancel: closes the report as cancelled, with an optional reason (at any step)."),
        _p("When a report is closed, a final verdict message is posted in the evidence thread, positive or "
           "negative, with the votes."),
        _h("Who can do what"),
        _b("Everybody: vote."),
        _b("Staff: add replay / media, reason, confirm, cancel, end the report, and all the blacklist commands."),
    ]},
    {"title": "Urchin tags", "blocks": [
        _p("Urchin is a community cheater detection system that saves tagged players. The bot checks the "
           "members of the guild about every minute."),
        _b("When a member gets a new tag, the bot posts it in the evidence channel with the tags and a "
           "Create cheating report button. The report starts with the Urchin tags as the reason."),
        _b("The first check only remembers who is already tagged, so the channel is not flooded."),
        _c("/urchin check ign", "Shows the Urchin tags of a player."),
        _c("/urchin scan", "Posts now every guild member who is tagged."),
        _c("/urchin test ign", "Debug: shows the raw answer of the Urchin API."),
    ]},
]

EXTRA_PAGES = [
    {"title": "Country flags", "blocks": [
        _p("A country can be assigned to a player. Its flag then shows next to the player name on the cards. "
           "Players without a country show a grey flag with a question mark."),
        _c("/country set ign country", "Staff. Country name (Italy) or 2-letter code (IT)."),
        _c("/country remove ign", "Staff. Removes the country."),
        _c("/country view ign", "Everyone. Shows the country of a player."),
    ]},
    {"title": "Mute logs", "blocks": [
        _c("/mute add ign length reason [user]", "Staff. Length: 3d, 12h, 1d12h, 2w, 30m or perm. The muted "
           "player gets the No Guild Bridge role, so they can't write in the guild bridge, and is pinged in the mute "
           "logs channel with the date when the mute expires. Use user if their account is not linked."),
        _c("/mute end ign", "Staff. Lifts the mute now: the role is removed and the player is pinged."),
        _c("/mute warn ign reason [user]", "Staff. A verbal warning: it changes nothing, it is only recorded in the "
           "mute logs (with a ping to the player) and counted."),
        _c("/mute view ign", "Everyone. The full mute history of a player: every mute and verbal warning, newest "
           "first, with date, length and reason."),
        _c("/logins ign [days]", "Guild staff. Earliest and latest login, every login of each day, average "
           "session and time on per day (ET or MT, see /timezone). Useful before an early kick. The bot saves the logins of "
           "guild members every few minutes, so the data starts from the day it was installed; players who hide "
           "their online status on Hypixel may show nothing."),
        _c("/mute list", "Staff. The active mutes."),
        _p("When a timed mute ends, the bot removes the No Guild Bridge role by itself and pings the player to say "
           "they can write in the guild bridge again."),
    ]},
    {"title": "Guild parties", "blocks": [
        _c("/party create when description [host] [title] [image]", "Staff. Posts the party announcement with the "
           "date, a countdown and the buttons Showing up / Maybe / Not showing up. Names come from linked accounts "
           "and show the 1v1 tier."),
        _c("/party stats", "Staff (also the button on the message). Tier, 1v1 record, GEXP of the last 7 days, days in "
           "guild and last login of everyone who answered, so you know who to expect."),
        _c("/party resend", "Staff. Posts the message again with the current lists."),
        _c("/party cancel", "Staff. Cancels the party."),
        _p("The bot posts the message again by itself 24 and 1 hours before the start (PARTY_REMINDER_HOURS). "
           "Also automatic: new replies in the guild thread on the Hypixel forums are copied to the review channel "
           "(/applications check tests it), and the guild "
           "bridge channel topic shows the guild members and how many are online."),
    ]},
    {"title": "Members and applications", "blocks": [
        _c("/days player ign", "Total days a player has spent in the guild: saved days plus the current stay."),
        _c("/days leaderboard", "Members ordered by total days in the guild. Edit with /breaklist setdays or adddays."),
        _c("/waitlist add ign [note]", "Staff. Puts an applicant on the waiting list."),
        _c("/waitlist note ign text", "Staff. Adds a note while they wait."),
        _c("/waitlist remove ign [image] [note]", "Staff. Takes them off (accepted, declined...), with an optional image."),
        _c("/waitlist list, /waitlist view ign", "Staff. Everyone waiting with the latest note, or all notes of one applicant."),
        _c("/timezone et|mt", "Staff. Chooses the time zone shown and typed in the commands. The GEXP week and the "
           "purge always follow Eastern Time."),
        _c("/links", "Staff. Every linked Discord account and who linked it (the member or a staff member)."),
    ]},
    {"title": "Map stats and old matches", "blocks": [
        _c("/match ... [map]", "The optional map is saved with the match. /setmap match_id map changes it later."),
        _c("/mapstats [ign]", "Wins, losses and win rate of a player on each map."),
        _c("/mapleaderboard map", "Best players on one map."),
        _c("/importmatches file [dry_run]", "Staff. Logs many old 1v1s from a text/CSV file, one per line: "
           "winner,loser,score,date[,map]. Use dry_run first to check the file. Players need a tier first."),
        _c("/mute removemute, /mute removewarn, /mute warnexpiry", "Staff. Delete a mute or a warning from the "
           "history, or change when a verbal warning expires."),
        _c("/breaklist guildhistory ign", "Staff. Adds a screenshot or text of the guild history to the breaklist post "
           "(there is also a button on the post)."),
    ]},
    {"title": "Breaklist management", "blocks": [
        _c("/breaklist guild", "The breaklist with a menu to sort it: newest, oldest, days in guild, first joined, GEXP owed, "
           "name, or the manual order."),
        _c("/breaklist all [only]", "Staff. Everyone who has ever been on the breaklist, also who is back, with pages."),
        _c("/breaklist import [dry_run]", "Staff. Adds everyone who has a post in the breaklist forum (days start at 0)."),
        _c("/breaklist resetplayer ign [include_days]", "Staff. Deletes one player's whole breaklist history."),
        _c("/breaklist reset confirm", "Administrators. Deletes the whole breaklist after a backup. "
           "Write RESET BREAKLIST to confirm."),
        _c("/gexp remind ign", "Staff. Sends the GEXP reminder of one player now, with its buttons."),
        _c("/purgetime [when]", "Everyone. The Discord timestamp of the upcoming purge, ready to copy."),
        _c("/wipeplayer ign [mode] [confirm]", "Tier Tester. Cheater: cancels all their matches and gives back the "
           "elo to their opponents. Without confirm it only shows what would change."),
        _p("Mutes and warnings have two buttons: Give feedback and Appeal. The message goes to the staff channel "
           "(APPEALS_CHANNEL_ID), where staff can mark it reviewed or open a ticket. If someone on the breaklist "
           "joins another guild, the bot pings the guild master."),
    ]},
    {"title": "Custom guild tag", "blocks": [
        _c("/tag suggest tag", "Active players. Suggest a guild tag (up to 6 letters or numbers, without the brackets) "
           "and start a vote. Your Discord account must be linked and have the Active rank in the guild."),
        _p("The staff presses Pick voters and chooses the active players who are online. They are pinged and have to "
           "press Yes or No: votes are public. One No rejects the tag. If every picked player votes Yes, the guild "
           "master is pinged to change the tag by hand."),
        _c("/tag list", "Staff. The latest tag votes."),
    ]},
    {"title": "Settings and tests", "blocks": [
        _c("/gexp setreq amount", "Staff. Changes the weekly GEXP requirement (default 55,000). It is saved and "
           "used by every new calculation, card and notice."),
        _c("/test", "Administrators. Sends a test message to every channel set in the host variables and "
           "tells you which ones work."),
        _c("/guide", "This guide."),
    ]},
]


def _overview(title, text, items):
    return {"title": "Overview", "blocks": [_p(text), _h(title), *[_b(x) for x in items]]}


TIER_PAGES.insert(0, _overview(
    "What you will find here",
    "The 1v1 part of the bot: tier tests, elo, matches, leaderboards, seasons and years.",
    ["Getting started: link your account and first commands.",
     "Your stats and leaderboards: for everyone.",
     "Game modes, elo tables, seasons and years.",
     "Tier Tester commands and administrator commands."]))
BREAKLIST_PAGES.append(REQUEST_PAGES[0])
GEXP_PAGES.insert(0, _overview(
    "What you will find here",
    "How the weekly GEXP requirement works and what the bot does about it.",
    ["The weekly requirement and the exact joining time.",
     "Extra GEXP debt and /gexp missing.",
     "Automatic notices and the excuse list."]))

GUIDE_CATEGORIES = [
    {"name": "1v1 Tier Testing", "description": "Tiers, elo, matches and leaderboards", "pages": TIER_PAGES},
    {"name": "Breaklist", "description": "Breaklist, history, days in guild and requests", "pages": BREAKLIST_PAGES},
    {"name": "GEXP & notices", "description": "Requirement, debt, reminders and excuses", "pages": GEXP_PAGES},
    {"name": "Road to #6", "description": "GEXP needed to pass the guilds above", "pages": ROAD_PAGES},
    {"name": "Blacklist & evidence", "description": "Blacklist, cheating reports and Urchin", "pages": BLACKLIST_PAGES},
    {"name": "Extras", "description": "Country flags, mute logs and settings", "pages": EXTRA_PAGES},
]

# the old flat list is kept for any code that still imports it
GUIDE_PAGES = TIER_PAGES

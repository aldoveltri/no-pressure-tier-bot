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

GUIDE_CATEGORIES = [
    {"name": "Members", "description": "Every command members can use", "pages": START_PAGES},
]

GUIDE_PAGES = START_PAGES

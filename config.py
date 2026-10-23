STAFF_ROLE = STAFF_ROLE = os.getenv("TIER_TESTER_ROLE", "Tier Tester")

# Shown in the images. Change it here if you want the other name.
GUILD_NAME = "NO PRESSURE"        # or "COMBO SQUAD"
CREDIT = "made by Filipperto"

# Maximum number of matches shown by /matches (10 per page)
MATCH_LOG_LIMIT = 100

# (name, minimum elo), from highest to lowest
TIER_RANGES = [
    ("S", 400), ("A+", 330), ("A", 300), ("A-", 260),
    ("B+", 230), ("B", 200), ("B-", 170),
    ("C+", 130), ("C", 80), ("C-", 50), ("D", 0),
]
TIERS = [name for name, _ in TIER_RANGES]
TIER_MIN = dict(TIER_RANGES)


def tier_from_elo(elo: int) -> str:
    for name, minimum in TIER_RANGES:
        if elo >= minimum:
            return name
    return "D"

# Rules shown by /rules: (section title, [lines]). Edit the text here to change what the bot shows.
RULES = [
    ("Format", [
        "Matches are first to 3 (3-0, 3-1 or 3-2).",
        "1v1s are normally Red vs Blue on side rush maps",
        "And Red vs Gray on dia rush maps",
    ]),
    ("Islands", [
        "Only adjacent islands can be used.",
        "Red vs Blue: Green and Gray islands.",
        "Red vs Gray: Pink and Blue islands.",
    ]),
    ("Items and resources", [
        "Emeralds can only be used after you have lost your bed.",
        "Bows are not allowed.",
        "Rotational items are not allowed.",
        "Obsidian is not allowed.",
    ]),
]

# Fixed game modes (besides the Normal mode).
# "system" decides the match format and the elo table: "ft3" = first to 3, "ft5" = first to 5.
MODES = [
    {"key": "crossmap", "name": "Bedwars Crossmap", "system": "ft3"},
    {"key": "classic", "name": "Classic 1v1", "system": "ft5"},
    {"key": "bridge", "name": "The Bridge", "system": "ft3"},
]

# Guild tools (breaklist, GEXP)
GEXP_WEEKLY_REQ = 55000
HYPIXEL_GUILD_NAME = "No Pressure"   # the exact name of the guild on Hypixel (used to read the GEXP)

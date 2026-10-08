STAFF_ROLE = "Tester"

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
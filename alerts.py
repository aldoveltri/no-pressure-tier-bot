"""Automatic posts that staff can switch on and off with /alerts. State is kept in the kv table."""
import aiosqlite

from database import DB_PATH

ALERTS = {
    "1v1_logs": ("1v1 logs", "The match / forfeit / undo images the bot posts in the 1v1 logs channel"),
    "match_dates": ("Match date changes", "The note posted in the 1v1 logs channel when /setdate changes a date"),
    "blacklist": ("Blacklist posts", "The posts in the blacklist channel when someone is added, edited or removed"),
    "match_numbers": ("Match numbers on the result cards", "The 'Match #12' text at the bottom of the match / undo cards (off = only the date)"),
    "pro_day15": ("Pro reminder: day 15", "Pings a Pro who has not beaten a C+ player in 15 days"),
    "pro_day23": ("Pro reminder: day 23", "Second reminder to the Pro at day 23"),
    "pro_day30": ("Pro reminder: day 30 (demote)", "Pings the guild master to demote a Pro after 30 days without a C+ win"),
    "urchin": ("Urchin alerts", "The automatic post in the evidence channel when a guild member gets an Urchin tag"),
}
_cache = {}


async def _ensure(db):
    await db.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT)")


async def enabled(name: str) -> bool:
    """On by default: only an explicit 'off' disables an alert."""
    if name in _cache:
        return _cache[name]
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await _ensure(db)
            cur = await db.execute("SELECT value FROM kv WHERE key = ?", (f"alert:{name}",))
            row = await cur.fetchone()
        _cache[name] = not (row and row[0] == "off")
    except Exception:
        return True
    return _cache[name]


async def set_enabled(name: str, on: bool):
    async with aiosqlite.connect(DB_PATH) as db:
        await _ensure(db)
        await db.execute("INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                         (f"alert:{name}", "on" if on else "off"))
        await db.commit()
    _cache[name] = on

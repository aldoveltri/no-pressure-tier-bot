import shutil
import aiosqlite
from datetime import datetime, timedelta, timezone
from config import tier_from_elo

DB_PATH = "/data/tiers.db"


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
        CREATE TABLE IF NOT EXISTS players (
            uuid TEXT PRIMARY KEY,
            ign TEXT NOT NULL,
            tier TEXT,
            elo INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            played_at TEXT NOT NULL,
            winner_uuid TEXT NOT NULL,
            loser_uuid TEXT NOT NULL,
            winner_elo_before INTEGER,
            winner_elo_after INTEGER,
            loser_elo_before INTEGER,
            loser_elo_after INTEGER,
            cancelled INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS links (
            discord_id TEXT PRIMARY KEY,
            uuid TEXT NOT NULL,
            ign TEXT NOT NULL
        );
        """)
        # adds the score column to databases created before this update
        try:
            await db.execute("ALTER TABLE matches ADD COLUMN score TEXT")
        except aiosqlite.OperationalError:
            pass
        await db.commit()


async def init_extras():
    """Adds the 'verified' column if missing, and drops links that were never verified."""
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute("ALTER TABLE links ADD COLUMN verified INTEGER NOT NULL DEFAULT 0")
        except aiosqlite.OperationalError:
            pass
        await db.execute("DELETE FROM links WHERE verified = 0")
        await db.commit()


# ---------- players ----------

async def get_player(uuid: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM players WHERE uuid = ?", (uuid,))
        return await cur.fetchone()


async def upsert_player(uuid: str, ign: str, elo: int | None = None):
    """Creates the player or updates the IGN. If 'elo' is given, sets elo and tier."""
    start = elo if elo is not None else 0
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO players (uuid, ign, tier, elo) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(uuid) DO UPDATE SET ign = excluded.ign, "
            "elo = CASE WHEN ? IS NULL THEN players.elo ELSE excluded.elo END, "
            "tier = CASE WHEN ? IS NULL THEN players.tier ELSE excluded.tier END",
            (uuid, ign, tier_from_elo(start), start, elo, elo),
        )
        await db.commit()


async def remove_player(uuid: str) -> int:
    """Deletes a player and their links, and hides their matches.
    Returns how many matches were hidden. Opponents' elo is NOT changed."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "UPDATE matches SET cancelled = 1 "
            "WHERE (winner_uuid = ? OR loser_uuid = ?) AND cancelled = 0",
            (uuid, uuid),
        )
        hidden = cur.rowcount
        await db.execute("DELETE FROM players WHERE uuid = ?", (uuid,))
        await db.execute("DELETE FROM links WHERE uuid = ?", (uuid,))
        await db.commit()
        return hidden


# ---------- Discord <-> Minecraft links ----------

async def link_account(discord_id: int, uuid: str, ign: str):
    """Creates or changes a (verified) link. A UUID can belong to only one Discord account."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM links WHERE uuid = ? AND discord_id != ?",
                         (uuid, str(discord_id)))
        await db.execute(
            "INSERT INTO links (discord_id, uuid, ign, verified) VALUES (?, ?, ?, 1) "
            "ON CONFLICT(discord_id) DO UPDATE SET uuid = excluded.uuid, ign = excluded.ign, verified = 1",
            (str(discord_id), uuid, ign),
        )
        await db.commit()


async def get_link(discord_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM links WHERE discord_id = ?", (str(discord_id),))
        return await cur.fetchone()


async def remove_link(discord_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("DELETE FROM links WHERE discord_id = ?", (str(discord_id),))
        await db.commit()
        return cur.rowcount > 0


async def set_verified(discord_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE links SET verified = 1 WHERE discord_id = ?", (str(discord_id),))
        await db.commit()


async def verified_owner(uuid: str):
    """Discord id (as text) that has verified this UUID, or None."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT discord_id FROM links WHERE uuid = ? AND verified = 1 LIMIT 1", (uuid,))
        row = await cur.fetchone()
        return row[0] if row else None


# ---------- matches ----------

async def save_match(w_uuid, l_uuid, w_before, w_after, l_before, l_after, score, played_at=None):
    played_at = played_at or datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE players SET elo = ?, tier = ? WHERE uuid = ?",
                         (w_after, tier_from_elo(w_after), w_uuid))
        await db.execute("UPDATE players SET elo = ?, tier = ? WHERE uuid = ?",
                         (l_after, tier_from_elo(l_after), l_uuid))
        cur = await db.execute(
            "INSERT INTO matches (played_at, winner_uuid, loser_uuid, "
            "winner_elo_before, winner_elo_after, loser_elo_before, loser_elo_after, score) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (played_at, w_uuid, l_uuid, w_before, w_after, l_before, l_after, score),
        )
        await db.commit()
        return cur.lastrowid


async def get_stats(uuid: str):
    """Returns (wins, losses)."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM matches WHERE winner_uuid = ? AND cancelled = 0", (uuid,))
        wins = (await cur.fetchone())[0]
        cur = await db.execute(
            "SELECT COUNT(*) FROM matches WHERE loser_uuid = ? AND cancelled = 0", (uuid,))
        losses = (await cur.fetchone())[0]
    return wins, losses


async def get_rank(elo: int):
    """Global position (1 = best)."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM players WHERE elo > ?", (elo,))
        return (await cur.fetchone())[0] + 1


async def get_leaderboard():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT uuid, ign, elo, tier FROM players ORDER BY elo DESC, ign ASC")
        return await cur.fetchall()


async def count_history(uuid: str):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM matches "
            "WHERE (winner_uuid = ? OR loser_uuid = ?) AND cancelled = 0",
            (uuid, uuid),
        )
        return (await cur.fetchone())[0]


async def get_history(uuid: str, limit: int = 10, offset: int = 0):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT m.*, w.ign AS winner_ign, l.ign AS loser_ign "
            "FROM matches m "
            "JOIN players w ON w.uuid = m.winner_uuid "
            "JOIN players l ON l.uuid = m.loser_uuid "
            "WHERE (m.winner_uuid = ? OR m.loser_uuid = ?) AND m.cancelled = 0 "
            "ORDER BY m.id DESC LIMIT ? OFFSET ?",
            (uuid, uuid, limit, offset),
        )
        return await cur.fetchall()


async def count_all_matches():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM matches WHERE cancelled = 0")
        return (await cur.fetchone())[0]


async def get_all_matches(limit: int = 10, offset: int = 0):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT m.*, w.ign AS winner_ign, l.ign AS loser_ign "
            "FROM matches m "
            "JOIN players w ON w.uuid = m.winner_uuid "
            "JOIN players l ON l.uuid = m.loser_uuid "
            "WHERE m.cancelled = 0 "
            "ORDER BY m.id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
        return await cur.fetchall()


# ---------- undo ----------

async def get_match(match_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT m.*, w.ign AS winner_ign, l.ign AS loser_ign "
            "FROM matches m "
            "JOIN players w ON w.uuid = m.winner_uuid "
            "JOIN players l ON l.uuid = m.loser_uuid "
            "WHERE m.id = ?",
            (match_id,),
        )
        return await cur.fetchone()


async def last_match_id(uuid: str):
    """Newest non-cancelled match of a player (None if there is none)."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT MAX(id) FROM matches "
            "WHERE (winner_uuid = ? OR loser_uuid = ?) AND cancelled = 0",
            (uuid, uuid),
        )
        return (await cur.fetchone())[0]


async def cancel_match(m):
    """Restores both players' elo/tier from before the match and marks it cancelled."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE players SET elo = ?, tier = ? WHERE uuid = ?",
                         (m["winner_elo_before"], tier_from_elo(m["winner_elo_before"]), m["winner_uuid"]))
        await db.execute("UPDATE players SET elo = ?, tier = ? WHERE uuid = ?",
                         (m["loser_elo_before"], tier_from_elo(m["loser_elo_before"]), m["loser_uuid"]))
        await db.execute("UPDATE matches SET cancelled = 1 WHERE id = ?", (m["id"],))
        await db.commit()


# ---------- season reset ----------

def backup_db() -> str:
    """Creates a backup of the database inside the persistent volume."""
    name = f"/data/backup_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.db"
    shutil.copy2(DB_PATH, name)
    return name


async def apply_season_reset(penalty: int) -> int:
    """Removes 'penalty' elo from every player (minimum 0). Returns how many were updated."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT uuid, elo FROM players")
        rows = await cur.fetchall()
        for r in rows:
            new_elo = max(0, r["elo"] - penalty)
            await db.execute("UPDATE players SET elo = ?, tier = ? WHERE uuid = ?",
                             (new_elo, tier_from_elo(new_elo), r["uuid"]))
        await db.commit()
        return len(rows)


# ---------- server info ----------

async def get_info():
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        async def one(sql, params=()):
            cur = await db.execute(sql, params)
            return (await cur.fetchone())[0]

        players = await one("SELECT COUNT(*) FROM players")
        avg_elo = await one("SELECT AVG(elo) FROM players")
        matches = await one("SELECT COUNT(*) FROM matches WHERE cancelled = 0")
        recent = await one(
            "SELECT COUNT(*) FROM matches WHERE cancelled = 0 AND played_at >= ?", (week_ago,))
        linked = await one("SELECT COUNT(*) FROM links")
        verified = await one("SELECT COUNT(*) FROM links WHERE verified = 1")

        cur = await db.execute("SELECT ign, elo FROM players ORDER BY elo DESC, ign ASC LIMIT 1")
        row = await cur.fetchone()
        top = (row["ign"], row["elo"]) if row else None

        cur = await db.execute("SELECT tier, COUNT(*) AS n FROM players GROUP BY tier")
        tiers = {r["tier"]: r["n"] for r in await cur.fetchall()}

    return {"players": players, "avg_elo": avg_elo, "matches": matches, "recent": recent,
            "linked": linked, "verified": verified, "top": top, "tiers": tiers}

# ---------- username refresh ----------

async def get_all_player_names():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT uuid, ign FROM players")
        return await cur.fetchall()


async def rename_player(uuid: str, new_name: str):
    """Updates the stored username everywhere (players and links)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE players SET ign = ? WHERE uuid = ?", (new_name, uuid))
        await db.execute("UPDATE links SET ign = ? WHERE uuid = ?", (new_name, uuid))
        await db.commit()
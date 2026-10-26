import os
import sqlite3
import shutil
import aiosqlite
from datetime import datetime, timedelta, timezone
from config import tier_from_elo

# Database location. On Railway the volume path is picked up automatically;
# you can also force it with a DB_PATH variable.
DB_PATH = (os.getenv("DB_PATH")
           or os.path.join(os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or ".", "tiers.db"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
        try:
            await db.execute("ALTER TABLE matches ADD COLUMN score TEXT")
        except aiosqlite.OperationalError:
            pass
        await db.commit()


async def init_extras():
    """Adds columns/tables introduced after the first version (safe to run every start)."""
    async with aiosqlite.connect(DB_PATH) as db:
        for sql in ("ALTER TABLE links ADD COLUMN verified INTEGER NOT NULL DEFAULT 0",
                    "ALTER TABLE links ADD COLUMN linked_by TEXT",
                    "ALTER TABLE links ADD COLUMN forced INTEGER DEFAULT 0",
                    "ALTER TABLE links ADD COLUMN linked_at TEXT",
                    "ALTER TABLE players ADD COLUMN removed INTEGER NOT NULL DEFAULT 0",
                    "ALTER TABLE matches ADD COLUMN forfeit INTEGER NOT NULL DEFAULT 0"):
            try:
                await db.execute(sql)
            except aiosqlite.OperationalError:
                pass
        await db.execute("DELETE FROM links WHERE verified = 0")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS paused_matches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                p1_uuid TEXT NOT NULL,
                p2_uuid TEXT NOT NULL,
                p1_games INTEGER NOT NULL,
                p2_games INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'open'
            )
        """)
        await db.commit()


async def init_seasons():
    """Creates the year / season structure and migrates existing matches
    (existing matches go to season 1 of year 1). Safe to run every start."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.executescript("""
        CREATE TABLE IF NOT EXISTS years (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            ended_at TEXT
        );
        CREATE TABLE IF NOT EXISTS seasons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            year_id INTEGER NOT NULL,
            started_at TEXT NOT NULL,
            ended_at TEXT
        );
        CREATE TABLE IF NOT EXISTS season_players (
            season_id INTEGER NOT NULL,
            uuid TEXT NOT NULL,
            elo INTEGER NOT NULL DEFAULT 0,
            general_tier TEXT,
            PRIMARY KEY (season_id, uuid)
        );
        CREATE TABLE IF NOT EXISTS year_players (
            year_id INTEGER NOT NULL,
            uuid TEXT NOT NULL,
            ign TEXT NOT NULL,
            tier TEXT,
            elo INTEGER NOT NULL DEFAULT 0,
            wins INTEGER NOT NULL DEFAULT 0,
            losses INTEGER NOT NULL DEFAULT 0,
            forfeits INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (year_id, uuid)
        );
        """)
        for sql in ("ALTER TABLE matches ADD COLUMN season_id INTEGER",
                    "ALTER TABLE matches ADD COLUMN year_id INTEGER",
                    "ALTER TABLE matches ADD COLUMN w_season_before INTEGER",
                    "ALTER TABLE matches ADD COLUMN w_season_after INTEGER",
                    "ALTER TABLE matches ADD COLUMN l_season_before INTEGER",
                    "ALTER TABLE matches ADD COLUMN l_season_after INTEGER"):
            try:
                await db.execute(sql)
            except aiosqlite.OperationalError:
                pass

        cur = await db.execute("SELECT COUNT(*) FROM years")
        if (await cur.fetchone())[0] == 0:
            now = _now()
            await db.execute("INSERT INTO years (id, started_at) VALUES (1, ?)", (now,))
            await db.execute("INSERT INTO seasons (id, year_id, started_at) VALUES (1, 1, ?)", (now,))
            await db.execute("UPDATE matches SET season_id = 1, year_id = 1")

            # rebuild the season elo of season 1 by replaying the existing matches
            season_elo = {}
            cur = await db.execute(
                "SELECT id, winner_uuid, loser_uuid, winner_elo_before, winner_elo_after, "
                "loser_elo_before, loser_elo_after FROM matches WHERE cancelled = 0 ORDER BY id ASC")
            for m in await cur.fetchall():
                wb = season_elo.get(m["winner_uuid"], 0)
                lb = season_elo.get(m["loser_uuid"], 0)
                wa = max(0, wb + (m["winner_elo_after"] - m["winner_elo_before"]))
                la = max(0, lb + (m["loser_elo_after"] - m["loser_elo_before"]))
                season_elo[m["winner_uuid"]] = wa
                season_elo[m["loser_uuid"]] = la
                await db.execute(
                    "UPDATE matches SET w_season_before = ?, w_season_after = ?, "
                    "l_season_before = ?, l_season_after = ? WHERE id = ?",
                    (wb, wa, lb, la, m["id"]))
            for uuid, e in season_elo.items():
                await db.execute(
                    "INSERT INTO season_players (season_id, uuid, elo) VALUES (1, ?, ?)", (uuid, e))
        await db.commit()


async def init_modes():
    """Creates the tables of the extra game modes (safe to run every start)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
        CREATE TABLE IF NOT EXISTS modes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE COLLATE NOCASE,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS mode_players (
            mode_id INTEGER NOT NULL,
            uuid TEXT NOT NULL,
            ign TEXT NOT NULL,
            tier TEXT,
            elo INTEGER NOT NULL DEFAULT 0,
            removed INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (mode_id, uuid)
        );
        CREATE TABLE IF NOT EXISTS mode_year_players (
            year_id INTEGER NOT NULL,
            mode_id INTEGER NOT NULL,
            uuid TEXT NOT NULL,
            ign TEXT NOT NULL,
            tier TEXT,
            elo INTEGER NOT NULL DEFAULT 0,
            wins INTEGER NOT NULL DEFAULT 0,
            losses INTEGER NOT NULL DEFAULT 0,
            forfeits INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (year_id, mode_id, uuid)
        );
        """)
        for sql in ("ALTER TABLE matches ADD COLUMN mode_id INTEGER",
                    "ALTER TABLE matches ADD COLUMN mode_year_id INTEGER",
                    "ALTER TABLE matches ADD COLUMN num INTEGER"):
            try:
                await db.execute(sql)
            except aiosqlite.OperationalError:
                pass
        # 'num' is the match number people see. It starts equal to the id; a new match can take the
        # number of a cancelled one (the id stays unique).
        await db.execute("UPDATE matches SET num = id WHERE num IS NULL")
        await db.commit()


async def _current(db):
    """(current year id, current season id) using an open connection."""
    cur = await db.execute("SELECT id FROM years WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1")
    year_id = (await cur.fetchone())[0]
    cur = await db.execute("SELECT id FROM seasons WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1")
    season_id = (await cur.fetchone())[0]
    return year_id, season_id


async def get_current():
    async with aiosqlite.connect(DB_PATH) as db:
        return await _current(db)


async def list_seasons():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT id, year_id, ended_at FROM seasons ORDER BY id DESC")
        return await cur.fetchall()


async def list_years():
    """Years that have ended (their final leaderboard is archived)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT id FROM years WHERE ended_at IS NOT NULL ORDER BY id DESC")
        return await cur.fetchall()


# ---------- game modes ----------

async def list_modes(active_only: bool = True):
    sql = "SELECT id, name FROM modes"
    if active_only:
        sql += " WHERE active = 1"
    sql += " ORDER BY name COLLATE NOCASE"
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(sql)
        return await cur.fetchall()


async def get_mode_by_name(name: str, include_inactive: bool = False):
    sql = "SELECT * FROM modes WHERE name = ?"
    if not include_inactive:
        sql += " AND active = 1"
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(sql, (name.strip(),))
        return await cur.fetchone()


async def get_mode(mode_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM modes WHERE id = ?", (mode_id,))
        return await cur.fetchone()


async def seed_modes(names):
    """Makes the game modes match the fixed list in config: creates the missing ones, activates the
    listed ones and hides any other (its data is kept)."""
    async with aiosqlite.connect(DB_PATH) as db:
        for name in names:
            await db.execute("INSERT OR IGNORE INTO modes (name, active, created_at) VALUES (?, 1, ?)",
                             (name, _now()))
            await db.execute("UPDATE modes SET active = 1 WHERE name = ?", (name,))
        marks = ",".join("?" for _ in names)
        await db.execute(f"UPDATE modes SET active = 0 WHERE name NOT IN ({marks})", list(names))
        await db.commit()

# ---------- players (main mode: mode_id=None) ----------

async def get_player(uuid: str, include_removed: bool = False, mode_id: int | None = None):
    if mode_id is None:
        sql, params = "SELECT * FROM players WHERE uuid = ?", [uuid]
    else:
        sql, params = "SELECT * FROM mode_players WHERE uuid = ? AND mode_id = ?", [uuid, mode_id]
    if not include_removed:
        sql += " AND removed = 0"
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(sql, params)
        return await cur.fetchone()


async def upsert_player(uuid: str, ign: str, elo: int | None = None, mode_id: int | None = None):
    """Creates the player or updates the IGN. If 'elo' is given, sets elo and tier
    (and brings back a player that was removed)."""
    start = elo if elo is not None else 0
    async with aiosqlite.connect(DB_PATH) as db:
        if mode_id is None:
            await db.execute(
                "INSERT INTO players (uuid, ign, tier, elo, removed) VALUES (?, ?, ?, ?, 0) "
                "ON CONFLICT(uuid) DO UPDATE SET ign = excluded.ign, "
                "elo = CASE WHEN ? IS NULL THEN players.elo ELSE excluded.elo END, "
                "tier = CASE WHEN ? IS NULL THEN players.tier ELSE excluded.tier END, "
                "removed = CASE WHEN ? IS NULL THEN players.removed ELSE 0 END",
                (uuid, ign, tier_from_elo(start), start, elo, elo, elo),
            )
        else:
            await db.execute(
                "INSERT INTO mode_players (mode_id, uuid, ign, tier, elo, removed) "
                "VALUES (?, ?, ?, ?, ?, 0) "
                "ON CONFLICT(mode_id, uuid) DO UPDATE SET ign = excluded.ign, "
                "elo = CASE WHEN ? IS NULL THEN mode_players.elo ELSE excluded.elo END, "
                "tier = CASE WHEN ? IS NULL THEN mode_players.tier ELSE excluded.tier END, "
                "removed = CASE WHEN ? IS NULL THEN mode_players.removed ELSE 0 END",
                (mode_id, uuid, ign, tier_from_elo(start), start, elo, elo, elo),
            )
        await db.commit()


async def remove_player(uuid: str, mode_id: int | None = None) -> int:
    """Hides a player from the leaderboard and profile. Matches are kept.
    In the main mode the Discord link is removed too.
    Returns how many matches involve the player."""
    async with aiosqlite.connect(DB_PATH) as db:
        if mode_id is None:
            await db.execute("UPDATE players SET removed = 1 WHERE uuid = ?", (uuid,))
            await db.execute("DELETE FROM links WHERE uuid = ?", (uuid,))
            cur = await db.execute(
                "SELECT COUNT(*) FROM matches WHERE (winner_uuid = ? OR loser_uuid = ?) AND mode_id IS NULL",
                (uuid, uuid))
        else:
            await db.execute("UPDATE mode_players SET removed = 1 WHERE mode_id = ? AND uuid = ?",
                             (mode_id, uuid))
            cur = await db.execute(
                "SELECT COUNT(*) FROM matches WHERE (winner_uuid = ? OR loser_uuid = ?) AND mode_id = ?",
                (uuid, uuid, mode_id))
        kept = (await cur.fetchone())[0]
        await db.commit()
        return kept


# ---------- Discord <-> Minecraft links ----------

async def link_account(discord_id: int, uuid: str, ign: str, by: int | None = None, forced: bool = False):
    """Creates or changes a (verified) link. A UUID can belong to only one Discord account.
    by / forced: who made the link (the member themselves, or a staff member who forced it)."""
    async with aiosqlite.connect(DB_PATH) as db:
        for col in ("linked_by TEXT", "forced INTEGER DEFAULT 0", "linked_at TEXT"):
            try:
                await db.execute(f"ALTER TABLE links ADD COLUMN {col}")
            except aiosqlite.OperationalError:
                pass
        await db.execute("DELETE FROM links WHERE uuid = ? AND discord_id != ?",
                         (uuid, str(discord_id)))
        await db.execute(
            "INSERT INTO links (discord_id, uuid, ign, verified, linked_by, forced, linked_at) "
            "VALUES (?, ?, ?, 1, ?, ?, ?) "
            "ON CONFLICT(discord_id) DO UPDATE SET uuid = excluded.uuid, ign = excluded.ign, verified = 1, "
            "linked_by = excluded.linked_by, forced = excluded.forced, linked_at = excluded.linked_at",
            (str(discord_id), uuid, ign, str(by) if by else str(discord_id), 1 if forced else 0, _now()),
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
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT discord_id FROM links WHERE uuid = ? AND verified = 1 LIMIT 1", (uuid,))
        row = await cur.fetchone()
        return row[0] if row else None


# ---------- matches ----------

async def _next_number(db) -> int:
    """The number of the next match: the lowest number of a cancelled match that no live match is using
    (the new match takes its place), otherwise the highest number + 1."""
    cur = await db.execute(
        "SELECT MIN(c.num) FROM matches c WHERE c.cancelled = 1 AND NOT EXISTS "
        "(SELECT 1 FROM matches l WHERE l.cancelled = 0 AND l.num = c.num)")
    row = await cur.fetchone()
    if row and row[0] is not None:
        return row[0]
    cur = await db.execute("SELECT COALESCE(MAX(num), 0) + 1 FROM matches")
    return (await cur.fetchone())[0]


async def save_match(w_uuid, l_uuid, w_before, w_after, l_before, l_after, score,
                     played_at=None, forfeit=False, mode_id=None):
    """Saves a match and updates elo. Main mode: general elo and season elo.
    Extra mode (mode_id given): the elo of that mode only. Returns the match NUMBER."""
    played_at = played_at or _now()
    async with aiosqlite.connect(DB_PATH) as db:
        year_id, season_id = await _current(db)
        num = await _next_number(db)

        if mode_id is not None:
            for uuid, e in ((w_uuid, w_after), (l_uuid, l_after)):
                await db.execute(
                    "UPDATE mode_players SET elo = ?, tier = ? WHERE mode_id = ? AND uuid = ?",
                    (e, tier_from_elo(e), mode_id, uuid))
            cur = await db.execute(
                "INSERT INTO matches (played_at, winner_uuid, loser_uuid, "
                "winner_elo_before, winner_elo_after, loser_elo_before, loser_elo_after, score, forfeit, "
                "mode_id, mode_year_id, num) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (played_at, w_uuid, l_uuid, w_before, w_after, l_before, l_after, score,
                 1 if forfeit else 0, mode_id, year_id, num),
            )
            await db.commit()
            return num

        async def season_elo(uuid):
            cur = await db.execute(
                "SELECT elo FROM season_players WHERE season_id = ? AND uuid = ?", (season_id, uuid))
            row = await cur.fetchone()
            return row[0] if row else 0

        ws_b = await season_elo(w_uuid)
        ls_b = await season_elo(l_uuid)
        ws_a = max(0, ws_b + (w_after - w_before))
        ls_a = max(0, ls_b + (l_after - l_before))

        await db.execute("UPDATE players SET elo = ?, tier = ? WHERE uuid = ?",
                         (w_after, tier_from_elo(w_after), w_uuid))
        await db.execute("UPDATE players SET elo = ?, tier = ? WHERE uuid = ?",
                         (l_after, tier_from_elo(l_after), l_uuid))
        for uuid, e in ((w_uuid, ws_a), (l_uuid, ls_a)):
            await db.execute(
                "INSERT INTO season_players (season_id, uuid, elo) VALUES (?, ?, ?) "
                "ON CONFLICT(season_id, uuid) DO UPDATE SET elo = excluded.elo",
                (season_id, uuid, e))
        cur = await db.execute(
            "INSERT INTO matches (played_at, winner_uuid, loser_uuid, "
            "winner_elo_before, winner_elo_after, loser_elo_before, loser_elo_after, score, forfeit, "
            "season_id, year_id, w_season_before, w_season_after, l_season_before, l_season_after, num) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (played_at, w_uuid, l_uuid, w_before, w_after, l_before, l_after, score,
             1 if forfeit else 0, season_id, year_id, ws_b, ws_a, ls_b, ls_a, num),
        )
        await db.commit()
        return num


async def _scope(db, season_id=None, mode_id=None):
    """SQL condition (no table alias) for the matches to count: an extra mode in the current year,
    one season, or (default) the main mode in the current year."""
    if mode_id is not None:
        year_id, _ = await _current(db)
        return "mode_id = ? AND mode_year_id = ?", (mode_id, year_id)
    if season_id is not None:
        return "season_id = ?", (season_id,)
    year_id, _ = await _current(db)
    return "year_id = ?", (year_id,)


async def _match_scope(db, mode_id=None):
    """Same idea with the 'm' alias, used by the match lists."""
    year_id, _ = await _current(db)
    if mode_id is None:
        return "m.mode_id IS NULL AND m.year_id = ?", (year_id,)
    return "m.mode_id = ? AND m.mode_year_id = ?", (mode_id, year_id)


_NAMES_SELECT = (
    "SELECT m.*, COALESCE(mw.ign, w.ign) AS winner_ign, COALESCE(ml.ign, l.ign) AS loser_ign "
    "FROM matches m "
    "LEFT JOIN players w ON w.uuid = m.winner_uuid AND m.mode_id IS NULL "
    "LEFT JOIN players l ON l.uuid = m.loser_uuid AND m.mode_id IS NULL "
    "LEFT JOIN mode_players mw ON mw.uuid = m.winner_uuid AND mw.mode_id = m.mode_id "
    "LEFT JOIN mode_players ml ON ml.uuid = m.loser_uuid AND ml.mode_id = m.mode_id "
)


async def get_stats(uuid: str, season_id: int | None = None, mode_id: int | None = None):
    """Returns (wins, losses). Cancelled matches don't count."""
    async with aiosqlite.connect(DB_PATH) as db:
        cond, params = await _scope(db, season_id, mode_id)
        cur = await db.execute(
            f"SELECT COUNT(*) FROM matches WHERE winner_uuid = ? AND cancelled = 0 AND {cond}",
            (uuid, *params))
        wins = (await cur.fetchone())[0]
        cur = await db.execute(
            f"SELECT COUNT(*) FROM matches WHERE loser_uuid = ? AND cancelled = 0 AND {cond}",
            (uuid, *params))
        losses = (await cur.fetchone())[0]
    return wins, losses


async def count_forfeits(uuid: str, season_id: int | None = None, mode_id: int | None = None) -> int:
    """How many (non-cancelled) matches this player forfeited."""
    async with aiosqlite.connect(DB_PATH) as db:
        cond, params = await _scope(db, season_id, mode_id)
        cur = await db.execute(
            f"SELECT COUNT(*) FROM matches WHERE loser_uuid = ? AND forfeit = 1 AND cancelled = 0 AND {cond}",
            (uuid, *params))
        return (await cur.fetchone())[0]


async def get_rank(elo: int, mode_id: int | None = None):
    """Position (1 = best) among the players that are not removed."""
    async with aiosqlite.connect(DB_PATH) as db:
        if mode_id is None:
            cur = await db.execute(
                "SELECT COUNT(*) FROM players WHERE elo > ? AND removed = 0", (elo,))
        else:
            cur = await db.execute(
                "SELECT COUNT(*) FROM mode_players WHERE mode_id = ? AND elo > ? AND removed = 0",
                (mode_id, elo))
        return (await cur.fetchone())[0] + 1


# ---------- leaderboards ----------

def _finish(r: dict) -> dict:
    matches = r["wins"] + r["losses"]
    r["matches"] = matches
    r["winrate"] = (r["wins"] / matches) if matches else 0.0
    return r


def sort_rows(rows, sort: str):
    """Sorts leaderboard rows. sort: elo, wins, losses, winrate or matches."""
    keys = {
        "elo": lambda r: (-r["elo"], -r["wins"], r["ign"].lower()),
        "wins": lambda r: (-r["wins"], -r["elo"], r["ign"].lower()),
        "losses": lambda r: (-r["losses"], -r["elo"], r["ign"].lower()),
        "winrate": lambda r: (-r["winrate"], -r["matches"], -r["elo"], r["ign"].lower()),
        "matches": lambda r: (-r["matches"], -r["elo"], r["ign"].lower()),
    }
    return sorted(rows, key=keys.get(sort, keys["elo"]))


async def get_leaderboard(mode_id: int | None = None):
    """General leaderboard of the current year (or of an extra mode). Rows are dicts with uuid, ign,
    elo, tier, wins, losses, matches, winrate (not sorted: use sort_rows)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        year_id, _ = await _current(db)
        if mode_id is None:
            cur = await db.execute(
                "SELECT p.uuid, p.ign, p.elo, p.tier, COALESCE(w.n, 0) AS wins, COALESCE(l.n, 0) AS losses "
                "FROM players p "
                "LEFT JOIN (SELECT winner_uuid AS uuid, COUNT(*) AS n FROM matches "
                "           WHERE cancelled = 0 AND year_id = ? GROUP BY winner_uuid) w ON w.uuid = p.uuid "
                "LEFT JOIN (SELECT loser_uuid AS uuid, COUNT(*) AS n FROM matches "
                "           WHERE cancelled = 0 AND year_id = ? GROUP BY loser_uuid) l ON l.uuid = p.uuid "
                "WHERE p.removed = 0",
                (year_id, year_id),
            )
        else:
            cur = await db.execute(
                "SELECT p.uuid, p.ign, p.elo, p.tier, COALESCE(w.n, 0) AS wins, COALESCE(l.n, 0) AS losses "
                "FROM mode_players p "
                "LEFT JOIN (SELECT winner_uuid AS uuid, COUNT(*) AS n FROM matches "
                "           WHERE cancelled = 0 AND mode_id = ? AND mode_year_id = ? "
                "           GROUP BY winner_uuid) w ON w.uuid = p.uuid "
                "LEFT JOIN (SELECT loser_uuid AS uuid, COUNT(*) AS n FROM matches "
                "           WHERE cancelled = 0 AND mode_id = ? AND mode_year_id = ? "
                "           GROUP BY loser_uuid) l ON l.uuid = p.uuid "
                "WHERE p.mode_id = ? AND p.removed = 0",
                (mode_id, year_id, mode_id, year_id, mode_id),
            )
        return [_finish(dict(r)) for r in await cur.fetchall()]


async def get_season_leaderboard(season_id: int):
    """Returns (rows, is_current) or None if the season doesn't exist.
    elo / tier are the player's GENERAL elo and tier (live for the current season; for a closed
    season, the elo after the player's last match of that season).
    wins / losses / matches / winrate count only the matches of that season."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT ended_at FROM seasons WHERE id = ?", (season_id,))
        s = await cur.fetchone()
        if s is None:
            return None
        is_current = s["ended_at"] is None
        cur = await db.execute(
            "SELECT sp.uuid, p.ign, p.elo AS live_elo, p.removed, "
            "(SELECT CASE WHEN m.winner_uuid = sp.uuid THEN m.winner_elo_after ELSE m.loser_elo_after END "
            "   FROM matches m WHERE m.season_id = sp.season_id AND m.cancelled = 0 "
            "   AND (m.winner_uuid = sp.uuid OR m.loser_uuid = sp.uuid) "
            "   ORDER BY m.id DESC LIMIT 1) AS end_elo, "
            "COALESCE(w.n, 0) AS wins, COALESCE(l.n, 0) AS losses "
            "FROM season_players sp "
            "JOIN players p ON p.uuid = sp.uuid "
            "LEFT JOIN (SELECT winner_uuid AS uuid, COUNT(*) AS n FROM matches "
            "           WHERE cancelled = 0 AND season_id = ? GROUP BY winner_uuid) w ON w.uuid = sp.uuid "
            "LEFT JOIN (SELECT loser_uuid AS uuid, COUNT(*) AS n FROM matches "
            "           WHERE cancelled = 0 AND season_id = ? GROUP BY loser_uuid) l ON l.uuid = sp.uuid "
            "WHERE sp.season_id = ?",
            (season_id, season_id, season_id),
        )
        rows = []
        for r in await cur.fetchall():
            r = dict(r)
            if is_current and r["removed"]:
                continue
            r = _finish(r)
            if r["matches"] == 0:
                continue
            if is_current or r["end_elo"] is None:
                r["elo"] = r["live_elo"]
            else:
                r["elo"] = r["end_elo"]
            r["tier"] = tier_from_elo(r["elo"])
            rows.append(r)
        return rows, is_current


async def get_year_leaderboard(year_id: int, mode_id: int | None = None):
    """Archived final leaderboard of a year that has ended (main mode or an extra mode), or None."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT ended_at FROM years WHERE id = ?", (year_id,))
        y = await cur.fetchone()
        if y is None or y["ended_at"] is None:
            return None
        if mode_id is None:
            cur = await db.execute(
                "SELECT uuid, ign, tier, elo, wins, losses, forfeits FROM year_players WHERE year_id = ?",
                (year_id,))
        else:
            cur = await db.execute(
                "SELECT uuid, ign, tier, elo, wins, losses, forfeits FROM mode_year_players "
                "WHERE year_id = ? AND mode_id = ?", (year_id, mode_id))
        return [_finish(dict(r)) for r in await cur.fetchall()]


# ---------- match lists (current year; cancelled matches included in the player history) ----------

async def count_history(uuid: str, mode_id: int | None = None):
    async with aiosqlite.connect(DB_PATH) as db:
        cond, params = await _match_scope(db, mode_id)
        cur = await db.execute(
            f"SELECT COUNT(*) FROM matches m WHERE (m.winner_uuid = ? OR m.loser_uuid = ?) AND {cond}",
            (uuid, uuid, *params))
        return (await cur.fetchone())[0]


async def get_history(uuid: str, limit: int = 10, offset: int = 0, mode_id: int | None = None):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cond, params = await _match_scope(db, mode_id)
        cur = await db.execute(
            _NAMES_SELECT +
            f"WHERE (m.winner_uuid = ? OR m.loser_uuid = ?) AND {cond} "
            "ORDER BY m.id DESC LIMIT ? OFFSET ?",
            (uuid, uuid, *params, limit, offset))
        return await cur.fetchall()


async def count_all_matches(mode_id: int | None = None):
    async with aiosqlite.connect(DB_PATH) as db:
        cond, params = await _match_scope(db, mode_id)
        cur = await db.execute(f"SELECT COUNT(*) FROM matches m WHERE m.cancelled = 0 AND {cond}", params)
        return (await cur.fetchone())[0]


async def get_all_matches(limit: int = 10, offset: int = 0, mode_id: int | None = None):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cond, params = await _match_scope(db, mode_id)
        cur = await db.execute(
            _NAMES_SELECT + f"WHERE m.cancelled = 0 AND {cond} ORDER BY m.id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset))
        return await cur.fetchall()


async def count_cancelled_matches(mode_id: int | None = None):
    async with aiosqlite.connect(DB_PATH) as db:
        cond, params = await _match_scope(db, mode_id)
        cur = await db.execute(f"SELECT COUNT(*) FROM matches m WHERE m.cancelled = 1 AND {cond}", params)
        return (await cur.fetchone())[0]


async def get_cancelled_matches(limit: int = 10, offset: int = 0, mode_id: int | None = None):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cond, params = await _match_scope(db, mode_id)
        cur = await db.execute(
            _NAMES_SELECT + f"WHERE m.cancelled = 1 AND {cond} ORDER BY m.id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset))
        return await cur.fetchall()


# ---------- undo ----------

async def get_match(match_number: int):
    """The match with this number. If a cancelled match and a live one share the number, the live one."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(_NAMES_SELECT + "WHERE m.num = ? ORDER BY m.cancelled ASC, m.id DESC LIMIT 1",
                               (match_number,))
        return await cur.fetchone()


async def set_match_date(match_id: int, played_at: str):
    """Changes the date of a match (match_id is the row id: use get_match(...)['id'])."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE matches SET played_at = ? WHERE id = ?", (played_at, match_id))
        await db.commit()


async def last_match_id(uuid: str, mode_id: int | None = None):
    """Newest non-cancelled match of a player in the main mode or in an extra mode:
    (row id, match number), or (None, None) if there is none."""
    async with aiosqlite.connect(DB_PATH) as db:
        if mode_id is None:
            cur = await db.execute(
                "SELECT id, num FROM matches WHERE (winner_uuid = ? OR loser_uuid = ?) "
                "AND cancelled = 0 AND mode_id IS NULL ORDER BY id DESC LIMIT 1", (uuid, uuid))
        else:
            cur = await db.execute(
                "SELECT id, num FROM matches WHERE (winner_uuid = ? OR loser_uuid = ?) "
                "AND cancelled = 0 AND mode_id = ? ORDER BY id DESC LIMIT 1", (uuid, uuid, mode_id))
        row = await cur.fetchone()
        return (row[0], row[1]) if row else (None, None)


async def cancel_match(m):
    """Restores both players' elo/tier from before the match and marks the match as cancelled
    (for the main mode also the season elo)."""
    async with aiosqlite.connect(DB_PATH) as db:
        if m["mode_id"] is not None:
            await db.execute("UPDATE mode_players SET elo = ?, tier = ? WHERE mode_id = ? AND uuid = ?",
                             (m["winner_elo_before"], tier_from_elo(m["winner_elo_before"]),
                              m["mode_id"], m["winner_uuid"]))
            await db.execute("UPDATE mode_players SET elo = ?, tier = ? WHERE mode_id = ? AND uuid = ?",
                             (m["loser_elo_before"], tier_from_elo(m["loser_elo_before"]),
                              m["mode_id"], m["loser_uuid"]))
        else:
            await db.execute("UPDATE players SET elo = ?, tier = ? WHERE uuid = ?",
                             (m["winner_elo_before"], tier_from_elo(m["winner_elo_before"]), m["winner_uuid"]))
            await db.execute("UPDATE players SET elo = ?, tier = ? WHERE uuid = ?",
                             (m["loser_elo_before"], tier_from_elo(m["loser_elo_before"]), m["loser_uuid"]))
            if m["season_id"] is not None and m["w_season_before"] is not None:
                await db.execute("UPDATE season_players SET elo = ? WHERE season_id = ? AND uuid = ?",
                                 (m["w_season_before"], m["season_id"], m["winner_uuid"]))
                await db.execute("UPDATE season_players SET elo = ? WHERE season_id = ? AND uuid = ?",
                                 (m["l_season_before"], m["season_id"], m["loser_uuid"]))
        await db.execute("UPDATE matches SET cancelled = 1 WHERE id = ?", (m["id"],))
        await db.commit()


# ---------- end of season / end of year ----------

def backup_db(reason="manual"):
    """Salva in <cartella del db>/backups/AAAA-MM/AAAAMMGG_HHMMSS_<motivo>.db e ritorna il percorso relativo."""
    folder = os.path.dirname(os.path.abspath(DB_PATH))
    now = datetime.now()
    sub = os.path.join(folder, "backups", now.strftime("%Y-%m"))
    os.makedirs(sub, exist_ok=True)
    name = f"{now.strftime('%Y%m%d_%H%M%S')}_{reason}.db"
    dest = os.path.join(sub, name)
    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(dest)
    with dst:
        src.backup(dst)
    src.close(); dst.close()
    return os.path.relpath(dest, folder)


async def _snapshot_tiers(db, season_id: int):
    """Saves the general tier of every player of a season (kept when the season is archived)."""
    await db.execute(
        "UPDATE season_players SET general_tier = "
        "(SELECT tier FROM players WHERE players.uuid = season_players.uuid) "
        "WHERE season_id = ?", (season_id,))


async def end_season(penalty: int):
    """Closes the current season, opens the next one and removes 'penalty' general elo
    from every active player (minimum 0). Returns (closed_id, new_id, players_updated)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        year_id, season_id = await _current(db)
        now = _now()
        await _snapshot_tiers(db, season_id)
        await db.execute("UPDATE seasons SET ended_at = ? WHERE id = ?", (now, season_id))
        cur = await db.execute("INSERT INTO seasons (year_id, started_at) VALUES (?, ?)", (year_id, now))
        new_id = cur.lastrowid

        cur = await db.execute("SELECT uuid, elo FROM players WHERE removed = 0")
        rows = await cur.fetchall()
        for r in rows:
            new_elo = max(0, r["elo"] - penalty)
            await db.execute("UPDATE players SET elo = ?, tier = ? WHERE uuid = ?",
                             (new_elo, tier_from_elo(new_elo), r["uuid"]))
        await db.commit()
        return season_id, new_id, len(rows)


async def end_year():
    """Archives the final leaderboards of the year (main mode and every extra mode), closes the
    current season and year, opens a new year and season, and resets every player to 0 elo (tier D).
    Discord links are kept. Returns (closed_year, new_year, new_season, players_archived)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        year_id, season_id = await _current(db)
        now = _now()

        await _snapshot_tiers(db, season_id)
        await db.execute("UPDATE seasons SET ended_at = ? WHERE id = ?", (now, season_id))

        # main mode
        cur = await db.execute(
            "SELECT p.uuid, p.ign, p.tier, p.elo, "
            "COALESCE(w.n, 0) AS wins, COALESCE(l.n, 0) AS losses, COALESCE(f.n, 0) AS forfeits "
            "FROM players p "
            "LEFT JOIN (SELECT winner_uuid AS uuid, COUNT(*) AS n FROM matches "
            "           WHERE cancelled = 0 AND year_id = ? GROUP BY winner_uuid) w ON w.uuid = p.uuid "
            "LEFT JOIN (SELECT loser_uuid AS uuid, COUNT(*) AS n FROM matches "
            "           WHERE cancelled = 0 AND year_id = ? GROUP BY loser_uuid) l ON l.uuid = p.uuid "
            "LEFT JOIN (SELECT loser_uuid AS uuid, COUNT(*) AS n FROM matches "
            "           WHERE cancelled = 0 AND forfeit = 1 AND year_id = ? GROUP BY loser_uuid) f "
            "ON f.uuid = p.uuid "
            "WHERE p.removed = 0",
            (year_id, year_id, year_id),
        )
        rows = await cur.fetchall()
        for r in rows:
            await db.execute(
                "INSERT INTO year_players (year_id, uuid, ign, tier, elo, wins, losses, forfeits) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (year_id, r["uuid"], r["ign"], r["tier"], r["elo"], r["wins"], r["losses"], r["forfeits"]))

        # extra modes
        cur = await db.execute("SELECT id FROM modes")
        for mode in await cur.fetchall():
            mid = mode["id"]
            cur2 = await db.execute(
                "SELECT p.uuid, p.ign, p.tier, p.elo, "
                "COALESCE(w.n, 0) AS wins, COALESCE(l.n, 0) AS losses, COALESCE(f.n, 0) AS forfeits "
                "FROM mode_players p "
                "LEFT JOIN (SELECT winner_uuid AS uuid, COUNT(*) AS n FROM matches "
                "           WHERE cancelled = 0 AND mode_id = ? AND mode_year_id = ? "
                "           GROUP BY winner_uuid) w ON w.uuid = p.uuid "
                "LEFT JOIN (SELECT loser_uuid AS uuid, COUNT(*) AS n FROM matches "
                "           WHERE cancelled = 0 AND mode_id = ? AND mode_year_id = ? "
                "           GROUP BY loser_uuid) l ON l.uuid = p.uuid "
                "LEFT JOIN (SELECT loser_uuid AS uuid, COUNT(*) AS n FROM matches "
                "           WHERE cancelled = 0 AND forfeit = 1 AND mode_id = ? AND mode_year_id = ? "
                "           GROUP BY loser_uuid) f ON f.uuid = p.uuid "
                "WHERE p.mode_id = ? AND p.removed = 0",
                (mid, year_id, mid, year_id, mid, year_id, mid),
            )
            for r in await cur2.fetchall():
                await db.execute(
                    "INSERT INTO mode_year_players (year_id, mode_id, uuid, ign, tier, elo, wins, losses, forfeits) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (year_id, mid, r["uuid"], r["ign"], r["tier"], r["elo"],
                     r["wins"], r["losses"], r["forfeits"]))

        await db.execute("UPDATE years SET ended_at = ? WHERE id = ?", (now, year_id))
        cur = await db.execute("INSERT INTO years (started_at) VALUES (?)", (now,))
        new_year = cur.lastrowid
        cur = await db.execute("INSERT INTO seasons (year_id, started_at) VALUES (?, ?)", (new_year, now))
        new_season = cur.lastrowid

        await db.execute("UPDATE players SET elo = 0, tier = ?", (tier_from_elo(0),))
        await db.execute("UPDATE mode_players SET elo = 0, tier = ?", (tier_from_elo(0),))
        await db.execute("UPDATE paused_matches SET status = 'dropped' WHERE status = 'open'")
        await db.commit()
        return year_id, new_year, new_season, len(rows)


# ---------- server info ----------

async def get_info():
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        year_id, season_id = await _current(db)

        async def one(sql, params=()):
            cur = await db.execute(sql, params)
            return (await cur.fetchone())[0]

        players = await one("SELECT COUNT(*) FROM players WHERE removed = 0")
        avg_elo = await one("SELECT AVG(elo) FROM players WHERE removed = 0")
        matches = await one(
            "SELECT COUNT(*) FROM matches WHERE cancelled = 0 AND year_id = ?", (year_id,))
        recent = await one(
            "SELECT COUNT(*) FROM matches WHERE cancelled = 0 AND year_id = ? AND played_at >= ?",
            (year_id, week_ago))
        linked = await one("SELECT COUNT(*) FROM links")
        verified = await one("SELECT COUNT(*) FROM links WHERE verified = 1")

        cur = await db.execute(
            "SELECT ign, elo FROM players WHERE removed = 0 ORDER BY elo DESC, ign ASC LIMIT 1")
        row = await cur.fetchone()
        top = (row["ign"], row["elo"]) if row else None

        cur = await db.execute(
            "SELECT tier, COUNT(*) AS n FROM players WHERE removed = 0 GROUP BY tier")
        tiers = {r["tier"]: r["n"] for r in await cur.fetchall()}

    return {"players": players, "avg_elo": avg_elo, "matches": matches, "recent": recent,
            "linked": linked, "verified": verified, "top": top, "tiers": tiers,
            "season": season_id, "year": year_id}


# ---------- username refresh ----------

async def get_all_player_names():
    """Every player of the main mode and of the extra modes (one row per UUID)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT uuid, MAX(ign) AS ign FROM ("
            "SELECT uuid, ign FROM players UNION ALL SELECT uuid, ign FROM mode_players) "
            "GROUP BY uuid")
        return await cur.fetchall()


async def rename_player(uuid: str, new_name: str):
    """Updates the stored username everywhere (players, mode players and links)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE players SET ign = ? WHERE uuid = ?", (new_name, uuid))
        await db.execute("UPDATE mode_players SET ign = ? WHERE uuid = ?", (new_name, uuid))
        await db.execute("UPDATE links SET ign = ? WHERE uuid = ?", (new_name, uuid))
        await db.commit()


# ---------- paused matches (main mode only) ----------

_PAUSE_SELECT = (
    "SELECT pm.*, a.ign AS p1_ign, b.ign AS p2_ign "
    "FROM paused_matches pm "
    "JOIN players a ON a.uuid = pm.p1_uuid "
    "JOIN players b ON b.uuid = pm.p2_uuid "
)


async def create_pause(p1_uuid, p2_uuid, p1_games, p2_games) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO paused_matches (created_at, p1_uuid, p2_uuid, p1_games, p2_games) "
            "VALUES (?, ?, ?, ?, ?)",
            (_now(), p1_uuid, p2_uuid, p1_games, p2_games),
        )
        await db.commit()
        return cur.lastrowid


async def get_pause(pause_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(_PAUSE_SELECT + "WHERE pm.id = ?", (pause_id,))
        return await cur.fetchone()


async def list_open_pauses(limit: int = 10):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            _PAUSE_SELECT + "WHERE pm.status = 'open' ORDER BY pm.id DESC LIMIT ?", (limit,))
        return await cur.fetchall()


async def count_open_pauses() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM paused_matches WHERE status = 'open'")
        return (await cur.fetchone())[0]


async def close_pause(pause_id: int, status: str):
    """status: 'resumed', 'dropped' or 'forfeited'."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE paused_matches SET status = ? WHERE id = ?", (status, pause_id))
        await db.commit()

# ---------- test only: delete everything ----------

async def reset_all_data():
    """Deletes EVERYTHING (players, matches, links, seasons, years, mode data) and starts from scratch."""
    async with aiosqlite.connect(DB_PATH) as db:
        for table in ("matches", "players", "links", "paused_matches", "season_players", "seasons",
                      "years", "year_players", "mode_players", "mode_year_players"):
            await db.execute(f"DELETE FROM {table}")
        await db.execute("DELETE FROM sqlite_sequence WHERE name != 'modes'")
        await db.commit()
    await init_seasons()   # creates year 1 / season 1 again

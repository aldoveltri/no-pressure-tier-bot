# No Pressure Tiers - Discord bot

A Discord bot that keeps track of the guild's private 1v1 tier tests on Hypixel: elo, tiers, leaderboards, match history, seasons and years. Every answer is an image card. It is written in Python (discord.py) and keeps everything in a single SQLite file.

> This bot is not affiliated with or endorsed by Hypixel.

---

## 1. What it does

- Every player has an **elo** and a **tier** (S, A+, A, A-, B+, B, B-, C+, C, C-, D). The tier always follows the elo.
- Staff log the results of 1v1s. The elo changes automatically using a points table based on the score and on the elo difference with the opponent.
- Leaderboards (sortable), profiles, match history, cancelled matches, paused matches, forfeits.
- **Seasons** and **years**: a season ends with `/endseason`, the year with `/endyear` (final leaderboards and profiles are saved).
- **Game modes**: Normal (the base mode), Bedwars Crossmap, Classic 1v1 (first to 5, with its own elo table) and The Bridge. Each mode has its own elo, tiers and leaderboard.
- Players link their Discord account to their Minecraft account. The link is **verified with Hypixel** (the Discord name set in the Hypixel profile must match).
- `/guide` shows a full guide of the bot inside Discord, and `/rules` shows the 1v1 rules.

## 2. Commands

**Everyone:** `/link`, `/unlink`, `/profile`, `/history`, `/matches`, `/cancelled`, `/paused`, `/leaderboard`, `/info`, `/modes`, `/rules`, `/guide`, `/ping`.

**Staff** (role set in `config.py`, plus server administrators): `/settier`, `/setelo`, `/match`, `/forfeit`, `/undo`, `/pause`, `/resume`, `/droppause`, `/removeplayer`, `/endseason`, `/endyear`, `/refreshnames`, and `/link ign user:@member` (links a member without the Hypixel check).

**Administrators, TEST ONLY:** `/resetall` deletes all data. Remove it from `main.py` before the official launch.

Most commands have a `mode` option to work on another game mode (not `/pause`, `/resume`, `/droppause`). `/profile` and `/leaderboard` also accept `season` and `year`.

## 3. Elo systems

Tiers: S 400+, A+ 330-399, A 300-329, A- 260-299, B+ 230-259, B 200-229, B- 170-199, C+ 130-169, C 80-129, C- 50-79, D 0-49. Elo never goes below 0.

**First to 3** (Normal, Bedwars Crossmap, The Bridge). Points for 3-0 / 3-1 / 3-2:

| Opponent's elo vs yours | You win | You lose |
|---|---|---|
| 100+ higher | +90 / +60 / +30 | -1 / -1 / -1 |
| 35 to 99 higher | +45 / +30 / +15 | -3 / -2 / -1 |
| within 34 | +15 / +10 / +5 | -9 / -6 / -3 |
| 35 to 99 lower | +3 / +2 / +1 | -18 / -12 / -6 |
| 100+ lower | +1 / +1 / +1 | -36 / -24 / -12 |

**First to 5** (Classic 1v1). Points for 5-0 / 5-1 / 5-2 / 5-3 / 5-4 (the loser's games are 0 to 4):

| Opponent's elo vs yours | You win | You lose |
|---|---|---|
| 100+ higher | +150 / +150 / +100 / +100 / +50 | -1 for any score |
| 35 to 99 higher | +75 / +75 / +50 / +50 / +25 | -5 for any score |
| within 34 | +25 / +25 / +17 / +17 / +8 | -15 / -15 / -10 / -10 / -5 |
| 35 to 99 lower | +5 / +5 / +3 / +3 / +2 | -30 / -30 / -20 / -20 / -10 |
| 100+ lower | +1 for any score | -60 / -60 / -40 / -40 / -20 |

The limits (35 and 100) are inclusive. The tables live in `elo.py`.

A **forfeit** is always logged as the biggest loss for the player who left (3-0, or 5-0 in Classic 1v1), but the card shows the real score before the forfeit.

## 4. Project files

| File | What it is |
|---|---|
| `main.py` | The bot: all the commands, buttons and menus |
| `database.py` | SQLite database: players, matches, seasons, years, modes, backups |
| `graphics.py` | Draws all the image cards (colors are at the top of the file) |
| `guide.py` | The text of `/guide` |
| `elo.py` | The elo points tables |
| `config.py` | Settings: guild name, staff role, tiers, rules, game modes |
| `mojang.py` | Minecraft username <-> UUID lookups |
| `hypixel.py` | Hypixel API check used by `/link` |
| `namesync.py` | Updates usernames when a player changes their Minecraft name (every 6 hours, and `/refreshnames`) |
| `requirements.txt` | Python packages |

Requirements: Python 3.10 or newer and the packages `discord.py`, `python-dotenv`, `aiohttp`, `pillow`, `aiosqlite`.

## 5. First-time setup (starting as a new owner)

### 5.1 Discord application (the bot)
1. Go to the Discord Developer Portal (discord.com/developers/applications) and create a **New Application**.
2. In **Bot**, press **Reset Token** and copy the token. Keep it secret: it is the password of the bot. No privileged intents are needed.
3. In **OAuth2 > URL Generator** choose the scopes `bot` and `applications.commands`, and the permissions *Send Messages*, *Embed Links*, *Attach Files*, *Read Message History*. Open the generated link and add the bot to the server.
4. In the server, create the staff role (default name `Tester`) and give it to the staff. Server administrators always count as staff.

### 5.2 Hypixel API key (needed by `/link`)
1. Open developer.hypixel.net and sign in with the forum account linked to Minecraft.
2. Create an app and request a **Personal API Key**. The API policy grants personal apps to the **owner of a guild**, so it must be requested from the guild owner's account. A plain development key expires after about 3 days and is not enough.
3. Never share the key and never write it in the code.

Without a working key, players cannot use `/link`. Staff can still link members by hand with `/link ign:Name user:@member`.

### 5.3 GitHub
Put the project in a **private** repository. Never commit a `.env` file, the token or any key. A `.gitignore` with `.env`, `*.db` and `venv/` is recommended.

### 5.4 Hosting on Railway
1. Create a project from the GitHub repository.
2. Add a **Volume** to the service (for example mounted at `/data`). The database is stored there, so it survives deploys. The bot finds the volume path by itself.
3. In **Variables** set `TOKEN` and `HYPIXEL_API_KEY` (see section 6).
4. Deploy. In the **Deploy Logs** you should see `Online as <bot name>`.

Railway updates the bot automatically every time you push to GitHub.

### 5.5 First use in the server
1. `/guide` and `/rules` to check that the cards look right.
2. `/settier ign:Name tier:B+` for every player that did the tier test (add `mode:` for the other modes).
3. Players run `/link ign:Name` once.
4. Log matches with `/match`. Use `/undo` if one is wrong (only the latest match of both players).

## 6. Configuration

**Environment variables** (Railway > Variables, or a local `.env` file):

| Variable | Required | What it is |
|---|---|---|
| `TOKEN` | yes | Discord bot token |
| `HYPIXEL_API_KEY` | for `/link` | Hypixel API key (it is read again at every `/link`: changing it needs no restart) |
| `DB_PATH` | no | Forces the database file location (it must be inside the volume) |

**`config.py`:** `STAFF_ROLE`, `GUILD_NAME`, `CREDIT` (the small text in the corner of the images), `MATCH_LOG_LIMIT`, `TIER_RANGES`, `RULES` (what `/rules` shows) and `MODES` (the game modes: each one is `ft3` = first to 3 or `ft5` = first to 5).

**`main.py`:** `SEASON_ELO_LOSS` (elo removed from everyone at `/endseason`, now 100).

**`graphics.py`:** `ACCENT` is the main color of the cards.

## 7. Everyday operation

- **End of a season:** `/endseason`. Everyone loses 100 elo in the general leaderboard (minimum 0), the season leaderboard is saved and a new season starts. It asks for confirmation and saves a backup first.
- **End of the year:** `/endyear`. The final leaderboards and profiles (all modes) are saved, then everyone goes back to 0 elo (tier D). Discord links are kept. It asks for confirmation and saves a backup first.
- **Backups:** `backup_YYYYMMDD_HHMMSS.db` files are created in the same folder as the database (the volume) at every `/endseason`, `/endyear` and `/resetall`. To go back to a backup, set the `DB_PATH` variable to that file name (inside the volume).
- **Updating the bot:** change the code, commit and push. The database is migrated automatically when the bot starts.
- **Renewing the Hypixel key:** regenerate it in the dashboard and replace `HYPIXEL_API_KEY` in Railway > Variables.
- **Logs:** Railway > Deploy Logs. Errors are shown there, and each command error also writes a line in the logs.

## 8. Handing the project over to a new owner

The cleanest way is to start **new**, with the owner's own accounts and keys:

1. **Code:** transfer the GitHub repository to the owner (Settings > Danger Zone > Transfer), or let the owner create their own repository and upload the code. Check that no token or key was ever committed.
2. **Discord bot:** the owner creates a **new application**, gets a new token and adds the new bot to the server (section 5.1). Then remove the old bot from the server and reset its token (or delete the old application). Discord can also move an application to a Team ("Transfer App to Team" in General Information), but a new application is simpler.
3. **Hypixel key:** the guild owner requests a Personal API Key from their own account (section 5.2). Do not pass on a key.
4. **Hosting:** the owner creates their own Railway project from their repository, with a new volume and their own variables (section 5.4). The hosting cost is then theirs. Railway can also transfer a whole project to another user (project settings > members > *Transfer Ownership*), but then the test data comes along.
5. **Database:** do **not** hand over the test database. A new volume gives a clean, empty database on the first start. Staff then assign tiers with `/settier`.
6. **Server checks:** the staff role exists, the bot can see and write in the log channel (`1v1-logs`), `config.py` has the right guild name and credit, and `/resetall` is **removed** from `main.py`.
7. **Cleanup by the previous developer:** switch off the old Railway project (two bots with the same token disturb each other), reset the old bot token, delete any test key, and remove access to the repository and to Railway when it is no longer needed.
8. **Final test:** `/ping`, `/guide`, `/settier`, `/match`, `/leaderboard`, `/profile`, `/link`, `/undo`.

## 9. Troubleshooting

| Problem | What to do |
|---|---|
| New commands don't show up | Wait a minute and reload Discord (Ctrl+R). Check that the logs say `Online as ...` after the last push |
| "Linking is temporarily unavailable" | The Hypixel key was rejected (expired?). Renew it (section 7). Staff can link by hand |
| "Linking is not available yet" | `HYPIXEL_API_KEY` is not set |
| The leaderboard is empty after a deploy | The database location changed. Check the volume and the `DB_PATH` variable |
| A symbol appears as an empty square | The host has no font with that symbol. Put `DejaVuSans.ttf` and `DejaVuSans-Bold.ttf` in a `fonts` folder next to `graphics.py` |
| Player heads are missing | The skin services didn't answer. They are tried again after an hour (cache) |
| The bot answers twice | Two copies of the bot use the same token. Stop one of them |
| Warning "Privileged message content intent is missing" | Harmless: the bot only uses slash commands |

## 10. Security

- Keep `TOKEN` and `HYPIXEL_API_KEY` only in Railway Variables (or a local `.env` that is never committed). If one leaks, regenerate it immediately.
- Respect the Hypixel API policy: do not share the key, keep the project non-commercial and say clearly that the bot is not affiliated with Hypixel.
- The database contains Discord IDs and Minecraft UUIDs: do not publish it.

# No Pressure - Discord bot (1v1 tiers + guild management)

Python (discord.py), SQLite, Pillow. Every answer is an image card. Not affiliated with Hypixel.

## 1. Categories
1. **1v1 tier testing**: elo, tiers, matches, seasons, years, modes (Normal, Bedwars Crossmap, Classic 1v1 ft5, The Bridge).
2. **Breaklist**: players who leave and can rejoin; their days in guild are frozen and kept.
3. **GEXP & notices**: weekly requirement, extra debt, excusals, automatic notices.
4. **Road to #6**: daily post with the GEXP gap to the guilds above us.
Use `/guide` in Discord for the same, category by category.

## 2. Roles
- **Tier Tester** (variable `TIER_TESTER_ROLE`): 1v1 only: add/remove players, set elo/tier, log/forfeit/undo/pause matches. Cannot do `/endseason`, `/endyear`, `/resetall` and has no breaklist powers.
- **Guild staff** (variable `GUILD_STAFF_ROLE`): breaklist, excuses, GEXP tools.
- **Administrators**: everything, and the only ones for `/endseason`, `/endyear`, `/resetall`.

## 3. Commands
### Breaklist (`/breaklist ...`)
- `add ign reason [start] [end] [gexp] [days] [debt]` (staff): puts a player on the breaklist with a reason (visible to everyone). `days` = days already spent in guild (for players breaklisted before the bot existed). `debt` = extra GEXP owed. Posts the card in the breaklist forum.
- `view ign`: the player's breaklist card (reason, dates, days in guild, debt).
- `guild`: the whole breaklist.
- `eligibility ign`: can they rejoin, days in guild (veteran at 180), debt to pay.
- `history ign`: every break (reason, length) and how many times it was used.
- `setdays ign days` (staff): fix/seed the banked days.
- `remove ign` (staff): the player rejoined; days restart counting.
Days in guild = banked days + time since the Hypixel join date while in guild. They freeze on the breaklist and are never lost.

### GEXP and excuses
- `/gexp missing ign join start_gexp [current] [debt] [save]`: normal requirement (55,000 x time left until Tuesday 12:00 AM ET / week length, to the second), starting GEXP, current GEXP, extra debt, total target, remaining debt. Time format: `10/21/2026 6:00 PM` (ET). `save:True` stores the remaining debt.
- Debt rule: the normal requirement is met first, only the GEXP beyond it pays the debt (owe 9,286+40: earn 9,326 = clear; 9,300 = 26 left). Debt carries over weeks and through the breaklist.
- `/excuse add|remove|list ...` (staff): excusal list stored in the database.

### Automatic notices (#inactivity-notices)
- Friday 12:00 AM Mountain Time: players under `FRIDAY_NOTICE_MAX_GEXP` (20,000) and in guild more than 7 days.
- Final notice `FINAL_NOTICE_HOURS_BEFORE_PURGE` (12) hours before the purge: everyone under 55,000.
- The card says why, and how much GEXP is needed. Buttons: **Breaklist Me** (posts "added" in the breaklist post) and **Excuse Me** (posts "excused" in the excuse list channel). Only staff and the pinged linked account can press them.

### Road to #6
Daily post (`ROAD_POST_TIME_ET`, default 12:00 AM ET) in the road forum: GEXP gap to the three guilds above us, daily rates, ETA, chart. Needs `ROAD_GUILDS` (see below). After passing #6 the titles move up.

### 1v1 (unchanged)
`/link /unlink /profile /history /matches /cancelled /paused /leaderboard /info /modes /rules /guide /ping` and staff `/settier /setelo /match /forfeit /undo /pause /resume /droppause /removeplayer /refreshnames`. Admin only: `/endseason /endyear /resetall` (resetall is for tests: remove before launch). Every 1v1 result is also posted in the 1v1 logs channel.

## 4. Project files
main.py, database.py, graphics.py, guide.py, elo.py, config.py, mojang.py, hypixel.py, namesync.py, **gexpcalc.py** (requirement maths), **breaklist.py** (breaklist, excuses, gexp, notices, road), requirements.txt.

## 5. Environment variables (host panel or `.env`)
| Variable | Required | Meaning |
|---|---|---|
| `TOKEN` | yes | Discord bot token |
| `HYPIXEL_API_KEY` | yes | Hypixel key (also for guild data) |
| `DB_PATH` | yes on host | Full path of the database file, in a folder that persists, e.g. `/home/container/data/bot.db`. Backups go in `data/backups/YYYY-MM/` |
| `TIER_TESTER_ROLE` | no | Default `Tier Tester` |
| `GUILD_STAFF_ROLE` | no | Role name for guild staff |
| `HYPIXEL_GUILD_NAME` | yes | `No Pressure` |
| `GUILD_DISPLAY_NAME` | no | Name on the cards |
| `BREAKLIST_CHANNEL_ID` | no | Forum #guild-breaklist |
| `INACTIVITY_CHANNEL_ID` | no | #inactivity-notices |
| `EXCUSE_CHANNEL_ID` | no | #inactivity-notices-list |
| `ROAD_CHANNEL_ID` | no | Forum #road-to-#6-guild |
| `LOGS_1V1_CHANNEL_ID` | no | #1v1-logs |
| `VETERAN_DAYS` | no | 180 |
| `FRIDAY_NOTICE_MAX_GEXP` | no | 20000 |
| `FINAL_NOTICE_HOURS_BEFORE_PURGE` | no | 12 |
| `ROAD_POST_TIME_ET` | no | `00:00` |
| `ROAD_GUILDS` | for Road | Comma-separated names of the guilds near us, e.g. `GuildA,GuildB,GuildC,GuildD` (we compare their total GEXP with ours) |
| `AUTO_BACKUP_KEEP` | no | Automatic backups kept (14) |

Channels are read at every use: change the ID in the panel and restart, no code change. **Channel variable empty = the output goes in the channel where you used the command** (handy for testing without those channels).

## 6. SparkedHost
1. Upload all the files (Files tab). Never upload `.env` publicly or commit it.
2. Startup file: `main.py`. Install `requirements.txt` (Startup tab: extra Python packages / "Install requirements" or run `pip install -r requirements.txt` in the console).
3. Variables go in the Startup/Variables tab (or `.env` in the root).
4. `DB_PATH`: keep the database inside the server files, e.g. `/home/container/data/bot.db` (create the `data` folder). Do not put it where redeploys wipe it. Download `data/backups` now and then.
5. Start, and check the console for `Online as ...`.

## 7. Operation
- **Backups**: automatic daily (`*_auto.db`, last 14 kept) and at every `/endseason`, `/endyear`, `/resetall`: `backups/YYYY-MM/YYYYMMDD_HHMMSS_reason.db`. To restore, stop the bot and replace the database file.
- **Handover to a new owner**: new Discord application and token, own Hypixel key (guild owner), own host, empty database, remove `/resetall`, reset old token. Do not hand over test data.

## 8. Assumptions to confirm
- Final notice = 12h before Tuesday 12:00 AM ET (Monday 12:00 PM ET).
- "Friday midnight MT" = Friday 12:00 AM Mountain Time.
- GEXP resets at midnight ET (affects only the Road post time).
- The requirement counts down from the exact join time to the purge (55,000 x time left / week).
- Country flag command and mute logs: not implemented yet.

## 9. Troubleshooting
- `cannot import name '_read_key'`: an old `breaklist.py` is on the host. Upload the new one and restart.
- Commands missing: wait a minute, reload Discord (Ctrl+R).
- Empty leaderboard after a deploy: `DB_PATH` points to a new place.
- Square instead of a symbol: put `DejaVuSans.ttf` and `DejaVuSans-Bold.ttf` in a `fonts` folder next to `graphics.py`.

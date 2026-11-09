# Shapes (Yapper bot)

The shapes.lol bot, running on [Yapper](https://www.yapper.website/developers) with `yapper.py`.

## Run it

```bash
pip install -r requirements.txt
cp .env.example .env      # fill in YAPPER_BOT_TOKEN and the rest
python main.py
```

The web server listens on **port 5004** (`PORT` to change it). Open `/` and log in with `ADMIN_KEY` to see servers,
channels, recent messages, and to send messages as the bot. `/commands`, `/botinfo`, `/count/servers`, `/count/users`,
`/clb` and `/mutuals` are public JSON like before; `/api/*` is admin-only JSON.

## Storage

Data lives in Cloudflare D1 (`tables.py`), which is free for this bot's size: create a D1 database (Workers & Pages -> D1),
then set `CLOUDFLARE_API_TOKEN` (permission: D1 Edit), `CLOUDFLARE_ACCOUNT_ID` and `CLOUDFLARE_D1_DATABASE_ID`.
Each store is its own real table (`bot_counting`, `bot_embed`, `bot_users`, `bot_autorole`, `bot_ai`, `bot_automod`, `bot_audit`,
`bot_cmdlog`, `bot_cmdlog_config`, `bot_actions`, `bot_dms`, `bot_partners`, `bot_holds`, `bot_welcomed`, `bot_rverify`), created
automatically, with typed columns and an `extra_json` column that keeps anything that has no column, so nothing is dropped.
Without the variables the bot falls back to local `.json` files. To copy old pickledb data across, run `python migrate_pickledb.py`.

**Moving off the old key/value table:** on the first start the rows of the old `kv` table are copied into the new tables, read back from D1
and compared. Only when every row matches is it marked done (`bot_meta`). If anything differs the bot keeps using `kv` for that run and retries
next start (the log says `[tables] ...`). `kv` is never deleted by the bot; drop it yourself once you're happy. `/botinfo` shows which
backend each store is on (`d1-tables`, `d1-kv` or `local-json`).

## Files

- `main.py`: commands, events, web server
- `yshim.py`: a small discord.py-shaped layer on top of `yapper.py` (embeds, buttons, command arguments)
- `tables.py`: Cloudflare D1 storage as real tables, with the same get / set / remove / items API
- `migrate_pickledb.py`: one-time import of the old `.db` files

## Last.fm linking

`/lastfm link` uses Last.fm's web auth, so the account is verified by Last.fm and nobody can link a name they don't own.

1. Create an API account at https://www.last.fm/api/account/create. Set its **Callback URL** to `<PUBLIC_URL>/lastfm/callback`.
2. Set `LASTFM_API_KEY`, `LASTFM_API_SECRET` and `PUBLIC_URL` (where this bot's web server, port 5004, is reachable over https).
3. Users run `/lastfm link`, tap the button, approve on Last.fm. `/lastfm unlink` removes it. `/lastfm [@user]` shows now playing, and the link shows on `/yapinuser`.

Profile lookups are cached for 60 seconds, so heavy use stays well under Last.fm's rate limit.

## Website and dashboard

The bot's web server now serves the public site (`/`, `/commands`, `/about`, `/counting/leaderboard/`) straight from the
bot's own data, and a dashboard at `/dashboard`:

- **Login with Yapin'** goes through the Shapes API (`api.shapes.lol` repo). Put `YAPIN_CLIENT_ID` / `YAPIN_CLIENT_SECRET` in the API's `.env`, register `${API PUBLIC_URL}/auth/yapin/callback` as the redirect, and list this site's host in the API's `YAPIN_RETURN_HOSTS`.
- **My servers**: owners, administrators and people with a manage-server role can edit automod, counting (channel, emoji) and autorole.
- **Other servers**: servers you're in with Shapes but can't manage (read-only counting stats).
- **Admin** (only for `ADMIN_USER_IDS`): the bot-owner dashboard, also reachable at `/admin` (the `ADMIN_KEY` login still works).
- Activities: `/activity` redirects to the dashboard until Yapper has an activity SDK. Set `ACTIVITIES_ENABLED=1` to serve the `activity/` folder publicly.

## Dashboard login fallback

`/dashboard/login` asks the Shapes API whether it is up (3s check, remembered for 15s). If it is, login goes through the API as before.
If it is down, the bot does the Yapin' OAuth2 itself at `/dashboard/oauth/login` and `/dashboard/oauth/callback`. Set `YAPIN_CLIENT_ID` and
`YAPIN_CLIENT_SECRET` and register `<PUBLIC_URL>/dashboard/oauth/callback` as a redirect URI. The state is signed, single use and tied to a cookie, and the token calls have no timeout.

## Vortex

`/vortexgame <game ID>` and `/vortexinfo <user ID>` (IDs, not usernames) show a game or user with link buttons. `/vortexcookie` opens a private form (so the cookie never appears in chat) that saves your own Vortex login so lookups are made as you and also show your friend/follow status; submitting the form empty deletes it.

## When Shapes is added or removed

- **Welcome:** the first time Shapes joins a server it posts a short welcome in the first channel it can write to and DMs the owner. It never welcomes the same server twice.
- **30 day data hold:** when Shapes is removed from a server its data (counting, autorole, automod, command log) is *held*, not deleted. Adding Shapes back, or the server showing up in the list again, cancels the hold. After 30 days the data is deleted, but only after re-checking that the server is really gone, and never while the server list looks wrong (empty or suddenly much shorter), so an API hiccup can't cost anyone their data. A removal is only believed after the server is missing from 5 checks in a row.
- Yapin' has no join/leave event in yapper.py 1.6.0, so this is detected by checking the server list every minute. If Yapin' adds the event, it is already wired to react at once (`server_join` / `server_leave` and a few other names).
- `/clearai` deletes your own AI chat history.

## Partners page

The bot owner runs `/partner add <server id> [description]` (inside the server, the id can be left out) to feature a server at `/partners`. Shapes tries to create an invite for it, otherwise reuses one the server already has (a vanity link first); `/partner invite <server id> <https://www.yapin.gg/... link>` sets one by hand. The page shows the server's icon, name, member / channel / role counts and the description you wrote (or the server's own description when you wrote none). Also: `/partner description`, `/partner remove`, `/partner list`. The same data is at `/api/partners`. Only yapin.gg invite links are accepted.

## Roblox verification
OAuth2 through the Shapes API (no bio text). `/verify` (link / accounts / auto / remove / verify here), `/verifysetup` (group, nickname template, verified/unverified role, mirror, DM prompt), `/sync`. Group roles are mirrored 1:1 into Yapin' roles; members sync when they verify or auto-verify, on `/sync`, and by a backup sync every 24 hours measured in elapsed UTC seconds (daylight saving and leap years can't shift it). Accounts are managed at `/roblox` (linked from the dashboard), which also links or unlinks your Discord account (Discord OAuth in the API, so Discord servers see the same Roblox accounts). The API tells the bot about new verifications at `POST /internal/verified`; set `BOT_NOTIFY_URLS` on the API if the bot isn't at `BOT_URL`.

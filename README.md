# Discord Bot Code For Shapes

`main.py` holds the bot, the data, shared helpers and the web server. **No commands live in it:** every command is in its own topic file in `cogs/` (general, owner, counting, server_tools, ai, profiles, roblox_lookup, roblox, vortex, lastfm, fun, command_logs, dm_log). Commands with a space are command groups.

**Adding a command:** make `cogs/<topic>.py` (or add to one). Start it with `m = sys.modules.get("main") or sys.modules["__main__"]`, bind what you need from `main.py` (`bot = m.bot`, `embedDB = m.embedDB`, ...), decorate with `@bot.tree.command(...)`, and list it in the file's `_COMMANDS` so it unloads cleanly. Every file in `cogs/` loads on start (A to Z; files starting with `_` are skipped). While debugging, `await bot.reload_extension("cogs.fun")` reloads one file without a restart.

## Setup
1. `pip install -r requirements.txt`
2. Copy `.env.example` to `.env` and fill it in. Every secret and setting comes from there (or the host's environment variables).
3. `python main.py`

## Database
The bot keeps its data in its own Cloudflare D1 database (separate from the Yapin' bot's; the two only share data through the Shapes API). Set `CLOUDFLARE_API_TOKEN` (D1: Edit), `CLOUDFLARE_ACCOUNT_ID` and `CLOUDFLARE_D1_DATABASE_ID`. Tables are created on first start. On that first start the old pickleDB files (`counting.db`, `embed.db`, `users.db`, `autorole.db`, `ai.db`) next to `main.py` are copied in, read back and compared; the files themselves are never touched. Without the Cloudflare variables the bot keeps local `.db` files, so you can develop offline.

## Commands with spaces
- `/roblox`: `info`, `avatar`, `outfits`, `item`, `bundle`, `asset`, `place`, `badge`, `badges`, `recentbadges`, `limiteds`, `british`, `todiscord`, `fromdiscord`, `group` (group info by ID, link or name), plus verification: `verify`, `link`, `accounts`, `auto`, `remove`, `hide`, `show`, `sync`, and `panel` (`status`, `setgroup`, `nickname`, `role`, `unverified`, `mirror`, `prompt`, `title`, `text`, `stats`, `send`).
- `/vortex`: `game`, `user`, `cookie` (PlayVortex lookups; the cookie is entered in a form, never in chat).
- `/roll`, `/coinflip`, `/8ball`, `/choose`, `/calc`, `/cat`, `/dog`, `/fact`: dice, coins, answers, picks, maths, pictures and facts.
- `/lastfm`: `link`, `unlink`, `user` (links are stored by the Shapes API, kept separate from Yapin' links).
- `/server info`

## Web dashboard

Open the bot's web address and sign in with `ADMIN_KEY`. Servers (channels, members, roles, with total/people/bot counts), reading and posting in channels as the bot (with a hide-sender option), DMs, DM people (a list or everyone) and a log of what was done from the dashboard. It's the same page as the Yapin' bot's admin dashboard, so it works on KaiOS 3 too.

## Roblox verification
- People sign in with Roblox (OAuth2 through the Shapes API, nothing goes in their bio). **No Yapin' account is needed.** Accounts (up to 100) live in the Shapes API; a person who also has a Yapin' account can press Link Discord on the Yapin' dashboard to merge them. `/linkroblox` stays for people who only want to link.
- With mirroring on, `/roblox sync` creates, renames and deletes roles to match the Roblox group (it only deletes roles the bot made) and explains in plain words when Discord refuses (missing Manage Roles / Manage Nicknames, or the bot's role sitting below the roles it hands out).
- Set `SHAPES_API_TOKEN` and add this bot's URL to the API's `BOT_NOTIFY_URLS` (for example `http://127.0.0.1:13455`) so new verifications DM people and sync their roles.

## Other
- `/cmdlog` (server admins) logs commands to a channel through a webhook; `/botlog` (bot owners) logs every command and server add/remove. A webhook the bot already made in a channel is reused and shared between logs.
- `/counting warnings:<0-50>` lets bot owners change how many warnings a server gets (default 3).

## Discord events and sign-in
- **Event webhook** (`POST /discord/webhook`): in the Developer Portal set *Webhook Events URL* to `https://YOUR-HOST/discord/webhook`, and put the app's public key in `DISCORD_PUBLIC_KEY`. Events (app authorized/removed, etc.) are posted to `webhook_url`.
- **Sign in with Discord** (optional): set `DISCORD_CLIENT_ID` and `DISCORD_CLIENT_SECRET` and add `https://YOUR-HOST/login/discord/callback` as a redirect. Bot owners (see `OWNER_IDS`) can then use it instead of the admin key, and the Logs tab shows who did what.
- **Website**: `/` is the landing page (with Counting, Commands, About, Support). Members sign in at `/dashboard` with the same Discord login to manage counting in servers they run (Manage Server or Administrator) and their linked Roblox accounts. The admin dashboard is at `/admin`.
- Optional website settings: `YAPIN_SITE_URL` (link to the Yapin' version, default `https://shapes-yapper.oriaxen.com`), `BOT_INVITE_URL`, `SUPPORT_URL`.
- If the Shapes API is down, Roblox and Last.fm commands say so instead of reporting "not linked".

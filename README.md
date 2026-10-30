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

Data lives in Cloudflare D1 (`cfstore.py`), which is free for this bot's size: create a D1 database (Workers & Pages -> D1),
then set `CLOUDFLARE_API_TOKEN` (permission: D1 Edit), `CLOUDFLARE_ACCOUNT_ID` and `CLOUDFLARE_D1_DATABASE_ID`.
The table is created automatically. Without those variables the bot falls back to local `.json` files.
To copy old data across, run `python migrate_pickledb.py`.

## Files

- `main.py`: commands, events, web server
- `yshim.py`: a small discord.py-shaped layer on top of `yapper.py` (embeds, buttons, command arguments)
- `cfstore.py`: Cloudflare D1 storage with a pickledb-style API
- `migrate_pickledb.py`: one-time import of the old `.db` files

## Last.fm linking

`/lastfm link` uses Last.fm's web auth, so the account is verified by Last.fm and nobody can link a name they don't own.

1. Create an API account at https://www.last.fm/api/account/create. Set its **Callback URL** to `<PUBLIC_URL>/lastfm/callback`.
2. Set `LASTFM_API_KEY`, `LASTFM_API_SECRET` and `PUBLIC_URL` (where this bot's web server, port 5004, is reachable over https).
3. Users run `/lastfm link`, tap the button, approve on Last.fm. `/lastfm unlink` removes it. `/lastfm [@user]` shows now playing, and the link shows on `/yapperuser`.

Profile lookups are cached for 60 seconds, so heavy use stays well under Last.fm's rate limit.

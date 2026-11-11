# Discord Bot Code For shapes.lol
## Roblox verification, command logs

- `/robloxverify` (verify here, `link`, `accounts`, `auto`, `remove`, `hide`/`show` one, several or `all` accounts), `/robloxpanel` (`group`, `nickname`, `role`, `unverified`, `mirror`, `prompt`, `title`, `text`, `send` a panel with a Verify button, `stats`) and `/sync`. With mirroring on, `/sync` creates, renames and deletes roles to match the Roblox group (it only deletes roles the bot made) and explains in plain words when Discord refuses (missing Manage Roles / Manage Nicknames, or the bot's role sitting below the roles it hands out).
- Accounts live in the Shapes API and are shared with the Yapin' bot. A Discord account is attached to a Yapin' account from the Yapin' dashboard, so people who have not linked one are told how. `/linkroblox` stays for people who only want to link.
- Set `SHAPES_API_TOKEN` (the API's `INTERNAL_TOKEN`; `Shapes_API_Key` still works) and add this bot's URL to the API's `BOT_NOTIFY_URLS` (for example `http://127.0.0.1:13455`) so new verifications DM people and sync their roles.
- `/cmdlog` (server admins) logs commands to a channel through a webhook; `/botlog` (bot owners) logs every command and server add/remove. A webhook the bot already made in a channel is reused and shared between logs.
- `/counting warnings:<0-50>` lets bot owners change how many warnings a server gets (default 3).


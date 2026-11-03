# Activities

Everything in this folder is public at `/activity` on the bot's web server.

- Each activity is a folder with an `index.html` (plus any css/js/images next to it): `activity/my-game/index.html` is served at `/activity/my-game/`.
- Optional `activity.json` in the folder gives it a name for the list page: `{"name": "My game", "description": "One line about it"}`.
- `/activity/` lists every folder that has an `index.html`.
- Files starting with `.` or `_` are never served, so use `_private/` for anything that must stay out.
- The server sends no `X-Frame-Options`, so an activity can be shown inside Yapin's activity frame.
- The Yapin-side details (how an activity is launched and what it receives) aren't in the public bot API, so add them here when you have them.

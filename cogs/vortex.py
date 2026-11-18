"""Cog: PlayVortex lookups: /vortex game, /vortex user, /vortex cookie."""
import sys
from datetime import datetime, timezone
from discord import app_commands
from discord.ext import commands
import asyncio
import discord
import hashlib
import os
import re
import requests
import time
import urllib.parse

m = sys.modules.get("main") or sys.modules["__main__"]   # this bot's main.py: shared helpers, data and the bot itself
_embed_color = m._embed_color
usersDB = m.usersDB


# --- PlayVortex (https://playvortex.io): /vortex game, /vortex user, /vortex cookie ---
# API: /api/games/<id> and /api/users/<id>. Pages: /users/<id>/profile and /games/<id>.
# A person can store their own Vortex login (/vortex cookie) so lookups are made as them; that adds how they relate to
# the user (friend request, following) which the API only knows for a signed-in viewer.
VORTEX_URL = os.environ.get("PLAYVORTEX_URL", "https://playvortex.io").rstrip("/")


VORTEX_COOKIE_NAME = os.environ.get("PLAYVORTEX_COOKIE_NAME", "session")      # the cookie's name when someone pastes only its value


_vortex_cache = {}


_VORTEX_SHARED_COOKIE = os.environ.get("PLAYVORTEX_COOKIE", "").strip()       # optional: one login for everyone (bot-owner account)


def vortex_auth_headers(secret):
    """Headers for a pasted login. `name=value` is sent as a cookie as it is; a JWT-looking value as a Bearer token;
    anything else as the value of the cookie named PLAYVORTEX_COOKIE_NAME."""
    secret = (secret or "").strip()
    if not secret:
        return {}
    if secret.lower().startswith("bearer "):
        return {"Authorization": secret}
    if "=" in secret.split(";")[0]:
        return {"Cookie": secret}
    if re.fullmatch(r"[\w-]+\.[\w-]+\.[\w-]+", secret):
        return {"Authorization": f"Bearer {secret}"}
    return {"Cookie": f"{VORTEX_COOKIE_NAME}={secret}"}


def vortex_secret_of(user_id):
    entry = usersDB.get(str(user_id))
    own = entry.get("vortex") if isinstance(entry, dict) else None
    return own or _VORTEX_SHARED_COOKIE or ""


def vortex_get(kind, ident, secret=""):
    """The JSON object for /api/<kind>/<ident>, 'missing' when it doesn't exist, None when the site can't be reached."""
    ident = str(ident).strip()
    if not re.fullmatch(r"[\w.-]{1,64}", ident):
        return "missing"
    key = (kind, ident.lower(), hashlib.sha256(secret.encode()).hexdigest()[:12] if secret else "")     # a login's results are never shared
    hit = _vortex_cache.get(key)
    if hit and hit[0] > time.time():
        return hit[1]
    try:
        r = requests.get(f"{VORTEX_URL}/api/{kind}/{urllib.parse.quote(ident)}", timeout=10,
                         headers={"Accept": "application/json", "User-Agent": "ShapesBot", **vortex_auth_headers(secret)})
        if r.status_code in (400, 404):
            result = "missing"
        elif r.status_code in (401, 403) and secret:
            result = "denied"
        else:
            r.raise_for_status()
            data = r.json()
            result = data if isinstance(data, dict) and data else "missing"
    except (requests.RequestException, ValueError):
        return None
    _vortex_cache[key] = (time.time() + 120, result)
    if len(_vortex_cache) > 300:
        _vortex_cache.pop(next(iter(_vortex_cache)))
    return result


def _vtime(value, style="D"):
    """A Discord timestamp tag from an ISO string (2026-04-23T05:37:25.988850+00:00) or Unix seconds/millis."""
    if value in (None, "", False):
        return None
    try:
        if isinstance(value, (int, float)):
            seconds = value / 1000 if value > 1e12 else value
        else:
            text = str(value).strip()
            try:
                seconds = float(text)
                seconds = seconds / 1000 if seconds > 1e12 else seconds
            except ValueError:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                seconds = parsed.timestamp()
        return f"<t:{int(seconds)}:{style}>"
    except (ValueError, OverflowError, OSError):
        return None


def _vnum(value):
    return f"{value:,}" if isinstance(value, (int, float)) and not isinstance(value, bool) else (str(value) if value not in (None, "") else None)


def vortex_user_url(user_id):
    return f"{VORTEX_URL}/users/{urllib.parse.quote(str(user_id))}/profile"


def vortex_game_url(game_id):
    return f"{VORTEX_URL}/games/{urllib.parse.quote(str(game_id))}"


_VORTEX_BADGES = (("is_staff", "Staff"), ("is_moderator", "Moderator"), ("is_developer", "Developer"), ("is_content_creator", "Content Creator"), ("is_booster", "Booster"))


_VORTEX_STATUS = {"online": "🟢 Online", "offline": "⚫ Offline", "away": "🟡 Away", "playing": "🎮 Playing", "in_game": "🎮 In a game"}


_VORTEX_RELATION = {"request_sent": "Friend request sent", "request_received": "Sent you a friend request", "friends": "Friends", "none": None}


def _vclean(text, limit):
    text = str(text or "").strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


def vortex_user_embed(data, color, signed_in=False):
    uid = data.get("id")
    embed = discord.Embed(title=_vclean(data.get("username") or f"User {uid}", 200), url=vortex_user_url(uid) if uid is not None else None, color=color)
    embed.description = _vclean(data.get("bio"), 600) or None
    status = str(data.get("online_status") or "").lower()
    badges = [label for key, label in _VORTEX_BADGES if data.get(key)]
    fields = [("Followers", _vnum(data.get("follower_count")), True), ("Following", _vnum(data.get("following_count")), True),
              ("Friends", _vnum(data.get("friend_count")), True), ("Visits", _vnum(data.get("visits")), True),
              ("Status", _VORTEX_STATUS.get(status, status.replace("_", " ").title() or None), True),
              ("Joined", _vtime(data.get("created_at")), True)]
    if status != "online":
        fields.append(("Last seen", _vtime(data.get("last_seen"), "R"), True))
    if badges:
        fields.append(("Badges", ", ".join(badges), False))
    prev = [str(p.get("username") if isinstance(p, dict) else p) for p in (data.get("previous_usernames") or [])]
    if prev:
        fields.append(("Previous usernames", _vclean(", ".join(prev), 300), False))
    if signed_in:       # these only mean something to the person who is signed in
        rel = [_VORTEX_RELATION.get(str(data.get("friendship_status") or "").lower(), str(data.get("friendship_status") or "").replace("_", " ").title()),
               {"following": "You follow them"}.get(str(data.get("follow_status") or "").lower(), "")]
        fields.append(("You and them", "\n".join(x for x in rel if x) or None, False))
    if data.get("is_deleted"):
        fields.insert(0, ("⚠️", "This account was deleted.", False))
    for name, value, inline in fields[:25]:
        if value:
            embed.add_field(name=name, value=value, inline=inline)
    return embed


def vortex_game_embed(data, color):
    gid = data.get("id")
    embed = discord.Embed(title=_vclean(data.get("name") or f"Game {gid}", 200), url=vortex_game_url(gid) if gid is not None else None, color=color)
    embed.description = _vclean(data.get("description"), 600) or None
    creator = data.get("creator_name")
    if creator and data.get("creator_id") is not None:
        creator = f"[{creator}]({vortex_user_url(data['creator_id'])})"
    fields = [("Creator", creator, True), ("Playing now", _vnum(data.get("player_count")), True), ("Max players", _vnum(data.get("max_players")), True),
              ("Running servers", _vnum(data.get("total_instances")), True), ("Created", _vtime(data.get("created_at")), True),
              ("Map updated", _vtime(data.get("map_updated_at")), True), ("ID", _vnum(gid), True)]
    for name, value, inline in fields:
        if value:
            embed.add_field(name=name, value=value, inline=inline)
    for t in data.get("thumbnails") or []:                         # the first thumbnail that is a plain link
        url = t.get("url") if isinstance(t, dict) else t
        if isinstance(url, str):
            url = VORTEX_URL + url if url.startswith("/") else url
            if url.startswith("https://"):
                embed.set_image(url=url)
                break
    return embed


def _link_view(*buttons):
    view = discord.ui.View(timeout=None)
    for label, url in buttons:
        view.add_item(discord.ui.Button(label=label, style=discord.ButtonStyle.link, url=url))
    return view


async def _vortex_lookup(interaction, kind, ident):
    await interaction.response.defer()
    secret = vortex_secret_of(interaction.user.id)
    data = await asyncio.to_thread(vortex_get, kind, ident, secret)
    what = "game" if kind == "games" else "user"
    rejected = False
    if data == "denied":                    # the stored login stopped working: fall back to a plain lookup
        data = await asyncio.to_thread(vortex_get, kind, ident, "")
        secret, rejected = "", True
    if data is None:
        return await interaction.edit_original_response(embed=discord.Embed(title="Vortex didn't answer", description="Try again in a moment.", color=discord.Color.red()))
    if data == "missing":
        return await interaction.edit_original_response(embed=discord.Embed(title=f"No {what} found for `{_vclean(ident, 60)}`", color=discord.Color.red()))
    color = _embed_color(interaction.user.id)
    if kind == "games":
        embed = vortex_game_embed(data, color)
        buttons = [("View game", vortex_game_url(data["id"]))] if data.get("id") is not None else []
        if data.get("creator_id") is not None:
            buttons.append((f"{data.get('creator_name') or 'Creator'}'s profile", vortex_user_url(data["creator_id"])))
    else:
        embed = vortex_user_embed(data, color, signed_in=bool(secret))
        buttons = [("View profile", vortex_user_url(data["id"]))] if data.get("id") is not None else []
    embed.set_footer(text=f"Requested By {interaction.user.name} | playvortex.io" + (" | your saved login was rejected, run /vortex cookie" if rejected else ""))
    await interaction.edit_original_response(embed=embed, view=_link_view(*buttons) if buttons else None)


class VortexCookieModal(discord.ui.Modal, title="Vortex login"):
    """A form, so the cookie is never typed into the chat."""
    cookie = discord.ui.TextInput(label="Your Vortex cookie", style=discord.TextStyle.paragraph, required=False, max_length=4000,
                                  placeholder="name=value, just the value, or a Bearer token. Empty deletes your saved login.")

    async def on_submit(self, interaction: discord.Interaction):
        uid = str(interaction.user.id)
        cookie = str(self.cookie.value or "").strip()
        entry = usersDB.get(uid)
        entry = dict(entry) if isinstance(entry, dict) else {}
        if not cookie:
            entry.pop("vortex", None)
            usersDB.set(uid, entry)
            return await interaction.response.send_message("Your Vortex login was deleted.", ephemeral=True)
        if "\n" in cookie or not cookie.isprintable():
            return await interaction.response.send_message("That doesn't look like a cookie (it has line breaks). Nothing was saved.", ephemeral=True)
        entry["vortex"] = cookie
        usersDB.set(uid, entry)
        _vortex_cache.clear()
        await interaction.response.send_message("Saved. Your Vortex lookups now use your login. Run `/vortex cookie` and submit it empty to delete it.", ephemeral=True)


class VortexCog(commands.Cog, name="Vortex"):
    """PlayVortex lookups: /vortex game, /vortex user, /vortex cookie."""
    vortex = app_commands.Group(name="vortex", description="Look things up on PlayVortex",
                                allowed_installs=app_commands.AppInstallationType(guild=True, user=True),
                                allowed_contexts=app_commands.AppCommandContext(guild=True, dm_channel=True, private_channel=True))

    def __init__(self, bot):
        self.bot = bot

    @vortex.command(name="game", description="Look up a Vortex game by its game ID")
    @app_commands.describe(game="The game's ID (the number in its link)")
    async def game(self, interaction: discord.Interaction, game: str):
        await _vortex_lookup(interaction, "games", game)

    @vortex.command(name="user", description="Look up a Vortex user by their user ID (not their username)")
    @app_commands.describe(user_id="The user's ID (the number in their profile link)")
    async def user(self, interaction: discord.Interaction, user_id: str):
        if not str(user_id).strip().isdigit():
            return await interaction.response.send_message("Vortex lookups use the **user ID** (the number in their profile link), not the username.", ephemeral=True)
        await _vortex_lookup(interaction, "users", user_id)

    @vortex.command(name="cookie", description="Save your Vortex login so lookups are made as you")
    async def cookie(self, interaction: discord.Interaction):
        await interaction.response.send_modal(VortexCookieModal())

async def setup(bot):
    await bot.add_cog(VortexCog(bot))

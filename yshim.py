"""yshim: the small slice of the discord.py API that shapes-bot uses, built on top of yapper.py.

It exists so the ~40 command bodies could be ported with very few changes. What you get:

  * Embed / Color      -> embed dicts for Yapper (see "Embeds" below)
  * ui.View / ui.Button / @ui.button -> Yapper buttons. Clicks are routed back to the callbacks.
  * Interaction        -> wraps a Yapper command message (or a button click). It offers the calls the commands
                          already make: response.send_message / defer / edit_message, followup.send,
                          edit_original_response.
  * tree.command(...)  -> registers a Yapper command and parses "/command some args" into the function's parameters.

Things Yapper cannot do (so they do not exist here): select menus, context menus, ephemeral replies to
commands. Buttons support ephemeral replies; for commands they are posted normally.

Embeds
  The Yapper docs were not readable while this was written, so embeds are sent as Discord-style dicts
  (title, description, url, color, fields, footer, thumbnail, image, author, timestamp). If Yapper rejects them
  (HTTP 400/422) the layer switches itself to a readable plain-text version for the rest of the run.
  YAPPER_EMBED_MODE=auto (default) | embed (never fall back) | text (never send embeds).
  If the format is different, fix Embed.to_dict() below, it is the only place that builds it.
"""

import asyncio
import functools
import inspect
import json
import itertools
import os
import random
import re
import secrets
import threading
import time
import traceback
import types
import typing
from datetime import datetime, timezone

import yapper
from yapper import YapperError

BOT = None
EMBED_MODE = os.environ.get("YAPPER_EMBED_MODE", "auto").strip().lower()
_embeds_ok = EMBED_MODE != "text"
_uid = itertools.count(1)

OWNER_IDS = {x.strip() for x in os.environ.get("OWNER_IDS", "").split(",") if x.strip()}
OWNER_USERNAMES = {x.strip().lower() for x in os.environ.get("OWNER_USERNAMES", "leandre,pj,lecanact,phis,shapes").split(",") if x.strip()}


# --------------------------------------------------------------------------- text cleanup

EMOJI_FALLBACK = {
    "RobloxVerified": "☑️", "RobloxPremium": "💎", "RobloxAdmin": "🛡️", "VerifiedInstagram": "☑️",
    "loading": "⏳",
}
SHORTCODES = {
    ":x:": "❌", ":warning:": "⚠️", ":white_check_mark:": "✅", ":flag_gb:": "🇬🇧", ":hourglass:": "⏳",
    ":thumbsup:": "👍", ":tada:": "🎉",
}
_CUSTOM_EMOJI = re.compile(r"<a?:(\w+):\d+>")
_TS = re.compile(r"<t:(-?\d+)(?::([tTdDfFR]))?>")


def _relative(dt):
    secs = int((datetime.now(timezone.utc) - dt).total_seconds())
    future = secs < 0
    secs = abs(secs)
    for unit, size in (("year", 31536000), ("month", 2592000), ("day", 86400), ("hour", 3600), ("minute", 60)):
        if secs >= size:
            n = secs // size
            label = f"{n} {unit}{'' if n == 1 else 's'}"
            return f"in {label}" if future else f"{label} ago"
    return "in a few seconds" if future else "just now"


def fmt_unix(ts, style="f"):
    """A readable replacement for Discord's <t:...> timestamps (Yapper does not render those)."""
    dt = datetime.fromtimestamp(int(ts), timezone.utc)
    if style == "R":
        return _relative(dt)
    day = f"{dt:%B} {dt.day}, {dt.year}"
    return {
        "t": f"{dt:%H:%M} UTC", "T": f"{dt:%H:%M:%S} UTC", "d": f"{dt:%m/%d/%Y}", "D": day,
        "f": f"{day} {dt:%H:%M} UTC", "F": f"{dt:%A}, {day} {dt:%H:%M} UTC",
    }.get(style, f"{day} {dt:%H:%M} UTC")


def clean(text):
    """Strip Discord-only syntax (custom emoji, <t:..> timestamps, :shortcodes:, '-#' small text)."""
    if not isinstance(text, str) or not text:
        return text
    text = _CUSTOM_EMOJI.sub(lambda m: EMOJI_FALLBACK.get(m.group(1), ""), text)
    text = _TS.sub(lambda m: fmt_unix(m.group(1), m.group(2) or "f"), text)
    for code, char in SHORTCODES.items():
        text = text.replace(code, char)
    return re.sub(r"(?m)^-# ", "", text)


def utcnow():
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- Color and Embed

class Color:
    def __init__(self, value=0):
        self.value = int(value)

    def __int__(self):
        return self.value

    def __eq__(self, other):
        return int(other) == self.value if isinstance(other, (int, Color)) else NotImplemented

    def __hash__(self):
        return hash(self.value)

    def __repr__(self):
        return f"Color(0x{self.value:06x})"

    @classmethod
    def random(cls):
        return cls(random.randint(0, 0xFFFFFF))


for _name, _val in {
    "blue": 0x3498DB, "red": 0xE74C3C, "green": 0x2ECC71, "purple": 0x9B59B6, "orange": 0xE67E22,
    "gold": 0xF1C40F, "teal": 0x1ABC9C, "dark_blue": 0x206694, "dark_red": 0x992D22, "dark_green": 0x1F8B4C,
    "dark_purple": 0x71368A, "dark_orange": 0xA84300, "dark_gold": 0xC27C0E, "dark_teal": 0x11806A,
    "yellow": 0xFEE75C, "dark_gray": 0x607D8B, "light_gray": 0x979C9F, "blurple": 0x5865F2,
}.items():
    setattr(Color, _name, classmethod(lambda cls, v=_val: cls(v)))


class _Field:
    def __init__(self, name, value, inline):
        self.name, self.value, self.inline = name, value, inline


class Embed:
    def __init__(self, *, title=None, description=None, color=None, colour=None, url=None, timestamp=None, **_):
        self.title = title
        self.description = description
        self.color = color if color is not None else colour
        self.url = url
        self.timestamp = timestamp
        self.fields = []
        self._footer = None
        self._thumbnail = None
        self._image = None
        self._author = None

    def add_field(self, *, name, value, inline=True):
        self.fields.append(_Field(name, value, inline))
        return self

    def clear_fields(self):
        self.fields.clear()
        return self

    def set_footer(self, *, text=None, icon_url=None):
        self._footer = {"text": text, "icon_url": icon_url}
        return self

    def set_thumbnail(self, *, url=None):
        self._thumbnail = url
        return self

    def set_image(self, *, url=None):
        self._image = url
        return self

    def set_author(self, *, name, url=None, icon_url=None):
        self._author = {"name": name, "url": url, "icon_url": icon_url}
        return self

    def _color_int(self):
        if self.color is None:
            return None
        try:
            return int(self.color)
        except (TypeError, ValueError):
            return None

    def to_dict(self):
        """The embed as Yapper receives it. Change this if Yapper's embed format differs."""
        d = {}
        if self.title:
            d["title"] = clean(str(self.title))[:256]
        if self.description:
            d["description"] = clean(str(self.description))[:4096]
        if self.url:
            d["url"] = str(self.url)
        color = self._color_int()
        if color is not None:
            d["color"] = color
        if self.fields:
            # Yapin allows 10 fields per embed (Discord allowed 25): the rest go into the description as lines.
            rows = [{"name": (clean(str(f.name))[:256] or "\u200b"), "value": (clean(str(f.value))[:1024] or "\u200b"),
                     "inline": bool(f.inline)} for f in self.fields]
            d["fields"] = rows[:10]
            if len(rows) > 10:
                extra = "\n".join(f"**{r['name']}**: {r['value']}" for r in rows[10:])
                d["description"] = ((d.get("description", "") + "\n\n") if d.get("description") else "") + extra
                d["description"] = d["description"][:4096]
        if self._footer and self._footer.get("text"):           # footer.text is required, an icon alone is invalid
            d["footer"] = {k: (clean(v)[:2048] if k == "text" else v) for k, v in self._footer.items() if v}
        if self._thumbnail:
            d["thumbnail"] = {"url": self._thumbnail}
        if self._image:
            d["image"] = {"url": self._image}
        if self._author and self._author.get("name"):
            d["author"] = {k: (str(v)[:256] if k == "name" else v) for k, v in self._author.items() if v}
        if isinstance(self.timestamp, datetime):
            d["timestamp"] = self.timestamp.astimezone(timezone.utc).isoformat()
        return d

    def to_text(self):
        """Readable plain-text version, used when embeds are unavailable."""
        lines = []
        if self.title:
            title = clean(str(self.title))
            lines.append(f"**{title}**" + (f"\n{self.url}" if self.url else ""))
        if self.description:
            lines.append(clean(str(self.description)))
        for f in self.fields:
            name, value = clean(str(f.name)).strip(), clean(str(f.value)).strip()
            if name in ("", "​") and value in ("", "​"):
                continue
            lines.append(f"**{name}:** {value}" if "\n" not in value else f"**{name}**\n{value}")
        for label, url in (("Image", self._image), ("Thumbnail", self._thumbnail)):
            if url:
                lines.append(url)
        if self._footer and self._footer.get("text"):
            lines.append(f"_{clean(self._footer['text'])}_")
        return "\n".join(lines).strip()


# --------------------------------------------------------------------------- users and permissions

def _norm_id(v):
    return int(v) if isinstance(v, str) and v.isdigit() else v


_USER_CACHE = {}


SITE_URL = (os.environ.get("YAPPER_SITE_URL") or "https://www.yapin.gg").rstrip("/")


def _image_url(value):
    """A usable absolute https image URL from whatever the API gave (string, {"url": ...}, relative path, //host)."""
    if isinstance(value, dict):
        value = _first(value, "url", "src", "href")
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or value.lower() in ("null", "none", "undefined"):
        return None
    if value.startswith("//"):
        value = "https:" + value
    elif value.startswith("/"):
        value = SITE_URL + value
    elif value.startswith("http://"):
        value = "https://" + value[len("http://"):]     # embeds and images only load over https
    if not value.startswith("https://"):
        return None
    return value.replace(" ", "%20")


FILES_URL = (os.environ.get("YAPPER_FILES_URL") or "https://files.yapper.website").rstrip("/")


def _asset_url(value, bucket):
    """Yapin gives pictures as a PATH, not a link (OpenAPI: avatar_path, banner_path, icon_path, attachment_path):
    https://files.yapper.website/<bucket>/<path>, and a path starting with / lives on https://www.yapin.gg.
    A full https link is passed through, so older/other fields that already hold a URL keep working."""
    if isinstance(value, dict):
        value = _first(value, "url", "path")
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    if value.startswith(("http://", "https://", "//")):
        return _image_url(value)
    if value.startswith("/"):
        return SITE_URL + value.replace(" ", "%20")
    return f"{FILES_URL}/{bucket}/{value.lstrip('/')}".replace(" ", "%20")


def fetch_user(user_id):
    """The raw Yapper user dict (cached for 5 minutes), or {}."""
    key = str(user_id)
    hit = _USER_CACHE.get(key)
    if hit and time.time() - hit[0] < 300:
        return hit[1]
    try:
        data = dict(BOT.user(user_id))
    except Exception:
        data = {}
    _USER_CACHE[key] = (time.time(), data)
    return data


def _first(data, *keys):
    for k in keys:
        v = data.get(k) if isinstance(data, dict) else None
        if v not in (None, ""):
            return v
    return None


class _Asset:
    def __init__(self, url):
        self.url = url

    def with_size(self, _size):
        return self

    def __str__(self):
        return self.url or ""


class _Perms:
    def __init__(self, administrator):
        self.administrator = administrator


class YUser:
    """A Yapper user shaped like a discord.py user (id, name, display_name, mention, ...)."""

    def __init__(self, id, name=None, display_name=None, data=None, bot=False):
        self.id = _norm_id(id)
        self.data = dict(data or {})
        self.name = name or _first(self.data, "username", "name", "display_name") or str(id)
        self.display_name = display_name or _first(self.data, "display_name", "displayName") or self.name
        self._bot_hint = bool(bot)
        self._guild_id = None

    @property
    def is_bot(self):
        """Yapin's user object has no bot flag in its API description, so this looks at every sign: the message event's
        author_bot, a "bot" badge, a bot object/flag (the bot's own /me has one), or a type of "bot"."""
        d = self.data
        kind = str(_first(d, "type", "kind", "account_type", "user_type") or "").lower()
        badges = {str(b).lower().replace(" ", "_") for b in (d.get("badges") or []) if isinstance(b, str)}
        flag = d.get("bot")
        return bool(self._bot_hint or flag is True or isinstance(flag, dict) or d.get("is_bot") is True or d.get("isBot") is True
                    or d.get("author_bot") is True or kind == "bot" or badges & {"bot", "verified_bot", "app"})

    @property
    def bot(self):
        return self.is_bot

    @classmethod
    def from_api(cls, data):
        return cls(_first(data, "id", "user_id") or "unknown", data=data)

    @classmethod
    def from_message(cls, msg):
        uid = msg.get("author_id")
        name = _first(msg, "author_username", "author_name", "username")
        shown = _first(msg, "author_display_name", "display_name")
        author = msg.get("author") if isinstance(msg.get("author"), dict) else None
        data = dict(author or {})
        if not (name or _first(data, "username", "name")):
            data = fetch_user(uid)
        return cls(uid, name=name, display_name=shown, data=data, bot=bool(msg.get("author_bot")))

    @property
    def mention(self):
        return f"<@{self.id}>"

    @property
    def avatar_url(self):
        return (_image_url(_first(self.data, "avatar_url", "avatarUrl", "image_url", "author_avatar_url"))
                or _asset_url(_first(self.data, "avatar_path", "author_avatar_path", "avatar"), "avatars"))

    @property
    def banner_url(self):
        return (_image_url(_first(self.data, "banner_url", "bannerUrl"))
                or _asset_url(_first(self.data, "banner_path", "banner"), "banners"))

    @property
    def badges(self):
        b = self.data.get("badges")
        return [str(x) for x in b] if isinstance(b, (list, tuple)) else []

    @property
    def pronouns(self):
        return _first(self.data, "pronouns")

    @property
    def display_avatar(self):
        return _Asset(self.avatar_url)

    @property
    def created_at(self):
        raw = _first(self.data, "created_at", "createdAt")
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00")) if raw else None
        except ValueError:
            return None

    @property
    def guild_permissions(self):
        gid = self._guild_id() if callable(self._guild_id) else self._guild_id
        return _Perms(bool(gid) and is_admin(gid, self.id))

    def __eq__(self, other):
        return isinstance(other, YUser) and str(self.id) == str(other.id)

    def __ne__(self, other):
        return not self.__eq__(other)

    def __hash__(self):
        return hash(str(self.id))

    def __str__(self):
        return str(self.name)


class YRole:
    def __init__(self, id, name=None, data=None):
        self.id = _norm_id(id)
        self.name = name or str(id)
        self.data = dict(data or {})

    @property
    def mention(self):
        return f"<@&{self.id}>"

    def __str__(self):
        return str(self.name)


_MENTION = re.compile(r"^<@!?([^>\s]+)>$")
_ROLE_MENTION = re.compile(r"^<@&([^>\s]+)>$")


def resolve_user(token):
    token = (token or "").strip()
    m = _MENTION.match(token)
    uid = m.group(1) if m else token.lstrip("@")
    if not uid:
        raise ValueError("no user given")
    data = fetch_user(uid)
    return YUser.from_api({**data, "id": data.get("id", uid)}) if data else YUser(uid)


def resolve_role(token, server_id):
    token = (token or "").strip()
    m = _ROLE_MENTION.match(token)
    wanted = (m.group(1) if m else token).lstrip("@#")
    for r in BOT.roles(server_id):
        if str(r.get("id")) == wanted or str(r.get("name", "")).lower() == wanted.lower():
            return YRole(r.get("id"), r.get("name"), r)
    raise ValueError(f"I couldn't find a role called {token}")


_warned_owner = False


def is_owner(user):
    """Bot owners. Use OWNER_IDS (comma separated Yapper user ids). Usernames are only a fallback when no ids are
    set, because a username can be claimed by someone else."""
    global _warned_owner
    if OWNER_IDS:
        return str(user.id) in OWNER_IDS
    if not _warned_owner:
        _warned_owner = True
        print("[yshim] OWNER_IDS is not set, so owner commands fall back to usernames. Set OWNER_IDS in .env.")
    return str(user.name).lower() in OWNER_USERNAMES


_ADMIN_PERMS = {"administrator", "admin", "manage_server", "manage_guild", "manage_community", "owner"}
_STAFF_WORDS = re.compile(r"\b(admin(istrator)?s?|owner|co-?owner|moderators?|mods?|staff)\b", re.I)


def _has_admin(blob):
    """Does this member/role dict say it can manage the server? Yapin's field names aren't documented, so this accepts
    booleans (is_admin, administrator…), permission lists/dicts of names, a Discord-style number (0x8 administrator,
    0x20 manage server), and a plain rank like role: "admin"."""
    if not isinstance(blob, dict):
        return False
    for key in ("is_admin", "admin", "administrator", "is_owner", "owner", "manage_server", "can_manage_server"):
        if blob.get(key) is True:
            return True
    for key in ("role", "rank", "member_role", "server_role"):
        v = blob.get(key)
        if isinstance(v, str) and v.lower() in ("owner", "admin", "administrator", "co-owner", "coowner"):
            return True
    perms = blob.get("permissions", blob.get("perms"))
    if isinstance(perms, (list, tuple, set)):
        return any(str(p).lower().replace(" ", "_") in _ADMIN_PERMS for p in perms)
    if isinstance(perms, dict):
        return any(perms.get(k) for k in _ADMIN_PERMS)
    if isinstance(perms, str) and perms.isdigit():
        perms = int(perms)
    if isinstance(perms, int) and not isinstance(perms, bool):
        return bool(perms & 0x8 or perms & 0x20)
    return False


_denied_logged = set()


def is_admin(server_id, user_id):
    """Best effort: bot owners, the server owner, a member/role flagged as administrator, or a role named like
    Admin / Owner / Moderator / Staff (only admins can create roles, so a name is a fair sign)."""
    if str(user_id) in OWNER_IDS:
        return True
    try:
        server = BOT.server(server_id)
        owner_keys = ("owner_id", "ownerId", "owner", "created_by", "creator_id", "founder_id")
        if any(str(server.get(k) if not isinstance(server.get(k), dict) else server[k].get("id")) == str(user_id) for k in owner_keys if server.get(k) is not None):
            return True
        member = BOT.member(server_id, user_id)
        if _has_admin(member) or (isinstance(member.get("user"), dict) and _has_admin(member["user"])):
            return True
        role_ids = {str(r.get("id") if isinstance(r, dict) else r) for r in (member.get("roles") or member.get("role_ids") or [])}
        for r in BOT.roles(server_id):
            if str(r.get("id")) in role_ids and (_has_admin(r) or _STAFF_WORDS.search(str(r.get("name") or ""))):
                return True
        key = (str(server_id), str(user_id))
        if key not in _denied_logged:     # once per person, to help work out which field Yapin uses
            _denied_logged.add(key)
            print(f"[admin check] {user_id} is not an admin in {server_id}. member fields: {sorted(member)}; "
                  f"server fields: {sorted(server)}; roles: {[r.get('name') for r in BOT.roles(server_id) if str(r.get('id')) in role_ids]}")
    except Exception as e:
        print(f"[admin check] failed for {user_id} in {server_id}: {e}")
    return False


# --------------------------------------------------------------------------- sending

_PRIVATE_TEXT = re.compile(r"^\s*(?::x:|❌|⚠️|:warning:|usage:|only\b|you (?:need|can't|cannot|haven't|do not|don't)|couldn't|could not|i couldn't|invalid|no such|not found|unknown|this command only)", re.I)
_PRIVATE_TITLE = re.compile(r"\b(error|failed|not found|invalid|couldn't|could not|unable|no results|missing|denied|private)\b", re.I)


def looks_private(content, embed):
    """Replies that only matter to the person who ran the command (errors, usage hints, 'not found') are sent
    privately, so a typo doesn't leave a pile of error messages in the channel."""
    if isinstance(content, str) and _PRIVATE_TEXT.match(content):
        return True
    title = getattr(embed, "title", None) if embed is not None else None
    return bool(title and _PRIVATE_TITLE.search(str(title)))


class SentMessage:
    def __init__(self, channel_id, data):
        data = data or {}
        self.id = data.get("id")
        self.channel_id = channel_id
        self.data = data

    async def edit(self, *, content=None, embed=None, view=None):
        _edit(self.channel_id, self.id, content, embed, view)
        return self

    async def delete(self):
        BOT.delete(self.channel_id, self.id)


_embed_failures = 0


def _disable_embeds(err):
    """One refused embed (too long, a bad link…) only costs that message its embed. If embeds keep being refused,
    something is wrong with the format itself, so after a few in a row the shim stops trying them for this run."""
    global _embeds_ok, _embed_failures
    _embed_failures += 1
    print(f"[yshim] Yapin refused an embed ({err}); sending that message as plain text.")
    if _embed_failures >= 5 and _embeds_ok:
        _embeds_ok = False
        print("[yshim] Embeds were refused 5 times in a row, so the rest of this run uses plain text. "
              "Set YAPPER_EMBED_MODE=text to skip the attempt, or fix Embed.to_dict() in yshim.py.")


def _embed_worked():
    global _embed_failures
    _embed_failures = 0


def _components(view):
    if view is None:
        return None
    _touch(view)
    return view.to_components()


def _join(*parts):
    return "\n".join(p for p in parts if p).strip()


def _send_msg(channel_id, text, embeds=None, comps=None, reply_to=None, ephemeral=False, via=None):
    text = str(text or "")
    if len(text) > 2000:       # Yapin's limit for message content
        text = text[:1997] + "…"
    if via is not None:        # the answer to a slash command: Yapin shows "<user> used /cmd" above it, and ephemeral is truly private
        out = via._answer("ephemeral" if ephemeral else "reply", text, embeds, comps)
        return out if isinstance(out, dict) else {}
    if not ephemeral:
        return BOT.send(channel_id, text, embeds=embeds, components=comps, reply_to=reply_to)
    body = {"content": text, "ephemeral": True}   # only the person who ran the command sees it
    if reply_to:
        body["reply_to"] = reply_to
    if embeds:
        body["embeds"] = embeds
    if comps:
        body["components"] = comps
    return BOT.api("POST", f"/channels/{channel_id}/messages", body).get("message") or {}


def _post(channel_id, content=None, embed=None, view=None, reply_to=None, ephemeral=False, via=None):
    comps = _components(view) or None
    text = clean(content) if content else ""
    if embed is not None and _embeds_ok:
        try:
            sent = SentMessage(channel_id, _send_msg(channel_id, text, [embed.to_dict()], comps, reply_to, ephemeral, via))
            _embed_worked()
            return sent
        except YapperError as e:
            if EMBED_MODE == "embed" or e.status not in (400, 422):
                raise
            _disable_embeds(e)
    if embed is not None:
        text = _join(text, embed.to_text())
    return SentMessage(channel_id, _send_msg(channel_id, text or "\u200b", None, comps, reply_to, ephemeral, via))


def _edit(channel_id, message_id, content=None, embed=None, view=None):
    comps = _components(view)
    text = clean(content) if content is not None else None
    if embed is not None and _embeds_ok:
        try:
            return BOT.edit(channel_id, message_id, content=text if text is not None else "", embeds=[embed.to_dict()], components=comps)
        except YapperError as e:
            if EMBED_MODE == "embed" or e.status not in (400, 422):
                raise
            _disable_embeds(e)
    if embed is not None:
        return BOT.edit(channel_id, message_id, content=_join(text, embed.to_text()) or "​", embeds=[], components=comps)
    return BOT.edit(channel_id, message_id, content=text, components=comps)


def post(channel_id, content=None, embed=None, view=None, reply_to=None):
    """Send a message from anywhere (events, background tasks). Returns a SentMessage."""
    return _post(channel_id, content, embed, view, reply_to)


# --------------------------------------------------------------------------- buttons and views

class ButtonStyle:
    primary = "primary"
    secondary = "secondary"
    success = "success"
    danger = "danger"
    link = "link"
    blurple = "primary"
    gray = "secondary"
    grey = "secondary"
    green = "success"
    red = "danger"


def _button_emoji(e):
    return e if isinstance(e, str) and e and not e.startswith("<") else None


class Button:
    def __init__(self, *, style="secondary", label=None, disabled=False, custom_id=None, url=None, emoji=None, row=None):
        self.style = style
        self.label = label
        self.disabled = disabled
        self.custom_id = custom_id
        self.url = url
        self.emoji = emoji
        self.callback = None
        self.view = None
        self._uid = next(_uid)

    @property
    def is_link(self):
        return self.style == "link" or bool(self.url)

    def _to_dict(self, view):
        label = self.label or _button_emoji(self.emoji) or ("Open" if self.is_link else "•")
        if self.is_link:
            return yapper.link_button(str(label), self.url)
        return yapper.btn(str(label), f"v:{view._vid}:{self._uid}", self.style or "secondary",
                          emoji=_button_emoji(self.emoji), disabled=bool(self.disabled))


def _button_deco(*, label=None, style="secondary", emoji=None, custom_id=None, disabled=False, row=None):
    def deco(fn):
        fn.__yshim_button__ = dict(label=label, style=style, emoji=emoji, custom_id=custom_id, disabled=disabled)
        return fn
    return deco


VIEWS = {}
_VIEWS_LOCK = threading.Lock()


class View:
    def __init__(self, *, timeout=180.0):
        self.timeout = timeout
        self.children = []
        self.message = None
        self._vid = secrets.token_hex(4)
        self._deadline = None
        self._finished = False
        for cls in reversed(type(self).__mro__):
            for attr in vars(cls).values():
                spec = getattr(attr, "__yshim_button__", None)
                if spec:
                    btn = Button(**spec)
                    btn.callback = functools.partial(self._call_decorated, attr, btn)
                    self.add_item(btn)

    async def _call_decorated(self, fn, button, interaction):
        return await fn(self, interaction, button)

    def add_item(self, item):
        if len(self.children) >= 25:
            raise ValueError("a view holds at most 25 buttons")
        item.view = self
        self.children.append(item)
        return self

    def remove_item(self, item):
        if item in self.children:
            self.children.remove(item)
        return self

    def clear_items(self):
        self.children.clear()
        return self

    def stop(self):
        self._finished = True
        with _VIEWS_LOCK:
            VIEWS.pop(self._vid, None)

    def is_finished(self):
        return self._finished

    async def interaction_check(self, interaction):
        return True

    async def on_timeout(self):
        pass

    def to_components(self):
        rows, cur = [], []
        for item in self.children:
            cur.append(item._to_dict(self))
            if len(cur) == 5:
                rows.append(yapper.row(*cur))
                cur = []
        if cur:
            rows.append(yapper.row(*cur))
        return rows


def _touch(view):
    """Remember a view that has clickable (non-link) buttons, so clicks can find it. Expires after its timeout."""
    if not any(not c.is_link for c in view.children):
        return
    view._deadline = time.time() + (view.timeout if view.timeout else 6 * 3600)
    with _VIEWS_LOCK:
        VIEWS[view._vid] = view


def _sweeper():
    while True:
        time.sleep(5)
        now = time.time()
        with _VIEWS_LOCK:
            expired = [v for v in VIEWS.values() if v._deadline and v._deadline < now]
            for v in expired:
                VIEWS.pop(v._vid, None)
        for v in expired:
            if v.timeout is not None and not v._finished:
                threading.Thread(target=_run_coro, args=(v.on_timeout(),), daemon=True).start()


def _run_coro(coro):
    try:
        asyncio.run(coro)
    except Exception:
        traceback.print_exc()


def _on_click(i):
    threading.Thread(target=_click_thread, args=(i,), daemon=True, name="button-click").start()


def _click_thread(i):
    parts = str(i.custom_id or "").split(":")
    view = VIEWS.get(parts[1]) if len(parts) == 3 else None
    item = next((c for c in view.children if str(c._uid) == parts[2]), None) if view else None
    if not item or not item.callback:
        try:
            i.ephemeral("That button has expired. Run the command again.")
        except Exception:
            pass
        return
    inter = Interaction(click=i)

    async def run():
        if await view.interaction_check(inter) is False:
            return
        _touch(view)
        await item.callback(inter)

    try:
        asyncio.run(run())
    except Exception as e:
        traceback.print_exc()
        if not inter._responded:
            try:
                i.ephemeral(f"Something went wrong: {e}")
                inter._responded = True
            except Exception:
                pass
    if not inter._responded:
        try:
            i.ack()
        except Exception:
            pass


ui = types.SimpleNamespace(View=View, Button=Button, button=_button_deco)


# --------------------------------------------------------------------------- private (ephemeral) replies

# --------------------------------------------------------------------------- Interaction

class _Response:
    def __init__(self, inter):
        self._i = inter

    async def defer(self, *, ephemeral=False, **_):
        self._i._defer(ephemeral)

    async def send_message(self, content=None, *, embed=None, embeds=None, view=None, ephemeral=False, **_):
        return self._i._send(content, embed, view, ephemeral)

    async def edit_message(self, *, content=None, embed=None, view=None, **_):
        return self._i._edit_message(content, embed, view)

    async def send_modal(self, modal):
        """Open a yapper.Modal for the person (only as the first answer). Comes back to bot.on_modal(<modal's custom_id>)."""
        target = self._i._click if self._i._click is not None else self._i._msg
        show = getattr(target, "show_modal", None)
        if show is None:
            raise NotImplementedError("This message can't open a form (it needs to be a slash command)")
        self._i._responded = True
        return show(modal)


class _Followup:
    def __init__(self, inter):
        self._i = inter

    async def send(self, content=None, *, embed=None, embeds=None, view=None, ephemeral=False, wait=False, **_):
        return self._i._send(content, embed, view, ephemeral)


class Interaction:
    """One command call (a Yapper message) or one button click, with discord.py-style helpers."""

    def __init__(self, msg=None, click=None):
        self._msg = msg
        self._click = click
        self._original = None
        self._responded = False
        self._deferred = False
        self._sent_first = False
        self._answered = False          # the slash command's one interaction answer has been used
        self._placeholder = None
        self.created_at = datetime.now(timezone.utc)
        if msg is not None:
            self.channel_id = msg.get("channel_id")
            self._guild_id = _first(msg, "server_id", "guild_id")
            self.user = YUser.from_message(msg)
        else:
            self.channel_id = _first(click, "channel_id")
            self._guild_id = _first(click, "server_id", "guild_id")
            self.user = YUser(click.get("user_id"), name=click.get("username"))
        self.user._guild_id = lambda: self.guild_id
        self.response = _Response(self)
        self.followup = _Followup(self)

    @property
    def guild_id(self):
        if self._guild_id is None and self.channel_id is not None:
            ch = BOT.channel(self.channel_id)
            self._guild_id = _first(ch or {}, "server_id", "guild_id")
        return self._guild_id

    @property
    def is_dm(self):
        return self.guild_id is None

    # -- internals used by response / followup
    def _command_msg(self):
        """The slash-command event to answer through (None for typed !commands and clicks, which are answered another way)."""
        m = self._msg
        return m if m is not None and callable(getattr(m, "_answer", None)) and m.get("interaction") else None

    def _defer(self, ephemeral=False):
        self._deferred = True
        via = self._command_msg()
        if via is not None and not self._answered and not ephemeral:
            # a command must be answered quickly: put a placeholder up now, the real answer replaces it
            try:
                sent = _post(self.channel_id, "⏳ Working on it…", via=via)
            except Exception:
                traceback.print_exc()
                return
            self._answered = True
            if sent.id:
                self._placeholder = self._original = sent

    def _send(self, content, embed, view, ephemeral):
        self._responded = True
        if not ephemeral and not self._sent_first and looks_private(content, embed):
            ephemeral = True
        if self._click is not None:
            embeds = None
            text = clean(content) if content else ""
            if embed is not None:
                if _embeds_ok:
                    embeds = [embed.to_dict()]
                else:
                    text = _join(text, embed.to_text())
            comps = _components(view) or None
            if ephemeral:
                answer = getattr(self._click, "_answer", None)
                if answer is None:      # older yapper.py: private replies cannot carry components
                    self._click.ephemeral(text, embeds)
                    return None
                try:
                    answer("ephemeral", text, embeds, comps)
                except YapperError:
                    if not comps:
                        raise
                    answer("ephemeral", text, embeds)   # server refused buttons on a private reply
            else:
                self._click.reply(text, embeds, comps)
            return None
        if self._placeholder is not None and not ephemeral:
            sent, self._placeholder = self._placeholder, None
            _edit(self.channel_id, sent.id, content if content else "", embed, view)
            self._sent_first = True
            return sent
        via = None if self._answered else self._command_msg()
        # slash commands have no chat message to quote; typed !commands quote theirs once
        reply_to = None if (self._sent_first or self._command_msg() is not None) else (self._msg or {}).get("id")
        sent = _post(self.channel_id, content, embed, view, reply_to=reply_to, ephemeral=ephemeral, via=via)
        if via is not None:
            self._answered = True
        self._sent_first = True
        if self._original is None:
            self._original = sent
        return sent

    def _edit_message(self, content, embed, view):
        """Click only: change the message the button is on."""
        self._responded = True
        embeds = None
        text = clean(content) if content is not None else None
        if embed is not None:
            if _embeds_ok:
                embeds = [embed.to_dict()]
                text = text if text is not None else ""
            else:
                text = _join(text, embed.to_text()) or "​"
                embeds = []
        self._click.update(text, embeds, _components(view))

    async def edit_original_response(self, *, content=None, embed=None, view=None, **_):
        if self._original is None:
            return self._send(content, embed, view, False)
        self._placeholder = None
        await self._original.edit(content=content, embed=embed, view=view)
        return self._original


# --------------------------------------------------------------------------- commands

class _Usage(Exception):
    pass


class _Spec:
    def __init__(self, name, description, fn):
        self.name = name
        self.description = description
        self.fn = fn
        self.params = list(inspect.signature(fn).parameters.values())[1:]

    @property
    def usage(self):
        bits = []
        for p in self.params:
            bits.append(f"[{p.name}]" if p.default is not inspect.Parameter.empty else f"<{p.name}>")
        return f"/{self.name} " + " ".join(bits) if bits else f"/{self.name}"


def _unwrap(ann):
    if typing.get_origin(ann) is typing.Union:
        args = [a for a in typing.get_args(ann) if a is not type(None)]
        return args[0] if args else str
    return ann


def _convert(token, ann, inter):
    ann = _unwrap(ann)
    try:
        if ann is int:
            return int(token)
        if ann is bool:
            low = token.lower()
            if low in ("true", "yes", "y", "on", "enable", "enabled", "1"):
                return True
            if low in ("false", "no", "n", "off", "disable", "disabled", "0"):
                return False
            raise ValueError(token)
        if ann is YUser:
            return resolve_user(token)
        if ann is YRole:
            return resolve_role(token, inter.guild_id)
    except YapperError:
        raise _Usage(f"I couldn't find {token}.")
    except ValueError as e:
        raise _Usage(str(e) if str(e) != token else f"{token} isn't valid here.")
    return token.strip("\"'") if token[:1] in "\"'" and token[-1:] == token[:1] and len(token) > 1 else token


def _next_token(rest):
    m = re.match(r'\s*("([^"]*)"|\'([^\']*)\'|(\S+))\s*(.*)$', rest, re.S)
    return (m.group(1), m.group(5)) if m else (rest, "")


def _parse(spec, raw, inter):
    rest = (raw or "").strip()
    kwargs = {}
    for idx, p in enumerate(spec.params):
        ann = _unwrap(p.annotation)
        last = idx == len(spec.params) - 1
        if not rest:
            if p.default is inspect.Parameter.empty:
                raise _Usage(f"Usage: {spec.usage}")
            continue
        if last and ann in (str, inspect.Parameter.empty):
            token, rest = rest, ""
        else:
            token, rest = _next_token(rest)
        kwargs[p.name] = _convert(token, p.annotation, inter)
    return kwargs


COMMAND_HOOKS = []      # functions called as hook(command_name, msg) whenever someone runs a command (e.g. command logging)


def _run_command(spec, msg):
    inter = Interaction(msg=msg)
    for hook in COMMAND_HOOKS:
        try:
            hook(spec.name, msg)
        except Exception:
            traceback.print_exc()
    print(f"[cmd] /{spec.name} by @{msg.get('author_username') or msg.get('username') or msg.get('author_id')} "
          f"in {msg.get('server_id') or 'a DM'}")
    try:
        kwargs = _parse(spec, msg.get("args"), inter)
    except _Usage as e:
        _safe_reply(inter, str(e) if str(e).startswith("Usage") else f"{e}\nUsage: {spec.usage}")
        return
    except Exception:
        traceback.print_exc()
        return
    try:
        asyncio.run(spec.fn(inter, **kwargs))
    except Exception:
        print(f"[cmd] /{spec.name} failed:")
        traceback.print_exc()
        _safe_reply(inter, f"⚠️ Something went wrong running /{spec.name}.")


def _safe_reply(inter, text):
    try:
        inter._send(text, None, None, False)
    except Exception:
        traceback.print_exc()


class Tree:
    """Stands in for discord.py's command tree: @tree.command(name=..., description=...)."""

    def __init__(self):
        self.commands = {}
        self.unsupported = []

    def command(self, name=None, description=""):
        def deco(fn):
            spec = _Spec((name or fn.__name__).lower(), description, fn)
            self.commands[spec.name] = spec
            return fn
        return deco

    def context_menu(self, name=None):
        """Right-click commands do not exist on Yapper. They are recorded so /help can say so."""
        def deco(fn):
            self.unsupported.append(name or fn.__name__)
            return fn
        return deco

    def get_commands(self):
        return list(self.commands.values())


tree = Tree()


def _pool_api(bot):
    """Swap bot.api for a keep-alive version. The SDK opens a brand-new TLS connection for every call (urllib), which
    is most of the delay on each reply and on /ping. One pooled requests.Session reuses the connection instead.
    Same behaviour as the SDK: 429 / 503-busy are waited out and retried, other failures raise YapperError."""
    import random
    import requests
    from requests.adapters import HTTPAdapter

    session = requests.Session()
    session.mount("https://", HTTPAdapter(pool_connections=4, pool_maxsize=32))
    session.headers.update({"Authorization": f"Bot {bot.token}", "Content-Type": "application/json",
                            "User-Agent": "shapes-bot (yapper.py)"})
    original = bot.api

    def api(method, path, body=None, timeout=40, retries=3):
        try:
            r = session.request(method, bot.base + path, data=None if body is None else json.dumps(body), timeout=timeout)
        except requests.RequestException:
            return original(method, path, body, timeout, retries)   # let the SDK's own fallback logic handle it
        try:
            data = r.json() if r.content else {}
        except ValueError:
            data = {}
        if r.status_code < 400:
            return data
        err = data if isinstance(data, dict) else {}
        busy = r.status_code == 503 and "retry_after" in err
        wait = 0.0
        if r.status_code == 429 or busy:
            try:
                wait = float(r.headers.get("Retry-After") or err.get("retry_after") or 0)
            except (TypeError, ValueError):
                wait = 0.0
        if 0 < wait <= 60 and retries > 0:
            time.sleep(wait * (1 + random.random() * 0.5))
            return api(method, path, body, timeout, retries - 1)
        if r.status_code in (502, 504):
            return original(method, path, body, timeout, retries)
        raise YapperError(err.get("error") or f"{method} {path} failed ({r.status_code})", r.status_code, wait or None)

    bot.api = api


def install(bot):
    """Call once, after every command is defined and before bot.run()."""
    global BOT
    BOT = bot
    try:
        _pool_api(bot)
    except Exception as e:
        print(f"[shim] connection pooling unavailable, using the SDK's default: {e}")
    for spec in tree.commands.values():
        def handler(msg, spec=spec):
            threading.Thread(target=_run_command, args=(spec, msg), daemon=True, name=f"cmd-{spec.name}").start()
        bot.add_command(spec.name, spec.description, handler)
    bot.button("v:")(_on_click)
    threading.Thread(target=_sweeper, daemon=True, name="view-sweeper").start()


# --------------------------------------------------------------------------- discord.py-shaped names
# Lets the command bodies keep writing discord.Embed(...), discord.Color.blue(), app_commands.describe(...).

def _noop_decorator(*_a, **_k):
    def deco(fn):
        return fn
    return deco


class _Choice:
    def __init__(self, name, value):
        self.name, self.value = name, value


discord = types.SimpleNamespace(
    Embed=Embed, Color=Color, Colour=Color, Interaction=Interaction, User=YUser, Member=YUser, Role=YRole,
    ui=ui, ButtonStyle=ButtonStyle, utils=types.SimpleNamespace(utcnow=utcnow),
)
app_commands = types.SimpleNamespace(
    allowed_installs=_noop_decorator, allowed_contexts=_noop_decorator, describe=_noop_decorator,
    choices=_noop_decorator, default_permissions=_noop_decorator, Choice=_Choice,
)
commands = types.SimpleNamespace(bot_has_permissions=_noop_decorator)

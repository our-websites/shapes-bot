"""yshim: the small slice of the discord.py API that shapes-bot uses, built on top of yapper.py.

It exists so the ~40 command bodies could be ported with very few changes. What you get:

  * Embed / Color      -> embed dicts for Yapper (see "Embeds" below)
  * ui.View / ui.Button / @ui.button -> Yapper buttons. Clicks are routed back to the callbacks.
  * Interaction        -> wraps a Yapper command message (or a button click). It offers the calls the commands
                          already make: response.send_message / defer / edit_message, followup.send,
                          edit_original_response.
  * tree.command(...)  -> registers a Yapper command and parses "/command some args" into the function's parameters.

Things Yapper cannot do (so they do not exist here): select menus, modals, context menus, ephemeral replies to
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
            d["fields"] = [{"name": (clean(str(f.name))[:256] or "​"), "value": (clean(str(f.value))[:1024] or "​"),
                            "inline": bool(f.inline)} for f in self.fields[:25]]
        if self._footer and (self._footer.get("text") or self._footer.get("icon_url")):
            d["footer"] = {k: (clean(v) if k == "text" else v) for k, v in self._footer.items() if v}
        if self._thumbnail:
            d["thumbnail"] = {"url": self._thumbnail}
        if self._image:
            d["image"] = {"url": self._image}
        if self._author and self._author.get("name"):
            d["author"] = {k: v for k, v in self._author.items() if v}
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
        """Read live from the profile data, because the full profile is merged in after the user is created."""
        d = self.data
        kind = str(_first(d, "type", "kind", "account_type", "user_type") or "").lower()
        return bool(self._bot_hint or d.get("bot") is True or d.get("is_bot") is True or d.get("isBot") is True
                    or d.get("author_bot") is True or kind == "bot")

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
        return _image_url(_first(self.data, "avatar_url", "avatarUrl", "avatar", "image_url", "author_avatar_url", "profile_picture"))

    @property
    def banner_url(self):
        return _image_url(_first(self.data, "banner_url", "bannerUrl", "banner", "profile_banner"))

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


def _has_admin(blob):
    if not isinstance(blob, dict):
        return False
    for key in ("is_admin", "admin", "administrator", "is_owner", "owner"):
        if blob.get(key) is True:
            return True
    perms = blob.get("permissions")
    if isinstance(perms, (list, tuple, set)):
        return any(str(p).lower() in ("administrator", "admin", "manage_server") for p in perms)
    if isinstance(perms, dict):
        return any(perms.get(k) for k in ("administrator", "admin", "manage_server"))
    return False


def is_admin(server_id, user_id):
    """Best effort: bot owners, the server owner, or a member/role flagged as administrator.
    Yapper's permission fields are not documented in what I could read, so several likely names are checked."""
    if str(user_id) in OWNER_IDS:
        return True
    try:
        server = BOT.server(server_id)
        if str(_first(server, "owner_id", "ownerId", "owner")) == str(user_id):
            return True
        member = BOT.member(server_id, user_id)
        if _has_admin(member):
            return True
        role_ids = {str(r.get("id") if isinstance(r, dict) else r) for r in (member.get("roles") or [])}
        return any(_has_admin(r) for r in BOT.roles(server_id) if str(r.get("id")) in role_ids)
    except Exception:
        return False


# --------------------------------------------------------------------------- sending

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


def _disable_embeds(err):
    global _embeds_ok
    if _embeds_ok:
        _embeds_ok = False
        print(f"[yshim] Yapper rejected the embed format ({err}). Falling back to plain text for this run. "
              f"Set YAPPER_EMBED_MODE=text to skip the attempt, or fix Embed.to_dict() in yshim.py.")


def _components(view):
    if view is None:
        return None
    _touch(view)
    return view.to_components()


def _join(*parts):
    return "\n".join(p for p in parts if p).strip()


def _send_msg(channel_id, text, embeds=None, comps=None, reply_to=None, ephemeral=False):
    if not ephemeral:
        return BOT.send(channel_id, text, embeds=embeds, components=comps, reply_to=reply_to)
    body = {"content": str(text or ""), "ephemeral": True}   # only the person who ran the command sees it
    if reply_to:
        body["reply_to"] = reply_to
    if embeds:
        body["embeds"] = embeds
    if comps:
        body["components"] = comps
    return BOT.api("POST", f"/channels/{channel_id}/messages", body).get("message") or {}


def _post(channel_id, content=None, embed=None, view=None, reply_to=None, ephemeral=False):
    comps = _components(view) or None
    text = clean(content) if content else ""
    if embed is not None and _embeds_ok:
        try:
            return SentMessage(channel_id, _send_msg(channel_id, text, [embed.to_dict()], comps, reply_to, ephemeral))
        except YapperError as e:
            if EMBED_MODE == "embed" or e.status not in (400, 422):
                raise
            _disable_embeds(e)
    if embed is not None:
        text = _join(text, embed.to_text())
    return SentMessage(channel_id, _send_msg(channel_id, text or "\u200b", None, comps, reply_to, ephemeral))


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

    async def defer(self, **_):
        self._i._deferred = True

    async def send_message(self, content=None, *, embed=None, embeds=None, view=None, ephemeral=False, **_):
        return self._i._send(content, embed, view, ephemeral)

    async def edit_message(self, *, content=None, embed=None, view=None, **_):
        return self._i._edit_message(content, embed, view)

    async def send_modal(self, _modal):
        raise NotImplementedError("Yapper has no modals")


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
    def _send(self, content, embed, view, ephemeral):
        self._responded = True
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
        reply_to = None if self._sent_first else (self._msg or {}).get("id")
        sent = _post(self.channel_id, content, embed, view, reply_to=reply_to, ephemeral=ephemeral)
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


def _run_command(spec, msg):
    inter = Interaction(msg=msg)
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
        traceback.print_exc()
        _safe_reply(inter, f"⚠️ Something went wrong running /{spec.name}.")


def _safe_reply(inter, text):
    try:
        _post(inter.channel_id, text, reply_to=(inter._msg or {}).get("id"))
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

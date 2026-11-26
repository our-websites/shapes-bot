"""Shapes for Yapper.

Same commands as the Discord bot, running on Yapper through yapper.py. Data lives in Cloudflare D1
(see tables.py), and a small web server runs on port 5004.

Layout of this file:
  1. setup (env, databases, helpers)
  2. commands rewritten for Yapper (no select menus / modals / ephemeral replies there)
  3. commands carried over from the Discord bot (Roblox, Google, AI, GitHub, Instagram, ...)
  4. events (counting, autorole, ready)
  5. web server + startup
"""

import sys
sys.dont_write_bytecode = True      # don't litter __pycache__ folders
import asyncio
import base64
import hashlib
import hmac
import html
import io
import json
import os
import re
import socket
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
import time
import typing
import urllib
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import aiohttp
import pytz
import requests
import yapper
from flask import Flask, Response, jsonify, redirect, render_template_string, request, session, url_for, has_request_context
from flask_cors import CORS
from openai import OpenAI
from slpp import slpp as lua

import automod


class _Stamped:
    """Wraps stdout/stderr so every line is timestamped and flushed at once. Hosting panels buffer a plain print
    until the output fills up, which is why logs showed up late (or never) before."""

    def __init__(self, stream):
        self._s, self._new = stream, True
        self._lock = threading.Lock()

    def write(self, text):
        if not text:
            return 0
        if isinstance(text, (bytes, bytearray)):          # some libraries (click) write bytes
            text = bytes(text).decode("utf-8", "replace")
        with self._lock:
            out = []
            for chunk in text.splitlines(keepends=True):
                if self._new:
                    out.append(time.strftime("%H:%M:%S "))
                out.append(chunk)
                self._new = chunk.endswith("\n")
            self._s.write("".join(out))
            self._s.flush()
        return len(text)

    def flush(self):
        self._s.flush()

    def __getattr__(self, name):
        return getattr(self._s, name)


if not isinstance(sys.stdout, _Stamped):
    sys.stdout, sys.stderr = _Stamped(sys.stdout), _Stamped(sys.stderr)
import logging
logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="[%(levelname)s] %(name)s: %(message)s")
logging.getLogger("werkzeug").setLevel(logging.WARNING)     # one line per web request drowned everything else
from tables import Table
# ====================================================================================================
# The discord.py-shaped layer: the small slice of the discord.py API that shapes-bot uses,
# built on top of yapper.py.
#
# It exists so the ~40 command bodies could be ported with very few changes. What you get:
#
#   * Embed / Color      -> embed dicts for Yapper (see "Embeds" below)
#   * ui.View / ui.Button / @ui.button -> Yapper buttons. Clicks are routed back to the callbacks.
#   * Interaction        -> wraps a Yapper command message (or a button click). It offers the calls the commands
#                           already make: response.send_message / defer / edit_message, followup.send,
#                           edit_original_response.
#   * tree.command(...)  -> registers a Yapper command and parses "/command some args" into the function's parameters.
#
# Things Yapper cannot do (so they do not exist here): select menus, context menus, ephemeral replies to
# commands. Buttons support ephemeral replies; for commands they are posted normally.
#
# Embeds
#   The Yapper docs were not readable while this was written, so embeds are sent as Discord-style dicts
#   (title, description, url, color, fields, footer, thumbnail, image, author, timestamp). If Yapper rejects them
#   (HTTP 400/422) the layer switches itself to a readable plain-text version for the rest of the run.
#   YAPPER_EMBED_MODE=auto (default) | embed (never fall back) | text (never send embeds).
#   If the format is different, fix Embed.to_dict() below, it is the only place that builds it.
# ====================================================================================================
import functools
import inspect
import itertools
import random
import secrets
import traceback
import types

from yapper import YapperError

BOT = None
EMBED_MODE = os.environ.get("YAPPER_EMBED_MODE", "auto").strip().lower()
_embeds_ok = EMBED_MODE != "text"
_uid = itertools.count(1)

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
        """Yapin's user object has no documented bot flag, so this looks at every sign: the message event's author_bot, a "bot" badge
        (a plain word or a badge object), a bot flag/object, an account type of bot/app/integration, an application or bot id, or an
        integration-style flag. Anything that says "bot" in one of those places counts."""
        d = self.data
        words = {"bot", "verified_bot", "app", "application", "integration", "automated"}
        norm = lambda v: str(v).lower().replace(" ", "_").replace("-", "_")
        kind = norm(_first(d, "type", "kind", "account_type", "user_type", "accountType", "userType") or "")
        badges = set()
        for b in (d.get("badges") or []):
            badges.add(norm(b)) if isinstance(b, str) else badges.update(norm(b.get(k)) for k in ("name", "id", "slug", "label", "key") if isinstance(b, dict) and b.get(k))
        flags = d.get("flags")
        flagset = {norm(x) for x in flags} if isinstance(flags, (list, tuple)) else set()
        flag = d.get("bot")
        return bool(self._bot_hint or flag is True or isinstance(flag, dict) or str(flag).lower() == "true"
                    or d.get("is_bot") is True or d.get("isBot") is True or d.get("author_bot") is True
                    or d.get("is_application") is True or d.get("application") or d.get("application_id") or d.get("bot_id") or d.get("botId")
                    or d.get("managed") is True or d.get("integration") is True
                    or kind in words or badges & words or flagset & words)

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


def resolve_user(token, inter=None):
    """A user from a mention, an id, or a @username. Yapin's API only looks people up by id, so a username is matched to an id first
    (the person running the command, then anyone in the servers the bot is in) and the lookups that need an id get the real one."""
    token = (token or "").strip()
    m = _MENTION.match(token)
    uid = m.group(1) if m else token.lstrip("@")
    if not uid:
        raise ValueError("no user given")
    ids = getattr(inter, "_mentions", None) or {}
    if not m and uid.lower() in ids:                       # the event said who was pinged: use its id, not the text
        uid = ids[uid.lower()]
    data = fetch_user(uid)
    if not data and not m:
        wanted = uid.lower()
        me = getattr(inter, "user", None)
        if me is not None and str(getattr(me, "name", "")).lower() == wanted:
            uid = str(me.id)
        else:
            try:
                hit = next((p for p in _all_people() if p["name"].lower() == wanted), None)
            except Exception:
                hit = None
            if hit:
                uid = hit["id"]
        if uid != token.lstrip("@"):
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
        print("[shapes] OWNER_IDS is not set, so owner commands fall back to usernames. Set OWNER_IDS in .env.")
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
    print(f"[shapes] Yapin refused an embed ({err}); sending that message as plain text.")
    if _embed_failures >= 5 and _embeds_ok:
        _embeds_ok = False
        print("[shapes] Embeds were refused 5 times in a row, so the rest of this run uses plain text. "
              "Set YAPPER_EMBED_MODE=text to skip the attempt, or fix Embed.to_dict() in main.py.")


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


def _link_buttons(comps):
    """[(label, url)] of every link button in a component tree."""
    out = []
    if isinstance(comps, dict):
        if comps.get("url") and comps.get("label"):
            out.append((str(comps["label"]), str(comps["url"])))
        for v in comps.values():
            if isinstance(v, (list, dict)):
                out += _link_buttons(v)
    elif isinstance(comps, list):
        for v in comps:
            out += _link_buttons(v)
    return out


def _send_msg(channel_id, text, embeds=None, comps=None, reply_to=None, ephemeral=False, via=None):
    text = str(text or "")
    if ephemeral and comps:
        # Private messages may not show buttons (they can't be edited or looked up again), so every link button is also
        # written out as a plain link underneath: the person always has a way to open it.
        links = [f"[{label}]({url})" for label, url in _link_buttons(comps)]
        if links:
            text = _join(text, " · ".join(dict.fromkeys(links)))
    if len(text) > 2000:       # Yapin's limit for message content
        text = text[:1997] + "…"
    if via is not None:        # the answer to a slash command: Yapin shows "<user> used /cmd" above it, and ephemeral is truly private
        kind = "ephemeral" if ephemeral else "reply"
        try:
            out = via._answer(kind, text, embeds, comps)
        except YapperError as e:
            if not comps:
                raise
            print(f"[buttons] Yapin' refused the buttons on this {kind} answer ({e}); sending it without them")
            out = via._answer(kind, text, embeds)       # the server refused buttons on this answer: send it without them
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
        fn.__button__ = dict(label=label, style=style, emoji=emoji, custom_id=custom_id, disabled=disabled)
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
                spec = getattr(attr, "__button__", None)
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
            return resolve_user(token, inter)
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


def event_mentions(msg):
    """{lowercase username: user id} for everyone the event itself says was mentioned. A ping in the chat box arrives as
    "@name" text, but the event carries the real ids next to it, so those are trusted before any name guessing."""
    found = {}

    def walk(value, depth=0):
        if depth > 4:
            return
        if isinstance(value, dict):
            uid = value.get("user_id") or value.get("id")
            name = value.get("username") or value.get("name") or value.get("display_name")
            if uid and name and isinstance(name, str):
                found.setdefault(name.lower().lstrip("@"), str(uid))
            for v in value.values():
                walk(v, depth + 1)
        elif isinstance(value, (list, tuple)):
            for v in value:
                walk(v, depth + 1)
    try:
        for key, value in dict(msg).items():
            if "mention" in str(key).lower() or str(key).lower() in ("options", "resolved", "users", "args_users"):
                walk(value)
    except Exception:
        pass
    return found


def _run_command(spec, msg):
    _dm_ctx.command = spec.name        # replies sent from here on are labelled with the command (DM log)
    try:
        _run_command_inner(spec, msg)
    finally:
        _dm_ctx.command = None


def _run_command_inner(spec, msg):
    inter = Interaction(msg=msg)
    inter._mentions = event_mentions(msg)
    if "@" in str(msg.get("args") or "") or inter._mentions:     # what a ping looks like in the event (names and keys only), to check it against the logs
        print(f"[cmd] mention data: keys={sorted(str(k) for k in dict(msg).keys())} ids={inter._mentions}")
    for hook in COMMAND_HOOKS:
        try:
            hook(spec.name, msg)
        except Exception:
            traceback.print_exc()
    print(f"[cmd] /{spec.name} by @{msg.get('author_username') or msg.get('username') or msg.get('author_id')} "
          f"in {msg.get('server_id') or 'a DM'}")
    if not _first(msg, "server_id", "guild_id"):               # a command used in a DM is a DM to the bot
        who = str(msg.get("author_id") or msg.get("user_id") or "")
        if who:
            _dm_channels[str(msg.get("channel_id"))] = who
            log_dm("in", who, f"/{spec.name} {msg.get('args') or ''}".strip(), "command in a DM", name=msg.get("author_username") or msg.get("username"))
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

# ====================================================================================================
# end of the discord.py-shaped layer
# ====================================================================================================

# Emoji used by the Discord version were custom server emoji. clean() turns them into plain text/emoji.
Emojis = {
    "loading": "<a:loading:1416950730094542881>",
    "RobloxEmojis": {
        "rolimons": "<:RolimonsLogo:1417258794974711901>",
        "logo": "<:RobloxLogo:1416951004607418398>",
        "verified": "<:RobloxVerified:1416951927513677874>",
        "premium": "<:RobloxPremium:1416951078200541378>",
    },
}


def _secret(env_name, file_name=None):
    """A secret from the environment, or from a local text file (how the Discord bot stored them)."""
    value = os.environ.get(env_name)
    if value:
        return value.strip()
    if file_name:
        try:
            with open(file_name, "r") as f:
                return f.read().strip()
        except OSError:
            pass
    return None


# OpenAI-compatible client for /ai
chatgpt = OpenAI(api_key=os.getenv("OPENAI_KEY") or "unset", base_url="https://openrouter.ai/api/v1")
AIModel = "nvidia/nemotron-3-ultra-550b-a55b:free"

# === Databases (Cloudflare D1, or local JSON when the Cloudflare env vars are not set) ===
countingDB = Table("counting")
embedDB = Table("embed")
usersDB = Table("users")
autoroleDB = Table("autorole")
welcomeCfgDB = Table("welcome")     # server id -> its member welcome settings
warnsDB = Table("warns")                # "server:user" -> that person's warnings in that server
botstateDB = Table("botstate")        # small facts about the bot itself (when it was last running)
AI_DB = Table("ai")
automodDB = Table("automod")
auditDB = Table("audit")     # who changed what from the dashboard
cmdlogDB = Table("cmdlog")     # command log lines: "<server>:<ms>-<rand>" -> who ran what, where, when
cmdlogcfgDB = Table("cmdlogcfg")   # per server: where the command log goes
actionsDB = Table("actions")   # who did what from the dashboard: sends, DMs, settings, with username and time
dmDB = Table("dms")       # DM channels the dashboard knows about: user id -> {channel_id, name, last}
dmlogDB = Table("dmlog")  # every DM the bot sends or receives: "<ms>-<rand>" -> {at, dir, user_id, name, text, source}
DATABASES = {"embedDB": embedDB, "countingDB": countingDB, "usersDB": usersDB, "autoroleDB": autoroleDB, "AI_DB": AI_DB,
             "automodDB": automodDB, "dmDB": dmDB, "auditDB": auditDB}

# === Yapper bot ===
bot = yapper.Bot()
from yapper import telemetry
telemetry.enable(bot, interval=300)   # Shapes is apart of the official yapper.py telemetry program! (reports every 5 minutes)
bot.tree = tree
BOT = bot
owner = "leandre"
MainURL = "https://shapes-yapper.oriaxen.com"
InviteURL = os.environ.get("YAPPER_INVITE_URL", MainURL)
searchengine = "621a38269031b4e89"  # PLEASE USE YOUR OWN SEARCH ENGINE ID FROM https://cse.google.com/

APIBaseURL_Key = _secret("Shapes_API_Key")
import apiurl
APIBaseURL = apiurl.clean_url(os.environ.get("SHAPES_API_URL"), "http://localhost:1337")
# If the address above fails (or answers 403), the API is tried on its public address too.
APIFallbackURL = apiurl.clean_url(os.environ.get("SHAPES_API_FALLBACK_URL"), "https://api.shapes-yapper.oriaxen.com")
api_link = apiurl.Link(APIBaseURL, APIFallbackURL)
# The token for the API's /internal routes (its INTERNAL_TOKEN). The older Shapes_API_Key secret still works.
API_INTERNAL_TOKEN = os.environ.get("SHAPES_API_TOKEN", "").strip() or APIBaseURL_Key
APIBaseURL_Headers = {"Authorization": f"Bearer {API_INTERNAL_TOKEN}"}
_api_session = requests.Session()


def api_ready():
    return bool(API_INTERNAL_TOKEN)


API_DOWN_TEXT = "The Shapes API is down right now, so this is turned off for the moment. Try again in a few minutes."
_api_state = {"down": False, "at": 0.0}


def shapes_api(method, path, body=None, timeout=30):
    """Call the Shapes API on this machine. Returns its JSON (a dict) or None when it can't be reached.
    Remembers whether it answered, so features that need it can say "the API is down" instead of "not linked"."""
    if not api_ready():
        return None
    try:
        r = api_link.request(method, path, session=_api_session, json=body, headers=APIBaseURL_Headers, timeout=timeout)
        if r.status_code >= 502:
            raise requests.RequestException(f"HTTP {r.status_code}")
        data = r.json()
        _api_state.update(down=False, at=time.time())
        return data if isinstance(data, dict) else None
    except (requests.RequestException, ValueError):
        if not _api_state["down"]:
            print("Shapes API Is Not Responding.")
        _api_state.update(down=True, at=time.time())
        return None


def api_down():
    """True when the Shapes API isn't answering (and the bot is set up to use it). Re-checked at most every 20 seconds."""
    if not api_ready():
        return False
    if time.time() - _api_state["at"] > 20:
        shapes_api("GET", "/internal/version", timeout=4)
    return _api_state["down"]


_ROBLOX_LINK_CACHE = {}


GoogleAPIKey = _secret("GOOGLE_API_KEY", "GoogleToken.txt")
ADMIN_KEY = _secret("ADMIN_KEY", "akey.txt")

bot_ready = False
started_at = time.time()


def ecolor(user_id):
    """A user's saved embed color, or blue."""
    return embedDB.get(f"{user_id}") or discord.Color.blue()


def IsInteger(s):
    try:
        int(s)
        return True
    except (TypeError, ValueError):
        return False


def isotodiscordtimestamp(iso_timestamp_str: str, format_type: str = "D") -> str:
    """ISO timestamp -> a <t:unix:style> marker. clean() turns the marker into readable text when sending."""
    try:
        if '.' in iso_timestamp_str and iso_timestamp_str.endswith('Z'):
            iso_timestamp_str = iso_timestamp_str.split('.')[0] + '+00:00'
        elif '.' in iso_timestamp_str and '+00:00' in iso_timestamp_str:
            iso_timestamp_str = iso_timestamp_str.split('.')[0] + '+00:00'
        elif iso_timestamp_str.endswith('Z'):
            iso_timestamp_str = iso_timestamp_str.replace('Z', '+00:00')
        dt_object = datetime.fromisoformat(iso_timestamp_str)
        if dt_object.tzinfo is None:
            dt_object = pytz.utc.localize(dt_object)
        else:
            dt_object = dt_object.astimezone(pytz.utc)
        return f"<t:{int(dt_object.timestamp())}:{format_type}>"
    except ValueError:
        return None


DiscordColors = [
    ("Blue", discord.Color.blue()), ("Red", discord.Color.red()), ("Green", discord.Color.green()),
    ("Purple", discord.Color.purple()), ("Orange", discord.Color.orange()), ("Gold", discord.Color.gold()),
    ("Teal", discord.Color.teal()), ("Dark Blue", discord.Color.dark_blue()), ("Dark Red", discord.Color.dark_red()),
    ("Dark Green", discord.Color.dark_green()), ("Dark Purple", discord.Color.dark_purple()),
    ("Dark Orange", discord.Color.dark_orange()), ("Dark Gold", discord.Color.dark_gold()),
    ("Dark Teal", discord.Color.dark_teal()),
]


# === Server / channel helpers ===
_chan_server = {}


def server_of(channel_id, hint=None):
    """The server a channel belongs to (None for a DM)."""
    if hint:
        return str(hint)
    key = str(channel_id)
    if key not in _chan_server:
        ch = bot.channel(channel_id)
        _chan_server[key] = str(_first(ch or {}, "server_id", "guild_id") or "") or None
    return _chan_server[key]


def resolve_channel(server_id, token):
    """A text channel from a <#id> mention, an id, or a name."""
    token = (token or "").strip()
    m = re.match(r"^<#([^>\s]+)>$", token)
    wanted = (m.group(1) if m else token).lstrip("#")
    for c in bot.channels(server_id, type="text"):
        if str(c.get("id")) == wanted or str(c.get("name", "")).lower() == wanted.lower():
            return c
    return None


_member_cache = {"at": 0, "value": 0}


_KNOWN_BOT = {}          # user id -> True/False, from looking the account up (a member record doesn't always say)


_lookups_running = set()


def _lookup_bots(members, sid="", cap=200):
    """Members whose own record doesn't say "bot": the full account is asked for once (in the background, so a command never waits
    on it) and remembered for good. What is already known is applied now, the rest shows up the next time the list refreshes."""
    todo = [p["id"] for p in members if not p["bot"] and p["id"] not in _KNOWN_BOT][:cap]
    if todo and sid not in _lookups_running:
        _lookups_running.add(sid)

        def work():
            try:
                with ThreadPoolExecutor(max_workers=6) as pool:
                    for uid, flag in pool.map(lambda u: (u, YUser(u, data={**fetch_user(u), "id": u}).is_bot), todo):
                        _KNOWN_BOT[uid] = bool(flag)
            except Exception as e:
                print(f"[members] bot lookup stopped: {e}")
            finally:
                _lookups_running.discard(sid)
                _cache.pop(f"allmembers:{sid}", None)       # next count uses what was learned
        threading.Thread(target=work, daemon=True).start()
    for p in members:
        if _KNOWN_BOT.get(p["id"]):
            p["bot"] = True
    return members


def member_stats(sid, total=None):
    """(members, people, bots) for a server. Members is everyone, bots included. When the member list can be read in full it is the
    truth (the API's own count may leave bots out), otherwise the API's total is used and the bots seen in the list are taken off it."""
    try:
        total = int(total) if total is not None else None
    except (TypeError, ValueError):
        total = None
    try:
        members = cached(f"allmembers:{sid}", 120, lambda: _lookup_bots(_all_members(sid), sid))
    except Exception:
        members = None
    if not members:
        return total, (total if total is not None else None), None
    bots = sum(1 for p in members if p["bot"])
    everyone = max(len(members), total or 0) if len(members) < 5000 else max(total or 0, len(members))
    return everyone, everyone - bots, bots


def server_counts(sid, total=None):
    """(members, people, bots); people and bots are None when the member list can't be read."""
    members, people, bots = member_stats(sid, total)
    return members, (None if bots is None else people), bots


def human_count(sid, total=None):
    """People in a server, bots left out (the total when the list can't be read)."""
    return member_stats(sid, total)[1]


def _total_of(server):
    return _first(server, "member_count", "members_count", "memberCount")


def server_counts_many(servers):
    """{server id: (members, people, bots)} for a list of server dicts, looked up a few at a time."""
    def one(s):
        return str(s.get("id")), member_stats(s.get("id"), _total_of(s))
    with ThreadPoolExecutor(max_workers=8) as pool:
        return dict(pool.map(one, servers))


def human_counts(servers):
    """{server id: people} for a list of server dicts."""
    return {sid: v[1] for sid, v in server_counts_many(servers).items()}


def total_members():
    """Approximate number of people across every server the bot is in (cached for 5 minutes)."""
    if time.time() - _member_cache["at"] < 300:
        return _member_cache["value"]
    total = sum(int(n or 0) for n in human_counts(list(bot.servers())).values())
    _member_cache.update(at=time.time(), value=total)
    return total


COUNTING_EMOJI_DEFAULT = {"correct": "👍", "warning": "⚠️", "wrong": "❌"}


def default_counting():
    return {"channel": None, "number": 0, "enabled": False, "warnings": 0, "lastcounter": None, "highestnumber": 0, "banned": False,
            "emojis": dict(COUNTING_EMOJI_DEFAULT), "max_warnings": COUNTING_MAX_WARNINGS, "lastmessage": None, "lastmessage_at": None}


COUNTING_MAX_WARNINGS = 3          # mistakes allowed before the count is ruined; a bot owner can change it per server


def counting_limit(data):
    """How many warnings this server gets (3 unless a bot owner changed it)."""
    v = (data or {}).get("max_warnings")
    return v if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 50 else COUNTING_MAX_WARNINGS


COUNT_WATCH_SECONDS = 15           # how often the last counted message is checked to see if it was deleted
COUNT_WATCH_WINDOW = 900           # ...for this long after it was counted; later it is checked when the next message arrives


def count_message_gone(channel, mid):
    """True only when Yapin' says the message no longer exists. Any other trouble (network, rate limit) counts as 'still there'."""
    try:
        bot.message(channel, mid)
        return False
    except yapper.YapperError as e:
        return getattr(e, "status", None) in (404, 410)
    except Exception:
        return False


def announce_deleted_count(sid, data):
    """The message with the last number was deleted: say so. The count itself stays where it was."""
    channel, number, who = data.get("channel"), int(data.get("number") or 0), data.get("lastcounter")
    data["lastmessage"] = None
    countingDB.set(sid, data)
    emo = counting_emojis(data)
    mention = f"<@{who}>" if who else "Someone"
    try:
        bot.send(channel, f"{emo['warning']} {mention} deleted their message with the last number (**{number}**)! The next number is **{number + 1}**.")
    except Exception as e:
        print(f"[counting] couldn't announce a deleted count in {sid}: {e}")


_count_watch_started = False


def start_count_watch():
    """Yapin' sends no 'message deleted' event, so look at the last counted message for a while after it was counted."""
    global _count_watch_started
    if _count_watch_started:
        return
    _count_watch_started = True

    def loop():
        while True:
            time.sleep(COUNT_WATCH_SECONDS)
            try:
                now = time.time()
                for sid, data in countingDB.items().items():
                    if not isinstance(data, dict) or not data.get("enabled") or not data.get("lastmessage") or not data.get("channel"):
                        continue
                    if now - int(data.get("lastmessage_at") or 0) > COUNT_WATCH_WINDOW:
                        continue
                    mid = data["lastmessage"]
                    if count_message_gone(data["channel"], mid) and data.get("lastmessage") == mid:
                        announce_deleted_count(sid, data)
                    time.sleep(0.2)
            except Exception as e:
                print(f"[counting] deleted-message check: {type(e).__name__}: {e}")

    threading.Thread(target=loop, name="count-watch", daemon=True).start()


def clean_emoji(value):
    """One emoji (Unicode or a custom one written like :name: / <:name:id>), or None when it isn't usable."""
    value = str(value or "").strip()
    return value if value and len(value) <= 64 and not re.search(r"\s", value) else None


def counting_emojis(data):
    """The three emoji a server uses, falling back to the defaults for any that are missing."""
    chosen = (data or {}).get("emojis") or {}
    return {k: clean_emoji(chosen.get(k)) or v for k, v in COUNTING_EMOJI_DEFAULT.items()}


# =====================================================================================
# Commands rewritten for Yapper
# (select menus and modals do not exist for commands, so these take arguments instead; private replies use ephemeral=True)
# =====================================================================================


# --- Command log: the server owner picks a channel and every command run in the server is written there ---

CMDLOG_DEFAULT = {"enabled": False, "channel": None, "public_ok": None, "mode": None, "webhook_id": None}
CMDLOG_KEEP = 300                   # lines kept per server for the dashboard
_cmdlog_queue = None
_cmdlog_sent = {}                   # server -> recent post times (a busy server can't flood Yapin' or the log channel)


def get_cmdlog(server_id):
    return {**CMDLOG_DEFAULT, **(cmdlogcfgDB.get(str(server_id)) or {})}


def server_owner_id(server_id):
    server = dict(cached(f"server:{server_id}", 30, lambda: bot.server(server_id)))
    owner = server.get("owner_id")
    return str(owner.get("id") if isinstance(owner, dict) else owner) if owner is not None else None


def is_server_owner(server_id, user_id):
    """Only the owner, not administrators. (Bot owners aren't automatically owners of anyone's server.)"""
    try:
        return server_owner_id(server_id) == str(user_id)
    except Exception:
        return False


def channel_is_public(server_id, channel_id):
    """True when everybody in the server can read this channel (Yapin' marks restricted ones private)."""
    for c in bot.channels(server_id):
        if str(c.get("id")) == str(channel_id):
            return not c.get("private")
    return True


def cmdlog_configure(server_id, channel_id=None, enabled=None, confirm_public=False):
    """Change the command log settings. Returns (config, warning): a warning (and no change to the channel) when the new
    channel can be read by everyone and the owner hasn't said that's fine."""
    cfg = get_cmdlog(server_id)
    warning = None
    if channel_id is not None and str(channel_id) != str(cfg["channel"] or ""):
        channel_id = str(channel_id)
        if channel_is_public(server_id, channel_id) and not confirm_public:
            warning = ("Everyone in this server can read that channel, so everyone would see who runs which command. "
                       "Pick a private channel, or confirm that you want public logs.")
        else:
            release_log_webhook(str(server_id), cfg)
            cfg.update(channel=channel_id, mode=None, webhook_id=None, retry_webhook_at=None,
                       public_ok=channel_id if channel_is_public(server_id, channel_id) else None)
    elif confirm_public and cfg["channel"] and channel_is_public(server_id, cfg["channel"]):
        cfg["public_ok"] = cfg["channel"]
    if enabled is not None:
        cfg["enabled"] = bool(enabled) and bool(cfg["channel"])
    cmdlogcfgDB.set(str(server_id), cfg)
    return cfg, warning


def cmdlog_rows(server_id, limit=100):
    prefix = f"{server_id}:"
    rows = [v for k, v in cmdlogDB.items().items() if k.startswith(prefix) and isinstance(v, dict)]
    return sorted(rows, key=lambda r: -int(r.get("at") or 0))[:limit]


def _cmdlog_embed(row):
    args = row.get("args") or ""
    fields = [{"name": "User", "value": f"<@{row['user_id']}> (@{row['username']})", "inline": True},
              {"name": "Channel", "value": f"<#{row['channel_id']}>", "inline": True},
              {"name": "When", "value": f"<t:{row['at']}:F>", "inline": False}]
    if args:
        fields.insert(2, {"name": "Arguments", "value": "```\n" + args.replace("```", "'''")[:200] + "\n```", "inline": False})
    return {"title": f"/{row['command']}", "fields": fields, "footer": {"text": "Shapes command log"}}


# ---- log webhooks: the per-server command logs and the global log (botlog) all post through webhooks, and share one when
# they log into the same channel (a Yapin' webhook belongs to one channel, and the sender name is set per message). ----

WEBHOOK_RETRY_SECONDS = 3600        # after Yapin' refuses webhooks for a channel, try again this often
_WEBHOOK_GONE = (401, 403, 404, 410)    # on a send: the webhook was deleted or its access was taken away
_WEBHOOK_REFUSED = (400, 401, 403, 404)  # on a create: Yapin' won't give the bot a webhook in that channel


def _log_configs():
    """{key: config} for every log: server ids for the command logs, BOTLOG_KEY for the global one."""
    return {k: c for k, c in cmdlogcfgDB.items().items() if isinstance(c, dict)}


def release_log_webhook(key, cfg):
    """Let go of a log's webhook. It is only deleted from Yapin' when no other log still uses it."""
    wid = str(cfg.get("webhook_id") or "")
    if wid and not any(str(c.get("webhook_id") or "") == wid for k, c in _log_configs().items() if k != key):
        try:
            bot.delete_webhook(wid)
        except Exception:
            pass


def find_log_webhook(key, channel):
    """A webhook we can reuse in this channel instead of making one: the one another log (the global log, say) already
    posts through there, otherwise a leftover 'Shapes ...' webhook of the bot. None when there isn't one."""
    for k, c in _log_configs().items():
        if k != key and c.get("webhook_id") and str(c.get("channel") or "") == str(channel) and c.get("mode") == "webhook":
            return str(c["webhook_id"])
    try:
        mine = {str(w.get("id")): dict(w) for w in bot.my_webhooks() if w.get("id")}      # the bot's own, newest first
        if mine:
            try:
                here = {str(w.get("id")) for w in bot.webhooks(channel)}                  # everything in this channel
            except Exception:
                here = {i for i, d in mine.items() if str(_first(d, "channel_id", "channelId", "channel") or "") == str(channel)}
            ours = [i for i in mine if i in here]
            ours.sort(key=lambda i: not str(mine[i].get("name") or "").startswith("Shapes"))   # our named ones first, newest first
            if ours:
                return ours[0]
    except Exception as e:
        print(f"[logs] couldn't look for an existing webhook: {e}")
    return None


def post_log(key, cfg, embed, username, label):
    """Post one embed to the log's channel through its webhook (found, shared or made on first use). The bot posts directly
    only for that one line when something passing goes wrong, and from then on only if Yapin' refuses webhooks there."""
    channel = cfg["channel"]

    def save():
        cmdlogcfgDB.set(key, cfg)

    if cfg.get("mode") == "bot" and time.time() >= float(cfg.get("retry_webhook_at") or 0):
        cfg.update(mode=None, retry_webhook_at=None)      # time to see whether webhooks work now
        save()
    if cfg.get("mode") != "bot":
        for _ in range(2):
            made = False
            try:
                if not cfg.get("webhook_id"):
                    made = True
                    wid = find_log_webhook(key, channel) or str(bot.create_webhook(channel, username).id)
                    cfg.update(mode="webhook", webhook_id=wid, retry_webhook_at=None)
                    save()
                    made = False
                bot.send_webhook(cfg["webhook_id"], "", username=username, embeds=[embed])
                return
            except yapper.YapperError as e:
                if made and e.status in _WEBHOOK_REFUSED:
                    print(f"[{label}] Yapin' won't give a webhook in {channel} ({e}); posting as the bot, trying webhooks again in an hour")
                    cfg.update(mode="bot", webhook_id=None, retry_webhook_at=time.time() + WEBHOOK_RETRY_SECONDS)
                    save()
                    break
                if not made and e.status in _WEBHOOK_GONE:
                    print(f"[{label}] its webhook is gone ({e}); finding or making another")
                    dead = str(cfg.get("webhook_id") or "")
                    for k, c in _log_configs().items():       # a shared webhook is gone for every log that used it
                        if k != key and dead and str(c.get("webhook_id") or "") == dead:
                            c.update(webhook_id=None)
                            cmdlogcfgDB.set(k, c)
                    cfg.update(webhook_id=None)
                    save()
                    continue
                print(f"[{label}] webhook post failed ({e}); posting this one as the bot")
                break
            except Exception as e:
                print(f"[{label}] webhook post failed ({e}); posting this one as the bot")
                break
    try:
        bot.send(channel, "", embeds=[embed])
    except Exception as e:
        print(f"[{label}] couldn't post: {e}")


def cmdlog_deliver(server_id, row):
    """Post one log line to the server's log channel (through the log's webhook, see post_log)."""
    cfg = get_cmdlog(server_id)
    if not cfg["enabled"] or not cfg["channel"]:
        return
    post_log(str(server_id), cfg, _cmdlog_embed(row), "Shapes Command Log", f"cmdlog {server_id}")


def _cmdlog_worker():
    while True:
        server_id, row = _cmdlog_queue.get()
        now = time.time()
        recent = [t for t in _cmdlog_sent.get(server_id, []) if now - t < 60]
        if len(recent) >= 20:                 # the dashboard still has every line; only the channel post is skipped
            _cmdlog_sent[server_id] = recent
            continue
        _cmdlog_sent[server_id] = recent + [now]
        try:
            cmdlog_deliver(server_id, row)
        except Exception as e:
            print(f"[cmdlog] delivery failed: {e}")
        time.sleep(0.3)


def cmdlog_record(command, msg):
    """Command hook: runs for every command. Cheap when the server has no log; otherwise stores the line and queues the post."""
    global _cmdlog_queue
    sid = server_of(msg.get("channel_id"), _first(msg, "server_id", "guild_id"))
    if not sid or command == "cmdlog":
        return
    cfg = cmdlogcfgDB.get(str(sid))
    if not cfg or not cfg.get("enabled"):
        return
    row = {"at": int(time.time()), "user_id": str(msg.get("author_id")), "username": str(msg.get("author_username") or msg.get("username") or msg.get("author_id")),
           "command": command, "args": str(msg.get("args") or "")[:200], "channel_id": str(msg.get("channel_id")), "server_id": str(sid)}
    cmdlogDB.set(f"{sid}:{int(time.time() * 1000)}-{os.urandom(2).hex()}", row)
    mine = sorted(k for k in cmdlogDB.items() if k.startswith(f"{sid}:"))
    for old in mine[:max(0, len(mine) - CMDLOG_KEEP)]:
        cmdlogDB.remove(old)
    if _cmdlog_queue is None:
        import queue
        _cmdlog_queue = queue.Queue()
        threading.Thread(target=_cmdlog_worker, daemon=True, name="cmdlog").start()
    _cmdlog_queue.put((str(sid), row))


COMMAND_HOOKS.append(cmdlog_record)


# --- Partners: servers the bot owner features on the website (/partners) ---
partnersDB = Table("partners")      # server id -> {name, description, invite, added_by, added_at, icon}
PARTNER_INVITE_HOSTS = ("yapin.gg", "www.yapin.gg")


def valid_invite_url(url):
    try:
        parsed = urllib.parse.urlparse(str(url or "").strip())
    except ValueError:
        return None
    return parsed.geturl() if parsed.scheme == "https" and parsed.hostname in PARTNER_INVITE_HOSTS and len(parsed.geturl()) <= 200 else None


def make_partner_invite(sid):
    """An invite for the server. Yapin's bot API has no create-invite endpoint (only GET /servers/{id}/invites, which needs
    Manage Server), so this reuses one the server already has, a vanity link first, and never one that expires. Returns a URL,
    or None when there isn't one (then the owner passes one with /partner invite)."""
    try:
        found = [dict(i) for i in bot.invites(sid)]
    except Exception as e:
        print(f"[partners] couldn't read the invites of {sid}: {e}")
        return None
    found.sort(key=lambda i: (not i.get("vanity"), bool(i.get("expires_at")), int(i.get("max_uses") or 0) != 0))
    for i in found:
        url = valid_invite_url(i.get("url") or (f"https://www.yapin.gg/invite/{i['code']}" if i.get("code") else None))
        if url and not i.get("expires_at"):
            return url
    return None


def _fresh_partner_invite(sid, row):
    """The invite to show. One the bot grabbed itself is looked up again (at most every 2 minutes) so it is never stale;
    one the owner typed in is left alone. Falls back to the saved one when Yapin' can't be asked."""
    if not row.get("invite_auto"):
        return row.get("invite")
    try:
        url = cached(f"partner-invite:{sid}", 120, lambda: make_partner_invite(sid))
    except Exception:
        url = None
    if url and url != row.get("invite"):
        partnersDB.set(sid, {**row, "invite": url})
    return url or row.get("invite")


def partner_cards():
    """What the partners page shows: for each partner the live server info where the bot can see it, else what was saved."""
    cards = []
    for sid in partnersDB.all():
        row = partnersDB.get(sid) or {}
        try:
            info = cached(f"summary:{sid}", 300, lambda sid=sid: server_summary(sid))
        except Exception:
            info = None
        description = (row.get("description") or (info or {}).get("description") or "").strip()
        cards.append({"id": sid, "name": (info or {}).get("name") or row.get("name") or sid, "icon": (info or {}).get("icon") or row.get("icon"),
                      "description": description[:400], "invite": valid_invite_url(_fresh_partner_invite(sid, row)), "added_at": row.get("added_at") or 0,
                      "members": (info or {}).get("members"), "humans": (info or {}).get("humans"), "channels": (info or {}).get("channels"), "roles": (info or {}).get("roles")})
    return sorted(cards, key=lambda c: c["added_at"])


# --- Bot log (bot owners only): one channel that gets every command from every server, and when Shapes is added or removed ---
# It posts through a webhook (bot.create_webhook / bot.send_webhook) and falls back to the bot posting in the channel.

BOTLOG_KEY = "_bot"
BOTLOG_DEFAULT = {"enabled": False, "channel": None, "mode": None, "webhook_id": None, "commands": True, "events": True}
_botlog_queue = None
_botlog_times = []


def get_botlog():
    return {**BOTLOG_DEFAULT, **(cmdlogcfgDB.get(BOTLOG_KEY) or {})}


def resolve_any_channel(token):
    """A text channel anywhere the bot is, from <#id>, an id or a name (the first match wins for names)."""
    token = (token or "").strip()
    found = re.match(r"^<#([^>\\s]+)>$", token)
    wanted = (found.group(1) if found else token).lstrip("#")
    for c in bot.channels():
        if str(c.get("id")) == wanted or (not found and str(c.get("name", "")).lower() == wanted.lower()):
            return c
    return None


def botlog_configure(channel_id=None, enabled=None, commands=None, events=None):
    cfg = get_botlog()
    if channel_id is not None and str(channel_id) != str(cfg["channel"] or ""):
        release_log_webhook(BOTLOG_KEY, cfg)
        cfg.update(channel=str(channel_id), mode=None, webhook_id=None, retry_webhook_at=None)
    if enabled is not None:
        cfg["enabled"] = bool(enabled) and bool(cfg["channel"])
    if commands is not None:
        cfg["commands"] = bool(commands)
    if events is not None:
        cfg["events"] = bool(events)
    cmdlogcfgDB.set(BOTLOG_KEY, cfg)
    return cfg


def _botlog_deliver(embed):
    cfg = get_botlog()
    if not cfg["enabled"] or not cfg["channel"]:
        return
    post_log(BOTLOG_KEY, cfg, embed, "Shapes Log", "botlog")


def _botlog_worker():
    while True:
        embed = _botlog_queue.get()
        now = time.time()
        _botlog_times[:] = [t for t in _botlog_times if now - t < 60]
        if len(_botlog_times) >= 30:           # a flood is dropped from the channel (the dashboard still has the commands)
            continue
        _botlog_times.append(now)
        try:
            _botlog_deliver(embed)
        except Exception as e:
            print(f"[botlog] delivery failed: {e}")
        time.sleep(0.3)


def botlog_post(embed):
    """Queue one embed (a dict) for the bot log. Does nothing when the log is off."""
    global _botlog_queue
    cfg = cmdlogcfgDB.get(BOTLOG_KEY)
    if not cfg or not cfg.get("enabled"):
        return
    if _botlog_queue is None:
        import queue
        _botlog_queue = queue.Queue()
        threading.Thread(target=_botlog_worker, daemon=True, name="botlog").start()
    _botlog_queue.put(embed)


GLOBAL_CMDLOG_KEEP = 500


def global_cmdlog_rows(limit=200):
    return cmdlog_rows("_global", limit)


def global_cmdlog_record(command, msg):
    """Command hook: every command in every server, for the bot owners. Off until an owner turns it on."""
    cfg = cmdlogcfgDB.get(BOTLOG_KEY)
    if not cfg or not cfg.get("enabled") or not cfg.get("commands") or command == "botlog":
        return
    sid = server_of(msg.get("channel_id"), _first(msg, "server_id", "guild_id"))
    try:
        name = (dict(cached(f"server:{sid}", 30, lambda: bot.server(sid))).get("name") if sid else None) or ("a DM" if not sid else str(sid))
    except Exception:
        name = str(sid or "a DM")
    row = {"at": int(time.time()), "user_id": str(msg.get("author_id")), "username": str(msg.get("author_username") or msg.get("username") or msg.get("author_id")),
           "command": command, "args": str(msg.get("args") or "")[:200], "channel_id": str(msg.get("channel_id")), "server_id": str(sid or ""), "server": name}
    cmdlogDB.set(f"_global:{int(time.time() * 1000)}-{os.urandom(2).hex()}", row)
    mine = sorted(k for k in cmdlogDB.items() if k.startswith("_global:"))
    for old in mine[:max(0, len(mine) - GLOBAL_CMDLOG_KEEP)]:
        cmdlogDB.remove(old)
    embed = _cmdlog_embed(row)
    embed["fields"].insert(2, {"name": "Server", "value": f"{name}" + (f" (`{sid}`)" if sid else ""), "inline": True})
    embed["footer"] = {"text": "Shapes global command log"}
    botlog_post(embed)


COMMAND_HOOKS.append(global_cmdlog_record)


# Servers Shapes is in: noticed by comparing the list now with the one saved last time, so adds and removals are seen even
# when Yapin' sends no event for them, and nothing is announced twice after a restart.
_known_servers_lock = threading.Lock()


REMOVAL_CONFIRM_POLLS = 5          # a server has to be missing from this many checks in a row before it counts as removed
REMOVAL_MAX_DROP = 0.5             # ...and the list can't have shrunk by more than half at once (that's the API glitching, not people leaving)
DATA_HOLD_DAYS = 30
holdsDB = Table("holds")            # server id -> {name, since, delete_after}: removed servers whose data is on hold
welcomedDB = Table("welcomed")      # server id -> when Shapes first welcomed it
_missing_polls = {}


def server_watch_poll():
    """Compare bot.servers() with the saved list. Returns (added, removed) as lists of {id, name, ...}.
    Removals are only believed after REMOVAL_CONFIRM_POLLS misses in a row and never when the list looks broken, because a
    removal starts the clock on deleting that server's data (see start_data_hold)."""
    with _known_servers_lock:
        try:
            now = {str(s.get("id")): dict(s) for s in bot.servers()}
        except Exception as e:
            print(f"[servers] couldn't list servers: {e}")
            return [], []
        saved = cmdlogcfgDB.get("_servers")
        before = (saved or {}).get("servers") or {}
        list_ok = bool(now) and len(now) >= len(before) * REMOVAL_MAX_DROP
        removed = []
        keep = {k: {"name": v.get("name")} for k, v in now.items()}
        for k, v in before.items():
            if k in now:
                _missing_polls.pop(k, None)
                continue
            if not list_ok:                      # the list looks wrong: remember everyone, count nothing
                keep[k] = v
                continue
            _missing_polls[k] = _missing_polls.get(k, 0) + 1
            if _missing_polls[k] >= REMOVAL_CONFIRM_POLLS:
                removed.append({"id": k, **v})
                _missing_polls.pop(k, None)
            else:
                keep[k] = v                      # still counted as ours until the misses add up
        cmdlogcfgDB.set("_servers", {"servers": keep, "at": int(time.time())})
        if saved is None:                      # first run: remember them, announce nothing
            return [], []
        added = [now[k] for k in now if k not in before]
        back = [now[k] for k in now if k in before and holdsDB.exists(k)]      # seen again while on hold
    for s in added:
        _server_event("add", s["id"], s.get("name"), s, len(now))
    for s in removed:
        _server_event("remove", s["id"], s.get("name"), s, len(now))
    for s in back:
        cancel_data_hold(s["id"], s.get("name"), "the server showed up again")
    try:
        sweep_data_holds(now if list_ok else None)
    except Exception as e:
        print(f"[servers] hold sweep failed: {e}")
    return added, removed


# --- what happens to a server's data when Shapes leaves: a 30 day hold, undone if it comes back ---

def start_data_hold(sid, name):
    sid = str(sid)
    now = int(time.time())
    holdsDB.set(sid, {"name": name or sid, "since": now, "delete_after": now + DATA_HOLD_DAYS * 86400})
    print(f"[servers] holding {name or sid}'s data for {DATA_HOLD_DAYS} days")


def cancel_data_hold(sid, name=None, why="it was added back"):
    sid = str(sid)
    if not holdsDB.exists(sid):
        return False
    holdsDB.remove(sid)
    print(f"[servers] released the data hold on {name or sid}: {why}")
    try:
        embed = {"title": f"♻️ {name or sid}'s data hold was cancelled", "description": f"Reason: {why}. Their settings and counting are untouched.", "color": 0x5865F2,
                 "footer": {"text": "Shapes server log"}}
        cfg = cmdlogcfgDB.get(BOTLOG_KEY)
        if cfg and cfg.get("events"):
            botlog_post(embed)
    except Exception:
        pass
    return True


def purge_server_data(sid):
    """Delete everything stored for one server (settings, counting, logs). Per-person data is never touched here."""
    sid = str(sid)
    for db in (countingDB, autoroleDB, automodDB, cmdlogcfgDB, holdsDB, welcomedDB, welcomeCfgDB):
        db.remove(sid)
    for key in [k for k in cmdlogDB.items() if k.startswith(f"{sid}:")]:
        cmdlogDB.remove(key)
    for key in [k for k in warnsDB.items() if k.startswith(f"{sid}:")]:
        warnsDB.remove(key)
    for key, row in list(actionsDB.items().items()):
        if isinstance(row, dict) and str(row.get("server_id")) == sid:
            actionsDB.remove(key)
    for key, row in list(auditDB.items().items()):
        if isinstance(row, dict) and str(row.get("key")) == sid:
            auditDB.remove(key)
    print(f"[servers] deleted the stored data of server {sid}")


def sweep_data_holds(now_servers):
    """Delete data whose 30 days are up, but only after checking again that the server really is gone. `now_servers` is the
    fresh server list, or None when that list couldn't be trusted this time (then nothing is deleted and the hold just waits)."""
    if now_servers is None:
        return []
    done = []
    for sid in list(holdsDB.all()):
        hold = holdsDB.get(sid) or {}
        if sid in now_servers:
            cancel_data_hold(sid, hold.get("name"), "the server is in the list again")
        elif int(hold.get("delete_after") or 0) and time.time() >= int(hold["delete_after"]):
            purge_server_data(sid)
            log_action("server_data_deleted", f"{hold.get('name') or sid}: data deleted after the {DATA_HOLD_DAYS} day hold", server_id=sid)
            done.append(sid)
    return done


WELCOME_TEXT = ("Thanks for adding Shapes! Here's how to get going:\n"
                "• **/help** lists everything I can do\n"
                "• **/counting** sets up a counting channel\n"
                "• **/cmdlog** (server owner) logs the commands people run\n"
                "• Manage me from the **dashboard**: autorole, automod, messages and more\n"
                "Pick a color for my embeds with **/settings**.")


def welcome_server(sid, name, data=None):
    """Say hello in the first channel Shapes may post in, and tell the owner by DM. Only once per server."""
    sid = str(sid)
    if welcomedDB.exists(sid):
        return False
    data = data or {}
    embed = {"title": f"👋 Hi {name or 'there'}, I'm Shapes!", "description": WELCOME_TEXT, "color": 0x6366F1,
             "footer": {"text": f"Dashboard: {PUBLIC_URL or MainURL}/dashboard"}}
    posted = False
    try:
        channels = [dict(c) for c in bot.channels(sid)]
    except Exception as e:
        print(f"[servers] couldn't list channels of {name or sid} to welcome them: {e}")
        channels = []
    preferred = str(_first(data, "system_channel_id", "default_channel_id", "general_channel_id") or "")
    text = [c for c in channels if _is_text(str(_first(c, "type", "kind") or "text").lower())]
    text.sort(key=lambda c: (str(c.get("id")) != preferred, "general" not in str(c.get("name", "")).lower(), int(c.get("position") or 0)))
    for channel in text[:5]:                       # the first one it lets us post in
        try:
            bot.send(str(channel["id"]), "", embeds=[embed])
            posted = True
            break
        except Exception as e:
            print(f"[servers] couldn't welcome #{channel.get('name')}: {e}")
    owner = _first(data, "owner_id", "ownerId")
    owner = str(owner.get("id") if isinstance(owner, dict) else owner) if owner is not None else None
    if owner:
        try:
            bot.send(bot.dm(owner), "", embeds=[{**embed, "title": f"Thanks for adding Shapes to {name or 'your server'}!",
                                                   "description": WELCOME_TEXT + ("" if posted else "\n\nI couldn't post in any channel of your server, so I'm telling you here.")}])
            posted = True
        except Exception as e:
            print(f"[servers] couldn't DM the owner of {name or sid}: {e}")
    if posted:
        welcomedDB.set(sid, {"at": int(time.time()), "name": name})
    return posted


def _server_event(kind, sid, name, data, total):
    name = name or str(sid)
    fields = [{"name": "Server", "value": f"{name}\\n`{sid}`", "inline": True}]
    try:
        members, people, bots_n = server_counts(sid, _first(data, "member_count", "members_count", "memberCount"))
    except Exception:
        members = _first(data, "member_count", "members_count", "memberCount")
        people = bots_n = None
    if members is not None:
        text = f"**{members:,}** members" if isinstance(members, int) else str(members)
        if people is not None:
            text += f"\n{people:,} people · {bots_n:,} bots"
        fields.append({"name": "Members", "value": text, "inline": True})
    owner = data.get("owner_id")
    if owner:
        fields.append({"name": "Owner", "value": f"<@{owner}>", "inline": True})
    fields.append({"name": "Servers now", "value": str(total), "inline": True})
    embed = {"title": ("➕ Shapes was added to " if kind == "add" else "➖ Shapes was removed from ") + name,
             "color": 0x57F287 if kind == "add" else 0xED4245, "fields": fields, "footer": {"text": "Shapes server log"},
             "timestamp": datetime.now(timezone.utc).isoformat()}
    actionsDB.set(f"{int(time.time() * 1000)}-{os.urandom(2).hex()}", {
        "at": int(time.time()), "actor_id": "system", "actor": "system", "kind": f"server_{kind}", "detail": f"{name} ({members or '?'} members)",
        "server_id": str(sid), "target": None})
    print(f"[servers] Shapes was {'added to' if kind == 'add' else 'removed from'} {name} ({sid}); now in {total}")
    if kind == "remove":
        start_data_hold(sid, name)
        embed["fields"].append({"name": "Their data", "value": f"Kept for {DATA_HOLD_DAYS} days, then deleted. Adding Shapes back cancels that.", "inline": False})
    else:
        if holdsDB.exists(str(sid)):
            cancel_data_hold(sid, name, "Shapes was added back")
        try:
            welcome_server(sid, name, data)
        except Exception as e:
            print(f"[servers] welcome failed for {name}: {e}")
    cfg = cmdlogcfgDB.get(BOTLOG_KEY)
    if cfg and cfg.get("events"):
        botlog_post(embed)


def _server_watch_loop():
    while True:
        time.sleep(60)
        try:
            server_watch_poll()
        except Exception as e:
            print(f"[servers] watcher error: {e}")


_server_watch_started = False


def start_server_watch():
    global _server_watch_started
    if _server_watch_started:
        return
    _server_watch_started = True
    server_watch_poll()
    threading.Thread(target=_server_watch_loop, daemon=True, name="server-watch").start()


# If Yapin' ever sends an event for it, react at once instead of waiting for the next check.
for _ev in ("server_join", "server_add", "bot_added", "bot_join", "server_leave", "server_remove", "bot_removed", "bot_leave"):
    bot.on(_ev, lambda _e: threading.Thread(target=server_watch_poll, daemon=True).start())


# --- Autorole: a role for new members, with people who are left out ---

AUTOROLE_DEFAULT = {"role_id": "", "enabled": False, "exempt_users": [], "include_bots": True}


def get_autorole(server_id):
    cfg = {**AUTOROLE_DEFAULT, **(autoroleDB.get(str(server_id)) or {})}
    cfg["exempt_users"] = [str(u) for u in cfg.get("exempt_users") or []]
    return cfg


def save_autorole(server_id, cfg):
    cfg = {**AUTOROLE_DEFAULT, **cfg}
    out = {"role_id": str(cfg["role_id"] or ""), "enabled": bool(cfg["enabled"]), "include_bots": bool(cfg["include_bots"]),
           "exempt_users": sorted({str(u).strip() for u in cfg["exempt_users"] if str(u).strip()})[:200]}
    autoroleDB.set(str(server_id), out)
    return out


def _user_ids(text):
    """User ids out of <@id> mentions, bare ids, or a comma/space separated mix."""
    ids = re.findall(r"<@!?([^>\s]+)>", text or "")
    rest = re.sub(r"<@!?[^>\s]+>", " ", text or "")
    ids += [t.strip("@,") for t in re.split(r"[\s,]+", rest) if t.strip("@,")]
    return list(dict.fromkeys(ids))


def autorole_applies(cfg, server_id, user_id):
    """Should this person get the role? Not when they are exempt, or a bot while bots are switched off."""
    if not cfg["enabled"] or not cfg["role_id"] or str(user_id) in cfg["exempt_users"]:
        return False
    if not cfg["include_bots"]:
        return not YUser(user_id, data=fetch_user(user_id) or {}).is_bot
    return True


# --- Member welcome: a message (and/or a DM) when someone joins ---

WELCOME_DEFAULT = {"enabled": False, "channel": "", "message": "Welcome to **{server}**, {user}! You are member #{count}.", "embed": True,
                   "title": "Welcome!", "dm": False, "dm_message": "Welcome to {server}, {name}! Have a great time.", "include_bots": False}


def get_welcome(server_id):
    cfg = {**WELCOME_DEFAULT, **{k: v for k, v in (welcomeCfgDB.get(str(server_id)) or {}).items() if v is not None}}
    cfg["channel"] = str(cfg.get("channel") or "")
    return cfg


def save_welcome(server_id, cfg):
    cfg = {**WELCOME_DEFAULT, **cfg}
    out = {"enabled": bool(cfg["enabled"]), "channel": str(cfg["channel"] or ""), "message": str(cfg["message"] or "")[:1500],
           "embed": bool(cfg["embed"]), "title": str(cfg["title"] or "")[:100], "dm": bool(cfg["dm"]),
           "dm_message": str(cfg["dm_message"] or "")[:1500], "include_bots": bool(cfg["include_bots"])}
    welcomeCfgDB.set(str(server_id), out)
    return out


def _vwelcome(v):
    """Checks a welcome settings dict from the dashboard; returns the cleaned dict or raises ValueError."""
    if not isinstance(v, dict):
        raise ValueError("Welcome needs an object of settings.")
    out = {"enabled": bool(v.get("enabled")), "channel": str(v.get("channel") or "").strip(), "message": str(v.get("message") or "").strip(),
           "embed": bool(v.get("embed", True)), "title": str(v.get("title") or "").strip(), "dm": bool(v.get("dm")),
           "dm_message": str(v.get("dm_message") or "").strip(), "include_bots": bool(v.get("include_bots"))}
    if len(out["message"]) > 1500 or len(out["dm_message"]) > 1500 or len(out["title"]) > 100:
        raise ValueError("The message is too long (1500 characters at most, 100 for the title).")
    if out["enabled"] and not out["channel"] and not out["dm"]:
        raise ValueError("Pick a welcome channel (or turn on the DM) before switching the welcome on.")
    if out["enabled"] and out["channel"] and not out["message"]:
        raise ValueError("Write the welcome message first.")
    return out


def render_welcome(text, uid, name, server_name, count):
    """{user} mentions them, {name} is their name, {server} the server's name, {count} the member number."""
    return (str(text).replace("{user}", f"<@{uid}>").replace("{name}", str(name)).replace("{server}", str(server_name))
            .replace("{count}", str(count or "?")).replace("{id}", str(uid)))


def send_welcome(sid, uid, force=False):
    """Welcome one member in the server's welcome channel and/or by DM. Returns what was sent ("channel", "dm", both, or "").
    force=True (the test button) ignores the on/off switch and the bot skip."""
    cfg = get_welcome(sid)
    if not (cfg["enabled"] or force):
        return ""
    user = YUser(uid, data=fetch_user(uid) or {})
    if user.is_bot and not (cfg["include_bots"] or force):
        return ""
    try:
        info = dict(cached(f"server:{sid}", 30, lambda: bot.server(sid)))
        server_name = info.get("name") or "the server"
        count = _total_of(info)
    except Exception:
        server_name, count = "the server", None
    sent = []
    if cfg["channel"] and cfg["message"]:
        text = render_welcome(cfg["message"], uid, user.display_name, server_name, count)
        try:
            if cfg["embed"]:
                emb = {"description": text, "color": 0x6366F1}
                if cfg["title"]:
                    emb["title"] = render_welcome(cfg["title"], uid, user.display_name, server_name, count)[:256]
                bot.send(cfg["channel"], f"<@{uid}>", embeds=[emb])
            else:
                bot.send(cfg["channel"], text)
            sent.append("channel")
        except Exception as e:
            print(f"[welcome] couldn't post in {cfg['channel']}: {type(e).__name__}: {e}")
    if cfg["dm"] and cfg["dm_message"]:
        try:
            bot.send(bot.dm(uid), render_welcome(cfg["dm_message"], uid, user.display_name, server_name, count))
            sent.append("dm")
        except Exception as e:
            print(f"[welcome] couldn't DM {uid}: {type(e).__name__}: {e}")
    return "+".join(sent)


# ====================================================================================================
# Fun and utility commands: /roll /coinflip /8ball /choose /calc /cat /dog /fact
# ====================================================================================================
# --- fun/utility command helpers (no Discord/Yapin' types, so both bots share the same text) ---


# --- Automod ---

_spam = automod.SpamTracker()
_strikes = automod.Strikes()
_admin_cache = {}       # (server, user) -> (expires_at, bool); is_admin costs several API calls


def get_automod(server_id):
    return automod.merged(automodDB.get(str(server_id)))


def save_automod(server_id, cfg):
    cfg = automod.clean_config(cfg)
    automodDB.set(str(server_id), cfg)
    return cfg


def _exempt(sid, uid, channel_id, cfg):
    if str(channel_id) in cfg["exempt_channels"]:
        return True
    key = (sid, str(uid))
    hit = _admin_cache.get(key)
    if not hit or hit[0] < time.time():
        hit = (time.time() + 120, bool(is_admin(sid, uid)))
        _admin_cache[key] = hit
    if hit[1]:
        return True
    if cfg["exempt_roles"]:
        try:
            roles = {str(r.get("id") if isinstance(r, dict) else r) for r in (bot.member(sid, uid).get("roles") or [])}
            return bool(roles & set(cfg["exempt_roles"]))
        except Exception:
            return False
    return False


def handle_automod(msg):
    """Returns True when the message was removed (so other handlers, like counting, skip it)."""
    if msg.get("author_bot") or msg.get("command"):
        return False
    sid = server_of(msg.get("channel_id"), _first(msg, "server_id", "guild_id"))
    if not sid:
        return False
    stored = automodDB.get(sid)
    if not stored:
        return False
    cfg = automod.merged(stored)
    if not cfg["enabled"]:
        return False
    uid, channel, mid = str(msg.get("author_id")), msg.get("channel_id"), msg.get("id")
    hits = automod.violations(cfg, msg.get("content") or "", (sid, uid), _spam)
    if not hits or _exempt(sid, uid, channel, cfg):
        return False

    actions = cfg["actions"]
    removed = False
    if actions["delete"]:
        try:
            bot.delete(channel, mid)
            removed = True
        except Exception as e:
            print(f"[automod] could not delete {mid}: {e}")
    strikes = _strikes.add((sid, uid), actions["strike_minutes"] * 60)
    punishment = automod.escalation(cfg, strikes)
    why = ", ".join(automod.REASONS[h] for h in hits)
    outcome = "message removed" if removed else "flagged"
    if punishment == "kick":
        try:
            bot.kick(sid, uid)
            outcome, _ = "kicked", _strikes.clear((sid, uid))
        except Exception as e:
            print(f"[automod] kick failed: {e}")
    elif punishment == "timeout":
        try:
            bot.timeout(sid, uid, actions["timeout_minutes"], reason=f"Automod: {why}")
            outcome = f"timed out for {actions['timeout_minutes']} min"
            _strikes.clear((sid, uid))
        except Exception as e:
            print(f"[automod] timeout failed: {e}")
    if actions["warn"] and punishment is None:
        try:
            bot.send(channel, f"⚠️ <@{uid}>, {why}. ({strikes}/{actions['timeout_after'] or '∞'} strikes)")
        except Exception as e:
            print(f"[automod] warning failed: {e}")
    if cfg["log_channel"]:
        try:
            bot.send(cfg["log_channel"], f"🛡️ **Automod** · <@{uid}> in <#{channel}> {why}. Result: {outcome}.")
        except Exception as e:
            print(f"[automod] log failed: {e}")
    return removed or punishment is not None


def server_summary(sid):
    """What /serverinfo shows, as plain data (the dashboard shows the same numbers)."""
    info = dict(bot.server(sid))
    channels = bot.channels(sid)
    roles = bot.roles(sid)
    members = cached(f"allmembers:{sid}", 120, lambda: _all_members(sid))
    total = int(_first(info, "member_count", "members_count", "memberCount") or len(members))
    texts = sum(1 for c in channels if str(_first(c, "type", "kind") or "text") == "text")
    categories = {str(_first(c.get("category") or {}, "id") or c.get("category_id")) for c in channels if c.get("category") or c.get("category_id")}
    owner = _first(info, "owner_id", "ownerId")
    owner = str(owner.get("id") if isinstance(owner, dict) else owner) if owner is not None else None
    counts = server_counts(sid, total)
    return {"id": sid, "name": info.get("name") or "Server", "description": str(info.get("description") or "")[:400], "owner": owner,
            "members": counts[0], "humans": counts[1], "bots": counts[2],
            "channels": len(channels), "text": texts, "voice": len(channels) - texts, "categories": len(categories), "roles": len(roles),
            "extra": [(label, str(info[key])) for key, label in (("verified", "Verified"), ("public", "Public"), ("boost_level", "Boost level")) if info.get(key) not in (None, "", False)],
            "icon": _icon_of(info), "banner": _asset_url(_first(info, "banner_path", "banner_url", "banner"), "banners")}


# --- Last.fm: the Shapes API does all of it (linking, the account, the profile lookups); the bot just asks it ---

PUBLIC_URL = (os.environ.get("PUBLIC_URL") or "").rstrip("/")


def _read_git(here):
    """{sha, branch, repo, title} of the checkout the code runs from, read straight out of the .git folder. Hosts often have no git program,
    so nothing here runs one. The title comes from the commit object when it's stored loose, else from GitHub (public repos) in the background."""
    import zlib
    gd = os.path.join(here, ".git")
    out = {"sha": "", "branch": "", "repo": "", "title": ""}
    try:
        head = open(os.path.join(gd, "HEAD")).read().strip()
        if head.startswith("ref:"):
            ref = head[4:].strip()
            out["branch"] = ref.split("/", 2)[-1]
            path = os.path.join(gd, *ref.split("/"))
            if os.path.exists(path):
                out["sha"] = open(path).read().strip()
            elif os.path.exists(os.path.join(gd, "packed-refs")):
                for line in open(os.path.join(gd, "packed-refs")):
                    if line.strip().endswith(" " + ref):
                        out["sha"] = line.split()[0]
        else:
            out["sha"] = head
        try:                                                           # which branch was pulled last tells more than the local name does
            fetched = open(os.path.join(gd, "FETCH_HEAD")).read()
            found = re.search(r"branch '([^']+)' of (\S+)", fetched)
            if found:
                out["branch"], out["repo"] = found.group(1), found.group(2)
        except OSError:
            pass
        if not out["repo"]:
            found = re.search(r"url\s*=\s*(\S+)", open(os.path.join(gd, "config")).read())
            out["repo"] = found.group(1) if found else ""
        out["repo"] = re.sub(r"^git@github\.com:", "https://github.com/", re.sub(r"\.git$", "", out["repo"])).rstrip("/")
        out["repo"] = re.sub(r"^https://[^@/]+@", "https://", out["repo"])      # never keep a token that was part of the remote
        if out["sha"]:
            try:
                raw = zlib.decompress(open(os.path.join(gd, "objects", out["sha"][:2], out["sha"][2:]), "rb").read())
                out["title"] = raw.split(b"\n\n", 1)[1].split(b"\n", 1)[0].decode("utf-8", "replace")[:120]
            except Exception:
                pass
    except Exception:
        pass
    if not out["sha"]:
        out["sha"] = os.environ.get("COMMIT_SHA", "").strip()
    out["short"] = out["sha"][:7]
    return out


GIT = _read_git(os.path.dirname(os.path.abspath(__file__)))


def commit_info():
    """The running commit for the dashboards: {sha, title, url}. The link only exists for GitHub repos; the page only links it when public."""
    sha = GIT["sha"]
    url = f"{GIT['repo']}/commit/{sha}" if sha and GIT["repo"].startswith("https://github.com/") else ""
    return {"sha": GIT["short"], "title": GIT["title"], "url": url, "branch": GIT["branch"]}


def _fill_commit_title():
    """No loose commit object (a shallow clone keeps it in a pack): ask GitHub once, in the background. Private repos just say no."""
    if not GIT["sha"] or GIT["title"] or not GIT["repo"].startswith("https://github.com/"):
        return
    try:
        r = requests.get(f"https://api.github.com/repos/{GIT['repo'][len('https://github.com/'):]}/commits/{GIT['sha']}", timeout=8)
        if r.ok:
            GIT["title"] = str((r.json().get("commit") or {}).get("message") or "").split("\n", 1)[0][:120]
    except Exception:
        pass


threading.Thread(target=_fill_commit_title, daemon=True).start()


COMMIT = (GIT["short"], GIT["title"])      # kept for older callers


def api_commit_info():
    """The Shapes API's own commit ({sha, title}), asked of it and kept for 5 minutes. Its repo is private, so there is no link."""
    def ask():
        data = shapes_api("GET", "/internal/version", timeout=4) or {}
        return {"sha": data.get("sha") or "", "title": data.get("title") or ""}
    return cached("api_commit", 300, ask)


def get_lastfm(user_id):
    """The Last.fm username this Yapin user linked (kept by the API), or None."""
    data = shapes_api("GET", f"/internal/links/{user_id}")
    return ((data or {}).get("lastfm") or {}).get("name")


def lastfm_profile(name):
    """Profile summary (url, scrobbles, now playing / last track, picture) from the API, or None. The API caches it."""
    data = shapes_api("GET", f"/internal/lastfm/{urllib.parse.quote(str(name), safe='')}", timeout=10)
    return (data or {}).get("profile") if data and data.get("success") else None


def lastfm_line(p):
    scrobbles = f"{int(p['scrobbles']):,}" if str(p.get("scrobbles") or "").isdigit() else "?"
    text = f"[{p['name']}]({p['url']}) · {scrobbles} scrobbles"
    if p.get("track"):
        text += f"\n{'🎶 Now playing' if p['now'] else '⏮️ Last played'}: {p['track']}"
    return text


# --- Yapper profile commands (/yapinuser, /yapinavatar, /yapinbanner; these were the Discord user/avatar/banner commands) ---


# =====================================================================================
# Commands carried over from the Discord bot
# =====================================================================================


# --- search ---
# Google is retiring the Custom Search JSON API (discontinued January 1, 2027) and has no whole-web replacement.
# /search now uses the Brave Search API (BRAVE_API_KEY). If only the old GOOGLE_API_KEY is set, it keeps using
# Google's API until that shuts off. Adding another provider = one more function in SEARCH_PROVIDERS.
BraveAPIKey = _secret("BRAVE_API_KEY", "BraveToken.txt")


async def get_badge_thumbnail(session: aiohttp.ClientSession, badge_id: str) -> Optional[str]:
    thumbnail_url = f"https://thumbnails.roblox.com/v1/badges/icons?badgeIds={badge_id}&size=150x150&format=Png"

    try:
        async with session.get(thumbnail_url) as response:
            if response.status == 200:
                thumbnail_data = await response.json()
                if thumbnail_data.get('data') and len(thumbnail_data['data']) > 0:
                    return thumbnail_data['data'][0].get('imageUrl')
    except Exception:
        pass
    return None


roblox_colors = {
    1: "F2F3F3", 2: "A1A5A2", 3: "F9E999", 5: "D7C59A", 6: "C2DAB8", 9: "E8BAC8",
    11: "80BBDB", 12: "CB8442", 18: "CC8E69", 21: "C4281C", 22: "C470A0", 23: "0D69AC",
    24: "F5CD30", 25: "624732", 26: "1B2A35", 27: "6D6E6C", 28: "287F47", 29: "A1C48C",
    36: "F3CF9B", 37: "4B974B", 38: "A05F35", 39: "C1CADC", 40: "ECECEC", 41: "CD544B",
    42: "C1DFF0", 43: "7BB6E8", 44: "F7F18D", 45: "B4D2E4", 47: "D9856C", 48: "84B68D",
    49: "F8F184", 50: "ECE8DE", 100: "EEC4B6", 101: "DA867A", 102: "6E99CA", 103: "C7C1B7",
    104: "6B327C", 105: "E29B40", 106: "DA8541", 107: "008F9C", 108: "685C43", 110: "435493",
    111: "BFB7B1", 112: "6874AC", 113: "E5ADCC", 115: "C7D23C", 116: "55A5AF", 118: "B7D7D5",
    119: "A4BD47", 120: "D9E4A7", 121: "E7AC58", 123: "D36F4C", 124: "923978", 125: "EAB892",
    126: "A5A5CB", 127: "DCBC81", 128: "AE7A59", 131: "9CA3A8", 133: "D5733D", 134: "D8DD56",
    135: "74869D", 136: "877C90", 137: "E09864", 138: "958A73", 140: "203A56", 141: "27462D",
    143: "CFE2F7", 145: "7988A1", 146: "958EA3", 147: "938767", 148: "575857", 149: "161D32",
    150: "ABADA9", 151: "789082", 153: "957977", 154: "7B2E2F", 157: "FFF67B", 158: "E1A4C2",
    168: "756C62", 176: "97695B", 178: "B48455", 179: "898788", 180: "D7A94B", 190: "F9D62E",
    191: "E8AB2D", 192: "694028", 193: "CF6024", 194: "A3A2A5", 195: "4667A4", 196: "23478B",
    198: "8E4285", 199: "635F62", 200: "828A5D", 208: "E5E4E3", 209: "B08E44", 210: "709578",
    211: "79B5B5", 212: "9FC3E9", 213: "6C81B7", 216: "904C2A", 217: "7C5C46", 218: "96709F",
    219: "6B629B", 220: "A7A9CE", 221: "CD6298", 222: "E4ADC8", 223: "DC9095", 224: "F0D5A0",
    225: "EBB87F", 226: "FDEAA1", 232: "7DBBDD", 268: "342B75", 301: "506D54", 302: "5B5D69",
    303: "0010B0", 304: "2C651D", 305: "527CAE", 306: "335882", 307: "102ADC", 308: "3D1585",
    309: "348E40", 310: "5B9A4C", 311: "9FA1AC", 312: "592259", 313: "1F801D", 314: "9FADC0",
    315: "0989CF", 316: "7B007B", 317: "7C9C6B", 318: "8AAB85", 319: "B9C4B1", 320: "CACBD1",
    321: "A75E9B", 322: "7B2F7B", 323: "94BE81", 324: "A8BD99", 325: "DFDFDE", 327: "970000",
    328: "B1E5A6", 329: "98C2DB", 330: "FF98DC", 331: "FF5959", 332: "750000", 333: "EFB838",
    334: "F8D96D", 335: "E7E7EC", 336: "C7D4E4", 337: "FF9494", 338: "BE6862", 339: "562424",
    340: "F1E7C7", 341: "FEF3BB", 342: "E0B2D0", 343: "D490BD", 344: "965555", 345: "8F4C2A",
    346: "D3BE96", 347: "E2DCC6", 348: "EDEDEA", 349: "E9DADA", 350: "883E3E", 351: "BC9B5D",
    352: "C7AC78", 353: "CAC0A3", 354: "BBB3B2", 355: "6C584B", 356: "A0844F", 357: "958988",
    358: "ABA89E", 359: "AF9483", 360: "966766", 361: "564236", 362: "7E683F", 363: "69665C",
    364: "5A4C42", 365: "6A3909", 1001: "F8F8F8", 1002: "CDCDCD", 1003: "111111", 1004: "FF0000",
    1005: "FFB000", 1006: "B480FF", 1007: "A34B4B", 1008: "C1BE42", 1009: "FFFF00", 1010: "0000FF",
    1011: "002060", 1012: "2154B9", 1013: "04AFEC", 1014: "AA5500", 1015: "AA00AA", 1016: "FF66CC",
    1017: "FFAF00", 1018: "12EED4", 1019: "00FFFF", 1020: "00FF00", 1021: "3A7D15", 1022: "7F8E64",
    1023: "8C5B9F", 1024: "AFDDFF", 1025: "FFC9C9", 1026: "B1A7FF", 1027: "9FF3E9", 1028: "CCFFCC",
    1029: "FFFFCC", 1030: "FFCC99", 1031: "6225D1", 1032: "FF00BF"
}


            


# =====================================================================================
# Events
# =====================================================================================

COUNT_RE = re.compile(r"^\d+(?:[+\-*/x]\d+)*$")


def parse_count(text):
    """'5', '2+3', '2x3' -> a number (evaluated left to right, like the Discord bot). None if it is not a count."""
    text = text.replace(" ", "")
    if not COUNT_RE.match(text):
        return None
    parts = re.findall(r"\d+|[+\-*/x]", text)
    total = int(parts[0])
    for op, val in zip(parts[1::2], parts[2::2]):
        val = int(val)
        if op == "+":
            total += val
        elif op == "-":
            total -= val
        elif op in ("*", "x"):
            total *= val
        elif op == "/":
            if val == 0:
                return None
            total = total / val
            if total == int(total):
                total = int(total)
    return total


def handle_counting(msg):
    if msg.get("author_bot") or msg.get("command"):
        return
    sid = server_of(msg.get("channel_id"), _first(msg, "server_id", "guild_id"))
    if not sid:
        return
    data = countingDB.get(sid)
    if not data or not data.get("enabled") or str(data.get("channel")) != str(msg.get("channel_id")):
        return
    value = parse_count(msg.get("content") or "")
    if value is None:
        return

    channel, mid, author = msg.get("channel_id"), msg.get("id"), str(msg.get("author_id"))
    mention = f"<@{author}>"
    number = data["number"]
    next_number = number + 1
    last = str(data["lastcounter"]) if data.get("lastcounter") is not None else None
    warnings = data.get("warnings", 0)
    highest = data.get("highestnumber", 0)

    def save():
        countingDB.set(sid, data)

    emo = counting_emojis(data)

    def react(kind):
        """Add the server's emoji; if Yapin won't take a custom one, use the default so the count still gets a reaction."""
        try:
            bot.react(channel, mid, emo[kind])
        except Exception as e:
            if emo[kind] != COUNTING_EMOJI_DEFAULT[kind]:
                print(f"[counting] emoji {emo[kind]!r} rejected ({e}); using the default")
                try:
                    bot.react(channel, mid, COUNTING_EMOJI_DEFAULT[kind])
                except Exception:
                    pass

    if data.get("lastmessage") and time.time() - int(data.get("lastmessage_at") or 0) > COUNT_WATCH_WINDOW \
            and count_message_gone(channel, data["lastmessage"]):
        announce_deleted_count(sid, data)       # nobody was watching it any more; catch it now that the count goes on

    if str(value) == str(next_number) and author != last:
        data.update(number=next_number, lastcounter=author, highestnumber=max(highest, next_number), lastmessage=str(mid), lastmessage_at=int(time.time()))
        save()
        react("correct")
        return

    limit = counting_limit(data)
    if author == last and warnings < limit:
        react("warning")
        bot.send(channel, f"{emo['warning']} You can't count by yourself!", reply_to=mid)
        data["warnings"] = warnings + 1
        save()
        return
    if warnings < limit:
        react("warning")
        bot.send(channel, f"{emo['warning']} The next number is {next_number}", reply_to=mid)
        data["warnings"] = warnings + 1
        save()
        return

    react("wrong")
    data["highestnumber"] = max(highest, number)
    name = fetch_user(author).get("username")
    who = f"{mention} (@{name})" if name else mention
    if number == 0:
        bot.send(channel, f"{emo['wrong']} {who} ruined it at 1, the next number is 1 (again)")
    else:
        bot.send(channel, f"{emo['wrong']} {who} ruined it at {number}, the next number is 1")
    data.update(number=0, warnings=0, lastcounter=None, lastmessage=None, lastmessage_at=None)
    save()


@bot.on("message")
def on_message(msg):
    try:
        if handle_automod(msg):
            return
    except Exception as e:
        print(f"[automod] {type(e).__name__}: {e}")
    try:
        track_dm(msg)
    except Exception as e:
        print(f"[dm] {type(e).__name__}: {e}")
    try:
        handle_counting(msg)
    except Exception as e:
        print(f"[counting] {type(e).__name__}: {e}")

@bot.on_dm()
def on_direct_message(msg):
    try:
        track_dm(msg)
    except Exception as e:
        print(f"[dm] {type(e).__name__}: {e}")
    msg.reply("You Can Sadly Only Use This Bot In Servers!")

@bot.on("member_join")
def on_member_join(ev):
    try:
        cfg = get_autorole(ev.server_id)
        if autorole_applies(cfg, ev.server_id, ev.user_id):
            bot.add_role(ev.server_id, ev.user_id, cfg["role_id"])
    except Exception as e:
        print(f"[autorole] {type(e).__name__}: {e}")
    try:
        send_welcome(ev.server_id, ev.user_id)
    except Exception as e:
        print(f"[welcome] {type(e).__name__}: {e}")


@bot.on("test")
def on_test(ev):
    """The "Send test event" button in Yapin's developer settings."""
    note = _first(dict(ev), "message", "note")
    print(f"[test] ✅ Test event received{f': {note}' if note else ''}. The bot is connected and getting events. "
          f"(event id {_first(dict(ev), 'id') or '?'}, bot ready: {bot_ready})")


@bot.on("ready")
def on_ready(me):
    global bot_ready
    bot_ready = True
    try:
        bot.set_status("online", "🔗 shapes-yapper.oriaxen.com")
    except Exception as e:
        print(f"could not set status: {e}")
    start_server_watch()
    start_count_watch()
    print(f"[SYSTEM] Logged in as {me.username}, watching {len(bot.servers())} servers. Storage: {__import__('tables').backend_summary()}")


# =====================================================================================
# Web server (port 5004)
# =====================================================================================

app = Flask(__name__)
CORS(app)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or os.urandom(32).hex()
app.config.update(SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_HTTPONLY=True, PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 14)
if not os.environ.get("FLASK_SECRET_KEY"):
    print("FLASK_SECRET_KEY is not set: admin logins will reset whenever the bot restarts.")

_cache = {}


def cached(key, ttl, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    value = fn()
    _cache[key] = (time.time(), value)
    return value


# Two separate lists of Yapin user ids. Owners control everything (every command, DM everyone, the global command log).
# Admins only get the bot-admin parts of the dashboard (every server, hide-sender, DM a list). Owners count as admins too,
# and an id on both lists is only ever counted once.
def _id_set(name):
    return {x.strip() for x in (os.environ.get(name) or "").split(",") if x.strip()}


OWNER_IDS = _id_set("OWNER_IDS")
ADMIN_USER_IDS = _id_set("ADMIN_USER_IDS") - OWNER_IDS


def is_bot_owner(user_id):
    return str(user_id) in OWNER_IDS


def is_staff_user(user_id):
    """Bot admins and bot owners."""
    return str(user_id) in OWNER_IDS or str(user_id) in ADMIN_USER_IDS


def _is_admin_session():
    """The admin key was entered, or this is a signed-in Yapin user whose id is on the staff list."""
    user = session.get("yuser")
    return session.get("admin") is True or (isinstance(user, dict) and is_staff_user(user.get("id")))


_hits = {}
_hits_lock = threading.Lock()


def client_ip():
    return (request.headers.get("X-Forwarded-For") or request.remote_addr or "?").split(",")[0].strip()


def rate_limited(limit=60, window=60):
    """Allow `limit` requests per `window` seconds from one address, then answer 429 with Retry-After. The Shapes API on
    this machine (and anything holding the internal token) is exempt, so the API's own cached lookups never trip it."""
    def deco(f):
        def wrapped(*args, **kwargs):
            local = request.remote_addr in ("127.0.0.1", "::1") and not request.headers.get("X-Forwarded-For")
            if not local:
                now, key = time.time(), (f.__name__, client_ip())
                with _hits_lock:
                    recent = [t for t in _hits.get(key, []) if now - t < window]
                    if len(recent) >= limit:
                        _hits[key] = recent
                        retry = max(1, int(window - (now - recent[0])))
                        resp = jsonify({"error": "Too many requests. Slow down.", "retry_after": retry})
                        resp.status_code = 429
                        resp.headers["Retry-After"] = str(retry)
                        return resp
                    recent.append(now)
                    _hits[key] = recent
                    if len(_hits) > 5000:                 # forget addresses that went quiet
                        for k in [k for k, v in _hits.items() if not v or now - v[-1] > window]:
                            _hits.pop(k, None)
            return f(*args, **kwargs)
        wrapped.__name__ = f.__name__
        return wrapped
    return deco


def _is_shapes_api_call():
    """The Shapes API (same machine) presenting the internal token. It is how the Shapes API reads /mutuals for its keyed
    route; nothing that arrives through a proxy counts."""
    if not API_INTERNAL_TOKEN:
        return False
    header = request.headers.get("Authorization", "")
    return header.startswith("Bearer ") and hmac.compare_digest(header[7:].strip(), API_INTERNAL_TOKEN)


def admin_required(f):
    def wrapped(*args, **kwargs):
        if request.path == "/mutuals" and _is_shapes_api_call():
            return f(*args, **kwargs)
        if not _is_admin_session():
            if request.path.startswith("/api/") or request.path == "/mutuals":
                return jsonify({"error": "login required"}), 401
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    wrapped.__name__ = f.__name__
    return wrapped


def _name(d):
    return _first(d, "username", "display_name", "name") or str(_first(d, "id", "user_id") or "?")


WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


def _web_file(name):
    with open(os.path.join(WEB_DIR, name), "r", encoding="utf-8") as f:
        return f.read()


# --- activities: the activity/ folder is public under /activity ---

ACTIVITY_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "activity")
# No Yapper activity SDK exists yet, so /activity sends people to the dashboard. Set ACTIVITIES_ENABLED=1 to serve the folder.
ACTIVITIES_ENABLED = os.environ.get("ACTIVITIES_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")


def _activity_list():
    out = []
    if os.path.isdir(ACTIVITY_DIR):
        for name in sorted(os.listdir(ACTIVITY_DIR)):
            folder = os.path.join(ACTIVITY_DIR, name)
            if name.startswith((".", "_")) or not os.path.isfile(os.path.join(folder, "index.html")):
                continue
            meta = {}
            try:
                with open(os.path.join(folder, "activity.json"), "r", encoding="utf-8") as f:
                    meta = json.load(f)
            except (OSError, ValueError):
                pass
            out.append({"slug": name, "name": str(meta.get("name") or name), "description": str(meta.get("description") or "")})
    return out


@app.route("/activity/")
@app.route("/activity")
def activity_index():
    if not ACTIVITIES_ENABLED:
        return redirect("/dashboard", 302)
    if os.path.isfile(os.path.join(ACTIVITY_DIR, "index.html")):
        from flask import send_from_directory
        return send_from_directory(ACTIVITY_DIR, "index.html")
    rows = "".join(f'<li><a href="/activity/{urllib.parse.quote(a["slug"])}/"><b>{_html_escape(a["name"])}</b></a>'
                   f'{"<br><span>" + _html_escape(a["description"]) + "</span>" if a["description"] else ""}</li>' for a in _activity_list())
    return Response("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>Activities</title>"
                    "<style>:root{color-scheme:dark}body{background:#050505;color:#fff;font:16px/1.5 system-ui,sans-serif;max-width:40rem;margin:3rem auto;padding:0 1rem}"
                    "li{margin:.8rem 0}span{opacity:.7}a{text-decoration:none}</style><h1>Activities</h1>"
                    f"<ul>{rows or '<li>No activities yet.</li>'}</ul>", mimetype="text/html")


@app.route("/activity/<path:subpath>")
def activity_files(subpath):
    if not ACTIVITIES_ENABLED:
        return redirect("/dashboard", 302)
    from flask import abort, send_from_directory
    parts = [p for p in subpath.split("/") if p]
    if not parts or any(p.startswith((".", "_")) for p in parts):
        abort(404)
    target = os.path.join(ACTIVITY_DIR, *parts)
    if os.path.isdir(target):
        subpath = "/".join(parts + ["index.html"])
    return send_from_directory(ACTIVITY_DIR, subpath)      # send_from_directory refuses anything outside the folder


def _html_escape(text):
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


@app.route("/status")
def web_status():
    return "OK", 200


_login_attempts = {}


def _too_many_logins(ip):
    now = time.time()
    recent = [t for t in _login_attempts.get(ip, []) if now - t < 300]
    _login_attempts[ip] = recent
    return len(recent) >= 8


@app.route("/login", methods=["GET", "POST"])
def admin_login():
    if _is_admin_session():
        return redirect(url_for("dashboard"))
    error = None
    if request.method == "POST":
        ip = request.headers.get("X-Forwarded-For", request.remote_addr or "?").split(",")[0].strip()
        if _too_many_logins(ip):
            error = "Too many attempts. Wait a few minutes and try again."
        elif not ADMIN_KEY:
            error = "No admin key is configured (set ADMIN_KEY)."
        elif hmac.compare_digest(request.form.get("pass", ""), ADMIN_KEY):
            session["admin"] = True
            return redirect(url_for("dashboard"))
        else:
            _login_attempts.setdefault(ip, []).append(time.time())
            error = "That key isn't right."
    return render_template_string(_web_file("login.html"), error=error)


@app.route("/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))


def _server_rows():
    servers = list(cached("servers", 15, bot.servers))
    counts = server_counts_many(servers)
    return [{"id": s.get("id"), "name": s.get("name") or str(s.get("id")), "members": (counts.get(str(s.get("id"))) or ("",))[0] or ""} for s in servers]


@app.route("/admin", methods=["GET"])
@admin_required
def dashboard():
    if not bot_ready:
        return Response("<!doctype html><meta charset=utf-8><meta http-equiv=refresh content=3><body style='font-family:system-ui;padding:3rem'>The bot is still starting. This page refreshes on its own.", mimetype="text/html")
    # Sign out only belongs to the password login: a signed-in staff member leaves through the user dashboard, and inside an iframe
    # (the user dashboard's Admin tab) there is nothing to sign out of. The page also removes it when it finds itself framed.
    out = '<a href="/logout" id="signout">Sign out</a>' if session.get("admin") is True else ""
    return Response(_web_file("app.html").replace("<!--SIGNOUT-->", out), mimetype="text/html")


def _avatar_of(d):
    """Profile picture link: Yapin sends a path (avatar_path), the bot's own /me sends a full avatar_url."""
    return (_image_url(_first(d, "avatar_url", "avatarUrl", "image_url", "author_avatar_url"))
            or _asset_url(_first(d, "avatar_path", "author_avatar_path", "avatar"), "avatars"))


def _icon_of(d):
    return (_image_url(_first(d, "icon_url", "iconUrl", "image_url"))
            or _asset_url(_first(d, "icon_path", "icon"), "icons"))


_AVATAR_COLORS = ("#6366f1", "#8b5cf6", "#ec4899", "#f59e0b", "#10b981", "#06b6d4", "#ef4444", "#3b82f6")


def default_avatar_url(uid, name=None):
    """A picture for people with none: their initial on a color picked from their id. Served by /avatar/<id>.svg."""
    q = urllib.parse.urlencode({"n": str(name or "")[:40]}) if name else ""
    return f"{PUBLIC_URL}/avatar/{urllib.parse.quote(str(uid), safe='')}.svg" + (f"?{q}" if q else "")


@app.route("/avatar/<uid>.svg")
def default_avatar(uid):
    name = (request.args.get("n") or uid).strip() or "?"
    color = _AVATAR_COLORS[sum(ord(c) for c in str(uid)) % len(_AVATAR_COLORS)]
    letter = html.escape(name[:1].upper() or "?")
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" fill="{color}"/>'
           f'<text x="32" y="32" dy=".35em" text-anchor="middle" font-family="Arial,sans-serif" font-size="30" font-weight="700" fill="#fff">{letter}</text></svg>')
    return Response(svg, mimetype="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})


def _person(d):
    """A flat, UI-friendly version of a member/user dict (some APIs nest the user under "user")."""
    inner = d.get("user") if isinstance(d.get("user"), dict) else {}
    merged = {**inner, **{k: v for k, v in d.items() if k != "user"}}
    uid = _first(merged, "user_id", "id")
    return {
        "id": str(uid), "name": _first(merged, "username", "name") or str(uid),
        "display": _first(merged, "nickname", "display_name", "displayName", "nick") or _first(merged, "username", "name") or str(uid),
        "avatar": _avatar_of(merged) or default_avatar_url(uid, _first(merged, "nickname", "display_name", "username", "name")),
        "bot": YUser(uid, data=merged).is_bot or str(uid) == str((bot.me or {}).get("id")),
        "roles": [str(r.get("id") if isinstance(r, dict) else r) for r in (merged.get("roles") or merged.get("role_ids") or [])],
        "joined": _first(merged, "joined_at", "joinedAt", "created_at"),
    }


def _group_channels(channels):
    """Categories with their channels. Each Yapin channel carries its category ({id, name, position}) and category_id,
    channels without one are listed first with no heading."""
    cats, loose = {}, []
    for c in channels:
        c = dict(c)
        row = {"id": str(c.get("id")), "name": c.get("name") or str(c.get("id")),
               "type": str(_first(c, "type", "kind") or "text").lower(), "topic": c.get("topic") or "",
               "private": bool(c.get("private")), "position": c.get("position") or 0}
        cat = c.get("category") if isinstance(c.get("category"), dict) else None
        cid = _first(cat or {}, "id") or c.get("category_id")
        if cid is None:
            loose.append(row)
            continue
        entry = cats.setdefault(str(cid), {"id": str(cid), "name": (cat or {}).get("name") or "Category",
                                           "position": (cat or {}).get("position") or 0, "channels": []})
        entry["channels"].append(row)
    out = sorted(cats.values(), key=lambda e: e["position"])
    for e in out:
        e["channels"].sort(key=lambda r: r["position"])
    loose.sort(key=lambda r: r["position"])
    if loose:
        out.insert(0, {"id": "", "name": "", "channels": loose})
    return out


def _is_text(ch_type):
    return ch_type in ("text", "0", "announcement", "news", "")


def _api_error(e):
    return jsonify({"error": str(e)}), (e.status if getattr(e, "status", None) in (400, 401, 403, 404, 429) else 502)


@app.route("/api/overview")
@admin_required
def api_overview():
    """Everything the API gives us about the bot, in one place."""
    if not bot_ready:
        return jsonify({"ready": False}), 503
    servers = []
    raw = list(cached("servers", 15, bot.servers))
    counts = server_counts_many(raw)
    for s in raw:
        sid = str(s.get("id"))
        members, people, bots = counts.get(sid) or (None, None, None)
        servers.append({**dict(s), "id": sid, "name": s.get("name") or sid, "member_count": members, "people": people,
                        "bots": bots, "icon": _icon_of(s)})
    return jsonify({
        "ready": True, "me": {**dict(bot.me or {}), "avatar": _avatar_of(dict(bot.me or {}))}, "uptime_seconds": int(time.time() - started_at),
        "storage": __import__("tables").backend_summary(), "commands": sorted(bot.commands), "servers": servers,
        "is_owner": actor_is_owner(), "colors": color_choices(), "commit": commit_info(), "api_commit": api_commit_info(),
    })


@app.route("/api/servers/<server_id>")
@admin_required
def api_server(server_id):
    try:
        channels = cached(f"channels:{server_id}", 20, lambda: bot.channels(server_id))
        roles = cached(f"roles:{server_id}", 30, lambda: bot.roles(server_id))
        info = cached(f"server:{server_id}", 30, lambda: bot.server(server_id))
        return jsonify({
            "server": dict(info), "categories": _group_channels(channels),
            "roles": sorted(({"id": str(r.get("id")), "name": r.get("name") or "role", "color": _first(r, "color", "colour"),
                              "position": r.get("position") or 0, "admin": _has_admin(r)} for r in roles),
                            key=lambda r: -int(r["position"] or 0)),
        })
    except yapper.YapperError as e:
        return _api_error(e)


@app.route("/api/servers/<server_id>/members")
@admin_required
def api_members(server_id):
    after = request.args.get("after") or None
    try:
        rows = bot.members(server_id, after=after)
        people = [_person(dict(m)) for m in rows]
        return jsonify({"members": people, "next": people[-1]["id"] if len(rows) >= 200 and people else None})
    except yapper.YapperError as e:
        return _api_error(e)


def _message_row(m):
    m = dict(m)
    author = dict(m.get("author")) if isinstance(m.get("author"), dict) else {}
    if not author and m.get("author_id"):
        author = dict(fetch_user(m.get("author_id")) or {})      # messages only carry author_id; profiles are cached
    person = _person({**author, "id": author.get("id") or m.get("author_id")}) if (author or m.get("author_id")) else {"id": "", "name": "?", "display": "?", "avatar": None, "bot": False}
    name = person["display"] if author else (_first(m, "author_display_name", "author_username", "author_name") or person["display"])
    images = []
    for e in (m.get("embeds") or []):
        if isinstance(e, dict):
            for key in ("image", "thumbnail"):
                u = e.get(key)
                u = _image_url(u.get("url") if isinstance(u, dict) else u)
                if u:
                    images.append(u)
    for att in (m.get("attachments") or m.get("files") or []):
        if isinstance(att, dict):
            u = _image_url(_first(att, "url", "proxy_url", "src"))
            ctype = str(att.get("content_type") or att.get("type") or "")
            if u and (ctype.startswith("image") or re.search(r"\.(png|jpe?g|gif|webp|avif)(\?|$)", u, re.I)):
                images.append(u)
    pic = _asset_url(m.get("attachment_path"), "attachments")
    if pic and re.search(r"\.(png|jpe?g|gif|webp|avif)(\?|$)", pic, re.I):
        images.append(pic)
    elif pic:
        m["content"] = (m.get("content") or "") + ("\n" if m.get("content") else "") + pic      # shows as a link
    return {"id": str(m.get("id")), "author": name, "author_id": person["id"], "avatar": person["avatar"] or _image_url(m.get("author_avatar_url")),
            "bot": bool(m.get("author_bot")) or person["bot"], "content": m.get("content") or "", "created_at": m.get("created_at"),
            "embeds": [{"title": e.get("title"), "description": e.get("description"), "url": e.get("url")}
                       for e in (m.get("embeds") or []) if isinstance(e, dict)], "images": images[:6]}


@app.route("/api/channels/<channel_id>/messages")
@admin_required
def api_messages(channel_id):
    limit = max(1, min(100, request.args.get("limit", 50, type=int)))
    try:
        return jsonify({"messages": [_message_row(m) for m in bot.messages(channel_id, limit=limit)]})
    except yapper.YapperError as e:
        return _api_error(e)


def actor_is_owner():
    """The dashboard user is a bot owner (the admin key counts: whoever holds it runs the bot)."""
    if not has_request_context():
        return False
    user = session.get("yuser")
    if isinstance(user, dict) and user.get("id"):
        return is_bot_owner(user["id"])
    return session.get("admin") is True


def current_actor():
    """(id, name, is_bot_staff) of whoever is using the dashboard right now. The admin key has no identity of its own."""
    if not has_request_context():            # the bot itself (a background job), not a person on the dashboard
        return "system", "system", True
    user = session.get("yuser")
    if isinstance(user, dict) and user.get("id"):
        return str(user["id"]), str(user.get("name") or user["id"]), is_staff_user(user["id"])
    if session.get("admin") is True:
        return "admin-key", "admin key", True
    return "unknown", "unknown", False


_ACTION_KEEP = 2000
_last_trim = 0.0


def log_action(kind, detail, server_id=None, target=None):
    """Write down who did what and when (shown in the dashboards, so people can refer back to it)."""
    uid, name, _ = current_actor()
    stamp = time.time()
    actionsDB.set(f"{int(stamp * 1000)}-{os.urandom(2).hex()}", {
        "at": int(stamp), "actor_id": uid, "actor": name, "kind": kind, "detail": str(detail)[:500],
        "server_id": str(server_id) if server_id else None, "target": str(target)[:200] if target else None})
    print(f"[action] @{name} {kind}: {str(detail)[:120]}" + (f" (server {server_id})" if server_id else ""))
    global _last_trim
    if time.time() - _last_trim < 3600:
        return
    _last_trim = time.time()
    rows = sorted(actionsDB.items().items())
    for key, _row in rows[:max(0, len(rows) - _ACTION_KEEP)]:         # keep the newest 2000
        actionsDB.remove(key)


def action_rows(server_id=None, limit=100):
    rows = [v for v in actionsDB.items().values() if isinstance(v, dict) and (server_id is None or v.get("server_id") == str(server_id))]
    return sorted(rows, key=lambda r: -int(r.get("at") or 0))[:limit]


def _stamp_text(name, where=None):
    return f"Sent by @{name}" + (f" from {where}" if where else "") + " · " + time.strftime("%b %d, %Y %H:%M UTC", time.gmtime())


def resolve_color(choice=None, custom=None, user_id=None):
    """The embed color (an int) for a message sent as Shapes: choice is "mine" (their saved /settings color, else blue),
    a name from the color list, "random", or "custom" with custom = "#rrggbb". Raises ValueError for anything else."""
    choice = str(choice or "mine").strip()
    if choice.lower() == "mine":
        saved = embedDB.get(f"{user_id}") if user_id else None
        return int(saved) if isinstance(saved, int) else discord.Color.blue().value
    if choice.lower() == "random":
        return discord.Color.random().value
    if choice.lower() == "custom":
        found = re.fullmatch(r"#?([0-9a-fA-F]{6})", str(custom or "").strip())
        if not found:
            raise ValueError("A custom color looks like #5865f2.")
        return int(found.group(1), 16)
    for name, color in DiscordColors:
        if name.lower() == choice.lower():
            return color.value
    raise ValueError("Pick a color from the list, or use a custom one.")


def color_choices():
    return [{"name": n, "hex": "#%06x" % c.value} for n, c in DiscordColors]


def _announcement_embed(title, text, image_url, footer=None, color=None):
    embed = {"description": text}
    if color is not None:
        embed["color"] = int(color)
    if title:
        embed["title"] = title
    if image_url:
        embed["image"] = {"url": image_url}
    if footer:
        embed["footer"] = {"text": footer}
    return embed


def _post_with_optional_image(channel_id, text, image_url, title=None, footer=None, color=None):
    """Plain text when there is no image, title, color or sender line; otherwise an embed. If the server refuses embeds, falls
    back to text with the image link and the sender line."""
    if not image_url and not title and not footer and color is None:
        return bot.send(channel_id, text)
    try:
        return bot.send(channel_id, "", embeds=[_announcement_embed(title, text, image_url, footer, color)])
    except yapper.YapperError as e:
        if e.status not in (400, 422):
            raise
        parts = [p for p in (f"**{title}**" if title else "", text, image_url, f"*{footer}*" if footer else "") if p]
        return bot.send(channel_id, "\n\n".join(parts))


def send_as_bot(channel_id, text, image=None, title=None, where=None, hidden=False, color=None):
    """Post as the bot and, unless a bot admin chose to hide it, say who it is from (and when). Returns the footer used."""
    uid, name, staff = current_actor()
    if hidden and not staff:
        raise PermissionError("Only bot admins can hide who a message is from.")
    footer = None if hidden else _stamp_text(name, where)
    _post_with_optional_image(channel_id, text, image, title, footer, color)
    return footer


def _valid_image(url):
    url = (url or "").strip()
    if not url:
        return None
    clean = _image_url(url)
    if not clean:
        raise ValueError("The image link must be a public https address.")
    return clean


@app.route("/api/channels/<channel_id>/send", methods=["POST"])
@admin_required
def api_send(channel_id):
    body = request.get_json(silent=True) or {}
    text = str(body.get("message", "")).strip()
    try:
        image = _valid_image(body.get("image_url"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if not text and not image:
        return jsonify({"error": "Write a message or add an image link."}), 400
    hidden = body.get("hidden") is True
    try:
        # A DM channel is only ever a bot admin's doing here, so it follows the same hidden option.
        send_as_bot(channel_id, text, image, hidden=hidden)
        log_action("send", ("(hidden sender) " if hidden else "") + text[:200], target=f"channel {channel_id}")
        return jsonify({"ok": True})
    except PermissionError as e:
        return jsonify({"error": str(e)}), 403
    except yapper.YapperError as e:
        return _api_error(e)


# --- direct messages ---
# Every DM the bot sends or receives is written to dmlogDB: who, which way, what was said and what caused it (a command, the dashboard,
# verification, a welcome message...). Sending goes through bot.send / bot.respond, so both are wrapped once here; receiving is
# caught where the bot is DMed (on_dm) and where a command is run in a DM.
_dm_channels = {}              # DM channel id -> the other person's user id (filled as DMs are opened, received or answered)
_dm_ctx = threading.local()    # the command being run on this thread, so its replies are labelled with it
_DM_KEEP = 5000
_last_dm_trim = 0.0


def _dm_text(content=None, embeds=None, layout=None):
    parts = [str(content)] if content else []
    for e in (embeds or []):
        e = e if isinstance(e, dict) else getattr(e, "data", None) or {}
        for key in ("title", "description"):
            if e.get(key):
                parts.append(str(e[key]))
        for f in e.get("fields") or []:
            parts.append(f"{f.get('name', '')}: {f.get('value', '')}")
    if layout and not parts:
        parts.append("(a layout message)")
    return " | ".join(parts)[:1000]


def _dm_source():
    """What caused this message: the command being run, else the nearest function of ours on the stack."""
    cmd = getattr(_dm_ctx, "command", None)
    if cmd:
        return f"/{cmd}"
    frame = sys._getframe(2)
    here = os.path.dirname(os.path.abspath(__file__))
    while frame:
        code = frame.f_code
        if os.path.abspath(code.co_filename).startswith(here) and code.co_name not in ("<module>", "_dm_source", "log_dm", "send", "respond", "_send_logged", "_respond_logged"):
            return f"{os.path.basename(code.co_filename)}:{code.co_name}"
        frame = frame.f_back
    return "bot"


def log_dm(direction, user_id, text, source=None, name=None):
    """Write one DM down. direction is "out" (the bot sent it) or "in" (someone sent it to the bot). Never raises."""
    global _last_dm_trim
    try:
        stamp = time.time()
        uid = str(user_id or "")
        known = dmDB.get(uid) or {}
        dmlogDB.set(f"{int(stamp * 1000)}-{os.urandom(2).hex()}", {
            "at": int(stamp), "dir": direction, "user_id": uid, "name": name or known.get("name") or uid,
            "text": str(text or "")[:1000], "source": str(source or "")[:80]})
        print(f"[dm {'->' if direction == 'out' else '<-'}] {name or known.get('name') or uid}: {str(text or '')[:100]!r} ({source})")
        if stamp - _last_dm_trim > 3600:
            _last_dm_trim = stamp
            rows = sorted(dmlogDB.items().items())
            for key, _row in rows[:max(0, len(rows) - _DM_KEEP)]:
                dmlogDB.remove(key)
    except Exception as e:
        print(f"[dm log] {type(e).__name__}: {e}")


def _install_dm_logging():
    raw_dm, raw_send, raw_respond = bot.dm, bot.send, bot.respond

    def dm(user_id):
        channel = raw_dm(user_id)
        _dm_channels[str(channel)] = str(user_id)
        return channel

    def send(channel_id, content="", embeds=None, components=None, reply_to=None, files=None, **kw):
        out = raw_send(channel_id, content, embeds=embeds, components=components, reply_to=reply_to, files=files, **kw)
        who = _dm_channels.get(str(channel_id))
        if who:
            log_dm("out", who, _dm_text(content, embeds), _dm_source())
        return out

    def respond(interaction, kind, content=None, embeds=None, components=None, layout=None, modal=None):
        out = raw_respond(interaction, kind, content=content, embeds=embeds, components=components, layout=layout, modal=modal)
        try:
            if kind in ("reply", "ephemeral", "update") and not _first(interaction, "server_id", "guild_id"):
                who = str(interaction.get("user_id") or interaction.get("author_id") or "")
                if who:
                    _dm_channels[str(interaction.get("channel_id"))] = who
                    log_dm("out", who, _dm_text(content, embeds, layout), _dm_source(), name=interaction.get("username"))
        except Exception as e:
            print(f"[dm log] {type(e).__name__}: {e}")
        return out
    bot.dm, bot.send, bot.respond = dm, send, respond


_install_dm_logging()


def track_dm(msg):
    """Remember who has messaged the bot so their DM shows up in the dashboard."""
    if msg.get("author_bot"):
        return
    sid = server_of(msg.get("channel_id"), _first(msg, "server_id", "guild_id"))
    if sid:
        return
    uid = str(msg.get("author_id"))
    name = _first(msg, "author_display_name", "author_username", "author_name") or uid
    dmDB.set(uid, {"user_id": uid, "channel_id": str(msg.get("channel_id")), "name": name,
                   "last": int(time.time()), "preview": str(msg.get("content") or "")[:80]})
    _dm_channels[str(msg.get("channel_id"))] = uid
    log_dm("in", uid, msg.get("content") or "(attachment)", "DM to the bot", name=name)


@app.route("/api/dms/log")
@admin_required
def api_dm_log():
    """Everything the bot sent or received as a DM, newest first. ?user_id= limits it to one person."""
    who = request.args.get("user_id", "").strip()
    rows = [v for v in dmlogDB.items().values() if isinstance(v, dict) and (not who or v.get("user_id") == who)]
    rows.sort(key=lambda r: -int(r.get("at") or 0))
    return jsonify({"log": rows[:300], "total": len(rows)})


@app.route("/api/dms")
@admin_required
def api_dms():
    rows = [v for v in dmDB.items().values() if isinstance(v, dict)]
    return jsonify({"dms": sorted(rows, key=lambda r: -int(r.get("last") or 0))})


def _all_people():
    """Members of every server the bot is in (first page each), for finding someone to DM."""
    def load():
        seen = {}
        for s in bot.servers():
            try:
                for m in bot.members(s.get("id")):
                    p = _person(dict(m))
                    seen.setdefault(p["id"], p)
            except Exception:
                continue
        return list(seen.values())
    return cached("people", 120, load)


@app.route("/api/users/search")
@admin_required
def api_user_search():
    q = request.args.get("q", "").strip().lower().lstrip("@")
    if len(q) < 2:
        return jsonify({"users": []})
    hits = [p for p in _all_people() if q in p["name"].lower() or q in p["display"].lower() or q == p["id"]]
    return jsonify({"users": hits[:15]})


@app.route("/api/dms", methods=["POST"])
@admin_required
def api_dm_open():
    body = request.get_json(silent=True) or {}
    user_id = str(body.get("user_id", "")).strip().strip("<@!>")
    if not user_id:
        return jsonify({"error": "Pick a person first."}), 400
    try:
        channel_id = bot.dm(user_id)
        try:
            profile = _person(dict(bot.user(user_id)))
        except Exception:
            profile = {"display": str(body.get("name") or user_id)}
        row = dmDB.get(user_id) or {}
        row.update({"user_id": user_id, "channel_id": str(channel_id), "name": profile["display"], "last": row.get("last") or int(time.time())})
        dmDB.set(user_id, row)
        return jsonify({"dm": row})
    except yapper.YapperError as e:
        return _api_error(e)


def dm_people(user_ids, text, title=None, image=None, where=None, hidden=False, delay=0.5, actor=None, progress=None, color=None):
    """DM each person as the bot. Returns [{user_id, ok, error?}]; one failure (DMs closed) never stops the rest.
    `actor` is (id, name, staff) when this runs outside a request (a background job); progress(done, results) is told as it goes."""
    uid, name, staff = actor or current_actor()
    if hidden and not staff:
        raise PermissionError("Only bot admins can hide who a message is from.")
    footer = None if hidden else _stamp_text(name, where)
    results = []
    for target in dict.fromkeys(str(t) for t in user_ids):         # never the same person twice
        try:
            _post_with_optional_image(bot.dm(target), text, image, title, footer, color)
            results.append({"user_id": target, "ok": True})
        except Exception as e:
            results.append({"user_id": target, "ok": False, "error": str(e)[:120]})
        if progress:
            progress(len(results), results)
        time.sleep(delay)
    return results


_dm_jobs = {}


def start_dm_job(user_ids, text, title=None, image=None, where=None, hidden=False, color=None):
    """DM a (possibly huge) list in the background, so the request returns at once. Poll dm_job_status(id)."""
    ids = list(dict.fromkeys(str(i) for i in user_ids))
    actor = current_actor()
    job_id = os.urandom(6).hex()
    job = {"total": len(ids), "done": 0, "sent": 0, "failed": [], "finished": False, "started": time.time(), "owner": actor[0]}
    for old in [k for k, v in _dm_jobs.items() if time.time() - v["started"] > 6 * 3600]:
        _dm_jobs.pop(old, None)
    _dm_jobs[job_id] = job

    def update(done, results):
        job.update(done=done, sent=sum(r["ok"] for r in results), failed=[r for r in results if not r["ok"]][:50])

    def run():
        try:
            dm_people(ids, text, title, image, where, hidden, actor=actor, progress=update, color=color)
        except Exception as e:
            job["error"] = str(e)[:200]
        finally:
            job["finished"] = True
            print(f"[dm job {job_id}] {job['sent']}/{job['total']} sent" + (f", {len(job['failed'])} failed" if job["failed"] else ""))

    threading.Thread(target=run, daemon=True, name=f"dm-job-{job_id}").start()
    return job_id


def dm_job_status(job_id, owner_id):
    job = _dm_jobs.get(job_id)
    if not job or job["owner"] != owner_id:
        return None
    return {k: job[k] for k in ("total", "done", "sent", "failed", "finished")} | ({"error": job["error"]} if job.get("error") else {})


def all_member_ids(server_ids=None):
    """Every non-bot member id across the given servers (default: all of them), each only once."""
    seen = {}
    for srv in (server_ids if server_ids is not None else [str(s.get("id")) for s in bot.servers()]):
        try:
            for p in _all_members(srv, limit=10 ** 9):
                if not p["bot"]:
                    seen[p["id"]] = True
        except Exception as e:
            print(f"[dm] couldn't list members of {srv}: {e}")
    return list(seen)


@app.route("/api/dm/send", methods=["POST"])
@admin_required
def api_dm_send():
    """Bot staff: DM one person, a pasted list, or everyone you pick, e.g. for an announcement that matters more."""
    body = request.get_json(silent=True) or {}
    text, title = str(body.get("message", "")).strip(), str(body.get("title", "")).strip()
    ids = body.get("user_ids")
    ids = _user_ids(ids) if isinstance(ids, str) else [str(i).strip().strip("<@!>") for i in (ids or []) if str(i).strip()]
    ids = list(dict.fromkeys(ids))
    if not text:
        return jsonify({"error": "Write the message first."}), 400
    everyone = body.get("everyone") is True
    if everyone:
        if not actor_is_owner():
            return jsonify({"error": "Only bot owners can DM everyone."}), 403
        ids = all_member_ids()
    if not ids:
        return jsonify({"error": "Pick at least one person."}), 400
    if len(ids) > 200 and not actor_is_owner():
        return jsonify({"error": "That's more than 200 people at once; split it up."}), 400
    try:
        image = _valid_image(body.get("image_url"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    hidden = body.get("hidden") is True
    try:
        color = resolve_color(body.get("color"), body.get("custom_color"), current_actor()[0]) if (body.get("color") or body.get("hidden") is not True) else None
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    job = start_dm_job(ids, text, title or None, image, hidden=hidden, color=color)
    log_action("dm", ("(hidden sender) " if hidden else "") + text[:200], target=("everyone: " if everyone else "") + f"{len(ids)} people: " + ", ".join(ids[:8]) + ("…" if len(ids) > 8 else ""))
    return jsonify({"job": job, "total": len(ids)})


@app.route("/api/dm/job/<job>")
@admin_required
def api_dm_job(job):
    status = dm_job_status(job, current_actor()[0])
    return (jsonify(status), 200) if status else (jsonify({"error": "No such job."}), 404)


@app.route("/api/botlog", methods=["GET", "POST"])
@admin_required
def api_botlog():
    """Bot owners: the global command log and its settings (admins can't change or read it)."""
    if not actor_is_owner():
        return jsonify({"error": "Only bot owners can use the bot log."}), 403
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        channel = None
        if body.get("channel"):
            ch = resolve_any_channel(str(body["channel"]))
            if not ch:
                return jsonify({"error": "That channel isn't in a server the bot is in."}), 400
            channel = str(ch.get("id"))
        botlog_configure(channel_id=channel, enabled=body.get("enabled") if isinstance(body.get("enabled"), bool) else None,
                         commands=body.get("commands") if isinstance(body.get("commands"), bool) else None,
                         events=body.get("events") if isinstance(body.get("events"), bool) else None)
        log_action("settings", "bot log changed")
    return jsonify({"config": get_botlog(), "rows": global_cmdlog_rows(200)})


@app.route("/api/actions")
@admin_required
def api_actions():
    return jsonify({"actions": action_rows(request.args.get("server") or None, 200)})


# --- partners, status and people (bot owners) ---

def _owner_only():
    if not actor_is_owner():
        return jsonify({"error": "Only bot owners can do this."}), 403
    return None


def _partner_view(sid):
    row = partnersDB.get(sid) or {}
    try:
        info = cached(f"summary:{sid}", 300, lambda: server_summary(sid))
    except Exception:
        info = None
    return {"id": sid, "name": (info or {}).get("name") or row.get("name") or sid, "icon": (info or {}).get("icon") or row.get("icon"),
            "description": row.get("description") or "", "description_fallback": ((info or {}).get("description") or "")[:400],
            "invite": row.get("invite") or "", "invite_auto": bool(row.get("invite_auto")), "added_at": row.get("added_at") or 0,
            "in_server": info is not None, "members": (info or {}).get("members"), "humans": (info or {}).get("humans")}


@app.route("/api/partners", methods=["GET", "POST"])
@admin_required
def api_partners():
    """The partners shown on the website's /partners page. Bot owners only. POST adds one: {server, description?, invite?}."""
    denied = _owner_only()
    if denied:
        return denied
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        sid = str(body.get("server") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", sid):
            return jsonify({"error": "Give the server's ID."}), 400
        if partnersDB.exists(sid):
            return jsonify({"error": "That server is already a partner."}), 409
        try:
            info = server_summary(sid)
        except Exception:
            return jsonify({"error": "I can only partner servers I'm in. I couldn't load that one."}), 400
        row = {"name": info["name"], "icon": info["icon"], "added_by": current_actor()[0], "added_at": int(time.time())}
        typed = str(body.get("invite") or "").strip()
        if typed:
            url = valid_invite_url(typed)
            if not url:
                return jsonify({"error": "The invite must be a https://www.yapin.gg/... link."}), 400
            row.update(invite=url, invite_auto=False)
        else:
            url = make_partner_invite(sid)
            row.update(invite=url, invite_auto=bool(url))
        if str(body.get("description") or "").strip():
            row["description"] = str(body["description"]).strip()[:400]
        partnersDB.set(sid, row)
        _cache.pop(f"summary:{sid}", None)
        log_action("partner", f"added {info['name']}", server_id=sid)
    rows = sorted((_partner_view(s) for s in partnersDB.all()), key=lambda r: r["added_at"])
    return jsonify({"partners": rows, "site": f"{MainURL}/partners"})


@app.route("/api/partners/<sid>", methods=["POST", "DELETE"])
@admin_required
def api_partner(sid):
    """Edit ({description?, invite? (a link, or "new" to grab one)}) or remove a partner. Bot owners only."""
    denied = _owner_only()
    if denied:
        return denied
    row = partnersDB.get(sid)
    if row is None:
        return jsonify({"error": "That server isn't a partner."}), 404
    name = row.get("name") or sid
    if request.method == "DELETE":
        partnersDB.remove(sid)
        _cache.pop(f"summary:{sid}", None)
        log_action("partner", f"removed {name}", server_id=sid)
        return jsonify({"ok": True})
    body = request.get_json(silent=True) or {}
    row = dict(row)
    if "description" in body:
        row["description"] = str(body.get("description") or "").strip()[:400] or None
    if "invite" in body:
        typed = str(body.get("invite") or "").strip()
        if typed.lower() in ("", "new"):
            url = make_partner_invite(sid)
            if not url:
                return jsonify({"error": "I couldn't make or find an invite myself. Paste a https://www.yapin.gg/... link."}), 400
            row.update(invite=url, invite_auto=True)
        else:
            url = valid_invite_url(typed)
            if not url:
                return jsonify({"error": "The invite must be a https://www.yapin.gg/... link."}), 400
            row.update(invite=url, invite_auto=False)
    partnersDB.set(sid, row)
    _cache.pop(f"summary:{sid}", None)
    log_action("partner", f"edited {name}", server_id=sid)
    return jsonify({"ok": True, "partner": _partner_view(sid)})


# --- /ping data: how this bot, the API and the Discord bot are doing ------------------------------------------
import procstats
_latency = {"ms": None, "at": 0.0}


def measure_latency(max_age=0):
    """How long THIS server takes to reach the Shapes API, in ms: the fastest of a few requests on a warm connection, timed only until
    the API starts answering (None when it doesn't answer). Uses whichever address (IP or public https) works."""
    if _latency["ms"] is not None and time.time() - _latency["at"] < max_age:
        return _latency["ms"]
    ms = api_link.ping(_api_session)
    _latency.update(ms=ms, at=time.time())
    return ms


def cached_api_ping():
    """measure_latency() for the heartbeat: a recent value, refreshed here in the background (it is never measured on a command's time)."""
    if time.time() - _latency["at"] > 25:
        measure_latency()
    return _latency["ms"]


_platform = {"ms": None, "at": 0}


def platform_latency(max_age=0):
    """Round trip to Yapin' itself (the platform this bot runs on) on a warm connection, in ms (None when it doesn't answer)."""
    if _platform["ms"] is not None and time.time() - _platform["at"] < max_age:
        return _platform["ms"]
    try:
        bot.api("GET", "/me", retries=0)
        start = time.perf_counter()
        bot.api("GET", "/me", retries=0)
        _platform.update(ms=(time.perf_counter() - start) * 1000, at=time.time())
    except Exception:
        _platform.update(ms=None, at=time.time())
    return _platform["ms"]


def cached_platform_latency():
    """The last measured ping to Yapin' without waiting (for /internal/stats and the heartbeat)."""
    if time.time() - _platform["at"] > 30:
        _platform["at"] = time.time()                          # one refresh at a time
        threading.Thread(target=lambda: platform_latency(0), daemon=True).start()
    return _platform["ms"]


@app.route("/internal/stats")
def internal_stats():
    """For the Shapes API (shared token): this bot's uptime, memory, CPU and ping."""
    if not _is_shapes_api_call():
        return jsonify({"success": False, "data": "Not allowed"}), 403
    return jsonify({"success": True, "platform": "yapin", "latency_ms": cached_platform_latency(), "api_ms": _latency["ms"], **procstats.snapshot()})


def send_heartbeat():
    """Tell the API how this bot is doing (the API shows it in /ping on either bot)."""
    return shapes_api("POST", "/internal/heartbeat/yapin", {**procstats.snapshot(), "latency_ms": cached_platform_latency(), "api_ms": cached_api_ping()}, timeout=8)


def _heartbeat_loop():
    time.sleep(15)
    while True:
        try:
            send_heartbeat()
        except Exception as e:
            print(f"heartbeat failed: {e}")
        time.sleep(30)


threading.Thread(target=_heartbeat_loop, daemon=True, name="heartbeat").start()


def fleet_stats():
    """{api, yapin, discord, problems} from the Shapes API (a part that isn't answering is None and problems says why)."""
    send_heartbeat()                     # so this bot's own numbers are fresh in the API
    got = shapes_api("GET", "/internal/stats", timeout=20)
    if not got or not got.get("success"):
        why = "The Shapes API isn't answering (or hasn't been redeployed with /internal/stats)."
        return {"api": None, "yapin": None, "discord": None, "problems": {"api": why}}
    return got


@app.route("/api/status")
@admin_required
def api_status():
    """Is everything this bot depends on working? Anyone on the admin dashboard can see this."""
    started = time.time()
    down = bool(api_down())
    api_ms = None
    if api_ready() and not down:
        t0 = time.time()
        shapes_api("GET", "/internal/version", timeout=4)
        api_ms = int((time.time() - t0) * 1000)
        down = bool(_api_state["down"])
    try:
        count = len(bot.servers())
    except Exception:
        count = None
    return jsonify({
        "bot": {"uptime_seconds": int(time.time() - started_at), "servers": count, "commands": len(bot.tree.commands),
                "partners": len(partnersDB.all()), "commit": commit_info()},
        "bot_detection": {"known_accounts": len(_KNOWN_BOT), "bots_found": sum(1 for v in _KNOWN_BOT.values() if v), **_member_sample} if actor_is_owner() else None,
        "api": {"configured": api_ready(), "down": down, "ms": api_ms, "commit": api_commit_info() if api_ready() and not down else None},
        "checked_at": int(started)})


def _person_platform(value):
    return value if value in ("yapin", "discord") else "yapin"


@app.route("/api/people/<platform>/<uid>")
@admin_required
def api_person(platform, uid):
    """What the Shapes API has linked for a person (Roblox accounts, Discord link). Bot owners only."""
    denied = _owner_only()
    if denied:
        return denied
    platform = _person_platform(platform)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", uid):
        return jsonify({"error": "That isn't an ID."}), 400
    if api_down():
        return jsonify({"error": API_DOWN_TEXT, "api_down": True}), 503
    d = shapes_api("GET", f"/internal/person/{platform}/{urllib.parse.quote(uid, safe='')}")
    if not d or not d.get("success"):
        return jsonify({"error": API_DOWN_TEXT if d is None else "Nobody found with that ID.", "api_down": d is None}), 503 if d is None else 404
    return jsonify({"platform": platform, "id": uid, "accounts": d.get("accounts", []), "max": d.get("max", 100),
                    "discord_id": d.get("discord_id") or None, "discord_ids": d.get("discord_ids") or []})


@app.route("/api/people/<platform>/<uid>/unlink", methods=["POST"])
@admin_required
def api_person_unlink(platform, uid):
    """Unlink one Roblox account ({roblox_id}) or the Discord link ({discord: true}) from a person. Bot owners only."""
    denied = _owner_only()
    if denied:
        return denied
    platform = _person_platform(platform)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", uid):
        return jsonify({"error": "That isn't an ID."}), 400
    body = request.get_json(silent=True) or {}
    quoted = urllib.parse.quote(uid, safe="")
    if body.get("discord"):
        did = str(body.get("discord_id") or "")
        path = f"/internal/person/{platform}/{quoted}/discord" + (f"?discord_id={urllib.parse.quote(did, safe='')}" if did else "")
        d = shapes_api("DELETE", path)
        what = "Discord link"
    else:
        rid = str(body.get("roblox_id") or "")
        if not rid:
            return jsonify({"error": "Which Roblox account?"}), 400
        d = shapes_api("DELETE", f"/internal/person/{platform}/{quoted}/roblox/{urllib.parse.quote(rid, safe='')}")
        what = f"Roblox account {rid}"
    if d is None:
        return jsonify({"error": API_DOWN_TEXT, "api_down": True}), 503
    if not d.get("success"):
        return jsonify({"error": str(d.get("data") or d.get("error") or "The API refused that.")}), 400
    log_action("people", f"unlinked {what}", target=f"{platform}:{uid}")
    return jsonify({"ok": True})


# --- announcements to every server ---

_ANNOUNCE_NAMES = ("announcements", "announcement", "news", "updates", "general", "chat", "main", "lobby")
_jobs = {}


def _default_channel(channels):
    texts = [c for ch in _group_channels(channels) for c in ch["channels"] if _is_text(c["type"])]
    for wanted in _ANNOUNCE_NAMES:
        for c in texts:
            if wanted in c["name"].lower():
                return c["id"]
    return texts[0]["id"] if texts else None


@app.route("/api/announce/targets")
@admin_required
def api_announce_targets():
    out = []
    for s in cached("servers", 15, bot.servers):
        sid = str(s.get("id"))
        try:
            channels = cached(f"channels:{sid}", 20, lambda sid=sid: bot.channels(sid))
        except Exception:
            continue
        texts = [c for ch in _group_channels(channels) for c in ch["channels"] if _is_text(c["type"])]
        out.append({"id": sid, "name": s.get("name") or sid, "default": _default_channel(channels),
                    "channels": [{"id": c["id"], "name": c["name"]} for c in texts]})
    return jsonify({"servers": out})


def _run_announcement(job_id, text, title, image, targets, footer=None):
    job = _jobs[job_id]
    for sid, channel_id in targets.items():
        try:
            _post_with_optional_image(channel_id, text, image, title, footer)
            job["results"].append({"server": sid, "ok": True})
        except Exception as e:
            job["results"].append({"server": sid, "ok": False, "error": str(e)})
        time.sleep(0.4)     # stay friendly with the rate limit
    job["done"] = True


@app.route("/api/announce", methods=["POST"])
@admin_required
def api_announce():
    body = request.get_json(silent=True) or {}
    text, title = str(body.get("message", "")).strip(), str(body.get("title", "")).strip()
    try:
        image = _valid_image(body.get("image_url"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if not text:
        return jsonify({"error": "Write the announcement first."}), 400
    targets = {str(k): str(v) for k, v in (body.get("targets") or {}).items() if v}
    if not targets:
        return jsonify({"error": "Pick at least one server."}), 400
    hidden = body.get("hidden") is True
    _, name, staff = current_actor()
    footer = None if hidden and staff else _stamp_text(name)
    log_action("announce", ("(hidden sender) " if hidden and staff else "") + (title + ": " if title else "") + text[:200], target=f"{len(targets)} servers")
    job_id = secrets_token()
    _jobs[job_id] = {"total": len(targets), "results": [], "done": False, "started": time.time()}
    for old in [k for k, v in _jobs.items() if time.time() - v["started"] > 3600]:
        _jobs.pop(old, None)
    threading.Thread(target=_run_announcement, args=(job_id, text, title, image, targets, footer), daemon=True, name="announce").start()
    return jsonify({"job": job_id, "total": len(targets)})


@app.route("/api/announce/<job_id>")
@admin_required
def api_announce_job(job_id):
    job = _jobs.get(job_id)
    return (jsonify(job), 200) if job else (jsonify({"error": "unknown job"}), 404)


def secrets_token():
    return base64.urlsafe_b64encode(os.urandom(9)).decode()


# --- edit a server's or user's stored data ---

def _vcounting(v):
    if not isinstance(v, dict):
        raise ValueError("Counting data must be an object.")
    out = {**default_counting(), **v}
    for key in ("number", "warnings", "highestnumber"):
        if not isinstance(out[key], int) or isinstance(out[key], bool) or out[key] < 0:
            raise ValueError(f'"{key}" must be a whole number, 0 or more.')
    for key in ("enabled", "banned"):
        if not isinstance(out[key], bool):
            raise ValueError(f'"{key}" must be true or false.')
    for key in ("channel", "lastcounter", "lastmessage"):
        if out.get(key) is not None:
            out[key] = str(out[key])
    if not isinstance(out.get("max_warnings"), int) or isinstance(out["max_warnings"], bool) or not 0 <= out["max_warnings"] <= 50:
        raise ValueError('"max_warnings" must be a whole number from 0 to 50.')
    if out.get("lastmessage_at") is not None and (not isinstance(out["lastmessage_at"], int) or isinstance(out["lastmessage_at"], bool)):
        raise ValueError('"lastmessage_at" must be a unix time in seconds, or null.')
    if not isinstance(out.get("emojis"), dict):
        raise ValueError('"emojis" must be an object like {"correct": "👍", "warning": "⚠️", "wrong": "❌"}.')
    out["emojis"] = {k: clean_emoji(out["emojis"].get(k)) or d for k, d in COUNTING_EMOJI_DEFAULT.items()}
    return out


def _vautorole(v):
    if not isinstance(v, dict) or not isinstance(v.get("enabled"), bool):
        raise ValueError('Autorole needs {"role_id": "…", "enabled": true/false} (optional: "exempt_users": [ids], "include_bots": true/false).')
    exempt = v.get("exempt_users", [])
    if not isinstance(exempt, list):
        raise ValueError('"exempt_users" must be a list of user ids.')
    return {"role_id": str(v.get("role_id") or ""), "enabled": v["enabled"], "include_bots": bool(v.get("include_bots", True)),
            "exempt_users": [str(u) for u in exempt]}


def _vcolor(v):
    if not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= 0xFFFFFF:
        raise ValueError("The embed color is a whole number between 0 and 16777215 (0xFFFFFF).")
    return v


def _vobject(v):
    if not isinstance(v, dict):
        raise ValueError("This must be a JSON object ({ … }).")
    return v


# kind -> [(database name, label, what it holds, validator)]
DATA_SCHEMA = {
    "server": [("countingDB", "Counting", "Counting channel, current number, warnings", _vcounting),
               ("autoroleDB", "Autorole", "Role given to new members", _vautorole),
               ("automodDB", "Automod", "Rules and punishments", automod.clean_config)],
    "user": [("embedDB", "Embed color", "Color of this person's embeds, as a number", _vcolor),
             ("usersDB", "Profile data", "Linked accounts (Last.fm, Roblox…) and other saved details", _vobject),
             ("AI_DB", "AI chat history", "Saved /ai conversation", _vobject)],
}


def _entity(kind, entity_id):
    """Who or what is being edited, for the header on the data page."""
    if kind == "server":
        info = dict(cached(f"server:{entity_id}", 30, lambda: bot.server(entity_id)))
        return {"kind": "server", "id": entity_id, "name": info.get("name") or entity_id,
                "avatar": _icon_of(info),
                "note": (lambda c: f"{c[0] if c[0] is not None else '?'} members" + (f" ({c[1]:,} people · {c[2]:,} bots)" if c[1] is not None else ""))(
                    server_counts(entity_id, _first(info, 'member_count', 'members_count', 'memberCount')))}
    p = _person(dict(bot.user(entity_id)))
    return {"kind": "user", "id": entity_id, "name": p["display"], "avatar": p["avatar"], "note": f"@{p['name']}" + (" · bot" if p["bot"] else "")}


@app.route("/api/data/<kind>/<entity_id>")
@admin_required
def api_data_get(kind, entity_id):
    if kind not in DATA_SCHEMA:
        return jsonify({"error": "kind must be server or user"}), 404
    try:
        entity = _entity(kind, entity_id)
    except yapper.YapperError as e:
        return _api_error(e)
    records = [{"db": name, "label": label, "about": about, "exists": DATABASES[name].exists(entity_id), "value": DATABASES[name].get(entity_id)}
               for name, label, about, _ in DATA_SCHEMA[kind]]
    records = _api_records(kind, entity_id) + records
    log = [v for v in auditDB.items().values() if isinstance(v, dict) and str(v.get("key")) == entity_id]
    return jsonify({"entity": entity, "records": records, "history": sorted(log, key=lambda v: -v.get("at", 0))[:10]})


def _api_records(kind, entity_id):
    """Read-only records that live in the Shapes API, not in this bot's tables: a person's linked Roblox and Last.fm accounts, a
    server's Roblox verification setup and numbers. (This is why Profile data looked empty for people who had linked accounts.)"""
    out = []
    try:
        if kind == "user":
            person = shapes_api("GET", f"/internal/person/yapin/{urllib.parse.quote(str(entity_id), safe='')}") or {}
            accounts = person.get("accounts") or []
            out.append({"db": "api:roblox", "label": "Roblox accounts", "readonly": True, "exists": bool(accounts or person.get("discord_id")),
                        "about": "Linked through the Shapes API (shared with the Discord bot). Read-only here.",
                        "value": {"discord_id": person.get("discord_id"),
                                  "accounts": [{k: a.get(k) for k in ("id", "name", "display_name", "auto_verify", "hidden", "linked_at", "profile")} for a in accounts]}
                        if (accounts or person.get("discord_id")) else None})
            links = shapes_api("GET", f"/internal/links/{urllib.parse.quote(str(entity_id), safe='')}") or {}
            out.append({"db": "api:lastfm", "label": "Last.fm", "readonly": True, "exists": bool(links.get("lastfm")),
                        "about": "Linked through the Shapes API. Read-only here.", "value": links.get("lastfm")})
        else:
            cfg = (shapes_api("GET", f"/internal/guild/yapin/{urllib.parse.quote(str(entity_id), safe='')}/config") or {}).get("config")
            stats = (shapes_api("GET", f"/internal/guild/yapin/{urllib.parse.quote(str(entity_id), safe='')}/stats") or {})
            value = None
            if cfg and cfg.get("group_id"):
                value = {**{k: cfg.get(k) for k in ("group_id", "nickname_template", "mirror_roles", "verified_role_id", "unverified_role_id", "prompt_dm",
                                                    "panel_channel_id", "panel_message_id", "last_sync_at")},
                         "verified_now": stats.get("verified"), "verified_ever": stats.get("ever_verified"), "last_24h": stats.get("last_24h"),
                         "last_7d": stats.get("last_7d"), "last_30d": stats.get("last_30d")}
            out.append({"db": "api:roblox-setup", "label": "Roblox verification", "readonly": True, "exists": value is not None,
                        "about": "Set with /robloxpanel; numbers from the Shapes API. Read-only here.", "value": value})
    except Exception as e:
        print(f"[admin] couldn't read the Shapes API records: {type(e).__name__}: {e}")
    return out


def _audit(db, key, action, before, after):
    stamp = time.time()
    auditDB.set(f"{int(stamp * 1000)}", {"at": int(stamp), "db": db, "key": key, "action": action,
                                          "before": json.dumps(before)[:2000], "after": json.dumps(after)[:2000]})
    print(f"[data edit] {action} {db}[{key}] from the dashboard")
    try:
        log_action("data", f"{action} {db}", server_id=key if db in ("countingDB", "autoroleDB", "automodDB") or db.startswith("dashboard:") else None, target=f"{db}[{key}]")
    except Exception as e:
        print(f"[action] couldn't log: {e}")


@app.route("/api/data/<kind>/<entity_id>/<db>", methods=["PUT", "DELETE"])
@admin_required
def api_data_write(kind, entity_id, db):
    schema = {name: check for name, _, _, check in DATA_SCHEMA.get(kind, [])}
    if db not in schema:
        return jsonify({"error": f"{db} isn't editable for a {kind}."}), 404
    store, before = DATABASES[db], DATABASES[db].get(entity_id)
    if request.method == "DELETE":
        store.remove(entity_id)
        _audit(db, entity_id, "delete", before, None)
        return jsonify({"ok": True, "exists": False, "value": None})
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or "value" not in body:
        return jsonify({"error": "Send {\"value\": …}."}), 400
    try:
        value = schema[db](body["value"])
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    store.set(entity_id, value)
    store.save()
    _audit(db, entity_id, "edit", before, value)
    return jsonify({"ok": True, "exists": True, "value": value})


# --- automod from the dashboard ---

@app.route("/api/servers/<server_id>/automod", methods=["GET", "POST"])
@admin_required
def api_automod(server_id):
    if request.method == "POST":
        return jsonify(save_automod(server_id, request.get_json(silent=True) or {}))
    return jsonify(get_automod(server_id))


def _public(ttl, limit=60):
    """Public JSON routes: answered from a short cache (so a burst of requests costs one lookup) and rate limited."""
    def deco(f):
        @rate_limited(limit, 60)
        def wrapped(*args, **kwargs):
            key, hit = f"pub:{f.__name__}", _cache.get(f"pub:{f.__name__}")
            if hit and time.time() - hit[0] < ttl:
                return hit[1]
            result = f(*args, **kwargs)
            if isinstance(result, tuple) and result[1] == 200:      # never keep an error ("still starting") around
                _cache[key] = (time.time(), result)
            return result
        wrapped.__name__ = f.__name__
        return wrapped
    return deco


def leaderboard_rows():
    """Counting leaderboard rows, best first. Cached for 30 seconds: it reads every server's counting record."""
    def build():
        names = {str(s.get("id")): s.get("name") for s in cached("servers", 15, bot.servers)}
        rows = []
        for sid in countingDB.all():
            data = countingDB.get(sid) or {}
            if data.get("banned") or not data.get("enabled") or sid not in names:
                continue
            rows.append({"id": sid, "name": names[sid], "current": int(data.get("number", 0)), "highest": int(data.get("highestnumber", 0))})
        return sorted(rows, key=lambda r: r["highest"], reverse=True)
    return cached("leaderboard", 30, build)


@app.route("/count/commands", methods=["GET"])
@_public(300)
def get_command_count():
    return {"Commands": str(len(tree.commands))}, 200


@app.route("/botinfo", methods=["GET"])
@_public(30)
def get_bot_info():
    if not bot_ready:
        return {"Servers": "Unknown", "Users": "Unknown"}, 503
    return {"Servers": str(len(cached("servers", 15, bot.servers))), "Users": str(total_members())}, 200


@app.route("/count/servers", methods=["GET"])
@_public(30)
def get_server_count():
    if not bot_ready:
        return {"Servers": "Unknown"}, 503
    return {"Servers": str(len(cached("servers", 15, bot.servers)))}, 200


@app.route("/count/users", methods=["GET"])
@_public(30)
def get_user_count():
    if not bot_ready:
        return {"Users": "Unknown"}, 503
    return {"Users": str(total_members())}, 200


@app.route("/clb", methods=["GET"])
@rate_limited(60, 60)
def countinglb():
    if not bot_ready:
        return jsonify("Bot is still starting"), 503
    lb = {r["id"]: {"currentnumber": r["current"], "highestnumber": r["highest"], "serverName": r["name"]} for r in leaderboard_rows()}
    return {"Leaderboard": list(lb.items())}, 200


_member_sample = {}
_member_shape_logged = False


def _log_member_shape(page):
    """Once per run, print what a member record looks like (the bot's own and one other) so bot detection can be checked against real data."""
    global _member_shape_logged
    if _member_shape_logged or not page:
        return
    _member_shape_logged = True
    try:
        me = str((bot.me or {}).get("id") or "")
        mine = next((dict(m) for m in page if str(_first(dict(m), "user_id", "id") or (dict(m).get("user") or {}).get("id")) == me), None)
        other = next((dict(m) for m in page if dict(m) is not mine), None)
        show = lambda r: json.dumps({k: (v if not isinstance(v, (dict, list)) or len(str(v)) < 160 else "…") for k, v in (r or {}).items()}, default=str)[:700]
        print(f"[members] the bot's own record: {show(mine)}")
        print(f"[members] another record: {show(other)}")
        _member_sample.update(mine=show(mine), other=show(other))
    except Exception as e:
        print(f"[members] couldn't describe a record: {e}")


def _all_members(server_id, limit=5000):
    """Every member (paged 200 at a time), as flat dicts from _person."""
    out, after = [], None
    while len(out) < limit:
        page = bot.members(server_id, after=after)
        _log_member_shape(page)
        people = [_person(dict(m)) for m in page]
        out.extend(people)
        if len(page) < 200 or not people:
            break
        after = people[-1]["id"]
    return out


@app.route("/mutuals", methods=["GET"])
@admin_required     # member lists are user data, so this is no longer public (the /mutuals command still is)
def mutualservers():
    if not bot_ready:
        return {"Servers": "Unknown"}, 503

    def build():
        servers = {}
        for s in bot.servers():
            sid = s.get("id")
            try:
                entry = {"name": s.get("name"), "iconurl": _icon_of(s),
                         "owner": _first(s, "owner_id", "ownerId", "owner"), "channels": len(bot.channels(sid)),
                         "roles": len(bot.roles(sid)), "members": [], "bots": []}
                for p in _all_members(sid):
                    (entry["bots"] if p["bot"] else entry["members"]).append({"name": p["name"], "id": p["id"]})
            except Exception as e:
                print(f"[mutuals] {sid}: {e}")
                continue
            entry["membercount"], entry["botcount"] = len(entry["members"]), len(entry["bots"])
            servers[sid] = entry
        return servers
    return {"Servers": cached("mutuals", 60, build)}, 200


bot.core = sys.modules[__name__]               # what cogs use to reach this file's helpers (cog.bot.core)
bot.load_extensions(os.path.join(os.path.dirname(os.path.abspath(__file__)), "cogs"))     # every file in cogs/ (Roblox verification, ...); one topic per file
import userdash
userdash.register(sys.modules[__name__])      # the public website and the user dashboard


# =====================================================================================
# Startup
# =====================================================================================

_web_server = None


def run_web():
    global _web_server
    from werkzeug.serving import make_server
    port = int(os.environ.get("PORT", 5004))
    print(f"Starting web server on port {port}")
    _web_server = make_server("0.0.0.0", port, app, threaded=True)
    _web_server.serve_forever()


if __name__ == "__main__":
    install(bot)
    threading.Thread(target=run_web, daemon=True, name="web").start()
    bot.run()

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
bot.tree = tree
BOT = bot
owner = "leandre"
MainURL = "https://shapes-yapper.oriaxen.com"
InviteURL = os.environ.get("YAPPER_INVITE_URL", "https://our-websites.github.io/shapes.lol/yapin?s=true")
searchengine = "621a38269031b4e89"  # PLEASE USE YOUR OWN SEARCH ENGINE ID FROM https://cse.google.com/

APIBaseURL_Key = _secret("Shapes_API_Key")
APIBaseURL = os.environ.get("SHAPES_API_URL", "http://localhost:1337").rstrip("/")
# The token for the API's /internal routes (its INTERNAL_TOKEN). The older Shapes_API_Key secret still works.
API_INTERNAL_TOKEN = os.environ.get("SHAPES_API_TOKEN", "").strip() or APIBaseURL_Key
APIBaseURL_Headers = {"Authorization": f"Bearer {API_INTERNAL_TOKEN}"}
_api_session = requests.Session()


def api_ready():
    return bool(API_INTERNAL_TOKEN)


def shapes_api(method, path, body=None, timeout=30):
    """Call the Shapes API on this machine. Returns its JSON (a dict) or None when it can't be reached.
    A short timeout: it is on localhost, so if it doesn't answer quickly something is wrong."""
    if not api_ready():
        return None
    try:
        #print(timeout)
        r = _api_session.request(method, f"{APIBaseURL}{path}", json=body, headers=APIBaseURL_Headers, timeout=30)
        data = r.json()
        print(data)
        print('Shapes API Is Responding.' if data['success'] == True else 'Shapes API Is Responding But Denying Your Requests.')
        return data if isinstance(data, dict) else None
    except (requests.RequestException, ValueError):
        print("Shapes API Is Not Responding.")
        return None


_ROBLOX_LINK_CACHE = {}


def roblox_of_yapin(yapin_id):
    """(roblox_id, roblox_name, hidden) for a Yapin user, or None when not linked / API down."""
    data = shapes_api("GET", f"/internal/lookup/yapin/{yapin_id}")
    if not data or not data.get("success"):
        return None
    return (data.get("data"), data.get("roblox_name"), bool(data.get("hidden")))


def yapin_of_roblox(roblox):
    """(yapin_id, hidden) for a Roblox id or username, or None."""
    data = shapes_api("GET", f"/internal/lookup/roblox/{urllib.parse.quote(str(roblox), safe='')}")
    if not data or not data.get("success"):
        return None
    return (data.get("data"), bool(data.get("hidden")))
def owner_of_roblox(roblox):
    """{"yapin_id", "discord_id"} of whoever linked this Roblox account; {} when nobody did, it is hidden, or the API is down."""
    data = shapes_api("GET", f"/internal/lookup/roblox/{urllib.parse.quote(str(roblox), safe='')}")
    if not data or not data.get("success") or data.get("hidden"):
        return {}
    return {"yapin_id": data.get("yapin_id") or data.get("data"), "discord_id": data.get("discord_id")}


GoogleAPIKey = _secret("GOOGLE_API_KEY", "GoogleToken.txt")
ADMIN_KEY = _secret("ADMIN_KEY", "akey.txt")

bot_ready = False
started_at = time.time()


def ecolor(user_id):
    """A user's saved embed colour, or blue."""
    return embedDB.get(f"{user_id}") or discord.Color.blue()


# last online cache (Roblox)
def load_cached_timestamps():
    try:
        with open('lastonline.json', 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def update_cached_timestamp(user_id, timestamp):
    try:
        cached_timestamps = load_cached_timestamps()
        cached_timestamps[str(user_id)] = timestamp
        with open('lastonline.json', 'w') as f:
            json.dump(cached_timestamps, f, indent=2)
    except Exception as e:
        print(f"Error updating cached timestamp: {e}")


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


def human_count(sid, total=None):
    """People in a server, bots left out. The API's member_count includes bots, so count from the member list (cached for
    two minutes) and fall back to that total only when the list can't be read. Past 5,000 members the list is a sample, so
    the bots in it are taken off the reported total instead."""
    try:
        members = cached(f"allmembers:{sid}", 120, lambda: _all_members(sid))
    except Exception:
        members = None
    if members:
        bots = sum(1 for p in members if p["bot"])
        if len(members) < 5000:
            return len(members) - bots
        return max(0, int(total or len(members)) - bots)
    return int(total) if total is not None else None


def server_counts(sid, total=None):
    """(total, people, bots) for a server. Total is what the API reports (bots included), people are counted from the member list."""
    try:
        total = int(total) if total is not None else None
    except (TypeError, ValueError):
        total = None
    people = human_count(sid, total)
    if total is None:
        total = people
    if people is None:
        return total, None, None
    return max(total, people), people, max(0, max(total, people) - people)


def human_counts(servers):
    """{server id: people} for a list of server dicts, looked up a few at a time."""
    def one(s):
        total = _first(s, "member_count", "members_count", "memberCount")
        try:
            total = int(total) if total is not None else None
        except (TypeError, ValueError):
            total = None
        return str(s.get("id")), human_count(s.get("id"), total)
    with ThreadPoolExecutor(max_workers=8) as pool:
        return dict(pool.map(one, servers))


def total_members():
    """Approximate number of people across every server the bot is in (cached for 5 minutes)."""
    if time.time() - _member_cache["at"] < 300:
        return _member_cache["value"]
    total = sum(int(n or 0) for n in human_counts(list(bot.servers())).values())
    _member_cache.update(at=time.time(), value=total)
    return total


COUNTING_EMOJI_DEFAULT = {"correct": "👍", "warning": "⚠️", "wrong": "❌"}
COUNTING_EMOJI_NAMES = {"correct": "correct", "right": "correct", "good": "correct", "warning": "warning", "warn": "warning",
                        "wrong": "wrong", "incorrect": "wrong", "ruined": "wrong", "fail": "wrong"}


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


def _counter_label(user_id):
    """'<@id> (@username)' for the last person to count, or None."""
    if not user_id:
        return None
    name = fetch_user(user_id).get("username")
    return f"<@{user_id}>" + (f" (@{name})" if name else "")


def counting_stats_embed(data, color, requester):
    """What anyone may see: how far the count is. No settings."""
    nxt = data.get("number", 0) + 1
    embed = discord.Embed(title="Counting", color=color)
    embed.add_field(name="Current Number", value=f"`{data.get('number', 0):,}`", inline=True)
    embed.add_field(name="Highest Number", value=f"`{data.get('highestnumber', 0):,}`", inline=True)
    embed.add_field(name="Next Number", value=f"`{nxt:,}`", inline=True)
    if data.get("lastcounter"):
        embed.add_field(name="Last Counter", value=_counter_label(data["lastcounter"]), inline=False)
    if data.get("channel"):
        embed.add_field(name="Channel", value=f"<#{data['channel']}>", inline=False)
    embed.set_footer(text=f"Requested By {requester} | {MainURL}")
    return embed


async def need_admin(interaction):
    """True if the caller may manage this server (replies and returns False otherwise)."""
    if interaction.is_dm:
        await interaction.response.send_message("This command only works inside a server.", ephemeral=True)
        return False
    if not is_admin(interaction.guild_id, interaction.user.id):
        await interaction.response.send_message("You need to be a server admin to use this command.", ephemeral=True)
        return False
    return True



# =====================================================================================
# Commands rewritten for Yapper
# (select menus and modals do not exist for commands, so these take arguments instead; private replies use ephemeral=True)
# =====================================================================================

@bot.tree.command(name="userinstalls", description="Get The User Installation Count For Shapes!")
async def userinstalls(interaction: discord.Interaction):
    await interaction.response.send_message(f"{total_members():,} Users Use Shapes!")


@bot.tree.command(name="servercount", description="Get The Server Count For Shapes!")
async def servercount(interaction: discord.Interaction):
    await interaction.response.send_message(f"{len(bot.servers()):,} Servers Use Shapes!")


@bot.tree.command(name="getdata", description="Owner only: /getdata <embedDB|countingDB|usersDB|autoroleDB|AI_DB> <key>")
async def getdata(interaction: discord.Interaction, database: str, key: str):
    # Private reply (ephemeral): only the person who ran the command sees it.
    if not is_owner(interaction.user):
        await interaction.response.send_message(":x: You don't have permission to use this command! :x:", ephemeral=True)
        return
    db = DATABASES.get(database)
    if db is None:
        await interaction.response.send_message(f":x: Database `{database}` not found! Options: {', '.join(DATABASES)}", ephemeral=True)
        return
    data = db.get(key)
    if data:
        text = f"Data for key `{key}` in database `{database}`:\n```{json.dumps(data, indent=2, default=str)[:1800]}```"
    else:
        text = f":x: No data found for key `{key}` in database `{database}`! :x:"
    await interaction.response.send_message(text, ephemeral=True)


@bot.tree.command(name="settings", description="Your Settings For Shapes: /settings color <name>")
async def settings(interaction: discord.Interaction, option: str = "", value: str = ""):
    uid = interaction.user.id
    names = [n for n, _ in DiscordColors]
    if option.lower() in ("color", "colour"):
        pick = value.strip().lower().replace("_", " ")
        match = next(((n, c) for n, c in DiscordColors if n.lower() == pick), None)
        if pick == "random":
            match = ("Random", discord.Color.random())
        if not match:
            await interaction.response.send_message(f"Pick one of: {', '.join(names)}, Random", ephemeral=True)
            return
        embedDB.set(f"{uid}", match[1].value)
        embed = discord.Embed(title="Embed Color Changed!",
                              description=f"Your embed color has been changed successfully to {match[0]}!", color=match[1])
        embed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    await interaction.response.send_message(embed=_settings_embed(interaction.user), view=_color_view(uid), ephemeral=True)


def _settings_embed(user, note=None):
    current = embedDB.get(f"{user.id}")
    current_name = next((n for n, c in DiscordColors if c.value == current), "Blue" if not current else "Custom")
    embed = discord.Embed(title="Settings", color=ecolor(user.id),
                          description=(f"✅ {note}\n\n" if note else "") + f"**Embed color:** {current_name}\n\nTap a color below to change it.")
    embed.set_footer(text=f"Requested By {user.name} | {MainURL}")
    return embed


def _color_view(user_id):
    """One button per embed color; tapping one saves it and refreshes the panel."""
    view = discord.ui.View(timeout=300)
    current = embedDB.get(f"{user_id}")
    for name, color in DiscordColors + [("Random", None)]:
        selected = color is not None and color.value == current
        button = discord.ui.Button(label=name, style=discord.ButtonStyle.primary if selected else discord.ButtonStyle.secondary)

        async def pick(click, name=name, color=color):
            if str(click.user.id) != str(user_id):
                await click.response.send_message("This isn't your settings panel. Run /settings yourself.", ephemeral=True)
                return
            chosen = color or discord.Color.random()
            embedDB.set(f"{user_id}", chosen.value)
            await click.response.edit_message(embed=_settings_embed(click.user, f"Embed color changed to {name}!"), view=_color_view(user_id))

        button.callback = pick
        view.add_item(button)
    return view


@bot.tree.command(name="status", description=f"Get the {MainURL} status")
async def status_command(interaction: discord.Interaction):
    await interaction.response.send_message(f"[Shapes Status](https://status.lecanact.me)")


def _save_everything():
    for db in DATABASES.values():
        try:
            db.save()
        except Exception as e:
            print(f"save failed for {db.name}: {e}")


def _shutdown_services():
    """Stop the bot and release the web server's port, so a restart can bind it again right away."""
    for label, stopper in (("bot", getattr(bot, "stop", None)), ("web server", stop_web)):
        try:
            if stopper:
                stopper()
        except Exception as e:
            print(f"could not stop the {label}: {e}")


class ConfirmStop(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)

    async def interaction_check(self, click):
        if not is_owner(click.user):
            await click.response.send_message("Only the bot owner can press this.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Stop the bot", style=discord.ButtonStyle.danger)
    async def confirm(self, click, button):
        self.stop()
        await click.response.edit_message(content="✅ Shutting down. Saving data first…", embed=None, view=None)
        _save_everything()
        print("Bot shutdown initiated by command")
        _shutdown_services()
        threading.Timer(1.0, lambda: os._exit(0)).start()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, click, button):
        self.stop()
        await click.response.edit_message(content="Cancelled. The bot is still running.", embed=None, view=None)


@bot.tree.command(name="stop", description="Stop the bot (asks you to confirm).")
async def stop(interaction: discord.Interaction):
    if not is_owner(interaction.user):
        await interaction.response.send_message(f"Only {owner} can use this command.", ephemeral=True)
        return
    await interaction.response.send_message("⚠️ Stop the bot completely? It stays offline until someone starts it again.",
                                            view=ConfirmStop(), ephemeral=True)


@bot.tree.command(name="restart", description="Restart the bot.")
async def restart(interaction: discord.Interaction):
    if not is_owner(interaction.user):
        await interaction.response.send_message(f"Only {owner} can use this command.", ephemeral=True)
        return
    await interaction.response.send_message(":white_check_mark: Restarting...", ephemeral=True)
    _save_everything()
    print("Bot restart initiated by command")
    _shutdown_services()    # frees port first: starting a second server on it was what broke restarts

    def relaunch():
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:
            pass
        argv = [sys.executable] + sys.argv
        try:
            os.closerange(3, 1024)      # nothing (sockets, files) should leak into the new process
            os.execv(sys.executable, argv)
        except Exception as e:
            print(f"execv failed ({e}), starting a new process instead")
            import subprocess
            subprocess.Popen(argv)
            os._exit(0)
    threading.Timer(1.5, relaunch).start()


@bot.tree.command(name="counting", description="Counting: stats for everyone; admins can use /counting [toggle|channel|emoji]")
async def counting(interaction: discord.Interaction, action: str = "", value: str = ""):
    if interaction.is_dm:
        await interaction.response.send_message("This command only works inside a server.", ephemeral=True)
        return
    sid = str(interaction.guild_id)
    data = {**default_counting(), **(countingDB.get(sid) or {})}
    is_staff = is_admin(interaction.guild_id, interaction.user.id)
    if action.lower() in ("warnings", "maxwarnings"):       # bot owners only, in any server
        if not is_owner(interaction.user):
            await interaction.response.send_message("Only the bot owner can change how many warnings counting gives.", ephemeral=True)
            return
        try:
            amount = int(str(value).strip())
            if not 0 <= amount <= 50:
                raise ValueError
        except ValueError:
            await interaction.response.send_message(f"Usage: `/counting warnings <0-50>` (now **{counting_limit(data)}**; 0 ruins the count on the first mistake).", ephemeral=True)
            return
        data["max_warnings"] = amount
        countingDB.set(sid, data)
        await interaction.response.send_message(f"This server now gets **{amount}** warning{'s' if amount != 1 else ''} before the count is ruined.", ephemeral=True)
        return
    if not is_staff:
        if not data.get("channel") and not data.get("enabled"):
            await interaction.response.send_message("Counting isn't set up in this server.", ephemeral=True)
            return
        await interaction.response.send_message(embed=counting_stats_embed(data, ecolor(interaction.user.id), interaction.user.name))
        return
    action = action.lower()
    if action == "toggle":
        data["enabled"] = not data["enabled"]
        countingDB.set(sid, data)
        await interaction.response.send_message(f"Counting {'enabled' if data['enabled'] else 'disabled'}.", ephemeral=True)
        return
    if action == "channel":
        ch = resolve_channel(sid, value) if value else None
        if not ch:
            await interaction.response.send_message("Tell me which text channel: `/counting channel #general`", ephemeral=True)
            return
        data["channel"] = str(ch.get("id"))
        countingDB.set(sid, data)
        await interaction.response.send_message(f"Counting channel set to <#{ch.get('id')}>.", ephemeral=True)
        return
    if action in ("emoji", "emojis"):
        parts = value.split(None, 1)
        kind = COUNTING_EMOJI_NAMES.get(parts[0].lower()) if parts else None
        if parts and parts[0].lower() == "reset":
            data["emojis"] = dict(COUNTING_EMOJI_DEFAULT)
            countingDB.set(sid, data)
            await interaction.response.send_message("Counting emoji are back to the defaults.", ephemeral=True)
            return
        emoji = clean_emoji(parts[1]) if len(parts) > 1 else None
        if not kind or not emoji:
            await interaction.response.send_message("Usage: `/counting emoji <correct|warning|wrong> <emoji>` or `/counting emoji reset`", ephemeral=True)
            return
        data["emojis"] = {**counting_emojis(data), kind: emoji}
        countingDB.set(sid, data)
        await interaction.response.send_message(f"The **{kind}** emoji is now {emoji}", ephemeral=True)
        return
    countingDB.set(sid, data)
    emo = counting_emojis(data)
    channel = f"<#{data['channel']}>" if data["channel"] else "not set"
    embed = counting_stats_embed(data, ecolor(interaction.user.id), interaction.user.name)
    embed.title = "Counting Settings"
    embed.add_field(name="Enabled", value=f"`{data['enabled']}`", inline=True)
    embed.add_field(name="Warnings", value=f"`{data['warnings']}`/{counting_limit(data)}", inline=True)
    embed.add_field(name="Emoji", value=f"Correct {emo['correct']}  ·  Warning {emo['warning']}  ·  Wrong {emo['wrong']}", inline=False)
    embed.add_field(name="Change it", value="`/counting toggle` · `/counting channel #channel` · `/counting emoji <correct|warning|wrong> <emoji>`", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


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


def _cmdlog_status_embed(sid, color, note=None):
    cfg = get_cmdlog(sid)
    embed = discord.Embed(title="Command log", color=color)
    state = "on" if cfg["enabled"] else "off"
    embed.description = (f"✅ {note}\n\n" if note else "") + (
        f"Logging is **{state}**" + (f" in <#{cfg['channel']}>" if cfg["channel"] else " (no channel chosen)") + ".")
    if cfg["channel"]:
        how = {"webhook": "through a webhook", "bot": "as the bot (Yapin' wouldn't let me make a webhook)"}.get(cfg["mode"], "through a webhook if possible, otherwise as the bot")
        embed.add_field(name="Posts", value=how, inline=False)
        if channel_is_public(sid, cfg["channel"]):
            embed.add_field(name="⚠️ Public channel", value="Everyone in the server can read this channel, so everyone can see who runs which command.", inline=False)
    embed.add_field(name="Change it", value="`/cmdlog channel #private-logs` · `/cmdlog on` · `/cmdlog off` · the dashboard shows the same log with names and times", inline=False)
    return embed


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


@bot.tree.command(name="partner", description="Manage partner servers (bot owner): /partner add|remove|description|invite|list <server id> [text]")
async def partner(interaction: discord.Interaction, action: str = "list", server: str = "", text: str = ""):
    uid = str(interaction.user.id)
    if not is_bot_owner(uid):
        await interaction.response.send_message("Only the bot owner can manage partners.", ephemeral=True)
        return
    action, server = (action or "list").lower(), str(server or "").strip()
    if server == "" and action in ("add", "remove", "description", "invite") and interaction.guild_id:
        server = str(interaction.guild_id)           # run it inside the server to partner that one
    if action == "list" or (action not in ("add", "remove", "description", "invite")):
        rows = [f"**{partnersDB.get(s).get('name') or s}** (`{s}`) " + ("— " + partnersDB.get(s)["invite"] if partnersDB.get(s).get("invite") else "— no invite yet") for s in partnersDB.all()]
        embed = discord.Embed(title="Partners", description="\n".join(rows) or "No partners yet. `/partner add <server id> [description]`", color=ecolor(uid))
        embed.add_field(name="Commands", value="`/partner add <server id> [description]` · `/partner remove <server id>` · `/partner description <server id> <text>` · `/partner invite <server id> <invite url>` (or `new`)", inline=False)
        embed.set_footer(text=f"Shown at {MainURL}/partners")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    if not server:
        await interaction.response.send_message("Which server? Give its id (or run this inside the server).", ephemeral=True)
        return
    if action == "remove":
        gone = partnersDB.remove(server)
        _cache.pop(f"summary:{server}", None)
        await interaction.response.send_message("Removed from the partners page." if gone else "That server isn't a partner.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    row = dict(partnersDB.get(server) or {})
    if action == "add":
        try:
            info = await asyncio.to_thread(server_summary, server)
        except Exception as e:
            await interaction.followup.send(f"I can only partner servers I'm in. I couldn't load that one ({e}).", ephemeral=True)
            return
        invite = await asyncio.to_thread(make_partner_invite, server)
        row.update(name=info["name"], icon=info["icon"], added_by=uid, added_at=row.get("added_at") or int(time.time()), invite=invite or row.get("invite"), invite_auto=bool(invite) or bool(row.get("invite_auto")))
        if text:
            row["description"] = text.strip()[:400]
        partnersDB.set(server, row)
        log_action("partner", f"added {info['name']}", server_id=server)
        note = "" if row.get("invite") else "\n⚠️ I couldn't make or find an invite. Add one with `/partner invite <server id> <invite url>`."
        await interaction.followup.send(f"**{info['name']}** is now a partner: {MainURL}/partners{note}", ephemeral=True)
        return
    if not partnersDB.exists(server):
        await interaction.followup.send("That server isn't a partner yet. `/partner add <server id>` first.", ephemeral=True)
        return
    if action == "description":
        row["description"] = text.strip()[:400] or None
        partnersDB.set(server, row)
        await interaction.followup.send("Description saved." if row["description"] else "Description cleared: the server's own description is used.", ephemeral=True)
        return
    if action == "invite":
        url = (await asyncio.to_thread(make_partner_invite, server)) if text.strip().lower() in ("", "new") else valid_invite_url(text)
        if not url:
            await interaction.followup.send("I need an https://www.yapin.gg/... invite link (I couldn't make one myself).", ephemeral=True)
            return
        row["invite"] = url
        row["invite_auto"] = text.strip().lower() in ("", "new")      # only a grabbed invite is refreshed on its own
        partnersDB.set(server, row)
        await interaction.followup.send(f"Invite saved: {url}", ephemeral=True)


@bot.tree.command(name="cmdlog", description="Log every command used in this server (server owner only): /cmdlog [channel #channel | on | off]")
async def cmdlog(interaction: discord.Interaction, action: str = "status", value: str = ""):
    if interaction.is_dm:
        await interaction.response.send_message("This command only works inside a server.", ephemeral=True)
        return
    sid, uid = str(interaction.guild_id), str(interaction.user.id)
    if not is_server_owner(sid, uid):
        await interaction.response.send_message("Only the server owner can set up command logs.", ephemeral=True)
        return
    color, action = ecolor(uid), (action or "status").lower()
    if action in ("on", "off"):
        cfg, _ = cmdlog_configure(sid, enabled=action == "on")
        if action == "on" and not cfg["enabled"]:
            await interaction.response.send_message("Pick a channel first: `/cmdlog channel #private-logs`", ephemeral=True)
            return
        await interaction.response.send_message(embed=_cmdlog_status_embed(sid, color, f"Command logging is now {action}."), ephemeral=True)
        return
    if action == "channel":
        ch = resolve_channel(sid, value) if value else None
        if not ch:
            await interaction.response.send_message("Tell me which text channel: `/cmdlog channel #private-logs`", ephemeral=True)
            return
        cfg, warning = cmdlog_configure(sid, channel_id=str(ch.get("id")), enabled=True)
        if warning:
            view = discord.ui.View(timeout=300)
            use = discord.ui.Button(label="Log publicly anyway", style=discord.ButtonStyle.danger)
            cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)

            async def confirm(click):
                if str(click.user.id) != uid:
                    await click.response.send_message("Only the owner who ran the command can answer this.", ephemeral=True)
                    return
                cmdlog_configure(sid, channel_id=str(ch.get("id")), enabled=True, confirm_public=True)
                await click.response.edit_message(embed=_cmdlog_status_embed(sid, color, "Public logging is on."), view=None)

            async def nope(click):
                if str(click.user.id) == uid:
                    await click.response.edit_message(embed=discord.Embed(title="Command log", description="Okay, nothing changed.", color=color), view=None)
            use.callback, cancel.callback = confirm, nope
            view.add_item(use)
            view.add_item(cancel)
            await interaction.response.send_message(embed=discord.Embed(title="⚠️ That channel is public", color=discord.Color.orange(), description=(
                f"<#{ch.get('id')}> can be read by everyone in the server, so everyone would see who runs which command.\n"
                "Choose a private channel, or log publicly if that's what you want.")), view=view, ephemeral=True)
            return
        await interaction.response.send_message(embed=_cmdlog_status_embed(sid, color, f"Logging to <#{ch.get('id')}>."), ephemeral=True)
        return
    await interaction.response.send_message(embed=_cmdlog_status_embed(sid, color), ephemeral=True)


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
    for db in (countingDB, autoroleDB, automodDB, cmdlogcfgDB, holdsDB, welcomedDB):
        db.remove(sid)
    for key in [k for k in cmdlogDB.items() if k.startswith(f"{sid}:")]:
        cmdlogDB.remove(key)
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
                "Pick a colour for my embeds with **/settings**.")


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
        text = f"**{members:,}** total" if isinstance(members, int) else str(members)
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


def _botlog_status_embed(color, note=None):
    cfg = get_botlog()
    embed = discord.Embed(title="Bot log", color=color)
    embed.description = (f"✅ {note}\\n\\n" if note else "") + (f"Logging is **{'on' if cfg['enabled'] else 'off'}**" + (f" in <#{cfg['channel']}>" if cfg["channel"] else " (no channel chosen)") + ".")
    embed.add_field(name="Every command, in every server", value="on" if cfg["commands"] else "off", inline=True)
    embed.add_field(name="Server added / removed", value="on" if cfg["events"] else "off", inline=True)
    embed.add_field(name="Change it", value="`/botlog channel #logs` · `/botlog on|off` · `/botlog commands on|off` · `/botlog events on|off`", inline=False)
    return embed


@bot.tree.command(name="botlog", description="Bot owners only: log every command and server add/remove: /botlog [channel #c | on | off | commands on|off | events on|off]")
async def botlog(interaction: discord.Interaction, action: str = "status", value: str = ""):
    uid = str(interaction.user.id)
    if not is_bot_owner(uid):
        await interaction.response.send_message("Only the bot owners can set up the bot log.", ephemeral=True)
        return
    color, action, value = ecolor(uid), (action or "status").lower(), (value or "").strip().lower()
    if action in ("on", "off"):
        cfg = botlog_configure(enabled=action == "on")
        if action == "on" and not cfg["enabled"]:
            await interaction.response.send_message("Pick a channel first: `/botlog channel #logs`", ephemeral=True)
            return
        await interaction.response.send_message(embed=_botlog_status_embed(color, f"The bot log is now {action}."), ephemeral=True)
    elif action in ("commands", "events") and value in ("on", "off"):
        botlog_configure(**{action: value == "on"})
        await interaction.response.send_message(embed=_botlog_status_embed(color, f"{action.title()} logging is now {value}."), ephemeral=True)
    elif action == "channel":
        ch = resolve_any_channel(value)
        if not ch:
            await interaction.response.send_message("I couldn't find that channel. Use `/botlog channel #logs` (a channel in a server I'm in).", ephemeral=True)
            return
        botlog_configure(channel_id=str(ch.get("id")), enabled=True)
        await interaction.response.send_message(embed=_botlog_status_embed(color, f"Logging to <#{ch.get('id')}>."), ephemeral=True)
    else:
        await interaction.response.send_message(embed=_botlog_status_embed(color), ephemeral=True)


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


def assign_autorole_to_existing(server_id, cfg):
    """Gives the role to everyone who is not exempt and does not have it yet. Returns how many got it."""
    count = 0
    for m in _all_members(server_id):
        if m["id"] in cfg["exempt_users"] or cfg["role_id"] in m["roles"] or (m["bot"] and not cfg["include_bots"]):
            continue
        try:
            bot.add_role(server_id, m["id"], cfg["role_id"])
            count += 1
        except Exception as e:
            print(f"[autorole] could not give the role to {m['id']}: {e}")
    return count


def _autorole_embed(cfg, user, role_name=None, note=None):
    on = cfg["enabled"] and cfg["role_id"]
    embed = discord.Embed(title="Autorole", color=discord.Color.green() if on else discord.Color.red(),
                          description=(f"✅ {note}\n\n" if note else "") + (f"New members get <@&{cfg['role_id']}>." if on else "Autorole is **off**."))
    embed.add_field(name="Role", value=f"<@&{cfg['role_id']}>" if cfg["role_id"] else "Not set", inline=True)
    embed.add_field(name="Bots", value="Get the role" if cfg["include_bots"] else "Skipped", inline=True)
    exempt = cfg["exempt_users"]
    embed.add_field(name=f"Left out ({len(exempt)})", inline=False,
                    value=" ".join(f"<@{u}>" for u in exempt[:20]) + (f" and {len(exempt) - 20} more" if len(exempt) > 20 else "") if exempt else "Nobody. Add people with `/autorole exempt @user`.")
    embed.set_footer(text="/autorole <role> · exempt <@users> · unexempt <@users> · bots <on|off> · off")
    return embed


def _autorole_view(server_id, owner_id):
    cfg = get_autorole(server_id)
    view = discord.ui.View(timeout=300)

    def add(label, style, action):
        button = discord.ui.Button(label=label, style=style)

        async def pressed(click):
            if str(click.user.id) != str(owner_id) or not is_admin(server_id, click.user.id):
                await click.response.send_message("Only the admin who opened this panel can use it.", ephemeral=True)
                return
            current = get_autorole(server_id)
            note = action(current)
            current = save_autorole(server_id, current)
            await click.response.edit_message(embed=_autorole_embed(current, click.user, note=note), view=_autorole_view(server_id, owner_id))
        button.callback = pressed
        view.add_item(button)

    def toggle(c):
        c["enabled"] = not c["enabled"]
        return "Autorole is now " + ("on." if c["enabled"] else "off.")

    def bots(c):
        c["include_bots"] = not c["include_bots"]
        return "Bots " + ("get the role." if c["include_bots"] else "are skipped.")

    def existing(c):
        return f"Gave the role to {assign_autorole_to_existing(server_id, c)} existing members."
    add("Turn off" if cfg["enabled"] else "Turn on", discord.ButtonStyle.danger if cfg["enabled"] else discord.ButtonStyle.primary, toggle)
    add("Skip bots" if cfg["include_bots"] else "Include bots", discord.ButtonStyle.secondary, bots)
    if cfg["role_id"]:
        add("Give to existing members", discord.ButtonStyle.secondary, existing)
    return view


@bot.tree.command(name="autorole", description="Give new members a role: /autorole <role> | exempt <@users> | unexempt <@users> | bots <on|off> | off")
async def autorole(interaction: discord.Interaction, action: str = "status", *, rest: str = ""):
    if not await need_admin(interaction):
        return
    sid = str(interaction.guild_id)
    cfg = get_autorole(sid)
    word = action.lower().strip()
    note = None

    async def say(text):
        await interaction.response.send_message(text, ephemeral=True)

    if word in ("status", "", "panel"):
        pass
    elif word in ("off", "disable"):
        cfg["enabled"], note = False, "Autorole is off."
    elif word in ("on", "enable"):
        if not cfg["role_id"]:
            return await say("Pick a role first: `/autorole <role>`")
        cfg["enabled"], note = True, "Autorole is on."
    elif word in ("exempt", "skip", "ignore"):
        ids = _user_ids(rest)
        if not ids:
            return await say("Usage: `/autorole exempt @user @other` (one person or several)")
        cfg["exempt_users"] = list(dict.fromkeys(cfg["exempt_users"] + ids))
        note = f"Left out {len(ids)} {'person' if len(ids) == 1 else 'people'}."
    elif word in ("unexempt", "include", "unskip"):
        ids = set(_user_ids(rest))
        if not ids:
            return await say("Usage: `/autorole unexempt @user`")
        cfg["exempt_users"] = [u for u in cfg["exempt_users"] if u not in ids]
        note = f"Removed {len(ids)} from the left-out list."
    elif word == "bots":
        state = rest.strip().lower()
        if state not in ("on", "off", "true", "false"):
            return await say("Usage: `/autorole bots on` (bots get the role) or `/autorole bots off`")
        cfg["include_bots"] = state in ("on", "true")
        note = "Bots " + ("get the role." if cfg["include_bots"] else "are skipped.")
    else:
        # /autorole <role> [true|false]: the original form, the role name is the first word (or in quotes)
        token = (action + " " + rest).strip()
        enable = True
        if token.lower().endswith((" false", " off")):
            enable, token = False, token.rsplit(" ", 1)[0]
        elif token.lower().endswith((" true", " on")):
            token = token.rsplit(" ", 1)[0]
        try:
            role = resolve_role(token.strip().strip('"'), sid)
        except ValueError as e:
            return await say(f"{e}. Use `/autorole <role name>`, or `/autorole status` to see the settings.")
        cfg.update(role_id=str(role.id), enabled=enable)
        note = f"Autorole {'on' if enable else 'off'} for {role.name}."
    cfg = save_autorole(sid, cfg)
    await interaction.response.send_message(embed=_autorole_embed(cfg, interaction.user, note=note),
                                            view=_autorole_view(sid, interaction.user.id), ephemeral=True)


@bot.tree.command(name="help", description="Get help with all available commands")
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(title="Bot Help & Commands", color=ecolor(interaction.user.id),
                          description="Use `/command` or `!command`. Put names with spaces in quotes.")
    embed.add_field(name="Counting", value="• /counting - Count stats for everyone; admins also get settings (`toggle`, `channel #channel`, `emoji <correct|warning|wrong> <emoji>`)\n• /countingleaderboard - Leaderboard website\n• /cmdlog - Command log (server owner)", inline=False)
    embed.add_field(name="User Settings", value="• /settings - Customize your embed color\n• /autorole - Give new members a role\n• /lastfm - Link Last.fm", inline=False)
    embed.add_field(name="Moderation", value="• /automod - Block words, links, invites, spam, caps and mass mentions (admins)\n• /serverinfo - Server information", inline=False)
    embed.add_field(name="Roblox", value=(
        "• /robloxinfo - User information\n• /roblox2yapin - Find a Roblox user's linked account\n• /yapin2roblox - Find a user's Roblox\n"
        "• /britishuser - Check if a user is British\n• /iteminfo - Item information\n• /placeinfo - Place/game information\n"
        "• /badge - Badge information\n• /badges - Check a user's badges\n• /recentbadges - Latest badges\n• /outfits - A user's outfits\n"
        "• /limiteds - Scan for limiteds\n• /bundle - Bundle information\n• /asset - Asset information\n• /avatar - Avatar\n• /linkroblox - Link your Roblox\n• /robloxverify - Verify with Roblox, hide accounts\n• /robloxpanel - Roblox verification setup (admins)\n• /vortexgame <game ID>, /vortexinfo <user ID>, /vortexcookie - Vortex"), inline=False)
    embed.add_field(name="Utility", value=(
        "• /ping - Latency\n• /invite - Invite link\n• /search - Search The Web\n• /ai - Chat with an AI (/clearai deletes your history)\n• /github - GitHub user\n"
        "• /instagram - Instagram user\n• /status - shapes.lol status\n• /servercount, /userinstalls - Counts\n• /spookpfp - spook.bio"), inline=False)
    embed.add_field(name="Yapper profile", value="• /yapinuser - Info about a user\n• /yapinavatar - Avatar\n• /yapinbanner - Banner", inline=False)
    embed.set_footer(text="Right-click context menus from the Discord bot are not available on Yapper; use the slash commands.")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="countingleaderboard", description="View the counting leaderboard website")
async def countingleaderboard(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Counting Leaderboard",
        description=f"[Click here to view the counting leaderboard]({MainURL}/counting/leaderboard/)",
        color=ecolor(interaction.user.id))
    embed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="invite", description="Get the bot's invite link.")
async def invite(interaction: discord.Interaction):
    embed = discord.Embed(description=f"[Click Here To Add Shapes To Your Server]({InviteURL})", color=ecolor(interaction.user.id))
    embed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
    await interaction.response.send_message(embed=embed)


def _latency_ms():
    """Round trip to the API on a warm (reused) connection, so it measures the network and not the TLS handshake."""
    bot.api("GET", "/me", retries=0)
    start = time.perf_counter()
    bot.api("GET", "/me", retries=0)
    return (time.perf_counter() - start) * 1000


def _quality(ms):
    return "Great" if ms < 100 else "Good" if ms < 200 else "Average" if ms < 400 else "Poor"


@bot.tree.command(name="ping", description="Check the bot's latency.")
async def ping(interaction: discord.Interaction):
    latency = _latency_ms()
    embed = discord.Embed(title="Server Stats", description=f"Latency: `{latency:.2f}ms` ({_quality(latency)} Connection)",
                          color=ecolor(interaction.user.id))
    embed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
    await interaction.response.send_message(embed=embed)
    await asyncio.sleep(2)
    updated = _latency_ms()
    embed = discord.Embed(
        title="Server Stats", color=ecolor(interaction.user.id),
        description=f"OriginalLatency: `{latency:.2f}ms` ({_quality(latency)} Connection)\nEditLatency: `{updated:.2f}ms` ({_quality(updated)} Connection)")
    embed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
    await interaction.edit_original_response(embed=embed, content=None)


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


def _automod_embed(cfg, user, note=None):
    r, a = cfg["rules"], cfg["actions"]
    on = cfg["enabled"]
    embed = discord.Embed(title="Automod", color=discord.Color.green() if on else discord.Color.red(),
                          description=(f"✅ {note}\n\n" if note else "") + (f"**On** · violations are removed and logged" + (f" in <#{cfg['log_channel']}>" if cfg["log_channel"] else "")
                                                                         if on else "**Off** · nothing is being checked"))

    def line(name, label, detail):
        return f"{'🟢' if r[name]['on'] else '⚫'} **{label}** · {detail}"
    embed.add_field(name="Rules", inline=False, value="\n".join([
        line("words", "Words", f"{len(r['words']['list'])} blocked"),
        line("links", "Links", "any link" + (f" except {', '.join(r['links']['allow'][:4])}" + ("…" if len(r["links"]["allow"]) > 4 else "") if r["links"]["allow"] else "")),
        line("invites", "Invites", "links to other servers"),
        line("caps", "Caps", f"{r['caps']['percent']}%+ of {r['caps']['min_length']}+ letters"),
        line("spam", "Spam", f"{r['spam']['count']} messages in {r['spam']['seconds']}s"),
        line("mentions", "Mentions", f"more than {r['mentions']['limit']}"),
    ]))
    steps = ["Message removed" + (" + warning" if a["warn"] else "")]
    if a["timeout_after"]:
        steps.append(f"Timeout {a['timeout_minutes']} min at {a['timeout_after']} strikes")
    if a["kick_after"]:
        steps.append(f"Kick at {a['kick_after']} strikes")
    embed.add_field(name="Punishment", value=" → ".join(steps) + f"\nStrikes expire after {a['strike_minutes']} min", inline=False)
    if cfg["exempt_channels"] or cfg["exempt_roles"]:
        embed.add_field(name="Exempt", inline=False, value=" ".join([f"<#{c}>" for c in cfg["exempt_channels"][:8]] + [f"<@&{x}>" for x in cfg["exempt_roles"][:8]]))
    embed.set_footer(text="Tap a rule to switch it on or off · /automod help for every option · Admins are never checked")
    return embed


def _automod_view(sid, owner_id):
    """Toggle buttons under the panel. Each press saves and redraws the panel."""
    cfg = get_automod(sid)
    view = discord.ui.View(timeout=300)

    def add(label, on, toggle):
        button = discord.ui.Button(label=label, style=discord.ButtonStyle.success if on else discord.ButtonStyle.secondary)

        async def pressed(click):
            if str(click.user.id) != str(owner_id) or not is_admin(sid, click.user.id):
                await click.response.send_message("Only the admin who opened this panel can change it.", ephemeral=True)
                return
            current = get_automod(sid)
            toggle(current)
            current = save_automod(sid, current)
            await click.response.edit_message(embed=_automod_embed(current, click.user), view=_automod_view(sid, owner_id))
        button.callback = pressed
        view.add_item(button)

    def flip_enabled(c):
        c["enabled"] = not c["enabled"]
    add("Turn off" if cfg["enabled"] else "Turn on", False, flip_enabled)
    view.children[-1].style = discord.ButtonStyle.danger if cfg["enabled"] else discord.ButtonStyle.primary
    for name in automod.RULE_NAMES:
        def flip(c, name=name):
            c["rules"][name]["on"] = not c["rules"][name]["on"]
        add(name.capitalize(), cfg["rules"][name]["on"], flip)
    return view


AUTOMOD_HELP = ("**/automod** (admins only)\n"
                "`status` · `enable` · `disable`\n"
                "`rule <words|links|invites|caps|spam|mentions> <on|off> [number]` (number = caps %, spam messages, or mention limit)\n"
                "`words add|remove|list <word>`\n"
                "`allow <domain>` / `unallow <domain>` for the links rule\n"
                "`log <#channel|off>` · `exempt <#channel>` (toggle) · `exemptrole <role>` (toggle)\n"
                "`timeout <strikes> <minutes>` · `kick <strikes|0>`")


@bot.tree.command(name="automod", description="Configure automod: /automod status, enable, rule, words, allow, log, timeout...")
async def automod_command(interaction: discord.Interaction, action: str = "status", a: str = "", b: str = ""):
    if not await need_admin(interaction):
        return
    sid = str(interaction.guild_id)
    cfg = get_automod(sid)
    action = action.lower().strip()

    async def reply(text):
        await interaction.response.send_message(text, ephemeral=True)

    async def panel(note=None):
        await interaction.response.send_message(embed=_automod_embed(get_automod(sid), interaction.user, note),
                                                view=_automod_view(sid, interaction.user.id), ephemeral=True)

    if action in ("status", "panel", ""):
        return await panel()
    if action in ("enable", "disable"):
        cfg["enabled"] = action == "enable"
    elif action == "rule":
        name, state = a.lower(), b.lower()
        if name not in automod.RULE_NAMES or state not in ("on", "off"):
            return await reply("Usage: `/automod rule <words|links|invites|caps|spam|mentions> <on|off>`")
        cfg["rules"][name]["on"] = state == "on"
    elif action == "set":
        name, rest = a.lower(), b
        keys = {"caps": "percent", "spam": "count", "mentions": "limit"}
        if name not in keys or not rest.isdigit():
            return await reply("Usage: `/automod set <caps|spam|mentions> <number>`")
        cfg["rules"][name][keys[name]] = int(rest)
    elif action == "words":
        sub, word = a.lower(), b.strip().lower()
        words = cfg["rules"]["words"]["list"]
        if sub == "list":
            return await reply("Blocked words: " + (", ".join(f"||{w}||" for w in words) if words else "none yet"))
        if sub not in ("add", "remove") or not word:
            return await reply("Usage: `/automod words add|remove|list <word>`")
        if sub == "add" and word not in words:
            words.append(word)
        if sub == "remove" and word in words:
            words.remove(word)
        try:   # the message with the word in it is not left for everyone to read
            bot.delete(interaction.channel_id, interaction._msg.get("id")) if interaction._msg else None
        except Exception:
            pass
    elif action in ("allow", "unallow"):
        domain = a.lower().replace("https://", "").replace("http://", "").strip("/")
        allow = cfg["rules"]["links"]["allow"]
        if not domain:
            return await reply("Usage: `/automod allow <domain>`")
        if action == "allow" and domain not in allow:
            allow.append(domain)
        if action == "unallow" and domain in allow:
            allow.remove(domain)
    elif action == "log":
        if a.lower() in ("off", "none", ""):
            cfg["log_channel"] = None
        else:
            ch = resolve_channel(sid, a)
            if not ch:
                return await reply("I couldn't find that channel.")
            cfg["log_channel"] = str(ch.get("id"))
    elif action == "exempt":
        ch = resolve_channel(sid, a)
        if not ch:
            return await reply("Usage: `/automod exempt <#channel>` (run again to remove it)")
        cid = str(ch.get("id"))
        cfg["exempt_channels"] = [c for c in cfg["exempt_channels"] if c != cid] if cid in cfg["exempt_channels"] else cfg["exempt_channels"] + [cid]
    elif action == "exemptrole":
        try:
            role = resolve_role(a, sid) if a else None
        except ValueError:
            role = None
        if not role:
            return await reply("Usage: `/automod exemptrole <role>` (run again to remove it). I couldn't find that role.")
        rid = str(role.id)
        cfg["exempt_roles"] = [r for r in cfg["exempt_roles"] if r != rid] if rid in cfg["exempt_roles"] else cfg["exempt_roles"] + [rid]
    elif action == "timeout":
        if not (a.isdigit() and b.isdigit()):
            return await reply("Usage: `/automod timeout <strikes> <minutes>` (0 strikes = never time out)")
        cfg["actions"]["timeout_after"], cfg["actions"]["timeout_minutes"] = int(a), max(1, int(b))
    elif action == "kick":
        if not a.isdigit():
            return await reply("Usage: `/automod kick <strikes>` (0 = never kick)")
        cfg["actions"]["kick_after"] = int(a)
    else:
        embed = discord.Embed(title="Automod help", color=ecolor(interaction.user.id), description=AUTOMOD_HELP)
        return await interaction.response.send_message(embed=embed, ephemeral=True)
    save_automod(sid, cfg)
    await panel("Saved.")


@bot.tree.command(name="serverinfo", description="Show information about this server")
async def serverinfo(interaction: discord.Interaction):
    if interaction.is_dm:
        await interaction.response.send_message("This command only works inside a server.", ephemeral=True)
        return
    sid = str(interaction.guild_id)
    try:
        data = server_summary(sid)
    except Exception as e:
        await interaction.response.send_message(f"Couldn't load the server information: {e}", ephemeral=True)
        return
    embed = discord.Embed(title=data["name"], color=ecolor(interaction.user.id))
    if data["description"]:
        embed.description = data["description"]
    embed.add_field(name="Owner", value=f"<@{data['owner']}>" if data["owner"] else "Unknown", inline=True)
    embed.add_field(name="Server ID", value=f"`{sid}`", inline=True)
    embed.add_field(name="Members", inline=False,
                    value=f"**{data['members']:,}** total (people and bots)\n**{data['humans']:,}** people · **{data['bots']:,}** bots")
    embed.add_field(name="Channels", value=f"{data['channels']:,} total\n{data['text']:,} text · {data['voice']:,} voice\n{data['categories']:,} categories", inline=True)
    embed.add_field(name="Roles", value=f"{data['roles']:,}", inline=True)
    for label, value in data["extra"]:
        embed.add_field(name=label, value=value, inline=True)
    if data["icon"]:
        embed.set_thumbnail(url=data["icon"])
    if data["banner"]:
        embed.set_image(url=data["banner"])
    elif data["icon"]:
        embed.set_image(url=data["icon"])      # Yapin gives servers no banner, so the picture is shown large instead
    embed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
    await interaction.response.send_message(embed=embed)


def server_summary(sid):
    """What /serverinfo shows, as plain data (the dashboard shows the same numbers)."""
    info = dict(bot.server(sid))
    channels = bot.channels(sid)
    roles = bot.roles(sid)
    members = cached(f"allmembers:{sid}", 120, lambda: _all_members(sid))
    bots = sum(1 for m in members if m["bot"])
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
LASTFM_LINK_TTL = 600


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


@bot.tree.command(name="lastfm", description="Link your Last.fm account or see what someone is listening to")
async def lastfm(interaction: discord.Interaction, action: str = "", user: discord.User = None):
    action = (action or "").strip()
    uid = interaction.user.id
    if action.lower() not in ("", "link", "unlink") and user is None:
        # "/lastfm @someone" (or a bare user ID) lands in the first slot, so read it as the person to show
        try:
            user = resolve_user(action)
        except ValueError:
            user = None
        if user is None:
            await interaction.response.send_message("I couldn't find that person. Ping them, or use `/lastfm link` or `/lastfm unlink`.", ephemeral=True)
            return
        action = ""
    action = action.lower()
    if action == "link":
        started = await asyncio.to_thread(shapes_api, "POST", "/internal/link/start",
                                          {"provider": "lastfm", "yapin_id": str(uid), "yapin_name": interaction.user.name})
        link = (started or {}).get("url")
        if not link:
            reason = (started or {}).get("data") or "Couldn't reach the linking service. Try again in a moment."
            await interaction.response.send_message(str(reason), ephemeral=True)
            return
        view = discord.ui.View(timeout=LASTFM_LINK_TTL)
        view.add_item(discord.ui.Button(label="Authorize on Last.fm", style=discord.ButtonStyle.link, url=link))
        await interaction.response.send_message("Tap the button, approve access on Last.fm and you're linked. The link works for 10 minutes and only for you.", view=view, ephemeral=True)
        return
    if action == "unlink":
        had = await asyncio.to_thread(get_lastfm, uid)
        await asyncio.to_thread(shapes_api, "DELETE", f"/internal/links/{uid}/lastfm")
        await interaction.response.send_message("Unlinked your Last.fm account." if had else "You had no Last.fm account linked.", ephemeral=True)
        return
    target = user or interaction.user
    linked = await asyncio.to_thread(get_lastfm, target.id)
    if not linked:
        who = "You haven't" if target.id == uid else f"{target.display_name} hasn't"
        await interaction.response.send_message(f"{who} linked a Last.fm account. Use `/lastfm link`.", ephemeral=True)
        return
    p = await asyncio.to_thread(lastfm_profile, linked)
    if not p:
        await interaction.response.send_message(f"Couldn't reach Last.fm for `{linked}` right now.", ephemeral=True)
        return
    embed = discord.Embed(title=f"{target.display_name} on Last.fm", description=lastfm_line(p), color=ecolor(uid))
    if p["image"]:
        embed.set_thumbnail(url=p["image"])
    embed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
    await interaction.response.send_message(embed=embed)


# --- Yapper profile commands (/yapinuser, /yapinavatar, /yapinbanner; these were the Discord user/avatar/banner commands) ---

def _profile_target(interaction, user):
    """The user to look up. Fills in the full profile from Yapper so avatar/banner/bio are available."""
    target = user or interaction.user
    data = fetch_user(target.id)
    if data:
        target.data = {**target.data, **{k: v for k, v in data.items() if v not in (None, "")}}   # the full profile wins
        target.name = _first(data, "username", "name") or target.name
        target.display_name = _first(data, "display_name", "displayName") or target.display_name
    return target


def _mutual_servers(user_id):
    """Names of the servers the bot shares with this person (looked up side by side, one request per server)."""
    from concurrent.futures import ThreadPoolExecutor
    servers = list(cached("servers", 15, bot.servers))

    def check(s):
        try:
            bot.member(s.get("id"), user_id)
            return s.get("name") or str(s.get("id"))
        except Exception:
            return None      # not a member of that server
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            return [n for n in pool.map(check, servers) if n]
    except Exception as e:
        print(f"mutual server lookup failed: {e}")
        return []


@bot.tree.command(name="yapinuser", description="Get information about a user")
async def yapinuser(interaction: discord.Interaction, user: discord.User = None):
    target = _profile_target(interaction, user)
    handle = f"@{target.name}"
    embed = discord.Embed(title=target.display_name + ("  ·  🤖 Bot" if target.is_bot else ""), color=ecolor(interaction.user.id))
    bio = _first(target.data, "bio", "about", "description")
    lines = [f"{handle}" + (f" · {target.pronouns}" if target.pronouns else "")]
    if bio:
        lines.append("> " + str(bio).strip().replace("\n", "\n> ")[:400])
    embed.description = "\n".join(lines)
    created = target.created_at
    embed.add_field(name="Joined Yapin", value=f"<t:{int(created.timestamp())}:D>\n<t:{int(created.timestamp())}:R>" if created else "Unknown", inline=True)
    embed.add_field(name="ID", value=f"`{target.id}`", inline=True)
    if target.badges:
        embed.add_field(name="Badges", value="  ".join(f"`{b}`" for b in target.badges[:10]), inline=False)
    mutual = _mutual_servers(target.id)
    if mutual:
        shown = "\n".join(f"• {n}" for n in mutual[:8]) + (f"\n…and {len(mutual) - 8} more" if len(mutual) > 8 else "")
        embed.add_field(name=f"Servers with Shapes ({len(mutual)})", value=shown, inline=False)
    linked = await asyncio.to_thread(get_lastfm, target.id)
    if linked:
        p = await asyncio.to_thread(lastfm_profile, linked)
        embed.add_field(name="Last.fm", value=lastfm_line(p) if p else f"[{linked}](https://www.last.fm/user/{linked})", inline=False)
    if target.avatar_url:
        embed.set_thumbnail(url=target.avatar_url)
    if target.banner_url:
        embed.set_image(url=target.banner_url)
    embed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="yapinavatar", description="Get a user's avatar")
async def yapinavatar(interaction: discord.Interaction, user: discord.User = None):
    target = _profile_target(interaction, user)
    print(str(target))
    url = target.avatar_url
    if not url:
        await interaction.response.send_message(f"{target.name} doesn't have an avatar.")
        return
    embed = discord.Embed(title=f"{target.name}'s Avatar", color=ecolor(interaction.user.id), timestamp=interaction.created_at)
    embed.set_image(url=url)
    embed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="Download Avatar", style=discord.ButtonStyle.link, url=url))
    await interaction.response.send_message(embed=embed, view=view)


@bot.tree.command(name="yapinbanner", description="Get a user's banner")
async def yapinbanner(interaction: discord.Interaction, user: discord.User = None):
    target = _profile_target(interaction, user)
    url = target.banner_url
    if not url:
        await interaction.response.send_message(f"{target.name} doesn't have a banner image.")
        return
    embed = discord.Embed(title=f"{target.name}'s Banner", color=ecolor(interaction.user.id), timestamp=interaction.created_at)
    embed.set_image(url=url)
    embed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="Download Banner", style=discord.ButtonStyle.link, url=url))
    await interaction.response.send_message(embed=embed, view=view)



# =====================================================================================
# Commands carried over from the Discord bot
# =====================================================================================


# --- spookpfp ---
@bot.tree.command(name="spookpfp", description="Get a pfp from a user's spook.bio profile.")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def spookpfp(interaction: discord.Interaction, username: str = "leandre"):
    url = f"https://spook-v3.onrender.com/$/api/v2/users/{username}"
    response = requests.get(url)
    data = response.json()
    if response.status_code == 200:
        await interaction.response.send_message(data.get('Picture_URL') or f":x: {username} has no profile picture :x:", ephemeral=False)
        print("Fetched data successfully!")
    else:
        await interaction.response.send_message(f":x: {username} Not Found :x:", ephemeral=False)
        print(f"Error fetching data: {response.status_code}")


# --- roblox2yapin ---
@bot.tree.command(name="roblox2yapin", description="Get a Roblox user's linked Yapin account from their username.")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def roblox2yapin(interaction: discord.Interaction, user: str):
    await interaction.response.defer()

    #print(f"Searching For {user}")
    thinkingembed = discord.Embed(
        title=f"{Emojis.get('loading')} {interaction.user.mention} Searching For {user}!",
        color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
    )
    await interaction.followup.send(embed=thinkingembed)

    url = "https://users.roblox.com/v1/usernames/users"
    # print(f"Fetching Data From {url}")

    request_payload = {
        "usernames": [user],
        "excludeBannedUsers": True
    }

    try:
        response = requests.post(url, json=request_payload)
        response.raise_for_status()
        data = response.json()
        if data.get("data") and len(data["data"]) > 0:
            userinfo = data["data"][0]
            UserID = userinfo["id"]
            Display = userinfo["displayName"]
            user = userinfo["name"]
            if user == Display:
                Username = f"@{user}"
            else:
                Username = f"{Display} (@{user})"
            #print(f"UserInfo: {userinfo}")
        else:
            #print(f"{user} not found.")
            failedembed7 = discord.Embed(
                title=f":warning: {user} not found.",
                color=discord.Color.yellow()
            )
            await interaction.edit_original_response(embed=failedembed7)
            return

    except requests.exceptions.RequestException as e:
        #print(f"An error occurred during the API request: {e}")
        failedembed8 = discord.Embed(
            title=f":warning: {user} not found.",
            color=discord.Color.yellow()
        )
        await interaction.edit_original_response(embed=failedembed8)
        return

    found = await asyncio.to_thread(yapin_of_roblox, UserID)
    color = embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
    if found is None and not api_ready():
        await interaction.edit_original_response(embed=discord.Embed(title="Linking isn't set up on this bot yet", color=discord.Color.red()))
        return
    if found is None:
        await interaction.edit_original_response(embed=discord.Embed(
            title=f"No Yapin user found for {Username}",
            description="They haven't linked their Roblox account with `/linkroblox`.", color=discord.Color.red()))
        return
    yapin_id, hidden = found
    if hidden or not yapin_id:
        await interaction.edit_original_response(embed=discord.Embed(
            title=f"{Username} has hidden their Yapin link", color=discord.Color.red()))
        return
    person = await asyncio.to_thread(fetch_user, yapin_id)
    handle = f"@{person.get('username')}" if person.get("username") else yapin_id
    embed = discord.Embed(title=f"Yapin user for {Username}", description=f"**{person.get('display_name') or handle}** ({handle})", color=color)
    embed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
    await interaction.edit_original_response(embed=embed)


# --- yapin2roblox ---
@bot.tree.command(name="yapin2roblox", description="Get a Roblox profile from a Yapin user.")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def yapin2roblox(interaction: discord.Interaction, user: discord.User):
    userid = user.id
    await interaction.response.defer(thinking=True)

    loading = discord.Embed(
        title=f"{Emojis.get('loading')} {interaction.user.mention} Getting Roblox Profile For {user.name}",
        color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
    )

    await interaction.followup.send(embed=loading)

    found = await asyncio.to_thread(roblox_of_yapin, userid)
    color = embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
    if found is None:
        text = "Linking isn't set up on this bot yet" if not api_ready() else f"No Roblox Profile found for {user.name}"
        await interaction.edit_original_response(embed=discord.Embed(
            title=text, description="" if not api_ready() else "They haven't linked their Roblox account with `/linkroblox`.", color=discord.Color.red()))
        return
    roblox_id, roblox_name, hidden = found
    if hidden or not roblox_id:
        await interaction.edit_original_response(embed=discord.Embed(title=f"{user.name} has hidden their Roblox link", color=discord.Color.red()))
        return
    embed = discord.Embed(title=f"Roblox Profile for {user.name}", color=color,
                          description=f"[{roblox_name or roblox_id}](https://www.roblox.com/users/{roblox_id}/profile)")
    embed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
    await interaction.edit_original_response(embed=embed)


# --- ai ---
@bot.tree.command(name="clearai", description="Delete your AI chat history")
async def clearai(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    had = AI_DB.exists(user_id)
    AI_DB.remove(user_id)
    await interaction.response.send_message("Your AI chat history was deleted." if had else "You don't have any AI chat history.", ephemeral=True)


@bot.tree.command(name="ai", description="Chat with an AI assistant.")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def ai(interaction: discord.Interaction, *, prompt: str):
    await interaction.response.defer(thinking=True)

    loading = discord.Embed(
    title=f"{Emojis.get('loading')} {interaction.user.mention} Getting AI Response For: {prompt}",
    color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
)

    await interaction.followup.send(embed=loading)

    user_id = str(interaction.user.id)
    username = interaction.user.name

    # Load or initialize user data
    user_data = AI_DB.get(user_id) or {"username": username, "user_messages": [], "ai_responses": []}
    user_data["username"] = username
    user_data["user_messages"].append(prompt)

    # Keep last 50 messages
    user_data["user_messages"] = user_data["user_messages"][-50:]
    user_data["ai_responses"] = user_data["ai_responses"][-50:]

    # System instructions for the AI
    messages_for_ai = [
        {
            "role": "system",
            "content": (
                f"You are a helpful Yapper assistant chatting with {username}. "
                "Always respond in a single concise paragraph. Otherwise your response will not be recieved due to the embed text limit"
                "Follow Yapper's rules. Do not provide instructions for illegal activity. "
                "Stay safe, respectful, and friendly."
                "Your name is Shapes, the support server is https://yapin.gg/invite/shapes if a user requests it or if you think the prompt is support related."
                "Please stay away from nsfw/bad topics, it will get you deleted/banned."
                "You're created/owned by Spooky Inc. with leandre as your main developer."
            )
        }
    ]

    # Include previous conversation
    for u_msg, a_msg in zip(user_data["user_messages"], user_data["ai_responses"]):
        messages_for_ai.append({"role": "user", "content": u_msg[:1024]})
        messages_for_ai.append({"role": "assistant", "content": a_msg[:1024]})

    # Add latest message
    messages_for_ai.append({"role": "user", "content": prompt})

    try:
        response = chatgpt.chat.completions.create(
            model=AIModel,
            messages=messages_for_ai
        )
        ai_reply = response.choices[0].message.content.strip()
    except Exception as e:
        ai_reply = f"⚠️ API error: {e}"

    # Save AI response
    user_data["ai_responses"].append(ai_reply)
    AI_DB.set(user_id, user_data)
    AI_DB.save()

    # Create embed
    embed = discord.Embed(
        title=f"💬 Chat with {username}",
        color=embedDB.get(user_id) or discord.Color.blue()
    )
    embed.add_field(name="🧍 You said:", value=prompt, inline=False)
    embed.add_field(name="🤖 AI replied:", value=ai_reply, inline=False)
    embed.set_footer(text=f"Requested by {username} | {MainURL}")

    await interaction.edit_original_response(embed=embed)


# --- search ---
# Google is retiring the Custom Search JSON API (discontinued January 1, 2027) and has no whole-web replacement.
# /search now uses the Brave Search API (BRAVE_API_KEY). If only the old GOOGLE_API_KEY is set, it keeps using
# Google's API until that shuts off. Adding another provider = one more function in SEARCH_PROVIDERS.
BraveAPIKey = _secret("BRAVE_API_KEY", "BraveToken.txt")


def _clean_snippet(text):
    text = re.sub(r"<[^>]+>", "", text or "")
    return re.sub(r"&(?:amp|quot|#x27|#39|lt|gt);", lambda m: {"&amp;": "&", "&quot;": '"', "&lt;": "<", "&gt;": ">"}.get(m.group(0), "'"), text).strip()


async def _search_brave(session, query):
    async with session.get(
        "https://api.search.brave.com/res/v1/web/search",
        params={"q": query, "count": 5},
        headers={"X-Subscription-Token": BraveAPIKey, "Accept": "application/json"},
        timeout=aiohttp.ClientTimeout(total=15),
    ) as resp:
        resp.raise_for_status()
        data = await resp.json()
    return [{"title": r.get("title") or "No Title", "link": r.get("url") or "", "snippet": _clean_snippet(r.get("description"))}
            for r in (data.get("web") or {}).get("results", [])[:5]]


async def _search_google_cse(session, query):
    async with session.get(
        "https://www.googleapis.com/customsearch/v1",
        params={"key": GoogleAPIKey, "cx": searchengine, "q": query, "num": 5},
        timeout=aiohttp.ClientTimeout(total=15),
    ) as resp:
        resp.raise_for_status()
        data = await resp.json()
    return [{"title": r.get("title") or "No Title", "link": r.get("link") or "", "snippet": _clean_snippet(r.get("snippet"))}
            for r in data.get("items", [])[:5]]


def _search_providers():
    """Providers that have a key set, in order of preference."""
    found = []
    if BraveAPIKey:
        found.append(("Brave", _search_brave))
    if GoogleAPIKey:
        found.append(("Google", _search_google_cse))
    return found


@bot.tree.command(name="search", description="Search the web.")
async def search(interaction: discord.Interaction, query: str = "Yapin'"):
    await interaction.response.defer()

    thinkingembed = discord.Embed(
        title=f"{Emojis.get('loading')} {interaction.user.mention} Searching For {query}",
        color=ecolor(interaction.user.id)
    )
    await interaction.followup.send(embed=thinkingembed)

    more = f"https://www.google.com/search?q={urllib.parse.quote_plus(query)}"
    providers = _search_providers()
    results, used, failed = None, None, False
    async with aiohttp.ClientSession() as session:
        for name, fn in providers:
            try:
                results, used = await fn(session, query), name
                break
            except Exception as e:
                failed = True
                print(f"[google] {name} search failed: {type(e).__name__}: {e}")

    if results:
        lines = [f"**{i}. [{r['title']}]({r['link']})**\n{r['snippet']}" for i, r in enumerate(results, 1) if r["link"]]
        embed = discord.Embed(
            title=f"Search Results For {query}",
            description="\n\n".join(lines) + f"\n\n[Search For More Results]({more})",
            color=ecolor(interaction.user.id)
        )
        embed.set_footer(text=f"Requested By {interaction.user.name} via {used} | {MainURL}")
        await interaction.edit_original_response(embed=embed)
        return

    if not providers:
        title, desc = ":x: Search isn't set up :x:", f"The bot owner needs to set BRAVE_API_KEY. [Search it yourself]({more})"
    elif failed:
        title, desc = ":x: An error occurred while searching. Please try again later. :x:", f"[Search it yourself]({more})"
    else:
        title, desc = ":x: No results found! :x:", f"No Results for {query} | [Search on Google Yourself For More Results]({more})"
    embed = discord.Embed(title=title, description=desc, color=discord.Color.red())
    embed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
    await interaction.edit_original_response(embed=embed)


# --- robloxinfo ---
@bot.tree.command(name="robloxinfo", description="Get a Roblox user's profile information.")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def robloxinfo(interaction: discord.Interaction, user: str = "Roblox"):
    await interaction.response.defer(thinking=True)

    print(f"Searching For {user}'s profile")
    thinkingembed = discord.Embed(
        title=f"{Emojis.get('loading')} {interaction.user.mention} Searching For {user}'s Roblox profile!",
        color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
    )
    await interaction.followup.send(embed=thinkingembed)

    url = "https://users.roblox.com/v1/usernames/users"

    request_payload = {
        "usernames": [user],
        "excludeBannedUsers": False
    }

    async def check_ownership(session: aiohttp.ClientSession, user_id: str, asset_id: str) -> bool:
        url = f"https://inventory.roblox.com/v1/users/{user_id}/items/0/{asset_id}/is-owned"
        try:
            async with session.get(url, headers={
                'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36',
                'accept': 'application/json',
            }) as response:
                data = await response.json()
                return data is True
        except Exception:
            return False

    async def check_verification_items(session: aiohttp.ClientSession, user_id: str) -> bool:
        items = ['1567446', '18824203', '93078560', '102611803']   # the verified sign, then the hats
        results = await asyncio.gather(*(check_ownership(session, user_id, item) for item in items))
        return any(results)

    async def check_inventory_visibility(session: aiohttp.ClientSession, user_id: str) -> str:
        try:
            inventory_url = f"https://inventory.roblox.com/v1/users/{user_id}/can-view-inventory"
            async with session.get(inventory_url) as response:
                if response.status == 200:
                    inventory_data = await response.json()
                    return "Public" if inventory_data.get('canView', False) else "Private"
        except Exception as e:
            print(f"Error checking inventory visibility: {e}")
        return "Private"

    async def check_presence(session: aiohttp.ClientSession, user_id: str) -> tuple:
        try:
            try:
                with open('roblosecuritytoken.txt', 'r') as f:
                    roblosecurity_token = f.read().strip()
            except FileNotFoundError:
                return (None, None)

            presence_url = 'https://presence.roblox.com/v1/presence/users'
            headers = {
                "Content-Type": "application/json",
                "Cookie": f".ROBLOSECURITY={roblosecurity_token}"
            }

            async with session.post(presence_url, headers=headers, json={'userIds': [user_id]}) as response:
                if response.status == 200:
                    presence_data = await response.json()
                    if presence_data.get('userPresences') and len(presence_data['userPresences']) > 0:
                        user_presence = presence_data['userPresences'][0]
                        presence_type = user_presence.get('userPresenceType', 0)
                        place_id = user_presence.get('placeId')
                        return (presence_type, place_id)
        except Exception as e:
            print(f"Error checking presence: {e}")
        return (None, None)

    class BadgeService:
        def __init__(self, session: aiohttp.ClientSession):
            self.session = session

        async def get_user_badges(self, user_id: str):
            try:
                url = f"https://badges.roblox.com/v1/users/{user_id}/badges?sortOrder=Desc&limit=10"
                async with self.session.get(url) as response:
                    if response.status == 200:
                        data = await response.json()
                        return data.get('data', []), 200
                    return [], response.status
            except Exception as e:
                print(f"Error getting user badges: {e}")
                return [], 500

        async def get_awarded_dates(self, user_id: str, badge_ids: list):
            try:
                if not badge_ids:
                    return {}

                badge_ids_str = ",".join(map(str, badge_ids))
                url = f"https://badges.roblox.com/v1/users/{user_id}/badges/awarded-dates?badgeIds={badge_ids_str}"
                async with self.session.get(url) as response:
                    if response.status == 200:
                        data = await response.json()
                        awarded_dates = {}
                        for item in data.get('data', []):
                            awarded_dates[item['badgeId']] = item.get('awardedDate')
                        return awarded_dates
                    return {}
            except Exception as e:
                print(f"Error getting awarded dates: {e}")
                return {}

    class BadgeFormatter:
        @staticmethod
        def parse_iso_timestamp(iso_timestamp: str) -> int:
            try:
                if iso_timestamp:
                    dt = datetime.fromisoformat(iso_timestamp.replace('Z', '+00:00'))
                    return int(dt.timestamp())
            except (ValueError, AttributeError):
                pass
            return None

    async def get_last_online_from_badges(session: aiohttp.ClientSession, user_id: str) -> tuple:
        try:
            badge_service = BadgeService(session)
            badges, status_code = await badge_service.get_user_badges(user_id)

            if status_code == 200 and badges:
                badge_ids = [badge['id'] for badge in badges]
                awarded_dates = await badge_service.get_awarded_dates(user_id, badge_ids)

                for badge in badges:
                    badge_id = badge['id']
                    badge['awardedDate'] = awarded_dates.get(badge_id)

                valid_timestamps = []
                latest_place_id = None

                for badge in badges:
                    awarded_date = badge.get('awardedDate')
                    if awarded_date:
                        unix_timestamp = BadgeFormatter.parse_iso_timestamp(awarded_date)
                        if unix_timestamp:
                            valid_timestamps.append((unix_timestamp, badge))

                if valid_timestamps:
                    valid_timestamps.sort(key=lambda x: x[0], reverse=True)
                    latest_timestamp, latest_badge = valid_timestamps[0]

                    awarder = latest_badge.get('awarder', {})
                    if awarder.get('type') == 'Place':
                        latest_place_id = awarder.get('id')

                    return (latest_timestamp, latest_place_id)
        except Exception as e:
            print(f"Error fetching badge last online: {e}")
        return (None, None)

    async def get_friends_count(session: aiohttp.ClientSession, user_id: str) -> int:
        try:
            friends_url = f"https://friends.roblox.com/v1/users/{user_id}/friends/count"
            async with session.get(friends_url) as response:
                if response.status == 200:
                    friends_data = await response.json()
                    return friends_data.get('count', 0)
        except Exception as e:
            print(f"Error fetching friends count: {e}")
        return None

    async def get_followers_count(session: aiohttp.ClientSession, user_id: str) -> int:
        try:
            followers_url = f"https://friends.roblox.com/v1/users/{user_id}/followers/count"
            async with session.get(followers_url) as response:
                if response.status == 200:
                    followers_data = await response.json()
                    return followers_data.get('count', 0)
        except Exception as e:
            print(f"Error fetching followers count: {e}")
        return None

    async def get_followings_count(session: aiohttp.ClientSession, user_id: str) -> int:
        try:
            followings_url = f"https://friends.roblox.com/v1/users/{user_id}/followings/count"
            async with session.get(followings_url) as response:
                if response.status == 200:
                    followings_data = await response.json()
                    return followings_data.get('count', 0)
        except Exception as e:
            print(f"Error fetching followings count: {e}")
        return None

    async def get_user_games_visits(session: aiohttp.ClientSession, user_id: str) -> int:
        try:
            user_games_url = f"https://games.roblox.com/v2/users/{user_id}/games?accessFilter=2&limit=50"
            async with session.get(user_games_url) as response:
                if response.status == 200:
                    games_data = await response.json()
                    total_visits = 0

                    if games_data.get('data'):
                        games_list = games_data.get('data', [])
                        universe_ids = [game.get('id') for game in games_list if game.get('id') is not None]

                        if universe_ids:
                            visit_tasks = []
                            chunk_size = 50
                            for i in range(0, len(universe_ids), chunk_size):
                                chunk = universe_ids[i:i + chunk_size]
                                universe_ids_str = ",".join(map(str, chunk))
                                games_info_url = f"https://games.roblox.com/v1/games?universeIds={universe_ids_str}"
                                visit_tasks.append(session.get(games_info_url))

                            visit_responses = await asyncio.gather(*visit_tasks)
                            for response in visit_responses:
                                if response.status == 200:
                                    result = await response.json()
                                    for game in result.get('data', []):
                                        total_visits += game.get('visits', 0) or 0

                    return total_visits
        except Exception as e:
            print(f"Error fetching user games visits: {e}")
        return None

    async def get_profile_api_data(session: aiohttp.ClientSession, user_id: str) -> dict:
        try:
            with open('roblosecuritytoken.txt', 'r') as f:
                roblosecurity_token = f.read().strip()

            url = "https://apis.roblox.com/profile-platform-api/v1/profiles/get"
            payload = {
                "profileId": str(user_id),
                "profileType": "User",
                "components": [
                    {"component": "UserProfileHeader"},
                    {"component": "About"},
                    {"component": "RobloxBadges"},
                    {"component": "Statistics"}
                ],
                "includeComponentOrdering": True
            }

            headers = {
                "Content-Type": "application/json",
                "Cookie": f".ROBLOSECURITY={roblosecurity_token}",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            }

            async with session.post(url, json=payload, headers=headers) as response:
                if response.status == 200:
                    data = await response.json()
                    components = data.get('components', {})

                    profile_data = {}

                    user_profile_header = components.get('UserProfileHeader', {}) or {}
                    profile_data['is_premium'] = user_profile_header.get('isPremium', False)
                    profile_data['is_verified'] = user_profile_header.get('isVerified', False)

                    counts = user_profile_header.get('counts', {}) or {}
                    profile_data['friends_count'] = counts.get('friendsCount', 0)
                    profile_data['followers_count'] = counts.get('followersCount', 0)
                    profile_data['followings_count'] = counts.get('followingsCount', 0)

                    statistics = components.get('Statistics', {}) or {}
                    profile_data['visits'] = statistics.get('numberOfVisits', 0)
                    profile_data['join_date'] = statistics.get('userJoinedDate', '')

                    about = components.get('About', {}) or {}
                    profile_data['description'] = about.get('description', '')

                    roblox_badges = components.get('RobloxBadges', {}) or {}
                    badge_list = roblox_badges.get('robloxBadgeList', [])

                    excluded_badges = {'Homestead', 'Bricksmith'}
                    valid_badges = []

                    for badge in badge_list:
                        badge_type = badge.get('type', {})
                        badge_name = badge_type.get('value', '')

                        if badge_name not in excluded_badges:
                            valid_badges.append(badge)

                    profile_data['last_online'] = None
                    if valid_badges:
                        latest_timestamp = 0
                        for badge in valid_badges:
                            created_time = badge.get('createdTime', {})
                            seconds = created_time.get('seconds', 0)
                            if seconds > latest_timestamp:
                                latest_timestamp = seconds

                        if latest_timestamp > 0:
                            profile_data['last_online'] = latest_timestamp

                    return profile_data

        except FileNotFoundError:
            pass   # no .ROBLOSECURITY cookie file: the public endpoints fill the numbers in below
        except Exception as e:
            print(f"Error fetching profile API data: {e}")

        return {
            'visits': None,
            'friends_count': None,
            'followers_count': None,
            'followings_count': None,
            'is_verified': False,
            'is_premium': False,
            'description': '',
            'join_date': '',
            'last_online': None
        }

    def load_cached_timestamps():
        try:
            with open('lastonline.json', 'r') as f:
                return json.load(f)
        except FileNotFoundError:
            return {}

    def update_cached_timestamp(user_id, timestamp):
        try:
            cached_timestamps = load_cached_timestamps()
            cached_timestamps[str(user_id)] = timestamp
            with open('lastonline.json', 'w') as f:
                json.dump(cached_timestamps, f, indent=2)
        except Exception as e:
            print(f"Error updating cached timestamp: {e}")

    try:
        response = requests.post(url, json=request_payload)
        response.raise_for_status()
        data = response.json()
        if data.get("data") and len(data["data"]) > 0:
            userinfo = data["data"][0]
            UserID = userinfo["id"]
            Display = userinfo["displayName"]
            hasVerifiedBadge = userinfo.get("hasVerifiedBadge", False)

            profile_data = {'last_online': None}
            rap_value = None
            value_value = None
            rolimons_last_online = None
            badge_last_online = None
            badge_place_id = None
            is_premium = False
            is_verified = False
            roblox_badges = []
            total_visits = None
            inventory_visibility = "Private"
            friends_count = None
            followers_count = None
            followings_count = None
            presence_type = None
            current_place_id = None
            profile_description = ""

            try:
                connector = aiohttp.TCPConnector(family=socket.AF_INET)
                async with aiohttp.ClientSession(connector=connector) as session:
                    profile_data = await get_profile_api_data(session, UserID)

                    is_premium = profile_data['is_premium']
                    is_verified = profile_data['is_verified']
                    friends_count = profile_data['friends_count']
                    followers_count = profile_data['followers_count']
                    followings_count = profile_data['followings_count']
                    total_visits = profile_data['visits']
                    profile_description = profile_data['description']

                    # Without the profile API (no cookie) fall back to the public endpoints for every user.
                    if None in (friends_count, followers_count, followings_count, total_visits):
                        pub = await asyncio.gather(get_friends_count(session, UserID), get_followers_count(session, UserID),
                                                   get_followings_count(session, UserID), get_user_games_visits(session, UserID))
                        friends_count = pub[0] if friends_count is None else friends_count
                        followers_count = pub[1] if followers_count is None else followers_count
                        followings_count = pub[2] if followings_count is None else followings_count
                        total_visits = pub[3] if total_visits is None else total_visits

                    tasks = [
                        check_verification_items(session, UserID),
                        check_inventory_visibility(session, UserID),
                        check_presence(session, UserID),
                        get_last_online_from_badges(session, UserID)
                    ]

                    results = await asyncio.gather(*tasks)
                    is_verified = results[0]
                    inventory_visibility = results[1]
                    presence_result = results[2]
                    presence_type = presence_result[0]
                    current_place_id = presence_result[1]
                    badge_result = results[3]
                    badge_last_online = badge_result[0]
                    badge_place_id = badge_result[1]

                    badges_url = f"https://accountinformation.roblox.com/v1/users/{UserID}/roblox-badges"
                    async with session.get(badges_url) as response:
                        if response.status == 200:
                            badges_data = await response.json()
                            roblox_badges = badges_data

                    rolimons_stats_url = f"https://api.rolimons.com/players/v1/playerinfo/{UserID}"
                    async with session.get(rolimons_stats_url, headers={'User-Agent': 'our-websites.github.io/shapes.lol/yapin'}) as response:
                        if response.status == 200:
                            rolimons_stats_data = await response.json()
                            if rolimons_stats_data.get('success'):
                                rap_value = rolimons_stats_data.get('rap') or 0
                                value_value = rolimons_stats_data.get('value') or 0
                                rolimons_last_online = rolimons_stats_data.get('last_online')
            except Exception as e:
                print(f"Error fetching data: {e}")

            rolimonsurl = f"https://rolimons.com/player/{UserID}"

            url = f"https://users.roblox.com/v1/users/{UserID}"
            try:
                response = requests.get(url)
                response.raise_for_status()
                playerdata = response.json()
                Description = profile_description or playerdata["description"]
                Banned = playerdata["isBanned"]
                user = playerdata["name"]
                JoinDate = playerdata["created"]
                created_timestamp = isotodiscordtimestamp(JoinDate)
            except requests.exceptions.RequestException as e:
                print(f"Error fetching user data for ID {UserID}: {e}")
                failedembed = discord.Embed(
                    title=f":x: An error occurred while fetching data for user ID: {UserID}. Please try again later.",
                    color=discord.Color.red()
                )
                await interaction.edit_original_response(embed=failedembed)
                return

            if Display == user:
                Username = Display
            else:
                Username = f"{Display} (@{user})"

            if UserID == 124767284:
                hasVerifiedBadge = True

            if hasVerifiedBadge:
                Username += " <:RobloxVerified:1416951927513677874>"

            if is_premium:
                Username += " <:RobloxPremium:1416951078200541378>"

            is_terminated = False
            avatar_image = None

            try:
                avatar_url = f"https://thumbnails.roblox.com/v1/users/avatar?userIds={UserID}&size=420x420&format=Png&isCircular=false"
                response = await asyncio.to_thread(requests.get, avatar_url, timeout=10)
                response.raise_for_status()
                data = response.json()
                if data and data.get("data") and len(data["data"]) > 0:
                    avatar_image = data["data"][0].get("imageUrl")
                    if avatar_image and avatar_image.startswith("https://t7.rbxcdn.com"):
                        is_terminated = True

                        connector = aiohttp.TCPConnector(family=socket.AF_INET)
                        async with aiohttp.ClientSession(connector=connector) as session:
                            custom_avatar = await render_custom_avatar(session, UserID)
                            if custom_avatar:
                                avatar_image = custom_avatar
            except requests.exceptions.RequestException as e:
                print(f"Error fetching avatar: {e}")

            if is_terminated:
                Username = f":warning: [Banned] {Username}"

                try:
                    connector = aiohttp.TCPConnector(family=socket.AF_INET)
                    async with aiohttp.ClientSession(connector=connector) as session:
                        terminated_tasks = [
                            get_friends_count(session, UserID),
                            get_followers_count(session, UserID),
                            get_followings_count(session, UserID),
                            get_user_games_visits(session, UserID)
                        ]

                        terminated_results = await asyncio.gather(*terminated_tasks)
                        friends_count = terminated_results[0]
                        followers_count = terminated_results[1]
                        followings_count = terminated_results[2]
                        total_visits = terminated_results[3]
                except Exception as e:
                    print(f"Error fetching terminated user data: {e}")

            Discord = None
            owner = await asyncio.to_thread(owner_of_roblox, UserID)       # hidden accounts come back empty
            if owner.get("yapin_id"):
                person = await asyncio.to_thread(fetch_user, owner["yapin_id"])
                Discord = f"@{person['username']}" if person.get("username") else owner["yapin_id"]
            if owner.get("discord_id"):
                Discord = f"{Discord}\nDiscord: {owner['discord_id']}" if Discord else f"Discord: {owner['discord_id']}"

            profileurl = f"https://www.roblox.com/users/{UserID}/profile"

            cached_timestamps = load_cached_timestamps()
            cached_timestamp = cached_timestamps.get(str(UserID))

            if presence_type in [1, 2, 3]:
                current_time = int(time.time())
                update_cached_timestamp(UserID, current_time)
                cached_timestamp = current_time

            timestamps = []

            if cached_timestamp:
                timestamps.append((cached_timestamp, "Cached"))

            if badge_last_online:
                timestamps.append((badge_last_online, "Badge Activity"))

            if profile_data['last_online']:
                timestamps.append((profile_data['last_online'], "Profile API"))

            if rolimons_last_online:
                timestamps.append((rolimons_last_online, "Rolimons Data"))

            formatted_last_online = "Unknown"
            if timestamps:
                timestamps.sort(key=lambda x: x[0], reverse=True)
                last_online_timestamp, last_online_source = timestamps[0]
                formatted_last_online = f"<t:{last_online_timestamp}:D>"

            badges = {
                "Combat Initiation": "<:CombatInitiation:1430627878898368632>",
                "Administrator": "<:RobloxAdmin:1416951128876122152>",
                "Bloxxer": "<:Bloxxer:1430627881301704736>",
                "Warrior": "<:Warrior:1430640757403943063>",
                "Official Model Maker": "<:RobloxModelMaker:1416952360852263013>",
                "Bricksmith": "<:Roblox1000Visits:1416952101229170698>",
                "Homestead": "<:Roblox100Visits:1416952056324952184>",
                "Inviter": "<:RobloxInviter:1416952415772479559>",
                "Ambassador": "<:Ambassador:1430627877337960548>",
                "Friendship": "<:Friendship:1430641140679577630>",
                "Veteran": "<:RobloxVeteran:1416952185094406264>",
                "Welcome To The Club": "<:WelcomeToTheClub:1430627875337273525>"
            }

            badges_display = "None"
            if roblox_badges and len(roblox_badges) > 0:
                # Yapin has none of the Discord custom emoji, so badges are listed by name.
                badge_names = [b.get("name", "") for b in roblox_badges if b.get("name")]
                badges_display = ", ".join(badge_names) if badge_names else "None"

            presence_status_map = {      # the status shows as an emoji in the footer text (footer pictures from other hosts do not load)
                0: {'text': '⚫ Offline'},
                1: {'text': '🟢 Online'},
                2: {'text': '🎮 In Game'},
                3: {'text': '🛠️ In Studio'},
                None: {'text': '⚪ Status unknown'},
            }
            current_status = presence_status_map.get(presence_type, presence_status_map[None])

            def num(n):
                return f"{n:,}" if isinstance(n, (int, float)) else "N/A"


            view = discord.ui.View()
            if not is_terminated:
                view.add_item(discord.ui.Button(
                    label="Roblox Profile",
                    style=discord.ButtonStyle.link,
                    emoji="<:RobloxLogo:1416951004607418398>",
                    url=profileurl
                ))
            view.add_item(discord.ui.Button(
                label="View Rolimons",
                style=discord.ButtonStyle.link,
                emoji="<:RolimonsLogo:1417258794974711901>",
                url=rolimonsurl
            ))

            friends_followers_text = f"**{num(friends_count)}** Friends | **{num(followers_count)}** Followers | **{num(followings_count)}** Following"

            full_description = f"{friends_followers_text}\n\n{Description}"

            embed = discord.Embed(
                title=Username,
                url=profileurl,
                description=full_description,
                color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
            )

            if is_terminated:
                embed.add_field(name="ID", value=f"{UserID}", inline=True)
                embed.add_field(name="Terminated", value="True", inline=True)
                embed.add_field(name="Visits", value=num(total_visits), inline=True)

                embed.add_field(name="Created", value=created_timestamp if created_timestamp else "Unknown", inline=True)
                embed.add_field(name="Last Online", value=formatted_last_online, inline=True)
                embed.add_field(name="Badges", value=badges_display, inline=True)
            else:
                embed.add_field(name="ID", value=f"{UserID}", inline=True)
                embed.add_field(name="Verified", value="Hat" if is_verified else ("Unknown" if inventory_visibility == "Private" else "False"), inline=True)
                embed.add_field(name="Inventory", value=inventory_visibility, inline=True)

                embed.add_field(name="RAP", value=f"[`{num(rap_value)}`]({rolimonsurl})", inline=True)
                embed.add_field(name="Value", value=f"[`{num(value_value)}`]({rolimonsurl})", inline=True)
                embed.add_field(name="Visits", value=num(total_visits), inline=True)

                embed.add_field(name="Created", value=created_timestamp if created_timestamp else "Unknown", inline=True)
                embed.add_field(name="Last Online", value=formatted_last_online, inline=True)
                embed.add_field(name="Badges", value=badges_display, inline=True)

            if current_place_id and presence_type == 2:
                game_url = f"https://www.roblox.com/games/{current_place_id}"
                embed.add_field(
                    name="Current Game",
                    value=f"[Click to View Game]({game_url})",
                    inline=False
                )

            if Discord != None:
                embed.add_field(
                    name="Owner",
                    value=f"```txt\n{Discord}\n```",
                    inline=False
                )

            if avatar_image:
                embed.set_thumbnail(url=avatar_image)

            embed.set_footer(
                text=f"{current_status['text']} | Requested by {interaction.user.name} | {MainURL}"
            )

            await interaction.edit_original_response(embed=embed, view=view)
            return
        else:
            print(f"{user} not found.")
            failedembed7 = discord.Embed(
                title=f":warning: {user} not found.",
                color=discord.Color.yellow()
            )
            await interaction.edit_original_response(embed=failedembed7)
            return
    except requests.exceptions.RequestException as e:
        print(f"An error occurred during the request: {e}")
        failedembed8 = discord.Embed(
            title=f":warning: {user} not found.",
            color=discord.Color.yellow()
        )
        await interaction.edit_original_response(embed=failedembed8)
        return


# --- britishuser ---
@bot.tree.command(name="britishuser", description="Check if a user has their language set to British")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def british_check(interaction: discord.Interaction, user_input: str):
    await interaction.response.defer(thinking=True)

    embed_color = embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()

    thinkingembed = discord.Embed(
        title=f"{Emojis.get('loading')}  {interaction.user.mention} Checking if {user_input} is British!",
        color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
    )
    await interaction.followup.send(embed=thinkingembed)

    url = "https://users.roblox.com/v1/usernames/users"

    request_payload = {
        "usernames": [user_input],
        "excludeBannedUsers": False
    }

    try:
        response = requests.post(url, json=request_payload)
        response.raise_for_status()
        data = response.json()
        if data.get("data") and len(data["data"]) > 0:
            userinfo = data["data"][0]
            UserID = userinfo["id"]
            username = userinfo["name"]

            payload = json.dumps([{"name": "vieweeUserId", "type": "UserId", "value": int(UserID)}])
            b64encoded = base64.b64encode(payload.encode('utf-8')).decode('utf-8')

            british_url = f"https://apis.roblox.com/access-management/v1/upsell-feature-access?featureName=MustHideConnections&extraParameters={b64encoded}"
            british_response = requests.get(british_url)
            british_response.raise_for_status()
            british_data = british_response.json()

            is_british = british_data.get("access") == "Granted"

            avatar_url = f"https://thumbnails.roblox.com/v1/users/avatar-bust?userIds={UserID}&size=150x150&format=Png&isCircular=false"
            avatar_response = requests.get(avatar_url)
            avatar_data = avatar_response.json()
            avatar_thumbnail = avatar_data["data"][0]["imageUrl"] if avatar_data.get("data") and len(avatar_data["data"]) > 0 else None

            if is_british:
                embed = discord.Embed(
                    title=":flag_gb: British Check Result",
                    description=f"**{username}** is British! :flag_gb:",
                    color=embed_color
                )
            else:
                embed = discord.Embed(
                    title=":x: British Check Result",
                    description=f"**{username}** is not British :x:",
                    color=embed_color
                )

            if avatar_thumbnail:
                embed.set_thumbnail(url=avatar_thumbnail)

            embed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
            await interaction.edit_original_response(embed=embed)

        else:
            embed = discord.Embed(
                title=f":warning: {user_input} not found",
                color=embed_color
            )
            embed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
            await interaction.edit_original_response(embed=embed)

    except requests.exceptions.RequestException as e:
        embed = discord.Embed(
            title=":x: API Error",
            description=f"An error occurred: {str(e)}",
            color=embed_color
        )
        embed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
        await interaction.edit_original_response(embed=embed)


# --- iteminfo ---
@bot.tree.command(name="iteminfo", description="Get detailed information about a Roblox item")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def item(interaction: discord.Interaction, item_query: str = "Dominus Empyreus"):
    await interaction.response.defer(thinking=True)

    print(f"Searching For {item_query}'s item info")
    thinkingembed = discord.Embed(
        title=f"{Emojis.get('loading')}  {interaction.user.mention} Searching For {item_query}'s Item Information!",
        color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
    )
    await interaction.followup.send(embed=thinkingembed)

    if not item_query.isdigit():
        search_url = f"https://catalog.roblox.com/v1/search/items?category=All&limit=10&keyword={urllib.parse.quote(item_query)}"

        try:
            response = requests.get(search_url)
            response.raise_for_status()
            search_data = response.json()

            if search_data.get("data") and len(search_data["data"]) > 0:
                item_id = None
                for item in search_data["data"]:
                    if item.get("name", "").lower() == item_query.lower():
                        item_id = item.get("id")
                        break

                if not item_id:
                    item_id = search_data["data"][0].get("id")

                if not item_id:
                    failedembed = discord.Embed(
                        title=f":warning: {item_query} not found.",
                        color=discord.Color.yellow()
                    )
                    await interaction.edit_original_response(embed=failedembed)
                    return
            else:
                failedembed = discord.Embed(
                    title=f":warning: {item_query} not found.",
                    color=discord.Color.yellow()
                )
                await interaction.edit_original_response(embed=failedembed)
                return

        except requests.exceptions.RequestException as e:
            print(f"Error searching for item {item_query}: {e}")
            failedembed = discord.Embed(
                title=f":x: An error occurred while searching for {item_query}. Please try again later.",
                color=discord.Color.red()
            )
            await interaction.edit_original_response(embed=failedembed)
            return
    else:
        item_id = item_query

    url = f"https://catalog.roblox.com/v1/catalog/items/{item_id}/details?itemType=Asset"

    try:
        response = requests.get(url)
        response.raise_for_status()
        item_data = response.json()
        print(f"ItemData: {item_data}")

        name = item_data.get("name", "Unknown Item")
        description = item_data.get("description", "No description available")
        creator_name = item_data.get("creatorName", "Unknown Creator")
        creator_type = item_data.get("creatorType", "User")
        creator_verified = item_data.get("creatorHasVerifiedBadge", False)
        creator_target_id = item_data.get("creatorTargetId")
        favorite_count = item_data.get("favoriteCount", 0)
        lowest_price = item_data.get("lowestPrice", 0)
        is_purchasable = item_data.get("isPurchasable", False)
        item_type = item_data.get("itemType", "Asset")

        created_date = item_data.get("itemCreatedUtc")

        if creator_type == "Group" and creator_target_id == 5544706:
            creator_verified = True

        if creator_type == "User" and creator_target_id == 124767284:
            creator_verified = True

        if created_date:
            created_timestamp = isotodiscordtimestamp(created_date, "F")
        else:
            created_timestamp = "Unknown"

        updated_timestamp = created_timestamp

        creator_display = creator_name
        if creator_verified:
            creator_display += " <:RobloxVerified:1416951927513677874>"
        else:
            creator_display += f" ({creator_type})"

        price_display = "Not for sale"
        if is_purchasable and lowest_price is not None:
            price_display = "Free" if lowest_price == 0 else f"{lowest_price:,} Robux"

        thumbnail_url = f"https://thumbnails.roblox.com/v1/assets?assetIds={item_id}&size=420x420&format=Png"
        try:
            thumb_response = requests.get(thumbnail_url)
            thumb_response.raise_for_status()
            thumb_data = thumb_response.json()
            if thumb_data and thumb_data.get("data") and len(thumb_data["data"]) > 0:
                image_url = thumb_data["data"][0].get("imageUrl")
            else:
                image_url = None
        except requests.exceptions.RequestException as e:
            print(f"Error fetching item thumbnail: {e}")
            image_url = None

        embed = discord.Embed(
            title=name,
            url=f"https://www.roblox.com/catalog/{item_id}/",
            description=description,
            color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
        )

        if image_url:
            embed.set_thumbnail(url=image_url)

        embed.add_field(name="Item ID", value=item_id, inline=True)
        embed.add_field(name="Item Type", value=item_type, inline=True)
        embed.add_field(name="Price", value=price_display, inline=True)
        embed.add_field(name="Creator", value=creator_display, inline=True)
        embed.add_field(name="Favorites", value=f"{favorite_count:,}", inline=True)
        embed.add_field(name="Created", value=created_timestamp, inline=True)

        view = discord.ui.View()
        view.add_item(discord.ui.Button(
            label="View Item",
            style=discord.ButtonStyle.link,
            emoji="<:RobloxLogo:1416951004607418398>",
            url=f"https://www.roblox.com/catalog/{item_id}/"
        ))

        embed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
        await interaction.edit_original_response(embed=embed, view=view)

    except requests.exceptions.RequestException as e:
        print(f"Error fetching item data for ID {item_id}: {e}")
        failedembed = discord.Embed(
            title=f":x: An error occurred while fetching data for item ID: {item_id}. Please try again later.",
            color=discord.Color.red()
        )
        await interaction.edit_original_response(embed=failedembed)
        return


# --- placeinfo+badge ---
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@bot.tree.command(name="placeinfo", description="Get detailed information about a Roblox place")
@app_commands.describe(game_input="Roblox place ID or game URL")
async def placeinfo(interaction: discord.Interaction, game_input: str):
    await interaction.response.defer(thinking=True)

    thinkingembed = discord.Embed(
        title=f"{Emojis.get('loading')}  {interaction.user.mention} Searching For Place!",
        color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
    )
    await interaction.followup.send(embed=thinkingembed)

    try:
        connector = aiohttp.TCPConnector(family=socket.AF_INET)
        async with aiohttp.ClientSession(connector=connector) as session:
            place_id = None

            if "roblox.com/games/" in game_input:
                match = re.search(r'roblox\.com/games/(\d+)', game_input)
                if match:
                    place_id = match.group(1)
                else:
                    errorembed = discord.Embed(
                        title=":x: Invalid URL :x:",
                        description="Could not extract place ID from the URL",
                        color=discord.Color.red()
                    )
                    await interaction.edit_original_response(embed=errorembed)
                    return
            else:
                if not game_input.isdigit():
                    errorembed = discord.Embed(
                        title=":x: Invalid Input :x:",
                        description="Please provide a valid place ID or Roblox game URL",
                        color=discord.Color.red()
                    )
                    await interaction.edit_original_response(embed=errorembed)
                    return
                place_id = game_input

            universe_url = f"https://apis.roblox.com/universes/v1/places/{place_id}/universe"
            async with session.get(universe_url) as response:
                if response.status != 200:
                    errorembed = discord.Embed(
                        title=":x: API Error :x:",
                        description=f"Failed to fetch universe information (Status: {response.status})",
                        color=discord.Color.red()
                    )
                    await interaction.edit_original_response(embed=errorembed)
                    return

                universe_data = await response.json()
                universe_id = universe_data.get('universeId')

                if not universe_id:
                    errorembed = discord.Embed(
                        title=":x: Not Found :x:",
                        description="Could not find universe for this place ID",
                        color=discord.Color.red()
                    )
                    await interaction.edit_original_response(embed=errorembed)
                    return

            games_url = f"https://games.roblox.com/v1/games?universeIds={universe_id}"
            async with session.get(games_url) as response:
                if response.status != 200:
                    errorembed = discord.Embed(
                        title=":x: API Error :x:",
                        description=f"Failed to fetch game details (Status: {response.status})",
                        color=discord.Color.red()
                    )
                    errorembed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
                    await interaction.edit_original_response(embed=errorembed)
                    return

                games_data = await response.json()
                if not games_data.get('data') or len(games_data['data']) == 0:
                    errorembed = discord.Embed(
                        title=":x: Not Found :x:",
                        description="Could not find game details for this universe",
                        color=discord.Color.red()
                    )
                    errorembed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
                    await interaction.edit_original_response(embed=errorembed)
                    return

                game_info = games_data['data'][0]

            name = game_info.get('name', 'Unknown')
            description = game_info.get('description', 'No description available')
            creator_id = game_info.get('creator', {}).get('id', 'Unknown')
            creator_name = game_info.get('creator', {}).get('name', 'Unknown')
            creator_type = game_info.get('creator', {}).get('type', 'User')
            current_players = game_info.get('playing', 0)
            visits = game_info.get('visits', 0)
            max_players = game_info.get('maxPlayers', 0)
            created = game_info.get('created', 'Unknown')
            updated = game_info.get('updated', 'Unknown')
            genre = game_info.get('genre', 'Unknown')
            favorites_count = game_info.get('favoritedCount', 0)

            created_timestamp = isotodiscordtimestamp(created, "F") if created != 'Unknown' else "Unknown"
            updated_timestamp = isotodiscordtimestamp(updated, "F") if updated != 'Unknown' else "Unknown"

            thumbnail_url = f"https://thumbnails.roblox.com/v1/games/icons?universeIds={universe_id}&size=512x512&format=Png&isCircular=false"
            thumbnail_image = None
            async with session.get(thumbnail_url) as response:
                if response.status == 200:
                    thumbnail_data = await response.json()
                    if thumbnail_data.get('data') and len(thumbnail_data['data']) > 0:
                        thumbnail_image = thumbnail_data['data'][0]['imageUrl']

            embed = discord.Embed(
                title=name,
                url=f"https://www.roblox.com/games/{place_id}/",
                description=description,
                color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
            )

            embed.add_field(name="Place ID", value=f"`{place_id}`", inline=True)
            embed.add_field(name="Universe ID", value=f"`{universe_id}`", inline=True)

            if creator_type.lower() == "user":
                creator_display = f"[{creator_name}](https://www.roblox.com/users/{creator_id}/profile)"
            else:
                creator_display = f"[{creator_name}](https://www.roblox.com/groups/{creator_id}/)"
            embed.add_field(name="Creator", value=creator_display, inline=True)

            embed.add_field(name="Visits", value=f"{visits:,}", inline=True)
            embed.add_field(name="Current Players", value=f"{current_players:,}", inline=True)
            embed.add_field(name="Max Players", value=f"{max_players}", inline=True)

            embed.add_field(name="Favorites", value=f"{favorites_count:,}", inline=True)
            embed.add_field(name="Genre", value=genre, inline=True)

            embed.add_field(name="Created", value=created_timestamp, inline=True)
            embed.add_field(name="Updated", value=updated_timestamp, inline=True)

            if description and description != "No description available":
                if len(description) > 1024:
                    description = description[:1021] + "..."
                if len(embed.fields) % 3 != 0:
                    embed.add_field(name="\u200b", value="\u200b", inline=True)
                embed.add_field(name="Description", value=description, inline=False)

            if thumbnail_image:
                embed.set_thumbnail(url=thumbnail_image)

            view = discord.ui.View()
            view.add_item(discord.ui.Button(
                label="View Game",
                style=discord.ButtonStyle.link,
                emoji="<:RobloxLogo:1416951004607418398>",
                url=f"https://www.roblox.com/games/{place_id}/"
            ))

            embed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
            await interaction.edit_original_response(embed=embed, view=view)

    except Exception as e:
        print(f"Error in placeinfo command: {e}")
        errorembed = discord.Embed(
            title=":x: An error occurred :x:",
            description="Failed to fetch place information. Please try again later.",
            color=discord.Color.red()
        )
        errorembed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
        await interaction.edit_original_response(embed=errorembed)
        return

async def send_error_embed(interaction: discord.Interaction, title: str, description: str):
    embed = discord.Embed(
        title=f":x: {title}",
        description=description,
        color=discord.Color.red()
    )
    #embed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
    await interaction.followup.send(embed=embed)

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

async def create_badge_embed(badge_data: dict, thumbnail_url: Optional[str], badge_id: str, 
                           start_time: float, requester: discord.User) -> discord.Embed:
    badge_id = badge_data.get('id', 'N/A')
    name = badge_data.get('name', 'Unknown')
    display_name = badge_data.get('displayName', name)
    description = badge_data.get('description') or badge_data.get('displayDescription') or "No description"

    statistics = badge_data.get('statistics', {})
    past_day_awarded = statistics.get('pastDayAwardedCount', 0)
    awarded_count = statistics.get('awardedCount', 0)
    win_rate = statistics.get('winRatePercentage', 0)

    created = badge_data.get('created', 'Unknown')
    updated = badge_data.get('updated', 'Unknown')

    awarding_universe = badge_data.get('awardingUniverse', {})
    universe_name = awarding_universe.get('name', 'Unknown')
    universe_id = awarding_universe.get('id', 'N/A')
    root_place_id = awarding_universe.get('rootPlaceId', 'N/A')

    embed_color = embedDB.get(f"{requester.id}") if embedDB.get(f"{requester.id}") else discord.Color.blue()

    embed = discord.Embed(
        title=f"Badge: {display_name}",
        color=embed_color,
        timestamp=datetime.now(),
        url=f"https://www.roblox.com/badges/{badge_id}"
    )

    if thumbnail_url:
        embed.set_thumbnail(url=thumbnail_url)

    embed.add_field(name="Badge ID", value=f"`{badge_id}`", inline=True)
    embed.add_field(name="Internal Name", value=name, inline=True)
    embed.add_field(name="Enabled", value="Yes" if badge_data.get('enabled') else "No", inline=True)

    embed.add_field(name="Description", value=description, inline=False)

    embed.add_field(name="Awarded Today", value=f"{past_day_awarded:,}", inline=True)
    embed.add_field(name="Obtained Total", value=f"{awarded_count:,}", inline=True)
    embed.add_field(name="Win Rate", value=f"{win_rate}%", inline=True)

    created_timestamp = isotodiscordtimestamp(created)
    updated_timestamp = isotodiscordtimestamp(updated)

    if created_timestamp:
        embed.add_field(
            name="Created",
            value=f"{created_timestamp} {created_timestamp})", 
            inline=True
        )
    else:
        embed.add_field(name="Created", value=created, inline=True)

    if updated_timestamp:
        embed.add_field(
            name="Last Updated", 
            value=f"<t:{updated_timestamp}:f> (<t:{updated_timestamp}:R>)", 
            inline=True
        )
    else:
        embed.add_field(name="Last Updated", value=updated, inline=True)

    if universe_id != 'N/A':
        universe_field = f"[{universe_name}](https://www.roblox.com/games/{root_place_id}/)"
        universe_field += f"\nUniverse ID: `{universe_id}`"
        if root_place_id != 'N/A':
            universe_field += f"\nPlace ID: `{root_place_id}`"

        embed.add_field(name="Awarding Universe", value=universe_field, inline=False)

    elapsed_time = asyncio.get_event_loop().time() - start_time
    embed.set_footer(text=f"Load time: {elapsed_time:.2f}s • Requested by {requester.display_name} | {MainURL}")

    return embed

@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@bot.tree.command(name="badge", description="Get detailed information about a Roblox badge")
@app_commands.describe(badge_id="The ID of the Roblox badge")
async def badge_info(interaction: discord.Interaction, badge_id: str):
    await interaction.response.defer(thinking=True)
    start_time = asyncio.get_event_loop().time()

    thinkingembed = discord.Embed(
    title=f"{Emojis.get('loading')}  {interaction.user.mention} Searching For Place!",
    color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
    )
    await interaction.followup.send(embed=thinkingembed)

    if not badge_id.isdigit():
        await send_error_embed(
            interaction,
            "Invalid Badge ID",
            "Please provide a valid numeric badge ID."
        )
        return

    connector = aiohttp.TCPConnector(family=socket.AF_INET)
    async with aiohttp.ClientSession(connector=connector) as session:
        try:
            badge_url = f"https://badges.roblox.com/v1/badges/{badge_id}"

            async with session.get(badge_url) as response:
                if response.status == 404:
                    await send_error_embed(
                        interaction,
                        "Badge Not Found",
                        f"Could not find a badge with ID `{badge_id}`"
                    )
                    return
                elif response.status != 200:
                    await send_error_embed(
                        interaction,
                        "API Error",
                        f"Failed to fetch badge information (Status: {response.status})"
                    )
                    return

                badge_data = await response.json()

            thumbnail_url = await get_badge_thumbnail(session, badge_id)

            embed = await create_badge_embed(badge_data, thumbnail_url, badge_id, start_time, interaction.user)

            view = discord.ui.View()
            view.add_item(discord.ui.Button(
                label="View Badge",
                style=discord.ButtonStyle.link,
                emoji="<:RobloxLogo:1416951004607418398>",
                url=f"https://www.roblox.com/badges/{badge_id}"
            ))

            await interaction.edit_original_response(embed=embed, view=view)

        except Exception as e:
            await send_error_embed(interaction, "Unexpected Error", f"An error occurred: {str(e)}")


# --- outfits ---
@bot.tree.command(name="outfits", description="Get all outfits for a Roblox user")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def outfits(interaction: discord.Interaction, user: str = "Roblox"):
    await interaction.response.defer(thinking=True)
    
    print(f"Searching For {user}'s outfits")
    thinkingembed = discord.Embed(
        title=f"{Emojis.get('loading')}  {interaction.user.mention} Searching For {user}'s Roblox outfits!",
        color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
    )
    await interaction.followup.send(embed=thinkingembed)

    url = "https://users.roblox.com/v1/usernames/users"
    
    request_payload = {
        "usernames": [user],
        "excludeBannedUsers": False
    }

    async def get_outfit_thumbnails(session: aiohttp.ClientSession, outfit_ids: list) -> dict:
        """Get thumbnails for multiple outfits"""
        thumbnails = {}
        if not outfit_ids:
            return thumbnails
            
        for i in range(0, len(outfit_ids), 100):
            chunk = outfit_ids[i:i+100]
            ids_param = ",".join(str(outfit_id) for outfit_id in chunk)
            
            thumbnails_url = f"https://thumbnails.roblox.com/v1/users/outfits?userOutfitIds={ids_param}&size=420x420&format=Png"
            
            try:
                async with session.get(thumbnails_url) as response:
                    if response.status == 200:
                        thumbnails_data = await response.json()
                        for item in thumbnails_data.get('data', []):
                            thumbnails[item.get("targetId")] = item.get("imageUrl", "")
            except Exception as e:
                print(f"Error fetching thumbnails: {e}")
            
            if i + 100 < len(outfit_ids):
                await asyncio.sleep(1)
                
        return thumbnails

    try:
        response = requests.post(url, json=request_payload)
        response.raise_for_status()
        data = response.json()
        if data.get("data") and len(data["data"]) > 0:
            userinfo = data["data"][0]
            UserID = userinfo["id"]
            Display = userinfo["displayName"]
            hasVerifiedBadge = userinfo.get("hasVerifiedBadge", False)

            username = Display
            user_id = UserID

            if UserID == 124767284:
                hasVerifiedBadge = True

            outfits_url = f"https://avatar.roblox.com/v1/users/{user_id}/outfits?itemsPerPage=150&isEditable=true"
            
            try:
                connector = aiohttp.TCPConnector(family=socket.AF_INET)
                async with aiohttp.ClientSession(connector=connector) as session:
                    async with session.get(outfits_url) as response:
                        if response.status == 429:
                            failedembed = discord.Embed(
                                title=f":warning: Rate Limited",
                                description="Too many requests. Please try again in a few seconds.",
                                color=discord.Color.yellow()
                            )
                            await interaction.edit_original_response(embed=failedembed)
                            return
                        elif response.status != 200:
                            failedembed = discord.Embed(
                                title=f":warning: Failed to fetch outfits",
                                description=f"API returned status code: {response.status}",
                                color=discord.Color.yellow()
                            )
                            await interaction.edit_original_response(embed=failedembed)
                            return
                        
                        outfits_data = await response.json()
            except Exception as e:
                print(f"Error fetching outfits: {e}")
                failedembed = discord.Embed(
                    title=f":x: An error occurred while fetching outfits",
                    description="Please try again later.",
                    color=discord.Color.red()
                )
                await interaction.edit_original_response(embed=failedembed)
                return

            outfits = outfits_data.get('data', [])
            
            if not outfits:
                embed = discord.Embed(
                    title=f"{username}'s Outfits",
                    description="This user has no outfits.",
                    color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
                )
                embed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
                await interaction.edit_original_response(embed=embed)
                return

            outfit_ids = [outfit.get('id') for outfit in outfits if outfit.get('id')]
            outfit_thumbnails = {}
            
            try:
                connector = aiohttp.TCPConnector(family=socket.AF_INET)
                async with aiohttp.ClientSession(connector=connector) as session:
                    outfit_thumbnails = await get_outfit_thumbnails(session, outfit_ids)
            except Exception as e:
                print(f"Error fetching outfit thumbnails: {e}")

            class OutfitsView(discord.ui.View):
                def __init__(self, outfits, thumbnails, username, user_id, requester):
                    super().__init__(timeout=120)
                    self.outfits = outfits
                    self.thumbnails = thumbnails
                    self.current_page = 0
                    self.username = username
                    self.user_id = user_id
                    self.requester = requester
                    self.message = None
                    self.update_buttons()
                    
                async def create_embed(self) -> discord.Embed:
                    outfit = self.outfits[self.current_page]
                    outfit_id = outfit.get('id')
                    outfit_name = outfit.get('name', 'Unnamed Outfit')
                    created_date = outfit.get('created', '')
                    is_editable = outfit.get('isEditable', False)
                    
                    thumbnail_url = self.thumbnails.get(outfit_id, "")
                    
                    embed = discord.Embed(
                        title=f"{self.username}'s Outfits",
                        color=embedDB.get(f"{self.requester.id}") if embedDB.get(f"{self.requester.id}") else discord.Color.blue()
                    )
                    
                    if thumbnail_url:
                        embed.set_image(url=thumbnail_url)
                    else:
                        embed.description = "*Thumbnail not available*"
                    
                    embed.add_field(name="Outfit Name", value=outfit_name, inline=False)
                    embed.add_field(name="Outfit ID", value=f"`{outfit_id}`", inline=True)
                    embed.add_field(name="Editable", value="Yes" if is_editable else "No", inline=True)
                    
                    if created_date:
                        try:
                            created_timestamp = isotodiscordtimestamp(created_date, "F")
                            if created_timestamp:
                                embed.add_field(name="Created", value=created_timestamp, inline=True)
                        except:
                            pass
                    
                    embed.set_footer(
                        text=f"Outfit {self.current_page + 1}/{len(self.outfits)} | Requested by {self.requester.name} | {MainURL}"
                    )
                    
                    return embed
                
                def update_buttons(self):
                    self.clear_items()
                    
                    if self.current_page > 0:
                        previous_btn = discord.ui.Button(style=discord.ButtonStyle.primary, emoji="⬅️", custom_id="previous")
                        previous_btn.callback = self.previous_callback
                        self.add_item(previous_btn)
                        
                    if self.current_page < len(self.outfits) - 1:
                        next_btn = discord.ui.Button(style=discord.ButtonStyle.primary, emoji="➡️", custom_id="next")
                        next_btn.callback = self.next_callback
                        self.add_item(next_btn)
                    
                    current_outfit = self.outfits[self.current_page]
                    outfit_id = current_outfit.get('id')
                    if outfit_id:
                        link_btn = discord.ui.Button(
                            style=discord.ButtonStyle.link,
                            label="View Outfit",
                            url=f"https://www.roblox.com/outfits/{outfit_id}"
                        )
                        self.add_item(link_btn)
                
                async def previous_callback(self, interaction: discord.Interaction):
                    if interaction.user != self.requester:
                        await interaction.response.send_message("This is not your command!", ephemeral=True)
                        return
                        
                    self.current_page -= 1
                    embed = await self.create_embed()
                    self.update_buttons()
                    await interaction.response.edit_message(embed=embed, view=self)
                
                async def next_callback(self, interaction: discord.Interaction):
                    if interaction.user != self.requester:
                        await interaction.response.send_message("This is not your command!", ephemeral=True)
                        return
                        
                    self.current_page += 1
                    embed = await self.create_embed()
                    self.update_buttons()
                    await interaction.response.edit_message(embed=embed, view=self)
                
                async def on_timeout(self):
                    for item in self.children:
                        if isinstance(item, discord.ui.Button) and item.style != discord.ButtonStyle.link:
                            item.disabled = True
                    try:
                        await self.message.edit(view=self)
                    except Exception:
                        pass

            view = OutfitsView(outfits, outfit_thumbnails, username, user_id, interaction.user)
            embed = await view.create_embed()
            message = await interaction.edit_original_response(embed=embed, view=view)
            view.message = message
            return
        else:
            print(f"{user} not found.")
            failedembed = discord.Embed(
                title=f":warning: {user} not found.",
                color=discord.Color.yellow()
            )
            await interaction.edit_original_response(embed=failedembed)
            return
    except requests.exceptions.RequestException as e:
        print(f"An error occurred during the request: {e}")
        failedembed = discord.Embed(
            title=f":warning: {user} not found.",
            color=discord.Color.yellow()
        )
        await interaction.edit_original_response(embed=failedembed)
        return


# --- badges ---
@bot.tree.command(name="badges", description="Check if a user has a specific badge and get detailed information")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(
    username="Roblox username to check",
    badge_id="The badge ID to look for (optional)"
)
async def badges(interaction: discord.Interaction, username: str, badge_id: str = None):
    await interaction.response.defer(thinking=True)
    
    thinkingembed = discord.Embed(
        title=f"{Emojis.get('loading')}  {interaction.user.mention} Checking {username}'s badges!",
        color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
    )
    await interaction.followup.send(embed=thinkingembed)

    async def get_user_id():
        user_url = "https://users.roblox.com/v1/usernames/users"
        request_payload = {"usernames": [username], "excludeBannedUsers": False}
        async with aiohttp.ClientSession() as session:
            async with session.post(user_url, json=request_payload) as response:
                if response.status == 200:
                    data = await response.json()
                    return data, None
                return None, f"Failed to fetch user data (Status: {response.status})"

    async def get_user_badges(user_id):
        user_badges_url = f"https://badges.roblox.com/v1/users/{user_id}/badges?limit=100&sortOrder=Asc"
        async with aiohttp.ClientSession() as session:
            async with session.get(user_badges_url) as response:
                if response.status == 200:
                    data = await response.json()
                    return data, None
                return None, f"Failed to fetch badges (Status: {response.status})"

    async def get_badge_info(badge_id):
        badge_info_url = f"https://badges.roblox.com/v1/badges/{badge_id}"
        async with aiohttp.ClientSession() as session:
            async with session.get(badge_info_url) as response:
                if response.status == 200:
                    data = await response.json()
                    return data, None
                return None, f"Failed to fetch badge info (Status: {response.status})"

    async def get_awarded_date(user_id, badge_id):
        awarded_date_url = f"https://badges.roblox.com/v1/users/{user_id}/badges/awarded-dates?badgeIds={badge_id}"
        async with aiohttp.ClientSession() as session:
            async with session.get(awarded_date_url) as response:
                if response.status == 200:
                    data = await response.json()
                    return data, None
                return None, f"Failed to fetch awarded date (Status: {response.status})"

    async def get_badge_thumbnail(badge_id):
        thumbnail_url = f"https://thumbnails.roblox.com/v1/badges/icons?badgeIds={badge_id}&size=150x150&format=Png"
        async with aiohttp.ClientSession() as session:
            async with session.get(thumbnail_url) as response:
                if response.status == 200:
                    data = await response.json()
                    if data.get('data') and len(data['data']) > 0:
                        return data['data'][0]['imageUrl']
        return None

    try:
        user_data, error = await get_user_id()
        
        if error or not user_data.get("data") or len(user_data["data"]) == 0:
            errorembed = discord.Embed(
                title=f":x: User Not Found :x:",
                description=f"Could not find a Roblox user with the username `{username}`",
                color=discord.Color.red()
            )
            errorembed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
            await interaction.edit_original_response(embed=errorembed)
            return
            
        user_info = user_data["data"][0]
        user_id = user_info["id"]
        roblox_username = user_info["name"]
        
        badges_data, error = await get_user_badges(user_id)
        
        if error:
            errorembed = discord.Embed(
                title=f":x: Error Fetching Badges :x:",
                description=f"Failed to fetch badges for user `{username}`",
                color=discord.Color.red()
            )
            errorembed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
            await interaction.edit_original_response(embed=errorembed)
            return
        
        user_badges = badges_data.get("data", [])
        
        if not user_badges:
            errorembed = discord.Embed(
                title=f":x: No Badges Found :x:",
                description=f"User `{username}` has no badges",
                color=discord.Color.red()
            )
            errorembed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
            await interaction.edit_original_response(embed=errorembed)
            return
        
        if badge_id:
            target_badge = None
            for badge in user_badges:
                if str(badge.get("id")) == badge_id:
                    target_badge = badge
                    break
            
            if not target_badge:
                errorembed = discord.Embed(
                    title=f":x: Badge Not Found :x:",
                    description=f"User `{username}` does not have badge ID `{badge_id}`",
                    color=discord.Color.red()
                )
                errorembed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
                await interaction.edit_original_response(embed=errorembed)
                return
            
            badge_info_task = asyncio.create_task(get_badge_info(badge_id))
            awarded_date_task = asyncio.create_task(get_awarded_date(user_id, badge_id))
            thumbnail_task = asyncio.create_task(get_badge_thumbnail(badge_id))
            
            badge_info, badge_error = await badge_info_task
            awarded_data, awarded_error = await awarded_date_task
            badge_thumbnail = await thumbnail_task
            
            if badge_error:
                errorembed = discord.Embed(
                    title=f":x: Error Fetching Badge Info :x:",
                    description=f"Failed to fetch detailed information for badge ID `{badge_id}`",
                    color=discord.Color.red()
                )
                errorembed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
                await interaction.edit_original_response(embed=errorembed)
                return
            
            awarded_date = "Unknown"
            if awarded_data and awarded_data.get("data") and len(awarded_data["data"]) > 0:
                awarded_date_str = awarded_data["data"][0].get("awardedDate")
                if awarded_date_str:
                    awarded_timestamp = isotodiscordtimestamp(awarded_date_str, "F")
                    awarded_date = awarded_timestamp if awarded_timestamp else awarded_date_str
            
            badge_name = badge_info.get("displayName") or badge_info.get("name", "Unknown Badge")
            badge_description = badge_info.get("displayDescription") or badge_info.get("description", "No description available")
            badge_enabled = badge_info.get("enabled", False)
            
            created_date = badge_info.get("created", "Unknown")
            created_timestamp = isotodiscordtimestamp(created_date, "F") if created_date != "Unknown" else "Unknown"
            
            embed_color = embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
            
            embed = discord.Embed(
                title=f"✅ {username} has this badge!",
                description=f"**{badge_name}**\n{badge_description}",
                color=embed_color
            )
            
            if badge_thumbnail:
                embed.set_thumbnail(url=badge_thumbnail)
            
            embed.add_field(name="Badge Information", value=f"**ID:** `{badge_id}`\n**Status:** {'Enabled' if badge_enabled else 'Disabled'}", inline=True)
            embed.add_field(name="Awarded", value=f"**Date:** {awarded_date}", inline=True)
            embed.add_field(name="Created", value=f"**Date:** {created_timestamp}", inline=True)
            
            view = discord.ui.View()
            view.add_item(discord.ui.Button(
                label="View Badge",
                style=discord.ButtonStyle.link,
                emoji="<:RobloxLogo:1416951004607418398>",
                url=f"https://www.roblox.com/badges/{badge_id}"
            ))
            
            embed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
            await interaction.edit_original_response(embed=embed, view=view)
            return

        first_badge = user_badges[0]
        first_badge_id = str(first_badge.get("id"))
        
        badge_info_task = asyncio.create_task(get_badge_info(first_badge_id))
        awarded_date_task = asyncio.create_task(get_awarded_date(user_id, first_badge_id))
        thumbnail_task = asyncio.create_task(get_badge_thumbnail(first_badge_id))
        
        badge_info, badge_error = await badge_info_task
        awarded_data, awarded_error = await awarded_date_task
        badge_thumbnail = await thumbnail_task
        
        if badge_error:
            errorembed = discord.Embed(
                title=f":x: Error Fetching Badge Info :x:",
                description=f"Failed to fetch detailed information for badge ID `{first_badge_id}`",
                color=discord.Color.red()
            )
            errorembed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
            await interaction.edit_original_response(embed=errorembed)
            return
        
        awarded_date = "Unknown"
        if awarded_data and awarded_data.get("data") and len(awarded_data["data"]) > 0:
            awarded_date_str = awarded_data["data"][0].get("awardedDate")
            if awarded_date_str:
                awarded_timestamp = isotodiscordtimestamp(awarded_date_str, "F")
                awarded_date = awarded_timestamp if awarded_timestamp else awarded_date_str
        
        badge_name = badge_info.get("displayName") or badge_info.get("name", "Unknown Badge")
        badge_description = badge_info.get("displayDescription") or badge_info.get("description", "No description available")
        badge_enabled = badge_info.get("enabled", False)
        
        created_date = badge_info.get("created", "Unknown")
        created_timestamp = isotodiscordtimestamp(created_date, "F") if created_date != "Unknown" else "Unknown"
        
        embed_color = embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
        
        class BadgePaginator(discord.ui.View):
            def __init__(self, badges, user_id, username, requester_id, current_index=0):
                super().__init__(timeout=60)
                self.badges = badges
                self.user_id = user_id
                self.username = username
                self.requester_id = requester_id
                self.current_index = current_index
            
            async def update_badge_display(self, interaction: discord.Interaction):
                badge = self.badges[self.current_index]
                badge_id = str(badge.get("id"))
                
                badge_info_task = asyncio.create_task(get_badge_info(badge_id))
                awarded_date_task = asyncio.create_task(get_awarded_date(self.user_id, badge_id))
                thumbnail_task = asyncio.create_task(get_badge_thumbnail(badge_id))
                
                badge_info, badge_error = await badge_info_task
                awarded_data, awarded_error = await awarded_date_task
                badge_thumbnail = await thumbnail_task
                
                if badge_error:
                    await interaction.response.send_message("Failed to fetch badge information.", ephemeral=True)
                    return
                
                awarded_date = "Unknown"
                if awarded_data and awarded_data.get("data") and len(awarded_data["data"]) > 0:
                    awarded_date_str = awarded_data["data"][0].get("awardedDate")
                    if awarded_date_str:
                        awarded_timestamp = isotodiscordtimestamp(awarded_date_str, "F")
                        awarded_date = awarded_timestamp if awarded_timestamp else awarded_date_str
                
                badge_name = badge_info.get("displayName") or badge_info.get("name", "Unknown Badge")
                badge_description = badge_info.get("displayDescription") or badge_info.get("description", "No description available")
                badge_enabled = badge_info.get("enabled", False)
                
                created_date = badge_info.get("created", "Unknown")
                created_timestamp = isotodiscordtimestamp(created_date, "F") if created_date != "Unknown" else "Unknown"
                
                embed_color = embedDB.get(f"{self.requester_id}") if embedDB.get(f"{self.requester_id}") else discord.Color.blue()
                
                embed = discord.Embed(
                    title=f"✅ {self.username}'s Badges",
                    description=f"**{badge_name}**\n{badge_description}",
                    color=embed_color
                )
                
                if badge_thumbnail:
                    embed.set_thumbnail(url=badge_thumbnail)
                
                embed.add_field(name="Badge Information", value=f"**ID:** `{badge_id}`\n**Status:** {'Enabled' if badge_enabled else 'Disabled'}", inline=True)
                embed.add_field(name="Awarded", value=f"**Date:** {awarded_date}", inline=True)
                embed.add_field(name="Created", value=f"**Date:** {created_timestamp}", inline=True)
                embed.set_footer(text=f"Badge {self.current_index + 1}/{len(self.badges)} | Requested By {interaction.user.name} | {MainURL}")
                
                view = BadgePaginator(self.badges, self.user_id, self.username, self.requester_id, self.current_index)
                await interaction.response.edit_message(embed=embed, view=view)
            
            @discord.ui.button(emoji="⬅️", style=discord.ButtonStyle.secondary)
            async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                if interaction.user.id != self.requester_id:
                    await interaction.response.send_message("This is not your command!", ephemeral=True)
                    return
                
                self.current_index = (self.current_index - 1) % len(self.badges)
                await self.update_badge_display(interaction)
            
            @discord.ui.button(emoji="➡️", style=discord.ButtonStyle.secondary)
            async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                if interaction.user.id != self.requester_id:
                    await interaction.response.send_message("This is not your command!", ephemeral=True)
                    return
                
                self.current_index = (self.current_index + 1) % len(self.badges)
                await self.update_badge_display(interaction)
            
        
        embed = discord.Embed(
            title=f"✅ {username}'s Badges",
            description=f"**{badge_name}**\n{badge_description}",
            color=embed_color
        )
        
        if badge_thumbnail:
            embed.set_thumbnail(url=badge_thumbnail)
        
        embed.add_field(name="Badge Information", value=f"**ID:** `{first_badge_id}`\n**Status:** {'Enabled' if badge_enabled else 'Disabled'}", inline=True)
        embed.add_field(name="Awarded", value=f"**Date:** {awarded_date}", inline=True)
        embed.add_field(name="Created", value=f"**Date:** {created_timestamp}", inline=True)
        embed.set_footer(text=f"Badge 1/{len(user_badges)} | Requested By {interaction.user.name} | {MainURL}")
        
        view = BadgePaginator(user_badges, user_id, roblox_username, interaction.user.id, 0)
        await interaction.edit_original_response(embed=embed, view=view)
        
    except Exception as e:
        errorembed = discord.Embed(
            title=f":x: Error :x:",
            description=f"An error occurred: {str(e)}",
            color=discord.Color.red()
        )
        errorembed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
        await interaction.edit_original_response(embed=errorembed)


# --- limiteds ---
@bot.tree.command(name="limiteds", description="Scan a roblox users inventory for limiteds")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def limiteds(interaction: discord.Interaction, username: str):
    await interaction.response.defer()
    
    thinkingembed = discord.Embed(
        title=f"{Emojis.get('loading')} {interaction.user.mention} Scanning for {username}'s limiteds!",
        color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
    )
    await interaction.followup.send(embed=thinkingembed)

    user_url = "https://users.roblox.com/v1/usernames/users"
    request_payload = {
        "usernames": [username],
        "excludeBannedUsers": False
    }

    try:
        response = requests.post(user_url, json=request_payload)
        response.raise_for_status()
        user_data = response.json()
        
        if not user_data.get("data") or len(user_data["data"]) == 0:
            errorembed = discord.Embed(
                title=":x: User Not Found :x:",
                description=f"Could not find a Roblox user with the username `{username}`",
                color=discord.Color.red()
            )
            errorembed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
            await interaction.edit_original_response(embed=errorembed)
            return
            
        user_info = user_data["data"][0]
        user_id = user_info["id"]
        display_name = user_info["displayName"]

        connector = aiohttp.TCPConnector(family=socket.AF_INET)
        async with aiohttp.ClientSession(connector=connector) as session:
            all_limiteds_items = []
            cursor = ""
            
            while True:
                limiteds_url = f"https://inventory.roblox.com/v1/users/{user_id}/assets/collectibles?sortOrder=Asc&limit=100"
                if cursor:
                    limiteds_url += f"&cursor={cursor}"
                
                async with session.get(limiteds_url) as response:
                    if response.status != 200:
                        errorembed = discord.Embed(
                            title=":x: Error Fetching Limiteds :x:",
                            description=f"Failed to fetch limiteds for user `{username}` (Status: {response.status})",
                            color=discord.Color.red()
                        )
                        errorembed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
                        await interaction.edit_original_response(embed=errorembed)
                        return
                    
                    limiteds_data = await response.json()
                    limiteds_items = limiteds_data.get("data", [])
                    all_limiteds_items.extend(limiteds_items)
                    
                    next_cursor = limiteds_data.get("nextPageCursor")
                    if not next_cursor:
                        break
                    
                    cursor = next_cursor
                    
                    if len(all_limiteds_items) >= 100:
                        await asyncio.sleep(2.5)
            
            rap = 0
            value = 0
            try:
                rolimons_url = f"https://api.rolimons.com/players/v1/playerinfo/{user_id}"
                async with session.get(rolimons_url, headers={'User-Agent': 'shapes.lol'}) as response:
                    if response.status == 200:
                        rolimons_data = await response.json()
                        if rolimons_data.get('success'):
                            rap = rolimons_data.get('rap', 0) or 0
                            value = rolimons_data.get('value', 0) or 0
            except Exception:
                pass
            
            inventory_url = f"https://inventory.roblox.com/v1/users/{user_id}/can-view-inventory"
            inventory_visibility = "Private"
            try:
                async with session.get(inventory_url) as response:
                    if response.status == 200:
                        inventory_data = await response.json()
                        inventory_visibility = "Public" if inventory_data.get('canView', False) else "Private"
            except Exception:
                pass

            avatar_url = f"https://thumbnails.roblox.com/v1/users/avatar?userIds={user_id}&size=420x420&format=Png&isCircular=false"
            avatar_thumbnail = None
            try:
                async with session.get(avatar_url) as response:
                    if response.status == 200:
                        avatar_data = await response.json()
                        if avatar_data.get("data") and len(avatar_data["data"]) > 0:
                            avatar_thumbnail = avatar_data["data"][0].get("imageUrl")
            except Exception:
                pass

            limited_count = len(all_limiteds_items)
            limiteds_by_name = {}
            
            for item in all_limiteds_items:
                name = item.get("name", "Unknown Item")
                user_asset_id = item.get("userAssetId")
                
                if name in limiteds_by_name:
                    limiteds_by_name[name]["count"] += 1
                else:
                    limiteds_by_name[name] = {
                        "count": 1,
                        "user_asset_id": user_asset_id
                    }
            
            limiteds_list = []
            for name, data in limiteds_by_name.items():
                count = data["count"]
                user_asset_id = data["user_asset_id"]
                
                if count > 1:
                    display_text = f"{count}x [{name}](https://www.rolimons.com/uaid/{user_asset_id})"
                else:
                    display_text = f"[{name}](https://www.rolimons.com/uaid/{user_asset_id})"
                
                limiteds_list.append(display_text)
            
            limiteds_chunks = [limiteds_list[i:i+10] for i in range(0, len(limiteds_list), 10)]
            
            class LimitedsPaginationView(discord.ui.View):
                def __init__(self, limiteds_chunks, display_name, user_id, rap, value, limited_count, inventory_visibility, requester_id, avatar_thumbnail):
                    super().__init__(timeout=120)
                    self.limiteds_chunks = limiteds_chunks
                    self.current_page = 0
                    self.display_name = display_name
                    self.user_id = user_id
                    self.rap = rap
                    self.value = value
                    self.limited_count = limited_count
                    self.inventory_visibility = inventory_visibility
                    self.requester_id = requester_id
                    self.avatar_thumbnail = avatar_thumbnail
                
                async def create_embed(self):
                    embed = discord.Embed(
                        title=f"{self.display_name}'s Limiteds",
                        url=f"https://www.roblox.com/users/{self.user_id}/profile",
                        color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
                    )
                    
                    if self.avatar_thumbnail:
                        embed.set_thumbnail(url=self.avatar_thumbnail)
                    
                    stats_info = f"""
                    > **RAP:** {self.rap:,}
                    > **Value:** {self.value:,}
                    > **Limited Count:** {self.limited_count}
                    > **Inventory:** {self.inventory_visibility}
                    """
                    
                    embed.add_field(
                        name="Statistics",
                        value=stats_info,
                        inline=False
                    )
                    
                    if self.limiteds_chunks:
                        current_chunk = self.limiteds_chunks[self.current_page]
                        limiteds_text = "\n".join(current_chunk)
                        field_name = f"Limiteds (Page {self.current_page + 1}/{len(self.limiteds_chunks)})"
                        embed.add_field(
                            name=field_name,
                            value=limiteds_text,
                            inline=False
                        )
                    else:
                        embed.add_field(
                            name="Limiteds",
                            value="No limiteds found",
                            inline=False
                        )
                    
                    embed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
                    return embed
                
                def update_buttons(self):
                    self.clear_items()
                    
                    if self.current_page > 0:
                        previous_btn = discord.ui.Button(style=discord.ButtonStyle.gray, emoji="⬅️", custom_id="previous")
                        previous_btn.callback = self.previous_callback
                        self.add_item(previous_btn)
                    
                    if self.current_page < len(self.limiteds_chunks) - 1:
                        next_btn = discord.ui.Button(style=discord.ButtonStyle.gray, emoji="➡️", custom_id="next")
                        next_btn.callback = self.next_callback
                        self.add_item(next_btn)
                    
                    self.add_item(discord.ui.Button(
                        label="View Rolimons Profile",
                        style=discord.ButtonStyle.link,
                        emoji="<:RolimonsLogo:1417258794974711901>",
                        url=f"https://rolimons.com/player/{self.user_id}"
                    ))
                    
                    self.add_item(discord.ui.Button(
                        label="View Roblox Profile",
                        style=discord.ButtonStyle.link,
                        emoji="<:RobloxLogo:1416951004607418398>",
                        url=f"https://www.roblox.com/users/{self.user_id}/profile"
                    ))
                
                async def previous_callback(self, interaction: discord.Interaction):
                    if interaction.user.id != self.requester_id:
                        await interaction.response.send_message("This is not your command!", ephemeral=True)
                        return
                    
                    self.current_page -= 1
                    embed = await self.create_embed()
                    self.update_buttons()
                    await interaction.response.edit_message(embed=embed, view=self)
                
                async def next_callback(self, interaction: discord.Interaction):
                    if interaction.user.id != self.requester_id:
                        await interaction.response.send_message("This is not your command!", ephemeral=True)
                        return
                    
                    self.current_page += 1
                    embed = await self.create_embed()
                    self.update_buttons()
                    await interaction.response.edit_message(embed=embed, view=self)
            
            view = LimitedsPaginationView(limiteds_chunks, display_name, user_id, rap, value, limited_count, inventory_visibility, interaction.user.id, avatar_thumbnail)
            embed = await view.create_embed()
            view.update_buttons()
            await interaction.edit_original_response(embed=embed, view=view)
            
    except requests.exceptions.RequestException as e:
        errorembed = discord.Embed(
            title=":x: API Error :x:",
            description=f"An error occurred while fetching data: {str(e)}",
            color=discord.Color.red()
        )
        errorembed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
        await interaction.edit_original_response(embed=errorembed)
    except Exception as e:
        errorembed = discord.Embed(
            title=":x: Unexpected Error :x:",
            description=f"An unexpected error occurred: {str(e)}",
            color=discord.Color.red()
        )
        errorembed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
        await interaction.edit_original_response(embed=errorembed)


# --- bundle ---
@bot.tree.command(name="bundle", description="Get information about a Roblox bundle")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(bundleid="The ID of the bundle")
async def bundle(interaction: discord.Interaction, bundleid: str):
    await interaction.response.defer(thinking=True)
    
    print(f"Searching for bundle {bundleid}")
    thinkingembed = discord.Embed(
        title=f"{Emojis.get('loading')} {interaction.user.mention} Searching for bundle {bundleid}!",
        color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
    )
    await interaction.followup.send(embed=thinkingembed)

    try:
        url = f"https://catalog.roblox.com/v1/catalog/items/{bundleid}/details?itemType=Bundle"
        
        connector = aiohttp.TCPConnector(family=socket.AF_INET)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(url, headers={
                'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36',
                'accept': 'application/json',
            }) as response:
                if response.status != 200:
                    failedembed = discord.Embed(
                        title=f"❌ Bundle not found",
                        description=f"Could not find a bundle with ID `{bundleid}`",
                        color=discord.Color.red()
                    )
                    failedembed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
                    await interaction.edit_original_response(embed=failedembed)
                    return
                
                data = await response.json()
                
                name = data.get("name", "Unknown")
                description = data.get("description", "No description available")
                item_type = data.get("itemType", "Unknown")
                creator_name = data.get("creatorName", "Unknown")
                creator_has_verified_badge = data.get("creatorHasVerifiedBadge", False)
                lowest_price = data.get("lowestPrice", 0)
                price_status = data.get("priceStatus", "Unknown")
                favorite_count = data.get("favoriteCount", 0)
                item_created_utc = data.get("itemCreatedUtc", "")
                
                created_timestamp = "Unknown"
                if item_created_utc:
                    created_timestamp = isotodiscordtimestamp(item_created_utc, "D")
                
                creator_display = creator_name
                if creator_has_verified_badge:
                    creator_display += " <:RobloxVerified:1416951927513677874>"
                
                price_display = f"{lowest_price} Robux" if price_status != "Off Sale" else "Off Sale"
                
                bundle_info = f"""
                > **Name:** {name}
                > **Type:** {item_type}
                > **Creator:** {creator_display}
                > **Price:** {price_display}
                > **Favorites:** {favorite_count:,}
                > **Created:** {created_timestamp}
                """
                
                embed = discord.Embed(
                    title=name,
                    color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue(),
                    timestamp=interaction.created_at,
                    url=f"https://www.roblox.com/bundles/{bundleid}"
                )
                
                embed.add_field(
                    name="Bundle Information",
                    value=bundle_info,
                    inline=False
                )
                
                if description and description != "No description available":
                    embed.add_field(
                        name="Description",
                        value=f"> {description}",
                        inline=False
                    )
                
                thumbnail_url = f"https://thumbnails.roblox.com/v1/bundles/thumbnails?bundleIds={bundleid}&size=420x420&format=Png"
                async with session.get(thumbnail_url) as thumb_response:
                    if thumb_response.status == 200:
                        thumb_data = await thumb_response.json()
                        if thumb_data.get('data') and len(thumb_data['data']) > 0:
                            image_url = thumb_data['data'][0].get('imageUrl')
                            if image_url:
                                embed.set_thumbnail(url=image_url)
                
                embed.set_footer(text=f"Requested by {interaction.user.display_name} | {MainURL}")
                
                class BundleView(discord.ui.View):
                    def __init__(self, bundle_id: str):
                        super().__init__(timeout=120)
                        self.add_item(discord.ui.Button(
                            label="View Bundle on Roblox",
                            url=f"https://www.roblox.com/bundles/{bundle_id}",
                            style=discord.ButtonStyle.link,
                            emoji="<:RobloxLogo:1416951004607418398>"
                        ))
                
                view = BundleView(bundleid)
                await interaction.edit_original_response(embed=embed, view=view)
                
    except Exception as e:
        failedembed = discord.Embed(
            title=f"❌ Error fetching bundle",
            description=f"An error occurred: {str(e)}",
            color=discord.Color.red()
        )
        failedembed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
        await interaction.edit_original_response(embed=failedembed)


# --- github ---
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@bot.tree.command(name="github", description="Get detailed information about a GitHub user")
@app_commands.describe(username="GitHub username")
async def github(interaction: discord.Interaction, username: str):
    await interaction.response.defer(thinking=True)
    
    thinkingembed = discord.Embed(
        title=f"{Emojis.get('loading')} {interaction.user.mention} Searching For GitHub Profile!",
        color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.dark_gray()
    )
    await interaction.followup.send(embed=thinkingembed)
    
    try:
        connector = aiohttp.TCPConnector(family=socket.AF_INET)
        async with aiohttp.ClientSession(connector=connector) as session:
            url = f"https://api.github.com/users/{username}"
            
            async with session.get(url) as response:
                if response.status == 404:
                    errorembed = discord.Embed(
                        description=f"❌ GitHub user `{username}` not found",
                        color=discord.Color.red()
                    )
                    errorembed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
                    await interaction.edit_original_response(embed=errorembed)
                    return
                
                response.raise_for_status()
                data = await response.json()
            
            avatar_url = data.get("avatar_url", "")
            profile_url = data.get("html_url", f"https://github.com/{username}")
            company = data.get("company", "Not specified")
            blog = data.get("blog", "Not specified")
            location = data.get("location", "Not specified")
            bio = data.get("bio", "No bio available")
            public_repos = data.get("public_repos", 0)
            followers = data.get("followers", 0)
            following = data.get("following", 0)
            created_at = data.get("created_at", "Unknown")
            updated_at = data.get("updated_at", "Unknown")
            
            created_timestamp = isotodiscordtimestamp(created_at, "D") if created_at != 'Unknown' else "Unknown"
            updated_timestamp = isotodiscordtimestamp(updated_at, "D") if updated_at != 'Unknown' else "Unknown"
            
            embed = discord.Embed(
                title=f"@{username}",
                url=profile_url,
                color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.dark_gray(),
                timestamp=interaction.created_at
            )
            
            if avatar_url:
                embed.set_thumbnail(url=avatar_url)
            
            github_info = f"""
            > **Public Repos:** `{public_repos}`
            > **Followers:** `{followers}`
            > **Following:** `{following}`
            > **Company:** `{company}`
            > **Location:** `{location}`
            > **Created:** {created_timestamp}
            > **Updated:** {updated_timestamp}
            """
            
            if blog and blog != "Not specified":
                github_info += f"> **Website:** `{blog}`\n"
            
            embed.add_field(
                name="GitHub Information",
                value=github_info,
                inline=False
            )
            
            if bio and bio != "No bio available":
                if len(bio) > 1024:
                    bio = bio[:1021] + "..."
                embed.add_field(
                    name="Bio",
                    value=f"> `{bio}`",
                    inline=False
                )
            
            embed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
            
            view = discord.ui.View(timeout=120)
            view.add_item(discord.ui.Button(
                label="View on GitHub", 
                url=profile_url,
                style=discord.ButtonStyle.link,
                emoji="🐙"
            ))
            
            await interaction.edit_original_response(embed=embed, view=view)
            
    except aiohttp.ClientError as e:
        errorembed = discord.Embed(
            description=f"Failed to request github",
            color=discord.Color.red()
        )
        errorembed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
        await interaction.edit_original_response(embed=errorembed)
    except Exception as e:
        errorembed = discord.Embed(
            description=f"❌ Failed to fetch GitHub profile information",
            color=discord.Color.red()
        )
        errorembed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
        await interaction.edit_original_response(embed=errorembed)


# --- asset ---
@bot.tree.command(name="asset", description="Get information about a Roblox asset")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def asset(interaction: discord.Interaction, id: str):
    await interaction.response.defer()
    
    print(f"Searching For Asset {id} Information!")
    thinkingembed = discord.Embed(
        title=f"{Emojis.get('loading')} {interaction.user.mention} Searching for Asset {id}",
        color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
    )
    await interaction.followup.send(embed=thinkingembed)

    url = f"https://catalog.roblox.com/v1/catalog/items/{id}/details?itemType=Asset"
    
    try:
        response = requests.get(url)
        response.raise_for_status()
        asset_data = response.json()
        
        name = asset_data.get("name", "Unknown Asset")
        description = asset_data.get("description", "No description available")
        creator_name = asset_data.get("creatorName", "Unknown Creator")
        creator_verified = asset_data.get("creatorHasVerifiedBadge", False)
        lowest_price = asset_data.get("lowestPrice", 0)
        favorite_count = asset_data.get("favoriteCount", 0)
        created_date = asset_data.get("itemCreatedUtc")
        item_type = asset_data.get("itemType", "Asset")
        
        if created_date:
            created_timestamp = isotodiscordtimestamp(created_date, "D")
        else:
            created_timestamp = "Unknown"
        
        creator_display = creator_name
        if creator_verified:
            creator_display += " <:RobloxVerified:1416951927513677874>"
        
        price_display = "Free" if lowest_price == 0 else f"{lowest_price:,} Robux"
        
        thumbnail_url = f"https://thumbnails.roblox.com/v1/assets?assetIds={id}&size=420x420&format=Png"
        thumbnail_response = requests.get(thumbnail_url)
        thumbnail_data = thumbnail_response.json()
        
        image_url = None
        if thumbnail_data and thumbnail_data.get("data") and len(thumbnail_data["data"]) > 0:
            image_url = thumbnail_data["data"][0].get("imageUrl")
        
        embed = discord.Embed(
            title=name,
            url=f"https://www.roblox.com/catalog/{id}/",
            color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue(),
            timestamp=interaction.created_at
        )
        
        if image_url:
            embed.set_thumbnail(url=image_url)
        
        asset_info = f"""
        > **Name:** {name}
        > **Asset ID:** `{id}`
        > **Type:** {item_type}
        > **Creator:** {creator_display}
        > **Price:** {price_display}
        > **Favorites:** {favorite_count:,}
        > **Created:** {created_timestamp}
        """
        
        embed.add_field(
            name="Asset Information",
            value=asset_info,
            inline=False
        )
        
        if description and description != "No description available":
            if len(description) > 1024:
                description = description[:1021] + "..."
            embed.add_field(
                name="Description",
                value=f"> {description}",
                inline=False
            )
        
        view = discord.ui.View(timeout=120)
        view.add_item(discord.ui.Button(
            label="View Asset",
            style=discord.ButtonStyle.link,
            emoji="<:RobloxLogo:1416951004607418398>",
            url=f"https://www.roblox.com/catalog/{id}/"
        ))
        
        embed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
        await interaction.edit_original_response(embed=embed, view=view)
        
    except requests.exceptions.RequestException as e:
        errorembed = discord.Embed(
            title=":x: Error Fetching Asset :x:",
            description=f"Failed to fetch asset information for ID `{id}`. Please check if the asset ID is valid.",
            color=discord.Color.red()
        )
        errorembed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
        await interaction.edit_original_response(embed=errorembed)
    except Exception as e:
        errorembed = discord.Embed(
            title=":x: Unexpected Error :x:",
            description=f"An unexpected error occurred while fetching asset information: {str(e)}",
            color=discord.Color.red()
        )
        errorembed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
        await interaction.edit_original_response(embed=errorembed)


# --- linkroblox..instagram ---
@bot.tree.command(name="linkroblox", description="Link your Roblox account to your Yapin account")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def linkroblox(interaction: discord.Interaction, action: str = "link"):
    """/linkroblox [link | status | unlink | hide | show]. Everything is private to the person using it."""
    action = (action or "link").lower().strip()
    uid = str(interaction.user.id)
    color = embedDB.get(uid) if embedDB.get(uid) else discord.Color.blue()
    if not api_ready():
        await interaction.response.send_message("Linking isn't set up on this bot yet (the owner needs to connect it to the Shapes API).", ephemeral=True)
        return
    if action in ("unlink", "remove"):
        data = await asyncio.to_thread(shapes_api, "DELETE", f"/internal/links/{uid}/roblox")
        if data is None:
            text = "Couldn't reach the linking service. Try again in a moment."
        else:
            text = "Unlinked your Roblox account." if data.get("removed") else "You didn't have a Roblox account linked."
        await interaction.response.send_message(text, ephemeral=True)
        return
    if action in ("hide", "show"):
        data = await asyncio.to_thread(shapes_api, "PATCH", f"/internal/links/{uid}/roblox", {"enabled": action == "show"})
        if data is None:
            text = "Couldn't reach the linking service. Try again in a moment."
        elif not data.get("success"):
            text = "You have no linked Roblox account. Use `/linkroblox` first."
        else:
            text = "Other people can now look up your link." if action == "show" else "Your link is now hidden from lookups."
        await interaction.response.send_message(text, ephemeral=True)
        return
    links = await asyncio.to_thread(shapes_api, "GET", f"/internal/links/{uid}")
    current = (links or {}).get("roblox")
    if action in ("status", "info"):
        if links is None:
            text = "Couldn't reach the linking service. Try again in a moment."
        elif not current:
            text = "No Roblox account linked. Use `/linkroblox` to link one."
        else:
            text = (f"Linked to [{current.get('name') or current['id']}](https://www.roblox.com/users/{current['id']}/profile). "
                    f"Lookups: {'on' if current.get('enabled', True) else 'hidden'}.")
        await interaction.response.send_message(text, ephemeral=True)
        return
    started = await asyncio.to_thread(shapes_api, "POST", "/internal/link/start", {"provider": "roblox", "yapin_id": uid, "yapin_name": interaction.user.name})
    if not started or not started.get("url"):
        reason = (started or {}).get("data") or "Couldn't reach the linking service. Try again in a moment."
        await interaction.response.send_message(str(reason), ephemeral=True)
        return
    embed = discord.Embed(title="Link your Roblox account", color=color, description=(
        "Tap the button and approve access on Roblox. You'll be linked to your Yapin account as soon as you do.\n"
        "The link works for 10 minutes and only for you."
        + (f"\n\nCurrently linked to **{current.get('name') or current['id']}**; linking again replaces it." if current else "")))
    view = discord.ui.View(timeout=600)
    view.add_item(discord.ui.Button(label="Link with Roblox", style=discord.ButtonStyle.link, url=started["url"]))
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


# --- PlayVortex (https://playvortex.io) ---
# API: /api/games/<id> and /api/users/<id>. Pages: /users/<id>/profile and /games/<id>.
# A person can store their own Vortex login (/vortexcookie) so lookups are made as them; that adds how they relate to
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
    """A Discord/Yapin timestamp tag from an ISO string (2026-04-23T05:37:25.988850+00:00) or Unix seconds/millis."""
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
    for name, value, inline in fields[:10]:    # Yapin allows 10 fields per embed
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
        await interaction.edit_original_response(embed=discord.Embed(title="Vortex didn't answer", description="Try again in a moment.", color=discord.Color.red()))
        return
    if data == "missing":
        await interaction.edit_original_response(embed=discord.Embed(title=f"No {what} found for `{_vclean(ident, 60)}`", color=discord.Color.red()))
        return
    color = ecolor(interaction.user.id)
    if kind == "games":
        embed = vortex_game_embed(data, color)
        buttons = [("View game", vortex_game_url(data["id"]))] if data.get("id") is not None else []
        if data.get("creator_id") is not None:
            buttons.append((f"{data.get('creator_name') or 'Creator'}'s profile", vortex_user_url(data["creator_id"])))
    else:
        embed = vortex_user_embed(data, color, signed_in=bool(secret))
        buttons = [("View profile", vortex_user_url(data["id"]))] if data.get("id") is not None else []
    embed.set_footer(text=f"Requested By {interaction.user.name} | playvortex.io" + (" | your saved login was rejected, run /vortexcookie" if rejected else ""))
    await interaction.edit_original_response(embed=embed, view=_link_view(*buttons) if buttons else None)


@bot.tree.command(name="vortexgame", description="Look up a Vortex game by its game ID")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def vortexgame(interaction: discord.Interaction, game: str):
    await _vortex_lookup(interaction, "games", game)


@bot.tree.command(name="vortexinfo", description="Look up a Vortex user by their user ID (not their username)")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def vortexinfo(interaction: discord.Interaction, user_id: str):
    if not str(user_id).strip().isdigit():
        await interaction.response.send_message("Vortex lookups use the **user ID** (the number in their profile link), not the username.", ephemeral=True)
        return
    await _vortex_lookup(interaction, "users", user_id)


@bot.tree.command(name="vortexcookie", description="Save your Vortex login so lookups are made as you")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def vortexcookie(interaction: discord.Interaction):
    # A form, so the cookie is never typed into the chat. It arrives in vortex_modal below.
    have = bool((usersDB.get(str(interaction.user.id)) or {}).get("vortex"))
    box = (yapper.main.TextInput.paragraph("cookie", "Your Vortex cookie").required(False).length(max=4000)
           .placeholder("name=value, just the value, or a Bearer token")
           .description("Leave empty to delete your saved login." if have else "Used only for your own lookups."))
    try:
        await interaction.response.send_modal(yapper.main.Modal("vortexcookie", "Vortex login", box))
    except NotImplementedError:
        await interaction.response.send_message("Run this as a slash command (pick /vortexcookie from the command list) so I can open the form.", ephemeral=True)


@bot.on_modal("vortexcookie")
def vortex_modal(i):
    uid = str(i.user_id)
    cookie = str(((i.get("values") if hasattr(i, "get") else None) or {}).get("cookie") or "").strip()    # i.values is dict.values: read the key
    entry = usersDB.get(uid)
    entry = dict(entry) if isinstance(entry, dict) else {}
    if not cookie:
        entry.pop("vortex", None)
        usersDB.set(uid, entry)
        return i.ephemeral("Your Vortex login was deleted.")
    if len(cookie) > 4000 or "\n" in cookie or not cookie.isprintable():
        return i.ephemeral("That doesn't look like a cookie (too long, or has line breaks). Nothing was saved.")
    entry["vortex"] = cookie
    usersDB.set(uid, entry)
    _vortex_cache.clear()
    return i.ephemeral("Saved. Your Vortex lookups now use your login. Run /vortexcookie and submit it empty to delete it.")


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

async def get_csrf_token(session):
    try:
        with open('roblosecuritytoken.txt', 'r') as f:
            roblosecurity_token = f.read().strip()
            
        async with session.post("https://auth.roblox.com/v2/logout", 
                              headers={'Cookie': f'.ROBLOSECURITY={roblosecurity_token}'}) as response:
            token = response.headers.get("x-csrf-token")
            return token
    except Exception:
        return None

async def render_custom_avatar(session, user_id):
    try:
        with open('roblosecuritytoken.txt', 'r') as f:
            roblosecurity_token = f.read().strip()
        
        headers = {'Cookie': f'.ROBLOSECURITY={roblosecurity_token}'}
        
        async with session.get(f'https://avatar.roblox.com/v1/users/{user_id}/avatar', 
                             headers=headers) as response:
            if response.status == 200:
                v1_avatar_details = await response.json()
                
                if v1_avatar_details and 'bodyColors' in v1_avatar_details:
                    avatar_definition = {
                        "assets": [{"id": asset["id"]} for asset in v1_avatar_details["assets"]],
                        "bodyColors": {
                            "headColor": roblox_colors.get(v1_avatar_details["bodyColors"]["headColorId"], "FFFFFF"),
                            "torsoColor": roblox_colors.get(v1_avatar_details["bodyColors"]["torsoColorId"], "FFFFFF"),
                            "leftArmColor": roblox_colors.get(v1_avatar_details["bodyColors"]["leftArmColorId"], "FFFFFF"),
                            "rightArmColor": roblox_colors.get(v1_avatar_details["bodyColors"]["rightArmColorId"], "FFFFFF"),
                            "leftLegColor": roblox_colors.get(v1_avatar_details["bodyColors"]["leftLegColorId"], "FFFFFF"),
                            "rightLegColor": roblox_colors.get(v1_avatar_details["bodyColors"]["rightLegColorId"], "FFFFFF"),
                        },
                        "scales": v1_avatar_details["scales"],
                        "playerAvatarType": {"playerAvatarType": v1_avatar_details["playerAvatarType"]}
                    }
                else:
                    return None
            else:
                return None

        render_payload = {
            "thumbnailConfig": {"thumbnailId": 1, "thumbnailType": "2dWebp", "size": "420x420"},
            "avatarDefinition": avatar_definition
        }

        csrf_token = await get_csrf_token(session)
        if not csrf_token:
            return None

        request_headers = {
            'Cookie': f'.ROBLOSECURITY={roblosecurity_token}',
            'x-csrf-token': csrf_token,
            'Content-Type': 'application/json'
        }
        
        for i in range(5):
            async with session.post("https://avatar.roblox.com/v1/avatar/render", 
                                  json=render_payload, headers=request_headers) as render_response:
                if render_response.status == 200:
                    render_data = await render_response.json()
                    if render_data.get('state') == 'Completed' and render_data.get('imageUrl'):
                        return render_data['imageUrl']
            
            if i < 4:
                await asyncio.sleep(2)
        
        return None

    except Exception:
        return None

@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@bot.tree.command(name="avatar", description="Get a Roblox user's avatar")
@app_commands.describe(user="Roblox username or user ID")
async def avatar_command(interaction: discord.Interaction, user: str):
    await interaction.response.defer()
    
    async def get_user_id(session, user_input):
        if user_input.isdigit():
            return user_input
        url = "https://users.roblox.com/v1/usernames/users"
        try:
            async with session.post(url, json={"usernames": [user_input]}) as response:
                if response.status == 200:
                    data = await response.json()
                    if data.get("data") and len(data["data"]) > 0:
                        return str(data["data"][0]["id"])
        except Exception:
            pass
        return None

    async def get_username_from_id(session, user_id):
        url = f"https://users.roblox.com/v1/users/{user_id}"
        try:
            async with session.get(url) as response:
                if response.status == 200:
                    data = await response.json()
                    return data.get("name")
        except Exception:
            pass
        return None

    connector = aiohttp.TCPConnector(family=socket.AF_INET)
    async with aiohttp.ClientSession(connector=connector) as session:
        user_id = await get_user_id(session, user)
        
        if not user_id:
            await interaction.followup.send("❌ Cannot find user")
            return

        username = await get_username_from_id(session, user_id)
        if not username:
            await interaction.followup.send("❌ Cannot find user")
            return

        try:
            user_url = f"https://users.roblox.com/v1/users/{user_id}"
            async with session.get(user_url) as response:
                if response.status == 200:
                    user_data = await response.json()
                    is_banned = user_data.get("isBanned", False)
                    
                    if is_banned:
                        image_url = await render_custom_avatar(session, user_id)
                        if image_url:
                            embed = discord.Embed(
                                title=f"{username}'s Avatar",
                                color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
                            )
                            embed.set_image(url=image_url)
                            embed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
                            await interaction.followup.send(embed=embed)
                        else:
                            await interaction.followup.send("❌ User is terminated")
                        return
                    else:
                        avatar_url = f"https://thumbnails.roblox.com/v1/users/avatar?userIds={user_id}&size=420x420&format=Png&isCircular=false"
                        async with session.get(avatar_url) as avatar_response:
                            if avatar_response.status == 200:
                                avatar_data = await avatar_response.json()
                                if avatar_data.get('data') and len(avatar_data['data']) > 0:
                                    image_url = avatar_data['data'][0]['imageUrl']
                                    embed = discord.Embed(
                                        title=f"{username}'s Avatar",
                                        color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
                                    )
                                    embed.set_image(url=image_url)
                                    embed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
                                    await interaction.followup.send(embed=embed)
                                    return
                        await interaction.followup.send("❌ Failed to fetch avatar")
                        return
                else:
                    await interaction.followup.send("❌ Cannot find user")
                    return
                    
        except Exception:
            await interaction.followup.send("Failed to fetch user data")
            return
            
class BadgeService:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
    
    async def get_user_id(self, user: str):
        if user.isdigit():
            return user
        return await self._get_id_from_username(user)
    
    async def _get_id_from_username(self, username: str):
        url = "https://users.roblox.com/v1/usernames/users"
        try:
            async with self.session.post(url, json={"usernames": [username]}) as response:
                if response.status == 200:
                    data = await response.json()
                    if data.get("data") and len(data["data"]) > 0:
                        return str(data["data"][0]["id"])
        except Exception:
            pass
        return None
    
    async def get_username(self, user_id: str):
        url = f"https://users.roblox.com/v1/users/{user_id}"
        try:
            async with self.session.get(url) as response:
                if response.status == 200:
                    data = await response.json()
                    return data.get("name")
        except Exception:
            pass
        return None
    
    async def get_user_badges(self, user_id: str, limit: int = 10):
        badges_url = f"https://badges.roblox.com/v1/users/{user_id}/badges?sortOrder=Desc&limit={limit}"
        
        async with self.session.get(badges_url) as response:
            if response.status != 200:
                return [], response.status
            
            data = await response.json()
            return data.get('data', []), response.status
    
    async def get_awarded_dates(self, user_id: str, badge_ids: list):
        if not badge_ids:
            return {}
            
        awarded_dates_url = f"https://badges.roblox.com/v1/users/{user_id}/badges/awarded-dates?badgeIds={','.join(map(str, badge_ids))}"
        
        try:
            async with self.session.get(awarded_dates_url) as response:
                if response.status == 200:
                    data = await response.json()
                    return {item['badgeId']: item.get('awardedDate') for item in data.get('data', [])}
        except Exception:
            pass
        return {}
    
    async def get_badge_thumbnail(self, badge):
        icon_image_id = badge.get('iconImageId') or badge.get('displayIconImageId')
        if not icon_image_id:
            return None
            
        icon_url = f"https://thumbnails.roblox.com/v1/assets?assetIds={icon_image_id}&size=150x150&format=Png&isCircular=false"
        
        try:
            async with self.session.get(icon_url) as response:
                if response.status == 200:
                    data = await response.json()
                    if data.get('data') and len(data['data']) > 0:
                        return data['data'][0]['imageUrl']
        except Exception:
            pass
        return None

class BadgeFormatter:
    @staticmethod
    def parse_iso_timestamp(timestamp_str):
        if not timestamp_str:
            return None
            
        try:
            timestamp_str = timestamp_str.split('.')[0]
            
            if timestamp_str.endswith('Z'):
                dt = datetime.fromisoformat(timestamp_str[:-1]).replace(
                    tzinfo=datetime.timezone.utc
                )
            else:
                if '+' not in timestamp_str and 'Z' not in timestamp_str:
                    timestamp_str += '+00:00'
                dt = datetime.fromisoformat(timestamp_str)
            
            return int(dt.timestamp())
        except (ValueError, AttributeError, TypeError):
            return None
    
    @staticmethod
    def format_creator_info(creator):
        creator_name = creator.get('name', 'Unknown')
        creator_type = creator.get('type', 'User')
        creator_id = creator.get('id')
        
        if not creator_id:
            return creator_name
            
        if creator_type.lower() == 'user':
            return f"[{creator_name}](https://www.roblox.com/users/{creator_id}/profile)"
        else:
            return f"[{creator_name}](https://www.roblox.com/groups/{creator_id}/)"
    
    @staticmethod
    def format_awarder_info(awarder):
        awarder_type = awarder.get('type', 'Unknown')
        awarder_id = awarder.get('id')
        
        if not awarder_id:
            return "Unknown"
            
        if awarder_type.lower() == 'place':
            return f"[Place #{awarder_id}](https://www.roblox.com/games/{awarder_id}/)"
        else:
            return f"{awarder_type} #{awarder_id}"

class BadgesView(discord.ui.View):
    def __init__(self, badges, username, user_id, requester, start_time, badge_service):
        super().__init__(timeout=120)
        self.badges = badges
        self.current_page = 0
        self.username = username
        self.user_id = user_id
        self.requester = requester
        self.start_time = start_time
        self.badge_service = badge_service
        self.message = None
        self.thumbnail_cache = {}
        self.update_buttons()
    
    async def preload_thumbnails(self):
        for i, badge in enumerate(self.badges):
            thumbnail_url = await self.badge_service.get_badge_thumbnail(badge)
            if thumbnail_url:
                self.thumbnail_cache[i] = thumbnail_url
    
    async def create_embed(self):
        badge = self.badges[self.current_page]
        
        embed = discord.Embed(
            title=f"{self.username}'s Recent Badges",
            color=embedDB.get(f"{self.requester.id}") if embedDB.get(f"{self.requester.id}") else discord.Color.blue(),
            timestamp=datetime.now(),
            url=f"https://www.roblox.com/users/{self.user_id}/badges"
        )
        
        thumbnail_url = self.thumbnail_cache.get(self.current_page)
        if thumbnail_url:
            embed.set_thumbnail(url=thumbnail_url)
        
        badge_name = badge.get('displayName') or badge.get('name', 'Unknown Badge')
        badge_description = badge.get('displayDescription') or badge.get('description', 'No description')
        badge_id = badge.get('id', 'N/A')
        
        embed.add_field(name="Badge Name", value=badge_name, inline=False)
        embed.add_field(name="Description", value=badge_description or "No description", inline=False)
        embed.add_field(name="Badge ID", value=f"`{badge_id}`", inline=True)
        
        awarded_date = badge.get('awardedDate')
        if awarded_date:
            unix_timestamp = BadgeFormatter.parse_iso_timestamp(awarded_date)
            if unix_timestamp:
                embed.add_field(
                    name="Awarded Date", 
                    value=f"<t:{unix_timestamp}:D> (<t:{unix_timestamp}:R>)",  
                    inline=False
                )
            else:
                embed.add_field(name="Awarded Date", value=awarded_date, inline=False)
        
        creator = badge.get('creator', {})
        creator_text = BadgeFormatter.format_creator_info(creator)
        embed.add_field(name="Creator", value=creator_text, inline=True)
        
        awarder = badge.get('awarder', {})
        awarder_text = BadgeFormatter.format_awarder_info(awarder)
        embed.add_field(name="Awarded By", value=awarder_text, inline=True)
        
        stats = badge.get('statistics', {})
        awarded_count = stats.get('awardedCount', 0)
        win_rate = stats.get('winRatePercentage', 0)
        
        embed.add_field(name="Times Awarded", value=f"{awarded_count:,}", inline=True)
        embed.add_field(name="Win Rate", value=f"{win_rate}%", inline=True)
        
        elapsed_time = asyncio.get_event_loop().time() - self.start_time
        embed.set_footer(
            text=f"Badge {self.current_page + 1}/{len(self.badges)} • Load time: {elapsed_time:.2f}s • Requested by {self.requester.display_name}"
        )
        
        return embed
        
    def update_buttons(self):
        self.clear_items()
        
        if self.current_page > 0:
            previous_btn = discord.ui.Button(style=discord.ButtonStyle.primary, label="<<", custom_id="previous")
            previous_btn.callback = self.previous_callback
            self.add_item(previous_btn)
            
        if self.current_page < len(self.badges) - 1:
            next_btn = discord.ui.Button(style=discord.ButtonStyle.primary, label=">>", custom_id="next")
            next_btn.callback = self.next_callback
            self.add_item(next_btn)
        
        current_badge = self.badges[self.current_page]
        badge_id = current_badge.get('id')
        if badge_id:
            link_btn = discord.ui.Button(
                style=discord.ButtonStyle.link,
                label="View Badge",
                url=f"https://www.roblox.com/badges/{badge_id}"
            )
            self.add_item(link_btn)
    
    async def _handle_navigation(self, interaction: discord.Interaction, direction: int):
        if interaction.user != self.requester:
            await interaction.response.send_message("You can't interact with this command!", ephemeral=True)
            return
        
        self.current_page += direction
        embed = await self.create_embed()
        self.update_buttons()
        await interaction.response.edit_message(embed=embed, view=self)
    
    async def previous_callback(self, interaction: discord.Interaction):
        await self._handle_navigation(interaction, -1)
    
    async def next_callback(self, interaction: discord.Interaction):
        await self._handle_navigation(interaction, 1)
    
    async def on_timeout(self):
        for item in self.children:
            if isinstance(item, discord.ui.Button) and item.style != discord.ButtonStyle.link:
                item.disabled = True
        try:
            await self.message.edit(view=self)
        except Exception:
            pass

@bot.tree.command(name="recentbadges", description="Get a user's most recently earned Roblox badges")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def recent_badges(interaction: discord.Interaction, user: str):
    await interaction.response.defer()
    
    thinkingembed = discord.Embed(
        title=f"{Emojis.get('loading')} {interaction.user.mention} Searching For {user}'s recent badges",
        color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
    )
    await interaction.followup.send(embed=thinkingembed)
    
    start_time = asyncio.get_event_loop().time()
    
    connector = aiohttp.TCPConnector(family=socket.AF_INET)
    async with aiohttp.ClientSession(connector=connector) as session:
        badge_service = BadgeService(session)
        
        try:
            user_id = await badge_service.get_user_id(user)
            if not user_id:
                embed = discord.Embed(
                    description=f"Cannot find user",
                    color=discord.Color.red()
                )
                await interaction.edit_original_response(embed=embed)
                return
            
            username = await badge_service.get_username(user_id)
            if not username:
                embed = discord.Embed(
                    description=f"Cannot find user",
                    color=discord.Color.red()
                )
                await interaction.edit_original_response(embed=embed)
                return
            
            badges, status_code = await badge_service.get_user_badges(user_id)
            
            if status_code == 403:
                embed = discord.Embed(
                    description="This user's inventory is private. Badges cannot be viewed.",
                    color=discord.Color.red()
                )
                await interaction.edit_original_response(embed=embed)
                return
            elif status_code != 200:
                embed = discord.Embed(
                    description=f"Failed to fetch badges",
                    color=discord.Color.red()
                )
                await interaction.edit_original_response(embed=embed)
                return
            
            if not badges:
                embed = discord.Embed(
                    description="This user has no badges.",
                    color=discord.Color.red()
                )
                await interaction.edit_original_response(embed=embed)
                return
            
            badge_ids = [badge['id'] for badge in badges]
            awarded_dates = await badge_service.get_awarded_dates(user_id, badge_ids)
            
            for badge in badges:
                badge_id = badge['id']
                badge['awardedDate'] = awarded_dates.get(badge_id)
            
            view = BadgesView(badges, username, user_id, interaction.user, start_time, badge_service)
            await view.preload_thumbnails()
            embed = await view.create_embed()
            view.update_buttons()
            message = await interaction.edit_original_response(embed=embed, view=view)
            view.message = message
            
        except asyncio.TimeoutError:
            embed = discord.Embed(
                description=f"The request timed out while fetching badge data.",
                color=discord.Color.red()
            )
            await interaction.edit_original_response(embed=embed)
        except aiohttp.ClientError as e:
            embed = discord.Embed(
                description=f"Failed to connect",
                color=discord.Color.red()
            )
            await interaction.edit_original_response(embed=embed)
        except Exception as e:
            print(f"Unexpected error details: {type(e).__name__}: {str(e)}")
            embed = discord.Embed(
                description=f"An unexpected error occurred",
                color=discord.Color.red()
            )
            await interaction.edit_original_response(embed=embed)

@bot.tree.command(name="instagram", description="Get detailed information about an Instagram user")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(username="Instagram username")
async def instagram_command(interaction: discord.Interaction, username: str):
    await interaction.response.defer(thinking=True)

    loading = discord.Embed(
        title=f"{Emojis.get('loading')} {interaction.user.mention} Getting Instagram Profile For @{username}",
        color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
    )
    await interaction.edit_original_response(embed=loading)

    try:
        username = username.strip().replace('@', '')

        if not username:
            errorembed = discord.Embed(
                description="Please provide a valid Instagram username",
                color=discord.Color.red()
            )
            await interaction.edit_original_response(embed=errorembed)
            return

        profile_info = await fetch_and_parse_instagram_profile(username)

        if isinstance(profile_info, str):
            errorembed = discord.Embed(description=f"{profile_info}", color=discord.Color.red())
            await interaction.edit_original_response(embed=errorembed)
            return

        embed = create_instagram_embed(username, profile_info, interaction.user.name)
        await interaction.edit_original_response(embed=embed)

    except Exception as e:
        errorembed = discord.Embed(
            description="Failed to fetch Instagram profile information",
            color=discord.Color.red()
        )
        await interaction.edit_original_response(embed=errorembed)

async def get_instagram_profile(session, username):
    url = f"https://i.instagram.com/api/v1/users/web_profile_info/?username={username}"

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "X-IG-App-ID": "936619743392459",
        "Referer": f"https://www.instagram.com/{username}/",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
    }

    try:
        async with session.get(url, headers=headers, timeout=10) as response:
            if response.status == 404:
                return None, "User not found"
            response.raise_for_status()
            data = await response.json()
            return data["data"]["user"], None
    except aiohttp.ClientError as e:
        return None, f"Connection error: {str(e)}"
    except Exception as e:
        return None, f"Error fetching profile: {str(e)}"

def parse_instagram_data(user):
    return {
        "username": user["username"],
        "full_name": user["full_name"],
        "biography": user["biography"],
        "followers": user["edge_followed_by"]["count"],
        "following": user["edge_follow"]["count"],
        "posts": user["edge_owner_to_timeline_media"]["count"],
        "profile_pic_url": user["profile_pic_url_hd"],
        "is_verified": user["is_verified"],
        "is_private": user["is_private"],
    }

async def fetch_and_parse_instagram_profile(username):
    async with aiohttp.ClientSession() as session:
        user, error = await get_instagram_profile(session, username)

        if error:
            return error

        return parse_instagram_data(user)

def create_instagram_embed(username, profile_info, requester_name):
    profile_url = f"https://www.instagram.com/{username}/"

    title_display = f"@{username}"
    if profile_info['is_verified']:
        title_display += " <:VerifiedInstagram:1447033802802331760>"

    embed = discord.Embed(
        title=title_display,
        url=profile_url,
        color=0xE1306C
    )

    if profile_info['profile_pic_url']:
        embed.set_thumbnail(url=profile_info['profile_pic_url'])

    embed.add_field(name="Posts", value=f"**{profile_info['posts']:,}**", inline=True)
    embed.add_field(name="Followers", value=f"**{profile_info['followers']:,}**", inline=True)
    embed.add_field(name="Following", value=f"**{profile_info['following']:,}**", inline=True)
    embed.add_field(name="Account Type", value="Private" if profile_info['is_private'] else "Public", inline=True)

    if profile_info['full_name']:
        embed.add_field(name="Full Name", value=profile_info['full_name'], inline=True)

    if profile_info['biography']:
        bio_text = profile_info['biography']
        if len(bio_text) > 1024:
            bio_text = bio_text[:1021] + "..."
        embed.add_field(name="Description", value=bio_text, inline=False)

    embed.set_footer(text=f"Requested By {requester_name} │ {MainURL}")

    return embed

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
    """The Shapes API (same machine) presenting the internal token. It is how api.shapes.lol reads /mutuals for its keyed
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
    counts = human_counts(servers)
    return [{"id": s.get("id"), "name": s.get("name") or str(s.get("id")), "members": counts.get(str(s.get("id")), "") if counts.get(str(s.get("id"))) is not None else ""}
            for s in servers]


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
    """A picture for people with none: their initial on a colour picked from their id. Served by /avatar/<id>.svg."""
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
    counts = human_counts(raw)                    # people only: the API's own count includes bots
    for s in raw:
        sid = str(s.get("id"))
        people = counts.get(sid)
        try:
            total = int(_first(s, "member_count", "members_count", "memberCount"))
        except (TypeError, ValueError):
            total = people
        total = max(total, people) if total is not None and people is not None else total
        servers.append({**dict(s), "id": sid, "name": s.get("name") or sid, "member_count": total, "people": people,
                        "bots": (total - people) if total is not None and people is not None else None, "icon": _icon_of(s)})
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
    """The embed colour (an int) for a message sent as Shapes: choice is "mine" (their saved /settings colour, else blue),
    a name from the colour list, "random", or "custom" with custom = "#rrggbb". Raises ValueError for anything else."""
    choice = str(choice or "mine").strip()
    if choice.lower() == "mine":
        saved = embedDB.get(f"{user_id}") if user_id else None
        return int(saved) if isinstance(saved, int) else discord.Color.blue().value
    if choice.lower() == "random":
        return discord.Color.random().value
    if choice.lower() == "custom":
        found = re.fullmatch(r"#?([0-9a-fA-F]{6})", str(custom or "").strip())
        if not found:
            raise ValueError("A custom colour looks like #5865f2.")
        return int(found.group(1), 16)
    for name, color in DiscordColors:
        if name.lower() == choice.lower():
            return color.value
    raise ValueError("Pick a colour from the list, or use a custom one.")


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
    """Plain text when there is no image, title, colour or sender line; otherwise an embed. If the server refuses embeds, falls
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


def _all_members(server_id, limit=5000):
    """Every member (paged 200 at a time), as flat dicts from _person."""
    out, after = [], None
    while len(out) < limit:
        page = bot.members(server_id, after=after)
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


def stop_web():
    """Close the web server's socket (so the port is free again)."""
    if _web_server is not None:
        _web_server.shutdown()
        _web_server.server_close()


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

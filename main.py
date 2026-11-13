import sys
sys.dont_write_bytecode = True      # don't litter __pycache__ folders
import os
import sys
import threading
import atexit
import time
import json
import signal
import asyncio
import discord
import aiohttp
import pytz
import numbers
#import pandas as pd # Last resort if i keep getting json errors
from slpp import slpp as lua
from datetime import datetime, timezone
from discord import app_commands
import requests
from discord.ext import commands
from discord.gateway import DiscordWebSocket
from discord.ext.commands import Bot
from flask import Flask, render_template_string, request, redirect, url_for, session, jsonify, Response
from flask_cors import CORS
import base64
import urllib
import urllib.parse
import hmac
import random
import secrets
import hashlib
import re
import io
import socket
import typing
from typing import Dict, Any, Optional
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()      # every secret and setting comes from the environment: a .env file next to main.py (see .env.example) or the host's variables
import nacl.signing
import nacl.exceptions

# Setup Emojis
Emojis = {
    "loading": "<a:loading:1416950730094542881>",
    "RobloxEmojis": {
        "rolimons": "<:RolimonsLogo:1417258794974711901>",
        "logo": "<:RobloxLogo:1416951004607418398>",
        "verified": "<:RobloxVerified:1416951927513677874>",
        "premium": "<:RobloxPremium:1416951078200541378>",
        "admin": "<:RobloxAdmin:1416951128876122152>",
        "inviter": "<:RobloxInviter:1416952415772479559>",
        "homestead": "<:Roblox100Visits:1416952056324952184>",
        "ambassador": "<:Ambassador:1430627877337960548>",
        "friendship": "<:Friendship:1430641140679577630>",
        "warrior": "<:Warrior:1430640757403943063>",
        "game": "<:RobloxInGame:1430640335393915041>",
        "studio": "<:RobloxInStudio:1430627885261262869>",
        "online": "<:RobloxOnline:1430627882912190608>",
        "offline": "<:RobloxOffline:1430627883809902632>"
    }
}

# OpenAI client
chatgpt = OpenAI(api_key=os.getenv("OPENAI_KEY"), base_url="https://openrouter.ai/api/v1") # MapleAI is a scam.
AIModel = "openai/gpt-oss-20b:free"

# =============================================================================================================
# Database: Cloudflare D1 (the Discord bot's own database, separate from the Yapin' bot's and never read by it).
# Every store (counting, embed, users, ...) is a table with real columns, one row per key. Reads come from memory,
# save() hands changes to a background writer, so nothing waits on the network. First start: rows from the old pickleDB
# files (counting.db, embed.db, ...) are copied in once, read back and compared; the old files are left alone.
# Without CLOUDFLARE_API_TOKEN / CLOUDFLARE_ACCOUNT_ID / CLOUDFLARE_D1_DATABASE_ID it keeps using local files.
# =============================================================================================================
FLUSH_INTERVAL = 3         # seconds between background passes (a set()/save() triggers one sooner)
BATCH_SIZE = 50            # statements per request


class Spec:
    def __init__(self, table, pk, cols=(), scalar=None):
        self.table, self.pk, self.cols, self.scalar = table, pk, tuple(cols), scalar
        self.types = dict(self.cols)


# column types: text, int, bool, json (any JSON value, kept as text)
SPECS = {
    "counting": Spec("bot_counting", "server_id", [("channel", "text"), ("number", "int"), ("enabled", "bool"), ("warnings", "int"),
                                                   ("lastcounter", "text"), ("highestnumber", "int"), ("banned", "bool"), ("emojis", "json")]),
    "embed": Spec("bot_embed", "user_id", [("color", "int")], scalar="color"),
    "users": Spec("bot_users", "user_id"),
    "autorole": Spec("bot_autorole", "server_id", [("role_id", "text"), ("enabled", "bool"), ("exempt_users", "json"), ("include_bots", "bool")]),
    "ai": Spec("bot_ai", "user_id", [("username", "text"), ("user_messages", "json"), ("ai_responses", "json")]),
    "rverify": Spec("bot_rverify", "server_id", [("roles", "json"), ("made", "json"), ("last", "json")]),
    "cmdlog": Spec("bot_cmdlog", "log_id", [("channel", "text"), ("enabled", "bool"), ("events", "bool"), ("commands", "bool"),
                                            ("refused", "bool"), ("retry_at", "int")]),
    "actions": Spec("bot_actions", "entry_id", [("at", "int"), ("actor_id", "text"), ("actor", "text"), ("kind", "text"), ("detail", "text"),
                                                ("server_id", "text"), ("target", "text")]),
    "dmlog": Spec("bot_dmlog", "entry_id", [("at", "int"), ("dir", "text"), ("user_id", "text"), ("name", "text"), ("text", "text"), ("source", "text")]),
    "lastonline": Spec("bot_lastonline", "roblox_id", [("at", "int")], scalar="at"),
}
# A store with no spec still works: it lives in bot_misc.

META_SQL = "CREATE TABLE IF NOT EXISTS bot_meta (k TEXT PRIMARY KEY, v TEXT NOT NULL)"
MISC_SQL = "CREATE TABLE IF NOT EXISTS bot_misc (db TEXT NOT NULL, k TEXT NOT NULL, v TEXT NOT NULL, PRIMARY KEY (db, k))"

_client = None
_client_lock = threading.Lock()
_stores = []
_mode = None               # "d1" | "local"
_mode_lock = threading.Lock()


def _q(name):
    return '"' + name.replace('"', '""') + '"'


def d1_configured():
    return bool(os.environ.get("CLOUDFLARE_API_TOKEN") and os.environ.get("CLOUDFLARE_ACCOUNT_ID") and os.environ.get("CLOUDFLARE_D1_DATABASE_ID"))


def _cf():
    global _client
    if not d1_configured():
        return None
    with _client_lock:
        if _client is None:
            from cloudflare import Cloudflare
            _client = Cloudflare(api_token=os.environ["CLOUDFLARE_API_TOKEN"])
    return _client


def _query(sql, params=None):
    page = _cf().d1.database.query(os.environ["CLOUDFLARE_D1_DATABASE_ID"], account_id=os.environ["CLOUDFLARE_ACCOUNT_ID"], sql=sql,
                                   **({"params": [str(p) for p in params]} if params else {}))
    rows = []
    for result in page:
        rows.extend(getattr(result, "results", None) or [])
    return rows


def _batch(statements):
    _cf().d1.database.query(os.environ["CLOUDFLARE_D1_DATABASE_ID"], account_id=os.environ["CLOUDFLARE_ACCOUNT_ID"],
                            batch=[{"sql": sql, "params": [str(p) for p in params]} for sql, params in statements])


# ------------------------------------------------------------------------------------------------ rows <-> values

def _fits(kind, v):
    if kind == "text":
        return isinstance(v, str)
    if kind == "int":
        return isinstance(v, int) and not isinstance(v, bool)
    if kind == "bool":
        return isinstance(v, bool)
    return True


def to_row(spec, value):
    cols, extra = {n: None for n, _ in spec.cols}, {}
    if not isinstance(value, dict):
        if spec.scalar and _fits(spec.types[spec.scalar], value):
            cols[spec.scalar] = value
        else:
            extra["__value__"] = value
        return cols, extra
    for k, v in value.items():
        if v is None:
            continue
        kind = spec.types.get(k) if isinstance(k, str) else None
        if kind and _fits(kind, v):
            cols[k] = v
        else:
            extra[k] = v
    return cols, extra


def _py(kind, v):
    if kind == "json":
        return json.loads(v)
    if kind == "bool":
        return bool(int(v))
    if kind == "int":
        return int(v)
    return str(v)


def from_row(spec, row):
    extra = json.loads(row["extra_json"]) if row.get("extra_json") else {}
    if "__value__" in extra and len(extra) == 1:
        return extra["__value__"]
    if spec.scalar and row.get(spec.scalar) is not None and not extra:
        return _py(spec.types[spec.scalar], row[spec.scalar])
    rec = {n: _py(kind, row[n]) for n, kind in spec.cols if row.get(n) is not None}
    rec.update(extra)
    return rec


def upsert_statement(spec, key, value):
    cols, extra = to_row(spec, value)
    names = [spec.pk] + [n for n, _ in spec.cols] + ["extra_json"]
    frags, params = ["?"], [str(key)]
    for name, kind in spec.cols:
        v = cols[name]
        if v is None:
            frags.append("NULL")        # every bound value travels as text, so NULL goes in as a literal
        elif kind == "json":
            frags.append("?")
            params.append(json.dumps(v, sort_keys=True))
        elif kind == "bool":
            frags.append("?")
            params.append(1 if v else 0)
        else:
            frags.append("?")
            params.append(v)
    frags.append("?" if extra else "NULL")
    if extra:
        params.append(json.dumps(extra, sort_keys=True))
    sets = ", ".join(f"{_q(n)} = excluded.{_q(n)}" for n in names[1:])
    return (f"INSERT INTO {spec.table} ({', '.join(_q(n) for n in names)}) VALUES ({', '.join(frags)}) "
            f"ON CONFLICT({_q(spec.pk)}) DO UPDATE SET {sets}"), params


def create_statement(spec):
    cols = ", ".join(f"{_q(n)} {'INTEGER' if k in ('int', 'bool') else 'TEXT'}" for n, k in spec.cols)
    return f"CREATE TABLE IF NOT EXISTS {spec.table} ({_q(spec.pk)} TEXT PRIMARY KEY, {cols}{', ' if cols else ''}extra_json TEXT)"


def _norm(v):
    return {k: x for k, x in v.items() if x is not None} if isinstance(v, dict) else v


def _fingerprint(value):
    try:
        return json.dumps(value, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return repr(value)


# ------------------------------------------------------------------------------------------------ setup

def _ensure_mode():
    global _mode
    with _mode_lock:
        if _mode:
            return _mode
        if not _cf():
            _mode = "local"
            print("[db] Cloudflare D1 isn't configured (CLOUDFLARE_API_TOKEN / CLOUDFLARE_ACCOUNT_ID / CLOUDFLARE_D1_DATABASE_ID): using local files")
            return _mode
        _batch([(META_SQL, []), (MISC_SQL, [])] + [(create_statement(s), []) for s in SPECS.values()])
        _mode = "d1"
        return _mode


def _meta_get(key):
    rows = _query("SELECT v FROM bot_meta WHERE k = ?", [key])
    return rows[0]["v"] if rows else None


def _meta_set(key, value):
    _query("INSERT INTO bot_meta (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v", [key, value])


def _flush_all_at_exit():
    for s in list(_stores):
        try:
            s.flush()
        except Exception as e:
            print(f"[db] final save failed for {s.name}: {e}")


atexit.register(_flush_all_at_exit)


def flush_all():
    """Write every store now (blocks). Returns True when everything was written."""
    ok = True
    for s in list(_stores):
        ok = s.flush() and ok
    return ok


class Table:
    """One store. Same methods the PickleDB objects had that the bot uses."""

    def __init__(self, name, legacy_file=None):
        self.name = name
        self.spec = SPECS.get(name)
        self._data = {}
        self._written = {}               # key -> fingerprint of what D1 (or the file) holds
        self._lock = threading.RLock()
        self._io = threading.Lock()      # one write pass at a time
        self._wake = threading.Event()
        self._path = legacy_file or f"{name}.db"
        self.mode = _ensure_mode()
        self._load()
        _stores.append(self)
        threading.Thread(target=self._writer, name=f"db-{name}", daemon=True).start()

    # ---- loading ----

    def _read_legacy(self):
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except Exception as e:
            print(f"[db] could not read {self._path}: {e}")
            return {}

    def _load(self):
        if self.mode == "local":
            self._data = self._read_legacy()
            self._written = {k: _fingerprint(v) for k, v in self._data.items()}
            return
        if self.spec:
            for row in _query(f"SELECT * FROM {self.spec.table}"):
                self._data[str(row[self.spec.pk])] = from_row(self.spec, row)
        else:
            for row in _query("SELECT k, v FROM bot_misc WHERE db = ?", [self.name]):
                self._data[row["k"]] = json.loads(row["v"])
        self._written = {k: _fingerprint(v) for k, v in self._data.items()}
        self._import_legacy()

    def _import_legacy(self):
        """Copy the old pickleDB file in once, verified."""
        mark = f"imported:{self.name}"
        if _meta_get(mark) is not None:
            return
        old = self._read_legacy()
        if not old:
            _meta_set(mark, f"nothing to import {int(time.time())}")
            return
        todo = {k: v for k, v in old.items() if str(k) not in self._data}
        statements = [self._statement(str(k), v) for k, v in todo.items()]
        for i in range(0, len(statements), BATCH_SIZE):
            _batch(statements[i:i + BATCH_SIZE])
        back = {}
        if self.spec:
            back = {str(r[self.spec.pk]): from_row(self.spec, r) for r in _query(f"SELECT * FROM {self.spec.table}")}
        else:
            back = {r["k"]: json.loads(r["v"]) for r in _query("SELECT k, v FROM bot_misc WHERE db = ?", [self.name])}
        bad = [str(k) for k, v in todo.items() if str(k) not in back or _norm(back[str(k)]) != _norm(json.loads(json.dumps(v)))]
        if bad:
            print(f"[db] import of {self._path} FAILED the read-back check for {len(bad)} of {len(todo)} rows (first: {', '.join(bad[:5])}); it will retry next start")
            return
        for k, v in todo.items():
            self._data[str(k)] = back[str(k)]
            self._written[str(k)] = _fingerprint(back[str(k)])
        _meta_set(mark, f"{len(todo)} rows {int(time.time())}")
        print(f"[db] imported {len(todo)} rows from {self._path} into D1 and verified every one ({self._path} is left as it was)")

    # ---- dict-like API ----

    def get(self, key, default=None):
        with self._lock:
            return self._data.get(str(key), default)

    def set(self, key, value):
        with self._lock:
            self._data[str(key)] = value
        self._wake.set()
        return True

    def exists(self, key):
        with self._lock:
            return str(key) in self._data

    def remove(self, key):
        with self._lock:
            existed = self._data.pop(str(key), None) is not None
        self._wake.set()
        return existed

    rem = remove

    def all(self):
        with self._lock:
            return list(self._data.keys())

    getall = all

    def items(self):
        with self._lock:
            return dict(self._data)

    def save(self):
        """Ask the background writer to send what changed. Returns at once."""
        self._wake.set()
        return True

    # ---- writing ----

    def _statement(self, key, value):
        if self.spec:
            return upsert_statement(self.spec, key, value)
        return ("INSERT INTO bot_misc (db, k, v) VALUES (?, ?, ?) ON CONFLICT(db, k) DO UPDATE SET v = excluded.v", [self.name, key, json.dumps(value)])

    def _delete_statement(self, key):
        if self.spec:
            return (f"DELETE FROM {self.spec.table} WHERE {_q(self.spec.pk)} = ?", [key])
        return ("DELETE FROM bot_misc WHERE db = ? AND k = ?", [self.name, key])

    def flush(self):
        """Write what changed, now. True if everything was written (a failure is kept and retried on the next pass)."""
        with self._io:
            with self._lock:
                current = {k: (v, _fingerprint(v)) for k, v in self._data.items()}
                written = dict(self._written)
            dirty = {k: v for k, (v, fp) in current.items() if written.get(k) != fp}
            deleted = [k for k in written if k not in current]
            if not dirty and not deleted:
                return True
            if self.mode == "local":
                try:
                    with self._lock:
                        snapshot = dict(self._data)
                    tmp = self._path + ".tmp"
                    with open(tmp, "w", encoding="utf-8") as f:
                        json.dump(snapshot, f)
                    os.replace(tmp, self._path)
                except Exception as e:
                    print(f"[db] could not write {self._path}: {e}")
                    return False
                with self._lock:
                    self._written = {k: _fingerprint(v) for k, v in snapshot.items()}
                return True
            work = [(("U", k), self._statement(k, v)) for k, v in dirty.items()] + [(("D", k), self._delete_statement(k)) for k in deleted]
            ok = True
            for i in range(0, len(work), BATCH_SIZE):
                chunk = work[i:i + BATCH_SIZE]
                try:
                    _batch([s for _, s in chunk])
                except Exception as e:
                    ok = False
                    print(f"[db] write failed for {self.name} ({len(chunk)} rows), will retry: {e}")
                    continue
                with self._lock:
                    for (kind, k), _ in chunk:
                        if kind == "U":
                            self._written[k] = current[k][1]
                        else:
                            self._written.pop(k, None)
            return ok

    def _writer(self):
        while True:
            self._wake.wait(FLUSH_INTERVAL)
            self._wake.clear()
            time.sleep(0.2)              # let a burst of set() calls land in one pass
            try:
                self.flush()
            except Exception as e:
                print(f"[db] background save error in {self.name}: {e}")


def summary():
    return ", ".join(f"{s.name}={s.mode}" for s in _stores) or "no stores"

#=== Database Setup ===
countingDB = Table('counting')
embedDB = Table('embed')
usersDB = Table('users')
autoroleDB = Table('autorole')
AI_DB = Table('ai')
actionsDB = Table('actions')                # what was done from the web dashboard
dmlogDB = Table('dmlog')                   # every DM the bot sends or receives
rverifyDB = Table('rverify')                 # server id -> Roblox role mapping and the last sync's result
cmdlogDB = Table('cmdlog')                   # server id (or 'botlog') -> where its command log posts
lastOnlineDB = Table('lastonline')       # roblox user id -> unix time they were last seen online

# === Discord Bot Setup ===
intents = discord.Intents.default()
intents.guilds = True
intents.message_content = True
owner = "sl.ip"
co_owner = "<@481295611417853982>"
MainURL = "https://shapes.lol"
searchengine = "621a38269031b4e89" # PLEASE USE YOUR OWN SEARCH ENGINE ID FROM https://cse.google.com/

# Secrets and settings, all from the environment (.env). See .env.example for the full list.
PUBLIC_KEY = os.environ.get("DISCORD_PUBLIC_KEY")
APIBaseURL_Key = (os.environ.get("SHAPES_API_TOKEN") or os.environ.get("Shapes_API_Key") or "").strip()    # bearer token for the Shapes API's /internal routes
APIBaseURL_Headers = {"Authorization": f"Bearer {APIBaseURL_Key}"}
token = (os.environ.get("DISCORD_TOKEN") or "").strip()                    # the bot token
ADMIN_KEY = (os.environ.get("ADMIN_KEY") or "").strip()                    # password for the /login admin page (empty = the page can't be unlocked)
GoogleAPIKey = (os.environ.get("GOOGLE_API_KEY") or "").strip()            # Google Custom Search key for /google
ROBLOSECURITY = (os.environ.get("ROBLOSECURITY") or "").strip()            # a Roblox account's .ROBLOSECURITY cookie (presence, avatar renders, profile data)
APIBaseURL = (os.environ.get("SHAPES_API_URL") or "http://localhost:1337").rstrip("/")     # the Shapes API


# last online cache
def load_cached_timestamps():
    return lastOnlineDB.items()

def update_cached_timestamp(user_id, timestamp):
    lastOnlineDB.set(user_id, timestamp)

# === Flask App Setup ===
app = Flask(__name__)
CORS(app)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)       # signs the dashboard login; set FLASK_SECRET_KEY to stay signed in across restarts

# === Admin dashboard (the web pages in web/: the same ones the Yapin' bot uses) ===
# Sign in with ADMIN_KEY at /login. Everything the pages need is under /api: servers, members, channels, DMs, logs.
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
dash_started = time.time()
_login_attempts = {}


def _web_file(name):
    with open(os.path.join(WEB_DIR, name), "r", encoding="utf-8") as f:
        return f.read()


def _too_many_logins(ip):
    now = time.time()
    tries = [t for t in _login_attempts.get(ip, []) if now - t < 600]
    _login_attempts[ip] = tries
    return len(tries) >= 8


def admin_required(f):
    def wrapped(*args, **kwargs):
        if session.get("admin") is not True:
            if request.path.startswith("/api/"):
                return jsonify({"error": "login required"}), 401
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    wrapped.__name__ = f.__name__
    return wrapped


def dash_run(coro, timeout=30):
    """Run a coroutine on the bot's event loop from a Flask thread and wait for its result."""
    return asyncio.run_coroutine_threadsafe(coro, bot.loop).result(timeout)


def log_action(kind, detail, server_id="", target=""):
    """Everything done from the dashboard is written down (shown under Logs)."""
    try:
        now = time.time()
        actionsDB.set(f"{int(now * 1000)}-{os.urandom(2).hex()}", {"at": int(now), "actor_id": "admin", "actor": "Admin key", "kind": kind,
                                                                    "detail": str(detail)[:400], "server_id": str(server_id or ""), "target": str(target or "")})
        keys = sorted(actionsDB.items())
        for old in keys[:max(0, len(keys) - 2000)]:
            actionsDB.remove(old)
    except Exception as e:
        print(f"[dash] couldn't log the action: {e}")


@app.route("/status")
def status():
    return "OK", 200


@app.route("/activity")
def activity():
    return redirect(url_for("admin_login"))


@app.route("/login", methods=["GET", "POST"])
def admin_login():
    if session.get("admin") is True:
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


@app.route("/", methods=["GET"])
@app.route("/admin", methods=["GET"])
@admin_required
def dashboard():
    if not bot_ready:
        return Response("<!doctype html><meta charset=utf-8><meta http-equiv=refresh content=3><body style='font-family:system-ui;padding:3rem'>The bot is still starting. This page reloads on its own.", mimetype="text/html")
    return Response(_web_file("app.html").replace("<!--SIGNOUT-->", '<a href="/logout" id="signout">Sign out</a>'), mimetype="text/html")


DASH_COLORS = {"Blue": 0x5865F2, "Green": 0x57F287, "Yellow": 0xFEE75C, "Red": 0xED4245, "Pink": 0xEB459E, "Purple": 0x9B59B6, "Orange": 0xE67E22, "White": 0xFFFFFF}


def _dash_color(choice):
    choice = str(choice or "Blue")
    if choice == "random":
        return random.choice(list(DASH_COLORS.values()))
    if choice == "mine":
        choice = "Blue"
    if choice not in DASH_COLORS:
        raise ValueError("That isn't one of the colours.")
    return DASH_COLORS[choice]


def _dash_valid_image(url):
    url = str(url or "").strip()
    if not url:
        return None
    if not re.match(r"^https://[^\s]+$", url) or len(url) > 800:
        raise ValueError("The image link must start with https:// and have no spaces.")
    return url


def _dash_avatar(u):
    try:
        return str(u.display_avatar.replace(size=64).url)
    except Exception:
        return None


def _dash_person(m):
    u = m._user if isinstance(m, discord.Member) and hasattr(m, "_user") else m
    return {"id": str(m.id), "name": m.name, "display": m.display_name, "avatar": _dash_avatar(m), "bot": bool(m.bot),
            "roles": [str(r.id) for r in getattr(m, "roles", []) if r.name != "@everyone"],
            "joined": m.joined_at.isoformat() if getattr(m, "joined_at", None) else None}


def _dash_guild(server_id):
    g = bot.get_guild(int(server_id)) if str(server_id).isdigit() else None
    if g is None:
        raise LookupError("The bot isn't in that server.")
    return g


def _dash_error(e):
    code = 404 if isinstance(e, (LookupError, discord.NotFound)) else 403 if isinstance(e, discord.Forbidden) else 502
    return jsonify({"error": str(e) or e.__class__.__name__}), code


@app.route("/api/overview")
@admin_required
def api_overview():
    if not bot_ready:
        return jsonify({"ready": False}), 503
    servers = []
    for g in sorted(bot.guilds, key=lambda g: g.name.lower()):
        bots = people = None
        if g.chunked:
            bots = sum(1 for m in g.members if m.bot)
            people = len(g.members) - bots
        total = max(g.member_count or 0, len(g.members)) if g.chunked else (g.member_count or 0)
        servers.append({"id": str(g.id), "name": g.name, "member_count": total, "people": people, "bots": bots, "icon": g.icon.url if g.icon else None})
    me = bot.user
    return jsonify({
        "ready": True, "me": {"username": "Shapes", "avatar": str(me.display_avatar.replace(size=128).url) if me else None},
        "uptime_seconds": int(time.time() - dash_started), "servers": servers,
        "commands": sorted(c.qualified_name for c in bot.tree.walk_commands() if not isinstance(c, discord.app_commands.Group)),
        "storage": {"backend": "Cloudflare D1" if _mode == "d1" else "local files"},
        "is_owner": True, "colors": [{"name": n, "hex": "#%06x" % v} for n, v in DASH_COLORS.items()],
    })


@app.route("/api/servers/<server_id>")
@admin_required
def api_server(server_id):
    try:
        g = _dash_guild(server_id)
        cats = []
        loose = [c for c in g.channels if c.category is None and not isinstance(c, discord.CategoryChannel)]
        groups = [(None, sorted(loose, key=lambda c: c.position))] + [(cat, cat.channels) for cat in g.categories]
        for cat, chans in groups:
            rows = [{"id": str(c.id), "name": c.name, "type": c.type.name, "topic": getattr(c, "topic", None) or "",
                     "private": False, "position": c.position} for c in chans]
            if rows:
                cats.append({"id": str(cat.id) if cat else None, "name": cat.name if cat else "", "channels": rows})
        roles = [{"id": str(r.id), "name": r.name, "color": ("#%06x" % r.color.value) if r.color.value else None, "position": r.position,
                  "admin": r.permissions.administrator} for r in sorted(g.roles, key=lambda r: -r.position) if not r.is_default()]
        return jsonify({"server": {"id": str(g.id), "name": g.name, "owner_id": str(g.owner_id)}, "categories": cats, "roles": roles})
    except Exception as e:
        return _dash_error(e)


@app.route("/api/servers/<server_id>/members")
@admin_required
def api_members(server_id):
    try:
        g = _dash_guild(server_id)
        if not g.chunked:
            dash_run(g.chunk())
        after = int(request.args.get("after") or 0)
        rows = [m for m in sorted(g.members, key=lambda m: m.id) if m.id > after][:200]
        return jsonify({"members": [_dash_person(m) for m in rows], "next": str(rows[-1].id) if len(rows) >= 200 else None})
    except Exception as e:
        return _dash_error(e)


def _dash_message(m):
    images = [a.url for a in m.attachments if (a.content_type or "").startswith("image")]
    for e in m.embeds:
        for part in (e.image, e.thumbnail):
            if part and part.url:
                images.append(part.url)
    return {"id": str(m.id), "author": m.author.display_name, "author_id": str(m.author.id), "avatar": _dash_avatar(m.author), "bot": bool(m.author.bot),
            "content": m.content or "", "created_at": m.created_at.isoformat(),
            "embeds": [{"title": e.title, "description": e.description, "url": e.url} for e in m.embeds], "images": images[:6]}


@app.route("/api/channels/<channel_id>/messages")
@admin_required
def api_messages(channel_id):
    limit = max(1, min(100, request.args.get("limit", 50, type=int)))

    async def load():
        ch = bot.get_channel(int(channel_id)) or await bot.fetch_channel(int(channel_id))
        return [_dash_message(m) async for m in ch.history(limit=limit)]
    try:
        return jsonify({"messages": dash_run(load())})
    except Exception as e:
        return _dash_error(e)


@app.route("/api/channels/<channel_id>/send", methods=["POST"])
@admin_required
def api_send(channel_id):
    body = request.get_json(silent=True) or {}
    text = str(body.get("message", "")).strip()
    try:
        image = _dash_valid_image(body.get("image_url"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if not text and not image:
        return jsonify({"error": "Write a message or add an image link."}), 400
    hidden = body.get("hidden") is True
    if not hidden:
        text = (text + "\n" if text else "") + "-# Sent from the Shapes dashboard"

    async def go():
        ch = bot.get_channel(int(channel_id)) or await bot.fetch_channel(int(channel_id))
        await ch.send(text[:2000], embed=discord.Embed(color=0x2b2d31).set_image(url=image) if image else None)
    try:
        dash_run(go())
        log_action("send", ("(hidden sender) " if hidden else "") + text[:200], target=f"channel {channel_id}")
        return jsonify({"ok": True})
    except Exception as e:
        return _dash_error(e)


# --- direct messages: everything the bot sends or receives is in dmlogDB; conversations are built from it ---
@app.route("/api/dms/log")
@admin_required
def api_dm_log():
    who = request.args.get("user_id", "").strip()
    rows = [v for v in dmlogDB.items().values() if isinstance(v, dict) and (not who or v.get("user_id") == who)]
    rows.sort(key=lambda r: -int(r.get("at") or 0))
    return jsonify({"log": rows[:300], "total": len(rows)})


@app.route("/api/dms")
@admin_required
def api_dms():
    convo = {}
    for r in dmlogDB.items().values():
        if isinstance(r, dict) and r.get("user_id"):
            c = convo.setdefault(r["user_id"], {"user_id": r["user_id"], "channel_id": "", "name": r.get("name") or r["user_id"], "last": 0})
            c["last"] = max(c["last"], int(r.get("at") or 0))
            c["name"] = r.get("name") or c["name"]
    return jsonify({"dms": sorted(convo.values(), key=lambda c: -c["last"])[:200]})


@app.route("/api/users/search")
@admin_required
def api_user_search():
    q = request.args.get("q", "").strip().lower().lstrip("@")
    if len(q) < 2:
        return jsonify({"users": []})
    seen = {}
    for g in bot.guilds:
        for m in g.members:
            if q in m.name.lower() or q in m.display_name.lower() or q == str(m.id):
                seen.setdefault(m.id, _dash_person(m))
        if len(seen) >= 15:
            break
    if not seen and q.isdigit():
        try:
            u = dash_run(bot.fetch_user(int(q)))
            seen[u.id] = {"id": str(u.id), "name": u.name, "display": u.display_name, "avatar": _dash_avatar(u), "bot": u.bot, "roles": [], "joined": None}
        except Exception:
            pass
    return jsonify({"users": list(seen.values())[:15]})


@app.route("/api/dms", methods=["POST"])
@admin_required
def api_dm_open():
    body = request.get_json(silent=True) or {}
    user_id = str(body.get("user_id", "")).strip().strip("<@!>")
    if not user_id.isdigit():
        return jsonify({"error": "Pick a person first."}), 400

    async def go():
        u = await bot.fetch_user(int(user_id))
        return u, (await u.create_dm()).id
    try:
        u, channel_id = dash_run(go())
        return jsonify({"dm": {"user_id": user_id, "channel_id": str(channel_id), "name": u.display_name, "last": int(time.time())}})
    except Exception as e:
        return _dash_error(e)


_dm_jobs = {}


def _dash_dm_one(uid, text, title, image, footer, color):
    async def go():
        u = await bot.fetch_user(int(uid))
        e = discord.Embed(title=title or None, description=text[:4000], color=color)
        if image:
            e.set_image(url=image)
        if footer:
            e.set_footer(text=footer)
        await u.send(embed=e)
    dash_run(go(), timeout=40)


def _dash_dm_job(job, ids, text, title, image, hidden, color):
    j = _dm_jobs[job]
    footer = None if hidden else "Sent by the Shapes team"
    try:
        for uid in ids:
            try:
                _dash_dm_one(uid, text, title, image, footer, color)
                j["sent"] += 1
            except Exception:
                j["failed"] += 1
            j["done"] += 1
            time.sleep(0.6)
    except Exception as e:
        j["error"] = str(e)[:150]
    j["finished"] = True


@app.route("/api/dm/send", methods=["POST"])
@admin_required
def api_dm_send():
    body = request.get_json(silent=True) or {}
    text, title = str(body.get("message", "")).strip(), str(body.get("title", "")).strip()
    raw = body.get("user_ids")
    ids = re.findall(r"\d{15,22}", raw) if isinstance(raw, str) else [str(i).strip().strip("<@!>") for i in (raw or []) if str(i).strip().strip("<@!>").isdigit()]
    ids = list(dict.fromkeys(ids))
    if not text:
        return jsonify({"error": "Write the message first."}), 400
    everyone = body.get("everyone") is True
    if everyone:
        ids = list(dict.fromkeys(str(m.id) for g in bot.guilds for m in g.members if not m.bot))
    if not ids:
        return jsonify({"error": "Pick at least one person."}), 400
    try:
        image = _dash_valid_image(body.get("image_url"))
        color = _dash_color(body.get("color"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    hidden = body.get("hidden") is True
    job = os.urandom(6).hex()
    _dm_jobs[job] = {"total": len(ids), "done": 0, "sent": 0, "failed": 0, "finished": False}
    threading.Thread(target=_dash_dm_job, args=(job, ids, text, title, image, hidden, color), daemon=True).start()
    log_action("dm", ("(hidden sender) " if hidden else "") + text[:200], target=("everyone: " if everyone else "") + f"{len(ids)} people: " + ", ".join(ids[:8]) + ("…" if len(ids) > 8 else ""))
    return jsonify({"job": job, "total": len(ids)})


@app.route("/api/dm/job/<job>")
@admin_required
def api_dm_job(job):
    j = _dm_jobs.get(job)
    return (jsonify(j), 200) if j else (jsonify({"error": "No such job."}), 404)


@app.route("/api/actions")
@admin_required
def api_actions():
    rows = [v for v in actionsDB.items().values() if isinstance(v, dict)]
    return jsonify({"actions": sorted(rows, key=lambda r: -int(r.get("at") or 0))[:200]})


@app.route("/avatar/<uid>.svg")
def default_avatar(uid):
    return Response('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" fill="#6366f1"/></svg>', mimetype="image/svg+xml")


@app.route('/shutdown', methods=['POST'])
def shutdown():
    if request.remote_addr not in ['127.0.0.1', '::1']:
        return 'Forbidden', 403
    
    func = request.environ.get('werkzeug.server.shutdown')
    if func is None:
        return 'Not running with the Werkzeug Server', 500
    
    func()
    return 'Server shutting down...'  
    
@app.route('/commands', methods=['GET'])
def send_commands():
    commands = {}
    commands['Commands'] = {}
    for cmd in list(bot.tree.walk_commands()) + list(bot.tree.get_commands(type=discord.AppCommandType.user)) + list(bot.tree.get_commands(type=discord.AppCommandType.message)):
        if isinstance(cmd, discord.app_commands.Group):
            continue                                  # a group is only a folder; its subcommands are listed
        if isinstance(cmd, discord.app_commands.ContextMenu):
            # Context menu commands don't have description/options
            cmd_data = {
                "name": cmd.qualified_name,
                "type": "context_menu_command"
            }
        else:  # Slash commands
            cmd_data = {
                "name": cmd.qualified_name,
                "description": getattr(cmd, "description", ""),
                "options": [opt.name for opt in getattr(cmd, "options", [])],
                "choices": [choice.name for choice in getattr(cmd, "choices", [])],
                "type": "slash_command"
            }
        
        commands['Commands'][cmd.qualified_name] = cmd_data
    commands['success'] = True
    return jsonify(commands), 200
    
@app.route('/count/commands', methods=['GET'])
def get_command_count():
    return {'Commands': str(sum(1 for c in bot.tree.walk_commands() if not isinstance(c, discord.app_commands.Group)))}, 200

@app.route('/discord/webhook', methods=['POST'])
def send_webhook():
    signature = request.headers.get("X-Signature-Ed25519")
    timestamp = request.headers.get("X-Signature-Timestamp")
    body = request.data.decode("utf-8")

    if not signature or not timestamp:
        return Response("missing signature", 401)

    try:
        verify_key = nacl.signing.VerifyKey(bytes.fromhex(PUBLIC_KEY))
        verify_key.verify(f'{timestamp}{body}'.encode(), bytes.fromhex(signature))
    except nacl.exceptions.BadSignatureError:
        return Response("invalid signature", 401)

    payload = request.json

    # Ping Check
    if payload.get("type") == 0:
        return Response(status=204)

    # Event Sent
    if payload.get("type") == 1:
        event = payload.get("event", {})
        data = event.get("data", {})
        integration_type = data.get("integration_type")
        event_type = event.get("type")

        print("Received event:", event)  # debugging


        webhook = os.environ.get('webhook_url')
        if not webhook:
            print("Webhook URL not set in environment")
            return "webhook missing", 500

        if event_type == "APPLICATION_AUTHORIZED":
            # User authorization
            if integration_type == 1:
                user = data.get("user", {})
                user_id = user.get("id", "Unknown")
                user_name = user.get("username", "Unknown")
                user_avatar = user.get("avatar")

                scopes_data = data.get("scopes", [])
                scopes = ", ".join(scopes_data)

                if user_avatar:
                    ext = "gif" if user_avatar.startswith("a_") else "png"
                    avatar_url = f"https://cdn.discordapp.com/avatars/{user_id}/{user_avatar}.{ext}"
                else:
                    avatar_url = "https://cdn.discordapp.com/embed/avatars/0.png"

                embed = {
                    "author": {"name": user_name, "icon_url": avatar_url},
                    "title": f"{user_name} Installed Shapes to their apps",
                    "description": f"**{user_name}** (`{user_id}`) has authorized Shapes. With the scopes: {scopes}",
                    "color": 3066993,
                    "timestamp": data.get("timestamp")
                }

                requests.post(webhook, json={"embeds": [embed]})

            # Guild authorization
            elif integration_type == 0:
                user = data.get("user", {})
                user_id = user.get("id", "Unknown")
                user_name = user.get("username", "Unknown")

                guild = data.get("guild", {})
                guild_id = guild.get("id", "Unknown")
                guild_name = guild.get("name", "Unknown")
                guild_icon = guild.get("icon")

                scopes_data = data.get("scopes", [])
                scopes = ", ".join(scopes_data)


                if guild_icon:
                    ext = "gif" if guild_icon.startswith("a_") else "png"
                    icon_url = f"https://cdn.discordapp.com/icons/{guild_id}/{guild_icon}.{ext}"
                else:
                    icon_url = "https://cdn.discordapp.com/embed/avatars/0.png"

                embed = {
                    "author": {"name": guild_name, "icon_url": icon_url},
                    "title": "Shapes Added To A New Server",
                    "description": f"**{guild_name}** (`{guild_id}`) has authorized Shapes. Added by {user_name} (`{user_id}`) with the scopes: {scopes}",
                    "color": 3066993,
                    "timestamp": data.get("timestamp")
                }

                requests.post(webhook, json={"embeds": [embed]})
            return Response(status=204)
        elif event_type == "APPLICATION_DEAUTHORIZED":
            # User de-authorization
            user = data.get("user", {})
            user_id = user.get("id", "Unknown")
            user_name = user.get("username", "Unknown")
            user_avatar = user.get("avatar")

            if user_avatar:
                ext = "gif" if user_avatar.startswith("a_") else "png"
                avatar_url = f"https://cdn.discordapp.com/avatars/{user_id}/{user_avatar}.{ext}"
            else:
                avatar_url = "https://cdn.discordapp.com/embed/avatars/0.png"

            embed = {
                "author": {"name": user_name, "icon_url": avatar_url},
                "title": f"{user_name} Removed Shapes from their apps",
                "description": f"**{user_name}** (`{user_id}`) has deauthorized Shapes.",
                "color": 15158332,
                "timestamp": data.get("timestamp")
            }

            requests.post(webhook, json={"embeds": [embed]})
            return Response(status=204)
        else:
            # Unknown type
            return Response("Unhandled Event", 400)
    else:
        return Response("invalid type", 400)

# === Globals for caching and ready state ===
cached_guilds = []
bot_ready = False

# Show the bot as "Discord Android" (the phone icon). This used to be a copy of discord.py's identify() and connect(), which
# broke on every discord.py update. Now only the IDENTIFY payload's properties are changed, on top of whatever discord.py sends.
_original_send_as_json = DiscordWebSocket.send_as_json


async def _send_as_json_android(self, data):
    if isinstance(data, dict) and data.get("op") == DiscordWebSocket.IDENTIFY:
        props = data.setdefault("d", {}).setdefault("properties", {})
        props.update({"os": sys.platform, "browser": "Discord Android", "device": "Discord Android"})
    return await _original_send_as_json(self, data)


DiscordWebSocket.send_as_json = _send_as_json_android


class Shapes(Bot):
    async def setup_hook(self):
        for cog in COGS:                      # the cogs are defined further down this file (Roblox, command logs, Vortex, server)
            await self.add_cog(cog(self))

colors_lua = """{[3447003] = "Blue", [15158332] = "Red", [3066993] = "Green", [10181046] = "Purple", [15105570] = "Orange", [15844367] = "Gold", [1752220] = "Teal", [2123412] = "Dark Blue", [10038562] = "Dark Red", [2067276] = "Dark Green", [7419530] = "Dark Purple", [11027200] = "Dark Orange", [12745742] = "Dark Gold", [1146986] = "Dark Teal"}"""
colors = lua.decode(colors_lua)
#print(colors)

#bot = commands.Bot(command_prefix="/", intents=intents)
bot = Shapes(command_prefix="/", intents=discord.Intents.all())

# /roblox ... lives in one command group; the Roblox cog (below) adds the verification commands to it and the tree gets it when the cog loads.
roblox_group = app_commands.Group(
    name="roblox", description="Roblox lookups, verification, your linked accounts and group info",
    allowed_installs=app_commands.AppInstallationType(guild=True, user=True),
    allowed_contexts=app_commands.AppCommandContext(guild=True, dm_channel=True, private_channel=True))
roblox_panel = app_commands.Group(name="panel", description="Set up Roblox verification for this server (server admins)", parent=roblox_group)
#tree = app_commands.CommandTree(bot)

class TopGGIntegration:
    """Posts the bot's stats and slash commands to top.gg (API v1, https://docs.top.gg/api/v1/projects).

    Set `Commands-TK` (or TOPGG_PROJECT_TOKEN) to the bot's top.gg project token. If only the old TOPGG_TOKEN is set, the
    server count still goes to the old endpoint with it, as before."""

    BASE = "https://top.gg/api/v1/projects/@me"

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.token = os.environ.get("Commands-TK") or os.environ.get("TOPGG_PROJECT_TOKEN")
        self.legacy_token = os.environ.get("TOPGG_TOKEN")
        self._tasks = []

    async def _request(self, method, url, headers, body):
        """(ok, status, text). Never raises; a 429 says how long to wait."""
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
                async with session.request(method, url, headers=headers, json=body) as response:
                    text = await response.text()
                    if response.status == 429:
                        text = f"rate limited, retry after {response.headers.get('Retry-After', '?')}s {text[:120]}"
                    return 200 <= response.status < 300, response.status, text
        except Exception as e:
            return False, 0, str(e)

    async def post_server_count(self) -> bool:
        if self.token:
            body = {"server_count": len(self.bot.guilds)}
            if self.bot.shard_count:
                body["shard_count"] = self.bot.shard_count
            ok, status, text = await self._request("PATCH", f"{self.BASE}/metrics",
                                                   {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}, body)
            if ok:
                return True
            print(f"❌ top.gg refused the server count: {status} {text[:200]}")
        if self.legacy_token and self.bot.user:
            ok, status, text = await self._request("POST", f"https://top.gg/api/bots/{self.bot.user.id}/stats",
                                                   {"Authorization": self.legacy_token, "Content-Type": "application/json"},
                                                   {"server_count": len(self.bot.guilds)})
            if not ok:
                print(f"❌ top.gg (old endpoint) refused the server count: {status} {text[:200]}")
            return ok
        if not self.token:
            print("top.gg server count skipped: set Commands-TK (top.gg project token) in the environment.")
        return False

    async def post_commands_to_topgg(self) -> bool:
        """Replace the command list on top.gg with the bot's current slash and context-menu commands."""
        if not self.token:
            print("Commands token not found. Set Commands-TK in environment.")
            return False
        commands_data = self.get_commands_payload()
        if not commands_data:
            print("⚠️ No commands found to post to Top.gg")
            return False
        ok, status, text = await self._request("PUT", f"{self.BASE}/commands",
                                               {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}, commands_data)
        if ok:
            print(f"✅ Successfully posted {len(commands_data)} commands to Top.gg")
        else:
            print(f"❌ Failed to post commands to Top.gg: {status} {text[:200]}")
        return ok

    def get_commands_payload(self) -> list:
        """The commands as Discord application command objects (what top.gg wants), built by discord.py itself."""
        tree, found, excluded = self.bot.tree, [], 0
        every = list(tree.get_commands()) + list(tree.get_commands(type=discord.AppCommandType.user)) + list(tree.get_commands(type=discord.AppCommandType.message))
        for command in every:
            if self._is_command_excluded(command):
                excluded += 1
                continue
            try:
                found.append(command.to_dict(tree))
            except Exception as e:
                print(f"❌ Error converting command {getattr(command, 'name', 'unknown')}: {e}")
        print(f"📊 Top.gg command summary: {len(found)} posted, {excluded} excluded, {len(every)} total")
        return found

    def _is_command_excluded(self, command) -> bool:
        """Check if a command should be excluded from Top.gg"""
        # Check for decorator exclusion on callback
        if hasattr(command, 'callback') and hasattr(command.callback, '_exclude_from_topgg'):
            return True

        # For command groups, check the group itself
        if hasattr(command, '_callback') and hasattr(command._callback, '_exclude_from_topgg'):
            return True

        # Check if the command object itself has the exclusion flag
        if hasattr(command, '_exclude_from_topgg'):
            return True

        return False

    async def start_periodic_updates(self):
        """Start periodic updates for server count and commands"""
        self._tasks = [asyncio.create_task(self._periodic_commands()), asyncio.create_task(self._periodic_server_count())]

    async def _periodic_server_count(self):
        """Periodically update server count"""
        while True:
            await asyncio.sleep(900)  # 15 minutes
            try:
                await self.post_server_count()
            except Exception as e:
                print(f"❌ Error in periodic server count update: {e}")

    async def _periodic_commands(self):
        """Periodically update commands"""
        while True:
            await asyncio.sleep(86400)  # 24 hours
            try:
                await self.post_commands_to_topgg()
            except Exception as e:
                print(f"❌ Error in periodic command update: {e}")

class CommandSyncer:
    """Handles command synchronization with Discord"""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        
    async def sync_commands(self, guild_id: Optional[int] = None) -> int:
        """Sync commands to Discord"""
        try:
            if guild_id:
                # Sync to specific guild (faster for testing)
                guild = discord.Object(id=guild_id)
                synced = await self.bot.tree.sync(guild=guild)
                print(f"✅ Synced {len(synced)} commands to guild {guild_id}")
            else:
                # Sync globally (takes up to 1 hour to propagate)
                synced = await self.bot.tree.sync()
                print(f"✅ Synced {len(synced)} commands globally")
            
            return len(synced)
            
        except Exception as e:
            print(f"❌ Failed to sync commands: {e}")
            return 0

# == save databases if bot closes/goes offline == #
# async def update_db_on_close():
    #while True:
        #time.sleep(2)
        #if bot.is_closed():
            #countingDB.save()
            #embedDB.save()
            #usersDB.save()
            #print(f"Saved EmbedDB {embedDB.all()}")
            #print(f"Saved CountingDB {countingDB.all()}")
            #print(f"Saved UsersDB {usersDB.all()}")
            #print("Bot Closed, Shutting Down Flask Server.")
            #os._exit(0)

# == update databases 0.5 seconds == #

async def update_db():
    while True:
        await asyncio.sleep(0.5)
        if not bot.is_closed():
            #print(f"EmbedDB = {embedDB.all()}")
            #print(f"CountingDB = {countingDB.all()}")
            #print(f"UsersDB = {usersDB.all()}")
    
            for guild in bot.guilds:
                if not countingDB.get(f"{guild.id}"):
                    countingDB.set(f"{guild.id}", {"channel":None,"number":0,"enabled":False,"warnings":0,"lastcounter":None,"highestnumber":0, "banned":False})
        if bot.is_closed():
            flush_all()
            print("Bot Closed, Shutting Down Flask Server.")
            os._exit(0)


# === Background task to update cached guilds every 2 seconds ===
async def update_guild_cache():
    global cached_guilds
    while True:
        #await bot.tree.sync()
        #BotInfo = await bot.application_info()
        cached_guilds = list(bot.guilds)
        print(f"[SYSTEM] Watching {len(cached_guilds)} Servers!")
        print(f"[SYSTEM] Watching {BotInfo.approximate_user_install_count} Users!")
        await bot.change_presence(activity=discord.CustomActivity(name=f"🔗 shapes.lol/discord"))
        await asyncio.sleep(2)
        #if len(bot.guilds) == 1:
            #print(bot.guilds[0].name)
            #await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name=bot.guilds[0].name))
        #else:
            #print(f"Watching {len(bot.guilds)} Servers")
            #await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name=f"{len(bot.guilds)} servers"))

        await asyncio.sleep(30)

COUNTING_DEFAULT_WARNINGS = 3
COUNTING_MAX_WARNINGS = 50


def counting_limit(data):
    """How many warnings a server gets before the count is ruined (bot owners can change it per server)."""
    try:
        return max(0, min(COUNTING_MAX_WARNINGS, int(data.get("max_warnings", COUNTING_DEFAULT_WARNINGS))))
    except (TypeError, ValueError):
        return COUNTING_DEFAULT_WARNINGS


def is_bot_owner(user):
    """The people the restart/shutdown commands already trust, plus any ids in OWNER_IDS (comma separated)."""
    ids = {x.strip() for x in os.environ.get("OWNER_IDS", "").split(",") if x.strip()}
    return str(user.id) in ids or user.name in ("lcjunior1220", "sl.ip", "kiwixor")


def IsInteger(s):
    try:
        int(s)
        return True  # Conversion succeeded, it is an integer
    except ValueError:
        return False  # Conversion failed, it is not an integer

# === Bot Events ===
@bot.event
async def on_message(message):
    if message.author == bot.user:
        return
    if message.author.bot:
        return

    if message.guild:
        server = message.guild
        countingjson = countingDB.get(server.id)
        counting_data = countingjson
        enabled = counting_data['enabled']
        number = counting_data['number']
        channel = counting_data['channel']
        warnings = counting_data['warnings']
        warning_limit = counting_limit(counting_data)
        LastCounter = counting_data['lastcounter']
        HighestNumber = counting_data['highestnumber']
        next_number = number + 1
        if enabled == False or message.channel.id != channel:
            return
        
        messagecontent = message.content
        messagecontent = messagecontent.replace(" ", "")
        InputNumber = None
        
        if not any(op in messagecontent for op in "+-*/x"):
            if IsInteger(messagecontent):
                InputNumber = int(messagecontent)
            else:
                ##print("stop")
                return
        else:
            num = ''
            parts = []
            for ch in messagecontent:
                if IsInteger(ch):
                    num += ch
                else:
                    parts.append(num)
                    parts.append(ch)
                    num = ''
            parts.append(num)
        
            if not IsInteger(parts[0]):
                return
            InputNumber = int(parts[0])
            i = 1
            while i < len(parts):
                op = parts[i]
                val = int(parts[i + 1])
        
                if op == '+':
                    InputNumber += val
                elif op == '-':
                    InputNumber -= val
                elif op == '*' or op == 'x':
                    InputNumber *= val
                elif op == '/':
                    result = InputNumber / val
                    InputNumber = int(result) if IsInteger(ch) else result
        
                i += 2
        
        if str(InputNumber) == str(next_number) and message.author.id != LastCounter:
            
            LastCounter = message.author.id
            number = next_number
            if number > HighestNumber:
                HighestNumber = number
            counting_data['highestnumber'] = HighestNumber
            counting_data['number'] = number
            counting_data["lastcounter"] = LastCounter
            countingDB.set(server.id, counting_data)
            countingDB.save()
            await message.add_reaction('👍')
        else:
            if InputNumber == None:
                return
            if enabled == False:
                return
            
            if channel != message.channel.id:
                return
            
            if message.author.id == LastCounter and warnings < warning_limit:
                await message.add_reaction('⚠️')
                await message.reply(f":warning: You can't count by yourself!")
                warnings = warnings + 1
                counting_data['warnings'] = warnings
                countingDB.set(server.id, counting_data)
                countingDB.save()
                return
            if warnings < warning_limit:
                await message.add_reaction('⚠️')
                if number == 0:
                    await message.reply(f":warning: The next number is 1")
                await message.reply(f":warning: The next number is {next_number}")
                warnings = warnings + 1
                counting_data['warnings'] = warnings
                countingDB.set(server.id, counting_data)
                countingDB.save()
                return
            if warnings >= warning_limit:
                await message.add_reaction('❌')
                if number > HighestNumber:
                    HighestNumber = number
                counting_data['highestnumber'] = HighestNumber
                if number == 0:
                    await message.channel.send(f":x: {message.author.mention} ruined it at 1, the next number is 1 (again)")
                    next_number = 1
                    warnings = 0
                    counting_data['warnings'] = warnings
                    counting_data['number'] = number
                    countingDB.set(server.id, counting_data)
                    countingDB.save()
                    return
                await message.channel.send(f":x: {message.author.mention} ruined it at {number}, the next number is 1")
                number = 0
                next_number = 1
                warnings = 0
                LastCounter = None
                counting_data['lastcounter'] = LastCounter
                counting_data['warnings'] = warnings
                counting_data['number'] = number
                countingDB.set(server.id, counting_data)
                countingDB.save()
                return
    else:
        invite_url = f"https://discord.com/api/oauth2/authorize?client_id={bot.user.id}"
        invite_embed = discord.Embed(
            description=f"[Click Here To Add Shapes To Your Server or Apps]({invite_url})",
            color=embedDB.get(f"{message.author.id}") if embedDB.get(f"{message.author.id}") else discord.Color.blue()
        )
        await message.channel.send(embed=invite_embed)

@bot.event
async def on_message_delete(message):
    print(f"Message by {message.author} deleted in channel {message.channel}: {message.content}")

    if message.guild:
        server = message.guild
        counting = countingDB.get(f"{server.id}")
        if counting:
            if message.author.id == counting['lastcounter'] and message.channel.id == counting['channel'] and IsInteger(message.content):
                nextnumber = counting['number'] + 1
                await message.channel.send(f"{message.author.mention} deleted their message containing the last number. The next number is {nextnumber}")

@bot.event
async def on_message_edit(before, after):
    if before.author.bot:
        return

    if before.guild:
        server = before.guild
        counting = countingDB.get(f"{server.id}")
        if counting:
            if before.author.id == counting['lastcounter'] and before.channel.id == counting['channel'] and IsInteger(before.content):
                nextnumber = counting['number'] + 1
                await before.channel.send(f"{before.author.mention} edited their message containing the last number. The next number is {nextnumber}")

@bot.event
async def on_ready():
    global bot_ready
    global BotInfo
    bot_ready = True
    await bot.tree.sync()
    print(f"Logged in as {bot.user}")
    BotInfo = await bot.application_info()
    #print(BotInfo)
    await bot.change_presence(activity=discord.CustomActivity(name=f"🔗 shapes.lol/discord"))
    # Start the cache updater task
    Shapes(command_prefix="/", intents=discord.Intents.all())
    bot.loop.create_task(update_guild_cache())
    bot.loop.create_task(update_db())
    #bot.loop.create_task(update_db_on_close())

    TOP_GG = TopGGIntegration(bot)
    TOP_GG_COMMANDS = CommandSyncer(bot)

    print("Loading top.gg API")

    try:
        synced_count = await TOP_GG_COMMANDS.sync_commands()
        print(f"✅ Command sync completed: {synced_count} commands")
    except Exception as e:
        print(f"❌ Command sync failed: {e}")
    
    try:
        await TOP_GG.start_periodic_updates()
        print("✅ Top.gg integration started")
    except Exception as e:
        print(f"❌ Top.gg integration failed: {e}")
    
    try:
        if await TOP_GG.post_commands_to_topgg():
            print("✅ Initial Top.gg commands posted")
    except Exception as e:
        print(f"❌ Failed to post initial Top.gg commands: {e}")

    try:
        if await TOP_GG.post_server_count():
            print("✅ Server count updated successfully")
    except Exception as e:
        print(f"❌ Failed to update server count: {e}")

@app.route('/topgg/commands', methods=['GET'])
def topgg_commands():
    if not bot.is_ready():
        return jsonify({"success": False, "data": "The bot isn't ready yet."}), 503
    return jsonify(TopGGIntegration(bot).get_commands_payload())

@bot.event
async def on_member_join(member):
    if member.bot:
        return
        
    autorole_data = autoroleDB.get(f"{member.guild.id}")
    if autorole_data and autorole_data.get("enabled"):
        role_id = autorole_data.get("role_id")
        if role_id:
            role = member.guild.get_role(role_id)
            if role:
                await member.add_roles(role)

@app.route('/botinfo', methods=["GET"])
def get_bot_info():
    # Ensure the bot is ready before accessing guilds
    if bot.is_ready():
        server_count = len(bot.guilds)
        jsonData = {"Servers":str(server_count),"Users":str(BotInfo.approximate_user_install_count)}
        return jsonify(jsonData), 200
    else:
        jsonData = {"Servers":"Unknown","Users":"Unknown"}
        return jsonify(jsonData), 503
        
@app.route('/count/servers', methods=["GET"])
def get_server_count():
    if bot.is_ready():
        server_count = len(bot.guilds)
        jsondata = {"Servers":str(server_count)}
        return jsonify(jsondata), 200
    else:
        return {"Servers":"Unknown"}, 503
        
@app.route('/count/users', methods=["GET"])
def get_user_count():
    if bot.is_ready():
        user_count = BotInfo.approximate_user_install_count
        jsondata = {"Users":str(user_count)}
        return jsonify(jsondata), 200
    else:
        return {"Users":"Unknown"}, 503

@app.route('/clb', methods=["GET"])
def countinglb():
    if bot.is_ready():
        lb = {}
        for server in countingDB.all():
            data = countingDB.get(f"{server}")
            if data.get("banned", False) == True:
                print(f'{bot.get_guild(int(server)).name if bot.get_guild(int(server)) else "Unknown"} Is Banned From Counting.')
                continue
            if data["enabled"] == True:
                server_name = bot.get_guild(int(server)).name if bot.get_guild(int(server)) else None
                if server_name == None:
                    continue
                lb[f"{server}"] = {"currentnumber": data['number'],"highestnumber": data['highestnumber'], "serverName": server_name}
        FullLB = sorted(lb.items(), key=lambda x: x[1]['highestnumber'], reverse=True)
        return {"Leaderboard":FullLB}, 200
    else:
        return jsonify("Bot is still starting"), 503

@app.route('/mutuals', methods=["GET"])
def mutualservers():
    if bot.is_ready():
        servers = {}
        for s in bot.guilds:
            servers[s.id] = {"name": s.name, "botcount": 0, "membercount": 0, "iconurl": str(s.icon.url) if s.icon else "https://cdn.discordapp.com/embed/avatars/0.png", "owner": str(s.owner), "ownerid": s.owner.id, "members": [], "bots": [], "verificationlevel": str(s.verification_level), "createdat": str(s.created_at), "joinedat": str(s.me.joined_at), "roles": len(s.roles), "channels": len(s.channels),"bannerurl": str(s.banner.url) if s.banner else None}
            for m in s.members:
                if m.bot:
                    servers[s.id]["bots"].append({"name": str(m), "id": m.id, "icon": str(m.display_avatar.url) if m.display_avatar else "https://cdn.discordapp.com/embed/avatars/0.png"})
                    servers[s.id]["botcount"] += 1
                else:
                    servers[s.id]["members"].append({"name": str(m), "id": m.id, "icon": str(m.display_avatar.url) if m.display_avatar else "https://cdn.discordapp.com/embed/avatars/0.png"})
                    servers[s.id]["membercount"] += 1
        return {"Servers":servers}, 200

async def restartbot():
    print("Bot Restarting.")
    await bot.close(close=True)
    await asyncio.sleep(20)
    bot.run(token)

def isotodiscordtimestamp(iso_timestamp_str: str, format_type: str = "D") -> str:
    try:
        if '.' in iso_timestamp_str and iso_timestamp_str.endswith('Z'):
            main_part = iso_timestamp_str.split('.')[0]
            iso_timestamp_str = main_part + '+00:00'
        elif '.' in iso_timestamp_str and '+00:00' in iso_timestamp_str:
            main_part = iso_timestamp_str.split('.')[0]
            iso_timestamp_str = main_part + '+00:00'
        elif iso_timestamp_str.endswith('Z'):
            iso_timestamp_str = iso_timestamp_str.replace('Z', '+00:00')
        
        dt_object = datetime.fromisoformat(iso_timestamp_str)

        if dt_object.tzinfo is None:
            dt_object = pytz.utc.localize(dt_object)
        else:
            dt_object = dt_object.astimezone(pytz.utc)

        unix_timestamp = int(dt_object.timestamp())
        return f"<t:{unix_timestamp}:{format_type}>"
    except ValueError as e:
        return None

DiscordColors = [
    discord.Color.blue(),
    discord.Color.red(),
    discord.Color.green(),
    discord.Color.purple(),
    discord.Color.orange(),
    discord.Color.gold(),
    discord.Color.teal(),
    discord.Color.dark_blue(),
    discord.Color.dark_red(),
    discord.Color.dark_green(),
    discord.Color.dark_purple(),
    discord.Color.dark_orange(),
    discord.Color.dark_gold(),
    discord.Color.dark_teal(),
    discord.Color.random()
]

class EmbedColorSelection(discord.ui.Modal, title="Test Modal"):
    modal_choices = [discord.Color.blue(), discord.Color.red(), discord.Color.green(), discord.Color.purple(), discord.Color.orange(), discord.Color.gold(), discord.Color.teal(), discord.Color.dark_blue(), discord.Color.dark_red(), discord.Color.dark_green(), discord.Color.dark_purple(), discord.Color.dark_orange(), discord.Color.dark_gold(), discord.Color.dark_teal(), discord.Color.random()]
    color_select = discord.ui.Select(
        options=[discord.SelectOption(label="Blue", description="A nice blue color", value=str(discord.Color.blue().value), emoji="🔵"),
        discord.SelectOption(label="Red", description="A vibrant red color", value=str(discord.Color.red().value), emoji="🔴"),
            discord.SelectOption(label="Green", description="A refreshing green color", value=str(discord.Color.green().value), emoji="🟢"),
            discord.SelectOption(label="Purple", description="A royal purple color", value=str(discord.Color.purple().value), emoji="🟣"),
            discord.SelectOption(label="Orange", description="A bright orange color", value=str(discord.Color.orange().value), emoji="🟠"),
            discord.SelectOption(label="Gold", description="A shiny gold color", value=str(discord.Color.gold().value), emoji="🟡"),
            discord.SelectOption(label="Teal", description="A cool teal color", value=str(discord.Color.teal().value), emoji="🔷"),
            discord.SelectOption(label="Dark Blue", description="A deep dark blue color", value=str(discord.Color.dark_blue().value), emoji="🔷"),
            discord.SelectOption(label="Dark Red", description="A deep dark red color", value=str(discord.Color.dark_red().value), emoji="🔴"),
            discord.SelectOption(label="Dark Green", description="A deep dark green color", value=str(discord.Color.dark_green().value), emoji="🟢"),
            discord.SelectOption(label="Dark Purple", description="A deep dark purple color", value=str(discord.Color.dark_purple().value), emoji="🟣"),
            discord.SelectOption(label="Dark Orange", description="A deep dark orange color", value=str(discord.Color.dark_orange().value), emoji="🟠"),
            discord.SelectOption(label="Dark Gold", description="A deep dark gold color", value=str(discord.Color.dark_gold().value), emoji="🟡"),
            discord.SelectOption(label="Dark Teal", description="A deep dark teal color", value=str(discord.Color.dark_teal().value), emoji="🔷"),
            discord.SelectOption(label="Random", description="A random color", value=str(discord.Color.random().value), emoji="🔷"),
        ]
    )
    def __init__(self):
        super().__init__()
        self.add_item(self.color_select)
    async def on_submit(self, interaction: discord.Interaction):
        selected_color_value = int(self.color_select.values[0])
        embedDB.set(f"{interaction.user.id}", selected_color_value)
        embedDB.save()
        embed = discord.Embed(
            title="Embed Color Changed!",
            description=f"Your embed color has been changed successfully to {selected_color_value}!",
            color=selected_color_value
        )
        embed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
        await interaction.response.send_message(embed=embed, ephemeral=True)

# === User Commands ===
@bot.tree.context_menu(name="discord2roblox")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def discord2roblox(interaction: discord.Interaction, user: discord.User): # = 481295611417853982):
    userid = user.id
    await interaction.response.defer(thinking=True)

    loading = discord.Embed(
        title=f"{Emojis.get('loading')} {interaction.user.mention} Getting Roblox Profile For {user.name}",
        color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
    )

    await interaction.followup.send(embed=loading)

    url = f"{APIBaseURL}/d2r/{userid}"

    try:
        response = requests.get(url, headers=APIBaseURL_Headers)
        response.raise_for_status()
        APIData = response.json()
        print(APIData)
        Roblox = None
        if APIData.get("data") and APIData.get("success", False) == True:
            successembed = discord.Embed(
                title=f"Roblox Profile for {user.name}",
                description=f"[View Roblox Profile](https://www.roblox.com/users/{APIData['data']}/profile)",
                color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
            )
            successembed.set_thumbnail(url=f"https://www.roblox.com/headshot-thumbnail/image?userId={APIData['data']}&width=420&height=420&format=png")
            successembed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
            await interaction.edit_original_response(embed=successembed)
            return
        else:
            #print(f"No Roblox Profile found for {user.name}")
            failedembed = discord.Embed(
                title=f"No Roblox Profile found for {user.name}",
                color=discord.Color.red()
            )
            await interaction.edit_original_response(embed=failedembed)
            return
    except requests.exceptions.RequestException as e:
        #print(f"Error fetching data for Discord ID: {userid}: {e}")
        failedembed3 = discord.Embed(
            title=f"Error retrieving Roblox Profile from {user.name}",
            color=discord.Color.red()
        )
        await interaction.edit_original_response(embed=failedembed3)
        return

@bot.tree.context_menu(name="discord2spook")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def discord2spook(interaction: discord.Interaction, user: discord.User):
    url = f"https://spook.bio/api/profiles?discordId={user.id}"
    print(user.id)
    print(url)
    response = requests.get(url)
    data = response.json()
    if response.status_code == 200:
        await interaction.response.send_message(f"{user.mention}'s [spook.bio Profile](https://spook.bio/@{data.get('username')})", ephemeral=False)
        print(f"Fetched {data['username']} successfully!")
    else:
        if interaction.user.name == user.name:
            await interaction.response.send_message(f":x: You don't have a spook.bio profile linked to your account {user.mention}! :x: To link your profile to your account create a spook.bio account [here](https://spook.bio/login)")
            return
        await interaction.response.send_message(f":x: {user.mention} doesn't have a spook.bio profile linked to their account! :x:", ephemeral=False)
        print(f"Error fetching data: {response.status_code}")

# === Message Commands ===
@bot.tree.context_menu(name="ai")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def ai(interaction: discord.Interaction, prompt: discord.Message):
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
                f"You are a helpful Discord assistant chatting with {username}. "
                "Always respond in a single concise paragraph. Otherwise your response will not be recieved due to the embed text limit"
                "Follow Discord TOS. Do not provide instructions for illegal activity. "
                "Stay safe, respectful, and friendly."
            )
        }
    ]

    # Include previous conversation
    for u_msg, a_msg in zip(user_data["user_messages"], user_data["ai_responses"]):
        messages_for_ai.append({"role": "user", "content": u_msg})
        messages_for_ai.append({"role": "assistant", "content": a_msg})

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

@bot.tree.context_menu(name="google")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def google(interaction: discord.Interaction, message: discord.Message):
    await interaction.response.defer(thinking=True)
    query = message.content
    thinkingembed = discord.Embed(
        title=f"{Emojis.get('loading')} {interaction.user.mention} Searching Google For {query}!",
        color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
    )
    await interaction.followup.send(embed=thinkingembed)
    # replace spaces with + in query for google search link
    properquery = query.replace(" ", "+")
    
    url = f"https://www.googleapis.com/customsearch/v1?key={GoogleAPIKey}&cx={searchengine}&q={properquery}"
    try:
        response = requests.get(url)
        response.raise_for_status()
        data = response.json()

        # Get First 5 results
        if "items" in data and len(data["items"]) >= 5:
            first_result = data["items"][0]
            title = first_result.get("title", "No Title")
            snippet = first_result.get("snippet", "No Description")
            link = first_result.get("link", "No Link")
            print(f"First Result: {title} - {link}")
            second_result = data["items"][1]
            second_result_title = second_result.get("title", "No Title")
            second_result_snippet = second_result.get("snippet", "No Description")
            second_result_link = second_result.get("link", "No Link")
            print(f"Second Result: {second_result_title} - {second_result_link}")
            third_result = data["items"][2]
            third_result_title = third_result.get("title", "No Title")
            third_result_snippet = third_result.get("snippet", "No Description")
            third_result_link = third_result.get("link", "No Link")
            print(f"Third Result: {third_result_title} - {third_result_link}")
            fourth_result = data["items"][3]
            fourth_result_title = fourth_result.get("title", "No Title")
            fourth_result_snippet = fourth_result.get("snippet", "No Description")
            fourth_result_link = fourth_result.get("link", "No Link")
            print(f"Fourth Result: {fourth_result_title} - {fourth_result_link}")
            fifth_result = data["items"][4]
            fifth_result_title = fifth_result.get("title", "No Title")
            fifth_result_snippet = fifth_result.get("snippet", "No Description")
            fifth_result_link = fifth_result.get("link", "No Link")
            print(f"Fifth Result: {fifth_result_title} - {fifth_result_link}")
            
            embed = discord.Embed(
                title=f"Google Results For {query}",
                description=f"**1. [{title}]({link})**\n{snippet}\n\n**2. [{second_result_title}]({second_result_link})**\n{second_result_snippet}\n\n**3. [{third_result_title}]({third_result_link})**\n{third_result_snippet}\n\n**4. [{fourth_result_title}]({fourth_result_link})**\n{fourth_result_snippet}\n\n**5. [{fifth_result_title}]({fifth_result_link})**\n{fifth_result_snippet}\n\n[Search For More Results](https://google.com/search?q={properquery})",
                color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
            )
            embed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
            await interaction.edit_original_response(embed=embed)
        else:
            if "items" in data and len(data["items"]) > 0:
                notenoughresultsembed = discord.Embed(
                title=":x: Not enough results found! :x:",
                description=f"Please search on google yourself as there wasn't enough results to generate an embed | [Search on Google Yourself For More Results](https://google.com/search?q={properquery})",
                color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
                )
                notenoughresultsembed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")

                await interaction.edit_original_response(embed=notenoughresultsembed)
                return
            noresultembed = discord.Embed(
                title=":x: No results found! :x:",
                description=f"No Results for {query} | [Search on Google Yourself For More Results](https://google.com/search?q={properquery})",
                color=discord.Color.red()
            )
            noresultembed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
            await interaction.edit_original_response(embed=noresultembed)
    except requests.exceptions.RequestException as e:
        print(f"An error occurred during the API request: {e}")
        errorembed = discord.Embed(
            title=":x: An error occurred while searching Google. Please try again later. :x:",
            color=discord.Color.red()
        )
        errorembed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
        await interaction.edit_original_response(embed=errorembed)

# === Bot Commands ===
@bot.tree.command(name="userinstalls", description="Get The User Installation Count For Shapes!")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def userinstalls(interaction: discord.Interaction):
    await interaction.response.send_message(f"{BotInfo.approximate_user_install_count} Users Use Shapes!")

@bot.tree.command(name="servercount", description="Get The Server Count For Shapes!")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def servercount(interaction: discord.Interaction):
    await interaction.response.send_message(f"{len(bot.guilds)} Servers Use Shapes!")

@bot.tree.command(name="getdata", description="Get The Data From One Of Our Databases")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(database="The database to get data from", key="The key to get data for")
@app_commands.choices(database=[
    app_commands.Choice(name="embedDB", value="embedDB"),
    app_commands.Choice(name="countingDB", value="countingDB"),
    app_commands.Choice(name="usersDB", value="usersDB"),
    app_commands.Choice(name="autoroleDB", value="autoroleDB"),
    app_commands.Choice(name="AI_DB", value="AI_DB"),
])
async def getdata(interaction: discord.Interaction, database: str, key: str):
    # add options for each database and each key and restrict who can use the command.
    if not interaction.user.id in [481295611417853982, 1129085908390518895]:
        await interaction.response.send_message(":x: You don't have permission to use this command! :x:", ephemeral=True)
        return
    db_map = {
        "embedDB": embedDB,
        "countingDB": countingDB,
        "usersDB": usersDB,
        "autoroleDB": autoroleDB,
        "AI_DB": AI_DB,
    }
    selected_db = db_map.get(database)
    if selected_db:
        data = selected_db.get(f"{key}")
        if data:
            await interaction.response.send_message(f"Data for key `{key}` in database `{database}`: ```{data}```", ephemeral=True)
        else:
            await interaction.response.send_message(f":x: No data found for key `{key}` in database `{database}`! :x:", ephemeral=True)
    else:
        await interaction.response.send_message(f":x: Database `{database}` not found! :x:", ephemeral=True)

@bot.tree.command(name="settings", description="Your Settings For Shapes")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def settings(interaction: discord.Interaction):
    current = embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
    # loop through the colors to figure out which one is the current one and set the default value on the selectoption as True
    defaults = []
    for color in DiscordColors:
        if color.value == current:
            defaults.append(True)
        else:
            defaults.append(False)


    view = discord.ui.View()
    color_select = discord.ui.Select(
        placeholder="Select your embed color",
        options=[
            discord.SelectOption(label="Blue", description="A nice blue color", value=str(discord.Color.blue().value), emoji="🔵", default=defaults[0]),
            discord.SelectOption(label="Red", description="A vibrant red color", value=str(discord.Color.red().value), emoji="🔴", default=defaults[1]),
            discord.SelectOption(label="Green", description="A refreshing green color", value=str(discord.Color.green().value), emoji="🟢", default=defaults[2]),
            discord.SelectOption(label="Purple", description="A royal purple color", value=str(discord.Color.purple().value), emoji="🟣", default=defaults[3]),
            discord.SelectOption(label="Orange", description="A bright orange color", value=str(discord.Color.orange().value), emoji="🟠", default=defaults[4]),
            discord.SelectOption(label="Gold", description="A shiny gold color", value=str(discord.Color.gold().value), emoji="🟡", default=defaults[5]),
            discord.SelectOption(label="Teal", description="A cool teal color", value=str(discord.Color.teal().value), emoji="🔷", default=defaults[6]),
            discord.SelectOption(label="Dark Blue", description="A deep dark blue color", value=str(discord.Color.dark_blue().value), emoji="🔷", default=defaults[7]),
            discord.SelectOption(label="Dark Red", description="A deep dark red color", value=str(discord.Color.dark_red().value), emoji="🔴", default=defaults[8]),
            discord.SelectOption(label="Dark Green", description="A deep dark green color", value=str(discord.Color.dark_green().value), emoji="🟢", default=defaults[9]),
            discord.SelectOption(label="Dark Purple", description="A deep dark purple color", value=str(discord.Color.dark_purple().value), emoji="🟣", default=defaults[10]),
            discord.SelectOption(label="Dark Orange", description="A deep dark orange color", value=str(discord.Color.dark_orange().value), emoji="🟠", default=defaults[11]),
            discord.SelectOption(label="Dark Gold", description="A deep dark gold color", value=str(discord.Color.dark_gold().value), emoji="🟡", default=defaults[12]),
            discord.SelectOption(label="Dark Teal", description="A deep dark teal color", value=str(discord.Color.dark_teal().value), emoji="🔷", default=defaults[13]),
            discord.SelectOption(label="Random", description="A random color", value=str(discord.Color.random().value), emoji="🔷", default=defaults[14]),
        ]
    )
    async def on_submit(interaction: discord.Interaction):
        selected_color_value = int(color_select.values[0])
        selected_color_name = colors.get(selected_color_value, "Unknown")
        print(selected_color_name)
        print(color_select)
        print(color_select.values)
        embedDB.set(f"{interaction.user.id}", selected_color_value)
        embed = discord.Embed(
            title="Embed Color Changed!",
            description=f"Your embed color has been changed successfully to {selected_color_name}!",
            color=selected_color_value
        )
        embed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
        await interaction.response.send_message(embed=embed, ephemeral=True)
    color_select.callback = on_submit
    view.add_item(color_select)

    class SettingsView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=None)  # No timeout

        @discord.ui.button(label="Change Embed Color", style=discord.ButtonStyle.primary, custom_id="change_embed_color", emoji="🎨")
        async def change_embed_color(self, interaction: discord.Interaction, button: discord.ui.Button):
            await interaction.response.send_message(content="Select an embed color from the menu", view=view, ephemeral=True)
            #await interaction.response.send_modal(EmbedColorModal())
        #@discord.ui.button(label="Toggle Counting", style=discord.ButtonStyle.primary, custom_id="toggle_counting")
        #async def toggle_counting(self, interaction: discord.Interaction, button: discord.ui.Button):
        #    current_setting = countingDB.get("enabled")
        #    if current_setting:
        #        countingDB.set("enabled", False)
        #        await interaction.response.send_message("Counting feature disabled.", ephemeral=True)
        #    else:
        #        countingDB.set("enabled", True)
        #        await interaction.response.send_message("Counting feature enabled.", ephemeral=True)
    embed = discord.Embed(
        title="Settings",
        description="Choose a setting below to modify it.",
        color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
    )
    embed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
    await interaction.response.send_message(embed=embed, view=SettingsView(), ephemeral=True)


@bot.tree.command(name="status", description=f"Get the {MainURL} status")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(f"[shapes.lol Status Page](https://shapes.lol/status)")

@bot.tree.command(name="stop", description="Stop the bot.")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def stop(interaction: discord.Interaction):
    if interaction.user.name == "lcjunior1220" or interaction.user.name == "sl.ip" or interaction.user.name == "kiwixor":
        await interaction.response.send_message(":white_check_mark: Shutdown Successfully!", ephemeral=False)
        
        flush_all()
        
        print("Bot shutdown initiated by command")
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post('http://localhost:13455/shutdown') as resp:
                    pass
        except:
            pass
        
        await bot.close()
        os._exit(0)
    else:
        await interaction.response.send_message(f"Only {owner}, and {co_owner} can use this command.", ephemeral=True)

@bot.tree.command(name="restart", description="Restart the bot.")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def restart(interaction: discord.Interaction):
    if interaction.user.name == "lcjunior1220" or interaction.user.name == "sl.ip" or interaction.user.name == "kiwixor":
        await interaction.response.send_message(":white_check_mark: Restarted Successfully!!", ephemeral=False)
        await restartbot()
    else:
        await interaction.response.send_message(f"Only <@481295611417853982 and <@1129085908390518895> can use this command.", ephemeral=True)

@bot.tree.command(name="counting", description="Counting Settings For Shapes")
@app_commands.default_permissions(administrator=True)
@commands.bot_has_permissions(add_reactions=True, moderate_members=True, read_message_history=True, view_channel=True, send_messages=True)
@app_commands.allowed_installs(guilds=True, users=False)
@app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
async def counting(interaction: discord.Interaction, warnings: typing.Optional[app_commands.Range[int, 0, 50]] = None):
    server = interaction.guild
    if warnings is not None:      # bot owners only: how many warnings before the count is ruined (0 = ruined on the first mistake)
        if not is_bot_owner(interaction.user):
            return await interaction.response.send_message("Only the bot owners can change the warnings amount.", ephemeral=True)
        data = countingDB.get(server.id) or {"channel": None, "number": 0, "enabled": False, "warnings": 0, "lastcounter": None, "highestnumber": 0}
        data["max_warnings"] = int(warnings)
        countingDB.set(server.id, data)
        countingDB.save()
        return await interaction.response.send_message(f"Counting here now allows **{warnings}** warning{'s' if warnings != 1 else ''} before the count is ruined.", ephemeral=True)
    print(server.id)
    counting_json = countingDB.get(server.id)
    countingData = counting_json
    print(countingData)
    if not countingData:
        countingDB.set(server.id, {"channel":None,"number":0,"enabled":False,"warnings":0,"lastcounter":None,"highestnumber":0})
        countingDB.save()
        counting_json = countingDB.get(server.id)
        countingData = counting_json
    print(countingData)
    print(countingData['channel'])
    print(countingData['number'])
    print(countingData['enabled'])
    print(countingData['warnings'])
    print(countingData['lastcounter'])
    channels = server.channels
    channel_options = []
    ccount = 0
    for channel in channels:
        ccount = ccount + 1
        if isinstance(channel, discord.TextChannel) and ccount != 25:
            channel_options.append(discord.SelectOption(label=channel.name, value=str(channel.id)))
    class CountingView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=None)

        @discord.ui.button(label="Toggle Counting", style=discord.ButtonStyle.primary, custom_id="toggle_counting", emoji="🔢")
        async def toggle_counting(self, interaction: discord.Interaction, button: discord.ui.Button):
            counting_json = countingDB.get(server.id)
            countingData = counting_json
            current_setting = countingData['enabled']
            if current_setting:
                countingData['enabled'] = False
                countingDB.set(server.id, countingData)
                await interaction.response.send_message("Counting disabled.", ephemeral=True)
            else:
                countingData['enabled'] = True
                countingDB.set(server.id, countingData)
                await interaction.response.send_message("Counting enabled.", ephemeral=True)

        @discord.ui.select(placeholder="Select Counting Channel", options=channel_options, custom_id="select_channel")
        async def select_channel(self, interaction: discord.Interaction, select: discord.ui.Select):
            selected_channel_id = int(select.values[0])
            counting_json = countingDB.get(server.id)
            countingData = counting_json
            countingData['channel'] = selected_channel_id
            countingDB.set(server.id, countingData)
            await interaction.response.send_message(f"Counting channel set to <#{selected_channel_id}>.", ephemeral=True)
    embed = discord.Embed(
        title="Counting Settings",
        description=f"**Current Settings:**\n- Counting Enabled: `{countingData['enabled']}`\n- Counting Channel: `<#{countingData['channel']}>`\n- Current Number: `{countingData['number']}`\n- Highest Number: `{countingData.get('highestnumber', 0)}`\n- Warnings: `{countingData['warnings']}/{counting_limit(countingData)}`\n- Last Counter: `{countingData['lastcounter']}`",
        color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
    )
    embed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
    await interaction.response.send_message(embed=embed, view=CountingView(), ephemeral=True)

@bot.tree.command(name="spookpfp", description="Get a pfp from a user's spook.bio profile.")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def spookpfp(interaction: discord.Interaction, username: str = "phis"):
    url = f"https://spook.bio/api/profiles/{username}"
    response = requests.get(url)
    data = response.json()
    if response.status_code == 200:
        await interaction.response.send_message(data.avatar, ephemeral=False)
        print("Fetched data successfully!")
    else:
        await interaction.response.send_message(f":x: {username} Not Found :x:", ephemeral=False)
        print(f"Error fetching data: {response.status_code}")

@bot.tree.command(name="discord2spook", description="Get a spook.bio profile from a discord user.")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def discord2spook(interaction: discord.Interaction, user: discord.User): # = 481295611417853982):
    url = f"https://spook.bio/api/profiles?discordId={user.id}"
    print(user.id)
    print(url)
    response = requests.get(url)
    data = response.json()
    if response.status_code == 200:
        await interaction.response.send_message(f"{user.mention}'s [spook.bio Profile](https://spook.bio/@{data.get('username')})", ephemeral=False)
        print(f"Fetched {data['username']} successfully!")
    else:
        if interaction.user.name == user.name:
            await interaction.response.send_message(f":x: You don't have a spook.bio profile linked to your account {user.mention}! :x: To link your profile to your account create a spook.bio account [here](https://spook.bio/login)")
            return
        await interaction.response.send_message(f":x: {user.mention} doesn't have a spook.bio profile linked to their account! :x:", ephemeral=False)
        print(f"Error fetching data: {response.status_code}")

@bot.tree.command(name="ping", description="Check the bot's latency.")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def ping(interaction: discord.Interaction):
    latency = bot.latency * 1000
    connection = "Good" if latency < 200 else "Average" if latency < 400 else "Poor"
    embed = discord.Embed(
        title="Bot Server Stats"
        , description=f"Latency: `{latency:.2f}ms` ({connection} Connection)"
        , color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
    )
    embed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
    await interaction.response.send_message(embed=embed)
    await asyncio.sleep(2)
    updatedlatency = bot.latency * 1000
    updatedconnection = "Good" if updatedlatency < 200 else "Average" if updatedlatency < 400 else "Poor"
    embed = discord.Embed(
        title="Bot Server Stats"
        , description=f"OriginalLatency: `{latency:.2f}ms` ({connection} Connection)\nEditLatency: `{updatedlatency:.2f}ms` ({updatedconnection} Connection)"
        , color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
    )
    embed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
    await interaction.edit_original_response(embed=embed, content=None)

@roblox_group.command(name="todiscord", description="Get a roblox user's Discord from their username.")
async def roblox2discord(interaction: discord.Interaction, user: str):
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

    url = f"{APIBaseURL}/r2d/{UserID}"

    try:
        response = requests.get(url, headers=APIBaseURL_Headers)
        response.raise_for_status()
        APIData = response.json()
        print(APIData)
        Discord = None
        if APIData.get("data") and APIData.get("success", False) == True:
            successembed2 = discord.Embed(
                title=f"Discord User for {Username}",
                description=f"[View Discord Profile](https://discord.com/users/{APIData['data']})",
                color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
            )

            successembed2.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
            await interaction.edit_original_response(embed=successembed2)
            return
        else:
            #print(f"No matching Discord user found for {Username}")
            failedembed1 = discord.Embed(
                title=f"No Discord User found for {Username}",
                color=discord.Color.red()
            )
            await interaction.edit_original_response(embed=failedembed1)
            return
    except requests.exceptions.RequestException as e:
        #print(f"Error fetching data for ID {UserID}: {e}")
        failedembed2 = discord.Embed(
            title=f"Error retrieving Discord User from {Username}",
            color=discord.Color.red()
        )
        await interaction.edit_original_response(embed=failedembed2)
        # await interaction.edit_original_response(f"Error retrieving Discord User from {url}")
        return

@roblox_group.command(name="fromdiscord", description="Get a roblox profile from their Discord UserID.")
async def discord2roblox(interaction: discord.Interaction, user: discord.User):
    userid = user.id
    await interaction.response.defer(thinking=True)

    loading = discord.Embed(
        title=f"{Emojis.get('loading')} {interaction.user.mention} Getting Roblox Profile For {user.name}",
        color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
    )

    await interaction.followup.send(embed=loading)

    url = f"{APIBaseURL}/d2r/{userid}"

    try:
        response = requests.get(url, headers=APIBaseURL_Headers)
        response.raise_for_status()
        APIData = response.json()
        print(APIData)
        Roblox = None
        if APIData.get("data") and APIData.get("success", False) == True:
            successembed = discord.Embed(
                title=f"Roblox Profile for {user.name}",
                description=f"[View Roblox Profile](https://www.roblox.com/users/{APIData['data']}/profile)",
                color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
            )
            successembed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
            await interaction.edit_original_response(embed=successembed)
            return
        else:
            #print(f"No Roblox Profile found for {user.name}")
            failedembed = discord.Embed(
                title=f"No Roblox Profile found for {user.name}",
                color=discord.Color.red()
            )
            await interaction.edit_original_response(embed=failedembed)
            return
    except requests.exceptions.RequestException as e:
        #print(f"Error fetching data for Discord ID: {userid}: {e}")
        failedembed3 = discord.Embed(
            title=f"Error retrieving Roblox Profile from {user.name}",
            color=discord.Color.red()
        )
        await interaction.edit_original_response(embed=failedembed3)
        return

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
                f"You are a helpful Discord assistant chatting with {username}. "
                "Always respond in a single concise paragraph. Otherwise your response will not be recieved due to the embed text limit"
                "Follow Discord TOS. Do not provide instructions for illegal activity. "
                "Stay safe, respectful, and friendly."
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

@bot.tree.command(name="google", description="Search Something On Google.")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def google(interaction: discord.Interaction, query: str = "shapes.lol"):
    await interaction.response.defer()
    
    thinkingembed = discord.Embed(
        title=f"{Emojis.get('loading')} {interaction.user.mention} Searching Google For {query}",
        color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
    )
    await interaction.followup.send(embed=thinkingembed)
    
    # replace spaces with + in query for google search link
    properquery = query.replace(" ", "+")
    
    url = f"https://www.googleapis.com/customsearch/v1?key={GoogleAPIKey}&cx={searchengine}&q={properquery}"
    try:
        response = requests.get(url)
        response.raise_for_status()
        data = response.json()

        # Get First 5 results
        if "items" in data and len(data["items"]) >= 5:
            first_result = data["items"][0]
            title = first_result.get("title", "No Title")
            snippet = first_result.get("snippet", "No Description")
            link = first_result.get("link", "No Link")
            print(f"First Result: {title} - {link}")
            second_result = data["items"][1]
            second_result_title = second_result.get("title", "No Title")
            second_result_snippet = second_result.get("snippet", "No Description")
            second_result_link = second_result.get("link", "No Link")
            print(f"Second Result: {second_result_title} - {second_result_link}")
            third_result = data["items"][2]
            third_result_title = third_result.get("title", "No Title")
            third_result_snippet = third_result.get("snippet", "No Description")
            third_result_link = third_result.get("link", "No Link")
            print(f"Third Result: {third_result_title} - {third_result_link}")
            fourth_result = data["items"][3]
            fourth_result_title = fourth_result.get("title", "No Title")
            fourth_result_snippet = fourth_result.get("snippet", "No Description")
            fourth_result_link = fourth_result.get("link", "No Link")
            print(f"Fourth Result: {fourth_result_title} - {fourth_result_link}")
            fifth_result = data["items"][4]
            fifth_result_title = fifth_result.get("title", "No Title")
            fifth_result_snippet = fifth_result.get("snippet", "No Description")
            fifth_result_link = fifth_result.get("link", "No Link")
            print(f"Fifth Result: {fifth_result_title} - {fifth_result_link}")
            
            embed = discord.Embed(
                title=f"Google Results For {query}",
                description=f"**1. [{title}]({link})**\n{snippet}\n\n**2. [{second_result_title}]({second_result_link})**\n{second_result_snippet}\n\n**3. [{third_result_title}]({third_result_link})**\n{third_result_snippet}\n\n**4. [{fourth_result_title}]({fourth_result_link})**\n{fourth_result_snippet}\n\n**5. [{fifth_result_title}]({fifth_result_link})**\n{fifth_result_snippet}\n\n[Search For More Results](https://google.com/search?q={properquery})",
                color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
            )
            embed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
            await interaction.edit_original_response(embed=embed)
        else:
            noresultembed = discord.Embed(
                title=":x: No results found! :x:",
                description=f"No Results for {query} | [Search on Google Yourself For More Results](https://google.com/search?q={properquery})",
                color=discord.Color.red()
            )
            noresultembed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
            await interaction.edit_original_response(embed=noresultembed)
    except requests.exceptions.RequestException as e:
        print(f"An error occurred during the API request: {e}")
        errorembed = discord.Embed(
            title=":x: An error occurred while searching Google. Please try again later. :x:",
            color=discord.Color.red()
        )
        errorembed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
        await interaction.edit_original_response(embed=errorembed)

@bot.tree.command(name="invite", description="Get the bot's invite link.")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def invite(interaction: discord.Interaction):
    invite_url = f"https://discord.com/api/oauth2/authorize?client_id={bot.user.id}"
    invite_embed = discord.Embed(
        description=f"[Click Here To Add Shapes To Your Server or Apps]({invite_url})",
        color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
    )
    invite_embed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
    await interaction.response.send_message(embed=invite_embed, ephemeral=False)
    #await interaction.response.send_message(f"Invite me to your server or add me to your apps using this link: {invite_url}", ephemeral=False)
    
@roblox_group.command(name="info", description="Get a Roblox user's profile information.")
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
        items = {
            'hats': ['18824203', '93078560', '102611803'],
            'sign': '1567446'
        }
        
        has_sign = await check_ownership(session, user_id, items['sign'])
        if has_sign:
            return True
        
        for hat_id in items['hats']:
            if await check_ownership(session, user_id, hat_id):
                return True
        
        return False

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
            roblosecurity_token = ROBLOSECURITY
            if not roblosecurity_token:
                print("ROBLOSECURITY is not set in .env")
                return (0, None)
            
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
        return (0, None)

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
        return 0

    async def get_followers_count(session: aiohttp.ClientSession, user_id: str) -> int:
        try:
            followers_url = f"https://friends.roblox.com/v1/users/{user_id}/followers/count"
            async with session.get(followers_url) as response:
                if response.status == 200:
                    followers_data = await response.json()
                    return followers_data.get('count', 0)
        except Exception as e:
            print(f"Error fetching followers count: {e}")
        return 0

    async def get_followings_count(session: aiohttp.ClientSession, user_id: str) -> int:
        try:
            followings_url = f"https://friends.roblox.com/v1/users/{user_id}/followings/count"
            async with session.get(followings_url) as response:
                if response.status == 200:
                    followings_data = await response.json()
                    return followings_data.get('count', 0)
        except Exception as e:
            print(f"Error fetching followings count: {e}")
        return 0

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
        return 0

    async def get_profile_api_data(session: aiohttp.ClientSession, user_id: str) -> dict:
        try:
            roblosecurity_token = ROBLOSECURITY
            
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
                    
        except Exception as e:
            print(f"Error fetching profile API data: {e}")
        
        return {
            'visits': 0,
            'friends_count': 0,
            'followers_count': 0,
            'followings_count': 0,
            'is_verified': False,
            'is_premium': False,
            'description': '',
            'join_date': '',
            'last_online': None
        }

    try:
        response = requests.post(url, json=request_payload)
        response.raise_for_status()
        data = response.json()
        if data.get("data") and len(data["data"]) > 0:
            userinfo = data["data"][0]
            UserID = userinfo["id"]
            Display = userinfo["displayName"]
            hasVerifiedBadge = userinfo.get("hasVerifiedBadge", False)

            rap_value = 0
            value_value = 0
            rolimons_last_online = None
            badge_last_online = None
            badge_place_id = None
            is_premium = False
            is_verified = False
            roblox_badges = []
            total_visits = 0
            inventory_visibility = "Private"
            friends_count = 0
            followers_count = 0
            followings_count = 0
            presence_type = 0
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
                    async with session.get(rolimons_stats_url, headers={'User-Agent': 'shapes.lol'}) as response:
                        if response.status == 200:
                            rolimons_stats_data = await response.json()
                            if rolimons_stats_data.get('success'):
                                rap_value = rolimons_stats_data.get('rap', 0) or 0
                                value_value = rolimons_stats_data.get('value', 0) or 0
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
                response = requests.get(avatar_url)
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

            url = f"{APIBaseURL}/r2d/{UserID}"
            try:
                response = requests.get(url, headers={"Authorization": f"Bearer {APIBaseURL_Key}"})
                response.raise_for_status()
                Data = response.json()
                
                if Data["success"] == False:
                    Discord = None
                elif Data["success"] == True:
                    Discord = Data["data"]
                else:
                    Discord = None
            except requests.exceptions.RequestException as e:
                #print(f"Error fetching data for ID: {UserID}: {e}")
                Discord = None

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
                badge_emojis = []
                for badge in roblox_badges:
                    badge_name = badge.get("name", "")
                    if badge_name in badges:
                        badge_emojis.append(badges[badge_name])
                
                if badge_emojis:
                    badges_display = " ".join(badge_emojis)
                else:
                    badges_display = "None"

            presence_status_map = {
                0: {'icon_url': 'https://files.catbox.moe/tjiecu.png', 'text': 'Offline'},
                1: {'icon_url': 'https://files.catbox.moe/h69xeq.png', 'text': 'Online'},
                2: {'icon_url': 'https://files.catbox.moe/80ta5t.png', 'text': 'In Game'},
                3: {'icon_url': 'https://files.catbox.moe/72opoa.png', 'text': 'In Studio'}
            }

            current_status = presence_status_map.get(presence_type, presence_status_map[0])

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

            friends_followers_text = f"-# **{friends_count:,}** Friends | **{followers_count:,}** Followers | **{followings_count:,}** Following"
            
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
                embed.add_field(name="Visits", value=f"{total_visits:,}", inline=True)
                
                embed.add_field(name="Created", value=created_timestamp if created_timestamp else "Unknown", inline=True)
                embed.add_field(name="Last Online", value=formatted_last_online, inline=True)
                embed.add_field(name="Badges", value=badges_display, inline=True)
            else:
                embed.add_field(name="ID", value=f"{UserID}", inline=True)
                embed.add_field(name="Verified", value="Hat" if is_verified else "False", inline=True)
                embed.add_field(name="Inventory", value=inventory_visibility, inline=True)
                
                embed.add_field(name="RAP", value=f"[`{rap_value:,}`]({rolimonsurl})", inline=True)
                embed.add_field(name="Value", value=f"[`{value_value:,}`]({rolimonsurl})", inline=True)
                embed.add_field(name="Visits", value=f"{total_visits:,}", inline=True)
                
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
                    name="Discord",
                    value=f"```txt\n{Discord}\n```",
                    inline=False
                )

            if avatar_image:
                embed.set_thumbnail(url=avatar_image)
            
            embed.set_footer(
                text=f"{current_status['text']} | Requested by {interaction.user.name} | https://shapes.lol",
                icon_url=current_status['icon_url']
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
        
@roblox_group.command(name="british", description="Check if a user has their language set to British")
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

@roblox_group.command(name="item", description="Get detailed information about a Roblox item")
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

@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@roblox_group.command(name="place", description="Get detailed information about a Roblox place")
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
@roblox_group.command(name="badge", description="Get detailed information about a Roblox badge")
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
            
@bot.tree.command(name="autorole", description="Set a role to be automatically given to members")
@app_commands.default_permissions(administrator=True)
@commands.bot_has_permissions(add_reactions=True, moderate_members=True, read_message_history=True, view_channel=True, send_messages=True)
@app_commands.allowed_installs(guilds=True, users=False)
@app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
@app_commands.describe(
    role="The role to automatically assign to members",
    enable="Whether to enable or disable autorole (default: True)"
)
async def autorole(interaction: discord.Interaction, role: discord.Role, enable: bool = True):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("You need administrator permissions to use this command.", ephemeral=True)
        return
    
    autorole_data = {"role_id": role.id, "enabled": enable}
    autoroleDB.set(f"{interaction.guild.id}", autorole_data)
    autoroleDB.save()
    
    class AutoroleView(discord.ui.View):
        @discord.ui.button(label="Assign to Existing Members", style=discord.ButtonStyle.primary)
        async def assign_existing(self, interaction: discord.Interaction, button: discord.ui.Button):
            count = 0
            for member in interaction.guild.members:
                if not member.bot and role not in member.roles:
                    try:
                        await member.add_roles(role)
                        count += 1
                    except:
                        pass
            await interaction.response.send_message(f"Assigned {role.mention} to {count} existing members", ephemeral=True)
    
    status = "enabled" if enable else "disabled"
    await interaction.response.send_message(
        f"Autorole {status} for {role.mention}. Assign to existing members?", 
        view=AutoroleView(), 
        ephemeral=True
    )

class HelpDropdown(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Counting Commands", description="Counting game and settings"),
            discord.SelectOption(label="User Settings", description="Personal bot settings"),
            discord.SelectOption(label="Roblox Commands", description="Roblox user and item information"),
            discord.SelectOption(label="Utility Commands", description="General utility commands"),
            discord.SelectOption(label="Context Menu Commands", description="Right-click context commands")
        ]
        super().__init__(placeholder="Choose a command category...", options=options)

    async def callback(self, interaction: discord.Interaction):
        category = self.values[0]
        embed_color = embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
        
        if category == "Counting Commands":
            embed = discord.Embed(title="Counting Commands", color=embed_color)
            embed.add_field(name="Commands", value=
                "• /counting - Counting in a specific channel", inline=False)
                
        elif category == "User Settings":
            embed = discord.Embed(title="User Settings", color=embed_color)
            embed.add_field(name="Commands", value=
                "• /settings - Customize your personal bot settings", inline=False)
                
        elif category == "Roblox Commands":
            embed = discord.Embed(title="Roblox Commands", color=embed_color)
            embed.add_field(name="Commands", value=
                "• /roblox info - Get detailed Roblox user information\n"
                "• /roblox todiscord - Find a Roblox user's Discord\n"
                "• /roblox fromdiscord - Find a Discord user's Roblox\n"
                "• /roblox british - Check if user is british\n"
                "• /roblox item, /roblox bundle, /roblox asset - Item information\n"
                "• /roblox group - Roblox group information\n"
                "• /roblox place - Roblox place/game information\n"
                "• /roblox badge, /roblox badges, /roblox recentbadges - Badges\n"
                "• /roblox avatar, /roblox outfits, /roblox limiteds - Avatars and inventory", inline=False)
            embed.add_field(name="Verification", value=
                "• /roblox verify - Verify in a server (no Yapin' account needed)\n"
                "• /roblox link - Link a Roblox account (up to 100)\n"
                "• /roblox accounts, /roblox auto, /roblox remove, /roblox hide, /roblox show - Manage your accounts\n"
                "• /roblox panel ..., /roblox sync - Server setup (admins)\n"
                "• /vortex game, /vortex user, /vortex cookie - PlayVortex lookups\n"
                "• /lastfm link, /lastfm unlink, /lastfm user - Last.fm", inline=False)
                
        elif category == "Utility Commands":
            embed = discord.Embed(title="Utility Commands", color=embed_color)
            embed.add_field(name="Commands", value=
                "• /ping - Check bot latency and connection\n"
                "• /invite - Get bot invite link\n"
                "• /google - Search something on Google\n"
                "• /status - Check shapes.lol status\n"
                "• /userinstalls - Get user installation count\n"
                "• /servercount - Get server count\n"
                "• /server info - Information about this server\n"
                "• /spookpfp - Get profile picture from spook.bio", inline=False)
                
        elif category == "Context Menu Commands":
            embed = discord.Embed(title="Context Menu Commands", color=embed_color)
            embed.add_field(name="Commands", value=
                "• Right-click a user → Apps → sayhitouser - Say hello to a user\n"
                "• Right-click a user → Apps → discord2spook - Get user's spook.bio profile\n"
                "• Right-click a message → Apps → google - Search message content on Google", inline=False)
        
        embed.set_footer(text="Use slash commands (/) to interact")
        await interaction.response.edit_message(embed=embed, ephemeral=True)

class HelpView(discord.ui.View):
    def __init__(self):
        super().__init__()
        self.add_item(HelpDropdown())

@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@bot.tree.command(name="help", description="Get help with all available commands")
async def help_command(interaction: discord.Interaction):
    """Display help information for all bot commands"""
    
    embed_color = embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
    
    embed = discord.Embed(
        title="Bot Help & Commands",
        description="Use the dropdown menu below to browse commands by category.",
        color=embed_color
    )
    
    view = HelpView()
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

@bot.tree.command(name="countingleaderboard", description="View the counting leaderboard website")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def countingleaderboard(interaction: discord.Interaction):
    """Send the counting leaderboard website link"""
    
    embed = discord.Embed(
        title="Counting Leaderboard",
        description=f"[Click here to view the counting leaderboard](https://shapes.lol/counting/leaderboard/)",
        color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
    )
    embed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
    
    await interaction.response.send_message(embed=embed)
            
@roblox_group.command(name="outfits", description="Get all outfits for a Roblox user")
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
        
@roblox_group.command(name="badges", description="Check if a user has a specific badge and get detailed information")
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
            
            @discord.ui.button(emoji="🔍", style=discord.ButtonStyle.primary)
            async def search_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                if interaction.user.id != self.requester_id:
                    await interaction.response.send_message("This is not your command!", ephemeral=True)
                    return
                
                modal = BadgeSearchModal(self.badges, self.user_id, self.username, self.requester_id, self.current_index)
                await interaction.response.send_modal(modal)
        
        class BadgeSearchModal(discord.ui.Modal, title="Search Badge by ID"):
            def __init__(self, badges, user_id, username, requester_id, current_index):
                super().__init__()
                self.badges = badges
                self.user_id = user_id
                self.username = username
                self.requester_id = requester_id
                self.current_index = current_index
            
            badge_id_input = discord.ui.TextInput(
                label="Badge ID",
                placeholder="Enter the badge ID to search...",
                required=True,
                max_length=20
            )
            
            async def on_submit(self, interaction: discord.Interaction):
                badge_id = self.badge_id_input.value.strip()
                found_index = -1
                
                for i, badge in enumerate(self.badges):
                    if str(badge.get("id")) == badge_id:
                        found_index = i
                        break
                
                if found_index == -1:
                    await interaction.response.defer()
                    errorembed = discord.Embed(
                        title=f":x: Badge Not Found :x:",
                        description=f"User `{self.username}` does not have badge ID `{badge_id}`",
                        color=discord.Color.red()
                    )
                    errorembed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
                    
                    original_view = BadgePaginator(self.badges, self.user_id, self.username, self.requester_id, self.current_index)
                    await interaction.edit_original_response(embed=errorembed, view=original_view)
                    return
                
                await interaction.response.defer()
                
                badge = self.badges[found_index]
                badge_id = str(badge.get("id"))
                
                badge_info_task = asyncio.create_task(get_badge_info(badge_id))
                awarded_date_task = asyncio.create_task(get_awarded_date(self.user_id, badge_id))
                thumbnail_task = asyncio.create_task(get_badge_thumbnail(badge_id))
                
                badge_info, badge_error = await badge_info_task
                awarded_data, awarded_error = await awarded_date_task
                badge_thumbnail = await thumbnail_task
                
                if badge_error:
                    await interaction.followup.send("Failed to fetch badge information.", ephemeral=True)
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
                embed.set_footer(text=f"Badge {found_index + 1}/{len(self.badges)} | Requested By {interaction.user.name} | {MainURL}")
                
                view = BadgePaginator(self.badges, self.user_id, self.username, self.requester_id, found_index)
                await interaction.edit_original_response(embed=embed, view=view)
        
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

@bot.tree.command(name="discorduser", description="Get information about a user")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def discorduser(interaction: discord.Interaction, user: discord.User = None):
    await interaction.response.defer()
    
    if user is None:
        user = interaction.user
    
    target_member = None
    guild = interaction.guild
    
    if guild:
        target_member = guild.get_member(user.id)
    
    created_at = isotodiscordtimestamp(user.created_at.isoformat(), "D")
    
    BADGE_FLAGS = {
        4194304: "<:activedeveloper:1431764482312372385>",
        512: "<:earlysupporter:1431764483000504351>",
        64: "<:hypesquadp:1431764484179099788>",
        128: "<:hypesquadr:1431764485374214267>",
        256: "<:hypesquadg:1431764486636830780>",
    }
    
    badges = []
    public_flags = user.public_flags.value if user.public_flags else 0
    
    for flag, emoji in BADGE_FLAGS.items():
        if public_flags & flag:
            badges.append(emoji)
    
    has_nitro = False
    if user.avatar:
        if user.avatar.is_animated():
            has_nitro = True
            badges.append("<:Nitro:1431740129726300191>")
    
    try:
        user_data = await bot.fetch_user(user.id)
        if user_data.banner:
            has_nitro = True
            if "<:Nitro:1431777332552536155>" not in badges:
                badges.append("<:Nitro:1431777332552536155>")
    except:
        pass
    
    badges_display = " ".join(badges) if badges else "None"
    
    embed = discord.Embed(
        title=f"{user.name}",
        url=f"https://discord.com/users/{user.id}",
        color=discord.Color.blue(),
        timestamp=interaction.created_at
    )
    
    if user.avatar:
        embed.set_thumbnail(url=user.avatar.url)
    
    user_info = f"""
    > **Username:** `{user.name}`
    > **ID:** `{user.id}`
    > **Created:** {created_at}
    > **Bot:** `{user.bot}`
    > **Nitro:** `{has_nitro}`
    > **Badges:** {badges_display}
    """
    
    embed.add_field(
        name="User Information",
        value=user_info,
        inline=False
    )
    
    if guild and target_member:
        if target_member.joined_at:
            joined_at = isotodiscordtimestamp(target_member.joined_at.isoformat(), "D")
        else:
            joined_at = "Unknown"
        
        roles = [role.mention for role in target_member.roles[1:][:10]]
        roles_display = ", ".join(roles) if roles else "None"
        if len(target_member.roles) > 11:
            roles_display += f" (+{len(target_member.roles) - 11} more)"
        
        server_info = f"""
        > **Joined:** {joined_at}
        > **Nickname:** `{target_member.nick or 'None'}`
        > **Top Role:** {target_member.top_role.mention}
        """
        
        embed.add_field(
            name="Server Info",
            value=server_info,
            inline=False
        )
        
        embed.add_field(
            name=f"Roles ({len(target_member.roles)})",
            value=f"> {roles_display}",
            inline=False
        )
    
    embed.set_footer(text=f"Requested by {interaction.user.display_name} | {MainURL}")

    class MutualServersView(discord.ui.View):
        def __init__(self, user_id, username, requester_id, main_embed):
            super().__init__(timeout=60)
            self.user_id = user_id
            self.username = username
            self.requester_id = requester_id
            self.main_embed = main_embed

        async def interaction_check(self, interaction: discord.Interaction) -> bool:
            return interaction.user.id == self.requester_id

        @discord.ui.button(label="View Mutual Servers", style=discord.ButtonStyle.blurple)
        async def mutuals_button(self, interaction: discord.Interaction, button: discord.ui.Button):
            await interaction.response.defer()
            
            try:
                import aiohttp
                async with aiohttp.ClientSession() as session:
                    async with session.get("http://localhost:13455/mutuals") as response:
                        if response.status == 200:
                            data = await response.json()
                            
                            if not data or "Servers" not in data:
                                class NoServersView(discord.ui.View):
                                    def __init__(self, main_embed, requester_id, user_id, username):
                                        super().__init__(timeout=120)
                                        self.main_embed = main_embed
                                        self.requester_id = requester_id
                                        self.user_id = user_id
                                        self.username = username

                                    async def interaction_check(self, interaction: discord.Interaction) -> bool:
                                        return interaction.user.id == self.requester_id

                                    @discord.ui.button(label="Back to User Info", style=discord.ButtonStyle.gray)
                                    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                                        view = MutualServersView(self.user_id, self.username, self.requester_id, self.main_embed)
                                        await interaction.response.edit_message(content="", embed=self.main_embed, view=view)
                                
                                no_servers_view = NoServersView(self.main_embed, self.requester_id, self.user_id, self.username)
                                await interaction.edit_original_response(content="No mutual servers data available.", embed=None, view=no_servers_view)
                                return

                            user_servers = []
                            for server_id, server_data in data["Servers"].items():
                                members = server_data.get("members", [])
                                for member in members:
                                    if str(member.get("id")) == str(self.user_id):
                                        user_servers.append({
                                            "server_id": server_id,
                                            "server_name": server_data.get("name", "Unknown Server"),
                                            "member_count": server_data.get("membercount", 0),
                                            "bot_count": server_data.get("botcount", 0),
                                            "channel_count": server_data.get("channels", 0),
                                            "icon_url": server_data.get("iconurl"),
                                            "created_at": server_data.get("createdat"),
                                            "roles": server_data.get("roles", 0),
                                            "verification_level": server_data.get("verificationlevel", "Unknown"),
                                            "owner": server_data.get("owner", "Unknown"),
                                            "owner_id": server_data.get("ownerid", "Unknown")
                                        })
                                        break

                            if not user_servers:
                                class NoMutualServersView(discord.ui.View):
                                    def __init__(self, main_embed, requester_id, user_id, username):
                                        super().__init__(timeout=120)
                                        self.main_embed = main_embed
                                        self.requester_id = requester_id
                                        self.user_id = user_id
                                        self.username = username

                                    async def interaction_check(self, interaction: discord.Interaction) -> bool:
                                        return interaction.user.id == self.requester_id

                                    @discord.ui.button(label="Back to User Info", style=discord.ButtonStyle.gray)
                                    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                                        view = MutualServersView(self.user_id, self.username, self.requester_id, self.main_embed)
                                        await interaction.response.edit_message(content="", embed=self.main_embed, view=view)
                                
                                no_mutual_view = NoMutualServersView(self.main_embed, self.requester_id, self.user_id, self.username)
                                await interaction.edit_original_response(content="No mutual servers found for this user.", embed=None, view=no_mutual_view)
                                return

                            pages = []
                            for server in user_servers:
                                server_embed = discord.Embed(
                                    title=f"Mutual Server: {server['server_name']}",
                                    color=discord.Color.green(),
                                    timestamp=discord.utils.utcnow()
                                )
                                
                                if server['icon_url']:
                                    server_embed.set_thumbnail(url=server['icon_url'])
                                
                                if server['created_at']:
                                    created_at_display = isotodiscordtimestamp(server['created_at'], "D")
                                else:
                                    created_at_display = "Unknown"
                                
                                server_info = f"""
                                > **Name:** {server['server_name']}
                                > **ID:** `{server['server_id']}`
                                > **Created:** {created_at_display}
                                > **Owner:** {server['owner']} (`{server['owner_id']}`)
                                > **Verification:** {server['verification_level'].title()}
                                """
                                
                                server_stats = f"""
                                > **Members:** {server['member_count'] + server['bot_count']:,} total
                                > **People:** {server['member_count']:,}
                                > **Bots:** {server['bot_count']:,}
                                > **Channels:** {server['channel_count']}
                                > **Roles:** {server['roles']}
                                """
                                
                                server_embed.add_field(
                                    name="Server Information",
                                    value=server_info,
                                    inline=False
                                )
                                
                                server_embed.add_field(
                                    name="Server Stats",
                                    value=server_stats,
                                    inline=False
                                )
                                
                                server_embed.set_footer(text=f"Page {len(pages) + 1}/{len(user_servers)} | {MainURL}")
                                pages.append(server_embed)

                            if len(pages) == 1:
                                class SingleServerView(discord.ui.View):
                                    def __init__(self, main_embed, requester_id, user_id, username):
                                        super().__init__(timeout=120)
                                        self.main_embed = main_embed
                                        self.requester_id = requester_id
                                        self.user_id = user_id
                                        self.username = username

                                    async def interaction_check(self, interaction: discord.Interaction) -> bool:
                                        return interaction.user.id == self.requester_id

                                    @discord.ui.button(label="Back to User Info", style=discord.ButtonStyle.gray)
                                    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                                        view = MutualServersView(self.user_id, self.username, self.requester_id, self.main_embed)
                                        await interaction.response.edit_message(content="", embed=self.main_embed, view=view)
                                
                                single_view = SingleServerView(self.main_embed, self.requester_id, self.user_id, self.username)
                                await interaction.edit_original_response(content="", embed=pages[0], view=single_view)
                            else:
                                class PaginationView(discord.ui.View):
                                    def __init__(self, pages, main_embed, requester_id, user_id, username):
                                        super().__init__(timeout=120)
                                        self.pages = pages
                                        self.current_page = 0
                                        self.main_embed = main_embed
                                        self.requester_id = requester_id
                                        self.user_id = user_id
                                        self.username = username

                                    async def interaction_check(self, interaction: discord.Interaction) -> bool:
                                        return interaction.user.id == self.requester_id

                                    @discord.ui.button(emoji="⬅️", style=discord.ButtonStyle.gray)
                                    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                                        self.current_page = (self.current_page - 1) % len(self.pages)
                                        await interaction.response.edit_message(content="", embed=self.pages[self.current_page], view=self)
                                    
                                    @discord.ui.button(emoji="➡️", style=discord.ButtonStyle.gray)
                                    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                                        self.current_page = (self.current_page + 1) % len(self.pages)
                                        await interaction.response.edit_message(content="", embed=self.pages[self.current_page], view=self)
                                    
                                    @discord.ui.button(label="Back to User Info", style=discord.ButtonStyle.gray)
                                    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                                        view = MutualServersView(self.user_id, self.username, self.requester_id, self.main_embed)
                                        await interaction.response.edit_message(content="", embed=self.main_embed, view=view)
                                
                                pagination_view = PaginationView(pages, self.main_embed, self.requester_id, self.user_id, self.username)
                                await interaction.edit_original_response(content="", embed=pages[0], view=pagination_view)
                                
                        else:
                            class ErrorView(discord.ui.View):
                                def __init__(self, main_embed, requester_id, user_id, username):
                                    super().__init__(timeout=120)
                                    self.main_embed = main_embed
                                    self.requester_id = requester_id
                                    self.user_id = user_id
                                    self.username = username

                                async def interaction_check(self, interaction: discord.Interaction) -> bool:
                                    return interaction.user.id == self.requester_id

                                @discord.ui.button(label="Back to User Info", style=discord.ButtonStyle.gray)
                                async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                                    view = MutualServersView(self.user_id, self.username, self.requester_id, self.main_embed)
                                    await interaction.response.edit_message(content="", embed=self.main_embed, view=view)
                            
                            error_view = ErrorView(self.main_embed, self.requester_id, self.user_id, self.username)
                            await interaction.edit_original_response(content="Failed to fetch mutual servers data.", embed=None, view=error_view)
            except Exception as e:
                class ExceptionView(discord.ui.View):
                    def __init__(self, main_embed, requester_id, user_id, username):
                        super().__init__(timeout=120)
                        self.main_embed = main_embed
                        self.requester_id = requester_id
                        self.user_id = user_id
                        self.username = username

                    async def interaction_check(self, interaction: discord.Interaction) -> bool:
                        return interaction.user.id == self.requester_id

                    @discord.ui.button(label="Back to User Info", style=discord.ButtonStyle.gray)
                    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                        view = MutualServersView(self.user_id, self.username, self.requester_id, self.main_embed)
                        await interaction.response.edit_message(content="", embed=self.main_embed, view=view)
                
                exception_view = ExceptionView(self.main_embed, self.requester_id, self.user_id, self.username)
                await interaction.edit_original_response(content=f"Error fetching mutual servers: {str(e)}", embed=None, view=exception_view)

    view = MutualServersView(user.id, user.name, interaction.user.id, embed)
    await interaction.followup.send(content="", embed=embed, view=view)
    
@roblox_group.command(name="limiteds", description="Scan a roblox users inventory for limiteds")
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
                async with session.get(rolimons_url, headers={'User-Agent': 'cats.lol'}) as response:
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
        
@roblox_group.command(name="bundle", description="Get information about a Roblox bundle")
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
        
@roblox_group.command(name="asset", description="Get information about a Roblox asset")
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
        
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@bot.tree.command(name="discordavatar", description="Get a user's Discord avatar")
@app_commands.describe(user="The user to get the avatar from (leave empty for yourself)")
async def discordavatar(interaction: discord.Interaction, user: Optional[discord.User] = None):
    """Get a user's Discord avatar with high quality options"""
    await interaction.response.defer()
    target = user or interaction.user
    
    try:
        avatar_url = target.display_avatar.with_size(4096).url
        
        embed = discord.Embed(
            title=f"{target.name}'s Avatar",
            color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue(),
            timestamp=interaction.created_at
        )
        embed.set_image(url=avatar_url)
        embed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
        
        view = discord.ui.View(timeout=120)
        view.add_item(discord.ui.Button(
            label="Download Avatar",
            style=discord.ButtonStyle.link,
            url=avatar_url
        ))
        
        await interaction.followup.send(embed=embed, view=view)
        
    except Exception as e:
        errorembed = discord.Embed(
            title="❌ Failed to fetch avatar",
            description=f"An error occurred: {str(e)}",
            color=discord.Color.red()
        )
        errorembed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
        await interaction.followup.send(embed=errorembed)

@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@bot.tree.command(name="discordbanner", description="Get a user's Discord banner")
@app_commands.describe(user="The user to get the banner from (leave empty for yourself)")
async def discordbanner(interaction: discord.Interaction, user: Optional[discord.User] = None):
    """Get a user's Discord banner using direct API call"""
    await interaction.response.defer()
    target = user or interaction.user
    
    try:
        connector = aiohttp.TCPConnector(family=socket.AF_INET)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(
                f'https://discord.com/api/v9/users/{target.id}',
                headers={'Authorization': f'Bot {token}'}
            ) as resp:
                if resp.status != 200:
                    errorembed = discord.Embed(
                        title="User Not Found",
                        description="Could not fetch user data ",
                        color=discord.Color.red()
                    )
                    errorembed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
                    await interaction.followup.send(embed=errorembed)
                    return
                
                user_data = await resp.json()

        if user_data.get('banner'):
            banner_hash = user_data['banner']
            
            if banner_hash.startswith('a_'):
                banner_url = f"https://cdn.discordapp.com/banners/{target.id}/{banner_hash}.gif?size=4096"
            else:
                banner_url = f"https://cdn.discordapp.com/banners/{target.id}/{banner_hash}.webp?size=4096"
            
            embed = discord.Embed(
                title=f"{target.name}'s Banner",
                color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue(),
                timestamp=interaction.created_at
            )
            embed.set_image(url=banner_url)
            embed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
            
            view = discord.ui.View(timeout=120)
            view.add_item(discord.ui.Button(
                label="Download Banner",
                style=discord.ButtonStyle.link,
                url=banner_url
            ))
            
            await interaction.followup.send(embed=embed, view=view)
            
        else:
            errorembed = discord.Embed(
                title="No Banner Found",
                description=f"{target.name} doesn't have a banner image.",
                color=discord.Color.orange()
            )
            errorembed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
            await interaction.followup.send(embed=errorembed)
        
    except aiohttp.ClientError as e:
        errorembed = discord.Embed(
            title="❌ error",
            description=f"api request failed{str(e)}",
            color=discord.Color.red()
        )
        errorembed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
        await interaction.followup.send(embed=errorembed)
    except Exception as e:
        errorembed = discord.Embed(
            title="❌ Unexpected Error",
            description=f"Failed to fetch banner: {str(e)}",
            color=discord.Color.red()
        )
        errorembed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
        await interaction.followup.send(embed=errorembed)
        
@bot.tree.command(name="linkroblox", description="Link your Roblox account to Discord")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def linkroblox(interaction: discord.Interaction):
    embed = discord.Embed(
        description=f"[Click here to link your Roblox account](https://api.shapes.lol/login?type=discord)",
        color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
    )
    await interaction.response.send_message(embed=embed, ephemeral=False)
    
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
        roblosecurity_token = ROBLOSECURITY
            
        async with session.post("https://auth.roblox.com/v2/logout", 
                              headers={'Cookie': f'.ROBLOSECURITY={roblosecurity_token}'}) as response:
            token = response.headers.get("x-csrf-token")
            return token
    except Exception:
        return None

async def render_custom_avatar(session, user_id):
    try:
        roblosecurity_token = ROBLOSECURITY
        
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
@roblox_group.command(name="avatar", description="Get a Roblox user's avatar")
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

@roblox_group.command(name="recentbadges", description="Get a user's most recently earned Roblox badges")
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
        
# =============================================================================================================
# Roblox verification (/roblox ...), command logs (/cmdlog, /botlog), Vortex (/vortex ...), /server info.
# OAuth2 through the Shapes API, never bio text. Same system as the Yapin' bot on the same API data: people link Roblox
# accounts once (up to 100, no Yapin' account needed) and every server that uses verification sees them. A server admin
# points the bot at a Roblox group with /roblox panel and the bot mirrors it: one Discord role per group role (created,
# renamed and deleted as the group changes, when mirroring is on), a verified role and nicknames. Members verify with
# /roblox verify or the Verify button of a panel. They are synced when they verify, on join, on /roblox sync and by a
# backup sync every 24 hours (elapsed UTC seconds, so daylight saving can't move it). Roblox is only asked during a sync.
# Command logs post through a webhook (shared by the logs in one channel), falling back to the bot posting directly.
# =============================================================================================================
PLATFORM = "discord"
SYNC_EVERY = 24 * 60 * 60           # seconds of real elapsed time
CHECK_EVERY = 300                   # how often the scheduler looks at the clock
YAPIN_DASH = os.environ.get("YAPIN_DASH_URL", "https://shapes-yapper.oriaxen.com/dashboard/roblox")
_locks = {}
_prompted = {}
_clicked = {}
_tasks = set()
BOTLOG_KEY = "botlog"
RETRY_WEBHOOK_AFTER = 3600           # after Discord refuses a webhook in a channel, try again this often
HOOK_NAME = "Shapes logs"


class Issues:
    """What went wrong during one sync, grouped so the person gets one clear explanation instead of 500 identical errors."""

    def __init__(self, role_names=None):
        self.role_names = role_names or {}
        self.kinds = {}               # kind -> {"n": count, "names": set, "sample": text}
        self.blocked = set()          # role ids that were refused: stop trying them for everyone else
        self.nick_ok = self.nick_denied = 0

    @staticmethod
    def denied(e):
        return isinstance(e, discord.Forbidden) or getattr(e, "status", None) in (401, 403)

    def add(self, kind, e, name=None):
        """kind: create-role | edit-role | delete-role | give-role | take-role | nickname. Returns True when it was a refusal."""
        refused = self.denied(e)
        key = kind if refused else "other"
        d = self.kinds.setdefault(key, {"n": 0, "names": set(), "sample": ""})
        d["n"] += 1
        if name:
            d["names"].add(str(name))
        d["sample"] = d["sample"] or f"{kind}: {e}" + (f" (HTTP {e.status})" if getattr(e, "status", None) else "")
        return refused

    def name(self, rid):
        return self.role_names.get(str(rid), str(rid))

    def lines(self):
        """Plain-language explanations, most important first."""
        out = []

        def names(k):
            n = sorted(self.kinds[k]["names"])
            return (" (" + ", ".join(n[:8]) + (", …" if len(n) > 8 else "") + ")") if n else ""
        manage = [k for k in ("create-role", "edit-role", "delete-role") if k in self.kinds]
        if manage:
            what = {"create-role": "create", "edit-role": "rename", "delete-role": "delete"}
            out.append("I'm not allowed to " + "/".join(what[k] for k in manage) + f" roles{names(manage[0])}. Give my role the **Manage Roles** permission "
                       "(Server Settings → Roles → my role → Permissions), then run `/roblox sync` again. Without it I can only use roles that already exist.")
        given = [k for k in ("give-role", "take-role") if k in self.kinds]
        if given:
            n = sum(self.kinds[k]["n"] for k in given)
            nm = sorted(set().union(*(self.kinds[k]["names"] for k in given)))
            out.append(f"I couldn't change {n} role assignment{'s' if n != 1 else ''}" + (f" for **{', '.join(nm[:8])}**" if nm else "") +
                       ". A bot can only hand out roles that sit **below its own highest role**: drag my role above "
                       + ("those roles" if nm else "them") + " in Server Settings → Roles, and make sure it has **Manage Roles**.")
        if "nickname" in self.kinds:
            n = self.kinds["nickname"]["n"]
            out.append(f"I couldn't change {n} nickname{'s' if n != 1 else ''}. I need the **Manage Nicknames** permission and my role must be above "
                       "the member's highest role. Discord never lets a bot rename the server owner.")
        if "other" in self.kinds:
            d = self.kinds["other"]
            out.append(f"{d['n']} other error{'s' if d['n'] != 1 else ''} from Discord, for example: `{d['sample'][:200]}`")
        return out



def roblox_init(m):
    """m is the bot's main module (bot, embedDB, APIBaseURL, app...)."""
    bot = m.bot
    stateDB = m.rverifyDB                          # guild id -> {"roles": {roblox role id: discord role id}, "made": [...], "last": {...}}

    token = APIBaseURL_Key
    headers = {"Authorization": f"Bearer {token}"}
    http = requests.Session()

    def api_sync(method, path, body=None):
        """The Shapes API's JSON, or None when it can't be reached. Blocking: call through api()."""
        if not token:
            return None
        try:
            r = http.request(method, f"{m.APIBaseURL}{path}", json=body, headers=headers, timeout=30)
            data = r.json()
            return data if isinstance(data, dict) else None
        except (requests.RequestException, ValueError):
            return None

    async def api(method, path, body=None):
        return await asyncio.to_thread(api_sync, method, path, body)

    def color_of(user_id):
        saved = m.embedDB.get(f"{user_id}")
        return saved if saved else discord.Color.blue()

    # ------------------------------------------------------------------ helpers
    async def cfg_of(gid):
        d = await api("GET", f"/internal/guild/{PLATFORM}/{gid}/config")
        c = (d or {}).get("config") or {}
        return c if c.get("group_id") else None

    def template(cfg, acct, role_name):
        t = (cfg.get("nickname_template") or "").strip()
        if not t:
            return None
        out = (t.replace("{name}", acct.get("name") or "").replace("{display}", acct.get("display_name") or acct.get("name") or "")
                .replace("{id}", str(acct.get("id"))).replace("{rank}", role_name or ""))
        return out.strip(" |-–—·/:")[:32] if not role_name else out.strip()[:32]

    async def group_role(cfg, rid):
        d = await api("GET", f"/internal/roblox/group/{cfg['group_id']}/member/{rid}")
        return (d or {}).get("role") if d and d.get("success") else False       # False = Roblox didn't answer

    async def mirror_roles(guild, cfg, report=None, issues=None):
        """Make the Discord roles match the group's roles when mirroring is on: create missing ones, rename changed ones, delete
        ones the group dropped (only roles this bot made, never one that was already there). Returns {roblox role id: discord
        role id}; `report` (a dict) gets counts of what changed."""
        gid = str(guild.id)
        report = report if report is not None else {}
        issues = issues if issues is not None else Issues()
        for k in ("created", "renamed", "deleted"):
            report.setdefault(k, 0)
        st = dict(stateDB.get(gid) or {})
        mapping = dict(st.get("roles") or {})
        made = set(st.get("made") or [])
        d = await api("GET", f"/internal/roblox/group/{cfg['group_id']}/roles")
        if not d or not d.get("success"):
            return mapping
        group = {str(r["id"]): r for r in d["roles"] if int(r.get("rank") or 0) > 0}      # rank 0 is "Guest" = not in the group
        have = {str(r.id): r for r in guild.roles}
        by_name = {r.name.lower(): str(r.id) for r in guild.roles if not r.is_default() and not r.managed}
        mirror = bool(cfg.get("mirror_roles"))
        for rid, r in group.items():
            current = mapping.get(rid)
            if current and current in have:
                if mirror and have[current].name != r["name"]:       # renamed in the group
                    try:
                        await have[current].edit(name=r["name"], reason="Roblox group role renamed")
                        report["renamed"] += 1
                    except discord.HTTPException as e:
                        issues.add("edit-role", e, have[current].name)
                        if Issues.denied(e):
                            mirror = False                           # no point asking again for every other role
                continue
            if r["name"].lower() in by_name:                         # a role with that name already exists: use it
                mapping[rid] = by_name[r["name"].lower()]
            elif mirror:
                try:
                    new = await guild.create_role(name=r["name"], reason="Mirroring the Roblox group's roles")
                    mapping[rid] = str(new.id)
                    made.add(str(new.id))
                    have[str(new.id)] = new
                    report["created"] += 1
                except discord.HTTPException as e:
                    issues.add("create-role", e, r["name"])
                    if Issues.denied(e):
                        mirror = False                               # missing Manage Roles: stop here, the rest would fail the same way
        for rid in [k for k in mapping if k not in group]:           # the group dropped this role
            yid = mapping[rid]
            if mirror and yid in made and yid in have:
                try:
                    await have[yid].delete(reason="The Roblox group no longer has this role")
                    report["deleted"] += 1
                except discord.HTTPException as e:
                    issues.add("delete-role", e, issues.name(yid))
                    continue                                         # keep it in the map so the next sync tries again
            made.discard(yid)
            mapping.pop(rid, None)
        st["roles"], st["made"], st["at"] = mapping, sorted(made & {str(x) for x in mapping.values()}), time.time()
        stateDB.set(gid, st)
        stateDB.save()
        return mapping

    async def mapping_for(guild, cfg):
        """The saved role map; a fresh mirror only when it is older than ten minutes (so a busy panel doesn't hammer Roblox)."""
        st = stateDB.get(str(guild.id)) or {}
        if st.get("roles") and time.time() - float(st.get("at") or 0) < 600:
            return dict(st["roles"])
        return await mirror_roles(guild, cfg)

    async def ensure_verified_role(guild, cfg, issues=None):
        if cfg.get("verified_role_id") == "create":
            try:
                role = await guild.create_role(name="Verified", reason="Roblox verification")
                await api("PUT", f"/internal/guild/{PLATFORM}/{guild.id}/config", {"verified_role_id": str(role.id)})
                cfg["verified_role_id"] = str(role.id)
            except discord.HTTPException as e:
                if issues is not None:
                    issues.add("create-role", e, "Verified")
                cfg["verified_role_id"] = ""
        return cfg.get("verified_role_id") or None

    async def sync_member(guild, member, acct, cfg, mapping, issues):
        """Bring one member in line: roles from their group rank, the verified role, the nickname. acct=None unverifies."""
        mine = {str(r.id) for r in member.roles}
        want, drop = set(), set(mapping.values())
        vr, ur = cfg.get("verified_role_id") or None, cfg.get("unverified_role_id") or None
        role_name = None
        if acct:
            if vr and vr != "create":
                want.add(vr)
            role = await group_role(cfg, acct["id"])
            if role is False:
                return "roblox-unreachable"
            if role and str(role.get("id")) in mapping and int(role.get("rank") or 0) > 0:
                want.add(mapping[str(role["id"])])
                role_name = role.get("name")
        elif ur:
            want.add(ur)
        drop |= {x for x in (vr, ur) if x and x != "create"}
        roles = {str(r.id): r for r in guild.roles}
        for rid in (want - mine) - issues.blocked:
            if rid not in roles:
                continue
            try:
                await member.add_roles(roles[rid], reason="Roblox verification")
            except discord.HTTPException as e:
                if issues.add("give-role", e, issues.name(rid)):
                    issues.blocked.add(rid)                          # refused once for this role: skip it for everyone else
        for rid in ((drop - want) & mine) - issues.blocked:
            if rid not in roles:
                continue
            try:
                await member.remove_roles(roles[rid], reason="Roblox verification")
            except discord.HTTPException as e:
                if issues.add("take-role", e, issues.name(rid)):
                    issues.blocked.add(rid)
        if acct:
            nick = template(cfg, acct, role_name)
            if nick and member.nick != nick and member.id != member.guild.owner_id and not (issues.nick_denied >= 5 and not issues.nick_ok):
                try:
                    await member.edit(nick=nick, reason="Roblox verification")
                    issues.nick_ok += 1
                except discord.HTTPException as e:
                    if issues.add("nickname", e):
                        issues.nick_denied += 1
        return "ok"

    async def dm_verified(user_id, acct, how, guild_name=None):
        """The message every verification gets: avatar, username, user ID and a profile link."""
        embed = discord.Embed(title=f"✅ {'Auto-verified' if how == 'auto' else 'Verified'} as {acct['name']}", color=discord.Color.green(),
                              description=f"[{acct['name']}]({acct['profile']})\nUser ID: `{acct['id']}`" + (f"\nServer: **{guild_name}**" if guild_name else ""))
        if acct.get("avatar"):
            embed.set_thumbnail(url=acct["avatar"])
        embed.set_footer(text="Manage your accounts with /roblox accounts")
        try:
            user = bot.get_user(int(user_id)) or await bot.fetch_user(int(user_id))
            await user.send(embed=embed)
        except (discord.HTTPException, ValueError) as e:
            print(f"[verify] couldn't DM {user_id}: {e}")

    async def remember(gid, counts, issues):
        """Keep the last sync's result and problems so the Yapin' dashboard (or /roblox panel status) can show them."""
        st = dict(stateDB.get(gid) or {})
        st["last"] = {"at": int(time.time()), "counts": counts, "problems": issues.lines()}
        stateDB.set(gid, st)
        stateDB.save()

    async def apply_one(guild, member, quiet=False):
        """Verify/auto-verify one member of a configured server, then sync them. Returns the account or None."""
        gid = str(guild.id)
        cfg = await cfg_of(gid)
        if not cfg:
            return None
        res = ((await api("POST", f"/internal/guild/{PLATFORM}/{gid}/resolve", {"user_ids": [str(member.id)]})) or {}).get("members", {}).get(str(member.id))
        if not res:
            return None
        issues = Issues({str(r.id): r.name for r in guild.roles})
        await ensure_verified_role(guild, cfg, issues)
        await sync_member(guild, member, res, cfg, await mapping_for(guild, cfg), issues)
        if issues.kinds:
            print(f"[verify] {member.id} in {gid}: " + " | ".join(issues.lines()))
            await remember(gid, {"single": True}, issues)
        if not quiet:
            await dm_verified(member.id, res, res.get("how"), guild.name)
        return res

    # ------------------------------------------------------------------ full sync
    async def sync_guild(guild):
        """Sync every member of a server. One at a time per server. Returns counts."""
        gid = str(guild.id)
        lock = _locks.setdefault(gid, asyncio.Lock())
        if lock.locked():
            return {"busy": True}
        async with lock:
            counts = {"synced": 0, "unverified": 0, "skipped": 0, "errors": 0, "created": 0, "renamed": 0, "deleted": 0}
            cfg = await cfg_of(gid)
            if not cfg:
                return {"error": "no group set"}
            issues = Issues({str(r.id): r.name for r in guild.roles})
            await ensure_verified_role(guild, cfg, issues)
            mapping = await mirror_roles(guild, cfg, counts, issues)
            members = [mem async for mem in guild.fetch_members(limit=None) if not mem.bot]
            found = {}
            ids = [str(mem.id) for mem in members]
            for i in range(0, len(ids), 500):
                found.update(((await api("POST", f"/internal/guild/{PLATFORM}/{gid}/resolve", {"user_ids": ids[i:i + 500]})) or {}).get("members", {}))
            for mem in members:
                try:
                    r = await sync_member(guild, mem, found.get(str(mem.id)), cfg, mapping, issues)
                    counts["synced" if found.get(str(mem.id)) and r == "ok" else "unverified" if r == "ok" else "skipped"] += 1
                except Exception as e:
                    counts["errors"] += 1
                    print(f"[verify] sync {mem.id} in {gid}: {e}")
                await asyncio.sleep(0.3)
            await api("POST", f"/internal/guild/{PLATFORM}/{gid}/synced", {})
            counts["problems"] = issues.lines()
            await remember(gid, {k: v for k, v in counts.items() if k != "problems"}, issues)
            return counts

    def summary_text(counts):
        if counts.get("busy"):
            return "A sync is already running here."
        if "synced" not in counts:
            return f"Sync failed: {counts.get('error')}"
        text = (f"Sync finished: {counts['synced']} verified members updated, {counts['unverified']} not verified, "
                f"{counts['skipped']} skipped, {counts['errors']} errors.")
        if counts["created"] or counts["renamed"] or counts["deleted"]:
            text += f" Roles: {counts['created']} created, {counts['renamed']} renamed, {counts['deleted']} deleted."
        if counts.get("problems"):
            text += "\n\n**Things that need fixing:**\n" + "\n".join(f"• {l}" for l in counts["problems"])
        return text

    async def scheduler():
        """Backup sync: a server is due when 24 hours of real time have passed since its last sync (UTC epoch seconds)."""
        while True:
            await asyncio.sleep(CHECK_EVERY)
            try:
                for cfg in ((await api("GET", f"/internal/guilds/{PLATFORM}")) or {}).get("guilds", []):
                    if time.time() - int(cfg.get("last_sync_at") or 0) >= SYNC_EVERY:
                        guild = bot.get_guild(int(cfg["guild_id"]))
                        if guild:
                            await sync_guild(guild)
            except Exception as e:
                print(f"[verify] scheduler: {e}")

    started = {"scheduler": False}

    async def on_ready_start():
        if not started["scheduler"]:
            started["scheduler"] = True
            bot.loop.create_task(scheduler())
    bot.add_listener(on_ready_start, "on_ready")

    # ------------------------------------------------------------------ joins and the API's notice
    async def start_link(uid, gid=""):
        """(url, message): a one-time Roblox sign-in link, or why there isn't one."""
        d = await api("POST", "/internal/roblox/start", {"platform": PLATFORM, "user_id": str(uid), "guild": f"{PLATFORM}:{gid}" if gid else ""})
        if d and d.get("success") and d.get("url"):
            return d["url"], None
        if d and d.get("data"):
            return None, d["data"]
        return None, "I couldn't make a Roblox sign-in link right now."

    def yapin_note():
        return (f"You don't need a Yapin' account. If you have one, open {YAPIN_DASH} and press **Link Discord** to merge them, "
                "and your accounts show up in both places.")

    async def on_member_join(member):
        """A member joined: auto-verify them if they chose that, else DM the server's prompt (once a day per server)."""
        if member.bot:
            return
        gid, uid = str(member.guild.id), str(member.id)
        cfg = await cfg_of(gid)
        if not cfg:
            return
        if await apply_one(member.guild, member):
            return
        if cfg.get("prompt_dm") and time.time() - _prompted.get((gid, uid), 0) > 86400:
            _prompted[(gid, uid)] = time.time()
            link, why = await start_link(uid, gid)
            text = cfg.get("prompt_text") or f"**{member.guild.name}** uses Roblox verification."
            body = f"{text}\n\nSign in with Roblox here (no bio changes needed): {link}" if link else f"{text}\n\nRun `/roblox verify` in the server to get started."
            try:
                await member.send(embed=discord.Embed(title="Verify your Roblox account", description=body, color=discord.Color.blurple()))
            except discord.HTTPException as e:
                print(f"[verify] couldn't DM the prompt to {uid}: {e}")
    bot.add_listener(on_member_join, "on_member_join")

    def verified_notice():
        if not token or not hmac.compare_digest(request.headers.get("Authorization", "").removeprefix("Bearer ").strip(), token):
            return jsonify({"success": False}), 401
        b = request.get_json(silent=True) or {}
        uid = str(b.get("discord_id") or "")
        if not uid:
            return jsonify({"success": True, "ignored": True})
        acct = {"id": b.get("roblox_id"), "name": b.get("roblox_name"), "avatar": b.get("avatar"), "profile": b.get("profile")}

        async def work():
            await dm_verified(uid, acct, "auto" if b.get("auto_verify") else "verified")
            guild_hint = str(b.get("guild") or "")
            if guild_hint.startswith(f"{PLATFORM}:"):               # they came from a server's prompt: verify them there too
                await api("POST", f"/internal/guild/{PLATFORM}/{guild_hint.split(':', 1)[1]}/verify", {"user_id": uid, "roblox_id": acct["id"]})
            for cfg in ((await api("GET", f"/internal/guilds/{PLATFORM}")) or {}).get("guilds", []):      # keep every server they're in current
                guild = bot.get_guild(int(cfg["guild_id"]))
                member = guild.get_member(int(uid)) if guild else None
                if member:
                    await apply_one(guild, member, quiet=True)
        asyncio.run_coroutine_threadsafe(work(), bot.loop)
        return jsonify({"success": True})

    # ------------------------------------------------------------------ the panel (a message with a Verify button)
    def panel_embed(cfg, guild):
        title = (cfg.get("panel_title") or "").strip() or "Verify with Roblox"
        text = (cfg.get("panel_text") or "").strip() or (
            f"Press **Verify** to link your Roblox account to {guild.name}. You sign in with Roblox "
            "(nothing goes in your bio) and I'll give you your roles and nickname.\n"
            "Already linked? You're verified in one click. Pick another account with `/roblox verify`.")
        return discord.Embed(title=title[:256], description=text[:3000], color=discord.Color.blurple())

    async def send_panel(guild, channel, cfg):
        """Post the panel (or update the one posted before when it is in the same channel)."""
        embed = panel_embed(cfg, guild)
        old_ch, old_msg = cfg.get("panel_channel_id"), cfg.get("panel_message_id")
        if old_msg and str(old_ch) == str(channel.id):
            try:
                msg = await channel.fetch_message(int(old_msg))
                await msg.edit(embed=embed, view=PanelView())
                return msg
            except discord.HTTPException:
                pass                                                 # deleted in the meantime: post a new one
        msg = await channel.send(embed=embed, view=PanelView())
        await api("PUT", f"/internal/guild/{PLATFORM}/{guild.id}/config", {"panel_channel_id": str(channel.id), "panel_message_id": str(msg.id)})
        return msg

    class PanelView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=None)

        @discord.ui.button(label="Verify", style=discord.ButtonStyle.primary, custom_id="rv:verify")
        async def verify(self, interaction: discord.Interaction, button: discord.ui.Button):
            uid, guild = str(interaction.user.id), interaction.guild
            if not guild:
                return await interaction.response.send_message("Press the button in a server.", ephemeral=True)
            if time.time() - _clicked.get(uid, 0) < 5:
                return await interaction.response.send_message("One moment, I'm still working on your last click.", ephemeral=True)
            _clicked[uid] = time.time()
            if len(_clicked) > 2000:
                _clicked.clear()
            await interaction.response.defer(ephemeral=True)
            cfg = await cfg_of(str(guild.id))
            if not cfg:
                return await interaction.followup.send("This server hasn't finished setting up Roblox verification yet.", ephemeral=True)
            res = await api("POST", f"/internal/guild/{PLATFORM}/{guild.id}/verify", {"user_id": uid, "roblox_id": None})
            if not res or not res.get("success"):
                if res is None:
                    return await interaction.followup.send("Verification isn't available right now. Try again in a moment.", ephemeral=True)
                link, why = await start_link(uid, str(guild.id))
                if not link:
                    return await interaction.followup.send(why or "Couldn't verify you.", ephemeral=True)
                return await interaction.followup.send(f"Sign in with Roblox to verify (OAuth2, nothing goes in your bio). The link works for 10 minutes and only for you:\n{link}\n"
                                                       "When you finish I'll verify you here automatically.", ephemeral=True)
            acct = res.get("account") or {}
            who = f"[{acct.get('name')}]({acct.get('profile')}) (`{acct.get('id')}`)" if acct.get("id") else "your Roblox account"
            await interaction.followup.send(f"Verified as {who}. I'm updating your roles and nickname now.", ephemeral=True)
            member = interaction.user if isinstance(interaction.user, discord.Member) else guild.get_member(interaction.user.id)
            if member:
                await apply_one(guild, member)

    bot.add_view(PanelView())


    # ------------------------------------------------------------------ the /roblox command group
    def is_manager(interaction):
        user = interaction.user
        return bool(interaction.guild) and (user.id == interaction.guild.owner_id or user.guild_permissions.administrator or user.guild_permissions.manage_guild)

    def split_names(value):
        return [t for t in (value or "").replace(",", " ").split() if t]

    roblox, panel = roblox_group, roblox_panel

    async def say(interaction, text, **kw):
        return await interaction.response.send_message(text, ephemeral=True, **kw)

    async def my_person(interaction):
        """(accounts list, error text). The API makes the person record on first contact, so no Yapin' account is needed."""
        person = await api("GET", f"/internal/person/{PLATFORM}/{interaction.user.id}") or {}
        if not person.get("success"):
            return None, "Verification isn't available right now. Try again in a moment."
        return person.get("accounts") or [], None

    def pick(accts, q):
        return next((x for x in accts if str(x["id"]) == q or x["name"].lower() == q.lower()), None)

    @roblox.command(name="verify", description="Verify in this server (as your default account, or the one you name)")
    @app_commands.describe(account="A linked account's name or id (empty = your default)")
    async def roblox_verify(interaction: discord.Interaction, account: str = ""):
        uid, gid = str(interaction.user.id), str(interaction.guild_id or "")
        if not interaction.guild:
            return await say(interaction, "Run this in a server to verify there. To link an account from here, use `/roblox link`.")
        if not await cfg_of(gid):
            return await say(interaction, "This server hasn't set up Roblox verification.")
        accts, err = await my_person(interaction)
        if err:
            return await say(interaction, err)
        if not accts:
            link, why = await start_link(uid, gid)
            return await say(interaction, f"Sign in with Roblox to verify (OAuth2, nothing goes in your bio). The link works for 10 minutes and only for you:\n{link}\n"
                                          "When you finish I'll verify you here automatically." if link else why)
        res = await api("POST", f"/internal/guild/{PLATFORM}/{gid}/verify", {"user_id": uid, "roblox_id": account or None})
        if not res or not res.get("success"):
            return await say(interaction, (res or {}).get("data") or "Couldn't verify you.")
        await interaction.response.defer(ephemeral=True)
        member = interaction.user if isinstance(interaction.user, discord.Member) else interaction.guild.get_member(interaction.user.id)
        if member:
            await apply_one(interaction.guild, member)
        await interaction.followup.send(f"Verified as **{res['account']['name']}**. I've sent you a message with the details.", ephemeral=True)

    @roblox.command(name="link", description="Link a Roblox account by signing in with Roblox (no Yapin' account needed)")
    async def roblox_link(interaction: discord.Interaction):
        link, why = await start_link(interaction.user.id, interaction.guild_id or "")
        if not link:
            return await say(interaction, why)
        view = discord.ui.View(timeout=600)
        view.add_item(discord.ui.Button(label="Link with Roblox", style=discord.ButtonStyle.link, url=link))
        await say(interaction, "Sign in with Roblox (OAuth2, nothing goes in your bio). The link works for 10 minutes and only for you.\n" + yapin_note(), view=view)

    @roblox.command(name="accounts", description="List the Roblox accounts you've linked")
    async def roblox_accounts(interaction: discord.Interaction):
        accts, err = await my_person(interaction)
        if err:
            return await say(interaction, err)
        if not accts:
            return await say(interaction, "You haven't linked a Roblox account yet. Use `/roblox link`.")
        lines = [f"{'⭐ ' if x['auto_verify'] else ''}{'🙈 ' if x.get('hidden') else ''}[{x['name']}]({x['profile']}) · `{x['id']}`" for x in accts]
        await say(interaction, f"**Your Roblox accounts ({len(accts)}/100)**\n" + "\n".join(lines[:40]) + ("\n…" if len(lines) > 40 else "")
                  + "\n⭐ = auto-verify · 🙈 = hidden from lookups and the API. Add more with `/roblox link`.")

    @roblox.command(name="auto", description="Verify you automatically as one account in servers that use Roblox verification")
    @app_commands.describe(account="A linked account's name or id, or off")
    async def roblox_auto(interaction: discord.Interaction, account: str = "off"):
        uid = str(interaction.user.id)
        if account.lower() in ("off", "none", ""):
            await api("POST", f"/internal/person/{PLATFORM}/{uid}/auto", {"roblox_id": None})
            return await say(interaction, "Auto-verify is off.")
        accts, err = await my_person(interaction)
        x = pick(accts or [], account)
        if err or not x:
            return await say(interaction, err or "That isn't one of your linked accounts. See `/roblox accounts`.")
        await api("POST", f"/internal/person/{PLATFORM}/{uid}/auto", {"roblox_id": x["id"]})
        await say(interaction, f"**{x['name']}** will now verify you automatically in servers that use Roblox verification.")

    @roblox.command(name="remove", description="Unlink one of your Roblox accounts")
    @app_commands.describe(account="A linked account's name or id")
    async def roblox_remove(interaction: discord.Interaction, account: str):
        accts, err = await my_person(interaction)
        x = pick(accts or [], account)
        if err or not x:
            return await say(interaction, err or "That isn't one of your linked accounts. See `/roblox accounts`.")
        await api("DELETE", f"/internal/person/{PLATFORM}/{interaction.user.id}/roblox/{x['id']}")
        await say(interaction, f"Removed **{x['name']}**.")

    async def set_hidden(interaction, accounts, hide):
        names = split_names(accounts)
        verb = "hide" if hide else "show"
        if not names:
            return await say(interaction, f"Say which accounts to {verb}: one name, several names, or `all`. See `/roblox accounts`.")
        accts, err = await my_person(interaction)
        if err:
            return await say(interaction, err)
        body = {"hidden": hide}
        if any(n.lower() == "all" for n in names):
            body["all"] = True
            shown = "all your accounts"
        else:
            chosen = [pick(accts, n) for n in names]
            missing = [n for n, x in zip(names, chosen) if not x]
            if missing:
                return await say(interaction, f"Not one of your linked accounts: {', '.join(missing)}. See `/roblox accounts`.")
            body["roblox_ids"] = [x["id"] for x in chosen]
            shown = ", ".join(f"**{x['name']}**" for x in chosen)
        d = await api("POST", f"/internal/person/{PLATFORM}/{interaction.user.id}/hide", body)
        if not d or not d.get("success"):
            return await say(interaction, "Couldn't change that right now. Try again in a moment.")
        await say(interaction, f"Hidden: {shown}. They no longer show in lookups or the API, but servers still see them when you verify." if hide else f"Visible again: {shown}.")

    @roblox.command(name="hide", description="Hide accounts from lookups and the API (one, several, or all)")
    @app_commands.describe(accounts="Account names or ids, several separated by spaces, or all")
    async def roblox_hide(interaction: discord.Interaction, accounts: str):
        await set_hidden(interaction, accounts, True)

    @roblox.command(name="show", description="Show hidden accounts again (one, several, or all)")
    @app_commands.describe(accounts="Account names or ids, several separated by spaces, or all")
    async def roblox_show(interaction: discord.Interaction, accounts: str):
        await set_hidden(interaction, accounts, False)

    @roblox.command(name="group", description="Info about a Roblox group (ID, link or name)")
    @app_commands.describe(group="The group's ID, link or name")
    async def roblox_group_info(interaction: discord.Interaction, group: str):
        await interaction.response.defer(thinking=True)
        info, err = await fetch_group(group)
        if err:
            return await interaction.followup.send(err)
        await interaction.followup.send(embed=group_embed(info, color_of(interaction.user.id)))

    @roblox.command(name="sync", description="Sync every member's Roblox roles and nickname with the group (server admins)")
    async def roblox_sync(interaction: discord.Interaction):
        if not is_manager(interaction):
            return await say(interaction, "Only server admins can run a sync.")
        if not await cfg_of(str(interaction.guild_id)):
            return await say(interaction, "Set the group first: `/roblox panel setgroup`.")
        await say(interaction, "Syncing every member now. This can take a few minutes; I'll send you the result, including anything I'm not allowed to do "
                               "(missing permissions or a role that sits too high).")
        guild, user = interaction.guild, interaction.user

        async def job():
            text = summary_text(await sync_guild(guild))
            try:
                await user.send(text[:1900])
            except discord.HTTPException:
                pass
        task = asyncio.create_task(job())
        _tasks.add(task)                                 # keep a reference so it isn't collected mid-sync
        task.add_done_callback(_tasks.discard)

    # ---- /roblox panel ... (server admins)
    async def stats_line(gid):
        d = await api("GET", f"/internal/guild/{PLATFORM}/{gid}/stats")
        if not d or not d.get("success"):
            return None
        return f"Verified here: **{d['verified']}** now · **{d['ever_verified']}** ever · last 24h {d['last_24h']} · 7 days {d['last_7d']} · 30 days {d['last_30d']}"

    async def panel_apply(interaction, body=None):
        """Save a change (or just read the config) and answer with the server's current setup."""
        if not is_manager(interaction):
            return await say(interaction, "Only server admins can set up verification.")
        guild, gid = interaction.guild, str(interaction.guild_id)
        d = await api("PUT" if body else "GET", f"/internal/guild/{PLATFORM}/{gid}/config", body or None)
        c = (d or {}).get("config") or {}
        if (body or {}).get("verified_role_id") == "create":
            await ensure_verified_role(guild, c, Issues())
        lines = [f"Group: `{c.get('group_id') or 'not set'}`", f"Nickname: `{c.get('nickname_template') or 'off'}` (use {{name}}, {{display}}, {{id}}, {{rank}})",
                 f"Verified role: {'<@&' + str(c['verified_role_id']) + '>' if c.get('verified_role_id') and c.get('verified_role_id') != 'create' else 'none'} · "
                 f"Unverified role: {'<@&' + str(c['unverified_role_id']) + '>' if c.get('unverified_role_id') else 'none'}",
                 f"Mirror group roles: {'on' if c.get('mirror_roles') else 'off'} · DM prompt on join: {'on' if c.get('prompt_dm') else 'off'}",
                 f"Panel: {'posted in <#' + str(c['panel_channel_id']) + '>' if c.get('panel_message_id') else 'not posted. Use `/roblox panel send`'}"]
        last = (stateDB.get(gid) or {}).get("last")
        if last and last.get("problems"):
            lines.append("**The last sync hit problems:**\n" + "\n".join(f"• {l}" for l in last["problems"]))
        line = await stats_line(gid) if c.get("group_id") else None
        if line:
            lines.append(line)
        lines.append("Run `/roblox sync` to apply it to everyone. A backup sync runs every 24 hours.")
        await say(interaction, "\n".join(lines))

    ONOFF = [app_commands.Choice(name="on", value="on"), app_commands.Choice(name="off", value="off")]

    @panel.command(name="status", description="Show this server's Roblox verification setup")
    async def panel_status(interaction: discord.Interaction):
        await panel_apply(interaction)

    @panel.command(name="setgroup", description="Set the Roblox group this server mirrors")
    @app_commands.describe(group_id="The number in the group's URL")
    async def panel_setgroup(interaction: discord.Interaction, group_id: str):
        if not group_id.strip().isdigit():
            return await say(interaction, "Give the Roblox group ID (the number in the group's URL).")
        await panel_apply(interaction, {"group_id": group_id.strip()})

    @panel.command(name="nickname", description="Set the nickname template, or off")
    @app_commands.describe(template="Use {name}, {display}, {id}, {rank}; or off")
    async def panel_nickname(interaction: discord.Interaction, template: str):
        v = template.strip()
        await panel_apply(interaction, {"nickname_template": "" if v.lower() in ("off", "none", "") else "{name}" if v.lower() in ("on", "true", "yes") else v})

    @panel.command(name="role", description="Set the verified role (pick one, or leave empty to create it)")
    async def panel_role(interaction: discord.Interaction, role: discord.Role = None):
        await panel_apply(interaction, {"verified_role_id": "create" if role is None else str(role.id)})

    @panel.command(name="unverified", description="Set the role people get until they verify (pick one, or leave empty to create it)")
    async def panel_unverified(interaction: discord.Interaction, role: discord.Role = None):
        await panel_apply(interaction, {"unverified_role_id": "create" if role is None else str(role.id)})

    @panel.command(name="mirror", description="Make Discord roles follow the group's roles (create, rename, delete)")
    @app_commands.choices(state=ONOFF)
    async def panel_mirror(interaction: discord.Interaction, state: app_commands.Choice[str]):
        await panel_apply(interaction, {"mirror_roles": state.value == "on"})

    @panel.command(name="prompt", description="DM new members a Roblox verification prompt when they join")
    @app_commands.choices(state=ONOFF)
    @app_commands.describe(text="Optional message to put in the DM")
    async def panel_prompt(interaction: discord.Interaction, state: app_commands.Choice[str], text: str = ""):
        body = {"prompt_dm": state.value == "on"}
        if text.strip():
            body["prompt_text"] = text.strip()
        await panel_apply(interaction, body)

    @panel.command(name="title", description="Set the panel's title (empty = default)")
    async def panel_title(interaction: discord.Interaction, title: str = ""):
        await panel_apply(interaction, {"panel_title": "" if title.lower() in ("off", "default") else title.strip()})

    @panel.command(name="text", description="Set the panel's text (empty = default)")
    async def panel_text(interaction: discord.Interaction, text: str = ""):
        await panel_apply(interaction, {"panel_text": "" if text.lower() in ("off", "default") else text.strip()})

    @panel.command(name="stats", description="How many people verified here")
    async def panel_stats(interaction: discord.Interaction):
        if not is_manager(interaction):
            return await say(interaction, "Only server admins can see verification stats.")
        await say(interaction, (await stats_line(str(interaction.guild_id))) or "Stats aren't available right now.")

    @panel.command(name="send", description="Post the verification panel (a message with a Verify button)")
    @app_commands.describe(channel="Where to post it (default: here)")
    async def panel_send(interaction: discord.Interaction, channel: discord.TextChannel = None):
        if not is_manager(interaction):
            return await say(interaction, "Only server admins can set up verification.")
        cfg = await cfg_of(str(interaction.guild_id))
        if not cfg:
            return await say(interaction, "Set the group first: `/roblox panel setgroup`.")
        target = channel or interaction.channel
        try:
            await send_panel(interaction.guild, target, cfg)
        except discord.HTTPException as e:
            why = ("I'm missing **View Channel**, **Send Messages** or **Embed Links** there. Give my role those permissions in that channel."
                   if Issues.denied(e) else str(e))
            return await say(interaction, f"I couldn't post the panel in {target.mention}: {why}")
        await say(interaction, f"The verification panel is posted in {target.mention}. People press **Verify** to get their roles.")

    m.rverify_sync = sync_guild
    _ROBLOX["notice"] = verified_notice
    return roblox


def cmdlog_init(m):
    bot = m.bot
    cfgDB = m.cmdlogDB                               # guild id (or "botlog") -> {"channel", "enabled", "events", "commands", "retry_at", "refused"}
    hooks = {}                                       # channel id -> discord.Webhook, shared by every log posting there
    lock = asyncio.Lock()

    async def hook_for(channel):
        """A webhook the bot can use in this channel: one it already made (shared by the logs), else a new one."""
        if channel.id in hooks:
            return hooks[channel.id]
        mine = [w for w in await channel.webhooks() if w.token and (w.user == bot.user or w.application_id == bot.user.id)]
        mine.sort(key=lambda w: not (w.name or "").startswith("Shapes"))
        hook = mine[0] if mine else await channel.create_webhook(name=HOOK_NAME, reason="Command log")
        hooks[channel.id] = hook
        return hook

    async def post(key, embed):
        """Post one embed to a log's channel. Returns None, or a plain sentence about what the bot isn't allowed to do."""
        cfg = cfgDB.get(key)
        if not cfg or not cfg.get("enabled") or not cfg.get("channel"):
            return None
        channel = bot.get_channel(int(cfg["channel"]))
        if channel is None:
            return None
        async with lock:
            if not (cfg.get("refused") and time.time() < float(cfg.get("retry_at") or 0)):
                try:
                    hook = await hook_for(channel)
                    await hook.send(embed=embed, username=bot.user.name, avatar_url=bot.user.display_avatar.url)
                    if cfg.pop("refused", None):
                        cfgDB.set(key, cfg)
                    return None
                except discord.NotFound:                               # the webhook was deleted: forget it and try a fresh one once
                    hooks.pop(channel.id, None)
                    try:
                        hook = await hook_for(channel)
                        await hook.send(embed=embed, username=bot.user.name, avatar_url=bot.user.display_avatar.url)
                        return None
                    except discord.HTTPException:
                        pass
                except discord.Forbidden:
                    hooks.pop(channel.id, None)
                    cfg.update(refused=True, retry_at=time.time() + RETRY_WEBHOOK_AFTER)
                    cfgDB.set(key, cfg)
                except discord.HTTPException as e:
                    print(f"[cmdlog] webhook in {channel.id}: {e}")
            try:                                                       # the webhook isn't available: post as the bot
                await channel.send(embed=embed)
                return "I can't use webhooks in that channel, so I'm posting as the bot. Give me **Manage Webhooks** there to use one."
            except discord.HTTPException as e:
                print(f"[cmdlog] couldn't post in {channel.id}: {e}")
                return "I can't post in that channel. I need **View Channel**, **Send Messages** and **Embed Links** there."

    def command_embed(interaction, command):
        args = " ".join(f"{k}:{v}" for k, v in (interaction.namespace.__dict__.items() if interaction.namespace else []))[:200]
        embed = discord.Embed(title=f"/{command.qualified_name}", color=discord.Color.blurple(), timestamp=discord.utils.utcnow())
        embed.add_field(name="User", value=f"{interaction.user.mention} (@{interaction.user.name})", inline=True)
        if interaction.channel_id:
            embed.add_field(name="Channel", value=f"<#{interaction.channel_id}>", inline=True)
        if args:
            embed.add_field(name="Arguments", value="```\n" + args.replace("```", "'''") + "\n```", inline=False)
        if interaction.guild:
            embed.set_footer(text=f"{interaction.guild.name} ({interaction.guild.id})")
        return embed

    async def on_command(interaction, command):
        try:
            if interaction.guild_id:
                await post(str(interaction.guild_id), command_embed(interaction, command))
            g = cfgDB.get(BOTLOG_KEY)
            if g and g.get("commands", True):
                await post(BOTLOG_KEY, command_embed(interaction, command))
        except Exception as e:
            print(f"[cmdlog] {e}")
    bot.add_listener(on_command, "on_app_command_completion")

    async def server_event(kind, guild):
        g = cfgDB.get(BOTLOG_KEY)
        if not g or not g.get("events", True):
            return
        humans = sum(1 for mem in guild.members if not mem.bot) if guild.chunked else None
        total = guild.member_count or 0
        embed = discord.Embed(title=("➕ Shapes was added to " if kind == "add" else "➖ Shapes was removed from ") + guild.name,
                              color=discord.Color.green() if kind == "add" else discord.Color.red(), timestamp=discord.utils.utcnow())
        embed.add_field(name="Server", value=f"{guild.name}\n`{guild.id}`", inline=True)
        embed.add_field(name="Members", value=(f"**{total:,}** total\n{humans:,} people · {total - humans:,} bots" if humans is not None else f"**{total:,}** total (people and bots)"), inline=True)
        embed.add_field(name="Servers now", value=str(len(bot.guilds)), inline=True)
        embed.set_footer(text="Shapes server log")
        await post(BOTLOG_KEY, embed)

    async def on_guild_join(guild):
        await server_event("add", guild)

    async def on_guild_remove(guild):
        await server_event("remove", guild)
    bot.add_listener(on_guild_join, "on_guild_join")
    bot.add_listener(on_guild_remove, "on_guild_remove")

    def public_warning(channel):
        """A log shows who ran what, so say so when everyone can read the channel."""
        everyone = channel.guild.default_role
        return ("\n⚠️ Everyone in the server can see that channel, so they'll see who runs which command. Pick a private channel if that's not what you want."
                if channel.permissions_for(everyone).view_channel else "")

    STATE = [app_commands.Choice(name="on", value="on"), app_commands.Choice(name="off", value="off")]

    @bot.tree.command(name="cmdlog", description="Log every command used in this server to a channel (server admins)")
    @app_commands.describe(channel="Where to post the log", state="Turn the log on or off")
    @app_commands.choices(state=STATE)
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.allowed_installs(guilds=True, users=False)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
    async def cmdlog(interaction: discord.Interaction, channel: discord.TextChannel = None, state: app_commands.Choice[str] = None):
        user = interaction.user
        if not (user.id == interaction.guild.owner_id or user.guild_permissions.administrator or user.guild_permissions.manage_guild):
            return await interaction.response.send_message("Only server admins can change the command log.", ephemeral=True)
        key = str(interaction.guild_id)
        cfg = cfgDB.get(key) or {"enabled": False, "channel": None}
        note = ""
        if channel is not None:
            cfg["channel"] = str(channel.id)
            cfg.pop("refused", None)
            hooks.pop(channel.id, None)
            if state is None:
                cfg["enabled"] = True
            note = public_warning(channel)
        if state is not None:
            if state.value == "on" and not cfg.get("channel"):
                return await interaction.response.send_message("Pick a channel first: `/cmdlog channel:#logs`.", ephemeral=True)
            cfg["enabled"] = state.value == "on"
        cfgDB.set(key, cfg)
        where = f"<#{cfg['channel']}>" if cfg.get("channel") else "no channel yet"
        await interaction.response.send_message(f"The command log is **{'on' if cfg.get('enabled') else 'off'}** ({where}).{note}", ephemeral=True)

    @bot.tree.command(name="botlog", description="Bot owners only: log every command and every server added or removed")
    @app_commands.describe(channel="Where to post the global log", state="Turn the whole log on or off",
                           commands="Log commands from every server", events="Log servers added and removed",
                           dms="Post every DM the bot sends or receives")
    @app_commands.choices(state=STATE)
    @app_commands.allowed_installs(guilds=True, users=False)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
    async def botlog(interaction: discord.Interaction, channel: discord.TextChannel = None, state: app_commands.Choice[str] = None,
                     commands: bool = None, events: bool = None, dms: bool = None):
        if not m.is_bot_owner(interaction.user):
            return await interaction.response.send_message("Only the bot owners can use the bot log.", ephemeral=True)
        cfg = cfgDB.get(BOTLOG_KEY) or {"enabled": False, "channel": None, "commands": True, "events": True}
        if channel is not None:
            cfg["channel"] = str(channel.id)
            cfg.pop("refused", None)
            hooks.pop(channel.id, None)
            if state is None:
                cfg["enabled"] = True
        if state is not None:
            if state.value == "on" and not cfg.get("channel"):
                return await interaction.response.send_message("Pick a channel first: `/botlog channel:#logs`.", ephemeral=True)
            cfg["enabled"] = state.value == "on"
        if commands is not None:
            cfg["commands"] = commands
        if events is not None:
            cfg["events"] = events
        if dms is not None:
            cfg["dms"] = dms
        cfgDB.set(BOTLOG_KEY, cfg)
        await interaction.response.send_message(
            f"The bot log is **{'on' if cfg.get('enabled') else 'off'}** (" + (f"<#{cfg['channel']}>" if cfg.get("channel") else "no channel yet") +
            f"). Commands: {'on' if cfg.get('commands', True) else 'off'} · Server events: {'on' if cfg.get('events', True) else 'off'} · "
            f"DMs: {'on' if cfg.get('dms', True) else 'off'}.", ephemeral=True)

    m.cmdlog_post = post


# --- Roblox group info (used by /roblox group) ---
GROUP_UA = {"user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
            "accept": "application/json"}
GROUP_URL = re.compile(r"roblox\.com/(?:[a-z-]+/)?(?:groups|communities)/(\d+)", re.I)


async def _group_json(session, url):
    try:
        async with session.get(url, headers=GROUP_UA, timeout=aiohttp.ClientTimeout(total=12)) as r:
            data = await r.json(content_type=None)
            return (data if r.status < 400 else None), r.status
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return None, 0


async def find_group_id(session, query):
    """(group id, error text). Accepts an ID, a group link, or a name (the best match wins, an exact name first)."""
    q = (query or "").strip()
    if q.isdigit():
        return q, None
    found = GROUP_URL.search(q)
    if found:
        return found.group(1), None
    if not q:
        return None, "Give a group ID, a roblox.com group link, or a group name."
    data, status = await _group_json(session, f"https://groups.roblox.com/v1/groups/search?keyword={urllib.parse.quote(q, safe='')}&limit=10")
    rows = (data or {}).get("data") or []
    if not rows:
        return None, "Roblox couldn't be reached right now." if status in (0, 429) or status >= 500 else f"I couldn't find a group called **{q}**."
    best = next((g for g in rows if str(g.get("name", "")).lower() == q.lower()), rows[0])
    return str(best["id"]), None


async def fetch_group(group_id):
    """(info dict, error text) for one group: details, creation date, icon and role count."""
    async with aiohttp.ClientSession() as session:
        gid, err = await find_group_id(session, group_id)
        if err:
            return None, err
        base, status = await _group_json(session, f"https://groups.roblox.com/v1/groups/{gid}")
        if not base or not base.get("id"):
            return None, "Roblox couldn't be reached right now." if status in (0, 429) or status >= 500 else f"I couldn't find a group with the ID `{gid}`."
        created, icon, roles = await asyncio.gather(
            _group_json(session, f"https://groups.roblox.com/v2/groups?groupIds={gid}"),
            _group_json(session, f"https://thumbnails.roblox.com/v1/groups/icons?groupIds={gid}&size=420x420&format=Png"),
            _group_json(session, f"https://groups.roblox.com/v1/groups/{gid}/roles"))
        v2 = next(iter((created[0] or {}).get("data") or []), {}) or {}
        base["created"] = v2.get("created")
        base["icon"] = next(iter((icon[0] or {}).get("data") or []), {}).get("imageUrl")
        base["role_count"] = len((roles[0] or {}).get("roles") or []) or None
        return base, None


def _group_date(iso):
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except ValueError:
        return None


def group_embed(info, color):
    """The embed for a fetch_group() result."""
    gid = info["id"]
    verified = " ✅" if info.get("hasVerifiedBadge") else ""
    desc = (info.get("description") or "").strip()
    embed = discord.Embed(title=f"{info.get('name')}{verified}", url=f"https://www.roblox.com/communities/{gid}", color=color,
                          description=(desc[:600] + ("…" if len(desc) > 600 else "")) if desc else None)
    owner = info.get("owner") or {}
    if owner.get("userId"):
        embed.add_field(name="Owner", value=f"[{owner.get('displayName') or owner.get('username')}](https://www.roblox.com/users/{owner['userId']}/profile)\n@{owner.get('username')}", inline=True)
    else:
        embed.add_field(name="Owner", value="Nobody (unowned)", inline=True)
    embed.add_field(name="Members", value=f"{int(info.get('memberCount') or 0):,}", inline=True)
    embed.add_field(name="Group ID", value=f"`{gid}`", inline=True)
    created = _group_date(info.get("created"))
    if created:
        embed.add_field(name="Created", value=created, inline=True)
    embed.add_field(name="Join", value="Anyone can join" if info.get("publicEntryAllowed") else "Join requests (approval needed)", inline=True)
    if info.get("role_count"):
        embed.add_field(name="Roles", value=str(info["role_count"]), inline=True)
    if info.get("isLocked"):
        embed.add_field(name="Status", value="🔒 Locked by Roblox", inline=False)
    shout = info.get("shout") or {}
    if shout.get("body"):
        who = (shout.get("poster") or {}).get("username")
        when = _group_date(shout.get("updated"))
        embed.add_field(name="Shout", value=shout["body"][:500] + (f"\n— {who}" if who else "") + (f", {when}" if when else ""), inline=False)
    if info.get("icon"):
        embed.set_thumbnail(url=info["icon"])
    embed.set_footer(text="Roblox group")
    return embed


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


def _embed_color(user_id):
    saved = embedDB.get(f"{user_id}")
    return saved if saved else discord.Color.blue()


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


# =============================================================================================================
# Cogs. Each one is a class in this file; Shapes.setup_hook loads them. Commands with a space are app_commands.Groups.
# =============================================================================================================
_ROBLOX = {}      # filled by the Roblox cog: the handler for the API's "someone verified" notice


@app.route("/internal/verified", methods=["POST"])
def roblox_verified_notice():
    handler = _ROBLOX.get("notice")
    return handler() if handler else (jsonify({"success": False}), 503)



# --- DM log: every DM the bot sends or receives -------------------------------------------------------------
# Written to the dmlog table (who, which way, what was said, what caused it), shown with /dmlog (bot owners) and posted to the
# bot log channel when /botlog dms is on. Sending is caught once, on discord.py's Messageable.send and on interaction
# responses in a DM; receiving is caught as a DM to the bot or a command run in a DM.
import contextvars
_dm_user = contextvars.ContextVar("dm_user", default=None)         # set while a command that was run in a DM is being answered
_dm_cmd = contextvars.ContextVar("dm_cmd", default=None)
_DM_KEEP = 5000
_last_dm_trim = 0.0


def _dm_text(content=None, embeds=None, embed=None):
    parts = [str(content)] if content else []
    for e in list(embeds or []) + ([embed] if embed else []):
        d = e.to_dict() if hasattr(e, "to_dict") else (e or {})
        for key in ("title", "description"):
            if d.get(key):
                parts.append(str(d[key]))
        for f in d.get("fields") or []:
            parts.append(f"{f.get('name', '')}: {f.get('value', '')}")
    return " | ".join(parts)[:1000]


def _dm_source():
    cmd = _dm_cmd.get()
    if cmd:
        return f"/{cmd}"
    frame = sys._getframe(2)
    here = os.path.dirname(os.path.abspath(__file__))
    while frame:
        code = frame.f_code
        if os.path.abspath(code.co_filename).startswith(here) and code.co_name not in ("<module>", "_dm_source", "log_dm", "send", "send_message", "_send_logged"):
            return code.co_name
        frame = frame.f_back
    return "bot"


def log_dm(direction, user, text, source=None):
    """Write one DM down (user is a discord user/member or an id). Never raises."""
    global _last_dm_trim
    try:
        stamp = time.time()
        uid = str(getattr(user, "id", user) or "")
        name = getattr(user, "name", None) or uid
        dmlogDB.set(f"{int(stamp * 1000)}-{os.urandom(2).hex()}", {"at": int(stamp), "dir": direction, "user_id": uid, "name": name,
                                                                   "text": str(text or "")[:1000], "source": str(source or "")[:80]})
        print(f"[dm {'->' if direction == 'out' else '<-'}] {name}: {str(text or '')[:100]!r} ({source})")
        if stamp - _last_dm_trim > 3600:
            _last_dm_trim = stamp
            rows = sorted(dmlogDB.items().items())
            for key, _row in rows[:max(0, len(rows) - _DM_KEEP)]:
                dmlogDB.remove(key)
        cfg = cmdlogDB.get(BOTLOG_KEY) or {}
        post_fn = getattr(sys.modules[__name__], "cmdlog_post", None)
        if post_fn and cfg.get("enabled") and cfg.get("dms", True):
            embed = discord.Embed(title=("📤 DM sent to " if direction == "out" else "📥 DM from ") + str(name), description=str(text or "")[:1500] or "(no text)",
                                  color=discord.Color.green() if direction == "out" else discord.Color.orange(), timestamp=discord.utils.utcnow())
            embed.set_footer(text=f"{source or ''} · {uid}")
            asyncio.get_running_loop().create_task(post_fn(BOTLOG_KEY, embed))
    except Exception as e:
        print(f"[dm log] {type(e).__name__}: {e}")


def _install_dm_logging():
    orig_send = discord.abc.Messageable.send

    async def send(self, content=None, **kw):
        message = await orig_send(self, content, **kw)
        try:
            channel = getattr(message, "channel", None)
            if isinstance(channel, discord.DMChannel) and getattr(channel, "recipient", None) and (bot.user is None or channel.recipient.id != bot.user.id):
                log_dm("out", channel.recipient, _dm_text(content, kw.get("embeds"), kw.get("embed")), _dm_source())
        except Exception as e:
            print(f"[dm log] {type(e).__name__}: {e}")
        return message
    discord.abc.Messageable.send = send

    def wrap(cls, name, text_of):
        orig = getattr(cls, name)

        async def wrapped(self, *args, **kw):
            result = await orig(self, *args, **kw)
            who = _dm_user.get()
            if who is not None:
                log_dm("out", who, text_of(args, kw), _dm_source())
            return result
        setattr(cls, name, wrapped)
    first = lambda args, kw: _dm_text(kw.get("content") if "content" in kw else (args[0] if args else None), kw.get("embeds"), kw.get("embed"))
    wrap(discord.InteractionResponse, "send_message", first)
    wrap(discord.InteractionResponse, "edit_message", first)
    wrap(discord.Interaction, "edit_original_response", first)
    wrap(discord.Webhook, "send", first)         # followups; only logged while a DM command is being answered (see _dm_user)

    async def check(interaction):
        if interaction.guild_id is None and interaction.user and not interaction.user.bot:
            _dm_user.set(interaction.user)
            name = interaction.command.qualified_name if interaction.command else "command"
            _dm_cmd.set(name)
            args = " ".join(f"{k}:{v}" for k, v in (interaction.namespace.__dict__.items() if interaction.namespace else []))[:300]
            log_dm("in", interaction.user, f"/{name} {args}".strip(), "command in a DM")
        return True
    bot.tree.interaction_check = check

    async def on_dm(message):
        if message.guild is None and not message.author.bot and (bot.user is None or message.author.id != bot.user.id):
            log_dm("in", message.author, message.content or "(attachment)", "DM to the bot")
    bot.add_listener(on_dm, "on_message")


class DMLogCog(commands.Cog, name="DM log"):
    """Tracks every DM the bot sends or receives. /dmlog shows them (bot owners)."""

    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        _install_dm_logging()

    @app_commands.command(name="dmlog", description="Bot owners only: show the latest DMs the bot sent or received")
    @app_commands.describe(user="Only this person", count="How many to show (default 15, max 40)")
    @app_commands.allowed_installs(guilds=True, users=False)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
    async def dmlog(self, interaction: discord.Interaction, user: discord.User = None, count: app_commands.Range[int, 1, 40] = 15):
        if not is_bot_owner(interaction.user):
            return await interaction.response.send_message("Only the bot owners can read the DM log.", ephemeral=True)
        rows = [v for v in dmlogDB.items().values() if isinstance(v, dict) and (user is None or v.get("user_id") == str(user.id))]
        rows.sort(key=lambda r: -int(r.get("at") or 0))
        if not rows:
            return await interaction.response.send_message("No DMs logged yet.", ephemeral=True)
        lines = [f"{'📤' if r['dir'] == 'out' else '📥'} <t:{r['at']}:R> **{r.get('name')}** ({r.get('source') or ''})\n> {str(r.get('text') or '')[:150]}" for r in rows[:count]]
        await interaction.response.send_message(f"**DM log** ({len(rows)} total)\n" + "\n".join(lines)[:1900], ephemeral=True)


# --- Last.fm (/lastfm link, /lastfm unlink, /lastfm user): links are kept by the Shapes API per platform ("discord" here) ---
LASTFM_LINK_TTL = 600


def _shapes_call(method, path, body=None):
    """The Shapes API's JSON (a dict), or None when it can't be reached."""
    try:
        r = requests.request(method, f"{APIBaseURL}{path}", json=body, headers=APIBaseURL_Headers, timeout=15)
        data = r.json()
        return data if isinstance(data, dict) else None
    except (requests.RequestException, ValueError):
        return None


def lastfm_name_of(user_id):
    data = _shapes_call("GET", f"/internal/links/{user_id}?platform=discord") or {}
    return (data.get("lastfm") or {}).get("name")


def lastfm_line(p):
    scrobbles = f"{int(p['scrobbles']):,}" if str(p.get("scrobbles") or "").isdigit() else "?"
    text = f"[{p['name']}]({p['url']}) · {scrobbles} scrobbles"
    if p.get("track"):
        text += f"\n{'🎶 Now playing' if p.get('now') else '⏮️ Last played'}: {p['track']}"
    return text


class LastfmCog(commands.Cog, name="Last.fm"):
    """/lastfm link, /lastfm unlink, /lastfm user."""
    lastfm = app_commands.Group(name="lastfm", description="Link Last.fm and see what people are listening to",
                                allowed_installs=app_commands.AppInstallationType(guild=True, user=True),
                                allowed_contexts=app_commands.AppCommandContext(guild=True, dm_channel=True, private_channel=True))

    def __init__(self, bot):
        self.bot = bot

    @lastfm.command(name="link", description="Link your Last.fm account")
    async def link(self, interaction: discord.Interaction):
        started = await asyncio.to_thread(_shapes_call, "POST", "/internal/link/start",
                                          {"provider": "lastfm", "platform": "discord", "yapin_id": str(interaction.user.id), "yapin_name": interaction.user.name})
        url = (started or {}).get("url")
        if not url:
            return await interaction.response.send_message(str((started or {}).get("data") or "Couldn't reach the linking service. Try again in a moment."), ephemeral=True)
        view = discord.ui.View(timeout=LASTFM_LINK_TTL)
        view.add_item(discord.ui.Button(label="Authorize on Last.fm", style=discord.ButtonStyle.link, url=url))
        await interaction.response.send_message("Tap the button, approve access on Last.fm and you're linked. The link works for 10 minutes and only for you.", view=view, ephemeral=True)

    @lastfm.command(name="unlink", description="Unlink your Last.fm account")
    async def unlink(self, interaction: discord.Interaction):
        had = await asyncio.to_thread(lastfm_name_of, interaction.user.id)
        await asyncio.to_thread(_shapes_call, "DELETE", f"/internal/links/{interaction.user.id}/lastfm?platform=discord")
        await interaction.response.send_message("Unlinked your Last.fm account." if had else "You had no Last.fm account linked.", ephemeral=True)

    @lastfm.command(name="user", description="See what you or someone else is listening to")
    @app_commands.describe(user="Who to show (default: you)")
    async def user(self, interaction: discord.Interaction, user: discord.User = None):
        target = user or interaction.user
        name = await asyncio.to_thread(lastfm_name_of, target.id)
        if not name:
            who = "You haven't" if target.id == interaction.user.id else f"{target.display_name} hasn't"
            return await interaction.response.send_message(f"{who} linked a Last.fm account. Use `/lastfm link`.", ephemeral=True)
        await interaction.response.defer()
        data = await asyncio.to_thread(_shapes_call, "GET", f"/internal/lastfm/{urllib.parse.quote(str(name), safe='')}")
        p = (data or {}).get("profile") if data and data.get("success") else None
        if not p:
            return await interaction.followup.send(f"Couldn't reach Last.fm for `{name}` right now.")
        embed = discord.Embed(title=f"{target.display_name} on Last.fm", description=lastfm_line(p), color=_embed_color(interaction.user.id))
        if p.get("image"):
            embed.set_thumbnail(url=p["image"])
        embed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
        await interaction.followup.send(embed=embed)


class RobloxCog(commands.Cog, name="Roblox"):
    """Roblox verification, linked accounts and group info: the /roblox group."""

    def __init__(self, bot):
        self.bot = bot
        self.group = roblox_init(sys.modules[__name__])

    async def cog_load(self):
        self.bot.tree.add_command(self.group)

    async def cog_unload(self):
        self.bot.tree.remove_command(self.group.name)


class CommandLogCog(commands.Cog, name="Command logs"):
    """/cmdlog (a server's command log) and /botlog (bot owners)."""

    def __init__(self, bot):
        self.bot = bot
        cmdlog_init(sys.modules[__name__])


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


class ServerCog(commands.Cog, name="Server"):
    """/server info."""
    server = app_commands.Group(name="server", description="Information about this server", guild_only=True)

    def __init__(self, bot):
        self.bot = bot

    @server.command(name="info", description="Show information about this server")
    async def info(self, interaction: discord.Interaction):
        guild = interaction.guild
        people = sum(1 for mem in guild.members if not mem.bot)
        bots = sum(1 for mem in guild.members if mem.bot)
        embed = discord.Embed(title=guild.name, color=_embed_color(interaction.user.id), description=guild.description or None)
        embed.add_field(name="Owner", value=f"<@{guild.owner_id}>", inline=True)
        embed.add_field(name="Server ID", value=f"`{guild.id}`", inline=True)
        embed.add_field(name="Created", value=f"<t:{int(guild.created_at.timestamp())}:D>", inline=True)
        embed.add_field(name="Members", inline=False,
                        value=f"**{guild.member_count:,}** total (people and bots)\n**{people:,}** people · **{bots:,}** bots" if guild.chunked
                        else f"**{guild.member_count:,}** total (people and bots)")
        text, voice = len(guild.text_channels), len(guild.voice_channels)
        embed.add_field(name="Channels", value=f"{text + voice:,} total\n{text:,} text · {voice:,} voice\n{len(guild.categories):,} categories", inline=True)
        embed.add_field(name="Roles", value=f"{len(guild.roles):,}", inline=True)
        embed.add_field(name="Boosts", value=f"Level {guild.premium_tier} · {guild.premium_subscription_count or 0}", inline=True)
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        if guild.banner:
            embed.set_image(url=guild.banner.url)
        embed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
        await interaction.response.send_message(embed=embed)


COGS = (RobloxCog, CommandLogCog, VortexCog, ServerCog, LastfmCog, DMLogCog)


# === Bot Runner in Thread ===
def run_bot():
    if not token:
        print("DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in.")
        os._exit(1)
    bot.run(token)
# === Run Bot + Flask Webserver ===
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 13455))
    threading.Thread(target=run_bot).start()
    print(f"Starting Flask on port {port}")
    app.run(host="0.0.0.0", port=port, debug=True)

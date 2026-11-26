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
import types
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
SITE_URL = "https://shapes-yapper.oriaxen.com"                              # the Shapes website (also hosts the counting leaderboard)
MainURL = (os.environ.get("PUBLIC_URL") or SITE_URL).rstrip("/")             # shown in footers
searchengine = "621a38269031b4e89" # PLEASE USE YOUR OWN SEARCH ENGINE ID FROM https://cse.google.com/

# Secrets and settings, all from the environment (.env). See .env.example for the full list.
PUBLIC_KEY = os.environ.get("DISCORD_PUBLIC_KEY")      # verifies Discord's signed event webhooks (/discord/webhook)
DISCORD_CLIENT_ID = os.environ.get("DISCORD_CLIENT_ID") or ""          # "Sign in with Discord" on the dashboard
DISCORD_CLIENT_SECRET = os.environ.get("DISCORD_CLIENT_SECRET") or ""
DISCORD_REDIRECT_URI = os.environ.get("DISCORD_REDIRECT_URI") or ""    # must match one registered at discord.com/developers (…/login/discord/callback)
APIBaseURL_Key = (os.environ.get("SHAPES_API_TOKEN") or os.environ.get("Shapes_API_Key") or "").strip()    # bearer token for the Shapes API's /internal routes
APIBaseURL_Headers = {"Authorization": f"Bearer {APIBaseURL_Key}"}
token = (os.environ.get("DISCORD_TOKEN") or "").strip()                    # the bot token
ADMIN_KEY = (os.environ.get("ADMIN_KEY") or "").strip()                    # password for the /login admin page (empty = the page can't be unlocked)
GoogleAPIKey = (os.environ.get("GOOGLE_API_KEY") or "").strip()            # Google Custom Search key for /google
ROBLOSECURITY = (os.environ.get("ROBLOSECURITY") or "").strip()            # a Roblox account's .ROBLOSECURITY cookie (presence, avatar renders, profile data)
import apiurl
APIBaseURL = apiurl.clean_url(os.environ.get("SHAPES_API_URL"), "http://localhost:1337")     # the Shapes API (a pasted "NAME=" prefix is ignored)
APIFallbackURL = apiurl.clean_url(os.environ.get("SHAPES_API_FALLBACK_URL"), "https://api.shapes-yapper.oriaxen.com")   # tried when the first address fails
api_link = apiurl.Link(APIBaseURL, APIFallbackURL)
API_DOWN_TEXT = "The Shapes API is down right now, so this is turned off for the moment. Try again in a few minutes."
_api_state = {"down": False, "at": 0.0}


def _api_mark(ok):
    """Remember whether the Shapes API answered, so commands can say it's down instead of "not linked"."""
    if not ok and not _api_state["down"]:
        print("Shapes API Is Not Responding.")
    _api_state.update(down=not ok, at=time.time())


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
STARTED_AT = time.time()


def commit_info():
    """The running commit: {sha, title, url, branch}. Linked on the dashboard because this repo is public."""
    sha = GIT["sha"]
    url = f"{GIT['repo']}/commit/{sha}" if sha and GIT["repo"].startswith("https://github.com/") else ""
    return {"sha": GIT["short"], "title": GIT["title"], "url": url, "branch": GIT["branch"]}


def _fill_commit_title():
    """A shallow clone keeps its commit in a pack, so ask GitHub once in the background for the title."""
    if not GIT["sha"] or GIT["title"] or not GIT["repo"].startswith("https://github.com/"):
        return
    try:
        r = requests.get(f"https://api.github.com/repos/{GIT['repo'][len('https://github.com/'):]}/commits/{GIT['sha']}", timeout=8)
        if r.ok:
            GIT["title"] = str((r.json().get("commit") or {}).get("message") or "").split("\n", 1)[0][:120]
    except Exception:
        pass


threading.Thread(target=_fill_commit_title, daemon=True).start()
_api_commit = {"at": 0, "value": {}}


def api_commit_info():
    """The Shapes API's own commit ({sha, title}), asked of it every 5 minutes. Its repo is private, so it is shown without a link."""
    if time.time() - _api_commit["at"] > 300:
        data = _shapes_call("GET", "/internal/version") or {}
        _api_commit.update(at=time.time(), value={"sha": data.get("sha") or "", "title": data.get("title") or ""})
    return _api_commit["value"]


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
        actionsDB.set(f"{int(now * 1000)}-{os.urandom(2).hex()}", {"at": int(now), "actor_id": str(session.get("admin_id") or "admin"), "actor": session.get("admin_name") or "Admin key", "kind": kind,
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
    return render_template_string(_web_file("login.html"), error=error, discord_oauth=bool(DISCORD_CLIENT_ID and DISCORD_CLIENT_SECRET))


def _oauth_redirect_uri():
    if DISCORD_REDIRECT_URI:
        return DISCORD_REDIRECT_URI
    proto = request.headers.get("X-Forwarded-Proto", request.scheme).split(",")[0].strip()
    host = request.headers.get("X-Forwarded-Host", request.host).split(",")[0].strip()
    return f"{proto}://{host}/login/discord/callback"


def oauth_start(kind):
    """Send the person to Discord to sign in. kind: "admin" (bot owners, dashboard) or "user" (anyone, /dashboard)."""
    session["oauth_state"] = secrets.token_urlsafe(24)
    session["oauth_kind"] = kind
    q = urllib.parse.urlencode({"client_id": DISCORD_CLIENT_ID, "response_type": "code", "scope": "identify", "prompt": "none",
                                "redirect_uri": _oauth_redirect_uri(), "state": session["oauth_state"]})
    return redirect(f"https://discord.com/oauth2/authorize?{q}")


@app.route("/login/discord")
def admin_login_discord():
    """Bot owners can sign in with their Discord account instead of the admin key."""
    if not (DISCORD_CLIENT_ID and DISCORD_CLIENT_SECRET):
        return redirect(url_for("admin_login"))
    return oauth_start("admin")


@app.route("/login/discord/callback")
def admin_login_discord_callback():
    kind = session.pop("oauth_kind", "admin")

    def fail(msg):
        if kind == "user":
            return redirect("/dashboard?login_error=1")
        return render_template_string(_web_file("login.html"), error=msg, discord_oauth=bool(DISCORD_CLIENT_ID and DISCORD_CLIENT_SECRET)), 400
    state = session.pop("oauth_state", None)
    if request.args.get("error"):
        return fail("Discord sign-in was cancelled.")
    if not state or not hmac.compare_digest(state, request.args.get("state", "")) or not request.args.get("code"):
        return fail("That sign-in link expired. Try again.")
    try:
        tok = requests.post("https://discord.com/api/v10/oauth2/token", data={
            "client_id": DISCORD_CLIENT_ID, "client_secret": DISCORD_CLIENT_SECRET, "grant_type": "authorization_code",
            "code": request.args["code"], "redirect_uri": _oauth_redirect_uri()}, timeout=15)
        access = tok.json().get("access_token")
        me = requests.get("https://discord.com/api/v10/users/@me", headers={"Authorization": f"Bearer {access}"}, timeout=15).json() if access else {}
    except (requests.RequestException, ValueError):
        return fail("Couldn't reach Discord. Try again in a moment.")
    if not me.get("id"):
        return fail("Discord didn't accept that sign-in. Check DISCORD_CLIENT_ID, DISCORD_CLIENT_SECRET and the redirect URI (" + _oauth_redirect_uri() + ").")
    if kind == "user":
        avatar = f"https://cdn.discordapp.com/avatars/{me['id']}/{me['avatar']}.png?size=64" if me.get("avatar") else None
        session.clear()
        session["duser"] = {"id": str(me["id"]), "name": me.get("global_name") or me.get("username") or "You", "avatar": avatar}
        return redirect("/dashboard")
    user = types.SimpleNamespace(id=me["id"], name=me.get("username", ""))
    if not is_bot_owner(user):
        return fail("This Discord account isn't a bot owner.")
    session.clear()
    session.update(admin=True, admin_id=str(me["id"]), admin_name=me.get("global_name") or me.get("username") or "Owner")
    return redirect(url_for("dashboard"))


@app.route("/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))


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
        raise ValueError("That isn't one of the colors.")
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
        "commit": commit_info(), "api_commit": api_commit_info(), "is_owner": True, "colors": [{"name": n, "hex": "#%06x" % v} for n, v in DASH_COLORS.items()],
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


@app.route("/api/status")
@admin_required
def api_status():
    """Is everything this bot depends on working?"""
    down = bool(api_down())
    api_ms = None
    if APIBaseURL_Key and not down:
        t0 = time.time()
        _shapes_call("GET", "/internal/version", timeout=4)
        api_ms = int((time.time() - t0) * 1000)
        down = bool(_api_state["down"])
    return jsonify({
        "bot": {"uptime_seconds": int(time.time() - STARTED_AT), "servers": len(bot.guilds), "commands": len(bot.tree.get_commands()),
                "partners": None, "commit": commit_info()},
        "api": {"configured": bool(APIBaseURL_Key), "down": down, "ms": api_ms, "commit": api_commit_info() if APIBaseURL_Key and not down else None},
        "checked_at": int(time.time())})


def _person_platform(value):
    return value if value in ("yapin", "discord") else "discord"


@app.route("/api/people/<platform>/<uid>")
@admin_required
def api_person(platform, uid):
    """What the Shapes API has linked for a person (Roblox accounts, Discord link)."""
    platform = _person_platform(platform)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", uid):
        return jsonify({"error": "That isn't an ID."}), 400
    if api_down():
        return jsonify({"error": API_DOWN_TEXT, "api_down": True}), 503
    d = _shapes_call("GET", f"/internal/person/{platform}/{urllib.parse.quote(uid, safe='')}")
    if d is None:
        return jsonify({"error": API_DOWN_TEXT, "api_down": True}), 503
    if not d.get("success"):
        return jsonify({"error": "Nobody found with that ID."}), 404
    return jsonify({"platform": platform, "id": uid, "accounts": d.get("accounts", []), "max": d.get("max", 100),
                    "discord_id": d.get("discord_id") or None, "discord_ids": d.get("discord_ids") or []})


@app.route("/api/people/<platform>/<uid>/unlink", methods=["POST"])
@admin_required
def api_person_unlink(platform, uid):
    """Unlink one Roblox account ({roblox_id}) or a Discord link ({discord: true, discord_id}) from a person."""
    platform = _person_platform(platform)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", uid):
        return jsonify({"error": "That isn't an ID."}), 400
    body = request.get_json(silent=True) or {}
    quoted = urllib.parse.quote(uid, safe="")
    if body.get("discord"):
        did = str(body.get("discord_id") or "")
        d = _shapes_call("DELETE", f"/internal/person/{platform}/{quoted}/discord" + (f"?discord_id={urllib.parse.quote(did, safe='')}" if did else ""))
        what = "Discord link"
    else:
        rid = str(body.get("roblox_id") or "")
        if not rid:
            return jsonify({"error": "Which Roblox account?"}), 400
        d = _shapes_call("DELETE", f"/internal/person/{platform}/{quoted}/roblox/{urllib.parse.quote(rid, safe='')}")
        what = f"Roblox account {rid}"
    if d is None:
        return jsonify({"error": API_DOWN_TEXT, "api_down": True}), 503
    if not d.get("success"):
        return jsonify({"error": str(d.get("data") or d.get("error") or "The API refused that.")}), 400
    log_action("people", f"unlinked {what}", target=f"{platform}:{uid}")
    return jsonify({"ok": True})


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
    
def commands_payload():
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
    return commands


def command_entries():
    """Every command as {name, description, options, type}."""
    return list(commands_payload()["Commands"].values())


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

    if not PUBLIC_KEY:
        print("[events] DISCORD_PUBLIC_KEY isn't set, so Discord's event webhooks can't be verified.")
        return Response("not configured", 503)
    try:
        verify_key = nacl.signing.VerifyKey(bytes.fromhex(PUBLIC_KEY))
        verify_key.verify(f'{timestamp}{body}'.encode(), bytes.fromhex(signature))
    except (nacl.exceptions.BadSignatureError, ValueError):
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
            print("webhook_url isn't set, so this event isn't posted anywhere.")
            return Response(status=204)      # still acknowledge it, or Discord disables the endpoint

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
        cogs_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cogs")
        for f in sorted(os.listdir(cogs_dir)):     # every commands file in cogs/, A to Z: one topic per file, no commands live in this file
            if f.endswith(".py") and not f.startswith(("_", ".")):
                await self.load_extension(f"cogs.{f[:-3]}")

colors_lua = """{[3447003] = "Blue", [15158332] = "Red", [3066993] = "Green", [10181046] = "Purple", [15105570] = "Orange", [15844367] = "Gold", [1752220] = "Teal", [2123412] = "Dark Blue", [10038562] = "Dark Red", [2067276] = "Dark Green", [7419530] = "Dark Purple", [11027200] = "Dark Orange", [12745742] = "Dark Gold", [1146986] = "Dark Teal"}"""
colors = lua.decode(colors_lua)
#print(colors)

#bot = commands.Bot(command_prefix="/", intents=intents)
bot = Shapes(command_prefix="/", intents=discord.Intents.all())

# /roblox ... lives in one command group; the Roblox cog (below) adds the verification commands to it and the tree gets it when the cog loads.
@bot.tree.error
async def _tree_error(interaction, error):
    if isinstance(error, app_commands.CheckFailure) and "Shapes API is down" in str(error):
        return      # already answered with the "API is down" message
    import traceback
    print(f"[command error] {getattr(interaction.command, 'qualified_name', '?')}")
    traceback.print_exception(type(error), error, error.__traceback__)


roblox_group = app_commands.Group(
    name="roblox", description="Roblox lookups, verification, your linked accounts and group info",
    allowed_installs=app_commands.AppInstallationType(guild=True, user=True),
    allowed_contexts=app_commands.AppCommandContext(guild=True, dm_channel=True, private_channel=True))
roblox_panel = app_commands.Group(name="panel", description="Set up Roblox verification for this server (server admins)", parent=roblox_group)


async def _roblox_api_check(interaction):
    """Everything under /roblox except `group` needs the Shapes API. If it's down, say so instead of running."""
    if getattr(interaction.command, "name", "") == "group" or not await asyncio.to_thread(api_down):
        return True
    await interaction.response.send_message(API_DOWN_TEXT, ephemeral=True)
    raise app_commands.CheckFailure("Shapes API is down")


roblox_group.interaction_check = _roblox_api_check
roblox_panel.interaction_check = _roblox_api_check
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
        await bot.change_presence(activity=discord.CustomActivity(name="🔗 /help"))
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

COUNTING_DEFAULTS = {"channel": None, "number": 0, "enabled": False, "warnings": 0, "lastcounter": None, "highestnumber": 0, "banned": False}


def counting_get(guild_id):
    """A server's counting settings with every key present. The database leaves out anything that is empty (a channel nobody picked,
    nobody counting yet), which used to crash with KeyError: 'channel'. Ids always come back as numbers."""
    data = {**COUNTING_DEFAULTS, **dict(countingDB.get(str(guild_id)) or {})}
    for key in ("channel", "lastcounter"):
        try:
            data[key] = int(data[key]) if data[key] not in (None, "") else None
        except (TypeError, ValueError):
            data[key] = None
    return data


# === Bot Events ===
@bot.event
async def on_message(message):
    if message.author == bot.user:
        return
    if message.author.bot:
        return

    if message.guild:
        server = message.guild
        counting_data = counting_get(server.id)
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
        counting = counting_get(server.id)
        if counting["enabled"]:
            if message.author.id == counting['lastcounter'] and message.channel.id == counting['channel'] and IsInteger(message.content):
                nextnumber = counting['number'] + 1
                await message.channel.send(f"{message.author.mention} deleted their message containing the last number. The next number is {nextnumber}")

@bot.event
async def on_message_edit(before, after):
    if before.author.bot:
        return

    if before.guild:
        server = before.guild
        counting = counting_get(server.id)
        if counting["enabled"]:
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
    await bot.change_presence(activity=discord.CustomActivity(name="🔗 /help"))
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

def leaderboard_rows():
    """Counting servers, best first: [{serverName, currentnumber, highestnumber}]."""
    lb = []
    for server in countingDB.all():
        data = counting_get(server)
        guild = bot.get_guild(int(server)) if str(server).isdigit() else None
        if data.get("banned", False) == True:
            print(f'{guild.name if guild else "Unknown"} Is Banned From Counting.')
            continue
        if data["enabled"] == True and guild is not None:
            lb.append({"id": str(server), "serverName": guild.name, "currentnumber": data["number"], "highestnumber": data["highestnumber"]})
    return sorted(lb, key=lambda r: r["highestnumber"], reverse=True)


@app.route('/clb', methods=["GET"])
def countinglb():
    if bot.is_ready():
        FullLB = [(r["id"], {"currentnumber": r["currentnumber"], "highestnumber": r["highestnumber"], "serverName": r["serverName"]}) for r in leaderboard_rows()]
        return {"Leaderboard": FullLB}, 200
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



# === Message Commands ===


    #await interaction.response.send_message(f"Invite me to your server or add me to your apps using this link: {invite_url}", ephemeral=False)
    
        


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


            


        
_tasks = set()
BOTLOG_KEY = "botlog"


def _embed_color(user_id):
    saved = embedDB.get(f"{user_id}")
    return saved if saved else discord.Color.blue()


# =============================================================================================================
# Cogs live in cogs/ (one topic per file); Shapes.setup_hook loads them. Commands with a space are app_commands.Groups.
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
_dm_cmd = contextvars.ContextVar("dm_cmd", default=None)
_DM_KEEP = 5000
_last_dm_trim = 0.0


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


def _shapes_call(method, path, body=None, timeout=15):
    """The Shapes API's JSON (a dict), or None when it can't be reached."""
    try:
        r = api_link.request(method, path, json=body, headers=APIBaseURL_Headers, timeout=timeout)
        if r.status_code >= 502:
            raise requests.RequestException(f"HTTP {r.status_code}")
        data = r.json()
        _api_mark(True)
        return data if isinstance(data, dict) else None
    except (requests.RequestException, ValueError):
        _api_mark(False)
        return None


def api_down():
    """True when the Shapes API isn't answering (and this bot is set up to use it). Re-checked at most every 20 seconds."""
    if not APIBaseURL_Key:
        return False
    if time.time() - _api_state["at"] > 20:
        _shapes_call("GET", "/internal/version", timeout=4)
    return _api_state["down"]


# --- /ping data: how this bot, the API and the Yapin' bot are doing ---------------------------------------------
import procstats


def bot_latency_ms():
    """The Discord gateway latency in ms, or None while the bot isn't connected."""
    try:
        v = bot.latency
        return None if v != v else v * 1000          # nan before the first heartbeat
    except Exception:
        return None


@app.route("/internal/stats")
def internal_stats():
    """For the Shapes API (same machine, shared token): this bot's uptime, memory, CPU and ping."""
    header = request.headers.get("Authorization", "")
    if not APIBaseURL_Key or not (header.startswith("Bearer ") and hmac.compare_digest(header[7:].strip(), APIBaseURL_Key)):
        return jsonify({"success": False, "data": "Not allowed"}), 403
    return jsonify({"success": True, "platform": "discord", "latency_ms": bot_latency_ms(), **procstats.snapshot()})


_ping_session = requests.Session()      # kept open, so the timing below is the network and not a new TLS handshake


def api_ping_ms():
    """Round trip to the API on a warm connection, in ms (None when no address answers). Uses the fallback address if the first fails."""
    try:
        for i in range(2):
            start = time.perf_counter()
            api_link.request("GET", "/internal/version", session=_ping_session, headers=APIBaseURL_Headers, timeout=8)
        return (time.perf_counter() - start) * 1000
    except requests.RequestException:
        return None


def send_heartbeat():
    """Tell the API how this bot is doing (the API shows it in /ping on either bot)."""
    return _shapes_call("POST", "/internal/heartbeat/discord", {**procstats.snapshot(), "latency_ms": bot_latency_ms()}, timeout=8)


def _heartbeat_loop():
    time.sleep(20)
    while True:
        try:
            send_heartbeat()
        except Exception as e:
            print(f"heartbeat failed: {e}")
        time.sleep(30)


threading.Thread(target=_heartbeat_loop, daemon=True, name="heartbeat").start()


def fleet_stats():
    """({api, yapin, discord, problems}, the API's ping in ms). A part that doesn't answer is None and problems says why."""
    send_heartbeat()                     # so this bot's own numbers are fresh in the API
    ping = api_ping_ms()
    start = time.perf_counter()
    got = _shapes_call("GET", "/internal/stats", timeout=20)
    if ping is None and got:
        ping = (time.perf_counter() - start) * 1000      # the ping probe failed but the stats call worked: its time is the ping
    if not got or not got.get("success"):
        return {"api": None, "yapin": None, "discord": None, "problems": {"api": "The Shapes API isn't answering" + (f" ({api_link.last_error})" if api_link.last_error else "") + " (or hasn't been redeployed with /internal/stats)."}}, None
    return got, ping


# --- fun/utility command helpers (no Discord/Yapin' types, so both bots share the same text) ---


# === Bot Runner in Thread ===
def run_bot():
    if not token:
        print("DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in.")
        os._exit(1)
    bot.run(token)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import siteweb
import webdash
siteweb.register(sys.modules[__name__])
webdash.register(sys.modules[__name__])


# === Run Bot + Flask Webserver ===
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 13455))
    threading.Thread(target=run_bot).start()
    print(f"Starting Flask on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)

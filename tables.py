"""Cloudflare D1 storage as real tables, one table per bot store.

Every bot "database" (counting, embed, users, ...) is its own table with typed columns:

    bot_counting(server_id, channel, number, enabled, warnings, ...)    one row per server
    bot_partners(server_id, name, description, invite, ...)             and so on, see SPECS below

The code still talks to them the same way (get / set / remove / exists / all / items / save), and values are still plain
Python dicts, so nothing outside this file changed shape. Rules that keep it lossless:

  * A value that doesn't fit its column's type, and any key that has no column, is kept in the row's extra_json, so
    nothing is ever dropped or changed. Only None-valued keys are stored as NULL and read back as "missing".
  * Reads come from memory (loaded once at start). Writes are batched in the background, rapid updates to one key are
    coalesced, and a failed batch is retried on the next save.
  * Without the Cloudflare env vars the bot falls back to local JSON files, exactly as before.

One-time move from the old table (kv): on the first start the rows of kv are copied into the new tables, read back from D1
and compared. Only when every row matches is bot_meta marked done. If anything differs the bot keeps using kv for that run
(nothing is lost, it retries next start). kv itself is never deleted by this code.

Env vars: CLOUDFLARE_API_TOKEN (D1: Edit), CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_D1_DATABASE_ID.
"""
import atexit
import json
import os
import threading
import time

FLUSH_INTERVAL = 1.5    # seconds between background write batches
BATCH_SIZE = 50         # statements per request
MIGRATION_KEY = "kv_to_tables"


class Spec:
    def __init__(self, table, pk, cols=(), scalar=None, index=()):
        self.table, self.pk, self.cols, self.scalar, self.index = table, pk, tuple(cols), scalar, tuple(index)
        self.types = dict(self.cols)


# column types: text, int, bool, json (any JSON value, stored as text)
SPECS = {
    "counting": Spec("bot_counting", "server_id", [("channel", "text"), ("number", "int"), ("enabled", "bool"), ("warnings", "int"),
                                                   ("lastcounter", "text"), ("highestnumber", "int"), ("banned", "bool"), ("emojis", "json")]),
    "embed": Spec("bot_embed", "user_id", [("color", "int")], scalar="color"),
    "users": Spec("bot_users", "user_id", [("vortex", "text")]),
    "autorole": Spec("bot_autorole", "server_id", [("role_id", "text"), ("enabled", "bool"), ("exempt_users", "json"), ("include_bots", "bool")]),
    "ai": Spec("bot_ai", "user_id", [("username", "text"), ("user_messages", "json"), ("ai_responses", "json")]),
    "automod": Spec("bot_automod", "server_id", [("enabled", "bool"), ("log_channel", "text"), ("exempt_roles", "json"),
                                                 ("exempt_channels", "json"), ("rules", "json"), ("actions", "json")]),
    "audit": Spec("bot_audit", "entry_id", [("at", "int"), ("db", "text"), ("key", "text"), ("action", "text"), ("before", "text"), ("after", "text")],
                  index=["key"]),
    "cmdlog": Spec("bot_cmdlog", "entry_id", [("at", "int"), ("user_id", "text"), ("username", "text"), ("command", "text"), ("args", "text"),
                                              ("channel_id", "text"), ("server_id", "text"), ("server", "text")], index=["server_id"]),
    "cmdlogcfg": Spec("bot_cmdlog_config", "server_id", [("enabled", "bool"), ("channel", "text"), ("public_ok", "bool"), ("mode", "text"),
                                                         ("webhook_id", "text")]),
    "actions": Spec("bot_actions", "entry_id", [("at", "int"), ("actor_id", "text"), ("actor", "text"), ("kind", "text"), ("detail", "text"),
                                                ("server_id", "text"), ("target", "text")], index=["server_id"]),
    "dms": Spec("bot_dms", "user_id", [("channel_id", "text"), ("name", "text"), ("last", "int"), ("preview", "text")]),
    "partners": Spec("bot_partners", "server_id", [("name", "text"), ("description", "text"), ("invite", "text"), ("invite_auto", "bool"),
                                                   ("added_by", "text"), ("added_at", "int"), ("icon", "text")]),
    "holds": Spec("bot_holds", "server_id", [("name", "text"), ("since", "int"), ("delete_after", "int")]),
    "welcomed": Spec("bot_welcomed", "server_id", [("at", "int"), ("name", "text")]),
    "rverify": Spec("bot_rverify", "server_id", [("roles", "json")]),
}
# A name with no spec (a store added later, or one found in the old kv table) still works: it lives in bot_misc.

META_SQL = "CREATE TABLE IF NOT EXISTS bot_meta (k TEXT PRIMARY KEY, v TEXT NOT NULL)"
MISC_SQL = 'CREATE TABLE IF NOT EXISTS bot_misc (db TEXT NOT NULL, k TEXT NOT NULL, v TEXT NOT NULL, PRIMARY KEY (db, k))'
KV_SQL = "CREATE TABLE IF NOT EXISTS kv (db TEXT NOT NULL, k TEXT NOT NULL, v TEXT NOT NULL, PRIMARY KEY (db, k))"

_client = None
_client_lock = threading.Lock()
_stores = []
_mode = None            # "tables" | "kv" | "local"
_mode_lock = threading.Lock()


def _q(name):
    return '"' + name.replace('"', '""') + '"'


def d1_configured():
    return bool(os.environ.get("CLOUDFLARE_API_TOKEN") and os.environ.get("CLOUDFLARE_ACCOUNT_ID") and os.environ.get("CLOUDFLARE_D1_DATABASE_ID"))


def _cf():
    """The shared Cloudflare client, or None when the env vars are not set."""
    global _client
    if not d1_configured():
        return None
    with _client_lock:
        if _client is None:
            from cloudflare import Cloudflare
            _client = Cloudflare(api_token=os.environ["CLOUDFLARE_API_TOKEN"])
    return _client


def _query(sql, params=None):
    """One statement; the result rows as a list of dicts."""
    page = _cf().d1.database.query(os.environ["CLOUDFLARE_D1_DATABASE_ID"], account_id=os.environ["CLOUDFLARE_ACCOUNT_ID"], sql=sql,
                                   **({"params": [str(p) for p in params]} if params else {}))
    rows = []
    for result in page:
        rows.extend(getattr(result, "results", None) or [])
    return rows


def _batch(statements):
    """Several statements in one request: [(sql, params), ...]."""
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
    return True      # json


def to_row(spec, key, value):
    """(columns, extra) where columns is {name: python value or None} for every column of the table."""
    cols = {name: None for name, _ in spec.cols}
    extra = {}
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


def from_row(spec, row):
    extra = json.loads(row["extra_json"]) if row.get("extra_json") else {}
    if "__value__" in extra and len(extra) == 1:
        return extra["__value__"]
    if spec.scalar and row.get(spec.scalar) is not None and not extra:
        return _py(spec.types[spec.scalar], row[spec.scalar])
    rec = {}
    for name, kind in spec.cols:
        v = row.get(name)
        if v is not None:
            rec[name] = _py(kind, v)
    rec.update(extra)
    return rec


def _py(kind, v):
    if kind == "json":
        return json.loads(v)
    if kind == "bool":
        return bool(int(v))
    if kind == "int":
        return int(v)
    return str(v)


def _sql_value(kind, v):
    """(sql fragment, params to add). NULL goes in as a literal because every bound value travels as text."""
    if v is None:
        return "NULL", []
    if kind == "json":
        return "?", [json.dumps(v, sort_keys=True)]
    if kind == "bool":
        return "?", [1 if v else 0]
    return "?", [v]


def upsert_statement(spec, key, value):
    cols, extra = to_row(spec, key, value)
    names = [spec.pk] + [n for n, _ in spec.cols] + ["extra_json"]
    frags, params = ["?"], [str(key)]
    for name, kind in spec.cols:
        f, p = _sql_value(kind, cols[name])
        frags.append(f)
        params += p
    frags.append("?" if extra else "NULL")
    if extra:
        params.append(json.dumps(extra, sort_keys=True))
    sets = ", ".join(f"{_q(n)} = excluded.{_q(n)}" for n in names[1:])
    return (f"INSERT INTO {spec.table} ({', '.join(_q(n) for n in names)}) VALUES ({', '.join(frags)}) "
            f"ON CONFLICT({_q(spec.pk)}) DO UPDATE SET {sets}"), params


def create_statements(spec):
    cols = ", ".join(f"{_q(n)} {'INTEGER' if k in ('int', 'bool') else 'TEXT'}" for n, k in spec.cols)
    out = [f"CREATE TABLE IF NOT EXISTS {spec.table} ({_q(spec.pk)} TEXT PRIMARY KEY, {cols}{', ' if cols else ''}extra_json TEXT)"]
    out += [f"CREATE INDEX IF NOT EXISTS idx_{spec.table}_{c} ON {spec.table} ({_q(c)})" for c in spec.index]
    return out


def _norm(v):
    """What a stored value looks like after a round trip: top-level None keys are not kept."""
    return {k: x for k, x in v.items() if x is not None} if isinstance(v, dict) else v


# ------------------------------------------------------------------------------------------------ the one-time move

def _migrate_kv():
    """Copy kv -> tables, read back and compare. True when the tables are complete and marked done."""
    done = _query("SELECT v FROM bot_meta WHERE k = ?", [MIGRATION_KEY])
    if done:
        return True
    if not _query("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'kv'"):
        _query("INSERT INTO bot_meta (k, v) VALUES (?, ?)", [MIGRATION_KEY, f"fresh {int(time.time())}"])
        return True
    old = {}
    for r in _query("SELECT db, k, v FROM kv"):
        try:
            val = json.loads(r["v"])
        except (TypeError, ValueError):
            val = r["v"]
        old.setdefault(r["db"], {})[r["k"]] = val
    statements = []
    for name, rows in old.items():
        spec = SPECS.get(name)
        for k, val in rows.items():
            if spec:
                statements.append(upsert_statement(spec, k, val))
            else:
                statements.append(("INSERT INTO bot_misc (db, k, v) VALUES (?, ?, ?) ON CONFLICT(db, k) DO UPDATE SET v = excluded.v", [name, k, json.dumps(val)]))
    for i in range(0, len(statements), BATCH_SIZE):
        _batch(statements[i:i + BATCH_SIZE])
    bad = []
    for name, rows in old.items():
        spec = SPECS.get(name)
        if spec:
            back = {str(r[spec.pk]): from_row(spec, r) for r in _query(f"SELECT * FROM {spec.table}")}
        else:
            back = {r["k"]: json.loads(r["v"]) for r in _query("SELECT k, v FROM bot_misc WHERE db = ?", [name])}
        for k, val in rows.items():
            if k not in back or _norm(back[k]) != _norm(val):
                bad.append(f"{name}[{k}]")
    total = sum(len(r) for r in old.values())
    if bad:
        print(f"[tables] migration check FAILED for {len(bad)} of {total} rows (first: {', '.join(bad[:5])}); staying on the old kv table this run")
        return False
    _query("INSERT INTO bot_meta (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v", [MIGRATION_KEY, f"{total} rows {int(time.time())}"])
    print(f"[tables] moved {total} rows from kv into real tables and verified every one; kv is left untouched (drop it when you're happy)")
    return True


def _ensure_mode():
    global _mode
    with _mode_lock:
        if _mode:
            return _mode
        if not _cf():
            _mode = "local"
            return _mode
        try:
            _batch([(META_SQL, []), (MISC_SQL, [])] + [(s, []) for spec in SPECS.values() for s in create_statements(spec)])
            _mode = "tables" if _migrate_kv() else "kv"
        except Exception as e:
            print(f"[tables] setup failed ({e}); using the old kv table this run")
            _mode = "kv"
        if _mode == "kv":
            _query(KV_SQL)
        return _mode


def _flush_all():
    for s in list(_stores):
        try:
            s.save()
        except Exception as e:
            print(f"[tables] final save failed for {s.name}: {e}")


atexit.register(_flush_all)


class Table:
    """One store: a dict-like (get / set / remove / exists / all / items / save)."""

    def __init__(self, name):
        self.name = name
        self._data = {}
        self._dirty = set()
        self._deleted = set()
        self._lock = threading.RLock()
        self._path = f"{name}.json"
        self.mode = _ensure_mode()
        self.spec = SPECS.get(name)
        self.backend = {"tables": "d1-tables", "kv": "d1-kv", "local": "local-json"}[self.mode]
        self._load()
        _stores.append(self)
        threading.Thread(target=self._flusher, name=f"tables-{name}", daemon=True).start()

    def _load(self):
        if self.mode == "local":
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
            except FileNotFoundError:
                self._data = {}
            except Exception as e:
                print(f"[tables] could not read {self._path}: {e}")
            return
        try:
            if self.mode == "kv":
                for row in _query("SELECT k, v FROM kv WHERE db = ?", [self.name]):
                    try:
                        self._data[row["k"]] = json.loads(row["v"])
                    except (TypeError, ValueError):
                        self._data[row["k"]] = row["v"]
            elif self.spec:
                for row in _query(f"SELECT * FROM {self.spec.table}"):
                    self._data[str(row[self.spec.pk])] = from_row(self.spec, row)
            else:
                for row in _query("SELECT k, v FROM bot_misc WHERE db = ?", [self.name]):
                    self._data[row["k"]] = json.loads(row["v"])
        except Exception as e:
            print(f"[tables] could not load '{self.name}' from Cloudflare D1: {e}")

    # ---- dict-like API ----

    def get(self, key, default=None):
        with self._lock:
            return self._data.get(str(key), default)

    def set(self, key, value):
        key = str(key)
        with self._lock:
            self._data[key] = value
            self._dirty.add(key)
            self._deleted.discard(key)
        return True

    def exists(self, key):
        with self._lock:
            return str(key) in self._data

    def remove(self, key):
        key = str(key)
        with self._lock:
            existed = key in self._data
            self._data.pop(key, None)
            self._dirty.discard(key)
            if existed:
                self._deleted.add(key)
        return existed

    def all(self):
        with self._lock:
            return list(self._data.keys())

    getall = all

    def items(self):
        with self._lock:
            return dict(self._data)

    # ---- writing ----

    def _statement(self, key, value):
        if self.mode == "kv":
            return ("INSERT INTO kv (db, k, v) VALUES (?, ?, ?) ON CONFLICT(db, k) DO UPDATE SET v = excluded.v", [self.name, key, json.dumps(value)])
        if self.spec:
            return upsert_statement(self.spec, key, value)
        return ("INSERT INTO bot_misc (db, k, v) VALUES (?, ?, ?) ON CONFLICT(db, k) DO UPDATE SET v = excluded.v", [self.name, key, json.dumps(value)])

    def _delete_statement(self, key):
        if self.mode == "kv" or not self.spec:
            return ("DELETE FROM kv WHERE db = ? AND k = ?" if self.mode == "kv" else "DELETE FROM bot_misc WHERE db = ? AND k = ?", [self.name, key])
        return (f"DELETE FROM {self.spec.table} WHERE {_q(self.spec.pk)} = ?", [key])

    def save(self):
        """Write everything that changed. Safe to call as often as you like. Returns False if any write failed
        (failed writes are kept and retried on the next save)."""
        with self._lock:
            dirty = {k: self._data[k] for k in self._dirty if k in self._data}
            deleted = set(self._deleted)
            self._dirty.clear()
            self._deleted.clear()
        if self.mode == "local":
            if dirty or deleted or not os.path.exists(self._path):
                with self._lock:
                    snapshot = dict(self._data)
                tmp = self._path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(snapshot, f)
                os.replace(tmp, self._path)
            return True
        if not dirty and not deleted:
            return True
        statements = [(("U", k), self._statement(k, v)) for k, v in dirty.items()] + [(("D", k), self._delete_statement(k)) for k in deleted]
        ok = True
        for i in range(0, len(statements), BATCH_SIZE):
            chunk = statements[i:i + BATCH_SIZE]
            try:
                _batch([s for _, s in chunk])
            except Exception as e:
                ok = False
                print(f"[tables] write failed for {self.name} ({len(chunk)} rows): {e}")
                with self._lock:    # put them back so the next save retries
                    for (kind, k), _ in chunk:
                        if kind == "U" and k in self._data:
                            self._dirty.add(k)
                        elif kind == "D" and k not in self._data:
                            self._deleted.add(k)
        return ok

    def _flusher(self):
        while True:
            time.sleep(FLUSH_INTERVAL)
            try:
                if self._dirty or self._deleted:
                    self.save()
            except Exception as e:
                print(f"[tables] background save error in {self.name}: {e}")


def backend_summary():
    return ", ".join(f"{s.name}={s.backend}" for s in _stores) or "no stores"

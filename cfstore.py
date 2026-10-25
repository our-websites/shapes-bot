"""Cloudflare D1 storage with a pickledb-style API (get / set / remove / exists / all / save).

Why D1: its free plan (5 million row reads and 100,000 row writes per day, 5 GB) is far more than this bot needs,
so storage costs $0. Workers KV also has a free tier, but only 1,000 writes a day, which a busy counting channel
would burn through.

How it works:
  * Every "database" (counting, embed, users, autorole, ai) is a group of rows in ONE small D1 table:
        kv(db TEXT, k TEXT, v TEXT, PRIMARY KEY (db, k))     -- v is the value as JSON
    The table is created automatically on first start.
  * Reads come from an in-memory copy that is loaded once at startup, so commands never wait on the network.
  * Writes are batched in the background (every FLUSH_INTERVAL seconds, many rows per request). Rapid updates to the
    same key are coalesced, only the latest value is written.
  * If the Cloudflare env vars are missing, it falls back to local JSON files so you can still test the bot.

Env vars:
  CLOUDFLARE_API_TOKEN       API token with the permission "D1: Edit" (Account level)
  CLOUDFLARE_ACCOUNT_ID      your account id
  CLOUDFLARE_D1_DATABASE_ID  the D1 database id (Workers & Pages -> D1 -> create a database)
"""
import atexit
import json
import os
import threading
import time

FLUSH_INTERVAL = 1.5    # seconds between background write batches
BATCH_SIZE = 50         # statements per request

_client = None
_client_lock = threading.Lock()
_stores = []

CREATE_TABLE = "CREATE TABLE IF NOT EXISTS kv (db TEXT NOT NULL, k TEXT NOT NULL, v TEXT NOT NULL, PRIMARY KEY (db, k))"
UPSERT = "INSERT INTO kv (db, k, v) VALUES (?, ?, ?) ON CONFLICT(db, k) DO UPDATE SET v = excluded.v"
DELETE = "DELETE FROM kv WHERE db = ? AND k = ?"
SELECT = "SELECT k, v FROM kv WHERE db = ?"


def _cf():
    """The shared Cloudflare client, or None when the env vars are not set."""
    global _client
    if not (os.environ.get("CLOUDFLARE_API_TOKEN") and os.environ.get("CLOUDFLARE_ACCOUNT_ID")
            and os.environ.get("CLOUDFLARE_D1_DATABASE_ID")):
        return None
    with _client_lock:
        if _client is None:
            from cloudflare import Cloudflare
            _client = Cloudflare(api_token=os.environ["CLOUDFLARE_API_TOKEN"])
    return _client


def _flush_all():
    for s in list(_stores):
        try:
            s.save()
        except Exception as e:
            print(f"[cfstore] final save failed for {s.name}: {e}")


atexit.register(_flush_all)


class CloudflareD1:
    def __init__(self, name):
        self.name = name
        self._data = {}
        self._dirty = set()
        self._deleted = set()
        self._lock = threading.RLock()
        self._client = _cf()
        self._account = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
        self._database = os.environ.get("CLOUDFLARE_D1_DATABASE_ID")
        self._path = f"{name}.json"
        self.backend = "cloudflare-d1" if self._client else "local-json"
        self._load()
        _stores.append(self)
        threading.Thread(target=self._flusher, name=f"cfstore-{name}", daemon=True).start()

    # ---- D1 plumbing ----

    def _query(self, sql, params=None):
        """One statement; returns the result rows (a list of dicts)."""
        page = self._client.d1.database.query(self._database, account_id=self._account, sql=sql,
                                              **({"params": [str(p) for p in params]} if params else {}))
        rows = []
        for result in page:
            rows.extend(getattr(result, "results", None) or [])
        return rows

    def _batch(self, statements):
        """Several statements in one request: [(sql, params), ...]."""
        self._client.d1.database.query(
            self._database, account_id=self._account,
            batch=[{"sql": sql, "params": [str(p) for p in params]} for sql, params in statements])

    # ---- loading ----

    def _load(self):
        if not self._client:
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
            except FileNotFoundError:
                self._data = {}
            except Exception as e:
                print(f"[cfstore] could not read {self._path}: {e}")
            return
        try:
            self._query(CREATE_TABLE)
            for row in self._query(SELECT, [self.name]):
                try:
                    self._data[row["k"]] = json.loads(row["v"])
                except (TypeError, ValueError):
                    self._data[row["k"]] = row["v"]
        except Exception as e:
            print(f"[cfstore] could not load '{self.name}' from Cloudflare D1: {e}")

    # ---- pickledb-style API ----

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

    def save(self):
        """Write everything that changed. Safe to call as often as you like. Returns False if any write failed
        (failed writes are kept and retried on the next save)."""
        with self._lock:
            dirty = {k: self._data[k] for k in self._dirty if k in self._data}
            deleted = set(self._deleted)
            self._dirty.clear()
            self._deleted.clear()
        if not self._client:
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

        statements = [(UPSERT, [self.name, k, json.dumps(v)]) for k, v in dirty.items()]
        statements += [(DELETE, [self.name, k]) for k in deleted]
        ok = True
        for i in range(0, len(statements), BATCH_SIZE):
            chunk = statements[i:i + BATCH_SIZE]
            try:
                self._batch(chunk)
            except Exception as e:
                ok = False
                print(f"[cfstore] write failed for {self.name} ({len(chunk)} rows): {e}")
                with self._lock:    # put them back so the next save retries
                    for sql, params in chunk:
                        k = params[1]
                        if sql == UPSERT and k in self._data:
                            self._dirty.add(k)
                        elif sql == DELETE and k not in self._data:
                            self._deleted.add(k)
        return ok

    def _flusher(self):
        while True:
            time.sleep(FLUSH_INTERVAL)
            try:
                if self._dirty or self._deleted:
                    self.save()
            except Exception as e:
                print(f"[cfstore] background save error in {self.name}: {e}")


def backend_summary():
    return ", ".join(f"{s.name}={s.backend}" for s in _stores) or "no stores"

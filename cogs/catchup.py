"""Cog: start-up catch-up. While the bot was offline people may have joined, so on start (and then never again) it looks at each
server's newest members and gives them what a live join would have: the autorole, the welcome message and Roblox auto-verify.
It also writes a "last running" time every 30 seconds so it knows how long it was gone."""
import sys
import threading
import time
import types
from datetime import datetime

import yapper

m = sys.modules.get("main") or sys.modules["__main__"]   # this bot's main.py: shared helpers, data and the bot itself
bot = m.bot
botstateDB = m.botstateDB
get_autorole = m.get_autorole
autorole_applies = m.autorole_applies
get_welcome = m.get_welcome
send_welcome = m.send_welcome
_all_members = m._all_members

UNKNOWN_WINDOW = 6 * 3600        # no "last running" time saved yet: look at people who joined in the last 6 hours
MAX_WINDOW = 7 * 86400           # never look further back than a week
WELCOME_CAP = 20                 # at most this many welcome messages per server in one catch-up
BEAT_EVERY = 30


def _epoch(value):
    """A join time (ISO text or epoch seconds/milliseconds) as epoch seconds, or None."""
    if value in (None, ""):
        return None
    try:
        n = float(value)
        return n / 1000 if n > 1e11 else n
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _beat_loop():
    while True:
        try:
            botstateDB.set("lastup", {"at": int(time.time())})
        except Exception as e:
            print(f"[catchup] couldn't save the running time: {e}")
        time.sleep(BEAT_EVERY)


def catch_up():
    """Handle everyone who joined since the bot was last running. Safe to run any time: giving a role someone has is skipped."""
    now = time.time()
    last = int((botstateDB.get("lastup") or {}).get("at") or 0)
    since = max(last or now - UNKNOWN_WINDOW, now - MAX_WINDOW)
    print(f"[catchup] looking for members who joined in the last {int((now - since) / 60)} minutes")
    given = welcomed = verified = 0
    for server in list(bot.servers()):
        sid = str(server.get("id"))
        try:
            cfg, wcfg = get_autorole(sid), get_welcome(sid)
            verify = m.rverify_join
            members = _all_members(sid)
        except Exception as e:
            print(f"[catchup] {sid}: {type(e).__name__}: {e}")
            continue
        sent = 0
        recent = [p for p in members if (_epoch(p.get("joined")) or 0) >= since]
        for person in sorted(recent, key=lambda p: _epoch(p.get("joined")) or 0):
            uid = person["id"]
            try:
                if autorole_applies(cfg, sid, uid) and str(cfg["role_id"]) not in person["roles"]:
                    bot.add_role(sid, uid, cfg["role_id"])
                    given += 1
                if wcfg["enabled"] and sent < WELCOME_CAP and send_welcome(sid, uid):
                    sent += 1
                    welcomed += 1
                verify(types.SimpleNamespace(server_id=sid, user_id=uid))
                verified += 1
            except Exception as e:
                print(f"[catchup] {sid}/{uid}: {type(e).__name__}: {e}")
        # an autorole turned on while the bot was down also covers people who joined earlier and still lack the role
        if cfg["enabled"] and cfg["role_id"] and last:
            for person in members:
                if person not in recent and str(cfg["role_id"]) not in person["roles"] and autorole_applies(cfg, sid, person["id"]) \
                        and (_epoch(person.get("joined")) or 0) >= last - 3600:
                    try:
                        bot.add_role(sid, person["id"], cfg["role_id"])
                        given += 1
                    except Exception as e:
                        print(f"[catchup] {sid}/{person['id']}: {type(e).__name__}: {e}")
    print(f"[catchup] done: {given} roles given, {welcomed} welcomes sent, {verified} members checked for Roblox verification")


def _start():
    time.sleep(20)                       # let the servers and the Roblox hooks finish loading
    try:
        catch_up()
    except Exception as e:
        print(f"[catchup] failed: {type(e).__name__}: {e}")
    _beat_loop()


class Catchup(yapper.Cog):
    name = "Catch-up"

    @yapper.listener("ready")
    def on_ready(self, *_):
        threading.Thread(target=_start, name="catchup", daemon=True).start()


def setup(bot):
    bot.add_cog(Catchup(bot))

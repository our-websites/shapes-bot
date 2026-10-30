"""Automod: rule checks and escalating punishments, kept apart from the bot wiring so it can be tested on its own.

Per-server config (stored by main.py in the "automod" database, key = server id):

    {"enabled": False, "log_channel": None, "exempt_roles": [], "exempt_channels": [],
     "rules": {"words":    {"on": False, "list": []},
               "links":    {"on": False, "allow": []},          # allow = domains that are fine
               "invites":  {"on": False},                        # invite links to other servers
               "caps":     {"on": False, "percent": 70, "min_length": 10},
               "spam":     {"on": False, "count": 5, "seconds": 6},
               "mentions": {"on": False, "limit": 5}},
     "actions": {"delete": True, "warn": True, "timeout_after": 3, "timeout_minutes": 10,
                 "kick_after": 0, "strike_minutes": 10}}

Every broken rule deletes the message and warns. A "strike" is recorded each time; strikes older than strike_minutes
expire, and reaching timeout_after / kick_after strikes inside that window times the person out / kicks them
(kick_after 0 = never).
"""
import copy
import re
import time
from collections import defaultdict, deque

RULE_NAMES = ("words", "links", "invites", "caps", "spam", "mentions")

DEFAULTS = {
    "enabled": False,
    "log_channel": None,
    "exempt_roles": [],
    "exempt_channels": [],
    "rules": {
        "words": {"on": False, "list": []},
        "links": {"on": False, "allow": []},
        "invites": {"on": False},
        "caps": {"on": False, "percent": 70, "min_length": 10},
        "spam": {"on": False, "count": 5, "seconds": 6},
        "mentions": {"on": False, "limit": 5},
    },
    "actions": {"delete": True, "warn": True, "timeout_after": 3, "timeout_minutes": 10, "kick_after": 0, "strike_minutes": 10},
}

REASONS = {
    "words": "used a blocked word",
    "links": "posted a link",
    "invites": "posted a server invite",
    "caps": "used too many capital letters",
    "spam": "sent messages too fast",
    "mentions": "mentioned too many people",
}

URL_RE = re.compile(r"(?:https?://|www\.)([^\s/<>\"')]+)(?:[^\s<>]*)", re.I)
INVITE_RE = re.compile(r"(?:discord(?:app)?\.(?:gg|com/invite)|yapin\.gg/(?:invite|i)|yapper\.website/(?:invite|i))/[\w-]+", re.I)
MENTION_RE = re.compile(r"<@[!&]?[^>\s]+>|@everyone|@here")


def merged(stored):
    """The stored config on top of the defaults (so new settings appear for old servers), never mutating either."""
    cfg = copy.deepcopy(DEFAULTS)

    def merge(base, extra):
        for k, v in (extra or {}).items():
            if k in base and isinstance(base[k], dict) and isinstance(v, dict):
                merge(base[k], v)
            elif k in base:
                base[k] = v
    merge(cfg, stored if isinstance(stored, dict) else {})
    return cfg


def _domain_ok(domain, allow):
    domain = domain.lower().split(":")[0]
    return any(domain == a or domain.endswith("." + a) for a in (x.lower().lstrip("*.") for x in allow))


def _word_hit(text, words):
    """Whole-word match, so a blocked 'ass' doesn't hit 'class'. Returns the word that matched."""
    lowered = text.lower()
    for w in words:
        w = str(w).strip().lower()
        if not w:
            continue
        if re.search(r"(?<![\w])" + re.escape(w) + r"(?![\w])", lowered):
            return w
    return None


class SpamTracker:
    """Messages per (server, user) in the last few seconds."""

    def __init__(self):
        self._seen = defaultdict(deque)

    def hit(self, key, count, seconds, now=None):
        now = time.time() if now is None else now
        q = self._seen[key]
        q.append(now)
        while q and now - q[0] > seconds:
            q.popleft()
        if len(q) >= count:
            q.clear()
            return True
        return False


class Strikes:
    def __init__(self):
        self._strikes = defaultdict(list)

    def add(self, key, window_seconds, now=None):
        """Record a strike; returns how many are still inside the window."""
        now = time.time() if now is None else now
        kept = [t for t in self._strikes[key] if now - t <= window_seconds]
        kept.append(now)
        self._strikes[key] = kept
        return len(kept)

    def clear(self, key):
        self._strikes.pop(key, None)


def violations(cfg, content, key, spam, now=None):
    """The names of the rules this message breaks, in RULE_NAMES order. `spam` is a SpamTracker."""
    rules, text, hits = cfg["rules"], content or "", []

    if rules["words"]["on"] and _word_hit(text, rules["words"]["list"]):
        hits.append("words")

    if rules["invites"]["on"] and INVITE_RE.search(text):
        hits.append("invites")
    if rules["links"]["on"]:
        for m in URL_RE.finditer(text):
            if rules["invites"]["on"] and INVITE_RE.search(m.group(0)):
                continue    # already reported as an invite
            if not _domain_ok(m.group(1), rules["links"]["allow"]):
                hits.append("links")
                break

    r = rules["caps"]
    if r["on"]:
        letters = [c for c in text if c.isalpha()]
        if len(letters) >= int(r["min_length"]) and sum(c.isupper() for c in letters) * 100 / len(letters) >= int(r["percent"]):
            hits.append("caps")

    r = rules["mentions"]
    if r["on"] and len(MENTION_RE.findall(text)) > int(r["limit"]):
        hits.append("mentions")

    r = rules["spam"]
    if r["on"] and spam.hit(key, int(r["count"]), int(r["seconds"]), now):
        hits.append("spam")
    return hits


def escalation(cfg, strikes):
    """What to do on top of delete+warn after this many strikes: 'kick', 'timeout' or None."""
    a = cfg["actions"]
    if int(a["kick_after"]) and strikes >= int(a["kick_after"]):
        return "kick"
    if int(a["timeout_after"]) and strikes >= int(a["timeout_after"]):
        return "timeout"
    return None


def clean_config(data):
    """Validate/normalise a config posted by the dashboard or built by the command. Returns a full config."""
    cfg = merged(data)

    def boolean(v):
        return v is True or str(v).lower() in ("1", "true", "on", "yes")

    def number(v, lo, hi, default):
        try:
            return max(lo, min(hi, int(v)))
        except (TypeError, ValueError):
            return default

    def strings(v, limit=200):
        if isinstance(v, str):
            v = re.split(r"[\n,]+", v)
        return [s.strip().lower()[:80] for s in (v or []) if isinstance(s, str) and s.strip()][:limit]

    cfg["enabled"] = boolean(cfg["enabled"])
    cfg["log_channel"] = str(cfg["log_channel"]) if cfg["log_channel"] else None
    cfg["exempt_roles"] = [str(x) for x in cfg["exempt_roles"]][:50]
    cfg["exempt_channels"] = [str(x) for x in cfg["exempt_channels"]][:100]
    r = cfg["rules"]
    for name in RULE_NAMES:
        r[name]["on"] = boolean(r[name]["on"])
    r["words"]["list"] = strings(r["words"]["list"])
    r["links"]["allow"] = strings(r["links"]["allow"], 100)
    r["caps"]["percent"] = number(r["caps"]["percent"], 30, 100, 70)
    r["caps"]["min_length"] = number(r["caps"]["min_length"], 1, 200, 10)
    r["spam"]["count"] = number(r["spam"]["count"], 2, 30, 5)
    r["spam"]["seconds"] = number(r["spam"]["seconds"], 1, 120, 6)
    r["mentions"]["limit"] = number(r["mentions"]["limit"], 1, 50, 5)
    a = cfg["actions"]
    a["delete"] = boolean(a["delete"])
    a["warn"] = boolean(a["warn"])
    a["timeout_after"] = number(a["timeout_after"], 0, 50, 3)
    a["timeout_minutes"] = number(a["timeout_minutes"], 1, 40320, 10)
    a["kick_after"] = number(a["kick_after"], 0, 50, 0)
    a["strike_minutes"] = number(a["strike_minutes"], 1, 1440, 10)
    return cfg

"""The public website and the user dashboard.

The website (home, commands, about, counting leaderboard) is rendered here with the bot's own data, so there are no
HTTP round trips: the numbers come straight from the bot's memory and database.

The dashboard lets anyone sign in with Yapin' and see the servers they share with the bot. People who manage a server
(owner, administrator, or a role with manage server) can change its automod, counting and autorole settings; everyone else
gets a read-only list. The bot-owner admin dashboard (/admin) shows up as a tab only for ids in ADMIN_USER_IDS.

Sign-in goes through the Shapes API (the api.shapes.lol repo) running on this machine: it does the OAuth dance with Yapin
and hands the bot a one-time ticket over localhost, so the bot never sees the client secret or a code.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

import jinja2
import requests
from flask import Response, abort, jsonify, redirect, render_template, request, send_from_directory, session

HERE = os.path.dirname(os.path.abspath(__file__))
SITE_DIR = os.path.join(HERE, "site")
PAGE = 10


def register(m):
    """Attach the routes to m.app. `m` is the main module (bot, caches, databases, validators)."""
    app, yshim, bot = m.app, m.yshim, m.bot
    app.jinja_loader = jinja2.ChoiceLoader([app.jinja_loader, jinja2.FileSystemLoader(os.path.join(SITE_DIR, "templates"))])
    app.config.setdefault("SESSION_COOKIE_SAMESITE", "Lax")
    app.config.setdefault("SESSION_COOKIE_HTTPONLY", True)
    api_public = os.environ.get("API_PUBLIC_URL", "https://api.shapes-yapper.oriaxen.com").rstrip("/")

    # ------------------------------------------------------------------ numbers for the public pages (no HTTP)

    def stats():
        if not m.bot_ready:
            return None, None
        return len(m.cached("servers", 15, bot.servers)), m.cached("total_members", 30, m.total_members)

    def leaderboard():
        return [{"name": r["name"], "current": r["current"], "highest": r["highest"]} for r in m.leaderboard_rows()] if m.bot_ready else []

    def commands():
        """The command list: it only changes when the bot restarts, so it is built once per five minutes at most."""
        return m.cached("commands", 300, lambda: [{"name": n, "description": s.description, "usage": s.usage} for n, s in sorted(yshim.tree.commands.items())])

    def paged(items):
        pages = max(1, -(-len(items) // PAGE))
        page = min(max(request.args.get("page", 1, type=int), 1), pages)
        return items[(page - 1) * PAGE: page * PAGE], page, pages

    @app.route("/assets/<path:name>")
    def site_assets(name):
        return send_from_directory(os.path.join(SITE_DIR, "assets"), name, max_age=3600)

    @app.route("/", methods=["GET"])
    @m.rate_limited(120, 60)
    def home():
        servers, users = stats()
        return render_template("home.html", users=users, servers=servers, top=leaderboard()[:3])

    @app.route("/about")
    @m.rate_limited(120, 60)
    def about():
        return render_template("about.html")

    @app.route("/add")
    def add_bot():
        return redirect(os.environ.get("BOT_INVITE_URL", "https://yapin.gg/bot/5e872d4a-1892-43ce-aaae-40ecd7acd768"), 302)

    @app.route("/support")
    def support():
        return redirect(os.environ.get("SUPPORT_URL", "https://www.yapin.gg/invite/shapes"), 302)

    @app.route("/counting/leaderboard/")
    @app.route("/counting")
    @app.route("/leaderboard")
    @m.rate_limited(120, 60)
    def leaderboard_page():
        items, page, pages = paged(leaderboard())
        return render_template("leaderboard.html", items=items, page=page, pages=pages)

    @app.route("/partners")
    @m.rate_limited(120, 60)
    def partners_page():
        return render_template("partners.html", partners=m.partner_cards() if m.bot_ready else [])

    @app.route("/api/partners")
    @m.rate_limited(60, 60)
    def partners_json():
        return jsonify({"success": True, "partners": m.partner_cards() if m.bot_ready else []})

    @app.route("/commands", methods=["GET"])
    @m.rate_limited(120, 60)
    def send_commands():
        """A web page for browsers; the JSON the old site read for anything else (so existing callers keep working)."""
        if "text/html" in request.headers.get("Accept", "") and request.args.get("format") != "json":
            items, page, pages = paged(commands())
            return render_template("commands.html", items=items, page=page, pages=pages)
        out = {c["name"]: {"name": c["name"], "description": c["description"], "usage": c["usage"],
                           "options": [p.name for p in yshim.tree.commands[c["name"]].params], "type": "slash_command"} for c in commands()}
        return jsonify({"Commands": out, "success": True}), 200

    # ------------------------------------------------------------------ who the person is

    def current_user():
        u = session.get("yuser")
        return u if isinstance(u, dict) and u.get("id") else None

    def login_required(f):
        def wrapped(*a, **k):
            if not current_user():
                return jsonify({"error": "login required"}), 401
            return f(*a, **k)
        wrapped.__name__ = f.__name__
        return wrapped

    @app.before_request
    def _same_site_writes():
        """Dashboard writes must come from this site: JSON bodies, and an Origin (when sent) that matches the host."""
        if request.method in ("POST", "PUT", "DELETE", "PATCH") and request.path.startswith("/me/"):
            origin = request.headers.get("Origin")
            if origin and urllib.parse.urlparse(origin).netloc != request.host:
                abort(403)
            if not request.is_json:
                abort(415)

    @app.route("/dashboard")
    def user_dashboard():
        user = current_user()
        return Response(m._web_file("dash.html").replace("__BOOT__", jsonify(_boot(user)).get_data(as_text=True).replace("<", "\\u003c")),
                        mimetype="text/html")

    # --- Login: through the Shapes API when it's up, otherwise OAuth2 done right here ---
    local = {
        "id": os.environ.get("YAPIN_CLIENT_ID", ""), "secret": os.environ.get("YAPIN_CLIENT_SECRET", ""),
        "authorize": os.environ.get("YAPIN_AUTHORIZE_URL", "https://www.yapin.gg/oauth/authorize"),
        "token": os.environ.get("YAPIN_TOKEN_URL", "https://www.yapin.gg/api/oauth/token"),
        "me": os.environ.get("YAPIN_USERINFO_URL", "https://www.yapin.gg/api/oauth/me"),
        "scope": os.environ.get("YAPIN_SCOPE", "identify"),
    }
    if local["me"].rstrip("/").endswith("/userinfo"):          # Yapin' calls it /me
        local["me"] = local["me"].rstrip("/")[:-len("userinfo")] + "me"
    LOGIN_COOKIE, STATE_TTL = "yapin_login_local", 3600
    used_states = {}
    health = {"at": 0.0, "up": False}

    def local_ready():
        return bool(local["id"] and local["secret"])

    def api_up():
        """Is the Shapes API answering? Checked quickly and remembered for 15 seconds so a dead API never slows logins down."""
        if not m.api_ready():
            return False
        if time.time() - health["at"] > 15:
            result = m.shapes_api("GET", "/internal/health", timeout=3)
            health.update(at=time.time(), up=bool(result and result.get("success") and result.get("yapin_login", True)))
        return health["up"]

    def _callback_url(path):
        return f"{(m.PUBLIC_URL or request.host_url).rstrip('/')}{path}"

    def _sign(body):
        return hmac.new(str(app.secret_key).encode(), ("dash-login." + body).encode(), hashlib.sha256).hexdigest()[:32]

    def _make_state():
        body = base64.urlsafe_b64encode(json.dumps({"n": secrets.token_hex(8), "e": int(time.time()) + STATE_TTL}).encode()).decode().rstrip("=")
        return f"{body}.{_sign(body)}"

    def _read_state(state):
        """True for a state we made, that hasn't expired and hasn't been used."""
        try:
            body, sig = state.rsplit(".", 1)
            if not hmac.compare_digest(sig, _sign(body)):
                return False
            data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        except (ValueError, TypeError):
            return False
        now = time.time()
        for k in [k for k, t in used_states.items() if t < now]:
            used_states.pop(k, None)
        return data.get("e", 0) >= now and state not in used_states

    def _unwrap(data):
        if not isinstance(data, dict):
            return {}
        for key in ("data", "user", "result"):
            if isinstance(data.get(key), dict):
                return {**data, **data[key]}
        return data

    def _json(r):
        try:
            return _unwrap(r.json())
        except ValueError:
            return {}

    def local_token(code):
        """Trade the code for a token: client credentials in the body first, then HTTP Basic. No timeout: Yapin' can be slow."""
        base = {"grant_type": "authorization_code", "code": code, "redirect_uri": _callback_url("/dashboard/oauth/callback")}
        last = ""
        for kwargs in ({"data": {**base, "client_id": local["id"], "client_secret": local["secret"]}},
                       {"data": {**base, "client_id": local["id"]}, "auth": (local["id"], local["secret"])}):
            r = requests.post(local["token"], headers={"Accept": "application/json"}, timeout=None, **kwargs)
            data = _json(r)
            token = data.get("access_token") or data.get("token") or data.get("accessToken")
            if r.ok and isinstance(token, str) and token:
                return token
            last = f"{r.status_code} {str(data.get('error_description') or data.get('error') or r.text)[:120]}"
            if r.status_code not in (400, 401):
                break
        raise ValueError(f"token step failed ({last})")

    def local_profile(token):
        r = requests.get(local["me"], headers={"Authorization": f"Bearer {token}", "Accept": "application/json"}, timeout=None)
        info = _json(r)
        if not r.ok or not (info.get("id") or info.get("sub") or info.get("user_id")):
            raise ValueError(f"profile step failed ({r.status_code})")
        return {"id": str(info.get("id") or info.get("sub") or info.get("user_id")),
                "username": info.get("username") or info.get("preferred_username"), "display_name": info.get("display_name") or info.get("name"),
                "avatar_path": info.get("avatar_path") or info.get("avatar_url") or info.get("picture")}

    def _boot(user):
        return {"user": user, "staff": m.is_staff_user(user["id"]) if user else False, "owner": m.is_bot_owner(user["id"]) if user else False,
                "login": bool(local_ready() or api_up()), "bot_ready": bool(m.bot_ready), "colors": m.color_choices(),
                "color": (m.embedDB.get(user["id"]) if user else None),
                "commit": {"sha": m.COMMIT[0], "title": m.COMMIT[1]}}

    def _fail(message, status=400):
        return Response("<!doctype html><meta charset=utf-8><body style='font-family:system-ui;max-width:28rem;margin:4rem auto;background:#0b0b0f;color:#eee'>"
                        f"<h2>Login failed</h2><p>{message} <a style='color:#9ab' href='/dashboard/login'>Try again</a>.</p>", status=status, mimetype="text/html")

    def _sign_in(info):
        uid = str(info["id"])
        person = m._person(dict(yshim.fetch_user(uid))) if m.bot_ready else {}
        session.clear()
        session["yuser"] = {"id": uid, "name": info.get("username") or person.get("name") or uid,
                            "display": info.get("display_name") or person.get("display") or info.get("username") or uid,
                            "avatar": person.get("avatar") or yshim._asset_url(info.get("avatar_path"), "avatars") or m.default_avatar_url(uid, info.get("username") or uid)}
        session.permanent = True
        return redirect("/dashboard")

    @app.route("/dashboard/login")
    def dashboard_login():
        if request.args.get("local") != "1" and api_up():
            back = _callback_url("/dashboard/callback")
            return redirect(f"{api_public}/auth/yapin/login?return={urllib.parse.quote(back, safe='')}", 302)
        if not local_ready():
            return Response("Login isn't available: the Shapes API is down and this bot has no YAPIN_CLIENT_ID / YAPIN_CLIENT_SECRET to log you in itself.",
                            status=503, mimetype="text/plain")
        return redirect("/dashboard/oauth/login", 302)

    @app.route("/dashboard/oauth/login")
    def dashboard_oauth_login():
        if not local_ready():
            return Response("Login isn't set up on this server.", status=503, mimetype="text/plain")
        state = _make_state()
        query = urllib.parse.urlencode({"client_id": local["id"], "response_type": "code", "scope": local["scope"], "state": state,
                                        "redirect_uri": _callback_url("/dashboard/oauth/callback")})
        resp = redirect(f"{local['authorize']}?{query}", 302)
        resp.set_cookie(LOGIN_COOKIE, state, max_age=STATE_TTL, httponly=True, samesite="Lax", secure=(m.PUBLIC_URL or request.host_url).startswith("https"))
        return resp

    @app.route("/dashboard/oauth/callback")
    def dashboard_oauth_callback():
        state, cookie = request.args.get("state", ""), request.cookies.get(LOGIN_COOKIE, "")
        if request.args.get("error"):
            return _fail("You didn't approve the login.")
        if not state or not cookie or not hmac.compare_digest(state, cookie) or not _read_state(state):
            return _fail("That login link isn't valid any more (expired, already used, or started in another browser).")
        used_states[state] = time.time() + STATE_TTL
        code = request.args.get("code", "")
        if not code:
            return _fail("Yapin' didn't send a login code.")
        try:
            info = local_profile(local_token(code))
        except (requests.RequestException, ValueError) as e:
            print(f"[dashboard login] failed: {e}")
            return _fail(f"Yapin' didn't confirm who you are ({str(e)[:60]}).")
        resp = _sign_in(info)
        resp.delete_cookie(LOGIN_COOKIE)
        return resp

    @app.route("/dashboard/callback")
    def dashboard_callback():
        ticket = request.args.get("ticket", "")
        data = m.shapes_api("GET", f"/internal/yapin/ticket/{urllib.parse.quote(ticket, safe='')}") if ticket else None
        info = (data or {}).get("user")
        if not info or not info.get("id"):
            if local_ready():       # the API may have gone down mid-login: do it here instead
                return redirect("/dashboard/oauth/login", 302)
            return _fail("The login expired.")
        return _sign_in(info)

    @app.route("/dashboard/logout")
    def dashboard_logout():
        session.clear()
        return redirect("/dashboard")

    @app.route("/me")
    def me():
        user = current_user()
        return jsonify(_boot(user))

    # ------------------------------------------------------------------ what they can reach

    def perm(sid, uid):
        """(is_member, can_manage) for one person in one server: the owner, an administrator, or a manage-server role.
        Strict on purpose: unlike the in-chat check, a role that is merely named 'Moderator' doesn't count here.
        Bot owners and bot admins can open every server the bot is in."""
        if m.is_staff_user(uid):
            return True, True
        try:
            member = dict(bot.member(sid, uid))
        except Exception:
            return False, False
        try:
            server = dict(m.cached(f"server:{sid}", 30, lambda: bot.server(sid)))
            owner = server.get("owner_id")
            if owner is not None and str(owner.get("id") if isinstance(owner, dict) else owner) == str(uid):
                return True, True
            if yshim._has_admin(member) or (isinstance(member.get("user"), dict) and yshim._has_admin(member["user"])):
                return True, True
            mine = {str(r.get("id") if isinstance(r, dict) else r) for r in (member.get("roles") or member.get("role_ids") or [])}
            if mine:
                for r in m.cached(f"roles:{sid}", 60, lambda: [dict(x) for x in bot.roles(sid)]):
                    if str(r.get("id")) in mine and yshim._has_admin(r):
                        return True, True
        except Exception as e:
            print(f"[dashboard] permission check failed for {uid} in {sid}: {e}")
        return True, False

    def counting_brief(sid):
        d = m.countingDB.get(sid) or {}
        if not d.get("enabled"):
            return None
        return {"current": d.get("number", 0), "highest": d.get("highestnumber", 0)}

    def servers_for(uid):
        servers = list(m.cached("servers", 15, bot.servers))

        def one(s):
            sid = str(s.get("id"))
            member, manage = perm(sid, uid)
            if not member:
                return None
            return {"id": sid, "name": s.get("name") or sid, "icon": m._icon_of(s),
                    "members": yshim._first(s, "member_count", "members_count", "memberCount"),
                    "manage": manage, "staff_access": manage and m.is_staff_user(uid), "counting": counting_brief(sid)}
        with ThreadPoolExecutor(max_workers=8) as pool:
            rows = [r for r in pool.map(one, servers) if r]
        rows.sort(key=lambda r: r["name"].lower())
        return rows

    @app.route("/me/servers")
    @login_required
    def me_servers():
        if not m.bot_ready:
            return jsonify({"error": "The bot is still starting."}), 503
        uid = current_user()["id"]
        rows = m.cached(f"mine:{uid}", 60, lambda: servers_for(uid))
        return jsonify({"manage": [r for r in rows if r["manage"]], "other": [r for r in rows if not r["manage"]]})

    def require_manage(sid):
        uid = current_user()["id"]
        member, manage = perm(sid, uid)          # fresh, so a demotion takes effect at once
        if not manage:
            abort(403 if member else 404)

    def server_view(sid):
        server = dict(m.cached(f"server:{sid}", 30, lambda: bot.server(sid)))
        groups = m._group_channels([dict(c) for c in bot.channels(sid)])
        text = [{"id": ch["id"], "name": ch["name"], "category": g["name"]} for g in groups for ch in g["channels"] if m._is_text(ch["type"])]
        roles = [{"id": str(r.get("id")), "name": r.get("name") or str(r.get("id")), "admin": bool(yshim._has_admin(dict(r))), "color": r.get("color") if isinstance(r.get("color"), str) else None}
                 for r in bot.roles(sid) if str(r.get("name", "")).lower() != "@everyone"]
        counting = {**m.default_counting(), **(m.countingDB.get(sid) or {})}
        counting["emojis"] = m.counting_emojis(counting)
        counting["last_name"] = (yshim.fetch_user(counting["lastcounter"]).get("username") if counting.get("lastcounter") else None)
        return {"server": {"id": sid, "name": server.get("name") or sid, "icon": m._icon_of(server),
                           "members": yshim._first(server, "member_count", "members_count", "memberCount")},
                "channels": text, "roles": roles, "automod": m.get_automod(sid), "autorole": m.get_autorole(sid), "counting": counting,
                "owner": m.is_server_owner(sid, current_user()["id"]) or m.is_bot_owner(current_user()["id"]),
                "staff": m.is_staff_user(current_user()["id"]), "bot_owner": m.is_bot_owner(current_user()["id"])}

    @app.route("/me/servers/<sid>/info")
    @login_required
    def me_info(sid):
        """What /serverinfo shows: open to everyone who is in the server."""
        member, _ = perm(sid, current_user()["id"])
        if not member:
            abort(404)
        try:
            data = m.cached(f"summary:{sid}", 60, lambda: m.server_summary(sid))
        except Exception as e:
            return jsonify({"error": f"Couldn't load that server: {e}"}), 502
        owner = None
        if data["owner"]:
            try:
                owner = {k: m._person(dict(bot.member(sid, data["owner"])))[k] for k in ("id", "name", "display", "avatar")}
            except Exception:
                owner = {"id": data["owner"], "name": data["owner"], "display": data["owner"], "avatar": m.default_avatar_url(data["owner"])}
        return jsonify({**data, "owner": owner, "extra": [{"label": k, "value": v} for k, v in data["extra"]], "counting": counting_brief(sid)})

    @app.route("/me/servers/<sid>")
    @login_required
    def me_server(sid):
        require_manage(sid)
        try:
            return jsonify(server_view(sid))
        except Exception as e:
            return jsonify({"error": f"Couldn't load that server: {e}"}), 502

    def audit(sid, what, before, after):
        m._audit(f"dashboard:{what}", sid, f"edit by {current_user()['id']}", before, after)

    @app.route("/me/servers/<sid>/automod", methods=["POST"])
    @login_required
    def me_automod(sid):
        require_manage(sid)
        before = m.automodDB.get(sid)
        cfg = m.save_automod(sid, request.get_json(silent=True) or {})
        audit(sid, "automod", before, cfg)
        return jsonify(cfg)

    @app.route("/me/servers/<sid>/autorole", methods=["POST"])
    @login_required
    def me_autorole(sid):
        require_manage(sid)
        body = request.get_json(silent=True) or {}
        try:
            value = m._vautorole({"role_id": body.get("role_id"), "enabled": body.get("enabled"), "include_bots": body.get("include_bots", True),
                                  "exempt_users": m._user_ids(body["exempt_users"]) if isinstance(body.get("exempt_users"), str) else body.get("exempt_users", [])})
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        before = m.autoroleDB.get(sid)
        out = m.save_autorole(sid, value)
        audit(sid, "autorole", before, out)
        return jsonify(out)

    @app.route("/me/servers/<sid>/counting", methods=["POST"])
    @login_required
    def me_counting(sid):
        """Managers change the settings (on/off, channel, emoji). The number and highest number can only be edited by bot owners
        (not bot admins, not server owners); warnings and the ban flag stay out of reach."""
        require_manage(sid)
        body = request.get_json(silent=True) or {}
        before = m.countingDB.get(sid)
        data = {**m.default_counting(), **(before or {})}
        if "enabled" in body:
            if not isinstance(body["enabled"], bool):
                return jsonify({"error": '"enabled" must be true or false.'}), 400
            data["enabled"] = body["enabled"]
        if "channel" in body:
            channel = str(body["channel"] or "")
            if channel and channel not in {str(c.get("id")) for c in bot.channels(sid)}:
                return jsonify({"error": "That channel isn't in this server."}), 400
            data["channel"] = channel or None
        if isinstance(body.get("emojis"), dict):
            data["emojis"] = {**m.counting_emojis(data), **{k: v for k, v in
                              ((k, m.clean_emoji(v)) for k, v in body["emojis"].items() if k in m.COUNTING_EMOJI_DEFAULT) if v}}
        if body.get("reset") is True:
            data.update(number=0, warnings=0, lastcounter=None)
        if "number" in body or "highestnumber" in body:
            if not m.is_bot_owner(current_user()["id"]):
                return jsonify({"error": "Only the bot owner can change the counting numbers."}), 403
            for key in ("number", "highestnumber"):
                if key in body:
                    value = body[key]
                    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10 ** 12:
                        return jsonify({"error": f'"{key}" must be a whole number from 0 to 1,000,000,000,000.'}), 400
                    data[key] = value
            if "number" in body:
                data["lastcounter"] = None          # whoever counts next continues from the new number
            data["highestnumber"] = max(data.get("highestnumber", 0), data.get("number", 0))
        m.countingDB.set(sid, data)
        audit(sid, "counting", before, data)
        data["emojis"] = m.counting_emojis(data)
        return jsonify(data)

    # ------------------------------------------------------------------ speak as the bot, DM members, and the log

    _sent = {}

    def too_fast(kind, limit, window):
        """True once this person has done `kind` more than `limit` times inside `window` seconds."""
        key, now = (kind, current_user()["id"]), time.time()
        recent = [t for t in _sent.get(key, []) if now - t < window] + [now]
        _sent[key] = recent
        return len(recent) > limit

    def text_channels(sid):
        return {str(c.get("id")): c.get("name") for c in bot.channels(sid) if m._is_text(str(yshim._first(c, "type", "kind") or "text").lower())}

    @app.route("/me/servers/<sid>/say", methods=["POST"])
    @login_required
    def me_say(sid):
        """Send a message as Shapes into a channel. It always says who it is from; only bot admins can hide that."""
        require_manage(sid)
        body = request.get_json(silent=True) or {}
        text, channel = str(body.get("message", "")).strip(), str(body.get("channel", ""))
        if not text:
            return jsonify({"error": "Write a message first."}), 400
        if len(text) > 1800:
            return jsonify({"error": "That's too long (1800 characters at most)."}), 400
        chans = text_channels(sid)
        if channel not in chans:
            return jsonify({"error": "Pick a text channel from this server."}), 400
        try:
            image = m._valid_image(body.get("image_url"))
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        if too_fast("say", 15, 60):
            return jsonify({"error": "Slow down a little, then try again."}), 429
        try:
            color = m.resolve_color(body.get("color"), body.get("custom_color"), current_user()["id"]) if (body.get("color") or body.get("hidden") is not True) else None
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        try:
            footer = m.send_as_bot(channel, text, image, str(body.get("title", "")).strip() or None, hidden=body.get("hidden") is True, color=color)
        except PermissionError as e:
            return jsonify({"error": str(e)}), 403
        except Exception as e:
            return jsonify({"error": f"Yapin' wouldn't take it: {e}"}), 502
        m.log_action("send", ("(hidden sender) " if body.get("hidden") is True else "") + text[:200], server_id=sid, target=f"#{chans[channel]}")
        return jsonify({"ok": True, "footer": footer})

    @app.route("/me/servers/<sid>/members")
    @login_required
    def me_members(sid):
        """People in this server, to pick who to DM (search by name)."""
        require_manage(sid)
        q = request.args.get("q", "").strip().lower().lstrip("@")
        people = m.cached(f"people:{sid}", 60, lambda: m._all_members(sid, limit=3000))
        hits = [p for p in people if not p["bot"] and (not q or q in p["name"].lower() or q in p["display"].lower() or q == p["id"])]
        return jsonify({"members": [{k: p[k] for k in ("id", "name", "display", "avatar")} for p in hits[:25]], "total": len(people)})

    @app.route("/me/servers/<sid>/dm", methods=["POST"])
    @login_required
    def me_dm(sid):
        """DM chosen members of this server as Shapes. The message says who sent it and from which server."""
        require_manage(sid)
        body = request.get_json(silent=True) or {}
        text = str(body.get("message", "")).strip()
        ids = list(dict.fromkeys(str(i).strip() for i in (body.get("user_ids") or []) if str(i).strip()))
        if not text or len(text) > 1500:
            return jsonify({"error": "Write a message (1500 characters at most)."}), 400
        unlimited = m.is_server_owner(sid, current_user()["id"]) or m.is_bot_owner(current_user()["id"])
        people = {p["id"]: p for p in m.cached(f"people:{sid}", 60, lambda: m._all_members(sid, limit=10 ** 9)) if not p["bot"]}
        if body.get("everyone") is True:                  # the whole server, however big
            if not unlimited:
                return jsonify({"error": "Only the server owner can DM the whole server."}), 403
            ids = list(people)
        if not ids or (len(ids) > 50 and not unlimited):
            return jsonify({"error": "Pick between 1 and 50 members." if not unlimited else "Pick at least one member."}), 400
        if not unlimited and too_fast("dm", 10, 3600):
            return jsonify({"error": "You've sent a lot of DMs this hour. Try again later."}), 429
        if [i for i in ids if i not in people]:
            return jsonify({"error": "Some of those people aren't members of this server."}), 400
        server = dict(m.cached(f"server:{sid}", 30, lambda: bot.server(sid)))
        hidden = body.get("hidden") is True
        if hidden and not m.is_staff_user(current_user()["id"]):
            return jsonify({"error": "Only bot admins can hide who a message is from."}), 403
        try:
            color = m.resolve_color(body.get("color"), body.get("custom_color"), current_user()["id"]) if (body.get("color") or body.get("hidden") is not True) else None
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        job = m.start_dm_job(ids, text, where=server.get("name") or None, hidden=hidden, color=color)
        m.log_action("dm", ("(hidden sender) " if hidden else "") + text[:200], server_id=sid, target=("whole server: " if body.get("everyone") else "") + f"{len(ids)} members: " + ", ".join(ids[:8]))
        return jsonify({"job": job, "total": len(ids)})

    @app.route("/me/servers/<sid>/dm/<job>")
    @login_required
    def me_dm_job(sid, job):
        require_manage(sid)
        status = m.dm_job_status(job, current_user()["id"])
        return (jsonify(status), 200) if status else (jsonify({"error": "No such job."}), 404)

    # ------------------------------------------------------------------ giving and taking roles

    @app.route("/me/servers/<sid>/member/<uid>")
    @login_required
    def me_member(sid, uid):
        require_manage(sid)
        try:
            person = m._person(dict(bot.member(sid, uid)))
        except Exception:
            return jsonify({"error": "That person isn't in this server."}), 404
        return jsonify({k: person[k] for k in ("id", "name", "display", "avatar", "roles", "bot")})

    @app.route("/me/servers/<sid>/roles", methods=["POST"])
    @login_required
    def me_roles(sid):
        """Give or take a role. Roles that can manage the server stay with the server owner (and bot staff), so nobody can
        promote themselves past their own rank."""
        require_manage(sid)
        body = request.get_json(silent=True) or {}
        target, role_id, action = str(body.get("user_id", "")), str(body.get("role_id", "")), body.get("action")
        if action not in ("add", "remove"):
            return jsonify({"error": '"action" must be add or remove.'}), 400
        roles = {str(r.get("id")): dict(r) for r in m.cached(f"roles:{sid}", 60, lambda: [dict(x) for x in bot.roles(sid)])}
        role = roles.get(role_id)
        if not role:
            return jsonify({"error": "That role isn't in this server."}), 400
        me = current_user()["id"]
        powerful = m.is_server_owner(sid, me) or m.is_staff_user(me)
        if not powerful and yshim._has_admin(role):
            return jsonify({"error": "Only the server owner can hand out roles that manage the server."}), 403
        if str(role.get("name", "")).lower() == "@everyone" or role.get("is_default") is True:
            return jsonify({"error": "Everyone already has that role."}), 400
        try:
            person = m._person(dict(bot.member(sid, target)))
        except Exception:
            return jsonify({"error": "That person isn't in this server."}), 404
        try:
            (bot.add_role if action == "add" else bot.remove_role)(sid, target, role_id)
        except Exception as e:
            return jsonify({"error": f"Yapin' wouldn't do that: {e} (the role may be above Shapes' own role)."}), 502
        m.log_action("role", f"{'gave' if action == 'add' else 'took'} {role.get('name')}", server_id=sid, target=f"@{person['name']}")
        now = set(person["roles"]) | {role_id} if action == "add" else set(person["roles"]) - {role_id}
        return jsonify({"ok": True, "roles": sorted(now)})

    # ------------------------------------------------------------------ their own embed colour

    # ---- Roblox accounts (the API keeps them; this is the page for linking, auto-verify and removing) ----

    @app.route("/roblox")
    def roblox_page():
        if not current_user():
            return redirect("/dashboard/login")
        return Response(m._web_file("roblox.html"), mimetype="text/html")

    @app.route("/me/roblox")
    @login_required
    def me_roblox():
        d = m.shapes_api("GET", f"/internal/person/yapin/{current_user()['id']}")
        if not d or not d.get("success"):
            return jsonify({"accounts": [], "max": 50, "error": "The linking service isn't answering right now."})
        return jsonify({"accounts": d.get("accounts", []), "max": d.get("max", 50), "discord_id": d.get("discord_id") or None})

    @app.route("/me/roblox/discord/link", methods=["POST"])
    @login_required
    def me_discord_link():
        """One-time Discord OAuth2 link (the API proves the person owns the Discord account before attaching it)."""
        if not _json_only():
            return jsonify({"success": False}), 400
        u = current_user()
        d = m.shapes_api("POST", "/internal/discord/start", {"user_id": u["id"], "name": u.get("name", "")})
        return jsonify(d or {"success": False, "data": "The linking service isn't answering right now."})

    @app.route("/me/roblox/discord/unlink", methods=["POST"])
    @login_required
    def me_discord_unlink():
        if not _json_only():
            return jsonify({"success": False}), 400
        return jsonify(m.shapes_api("DELETE", f"/internal/person/yapin/{current_user()['id']}/discord") or {"success": False})

    def _json_only():
        return request.is_json       # a cross-site form can't send JSON, so this doubles as a CSRF guard

    @app.route("/me/roblox/link", methods=["POST"])
    @login_required
    def me_roblox_link():
        if not _json_only():
            return jsonify({"success": False}), 400
        u = current_user()
        d = m.shapes_api("POST", "/internal/roblox/start", {"platform": "yapin", "user_id": u["id"], "name": u.get("name", "")})
        return jsonify(d or {"success": False, "data": "The linking service isn't answering right now."})

    @app.route("/me/roblox/auto", methods=["POST"])
    @login_required
    def me_roblox_auto():
        if not _json_only():
            return jsonify({"success": False}), 400
        rid = (request.get_json(silent=True) or {}).get("roblox_id")
        return jsonify(m.shapes_api("POST", f"/internal/person/yapin/{current_user()['id']}/auto", {"roblox_id": rid}) or {"success": False})

    @app.route("/me/roblox/remove", methods=["POST"])
    @login_required
    def me_roblox_remove():
        if not _json_only():
            return jsonify({"success": False}), 400
        rid = str((request.get_json(silent=True) or {}).get("roblox_id") or "")
        return jsonify(m.shapes_api("DELETE", f"/internal/person/yapin/{current_user()['id']}/roblox/{urllib.parse.quote(rid, safe='')}") or {"success": False})

    @app.route("/me/settings", methods=["GET", "POST"])
    @login_required
    def me_settings():
        uid = current_user()["id"]
        if request.method == "POST":
            body = request.get_json(silent=True) or {}
            try:
                value = m.resolve_color(body.get("color") if body.get("color") != "mine" else "Blue", body.get("custom_color"), uid)
            except ValueError as e:
                return jsonify({"error": str(e)}), 400
            m.embedDB.set(uid, value)
        saved = m.embedDB.get(uid)
        return jsonify({"color": "#%06x" % saved if isinstance(saved, int) else None, "choices": m.color_choices()})

    @app.route("/me/servers/<sid>/log")
    @login_required
    def me_log(sid):
        require_manage(sid)
        return jsonify({"actions": m.action_rows(sid, 100)})

    # ------------------------------------------------------------------ command log (owner sets it up, managers can read it)

    def cmdlog_view(sid):
        cfg = m.get_cmdlog(sid)
        return {"config": cfg, "channel_public": bool(cfg["channel"]) and m.channel_is_public(sid, cfg["channel"]),
                "owner": m.is_server_owner(sid, current_user()["id"]), "rows": m.cmdlog_rows(sid, 100)}

    @app.route("/me/servers/<sid>/cmdlog", methods=["GET", "POST"])
    @login_required
    def me_cmdlog(sid):
        require_manage(sid)
        if request.method == "GET":
            return jsonify(cmdlog_view(sid))
        if not m.is_server_owner(sid, current_user()["id"]):
            return jsonify({"error": "Only the server owner can change command logging."}), 403
        body = request.get_json(silent=True) or {}
        channel = str(body["channel"]) if body.get("channel") else None
        if channel and channel not in text_channels(sid):
            return jsonify({"error": "Pick a text channel from this server."}), 400
        before = m.get_cmdlog(sid)
        cfg, warning = m.cmdlog_configure(sid, channel_id=channel, enabled=body.get("enabled") if isinstance(body.get("enabled"), bool) else None,
                                          confirm_public=body.get("confirm_public") is True)
        if warning:
            return jsonify({"warning": warning, "needs_confirm": True}), 409
        m.log_action("settings", f"command log {'on' if cfg['enabled'] else 'off'}" + (f" in #{text_channels(sid).get(cfg['channel'])}" if cfg["channel"] else ""), server_id=sid)
        return jsonify(cmdlog_view(sid))

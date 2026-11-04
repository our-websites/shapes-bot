"""The public website and the user dashboard.

The website (home, commands, about, counting leaderboard) is rendered here with the bot's own data, so there are no
HTTP round trips: the numbers come straight from the bot's memory and database.

The dashboard lets anyone sign in with Yapin' and see the servers they share with the bot. People who manage a server
(owner, administrator, or a role with manage server) can change its automod, counting and autorole settings; everyone else
gets a read-only list. The bot-owner admin dashboard (/admin) shows up as a tab only for ids in ADMIN_USER_IDS.

Sign-in goes through the Shapes API (the api.shapes.lol repo) running on this machine: it does the OAuth dance with Yapin
and hands the bot a one-time ticket over localhost, so the bot never sees the client secret or a code.
"""
import os
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

import jinja2
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

    def _boot(user):
        return {"user": user, "staff": m.is_staff_user(user["id"]) if user else False,
                "login": bool(m.api_ready()), "bot_ready": bool(m.bot_ready)}

    @app.route("/dashboard/login")
    def dashboard_login():
        if not m.api_ready():
            return Response("Login isn't set up: the bot needs SHAPES_API_TOKEN so it can reach the Shapes API.", status=503, mimetype="text/plain")
        back = f"{m.PUBLIC_URL.rstrip('/')}/dashboard/callback" if m.PUBLIC_URL else f"{request.host_url.rstrip('/')}/dashboard/callback"
        return redirect(f"{api_public}/auth/yapin/login?return={urllib.parse.quote(back, safe='')}", 302)

    @app.route("/dashboard/callback")
    def dashboard_callback():
        ticket = request.args.get("ticket", "")
        data = m.shapes_api("GET", f"/internal/yapin/ticket/{urllib.parse.quote(ticket, safe='')}") if ticket else None
        info = (data or {}).get("user")
        if not info or not info.get("id"):
            return Response("<!doctype html><meta charset=utf-8><body style='font-family:system-ui;max-width:28rem;margin:4rem auto'>"
                            "<h2>Login failed</h2><p>The login expired. <a href='/dashboard/login'>Try again</a>.</p>", status=400, mimetype="text/html")
        uid = str(info["id"])
        person = m._person(dict(yshim.fetch_user(uid))) if m.bot_ready else {}
        session.clear()
        session["yuser"] = {"id": uid, "name": info.get("username") or person.get("name") or uid,
                            "display": info.get("display_name") or person.get("display") or info.get("username") or uid,
                            "avatar": person.get("avatar") or yshim._asset_url(info.get("avatar_path"), "avatars")}
        session.permanent = True
        return redirect("/dashboard")

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
        Strict on purpose: unlike the in-chat check, a role that is merely named 'Moderator' doesn't count here."""
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
                    "manage": manage, "counting": counting_brief(sid)}
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
        roles = [{"id": str(r.get("id")), "name": r.get("name") or str(r.get("id"))} for r in bot.roles(sid)]
        counting = {**m.default_counting(), **(m.countingDB.get(sid) or {})}
        counting["emojis"] = m.counting_emojis(counting)
        counting["last_name"] = (yshim.fetch_user(counting["lastcounter"]).get("username") if counting.get("lastcounter") else None)
        return {"server": {"id": sid, "name": server.get("name") or sid, "icon": m._icon_of(server),
                           "members": yshim._first(server, "member_count", "members_count", "memberCount")},
                "channels": text, "roles": roles, "automod": m.get_automod(sid), "autorole": m.get_autorole(sid), "counting": counting}

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
        """Owners change the settings (on/off, channel, emoji). The number, warnings and ban flag aren't theirs to edit."""
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
        m.countingDB.set(sid, data)
        audit(sid, "counting", before, data)
        data["emojis"] = m.counting_emojis(data)
        return jsonify(data)

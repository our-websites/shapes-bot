"""The member dashboard: sign in with Discord, then manage the servers you run (counting) and your own Roblox accounts."""
import urllib.parse

import discord
from flask import Response, abort, jsonify, redirect, request, session

MANAGE = ("administrator", "manage_guild")


def register(m):
    """Attach the routes to m.app. `m` is the main module (bot, counting data, the Shapes API helpers)."""
    app, bot = m.app, m.bot

    def user():
        u = session.get("duser")
        return u if isinstance(u, dict) and u.get("id") else None

    def need_login(f):
        def wrapped(*a, **k):
            if not user():
                return jsonify({"error": "Sign in with Discord first."}), 401
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

    def can_manage(guild, uid):
        """Does this person run the server (owner, Administrator or Manage Server)? Asked of Discord itself each time."""
        if guild.owner_id == uid:
            return True
        member = guild.get_member(uid)
        if member is None:
            try:
                member = m.dash_run(guild.fetch_member(uid), timeout=10)
            except Exception:
                return False
        perms = member.guild_permissions
        return any(getattr(perms, p) for p in MANAGE)

    def managed_guild(sid):
        guild = bot.get_guild(int(sid)) if str(sid).isdigit() else None
        if guild is None:
            abort(404)
        if not can_manage(guild, int(user()["id"])):
            abort(403)
        return guild

    def icon_of(g):
        return str(g.icon.replace(size=64).url) if g.icon else None

    # ---------------------------------------------------------------- pages

    @app.route("/dashboard")
    def user_dashboard():
        boot = {"user": user(), "oauth": bool(m.DISCORD_CLIENT_ID and m.DISCORD_CLIENT_SECRET), "error": bool(request.args.get("login_error")),
                "invite": "/add", "support": "/support", "commit": m.commit_info()}
        return Response(m._web_file("dash.html").replace("__BOOT__", jsonify(boot).get_data(as_text=True).replace("<", "\\u003c")), mimetype="text/html")

    @app.route("/dashboard/login")
    def user_login():
        if not (m.DISCORD_CLIENT_ID and m.DISCORD_CLIENT_SECRET):
            return redirect("/dashboard")
        return m.oauth_start("user")

    @app.route("/dashboard/logout")
    def user_logout():
        session.pop("duser", None)
        return redirect("/dashboard")

    # ---------------------------------------------------------------- servers

    @app.route("/me/servers")
    @need_login
    def me_servers():
        """The servers (with Shapes in them) this person runs."""
        uid = int(user()["id"])
        out = []
        for g in list(bot.guilds):
            try:
                if can_manage(g, uid):
                    out.append({"id": str(g.id), "name": g.name, "icon": icon_of(g), "members": g.member_count or 0})
            except Exception:
                continue
        return jsonify({"servers": sorted(out, key=lambda s: s["name"].lower())})

    @app.route("/me/servers/<sid>")
    @need_login
    def me_server(sid):
        g = managed_guild(sid)
        c = m.counting_get(g.id)
        chans = [{"id": str(ch.id), "name": ch.name} for ch in g.channels if isinstance(ch, discord.TextChannel)]
        return jsonify({"id": str(g.id), "name": g.name, "icon": icon_of(g), "members": g.member_count or 0, "channels": chans,
                        "counting": {"enabled": bool(c["enabled"]), "channel": str(c["channel"]) if c["channel"] else "", "number": c["number"],
                                     "highest": c["highestnumber"], "warnings": c["warnings"], "banned": bool(c.get("banned"))}})

    @app.route("/me/servers/<sid>/counting", methods=["POST"])
    @need_login
    def me_counting(sid):
        g = managed_guild(sid)
        body = request.get_json(silent=True) or {}
        c = m.counting_get(g.id)
        if c.get("banned"):
            return jsonify({"error": "Counting is turned off for this server by the bot's owners."}), 403
        if "channel" in body:
            ch = g.get_channel(int(body["channel"])) if str(body["channel"]).isdigit() else None
            if not isinstance(ch, discord.TextChannel):
                return jsonify({"error": "Pick a text channel from this server."}), 400
            c["channel"] = ch.id
        if "enabled" in body:
            c["enabled"] = bool(body["enabled"])
        if c["enabled"] and not c["channel"]:
            return jsonify({"error": "Pick a counting channel first."}), 400
        m.countingDB.set(str(g.id), c)
        m.countingDB.save()
        return jsonify({"ok": True})

    # ---------------------------------------------------------------- Roblox accounts (through the Shapes API)

    def person():
        """(person dict, error response). Errors say the API is down rather than that nothing is linked."""
        if m.api_down():
            return None, (jsonify({"error": m.API_DOWN_TEXT, "api_down": True}), 503)
        d = m._shapes_call("GET", f"/internal/person/discord/{user()['id']}")
        if not d or not d.get("success"):
            return None, (jsonify({"error": m.API_DOWN_TEXT, "api_down": True}), 503)
        return d, None

    def api_write(method, path, body=None):
        d = m._shapes_call(method, path, body)
        if d is None:
            return jsonify({"error": m.API_DOWN_TEXT, "api_down": True}), 503
        if not d.get("success"):
            return jsonify({"error": str(d.get("data") or "That didn't work.")}), 400
        return jsonify({"ok": True, **{k: v for k, v in d.items() if k == "url"}})

    @app.route("/me/roblox")
    @need_login
    def me_roblox():
        d, err = person()
        if err:
            return err
        return jsonify({"accounts": d.get("accounts") or [], "max": d.get("max", 100)})

    @app.route("/me/roblox/link", methods=["POST"])
    @need_login
    def me_roblox_link():
        if m.api_down():
            return jsonify({"error": m.API_DOWN_TEXT, "api_down": True}), 503
        return api_write("POST", "/internal/roblox/start", {"platform": "discord", "user_id": user()["id"], "guild": ""})

    @app.route("/me/roblox/auto", methods=["POST"])
    @need_login
    def me_roblox_auto():
        rid = str((request.get_json(silent=True) or {}).get("roblox_id") or "") or None
        return api_write("POST", f"/internal/person/discord/{user()['id']}/auto", {"roblox_id": rid})

    @app.route("/me/roblox/remove", methods=["POST"])
    @need_login
    def me_roblox_remove():
        rid = str((request.get_json(silent=True) or {}).get("roblox_id") or "")
        if not rid.isdigit():
            return jsonify({"error": "Which account?"}), 400
        return api_write("DELETE", f"/internal/person/discord/{user()['id']}/roblox/{rid}")

    @app.route("/me/roblox/hide", methods=["POST"])
    @need_login
    def me_roblox_hide():
        body = request.get_json(silent=True) or {}
        rid = str(body.get("roblox_id") or "")
        if not rid.isdigit():
            return jsonify({"error": "Which account?"}), 400
        return api_write("POST", f"/internal/person/discord/{user()['id']}/hide", {"hidden": bool(body.get("hidden")), "roblox_ids": [rid]})

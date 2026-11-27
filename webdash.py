"""The member dashboard: sign in with Discord, then manage the servers you run (counting) and your own Roblox accounts."""
import asyncio
import sys
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
        """The servers (with Shapes in them) this person runs. Members Discord hasn't cached are fetched all at once, not one server at a time."""
        uid = int(user()["id"])
        guilds = list(bot.guilds)
        allowed, missing = [], []
        for g in guilds:
            if g.owner_id == uid:
                allowed.append(g)
            elif g.get_member(uid) is not None:
                if any(getattr(g.get_member(uid).guild_permissions, p) for p in MANAGE):
                    allowed.append(g)
            else:
                missing.append(g)

        async def lookup():
            async def one(g):
                try:
                    return g, await g.fetch_member(uid)
                except Exception:
                    return g, None
            return await asyncio.gather(*(one(g) for g in missing))

        if missing:
            try:
                for g, member in m.dash_run(lookup(), timeout=20):
                    if member is not None and any(getattr(member.guild_permissions, p) for p in MANAGE):
                        allowed.append(g)
            except Exception:
                pass
        out = [{"id": str(g.id), "name": g.name, "icon": icon_of(g), "members": g.member_count or 0} for g in allowed]
        return jsonify({"servers": sorted(out, key=lambda s: s["name"].lower())})

    def welcome_module():
        mod = sys.modules.get("cogs.welcome")
        if mod is None:
            abort(503)
        return mod

    def assignable_roles(g):
        """Roles the bot is allowed to hand out: not @everyone, not bot-managed, below the bot's own top role."""
        top = g.me.top_role if g.me else None
        return [{"id": str(r.id), "name": r.name} for r in sorted(g.roles, key=lambda r: -r.position)
                if not r.is_default() and not r.managed and (top is None or r < top)]

    @app.route("/me/servers/<sid>")
    @need_login
    def me_server(sid):
        g = managed_guild(sid)
        c = m.counting_get(g.id)
        chans = [{"id": str(ch.id), "name": ch.name} for ch in g.channels if isinstance(ch, discord.TextChannel)]
        auto = m.autoroleDB.get(str(g.id)) or {}
        return jsonify({"id": str(g.id), "name": g.name, "icon": icon_of(g), "members": g.member_count or 0, "channels": chans,
                        "roles": assignable_roles(g),
                        "autorole": {"enabled": bool(auto.get("enabled")), "role": str(auto.get("role_id") or "")},
                        "welcome": welcome_module().get_cfg(g.id),
                        "counting": {"enabled": bool(c["enabled"]), "channel": str(c["channel"]) if c["channel"] else "", "number": c["number"],
                                     "highest": c["highestnumber"], "warnings": c["warnings"], "banned": bool(c.get("banned"))}})

    @app.route("/me/servers/<sid>/autorole", methods=["POST"])
    @need_login
    def me_autorole(sid):
        g = managed_guild(sid)
        body = request.get_json(silent=True) or {}
        enabled = bool(body.get("enabled"))
        role = g.get_role(int(body["role"])) if str(body.get("role") or "").isdigit() else None
        if enabled and role is None:
            return jsonify({"error": "Pick the role new members should get."}), 400
        if role is not None and (role.is_default() or role.managed or (g.me and role >= g.me.top_role)):
            return jsonify({"error": "I can't hand out that role: pick one that sits below my highest role."}), 400
        m.autoroleDB.set(str(g.id), {"role_id": role.id if role else (m.autoroleDB.get(str(g.id)) or {}).get("role_id"), "enabled": enabled})
        m.autoroleDB.save()
        return jsonify({"ok": True})

    @app.route("/me/servers/<sid>/welcome", methods=["POST"])
    @need_login
    def me_welcome(sid):
        g = managed_guild(sid)
        body = request.get_json(silent=True) or {}
        w = welcome_module()
        cfg = {"enabled": bool(body.get("enabled")), "embed": bool(body.get("embed", True)), "dm": bool(body.get("dm")), "include_bots": bool(body.get("include_bots")),
               "channel": str(body.get("channel") or ""), "message": str(body.get("message") or "").strip(), "title": str(body.get("title") or "").strip(),
               "dm_message": str(body.get("dm_message") or "").strip()}
        if len(cfg["message"]) > 1500 or len(cfg["dm_message"]) > 1500 or len(cfg["title"]) > 100:
            return jsonify({"error": "The message is too long (1500 characters at most, 100 for the title)."}), 400
        if cfg["channel"]:
            ch = g.get_channel(int(cfg["channel"])) if cfg["channel"].isdigit() else None
            if not isinstance(ch, discord.TextChannel):
                return jsonify({"error": "Pick a text channel from this server."}), 400
        if cfg["enabled"] and not (cfg["channel"] or cfg["dm"]):
            return jsonify({"error": "Pick a welcome channel (or turn on the DM) before switching welcoming on."}), 400
        if cfg["enabled"] and cfg["channel"] and not cfg["message"]:
            return jsonify({"error": "Write the welcome message first."}), 400
        w.save_cfg(g.id, cfg)
        return jsonify({"ok": True})

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

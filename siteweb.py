"""The public website: landing page, about, command list and counting leaderboard (templates and assets live in site/)."""
import os

import jinja2
from flask import jsonify, redirect, render_template, request, send_from_directory

HERE = os.path.dirname(os.path.abspath(__file__))
SITE_DIR = os.path.join(HERE, "site")
PAGE = 10


def register(m):
    """Attach the routes to m.app. `m` is the main module (bot, counting data, command list)."""
    app, bot = m.app, m.bot
    app.jinja_loader = jinja2.ChoiceLoader([app.jinja_loader, jinja2.FileSystemLoader(os.path.join(SITE_DIR, "templates"))])
    yapin_url = os.environ.get("YAPIN_SITE_URL", "https://shapes-yapper.oriaxen.com").rstrip("/")

    def stats():
        if not m.bot_ready:
            return None, None
        users = sum(int(g.member_count or 0) for g in bot.guilds)
        try:
            users = max(users, int(m.BotInfo.approximate_user_install_count))
        except Exception:
            pass
        return len(bot.guilds), users

    def paged(items):
        pages = max(1, -(-len(items) // PAGE))
        page = min(max(request.args.get("page", 1, type=int), 1), pages)
        return items[(page - 1) * PAGE: page * PAGE], page, pages

    def board():
        return [{"name": r["serverName"], "current": r["currentnumber"], "highest": r["highestnumber"]} for r in m.leaderboard_rows()] if m.bot_ready else []

    def command_list():
        out = []
        for c in m.command_entries():
            if c.get("type") == "context_menu_command":
                continue
            opts = " ".join(f"<{o}>" for o in c.get("options", []))
            out.append({"name": c["name"], "description": c.get("description", ""), "usage": f"/{c['name']} {opts}".strip()})
        return sorted(out, key=lambda c: c["name"])

    @app.route("/assets/<path:name>")
    def site_assets(name):
        return send_from_directory(os.path.join(SITE_DIR, "assets"), name, max_age=3600)

    @app.route("/", methods=["GET"])
    def home():
        servers, users = stats()
        return render_template("home.html", users=users, servers=servers, top=board()[:3], yapin_url=yapin_url)

    @app.route("/about")
    def about():
        return render_template("about.html", yapin_url=yapin_url)

    @app.route("/add")
    def add_bot():
        default = f"https://discord.com/oauth2/authorize?client_id={m.DISCORD_CLIENT_ID or '1447659383755641045'}"
        return redirect(os.environ.get("BOT_INVITE_URL", default), 302)

    @app.route("/support")
    def support():
        return redirect(os.environ.get("SUPPORT_URL", "https://discord.gg/tXYZJYnpwd"), 302)

    @app.route("/counting/leaderboard/")
    @app.route("/counting")
    @app.route("/leaderboard")
    def leaderboard_page():
        items, page, pages = paged(board())
        return render_template("leaderboard.html", items=items, page=page, pages=pages)

    @app.route("/commands", methods=["GET"])
    def send_commands():
        """A web page for browsers; the JSON the old site read for anything else (so existing callers keep working)."""
        if "text/html" in request.headers.get("Accept", "") and request.args.get("format") != "json":
            items, page, pages = paged(command_list())
            return render_template("commands.html", items=items, page=page, pages=pages)
        return jsonify(m.commands_payload()), 200

import os
import sys
import threading
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
from pickledb import PickleDB
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
import re
import io
import socket
import typing
from typing import Dict, Any, Optional
from openai import OpenAI
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

#=== Database Setup ===
countingDB = PickleDB('counting.db')
embedDB = PickleDB('embed.db')
usersDB = PickleDB('users.db')
autoroleDB = PickleDB('autorole.db')
AI_DB = PickleDB('ai.db')

# === Discord Bot Setup ===
intents = discord.Intents.default()
intents.guilds = True
intents.message_content = True
owner = "sl.ip"
co_owner = "<@481295611417853982>"
MainURL = "https://shapes.lol"
searchengine = "621a38269031b4e89" # PLEASE USE YOUR OWN SEARCH ENGINE ID FROM https://cse.google.com/

# get the public key and api key for our api from .env
PUBLIC_KEY = os.environ.get("DISCORD_PUBLIC_KEY")
APIBaseURL_Key = os.environ.get("Shapes_API_Key")
APIBaseURL_Headers = {"Authorization": f"Bearer {APIBaseURL_Key}"}

# get the bot token from TOKEN.txt
try:
    with open('TOKEN.txt', 'r') as f:
        token = f.read()
except FileNotFoundError:
    print("Error: The file 'TOKEN.txt' was not found.")
except Exception as e:
    print(f"An error occurred: {e}")

# get the admin key from akey.txt
try:
    with open('akey.txt', 'r') as f:
        ADMIN_KEY = f.read()
except FileNotFoundError:
    print("Error: The file 'akey.txt' was not found.")
except Exception as e:
    print(f"An error occurred: {e}")

# get the google API key from GoogleToken.txt
try:
    with open('GoogleToken.txt', 'r') as f:
        GoogleAPIKey = f.read()
except FileNotFoundError:
    print("Error: The file 'GoogleToken.txt' was not found.")
except Exception as e:
    print(f"An error occurred: {e}")

# get the API url
APIBaseURL = "http://localhost:1337"


# last online cache
def load_cached_timestamps():
    try:
        with open('lastonline.json', 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        return {}

def update_cached_timestamp(user_id, timestamp):
    try:
        cached_timestamps = load_cached_timestamps()
        cached_timestamps[str(user_id)] = timestamp
        with open('lastonline.json', 'w') as f:
            json.dump(cached_timestamps, f, indent=2)
    except Exception as e:
        print(f"Error updating cached timestamp: {e}")

# === Flask App Setup ===
app = Flask(__name__)
CORS(app)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "keepthisasecret")

HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>Bot Dashboard</title>
    <style>
        body { font-family: Arial, sans-serif; background: #1e1e2f; color: #fff; padding: 20px; }
        h1 { color: #50fa7b; }
        select, input[type=text] { padding: 6px; border-radius: 5px; border: none; margin: 5px 0; }
        input[type=submit] { background: #50fa7b; border: none; padding: 8px 12px; border-radius: 5px; color: #000; cursor: pointer; }
        .server { background: #282a36; padding: 10px; margin-bottom: 15px; border-radius: 10px; }
        .logout { margin-top: 20px; }
        .presence-button {
            display: inline-block;
            margin-top: 10px;
            background-color: #7289da;
            color: white;
            padding: 10px 18px;
            border-radius: 8px;
            text-decoration: none;
            font-weight: bold;
            transition: background-color 0.3s ease;
        }
        .presence-button:hover {
            background-color: #5b6eae;
        }
    </style>
</head>
<body>
    <h1>🤖 Bot Dashboard</h1>
    <p>Status: <b style="color:lime;">Online</b></p>
    <p>Connected to {{ guilds|length }} {{ 'server' if guilds|length == 1 else 'servers' }}</p>
    
    <!-- Presence Button -->
    <a href="https://shapes.lol/discord" target="_blank" rel="noopener" class="presence-button">
        Join Our Server
    </a>
    
    {% for g in guilds %}
        <div class="server">
            <h3>{{ g.name }}</h3>
            <form action="/send" method="post">
                <input type="hidden" name="guild_id" value="{{ g.id }}">
                <label for="channel">Channel:</label>
                <select name="channel_id">
                    {% for c in g.text_channels %}
                        <option value="{{ c.id }}">{{ c.name }}</option>
                    {% endfor %}
                </select>
                <br>
                <label for="message">Message:</label>
                <input type="text" name="message" placeholder="Enter your message" required>
                <br>
                <input type="submit" value="Send">
            </form>
        </div>
    {% endfor %}
    <div class="logout">
        <a href="/logout" style="color: #ff5555;">Logout</a>
    </div>
</body>
</html>
"""

# === Admin Auth Decorator ===
def admin_required(f):
    def wrapped(*args, **kwargs):
        if session.get("admin") != True:
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    wrapped.__name__ = f.__name__
    return wrapped

# === Flask Routes ===
@app.route("/status")
def status():
    return "OK", 200

@app.route("/activity")
def activity():
    return redirect(url_for("admin_login"))

@app.route("/login", methods=["GET", "POST"])
def admin_login():
    if session.get("admin") == True:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        key = request.form.get("pass")
        if key == ADMIN_KEY:
            session["admin"] = True
            return redirect(url_for("dashboard"))
        else:
            return '''
                <h3 style="color: red; font-family: Arial, sans-serif;">Incorrect key.</h3>
                <a href="/login" style="font-family: Arial, sans-serif; color: #50fa7b;">Try again</a>
            '''

    return '''
    <!DOCTYPE html>
    <html>
    <head>
        <title>Admin Login</title>
        <style>
            body {
                background: #121212;
                color: #eee;
                font-family: Arial, sans-serif;
                display: flex;
                justify-content: center;
                align-items: center;
                height: 100vh;
                margin: 0;
            }
            .login-container {
                background: #282a36;
                padding: 40px 50px;
                border-radius: 12px;
                box-shadow: 0 0 15px #50fa7b;
                text-align: center;
                width: 320px;
            }
            h2 {
                margin-bottom: 25px;
                color: #50fa7b;
            }
            input[type=password] {
                width: 100%;
                padding: 12px;
                margin-bottom: 20px;
                border: none;
                border-radius: 6px;
                font-size: 16px;
                background: #44475a;
                color: #f8f8f2;
            }
            input[type=password]::placeholder {
                color: #bd93f9;
            }
            button {
                background: #50fa7b;
                border: none;
                color: #000;
                padding: 12px 0;
                width: 100%;
                font-size: 16px;
                font-weight: bold;
                border-radius: 6px;
                cursor: pointer;
                transition: background 0.3s ease;
            }
            button:hover {
                background: #44d366;
            }
            a {
                display: inline-block;
                margin-top: 15px;
                color: #50fa7b;
                text-decoration: none;
                font-size: 14px;
            }
            a:hover {
                text-decoration: underline;
            }
        </style>
    </head>
    <body>
        <div class="login-container">
            <h2>Admin Login</h2>
            <form method="POST">
                <input type="password" name="pass" placeholder="Enter admin key" required>
                <button type="submit">Login</button>
            </form>
        </div>
    </body>
    </html>
    '''

@app.route("/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))

# Dashboard at root /
@app.route("/", methods=["GET"])
@admin_required
def dashboard():
    if not bot_ready:
        return "<h3>Bot is not ready yet, please try again in a moment.</h3>"
    return render_template_string(HTML_TEMPLATE, guilds=cached_guilds)

@app.route("/send", methods=["POST"])
@admin_required
def send_message():
    guild_id = int(request.form["guild_id"])
    channel_id = int(request.form["channel_id"])
    message = request.form["message"]

    guild = discord.utils.get(bot.guilds, id=guild_id)
    if guild:
        channel = discord.utils.get(guild.text_channels, id=channel_id)
        if channel:
            try:
                bot.loop.create_task(channel.send(message))
            except Exception as e:
                print(f"Failed to send message: {e}")

    return redirect(url_for("dashboard"))
    
@app.route('/shutdown', methods=['POST'])
def shutdown():
    if request.remote_addr not in ['127.0.0.1', '::1']:
        return 'Forbidden', 403
    
    func = request.environ.get('werkzeug.server.shutdown')
    if func is None:
        return 'Not running with the Werkzeug Server', 500
    
    func()
    return 'Server shutting down...'  
    
@app.route('/commands', methods=['GET'])
def send_commands():
    commands = {}
    commands['Commands'] = {}
    for cmd in bot.tree.get_commands():
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
    return jsonify(commands), 200
    
@app.route('/count/commands', methods=['GET'])
def get_command_count():
    return {'Commands': str(len(bot.tree.get_commands()))}, 200

@app.route('/discord/webhook', methods=['POST'])
def send_webhook():
    signature = request.headers.get("X-Signature-Ed25519")
    timestamp = request.headers.get("X-Signature-Timestamp")
    body = request.data.decode("utf-8")

    if not signature or not timestamp:
        return Response("missing signature", 401)

    try:
        verify_key = nacl.signing.VerifyKey(bytes.fromhex(PUBLIC_KEY))
        verify_key.verify(f'{timestamp}{body}'.encode(), bytes.fromhex(signature))
    except nacl.exceptions.BadSignatureError:
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
            print("Webhook URL not set in environment")
            return "webhook missing", 500

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
    pass

colors_lua = """{[3447003] = "Blue", [15158332] = "Red", [3066993] = "Green", [10181046] = "Purple", [15105570] = "Orange", [15844367] = "Gold", [1752220] = "Teal", [2123412] = "Dark Blue", [10038562] = "Dark Red", [2067276] = "Dark Green", [7419530] = "Dark Purple", [11027200] = "Dark Orange", [12745742] = "Dark Gold", [1146986] = "Dark Teal"}"""
colors = lua.decode(colors_lua)
#print(colors)

#bot = commands.Bot(command_prefix="/", intents=intents)
bot = Shapes(command_prefix="/", intents=discord.Intents.all())
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
            countingDB.save()
            embedDB.save()
            usersDB.save()
            autoroleDB.save()
            #print(f"EmbedDB = {embedDB.all()}")
            #print(f"CountingDB = {countingDB.all()}")
            #print(f"UsersDB = {usersDB.all()}")
    
            for guild in bot.guilds:
                if not countingDB.get(f"{guild.id}"):
                    countingDB.set(f"{guild.id}", {"channel":None,"number":0,"enabled":False,"warnings":0,"lastcounter":None,"highestnumber":0, "banned":False})
                    countingDB.save()
        if bot.is_closed():
            countingDB.save()
            embedDB.save()
            usersDB.save()
            autoroleDB.save()
            AI_DB.save()
            print(f"Saved EmbedDB {embedDB.all()}")
            print(f"Saved CountingDB {countingDB.all()}")
            print(f"Saved UsersDB {usersDB.all()}")
            print(f"Saved AutoRoleDB {autoroleDB.all()}")
            print(f"Saved AI_DB {AI_DB.all()}")
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
        await bot.change_presence(activity=discord.CustomActivity(name=f"🔗 shapes.lol/discord"))
        await asyncio.sleep(2)
        #if len(bot.guilds) == 1:
            #print(bot.guilds[0].name)
            #await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name=bot.guilds[0].name))
        #else:
            #print(f"Watching {len(bot.guilds)} Servers")
            #await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name=f"{len(bot.guilds)} servers"))

        await asyncio.sleep(30)

def IsInteger(s):
    try:
        int(s)
        return True  # Conversion succeeded, it is an integer
    except ValueError:
        return False  # Conversion failed, it is not an integer

# === Bot Events ===
@bot.event
async def on_message(message):
    if message.author == bot.user:
        return
    if message.author.bot:
        return

    if message.guild:
        server = message.guild
        countingjson = countingDB.get(server.id)
        counting_data = countingjson
        enabled = counting_data['enabled']
        number = counting_data['number']
        channel = counting_data['channel']
        warnings = counting_data['warnings']
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
            
            if message.author.id == LastCounter and warnings != 3:
                await message.add_reaction('⚠️')
                await message.reply(f":warning: You can't count by yourself!")
                warnings = warnings + 1
                counting_data['warnings'] = warnings
                countingDB.set(server.id, counting_data)
                countingDB.save()
                return
            if warnings < 3:
                await message.add_reaction('⚠️')
                if number == 0:
                    await message.reply(f":warning: The next number is 1")
                await message.reply(f":warning: The next number is {next_number}")
                warnings = warnings + 1
                counting_data['warnings'] = warnings
                countingDB.set(server.id, counting_data)
                countingDB.save()
                return
            if warnings >= 3:
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
        counting = countingDB.get(f"{server.id}")
        if counting:
            if message.author.id == counting['lastcounter'] and message.channel.id == counting['channel'] and IsInteger(message.content):
                nextnumber = counting['number'] + 1
                await message.channel.send(f"{message.author.mention} deleted their message containing the last number. The next number is {nextnumber}")

@bot.event
async def on_message_edit(before, after):
    if before.author.bot:
        return

    if before.guild:
        server = before.guild
        counting = countingDB.get(f"{server.id}")
        if counting:
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
    await bot.change_presence(activity=discord.CustomActivity(name=f"🔗 shapes.lol/discord"))
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

@app.route('/clb', methods=["GET"])
def countinglb():
    if bot.is_ready():
        lb = {}
        for server in countingDB.all():
            data = countingDB.get(f"{server}")
            if data.get("banned", False) == True:
                print(f'{bot.get_guild(int(server)).name if bot.get_guild(int(server)) else "Unknown"} Is Banned From Counting.')
                continue
            if data["enabled"] == True:
                server_name = bot.get_guild(int(server)).name if bot.get_guild(int(server)) else None
                if server_name == None:
                    continue
                lb[f"{server}"] = {"currentnumber": data['number'],"highestnumber": data['highestnumber'], "serverName": server_name}
        FullLB = sorted(lb.items(), key=lambda x: x[1]['highestnumber'], reverse=True)
        return {"Leaderboard":FullLB}, 200
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

async def restartbot():
    print("Bot Restarting.")
    await bot.close(close=True)
    await asyncio.sleep(20)
    bot.run(token)

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

# === User Commands ===
@bot.tree.context_menu(name="discord2roblox")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def discord2roblox(interaction: discord.Interaction, user: discord.User): # = 481295611417853982):
    userid = user.id
    await interaction.response.defer(thinking=True)

    loading = discord.Embed(
        title=f"{Emojis.get('loading')} {interaction.user.mention} Getting Roblox Profile For {user.name}",
        color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
    )

    await interaction.followup.send(embed=loading)

    url = f"{APIBaseURL}/d2r/{userid}"

    try:
        response = requests.get(url, headers=APIBaseURL_Headers)
        response.raise_for_status()
        APIData = response.json()
        print(APIData)
        Roblox = None
        if APIData.get("data") and APIData.get("success", False) == True:
            successembed = discord.Embed(
                title=f"Roblox Profile for {user.name}",
                description=f"[View Roblox Profile](https://www.roblox.com/users/{APIData['data']}/profile)",
                color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
            )
            successembed.set_thumbnail(url=f"https://www.roblox.com/headshot-thumbnail/image?userId={APIData['data']}&width=420&height=420&format=png")
            successembed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
            await interaction.edit_original_response(embed=successembed)
            return
        else:
            #print(f"No Roblox Profile found for {user.name}")
            failedembed = discord.Embed(
                title=f"No Roblox Profile found for {user.name}",
                color=discord.Color.red()
            )
            await interaction.edit_original_response(embed=failedembed)
            return
    except requests.exceptions.RequestException as e:
        #print(f"Error fetching data for Discord ID: {userid}: {e}")
        failedembed3 = discord.Embed(
            title=f"Error retrieving Roblox Profile from {user.name}",
            color=discord.Color.red()
        )
        await interaction.edit_original_response(embed=failedembed3)
        return

@bot.tree.context_menu(name="discord2spook")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def discord2spook(interaction: discord.Interaction, user: discord.User):
    url = f"https://spook.bio/api/profiles?discordId={user.id}"
    print(user.id)
    print(url)
    response = requests.get(url)
    data = response.json()
    if response.status_code == 200:
        await interaction.response.send_message(f"{user.mention}'s [spook.bio Profile](https://spook.bio/@{data.get('username')})", ephemeral=False)
        print(f"Fetched {data['username']} successfully!")
    else:
        if interaction.user.name == user.name:
            await interaction.response.send_message(f":x: You don't have a spook.bio profile linked to your account {user.mention}! :x: To link your profile to your account create a spook.bio account [here](https://spook.bio/login)")
            return
        await interaction.response.send_message(f":x: {user.mention} doesn't have a spook.bio profile linked to their account! :x:", ephemeral=False)
        print(f"Error fetching data: {response.status_code}")

# === Message Commands ===
@bot.tree.context_menu(name="ai")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def ai(interaction: discord.Interaction, prompt: discord.Message):
    await interaction.response.defer(thinking=True)

    loading = discord.Embed(
    title=f"{Emojis.get('loading')} {interaction.user.mention} Getting AI Response For: {prompt}",
    color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
)

    await interaction.followup.send(embed=loading)
    
    user_id = str(interaction.user.id)
    username = interaction.user.name

    # Load or initialize user data
    user_data = AI_DB.get(user_id) or {"username": username, "user_messages": [], "ai_responses": []}
    user_data["username"] = username
    user_data["user_messages"].append(prompt)
    
    # Keep last 50 messages
    user_data["user_messages"] = user_data["user_messages"][-50:]
    user_data["ai_responses"] = user_data["ai_responses"][-50:]

    # System instructions for the AI
    messages_for_ai = [
        {
            "role": "system",
            "content": (
                f"You are a helpful Discord assistant chatting with {username}. "
                "Always respond in a single concise paragraph. Otherwise your response will not be recieved due to the embed text limit"
                "Follow Discord TOS. Do not provide instructions for illegal activity. "
                "Stay safe, respectful, and friendly."
            )
        }
    ]

    # Include previous conversation
    for u_msg, a_msg in zip(user_data["user_messages"], user_data["ai_responses"]):
        messages_for_ai.append({"role": "user", "content": u_msg})
        messages_for_ai.append({"role": "assistant", "content": a_msg})

    # Add latest message
    messages_for_ai.append({"role": "user", "content": prompt})

    try:
        response = chatgpt.chat.completions.create(
            model=AIModel,
            messages=messages_for_ai
        )
        ai_reply = response.choices[0].message.content.strip()
    except Exception as e:
        ai_reply = f"⚠️ API error: {e}"

    # Save AI response
    user_data["ai_responses"].append(ai_reply)
    AI_DB.set(user_id, user_data)
    AI_DB.save()

    # Create embed
    embed = discord.Embed(
        title=f"💬 Chat with {username}",
        color=embedDB.get(user_id) or discord.Color.blue()
    )
    embed.add_field(name="🧍 You said:", value=prompt, inline=False)
    embed.add_field(name="🤖 AI replied:", value=ai_reply, inline=False)
    embed.set_footer(text=f"Requested by {username} | {MainURL}")

    await interaction.edit_original_response(embed=embed)

@bot.tree.context_menu(name="google")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def google(interaction: discord.Interaction, message: discord.Message):
    await interaction.response.defer(thinking=True)
    query = message.content
    thinkingembed = discord.Embed(
        title=f"{Emojis.get('loading')} {interaction.user.mention} Searching Google For {query}!",
        color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
    )
    await interaction.followup.send(embed=thinkingembed)
    # replace spaces with + in query for google search link
    properquery = query.replace(" ", "+")
    
    url = f"https://www.googleapis.com/customsearch/v1?key={GoogleAPIKey}&cx={searchengine}&q={properquery}"
    try:
        response = requests.get(url)
        response.raise_for_status()
        data = response.json()

        # Get First 5 results
        if "items" in data and len(data["items"]) >= 5:
            first_result = data["items"][0]
            title = first_result.get("title", "No Title")
            snippet = first_result.get("snippet", "No Description")
            link = first_result.get("link", "No Link")
            print(f"First Result: {title} - {link}")
            second_result = data["items"][1]
            second_result_title = second_result.get("title", "No Title")
            second_result_snippet = second_result.get("snippet", "No Description")
            second_result_link = second_result.get("link", "No Link")
            print(f"Second Result: {second_result_title} - {second_result_link}")
            third_result = data["items"][2]
            third_result_title = third_result.get("title", "No Title")
            third_result_snippet = third_result.get("snippet", "No Description")
            third_result_link = third_result.get("link", "No Link")
            print(f"Third Result: {third_result_title} - {third_result_link}")
            fourth_result = data["items"][3]
            fourth_result_title = fourth_result.get("title", "No Title")
            fourth_result_snippet = fourth_result.get("snippet", "No Description")
            fourth_result_link = fourth_result.get("link", "No Link")
            print(f"Fourth Result: {fourth_result_title} - {fourth_result_link}")
            fifth_result = data["items"][4]
            fifth_result_title = fifth_result.get("title", "No Title")
            fifth_result_snippet = fifth_result.get("snippet", "No Description")
            fifth_result_link = fifth_result.get("link", "No Link")
            print(f"Fifth Result: {fifth_result_title} - {fifth_result_link}")
            
            embed = discord.Embed(
                title=f"Google Results For {query}",
                description=f"**1. [{title}]({link})**\n{snippet}\n\n**2. [{second_result_title}]({second_result_link})**\n{second_result_snippet}\n\n**3. [{third_result_title}]({third_result_link})**\n{third_result_snippet}\n\n**4. [{fourth_result_title}]({fourth_result_link})**\n{fourth_result_snippet}\n\n**5. [{fifth_result_title}]({fifth_result_link})**\n{fifth_result_snippet}\n\n[Search For More Results](https://google.com/search?q={properquery})",
                color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
            )
            embed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
            await interaction.edit_original_response(embed=embed)
        else:
            if "items" in data and len(data["items"]) > 0:
                notenoughresultsembed = discord.Embed(
                title=":x: Not enough results found! :x:",
                description=f"Please search on google yourself as there wasn't enough results to generate an embed | [Search on Google Yourself For More Results](https://google.com/search?q={properquery})",
                color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
                )
                notenoughresultsembed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")

                await interaction.edit_original_response(embed=notenoughresultsembed)
                return
            noresultembed = discord.Embed(
                title=":x: No results found! :x:",
                description=f"No Results for {query} | [Search on Google Yourself For More Results](https://google.com/search?q={properquery})",
                color=discord.Color.red()
            )
            noresultembed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
            await interaction.edit_original_response(embed=noresultembed)
    except requests.exceptions.RequestException as e:
        print(f"An error occurred during the API request: {e}")
        errorembed = discord.Embed(
            title=":x: An error occurred while searching Google. Please try again later. :x:",
            color=discord.Color.red()
        )
        errorembed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
        await interaction.edit_original_response(embed=errorembed)

# === Bot Commands ===
@bot.tree.command(name="userinstalls", description="Get The User Installation Count For Shapes!")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def userinstalls(interaction: discord.Interaction):
    await interaction.response.send_message(f"{BotInfo.approximate_user_install_count} Users Use Shapes!")

@bot.tree.command(name="servercount", description="Get The Server Count For Shapes!")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def servercount(interaction: discord.Interaction):
    await interaction.response.send_message(f"{len(bot.guilds)} Servers Use Shapes!")

@bot.tree.command(name="getdata", description="Get The Data From One Of Our Databases")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(database="The database to get data from", key="The key to get data for")
@app_commands.choices(database=[
    app_commands.Choice(name="embedDB", value="embedDB"),
    app_commands.Choice(name="countingDB", value="countingDB"),
    app_commands.Choice(name="usersDB", value="usersDB"),
    app_commands.Choice(name="autoroleDB", value="autoroleDB"),
    app_commands.Choice(name="AI_DB", value="AI_DB"),
])
async def getdata(interaction: discord.Interaction, database: str, key: str):
    # add options for each database and each key and restrict who can use the command.
    if not interaction.user.id in [481295611417853982, 1129085908390518895]:
        await interaction.response.send_message(":x: You don't have permission to use this command! :x:", ephemeral=True)
        return
    db_map = {
        "embedDB": embedDB,
        "countingDB": countingDB,
        "usersDB": usersDB,
        "autoroleDB": autoroleDB,
        "AI_DB": AI_DB,
    }
    selected_db = db_map.get(database)
    if selected_db:
        data = selected_db.get(f"{key}")
        if data:
            await interaction.response.send_message(f"Data for key `{key}` in database `{database}`: ```{data}```", ephemeral=True)
        else:
            await interaction.response.send_message(f":x: No data found for key `{key}` in database `{database}`! :x:", ephemeral=True)
    else:
        await interaction.response.send_message(f":x: Database `{database}` not found! :x:", ephemeral=True)

@bot.tree.command(name="settings", description="Your Settings For Shapes")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def settings(interaction: discord.Interaction):
    current = embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
    # loop through the colors to figure out which one is the current one and set the default value on the selectoption as True
    defaults = []
    for color in DiscordColors:
        if color.value == current:
            defaults.append(True)
        else:
            defaults.append(False)


    view = discord.ui.View()
    color_select = discord.ui.Select(
        placeholder="Select your embed color",
        options=[
            discord.SelectOption(label="Blue", description="A nice blue color", value=str(discord.Color.blue().value), emoji="🔵", default=defaults[0]),
            discord.SelectOption(label="Red", description="A vibrant red color", value=str(discord.Color.red().value), emoji="🔴", default=defaults[1]),
            discord.SelectOption(label="Green", description="A refreshing green color", value=str(discord.Color.green().value), emoji="🟢", default=defaults[2]),
            discord.SelectOption(label="Purple", description="A royal purple color", value=str(discord.Color.purple().value), emoji="🟣", default=defaults[3]),
            discord.SelectOption(label="Orange", description="A bright orange color", value=str(discord.Color.orange().value), emoji="🟠", default=defaults[4]),
            discord.SelectOption(label="Gold", description="A shiny gold color", value=str(discord.Color.gold().value), emoji="🟡", default=defaults[5]),
            discord.SelectOption(label="Teal", description="A cool teal color", value=str(discord.Color.teal().value), emoji="🔷", default=defaults[6]),
            discord.SelectOption(label="Dark Blue", description="A deep dark blue color", value=str(discord.Color.dark_blue().value), emoji="🔷", default=defaults[7]),
            discord.SelectOption(label="Dark Red", description="A deep dark red color", value=str(discord.Color.dark_red().value), emoji="🔴", default=defaults[8]),
            discord.SelectOption(label="Dark Green", description="A deep dark green color", value=str(discord.Color.dark_green().value), emoji="🟢", default=defaults[9]),
            discord.SelectOption(label="Dark Purple", description="A deep dark purple color", value=str(discord.Color.dark_purple().value), emoji="🟣", default=defaults[10]),
            discord.SelectOption(label="Dark Orange", description="A deep dark orange color", value=str(discord.Color.dark_orange().value), emoji="🟠", default=defaults[11]),
            discord.SelectOption(label="Dark Gold", description="A deep dark gold color", value=str(discord.Color.dark_gold().value), emoji="🟡", default=defaults[12]),
            discord.SelectOption(label="Dark Teal", description="A deep dark teal color", value=str(discord.Color.dark_teal().value), emoji="🔷", default=defaults[13]),
            discord.SelectOption(label="Random", description="A random color", value=str(discord.Color.random().value), emoji="🔷", default=defaults[14]),
        ]
    )
    async def on_submit(interaction: discord.Interaction):
        selected_color_value = int(color_select.values[0])
        selected_color_name = colors.get(selected_color_value, "Unknown")
        print(selected_color_name)
        print(color_select)
        print(color_select.values)
        embedDB.set(f"{interaction.user.id}", selected_color_value)
        embed = discord.Embed(
            title="Embed Color Changed!",
            description=f"Your embed color has been changed successfully to {selected_color_name}!",
            color=selected_color_value
        )
        embed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
        await interaction.response.send_message(embed=embed, ephemeral=True)
    color_select.callback = on_submit
    view.add_item(color_select)

    class SettingsView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=None)  # No timeout

        @discord.ui.button(label="Change Embed Color", style=discord.ButtonStyle.primary, custom_id="change_embed_color", emoji="🎨")
        async def change_embed_color(self, interaction: discord.Interaction, button: discord.ui.Button):
            await interaction.response.send_message(content="Select an embed color from the menu", view=view, ephemeral=True)
            #await interaction.response.send_modal(EmbedColorModal())
        #@discord.ui.button(label="Toggle Counting", style=discord.ButtonStyle.primary, custom_id="toggle_counting")
        #async def toggle_counting(self, interaction: discord.Interaction, button: discord.ui.Button):
        #    current_setting = countingDB.get("enabled")
        #    if current_setting:
        #        countingDB.set("enabled", False)
        #        await interaction.response.send_message("Counting feature disabled.", ephemeral=True)
        #    else:
        #        countingDB.set("enabled", True)
        #        await interaction.response.send_message("Counting feature enabled.", ephemeral=True)
    embed = discord.Embed(
        title="Settings",
        description="Choose a setting below to modify it.",
        color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
    )
    embed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
    await interaction.response.send_message(embed=embed, view=SettingsView(), ephemeral=True)


@bot.tree.command(name="status", description=f"Get the {MainURL} status")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(f"[shapes.lol Status Page](https://shapes.lol/status)")

@bot.tree.command(name="stop", description="Stop the bot.")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def stop(interaction: discord.Interaction):
    if interaction.user.name == "lcjunior1220" or interaction.user.name == "sl.ip" or interaction.user.name == "kiwixor":
        await interaction.response.send_message(":white_check_mark: Shutdown Successfully!", ephemeral=False)
        
        countingDB.save()
        embedDB.save()
        usersDB.save()
        autoroleDB.save()
        AI_DB.save()
        
        print("Bot shutdown initiated by command")
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post('http://localhost:13455/shutdown') as resp:
                    pass
        except:
            pass
        
        await bot.close()
        os._exit(0)
    else:
        await interaction.response.send_message(f"Only {owner}, and {co_owner} can use this command.", ephemeral=True)

@bot.tree.command(name="restart", description="Restart the bot.")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def restart(interaction: discord.Interaction):
    if interaction.user.name == "lcjunior1220" or interaction.user.name == "sl.ip" or interaction.user.name == "kiwixor":
        await interaction.response.send_message(":white_check_mark: Restarted Successfully!!", ephemeral=False)
        await restartbot()
    else:
        await interaction.response.send_message(f"Only <@481295611417853982 and <@1129085908390518895> can use this command.", ephemeral=True)

@bot.tree.command(name="counting", description="Counting Settings For Shapes")
@app_commands.default_permissions(administrator=True)
@commands.bot_has_permissions(add_reactions=True, moderate_members=True, read_message_history=True, view_channel=True, send_messages=True)
@app_commands.allowed_installs(guilds=True, users=False)
@app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
async def counting(interaction: discord.Interaction):
    server = interaction.guild
    print(server.id)
    counting_json = countingDB.get(server.id)
    countingData = counting_json
    print(countingData)
    if not countingData:
        countingDB.set(server.id, {"channel":None,"number":0,"enabled":False,"warnings":0,"lastcounter":None,"highestnumber":0})
        countingDB.save()
        counting_json = countingDB.get(server.id)
        countingData = counting_json
    print(countingData)
    print(countingData['channel'])
    print(countingData['number'])
    print(countingData['enabled'])
    print(countingData['warnings'])
    print(countingData['lastcounter'])
    channels = server.channels
    channel_options = []
    ccount = 0
    for channel in channels:
        ccount = ccount + 1
        if isinstance(channel, discord.TextChannel) and ccount != 25:
            channel_options.append(discord.SelectOption(label=channel.name, value=str(channel.id)))
    class CountingView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=None)

        @discord.ui.button(label="Toggle Counting", style=discord.ButtonStyle.primary, custom_id="toggle_counting", emoji="🔢")
        async def toggle_counting(self, interaction: discord.Interaction, button: discord.ui.Button):
            counting_json = countingDB.get(server.id)
            countingData = counting_json
            current_setting = countingData['enabled']
            if current_setting:
                countingData['enabled'] = False
                countingDB.set(server.id, countingData)
                await interaction.response.send_message("Counting disabled.", ephemeral=True)
            else:
                countingData['enabled'] = True
                countingDB.set(server.id, countingData)
                await interaction.response.send_message("Counting enabled.", ephemeral=True)

        @discord.ui.select(placeholder="Select Counting Channel", options=channel_options, custom_id="select_channel")
        async def select_channel(self, interaction: discord.Interaction, select: discord.ui.Select):
            selected_channel_id = int(select.values[0])
            counting_json = countingDB.get(server.id)
            countingData = counting_json
            countingData['channel'] = selected_channel_id
            countingDB.set(server.id, countingData)
            await interaction.response.send_message(f"Counting channel set to <#{selected_channel_id}>.", ephemeral=True)
    embed = discord.Embed(
        title="Counting Settings",
        description=f"**Current Settings:**\n- Counting Enabled: `{countingData['enabled']}`\n- Counting Channel: `<#{countingData['channel']}>`\n- Current Number: `{countingData['number']}`\n- Highest Number: `{countingData.get('highestnumber', 0)}`\n- Warnings: `{countingData['warnings']}`\n- Last Counter: `{countingData['lastcounter']}`",
        color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
    )
    embed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
    await interaction.response.send_message(embed=embed, view=CountingView(), ephemeral=True)

@bot.tree.command(name="spookpfp", description="Get a pfp from a user's spook.bio profile.")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def spookpfp(interaction: discord.Interaction, username: str = "phis"):
    url = f"https://spook.bio/api/profiles/{username}"
    response = requests.get(url)
    data = response.json()
    if response.status_code == 200:
        await interaction.response.send_message(data.avatar, ephemeral=False)
        print("Fetched data successfully!")
    else:
        await interaction.response.send_message(f":x: {username} Not Found :x:", ephemeral=False)
        print(f"Error fetching data: {response.status_code}")

@bot.tree.command(name="discord2spook", description="Get a spook.bio profile from a discord user.")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def discord2spook(interaction: discord.Interaction, user: discord.User): # = 481295611417853982):
    url = f"https://spook.bio/api/profiles?discordId={user.id}"
    print(user.id)
    print(url)
    response = requests.get(url)
    data = response.json()
    if response.status_code == 200:
        await interaction.response.send_message(f"{user.mention}'s [spook.bio Profile](https://spook.bio/@{data.get('username')})", ephemeral=False)
        print(f"Fetched {data['username']} successfully!")
    else:
        if interaction.user.name == user.name:
            await interaction.response.send_message(f":x: You don't have a spook.bio profile linked to your account {user.mention}! :x: To link your profile to your account create a spook.bio account [here](https://spook.bio/login)")
            return
        await interaction.response.send_message(f":x: {user.mention} doesn't have a spook.bio profile linked to their account! :x:", ephemeral=False)
        print(f"Error fetching data: {response.status_code}")

@bot.tree.command(name="ping", description="Check the bot's latency.")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def ping(interaction: discord.Interaction):
    latency = bot.latency * 1000
    connection = "Good" if latency < 200 else "Average" if latency < 400 else "Poor"
    embed = discord.Embed(
        title="Bot Server Stats"
        , description=f"Latency: `{latency:.2f}ms` ({connection} Connection)"
        , color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
    )
    embed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
    await interaction.response.send_message(embed=embed)
    await asyncio.sleep(2)
    updatedlatency = bot.latency * 1000
    updatedconnection = "Good" if updatedlatency < 200 else "Average" if updatedlatency < 400 else "Poor"
    embed = discord.Embed(
        title="Bot Server Stats"
        , description=f"OriginalLatency: `{latency:.2f}ms` ({connection} Connection)\nEditLatency: `{updatedlatency:.2f}ms` ({updatedconnection} Connection)"
        , color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
    )
    embed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
    await interaction.edit_original_response(embed=embed, content=None)

@bot.tree.command(name="roblox2discord", description="Get a roblox user's Discord from their username.")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def roblox2discord(interaction: discord.Interaction, user: str):
    await interaction.response.defer()
    
    #print(f"Searching For {user}")
    thinkingembed = discord.Embed(
        title=f"{Emojis.get('loading')} {interaction.user.mention} Searching For {user}!",
        color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
    )
    await interaction.followup.send(embed=thinkingembed)

    url = "https://users.roblox.com/v1/usernames/users"
    # print(f"Fetching Data From {url}")
    
    request_payload = {
        "usernames": [user],
        "excludeBannedUsers": True
    }

    try:
        response = requests.post(url, json=request_payload)
        response.raise_for_status()
        data = response.json()
        if data.get("data") and len(data["data"]) > 0:
            userinfo = data["data"][0]
            UserID = userinfo["id"]
            Display = userinfo["displayName"]
            user = userinfo["name"]
            if user == Display:
                Username = f"@{user}"
            else:
                Username = f"{Display} (@{user})"
            #print(f"UserInfo: {userinfo}")
        else:
            #print(f"{user} not found.")
            failedembed7 = discord.Embed(
                title=f":warning: {user} not found.",
                color=discord.Color.yellow()
            )
            await interaction.edit_original_response(embed=failedembed7)
            return

    except requests.exceptions.RequestException as e:
        #print(f"An error occurred during the API request: {e}")
        failedembed8 = discord.Embed(
            title=f":warning: {user} not found.",
            color=discord.Color.yellow()
        )
        await interaction.edit_original_response(embed=failedembed8)
        return

    url = f"{APIBaseURL}/r2d/{UserID}"

    try:
        response = requests.get(url, headers=APIBaseURL_Headers)
        response.raise_for_status()
        APIData = response.json()
        print(APIData)
        Discord = None
        if APIData.get("data") and APIData.get("success", False) == True:
            successembed2 = discord.Embed(
                title=f"Discord User for {Username}",
                description=f"[View Discord Profile](https://discord.com/users/{APIData['data']})",
                color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
            )

            successembed2.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
            await interaction.edit_original_response(embed=successembed2)
            return
        else:
            #print(f"No matching Discord user found for {Username}")
            failedembed1 = discord.Embed(
                title=f"No Discord User found for {Username}",
                color=discord.Color.red()
            )
            await interaction.edit_original_response(embed=failedembed1)
            return
    except requests.exceptions.RequestException as e:
        #print(f"Error fetching data for ID {UserID}: {e}")
        failedembed2 = discord.Embed(
            title=f"Error retrieving Discord User from {Username}",
            color=discord.Color.red()
        )
        await interaction.edit_original_response(embed=failedembed2)
        # await interaction.edit_original_response(f"Error retrieving Discord User from {url}")
        return

@bot.tree.command(name="discord2roblox", description="Get a roblox profile from their Discord UserID.")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def discord2roblox(interaction: discord.Interaction, user: discord.User):
    userid = user.id
    await interaction.response.defer(thinking=True)

    loading = discord.Embed(
        title=f"{Emojis.get('loading')} {interaction.user.mention} Getting Roblox Profile For {user.name}",
        color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
    )

    await interaction.followup.send(embed=loading)

    url = f"{APIBaseURL}/d2r/{userid}"

    try:
        response = requests.get(url, headers=APIBaseURL_Headers)
        response.raise_for_status()
        APIData = response.json()
        print(APIData)
        Roblox = None
        if APIData.get("data") and APIData.get("success", False) == True:
            successembed = discord.Embed(
                title=f"Roblox Profile for {user.name}",
                description=f"[View Roblox Profile](https://www.roblox.com/users/{APIData['data']}/profile)",
                color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
            )
            successembed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
            await interaction.edit_original_response(embed=successembed)
            return
        else:
            #print(f"No Roblox Profile found for {user.name}")
            failedembed = discord.Embed(
                title=f"No Roblox Profile found for {user.name}",
                color=discord.Color.red()
            )
            await interaction.edit_original_response(embed=failedembed)
            return
    except requests.exceptions.RequestException as e:
        #print(f"Error fetching data for Discord ID: {userid}: {e}")
        failedembed3 = discord.Embed(
            title=f"Error retrieving Roblox Profile from {user.name}",
            color=discord.Color.red()
        )
        await interaction.edit_original_response(embed=failedembed3)
        return

@bot.tree.command(name="ai", description="Chat with an AI assistant.")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def ai(interaction: discord.Interaction, *, prompt: str):
    await interaction.response.defer(thinking=True)

    loading = discord.Embed(
    title=f"{Emojis.get('loading')} {interaction.user.mention} Getting AI Response For: {prompt}",
    color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
)

    await interaction.followup.send(embed=loading)
    
    user_id = str(interaction.user.id)
    username = interaction.user.name

    # Load or initialize user data
    user_data = AI_DB.get(user_id) or {"username": username, "user_messages": [], "ai_responses": []}
    user_data["username"] = username
    user_data["user_messages"].append(prompt)
    
    # Keep last 50 messages
    user_data["user_messages"] = user_data["user_messages"][-50:]
    user_data["ai_responses"] = user_data["ai_responses"][-50:]

    # System instructions for the AI
    messages_for_ai = [
        {
            "role": "system",
            "content": (
                f"You are a helpful Discord assistant chatting with {username}. "
                "Always respond in a single concise paragraph. Otherwise your response will not be recieved due to the embed text limit"
                "Follow Discord TOS. Do not provide instructions for illegal activity. "
                "Stay safe, respectful, and friendly."
            )
        }
    ]

    # Include previous conversation
    for u_msg, a_msg in zip(user_data["user_messages"], user_data["ai_responses"]):
        messages_for_ai.append({"role": "user", "content": u_msg[:1024]})
        messages_for_ai.append({"role": "assistant", "content": a_msg[:1024]})

    # Add latest message
    messages_for_ai.append({"role": "user", "content": prompt})

    try:
        response = chatgpt.chat.completions.create(
            model=AIModel,
            messages=messages_for_ai
        )
        ai_reply = response.choices[0].message.content.strip()
    except Exception as e:
        ai_reply = f"⚠️ API error: {e}"

    # Save AI response
    user_data["ai_responses"].append(ai_reply)
    AI_DB.set(user_id, user_data)
    AI_DB.save()

    # Create embed
    embed = discord.Embed(
        title=f"💬 Chat with {username}",
        color=embedDB.get(user_id) or discord.Color.blue()
    )
    embed.add_field(name="🧍 You said:", value=prompt, inline=False)
    embed.add_field(name="🤖 AI replied:", value=ai_reply, inline=False)
    embed.set_footer(text=f"Requested by {username} | {MainURL}")

    await interaction.edit_original_response(embed=embed)

@bot.tree.command(name="google", description="Search Something On Google.")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def google(interaction: discord.Interaction, query: str = "shapes.lol"):
    await interaction.response.defer()
    
    thinkingembed = discord.Embed(
        title=f"{Emojis.get('loading')} {interaction.user.mention} Searching Google For {query}",
        color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
    )
    await interaction.followup.send(embed=thinkingembed)
    
    # replace spaces with + in query for google search link
    properquery = query.replace(" ", "+")
    
    url = f"https://www.googleapis.com/customsearch/v1?key={GoogleAPIKey}&cx={searchengine}&q={properquery}"
    try:
        response = requests.get(url)
        response.raise_for_status()
        data = response.json()

        # Get First 5 results
        if "items" in data and len(data["items"]) >= 5:
            first_result = data["items"][0]
            title = first_result.get("title", "No Title")
            snippet = first_result.get("snippet", "No Description")
            link = first_result.get("link", "No Link")
            print(f"First Result: {title} - {link}")
            second_result = data["items"][1]
            second_result_title = second_result.get("title", "No Title")
            second_result_snippet = second_result.get("snippet", "No Description")
            second_result_link = second_result.get("link", "No Link")
            print(f"Second Result: {second_result_title} - {second_result_link}")
            third_result = data["items"][2]
            third_result_title = third_result.get("title", "No Title")
            third_result_snippet = third_result.get("snippet", "No Description")
            third_result_link = third_result.get("link", "No Link")
            print(f"Third Result: {third_result_title} - {third_result_link}")
            fourth_result = data["items"][3]
            fourth_result_title = fourth_result.get("title", "No Title")
            fourth_result_snippet = fourth_result.get("snippet", "No Description")
            fourth_result_link = fourth_result.get("link", "No Link")
            print(f"Fourth Result: {fourth_result_title} - {fourth_result_link}")
            fifth_result = data["items"][4]
            fifth_result_title = fifth_result.get("title", "No Title")
            fifth_result_snippet = fifth_result.get("snippet", "No Description")
            fifth_result_link = fifth_result.get("link", "No Link")
            print(f"Fifth Result: {fifth_result_title} - {fifth_result_link}")
            
            embed = discord.Embed(
                title=f"Google Results For {query}",
                description=f"**1. [{title}]({link})**\n{snippet}\n\n**2. [{second_result_title}]({second_result_link})**\n{second_result_snippet}\n\n**3. [{third_result_title}]({third_result_link})**\n{third_result_snippet}\n\n**4. [{fourth_result_title}]({fourth_result_link})**\n{fourth_result_snippet}\n\n**5. [{fifth_result_title}]({fifth_result_link})**\n{fifth_result_snippet}\n\n[Search For More Results](https://google.com/search?q={properquery})",
                color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
            )
            embed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
            await interaction.edit_original_response(embed=embed)
        else:
            noresultembed = discord.Embed(
                title=":x: No results found! :x:",
                description=f"No Results for {query} | [Search on Google Yourself For More Results](https://google.com/search?q={properquery})",
                color=discord.Color.red()
            )
            noresultembed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
            await interaction.edit_original_response(embed=noresultembed)
    except requests.exceptions.RequestException as e:
        print(f"An error occurred during the API request: {e}")
        errorembed = discord.Embed(
            title=":x: An error occurred while searching Google. Please try again later. :x:",
            color=discord.Color.red()
        )
        errorembed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
        await interaction.edit_original_response(embed=errorembed)

@bot.tree.command(name="invite", description="Get the bot's invite link.")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def invite(interaction: discord.Interaction):
    invite_url = f"https://discord.com/api/oauth2/authorize?client_id={bot.user.id}"
    invite_embed = discord.Embed(
        description=f"[Click Here To Add Shapes To Your Server or Apps]({invite_url})",
        color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
    )
    invite_embed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
    await interaction.response.send_message(embed=invite_embed, ephemeral=False)
    #await interaction.response.send_message(f"Invite me to your server or add me to your apps using this link: {invite_url}", ephemeral=False)
    
@bot.tree.command(name="robloxinfo", description="Get a Roblox user's profile information.")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def robloxinfo(interaction: discord.Interaction, user: str = "Roblox"):
    await interaction.response.defer(thinking=True)
    
    print(f"Searching For {user}'s profile")
    thinkingembed = discord.Embed(
        title=f"{Emojis.get('loading')} {interaction.user.mention} Searching For {user}'s Roblox profile!",
        color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
    )
    await interaction.followup.send(embed=thinkingembed)

    url = "https://users.roblox.com/v1/usernames/users"
    
    request_payload = {
        "usernames": [user],
        "excludeBannedUsers": False
    }

    async def check_ownership(session: aiohttp.ClientSession, user_id: str, asset_id: str) -> bool:
        url = f"https://inventory.roblox.com/v1/users/{user_id}/items/0/{asset_id}/is-owned"
        try:
            async with session.get(url, headers={
                'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36',
                'accept': 'application/json',
            }) as response:
                data = await response.json()
                return data is True
        except Exception:
            return False

    async def check_verification_items(session: aiohttp.ClientSession, user_id: str) -> bool:
        items = {
            'hats': ['18824203', '93078560', '102611803'],
            'sign': '1567446'
        }
        
        has_sign = await check_ownership(session, user_id, items['sign'])
        if has_sign:
            return True
        
        for hat_id in items['hats']:
            if await check_ownership(session, user_id, hat_id):
                return True
        
        return False

    async def check_inventory_visibility(session: aiohttp.ClientSession, user_id: str) -> str:
        try:
            inventory_url = f"https://inventory.roblox.com/v1/users/{user_id}/can-view-inventory"
            async with session.get(inventory_url) as response:
                if response.status == 200:
                    inventory_data = await response.json()
                    return "Public" if inventory_data.get('canView', False) else "Private"
        except Exception as e:
            print(f"Error checking inventory visibility: {e}")
        return "Private"

    async def check_presence(session: aiohttp.ClientSession, user_id: str) -> tuple:
        try:
            try:
                with open('roblosecuritytoken.txt', 'r') as f:
                    roblosecurity_token = f.read().strip()
            except FileNotFoundError:
                print("ROBLOSECURITY token file not found")
                return (0, None)
            
            presence_url = 'https://presence.roblox.com/v1/presence/users'
            headers = {
                "Content-Type": "application/json",
                "Cookie": f".ROBLOSECURITY={roblosecurity_token}"
            }
            
            async with session.post(presence_url, headers=headers, json={'userIds': [user_id]}) as response:
                if response.status == 200:
                    presence_data = await response.json()
                    if presence_data.get('userPresences') and len(presence_data['userPresences']) > 0:
                        user_presence = presence_data['userPresences'][0]
                        presence_type = user_presence.get('userPresenceType', 0)
                        place_id = user_presence.get('placeId')
                        return (presence_type, place_id)
        except Exception as e:
            print(f"Error checking presence: {e}")
        return (0, None)

    class BadgeService:
        def __init__(self, session: aiohttp.ClientSession):
            self.session = session

        async def get_user_badges(self, user_id: str):
            try:
                url = f"https://badges.roblox.com/v1/users/{user_id}/badges?sortOrder=Desc&limit=10"
                async with self.session.get(url) as response:
                    if response.status == 200:
                        data = await response.json()
                        return data.get('data', []), 200
                    return [], response.status
            except Exception as e:
                print(f"Error getting user badges: {e}")
                return [], 500

        async def get_awarded_dates(self, user_id: str, badge_ids: list):
            try:
                if not badge_ids:
                    return {}
                
                badge_ids_str = ",".join(map(str, badge_ids))
                url = f"https://badges.roblox.com/v1/users/{user_id}/badges/awarded-dates?badgeIds={badge_ids_str}"
                async with self.session.get(url) as response:
                    if response.status == 200:
                        data = await response.json()
                        awarded_dates = {}
                        for item in data.get('data', []):
                            awarded_dates[item['badgeId']] = item.get('awardedDate')
                        return awarded_dates
                    return {}
            except Exception as e:
                print(f"Error getting awarded dates: {e}")
                return {}

    class BadgeFormatter:
        @staticmethod
        def parse_iso_timestamp(iso_timestamp: str) -> int:
            try:
                if iso_timestamp:
                    dt = datetime.fromisoformat(iso_timestamp.replace('Z', '+00:00'))
                    return int(dt.timestamp())
            except (ValueError, AttributeError):
                pass
            return None

    async def get_last_online_from_badges(session: aiohttp.ClientSession, user_id: str) -> tuple:
        try:
            badge_service = BadgeService(session)
            badges, status_code = await badge_service.get_user_badges(user_id)
            
            if status_code == 200 and badges:
                badge_ids = [badge['id'] for badge in badges]
                awarded_dates = await badge_service.get_awarded_dates(user_id, badge_ids)
                
                for badge in badges:
                    badge_id = badge['id']
                    badge['awardedDate'] = awarded_dates.get(badge_id)
                
                valid_timestamps = []
                latest_place_id = None
                
                for badge in badges:
                    awarded_date = badge.get('awardedDate')
                    if awarded_date:
                        unix_timestamp = BadgeFormatter.parse_iso_timestamp(awarded_date)
                        if unix_timestamp:
                            valid_timestamps.append((unix_timestamp, badge))
                
                if valid_timestamps:
                    valid_timestamps.sort(key=lambda x: x[0], reverse=True)
                    latest_timestamp, latest_badge = valid_timestamps[0]
                    
                    awarder = latest_badge.get('awarder', {})
                    if awarder.get('type') == 'Place':
                        latest_place_id = awarder.get('id')
                    
                    return (latest_timestamp, latest_place_id)
        except Exception as e:
            print(f"Error fetching badge last online: {e}")
        return (None, None)

    async def get_friends_count(session: aiohttp.ClientSession, user_id: str) -> int:
        try:
            friends_url = f"https://friends.roblox.com/v1/users/{user_id}/friends/count"
            async with session.get(friends_url) as response:
                if response.status == 200:
                    friends_data = await response.json()
                    return friends_data.get('count', 0)
        except Exception as e:
            print(f"Error fetching friends count: {e}")
        return 0

    async def get_followers_count(session: aiohttp.ClientSession, user_id: str) -> int:
        try:
            followers_url = f"https://friends.roblox.com/v1/users/{user_id}/followers/count"
            async with session.get(followers_url) as response:
                if response.status == 200:
                    followers_data = await response.json()
                    return followers_data.get('count', 0)
        except Exception as e:
            print(f"Error fetching followers count: {e}")
        return 0

    async def get_followings_count(session: aiohttp.ClientSession, user_id: str) -> int:
        try:
            followings_url = f"https://friends.roblox.com/v1/users/{user_id}/followings/count"
            async with session.get(followings_url) as response:
                if response.status == 200:
                    followings_data = await response.json()
                    return followings_data.get('count', 0)
        except Exception as e:
            print(f"Error fetching followings count: {e}")
        return 0

    async def get_user_games_visits(session: aiohttp.ClientSession, user_id: str) -> int:
        try:
            user_games_url = f"https://games.roblox.com/v2/users/{user_id}/games?accessFilter=2&limit=50"
            async with session.get(user_games_url) as response:
                if response.status == 200:
                    games_data = await response.json()
                    total_visits = 0
                    
                    if games_data.get('data'):
                        games_list = games_data.get('data', [])
                        universe_ids = [game.get('id') for game in games_list if game.get('id') is not None]
                        
                        if universe_ids:
                            visit_tasks = []
                            chunk_size = 50
                            for i in range(0, len(universe_ids), chunk_size):
                                chunk = universe_ids[i:i + chunk_size]
                                universe_ids_str = ",".join(map(str, chunk))
                                games_info_url = f"https://games.roblox.com/v1/games?universeIds={universe_ids_str}"
                                visit_tasks.append(session.get(games_info_url))
                            
                            visit_responses = await asyncio.gather(*visit_tasks)
                            for response in visit_responses:
                                if response.status == 200:
                                    result = await response.json()
                                    for game in result.get('data', []):
                                        total_visits += game.get('visits', 0) or 0
                    
                    return total_visits
        except Exception as e:
            print(f"Error fetching user games visits: {e}")
        return 0

    async def get_profile_api_data(session: aiohttp.ClientSession, user_id: str) -> dict:
        try:
            with open('roblosecuritytoken.txt', 'r') as f:
                roblosecurity_token = f.read().strip()
            
            url = "https://apis.roblox.com/profile-platform-api/v1/profiles/get"
            payload = {
                "profileId": str(user_id),
                "profileType": "User",
                "components": [
                    {"component": "UserProfileHeader"},
                    {"component": "About"},
                    {"component": "RobloxBadges"},
                    {"component": "Statistics"}
                ],
                "includeComponentOrdering": True
            }
            
            headers = {
                "Content-Type": "application/json",
                "Cookie": f".ROBLOSECURITY={roblosecurity_token}",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            }
            
            async with session.post(url, json=payload, headers=headers) as response:
                if response.status == 200:
                    data = await response.json()
                    components = data.get('components', {})
                    
                    profile_data = {}
                    
                    user_profile_header = components.get('UserProfileHeader', {}) or {}
                    profile_data['is_premium'] = user_profile_header.get('isPremium', False)
                    profile_data['is_verified'] = user_profile_header.get('isVerified', False)
                    
                    counts = user_profile_header.get('counts', {}) or {}
                    profile_data['friends_count'] = counts.get('friendsCount', 0)
                    profile_data['followers_count'] = counts.get('followersCount', 0)
                    profile_data['followings_count'] = counts.get('followingsCount', 0)
                    
                    statistics = components.get('Statistics', {}) or {}
                    profile_data['visits'] = statistics.get('numberOfVisits', 0)
                    profile_data['join_date'] = statistics.get('userJoinedDate', '')
                    
                    about = components.get('About', {}) or {}
                    profile_data['description'] = about.get('description', '')
                    
                    roblox_badges = components.get('RobloxBadges', {}) or {}
                    badge_list = roblox_badges.get('robloxBadgeList', [])
                    
                    excluded_badges = {'Homestead', 'Bricksmith'}
                    valid_badges = []
                    
                    for badge in badge_list:
                        badge_type = badge.get('type', {})
                        badge_name = badge_type.get('value', '')
                        
                        if badge_name not in excluded_badges:
                            valid_badges.append(badge)
                    
                    profile_data['last_online'] = None
                    if valid_badges:
                        latest_timestamp = 0
                        for badge in valid_badges:
                            created_time = badge.get('createdTime', {})
                            seconds = created_time.get('seconds', 0)
                            if seconds > latest_timestamp:
                                latest_timestamp = seconds
                        
                        if latest_timestamp > 0:
                            profile_data['last_online'] = latest_timestamp
                    
                    return profile_data
                    
        except Exception as e:
            print(f"Error fetching profile API data: {e}")
        
        return {
            'visits': 0,
            'friends_count': 0,
            'followers_count': 0,
            'followings_count': 0,
            'is_verified': False,
            'is_premium': False,
            'description': '',
            'join_date': '',
            'last_online': None
        }

    def load_cached_timestamps():
        try:
            with open('lastonline.json', 'r') as f:
                return json.load(f)
        except FileNotFoundError:
            return {}

    def update_cached_timestamp(user_id, timestamp):
        try:
            cached_timestamps = load_cached_timestamps()
            cached_timestamps[str(user_id)] = timestamp
            with open('lastonline.json', 'w') as f:
                json.dump(cached_timestamps, f, indent=2)
        except Exception as e:
            print(f"Error updating cached timestamp: {e}")

    try:
        response = requests.post(url, json=request_payload)
        response.raise_for_status()
        data = response.json()
        if data.get("data") and len(data["data"]) > 0:
            userinfo = data["data"][0]
            UserID = userinfo["id"]
            Display = userinfo["displayName"]
            hasVerifiedBadge = userinfo.get("hasVerifiedBadge", False)

            rap_value = 0
            value_value = 0
            rolimons_last_online = None
            badge_last_online = None
            badge_place_id = None
            is_premium = False
            is_verified = False
            roblox_badges = []
            total_visits = 0
            inventory_visibility = "Private"
            friends_count = 0
            followers_count = 0
            followings_count = 0
            presence_type = 0
            current_place_id = None
            profile_description = ""
            
            try:
                connector = aiohttp.TCPConnector(family=socket.AF_INET)
                async with aiohttp.ClientSession(connector=connector) as session:
                    profile_data = await get_profile_api_data(session, UserID)
                    
                    is_premium = profile_data['is_premium']
                    is_verified = profile_data['is_verified']
                    friends_count = profile_data['friends_count']
                    followers_count = profile_data['followers_count']
                    followings_count = profile_data['followings_count']
                    total_visits = profile_data['visits']
                    profile_description = profile_data['description']
                    
                    tasks = [
                        check_verification_items(session, UserID),
                        check_inventory_visibility(session, UserID),
                        check_presence(session, UserID),
                        get_last_online_from_badges(session, UserID)
                    ]
                    
                    results = await asyncio.gather(*tasks)
                    is_verified = results[0]
                    inventory_visibility = results[1]
                    presence_result = results[2]
                    presence_type = presence_result[0]
                    current_place_id = presence_result[1]
                    badge_result = results[3]
                    badge_last_online = badge_result[0]
                    badge_place_id = badge_result[1]
                    
                    badges_url = f"https://accountinformation.roblox.com/v1/users/{UserID}/roblox-badges"
                    async with session.get(badges_url) as response:
                        if response.status == 200:
                            badges_data = await response.json()
                            roblox_badges = badges_data
                    
                    rolimons_stats_url = f"https://api.rolimons.com/players/v1/playerinfo/{UserID}"
                    async with session.get(rolimons_stats_url, headers={'User-Agent': 'shapes.lol'}) as response:
                        if response.status == 200:
                            rolimons_stats_data = await response.json()
                            if rolimons_stats_data.get('success'):
                                rap_value = rolimons_stats_data.get('rap', 0) or 0
                                value_value = rolimons_stats_data.get('value', 0) or 0
                                rolimons_last_online = rolimons_stats_data.get('last_online')
            except Exception as e:
                print(f"Error fetching data: {e}")

            rolimonsurl = f"https://rolimons.com/player/{UserID}"

            url = f"https://users.roblox.com/v1/users/{UserID}"
            try:
                response = requests.get(url)
                response.raise_for_status()
                playerdata = response.json()
                Description = profile_description or playerdata["description"]
                Banned = playerdata["isBanned"]
                user = playerdata["name"]
                JoinDate = playerdata["created"]
                created_timestamp = isotodiscordtimestamp(JoinDate)
            except requests.exceptions.RequestException as e:
                print(f"Error fetching user data for ID {UserID}: {e}")
                failedembed = discord.Embed(
                    title=f":x: An error occurred while fetching data for user ID: {UserID}. Please try again later.",
                    color=discord.Color.red()
                )
                await interaction.edit_original_response(embed=failedembed)
                return

            if Display == user:
                Username = Display
            else:
                Username = f"{Display} (@{user})"

            if UserID == 124767284:
                hasVerifiedBadge = True
            
            if hasVerifiedBadge:
                Username += " <:RobloxVerified:1416951927513677874>"

            if is_premium:
                Username += " <:RobloxPremium:1416951078200541378>"

            is_terminated = False
            avatar_image = None
            
            try:
                avatar_url = f"https://thumbnails.roblox.com/v1/users/avatar?userIds={UserID}&size=420x420&format=Png&isCircular=false"
                response = requests.get(avatar_url)
                response.raise_for_status()
                data = response.json()
                if data and data.get("data") and len(data["data"]) > 0:
                    avatar_image = data["data"][0].get("imageUrl")
                    if avatar_image and avatar_image.startswith("https://t7.rbxcdn.com"):
                        is_terminated = True
                        
                        connector = aiohttp.TCPConnector(family=socket.AF_INET)
                        async with aiohttp.ClientSession(connector=connector) as session:
                            custom_avatar = await render_custom_avatar(session, UserID)
                            if custom_avatar:
                                avatar_image = custom_avatar
            except requests.exceptions.RequestException as e:
                print(f"Error fetching avatar: {e}")

            if is_terminated:
                Username = f":warning: [Banned] {Username}"
                
                try:
                    connector = aiohttp.TCPConnector(family=socket.AF_INET)
                    async with aiohttp.ClientSession(connector=connector) as session:
                        terminated_tasks = [
                            get_friends_count(session, UserID),
                            get_followers_count(session, UserID),
                            get_followings_count(session, UserID),
                            get_user_games_visits(session, UserID)
                        ]
                        
                        terminated_results = await asyncio.gather(*terminated_tasks)
                        friends_count = terminated_results[0]
                        followers_count = terminated_results[1]
                        followings_count = terminated_results[2]
                        total_visits = terminated_results[3]
                except Exception as e:
                    print(f"Error fetching terminated user data: {e}")

            url = f"{APIBaseURL}/r2d/{UserID}"
            try:
                response = requests.get(url, headers={"Authorization": f"Bearer {APIBaseURL_Key}"})
                response.raise_for_status()
                Data = response.json()
                
                if Data["success"] == False:
                    Discord = None
                elif Data["success"] == True:
                    Discord = Data["data"]
                else:
                    Discord = None
            except requests.exceptions.RequestException as e:
                #print(f"Error fetching data for ID: {UserID}: {e}")
                Discord = None

            profileurl = f"https://www.roblox.com/users/{UserID}/profile"

            cached_timestamps = load_cached_timestamps()
            cached_timestamp = cached_timestamps.get(str(UserID))

            if presence_type in [1, 2, 3]:
                current_time = int(time.time())
                update_cached_timestamp(UserID, current_time)
                cached_timestamp = current_time

            timestamps = []

            if cached_timestamp:
                timestamps.append((cached_timestamp, "Cached"))

            if badge_last_online:
                timestamps.append((badge_last_online, "Badge Activity"))

            if profile_data['last_online']:
                timestamps.append((profile_data['last_online'], "Profile API"))

            if rolimons_last_online:
                timestamps.append((rolimons_last_online, "Rolimons Data"))

            formatted_last_online = "Unknown"
            if timestamps:
                timestamps.sort(key=lambda x: x[0], reverse=True)
                last_online_timestamp, last_online_source = timestamps[0]
                formatted_last_online = f"<t:{last_online_timestamp}:D>"

            badges = {
                "Combat Initiation": "<:CombatInitiation:1430627878898368632>",
                "Administrator": "<:RobloxAdmin:1416951128876122152>",
                "Bloxxer": "<:Bloxxer:1430627881301704736>",
                "Warrior": "<:Warrior:1430640757403943063>",
                "Official Model Maker": "<:RobloxModelMaker:1416952360852263013>",
                "Bricksmith": "<:Roblox1000Visits:1416952101229170698>",
                "Homestead": "<:Roblox100Visits:1416952056324952184>",
                "Inviter": "<:RobloxInviter:1416952415772479559>",
                "Ambassador": "<:Ambassador:1430627877337960548>",
                "Friendship": "<:Friendship:1430641140679577630>",
                "Veteran": "<:RobloxVeteran:1416952185094406264>",
                "Welcome To The Club": "<:WelcomeToTheClub:1430627875337273525>"
            }

            badges_display = "None"
            if roblox_badges and len(roblox_badges) > 0:
                badge_emojis = []
                for badge in roblox_badges:
                    badge_name = badge.get("name", "")
                    if badge_name in badges:
                        badge_emojis.append(badges[badge_name])
                
                if badge_emojis:
                    badges_display = " ".join(badge_emojis)
                else:
                    badges_display = "None"

            presence_status_map = {
                0: {'icon_url': 'https://files.catbox.moe/tjiecu.png', 'text': 'Offline'},
                1: {'icon_url': 'https://files.catbox.moe/h69xeq.png', 'text': 'Online'},
                2: {'icon_url': 'https://files.catbox.moe/80ta5t.png', 'text': 'In Game'},
                3: {'icon_url': 'https://files.catbox.moe/72opoa.png', 'text': 'In Studio'}
            }

            current_status = presence_status_map.get(presence_type, presence_status_map[0])

            view = discord.ui.View()
            if not is_terminated:
                view.add_item(discord.ui.Button(
                    label="Roblox Profile",
                    style=discord.ButtonStyle.link,
                    emoji="<:RobloxLogo:1416951004607418398>",
                    url=profileurl
                ))
            view.add_item(discord.ui.Button(
                label="View Rolimons",
                style=discord.ButtonStyle.link,
                emoji="<:RolimonsLogo:1417258794974711901>",
                url=rolimonsurl
            ))

            friends_followers_text = f"-# **{friends_count:,}** Friends | **{followers_count:,}** Followers | **{followings_count:,}** Following"
            
            full_description = f"{friends_followers_text}\n\n{Description}"

            embed = discord.Embed(
                title=Username,
                url=profileurl,
                description=full_description,
                color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
            )
            
            if is_terminated:
                embed.add_field(name="ID", value=f"{UserID}", inline=True)
                embed.add_field(name="Terminated", value="True", inline=True)
                embed.add_field(name="Visits", value=f"{total_visits:,}", inline=True)
                
                embed.add_field(name="Created", value=created_timestamp if created_timestamp else "Unknown", inline=True)
                embed.add_field(name="Last Online", value=formatted_last_online, inline=True)
                embed.add_field(name="Badges", value=badges_display, inline=True)
            else:
                embed.add_field(name="ID", value=f"{UserID}", inline=True)
                embed.add_field(name="Verified", value="Hat" if is_verified else "False", inline=True)
                embed.add_field(name="Inventory", value=inventory_visibility, inline=True)
                
                embed.add_field(name="RAP", value=f"[`{rap_value:,}`]({rolimonsurl})", inline=True)
                embed.add_field(name="Value", value=f"[`{value_value:,}`]({rolimonsurl})", inline=True)
                embed.add_field(name="Visits", value=f"{total_visits:,}", inline=True)
                
                embed.add_field(name="Created", value=created_timestamp if created_timestamp else "Unknown", inline=True)
                embed.add_field(name="Last Online", value=formatted_last_online, inline=True)
                embed.add_field(name="Badges", value=badges_display, inline=True)
            
            if current_place_id and presence_type == 2:
                game_url = f"https://www.roblox.com/games/{current_place_id}"
                embed.add_field(
                    name="Current Game",
                    value=f"[Click to View Game]({game_url})",
                    inline=False
                )
            
            if Discord != None:
                embed.add_field(
                    name="Discord",
                    value=f"```txt\n{Discord}\n```",
                    inline=False
                )

            if avatar_image:
                embed.set_thumbnail(url=avatar_image)
            
            embed.set_footer(
                text=f"{current_status['text']} | Requested by {interaction.user.name} | https://shapes.lol",
                icon_url=current_status['icon_url']
            )
            
            await interaction.edit_original_response(embed=embed, view=view)
            return
        else:
            print(f"{user} not found.")
            failedembed7 = discord.Embed(
                title=f":warning: {user} not found.",
                color=discord.Color.yellow()
            )
            await interaction.edit_original_response(embed=failedembed7)
            return
    except requests.exceptions.RequestException as e:
        print(f"An error occurred during the request: {e}")
        failedembed8 = discord.Embed(
            title=f":warning: {user} not found.",
            color=discord.Color.yellow()
        )
        await interaction.edit_original_response(embed=failedembed8)
        return
        
@bot.tree.command(name="britishuser", description="Check if a user has their language set to British")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def british_check(interaction: discord.Interaction, user_input: str):
    await interaction.response.defer(thinking=True)
    
    embed_color = embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
    
    thinkingembed = discord.Embed(
        title=f"{Emojis.get('loading')}  {interaction.user.mention} Checking if {user_input} is British!",
        color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
    )
    await interaction.followup.send(embed=thinkingembed)

    url = "https://users.roblox.com/v1/usernames/users"
    
    request_payload = {
        "usernames": [user_input],
        "excludeBannedUsers": False
    }

    try:
        response = requests.post(url, json=request_payload)
        response.raise_for_status()
        data = response.json()
        if data.get("data") and len(data["data"]) > 0:
            userinfo = data["data"][0]
            UserID = userinfo["id"]
            username = userinfo["name"]

            payload = json.dumps([{"name": "vieweeUserId", "type": "UserId", "value": int(UserID)}])
            b64encoded = base64.b64encode(payload.encode('utf-8')).decode('utf-8')
            
            british_url = f"https://apis.roblox.com/access-management/v1/upsell-feature-access?featureName=MustHideConnections&extraParameters={b64encoded}"
            british_response = requests.get(british_url)
            british_response.raise_for_status()
            british_data = british_response.json()
            
            is_british = british_data.get("access") == "Granted"

            avatar_url = f"https://thumbnails.roblox.com/v1/users/avatar-bust?userIds={UserID}&size=150x150&format=Png&isCircular=false"
            avatar_response = requests.get(avatar_url)
            avatar_data = avatar_response.json()
            avatar_thumbnail = avatar_data["data"][0]["imageUrl"] if avatar_data.get("data") and len(avatar_data["data"]) > 0 else None

            if is_british:
                embed = discord.Embed(
                    title=":flag_gb: British Check Result",
                    description=f"**{username}** is British! :flag_gb:",
                    color=embed_color
                )
            else:
                embed = discord.Embed(
                    title=":x: British Check Result",
                    description=f"**{username}** is not British :x:",
                    color=embed_color
                )
            
            if avatar_thumbnail:
                embed.set_thumbnail(url=avatar_thumbnail)
            
            embed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
            await interaction.edit_original_response(embed=embed)
            
        else:
            embed = discord.Embed(
                title=f":warning: {user_input} not found",
                color=embed_color
            )
            embed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
            await interaction.edit_original_response(embed=embed)
            
    except requests.exceptions.RequestException as e:
        embed = discord.Embed(
            title=":x: API Error",
            description=f"An error occurred: {str(e)}",
            color=embed_color
        )
        embed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
        await interaction.edit_original_response(embed=embed)

@bot.tree.command(name="iteminfo", description="Get detailed information about a Roblox item")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def item(interaction: discord.Interaction, item_query: str = "Dominus Empyreus"):
    await interaction.response.defer(thinking=True)
    
    print(f"Searching For {item_query}'s item info")
    thinkingembed = discord.Embed(
        title=f"{Emojis.get('loading')}  {interaction.user.mention} Searching For {item_query}'s Item Information!",
        color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
    )
    await interaction.followup.send(embed=thinkingembed)

    if not item_query.isdigit():
        search_url = f"https://catalog.roblox.com/v1/search/items?category=All&limit=10&keyword={urllib.parse.quote(item_query)}"
        
        try:
            response = requests.get(search_url)
            response.raise_for_status()
            search_data = response.json()
            
            if search_data.get("data") and len(search_data["data"]) > 0:
                item_id = None
                for item in search_data["data"]:
                    if item.get("name", "").lower() == item_query.lower():
                        item_id = item.get("id")
                        break
                
                if not item_id:
                    item_id = search_data["data"][0].get("id")
                
                if not item_id:
                    failedembed = discord.Embed(
                        title=f":warning: {item_query} not found.",
                        color=discord.Color.yellow()
                    )
                    await interaction.edit_original_response(embed=failedembed)
                    return
            else:
                failedembed = discord.Embed(
                    title=f":warning: {item_query} not found.",
                    color=discord.Color.yellow()
                )
                await interaction.edit_original_response(embed=failedembed)
                return
                
        except requests.exceptions.RequestException as e:
            print(f"Error searching for item {item_query}: {e}")
            failedembed = discord.Embed(
                title=f":x: An error occurred while searching for {item_query}. Please try again later.",
                color=discord.Color.red()
            )
            await interaction.edit_original_response(embed=failedembed)
            return
    else:
        item_id = item_query

    url = f"https://catalog.roblox.com/v1/catalog/items/{item_id}/details?itemType=Asset"

    try:
        response = requests.get(url)
        response.raise_for_status()
        item_data = response.json()
        print(f"ItemData: {item_data}")

        name = item_data.get("name", "Unknown Item")
        description = item_data.get("description", "No description available")
        creator_name = item_data.get("creatorName", "Unknown Creator")
        creator_type = item_data.get("creatorType", "User")
        creator_verified = item_data.get("creatorHasVerifiedBadge", False)
        creator_target_id = item_data.get("creatorTargetId")
        favorite_count = item_data.get("favoriteCount", 0)
        lowest_price = item_data.get("lowestPrice", 0)
        is_purchasable = item_data.get("isPurchasable", False)
        item_type = item_data.get("itemType", "Asset")
        
        created_date = item_data.get("itemCreatedUtc")
        
        if creator_type == "Group" and creator_target_id == 5544706:
            creator_verified = True

        if creator_type == "User" and creator_target_id == 124767284:
            creator_verified = True
        
        if created_date:
            created_timestamp = isotodiscordtimestamp(created_date, "F")
        else:
            created_timestamp = "Unknown"
        
        updated_timestamp = created_timestamp

        creator_display = creator_name
        if creator_verified:
            creator_display += " <:RobloxVerified:1416951927513677874>"
        else:
            creator_display += f" ({creator_type})"

        price_display = "Not for sale"
        if is_purchasable and lowest_price is not None:
            price_display = "Free" if lowest_price == 0 else f"{lowest_price:,} Robux"

        thumbnail_url = f"https://thumbnails.roblox.com/v1/assets?assetIds={item_id}&size=420x420&format=Png"
        try:
            thumb_response = requests.get(thumbnail_url)
            thumb_response.raise_for_status()
            thumb_data = thumb_response.json()
            if thumb_data and thumb_data.get("data") and len(thumb_data["data"]) > 0:
                image_url = thumb_data["data"][0].get("imageUrl")
            else:
                image_url = None
        except requests.exceptions.RequestException as e:
            print(f"Error fetching item thumbnail: {e}")
            image_url = None

        embed = discord.Embed(
            title=name,
            url=f"https://www.roblox.com/catalog/{item_id}/",
            description=description,
            color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
        )

        if image_url:
            embed.set_thumbnail(url=image_url)

        embed.add_field(name="Item ID", value=item_id, inline=True)
        embed.add_field(name="Item Type", value=item_type, inline=True)
        embed.add_field(name="Price", value=price_display, inline=True)
        embed.add_field(name="Creator", value=creator_display, inline=True)
        embed.add_field(name="Favorites", value=f"{favorite_count:,}", inline=True)
        embed.add_field(name="Created", value=created_timestamp, inline=True)

        view = discord.ui.View()
        view.add_item(discord.ui.Button(
            label="View Item",
            style=discord.ButtonStyle.link,
            emoji="<:RobloxLogo:1416951004607418398>",
            url=f"https://www.roblox.com/catalog/{item_id}/"
        ))

        embed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
        await interaction.edit_original_response(embed=embed, view=view)

    except requests.exceptions.RequestException as e:
        print(f"Error fetching item data for ID {item_id}: {e}")
        failedembed = discord.Embed(
            title=f":x: An error occurred while fetching data for item ID: {item_id}. Please try again later.",
            color=discord.Color.red()
        )
        await interaction.edit_original_response(embed=failedembed)
        return

@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@bot.tree.command(name="placeinfo", description="Get detailed information about a Roblox place")
@app_commands.describe(game_input="Roblox place ID or game URL")
async def placeinfo(interaction: discord.Interaction, game_input: str):
    await interaction.response.defer(thinking=True)
    
    thinkingembed = discord.Embed(
        title=f"{Emojis.get('loading')}  {interaction.user.mention} Searching For Place!",
        color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
    )
    await interaction.followup.send(embed=thinkingembed)
    
    try:
        connector = aiohttp.TCPConnector(family=socket.AF_INET)
        async with aiohttp.ClientSession(connector=connector) as session:
            place_id = None
            
            if "roblox.com/games/" in game_input:
                match = re.search(r'roblox\.com/games/(\d+)', game_input)
                if match:
                    place_id = match.group(1)
                else:
                    errorembed = discord.Embed(
                        title=":x: Invalid URL :x:",
                        description="Could not extract place ID from the URL",
                        color=discord.Color.red()
                    )
                    await interaction.edit_original_response(embed=errorembed)
                    return
            else:
                if not game_input.isdigit():
                    errorembed = discord.Embed(
                        title=":x: Invalid Input :x:",
                        description="Please provide a valid place ID or Roblox game URL",
                        color=discord.Color.red()
                    )
                    await interaction.edit_original_response(embed=errorembed)
                    return
                place_id = game_input
            
            universe_url = f"https://apis.roblox.com/universes/v1/places/{place_id}/universe"
            async with session.get(universe_url) as response:
                if response.status != 200:
                    errorembed = discord.Embed(
                        title=":x: API Error :x:",
                        description=f"Failed to fetch universe information (Status: {response.status})",
                        color=discord.Color.red()
                    )
                    await interaction.edit_original_response(embed=errorembed)
                    return
                
                universe_data = await response.json()
                universe_id = universe_data.get('universeId')
                
                if not universe_id:
                    errorembed = discord.Embed(
                        title=":x: Not Found :x:",
                        description="Could not find universe for this place ID",
                        color=discord.Color.red()
                    )
                    await interaction.edit_original_response(embed=errorembed)
                    return
            
            games_url = f"https://games.roblox.com/v1/games?universeIds={universe_id}"
            async with session.get(games_url) as response:
                if response.status != 200:
                    errorembed = discord.Embed(
                        title=":x: API Error :x:",
                        description=f"Failed to fetch game details (Status: {response.status})",
                        color=discord.Color.red()
                    )
                    errorembed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
                    await interaction.edit_original_response(embed=errorembed)
                    return
                
                games_data = await response.json()
                if not games_data.get('data') or len(games_data['data']) == 0:
                    errorembed = discord.Embed(
                        title=":x: Not Found :x:",
                        description="Could not find game details for this universe",
                        color=discord.Color.red()
                    )
                    errorembed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
                    await interaction.edit_original_response(embed=errorembed)
                    return
                
                game_info = games_data['data'][0]
            
            name = game_info.get('name', 'Unknown')
            description = game_info.get('description', 'No description available')
            creator_id = game_info.get('creator', {}).get('id', 'Unknown')
            creator_name = game_info.get('creator', {}).get('name', 'Unknown')
            creator_type = game_info.get('creator', {}).get('type', 'User')
            current_players = game_info.get('playing', 0)
            visits = game_info.get('visits', 0)
            max_players = game_info.get('maxPlayers', 0)
            created = game_info.get('created', 'Unknown')
            updated = game_info.get('updated', 'Unknown')
            genre = game_info.get('genre', 'Unknown')
            favorites_count = game_info.get('favoritedCount', 0)
        
            created_timestamp = isotodiscordtimestamp(created, "F") if created != 'Unknown' else "Unknown"
            updated_timestamp = isotodiscordtimestamp(updated, "F") if updated != 'Unknown' else "Unknown"
            
            thumbnail_url = f"https://thumbnails.roblox.com/v1/games/icons?universeIds={universe_id}&size=512x512&format=Png&isCircular=false"
            thumbnail_image = None
            async with session.get(thumbnail_url) as response:
                if response.status == 200:
                    thumbnail_data = await response.json()
                    if thumbnail_data.get('data') and len(thumbnail_data['data']) > 0:
                        thumbnail_image = thumbnail_data['data'][0]['imageUrl']
            
            embed = discord.Embed(
                title=name,
                url=f"https://www.roblox.com/games/{place_id}/",
                description=description,
                color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
            )
        
            embed.add_field(name="Place ID", value=f"`{place_id}`", inline=True)
            embed.add_field(name="Universe ID", value=f"`{universe_id}`", inline=True)
            
            if creator_type.lower() == "user":
                creator_display = f"[{creator_name}](https://www.roblox.com/users/{creator_id}/profile)"
            else:
                creator_display = f"[{creator_name}](https://www.roblox.com/groups/{creator_id}/)"
            embed.add_field(name="Creator", value=creator_display, inline=True)

            embed.add_field(name="Visits", value=f"{visits:,}", inline=True)
            embed.add_field(name="Current Players", value=f"{current_players:,}", inline=True)
            embed.add_field(name="Max Players", value=f"{max_players}", inline=True)
            
            embed.add_field(name="Favorites", value=f"{favorites_count:,}", inline=True)
            embed.add_field(name="Genre", value=genre, inline=True)
            
            embed.add_field(name="Created", value=created_timestamp, inline=True)
            embed.add_field(name="Updated", value=updated_timestamp, inline=True)
            
            if description and description != "No description available":
                if len(description) > 1024:
                    description = description[:1021] + "..."
                if len(embed.fields) % 3 != 0:
                    embed.add_field(name="\u200b", value="\u200b", inline=True)
                embed.add_field(name="Description", value=description, inline=False)
            
            if thumbnail_image:
                embed.set_thumbnail(url=thumbnail_image)
            
            view = discord.ui.View()
            view.add_item(discord.ui.Button(
                label="View Game",
                style=discord.ButtonStyle.link,
                emoji="<:RobloxLogo:1416951004607418398>",
                url=f"https://www.roblox.com/games/{place_id}/"
            ))
            
            embed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
            await interaction.edit_original_response(embed=embed, view=view)
            
    except Exception as e:
        print(f"Error in placeinfo command: {e}")
        errorembed = discord.Embed(
            title=":x: An error occurred :x:",
            description="Failed to fetch place information. Please try again later.",
            color=discord.Color.red()
        )
        errorembed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
        await interaction.edit_original_response(embed=errorembed)
        return

async def send_error_embed(interaction: discord.Interaction, title: str, description: str):
    embed = discord.Embed(
        title=f":x: {title}",
        description=description,
        color=discord.Color.red()
    )
    #embed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
    await interaction.followup.send(embed=embed)

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

async def create_badge_embed(badge_data: dict, thumbnail_url: Optional[str], badge_id: str, 
                           start_time: float, requester: discord.User) -> discord.Embed:
    badge_id = badge_data.get('id', 'N/A')
    name = badge_data.get('name', 'Unknown')
    display_name = badge_data.get('displayName', name)
    description = badge_data.get('description') or badge_data.get('displayDescription') or "No description"
    
    statistics = badge_data.get('statistics', {})
    past_day_awarded = statistics.get('pastDayAwardedCount', 0)
    awarded_count = statistics.get('awardedCount', 0)
    win_rate = statistics.get('winRatePercentage', 0)
    
    created = badge_data.get('created', 'Unknown')
    updated = badge_data.get('updated', 'Unknown')
    
    awarding_universe = badge_data.get('awardingUniverse', {})
    universe_name = awarding_universe.get('name', 'Unknown')
    universe_id = awarding_universe.get('id', 'N/A')
    root_place_id = awarding_universe.get('rootPlaceId', 'N/A')
    
    embed_color = embedDB.get(f"{requester.id}") if embedDB.get(f"{requester.id}") else discord.Color.blue()
    
    embed = discord.Embed(
        title=f"Badge: {display_name}",
        color=embed_color,
        timestamp=datetime.now(),
        url=f"https://www.roblox.com/badges/{badge_id}"
    )
    
    if thumbnail_url:
        embed.set_thumbnail(url=thumbnail_url)
    
    embed.add_field(name="Badge ID", value=f"`{badge_id}`", inline=True)
    embed.add_field(name="Internal Name", value=name, inline=True)
    embed.add_field(name="Enabled", value="Yes" if badge_data.get('enabled') else "No", inline=True)
    
    embed.add_field(name="Description", value=description, inline=False)
    
    embed.add_field(name="Awarded Today", value=f"{past_day_awarded:,}", inline=True)
    embed.add_field(name="Obtained Total", value=f"{awarded_count:,}", inline=True)
    embed.add_field(name="Win Rate", value=f"{win_rate}%", inline=True)
    
    created_timestamp = isotodiscordtimestamp(created)
    updated_timestamp = isotodiscordtimestamp(updated)
    
    if created_timestamp:
        embed.add_field(
            name="Created", 
            value=f"{created_timestamp} {created_timestamp})", 
            inline=True
        )
    else:
        embed.add_field(name="Created", value=created, inline=True)
    
    if updated_timestamp:
        embed.add_field(
            name="Last Updated", 
            value=f"<t:{updated_timestamp}:f> (<t:{updated_timestamp}:R>)", 
            inline=True
        )
    else:
        embed.add_field(name="Last Updated", value=updated, inline=True)
    
    if universe_id != 'N/A':
        universe_field = f"[{universe_name}](https://www.roblox.com/games/{root_place_id}/)"
        universe_field += f"\nUniverse ID: `{universe_id}`"
        if root_place_id != 'N/A':
            universe_field += f"\nPlace ID: `{root_place_id}`"
        
        embed.add_field(name="Awarding Universe", value=universe_field, inline=False)
    
    elapsed_time = asyncio.get_event_loop().time() - start_time
    embed.set_footer(text=f"Load time: {elapsed_time:.2f}s • Requested by {requester.display_name} | {MainURL}")
    
    return embed

@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@bot.tree.command(name="badge", description="Get detailed information about a Roblox badge")
@app_commands.describe(badge_id="The ID of the Roblox badge")
async def badge_info(interaction: discord.Interaction, badge_id: str):
    await interaction.response.defer(thinking=True)
    start_time = asyncio.get_event_loop().time()

    thinkingembed = discord.Embed(
    title=f"{Emojis.get('loading')}  {interaction.user.mention} Searching For Place!",
    color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
    )
    await interaction.followup.send(embed=thinkingembed)
    
    if not badge_id.isdigit():
        await send_error_embed(
            interaction,
            "Invalid Badge ID",
            "Please provide a valid numeric badge ID."
        )
        return
    
    connector = aiohttp.TCPConnector(family=socket.AF_INET)
    async with aiohttp.ClientSession(connector=connector) as session:
        try:
            badge_url = f"https://badges.roblox.com/v1/badges/{badge_id}"
            
            async with session.get(badge_url) as response:
                if response.status == 404:
                    await send_error_embed(
                        interaction,
                        "Badge Not Found",
                        f"Could not find a badge with ID `{badge_id}`"
                    )
                    return
                elif response.status != 200:
                    await send_error_embed(
                        interaction,
                        "API Error",
                        f"Failed to fetch badge information (Status: {response.status})"
                    )
                    return
                
                badge_data = await response.json()
            
            thumbnail_url = await get_badge_thumbnail(session, badge_id)
            
            embed = await create_badge_embed(badge_data, thumbnail_url, badge_id, start_time, interaction.user)
            
            view = discord.ui.View()
            view.add_item(discord.ui.Button(
                label="View Badge",
                style=discord.ButtonStyle.link,
                emoji="<:RobloxLogo:1416951004607418398>",
                url=f"https://www.roblox.com/badges/{badge_id}"
            ))
            
            await interaction.edit_original_response(embed=embed, view=view)
            
        except Exception as e:
            await send_error_embed(interaction, "Unexpected Error", f"An error occurred: {str(e)}")
            
@bot.tree.command(name="autorole", description="Set a role to be automatically given to members")
@app_commands.default_permissions(administrator=True)
@commands.bot_has_permissions(add_reactions=True, moderate_members=True, read_message_history=True, view_channel=True, send_messages=True)
@app_commands.allowed_installs(guilds=True, users=False)
@app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
@app_commands.describe(
    role="The role to automatically assign to members",
    enable="Whether to enable or disable autorole (default: True)"
)
async def autorole(interaction: discord.Interaction, role: discord.Role, enable: bool = True):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("You need administrator permissions to use this command.", ephemeral=True)
        return
    
    autorole_data = {"role_id": role.id, "enabled": enable}
    autoroleDB.set(f"{interaction.guild.id}", autorole_data)
    autoroleDB.save()
    
    class AutoroleView(discord.ui.View):
        @discord.ui.button(label="Assign to Existing Members", style=discord.ButtonStyle.primary)
        async def assign_existing(self, interaction: discord.Interaction, button: discord.ui.Button):
            count = 0
            for member in interaction.guild.members:
                if not member.bot and role not in member.roles:
                    try:
                        await member.add_roles(role)
                        count += 1
                    except:
                        pass
            await interaction.response.send_message(f"Assigned {role.mention} to {count} existing members", ephemeral=True)
    
    status = "enabled" if enable else "disabled"
    await interaction.response.send_message(
        f"Autorole {status} for {role.mention}. Assign to existing members?", 
        view=AutoroleView(), 
        ephemeral=True
    )

class HelpDropdown(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Counting Commands", description="Counting game and settings"),
            discord.SelectOption(label="User Settings", description="Personal bot settings"),
            discord.SelectOption(label="Roblox Commands", description="Roblox user and item information"),
            discord.SelectOption(label="Utility Commands", description="General utility commands"),
            discord.SelectOption(label="Context Menu Commands", description="Right-click context commands")
        ]
        super().__init__(placeholder="Choose a command category...", options=options)

    async def callback(self, interaction: discord.Interaction):
        category = self.values[0]
        embed_color = embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
        
        if category == "Counting Commands":
            embed = discord.Embed(title="Counting Commands", color=embed_color)
            embed.add_field(name="Commands", value=
                "• /counting - Counting in a specific channel", inline=False)
                
        elif category == "User Settings":
            embed = discord.Embed(title="User Settings", color=embed_color)
            embed.add_field(name="Commands", value=
                "• /settings - Customize your personal bot settings", inline=False)
                
        elif category == "Roblox Commands":
            embed = discord.Embed(title="Roblox Commands", color=embed_color)
            embed.add_field(name="Commands", value=
                "• /robloxinfo - Get detailed Roblox user information\n"
                "• /roblox2discord - Find a Roblox user's Discord\n"
                "• /britishuser - Check if user is british\n"
                "• /iteminfo - Get Roblox item information\n"
                "• /groupinfo - Get Roblox group information\n"
                "• /placeinfo - Get Roblox place/game information\n"
                "• /badge - Get Roblox badge information", inline=False)
                
        elif category == "Utility Commands":
            embed = discord.Embed(title="Utility Commands", color=embed_color)
            embed.add_field(name="Commands", value=
                "• /ping - Check bot latency and connection\n"
                "• /invite - Get bot invite link\n"
                "• /google - Search something on Google\n"
                "• /status - Check shapes.lol status\n"
                "• /userinstalls - Get user installation count\n"
                "• /servercount - Get server count\n"
                "• /spookpfp - Get profile picture from spook.bio", inline=False)
                
        elif category == "Context Menu Commands":
            embed = discord.Embed(title="Context Menu Commands", color=embed_color)
            embed.add_field(name="Commands", value=
                "• Right-click a user → Apps → sayhitouser - Say hello to a user\n"
                "• Right-click a user → Apps → discord2spook - Get user's spook.bio profile\n"
                "• Right-click a message → Apps → google - Search message content on Google", inline=False)
        
        embed.set_footer(text="Use slash commands (/) to interact")
        await interaction.response.edit_message(embed=embed, ephemeral=True)

class HelpView(discord.ui.View):
    def __init__(self):
        super().__init__()
        self.add_item(HelpDropdown())

@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@bot.tree.command(name="help", description="Get help with all available commands")
async def help_command(interaction: discord.Interaction):
    """Display help information for all bot commands"""
    
    embed_color = embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
    
    embed = discord.Embed(
        title="Bot Help & Commands",
        description="Use the dropdown menu below to browse commands by category.",
        color=embed_color
    )
    
    view = HelpView()
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

@bot.tree.command(name="countingleaderboard", description="View the counting leaderboard website")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def countingleaderboard(interaction: discord.Interaction):
    """Send the counting leaderboard website link"""
    
    embed = discord.Embed(
        title="Counting Leaderboard",
        description=f"[Click here to view the counting leaderboard](https://shapes.lol/counting/leaderboard/)",
        color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
    )
    embed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
    
    await interaction.response.send_message(embed=embed)
            
@bot.tree.command(name="outfits", description="Get all outfits for a Roblox user")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def outfits(interaction: discord.Interaction, user: str = "Roblox"):
    await interaction.response.defer(thinking=True)
    
    print(f"Searching For {user}'s outfits")
    thinkingembed = discord.Embed(
        title=f"{Emojis.get('loading')}  {interaction.user.mention} Searching For {user}'s Roblox outfits!",
        color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
    )
    await interaction.followup.send(embed=thinkingembed)

    url = "https://users.roblox.com/v1/usernames/users"
    
    request_payload = {
        "usernames": [user],
        "excludeBannedUsers": False
    }

    async def get_outfit_thumbnails(session: aiohttp.ClientSession, outfit_ids: list) -> dict:
        """Get thumbnails for multiple outfits"""
        thumbnails = {}
        if not outfit_ids:
            return thumbnails
            
        for i in range(0, len(outfit_ids), 100):
            chunk = outfit_ids[i:i+100]
            ids_param = ",".join(str(outfit_id) for outfit_id in chunk)
            
            thumbnails_url = f"https://thumbnails.roblox.com/v1/users/outfits?userOutfitIds={ids_param}&size=420x420&format=Png"
            
            try:
                async with session.get(thumbnails_url) as response:
                    if response.status == 200:
                        thumbnails_data = await response.json()
                        for item in thumbnails_data.get('data', []):
                            thumbnails[item.get("targetId")] = item.get("imageUrl", "")
            except Exception as e:
                print(f"Error fetching thumbnails: {e}")
            
            if i + 100 < len(outfit_ids):
                await asyncio.sleep(1)
                
        return thumbnails

    try:
        response = requests.post(url, json=request_payload)
        response.raise_for_status()
        data = response.json()
        if data.get("data") and len(data["data"]) > 0:
            userinfo = data["data"][0]
            UserID = userinfo["id"]
            Display = userinfo["displayName"]
            hasVerifiedBadge = userinfo.get("hasVerifiedBadge", False)

            username = Display
            user_id = UserID

            if UserID == 124767284:
                hasVerifiedBadge = True

            outfits_url = f"https://avatar.roblox.com/v1/users/{user_id}/outfits?itemsPerPage=150&isEditable=true"
            
            try:
                connector = aiohttp.TCPConnector(family=socket.AF_INET)
                async with aiohttp.ClientSession(connector=connector) as session:
                    async with session.get(outfits_url) as response:
                        if response.status == 429:
                            failedembed = discord.Embed(
                                title=f":warning: Rate Limited",
                                description="Too many requests. Please try again in a few seconds.",
                                color=discord.Color.yellow()
                            )
                            await interaction.edit_original_response(embed=failedembed)
                            return
                        elif response.status != 200:
                            failedembed = discord.Embed(
                                title=f":warning: Failed to fetch outfits",
                                description=f"API returned status code: {response.status}",
                                color=discord.Color.yellow()
                            )
                            await interaction.edit_original_response(embed=failedembed)
                            return
                        
                        outfits_data = await response.json()
            except Exception as e:
                print(f"Error fetching outfits: {e}")
                failedembed = discord.Embed(
                    title=f":x: An error occurred while fetching outfits",
                    description="Please try again later.",
                    color=discord.Color.red()
                )
                await interaction.edit_original_response(embed=failedembed)
                return

            outfits = outfits_data.get('data', [])
            
            if not outfits:
                embed = discord.Embed(
                    title=f"{username}'s Outfits",
                    description="This user has no outfits.",
                    color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
                )
                embed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
                await interaction.edit_original_response(embed=embed)
                return

            outfit_ids = [outfit.get('id') for outfit in outfits if outfit.get('id')]
            outfit_thumbnails = {}
            
            try:
                connector = aiohttp.TCPConnector(family=socket.AF_INET)
                async with aiohttp.ClientSession(connector=connector) as session:
                    outfit_thumbnails = await get_outfit_thumbnails(session, outfit_ids)
            except Exception as e:
                print(f"Error fetching outfit thumbnails: {e}")

            class OutfitsView(discord.ui.View):
                def __init__(self, outfits, thumbnails, username, user_id, requester):
                    super().__init__(timeout=120)
                    self.outfits = outfits
                    self.thumbnails = thumbnails
                    self.current_page = 0
                    self.username = username
                    self.user_id = user_id
                    self.requester = requester
                    self.message = None
                    self.update_buttons()
                    
                async def create_embed(self) -> discord.Embed:
                    outfit = self.outfits[self.current_page]
                    outfit_id = outfit.get('id')
                    outfit_name = outfit.get('name', 'Unnamed Outfit')
                    created_date = outfit.get('created', '')
                    is_editable = outfit.get('isEditable', False)
                    
                    thumbnail_url = self.thumbnails.get(outfit_id, "")
                    
                    embed = discord.Embed(
                        title=f"{self.username}'s Outfits",
                        color=embedDB.get(f"{self.requester.id}") if embedDB.get(f"{self.requester.id}") else discord.Color.blue()
                    )
                    
                    if thumbnail_url:
                        embed.set_image(url=thumbnail_url)
                    else:
                        embed.description = "*Thumbnail not available*"
                    
                    embed.add_field(name="Outfit Name", value=outfit_name, inline=False)
                    embed.add_field(name="Outfit ID", value=f"`{outfit_id}`", inline=True)
                    embed.add_field(name="Editable", value="Yes" if is_editable else "No", inline=True)
                    
                    if created_date:
                        try:
                            created_timestamp = isotodiscordtimestamp(created_date, "F")
                            if created_timestamp:
                                embed.add_field(name="Created", value=created_timestamp, inline=True)
                        except:
                            pass
                    
                    embed.set_footer(
                        text=f"Outfit {self.current_page + 1}/{len(self.outfits)} | Requested by {self.requester.name} | {MainURL}"
                    )
                    
                    return embed
                
                def update_buttons(self):
                    self.clear_items()
                    
                    if self.current_page > 0:
                        previous_btn = discord.ui.Button(style=discord.ButtonStyle.primary, emoji="⬅️", custom_id="previous")
                        previous_btn.callback = self.previous_callback
                        self.add_item(previous_btn)
                        
                    if self.current_page < len(self.outfits) - 1:
                        next_btn = discord.ui.Button(style=discord.ButtonStyle.primary, emoji="➡️", custom_id="next")
                        next_btn.callback = self.next_callback
                        self.add_item(next_btn)
                    
                    current_outfit = self.outfits[self.current_page]
                    outfit_id = current_outfit.get('id')
                    if outfit_id:
                        link_btn = discord.ui.Button(
                            style=discord.ButtonStyle.link,
                            label="View Outfit",
                            url=f"https://www.roblox.com/outfits/{outfit_id}"
                        )
                        self.add_item(link_btn)
                
                async def previous_callback(self, interaction: discord.Interaction):
                    if interaction.user != self.requester:
                        await interaction.response.send_message("This is not your command!", ephemeral=True)
                        return
                        
                    self.current_page -= 1
                    embed = await self.create_embed()
                    self.update_buttons()
                    await interaction.response.edit_message(embed=embed, view=self)
                
                async def next_callback(self, interaction: discord.Interaction):
                    if interaction.user != self.requester:
                        await interaction.response.send_message("This is not your command!", ephemeral=True)
                        return
                        
                    self.current_page += 1
                    embed = await self.create_embed()
                    self.update_buttons()
                    await interaction.response.edit_message(embed=embed, view=self)
                
                async def on_timeout(self):
                    for item in self.children:
                        if isinstance(item, discord.ui.Button) and item.style != discord.ButtonStyle.link:
                            item.disabled = True
                    try:
                        await self.message.edit(view=self)
                    except Exception:
                        pass

            view = OutfitsView(outfits, outfit_thumbnails, username, user_id, interaction.user)
            embed = await view.create_embed()
            message = await interaction.edit_original_response(embed=embed, view=view)
            view.message = message
            return
        else:
            print(f"{user} not found.")
            failedembed = discord.Embed(
                title=f":warning: {user} not found.",
                color=discord.Color.yellow()
            )
            await interaction.edit_original_response(embed=failedembed)
            return
    except requests.exceptions.RequestException as e:
        print(f"An error occurred during the request: {e}")
        failedembed = discord.Embed(
            title=f":warning: {user} not found.",
            color=discord.Color.yellow()
        )
        await interaction.edit_original_response(embed=failedembed)
        return
        
@bot.tree.command(name="badges", description="Check if a user has a specific badge and get detailed information")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(
    username="Roblox username to check",
    badge_id="The badge ID to look for (optional)"
)
async def badges(interaction: discord.Interaction, username: str, badge_id: str = None):
    await interaction.response.defer(thinking=True)
    
    thinkingembed = discord.Embed(
        title=f"{Emojis.get('loading')}  {interaction.user.mention} Checking {username}'s badges!",
        color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
    )
    await interaction.followup.send(embed=thinkingembed)

    async def get_user_id():
        user_url = "https://users.roblox.com/v1/usernames/users"
        request_payload = {"usernames": [username], "excludeBannedUsers": False}
        async with aiohttp.ClientSession() as session:
            async with session.post(user_url, json=request_payload) as response:
                if response.status == 200:
                    data = await response.json()
                    return data, None
                return None, f"Failed to fetch user data (Status: {response.status})"

    async def get_user_badges(user_id):
        user_badges_url = f"https://badges.roblox.com/v1/users/{user_id}/badges?limit=100&sortOrder=Asc"
        async with aiohttp.ClientSession() as session:
            async with session.get(user_badges_url) as response:
                if response.status == 200:
                    data = await response.json()
                    return data, None
                return None, f"Failed to fetch badges (Status: {response.status})"

    async def get_badge_info(badge_id):
        badge_info_url = f"https://badges.roblox.com/v1/badges/{badge_id}"
        async with aiohttp.ClientSession() as session:
            async with session.get(badge_info_url) as response:
                if response.status == 200:
                    data = await response.json()
                    return data, None
                return None, f"Failed to fetch badge info (Status: {response.status})"

    async def get_awarded_date(user_id, badge_id):
        awarded_date_url = f"https://badges.roblox.com/v1/users/{user_id}/badges/awarded-dates?badgeIds={badge_id}"
        async with aiohttp.ClientSession() as session:
            async with session.get(awarded_date_url) as response:
                if response.status == 200:
                    data = await response.json()
                    return data, None
                return None, f"Failed to fetch awarded date (Status: {response.status})"

    async def get_badge_thumbnail(badge_id):
        thumbnail_url = f"https://thumbnails.roblox.com/v1/badges/icons?badgeIds={badge_id}&size=150x150&format=Png"
        async with aiohttp.ClientSession() as session:
            async with session.get(thumbnail_url) as response:
                if response.status == 200:
                    data = await response.json()
                    if data.get('data') and len(data['data']) > 0:
                        return data['data'][0]['imageUrl']
        return None

    try:
        user_data, error = await get_user_id()
        
        if error or not user_data.get("data") or len(user_data["data"]) == 0:
            errorembed = discord.Embed(
                title=f":x: User Not Found :x:",
                description=f"Could not find a Roblox user with the username `{username}`",
                color=discord.Color.red()
            )
            errorembed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
            await interaction.edit_original_response(embed=errorembed)
            return
            
        user_info = user_data["data"][0]
        user_id = user_info["id"]
        roblox_username = user_info["name"]
        
        badges_data, error = await get_user_badges(user_id)
        
        if error:
            errorembed = discord.Embed(
                title=f":x: Error Fetching Badges :x:",
                description=f"Failed to fetch badges for user `{username}`",
                color=discord.Color.red()
            )
            errorembed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
            await interaction.edit_original_response(embed=errorembed)
            return
        
        user_badges = badges_data.get("data", [])
        
        if not user_badges:
            errorembed = discord.Embed(
                title=f":x: No Badges Found :x:",
                description=f"User `{username}` has no badges",
                color=discord.Color.red()
            )
            errorembed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
            await interaction.edit_original_response(embed=errorembed)
            return
        
        if badge_id:
            target_badge = None
            for badge in user_badges:
                if str(badge.get("id")) == badge_id:
                    target_badge = badge
                    break
            
            if not target_badge:
                errorembed = discord.Embed(
                    title=f":x: Badge Not Found :x:",
                    description=f"User `{username}` does not have badge ID `{badge_id}`",
                    color=discord.Color.red()
                )
                errorembed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
                await interaction.edit_original_response(embed=errorembed)
                return
            
            badge_info_task = asyncio.create_task(get_badge_info(badge_id))
            awarded_date_task = asyncio.create_task(get_awarded_date(user_id, badge_id))
            thumbnail_task = asyncio.create_task(get_badge_thumbnail(badge_id))
            
            badge_info, badge_error = await badge_info_task
            awarded_data, awarded_error = await awarded_date_task
            badge_thumbnail = await thumbnail_task
            
            if badge_error:
                errorembed = discord.Embed(
                    title=f":x: Error Fetching Badge Info :x:",
                    description=f"Failed to fetch detailed information for badge ID `{badge_id}`",
                    color=discord.Color.red()
                )
                errorembed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
                await interaction.edit_original_response(embed=errorembed)
                return
            
            awarded_date = "Unknown"
            if awarded_data and awarded_data.get("data") and len(awarded_data["data"]) > 0:
                awarded_date_str = awarded_data["data"][0].get("awardedDate")
                if awarded_date_str:
                    awarded_timestamp = isotodiscordtimestamp(awarded_date_str, "F")
                    awarded_date = awarded_timestamp if awarded_timestamp else awarded_date_str
            
            badge_name = badge_info.get("displayName") or badge_info.get("name", "Unknown Badge")
            badge_description = badge_info.get("displayDescription") or badge_info.get("description", "No description available")
            badge_enabled = badge_info.get("enabled", False)
            
            created_date = badge_info.get("created", "Unknown")
            created_timestamp = isotodiscordtimestamp(created_date, "F") if created_date != "Unknown" else "Unknown"
            
            embed_color = embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
            
            embed = discord.Embed(
                title=f"✅ {username} has this badge!",
                description=f"**{badge_name}**\n{badge_description}",
                color=embed_color
            )
            
            if badge_thumbnail:
                embed.set_thumbnail(url=badge_thumbnail)
            
            embed.add_field(name="Badge Information", value=f"**ID:** `{badge_id}`\n**Status:** {'Enabled' if badge_enabled else 'Disabled'}", inline=True)
            embed.add_field(name="Awarded", value=f"**Date:** {awarded_date}", inline=True)
            embed.add_field(name="Created", value=f"**Date:** {created_timestamp}", inline=True)
            
            view = discord.ui.View()
            view.add_item(discord.ui.Button(
                label="View Badge",
                style=discord.ButtonStyle.link,
                emoji="<:RobloxLogo:1416951004607418398>",
                url=f"https://www.roblox.com/badges/{badge_id}"
            ))
            
            embed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
            await interaction.edit_original_response(embed=embed, view=view)
            return

        first_badge = user_badges[0]
        first_badge_id = str(first_badge.get("id"))
        
        badge_info_task = asyncio.create_task(get_badge_info(first_badge_id))
        awarded_date_task = asyncio.create_task(get_awarded_date(user_id, first_badge_id))
        thumbnail_task = asyncio.create_task(get_badge_thumbnail(first_badge_id))
        
        badge_info, badge_error = await badge_info_task
        awarded_data, awarded_error = await awarded_date_task
        badge_thumbnail = await thumbnail_task
        
        if badge_error:
            errorembed = discord.Embed(
                title=f":x: Error Fetching Badge Info :x:",
                description=f"Failed to fetch detailed information for badge ID `{first_badge_id}`",
                color=discord.Color.red()
            )
            errorembed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
            await interaction.edit_original_response(embed=errorembed)
            return
        
        awarded_date = "Unknown"
        if awarded_data and awarded_data.get("data") and len(awarded_data["data"]) > 0:
            awarded_date_str = awarded_data["data"][0].get("awardedDate")
            if awarded_date_str:
                awarded_timestamp = isotodiscordtimestamp(awarded_date_str, "F")
                awarded_date = awarded_timestamp if awarded_timestamp else awarded_date_str
        
        badge_name = badge_info.get("displayName") or badge_info.get("name", "Unknown Badge")
        badge_description = badge_info.get("displayDescription") or badge_info.get("description", "No description available")
        badge_enabled = badge_info.get("enabled", False)
        
        created_date = badge_info.get("created", "Unknown")
        created_timestamp = isotodiscordtimestamp(created_date, "F") if created_date != "Unknown" else "Unknown"
        
        embed_color = embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
        
        class BadgePaginator(discord.ui.View):
            def __init__(self, badges, user_id, username, requester_id, current_index=0):
                super().__init__(timeout=60)
                self.badges = badges
                self.user_id = user_id
                self.username = username
                self.requester_id = requester_id
                self.current_index = current_index
            
            async def update_badge_display(self, interaction: discord.Interaction):
                badge = self.badges[self.current_index]
                badge_id = str(badge.get("id"))
                
                badge_info_task = asyncio.create_task(get_badge_info(badge_id))
                awarded_date_task = asyncio.create_task(get_awarded_date(self.user_id, badge_id))
                thumbnail_task = asyncio.create_task(get_badge_thumbnail(badge_id))
                
                badge_info, badge_error = await badge_info_task
                awarded_data, awarded_error = await awarded_date_task
                badge_thumbnail = await thumbnail_task
                
                if badge_error:
                    await interaction.response.send_message("Failed to fetch badge information.", ephemeral=True)
                    return
                
                awarded_date = "Unknown"
                if awarded_data and awarded_data.get("data") and len(awarded_data["data"]) > 0:
                    awarded_date_str = awarded_data["data"][0].get("awardedDate")
                    if awarded_date_str:
                        awarded_timestamp = isotodiscordtimestamp(awarded_date_str, "F")
                        awarded_date = awarded_timestamp if awarded_timestamp else awarded_date_str
                
                badge_name = badge_info.get("displayName") or badge_info.get("name", "Unknown Badge")
                badge_description = badge_info.get("displayDescription") or badge_info.get("description", "No description available")
                badge_enabled = badge_info.get("enabled", False)
                
                created_date = badge_info.get("created", "Unknown")
                created_timestamp = isotodiscordtimestamp(created_date, "F") if created_date != "Unknown" else "Unknown"
                
                embed_color = embedDB.get(f"{self.requester_id}") if embedDB.get(f"{self.requester_id}") else discord.Color.blue()
                
                embed = discord.Embed(
                    title=f"✅ {self.username}'s Badges",
                    description=f"**{badge_name}**\n{badge_description}",
                    color=embed_color
                )
                
                if badge_thumbnail:
                    embed.set_thumbnail(url=badge_thumbnail)
                
                embed.add_field(name="Badge Information", value=f"**ID:** `{badge_id}`\n**Status:** {'Enabled' if badge_enabled else 'Disabled'}", inline=True)
                embed.add_field(name="Awarded", value=f"**Date:** {awarded_date}", inline=True)
                embed.add_field(name="Created", value=f"**Date:** {created_timestamp}", inline=True)
                embed.set_footer(text=f"Badge {self.current_index + 1}/{len(self.badges)} | Requested By {interaction.user.name} | {MainURL}")
                
                view = BadgePaginator(self.badges, self.user_id, self.username, self.requester_id, self.current_index)
                await interaction.response.edit_message(embed=embed, view=view)
            
            @discord.ui.button(emoji="⬅️", style=discord.ButtonStyle.secondary)
            async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                if interaction.user.id != self.requester_id:
                    await interaction.response.send_message("This is not your command!", ephemeral=True)
                    return
                
                self.current_index = (self.current_index - 1) % len(self.badges)
                await self.update_badge_display(interaction)
            
            @discord.ui.button(emoji="➡️", style=discord.ButtonStyle.secondary)
            async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                if interaction.user.id != self.requester_id:
                    await interaction.response.send_message("This is not your command!", ephemeral=True)
                    return
                
                self.current_index = (self.current_index + 1) % len(self.badges)
                await self.update_badge_display(interaction)
            
            @discord.ui.button(emoji="🔍", style=discord.ButtonStyle.primary)
            async def search_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                if interaction.user.id != self.requester_id:
                    await interaction.response.send_message("This is not your command!", ephemeral=True)
                    return
                
                modal = BadgeSearchModal(self.badges, self.user_id, self.username, self.requester_id, self.current_index)
                await interaction.response.send_modal(modal)
        
        class BadgeSearchModal(discord.ui.Modal, title="Search Badge by ID"):
            def __init__(self, badges, user_id, username, requester_id, current_index):
                super().__init__()
                self.badges = badges
                self.user_id = user_id
                self.username = username
                self.requester_id = requester_id
                self.current_index = current_index
            
            badge_id_input = discord.ui.TextInput(
                label="Badge ID",
                placeholder="Enter the badge ID to search...",
                required=True,
                max_length=20
            )
            
            async def on_submit(self, interaction: discord.Interaction):
                badge_id = self.badge_id_input.value.strip()
                found_index = -1
                
                for i, badge in enumerate(self.badges):
                    if str(badge.get("id")) == badge_id:
                        found_index = i
                        break
                
                if found_index == -1:
                    await interaction.response.defer()
                    errorembed = discord.Embed(
                        title=f":x: Badge Not Found :x:",
                        description=f"User `{self.username}` does not have badge ID `{badge_id}`",
                        color=discord.Color.red()
                    )
                    errorembed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
                    
                    original_view = BadgePaginator(self.badges, self.user_id, self.username, self.requester_id, self.current_index)
                    await interaction.edit_original_response(embed=errorembed, view=original_view)
                    return
                
                await interaction.response.defer()
                
                badge = self.badges[found_index]
                badge_id = str(badge.get("id"))
                
                badge_info_task = asyncio.create_task(get_badge_info(badge_id))
                awarded_date_task = asyncio.create_task(get_awarded_date(self.user_id, badge_id))
                thumbnail_task = asyncio.create_task(get_badge_thumbnail(badge_id))
                
                badge_info, badge_error = await badge_info_task
                awarded_data, awarded_error = await awarded_date_task
                badge_thumbnail = await thumbnail_task
                
                if badge_error:
                    await interaction.followup.send("Failed to fetch badge information.", ephemeral=True)
                    return
                
                awarded_date = "Unknown"
                if awarded_data and awarded_data.get("data") and len(awarded_data["data"]) > 0:
                    awarded_date_str = awarded_data["data"][0].get("awardedDate")
                    if awarded_date_str:
                        awarded_timestamp = isotodiscordtimestamp(awarded_date_str, "F")
                        awarded_date = awarded_timestamp if awarded_timestamp else awarded_date_str
                
                badge_name = badge_info.get("displayName") or badge_info.get("name", "Unknown Badge")
                badge_description = badge_info.get("displayDescription") or badge_info.get("description", "No description available")
                badge_enabled = badge_info.get("enabled", False)
                
                created_date = badge_info.get("created", "Unknown")
                created_timestamp = isotodiscordtimestamp(created_date, "F") if created_date != "Unknown" else "Unknown"
                
                embed_color = embedDB.get(f"{self.requester_id}") if embedDB.get(f"{self.requester_id}") else discord.Color.blue()
                
                embed = discord.Embed(
                    title=f"✅ {self.username}'s Badges",
                    description=f"**{badge_name}**\n{badge_description}",
                    color=embed_color
                )
                
                if badge_thumbnail:
                    embed.set_thumbnail(url=badge_thumbnail)
                
                embed.add_field(name="Badge Information", value=f"**ID:** `{badge_id}`\n**Status:** {'Enabled' if badge_enabled else 'Disabled'}", inline=True)
                embed.add_field(name="Awarded", value=f"**Date:** {awarded_date}", inline=True)
                embed.add_field(name="Created", value=f"**Date:** {created_timestamp}", inline=True)
                embed.set_footer(text=f"Badge {found_index + 1}/{len(self.badges)} | Requested By {interaction.user.name} | {MainURL}")
                
                view = BadgePaginator(self.badges, self.user_id, self.username, self.requester_id, found_index)
                await interaction.edit_original_response(embed=embed, view=view)
        
        embed = discord.Embed(
            title=f"✅ {username}'s Badges",
            description=f"**{badge_name}**\n{badge_description}",
            color=embed_color
        )
        
        if badge_thumbnail:
            embed.set_thumbnail(url=badge_thumbnail)
        
        embed.add_field(name="Badge Information", value=f"**ID:** `{first_badge_id}`\n**Status:** {'Enabled' if badge_enabled else 'Disabled'}", inline=True)
        embed.add_field(name="Awarded", value=f"**Date:** {awarded_date}", inline=True)
        embed.add_field(name="Created", value=f"**Date:** {created_timestamp}", inline=True)
        embed.set_footer(text=f"Badge 1/{len(user_badges)} | Requested By {interaction.user.name} | {MainURL}")
        
        view = BadgePaginator(user_badges, user_id, roblox_username, interaction.user.id, 0)
        await interaction.edit_original_response(embed=embed, view=view)
        
    except Exception as e:
        errorembed = discord.Embed(
            title=f":x: Error :x:",
            description=f"An error occurred: {str(e)}",
            color=discord.Color.red()
        )
        errorembed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
        await interaction.edit_original_response(embed=errorembed)

@bot.tree.command(name="discorduser", description="Get information about a user")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def discorduser(interaction: discord.Interaction, user: discord.User = None):
    await interaction.response.defer()
    
    if user is None:
        user = interaction.user
    
    target_member = None
    guild = interaction.guild
    
    if guild:
        target_member = guild.get_member(user.id)
    
    created_at = isotodiscordtimestamp(user.created_at.isoformat(), "D")
    
    BADGE_FLAGS = {
        4194304: "<:activedeveloper:1431764482312372385>",
        512: "<:earlysupporter:1431764483000504351>",
        64: "<:hypesquadp:1431764484179099788>",
        128: "<:hypesquadr:1431764485374214267>",
        256: "<:hypesquadg:1431764486636830780>",
    }
    
    badges = []
    public_flags = user.public_flags.value if user.public_flags else 0
    
    for flag, emoji in BADGE_FLAGS.items():
        if public_flags & flag:
            badges.append(emoji)
    
    has_nitro = False
    if user.avatar:
        if user.avatar.is_animated():
            has_nitro = True
            badges.append("<:Nitro:1431740129726300191>")
    
    try:
        user_data = await bot.fetch_user(user.id)
        if user_data.banner:
            has_nitro = True
            if "<:Nitro:1431777332552536155>" not in badges:
                badges.append("<:Nitro:1431777332552536155>")
    except:
        pass
    
    badges_display = " ".join(badges) if badges else "None"
    
    embed = discord.Embed(
        title=f"{user.name}",
        url=f"https://discord.com/users/{user.id}",
        color=discord.Color.blue(),
        timestamp=interaction.created_at
    )
    
    if user.avatar:
        embed.set_thumbnail(url=user.avatar.url)
    
    user_info = f"""
    > **Username:** `{user.name}`
    > **ID:** `{user.id}`
    > **Created:** {created_at}
    > **Bot:** `{user.bot}`
    > **Nitro:** `{has_nitro}`
    > **Badges:** {badges_display}
    """
    
    embed.add_field(
        name="User Information",
        value=user_info,
        inline=False
    )
    
    if guild and target_member:
        if target_member.joined_at:
            joined_at = isotodiscordtimestamp(target_member.joined_at.isoformat(), "D")
        else:
            joined_at = "Unknown"
        
        roles = [role.mention for role in target_member.roles[1:][:10]]
        roles_display = ", ".join(roles) if roles else "None"
        if len(target_member.roles) > 11:
            roles_display += f" (+{len(target_member.roles) - 11} more)"
        
        server_info = f"""
        > **Joined:** {joined_at}
        > **Nickname:** `{target_member.nick or 'None'}`
        > **Top Role:** {target_member.top_role.mention}
        """
        
        embed.add_field(
            name="Server Info",
            value=server_info,
            inline=False
        )
        
        embed.add_field(
            name=f"Roles ({len(target_member.roles)})",
            value=f"> {roles_display}",
            inline=False
        )
    
    embed.set_footer(text=f"Requested by {interaction.user.display_name} | {MainURL}")

    class MutualServersView(discord.ui.View):
        def __init__(self, user_id, username, requester_id, main_embed):
            super().__init__(timeout=60)
            self.user_id = user_id
            self.username = username
            self.requester_id = requester_id
            self.main_embed = main_embed

        async def interaction_check(self, interaction: discord.Interaction) -> bool:
            return interaction.user.id == self.requester_id

        @discord.ui.button(label="View Mutual Servers", style=discord.ButtonStyle.blurple)
        async def mutuals_button(self, interaction: discord.Interaction, button: discord.ui.Button):
            await interaction.response.defer()
            
            try:
                import aiohttp
                async with aiohttp.ClientSession() as session:
                    async with session.get("http://localhost:13455/mutuals") as response:
                        if response.status == 200:
                            data = await response.json()
                            
                            if not data or "Servers" not in data:
                                class NoServersView(discord.ui.View):
                                    def __init__(self, main_embed, requester_id, user_id, username):
                                        super().__init__(timeout=120)
                                        self.main_embed = main_embed
                                        self.requester_id = requester_id
                                        self.user_id = user_id
                                        self.username = username

                                    async def interaction_check(self, interaction: discord.Interaction) -> bool:
                                        return interaction.user.id == self.requester_id

                                    @discord.ui.button(label="Back to User Info", style=discord.ButtonStyle.gray)
                                    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                                        view = MutualServersView(self.user_id, self.username, self.requester_id, self.main_embed)
                                        await interaction.response.edit_message(content="", embed=self.main_embed, view=view)
                                
                                no_servers_view = NoServersView(self.main_embed, self.requester_id, self.user_id, self.username)
                                await interaction.edit_original_response(content="No mutual servers data available.", embed=None, view=no_servers_view)
                                return

                            user_servers = []
                            for server_id, server_data in data["Servers"].items():
                                members = server_data.get("members", [])
                                for member in members:
                                    if str(member.get("id")) == str(self.user_id):
                                        user_servers.append({
                                            "server_id": server_id,
                                            "server_name": server_data.get("name", "Unknown Server"),
                                            "member_count": server_data.get("membercount", 0),
                                            "bot_count": server_data.get("botcount", 0),
                                            "channel_count": server_data.get("channels", 0),
                                            "icon_url": server_data.get("iconurl"),
                                            "created_at": server_data.get("createdat"),
                                            "roles": server_data.get("roles", 0),
                                            "verification_level": server_data.get("verificationlevel", "Unknown"),
                                            "owner": server_data.get("owner", "Unknown"),
                                            "owner_id": server_data.get("ownerid", "Unknown")
                                        })
                                        break

                            if not user_servers:
                                class NoMutualServersView(discord.ui.View):
                                    def __init__(self, main_embed, requester_id, user_id, username):
                                        super().__init__(timeout=120)
                                        self.main_embed = main_embed
                                        self.requester_id = requester_id
                                        self.user_id = user_id
                                        self.username = username

                                    async def interaction_check(self, interaction: discord.Interaction) -> bool:
                                        return interaction.user.id == self.requester_id

                                    @discord.ui.button(label="Back to User Info", style=discord.ButtonStyle.gray)
                                    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                                        view = MutualServersView(self.user_id, self.username, self.requester_id, self.main_embed)
                                        await interaction.response.edit_message(content="", embed=self.main_embed, view=view)
                                
                                no_mutual_view = NoMutualServersView(self.main_embed, self.requester_id, self.user_id, self.username)
                                await interaction.edit_original_response(content="No mutual servers found for this user.", embed=None, view=no_mutual_view)
                                return

                            pages = []
                            for server in user_servers:
                                server_embed = discord.Embed(
                                    title=f"Mutual Server: {server['server_name']}",
                                    color=discord.Color.green(),
                                    timestamp=discord.utils.utcnow()
                                )
                                
                                if server['icon_url']:
                                    server_embed.set_thumbnail(url=server['icon_url'])
                                
                                if server['created_at']:
                                    created_at_display = isotodiscordtimestamp(server['created_at'], "D")
                                else:
                                    created_at_display = "Unknown"
                                
                                server_info = f"""
                                > **Name:** {server['server_name']}
                                > **ID:** `{server['server_id']}`
                                > **Created:** {created_at_display}
                                > **Owner:** {server['owner']} (`{server['owner_id']}`)
                                > **Verification:** {server['verification_level'].title()}
                                """
                                
                                server_stats = f"""
                                > **Members:** {server['member_count']}
                                > **Bots:** {server['bot_count']}
                                > **Channels:** {server['channel_count']}
                                > **Roles:** {server['roles']}
                                """
                                
                                server_embed.add_field(
                                    name="Server Information",
                                    value=server_info,
                                    inline=False
                                )
                                
                                server_embed.add_field(
                                    name="Server Stats",
                                    value=server_stats,
                                    inline=False
                                )
                                
                                server_embed.set_footer(text=f"Page {len(pages) + 1}/{len(user_servers)} | {MainURL}")
                                pages.append(server_embed)

                            if len(pages) == 1:
                                class SingleServerView(discord.ui.View):
                                    def __init__(self, main_embed, requester_id, user_id, username):
                                        super().__init__(timeout=120)
                                        self.main_embed = main_embed
                                        self.requester_id = requester_id
                                        self.user_id = user_id
                                        self.username = username

                                    async def interaction_check(self, interaction: discord.Interaction) -> bool:
                                        return interaction.user.id == self.requester_id

                                    @discord.ui.button(label="Back to User Info", style=discord.ButtonStyle.gray)
                                    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                                        view = MutualServersView(self.user_id, self.username, self.requester_id, self.main_embed)
                                        await interaction.response.edit_message(content="", embed=self.main_embed, view=view)
                                
                                single_view = SingleServerView(self.main_embed, self.requester_id, self.user_id, self.username)
                                await interaction.edit_original_response(content="", embed=pages[0], view=single_view)
                            else:
                                class PaginationView(discord.ui.View):
                                    def __init__(self, pages, main_embed, requester_id, user_id, username):
                                        super().__init__(timeout=120)
                                        self.pages = pages
                                        self.current_page = 0
                                        self.main_embed = main_embed
                                        self.requester_id = requester_id
                                        self.user_id = user_id
                                        self.username = username

                                    async def interaction_check(self, interaction: discord.Interaction) -> bool:
                                        return interaction.user.id == self.requester_id

                                    @discord.ui.button(emoji="⬅️", style=discord.ButtonStyle.gray)
                                    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                                        self.current_page = (self.current_page - 1) % len(self.pages)
                                        await interaction.response.edit_message(content="", embed=self.pages[self.current_page], view=self)
                                    
                                    @discord.ui.button(emoji="➡️", style=discord.ButtonStyle.gray)
                                    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                                        self.current_page = (self.current_page + 1) % len(self.pages)
                                        await interaction.response.edit_message(content="", embed=self.pages[self.current_page], view=self)
                                    
                                    @discord.ui.button(label="Back to User Info", style=discord.ButtonStyle.gray)
                                    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                                        view = MutualServersView(self.user_id, self.username, self.requester_id, self.main_embed)
                                        await interaction.response.edit_message(content="", embed=self.main_embed, view=view)
                                
                                pagination_view = PaginationView(pages, self.main_embed, self.requester_id, self.user_id, self.username)
                                await interaction.edit_original_response(content="", embed=pages[0], view=pagination_view)
                                
                        else:
                            class ErrorView(discord.ui.View):
                                def __init__(self, main_embed, requester_id, user_id, username):
                                    super().__init__(timeout=120)
                                    self.main_embed = main_embed
                                    self.requester_id = requester_id
                                    self.user_id = user_id
                                    self.username = username

                                async def interaction_check(self, interaction: discord.Interaction) -> bool:
                                    return interaction.user.id == self.requester_id

                                @discord.ui.button(label="Back to User Info", style=discord.ButtonStyle.gray)
                                async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                                    view = MutualServersView(self.user_id, self.username, self.requester_id, self.main_embed)
                                    await interaction.response.edit_message(content="", embed=self.main_embed, view=view)
                            
                            error_view = ErrorView(self.main_embed, self.requester_id, self.user_id, self.username)
                            await interaction.edit_original_response(content="Failed to fetch mutual servers data.", embed=None, view=error_view)
            except Exception as e:
                class ExceptionView(discord.ui.View):
                    def __init__(self, main_embed, requester_id, user_id, username):
                        super().__init__(timeout=120)
                        self.main_embed = main_embed
                        self.requester_id = requester_id
                        self.user_id = user_id
                        self.username = username

                    async def interaction_check(self, interaction: discord.Interaction) -> bool:
                        return interaction.user.id == self.requester_id

                    @discord.ui.button(label="Back to User Info", style=discord.ButtonStyle.gray)
                    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                        view = MutualServersView(self.user_id, self.username, self.requester_id, self.main_embed)
                        await interaction.response.edit_message(content="", embed=self.main_embed, view=view)
                
                exception_view = ExceptionView(self.main_embed, self.requester_id, self.user_id, self.username)
                await interaction.edit_original_response(content=f"Error fetching mutual servers: {str(e)}", embed=None, view=exception_view)

    view = MutualServersView(user.id, user.name, interaction.user.id, embed)
    await interaction.followup.send(content="", embed=embed, view=view)
    
@bot.tree.command(name="limiteds", description="Scan a roblox users inventory for limiteds")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def limiteds(interaction: discord.Interaction, username: str):
    await interaction.response.defer()
    
    thinkingembed = discord.Embed(
        title=f"{Emojis.get('loading')} {interaction.user.mention} Scanning for {username}'s limiteds!",
        color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
    )
    await interaction.followup.send(embed=thinkingembed)

    user_url = "https://users.roblox.com/v1/usernames/users"
    request_payload = {
        "usernames": [username],
        "excludeBannedUsers": False
    }

    try:
        response = requests.post(user_url, json=request_payload)
        response.raise_for_status()
        user_data = response.json()
        
        if not user_data.get("data") or len(user_data["data"]) == 0:
            errorembed = discord.Embed(
                title=":x: User Not Found :x:",
                description=f"Could not find a Roblox user with the username `{username}`",
                color=discord.Color.red()
            )
            errorembed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
            await interaction.edit_original_response(embed=errorembed)
            return
            
        user_info = user_data["data"][0]
        user_id = user_info["id"]
        display_name = user_info["displayName"]

        connector = aiohttp.TCPConnector(family=socket.AF_INET)
        async with aiohttp.ClientSession(connector=connector) as session:
            all_limiteds_items = []
            cursor = ""
            
            while True:
                limiteds_url = f"https://inventory.roblox.com/v1/users/{user_id}/assets/collectibles?sortOrder=Asc&limit=100"
                if cursor:
                    limiteds_url += f"&cursor={cursor}"
                
                async with session.get(limiteds_url) as response:
                    if response.status != 200:
                        errorembed = discord.Embed(
                            title=":x: Error Fetching Limiteds :x:",
                            description=f"Failed to fetch limiteds for user `{username}` (Status: {response.status})",
                            color=discord.Color.red()
                        )
                        errorembed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
                        await interaction.edit_original_response(embed=errorembed)
                        return
                    
                    limiteds_data = await response.json()
                    limiteds_items = limiteds_data.get("data", [])
                    all_limiteds_items.extend(limiteds_items)
                    
                    next_cursor = limiteds_data.get("nextPageCursor")
                    if not next_cursor:
                        break
                    
                    cursor = next_cursor
                    
                    if len(all_limiteds_items) >= 100:
                        await asyncio.sleep(2.5)
            
            rap = 0
            value = 0
            try:
                rolimons_url = f"https://api.rolimons.com/players/v1/playerinfo/{user_id}"
                async with session.get(rolimons_url, headers={'User-Agent': 'cats.lol'}) as response:
                    if response.status == 200:
                        rolimons_data = await response.json()
                        if rolimons_data.get('success'):
                            rap = rolimons_data.get('rap', 0) or 0
                            value = rolimons_data.get('value', 0) or 0
            except Exception:
                pass
            
            inventory_url = f"https://inventory.roblox.com/v1/users/{user_id}/can-view-inventory"
            inventory_visibility = "Private"
            try:
                async with session.get(inventory_url) as response:
                    if response.status == 200:
                        inventory_data = await response.json()
                        inventory_visibility = "Public" if inventory_data.get('canView', False) else "Private"
            except Exception:
                pass

            avatar_url = f"https://thumbnails.roblox.com/v1/users/avatar?userIds={user_id}&size=420x420&format=Png&isCircular=false"
            avatar_thumbnail = None
            try:
                async with session.get(avatar_url) as response:
                    if response.status == 200:
                        avatar_data = await response.json()
                        if avatar_data.get("data") and len(avatar_data["data"]) > 0:
                            avatar_thumbnail = avatar_data["data"][0].get("imageUrl")
            except Exception:
                pass

            limited_count = len(all_limiteds_items)
            limiteds_by_name = {}
            
            for item in all_limiteds_items:
                name = item.get("name", "Unknown Item")
                user_asset_id = item.get("userAssetId")
                
                if name in limiteds_by_name:
                    limiteds_by_name[name]["count"] += 1
                else:
                    limiteds_by_name[name] = {
                        "count": 1,
                        "user_asset_id": user_asset_id
                    }
            
            limiteds_list = []
            for name, data in limiteds_by_name.items():
                count = data["count"]
                user_asset_id = data["user_asset_id"]
                
                if count > 1:
                    display_text = f"{count}x [{name}](https://www.rolimons.com/uaid/{user_asset_id})"
                else:
                    display_text = f"[{name}](https://www.rolimons.com/uaid/{user_asset_id})"
                
                limiteds_list.append(display_text)
            
            limiteds_chunks = [limiteds_list[i:i+10] for i in range(0, len(limiteds_list), 10)]
            
            class LimitedsPaginationView(discord.ui.View):
                def __init__(self, limiteds_chunks, display_name, user_id, rap, value, limited_count, inventory_visibility, requester_id, avatar_thumbnail):
                    super().__init__(timeout=120)
                    self.limiteds_chunks = limiteds_chunks
                    self.current_page = 0
                    self.display_name = display_name
                    self.user_id = user_id
                    self.rap = rap
                    self.value = value
                    self.limited_count = limited_count
                    self.inventory_visibility = inventory_visibility
                    self.requester_id = requester_id
                    self.avatar_thumbnail = avatar_thumbnail
                
                async def create_embed(self):
                    embed = discord.Embed(
                        title=f"{self.display_name}'s Limiteds",
                        url=f"https://www.roblox.com/users/{self.user_id}/profile",
                        color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
                    )
                    
                    if self.avatar_thumbnail:
                        embed.set_thumbnail(url=self.avatar_thumbnail)
                    
                    stats_info = f"""
                    > **RAP:** {self.rap:,}
                    > **Value:** {self.value:,}
                    > **Limited Count:** {self.limited_count}
                    > **Inventory:** {self.inventory_visibility}
                    """
                    
                    embed.add_field(
                        name="Statistics",
                        value=stats_info,
                        inline=False
                    )
                    
                    if self.limiteds_chunks:
                        current_chunk = self.limiteds_chunks[self.current_page]
                        limiteds_text = "\n".join(current_chunk)
                        field_name = f"Limiteds (Page {self.current_page + 1}/{len(self.limiteds_chunks)})"
                        embed.add_field(
                            name=field_name,
                            value=limiteds_text,
                            inline=False
                        )
                    else:
                        embed.add_field(
                            name="Limiteds",
                            value="No limiteds found",
                            inline=False
                        )
                    
                    embed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
                    return embed
                
                def update_buttons(self):
                    self.clear_items()
                    
                    if self.current_page > 0:
                        previous_btn = discord.ui.Button(style=discord.ButtonStyle.gray, emoji="⬅️", custom_id="previous")
                        previous_btn.callback = self.previous_callback
                        self.add_item(previous_btn)
                    
                    if self.current_page < len(self.limiteds_chunks) - 1:
                        next_btn = discord.ui.Button(style=discord.ButtonStyle.gray, emoji="➡️", custom_id="next")
                        next_btn.callback = self.next_callback
                        self.add_item(next_btn)
                    
                    self.add_item(discord.ui.Button(
                        label="View Rolimons Profile",
                        style=discord.ButtonStyle.link,
                        emoji="<:RolimonsLogo:1417258794974711901>",
                        url=f"https://rolimons.com/player/{self.user_id}"
                    ))
                    
                    self.add_item(discord.ui.Button(
                        label="View Roblox Profile",
                        style=discord.ButtonStyle.link,
                        emoji="<:RobloxLogo:1416951004607418398>",
                        url=f"https://www.roblox.com/users/{self.user_id}/profile"
                    ))
                
                async def previous_callback(self, interaction: discord.Interaction):
                    if interaction.user.id != self.requester_id:
                        await interaction.response.send_message("This is not your command!", ephemeral=True)
                        return
                    
                    self.current_page -= 1
                    embed = await self.create_embed()
                    self.update_buttons()
                    await interaction.response.edit_message(embed=embed, view=self)
                
                async def next_callback(self, interaction: discord.Interaction):
                    if interaction.user.id != self.requester_id:
                        await interaction.response.send_message("This is not your command!", ephemeral=True)
                        return
                    
                    self.current_page += 1
                    embed = await self.create_embed()
                    self.update_buttons()
                    await interaction.response.edit_message(embed=embed, view=self)
            
            view = LimitedsPaginationView(limiteds_chunks, display_name, user_id, rap, value, limited_count, inventory_visibility, interaction.user.id, avatar_thumbnail)
            embed = await view.create_embed()
            view.update_buttons()
            await interaction.edit_original_response(embed=embed, view=view)
            
    except requests.exceptions.RequestException as e:
        errorembed = discord.Embed(
            title=":x: API Error :x:",
            description=f"An error occurred while fetching data: {str(e)}",
            color=discord.Color.red()
        )
        errorembed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
        await interaction.edit_original_response(embed=errorembed)
    except Exception as e:
        errorembed = discord.Embed(
            title=":x: Unexpected Error :x:",
            description=f"An unexpected error occurred: {str(e)}",
            color=discord.Color.red()
        )
        errorembed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
        await interaction.edit_original_response(embed=errorembed)
        
@bot.tree.command(name="bundle", description="Get information about a Roblox bundle")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(bundleid="The ID of the bundle")
async def bundle(interaction: discord.Interaction, bundleid: str):
    await interaction.response.defer(thinking=True)
    
    print(f"Searching for bundle {bundleid}")
    thinkingembed = discord.Embed(
        title=f"{Emojis.get('loading')} {interaction.user.mention} Searching for bundle {bundleid}!",
        color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
    )
    await interaction.followup.send(embed=thinkingembed)

    try:
        url = f"https://catalog.roblox.com/v1/catalog/items/{bundleid}/details?itemType=Bundle"
        
        connector = aiohttp.TCPConnector(family=socket.AF_INET)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(url, headers={
                'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36',
                'accept': 'application/json',
            }) as response:
                if response.status != 200:
                    failedembed = discord.Embed(
                        title=f"❌ Bundle not found",
                        description=f"Could not find a bundle with ID `{bundleid}`",
                        color=discord.Color.red()
                    )
                    failedembed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
                    await interaction.edit_original_response(embed=failedembed)
                    return
                
                data = await response.json()
                
                name = data.get("name", "Unknown")
                description = data.get("description", "No description available")
                item_type = data.get("itemType", "Unknown")
                creator_name = data.get("creatorName", "Unknown")
                creator_has_verified_badge = data.get("creatorHasVerifiedBadge", False)
                lowest_price = data.get("lowestPrice", 0)
                price_status = data.get("priceStatus", "Unknown")
                favorite_count = data.get("favoriteCount", 0)
                item_created_utc = data.get("itemCreatedUtc", "")
                
                created_timestamp = "Unknown"
                if item_created_utc:
                    created_timestamp = isotodiscordtimestamp(item_created_utc, "D")
                
                creator_display = creator_name
                if creator_has_verified_badge:
                    creator_display += " <:RobloxVerified:1416951927513677874>"
                
                price_display = f"{lowest_price} Robux" if price_status != "Off Sale" else "Off Sale"
                
                bundle_info = f"""
                > **Name:** {name}
                > **Type:** {item_type}
                > **Creator:** {creator_display}
                > **Price:** {price_display}
                > **Favorites:** {favorite_count:,}
                > **Created:** {created_timestamp}
                """
                
                embed = discord.Embed(
                    title=name,
                    color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue(),
                    timestamp=interaction.created_at,
                    url=f"https://www.roblox.com/bundles/{bundleid}"
                )
                
                embed.add_field(
                    name="Bundle Information",
                    value=bundle_info,
                    inline=False
                )
                
                if description and description != "No description available":
                    embed.add_field(
                        name="Description",
                        value=f"> {description}",
                        inline=False
                    )
                
                thumbnail_url = f"https://thumbnails.roblox.com/v1/bundles/thumbnails?bundleIds={bundleid}&size=420x420&format=Png"
                async with session.get(thumbnail_url) as thumb_response:
                    if thumb_response.status == 200:
                        thumb_data = await thumb_response.json()
                        if thumb_data.get('data') and len(thumb_data['data']) > 0:
                            image_url = thumb_data['data'][0].get('imageUrl')
                            if image_url:
                                embed.set_thumbnail(url=image_url)
                
                embed.set_footer(text=f"Requested by {interaction.user.display_name} | {MainURL}")
                
                class BundleView(discord.ui.View):
                    def __init__(self, bundle_id: str):
                        super().__init__(timeout=120)
                        self.add_item(discord.ui.Button(
                            label="View Bundle on Roblox",
                            url=f"https://www.roblox.com/bundles/{bundle_id}",
                            style=discord.ButtonStyle.link,
                            emoji="<:RobloxLogo:1416951004607418398>"
                        ))
                
                view = BundleView(bundleid)
                await interaction.edit_original_response(embed=embed, view=view)
                
    except Exception as e:
        failedembed = discord.Embed(
            title=f"❌ Error fetching bundle",
            description=f"An error occurred: {str(e)}",
            color=discord.Color.red()
        )
        failedembed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
        await interaction.edit_original_response(embed=failedembed)
        
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@bot.tree.command(name="github", description="Get detailed information about a GitHub user")
@app_commands.describe(username="GitHub username")
async def github(interaction: discord.Interaction, username: str):
    await interaction.response.defer(thinking=True)
    
    thinkingembed = discord.Embed(
        title=f"{Emojis.get('loading')} {interaction.user.mention} Searching For GitHub Profile!",
        color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.dark_gray()
    )
    await interaction.followup.send(embed=thinkingembed)
    
    try:
        connector = aiohttp.TCPConnector(family=socket.AF_INET)
        async with aiohttp.ClientSession(connector=connector) as session:
            url = f"https://api.github.com/users/{username}"
            
            async with session.get(url) as response:
                if response.status == 404:
                    errorembed = discord.Embed(
                        description=f"❌ GitHub user `{username}` not found",
                        color=discord.Color.red()
                    )
                    errorembed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
                    await interaction.edit_original_response(embed=errorembed)
                    return
                
                response.raise_for_status()
                data = await response.json()
            
            avatar_url = data.get("avatar_url", "")
            profile_url = data.get("html_url", f"https://github.com/{username}")
            company = data.get("company", "Not specified")
            blog = data.get("blog", "Not specified")
            location = data.get("location", "Not specified")
            bio = data.get("bio", "No bio available")
            public_repos = data.get("public_repos", 0)
            followers = data.get("followers", 0)
            following = data.get("following", 0)
            created_at = data.get("created_at", "Unknown")
            updated_at = data.get("updated_at", "Unknown")
            
            created_timestamp = isotodiscordtimestamp(created_at, "D") if created_at != 'Unknown' else "Unknown"
            updated_timestamp = isotodiscordtimestamp(updated_at, "D") if updated_at != 'Unknown' else "Unknown"
            
            embed = discord.Embed(
                title=f"@{username}",
                url=profile_url,
                color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.dark_gray(),
                timestamp=interaction.created_at
            )
            
            if avatar_url:
                embed.set_thumbnail(url=avatar_url)
            
            github_info = f"""
            > **Public Repos:** `{public_repos}`
            > **Followers:** `{followers}`
            > **Following:** `{following}`
            > **Company:** `{company}`
            > **Location:** `{location}`
            > **Created:** {created_timestamp}
            > **Updated:** {updated_timestamp}
            """
            
            if blog and blog != "Not specified":
                github_info += f"> **Website:** `{blog}`\n"
            
            embed.add_field(
                name="GitHub Information",
                value=github_info,
                inline=False
            )
            
            if bio and bio != "No bio available":
                if len(bio) > 1024:
                    bio = bio[:1021] + "..."
                embed.add_field(
                    name="Bio",
                    value=f"> `{bio}`",
                    inline=False
                )
            
            embed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
            
            view = discord.ui.View(timeout=120)
            view.add_item(discord.ui.Button(
                label="View on GitHub", 
                url=profile_url,
                style=discord.ButtonStyle.link,
                emoji="🐙"
            ))
            
            await interaction.edit_original_response(embed=embed, view=view)
            
    except aiohttp.ClientError as e:
        errorembed = discord.Embed(
            description=f"Failed to request github",
            color=discord.Color.red()
        )
        errorembed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
        await interaction.edit_original_response(embed=errorembed)
    except Exception as e:
        errorembed = discord.Embed(
            description=f"❌ Failed to fetch GitHub profile information",
            color=discord.Color.red()
        )
        errorembed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
        await interaction.edit_original_response(embed=errorembed)
        
@bot.tree.command(name="asset", description="Get information about a Roblox asset")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def asset(interaction: discord.Interaction, id: str):
    await interaction.response.defer()
    
    print(f"Searching For Asset {id} Information!")
    thinkingembed = discord.Embed(
        title=f"{Emojis.get('loading')} {interaction.user.mention} Searching for Asset {id}",
        color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
    )
    await interaction.followup.send(embed=thinkingembed)

    url = f"https://catalog.roblox.com/v1/catalog/items/{id}/details?itemType=Asset"
    
    try:
        response = requests.get(url)
        response.raise_for_status()
        asset_data = response.json()
        
        name = asset_data.get("name", "Unknown Asset")
        description = asset_data.get("description", "No description available")
        creator_name = asset_data.get("creatorName", "Unknown Creator")
        creator_verified = asset_data.get("creatorHasVerifiedBadge", False)
        lowest_price = asset_data.get("lowestPrice", 0)
        favorite_count = asset_data.get("favoriteCount", 0)
        created_date = asset_data.get("itemCreatedUtc")
        item_type = asset_data.get("itemType", "Asset")
        
        if created_date:
            created_timestamp = isotodiscordtimestamp(created_date, "D")
        else:
            created_timestamp = "Unknown"
        
        creator_display = creator_name
        if creator_verified:
            creator_display += " <:RobloxVerified:1416951927513677874>"
        
        price_display = "Free" if lowest_price == 0 else f"{lowest_price:,} Robux"
        
        thumbnail_url = f"https://thumbnails.roblox.com/v1/assets?assetIds={id}&size=420x420&format=Png"
        thumbnail_response = requests.get(thumbnail_url)
        thumbnail_data = thumbnail_response.json()
        
        image_url = None
        if thumbnail_data and thumbnail_data.get("data") and len(thumbnail_data["data"]) > 0:
            image_url = thumbnail_data["data"][0].get("imageUrl")
        
        embed = discord.Embed(
            title=name,
            url=f"https://www.roblox.com/catalog/{id}/",
            color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue(),
            timestamp=interaction.created_at
        )
        
        if image_url:
            embed.set_thumbnail(url=image_url)
        
        asset_info = f"""
        > **Name:** {name}
        > **Asset ID:** `{id}`
        > **Type:** {item_type}
        > **Creator:** {creator_display}
        > **Price:** {price_display}
        > **Favorites:** {favorite_count:,}
        > **Created:** {created_timestamp}
        """
        
        embed.add_field(
            name="Asset Information",
            value=asset_info,
            inline=False
        )
        
        if description and description != "No description available":
            if len(description) > 1024:
                description = description[:1021] + "..."
            embed.add_field(
                name="Description",
                value=f"> {description}",
                inline=False
            )
        
        view = discord.ui.View(timeout=120)
        view.add_item(discord.ui.Button(
            label="View Asset",
            style=discord.ButtonStyle.link,
            emoji="<:RobloxLogo:1416951004607418398>",
            url=f"https://www.roblox.com/catalog/{id}/"
        ))
        
        embed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
        await interaction.edit_original_response(embed=embed, view=view)
        
    except requests.exceptions.RequestException as e:
        errorembed = discord.Embed(
            title=":x: Error Fetching Asset :x:",
            description=f"Failed to fetch asset information for ID `{id}`. Please check if the asset ID is valid.",
            color=discord.Color.red()
        )
        errorembed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
        await interaction.edit_original_response(embed=errorembed)
    except Exception as e:
        errorembed = discord.Embed(
            title=":x: Unexpected Error :x:",
            description=f"An unexpected error occurred while fetching asset information: {str(e)}",
            color=discord.Color.red()
        )
        errorembed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
        await interaction.edit_original_response(embed=errorembed)
        
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@bot.tree.command(name="discordavatar", description="Get a user's Discord avatar")
@app_commands.describe(user="The user to get the avatar from (leave empty for yourself)")
async def discordavatar(interaction: discord.Interaction, user: Optional[discord.User] = None):
    """Get a user's Discord avatar with high quality options"""
    await interaction.response.defer()
    target = user or interaction.user
    
    try:
        avatar_url = target.display_avatar.with_size(4096).url
        
        embed = discord.Embed(
            title=f"{target.name}'s Avatar",
            color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue(),
            timestamp=interaction.created_at
        )
        embed.set_image(url=avatar_url)
        embed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
        
        view = discord.ui.View(timeout=120)
        view.add_item(discord.ui.Button(
            label="Download Avatar",
            style=discord.ButtonStyle.link,
            url=avatar_url
        ))
        
        await interaction.followup.send(embed=embed, view=view)
        
    except Exception as e:
        errorembed = discord.Embed(
            title="❌ Failed to fetch avatar",
            description=f"An error occurred: {str(e)}",
            color=discord.Color.red()
        )
        errorembed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
        await interaction.followup.send(embed=errorembed)

@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@bot.tree.command(name="discordbanner", description="Get a user's Discord banner")
@app_commands.describe(user="The user to get the banner from (leave empty for yourself)")
async def discordbanner(interaction: discord.Interaction, user: Optional[discord.User] = None):
    """Get a user's Discord banner using direct API call"""
    await interaction.response.defer()
    target = user or interaction.user
    
    try:
        connector = aiohttp.TCPConnector(family=socket.AF_INET)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(
                f'https://discord.com/api/v9/users/{target.id}',
                headers={'Authorization': f'Bot {token}'}
            ) as resp:
                if resp.status != 200:
                    errorembed = discord.Embed(
                        title="User Not Found",
                        description="Could not fetch user data ",
                        color=discord.Color.red()
                    )
                    errorembed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
                    await interaction.followup.send(embed=errorembed)
                    return
                
                user_data = await resp.json()

        if user_data.get('banner'):
            banner_hash = user_data['banner']
            
            if banner_hash.startswith('a_'):
                banner_url = f"https://cdn.discordapp.com/banners/{target.id}/{banner_hash}.gif?size=4096"
            else:
                banner_url = f"https://cdn.discordapp.com/banners/{target.id}/{banner_hash}.webp?size=4096"
            
            embed = discord.Embed(
                title=f"{target.name}'s Banner",
                color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue(),
                timestamp=interaction.created_at
            )
            embed.set_image(url=banner_url)
            embed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
            
            view = discord.ui.View(timeout=120)
            view.add_item(discord.ui.Button(
                label="Download Banner",
                style=discord.ButtonStyle.link,
                url=banner_url
            ))
            
            await interaction.followup.send(embed=embed, view=view)
            
        else:
            errorembed = discord.Embed(
                title="No Banner Found",
                description=f"{target.name} doesn't have a banner image.",
                color=discord.Color.orange()
            )
            errorembed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
            await interaction.followup.send(embed=errorembed)
        
    except aiohttp.ClientError as e:
        errorembed = discord.Embed(
            title="❌ error",
            description=f"api request failed{str(e)}",
            color=discord.Color.red()
        )
        errorembed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
        await interaction.followup.send(embed=errorembed)
    except Exception as e:
        errorembed = discord.Embed(
            title="❌ Unexpected Error",
            description=f"Failed to fetch banner: {str(e)}",
            color=discord.Color.red()
        )
        errorembed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
        await interaction.followup.send(embed=errorembed)
        
@bot.tree.command(name="linkroblox", description="Link your Roblox account to Discord")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def linkroblox(interaction: discord.Interaction):
    embed = discord.Embed(
        description=f"[Click here to link your Roblox account](https://api.shapes.lol/login?type=discord)",
        color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
    )
    await interaction.response.send_message(embed=embed, ephemeral=False)
    
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

async def get_csrf_token(session):
    try:
        with open('roblosecuritytoken.txt', 'r') as f:
            roblosecurity_token = f.read().strip()
            
        async with session.post("https://auth.roblox.com/v2/logout", 
                              headers={'Cookie': f'.ROBLOSECURITY={roblosecurity_token}'}) as response:
            token = response.headers.get("x-csrf-token")
            return token
    except Exception:
        return None

async def render_custom_avatar(session, user_id):
    try:
        with open('roblosecuritytoken.txt', 'r') as f:
            roblosecurity_token = f.read().strip()
        
        headers = {'Cookie': f'.ROBLOSECURITY={roblosecurity_token}'}
        
        async with session.get(f'https://avatar.roblox.com/v1/users/{user_id}/avatar', 
                             headers=headers) as response:
            if response.status == 200:
                v1_avatar_details = await response.json()
                
                if v1_avatar_details and 'bodyColors' in v1_avatar_details:
                    avatar_definition = {
                        "assets": [{"id": asset["id"]} for asset in v1_avatar_details["assets"]],
                        "bodyColors": {
                            "headColor": roblox_colors.get(v1_avatar_details["bodyColors"]["headColorId"], "FFFFFF"),
                            "torsoColor": roblox_colors.get(v1_avatar_details["bodyColors"]["torsoColorId"], "FFFFFF"),
                            "leftArmColor": roblox_colors.get(v1_avatar_details["bodyColors"]["leftArmColorId"], "FFFFFF"),
                            "rightArmColor": roblox_colors.get(v1_avatar_details["bodyColors"]["rightArmColorId"], "FFFFFF"),
                            "leftLegColor": roblox_colors.get(v1_avatar_details["bodyColors"]["leftLegColorId"], "FFFFFF"),
                            "rightLegColor": roblox_colors.get(v1_avatar_details["bodyColors"]["rightLegColorId"], "FFFFFF"),
                        },
                        "scales": v1_avatar_details["scales"],
                        "playerAvatarType": {"playerAvatarType": v1_avatar_details["playerAvatarType"]}
                    }
                else:
                    return None
            else:
                return None

        render_payload = {
            "thumbnailConfig": {"thumbnailId": 1, "thumbnailType": "2dWebp", "size": "420x420"},
            "avatarDefinition": avatar_definition
        }

        csrf_token = await get_csrf_token(session)
        if not csrf_token:
            return None

        request_headers = {
            'Cookie': f'.ROBLOSECURITY={roblosecurity_token}',
            'x-csrf-token': csrf_token,
            'Content-Type': 'application/json'
        }
        
        for i in range(5):
            async with session.post("https://avatar.roblox.com/v1/avatar/render", 
                                  json=render_payload, headers=request_headers) as render_response:
                if render_response.status == 200:
                    render_data = await render_response.json()
                    if render_data.get('state') == 'Completed' and render_data.get('imageUrl'):
                        return render_data['imageUrl']
            
            if i < 4:
                await asyncio.sleep(2)
        
        return None

    except Exception:
        return None

@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@bot.tree.command(name="avatar", description="Get a Roblox user's avatar")
@app_commands.describe(user="Roblox username or user ID")
async def avatar_command(interaction: discord.Interaction, user: str):
    await interaction.response.defer()
    
    async def get_user_id(session, user_input):
        if user_input.isdigit():
            return user_input
        url = "https://users.roblox.com/v1/usernames/users"
        try:
            async with session.post(url, json={"usernames": [user_input]}) as response:
                if response.status == 200:
                    data = await response.json()
                    if data.get("data") and len(data["data"]) > 0:
                        return str(data["data"][0]["id"])
        except Exception:
            pass
        return None

    async def get_username_from_id(session, user_id):
        url = f"https://users.roblox.com/v1/users/{user_id}"
        try:
            async with session.get(url) as response:
                if response.status == 200:
                    data = await response.json()
                    return data.get("name")
        except Exception:
            pass
        return None

    connector = aiohttp.TCPConnector(family=socket.AF_INET)
    async with aiohttp.ClientSession(connector=connector) as session:
        user_id = await get_user_id(session, user)
        
        if not user_id:
            await interaction.followup.send("❌ Cannot find user")
            return

        username = await get_username_from_id(session, user_id)
        if not username:
            await interaction.followup.send("❌ Cannot find user")
            return

        try:
            user_url = f"https://users.roblox.com/v1/users/{user_id}"
            async with session.get(user_url) as response:
                if response.status == 200:
                    user_data = await response.json()
                    is_banned = user_data.get("isBanned", False)
                    
                    if is_banned:
                        image_url = await render_custom_avatar(session, user_id)
                        if image_url:
                            embed = discord.Embed(
                                title=f"{username}'s Avatar",
                                color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
                            )
                            embed.set_image(url=image_url)
                            embed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
                            await interaction.followup.send(embed=embed)
                        else:
                            await interaction.followup.send("❌ User is terminated")
                        return
                    else:
                        avatar_url = f"https://thumbnails.roblox.com/v1/users/avatar?userIds={user_id}&size=420x420&format=Png&isCircular=false"
                        async with session.get(avatar_url) as avatar_response:
                            if avatar_response.status == 200:
                                avatar_data = await avatar_response.json()
                                if avatar_data.get('data') and len(avatar_data['data']) > 0:
                                    image_url = avatar_data['data'][0]['imageUrl']
                                    embed = discord.Embed(
                                        title=f"{username}'s Avatar",
                                        color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
                                    )
                                    embed.set_image(url=image_url)
                                    embed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
                                    await interaction.followup.send(embed=embed)
                                    return
                        await interaction.followup.send("❌ Failed to fetch avatar")
                        return
                else:
                    await interaction.followup.send("❌ Cannot find user")
                    return
                    
        except Exception:
            await interaction.followup.send("Failed to fetch user data")
            return
            
class BadgeService:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
    
    async def get_user_id(self, user: str):
        if user.isdigit():
            return user
        return await self._get_id_from_username(user)
    
    async def _get_id_from_username(self, username: str):
        url = "https://users.roblox.com/v1/usernames/users"
        try:
            async with self.session.post(url, json={"usernames": [username]}) as response:
                if response.status == 200:
                    data = await response.json()
                    if data.get("data") and len(data["data"]) > 0:
                        return str(data["data"][0]["id"])
        except Exception:
            pass
        return None
    
    async def get_username(self, user_id: str):
        url = f"https://users.roblox.com/v1/users/{user_id}"
        try:
            async with self.session.get(url) as response:
                if response.status == 200:
                    data = await response.json()
                    return data.get("name")
        except Exception:
            pass
        return None
    
    async def get_user_badges(self, user_id: str, limit: int = 10):
        badges_url = f"https://badges.roblox.com/v1/users/{user_id}/badges?sortOrder=Desc&limit={limit}"
        
        async with self.session.get(badges_url) as response:
            if response.status != 200:
                return [], response.status
            
            data = await response.json()
            return data.get('data', []), response.status
    
    async def get_awarded_dates(self, user_id: str, badge_ids: list):
        if not badge_ids:
            return {}
            
        awarded_dates_url = f"https://badges.roblox.com/v1/users/{user_id}/badges/awarded-dates?badgeIds={','.join(map(str, badge_ids))}"
        
        try:
            async with self.session.get(awarded_dates_url) as response:
                if response.status == 200:
                    data = await response.json()
                    return {item['badgeId']: item.get('awardedDate') for item in data.get('data', [])}
        except Exception:
            pass
        return {}
    
    async def get_badge_thumbnail(self, badge):
        icon_image_id = badge.get('iconImageId') or badge.get('displayIconImageId')
        if not icon_image_id:
            return None
            
        icon_url = f"https://thumbnails.roblox.com/v1/assets?assetIds={icon_image_id}&size=150x150&format=Png&isCircular=false"
        
        try:
            async with self.session.get(icon_url) as response:
                if response.status == 200:
                    data = await response.json()
                    if data.get('data') and len(data['data']) > 0:
                        return data['data'][0]['imageUrl']
        except Exception:
            pass
        return None

class BadgeFormatter:
    @staticmethod
    def parse_iso_timestamp(timestamp_str):
        if not timestamp_str:
            return None
            
        try:
            timestamp_str = timestamp_str.split('.')[0]
            
            if timestamp_str.endswith('Z'):
                dt = datetime.fromisoformat(timestamp_str[:-1]).replace(
                    tzinfo=datetime.timezone.utc
                )
            else:
                if '+' not in timestamp_str and 'Z' not in timestamp_str:
                    timestamp_str += '+00:00'
                dt = datetime.fromisoformat(timestamp_str)
            
            return int(dt.timestamp())
        except (ValueError, AttributeError, TypeError):
            return None
    
    @staticmethod
    def format_creator_info(creator):
        creator_name = creator.get('name', 'Unknown')
        creator_type = creator.get('type', 'User')
        creator_id = creator.get('id')
        
        if not creator_id:
            return creator_name
            
        if creator_type.lower() == 'user':
            return f"[{creator_name}](https://www.roblox.com/users/{creator_id}/profile)"
        else:
            return f"[{creator_name}](https://www.roblox.com/groups/{creator_id}/)"
    
    @staticmethod
    def format_awarder_info(awarder):
        awarder_type = awarder.get('type', 'Unknown')
        awarder_id = awarder.get('id')
        
        if not awarder_id:
            return "Unknown"
            
        if awarder_type.lower() == 'place':
            return f"[Place #{awarder_id}](https://www.roblox.com/games/{awarder_id}/)"
        else:
            return f"{awarder_type} #{awarder_id}"

class BadgesView(discord.ui.View):
    def __init__(self, badges, username, user_id, requester, start_time, badge_service):
        super().__init__(timeout=120)
        self.badges = badges
        self.current_page = 0
        self.username = username
        self.user_id = user_id
        self.requester = requester
        self.start_time = start_time
        self.badge_service = badge_service
        self.message = None
        self.thumbnail_cache = {}
        self.update_buttons()
    
    async def preload_thumbnails(self):
        for i, badge in enumerate(self.badges):
            thumbnail_url = await self.badge_service.get_badge_thumbnail(badge)
            if thumbnail_url:
                self.thumbnail_cache[i] = thumbnail_url
    
    async def create_embed(self):
        badge = self.badges[self.current_page]
        
        embed = discord.Embed(
            title=f"{self.username}'s Recent Badges",
            color=embedDB.get(f"{self.requester.id}") if embedDB.get(f"{self.requester.id}") else discord.Color.blue(),
            timestamp=datetime.now(),
            url=f"https://www.roblox.com/users/{self.user_id}/badges"
        )
        
        thumbnail_url = self.thumbnail_cache.get(self.current_page)
        if thumbnail_url:
            embed.set_thumbnail(url=thumbnail_url)
        
        badge_name = badge.get('displayName') or badge.get('name', 'Unknown Badge')
        badge_description = badge.get('displayDescription') or badge.get('description', 'No description')
        badge_id = badge.get('id', 'N/A')
        
        embed.add_field(name="Badge Name", value=badge_name, inline=False)
        embed.add_field(name="Description", value=badge_description or "No description", inline=False)
        embed.add_field(name="Badge ID", value=f"`{badge_id}`", inline=True)
        
        awarded_date = badge.get('awardedDate')
        if awarded_date:
            unix_timestamp = BadgeFormatter.parse_iso_timestamp(awarded_date)
            if unix_timestamp:
                embed.add_field(
                    name="Awarded Date", 
                    value=f"<t:{unix_timestamp}:D> (<t:{unix_timestamp}:R>)",  
                    inline=False
                )
            else:
                embed.add_field(name="Awarded Date", value=awarded_date, inline=False)
        
        creator = badge.get('creator', {})
        creator_text = BadgeFormatter.format_creator_info(creator)
        embed.add_field(name="Creator", value=creator_text, inline=True)
        
        awarder = badge.get('awarder', {})
        awarder_text = BadgeFormatter.format_awarder_info(awarder)
        embed.add_field(name="Awarded By", value=awarder_text, inline=True)
        
        stats = badge.get('statistics', {})
        awarded_count = stats.get('awardedCount', 0)
        win_rate = stats.get('winRatePercentage', 0)
        
        embed.add_field(name="Times Awarded", value=f"{awarded_count:,}", inline=True)
        embed.add_field(name="Win Rate", value=f"{win_rate}%", inline=True)
        
        elapsed_time = asyncio.get_event_loop().time() - self.start_time
        embed.set_footer(
            text=f"Badge {self.current_page + 1}/{len(self.badges)} • Load time: {elapsed_time:.2f}s • Requested by {self.requester.display_name}"
        )
        
        return embed
        
    def update_buttons(self):
        self.clear_items()
        
        if self.current_page > 0:
            previous_btn = discord.ui.Button(style=discord.ButtonStyle.primary, label="<<", custom_id="previous")
            previous_btn.callback = self.previous_callback
            self.add_item(previous_btn)
            
        if self.current_page < len(self.badges) - 1:
            next_btn = discord.ui.Button(style=discord.ButtonStyle.primary, label=">>", custom_id="next")
            next_btn.callback = self.next_callback
            self.add_item(next_btn)
        
        current_badge = self.badges[self.current_page]
        badge_id = current_badge.get('id')
        if badge_id:
            link_btn = discord.ui.Button(
                style=discord.ButtonStyle.link,
                label="View Badge",
                url=f"https://www.roblox.com/badges/{badge_id}"
            )
            self.add_item(link_btn)
    
    async def _handle_navigation(self, interaction: discord.Interaction, direction: int):
        if interaction.user != self.requester:
            await interaction.response.send_message("You can't interact with this command!", ephemeral=True)
            return
        
        self.current_page += direction
        embed = await self.create_embed()
        self.update_buttons()
        await interaction.response.edit_message(embed=embed, view=self)
    
    async def previous_callback(self, interaction: discord.Interaction):
        await self._handle_navigation(interaction, -1)
    
    async def next_callback(self, interaction: discord.Interaction):
        await self._handle_navigation(interaction, 1)
    
    async def on_timeout(self):
        for item in self.children:
            if isinstance(item, discord.ui.Button) and item.style != discord.ButtonStyle.link:
                item.disabled = True
        try:
            await self.message.edit(view=self)
        except Exception:
            pass

@bot.tree.command(name="recentbadges", description="Get a user's most recently earned Roblox badges")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def recent_badges(interaction: discord.Interaction, user: str):
    await interaction.response.defer()
    
    thinkingembed = discord.Embed(
        title=f"{Emojis.get('loading')} {interaction.user.mention} Searching For {user}'s recent badges",
        color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
    )
    await interaction.followup.send(embed=thinkingembed)
    
    start_time = asyncio.get_event_loop().time()
    
    connector = aiohttp.TCPConnector(family=socket.AF_INET)
    async with aiohttp.ClientSession(connector=connector) as session:
        badge_service = BadgeService(session)
        
        try:
            user_id = await badge_service.get_user_id(user)
            if not user_id:
                embed = discord.Embed(
                    description=f"Cannot find user",
                    color=discord.Color.red()
                )
                await interaction.edit_original_response(embed=embed)
                return
            
            username = await badge_service.get_username(user_id)
            if not username:
                embed = discord.Embed(
                    description=f"Cannot find user",
                    color=discord.Color.red()
                )
                await interaction.edit_original_response(embed=embed)
                return
            
            badges, status_code = await badge_service.get_user_badges(user_id)
            
            if status_code == 403:
                embed = discord.Embed(
                    description="This user's inventory is private. Badges cannot be viewed.",
                    color=discord.Color.red()
                )
                await interaction.edit_original_response(embed=embed)
                return
            elif status_code != 200:
                embed = discord.Embed(
                    description=f"Failed to fetch badges",
                    color=discord.Color.red()
                )
                await interaction.edit_original_response(embed=embed)
                return
            
            if not badges:
                embed = discord.Embed(
                    description="This user has no badges.",
                    color=discord.Color.red()
                )
                await interaction.edit_original_response(embed=embed)
                return
            
            badge_ids = [badge['id'] for badge in badges]
            awarded_dates = await badge_service.get_awarded_dates(user_id, badge_ids)
            
            for badge in badges:
                badge_id = badge['id']
                badge['awardedDate'] = awarded_dates.get(badge_id)
            
            view = BadgesView(badges, username, user_id, interaction.user, start_time, badge_service)
            await view.preload_thumbnails()
            embed = await view.create_embed()
            view.update_buttons()
            message = await interaction.edit_original_response(embed=embed, view=view)
            view.message = message
            
        except asyncio.TimeoutError:
            embed = discord.Embed(
                description=f"The request timed out while fetching badge data.",
                color=discord.Color.red()
            )
            await interaction.edit_original_response(embed=embed)
        except aiohttp.ClientError as e:
            embed = discord.Embed(
                description=f"Failed to connect",
                color=discord.Color.red()
            )
            await interaction.edit_original_response(embed=embed)
        except Exception as e:
            print(f"Unexpected error details: {type(e).__name__}: {str(e)}")
            embed = discord.Embed(
                description=f"An unexpected error occurred",
                color=discord.Color.red()
            )
            await interaction.edit_original_response(embed=embed)

@bot.tree.command(name="instagram", description="Get detailed information about an Instagram user")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(username="Instagram username")
async def instagram_command(interaction: discord.Interaction, username: str):
    await interaction.response.defer(thinking=True)
    
    loading = discord.Embed(
        title=f"{Emojis.get('loading')} {interaction.user.mention} Getting Instagram Profile For @{username}",
        color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
    )
    await interaction.edit_original_response(embed=loading)
    
    try:
        username = username.strip().replace('@', '')
        
        if not username:
            errorembed = discord.Embed(
                description="Please provide a valid Instagram username",
                color=discord.Color.red()
            )
            await interaction.edit_original_response(embed=errorembed)
            return
        
        profile_info = await fetch_and_parse_instagram_profile(username)
        
        if isinstance(profile_info, str):
            errorembed = discord.Embed(description=f"{profile_info}", color=discord.Color.red())
            await interaction.edit_original_response(embed=errorembed)
            return
        
        embed = create_instagram_embed(username, profile_info, interaction.user.name)
        await interaction.edit_original_response(embed=embed)
            
    except Exception as e:
        errorembed = discord.Embed(
            description="Failed to fetch Instagram profile information",
            color=discord.Color.red()
        )
        await interaction.edit_original_response(embed=errorembed)

async def get_instagram_profile(session, username):
    url = f"https://i.instagram.com/api/v1/users/web_profile_info/?username={username}"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "X-IG-App-ID": "936619743392459",
        "Referer": f"https://www.instagram.com/{username}/",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
    }
    
    try:
        async with session.get(url, headers=headers, timeout=10) as response:
            if response.status == 404:
                return None, "User not found"
            response.raise_for_status()
            data = await response.json()
            return data["data"]["user"], None
    except aiohttp.ClientError as e:
        return None, f"Connection error: {str(e)}"
    except Exception as e:
        return None, f"Error fetching profile: {str(e)}"

def parse_instagram_data(user):
    return {
        "username": user["username"],
        "full_name": user["full_name"],
        "biography": user["biography"],
        "followers": user["edge_followed_by"]["count"],
        "following": user["edge_follow"]["count"],
        "posts": user["edge_owner_to_timeline_media"]["count"],
        "profile_pic_url": user["profile_pic_url_hd"],
        "is_verified": user["is_verified"],
        "is_private": user["is_private"],
    }

async def fetch_and_parse_instagram_profile(username):
    async with aiohttp.ClientSession() as session:
        user, error = await get_instagram_profile(session, username)
        
        if error:
            return error
        
        return parse_instagram_data(user)

def create_instagram_embed(username, profile_info, requester_name):
    profile_url = f"https://www.instagram.com/{username}/"
    
    title_display = f"@{username}"
    if profile_info['is_verified']:
        title_display += " <:VerifiedInstagram:1447033802802331760>"
    
    embed = discord.Embed(
        title=title_display,
        url=profile_url,
        color=0xE1306C
    )
    
    if profile_info['profile_pic_url']:
        embed.set_thumbnail(url=profile_info['profile_pic_url'])
    
    embed.add_field(name="Posts", value=f"**{profile_info['posts']:,}**", inline=True)
    embed.add_field(name="Followers", value=f"**{profile_info['followers']:,}**", inline=True)
    embed.add_field(name="Following", value=f"**{profile_info['following']:,}**", inline=True)
    embed.add_field(name="Account Type", value="Private" if profile_info['is_private'] else "Public", inline=True)
    
    if profile_info['full_name']:
        embed.add_field(name="Full Name", value=profile_info['full_name'], inline=True)
    
    if profile_info['biography']:
        bio_text = profile_info['biography']
        if len(bio_text) > 1024:
            bio_text = bio_text[:1021] + "..."
        embed.add_field(name="Description", value=bio_text, inline=False)
    
    embed.set_footer(text=f"Requested By {requester_name} │ {MainURL}")
    
    return embed
        
# === Bot Runner in Thread ===
def run_bot():
    bot.run(token)
# === Run Bot + Flask Webserver ===
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 13455))
    threading.Thread(target=run_bot).start()
    print(f"Starting Flask on port {port}")
    app.run(host="0.0.0.0", port=port, debug=True)

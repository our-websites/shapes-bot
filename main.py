"""Shapes for Yapper.

Same commands as the Discord bot, running on Yapper through yapper.py. Data lives in Cloudflare D1
(see cfstore.py), and a small web server runs on port 5004.

Layout of this file:
  1. setup (env, databases, helpers)
  2. commands rewritten for Yapper (no select menus / modals / ephemeral replies there)
  3. commands carried over from the Discord bot (Roblox, Google, AI, GitHub, Instagram, ...)
  4. events (counting, autorole, ready)
  5. web server + startup
"""
import asyncio
import base64
import hmac
import io
import json
import os
import re
import socket
import sys
import threading
import time
import typing
import urllib
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import aiohttp
import pytz
import requests
import yapper
from flask import Flask, Response, jsonify, redirect, render_template_string, request, session, url_for
from flask_cors import CORS
from openai import OpenAI
from slpp import slpp as lua

import yshim
import automod
from cfstore import CloudflareD1
from yshim import app_commands, commands, discord

# Emoji used by the Discord version were custom server emoji. yshim.clean() turns them into plain text/emoji.
Emojis = {
    "loading": "<a:loading:1416950730094542881>",
    "RobloxEmojis": {
        "rolimons": "<:RolimonsLogo:1417258794974711901>",
        "logo": "<:RobloxLogo:1416951004607418398>",
        "verified": "<:RobloxVerified:1416951927513677874>",
        "premium": "<:RobloxPremium:1416951078200541378>",
    },
}


def _secret(env_name, file_name=None):
    """A secret from the environment, or from a local text file (how the Discord bot stored them)."""
    value = os.environ.get(env_name)
    if value:
        return value.strip()
    if file_name:
        try:
            with open(file_name, "r") as f:
                return f.read().strip()
        except OSError:
            pass
    return None


# OpenAI-compatible client for /ai
chatgpt = OpenAI(api_key=os.getenv("OPENAI_KEY") or "unset", base_url="https://openrouter.ai/api/v1")
AIModel = "nvidia/nemotron-3-ultra-550b-a55b:free"

# === Databases (Cloudflare D1, or local JSON when the Cloudflare env vars are not set) ===
countingDB = CloudflareD1("counting")
embedDB = CloudflareD1("embed")
usersDB = CloudflareD1("users")
autoroleDB = CloudflareD1("autorole")
AI_DB = CloudflareD1("ai")
automodDB = CloudflareD1("automod")
dmDB = CloudflareD1("dms")       # DM channels the dashboard knows about: user id -> {channel_id, name, last}
DATABASES = {"embedDB": embedDB, "countingDB": countingDB, "usersDB": usersDB, "autoroleDB": autoroleDB, "AI_DB": AI_DB,
             "automodDB": automodDB, "dmDB": dmDB}

# === Yapper bot ===
bot = yapper.Bot()
bot.tree = yshim.tree
yshim.BOT = bot
owner = "leandre"
MainURL = "https://our-websites.github.io/shapes.lol/yapin"
InviteURL = os.environ.get("YAPPER_INVITE_URL", "https://our-websites.github.io/shapes.lol/yapin?s=true")
searchengine = "621a38269031b4e89"  # PLEASE USE YOUR OWN SEARCH ENGINE ID FROM https://cse.google.com/

APIBaseURL_Key = _secret("Shapes_API_Key")
APIBaseURL_Headers = {"Authorization": f"Bearer {APIBaseURL_Key}"}
APIBaseURL = os.environ.get("SHAPES_API_URL", "http://localhost:25574")
GoogleAPIKey = _secret("GOOGLE_API_KEY", "GoogleToken.txt")
SPOOK_API_KEY = _secret("SPOOK_API_KEY", "SpookToken.txt") or ""
ADMIN_KEY = _secret("ADMIN_KEY", "akey.txt")

bot_ready = False
started_at = time.time()


def ecolor(user_id):
    """A user's saved embed colour, or blue."""
    return embedDB.get(f"{user_id}") or discord.Color.blue()


# last online cache (Roblox)
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


def IsInteger(s):
    try:
        int(s)
        return True
    except (TypeError, ValueError):
        return False


def isotodiscordtimestamp(iso_timestamp_str: str, format_type: str = "D") -> str:
    """ISO timestamp -> a <t:unix:style> marker. yshim.clean() turns the marker into readable text when sending."""
    try:
        if '.' in iso_timestamp_str and iso_timestamp_str.endswith('Z'):
            iso_timestamp_str = iso_timestamp_str.split('.')[0] + '+00:00'
        elif '.' in iso_timestamp_str and '+00:00' in iso_timestamp_str:
            iso_timestamp_str = iso_timestamp_str.split('.')[0] + '+00:00'
        elif iso_timestamp_str.endswith('Z'):
            iso_timestamp_str = iso_timestamp_str.replace('Z', '+00:00')
        dt_object = datetime.fromisoformat(iso_timestamp_str)
        if dt_object.tzinfo is None:
            dt_object = pytz.utc.localize(dt_object)
        else:
            dt_object = dt_object.astimezone(pytz.utc)
        return f"<t:{int(dt_object.timestamp())}:{format_type}>"
    except ValueError:
        return None


DiscordColors = [
    ("Blue", discord.Color.blue()), ("Red", discord.Color.red()), ("Green", discord.Color.green()),
    ("Purple", discord.Color.purple()), ("Orange", discord.Color.orange()), ("Gold", discord.Color.gold()),
    ("Teal", discord.Color.teal()), ("Dark Blue", discord.Color.dark_blue()), ("Dark Red", discord.Color.dark_red()),
    ("Dark Green", discord.Color.dark_green()), ("Dark Purple", discord.Color.dark_purple()),
    ("Dark Orange", discord.Color.dark_orange()), ("Dark Gold", discord.Color.dark_gold()),
    ("Dark Teal", discord.Color.dark_teal()),
]


# === Server / channel helpers ===
_chan_server = {}


def server_of(channel_id, hint=None):
    """The server a channel belongs to (None for a DM)."""
    if hint:
        return str(hint)
    key = str(channel_id)
    if key not in _chan_server:
        ch = bot.channel(channel_id)
        _chan_server[key] = str(yshim._first(ch or {}, "server_id", "guild_id") or "") or None
    return _chan_server[key]


def resolve_channel(server_id, token):
    """A text channel from a <#id> mention, an id, or a name."""
    token = (token or "").strip()
    m = re.match(r"^<#([^>\s]+)>$", token)
    wanted = (m.group(1) if m else token).lstrip("#")
    for c in bot.channels(server_id, type="text"):
        if str(c.get("id")) == wanted or str(c.get("name", "")).lower() == wanted.lower():
            return c
    return None


_member_cache = {"at": 0, "value": 0}


def total_members():
    """Approximate number of people across every server the bot is in (cached for 5 minutes)."""
    if time.time() - _member_cache["at"] < 300:
        return _member_cache["value"]
    total = 0
    for s in bot.servers():
        n = yshim._first(s, "member_count", "members_count", "memberCount")
        if n is None:
            try:
                n = len(bot.members(s.get("id")))
            except Exception:
                n = 0
        total += int(n)
    _member_cache.update(at=time.time(), value=total)
    return total


def default_counting():
    return {"channel": None, "number": 0, "enabled": False, "warnings": 0, "lastcounter": None, "highestnumber": 0, "banned": False}


async def need_admin(interaction):
    """True if the caller may manage this server (replies and returns False otherwise)."""
    if interaction.is_dm:
        await interaction.response.send_message("This command only works inside a server.", ephemeral=True)
        return False
    if not yshim.is_admin(interaction.guild_id, interaction.user.id):
        await interaction.response.send_message("You need to be a server admin to use this command.", ephemeral=True)
        return False
    return True



# =====================================================================================
# Commands rewritten for Yapper
# (select menus and modals do not exist for commands, so these take arguments instead; private replies use ephemeral=True)
# =====================================================================================

@bot.tree.command(name="userinstalls", description="Get The User Installation Count For Shapes!")
async def userinstalls(interaction: discord.Interaction):
    await interaction.response.send_message(f"{total_members():,} Users Use Shapes!")


@bot.tree.command(name="servercount", description="Get The Server Count For Shapes!")
async def servercount(interaction: discord.Interaction):
    await interaction.response.send_message(f"{len(bot.servers()):,} Servers Use Shapes!")


@bot.tree.command(name="getdata", description="Owner only: /getdata <embedDB|countingDB|usersDB|autoroleDB|AI_DB> <key>")
async def getdata(interaction: discord.Interaction, database: str, key: str):
    # Private reply (ephemeral): only the person who ran the command sees it.
    if not yshim.is_owner(interaction.user):
        await interaction.response.send_message(":x: You don't have permission to use this command! :x:", ephemeral=True)
        return
    db = DATABASES.get(database)
    if db is None:
        await interaction.response.send_message(f":x: Database `{database}` not found! Options: {', '.join(DATABASES)}", ephemeral=True)
        return
    data = db.get(key)
    if data:
        text = f"Data for key `{key}` in database `{database}`:\n```{json.dumps(data, indent=2, default=str)[:1800]}```"
    else:
        text = f":x: No data found for key `{key}` in database `{database}`! :x:"
    await interaction.response.send_message(text, ephemeral=True)


@bot.tree.command(name="settings", description="Your Settings For Shapes: /settings color <name>")
async def settings(interaction: discord.Interaction, option: str = "", value: str = ""):
    uid = interaction.user.id
    names = [n for n, _ in DiscordColors]
    if option.lower() in ("color", "colour"):
        pick = value.strip().lower().replace("_", " ")
        match = next(((n, c) for n, c in DiscordColors if n.lower() == pick), None)
        if pick == "random":
            match = ("Random", discord.Color.random())
        if not match:
            await interaction.response.send_message(f"Pick one of: {', '.join(names)}, Random", ephemeral=True)
            return
        embedDB.set(f"{uid}", match[1].value)
        embed = discord.Embed(title="Embed Color Changed!",
                              description=f"Your embed color has been changed successfully to {match[0]}!", color=match[1])
        embed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    await interaction.response.send_message(embed=_settings_embed(interaction.user), view=_color_view(uid), ephemeral=True)


def _settings_embed(user, note=None):
    current = embedDB.get(f"{user.id}")
    current_name = next((n for n, c in DiscordColors if c.value == current), "Blue" if not current else "Custom")
    embed = discord.Embed(title="Settings", color=ecolor(user.id),
                          description=(f"✅ {note}\n\n" if note else "") + f"**Embed color:** {current_name}\n\nTap a color below to change it.")
    embed.set_footer(text=f"Requested By {user.name} | {MainURL}")
    return embed


def _color_view(user_id):
    """One button per embed color; tapping one saves it and refreshes the panel."""
    view = discord.ui.View(timeout=300)
    current = embedDB.get(f"{user_id}")
    for name, color in DiscordColors + [("Random", None)]:
        selected = color is not None and color.value == current
        button = discord.ui.Button(label=name, style=discord.ButtonStyle.primary if selected else discord.ButtonStyle.secondary)

        async def pick(click, name=name, color=color):
            if str(click.user.id) != str(user_id):
                await click.response.send_message("This isn't your settings panel. Run /settings yourself.", ephemeral=True)
                return
            chosen = color or discord.Color.random()
            embedDB.set(f"{user_id}", chosen.value)
            await click.response.edit_message(embed=_settings_embed(click.user, f"Embed color changed to {name}!"), view=_color_view(user_id))

        button.callback = pick
        view.add_item(button)
    return view


@bot.tree.command(name="status", description=f"Get the {MainURL} status")
async def status_command(interaction: discord.Interaction):
    await interaction.response.send_message(f"[shapes.lol Status Page](https://shapes.lol/status)")


def _save_everything():
    for db in DATABASES.values():
        try:
            db.save()
        except Exception as e:
            print(f"save failed for {db.name}: {e}")


def _shutdown_services():
    """Stop the bot and release the web server's port, so a restart can bind it again right away."""
    for label, stopper in (("bot", getattr(bot, "stop", None)), ("web server", stop_web)):
        try:
            if stopper:
                stopper()
        except Exception as e:
            print(f"could not stop the {label}: {e}")


class ConfirmStop(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)

    async def interaction_check(self, click):
        if not yshim.is_owner(click.user):
            await click.response.send_message("Only the bot owner can press this.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Stop the bot", style=discord.ButtonStyle.danger)
    async def confirm(self, click, button):
        self.stop()
        await click.response.edit_message(content="✅ Shutting down. Saving data first…", embed=None, view=None)
        _save_everything()
        print("Bot shutdown initiated by command")
        _shutdown_services()
        threading.Timer(1.0, lambda: os._exit(0)).start()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, click, button):
        self.stop()
        await click.response.edit_message(content="Cancelled. The bot is still running.", embed=None, view=None)


@bot.tree.command(name="stop", description="Stop the bot (asks you to confirm).")
async def stop(interaction: discord.Interaction):
    if not yshim.is_owner(interaction.user):
        await interaction.response.send_message(f"Only {owner} can use this command.", ephemeral=True)
        return
    await interaction.response.send_message("⚠️ Stop the bot completely? It stays offline until someone starts it again.",
                                            view=ConfirmStop(), ephemeral=True)


@bot.tree.command(name="restart", description="Restart the bot.")
async def restart(interaction: discord.Interaction):
    if not yshim.is_owner(interaction.user):
        await interaction.response.send_message(f"Only {owner} can use this command.", ephemeral=True)
        return
    await interaction.response.send_message(":white_check_mark: Restarting...", ephemeral=True)
    _save_everything()
    print("Bot restart initiated by command")
    _shutdown_services()    # frees port 5004 first: starting a second server on it was what broke restarts

    def relaunch():
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:
            pass
        argv = [sys.executable] + sys.argv
        try:
            os.closerange(3, 1024)      # nothing (sockets, files) should leak into the new process
            os.execv(sys.executable, argv)
        except Exception as e:
            print(f"execv failed ({e}), starting a new process instead")
            import subprocess
            subprocess.Popen(argv)
            os._exit(0)
    threading.Timer(1.5, relaunch).start()


@bot.tree.command(name="counting", description="Counting Settings: /counting [toggle|channel <#channel>]")
async def counting(interaction: discord.Interaction, action: str = "", value: str = ""):
    if not await need_admin(interaction):
        return
    sid = str(interaction.guild_id)
    data = countingDB.get(sid) or default_counting()
    action = action.lower()
    if action == "toggle":
        data["enabled"] = not data["enabled"]
        countingDB.set(sid, data)
        await interaction.response.send_message(f"Counting {'enabled' if data['enabled'] else 'disabled'}.")
        return
    if action == "channel":
        ch = resolve_channel(sid, value) if value else None
        if not ch:
            await interaction.response.send_message("Tell me which text channel: `/counting channel #general`")
            return
        data["channel"] = str(ch.get("id"))
        countingDB.set(sid, data)
        await interaction.response.send_message(f"Counting channel set to <#{ch.get('id')}>.")
        return
    countingDB.set(sid, data)
    channel = f"<#{data['channel']}>" if data["channel"] else "not set"
    embed = discord.Embed(
        title="Counting Settings", color=ecolor(interaction.user.id),
        description=f"**Current Settings:**\n- Counting Enabled: `{data['enabled']}`\n- Counting Channel: {channel}\n"
                    f"- Current Number: `{data['number']}`\n- Highest Number: `{data.get('highestnumber', 0)}`\n"
                    f"- Warnings: `{data['warnings']}`\n- Last Counter: `{data['lastcounter']}`\n\n"
                    f"Change it with `/counting toggle` or `/counting channel #channel`")
    embed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="autorole", description="Automatically give new members a role: /autorole <role> [true|false]")
async def autorole(interaction: discord.Interaction, role: discord.Role, enable: bool = True):
    if not await need_admin(interaction):
        return
    sid = str(interaction.guild_id)
    autoroleDB.set(sid, {"role_id": str(role.id), "enabled": enable})
    server_id = interaction.guild_id

    class AutoroleView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=300)

        async def interaction_check(self, click):
            if not yshim.is_admin(server_id, click.user.id):
                await click.response.send_message("Only server admins can do this.", ephemeral=True)
                return False
            return True

        @discord.ui.button(label="Assign to Existing Members", style=discord.ButtonStyle.primary)
        async def assign_existing(self, click, button):
            await click.response.send_message("Working on it...", ephemeral=True)
            count, after, seen = 0, None, set()
            while True:
                page = bot.members(server_id, after=after)
                fresh = [m for m in page if str(yshim._first(m, "user_id", "id")) not in seen]
                if not fresh:
                    break
                for m in fresh:
                    uid = str(yshim._first(m, "user_id", "id"))
                    seen.add(uid)
                    if m.get("bot") or m.get("is_bot") or str(role.id) in {str(r.get("id") if isinstance(r, dict) else r) for r in (m.get("roles") or [])}:
                        continue
                    try:
                        bot.add_role(server_id, uid, role.id)
                        count += 1
                    except Exception:
                        pass
                after = yshim._first(fresh[-1], "user_id", "id")
                if len(page) < 200:
                    break
            yshim.post(click.channel_id, f"Assigned {role.name} to {count} existing members.")

    status = "enabled" if enable else "disabled"
    await interaction.response.send_message(f"Autorole {status} for {role.name}. Assign to existing members?", view=AutoroleView(), ephemeral=True)


@bot.tree.command(name="help", description="Get help with all available commands")
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(title="Bot Help & Commands", color=ecolor(interaction.user.id),
                          description="Use `/command` or `!command`. Put names with spaces in quotes.")
    embed.add_field(name="Counting", value="• /counting - Counting settings (`toggle`, `channel #channel`)\n• /countingleaderboard - Leaderboard website", inline=False)
    embed.add_field(name="User Settings", value="• /settings - Customize your embed color\n• /autorole - Give new members a role\n• /lastfm - Link Last.fm", inline=False)
    embed.add_field(name="Moderation", value="• /automod - Block words, links, invites, spam, caps and mass mentions (admins)", inline=False)
    embed.add_field(name="Roblox", value=(
        "• /robloxinfo - User information\n• /roblox2yapin - Find a Roblox user's linked account\n• /yapin2roblox - Find a user's Roblox\n"
        "• /britishuser - Check if a user is British\n• /iteminfo - Item information\n• /placeinfo - Place/game information\n"
        "• /badge - Badge information\n• /badges - Check a user's badges\n• /recentbadges - Latest badges\n• /outfits - A user's outfits\n"
        "• /limiteds - Scan for limiteds\n• /bundle - Bundle information\n• /asset - Asset information\n• /avatar - Avatar\n• /linkroblox - Link your Roblox"), inline=False)
    embed.add_field(name="Utility", value=(
        "• /ping - Latency\n• /invite - Invite link\n• /search - Search The Web\n• /ai - Chat with an AI\n• /github - GitHub user\n"
        "• /instagram - Instagram user\n• /status - shapes.lol status\n• /servercount, /userinstalls - Counts\n• /spookpfp, /yapin2spook - spook.bio"), inline=False)
    embed.add_field(name="Yapper profile", value="• /yapperuser - Info about a user\n• /yapinavatar - Avatar\n• /yapinbanner - Banner", inline=False)
    embed.set_footer(text="Right-click context menus from the Discord bot are not available on Yapper; use the slash commands.")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="countingleaderboard", description="View the counting leaderboard website")
async def countingleaderboard(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Counting Leaderboard",
        description=f"[Click here to view the counting leaderboard]({MainURL}/counting/leaderboard/)",
        color=ecolor(interaction.user.id))
    embed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="invite", description="Get the bot's invite link.")
async def invite(interaction: discord.Interaction):
    embed = discord.Embed(description=f"[Click Here To Add Shapes To Your Server]({InviteURL})", color=ecolor(interaction.user.id))
    embed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
    await interaction.response.send_message(embed=embed)


def _latency_ms():
    """Round trip to the API on a warm (reused) connection, so it measures the network and not the TLS handshake."""
    bot.api("GET", "/me", retries=0)
    start = time.perf_counter()
    bot.api("GET", "/me", retries=0)
    return (time.perf_counter() - start) * 1000


def _quality(ms):
    return "Good" if ms < 200 else "Average" if ms < 400 else "Poor"


@bot.tree.command(name="ping", description="Check the bot's latency.")
async def ping(interaction: discord.Interaction):
    latency = _latency_ms()
    embed = discord.Embed(title="Server Stats", description=f"Latency: `{latency:.2f}ms` ({_quality(latency)} Connection)",
                          color=ecolor(interaction.user.id))
    embed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
    await interaction.response.send_message(embed=embed)
    await asyncio.sleep(2)
    updated = _latency_ms()
    embed = discord.Embed(
        title="Server Stats", color=ecolor(interaction.user.id),
        description=f"OriginalLatency: `{latency:.2f}ms` ({_quality(latency)} Connection)\nEditLatency: `{updated:.2f}ms` ({_quality(updated)} Connection)")
    embed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
    await interaction.edit_original_response(embed=embed, content=None)


# --- Automod ---

_spam = automod.SpamTracker()
_strikes = automod.Strikes()
_admin_cache = {}       # (server, user) -> (expires_at, bool); is_admin costs several API calls


def get_automod(server_id):
    return automod.merged(automodDB.get(str(server_id)))


def save_automod(server_id, cfg):
    cfg = automod.clean_config(cfg)
    automodDB.set(str(server_id), cfg)
    return cfg


def _exempt(sid, uid, channel_id, cfg):
    if str(channel_id) in cfg["exempt_channels"]:
        return True
    key = (sid, str(uid))
    hit = _admin_cache.get(key)
    if not hit or hit[0] < time.time():
        hit = (time.time() + 120, bool(yshim.is_admin(sid, uid)))
        _admin_cache[key] = hit
    if hit[1]:
        return True
    if cfg["exempt_roles"]:
        try:
            roles = {str(r.get("id") if isinstance(r, dict) else r) for r in (bot.member(sid, uid).get("roles") or [])}
            return bool(roles & set(cfg["exempt_roles"]))
        except Exception:
            return False
    return False


def handle_automod(msg):
    """Returns True when the message was removed (so other handlers, like counting, skip it)."""
    if msg.get("author_bot") or msg.get("command"):
        return False
    sid = server_of(msg.get("channel_id"), yshim._first(msg, "server_id", "guild_id"))
    if not sid:
        return False
    stored = automodDB.get(sid)
    if not stored:
        return False
    cfg = automod.merged(stored)
    if not cfg["enabled"]:
        return False
    uid, channel, mid = str(msg.get("author_id")), msg.get("channel_id"), msg.get("id")
    hits = automod.violations(cfg, msg.get("content") or "", (sid, uid), _spam)
    if not hits or _exempt(sid, uid, channel, cfg):
        return False

    actions = cfg["actions"]
    removed = False
    if actions["delete"]:
        try:
            bot.delete(channel, mid)
            removed = True
        except Exception as e:
            print(f"[automod] could not delete {mid}: {e}")
    strikes = _strikes.add((sid, uid), actions["strike_minutes"] * 60)
    punishment = automod.escalation(cfg, strikes)
    why = ", ".join(automod.REASONS[h] for h in hits)
    outcome = "message removed" if removed else "flagged"
    if punishment == "kick":
        try:
            bot.kick(sid, uid)
            outcome, _ = "kicked", _strikes.clear((sid, uid))
        except Exception as e:
            print(f"[automod] kick failed: {e}")
    elif punishment == "timeout":
        try:
            bot.timeout(sid, uid, actions["timeout_minutes"], reason=f"Automod: {why}")
            outcome = f"timed out for {actions['timeout_minutes']} min"
            _strikes.clear((sid, uid))
        except Exception as e:
            print(f"[automod] timeout failed: {e}")
    if actions["warn"] and punishment is None:
        try:
            bot.send(channel, f"⚠️ <@{uid}>, {why}. ({strikes}/{actions['timeout_after'] or '∞'} strikes)")
        except Exception as e:
            print(f"[automod] warning failed: {e}")
    if cfg["log_channel"]:
        try:
            bot.send(cfg["log_channel"], f"🛡️ **Automod** · <@{uid}> in <#{channel}> {why}. Result: {outcome}.")
        except Exception as e:
            print(f"[automod] log failed: {e}")
    return removed or punishment is not None


def _automod_summary(cfg):
    r, a = cfg["rules"], cfg["actions"]

    def flag(name):
        return "✅" if r[name]["on"] else "❌"
    lines = [
        f"**Automod is {'ON' if cfg['enabled'] else 'OFF'}**" + (f" · logs in <#{cfg['log_channel']}>" if cfg["log_channel"] else ""),
        f"{flag('words')} words ({len(r['words']['list'])} blocked)",
        f"{flag('links')} links" + (f" (allowed: {', '.join(r['links']['allow'])})" if r["links"]["allow"] else ""),
        f"{flag('invites')} invites",
        f"{flag('caps')} caps (≥{r['caps']['percent']}% of {r['caps']['min_length']}+ letters)",
        f"{flag('spam')} spam ({r['spam']['count']} messages in {r['spam']['seconds']}s)",
        f"{flag('mentions')} mentions (more than {r['mentions']['limit']})",
        f"Punishment: delete + warn; timeout {a['timeout_minutes']} min after {a['timeout_after'] or 'never'} strikes"
        + (f"; kick after {a['kick_after']}" if a["kick_after"] else "") + f" (strikes last {a['strike_minutes']} min)",
    ]
    return "\n".join(lines)


AUTOMOD_HELP = ("**/automod** (admins only)\n"
                "`status` · `enable` · `disable`\n"
                "`rule <words|links|invites|caps|spam|mentions> <on|off> [number]` (number = caps %, spam messages, or mention limit)\n"
                "`words add|remove|list <word>`\n"
                "`allow <domain>` / `unallow <domain>` for the links rule\n"
                "`log <#channel|off>` · `exempt <#channel>` (toggle) · `exemptrole <role>` (toggle)\n"
                "`timeout <strikes> <minutes>` · `kick <strikes|0>`")


@bot.tree.command(name="automod", description="Configure automod: /automod status, enable, rule, words, allow, log, timeout...")
async def automod_command(interaction: discord.Interaction, action: str = "status", a: str = "", b: str = ""):
    if not await need_admin(interaction):
        return
    sid = str(interaction.guild_id)
    cfg = get_automod(sid)
    action = action.lower().strip()

    async def reply(text):
        await interaction.response.send_message(text, ephemeral=True)

    if action in ("status", ""):
        return await reply(_automod_summary(cfg))
    if action in ("enable", "disable"):
        cfg["enabled"] = action == "enable"
    elif action == "rule":
        name, state = a.lower(), b.lower()
        if name not in automod.RULE_NAMES or state not in ("on", "off"):
            return await reply("Usage: `/automod rule <words|links|invites|caps|spam|mentions> <on|off>`")
        cfg["rules"][name]["on"] = state == "on"
    elif action == "set":
        name, rest = a.lower(), b
        keys = {"caps": "percent", "spam": "count", "mentions": "limit"}
        if name not in keys or not rest.isdigit():
            return await reply("Usage: `/automod set <caps|spam|mentions> <number>`")
        cfg["rules"][name][keys[name]] = int(rest)
    elif action == "words":
        sub, word = a.lower(), b.strip().lower()
        words = cfg["rules"]["words"]["list"]
        if sub == "list":
            return await reply("Blocked words: " + (", ".join(f"||{w}||" for w in words) if words else "none yet"))
        if sub not in ("add", "remove") or not word:
            return await reply("Usage: `/automod words add|remove|list <word>`")
        if sub == "add" and word not in words:
            words.append(word)
        if sub == "remove" and word in words:
            words.remove(word)
        try:   # the message with the word in it is not left for everyone to read
            bot.delete(interaction.channel_id, interaction._msg.get("id")) if interaction._msg else None
        except Exception:
            pass
    elif action in ("allow", "unallow"):
        domain = a.lower().replace("https://", "").replace("http://", "").strip("/")
        allow = cfg["rules"]["links"]["allow"]
        if not domain:
            return await reply("Usage: `/automod allow <domain>`")
        if action == "allow" and domain not in allow:
            allow.append(domain)
        if action == "unallow" and domain in allow:
            allow.remove(domain)
    elif action == "log":
        if a.lower() in ("off", "none", ""):
            cfg["log_channel"] = None
        else:
            ch = resolve_channel(sid, a)
            if not ch:
                return await reply("I couldn't find that channel.")
            cfg["log_channel"] = str(ch.get("id"))
    elif action == "exempt":
        ch = resolve_channel(sid, a)
        if not ch:
            return await reply("Usage: `/automod exempt <#channel>` (run again to remove it)")
        cid = str(ch.get("id"))
        cfg["exempt_channels"] = [c for c in cfg["exempt_channels"] if c != cid] if cid in cfg["exempt_channels"] else cfg["exempt_channels"] + [cid]
    elif action == "exemptrole":
        try:
            role = yshim.resolve_role(a, sid) if a else None
        except ValueError:
            role = None
        if not role:
            return await reply("Usage: `/automod exemptrole <role>` (run again to remove it). I couldn't find that role.")
        rid = str(role.id)
        cfg["exempt_roles"] = [r for r in cfg["exempt_roles"] if r != rid] if rid in cfg["exempt_roles"] else cfg["exempt_roles"] + [rid]
    elif action == "timeout":
        if not (a.isdigit() and b.isdigit()):
            return await reply("Usage: `/automod timeout <strikes> <minutes>` (0 strikes = never time out)")
        cfg["actions"]["timeout_after"], cfg["actions"]["timeout_minutes"] = int(a), max(1, int(b))
    elif action == "kick":
        if not a.isdigit():
            return await reply("Usage: `/automod kick <strikes>` (0 = never kick)")
        cfg["actions"]["kick_after"] = int(a)
    else:
        return await reply(AUTOMOD_HELP)
    cfg = save_automod(sid, cfg)
    await reply("✅ Saved.\n\n" + _automod_summary(cfg))


# --- Last.fm (linked with Last.fm's web auth, so the account is verified by Last.fm itself) ---
# Flow: /lastfm link -> private message with a Last.fm authorize button -> Last.fm sends the person back to
# PUBLIC_URL/lastfm/callback/<state>?token=... -> we trade the token for a session (name + key) and save it.
# Env: LASTFM_API_KEY, LASTFM_API_SECRET (both from https://www.last.fm/api/account/create) and PUBLIC_URL.

import hashlib

LastFMKey = _secret("LASTFM_API_KEY", "LastFMToken.txt")
LastFMSecret = _secret("LASTFM_API_SECRET", "LastFMSecret.txt")
PUBLIC_URL = (os.environ.get("PUBLIC_URL") or "").rstrip("/")
LASTFM_STATE_KEY = (os.environ.get("FLASK_SECRET_KEY") or os.urandom(32).hex()).encode()
LASTFM_STATE_TTL = 600
_lastfm_used_states = {}
_lastfm_cache = {}          # name -> (expires_at, profile); keeps us far below Last.fm's rate limit
LASTFM_CACHE_SECONDS = 60


def lastfm_ready():
    return bool(LastFMKey and LastFMSecret and PUBLIC_URL)


def _lastfm_sign(params):
    """api_sig: md5 of every parameter (name+value, sorted by name, without format/callback) + the shared secret."""
    raw = "".join(f"{k}{v}" for k, v in sorted(params.items()) if k not in ("format", "callback")) + LastFMSecret
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def lastfm_call(method, signed=False, **params):
    """One Last.fm API call; returns the JSON dict, or None on any failure (including missing config)."""
    if not LastFMKey or (signed and not LastFMSecret):
        return None
    q = {"method": method, "api_key": LastFMKey, **params}
    if signed:
        q["api_sig"] = _lastfm_sign(q)
    q["format"] = "json"
    try:
        r = requests.get("https://ws.audioscrobbler.com/2.0/", params=q, timeout=8)
        data = r.json()
        return None if "error" in data else data
    except Exception as e:
        print(f"lastfm {method} failed: {e}")
        return None


def lastfm_state(user_id):
    """A tamper-proof, expiring token that says which Yapin user started the link."""
    expires = int(time.time()) + LASTFM_STATE_TTL
    payload = f"{user_id}.{expires}"
    return f"{payload}.{hmac.new(LASTFM_STATE_KEY, payload.encode(), hashlib.sha256).hexdigest()[:32]}"


def lastfm_state_user(state):
    """The user id a state was made for, or None if it is forged, expired or already used."""
    try:
        user_id, expires, sig = state.rsplit(".", 2)
        good = hmac.new(LASTFM_STATE_KEY, f"{user_id}.{expires}".encode(), hashlib.sha256).hexdigest()[:32]
        now = time.time()
        for k in [k for k, t in _lastfm_used_states.items() if t < now]:
            _lastfm_used_states.pop(k, None)
        if not hmac.compare_digest(sig, good) or int(expires) < now or state in _lastfm_used_states:
            return None
        _lastfm_used_states[state] = int(expires)
        return user_id
    except (ValueError, AttributeError):
        return None


def lastfm_auth_url(user_id):
    callback = f"{PUBLIC_URL}/lastfm/callback/{lastfm_state(user_id)}"
    return f"https://www.last.fm/api/auth/?api_key={LastFMKey}&cb={urllib.parse.quote(callback, safe='')}"


def lastfm_finish_link(state, token):
    """Callback step: returns the verified Last.fm username (and saves it), or None."""
    user_id = lastfm_state_user(state)
    if not user_id or not token:
        return None
    data = lastfm_call("auth.getSession", signed=True, token=token)
    session = (data or {}).get("session") or {}
    if not session.get("name"):
        return None
    set_lastfm(user_id, session["name"], session.get("key"))
    return session["name"]


def get_lastfm(user_id):
    """The linked (verified) Last.fm username for a Yapin user id, or None."""
    entry = usersDB.get(str(user_id))
    value = entry.get("lastfm") if isinstance(entry, dict) else None
    return value.get("name") if isinstance(value, dict) else value


def set_lastfm(user_id, name, session_key=None):
    """Link (or with name=None, unlink) a Last.fm account; keeps whatever else is stored for the user."""
    entry = usersDB.get(str(user_id))
    entry = dict(entry) if isinstance(entry, dict) else {}
    if name:
        entry["lastfm"] = {"name": name, "key": session_key, "linked_at": int(time.time())}
    else:
        entry.pop("lastfm", None)
    usersDB.set(str(user_id), entry)


def lastfm_profile(name):
    """Summary for a Last.fm user: url, scrobbles, and the current/last track. None if the lookup failed. Cached."""
    hit = _lastfm_cache.get(name.lower())
    if hit and hit[0] > time.time():
        return hit[1]
    result = _lastfm_profile_uncached(name)
    if result:
        _lastfm_cache[name.lower()] = (time.time() + LASTFM_CACHE_SECONDS, result)
    return result


def _lastfm_profile_uncached(name):
    info = lastfm_call("user.getinfo", user=name)
    if not info or "user" not in info:
        return None
    u = info["user"]
    out = {"name": u.get("name", name), "url": u.get("url") or f"https://www.last.fm/user/{name}",
           "scrobbles": u.get("playcount"), "track": None, "now": False, "image": None}
    for img in reversed(u.get("image") or []):
        if img.get("#text"):
            out["image"] = img["#text"]
            break
    recent = lastfm_call("user.getrecenttracks", user=name, limit=1)
    try:
        tracks = recent["recenttracks"]["track"]
        if isinstance(tracks, dict):
            tracks = [tracks]
        if tracks:
            t = tracks[0]
            out["track"] = f"{t['artist']['#text']} - {t['name']}"
            out["now"] = bool((t.get("@attr") or {}).get("nowplaying"))
    except (TypeError, KeyError):
        pass
    return out


def lastfm_line(p):
    scrobbles = f"{int(p['scrobbles']):,}" if str(p.get("scrobbles") or "").isdigit() else "?"
    text = f"[{p['name']}]({p['url']}) · {scrobbles} scrobbles"
    if p.get("track"):
        text += f"\n{'🎶 Now playing' if p['now'] else '⏮️ Last played'}: {p['track']}"
    return text


@bot.tree.command(name="lastfm", description="Link your Last.fm account or see what someone is listening to")
async def lastfm(interaction: discord.Interaction, action: str = "", user: discord.User = None):
    action = action.lower().strip()
    uid = interaction.user.id
    if action == "link":
        if not lastfm_ready():
            await interaction.response.send_message("Last.fm linking isn't set up on this bot yet (the owner needs LASTFM_API_KEY, LASTFM_API_SECRET and PUBLIC_URL).", ephemeral=True)
            return
        view = discord.ui.View(timeout=LASTFM_STATE_TTL)
        view.add_item(discord.ui.Button(label="Authorize on Last.fm", style=discord.ButtonStyle.link, url=lastfm_auth_url(uid)))
        await interaction.response.send_message("Tap the button, approve access on Last.fm and you're linked. The link works for 10 minutes and only for you.", view=view, ephemeral=True)
        return
    if action == "unlink":
        had = get_lastfm(uid)
        set_lastfm(uid, None)
        await interaction.response.send_message("Unlinked your Last.fm account." if had else "You had no Last.fm account linked.", ephemeral=True)
        return
    target = user or interaction.user
    linked = get_lastfm(target.id)
    if not linked:
        who = "You haven't" if target.id == uid else f"{target.display_name} hasn't"
        await interaction.response.send_message(f"{who} linked a Last.fm account. Use `/lastfm link`.", ephemeral=True)
        return
    p = lastfm_profile(linked)
    if not p:
        await interaction.response.send_message(f"Couldn't reach Last.fm for `{linked}` right now.", ephemeral=True)
        return
    embed = discord.Embed(title=f"{target.display_name} on Last.fm", description=lastfm_line(p), color=ecolor(uid))
    if p["image"]:
        embed.set_thumbnail(url=p["image"])
    embed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
    await interaction.response.send_message(embed=embed)


# --- Yapper profile commands (/yapperuser, /yapinavatar, /yapinbanner; these were the Discord user/avatar/banner commands) ---

def _profile_target(interaction, user):
    """The user to look up. Fills in the full profile from Yapper so avatar/banner/bio are available."""
    target = user or interaction.user
    data = yshim.fetch_user(target.id)
    if data:
        target.data = {**target.data, **{k: v for k, v in data.items() if v not in (None, "")}}   # the full profile wins
        target.name = yshim._first(data, "username", "name") or target.name
        target.display_name = yshim._first(data, "display_name", "displayName") or target.display_name
    return target


@bot.tree.command(name="yapperuser", description="Get information about a user")
async def yapperuser(interaction: discord.Interaction, user: discord.User = None):
    target = _profile_target(interaction, user)
    embed = discord.Embed(title=f"{target.display_name}" + (f" (@{target.name})" if target.display_name != target.name else ""),
                          color=ecolor(interaction.user.id))
    bio = yshim._first(target.data, "bio", "about", "description")
    if bio:
        embed.description = str(bio)
    embed.add_field(name="ID", value=f"`{target.id}`", inline=True)
    embed.add_field(name="Bot", value="Yes" if target.is_bot else "No", inline=True)
    created = target.created_at
    if created:
        embed.add_field(name="Created", value=f"<t:{int(created.timestamp())}:D>", inline=True)
    if target.avatar_url:
        embed.set_thumbnail(url=target.avatar_url)
    if target.banner_url:
        embed.set_image(url=target.banner_url)

    mutual = []
    try:
        for s in bot.servers():
            try:
                bot.member(s.get("id"), target.id)
                mutual.append(s.get("name") or str(s.get("id")))
            except Exception:
                continue   # not a member of that server
    except Exception as e:
        print(f"mutual server lookup failed: {e}")
    if mutual:
        embed.add_field(name=f"Mutual Servers ({len(mutual)})", value="\n".join(mutual[:15]) + ("\n..." if len(mutual) > 15 else ""), inline=False)
    linked = get_lastfm(target.id)
    if linked:
        p = lastfm_profile(linked)
        embed.add_field(name="LastFM", value=lastfm_line(p) if p else f"[{linked}](https://www.last.fm/user/{linked})", inline=False)
    embed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="yapinavatar", description="Get a user's avatar")
async def yapinavatar(interaction: discord.Interaction, user: discord.User = None):
    target = _profile_target(interaction, user)
    print(str(target))
    url = target.avatar_url
    if not url:
        await interaction.response.send_message(f"{target.name} doesn't have an avatar.")
        return
    embed = discord.Embed(title=f"{target.name}'s Avatar", color=ecolor(interaction.user.id), timestamp=interaction.created_at)
    embed.set_image(url=url)
    embed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="Download Avatar", style=discord.ButtonStyle.link, url=url))
    await interaction.response.send_message(embed=embed, view=view)


@bot.tree.command(name="yapinbanner", description="Get a user's banner")
async def yapinbanner(interaction: discord.Interaction, user: discord.User = None):
    target = _profile_target(interaction, user)
    url = target.banner_url
    if not url:
        await interaction.response.send_message(f"{target.name} doesn't have a banner image.")
        return
    embed = discord.Embed(title=f"{target.name}'s Banner", color=ecolor(interaction.user.id), timestamp=interaction.created_at)
    embed.set_image(url=url)
    embed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="Download Banner", style=discord.ButtonStyle.link, url=url))
    await interaction.response.send_message(embed=embed, view=view)



# =====================================================================================
# Commands carried over from the Discord bot
# =====================================================================================


# --- spookpfp ---
@bot.tree.command(name="spookpfp", description="Get a pfp from a user's spook.bio profile.")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def spookpfp(interaction: discord.Interaction, username: str = "leandre"):
    url = f"https://spook-v3.onrender.com/$/api/v2/users/{username}"
    response = requests.get(url)
    data = response.json()
    if response.status_code == 200:
        await interaction.response.send_message(data.get('Picture_URL') or f":x: {username} has no profile picture :x:", ephemeral=False)
        print("Fetched data successfully!")
    else:
        await interaction.response.send_message(f":x: {username} Not Found :x:", ephemeral=False)
        print(f"Error fetching data: {response.status_code}")


# --- yapin2spook ---
@bot.tree.command(name="yapin2spook", description="Get a Spook profile from a Yapin user.")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def yapin2spook(interaction: discord.Interaction, user: discord.User): # = 481295611417853982):
    url = f"https://spook.bio/api/profiles?discordId={user.id}"
    print(user.id)
    print(url)
    response = requests.get(url, headers={"Authorization": f"{SPOOK_API_KEY}"})
    data = response.json()
    if response.status_code == 200:
        await interaction.response.send_message(f"{user.mention}'s [Spook Profile](https://spook-v3.onrender.com/@{data.get('username')})", ephemeral=False)
        print(f"Fetched {data['username']} successfully!")
    else:
        if interaction.user.name == user.name:
            await interaction.response.send_message(f":x: You don't have a Spook profile linked to your account {user.mention}! :x: To link your profile to your account create a spook.bio account [here](https://spook.bio/login)")
            return
        await interaction.response.send_message(f":x: {user.mention} doesn't have a Spook profile linked to their account! :x:", ephemeral=False)
        print(f"Error fetching data: {response.status_code}")


# --- roblox2yapin ---
@bot.tree.command(name="roblox2yapin", description="Get a Roblox user's linked Yapin account from their username.")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def roblox2yapin(interaction: discord.Interaction, user: str):
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


# --- yapin2roblox ---
@bot.tree.command(name="yapin2roblox", description="Get a Roblox profile from a Yapin user.")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def yapin2roblox(interaction: discord.Interaction, user: discord.User):
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


# --- ai ---
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
                f"You are a helpful Yapper assistant chatting with {username}. "
                "Always respond in a single concise paragraph. Otherwise your response will not be recieved due to the embed text limit"
                "Follow Yapper's rules. Do not provide instructions for illegal activity. "
                "Stay safe, respectful, and friendly."
                "Your name is Shapes, the support server is https://yapin.gg/invite/shapes if a user requests it or if you think the prompt is support related."
                "Please stay away from nsfw/bad topics, it will get you deleted/banned."
                "You're created/owned by Spooky Inc. with leandre as your main developer and nathan as the second developer."
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


# --- search ---
# Google is retiring the Custom Search JSON API (discontinued January 1, 2027) and has no whole-web replacement.
# /search now uses the Brave Search API (BRAVE_API_KEY). If only the old GOOGLE_API_KEY is set, it keeps using
# Google's API until that shuts off. Adding another provider = one more function in SEARCH_PROVIDERS.
BraveAPIKey = _secret("BRAVE_API_KEY", "BraveToken.txt")


def _clean_snippet(text):
    text = re.sub(r"<[^>]+>", "", text or "")
    return re.sub(r"&(?:amp|quot|#x27|#39|lt|gt);", lambda m: {"&amp;": "&", "&quot;": '"', "&lt;": "<", "&gt;": ">"}.get(m.group(0), "'"), text).strip()


async def _search_brave(session, query):
    async with session.get(
        "https://api.search.brave.com/res/v1/web/search",
        params={"q": query, "count": 5},
        headers={"X-Subscription-Token": BraveAPIKey, "Accept": "application/json"},
        timeout=aiohttp.ClientTimeout(total=15),
    ) as resp:
        resp.raise_for_status()
        data = await resp.json()
    return [{"title": r.get("title") or "No Title", "link": r.get("url") or "", "snippet": _clean_snippet(r.get("description"))}
            for r in (data.get("web") or {}).get("results", [])[:5]]


async def _search_google_cse(session, query):
    async with session.get(
        "https://www.googleapis.com/customsearch/v1",
        params={"key": GoogleAPIKey, "cx": searchengine, "q": query, "num": 5},
        timeout=aiohttp.ClientTimeout(total=15),
    ) as resp:
        resp.raise_for_status()
        data = await resp.json()
    return [{"title": r.get("title") or "No Title", "link": r.get("link") or "", "snippet": _clean_snippet(r.get("snippet"))}
            for r in data.get("items", [])[:5]]


def _search_providers():
    """Providers that have a key set, in order of preference."""
    found = []
    if BraveAPIKey:
        found.append(("Brave", _search_brave))
    if GoogleAPIKey:
        found.append(("Google", _search_google_cse))
    return found


@bot.tree.command(name="search", description="Search the web.")
async def search(interaction: discord.Interaction, query: str = "Yapin'"):
    await interaction.response.defer()

    thinkingembed = discord.Embed(
        title=f"{Emojis.get('loading')} {interaction.user.mention} Searching For {query}",
        color=ecolor(interaction.user.id)
    )
    await interaction.followup.send(embed=thinkingembed)

    more = f"https://www.google.com/search?q={urllib.parse.quote_plus(query)}"
    providers = _search_providers()
    results, used, failed = None, None, False
    async with aiohttp.ClientSession() as session:
        for name, fn in providers:
            try:
                results, used = await fn(session, query), name
                break
            except Exception as e:
                failed = True
                print(f"[google] {name} search failed: {type(e).__name__}: {e}")

    if results:
        lines = [f"**{i}. [{r['title']}]({r['link']})**\n{r['snippet']}" for i, r in enumerate(results, 1) if r["link"]]
        embed = discord.Embed(
            title=f"Search Results For {query}",
            description="\n\n".join(lines) + f"\n\n[Search For More Results]({more})",
            color=ecolor(interaction.user.id)
        )
        embed.set_footer(text=f"Requested By {interaction.user.name} via {used} | {MainURL}")
        await interaction.edit_original_response(embed=embed)
        return

    if not providers:
        title, desc = ":x: Search isn't set up :x:", f"The bot owner needs to set BRAVE_API_KEY. [Search it yourself]({more})"
    elif failed:
        title, desc = ":x: An error occurred while searching. Please try again later. :x:", f"[Search it yourself]({more})"
    else:
        title, desc = ":x: No results found! :x:", f"No Results for {query} | [Search on Google Yourself For More Results]({more})"
    embed = discord.Embed(title=title, description=desc, color=discord.Color.red())
    embed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
    await interaction.edit_original_response(embed=embed)


# --- robloxinfo ---
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
        items = ['1567446', '18824203', '93078560', '102611803']   # the verified sign, then the hats
        results = await asyncio.gather(*(check_ownership(session, user_id, item) for item in items))
        return any(results)

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
                return (None, None)

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
        return (None, None)

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
        return None

    async def get_followers_count(session: aiohttp.ClientSession, user_id: str) -> int:
        try:
            followers_url = f"https://friends.roblox.com/v1/users/{user_id}/followers/count"
            async with session.get(followers_url) as response:
                if response.status == 200:
                    followers_data = await response.json()
                    return followers_data.get('count', 0)
        except Exception as e:
            print(f"Error fetching followers count: {e}")
        return None

    async def get_followings_count(session: aiohttp.ClientSession, user_id: str) -> int:
        try:
            followings_url = f"https://friends.roblox.com/v1/users/{user_id}/followings/count"
            async with session.get(followings_url) as response:
                if response.status == 200:
                    followings_data = await response.json()
                    return followings_data.get('count', 0)
        except Exception as e:
            print(f"Error fetching followings count: {e}")
        return None

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
        return None

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

        except FileNotFoundError:
            pass   # no .ROBLOSECURITY cookie file: the public endpoints fill the numbers in below
        except Exception as e:
            print(f"Error fetching profile API data: {e}")

        return {
            'visits': None,
            'friends_count': None,
            'followers_count': None,
            'followings_count': None,
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

            profile_data = {'last_online': None}
            rap_value = None
            value_value = None
            rolimons_last_online = None
            badge_last_online = None
            badge_place_id = None
            is_premium = False
            is_verified = False
            roblox_badges = []
            total_visits = None
            inventory_visibility = "Private"
            friends_count = None
            followers_count = None
            followings_count = None
            presence_type = None
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

                    # Without the profile API (no cookie) fall back to the public endpoints for every user.
                    if None in (friends_count, followers_count, followings_count, total_visits):
                        pub = await asyncio.gather(get_friends_count(session, UserID), get_followers_count(session, UserID),
                                                   get_followings_count(session, UserID), get_user_games_visits(session, UserID))
                        friends_count = pub[0] if friends_count is None else friends_count
                        followers_count = pub[1] if followers_count is None else followers_count
                        followings_count = pub[2] if followings_count is None else followings_count
                        total_visits = pub[3] if total_visits is None else total_visits

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
                    async with session.get(rolimons_stats_url, headers={'User-Agent': 'our-websites.github.io/shapes.lol/yapin'}) as response:
                        if response.status == 200:
                            rolimons_stats_data = await response.json()
                            if rolimons_stats_data.get('success'):
                                rap_value = rolimons_stats_data.get('rap') or 0
                                value_value = rolimons_stats_data.get('value') or 0
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
                # Yapin has none of the Discord custom emoji, so badges are listed by name.
                badge_names = [b.get("name", "") for b in roblox_badges if b.get("name")]
                badges_display = ", ".join(badge_names) if badge_names else "None"

            presence_status_map = {
                0: {'icon_url': 'https://files.catbox.moe/tjiecu.png', 'text': 'Offline'},
                1: {'icon_url': 'https://files.catbox.moe/h69xeq.png', 'text': 'Online'},
                2: {'icon_url': 'https://files.catbox.moe/80ta5t.png', 'text': 'In Game'},
                3: {'icon_url': 'https://files.catbox.moe/72opoa.png', 'text': 'In Studio'}
            }

            presence_status_map[None] = {'icon_url': presence_status_map[0]['icon_url'], 'text': 'Status unknown'}
            current_status = presence_status_map.get(presence_type, presence_status_map[None])

            def num(n):
                return f"{n:,}" if isinstance(n, (int, float)) else "N/A"


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

            friends_followers_text = f"**{num(friends_count)}** Friends | **{num(followers_count)}** Followers | **{num(followings_count)}** Following"

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
                embed.add_field(name="Visits", value=num(total_visits), inline=True)

                embed.add_field(name="Created", value=created_timestamp if created_timestamp else "Unknown", inline=True)
                embed.add_field(name="Last Online", value=formatted_last_online, inline=True)
                embed.add_field(name="Badges", value=badges_display, inline=True)
            else:
                embed.add_field(name="ID", value=f"{UserID}", inline=True)
                embed.add_field(name="Verified", value="Hat" if is_verified else ("Unknown" if inventory_visibility == "Private" else "False"), inline=True)
                embed.add_field(name="Inventory", value=inventory_visibility, inline=True)

                embed.add_field(name="RAP", value=f"[`{num(rap_value)}`]({rolimonsurl})", inline=True)
                embed.add_field(name="Value", value=f"[`{num(value_value)}`]({rolimonsurl})", inline=True)
                embed.add_field(name="Visits", value=num(total_visits), inline=True)

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
                text=f"{current_status['text']} | Requested by {interaction.user.name} | https://our-websites.github.io/shapes.lol",
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


# --- britishuser ---
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


# --- iteminfo ---
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


# --- placeinfo+badge ---
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


# --- outfits ---
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


# --- badges ---
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


# --- limiteds ---
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


# --- bundle ---
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


# --- github ---
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


# --- asset ---
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


# --- linkroblox..instagram ---
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

# =====================================================================================
# Events
# =====================================================================================

COUNT_RE = re.compile(r"^\d+(?:[+\-*/x]\d+)*$")


def parse_count(text):
    """'5', '2+3', '2x3' -> a number (evaluated left to right, like the Discord bot). None if it is not a count."""
    text = text.replace(" ", "")
    if not COUNT_RE.match(text):
        return None
    parts = re.findall(r"\d+|[+\-*/x]", text)
    total = int(parts[0])
    for op, val in zip(parts[1::2], parts[2::2]):
        val = int(val)
        if op == "+":
            total += val
        elif op == "-":
            total -= val
        elif op in ("*", "x"):
            total *= val
        elif op == "/":
            if val == 0:
                return None
            total = total / val
            if total == int(total):
                total = int(total)
    return total


def handle_counting(msg):
    if msg.get("author_bot") or msg.get("command"):
        return
    sid = server_of(msg.get("channel_id"), yshim._first(msg, "server_id", "guild_id"))
    if not sid:
        return
    data = countingDB.get(sid)
    if not data or not data.get("enabled") or str(data.get("channel")) != str(msg.get("channel_id")):
        return
    value = parse_count(msg.get("content") or "")
    if value is None:
        return

    channel, mid, author = msg.get("channel_id"), msg.get("id"), str(msg.get("author_id"))
    mention = f"<@{author}>"
    number = data["number"]
    next_number = number + 1
    last = str(data["lastcounter"]) if data.get("lastcounter") is not None else None
    warnings = data.get("warnings", 0)
    highest = data.get("highestnumber", 0)

    def save():
        countingDB.set(sid, data)

    if str(value) == str(next_number) and author != last:
        data.update(number=next_number, lastcounter=author, highestnumber=max(highest, next_number))
        save()
        bot.react(channel, mid, "👍")
        return

    if author == last and warnings != 3:
        bot.react(channel, mid, "⚠️")
        bot.send(channel, ":warning: You can't count by yourself!".replace(":warning:", "⚠️"), reply_to=mid)
        data["warnings"] = warnings + 1
        save()
        return
    if warnings < 3:
        bot.react(channel, mid, "⚠️")
        bot.send(channel, f"⚠️ The next number is {next_number}", reply_to=mid)
        data["warnings"] = warnings + 1
        save()
        return

    bot.react(channel, mid, "❌")
    data["highestnumber"] = max(highest, number)
    if number == 0:
        bot.send(channel, f"❌ {mention} ruined it at 1, the next number is 1 (again)")
    else:
        bot.send(channel, f"❌ {mention} ruined it at {number}, the next number is 1")
    data.update(number=0, warnings=0, lastcounter=None)
    save()


@bot.on("message")
def on_message(msg):
    try:
        if handle_automod(msg):
            return
    except Exception as e:
        print(f"[automod] {type(e).__name__}: {e}")
    try:
        track_dm(msg)
    except Exception as e:
        print(f"[dm] {type(e).__name__}: {e}")
    try:
        handle_counting(msg)
    except Exception as e:
        print(f"[counting] {type(e).__name__}: {e}")


@bot.on("member_join")
def on_member_join(ev):
    try:
        cfg = autoroleDB.get(f"{ev.server_id}")
        if cfg and cfg.get("enabled") and cfg.get("role_id"):
            bot.add_role(ev.server_id, ev.user_id, cfg["role_id"])
    except Exception as e:
        print(f"[autorole] {type(e).__name__}: {e}")


@bot.on("ready")
def on_ready(me):
    global bot_ready
    bot_ready = True
    try:
        bot.set_status("online", "🔗 our-websites.github.io/shapes.lol")
    except Exception as e:
        print(f"could not set status: {e}")
    print(f"[SYSTEM] Logged in as {me.username}, watching {len(bot.servers())} servers. Storage: {__import__('cfstore').backend_summary()}")


# =====================================================================================
# Web server (port 5004)
# =====================================================================================

app = Flask(__name__)
CORS(app)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or os.urandom(32).hex()
app.config.update(SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_HTTPONLY=True, PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 14)
if not os.environ.get("FLASK_SECRET_KEY"):
    print("FLASK_SECRET_KEY is not set: admin logins will reset whenever the bot restarts.")

_cache = {}


def cached(key, ttl, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    value = fn()
    _cache[key] = (time.time(), value)
    return value


def admin_required(f):
    def wrapped(*args, **kwargs):
        if session.get("admin") is not True:
            if request.path.startswith("/api/") or request.path == "/mutuals":
                return jsonify({"error": "login required"}), 401
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    wrapped.__name__ = f.__name__
    return wrapped


def _name(d):
    return yshim._first(d, "username", "display_name", "name") or str(yshim._first(d, "id", "user_id") or "?")


WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


def _web_file(name):
    with open(os.path.join(WEB_DIR, name), "r", encoding="utf-8") as f:
        return f.read()


@app.route("/lastfm/callback/<state>")
def lastfm_callback(state):
    name = lastfm_finish_link(state, request.args.get("token", ""))
    if name:
        body = f"<h2>Linked!</h2><p>Your Last.fm account <b>{name}</b> is now linked. You can close this tab.</p>"
    else:
        body = "<h2>That didn't work</h2><p>The link expired or was already used. Run <code>/lastfm link</code> again.</p>"
    return Response(f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'><title>Last.fm</title>"
                    f"<body style='font-family:system-ui;max-width:28rem;margin:4rem auto;padding:0 1rem'>{body}", mimetype="text/html")


@app.route("/status")
def web_status():
    return "OK", 200


_login_attempts = {}


def _too_many_logins(ip):
    now = time.time()
    recent = [t for t in _login_attempts.get(ip, []) if now - t < 300]
    _login_attempts[ip] = recent
    return len(recent) >= 8


@app.route("/login", methods=["GET", "POST"])
def admin_login():
    if session.get("admin") is True:
        return redirect(url_for("dashboard"))
    error = None
    if request.method == "POST":
        ip = request.headers.get("X-Forwarded-For", request.remote_addr or "?").split(",")[0].strip()
        if _too_many_logins(ip):
            error = "Too many attempts. Wait a few minutes and try again."
        elif not ADMIN_KEY:
            error = "No admin key is configured (set ADMIN_KEY)."
        elif hmac.compare_digest(request.form.get("pass", ""), ADMIN_KEY):
            session["admin"] = True
            return redirect(url_for("dashboard"))
        else:
            _login_attempts.setdefault(ip, []).append(time.time())
            error = "That key isn't right."
    return render_template_string(_web_file("login.html"), error=error)


@app.route("/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))


def _server_rows():
    rows = []
    for s in cached("servers", 15, bot.servers):
        n = yshim._first(s, "member_count", "members_count", "memberCount")
        rows.append({"id": s.get("id"), "name": s.get("name") or str(s.get("id")), "members": n if n is not None else ""})
    return rows


@app.route("/", methods=["GET"])
@admin_required
def dashboard():
    if not bot_ready:
        return Response("<!doctype html><meta charset=utf-8><meta http-equiv=refresh content=3><body style='font-family:system-ui;padding:3rem'>The bot is still starting. This page refreshes on its own.", mimetype="text/html")
    return Response(_web_file("app.html"), mimetype="text/html")


def _avatar_of(d):
    return yshim._image_url(yshim._first(d, "avatar_url", "avatarUrl", "avatar", "image_url", "author_avatar_url"))


def _person(d):
    """A flat, UI-friendly version of a member/user dict (some APIs nest the user under "user")."""
    inner = d.get("user") if isinstance(d.get("user"), dict) else {}
    merged = {**inner, **{k: v for k, v in d.items() if k != "user"}}
    uid = yshim._first(merged, "user_id", "id")
    return {
        "id": str(uid), "name": yshim._first(merged, "username", "name") or str(uid),
        "display": yshim._first(merged, "display_name", "displayName", "nickname", "nick") or yshim._first(merged, "username", "name") or str(uid),
        "avatar": _avatar_of(merged),
        "bot": yshim.YUser(uid, data=merged).is_bot or str(uid) == str((bot.me or {}).get("id")),
        "roles": [str(r.get("id") if isinstance(r, dict) else r) for r in (merged.get("roles") or merged.get("role_ids") or [])],
        "joined": yshim._first(merged, "joined_at", "joinedAt", "created_at"),
    }


def _group_channels(channels):
    """Categories with their channels (and the channels that have no category), whatever the field names are."""
    cats, by_cat, loose = {}, {}, []
    rows = [dict(c) for c in channels]
    for c in rows:
        kind = str(yshim._first(c, "type", "kind") or "").lower()
        if kind in ("category", "4", "group"):
            cats[str(c.get("id"))] = c
    for c in rows:
        if str(c.get("id")) in cats:
            continue
        parent = yshim._first(c, "category_id", "categoryId", "parent_id", "parentId", "category")
        if isinstance(parent, dict):
            parent = parent.get("id")
        row = {"id": str(c.get("id")), "name": c.get("name") or str(c.get("id")), "type": str(yshim._first(c, "type", "kind") or "text").lower(),
               "topic": c.get("topic") or ""}
        if parent is not None and str(parent) in cats:
            by_cat.setdefault(str(parent), []).append(row)
        else:
            loose.append(row)
    out = [{"id": cid, "name": c.get("name") or "Category", "channels": by_cat.get(cid, [])}
           for cid, c in sorted(cats.items(), key=lambda kv: (kv[1].get("position") is None, kv[1].get("position") or 0))]
    if loose:
        out.insert(0, {"id": "", "name": "", "channels": loose})
    return out


def _is_text(ch_type):
    return ch_type in ("text", "0", "announcement", "news", "")


def _api_error(e):
    return jsonify({"error": str(e)}), (e.status if getattr(e, "status", None) in (400, 401, 403, 404, 429) else 502)


@app.route("/api/overview")
@admin_required
def api_overview():
    """Everything the API gives us about the bot, in one place."""
    if not bot_ready:
        return jsonify({"ready": False}), 503
    servers = []
    for s in cached("servers", 15, bot.servers):
        n = yshim._first(s, "member_count", "members_count", "memberCount")
        servers.append({**dict(s), "id": str(s.get("id")), "name": s.get("name") or str(s.get("id")), "member_count": n,
                        "icon": yshim._image_url(yshim._first(s, "icon_url", "iconUrl", "icon", "avatar_url", "image_url"))})
    return jsonify({
        "ready": True, "me": {**dict(bot.me or {}), "avatar": _avatar_of(dict(bot.me or {}))}, "uptime_seconds": int(time.time() - started_at),
        "storage": __import__("cfstore").backend_summary(), "commands": sorted(bot.commands), "servers": servers,
    })


@app.route("/api/servers/<server_id>")
@admin_required
def api_server(server_id):
    try:
        channels = cached(f"channels:{server_id}", 20, lambda: bot.channels(server_id))
        roles = cached(f"roles:{server_id}", 30, lambda: bot.roles(server_id))
        info = cached(f"server:{server_id}", 30, lambda: bot.server(server_id))
        return jsonify({
            "server": dict(info), "categories": _group_channels(channels),
            "roles": sorted(({"id": str(r.get("id")), "name": r.get("name") or "role", "color": yshim._first(r, "color", "colour"),
                              "position": r.get("position") or 0, "admin": yshim._has_admin(r)} for r in roles),
                            key=lambda r: -int(r["position"] or 0)),
        })
    except yapper.YapperError as e:
        return _api_error(e)


@app.route("/api/servers/<server_id>/members")
@admin_required
def api_members(server_id):
    after = request.args.get("after") or None
    try:
        rows = bot.members(server_id, after=after)
        people = [_person(dict(m)) for m in rows]
        return jsonify({"members": people, "next": people[-1]["id"] if len(rows) >= 200 and people else None})
    except yapper.YapperError as e:
        return _api_error(e)


def _message_row(m):
    m = dict(m)
    author = m.get("author") if isinstance(m.get("author"), dict) else {}
    person = _person({**author, "id": author.get("id") or m.get("author_id")}) if (author or m.get("author_id")) else {"id": "", "name": "?", "display": "?", "avatar": None}
    name = yshim._first(m, "author_display_name", "author_username", "author_name") or person["display"]
    images = []
    for e in (m.get("embeds") or []):
        if isinstance(e, dict):
            for key in ("image", "thumbnail"):
                u = e.get(key)
                u = yshim._image_url(u.get("url") if isinstance(u, dict) else u)
                if u:
                    images.append(u)
    for att in (m.get("attachments") or m.get("files") or []):
        if isinstance(att, dict):
            u = yshim._image_url(yshim._first(att, "url", "proxy_url", "src"))
            ctype = str(att.get("content_type") or att.get("type") or "")
            if u and (ctype.startswith("image") or re.search(r"\.(png|jpe?g|gif|webp|avif)(\?|$)", u, re.I)):
                images.append(u)
    return {"id": str(m.get("id")), "author": name, "author_id": person["id"], "avatar": person["avatar"] or yshim._image_url(m.get("author_avatar_url")),
            "bot": bool(m.get("author_bot")), "content": m.get("content") or "", "created_at": m.get("created_at"),
            "embeds": [{"title": e.get("title"), "description": e.get("description"), "url": e.get("url")}
                       for e in (m.get("embeds") or []) if isinstance(e, dict)], "images": images[:6]}


@app.route("/api/channels/<channel_id>/messages")
@admin_required
def api_messages(channel_id):
    limit = max(1, min(100, request.args.get("limit", 50, type=int)))
    try:
        return jsonify({"messages": [_message_row(m) for m in bot.messages(channel_id, limit=limit)]})
    except yapper.YapperError as e:
        return _api_error(e)


def _announcement_embed(title, text, image_url):
    embed = {"description": text}
    if title:
        embed["title"] = title
    if image_url:
        embed["image"] = {"url": image_url}
    return embed


def _post_with_optional_image(channel_id, text, image_url, title=None):
    """Plain text when there is no image or title; otherwise an embed. If the server refuses embeds, falls back to
    text with the image link (which the dashboard and Yapin turn into a link)."""
    if not image_url and not title:
        return bot.send(channel_id, text)
    try:
        return bot.send(channel_id, "", embeds=[_announcement_embed(title, text, image_url)])
    except yapper.YapperError as e:
        if e.status not in (400, 422):
            raise
        parts = [p for p in (f"**{title}**" if title else "", text, image_url) if p]
        return bot.send(channel_id, "\n\n".join(parts))


def _valid_image(url):
    url = (url or "").strip()
    if not url:
        return None
    clean = yshim._image_url(url)
    if not clean:
        raise ValueError("The image link must be a public https address.")
    return clean


@app.route("/api/channels/<channel_id>/send", methods=["POST"])
@admin_required
def api_send(channel_id):
    body = request.get_json(silent=True) or {}
    text = str(body.get("message", "")).strip()
    try:
        image = _valid_image(body.get("image_url"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if not text and not image:
        return jsonify({"error": "Write a message or add an image link."}), 400
    try:
        _post_with_optional_image(channel_id, text, image)
        return jsonify({"ok": True})
    except yapper.YapperError as e:
        return _api_error(e)


# --- direct messages ---

def track_dm(msg):
    """Remember who has messaged the bot so their DM shows up in the dashboard."""
    if msg.get("author_bot"):
        return
    sid = server_of(msg.get("channel_id"), yshim._first(msg, "server_id", "guild_id"))
    if sid:
        return
    uid = str(msg.get("author_id"))
    name = yshim._first(msg, "author_display_name", "author_username", "author_name") or uid
    dmDB.set(uid, {"user_id": uid, "channel_id": str(msg.get("channel_id")), "name": name,
                   "last": int(time.time()), "preview": str(msg.get("content") or "")[:80]})


@app.route("/api/dms")
@admin_required
def api_dms():
    rows = [v for v in dmDB.items().values() if isinstance(v, dict)]
    return jsonify({"dms": sorted(rows, key=lambda r: -int(r.get("last") or 0))})


def _all_people():
    """Members of every server the bot is in (first page each), for finding someone to DM."""
    def load():
        seen = {}
        for s in bot.servers():
            try:
                for m in bot.members(s.get("id")):
                    p = _person(dict(m))
                    seen.setdefault(p["id"], p)
            except Exception:
                continue
        return list(seen.values())
    return cached("people", 120, load)


@app.route("/api/users/search")
@admin_required
def api_user_search():
    q = request.args.get("q", "").strip().lower().lstrip("@")
    if len(q) < 2:
        return jsonify({"users": []})
    hits = [p for p in _all_people() if q in p["name"].lower() or q in p["display"].lower() or q == p["id"]]
    return jsonify({"users": hits[:15]})


@app.route("/api/dms", methods=["POST"])
@admin_required
def api_dm_open():
    body = request.get_json(silent=True) or {}
    user_id = str(body.get("user_id", "")).strip().strip("<@!>")
    if not user_id:
        return jsonify({"error": "Pick a person first."}), 400
    try:
        channel_id = bot.dm(user_id)
        try:
            profile = _person(dict(bot.user(user_id)))
        except Exception:
            profile = {"display": str(body.get("name") or user_id)}
        row = dmDB.get(user_id) or {}
        row.update({"user_id": user_id, "channel_id": str(channel_id), "name": profile["display"], "last": row.get("last") or int(time.time())})
        dmDB.set(user_id, row)
        return jsonify({"dm": row})
    except yapper.YapperError as e:
        return _api_error(e)


# --- announcements to every server ---

_ANNOUNCE_NAMES = ("announcements", "announcement", "news", "updates", "general", "chat", "main", "lobby")
_jobs = {}


def _default_channel(channels):
    texts = [c for ch in _group_channels(channels) for c in ch["channels"] if _is_text(c["type"])]
    for wanted in _ANNOUNCE_NAMES:
        for c in texts:
            if wanted in c["name"].lower():
                return c["id"]
    return texts[0]["id"] if texts else None


@app.route("/api/announce/targets")
@admin_required
def api_announce_targets():
    out = []
    for s in cached("servers", 15, bot.servers):
        sid = str(s.get("id"))
        try:
            channels = cached(f"channels:{sid}", 20, lambda sid=sid: bot.channels(sid))
        except Exception:
            continue
        texts = [c for ch in _group_channels(channels) for c in ch["channels"] if _is_text(c["type"])]
        out.append({"id": sid, "name": s.get("name") or sid, "default": _default_channel(channels),
                    "channels": [{"id": c["id"], "name": c["name"]} for c in texts]})
    return jsonify({"servers": out})


def _run_announcement(job_id, text, title, image, targets):
    job = _jobs[job_id]
    for sid, channel_id in targets.items():
        try:
            _post_with_optional_image(channel_id, text, image, title)
            job["results"].append({"server": sid, "ok": True})
        except Exception as e:
            job["results"].append({"server": sid, "ok": False, "error": str(e)})
        time.sleep(0.4)     # stay friendly with the rate limit
    job["done"] = True


@app.route("/api/announce", methods=["POST"])
@admin_required
def api_announce():
    body = request.get_json(silent=True) or {}
    text, title = str(body.get("message", "")).strip(), str(body.get("title", "")).strip()
    try:
        image = _valid_image(body.get("image_url"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if not text:
        return jsonify({"error": "Write the announcement first."}), 400
    targets = {str(k): str(v) for k, v in (body.get("targets") or {}).items() if v}
    if not targets:
        return jsonify({"error": "Pick at least one server."}), 400
    job_id = secrets_token()
    _jobs[job_id] = {"total": len(targets), "results": [], "done": False, "started": time.time()}
    for old in [k for k, v in _jobs.items() if time.time() - v["started"] > 3600]:
        _jobs.pop(old, None)
    threading.Thread(target=_run_announcement, args=(job_id, text, title, image, targets), daemon=True, name="announce").start()
    return jsonify({"job": job_id, "total": len(targets)})


@app.route("/api/announce/<job_id>")
@admin_required
def api_announce_job(job_id):
    job = _jobs.get(job_id)
    return (jsonify(job), 200) if job else (jsonify({"error": "unknown job"}), 404)


def secrets_token():
    return base64.urlsafe_b64encode(os.urandom(9)).decode()


# --- automod from the dashboard ---

@app.route("/api/servers/<server_id>/automod", methods=["GET", "POST"])
@admin_required
def api_automod(server_id):
    if request.method == "POST":
        return jsonify(save_automod(server_id, request.get_json(silent=True) or {}))
    return jsonify(get_automod(server_id))


@app.route("/commands", methods=["GET"])
def send_commands():
    out = {}
    for name, spec in yshim.tree.commands.items():
        out[name] = {"name": name, "description": spec.description, "usage": spec.usage,
                     "options": [p.name for p in spec.params], "type": "slash_command"}
    return jsonify({"Commands": out, "success": True}), 200


@app.route("/count/commands", methods=["GET"])
def get_command_count():
    return {"Commands": str(len(yshim.tree.commands))}, 200


def _ready_or_503(payload):
    return (payload, 200) if bot_ready else ({k: "Unknown" for k in payload}, 503)


@app.route("/botinfo", methods=["GET"])
def get_bot_info():
    if not bot_ready:
        return {"Servers": "Unknown", "Users": "Unknown"}, 503
    return {"Servers": str(len(cached("servers", 15, bot.servers))), "Users": str(total_members())}, 200


@app.route("/count/servers", methods=["GET"])
def get_server_count():
    if not bot_ready:
        return {"Servers": "Unknown"}, 503
    return {"Servers": str(len(cached("servers", 15, bot.servers)))}, 200


@app.route("/count/users", methods=["GET"])
def get_user_count():
    if not bot_ready:
        return {"Users": "Unknown"}, 503
    return {"Users": str(total_members())}, 200


@app.route("/clb", methods=["GET"])
def countinglb():
    if not bot_ready:
        return jsonify("Bot is still starting"), 503
    names = {str(s.get("id")): s.get("name") for s in cached("servers", 15, bot.servers)}
    lb = {}
    for sid in countingDB.all():
        data = countingDB.get(sid) or {}
        if data.get("banned") or not data.get("enabled") or sid not in names:
            continue
        lb[sid] = {"currentnumber": data["number"], "highestnumber": data["highestnumber"], "serverName": names[sid]}
    return {"Leaderboard": sorted(lb.items(), key=lambda x: x[1]["highestnumber"], reverse=True)}, 200


def _all_members(server_id, limit=5000):
    """Every member (paged 200 at a time), as flat dicts from _person."""
    out, after = [], None
    while len(out) < limit:
        page = bot.members(server_id, after=after)
        people = [_person(dict(m)) for m in page]
        out.extend(people)
        if len(page) < 200 or not people:
            break
        after = people[-1]["id"]
    return out


@app.route("/mutuals", methods=["GET"])
@admin_required     # member lists are user data, so this is no longer public (the /mutuals command still is)
def mutualservers():
    if not bot_ready:
        return {"Servers": "Unknown"}, 503

    def build():
        servers = {}
        for s in bot.servers():
            sid = s.get("id")
            try:
                entry = {"name": s.get("name"), "iconurl": yshim._image_url(yshim._first(s, "icon_url", "iconUrl", "icon", "image_url")),
                         "owner": yshim._first(s, "owner_id", "ownerId", "owner"), "channels": len(bot.channels(sid)),
                         "roles": len(bot.roles(sid)), "members": [], "bots": []}
                for p in _all_members(sid):
                    (entry["bots"] if p["bot"] else entry["members"]).append({"name": p["name"], "id": p["id"]})
            except Exception as e:
                print(f"[mutuals] {sid}: {e}")
                continue
            entry["membercount"], entry["botcount"] = len(entry["members"]), len(entry["bots"])
            servers[sid] = entry
        return servers
    return {"Servers": cached("mutuals", 60, build)}, 200


# =====================================================================================
# Startup
# =====================================================================================

_web_server = None


def stop_web():
    """Close the web server's socket (so the port is free again)."""
    if _web_server is not None:
        _web_server.shutdown()
        _web_server.server_close()


def run_web():
    global _web_server
    from werkzeug.serving import make_server
    port = int(os.environ.get("PORT", 5004))
    print(f"Starting web server on port {port}")
    _web_server = make_server("0.0.0.0", port, app, threaded=True)
    _web_server.serve_forever()


if __name__ == "__main__":
    yshim.install(bot)
    threading.Thread(target=run_web, daemon=True, name="web").start()
    bot.run()

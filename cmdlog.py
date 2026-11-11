"""Command logs for Discord: /cmdlog (a server logs every command used there) and /botlog (bot owners: every command in every
server, plus servers added and removed).

Logs post through a webhook. A server's log and the global log that point at the same channel share one webhook, and a
webhook the bot already made in a channel is reused instead of making another. If Discord refuses (the bot lacks Manage
Webhooks there), the log falls back to the bot posting directly and says what permission is missing.
"""
import asyncio
import json
import os
import threading
import time

import discord
from discord import app_commands

BOTLOG_KEY = "botlog"
RETRY_WEBHOOK_AFTER = 3600           # after Discord refuses a webhook in a channel, try again this often
HOOK_NAME = "Shapes logs"


class JsonState:
    """A tiny dict kept in a JSON file. Plain files, so it doesn't depend on which pickleDB release is installed."""

    def __init__(self, path):
        self.path, self._lock = path, threading.Lock()
        try:
            with open(path, "r", encoding="utf-8") as f:
                self._data = json.load(f)
        except (OSError, ValueError):
            self._data = {}

    def get(self, key, default=None):
        with self._lock:
            return json.loads(json.dumps(self._data.get(str(key), default)))

    def set(self, key, value):
        with self._lock:
            self._data[str(key)] = value
            tmp = f"{self.path}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f)
            os.replace(tmp, self.path)


def init(m):
    bot = m.bot
    cfgDB = JsonState("cmdlog.json")                 # guild id (or "botlog") -> {"channel", "enabled", "events", "commands", "retry_at", "refused"}
    hooks = {}                                       # channel id -> discord.Webhook, shared by every log posting there
    lock = asyncio.Lock()

    async def hook_for(channel):
        """A webhook the bot can use in this channel: one it already made (shared by the logs), else a new one."""
        if channel.id in hooks:
            return hooks[channel.id]
        mine = [w for w in await channel.webhooks() if w.token and (w.user == bot.user or w.application_id == bot.user.id)]
        mine.sort(key=lambda w: not (w.name or "").startswith("Shapes"))
        hook = mine[0] if mine else await channel.create_webhook(name=HOOK_NAME, reason="Command log")
        hooks[channel.id] = hook
        return hook

    async def post(key, embed):
        """Post one embed to a log's channel. Returns None, or a plain sentence about what the bot isn't allowed to do."""
        cfg = cfgDB.get(key)
        if not cfg or not cfg.get("enabled") or not cfg.get("channel"):
            return None
        channel = bot.get_channel(int(cfg["channel"]))
        if channel is None:
            return None
        async with lock:
            if not (cfg.get("refused") and time.time() < float(cfg.get("retry_at") or 0)):
                try:
                    hook = await hook_for(channel)
                    await hook.send(embed=embed, username=bot.user.name, avatar_url=bot.user.display_avatar.url)
                    if cfg.pop("refused", None):
                        cfgDB.set(key, cfg)
                    return None
                except discord.NotFound:                               # the webhook was deleted: forget it and try a fresh one once
                    hooks.pop(channel.id, None)
                    try:
                        hook = await hook_for(channel)
                        await hook.send(embed=embed, username=bot.user.name, avatar_url=bot.user.display_avatar.url)
                        return None
                    except discord.HTTPException:
                        pass
                except discord.Forbidden:
                    hooks.pop(channel.id, None)
                    cfg.update(refused=True, retry_at=time.time() + RETRY_WEBHOOK_AFTER)
                    cfgDB.set(key, cfg)
                except discord.HTTPException as e:
                    print(f"[cmdlog] webhook in {channel.id}: {e}")
            try:                                                       # the webhook isn't available: post as the bot
                await channel.send(embed=embed)
                return "I can't use webhooks in that channel, so I'm posting as the bot. Give me **Manage Webhooks** there to use one."
            except discord.HTTPException as e:
                print(f"[cmdlog] couldn't post in {channel.id}: {e}")
                return "I can't post in that channel. I need **View Channel**, **Send Messages** and **Embed Links** there."

    def command_embed(interaction, command):
        args = " ".join(f"{k}:{v}" for k, v in (interaction.namespace.__dict__.items() if interaction.namespace else []))[:200]
        embed = discord.Embed(title=f"/{command.qualified_name}", color=discord.Color.blurple(), timestamp=discord.utils.utcnow())
        embed.add_field(name="User", value=f"{interaction.user.mention} (@{interaction.user.name})", inline=True)
        if interaction.channel_id:
            embed.add_field(name="Channel", value=f"<#{interaction.channel_id}>", inline=True)
        if args:
            embed.add_field(name="Arguments", value="```\n" + args.replace("```", "'''") + "\n```", inline=False)
        if interaction.guild:
            embed.set_footer(text=f"{interaction.guild.name} ({interaction.guild.id})")
        return embed

    async def on_command(interaction, command):
        try:
            if interaction.guild_id:
                await post(str(interaction.guild_id), command_embed(interaction, command))
            g = cfgDB.get(BOTLOG_KEY)
            if g and g.get("commands", True):
                await post(BOTLOG_KEY, command_embed(interaction, command))
        except Exception as e:
            print(f"[cmdlog] {e}")
    bot.add_listener(on_command, "on_app_command_completion")

    async def server_event(kind, guild):
        g = cfgDB.get(BOTLOG_KEY)
        if not g or not g.get("events", True):
            return
        humans = sum(1 for mem in guild.members if not mem.bot) if guild.chunked else None
        embed = discord.Embed(title=("➕ Shapes was added to " if kind == "add" else "➖ Shapes was removed from ") + guild.name,
                              color=discord.Color.green() if kind == "add" else discord.Color.red(), timestamp=discord.utils.utcnow())
        embed.add_field(name="Server", value=f"{guild.name}\n`{guild.id}`", inline=True)
        embed.add_field(name="Members", value=f"{humans:,}" if humans is not None else f"{guild.member_count:,} (with bots)", inline=True)
        embed.add_field(name="Servers now", value=str(len(bot.guilds)), inline=True)
        embed.set_footer(text="Shapes server log")
        await post(BOTLOG_KEY, embed)

    async def on_guild_join(guild):
        await server_event("add", guild)

    async def on_guild_remove(guild):
        await server_event("remove", guild)
    bot.add_listener(on_guild_join, "on_guild_join")
    bot.add_listener(on_guild_remove, "on_guild_remove")

    def public_warning(channel):
        """A log shows who ran what, so say so when everyone can read the channel."""
        everyone = channel.guild.default_role
        return ("\n⚠️ Everyone in the server can see that channel, so they'll see who runs which command. Pick a private channel if that's not what you want."
                if channel.permissions_for(everyone).view_channel else "")

    STATE = [app_commands.Choice(name="on", value="on"), app_commands.Choice(name="off", value="off")]

    @bot.tree.command(name="cmdlog", description="Log every command used in this server to a channel (server admins)")
    @app_commands.describe(channel="Where to post the log", state="Turn the log on or off")
    @app_commands.choices(state=STATE)
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.allowed_installs(guilds=True, users=False)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
    async def cmdlog(interaction: discord.Interaction, channel: discord.TextChannel = None, state: app_commands.Choice[str] = None):
        user = interaction.user
        if not (user.id == interaction.guild.owner_id or user.guild_permissions.administrator or user.guild_permissions.manage_guild):
            return await interaction.response.send_message("Only server admins can change the command log.", ephemeral=True)
        key = str(interaction.guild_id)
        cfg = cfgDB.get(key) or {"enabled": False, "channel": None}
        note = ""
        if channel is not None:
            cfg["channel"] = str(channel.id)
            cfg.pop("refused", None)
            hooks.pop(channel.id, None)
            if state is None:
                cfg["enabled"] = True
            note = public_warning(channel)
        if state is not None:
            if state.value == "on" and not cfg.get("channel"):
                return await interaction.response.send_message("Pick a channel first: `/cmdlog channel:#logs`.", ephemeral=True)
            cfg["enabled"] = state.value == "on"
        cfgDB.set(key, cfg)
        where = f"<#{cfg['channel']}>" if cfg.get("channel") else "no channel yet"
        await interaction.response.send_message(f"The command log is **{'on' if cfg.get('enabled') else 'off'}** ({where}).{note}", ephemeral=True)

    @bot.tree.command(name="botlog", description="Bot owners only: log every command and every server added or removed")
    @app_commands.describe(channel="Where to post the global log", state="Turn the whole log on or off",
                           commands="Log commands from every server", events="Log servers added and removed")
    @app_commands.choices(state=STATE)
    @app_commands.allowed_installs(guilds=True, users=False)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
    async def botlog(interaction: discord.Interaction, channel: discord.TextChannel = None, state: app_commands.Choice[str] = None,
                     commands: bool = None, events: bool = None):
        if not m.is_bot_owner(interaction.user):
            return await interaction.response.send_message("Only the bot owners can use the bot log.", ephemeral=True)
        cfg = cfgDB.get(BOTLOG_KEY) or {"enabled": False, "channel": None, "commands": True, "events": True}
        if channel is not None:
            cfg["channel"] = str(channel.id)
            cfg.pop("refused", None)
            hooks.pop(channel.id, None)
            if state is None:
                cfg["enabled"] = True
        if state is not None:
            if state.value == "on" and not cfg.get("channel"):
                return await interaction.response.send_message("Pick a channel first: `/botlog channel:#logs`.", ephemeral=True)
            cfg["enabled"] = state.value == "on"
        if commands is not None:
            cfg["commands"] = commands
        if events is not None:
            cfg["events"] = events
        cfgDB.set(BOTLOG_KEY, cfg)
        await interaction.response.send_message(
            f"The bot log is **{'on' if cfg.get('enabled') else 'off'}** (" + (f"<#{cfg['channel']}>" if cfg.get("channel") else "no channel yet") +
            f"). Commands: {'on' if cfg.get('commands', True) else 'off'} · Server events: {'on' if cfg.get('events', True) else 'off'}.", ephemeral=True)

    m.cmdlog_post = post

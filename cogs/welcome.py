"""Cog: welcome messages (/welcome) and the start-up catch-up for members who joined while the bot was offline."""
import asyncio
import sys
import time
from discord import app_commands
from discord.ext import commands
import discord

m = sys.modules.get("main") or sys.modules["__main__"]   # this bot's main.py: shared helpers, data and the bot itself
MainURL = m.MainURL
_embed_color = m._embed_color
autoroleDB = m.autoroleDB
welcomeDB = m.Table("welcome")       # server id -> its welcome settings
botstateDB = m.Table("botstate")     # "lastup" -> {"at": epoch}: when the bot was last running

DEFAULT = {"enabled": False, "channel": "", "message": "Welcome to **{server}**, {user}! You are member #{count}.", "embed": True,
           "title": "Welcome!", "dm": False, "dm_message": "Welcome to {server}, {name}! Have a great time.", "include_bots": False}

UNKNOWN_WINDOW = 6 * 3600        # no "last running" time saved yet: look at people who joined in the last 6 hours
MAX_WINDOW = 7 * 86400
WELCOME_CAP = 20                 # welcome messages per server in one catch-up
BEAT_EVERY = 30


def get_cfg(guild_id):
    saved = welcomeDB.get(str(guild_id)) or {}
    return {**DEFAULT, **{k: v for k, v in saved.items() if v is not None}}


def save_cfg(guild_id, cfg):
    welcomeDB.set(str(guild_id), {**DEFAULT, **cfg})
    welcomeDB.save()


def render(text, member):
    """{user} mentions them, {name} is their name, {server} the server's name, {count} the member number, {id} their id."""
    return (str(text).replace("{user}", member.mention).replace("{name}", member.display_name).replace("{server}", member.guild.name)
            .replace("{count}", str(member.guild.member_count)).replace("{id}", str(member.id)))


async def send_welcome(member, force=False):
    """Welcome one member in the welcome channel and/or by DM. Returns what was sent ("channel", "dm", both, or "")."""
    cfg = get_cfg(member.guild.id)
    if not (cfg["enabled"] or force):
        return ""
    if member.bot and not (cfg["include_bots"] or force):
        return ""
    sent = []
    channel = member.guild.get_channel(int(cfg["channel"])) if str(cfg["channel"]).isdigit() else None
    if channel and cfg["message"]:
        try:
            if cfg["embed"]:
                embed = discord.Embed(description=render(cfg["message"], member), color=discord.Color.blurple())
                if cfg["title"]:
                    embed.title = render(cfg["title"], member)[:256]
                embed.set_thumbnail(url=member.display_avatar.url)
                await channel.send(content=member.mention, embed=embed, allowed_mentions=discord.AllowedMentions(users=[member]))
            else:
                await channel.send(render(cfg["message"], member), allowed_mentions=discord.AllowedMentions(users=[member]))
            sent.append("channel")
        except discord.HTTPException as e:
            print(f"[welcome] couldn't post in #{channel.name}: {e}")
    if cfg["dm"] and cfg["dm_message"] and not member.bot:
        try:
            await member.send(render(cfg["dm_message"], member))
            sent.append("dm")
        except discord.HTTPException as e:
            print(f"[welcome] couldn't DM {member.id}: {e}")
    return "+".join(sent)


def _autorole_for(member):
    """The role this member should get from /autorole, or None (off, a bot, or the role is gone)."""
    data = autoroleDB.get(f"{member.guild.id}") or {}
    if member.bot or not data.get("enabled") or not data.get("role_id"):
        return None
    return member.guild.get_role(int(data["role_id"]))


class WelcomeCog(commands.Cog, name="Welcome"):
    """/welcome, the join listener and the start-up catch-up."""
    welcome = app_commands.Group(name="welcome", description="Welcome new members (server managers)", guild_only=True,
                                 default_permissions=discord.Permissions(manage_guild=True))

    def __init__(self, bot):
        self.bot = bot
        self._started = False

    # ------------------------------------------------------------------ settings
    @staticmethod
    def _embed(interaction, cfg, note=None):
        embed = discord.Embed(title="Welcome messages", color=_embed_color(interaction.user.id))
        where = f"in <#{cfg['channel']}>" if cfg["channel"] else "(no channel chosen)"
        embed.description = (f"✅ {note}\n\n" if note else "") + f"Welcoming is **{'on' if cfg['enabled'] else 'off'}** {where}."
        embed.add_field(name="Message", value=(cfg["message"] or "Not set")[:1000], inline=False)
        embed.add_field(name="DM", value=("On: " + cfg["dm_message"][:300]) if cfg["dm"] else "Off", inline=False)
        embed.add_field(name="Style", value=("Embed" if cfg["embed"] else "Plain text") + (", bots included" if cfg["include_bots"] else ", bots skipped"), inline=False)
        embed.set_footer(text="Use {user} {name} {server} {count} {id} in the text")
        return embed

    async def _save(self, interaction, cfg, note):
        save_cfg(interaction.guild_id, cfg)
        await interaction.response.send_message(embed=self._embed(interaction, cfg, note), ephemeral=True)

    @welcome.command(name="status", description="Show the welcome settings")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def status(self, interaction: discord.Interaction):
        await interaction.response.send_message(embed=self._embed(interaction, get_cfg(interaction.guild_id)), ephemeral=True)

    @welcome.command(name="channel", description="Where welcome messages are posted")
    @app_commands.describe(channel="The welcome channel")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        cfg = get_cfg(interaction.guild_id)
        cfg["channel"] = str(channel.id)
        await self._save(interaction, cfg, f"Welcome messages go to {channel.mention}.")

    @welcome.command(name="message", description="The welcome message ({user} {name} {server} {count} {id})")
    @app_commands.describe(text="For example: Welcome {user} to {server}!", title="Embed title (optional)")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def message(self, interaction: discord.Interaction, text: app_commands.Range[str, 1, 1500], title: app_commands.Range[str, 0, 100] = None):
        cfg = get_cfg(interaction.guild_id)
        cfg["message"] = text
        if title is not None:
            cfg["title"] = title
        await self._save(interaction, cfg, "Message saved.")

    @welcome.command(name="dm", description="Also send new members a DM")
    @app_commands.describe(enabled="Send the DM?", text="The DM text (optional)")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def dm(self, interaction: discord.Interaction, enabled: bool, text: app_commands.Range[str, 1, 1500] = None):
        cfg = get_cfg(interaction.guild_id)
        cfg["dm"] = enabled
        if text:
            cfg["dm_message"] = text
        await self._save(interaction, cfg, "DM welcome is " + ("on." if enabled else "off."))

    @welcome.command(name="style", description="Embed or plain text, and whether bots are welcomed")
    @app_commands.describe(embed="Send the message as an embed?", bots="Welcome bots too?")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def style(self, interaction: discord.Interaction, embed: bool = None, bots: bool = None):
        cfg = get_cfg(interaction.guild_id)
        if embed is not None:
            cfg["embed"] = embed
        if bots is not None:
            cfg["include_bots"] = bots
        await self._save(interaction, cfg, "Saved.")

    @welcome.command(name="toggle", description="Turn welcoming on or off")
    @app_commands.describe(enabled="On or off")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def toggle(self, interaction: discord.Interaction, enabled: bool):
        cfg = get_cfg(interaction.guild_id)
        if enabled and not (cfg["channel"] or cfg["dm"]):
            return await interaction.response.send_message("Pick a channel first: `/welcome channel`", ephemeral=True)
        cfg["enabled"] = enabled
        await self._save(interaction, cfg, "Welcoming is " + ("on." if enabled else "off."))

    @welcome.command(name="test", description="Send yourself a test welcome")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def test(self, interaction: discord.Interaction):
        result = await send_welcome(interaction.user, force=True)
        await interaction.response.send_message(f"Sent a test welcome ({result})." if result else "Nothing was sent. Set a channel and message first.", ephemeral=True)

    # ------------------------------------------------------------------ joins
    @commands.Cog.listener()
    async def on_member_join(self, member):
        try:
            await send_welcome(member)
        except Exception as e:
            print(f"[welcome] {type(e).__name__}: {e}")

    # ------------------------------------------------------------------ start-up catch-up
    @commands.Cog.listener()
    async def on_ready(self):
        if not self._started:                      # on_ready also fires after every reconnect: do this once
            self._started = True
            asyncio.get_running_loop().create_task(self._start())

    async def _start(self):
        await asyncio.sleep(20)                    # let the Roblox hooks and the member cache finish loading
        try:
            await self.catch_up()
        except Exception as e:
            print(f"[catchup] failed: {type(e).__name__}: {e}")
        while True:
            try:
                botstateDB.set("lastup", {"at": int(time.time())})
                botstateDB.save()
            except Exception as e:
                print(f"[catchup] couldn't save the running time: {e}")
            await asyncio.sleep(BEAT_EVERY)

    async def catch_up(self):
        """Give people who joined while the bot was down what a live join would have: autorole, welcome, Roblox auto-verify."""
        now = time.time()
        last = int((botstateDB.get("lastup") or {}).get("at") or 0)
        since = max(last or now - UNKNOWN_WINDOW, now - MAX_WINDOW)
        print(f"[catchup] looking for members who joined in the last {int((now - since) / 60)} minutes")
        given = welcomed = checked = 0
        verify = getattr(m, "rverify_join", None)
        for guild in list(self.bot.guilds):
            recent = sorted((mem for mem in guild.members if mem.joined_at and mem.joined_at.timestamp() >= since), key=lambda mem: mem.joined_at)
            sent = 0
            for member in recent:
                try:
                    role = _autorole_for(member)
                    if role and role not in member.roles:
                        await member.add_roles(role, reason="Autorole (catch-up after downtime)")
                        given += 1
                    if sent < WELCOME_CAP and not member.bot and await send_welcome(member):
                        sent += 1
                        welcomed += 1
                    if verify and not member.bot:
                        await verify(member)
                        checked += 1
                except Exception as e:
                    print(f"[catchup] {guild.id}/{member.id}: {type(e).__name__}: {e}")
        print(f"[catchup] done: {given} roles given, {welcomed} welcomes sent, {checked} members checked for Roblox verification")


async def setup(bot):
    await bot.add_cog(WelcomeCog(bot))

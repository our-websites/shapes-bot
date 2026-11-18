"""Cog: logs the DMs the bot sends and receives."""
import sys
from discord import app_commands
from discord.ext import commands
import discord

m = sys.modules.get("main") or sys.modules["__main__"]   # this bot's main.py: shared helpers, data and the bot itself
_dm_cmd = m._dm_cmd
_dm_source = m._dm_source
bot = m.bot
contextvars = m.contextvars
dmlogDB = m.dmlogDB
is_bot_owner = m.is_bot_owner
log_dm = m.log_dm


_dm_user = contextvars.ContextVar("dm_user", default=None)         # set while a command that was run in a DM is being answered


def _dm_text(content=None, embeds=None, embed=None):
    parts = [str(content)] if content else []
    for e in list(embeds or []) + ([embed] if embed else []):
        d = e.to_dict() if hasattr(e, "to_dict") else (e or {})
        for key in ("title", "description"):
            if d.get(key):
                parts.append(str(d[key]))
        for f in d.get("fields") or []:
            parts.append(f"{f.get('name', '')}: {f.get('value', '')}")
    return " | ".join(parts)[:1000]


def _install_dm_logging():
    orig_send = discord.abc.Messageable.send

    async def send(self, content=None, **kw):
        message = await orig_send(self, content, **kw)
        try:
            channel = getattr(message, "channel", None)
            if isinstance(channel, discord.DMChannel) and getattr(channel, "recipient", None) and (bot.user is None or channel.recipient.id != bot.user.id):
                log_dm("out", channel.recipient, _dm_text(content, kw.get("embeds"), kw.get("embed")), _dm_source())
        except Exception as e:
            print(f"[dm log] {type(e).__name__}: {e}")
        return message
    discord.abc.Messageable.send = send

    def wrap(cls, name, text_of):
        orig = getattr(cls, name)

        async def wrapped(self, *args, **kw):
            result = await orig(self, *args, **kw)
            who = _dm_user.get()
            if who is not None:
                log_dm("out", who, text_of(args, kw), _dm_source())
            return result
        setattr(cls, name, wrapped)
    first = lambda args, kw: _dm_text(kw.get("content") if "content" in kw else (args[0] if args else None), kw.get("embeds"), kw.get("embed"))
    wrap(discord.InteractionResponse, "send_message", first)
    wrap(discord.InteractionResponse, "edit_message", first)
    wrap(discord.Interaction, "edit_original_response", first)
    wrap(discord.Webhook, "send", first)         # followups; only logged while a DM command is being answered (see _dm_user)

    async def check(interaction):
        if interaction.guild_id is None and interaction.user and not interaction.user.bot:
            _dm_user.set(interaction.user)
            name = interaction.command.qualified_name if interaction.command else "command"
            _dm_cmd.set(name)
            args = " ".join(f"{k}:{v}" for k, v in (interaction.namespace.__dict__.items() if interaction.namespace else []))[:300]
            log_dm("in", interaction.user, f"/{name} {args}".strip(), "command in a DM")
        return True
    bot.tree.interaction_check = check

    async def on_dm(message):
        if message.guild is None and not message.author.bot and (bot.user is None or message.author.id != bot.user.id):
            log_dm("in", message.author, message.content or "(attachment)", "DM to the bot")
    bot.add_listener(on_dm, "on_message")


class DMLogCog(commands.Cog, name="DM log"):
    """Tracks every DM the bot sends or receives. /dmlog shows them (bot owners)."""

    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        _install_dm_logging()

    @app_commands.command(name="dmlog", description="Bot owners only: show the latest DMs the bot sent or received")
    @app_commands.describe(user="Only this person", count="How many to show (default 15, max 40)")
    @app_commands.allowed_installs(guilds=True, users=False)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
    async def dmlog(self, interaction: discord.Interaction, user: discord.User = None, count: app_commands.Range[int, 1, 40] = 15):
        if not is_bot_owner(interaction.user):
            return await interaction.response.send_message("Only the bot owners can read the DM log.", ephemeral=True)
        rows = [v for v in dmlogDB.items().values() if isinstance(v, dict) and (user is None or v.get("user_id") == str(user.id))]
        rows.sort(key=lambda r: -int(r.get("at") or 0))
        if not rows:
            return await interaction.response.send_message("No DMs logged yet.", ephemeral=True)
        lines = [f"{'📤' if r['dir'] == 'out' else '📥'} <t:{r['at']}:R> **{r.get('name')}** ({r.get('source') or ''})\n> {str(r.get('text') or '')[:150]}" for r in rows[:count]]
        await interaction.response.send_message(f"**DM log** ({len(rows)} total)\n" + "\n".join(lines)[:1900], ephemeral=True)

async def setup(bot):
    await bot.add_cog(DMLogCog(bot))

"""Cog: moderation: /kick /ban /unban /timeout /untimeout /purge /slowmode /nick /lock /unlock /warn /warnings /clearwarnings."""
import sys
import time
from datetime import timedelta
from discord import app_commands
from discord.ext import commands
import discord

m = sys.modules.get("main") or sys.modules["__main__"]   # this bot's main.py: shared helpers, data and the bot itself
MainURL = m.MainURL
_embed_color = m._embed_color
warnsDB = m.Table("warns")          # "server:user" -> [{"at": epoch, "by": id, "reason": text}, ...]

MAX_WARNS_SHOWN = 15


def _guild_only(fn):
    fn = app_commands.allowed_installs(guilds=True, users=False)(fn)
    return app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)(fn)


def _reason(text):
    return (text or "No reason given")[:400]


class ModerationCog(commands.Cog, name="Moderation"):
    """Kick, ban, timeout, purge, slowmode, lock and warnings. Each needs the matching Discord permission."""

    def __init__(self, bot):
        self.bot = bot

    @staticmethod
    def _embed(interaction, title, description, color=None):
        embed = discord.Embed(title=title, description=description, color=color or _embed_color(interaction.user.id))
        embed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
        return embed

    @staticmethod
    async def _say(interaction, text, ephemeral=True):
        if interaction.response.is_done():
            await interaction.followup.send(embed=discord.Embed(description=text, color=discord.Color.red() if text.startswith("❌") else discord.Color.green()), ephemeral=ephemeral)
        else:
            await interaction.response.send_message(embed=discord.Embed(description=text, color=discord.Color.red() if text.startswith("❌") else discord.Color.green()), ephemeral=ephemeral)

    async def _can_act(self, interaction, target):
        """True if the person using the command and the bot may act on `target` (role order); otherwise replies with why."""
        guild = interaction.guild
        if target.id == interaction.user.id:
            await self._say(interaction, "❌ You can't do that to yourself.")
        elif target.id == guild.owner_id:
            await self._say(interaction, "❌ Nobody can do that to the server owner.")
        elif target.id == self.bot.user.id:
            await self._say(interaction, "❌ I won't do that to myself.")
        elif interaction.user.id != guild.owner_id and target.top_role >= interaction.user.top_role:
            await self._say(interaction, "❌ That person's highest role is the same as or above yours.")
        elif target.top_role >= guild.me.top_role:
            await self._say(interaction, "❌ That person's highest role is above mine. Move my role higher in the server settings.")
        else:
            return True
        return False

    async def _dm(self, member, text):
        try:
            await member.send(text)
        except discord.HTTPException:
            pass

    # ---------------------------------------------------------------- removing people
    @app_commands.command(name="kick", description="Kick a member from the server")
    @app_commands.describe(member="Who to kick", reason="Why (shown in the audit log)")
    @app_commands.default_permissions(kick_members=True)
    @app_commands.checks.has_permissions(kick_members=True)
    @_guild_only
    async def kick(self, interaction: discord.Interaction, member: discord.Member, reason: str = ""):
        if not await self._can_act(interaction, member):
            return
        await self._dm(member, f"You were kicked from **{interaction.guild.name}**. Reason: {_reason(reason)}")
        try:
            await member.kick(reason=f"{interaction.user} ({interaction.user.id}): {_reason(reason)}")
        except discord.HTTPException as e:
            return await self._say(interaction, f"❌ Discord wouldn't let me kick them ({e.text or e.status}).")
        await interaction.response.send_message(embed=self._embed(interaction, "👢 Kicked", f"{member.mention} (`{member.id}`) was kicked.\n**Reason:** {_reason(reason)}"))

    @app_commands.command(name="ban", description="Ban a member from the server")
    @app_commands.describe(member="Who to ban", reason="Why (shown in the audit log)", delete_days="Delete their messages from the last 0 to 7 days")
    @app_commands.default_permissions(ban_members=True)
    @app_commands.checks.has_permissions(ban_members=True)
    @_guild_only
    async def ban(self, interaction: discord.Interaction, member: discord.Member, reason: str = "", delete_days: app_commands.Range[int, 0, 7] = 0):
        if not await self._can_act(interaction, member):
            return
        await self._dm(member, f"You were banned from **{interaction.guild.name}**. Reason: {_reason(reason)}")
        try:
            await member.ban(reason=f"{interaction.user} ({interaction.user.id}): {_reason(reason)}", delete_message_seconds=delete_days * 86400)
        except discord.HTTPException as e:
            return await self._say(interaction, f"❌ Discord wouldn't let me ban them ({e.text or e.status}).")
        await interaction.response.send_message(embed=self._embed(interaction, "🔨 Banned", f"{member.mention} (`{member.id}`) was banned.\n**Reason:** {_reason(reason)}"))

    @app_commands.command(name="unban", description="Unban someone by their user ID")
    @app_commands.describe(user_id="The ID of the banned user", reason="Why")
    @app_commands.default_permissions(ban_members=True)
    @app_commands.checks.has_permissions(ban_members=True)
    @_guild_only
    async def unban(self, interaction: discord.Interaction, user_id: str, reason: str = ""):
        if not user_id.strip().isdigit():
            return await self._say(interaction, "❌ Give the user's ID (numbers only).")
        try:
            await interaction.guild.unban(discord.Object(id=int(user_id)), reason=f"{interaction.user} ({interaction.user.id}): {_reason(reason)}")
        except discord.NotFound:
            return await self._say(interaction, "❌ That person isn't banned here.")
        except discord.HTTPException as e:
            return await self._say(interaction, f"❌ Discord wouldn't let me unban them ({e.text or e.status}).")
        await interaction.response.send_message(embed=self._embed(interaction, "✅ Unbanned", f"<@{int(user_id)}> (`{int(user_id)}`) was unbanned."))

    # ---------------------------------------------------------------- timeouts
    @app_commands.command(name="timeout", description="Time a member out (they can't talk or react)")
    @app_commands.describe(member="Who to time out", minutes="How long, 1 to 40320 (28 days)", reason="Why")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    @_guild_only
    async def timeout(self, interaction: discord.Interaction, member: discord.Member, minutes: app_commands.Range[int, 1, 40320], reason: str = ""):
        if not await self._can_act(interaction, member):
            return
        try:
            await member.timeout(timedelta(minutes=minutes), reason=f"{interaction.user} ({interaction.user.id}): {_reason(reason)}")
        except discord.HTTPException as e:
            return await self._say(interaction, f"❌ Discord wouldn't let me time them out ({e.text or e.status}).")
        until = int(time.time()) + minutes * 60
        await interaction.response.send_message(embed=self._embed(interaction, "🔇 Timed out", f"{member.mention} is timed out until <t:{until}:f> (<t:{until}:R>).\n**Reason:** {_reason(reason)}"))

    @app_commands.command(name="untimeout", description="Remove a member's timeout")
    @app_commands.describe(member="Who to let back in")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    @_guild_only
    async def untimeout(self, interaction: discord.Interaction, member: discord.Member):
        try:
            await member.timeout(None, reason=f"{interaction.user} ({interaction.user.id})")
        except discord.HTTPException as e:
            return await self._say(interaction, f"❌ Discord wouldn't let me do that ({e.text or e.status}).")
        await interaction.response.send_message(embed=self._embed(interaction, "🔊 Timeout removed", f"{member.mention} can talk again."))

    # ---------------------------------------------------------------- channels
    @app_commands.command(name="purge", description="Delete recent messages in this channel")
    @app_commands.describe(amount="How many recent messages to look at (1 to 100)", user="Only delete messages from this person")
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.checks.bot_has_permissions(manage_messages=True, read_message_history=True)
    @_guild_only
    async def purge(self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, 100], user: discord.Member = None):
        await interaction.response.defer(ephemeral=True)
        try:
            deleted = await interaction.channel.purge(limit=amount, check=(lambda msg: msg.author.id == user.id) if user else None, bulk=True,
                                                      reason=f"/purge by {interaction.user} ({interaction.user.id})")
        except discord.HTTPException as e:
            return await self._say(interaction, f"❌ I couldn't delete them ({e.text or e.status}).")
        await self._say(interaction, f"🧹 Deleted **{len(deleted)}** message{'s' if len(deleted) != 1 else ''}" + (f" from {user.mention}" if user else "") + ". (Discord only bulk-deletes messages newer than 14 days.)")

    @app_commands.command(name="slowmode", description="Set this channel's slowmode (0 turns it off)")
    @app_commands.describe(seconds="Seconds between messages, 0 to 21600")
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    @_guild_only
    async def slowmode(self, interaction: discord.Interaction, seconds: app_commands.Range[int, 0, 21600]):
        try:
            await interaction.channel.edit(slowmode_delay=seconds, reason=f"/slowmode by {interaction.user} ({interaction.user.id})")
        except (discord.HTTPException, TypeError) as e:
            return await self._say(interaction, f"❌ I couldn't change this channel ({e}).")
        await interaction.response.send_message(embed=self._embed(interaction, "🐌 Slowmode", "Slowmode is **off**." if not seconds else f"People can now post once every **{seconds}s**."))

    async def _lock(self, interaction, locked):
        channel = interaction.channel
        everyone = interaction.guild.default_role
        overwrite = channel.overwrites_for(everyone)
        overwrite.send_messages = False if locked else None
        try:
            await channel.set_permissions(everyone, overwrite=overwrite, reason=f"{'/lock' if locked else '/unlock'} by {interaction.user} ({interaction.user.id})")
        except (discord.HTTPException, TypeError) as e:
            return await self._say(interaction, f"❌ I couldn't change this channel ({e}).")
        await interaction.response.send_message(embed=self._embed(interaction, "🔒 Locked" if locked else "🔓 Unlocked",
                                                                    "Members can't send messages here until it's unlocked (`/unlock`)." if locked else "Members can send messages here again."))

    @app_commands.command(name="lock", description="Stop members sending messages in this channel")
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.checks.bot_has_permissions(manage_roles=True)
    @_guild_only
    async def lock(self, interaction: discord.Interaction):
        await self._lock(interaction, True)

    @app_commands.command(name="unlock", description="Let members send messages in this channel again")
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.checks.bot_has_permissions(manage_roles=True)
    @_guild_only
    async def unlock(self, interaction: discord.Interaction):
        await self._lock(interaction, False)

    @app_commands.command(name="nick", description="Change (or clear) a member's nickname")
    @app_commands.describe(member="Whose nickname", nickname="The new nickname (leave empty to clear it)")
    @app_commands.default_permissions(manage_nicknames=True)
    @app_commands.checks.has_permissions(manage_nicknames=True)
    @app_commands.checks.bot_has_permissions(manage_nicknames=True)
    @_guild_only
    async def nick(self, interaction: discord.Interaction, member: discord.Member, nickname: str = ""):
        if member.id != interaction.user.id and not await self._can_act(interaction, member):
            return
        try:
            await member.edit(nick=nickname[:32] or None, reason=f"/nick by {interaction.user} ({interaction.user.id})")
        except discord.HTTPException as e:
            return await self._say(interaction, f"❌ I couldn't change it ({e.text or e.status}).")
        await interaction.response.send_message(embed=self._embed(interaction, "✏️ Nickname", f"{member.mention}'s nickname " + (f"is now **{nickname[:32]}**." if nickname else "was cleared.")))

    # ---------------------------------------------------------------- warnings
    @staticmethod
    def _key(guild_id, user_id):
        return f"{guild_id}:{user_id}"

    @app_commands.command(name="warn", description="Warn a member (kept on record, they get a DM)")
    @app_commands.describe(member="Who to warn", reason="Why")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    @_guild_only
    async def warn(self, interaction: discord.Interaction, member: discord.Member, reason: str):
        if member.bot:
            return await self._say(interaction, "❌ Bots can't be warned.")
        if not await self._can_act(interaction, member):
            return
        key = self._key(interaction.guild_id, member.id)
        rows = list(warnsDB.get(key) or [])
        rows.append({"at": int(time.time()), "by": str(interaction.user.id), "reason": _reason(reason)})
        warnsDB.set(key, rows[-100:])
        warnsDB.save()
        await self._dm(member, f"You were warned in **{interaction.guild.name}**. Reason: {_reason(reason)}")
        await interaction.response.send_message(embed=self._embed(interaction, "⚠️ Warned", f"{member.mention} now has **{len(rows)}** warning{'s' if len(rows) != 1 else ''}.\n**Reason:** {_reason(reason)}"))

    @app_commands.command(name="warnings", description="See a member's warnings")
    @app_commands.describe(member="Whose warnings")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    @_guild_only
    async def warnings(self, interaction: discord.Interaction, member: discord.Member):
        rows = warnsDB.get(self._key(interaction.guild_id, member.id)) or []
        if not rows:
            return await interaction.response.send_message(embed=self._embed(interaction, "Warnings", f"{member.mention} has no warnings."), ephemeral=True)
        lines = [f"**{i}.** <t:{r['at']}:d> by <@{r['by']}>: {r['reason']}" for i, r in enumerate(rows[-MAX_WARNS_SHOWN:], max(1, len(rows) - MAX_WARNS_SHOWN + 1))]
        await interaction.response.send_message(embed=self._embed(interaction, f"Warnings: {len(rows)}", f"{member.mention}\n\n" + "\n".join(lines)), ephemeral=True)

    @app_commands.command(name="clearwarnings", description="Remove all of a member's warnings")
    @app_commands.describe(member="Whose warnings to clear")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    @_guild_only
    async def clearwarnings(self, interaction: discord.Interaction, member: discord.Member):
        key = self._key(interaction.guild_id, member.id)
        count = len(warnsDB.get(key) or [])
        warnsDB.remove(key)
        warnsDB.save()
        await interaction.response.send_message(embed=self._embed(interaction, "🧽 Warnings cleared", f"Removed {count} warning{'s' if count != 1 else ''} from {member.mention}."))


async def setup(bot):
    await bot.add_cog(ModerationCog(bot))

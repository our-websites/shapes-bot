"""Cog: server utilities: /userinfo /roleinfo /membercount /poll /remind."""
import asyncio
import re
import sys
import time
from discord import app_commands
from discord.ext import commands
import discord

m = sys.modules.get("main") or sys.modules["__main__"]   # this bot's main.py: shared helpers, data and the bot itself
MainURL = m.MainURL
_embed_color = m._embed_color
remindersDB = m.Table("reminders")       # id -> {"user", "channel", "at", "text"}

NUMBER_EMOJI = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
MAX_REMINDERS = 10                        # per person
MAX_SECONDS = 365 * 86400


def parse_duration(text):
    """'1h30m', '2d', '45 minutes', '90s' -> seconds, or None when it can't be read."""
    parts = re.findall(r"(\d+)\s*(d|day|days|h|hr|hrs|hour|hours|m|min|mins|minute|minutes|s|sec|secs|second|seconds)(?![a-z])", str(text).lower())
    if not parts or re.sub(r"(\d+)\s*([a-z]+)|\s+", "", str(text).lower()):
        return None
    unit = {"d": 86400, "h": 3600, "m": 60, "s": 1}
    return sum(int(n) * unit[u[0]] for n, u in parts)


def _everywhere(fn):
    fn = app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)(fn)
    return app_commands.allowed_installs(guilds=True, users=True)(fn)


def _guild_only(fn):
    fn = app_commands.allowed_installs(guilds=True, users=False)(fn)
    return app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)(fn)


class UtilityCog(commands.Cog, name="Utility"):
    """Information about people, roles and the server, polls and reminders."""

    def __init__(self, bot):
        self.bot = bot
        self._task = None

    @staticmethod
    def _embed(interaction, title=None, description=None):
        embed = discord.Embed(title=title, description=description, color=_embed_color(interaction.user.id))
        embed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
        return embed

    async def cog_load(self):
        self._task = asyncio.get_running_loop().create_task(self._reminder_loop())

    async def cog_unload(self):
        if self._task:
            self._task.cancel()

    # ---------------------------------------------------------------- info
    @app_commands.command(name="userinfo", description="Information about a member of this server")
    @app_commands.describe(member="Who (default: you)")
    @_guild_only
    async def userinfo(self, interaction: discord.Interaction, member: discord.Member = None):
        member = member or interaction.user
        embed = self._embed(interaction, member.display_name + ("  ·  🤖 Bot" if member.bot else ""))
        embed.color = member.color if member.color.value else embed.color
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Username", value=f"{member} (`{member.id}`)", inline=False)
        embed.add_field(name="Account created", value=f"<t:{int(member.created_at.timestamp())}:D>\n<t:{int(member.created_at.timestamp())}:R>", inline=True)
        if member.joined_at:
            embed.add_field(name="Joined this server", value=f"<t:{int(member.joined_at.timestamp())}:D>\n<t:{int(member.joined_at.timestamp())}:R>", inline=True)
        roles = [r.mention for r in reversed(member.roles) if not r.is_default()]
        embed.add_field(name=f"Roles ({len(roles)})", value=(" ".join(roles[:20]) + (f" and {len(roles) - 20} more" if len(roles) > 20 else "")) if roles else "None", inline=False)
        if member.timed_out_until:
            embed.add_field(name="Timed out until", value=f"<t:{int(member.timed_out_until.timestamp())}:R>", inline=True)
        if member.guild_permissions.administrator:
            embed.add_field(name="Key permission", value="Administrator", inline=True)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="roleinfo", description="Information about a role")
    @app_commands.describe(role="The role")
    @_guild_only
    async def roleinfo(self, interaction: discord.Interaction, role: discord.Role):
        embed = self._embed(interaction, role.name)
        embed.color = role.color if role.color.value else embed.color
        embed.add_field(name="ID", value=f"`{role.id}`", inline=True)
        embed.add_field(name="Members", value=f"{len(role.members):,}", inline=True)
        embed.add_field(name="Colour", value=str(role.color), inline=True)
        embed.add_field(name="Created", value=f"<t:{int(role.created_at.timestamp())}:D>", inline=True)
        embed.add_field(name="Shown separately", value="Yes" if role.hoist else "No", inline=True)
        embed.add_field(name="Mentionable", value="Yes" if role.mentionable else "No", inline=True)
        keys = [name.replace("_", " ") for name, on in role.permissions if on and name in ("administrator", "manage_guild", "manage_roles", "manage_channels", "kick_members", "ban_members", "moderate_members", "manage_messages", "mention_everyone")]
        embed.add_field(name="Key permissions", value=", ".join(keys) or "None", inline=False)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="membercount", description="How many people are in this server")
    @_guild_only
    async def membercount(self, interaction: discord.Interaction):
        guild = interaction.guild
        bots = sum(1 for mem in guild.members if mem.bot) if guild.chunked else None
        total = guild.member_count
        text = f"**{total:,}** members" + (f"\n**{total - bots:,}** people · **{bots:,}** bots" if bots is not None else "")
        await interaction.response.send_message(embed=self._embed(interaction, guild.name, text))

    # ---------------------------------------------------------------- poll
    @app_commands.command(name="poll", description="Start a poll people vote on with reactions")
    @app_commands.describe(question="What to ask", options="2 to 10 choices separated by | (leave empty for a yes/no poll)")
    @_everywhere
    async def poll(self, interaction: discord.Interaction, question: app_commands.Range[str, 1, 250], options: str = ""):
        choices = [o.strip() for o in options.split("|") if o.strip()]
        if options and not 2 <= len(choices) <= 10:
            return await interaction.response.send_message("Give between 2 and 10 choices separated by `|`, like `pizza | tacos | sushi`.", ephemeral=True)
        emojis = NUMBER_EMOJI[:len(choices)] if choices else ["👍", "👎"]
        body = "\n".join(f"{e} {c[:200]}" for e, c in zip(emojis, choices)) if choices else "React with 👍 or 👎"
        await interaction.response.send_message(embed=self._embed(interaction, f"📊 {question}", body))
        message = await interaction.original_response()
        for emoji in emojis:
            try:
                await message.add_reaction(emoji)
            except discord.HTTPException:
                break

    # ---------------------------------------------------------------- reminders
    @app_commands.command(name="remind", description="Remind you of something later")
    @app_commands.describe(when="For example 10m, 2h30m or 1d", text="What to remind you of")
    @_everywhere
    async def remind(self, interaction: discord.Interaction, when: str, text: app_commands.Range[str, 1, 500]):
        seconds = parse_duration(when)
        if not seconds or seconds > MAX_SECONDS:
            return await interaction.response.send_message("Say how long with numbers and units, like `10m`, `2h30m` or `1d` (up to a year).", ephemeral=True)
        mine = [k for k, v in remindersDB.items().items() if str(v.get("user")) == str(interaction.user.id)]
        if len(mine) >= MAX_REMINDERS:
            return await interaction.response.send_message(f"You already have {MAX_REMINDERS} reminders waiting.", ephemeral=True)
        at = int(time.time()) + seconds
        remindersDB.set(f"{interaction.user.id}:{at}:{int(time.time() * 1000) % 100000}", {"user": str(interaction.user.id), "channel": str(interaction.channel_id or ""), "at": at, "text": text})
        remindersDB.save()
        await interaction.response.send_message(embed=self._embed(interaction, "⏰ Reminder set", f"I'll remind you <t:{at}:R> (<t:{at}:f>):\n> {text}"), ephemeral=True)

    async def _reminder_loop(self):
        await self.bot.wait_until_ready()
        while True:
            try:
                now = time.time()
                for key, row in list(remindersDB.items().items()):
                    if row.get("at", 0) <= now:
                        await self._deliver(row)
                        remindersDB.remove(key)
                        remindersDB.save()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[remind] {type(e).__name__}: {e}")
            await asyncio.sleep(15)

    async def _deliver(self, row):
        late = time.time() - row["at"]
        text = f"⏰ <@{row['user']}> Reminder: {row['text']}" + ("\n(Sent late: the bot was offline.)" if late > 120 else "")
        try:
            user = await self.bot.fetch_user(int(row["user"]))
            await user.send(text)
            return
        except discord.HTTPException:
            pass
        channel = self.bot.get_channel(int(row["channel"])) if str(row.get("channel", "")).isdigit() else None
        if channel:
            try:
                await channel.send(text, allowed_mentions=discord.AllowedMentions(users=True))
            except discord.HTTPException:
                pass


async def setup(bot):
    await bot.add_cog(UtilityCog(bot))

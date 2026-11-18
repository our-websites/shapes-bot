"""Cog: server tools: /server (information) and /autorole."""
import sys
from discord import app_commands
from discord.ext import commands
import discord

m = sys.modules.get("main") or sys.modules["__main__"]   # this bot's main.py: shared helpers, data and the bot itself
MainURL = m.MainURL
_embed_color = m._embed_color
autoroleDB = m.autoroleDB
bot = m.bot


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


class ServerCog(commands.Cog, name="Server"):
    """/server info."""
    server = app_commands.Group(name="server", description="Information about this server", guild_only=True)

    def __init__(self, bot):
        self.bot = bot

    @server.command(name="info", description="Show information about this server")
    async def info(self, interaction: discord.Interaction):
        guild = interaction.guild
        people = sum(1 for mem in guild.members if not mem.bot)
        bots = sum(1 for mem in guild.members if mem.bot)
        embed = discord.Embed(title=guild.name, color=_embed_color(interaction.user.id), description=guild.description or None)
        embed.add_field(name="Owner", value=f"<@{guild.owner_id}>", inline=True)
        embed.add_field(name="Server ID", value=f"`{guild.id}`", inline=True)
        embed.add_field(name="Created", value=f"<t:{int(guild.created_at.timestamp())}:D>", inline=True)
        embed.add_field(name="Members", inline=False,
                        value=f"**{guild.member_count:,}** members\n**{people:,}** people · **{bots:,}** bots" if guild.chunked
                        else f"**{guild.member_count:,}** members")
        text, voice = len(guild.text_channels), len(guild.voice_channels)
        embed.add_field(name="Channels", value=f"{text + voice:,} total\n{text:,} text · {voice:,} voice\n{len(guild.categories):,} categories", inline=True)
        embed.add_field(name="Roles", value=f"{len(guild.roles):,}", inline=True)
        embed.add_field(name="Boosts", value=f"Level {guild.premium_tier} · {guild.premium_subscription_count or 0}", inline=True)
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        if guild.banner:
            embed.set_image(url=guild.banner.url)
        embed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
        await interaction.response.send_message(embed=embed)


_COMMANDS = [
    (bot.tree, "autorole"),
]


class ServerTools(commands.Cog, name="Autorole"):
    """server tools: /server (information) and /autorole."""

    async def cog_unload(self):                  # so a reload (bot.reload_extension) re-registers cleanly
        for parent, name in _COMMANDS:
            parent.remove_command(name)


async def setup(bot):
    await bot.add_cog(ServerTools(bot))
    await bot.add_cog(ServerCog(bot))

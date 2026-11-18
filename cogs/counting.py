"""Cog: the counting game: /counting and /countingleaderboard."""
import sys
from discord import app_commands
from discord.ext import commands
import discord
import typing

m = sys.modules.get("main") or sys.modules["__main__"]   # this bot's main.py: shared helpers, data and the bot itself
MainURL = m.MainURL
SITE_URL = m.SITE_URL
bot = m.bot
countingDB = m.countingDB
counting_get = m.counting_get
counting_limit = m.counting_limit
embedDB = m.embedDB
is_bot_owner = m.is_bot_owner


@bot.tree.command(name="counting", description="Counting Settings For Shapes")
@app_commands.default_permissions(administrator=True)
@commands.bot_has_permissions(add_reactions=True, moderate_members=True, read_message_history=True, view_channel=True, send_messages=True)
@app_commands.allowed_installs(guilds=True, users=False)
@app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
async def counting(interaction: discord.Interaction, warnings: typing.Optional[app_commands.Range[int, 0, 50]] = None):
    server = interaction.guild
    if warnings is not None:      # bot owners only: how many warnings before the count is ruined (0 = ruined on the first mistake)
        if not is_bot_owner(interaction.user):
            return await interaction.response.send_message("Only the bot owners can change the warnings amount.", ephemeral=True)
        data = counting_get(server.id)
        data["max_warnings"] = int(warnings)
        countingDB.set(server.id, data)
        countingDB.save()
        return await interaction.response.send_message(f"Counting here now allows **{warnings}** warning{'s' if warnings != 1 else ''} before the count is ruined.", ephemeral=True)
    countingData = counting_get(server.id)
    text_channels = [c for c in server.channels if isinstance(c, discord.TextChannel)]
    mine = server.get_channel(countingData["channel"]) if countingData["channel"] else None
    shown = ([mine] if mine else []) + [c for c in text_channels if c != mine]
    channel_options = [discord.SelectOption(label=c.name[:100], value=str(c.id), default=(c == mine)) for c in shown[:25]]
    if not channel_options:
        return await interaction.response.send_message("This server has no text channels for counting.", ephemeral=True)
    class CountingView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=None)

        @discord.ui.button(label="Toggle Counting", style=discord.ButtonStyle.primary, custom_id="toggle_counting", emoji="🔢")
        async def toggle_counting(self, interaction: discord.Interaction, button: discord.ui.Button):
            countingData = counting_get(server.id)
            current_setting = countingData['enabled']
            if current_setting:
                countingData['enabled'] = False
                countingDB.set(server.id, countingData)
                countingDB.save()
                await interaction.response.send_message("Counting disabled.", ephemeral=True)
            else:
                countingData['enabled'] = True
                countingDB.set(server.id, countingData)
                countingDB.save()
                await interaction.response.send_message("Counting enabled.", ephemeral=True)

        @discord.ui.select(placeholder="Select Counting Channel", options=channel_options, custom_id="select_channel")
        async def select_channel(self, interaction: discord.Interaction, select: discord.ui.Select):
            selected_channel_id = int(select.values[0])
            countingData = counting_get(server.id)
            countingData['channel'] = selected_channel_id
            countingDB.set(server.id, countingData)
            countingDB.save()
            await interaction.response.send_message(f"Counting channel set to <#{selected_channel_id}>.", ephemeral=True)
    embed = discord.Embed(
        title="Counting Settings",
        description=f"**Current Settings:**\n- Counting Enabled: `{countingData['enabled']}`\n- Counting Channel: {f"<#{countingData['channel']}>" if countingData['channel'] else "`none yet`"}\n- Current Number: `{countingData['number']}`\n- Highest Number: `{countingData.get('highestnumber', 0)}`\n- Warnings: `{countingData['warnings']}/{counting_limit(countingData)}`\n- Last Counter: `{countingData['lastcounter']}`",
        color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
    )
    embed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
    await interaction.response.send_message(embed=embed, view=CountingView(), ephemeral=True)


@bot.tree.command(name="countingleaderboard", description="View the counting leaderboard website")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def countingleaderboard(interaction: discord.Interaction):
    """Send the counting leaderboard website link"""
    
    embed = discord.Embed(
        title="Counting Leaderboard",
        description=f"[Click here to view the counting leaderboard]({SITE_URL}/counting/leaderboard/)",
        color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
    )
    embed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
    
    await interaction.response.send_message(embed=embed)


_COMMANDS = [
    (bot.tree, "counting"),
    (bot.tree, "countingleaderboard"),
]


class Counting(commands.Cog, name="Counting"):
    """the counting game: /counting and /countingleaderboard."""

    async def cog_unload(self):                  # so a reload (bot.reload_extension) re-registers cleanly
        for parent, name in _COMMANDS:
            parent.remove_command(name)


async def setup(bot):
    await bot.add_cog(Counting(bot))

"""Cog: everyday commands: /help, /ping, /invite, /status, /settings, server and install counts."""
import sys
import procstats
from discord import app_commands
from discord.ext import commands
import asyncio
import discord

m = sys.modules.get("main") or sys.modules["__main__"]   # this bot's main.py: shared helpers, data and the bot itself
DiscordColors = m.DiscordColors
MainURL = m.MainURL
bot = m.bot
colors = m.colors
embedDB = m.embedDB


# === Bot Commands ===
@bot.tree.command(name="userinstalls", description="Get The User Installation Count For Shapes!")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def userinstalls(interaction: discord.Interaction):
    await interaction.response.send_message(f"{m.BotInfo.approximate_user_install_count} Users Use Shapes!")


@bot.tree.command(name="servercount", description="Get The Server Count For Shapes!")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def servercount(interaction: discord.Interaction):
    await interaction.response.send_message(f"{len(bot.guilds)} Servers Use Shapes!")


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


@bot.tree.command(name="status", description="Get the bot status")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(f"[Shapes Status](https://status.lecanact.me)")


def _quality(ms):
    return "Great" if ms < 100 else "Good" if ms < 200 else "Average" if ms < 400 else "Poor"


def _ping_line(ms):
    return "Ping: not available" if ms is None else f"Ping: `{ms:.0f}ms` ({_quality(ms)})"


def _ping_embed(user, fleet, api_ms):
    """This bot, the API and the bot on the other platform: ping, uptime, memory and CPU for each. A part that doesn't answer says why."""
    problems = fleet.get("problems") or {}
    embed = discord.Embed(title="Shapes status", color=embedDB.get(f"{user.id}") if embedDB.get(f"{user.id}") else discord.Color.blue())
    embed.add_field(name="Discord bot (this one)", value=f"{_ping_line(m.bot_latency_ms())}\n{procstats.describe(procstats.snapshot())}", inline=False)
    api = fleet.get("api")
    api_text = f"Not responding. {problems.get('api', '')}".strip() if not api else f"{_ping_line(api_ms)}\n{procstats.describe(api)}" + (f"\nVersion: `{api['version']}`" if api.get("version") else "")
    embed.add_field(name="Shapes API", value=api_text, inline=False)
    other = fleet.get("yapin")
    if other:
        link = other.get("roundtrip_ms")
        other_text = f"{_ping_line(other.get('latency_ms'))}\n{procstats.describe(other)}" + (f"\nAPI link: `{link:.0f}ms`" if link else "") + (f"\nReported `{other['age']}s` ago" if other.get("age") else "")
    else:
        other_text = f"Not responding. {problems.get('yapin', 'The API has no answer from it.')}"
    embed.add_field(name="Yapin' bot", value=other_text, inline=False)
    embed.set_footer(text=f"Requested By {user.name} | {MainURL}")
    return embed


@bot.tree.command(name="ping", description="Ping, uptime, memory and CPU for this bot, the API and the Yapin' bot.")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def ping(interaction: discord.Interaction):
    await interaction.response.defer()
    fleet, api_ms = await asyncio.to_thread(m.fleet_stats)
    await interaction.followup.send(embed=_ping_embed(interaction.user, fleet, api_ms))


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


class HelpDropdown(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Counting Commands", description="Counting game and settings"),
            discord.SelectOption(label="User Settings", description="Personal bot settings"),
            discord.SelectOption(label="Roblox Commands", description="Roblox user and item information"),
            discord.SelectOption(label="Utility Commands", description="General utility commands"),
            discord.SelectOption(label="Fun Commands", description="Dice, coins, picks, maths and pictures"),
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
                
        elif category == "Fun Commands":
            embed = discord.Embed(title="Fun Commands", color=embed_color)
            embed.add_field(name="Commands", value=
                "• /roll - Roll dice (2d6, d20+3)\n• /coinflip - Flip a coin\n• /8ball - Ask the magic 8-ball\n• /choose - Pick between options\n"
                "• /calc - Do some maths\n• /cat, /dog - Random pictures\n• /fact - A random fact", inline=False)

        elif category == "Roblox Commands":
            embed = discord.Embed(title="Roblox Commands", color=embed_color)
            embed.add_field(name="Commands", value=
                "• /roblox info - Get detailed Roblox user information\n"
                "• /roblox todiscord - Find a Roblox user's Discord\n"
                "• /roblox fromdiscord - Find a Discord user's Roblox\n"
                "• /roblox british - Check if user is british\n"
                "• /roblox item, /roblox bundle, /roblox asset - Item information\n"
                "• /roblox group - Roblox group information\n"
                "• /roblox place - Roblox place/game information\n"
                "• /roblox badge, /roblox badges, /roblox recentbadges - Badges\n"
                "• /roblox avatar, /roblox outfits, /roblox limiteds - Avatars and inventory", inline=False)
            embed.add_field(name="Verification", value=
                "• /roblox verify - Verify in a server (no Yapin' account needed)\n"
                "• /roblox link - Link a Roblox account (up to 100)\n"
                "• /roblox accounts, /roblox auto, /roblox remove, /roblox hide, /roblox show - Manage your accounts\n"
                "• /roblox panel ..., /roblox sync - Server setup (admins)\n"
                "• /vortex game, /vortex user, /vortex cookie - PlayVortex lookups\n"
                "• /lastfm link, /lastfm unlink, /lastfm user - Last.fm", inline=False)
                
        elif category == "Utility Commands":
            embed = discord.Embed(title="Utility Commands", color=embed_color)
            embed.add_field(name="Commands", value=
                "• /ping - Check bot latency and connection\n"
                "• /invite - Get bot invite link\n"
                "• /google - Search something on Google\n"
                "• /status - Bot status\n"
                "• /userinstalls - Get user installation count\n"
                "• /servercount - Get server count\n"
                "• /server info - Information about this server\n"
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


_COMMANDS = [
    (bot.tree, "userinstalls"),
    (bot.tree, "servercount"),
    (bot.tree, "settings"),
    (bot.tree, "status"),
    (bot.tree, "ping"),
    (bot.tree, "invite"),
    (bot.tree, "help"),
]


class General(commands.Cog, name="General"):
    """everyday commands: /help, /ping, /invite, /status, /settings, server and install counts."""

    async def cog_unload(self):                  # so a reload (bot.reload_extension) re-registers cleanly
        for parent, name in _COMMANDS:
            parent.remove_command(name)


async def setup(bot):
    await bot.add_cog(General(bot))

"""Cog: everyday commands: /help, /ping, /invite, /status, /settings, server and install counts."""
import sys
import asyncio
import time
import yapper

import procstats

m = sys.modules.get("main") or sys.modules["__main__"]   # this bot's main.py: shared helpers, data and the bot itself
DiscordColors = m.DiscordColors
InviteURL = m.InviteURL
MainURL = m.MainURL
bot = m.bot
discord = m.discord
ecolor = m.ecolor
embedDB = m.embedDB
total_members = m.total_members


@bot.tree.command(name="userinstalls", description="Get The User Installation Count For Shapes!")
async def userinstalls(interaction: discord.Interaction):
    await interaction.response.send_message(f"{total_members():,} Users Use Shapes!")


@bot.tree.command(name="servercount", description="Get The Server Count For Shapes!")
async def servercount(interaction: discord.Interaction):
    await interaction.response.send_message(f"{len(bot.servers()):,} Servers Use Shapes!")


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
    await interaction.response.send_message(f"[Shapes Status](https://status.lecanact.me)")


@bot.tree.command(name="help", description="Get help with all available commands")
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(title="Bot Help & Commands", color=ecolor(interaction.user.id),
                          description="Use `/command` or `!command`. Put names with spaces in quotes.")
    embed.add_field(name="Counting", value="• /counting - Count stats for everyone; admins also get settings (`toggle`, `channel #channel`, `emoji <correct|warning|wrong> <emoji>`)\n• /countingleaderboard - Leaderboard website\n• /cmdlog - Command log (server owner)", inline=False)
    embed.add_field(name="User Settings", value="• /settings - Customize your embed color\n• /autorole - Give new members a role\n• /lastfm - Link Last.fm", inline=False)
    embed.add_field(name="Moderation", value="• /automod - Block words, links, invites, spam, caps and mass mentions (admins)\n• /serverinfo - Server information", inline=False)
    embed.add_field(name="Roblox", value=(
        "• /robloxinfo - User information\n• /roblox2yapin - Find a Roblox user's linked account\n• /yapin2roblox - Find a user's Roblox\n"
        "• /britishuser - Check if a user is British\n• /iteminfo - Item information\n• /placeinfo - Place/game information\n"
        "• /badge - Badge information\n• /badges - Check a user's badges\n• /recentbadges - Latest badges\n• /outfits - A user's outfits\n"
        "• /limiteds - Scan for limiteds\n• /bundle - Bundle information\n• /asset - Asset information\n• /avatar - Avatar\n• /linkroblox - Link your Roblox\n• /robloxverify - Verify with Roblox, hide accounts\n• /robloxpanel - Roblox verification setup (admins)\n• /vortexgame <game ID>, /vortexinfo <user ID>, /vortexcookie - Vortex"), inline=False)
    embed.add_field(name="Utility", value=(
        "• /ping - Latency\n• /invite - Invite link\n• /search - Search The Web\n• /ai - Chat with an AI (/clearai deletes your history)\n• /github - GitHub user\n"
        "• /roll, /coinflip, /8ball, /choose, /calc - Dice, coins, answers, picks and maths\n• /cat, /dog, /fact - Pictures and facts\n"
        "• /instagram - Instagram user\n• /status - Bot status\n• /servercount, /userinstalls - Counts\n• /spookpfp - spook.bio"), inline=False)
    embed.add_field(name="Yapper profile", value="• /yapinuser - Info about a user\n• /yapinavatar - Avatar\n• /yapinbanner - Banner", inline=False)
    embed.set_footer(text="Right-click context menus from the Discord bot are not available on Yapper; use the slash commands.")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="invite", description="Get the bot's invite link.")
async def invite(interaction: discord.Interaction):
    embed = discord.Embed(description=f"[Click Here To Add Shapes To Your Server]({InviteURL})", color=ecolor(interaction.user.id))
    embed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
    await interaction.response.send_message(embed=embed)


def _quality(ms):
    return "Great" if ms < 100 else "Good" if ms < 200 else "Average" if ms < 400 else "Poor"


def _ping_line(ms):
    return "Ping: not available" if ms is None else f"Ping: `{ms:.0f}ms` ({_quality(ms)})"


def _ping_embed(user, mine, fleet, api_ms):
    """This bot, the API and the bot on the other platform: ping, uptime, memory and CPU for each."""
    embed = discord.Embed(title="Shapes status", color=ecolor(user.id))
    embed.add_field(name="Yapin' bot (this one)", value=f"{_ping_line(mine)}\n{procstats.describe(procstats.snapshot())}", inline=False)
    api = fleet.get("api")
    api_text = "Not responding" if not api else f"Ping: `{api_ms:.0f}ms` ({_quality(api_ms)})\n{procstats.describe(api)}" + (f"\nVersion: `{api['version']}`" if api.get("version") else "")
    embed.add_field(name="Shapes API", value=api_text, inline=False)
    other = fleet.get("discord")
    embed.add_field(name="Discord bot", value="Not responding" if not other else f"{_ping_line(other.get('latency_ms'))}\n{procstats.describe(other)}", inline=False)
    embed.set_footer(text=f"Requested By {user.name} | {MainURL}")
    return embed


@bot.tree.command(name="ping", description="Ping, uptime, memory and CPU for this bot, the API and the Discord bot.")
async def ping(interaction: discord.Interaction):
    await interaction.response.defer()
    mine = await asyncio.to_thread(m.measure_latency)
    fleet, api_ms = await asyncio.to_thread(m.fleet_stats)
    await interaction.followup.send(embed=_ping_embed(interaction.user, mine, fleet, api_ms))


class General(yapper.Cog):
    name = "General"


def setup(bot):
    bot.add_cog(General(bot))

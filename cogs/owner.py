"""Cog: bot-owner commands: /stop, /restart, /getdata."""
import sys
from discord import app_commands
from discord.ext import commands
import aiohttp
import asyncio
import discord
import os
import threading

m = sys.modules.get("main") or sys.modules["__main__"]   # this bot's main.py: shared helpers, data and the bot itself
AI_DB = m.AI_DB
autoroleDB = m.autoroleDB
bot = m.bot
co_owner = m.co_owner
countingDB = m.countingDB
embedDB = m.embedDB
flush_all = m.flush_all
owner = m.owner
token = m.token
usersDB = m.usersDB


def _relaunch():
    """Start this same program again in place (same command line, so flags like -u survive). Frees the web port by replacing the process."""
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    argv = list(getattr(sys, "orig_argv", None) or [sys.executable] + sys.argv)
    try:
        os.closerange(3, 1024)      # nothing (sockets, files) should leak into the new process
        os.execv(sys.executable, argv)
    except Exception as e:
        print(f"execv failed ({e}), starting a new process instead")
        import subprocess
        subprocess.Popen(argv)
        os._exit(0)


@bot.tree.command(name="getdata", description="Get The Data From One Of Our Databases")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(database="The database to get data from", key="The key to get data for")
@app_commands.choices(database=[
    app_commands.Choice(name="embedDB", value="embedDB"),
    app_commands.Choice(name="countingDB", value="countingDB"),
    app_commands.Choice(name="usersDB", value="usersDB"),
    app_commands.Choice(name="autoroleDB", value="autoroleDB"),
    app_commands.Choice(name="AI_DB", value="AI_DB"),
])
async def getdata(interaction: discord.Interaction, database: str, key: str):
    # add options for each database and each key and restrict who can use the command.
    if not interaction.user.id in [481295611417853982, 1129085908390518895]:
        await interaction.response.send_message(":x: You don't have permission to use this command! :x:", ephemeral=True)
        return
    db_map = {
        "embedDB": embedDB,
        "countingDB": countingDB,
        "usersDB": usersDB,
        "autoroleDB": autoroleDB,
        "AI_DB": AI_DB,
    }
    selected_db = db_map.get(database)
    if selected_db:
        data = selected_db.get(f"{key}")
        if data:
            await interaction.response.send_message(f"Data for key `{key}` in database `{database}`: ```{data}```", ephemeral=True)
        else:
            await interaction.response.send_message(f":x: No data found for key `{key}` in database `{database}`! :x:", ephemeral=True)
    else:
        await interaction.response.send_message(f":x: Database `{database}` not found! :x:", ephemeral=True)


@bot.tree.command(name="stop", description="Stop the bot.")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def stop(interaction: discord.Interaction):
    if interaction.user.name == "lcjunior1220" or interaction.user.name == "sl.ip" or interaction.user.name == "kiwixor":
        await interaction.response.send_message(":white_check_mark: Shutdown Successfully!", ephemeral=False)
        
        flush_all()
        
        print("Bot shutdown initiated by command")
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post('http://localhost:13455/shutdown') as resp:
                    pass
        except:
            pass
        
        await bot.close()
        os._exit(0)
    else:
        await interaction.response.send_message(f"Only {owner}, and {co_owner} can use this command.", ephemeral=True)


@bot.tree.command(name="restart", description="Restart the bot.")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def restart(interaction: discord.Interaction):
    if interaction.user.name in ("lcjunior1220", "sl.ip", "kiwixor"):
        await interaction.response.send_message(":white_check_mark: Restarting... back in a few seconds.", ephemeral=False)
        flush_all()
        print("Bot restart initiated by command")
        # Not bot.close(): main.py's watchdog turns a closed bot into a shutdown. The new process takes over the gateway and the port.
        threading.Timer(1.5, _relaunch).start()
    else:
        await interaction.response.send_message(f"Only {owner} and {co_owner} can use this command.", ephemeral=True)


_COMMANDS = [
    (bot.tree, "getdata"),
    (bot.tree, "stop"),
    (bot.tree, "restart"),
]


class Owner(commands.Cog, name="Owner"):
    """bot-owner commands: /stop, /restart, /getdata."""

    async def cog_unload(self):                  # so a reload (bot.reload_extension) re-registers cleanly
        for parent, name in _COMMANDS:
            parent.remove_command(name)


async def setup(bot):
    await bot.add_cog(Owner(bot))

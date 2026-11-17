"""Cog: bot-owner commands: /stop, /restart, /getdata, /partner, /botlog."""
import sys
import asyncio
import json
import os
import threading
import time
import yapper

m = sys.modules.get("main") or sys.modules["__main__"]   # this bot's main.py: shared helpers, data and the bot itself
DATABASES = m.DATABASES
MainURL = m.MainURL
_cache = m._cache
bot = m.bot
botlog_configure = m.botlog_configure
discord = m.discord
ecolor = m.ecolor
get_botlog = m.get_botlog
is_bot_owner = m.is_bot_owner
is_owner = m.is_owner
log_action = m.log_action
make_partner_invite = m.make_partner_invite
owner = m.owner
partnersDB = m.partnersDB
resolve_any_channel = m.resolve_any_channel
server_summary = m.server_summary
valid_invite_url = m.valid_invite_url


@bot.tree.command(name="getdata", description="Owner only: /getdata <embedDB|countingDB|usersDB|autoroleDB|AI_DB> <key>")
async def getdata(interaction: discord.Interaction, database: str, key: str):
    # Private reply (ephemeral): only the person who ran the command sees it.
    if not is_owner(interaction.user):
        await interaction.response.send_message(":x: You don't have permission to use this command! :x:", ephemeral=True)
        return
    db = DATABASES.get(database)
    if db is None:
        await interaction.response.send_message(f":x: Database `{database}` not found! Options: {', '.join(DATABASES)}", ephemeral=True)
        return
    data = db.get(key)
    if data:
        text = f"Data for key `{key}` in database `{database}`:\n```{json.dumps(data, indent=2, default=str)[:1800]}```"
    else:
        text = f":x: No data found for key `{key}` in database `{database}`! :x:"
    await interaction.response.send_message(text, ephemeral=True)


def _save_everything():
    for db in DATABASES.values():
        try:
            db.save()
        except Exception as e:
            print(f"save failed for {db.name}: {e}")


def _shutdown_services():
    """Stop the bot and release the web server's port, so a restart can bind it again right away."""
    for label, stopper in (("bot", getattr(bot, "stop", None)), ("web server", stop_web)):
        try:
            if stopper:
                stopper()
        except Exception as e:
            print(f"could not stop the {label}: {e}")


class ConfirmStop(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)

    async def interaction_check(self, click):
        if not is_owner(click.user):
            await click.response.send_message("Only the bot owner can press this.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Stop the bot", style=discord.ButtonStyle.danger)
    async def confirm(self, click, button):
        self.stop()
        await click.response.edit_message(content="✅ Shutting down. Saving data first…", embed=None, view=None)
        _save_everything()
        print("Bot shutdown initiated by command")
        _shutdown_services()
        threading.Timer(1.0, lambda: os._exit(0)).start()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, click, button):
        self.stop()
        await click.response.edit_message(content="Cancelled. The bot is still running.", embed=None, view=None)


@bot.tree.command(name="stop", description="Stop the bot (asks you to confirm).")
async def stop(interaction: discord.Interaction):
    if not is_owner(interaction.user):
        await interaction.response.send_message(f"Only {owner} can use this command.", ephemeral=True)
        return
    await interaction.response.send_message("⚠️ Stop the bot completely? It stays offline until someone starts it again.",
                                            view=ConfirmStop(), ephemeral=True)


@bot.tree.command(name="restart", description="Restart the bot.")
async def restart(interaction: discord.Interaction):
    if not is_owner(interaction.user):
        await interaction.response.send_message(f"Only {owner} can use this command.", ephemeral=True)
        return
    await interaction.response.send_message(":white_check_mark: Restarting...", ephemeral=True)
    _save_everything()
    print("Bot restart initiated by command")
    _shutdown_services()    # frees port first: starting a second server on it was what broke restarts

    def relaunch():
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:
            pass
        argv = [sys.executable] + sys.argv
        try:
            os.closerange(3, 1024)      # nothing (sockets, files) should leak into the new process
            os.execv(sys.executable, argv)
        except Exception as e:
            print(f"execv failed ({e}), starting a new process instead")
            import subprocess
            subprocess.Popen(argv)
            os._exit(0)
    threading.Timer(1.5, relaunch).start()


@bot.tree.command(name="partner", description="Manage partner servers (bot owner): /partner add|remove|description|invite|list <server id> [text]")
async def partner(interaction: discord.Interaction, action: str = "list", server: str = "", text: str = ""):
    uid = str(interaction.user.id)
    if not is_bot_owner(uid):
        await interaction.response.send_message("Only the bot owner can manage partners.", ephemeral=True)
        return
    action, server = (action or "list").lower(), str(server or "").strip()
    if server == "" and action in ("add", "remove", "description", "invite") and interaction.guild_id:
        server = str(interaction.guild_id)           # run it inside the server to partner that one
    if action == "list" or (action not in ("add", "remove", "description", "invite")):
        rows = [f"**{partnersDB.get(s).get('name') or s}** (`{s}`) " + ("— " + partnersDB.get(s)["invite"] if partnersDB.get(s).get("invite") else "— no invite yet") for s in partnersDB.all()]
        embed = discord.Embed(title="Partners", description="\n".join(rows) or "No partners yet. `/partner add <server id> [description]`", color=ecolor(uid))
        embed.add_field(name="Commands", value="`/partner add <server id> [description]` · `/partner remove <server id>` · `/partner description <server id> <text>` · `/partner invite <server id> <invite url>` (or `new`)", inline=False)
        embed.set_footer(text=f"Shown at {MainURL}/partners")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    if not server:
        await interaction.response.send_message("Which server? Give its id (or run this inside the server).", ephemeral=True)
        return
    if action == "remove":
        gone = partnersDB.remove(server)
        _cache.pop(f"summary:{server}", None)
        await interaction.response.send_message("Removed from the partners page." if gone else "That server isn't a partner.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    row = dict(partnersDB.get(server) or {})
    if action == "add":
        try:
            info = await asyncio.to_thread(server_summary, server)
        except Exception as e:
            await interaction.followup.send(f"I can only partner servers I'm in. I couldn't load that one ({e}).", ephemeral=True)
            return
        invite = await asyncio.to_thread(make_partner_invite, server)
        row.update(name=info["name"], icon=info["icon"], added_by=uid, added_at=row.get("added_at") or int(time.time()), invite=invite or row.get("invite"), invite_auto=bool(invite) or bool(row.get("invite_auto")))
        if text:
            row["description"] = text.strip()[:400]
        partnersDB.set(server, row)
        log_action("partner", f"added {info['name']}", server_id=server)
        note = "" if row.get("invite") else "\n⚠️ I couldn't make or find an invite. Add one with `/partner invite <server id> <invite url>`."
        await interaction.followup.send(f"**{info['name']}** is now a partner: {MainURL}/partners{note}", ephemeral=True)
        return
    if not partnersDB.exists(server):
        await interaction.followup.send("That server isn't a partner yet. `/partner add <server id>` first.", ephemeral=True)
        return
    if action == "description":
        row["description"] = text.strip()[:400] or None
        partnersDB.set(server, row)
        await interaction.followup.send("Description saved." if row["description"] else "Description cleared: the server's own description is used.", ephemeral=True)
        return
    if action == "invite":
        url = (await asyncio.to_thread(make_partner_invite, server)) if text.strip().lower() in ("", "new") else valid_invite_url(text)
        if not url:
            await interaction.followup.send("I need an https://www.yapin.gg/... invite link (I couldn't make one myself).", ephemeral=True)
            return
        row["invite"] = url
        row["invite_auto"] = text.strip().lower() in ("", "new")      # only a grabbed invite is refreshed on its own
        partnersDB.set(server, row)
        await interaction.followup.send(f"Invite saved: {url}", ephemeral=True)


def _botlog_status_embed(color, note=None):
    cfg = get_botlog()
    embed = discord.Embed(title="Bot log", color=color)
    embed.description = (f"✅ {note}\\n\\n" if note else "") + (f"Logging is **{'on' if cfg['enabled'] else 'off'}**" + (f" in <#{cfg['channel']}>" if cfg["channel"] else " (no channel chosen)") + ".")
    embed.add_field(name="Every command, in every server", value="on" if cfg["commands"] else "off", inline=True)
    embed.add_field(name="Server added / removed", value="on" if cfg["events"] else "off", inline=True)
    embed.add_field(name="Change it", value="`/botlog channel #logs` · `/botlog on|off` · `/botlog commands on|off` · `/botlog events on|off`", inline=False)
    return embed


@bot.tree.command(name="botlog", description="Bot owners only: log every command and server add/remove: /botlog [channel #c | on | off | commands on|off | events on|off]")
async def botlog(interaction: discord.Interaction, action: str = "status", value: str = ""):
    uid = str(interaction.user.id)
    if not is_bot_owner(uid):
        await interaction.response.send_message("Only the bot owners can set up the bot log.", ephemeral=True)
        return
    color, action, value = ecolor(uid), (action or "status").lower(), (value or "").strip().lower()
    if action in ("on", "off"):
        cfg = botlog_configure(enabled=action == "on")
        if action == "on" and not cfg["enabled"]:
            await interaction.response.send_message("Pick a channel first: `/botlog channel #logs`", ephemeral=True)
            return
        await interaction.response.send_message(embed=_botlog_status_embed(color, f"The bot log is now {action}."), ephemeral=True)
    elif action in ("commands", "events") and value in ("on", "off"):
        botlog_configure(**{action: value == "on"})
        await interaction.response.send_message(embed=_botlog_status_embed(color, f"{action.title()} logging is now {value}."), ephemeral=True)
    elif action == "channel":
        ch = resolve_any_channel(value)
        if not ch:
            await interaction.response.send_message("I couldn't find that channel. Use `/botlog channel #logs` (a channel in a server I'm in).", ephemeral=True)
            return
        botlog_configure(channel_id=str(ch.get("id")), enabled=True)
        await interaction.response.send_message(embed=_botlog_status_embed(color, f"Logging to <#{ch.get('id')}>."), ephemeral=True)
    else:
        await interaction.response.send_message(embed=_botlog_status_embed(color), ephemeral=True)


def stop_web():
    """Close the web server's socket (so the port is free again)."""
    if m._web_server is not None:
        m._web_server.shutdown()
        m._web_server.server_close()


class Owner(yapper.Cog):
    name = "Owner"


def setup(bot):
    bot.add_cog(Owner(bot))

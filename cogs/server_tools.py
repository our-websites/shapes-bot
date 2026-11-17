"""Cog: server management: /cmdlog, /autorole, /automod, /serverinfo."""
import sys
import automod
import yapper

m = sys.modules.get("main") or sys.modules["__main__"]   # this bot's main.py: shared helpers, data and the bot itself
MainURL = m.MainURL
_all_members = m._all_members
_user_ids = m._user_ids
bot = m.bot
channel_is_public = m.channel_is_public
cmdlog_configure = m.cmdlog_configure
discord = m.discord
ecolor = m.ecolor
get_automod = m.get_automod
get_autorole = m.get_autorole
get_cmdlog = m.get_cmdlog
is_admin = m.is_admin
is_server_owner = m.is_server_owner
resolve_channel = m.resolve_channel
resolve_role = m.resolve_role
save_automod = m.save_automod
save_autorole = m.save_autorole
server_summary = m.server_summary


async def need_admin(interaction):
    """True if the caller may manage this server (replies and returns False otherwise)."""
    if interaction.is_dm:
        await interaction.response.send_message("This command only works inside a server.", ephemeral=True)
        return False
    if not is_admin(interaction.guild_id, interaction.user.id):
        await interaction.response.send_message("You need to be a server admin to use this command.", ephemeral=True)
        return False
    return True


def _cmdlog_status_embed(sid, color, note=None):
    cfg = get_cmdlog(sid)
    embed = discord.Embed(title="Command log", color=color)
    state = "on" if cfg["enabled"] else "off"
    embed.description = (f"✅ {note}\n\n" if note else "") + (
        f"Logging is **{state}**" + (f" in <#{cfg['channel']}>" if cfg["channel"] else " (no channel chosen)") + ".")
    if cfg["channel"]:
        how = {"webhook": "through a webhook", "bot": "as the bot (Yapin' wouldn't let me make a webhook)"}.get(cfg["mode"], "through a webhook if possible, otherwise as the bot")
        embed.add_field(name="Posts", value=how, inline=False)
        if channel_is_public(sid, cfg["channel"]):
            embed.add_field(name="⚠️ Public channel", value="Everyone in the server can read this channel, so everyone can see who runs which command.", inline=False)
    embed.add_field(name="Change it", value="`/cmdlog channel #private-logs` · `/cmdlog on` · `/cmdlog off` · the dashboard shows the same log with names and times", inline=False)
    return embed


@bot.tree.command(name="cmdlog", description="Log every command used in this server (server owner only): /cmdlog [channel #channel | on | off]")
async def cmdlog(interaction: discord.Interaction, action: str = "status", value: str = ""):
    if interaction.is_dm:
        await interaction.response.send_message("This command only works inside a server.", ephemeral=True)
        return
    sid, uid = str(interaction.guild_id), str(interaction.user.id)
    if not is_server_owner(sid, uid):
        await interaction.response.send_message("Only the server owner can set up command logs.", ephemeral=True)
        return
    color, action = ecolor(uid), (action or "status").lower()
    if action in ("on", "off"):
        cfg, _ = cmdlog_configure(sid, enabled=action == "on")
        if action == "on" and not cfg["enabled"]:
            await interaction.response.send_message("Pick a channel first: `/cmdlog channel #private-logs`", ephemeral=True)
            return
        await interaction.response.send_message(embed=_cmdlog_status_embed(sid, color, f"Command logging is now {action}."), ephemeral=True)
        return
    if action == "channel":
        ch = resolve_channel(sid, value) if value else None
        if not ch:
            await interaction.response.send_message("Tell me which text channel: `/cmdlog channel #private-logs`", ephemeral=True)
            return
        cfg, warning = cmdlog_configure(sid, channel_id=str(ch.get("id")), enabled=True)
        if warning:
            view = discord.ui.View(timeout=300)
            use = discord.ui.Button(label="Log publicly anyway", style=discord.ButtonStyle.danger)
            cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)

            async def confirm(click):
                if str(click.user.id) != uid:
                    await click.response.send_message("Only the owner who ran the command can answer this.", ephemeral=True)
                    return
                cmdlog_configure(sid, channel_id=str(ch.get("id")), enabled=True, confirm_public=True)
                await click.response.edit_message(embed=_cmdlog_status_embed(sid, color, "Public logging is on."), view=None)

            async def nope(click):
                if str(click.user.id) == uid:
                    await click.response.edit_message(embed=discord.Embed(title="Command log", description="Okay, nothing changed.", color=color), view=None)
            use.callback, cancel.callback = confirm, nope
            view.add_item(use)
            view.add_item(cancel)
            await interaction.response.send_message(embed=discord.Embed(title="⚠️ That channel is public", color=discord.Color.orange(), description=(
                f"<#{ch.get('id')}> can be read by everyone in the server, so everyone would see who runs which command.\n"
                "Choose a private channel, or log publicly if that's what you want.")), view=view, ephemeral=True)
            return
        await interaction.response.send_message(embed=_cmdlog_status_embed(sid, color, f"Logging to <#{ch.get('id')}>."), ephemeral=True)
        return
    await interaction.response.send_message(embed=_cmdlog_status_embed(sid, color), ephemeral=True)


def assign_autorole_to_existing(server_id, cfg):
    """Gives the role to everyone who is not exempt and does not have it yet. Returns how many got it."""
    count = 0
    for m in _all_members(server_id):
        if m["id"] in cfg["exempt_users"] or cfg["role_id"] in m["roles"] or (m["bot"] and not cfg["include_bots"]):
            continue
        try:
            bot.add_role(server_id, m["id"], cfg["role_id"])
            count += 1
        except Exception as e:
            print(f"[autorole] could not give the role to {m['id']}: {e}")
    return count


def _autorole_embed(cfg, user, role_name=None, note=None):
    on = cfg["enabled"] and cfg["role_id"]
    embed = discord.Embed(title="Autorole", color=discord.Color.green() if on else discord.Color.red(),
                          description=(f"✅ {note}\n\n" if note else "") + (f"New members get <@&{cfg['role_id']}>." if on else "Autorole is **off**."))
    embed.add_field(name="Role", value=f"<@&{cfg['role_id']}>" if cfg["role_id"] else "Not set", inline=True)
    embed.add_field(name="Bots", value="Get the role" if cfg["include_bots"] else "Skipped", inline=True)
    exempt = cfg["exempt_users"]
    embed.add_field(name=f"Left out ({len(exempt)})", inline=False,
                    value=" ".join(f"<@{u}>" for u in exempt[:20]) + (f" and {len(exempt) - 20} more" if len(exempt) > 20 else "") if exempt else "Nobody. Add people with `/autorole exempt @user`.")
    embed.set_footer(text="/autorole <role> · exempt <@users> · unexempt <@users> · bots <on|off> · off")
    return embed


def _autorole_view(server_id, owner_id):
    cfg = get_autorole(server_id)
    view = discord.ui.View(timeout=300)

    def add(label, style, action):
        button = discord.ui.Button(label=label, style=style)

        async def pressed(click):
            if str(click.user.id) != str(owner_id) or not is_admin(server_id, click.user.id):
                await click.response.send_message("Only the admin who opened this panel can use it.", ephemeral=True)
                return
            current = get_autorole(server_id)
            note = action(current)
            current = save_autorole(server_id, current)
            await click.response.edit_message(embed=_autorole_embed(current, click.user, note=note), view=_autorole_view(server_id, owner_id))
        button.callback = pressed
        view.add_item(button)

    def toggle(c):
        c["enabled"] = not c["enabled"]
        return "Autorole is now " + ("on." if c["enabled"] else "off.")

    def bots(c):
        c["include_bots"] = not c["include_bots"]
        return "Bots " + ("get the role." if c["include_bots"] else "are skipped.")

    def existing(c):
        return f"Gave the role to {assign_autorole_to_existing(server_id, c)} existing members."
    add("Turn off" if cfg["enabled"] else "Turn on", discord.ButtonStyle.danger if cfg["enabled"] else discord.ButtonStyle.primary, toggle)
    add("Skip bots" if cfg["include_bots"] else "Include bots", discord.ButtonStyle.secondary, bots)
    if cfg["role_id"]:
        add("Give to existing members", discord.ButtonStyle.secondary, existing)
    return view


@bot.tree.command(name="autorole", description="Give new members a role: /autorole <role> | exempt <@users> | unexempt <@users> | bots <on|off> | off")
async def autorole(interaction: discord.Interaction, action: str = "status", *, rest: str = ""):
    if not await need_admin(interaction):
        return
    sid = str(interaction.guild_id)
    cfg = get_autorole(sid)
    word = action.lower().strip()
    note = None

    async def say(text):
        await interaction.response.send_message(text, ephemeral=True)

    if word in ("status", "", "panel"):
        pass
    elif word in ("off", "disable"):
        cfg["enabled"], note = False, "Autorole is off."
    elif word in ("on", "enable"):
        if not cfg["role_id"]:
            return await say("Pick a role first: `/autorole <role>`")
        cfg["enabled"], note = True, "Autorole is on."
    elif word in ("exempt", "skip", "ignore"):
        ids = _user_ids(rest)
        if not ids:
            return await say("Usage: `/autorole exempt @user @other` (one person or several)")
        cfg["exempt_users"] = list(dict.fromkeys(cfg["exempt_users"] + ids))
        note = f"Left out {len(ids)} {'person' if len(ids) == 1 else 'people'}."
    elif word in ("unexempt", "include", "unskip"):
        ids = set(_user_ids(rest))
        if not ids:
            return await say("Usage: `/autorole unexempt @user`")
        cfg["exempt_users"] = [u for u in cfg["exempt_users"] if u not in ids]
        note = f"Removed {len(ids)} from the left-out list."
    elif word == "bots":
        state = rest.strip().lower()
        if state not in ("on", "off", "true", "false"):
            return await say("Usage: `/autorole bots on` (bots get the role) or `/autorole bots off`")
        cfg["include_bots"] = state in ("on", "true")
        note = "Bots " + ("get the role." if cfg["include_bots"] else "are skipped.")
    else:
        # /autorole <role> [true|false]: the original form, the role name is the first word (or in quotes)
        token = (action + " " + rest).strip()
        enable = True
        if token.lower().endswith((" false", " off")):
            enable, token = False, token.rsplit(" ", 1)[0]
        elif token.lower().endswith((" true", " on")):
            token = token.rsplit(" ", 1)[0]
        try:
            role = resolve_role(token.strip().strip('"'), sid)
        except ValueError as e:
            return await say(f"{e}. Use `/autorole <role name>`, or `/autorole status` to see the settings.")
        cfg.update(role_id=str(role.id), enabled=enable)
        note = f"Autorole {'on' if enable else 'off'} for {role.name}."
    cfg = save_autorole(sid, cfg)
    await interaction.response.send_message(embed=_autorole_embed(cfg, interaction.user, note=note),
                                            view=_autorole_view(sid, interaction.user.id), ephemeral=True)


def _automod_embed(cfg, user, note=None):
    r, a = cfg["rules"], cfg["actions"]
    on = cfg["enabled"]
    embed = discord.Embed(title="Automod", color=discord.Color.green() if on else discord.Color.red(),
                          description=(f"✅ {note}\n\n" if note else "") + (f"**On** · violations are removed and logged" + (f" in <#{cfg['log_channel']}>" if cfg["log_channel"] else "")
                                                                         if on else "**Off** · nothing is being checked"))

    def line(name, label, detail):
        return f"{'🟢' if r[name]['on'] else '⚫'} **{label}** · {detail}"
    embed.add_field(name="Rules", inline=False, value="\n".join([
        line("words", "Words", f"{len(r['words']['list'])} blocked"),
        line("links", "Links", "any link" + (f" except {', '.join(r['links']['allow'][:4])}" + ("…" if len(r["links"]["allow"]) > 4 else "") if r["links"]["allow"] else "")),
        line("invites", "Invites", "links to other servers"),
        line("caps", "Caps", f"{r['caps']['percent']}%+ of {r['caps']['min_length']}+ letters"),
        line("spam", "Spam", f"{r['spam']['count']} messages in {r['spam']['seconds']}s"),
        line("mentions", "Mentions", f"more than {r['mentions']['limit']}"),
    ]))
    steps = ["Message removed" + (" + warning" if a["warn"] else "")]
    if a["timeout_after"]:
        steps.append(f"Timeout {a['timeout_minutes']} min at {a['timeout_after']} strikes")
    if a["kick_after"]:
        steps.append(f"Kick at {a['kick_after']} strikes")
    embed.add_field(name="Punishment", value=" → ".join(steps) + f"\nStrikes expire after {a['strike_minutes']} min", inline=False)
    if cfg["exempt_channels"] or cfg["exempt_roles"]:
        embed.add_field(name="Exempt", inline=False, value=" ".join([f"<#{c}>" for c in cfg["exempt_channels"][:8]] + [f"<@&{x}>" for x in cfg["exempt_roles"][:8]]))
    embed.set_footer(text="Tap a rule to switch it on or off · /automod help for every option · Admins are never checked")
    return embed


def _automod_view(sid, owner_id):
    """Toggle buttons under the panel. Each press saves and redraws the panel."""
    cfg = get_automod(sid)
    view = discord.ui.View(timeout=300)

    def add(label, on, toggle):
        button = discord.ui.Button(label=label, style=discord.ButtonStyle.success if on else discord.ButtonStyle.secondary)

        async def pressed(click):
            if str(click.user.id) != str(owner_id) or not is_admin(sid, click.user.id):
                await click.response.send_message("Only the admin who opened this panel can change it.", ephemeral=True)
                return
            current = get_automod(sid)
            toggle(current)
            current = save_automod(sid, current)
            await click.response.edit_message(embed=_automod_embed(current, click.user), view=_automod_view(sid, owner_id))
        button.callback = pressed
        view.add_item(button)

    def flip_enabled(c):
        c["enabled"] = not c["enabled"]
    add("Turn off" if cfg["enabled"] else "Turn on", False, flip_enabled)
    view.children[-1].style = discord.ButtonStyle.danger if cfg["enabled"] else discord.ButtonStyle.primary
    for name in automod.RULE_NAMES:
        def flip(c, name=name):
            c["rules"][name]["on"] = not c["rules"][name]["on"]
        add(name.capitalize(), cfg["rules"][name]["on"], flip)
    return view


AUTOMOD_HELP = ("**/automod** (admins only)\n"
                "`status` · `enable` · `disable`\n"
                "`rule <words|links|invites|caps|spam|mentions> <on|off> [number]` (number = caps %, spam messages, or mention limit)\n"
                "`words add|remove|list <word>`\n"
                "`allow <domain>` / `unallow <domain>` for the links rule\n"
                "`log <#channel|off>` · `exempt <#channel>` (toggle) · `exemptrole <role>` (toggle)\n"
                "`timeout <strikes> <minutes>` · `kick <strikes|0>`")


@bot.tree.command(name="automod", description="Configure automod: /automod status, enable, rule, words, allow, log, timeout...")
async def automod_command(interaction: discord.Interaction, action: str = "status", a: str = "", b: str = ""):
    if not await need_admin(interaction):
        return
    sid = str(interaction.guild_id)
    cfg = get_automod(sid)
    action = action.lower().strip()

    async def reply(text):
        await interaction.response.send_message(text, ephemeral=True)

    async def panel(note=None):
        await interaction.response.send_message(embed=_automod_embed(get_automod(sid), interaction.user, note),
                                                view=_automod_view(sid, interaction.user.id), ephemeral=True)

    if action in ("status", "panel", ""):
        return await panel()
    if action in ("enable", "disable"):
        cfg["enabled"] = action == "enable"
    elif action == "rule":
        name, state = a.lower(), b.lower()
        if name not in automod.RULE_NAMES or state not in ("on", "off"):
            return await reply("Usage: `/automod rule <words|links|invites|caps|spam|mentions> <on|off>`")
        cfg["rules"][name]["on"] = state == "on"
    elif action == "set":
        name, rest = a.lower(), b
        keys = {"caps": "percent", "spam": "count", "mentions": "limit"}
        if name not in keys or not rest.isdigit():
            return await reply("Usage: `/automod set <caps|spam|mentions> <number>`")
        cfg["rules"][name][keys[name]] = int(rest)
    elif action == "words":
        sub, word = a.lower(), b.strip().lower()
        words = cfg["rules"]["words"]["list"]
        if sub == "list":
            return await reply("Blocked words: " + (", ".join(f"||{w}||" for w in words) if words else "none yet"))
        if sub not in ("add", "remove") or not word:
            return await reply("Usage: `/automod words add|remove|list <word>`")
        if sub == "add" and word not in words:
            words.append(word)
        if sub == "remove" and word in words:
            words.remove(word)
        try:   # the message with the word in it is not left for everyone to read
            bot.delete(interaction.channel_id, interaction._msg.get("id")) if interaction._msg else None
        except Exception:
            pass
    elif action in ("allow", "unallow"):
        domain = a.lower().replace("https://", "").replace("http://", "").strip("/")
        allow = cfg["rules"]["links"]["allow"]
        if not domain:
            return await reply("Usage: `/automod allow <domain>`")
        if action == "allow" and domain not in allow:
            allow.append(domain)
        if action == "unallow" and domain in allow:
            allow.remove(domain)
    elif action == "log":
        if a.lower() in ("off", "none", ""):
            cfg["log_channel"] = None
        else:
            ch = resolve_channel(sid, a)
            if not ch:
                return await reply("I couldn't find that channel.")
            cfg["log_channel"] = str(ch.get("id"))
    elif action == "exempt":
        ch = resolve_channel(sid, a)
        if not ch:
            return await reply("Usage: `/automod exempt <#channel>` (run again to remove it)")
        cid = str(ch.get("id"))
        cfg["exempt_channels"] = [c for c in cfg["exempt_channels"] if c != cid] if cid in cfg["exempt_channels"] else cfg["exempt_channels"] + [cid]
    elif action == "exemptrole":
        try:
            role = resolve_role(a, sid) if a else None
        except ValueError:
            role = None
        if not role:
            return await reply("Usage: `/automod exemptrole <role>` (run again to remove it). I couldn't find that role.")
        rid = str(role.id)
        cfg["exempt_roles"] = [r for r in cfg["exempt_roles"] if r != rid] if rid in cfg["exempt_roles"] else cfg["exempt_roles"] + [rid]
    elif action == "timeout":
        if not (a.isdigit() and b.isdigit()):
            return await reply("Usage: `/automod timeout <strikes> <minutes>` (0 strikes = never time out)")
        cfg["actions"]["timeout_after"], cfg["actions"]["timeout_minutes"] = int(a), max(1, int(b))
    elif action == "kick":
        if not a.isdigit():
            return await reply("Usage: `/automod kick <strikes>` (0 = never kick)")
        cfg["actions"]["kick_after"] = int(a)
    else:
        embed = discord.Embed(title="Automod help", color=ecolor(interaction.user.id), description=AUTOMOD_HELP)
        return await interaction.response.send_message(embed=embed, ephemeral=True)
    save_automod(sid, cfg)
    await panel("Saved.")


@bot.tree.command(name="serverinfo", description="Show information about this server")
async def serverinfo(interaction: discord.Interaction):
    if interaction.is_dm:
        await interaction.response.send_message("This command only works inside a server.", ephemeral=True)
        return
    sid = str(interaction.guild_id)
    try:
        data = server_summary(sid)
    except Exception as e:
        await interaction.response.send_message(f"Couldn't load the server information: {e}", ephemeral=True)
        return
    embed = discord.Embed(title=data["name"], color=ecolor(interaction.user.id))
    if data["description"]:
        embed.description = data["description"]
    embed.add_field(name="Owner", value=f"<@{data['owner']}>" if data["owner"] else "Unknown", inline=True)
    embed.add_field(name="Server ID", value=f"`{sid}`", inline=True)
    embed.add_field(name="Members", inline=False,
                    value=f"**{data['members']:,}** members" + (f"\n**{data['humans']:,}** people · **{data['bots']:,}** bots" if data["humans"] is not None else ""))
    embed.add_field(name="Channels", value=f"{data['channels']:,} total\n{data['text']:,} text · {data['voice']:,} voice\n{data['categories']:,} categories", inline=True)
    embed.add_field(name="Roles", value=f"{data['roles']:,}", inline=True)
    for label, value in data["extra"]:
        embed.add_field(name=label, value=value, inline=True)
    if data["icon"]:
        embed.set_thumbnail(url=data["icon"])
    if data["banner"]:
        embed.set_image(url=data["banner"])
    elif data["icon"]:
        embed.set_image(url=data["icon"])      # Yapin gives servers no banner, so the picture is shown large instead
    embed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
    await interaction.response.send_message(embed=embed)


class ServerTools(yapper.Cog):
    name = "Server tools"


def setup(bot):
    bot.add_cog(ServerTools(bot))

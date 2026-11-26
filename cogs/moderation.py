"""Cog: moderation: /kick /ban /unban /timeout /untimeout /purge /slowmode /lock /unlock /nick /warn /warnings /clearwarnings."""
import sys
import time
import yapper

m = sys.modules.get("main") or sys.modules["__main__"]   # this bot's main.py: shared helpers, data and the bot itself
MainURL = m.MainURL
bot = m.bot
discord = m.discord
app_commands = m.app_commands
ecolor = m.ecolor
is_admin = m.is_admin
is_server_owner = m.is_server_owner
YapperError = m.YapperError
warnsDB = m.warnsDB            # "server:user" -> [{"at": epoch, "by": id, "reason": text}, ...]

MAX_WARNS_SHOWN = 15
NAMES = {"kick_members": "Kick members", "ban_members": "Ban members", "moderate_members": "Timeout members", "manage_messages": "Manage messages",
         "manage_channels": "Manage channels", "manage_nicknames": "Manage nicknames"}


def can(sid, uid, perm):
    """Server owners, administrators and bot owners can do anything; others need the specific permission (when Yapin' reports it)."""
    if is_admin(sid, uid):
        return True
    try:
        got = bot.get_permissions(sid, uid)
        return bool(got.get("owner") or got.get("administrator") or (got.get("permissions") or {}).get(perm))
    except Exception:
        return False


def _embed(interaction, title, description):
    embed = discord.Embed(title=title, description=description, color=ecolor(interaction.user.id))
    embed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
    return embed


async def _say(interaction, text, ephemeral=True):
    await interaction.response.send_message(embed=discord.Embed(description=text, color=discord.Color.red() if text.startswith("❌") else discord.Color.green()), ephemeral=ephemeral)


async def _gate(interaction, perm):
    """True when this is a server and the person may use the command; otherwise answers with why."""
    if interaction.is_dm:
        await _say(interaction, "❌ This command only works in a server.")
        return False
    if not can(str(interaction.guild_id), interaction.user.id, perm):
        await _say(interaction, f"❌ You need the **{NAMES.get(perm, perm)}** permission for that.")
        return False
    return True


async def _target_ok(interaction, target):
    """True if `target` may be acted on (not yourself, the owner, Shapes or, unless you own the server, another admin)."""
    sid, uid = str(interaction.guild_id), str(target.id)
    if uid == str(interaction.user.id):
        await _say(interaction, "❌ You can't do that to yourself.")
    elif uid == str((bot.me or {}).get("id")):
        await _say(interaction, "❌ I won't do that to myself.")
    elif is_server_owner(sid, uid):
        await _say(interaction, "❌ Nobody can do that to the server owner.")
    elif is_admin(sid, uid) and not is_server_owner(sid, interaction.user.id) and str(interaction.user.id) not in m.OWNER_IDS:
        await _say(interaction, "❌ Only the server owner can do that to an admin.")
    else:
        return True
    return False


def _reason(text):
    return (text or "No reason given")[:400]


async def _try(interaction, what, fn):
    """Run an API call; on failure tell the person why (Yapin' says when the bot lacks a permission or the person is above it)."""
    try:
        fn()
        return True
    except YapperError as e:
        await _say(interaction, f"❌ Yapin' wouldn't let me {what} ({e}). Check that my role has the permission and sits above theirs.")
    return False


def _dm(uid, text):
    try:
        bot.send(bot.dm(uid), text)
    except Exception:
        pass


def _server_name(sid):
    try:
        return m.cached(f"server:{sid}", 30, lambda: bot.server(sid)).get("name") or "the server"
    except Exception:
        return "the server"


@bot.tree.command(name="kick", description="Kick a member: /kick @user [reason]")
async def kick(interaction: discord.Interaction, user: discord.User, reason: str = ""):
    if not await _gate(interaction, "kick_members") or not await _target_ok(interaction, user):
        return
    sid = str(interaction.guild_id)
    _dm(user.id, f"You were kicked from **{_server_name(sid)}**. Reason: {_reason(reason)}")
    if await _try(interaction, "kick them", lambda: bot.kick(sid, str(user.id))):
        await interaction.response.send_message(embed=_embed(interaction, "👢 Kicked", f"<@{user.id}> was kicked.\n**Reason:** {_reason(reason)}"))


@bot.tree.command(name="ban", description="Ban a member: /ban @user [reason]")
async def ban(interaction: discord.Interaction, user: discord.User, reason: str = ""):
    if not await _gate(interaction, "ban_members") or not await _target_ok(interaction, user):
        return
    sid = str(interaction.guild_id)
    _dm(user.id, f"You were banned from **{_server_name(sid)}**. Reason: {_reason(reason)}")
    if await _try(interaction, "ban them", lambda: bot.ban(sid, str(user.id))):
        await interaction.response.send_message(embed=_embed(interaction, "🔨 Banned", f"<@{user.id}> was banned.\n**Reason:** {_reason(reason)}"))


@bot.tree.command(name="unban", description="Unban someone by their user ID: /unban <id>")
async def unban(interaction: discord.Interaction, user_id: str):
    if not await _gate(interaction, "ban_members"):
        return
    user_id = user_id.strip().lstrip("<@!").rstrip(">")
    if not user_id:
        return await _say(interaction, "❌ Give the user's ID.")
    if await _try(interaction, "unban them", lambda: bot.unban(str(interaction.guild_id), user_id)):
        await interaction.response.send_message(embed=_embed(interaction, "✅ Unbanned", f"<@{user_id}> was unbanned."))


@bot.tree.command(name="timeout", description="Time a member out: /timeout @user <minutes> [reason]")
async def timeout(interaction: discord.Interaction, user: discord.User, minutes: int, reason: str = ""):
    if not await _gate(interaction, "moderate_members") or not await _target_ok(interaction, user):
        return
    if not 1 <= minutes <= 40320:
        return await _say(interaction, "❌ Minutes must be between 1 and 40320 (28 days).")
    if await _try(interaction, "time them out", lambda: bot.timeout(str(interaction.guild_id), str(user.id), minutes, _reason(reason))):
        until = int(time.time()) + minutes * 60
        await interaction.response.send_message(embed=_embed(interaction, "🔇 Timed out", f"<@{user.id}> is timed out until <t:{until}:f> (<t:{until}:R>).\n**Reason:** {_reason(reason)}"))


@bot.tree.command(name="untimeout", description="Remove a member's timeout: /untimeout @user")
async def untimeout(interaction: discord.Interaction, user: discord.User):
    if not await _gate(interaction, "moderate_members"):
        return
    if await _try(interaction, "remove the timeout", lambda: bot.timeout(str(interaction.guild_id), str(user.id), 0)):
        await interaction.response.send_message(embed=_embed(interaction, "🔊 Timeout removed", f"<@{user.id}> can talk again."))


@bot.tree.command(name="purge", description="Delete recent messages here: /purge <amount 1-100> [@user]")
async def purge(interaction: discord.Interaction, amount: int, user: discord.User = None):
    if not await _gate(interaction, "manage_messages"):
        return
    if not 1 <= amount <= 100:
        return await _say(interaction, "❌ Amount must be between 1 and 100.")
    try:
        # look at the newest `amount` messages (or, with a user, a wider window so their last `amount` can be found)
        recent = bot.messages(interaction.channel_id, 100 if user else amount)
        if user:
            recent = [x for x in recent if str((x.get("author") or {}).get("id") or x.get("author_id")) == str(user.id)]
        ids = [str(x.get("id")) for x in recent[-amount:] if x.get("id")]
        if not ids:
            return await _say(interaction, "Nothing to delete.")
        done = bot.bulk_delete(interaction.channel_id, ids)
    except YapperError as e:
        return await _say(interaction, f"❌ I couldn't delete them ({e}). I need the Manage messages permission here.")
    n = (done or {}).get("deleted", len(ids)) if isinstance(done, dict) else len(ids)
    await _say(interaction, f"🧹 Deleted **{n}** message{'s' if n != 1 else ''}" + (f" from <@{user.id}>" if user else "") + ".")


@bot.tree.command(name="slowmode", description="Set this channel's slowmode (0 turns it off): /slowmode <seconds>")
async def slowmode(interaction: discord.Interaction, seconds: int):
    if not await _gate(interaction, "manage_channels"):
        return
    if not 0 <= seconds <= 21600:
        return await _say(interaction, "❌ Seconds must be between 0 and 21600.")
    if await _try(interaction, "change this channel", lambda: bot.edit_channel(interaction.channel_id, slowmode=seconds)):
        await interaction.response.send_message(embed=_embed(interaction, "🐌 Slowmode", "Slowmode is **off**." if not seconds else f"People can now post once every **{seconds}s**."))


@bot.tree.command(name="lock", description="Stop members sending messages in this channel")
async def lock(interaction: discord.Interaction):
    if not await _gate(interaction, "manage_channels"):
        return
    if await _try(interaction, "lock this channel", lambda: bot.edit_channel(interaction.channel_id, read_only=True)):
        await interaction.response.send_message(embed=_embed(interaction, "🔒 Locked", "Members can't send messages here until it's unlocked (`/unlock`)."))


@bot.tree.command(name="unlock", description="Let members send messages in this channel again")
async def unlock(interaction: discord.Interaction):
    if not await _gate(interaction, "manage_channels"):
        return
    if await _try(interaction, "unlock this channel", lambda: bot.edit_channel(interaction.channel_id, read_only=False)):
        await interaction.response.send_message(embed=_embed(interaction, "🔓 Unlocked", "Members can send messages here again."))


@bot.tree.command(name="nick", description="Change a nickname: /nick @user [new nickname]")
async def nick(interaction: discord.Interaction, user: discord.User, nickname: str = ""):
    if not await _gate(interaction, "manage_nicknames"):
        return
    if str(user.id) != str(interaction.user.id) and not await _target_ok(interaction, user):
        return
    if await _try(interaction, "change that nickname", lambda: bot.set_nickname(str(interaction.guild_id), str(user.id), nickname[:32])):
        await interaction.response.send_message(embed=_embed(interaction, "✏️ Nickname", f"<@{user.id}>'s nickname " + (f"is now **{nickname[:32]}**." if nickname else "was cleared.")))


def _key(sid, uid):
    return f"{sid}:{uid}"


@bot.tree.command(name="warn", description="Warn a member (kept on record, they get a DM): /warn @user <reason>")
async def warn(interaction: discord.Interaction, user: discord.User, reason: str):
    if not await _gate(interaction, "moderate_members") or not await _target_ok(interaction, user):
        return
    if user.is_bot:
        return await _say(interaction, "❌ Bots can't be warned.")
    sid = str(interaction.guild_id)
    rows = list(warnsDB.get(_key(sid, user.id)) or [])
    rows.append({"at": int(time.time()), "by": str(interaction.user.id), "reason": _reason(reason)})
    warnsDB.set(_key(sid, user.id), rows[-100:])
    warnsDB.save()
    _dm(user.id, f"You were warned in **{_server_name(sid)}**. Reason: {_reason(reason)}")
    await interaction.response.send_message(embed=_embed(interaction, "⚠️ Warned", f"<@{user.id}> now has **{len(rows)}** warning{'s' if len(rows) != 1 else ''}.\n**Reason:** {_reason(reason)}"))


@bot.tree.command(name="warnings", description="See a member's warnings: /warnings @user")
async def warnings(interaction: discord.Interaction, user: discord.User):
    if not await _gate(interaction, "moderate_members"):
        return
    rows = warnsDB.get(_key(interaction.guild_id, user.id)) or []
    if not rows:
        return await interaction.response.send_message(embed=_embed(interaction, "Warnings", f"<@{user.id}> has no warnings."), ephemeral=True)
    lines = [f"**{i}.** <t:{r['at']}:d> by <@{r['by']}>: {r['reason']}" for i, r in enumerate(rows[-MAX_WARNS_SHOWN:], max(1, len(rows) - MAX_WARNS_SHOWN + 1))]
    await interaction.response.send_message(embed=_embed(interaction, f"Warnings: {len(rows)}", f"<@{user.id}>\n\n" + "\n".join(lines)), ephemeral=True)


@bot.tree.command(name="clearwarnings", description="Remove all of a member's warnings: /clearwarnings @user")
async def clearwarnings(interaction: discord.Interaction, user: discord.User):
    if not await _gate(interaction, "moderate_members"):
        return
    count = len(warnsDB.get(_key(interaction.guild_id, user.id)) or [])
    warnsDB.remove(_key(interaction.guild_id, user.id))
    warnsDB.save()
    await interaction.response.send_message(embed=_embed(interaction, "🧽 Warnings cleared", f"Removed {count} warning{'s' if count != 1 else ''} from <@{user.id}>."))


class Moderation(yapper.Cog):
    name = "Moderation"


def setup(bot):
    bot.add_cog(Moderation(bot))

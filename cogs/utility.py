"""Cog: server utilities: /userinfo /roleinfo /membercount /poll /remind."""
import re
import sys
import threading
import time
import yapper

m = sys.modules.get("main") or sys.modules["__main__"]   # this bot's main.py: shared helpers, data and the bot itself
MainURL = m.MainURL
bot = m.bot
discord = m.discord
app_commands = m.app_commands
ecolor = m.ecolor
Table = m.Table
remindersDB = Table("reminders")       # id -> {"user", "channel", "at", "text"}

NUMBER_EMOJI = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
MAX_REMINDERS = 10                      # per person
MAX_SECONDS = 365 * 86400


def parse_duration(text):
    """'1h30m', '2d', '45 minutes', '90s' -> seconds, or None when it can't be read."""
    low = str(text).lower()
    parts = re.findall(r"(\d+)\s*(d|day|days|h|hr|hrs|hour|hours|m|min|mins|minute|minutes|s|sec|secs|second|seconds)(?![a-z])", low)
    if not parts or re.sub(r"(\d+)\s*([a-z]+)|\s+", "", low):
        return None
    unit = {"d": 86400, "h": 3600, "m": 60, "s": 1}
    return sum(int(n) * unit[u[0]] for n, u in parts)


def _embed(interaction, title=None, description=None):
    embed = discord.Embed(title=title, description=description, color=ecolor(interaction.user.id))
    embed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
    return embed


def _ts(value):
    """A join/creation time (ISO text or epoch) as epoch seconds, or None."""
    from datetime import datetime
    if value in (None, ""):
        return None
    try:
        n = float(value)
        return int(n / 1000 if n > 1e11 else n)
    except (TypeError, ValueError):
        pass
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


async def _server_only(interaction):
    if interaction.is_dm:
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return False
    return True


@bot.tree.command(name="userinfo", description="Information about a member of this server: /userinfo [@user]")
async def userinfo(interaction: discord.Interaction, user: discord.User = None):
    if not await _server_only(interaction):
        return
    sid = str(interaction.guild_id)
    user = user or interaction.user
    try:
        person = m._person(dict(bot.member(sid, user.id)))
    except Exception:
        person = m._person({"id": user.id, "username": user.name})
    try:
        names = {str(r.get("id")): r.get("name") for r in m.cached(f"roles:{sid}", 30, lambda: bot.roles(sid))}
    except Exception:
        names = {}
    embed = _embed(interaction, person["display"] + ("  ·  🤖 Bot" if person["bot"] else ""))
    embed.add_field(name="Username", value=f"{person['name']} (`{person['id']}`)", inline=False)
    joined = _ts(person["joined"])
    if joined:
        embed.add_field(name="Joined this server", value=f"<t:{joined}:D>\n<t:{joined}:R>", inline=True)
    roles = [f"<@&{r}>" if r in names else r for r in person["roles"]]
    embed.add_field(name=f"Roles ({len(roles)})", value=(" ".join(roles[:20]) + (f" and {len(roles) - 20} more" if len(roles) > 20 else "")) if roles else "None", inline=False)
    if person.get("avatar"):
        embed.set_thumbnail(url=person["avatar"])
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="roleinfo", description="Information about a role: /roleinfo <role>")
async def roleinfo(interaction: discord.Interaction, role: discord.Role):
    if not await _server_only(interaction):
        return
    sid = str(interaction.guild_id)
    data = {**role.data}
    embed = _embed(interaction, role.name)
    embed.add_field(name="ID", value=f"`{role.id}`", inline=True)
    try:
        members = m.cached(f"allmembers:{sid}", 120, lambda: m._all_members(sid))
        embed.add_field(name="Members", value=f"{sum(1 for p in members if str(role.id) in p['roles']):,}", inline=True)
    except Exception:
        pass
    for key, label in (("color", "Colour"), ("position", "Position")):
        if data.get(key) not in (None, ""):
            embed.add_field(name=label, value=str(data[key]), inline=True)
    for key, label in (("hoist", "Shown separately"), ("mentionable", "Mentionable")):
        if key in data:
            embed.add_field(name=label, value="Yes" if data[key] else "No", inline=True)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="membercount", description="How many people are in this server")
async def membercount(interaction: discord.Interaction):
    if not await _server_only(interaction):
        return
    sid = str(interaction.guild_id)
    info = dict(m.cached(f"server:{sid}", 30, lambda: bot.server(sid)))
    members, people, bots = m.server_counts(sid, m._total_of(info))
    text = f"**{members:,}** members" if members is not None else "I couldn't count the members right now."
    if bots is not None and people is not None:
        text += f"\n**{people:,}** people · **{bots:,}** bots"
    await interaction.response.send_message(embed=_embed(interaction, info.get("name") or "Members", text))


@bot.tree.command(name="poll", description="Start a poll people vote on with reactions: /poll <question> [choice | choice | ...]")
async def poll(interaction: discord.Interaction, question: str, options: str = ""):
    # The question is one word at a time in a typed command, so a question with spaces goes in quotes: /poll "Pizza or tacos?" pizza | tacos
    choices = [o.strip() for o in options.split("|") if o.strip()]
    if options and not 2 <= len(choices) <= 10:
        return await interaction.response.send_message("Give between 2 and 10 choices separated by `|`, like `/poll \"Lunch?\" pizza | tacos | sushi`.", ephemeral=True)
    emojis = NUMBER_EMOJI[:len(choices)] if choices else ["👍", "👎"]
    body = "\n".join(f"{e} {c[:200]}" for e, c in zip(emojis, choices)) if choices else "React with 👍 or 👎"
    sent = await interaction.response.send_message(embed=_embed(interaction, f"📊 {question[:250]}", body))
    mid = getattr(sent, "id", None)
    for emoji in emojis if mid else []:
        try:
            bot.react(interaction.channel_id, mid, emoji)
        except Exception as e:
            print(f"[poll] couldn't react {emoji}: {e}")
            break


@bot.tree.command(name="remind", description="Remind you of something later: /remind <10m|2h30m|1d> <what>")
async def remind(interaction: discord.Interaction, when: str, text: str):
    seconds = parse_duration(when)
    if not seconds or seconds > MAX_SECONDS:
        return await interaction.response.send_message("Say how long with numbers and units, like `10m`, `2h30m` or `1d` (up to a year).", ephemeral=True)
    mine = [k for k, v in remindersDB.items().items() if str(v.get("user")) == str(interaction.user.id)]
    if len(mine) >= MAX_REMINDERS:
        return await interaction.response.send_message(f"You already have {MAX_REMINDERS} reminders waiting.", ephemeral=True)
    at = int(time.time()) + seconds
    remindersDB.set(f"{interaction.user.id}:{at}:{int(time.time() * 1000) % 100000}", {"user": str(interaction.user.id), "channel": str(interaction.channel_id or ""), "at": at, "text": text[:500]})
    remindersDB.save()
    await interaction.response.send_message(embed=_embed(interaction, "⏰ Reminder set", f"I'll remind you <t:{at}:R> (<t:{at}:f>):\n> {text[:500]}"), ephemeral=True)


def _deliver(row):
    late = time.time() - row["at"]
    text = f"⏰ <@{row['user']}> Reminder: {row['text']}" + ("\n(Sent late: the bot was offline.)" if late > 120 else "")
    try:
        bot.send(bot.dm(row["user"]), text)
        return
    except Exception:
        pass
    if row.get("channel"):
        try:
            bot.send(row["channel"], text)
        except Exception as e:
            print(f"[remind] couldn't deliver: {e}")


def check_reminders():
    """Send every reminder that is due (also run at start, so ones that came due while the bot was off still go out)."""
    now = time.time()
    for key, row in list(remindersDB.items().items()):
        if row.get("at", 0) <= now:
            _deliver(row)
            remindersDB.remove(key)
            remindersDB.save()


def _loop():
    time.sleep(10)
    while True:
        try:
            check_reminders()
        except Exception as e:
            print(f"[remind] {type(e).__name__}: {e}")
        time.sleep(15)


class Utility(yapper.Cog):
    name = "Utility"

    @yapper.listener("ready")
    def on_ready(self, *_):
        if not getattr(m, "_reminder_thread", None):
            m._reminder_thread = threading.Thread(target=_loop, name="reminders", daemon=True)
            m._reminder_thread.start()


def setup(bot):
    bot.add_cog(Utility(bot))

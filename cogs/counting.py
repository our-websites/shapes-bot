"""Cog: the counting game: /counting and /countingleaderboard."""
import sys
import yapper

m = sys.modules.get("main") or sys.modules["__main__"]   # this bot's main.py: shared helpers, data and the bot itself
COUNTING_EMOJI_DEFAULT = m.COUNTING_EMOJI_DEFAULT
MainURL = m.MainURL
bot = m.bot
clean_emoji = m.clean_emoji
countingDB = m.countingDB
counting_emojis = m.counting_emojis
counting_limit = m.counting_limit
default_counting = m.default_counting
discord = m.discord
ecolor = m.ecolor
fetch_user = m.fetch_user
is_admin = m.is_admin
is_owner = m.is_owner
resolve_channel = m.resolve_channel


COUNTING_EMOJI_NAMES = {"correct": "correct", "right": "correct", "good": "correct", "warning": "warning", "warn": "warning",
                        "wrong": "wrong", "incorrect": "wrong", "ruined": "wrong", "fail": "wrong"}


def _counter_label(user_id):
    """'<@id> (@username)' for the last person to count, or None."""
    if not user_id:
        return None
    name = fetch_user(user_id).get("username")
    return f"<@{user_id}>" + (f" (@{name})" if name else "")


def counting_stats_embed(data, color, requester):
    """What anyone may see: how far the count is. No settings."""
    nxt = data.get("number", 0) + 1
    embed = discord.Embed(title="Counting", color=color)
    embed.add_field(name="Current Number", value=f"`{data.get('number', 0):,}`", inline=True)
    embed.add_field(name="Highest Number", value=f"`{data.get('highestnumber', 0):,}`", inline=True)
    embed.add_field(name="Next Number", value=f"`{nxt:,}`", inline=True)
    if data.get("lastcounter"):
        embed.add_field(name="Last Counter", value=_counter_label(data["lastcounter"]), inline=False)
    if data.get("channel"):
        embed.add_field(name="Channel", value=f"<#{data['channel']}>", inline=False)
    embed.set_footer(text=f"Requested By {requester} | {MainURL}")
    return embed


@bot.tree.command(name="counting", description="Counting: stats for everyone; admins can use /counting [toggle|channel|emoji]")
async def counting(interaction: discord.Interaction, action: str = "", value: str = ""):
    if interaction.is_dm:
        await interaction.response.send_message("This command only works inside a server.", ephemeral=True)
        return
    sid = str(interaction.guild_id)
    data = {**default_counting(), **(countingDB.get(sid) or {})}
    is_staff = is_admin(interaction.guild_id, interaction.user.id)
    if action.lower() in ("warnings", "maxwarnings"):       # bot owners only, in any server
        if not is_owner(interaction.user):
            await interaction.response.send_message("Only the bot owner can change how many warnings counting gives.", ephemeral=True)
            return
        try:
            amount = int(str(value).strip())
            if not 0 <= amount <= 50:
                raise ValueError
        except ValueError:
            await interaction.response.send_message(f"Usage: `/counting warnings <0-50>` (now **{counting_limit(data)}**; 0 ruins the count on the first mistake).", ephemeral=True)
            return
        data["max_warnings"] = amount
        countingDB.set(sid, data)
        await interaction.response.send_message(f"This server now gets **{amount}** warning{'s' if amount != 1 else ''} before the count is ruined.", ephemeral=True)
        return
    if not is_staff:
        if not data.get("channel") and not data.get("enabled"):
            await interaction.response.send_message("Counting isn't set up in this server.", ephemeral=True)
            return
        await interaction.response.send_message(embed=counting_stats_embed(data, ecolor(interaction.user.id), interaction.user.name))
        return
    action = action.lower()
    if action == "toggle":
        data["enabled"] = not data["enabled"]
        countingDB.set(sid, data)
        await interaction.response.send_message(f"Counting {'enabled' if data['enabled'] else 'disabled'}.", ephemeral=True)
        return
    if action == "channel":
        ch = resolve_channel(sid, value) if value else None
        if not ch:
            await interaction.response.send_message("Tell me which text channel: `/counting channel #general`", ephemeral=True)
            return
        data["channel"] = str(ch.get("id"))
        countingDB.set(sid, data)
        await interaction.response.send_message(f"Counting channel set to <#{ch.get('id')}>.", ephemeral=True)
        return
    if action in ("emoji", "emojis"):
        parts = value.split(None, 1)
        kind = COUNTING_EMOJI_NAMES.get(parts[0].lower()) if parts else None
        if parts and parts[0].lower() == "reset":
            data["emojis"] = dict(COUNTING_EMOJI_DEFAULT)
            countingDB.set(sid, data)
            await interaction.response.send_message("Counting emoji are back to the defaults.", ephemeral=True)
            return
        emoji = clean_emoji(parts[1]) if len(parts) > 1 else None
        if not kind or not emoji:
            await interaction.response.send_message("Usage: `/counting emoji <correct|warning|wrong> <emoji>` or `/counting emoji reset`", ephemeral=True)
            return
        data["emojis"] = {**counting_emojis(data), kind: emoji}
        countingDB.set(sid, data)
        await interaction.response.send_message(f"The **{kind}** emoji is now {emoji}", ephemeral=True)
        return
    countingDB.set(sid, data)
    emo = counting_emojis(data)
    channel = f"<#{data['channel']}>" if data["channel"] else "not set"
    embed = counting_stats_embed(data, ecolor(interaction.user.id), interaction.user.name)
    embed.title = "Counting Settings"
    embed.add_field(name="Enabled", value=f"`{data['enabled']}`", inline=True)
    embed.add_field(name="Warnings", value=f"`{data['warnings']}`/{counting_limit(data)}", inline=True)
    embed.add_field(name="Emoji", value=f"Correct {emo['correct']}  ·  Warning {emo['warning']}  ·  Wrong {emo['wrong']}", inline=False)
    embed.add_field(name="Change it", value="`/counting toggle` · `/counting channel #channel` · `/counting emoji <correct|warning|wrong> <emoji>`", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="countingleaderboard", description="View the counting leaderboard website")
async def countingleaderboard(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Counting Leaderboard",
        description=f"[Click here to view the counting leaderboard]({MainURL}/counting/leaderboard/)",
        color=ecolor(interaction.user.id))
    embed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
    await interaction.response.send_message(embed=embed)


class Counting(yapper.Cog):
    name = "Counting"


def setup(bot):
    bot.add_cog(Counting(bot))

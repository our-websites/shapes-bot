"""Cog: Roblox group lookups (/groupinfo). Accepts a group ID, a roblox.com group link, or a name to search for."""
import asyncio
import re
from datetime import datetime

import aiohttp
import yapper

UA = {"user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
      "accept": "application/json"}
GROUP_URL = re.compile(r"roblox\.com/(?:[a-z-]+/)?(?:groups|communities)/(\d+)", re.I)


async def _json(session, url):
    try:
        async with session.get(url, headers=UA, timeout=aiohttp.ClientTimeout(total=12)) as r:
            data = await r.json(content_type=None)
            return (data if r.status < 400 else None), r.status
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return None, 0


async def find_group_id(session, query):
    """(group id, error text). Accepts an ID, a group link, or a name (the best match wins, an exact name first)."""
    q = (query or "").strip()
    if q.isdigit():
        return q, None
    m = GROUP_URL.search(q)
    if m:
        return m.group(1), None
    if not q:
        return None, "Give a group ID, a roblox.com group link, or a group name."
    data, status = await _json(session, f"https://groups.roblox.com/v1/groups/search?keyword={aiohttp.helpers.quote(q, safe='')}&limit=10")
    rows = (data or {}).get("data") or []
    if not rows:
        return None, "Roblox couldn't be reached right now." if status in (0, 429) or status >= 500 else f"I couldn't find a group called **{q}**."
    best = next((g for g in rows if str(g.get("name", "")).lower() == q.lower()), rows[0])
    return str(best["id"]), None


async def fetch_group(group_id):
    """(info dict, error text) for one group: details, creation date, icon and role count."""
    async with aiohttp.ClientSession() as session:
        gid, err = await find_group_id(session, group_id)
        if err:
            return None, err
        base, status = await _json(session, f"https://groups.roblox.com/v1/groups/{gid}")
        if not base or not base.get("id"):
            return None, "Roblox couldn't be reached right now." if status in (0, 429) or status >= 500 else f"I couldn't find a group with the ID `{gid}`."
        created, icon, roles = await asyncio.gather(
            _json(session, f"https://groups.roblox.com/v2/groups?groupIds={gid}"),
            _json(session, f"https://thumbnails.roblox.com/v1/groups/icons?groupIds={gid}&size=420x420&format=Png"),
            _json(session, f"https://groups.roblox.com/v1/groups/{gid}/roles"))
        v2 = next(iter((created[0] or {}).get("data") or []), {}) or {}
        base["created"] = v2.get("created")
        base["icon"] = next(iter((icon[0] or {}).get("data") or []), {}).get("imageUrl")
        base["role_count"] = len((roles[0] or {}).get("roles") or []) or None
        return base, None


def _date(iso):
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except ValueError:
        return None


def group_embed(discord, info, color):
    """The embed for a fetch_group() result."""
    gid = info["id"]
    verified = " ✅" if info.get("hasVerifiedBadge") else ""
    desc = (info.get("description") or "").strip()
    embed = discord.Embed(title=f"{info.get('name')}{verified}", url=f"https://www.roblox.com/communities/{gid}", color=color,
                          description=(desc[:600] + ("…" if len(desc) > 600 else "")) if desc else None)
    owner = info.get("owner") or {}
    if owner.get("userId"):
        embed.add_field(name="Owner", value=f"[{owner.get('displayName') or owner.get('username')}](https://www.roblox.com/users/{owner['userId']}/profile)\n@{owner.get('username')}", inline=True)
    else:
        embed.add_field(name="Owner", value="Nobody (unowned)", inline=True)
    embed.add_field(name="Members", value=f"{int(info.get('memberCount') or 0):,}", inline=True)
    embed.add_field(name="Group ID", value=f"`{gid}`", inline=True)
    created = _date(info.get("created"))
    if created:
        embed.add_field(name="Created", value=created, inline=True)
    embed.add_field(name="Join", value="Anyone can join" if info.get("publicEntryAllowed") else "Join requests (approval needed)", inline=True)
    if info.get("role_count"):
        embed.add_field(name="Roles", value=str(info["role_count"]), inline=True)
    if info.get("isLocked"):
        embed.add_field(name="Status", value="🔒 Locked by Roblox", inline=False)
    shout = info.get("shout") or {}
    if shout.get("body"):
        who = (shout.get("poster") or {}).get("username")
        when = _date(shout.get("updated"))
        embed.add_field(name="Shout", value=shout["body"][:500] + (f"\n— {who}" if who else "") + (f", {when}" if when else ""), inline=False)
    if info.get("icon"):
        embed.set_thumbnail(url=info["icon"])
    embed.set_footer(text="Roblox group")
    return embed


def init(m):
    bot, discord, app_commands = m.bot, m.discord, m.app_commands

    @bot.tree.command(name="groupinfo", description="Get a Roblox group's info (ID, link or name).")
    @app_commands.describe(group="The group's ID, link or name")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def groupinfo(interaction: discord.Interaction, group: str):
        await interaction.response.defer(thinking=True)
        info, err = await fetch_group(group)
        if err:
            return await interaction.followup.send(err)
        color = m.embedDB.get(f"{interaction.user.id}") or discord.Color.blue()
        await interaction.followup.send(embed=group_embed(discord, info, color))


class Groups(yapper.Cog):
    name = "Groups"

    def cog_load(self):
        init(self.bot.core)


def setup(bot):
    bot.add_cog(Groups(bot))

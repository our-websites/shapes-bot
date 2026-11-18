"""Cog: Last.fm: link an account or see what someone is listening to."""
import sys
from discord import app_commands
from discord.ext import commands
import asyncio
import discord
import urllib.parse

m = sys.modules.get("main") or sys.modules["__main__"]   # this bot's main.py: shared helpers, data and the bot itself
API_DOWN_TEXT = m.API_DOWN_TEXT
MainURL = m.MainURL
_embed_color = m._embed_color
_shapes_call = m._shapes_call
api_down = m.api_down


# --- Last.fm (/lastfm link, /lastfm unlink, /lastfm user): links are kept by the Shapes API per platform ("discord" here) ---
LASTFM_LINK_TTL = 600


def lastfm_name_of(user_id):
    data = _shapes_call("GET", f"/internal/links/{user_id}?platform=discord") or {}
    return (data.get("lastfm") or {}).get("name")


def lastfm_line(p):
    scrobbles = f"{int(p['scrobbles']):,}" if str(p.get("scrobbles") or "").isdigit() else "?"
    text = f"[{p['name']}]({p['url']}) · {scrobbles} scrobbles"
    if p.get("track"):
        text += f"\n{'🎶 Now playing' if p.get('now') else '⏮️ Last played'}: {p['track']}"
    return text


class LastfmCog(commands.Cog, name="Last.fm"):
    """/lastfm link, /lastfm unlink, /lastfm user."""
    lastfm = app_commands.Group(name="lastfm", description="Link Last.fm and see what people are listening to",
                                allowed_installs=app_commands.AppInstallationType(guild=True, user=True),
                                allowed_contexts=app_commands.AppCommandContext(guild=True, dm_channel=True, private_channel=True))

    def __init__(self, bot):
        self.bot = bot

    @lastfm.command(name="link", description="Link your Last.fm account")
    async def link(self, interaction: discord.Interaction):
        if await asyncio.to_thread(api_down):
            return await interaction.response.send_message(API_DOWN_TEXT, ephemeral=True)
        started = await asyncio.to_thread(_shapes_call, "POST", "/internal/link/start",
                                          {"provider": "lastfm", "platform": "discord", "yapin_id": str(interaction.user.id), "yapin_name": interaction.user.name})
        url = (started or {}).get("url")
        if not url:
            return await interaction.response.send_message(str((started or {}).get("data") or API_DOWN_TEXT), ephemeral=True)
        view = discord.ui.View(timeout=LASTFM_LINK_TTL)
        view.add_item(discord.ui.Button(label="Authorize on Last.fm", style=discord.ButtonStyle.link, url=url))
        await interaction.response.send_message("Tap the button, approve access on Last.fm and you're linked. The link works for 10 minutes and only for you.", view=view, ephemeral=True)

    @lastfm.command(name="unlink", description="Unlink your Last.fm account")
    async def unlink(self, interaction: discord.Interaction):
        if await asyncio.to_thread(api_down):
            return await interaction.response.send_message(API_DOWN_TEXT, ephemeral=True)
        had = await asyncio.to_thread(lastfm_name_of, interaction.user.id)
        await asyncio.to_thread(_shapes_call, "DELETE", f"/internal/links/{interaction.user.id}/lastfm?platform=discord")
        await interaction.response.send_message("Unlinked your Last.fm account." if had else "You had no Last.fm account linked.", ephemeral=True)

    @lastfm.command(name="user", description="See what you or someone else is listening to")
    @app_commands.describe(user="Who to show (default: you)")
    async def user(self, interaction: discord.Interaction, user: discord.User = None):
        target = user or interaction.user
        if await asyncio.to_thread(api_down):
            return await interaction.response.send_message(API_DOWN_TEXT, ephemeral=True)
        name = await asyncio.to_thread(lastfm_name_of, target.id)
        if not name:
            who = "You haven't" if target.id == interaction.user.id else f"{target.display_name} hasn't"
            return await interaction.response.send_message(f"{who} linked a Last.fm account. Use `/lastfm link`.", ephemeral=True)
        await interaction.response.defer()
        data = await asyncio.to_thread(_shapes_call, "GET", f"/internal/lastfm/{urllib.parse.quote(str(name), safe='')}")
        p = (data or {}).get("profile") if data and data.get("success") else None
        if not p:
            return await interaction.followup.send(f"Couldn't reach Last.fm for `{name}` right now.")
        embed = discord.Embed(title=f"{target.display_name} on Last.fm", description=lastfm_line(p), color=_embed_color(interaction.user.id))
        if p.get("image"):
            embed.set_thumbnail(url=p["image"])
        embed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
        await interaction.followup.send(embed=embed)

async def setup(bot):
    await bot.add_cog(LastfmCog(bot))

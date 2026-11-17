"""Cog: /lastfm: link an account or see what someone is listening to."""
import sys
import asyncio
import yapper

m = sys.modules.get("main") or sys.modules["__main__"]   # this bot's main.py: shared helpers, data and the bot itself
API_DOWN_TEXT = m.API_DOWN_TEXT
MainURL = m.MainURL
api_down = m.api_down
bot = m.bot
discord = m.discord
ecolor = m.ecolor
get_lastfm = m.get_lastfm
lastfm_line = m.lastfm_line
lastfm_profile = m.lastfm_profile
resolve_user = m.resolve_user
shapes_api = m.shapes_api


LASTFM_LINK_TTL = 600


@bot.tree.command(name="lastfm", description="Link your Last.fm account or see what someone is listening to")
async def lastfm(interaction: discord.Interaction, action: str = "", user: discord.User = None):
    action = (action or "").strip()
    uid = interaction.user.id
    if action.lower() not in ("", "link", "unlink") and user is None:
        # "/lastfm @someone" (or a bare user ID) lands in the first slot, so read it as the person to show
        try:
            user = resolve_user(action)
        except ValueError:
            user = None
        if user is None:
            await interaction.response.send_message("I couldn't find that person. Ping them, or use `/lastfm link` or `/lastfm unlink`.", ephemeral=True)
            return
        action = ""
    action = action.lower()
    if await asyncio.to_thread(api_down):
        await interaction.response.send_message(API_DOWN_TEXT, ephemeral=True)
        return
    if action == "link":
        started = await asyncio.to_thread(shapes_api, "POST", "/internal/link/start",
                                          {"provider": "lastfm", "yapin_id": str(uid), "yapin_name": interaction.user.name})
        link = (started or {}).get("url")
        if not link:
            reason = (started or {}).get("data") or API_DOWN_TEXT
            await interaction.response.send_message(str(reason), ephemeral=True)
            return
        view = discord.ui.View(timeout=LASTFM_LINK_TTL)
        view.add_item(discord.ui.Button(label="Authorize on Last.fm", style=discord.ButtonStyle.link, url=link))
        await interaction.response.send_message("Tap the button, approve access on Last.fm and you're linked. The link works for 10 minutes and only for you.", view=view, ephemeral=True)
        return
    if action == "unlink":
        had = await asyncio.to_thread(get_lastfm, uid)
        await asyncio.to_thread(shapes_api, "DELETE", f"/internal/links/{uid}/lastfm")
        await interaction.response.send_message("Unlinked your Last.fm account." if had else "You had no Last.fm account linked.", ephemeral=True)
        return
    target = user or interaction.user
    linked = await asyncio.to_thread(get_lastfm, target.id)
    if not linked:
        who = "You haven't" if target.id == uid else f"{target.display_name} hasn't"
        await interaction.response.send_message(f"{who} linked a Last.fm account. Use `/lastfm link`.", ephemeral=True)
        return
    p = await asyncio.to_thread(lastfm_profile, linked)
    if not p:
        await interaction.response.send_message(f"Couldn't reach Last.fm for `{linked}` right now.", ephemeral=True)
        return
    embed = discord.Embed(title=f"{target.display_name} on Last.fm", description=lastfm_line(p), color=ecolor(uid))
    if p["image"]:
        embed.set_thumbnail(url=p["image"])
    embed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
    await interaction.response.send_message(embed=embed)


class Lastfm(yapper.Cog):
    name = "Last.fm"


def setup(bot):
    bot.add_cog(Lastfm(bot))

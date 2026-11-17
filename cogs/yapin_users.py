"""Cog: Yapin' profile lookups and links: /yapinuser, /yapinavatar, /yapinbanner, /spookpfp, /roblox2yapin, /yapin2roblox, /linkroblox, /britishuser, /github, /instagram."""
import sys
import aiohttp
import asyncio
import base64
import json
import requests
import socket
import urllib.parse
import yapper

m = sys.modules.get("main") or sys.modules["__main__"]   # this bot's main.py: shared helpers, data and the bot itself
API_DOWN_TEXT = m.API_DOWN_TEXT
Emojis = m.Emojis
MainURL = m.MainURL
_first = m._first
api_down = m.api_down
api_ready = m.api_ready
app_commands = m.app_commands
bot = m.bot
cached = m.cached
discord = m.discord
ecolor = m.ecolor
embedDB = m.embedDB
fetch_user = m.fetch_user
get_lastfm = m.get_lastfm
isotodiscordtimestamp = m.isotodiscordtimestamp
lastfm_line = m.lastfm_line
lastfm_profile = m.lastfm_profile
shapes_api = m.shapes_api


def roblox_of_yapin(yapin_id):
    """(roblox_id, roblox_name, hidden) for a Yapin user, or None when not linked / API down."""
    data = shapes_api("GET", f"/internal/lookup/yapin/{yapin_id}")
    if not data or not data.get("success"):
        return None
    return (data.get("data"), data.get("roblox_name"), bool(data.get("hidden")))


def yapin_of_roblox(roblox):
    """(yapin_id, hidden) for a Roblox id or username, or None."""
    data = shapes_api("GET", f"/internal/lookup/roblox/{urllib.parse.quote(str(roblox), safe='')}")
    if not data or not data.get("success"):
        return None
    return (data.get("data"), bool(data.get("hidden")))


def _profile_target(interaction, user):
    """The user to look up. Fills in the full profile from Yapper so avatar/banner/bio are available."""
    target = user or interaction.user
    data = fetch_user(target.id)
    if data:
        target.data = {**target.data, **{k: v for k, v in data.items() if v not in (None, "")}}   # the full profile wins
        target.name = _first(data, "username", "name") or target.name
        target.display_name = _first(data, "display_name", "displayName") or target.display_name
    return target


def _mutual_servers(user_id):
    """Names of the servers the bot shares with this person (looked up side by side, one request per server)."""
    from concurrent.futures import ThreadPoolExecutor
    servers = list(cached("servers", 15, bot.servers))

    def check(s):
        try:
            bot.member(s.get("id"), user_id)
            return s.get("name") or str(s.get("id"))
        except Exception:
            return None      # not a member of that server
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            return [n for n in pool.map(check, servers) if n]
    except Exception as e:
        print(f"mutual server lookup failed: {e}")
        return []


@bot.tree.command(name="yapinuser", description="Get information about a user")
async def yapinuser(interaction: discord.Interaction, user: discord.User = None):
    target = _profile_target(interaction, user)
    handle = f"@{target.name}"
    embed = discord.Embed(title=target.display_name + ("  ·  🤖 Bot" if target.is_bot else ""), color=ecolor(interaction.user.id))
    bio = _first(target.data, "bio", "about", "description")
    lines = [f"{handle}" + (f" · {target.pronouns}" if target.pronouns else "")]
    if bio:
        lines.append("> " + str(bio).strip().replace("\n", "\n> ")[:400])
    embed.description = "\n".join(lines)
    created = target.created_at
    embed.add_field(name="Joined Yapin", value=f"<t:{int(created.timestamp())}:D>\n<t:{int(created.timestamp())}:R>" if created else "Unknown", inline=True)
    embed.add_field(name="ID", value=f"`{target.id}`", inline=True)
    if target.badges:
        embed.add_field(name="Badges", value="  ".join(f"`{b}`" for b in target.badges[:10]), inline=False)
    mutual = _mutual_servers(target.id)
    if mutual:
        shown = "\n".join(f"• {n}" for n in mutual[:8]) + (f"\n…and {len(mutual) - 8} more" if len(mutual) > 8 else "")
        embed.add_field(name=f"Servers with Shapes ({len(mutual)})", value=shown, inline=False)
    linked = await asyncio.to_thread(get_lastfm, target.id)
    if linked:
        p = await asyncio.to_thread(lastfm_profile, linked)
        embed.add_field(name="Last.fm", value=lastfm_line(p) if p else f"[{linked}](https://www.last.fm/user/{linked})", inline=False)
    if target.avatar_url:
        embed.set_thumbnail(url=target.avatar_url)
    if target.banner_url:
        embed.set_image(url=target.banner_url)
    embed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="yapinavatar", description="Get a user's avatar")
async def yapinavatar(interaction: discord.Interaction, user: discord.User = None):
    target = _profile_target(interaction, user)
    print(str(target))
    url = target.avatar_url
    if not url:
        await interaction.response.send_message(f"{target.name} doesn't have an avatar.")
        return
    embed = discord.Embed(title=f"{target.name}'s Avatar", color=ecolor(interaction.user.id), timestamp=interaction.created_at)
    embed.set_image(url=url)
    embed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="Download Avatar", style=discord.ButtonStyle.link, url=url))
    await interaction.response.send_message(embed=embed, view=view)


@bot.tree.command(name="yapinbanner", description="Get a user's banner")
async def yapinbanner(interaction: discord.Interaction, user: discord.User = None):
    target = _profile_target(interaction, user)
    url = target.banner_url
    if not url:
        await interaction.response.send_message(f"{target.name} doesn't have a banner image.")
        return
    embed = discord.Embed(title=f"{target.name}'s Banner", color=ecolor(interaction.user.id), timestamp=interaction.created_at)
    embed.set_image(url=url)
    embed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="Download Banner", style=discord.ButtonStyle.link, url=url))
    await interaction.response.send_message(embed=embed, view=view)


# --- spookpfp ---
@bot.tree.command(name="spookpfp", description="Get a pfp from a user's spook.bio profile.")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def spookpfp(interaction: discord.Interaction, username: str = "leandre"):
    url = f"https://spook-v3.onrender.com/$/api/v2/users/{username}"
    response = requests.get(url)
    data = response.json()
    if response.status_code == 200:
        await interaction.response.send_message(data.get('Picture_URL') or f":x: {username} has no profile picture :x:", ephemeral=False)
        print("Fetched data successfully!")
    else:
        await interaction.response.send_message(f":x: {username} Not Found :x:", ephemeral=False)
        print(f"Error fetching data: {response.status_code}")


# --- roblox2yapin ---
@bot.tree.command(name="roblox2yapin", description="Get a Roblox user's linked Yapin account from their username.")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def roblox2yapin(interaction: discord.Interaction, user: str):
    await interaction.response.defer()

    #print(f"Searching For {user}")
    thinkingembed = discord.Embed(
        title=f"{Emojis.get('loading')} {interaction.user.mention} Searching For {user}!",
        color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
    )
    await interaction.followup.send(embed=thinkingembed)

    url = "https://users.roblox.com/v1/usernames/users"
    # print(f"Fetching Data From {url}")

    request_payload = {
        "usernames": [user],
        "excludeBannedUsers": True
    }

    try:
        response = requests.post(url, json=request_payload)
        response.raise_for_status()
        data = response.json()
        if data.get("data") and len(data["data"]) > 0:
            userinfo = data["data"][0]
            UserID = userinfo["id"]
            Display = userinfo["displayName"]
            user = userinfo["name"]
            if user == Display:
                Username = f"@{user}"
            else:
                Username = f"{Display} (@{user})"
            #print(f"UserInfo: {userinfo}")
        else:
            #print(f"{user} not found.")
            failedembed7 = discord.Embed(
                title=f":warning: {user} not found.",
                color=discord.Color.yellow()
            )
            await interaction.edit_original_response(embed=failedembed7)
            return

    except requests.exceptions.RequestException as e:
        #print(f"An error occurred during the API request: {e}")
        failedembed8 = discord.Embed(
            title=f":warning: {user} not found.",
            color=discord.Color.yellow()
        )
        await interaction.edit_original_response(embed=failedembed8)
        return

    found = await asyncio.to_thread(yapin_of_roblox, UserID)
    color = embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
    if found is None and not api_ready():
        await interaction.edit_original_response(embed=discord.Embed(title="Linking isn't set up on this bot yet", color=discord.Color.red()))
        return
    if found is None and await asyncio.to_thread(api_down):
        await interaction.edit_original_response(embed=discord.Embed(title="The Shapes API is down", description=API_DOWN_TEXT, color=discord.Color.red()))
        return
    if found is None:
        await interaction.edit_original_response(embed=discord.Embed(
            title=f"No Yapin user found for {Username}",
            description="They haven't linked their Roblox account with `/linkroblox`.", color=discord.Color.red()))
        return
    yapin_id, hidden = found
    if hidden or not yapin_id:
        await interaction.edit_original_response(embed=discord.Embed(
            title=f"{Username} has hidden their Yapin link", color=discord.Color.red()))
        return
    person = await asyncio.to_thread(fetch_user, yapin_id)
    handle = f"@{person.get('username')}" if person.get("username") else yapin_id
    embed = discord.Embed(title=f"Yapin user for {Username}", description=f"**{person.get('display_name') or handle}** ({handle})", color=color)
    embed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
    await interaction.edit_original_response(embed=embed)


# --- yapin2roblox ---
@bot.tree.command(name="yapin2roblox", description="Get a Roblox profile from a Yapin user.")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def yapin2roblox(interaction: discord.Interaction, user: discord.User):
    userid = user.id
    await interaction.response.defer(thinking=True)

    loading = discord.Embed(
        title=f"{Emojis.get('loading')} {interaction.user.mention} Getting Roblox Profile For {user.name}",
        color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
    )

    await interaction.followup.send(embed=loading)

    found = await asyncio.to_thread(roblox_of_yapin, userid)
    color = embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
    if found is None and await asyncio.to_thread(api_down):
        await interaction.edit_original_response(embed=discord.Embed(title="The Shapes API is down", description=API_DOWN_TEXT, color=discord.Color.red()))
        return
    if found is None:
        text = "Linking isn't set up on this bot yet" if not api_ready() else f"No Roblox Profile found for {user.name}"
        await interaction.edit_original_response(embed=discord.Embed(
            title=text, description="" if not api_ready() else "They haven't linked their Roblox account with `/linkroblox`.", color=discord.Color.red()))
        return
    roblox_id, roblox_name, hidden = found
    if hidden or not roblox_id:
        await interaction.edit_original_response(embed=discord.Embed(title=f"{user.name} has hidden their Roblox link", color=discord.Color.red()))
        return
    embed = discord.Embed(title=f"Roblox Profile for {user.name}", color=color,
                          description=f"[{roblox_name or roblox_id}](https://www.roblox.com/users/{roblox_id}/profile)")
    embed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
    await interaction.edit_original_response(embed=embed)


# --- britishuser ---
@bot.tree.command(name="britishuser", description="Check if a user has their language set to British")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def british_check(interaction: discord.Interaction, user_input: str):
    await interaction.response.defer(thinking=True)

    embed_color = embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()

    thinkingembed = discord.Embed(
        title=f"{Emojis.get('loading')}  {interaction.user.mention} Checking if {user_input} is British!",
        color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
    )
    await interaction.followup.send(embed=thinkingembed)

    url = "https://users.roblox.com/v1/usernames/users"

    request_payload = {
        "usernames": [user_input],
        "excludeBannedUsers": False
    }

    try:
        response = requests.post(url, json=request_payload)
        response.raise_for_status()
        data = response.json()
        if data.get("data") and len(data["data"]) > 0:
            userinfo = data["data"][0]
            UserID = userinfo["id"]
            username = userinfo["name"]

            payload = json.dumps([{"name": "vieweeUserId", "type": "UserId", "value": int(UserID)}])
            b64encoded = base64.b64encode(payload.encode('utf-8')).decode('utf-8')

            british_url = f"https://apis.roblox.com/access-management/v1/upsell-feature-access?featureName=MustHideConnections&extraParameters={b64encoded}"
            british_response = requests.get(british_url)
            british_response.raise_for_status()
            british_data = british_response.json()

            is_british = british_data.get("access") == "Granted"

            avatar_url = f"https://thumbnails.roblox.com/v1/users/avatar-bust?userIds={UserID}&size=150x150&format=Png&isCircular=false"
            avatar_response = requests.get(avatar_url)
            avatar_data = avatar_response.json()
            avatar_thumbnail = avatar_data["data"][0]["imageUrl"] if avatar_data.get("data") and len(avatar_data["data"]) > 0 else None

            if is_british:
                embed = discord.Embed(
                    title=":flag_gb: British Check Result",
                    description=f"**{username}** is British! :flag_gb:",
                    color=embed_color
                )
            else:
                embed = discord.Embed(
                    title=":x: British Check Result",
                    description=f"**{username}** is not British :x:",
                    color=embed_color
                )

            if avatar_thumbnail:
                embed.set_thumbnail(url=avatar_thumbnail)

            embed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
            await interaction.edit_original_response(embed=embed)

        else:
            embed = discord.Embed(
                title=f":warning: {user_input} not found",
                color=embed_color
            )
            embed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
            await interaction.edit_original_response(embed=embed)

    except requests.exceptions.RequestException as e:
        embed = discord.Embed(
            title=":x: API Error",
            description=f"An error occurred: {str(e)}",
            color=embed_color
        )
        embed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
        await interaction.edit_original_response(embed=embed)


# --- github ---
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@bot.tree.command(name="github", description="Get detailed information about a GitHub user")
@app_commands.describe(username="GitHub username")
async def github(interaction: discord.Interaction, username: str):
    await interaction.response.defer(thinking=True)
    
    thinkingembed = discord.Embed(
        title=f"{Emojis.get('loading')} {interaction.user.mention} Searching For GitHub Profile!",
        color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.dark_gray()
    )
    await interaction.followup.send(embed=thinkingembed)
    
    try:
        connector = aiohttp.TCPConnector(family=socket.AF_INET)
        async with aiohttp.ClientSession(connector=connector) as session:
            url = f"https://api.github.com/users/{username}"
            
            async with session.get(url) as response:
                if response.status == 404:
                    errorembed = discord.Embed(
                        description=f"❌ GitHub user `{username}` not found",
                        color=discord.Color.red()
                    )
                    errorembed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
                    await interaction.edit_original_response(embed=errorembed)
                    return
                
                response.raise_for_status()
                data = await response.json()
            
            avatar_url = data.get("avatar_url", "")
            profile_url = data.get("html_url", f"https://github.com/{username}")
            company = data.get("company", "Not specified")
            blog = data.get("blog", "Not specified")
            location = data.get("location", "Not specified")
            bio = data.get("bio", "No bio available")
            public_repos = data.get("public_repos", 0)
            followers = data.get("followers", 0)
            following = data.get("following", 0)
            created_at = data.get("created_at", "Unknown")
            updated_at = data.get("updated_at", "Unknown")
            
            created_timestamp = isotodiscordtimestamp(created_at, "D") if created_at != 'Unknown' else "Unknown"
            updated_timestamp = isotodiscordtimestamp(updated_at, "D") if updated_at != 'Unknown' else "Unknown"
            
            embed = discord.Embed(
                title=f"@{username}",
                url=profile_url,
                color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.dark_gray(),
                timestamp=interaction.created_at
            )
            
            if avatar_url:
                embed.set_thumbnail(url=avatar_url)
            
            github_info = f"""
            > **Public Repos:** `{public_repos}`
            > **Followers:** `{followers}`
            > **Following:** `{following}`
            > **Company:** `{company}`
            > **Location:** `{location}`
            > **Created:** {created_timestamp}
            > **Updated:** {updated_timestamp}
            """
            
            if blog and blog != "Not specified":
                github_info += f"> **Website:** `{blog}`\n"
            
            embed.add_field(
                name="GitHub Information",
                value=github_info,
                inline=False
            )
            
            if bio and bio != "No bio available":
                if len(bio) > 1024:
                    bio = bio[:1021] + "..."
                embed.add_field(
                    name="Bio",
                    value=f"> `{bio}`",
                    inline=False
                )
            
            embed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
            
            view = discord.ui.View(timeout=120)
            view.add_item(discord.ui.Button(
                label="View on GitHub", 
                url=profile_url,
                style=discord.ButtonStyle.link,
                emoji="🐙"
            ))
            
            await interaction.edit_original_response(embed=embed, view=view)
            
    except aiohttp.ClientError as e:
        errorembed = discord.Embed(
            description=f"Failed to request github",
            color=discord.Color.red()
        )
        errorembed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
        await interaction.edit_original_response(embed=errorembed)
    except Exception as e:
        errorembed = discord.Embed(
            description=f"❌ Failed to fetch GitHub profile information",
            color=discord.Color.red()
        )
        errorembed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
        await interaction.edit_original_response(embed=errorembed)


# --- linkroblox..instagram ---
@bot.tree.command(name="linkroblox", description="Link your Roblox account to your Yapin account")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def linkroblox(interaction: discord.Interaction, action: str = "link"):
    """/linkroblox [link | status | unlink | hide | show]. Everything is private to the person using it."""
    action = (action or "link").lower().strip()
    uid = str(interaction.user.id)
    color = embedDB.get(uid) if embedDB.get(uid) else discord.Color.blue()
    if not api_ready():
        await interaction.response.send_message("Linking isn't set up on this bot yet (the owner needs to connect it to the Shapes API).", ephemeral=True)
        return
    if await asyncio.to_thread(api_down):
        await interaction.response.send_message(API_DOWN_TEXT, ephemeral=True)
        return
    if action in ("unlink", "remove"):
        data = await asyncio.to_thread(shapes_api, "DELETE", f"/internal/links/{uid}/roblox")
        if data is None:
            text = API_DOWN_TEXT
        else:
            text = "Unlinked your Roblox account." if data.get("removed") else "You didn't have a Roblox account linked."
        await interaction.response.send_message(text, ephemeral=True)
        return
    if action in ("hide", "show"):
        data = await asyncio.to_thread(shapes_api, "PATCH", f"/internal/links/{uid}/roblox", {"enabled": action == "show"})
        if data is None:
            text = API_DOWN_TEXT
        elif not data.get("success"):
            text = "You have no linked Roblox account. Use `/linkroblox` first."
        else:
            text = "Other people can now look up your link." if action == "show" else "Your link is now hidden from lookups."
        await interaction.response.send_message(text, ephemeral=True)
        return
    links = await asyncio.to_thread(shapes_api, "GET", f"/internal/links/{uid}")
    current = (links or {}).get("roblox")
    if action in ("status", "info"):
        if links is None:
            text = API_DOWN_TEXT
        elif not current:
            text = "No Roblox account linked. Use `/linkroblox` to link one."
        else:
            text = (f"Linked to [{current.get('name') or current['id']}](https://www.roblox.com/users/{current['id']}/profile). "
                    f"Lookups: {'on' if current.get('enabled', True) else 'hidden'}.")
        await interaction.response.send_message(text, ephemeral=True)
        return
    started = await asyncio.to_thread(shapes_api, "POST", "/internal/link/start", {"provider": "roblox", "yapin_id": uid, "yapin_name": interaction.user.name})
    if not started or not started.get("url"):
        reason = (started or {}).get("data") or API_DOWN_TEXT
        await interaction.response.send_message(str(reason), ephemeral=True)
        return
    embed = discord.Embed(title="Link your Roblox account", color=color, description=(
        "Tap the button and approve access on Roblox. You'll be linked to your Yapin account as soon as you do.\n"
        "The link works for 10 minutes and only for you."
        + (f"\n\nCurrently linked to **{current.get('name') or current['id']}**; linking again replaces it." if current else "")))
    view = discord.ui.View(timeout=600)
    view.add_item(discord.ui.Button(label="Link with Roblox", style=discord.ButtonStyle.link, url=started["url"]))
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


@bot.tree.command(name="instagram", description="Get detailed information about an Instagram user")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(username="Instagram username")
async def instagram_command(interaction: discord.Interaction, username: str):
    await interaction.response.defer(thinking=True)

    loading = discord.Embed(
        title=f"{Emojis.get('loading')} {interaction.user.mention} Getting Instagram Profile For @{username}",
        color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
    )
    await interaction.edit_original_response(embed=loading)

    try:
        username = username.strip().replace('@', '')

        if not username:
            errorembed = discord.Embed(
                description="Please provide a valid Instagram username",
                color=discord.Color.red()
            )
            await interaction.edit_original_response(embed=errorembed)
            return

        profile_info = await fetch_and_parse_instagram_profile(username)

        if isinstance(profile_info, str):
            errorembed = discord.Embed(description=f"{profile_info}", color=discord.Color.red())
            await interaction.edit_original_response(embed=errorembed)
            return

        embed = create_instagram_embed(username, profile_info, interaction.user.name)
        await interaction.edit_original_response(embed=embed)

    except Exception as e:
        errorembed = discord.Embed(
            description="Failed to fetch Instagram profile information",
            color=discord.Color.red()
        )
        await interaction.edit_original_response(embed=errorembed)


async def get_instagram_profile(session, username):
    url = f"https://i.instagram.com/api/v1/users/web_profile_info/?username={username}"

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "X-IG-App-ID": "936619743392459",
        "Referer": f"https://www.instagram.com/{username}/",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
    }

    try:
        async with session.get(url, headers=headers, timeout=10) as response:
            if response.status == 404:
                return None, "User not found"
            response.raise_for_status()
            data = await response.json()
            return data["data"]["user"], None
    except aiohttp.ClientError as e:
        return None, f"Connection error: {str(e)}"
    except Exception as e:
        return None, f"Error fetching profile: {str(e)}"


def parse_instagram_data(user):
    return {
        "username": user["username"],
        "full_name": user["full_name"],
        "biography": user["biography"],
        "followers": user["edge_followed_by"]["count"],
        "following": user["edge_follow"]["count"],
        "posts": user["edge_owner_to_timeline_media"]["count"],
        "profile_pic_url": user["profile_pic_url_hd"],
        "is_verified": user["is_verified"],
        "is_private": user["is_private"],
    }


async def fetch_and_parse_instagram_profile(username):
    async with aiohttp.ClientSession() as session:
        user, error = await get_instagram_profile(session, username)

        if error:
            return error

        return parse_instagram_data(user)


def create_instagram_embed(username, profile_info, requester_name):
    profile_url = f"https://www.instagram.com/{username}/"

    title_display = f"@{username}"
    if profile_info['is_verified']:
        title_display += " <:VerifiedInstagram:1447033802802331760>"

    embed = discord.Embed(
        title=title_display,
        url=profile_url,
        color=0xE1306C
    )

    if profile_info['profile_pic_url']:
        embed.set_thumbnail(url=profile_info['profile_pic_url'])

    embed.add_field(name="Posts", value=f"**{profile_info['posts']:,}**", inline=True)
    embed.add_field(name="Followers", value=f"**{profile_info['followers']:,}**", inline=True)
    embed.add_field(name="Following", value=f"**{profile_info['following']:,}**", inline=True)
    embed.add_field(name="Account Type", value="Private" if profile_info['is_private'] else "Public", inline=True)

    if profile_info['full_name']:
        embed.add_field(name="Full Name", value=profile_info['full_name'], inline=True)

    if profile_info['biography']:
        bio_text = profile_info['biography']
        if len(bio_text) > 1024:
            bio_text = bio_text[:1021] + "..."
        embed.add_field(name="Description", value=bio_text, inline=False)

    embed.set_footer(text=f"Requested By {requester_name} │ {MainURL}")

    return embed


class YapinUsers(yapper.Cog):
    name = "Yapin users"


def setup(bot):
    bot.add_cog(YapinUsers(bot))

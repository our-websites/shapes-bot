"""Cog: profile lookups: Discord users, avatars and banners, spook.bio, GitHub and Instagram."""
import sys
from discord import app_commands
from discord.ext import commands
from typing import Dict, Any, Optional
import aiohttp
import discord
import requests
import socket

m = sys.modules.get("main") or sys.modules["__main__"]   # this bot's main.py: shared helpers, data and the bot itself
Emojis = m.Emojis
MainURL = m.MainURL
bot = m.bot
embedDB = m.embedDB
isotodiscordtimestamp = m.isotodiscordtimestamp
token = m.token


@bot.tree.command(name="spookpfp", description="Get a pfp from a user's spook.bio profile.")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def spookpfp(interaction: discord.Interaction, username: str = "phis"):
    url = f"https://spook.bio/api/profiles/{username}"
    response = requests.get(url)
    data = response.json()
    if response.status_code == 200:
        await interaction.response.send_message(data.avatar, ephemeral=False)
        print("Fetched data successfully!")
    else:
        await interaction.response.send_message(f":x: {username} Not Found :x:", ephemeral=False)
        print(f"Error fetching data: {response.status_code}")


@bot.tree.command(name="discord2spook", description="Get a spook.bio profile from a discord user.")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def discord2spook(interaction: discord.Interaction, user: discord.User): # = 481295611417853982):
    url = f"https://spook.bio/api/profiles?discordId={user.id}"
    print(user.id)
    print(url)
    response = requests.get(url)
    data = response.json()
    if response.status_code == 200:
        await interaction.response.send_message(f"{user.mention}'s [spook.bio Profile](https://spook.bio/@{data.get('username')})", ephemeral=False)
        print(f"Fetched {data['username']} successfully!")
    else:
        if interaction.user.name == user.name:
            await interaction.response.send_message(f":x: You don't have a spook.bio profile linked to your account {user.mention}! :x: To link your profile to your account create a spook.bio account [here](https://spook.bio/login)")
            return
        await interaction.response.send_message(f":x: {user.mention} doesn't have a spook.bio profile linked to their account! :x:", ephemeral=False)
        print(f"Error fetching data: {response.status_code}")


@bot.tree.command(name="discorduser", description="Get information about a user")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def discorduser(interaction: discord.Interaction, user: discord.User = None):
    await interaction.response.defer()
    
    if user is None:
        user = interaction.user
    
    target_member = None
    guild = interaction.guild
    
    if guild:
        target_member = guild.get_member(user.id)
    
    created_at = isotodiscordtimestamp(user.created_at.isoformat(), "D")
    
    BADGE_FLAGS = {
        4194304: "<:activedeveloper:1431764482312372385>",
        512: "<:earlysupporter:1431764483000504351>",
        64: "<:hypesquadp:1431764484179099788>",
        128: "<:hypesquadr:1431764485374214267>",
        256: "<:hypesquadg:1431764486636830780>",
    }
    
    badges = []
    public_flags = user.public_flags.value if user.public_flags else 0
    
    for flag, emoji in BADGE_FLAGS.items():
        if public_flags & flag:
            badges.append(emoji)
    
    has_nitro = False
    if user.avatar:
        if user.avatar.is_animated():
            has_nitro = True
            badges.append("<:Nitro:1431740129726300191>")
    
    try:
        user_data = await bot.fetch_user(user.id)
        if user_data.banner:
            has_nitro = True
            if "<:Nitro:1431777332552536155>" not in badges:
                badges.append("<:Nitro:1431777332552536155>")
    except:
        pass
    
    badges_display = " ".join(badges) if badges else "None"
    
    embed = discord.Embed(
        title=f"{user.name}",
        url=f"https://discord.com/users/{user.id}",
        color=discord.Color.blue(),
        timestamp=interaction.created_at
    )
    
    if user.avatar:
        embed.set_thumbnail(url=user.avatar.url)
    
    user_info = f"""
    > **Username:** `{user.name}`
    > **ID:** `{user.id}`
    > **Created:** {created_at}
    > **Bot:** `{user.bot}`
    > **Nitro:** `{has_nitro}`
    > **Badges:** {badges_display}
    """
    
    embed.add_field(
        name="User Information",
        value=user_info,
        inline=False
    )
    
    if guild and target_member:
        if target_member.joined_at:
            joined_at = isotodiscordtimestamp(target_member.joined_at.isoformat(), "D")
        else:
            joined_at = "Unknown"
        
        roles = [role.mention for role in target_member.roles[1:][:10]]
        roles_display = ", ".join(roles) if roles else "None"
        if len(target_member.roles) > 11:
            roles_display += f" (+{len(target_member.roles) - 11} more)"
        
        server_info = f"""
        > **Joined:** {joined_at}
        > **Nickname:** `{target_member.nick or 'None'}`
        > **Top Role:** {target_member.top_role.mention}
        """
        
        embed.add_field(
            name="Server Info",
            value=server_info,
            inline=False
        )
        
        embed.add_field(
            name=f"Roles ({len(target_member.roles)})",
            value=f"> {roles_display}",
            inline=False
        )
    
    embed.set_footer(text=f"Requested by {interaction.user.display_name} | {MainURL}")

    class MutualServersView(discord.ui.View):
        def __init__(self, user_id, username, requester_id, main_embed):
            super().__init__(timeout=60)
            self.user_id = user_id
            self.username = username
            self.requester_id = requester_id
            self.main_embed = main_embed

        async def interaction_check(self, interaction: discord.Interaction) -> bool:
            return interaction.user.id == self.requester_id

        @discord.ui.button(label="View Mutual Servers", style=discord.ButtonStyle.blurple)
        async def mutuals_button(self, interaction: discord.Interaction, button: discord.ui.Button):
            await interaction.response.defer()
            
            try:
                import aiohttp
                async with aiohttp.ClientSession() as session:
                    async with session.get("http://localhost:13455/mutuals") as response:
                        if response.status == 200:
                            data = await response.json()
                            
                            if not data or "Servers" not in data:
                                class NoServersView(discord.ui.View):
                                    def __init__(self, main_embed, requester_id, user_id, username):
                                        super().__init__(timeout=120)
                                        self.main_embed = main_embed
                                        self.requester_id = requester_id
                                        self.user_id = user_id
                                        self.username = username

                                    async def interaction_check(self, interaction: discord.Interaction) -> bool:
                                        return interaction.user.id == self.requester_id

                                    @discord.ui.button(label="Back to User Info", style=discord.ButtonStyle.gray)
                                    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                                        view = MutualServersView(self.user_id, self.username, self.requester_id, self.main_embed)
                                        await interaction.response.edit_message(content="", embed=self.main_embed, view=view)
                                
                                no_servers_view = NoServersView(self.main_embed, self.requester_id, self.user_id, self.username)
                                await interaction.edit_original_response(content="No mutual servers data available.", embed=None, view=no_servers_view)
                                return

                            user_servers = []
                            for server_id, server_data in data["Servers"].items():
                                members = server_data.get("members", [])
                                for member in members:
                                    if str(member.get("id")) == str(self.user_id):
                                        user_servers.append({
                                            "server_id": server_id,
                                            "server_name": server_data.get("name", "Unknown Server"),
                                            "member_count": server_data.get("membercount", 0),
                                            "bot_count": server_data.get("botcount", 0),
                                            "channel_count": server_data.get("channels", 0),
                                            "icon_url": server_data.get("iconurl"),
                                            "created_at": server_data.get("createdat"),
                                            "roles": server_data.get("roles", 0),
                                            "verification_level": server_data.get("verificationlevel", "Unknown"),
                                            "owner": server_data.get("owner", "Unknown"),
                                            "owner_id": server_data.get("ownerid", "Unknown")
                                        })
                                        break

                            if not user_servers:
                                class NoMutualServersView(discord.ui.View):
                                    def __init__(self, main_embed, requester_id, user_id, username):
                                        super().__init__(timeout=120)
                                        self.main_embed = main_embed
                                        self.requester_id = requester_id
                                        self.user_id = user_id
                                        self.username = username

                                    async def interaction_check(self, interaction: discord.Interaction) -> bool:
                                        return interaction.user.id == self.requester_id

                                    @discord.ui.button(label="Back to User Info", style=discord.ButtonStyle.gray)
                                    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                                        view = MutualServersView(self.user_id, self.username, self.requester_id, self.main_embed)
                                        await interaction.response.edit_message(content="", embed=self.main_embed, view=view)
                                
                                no_mutual_view = NoMutualServersView(self.main_embed, self.requester_id, self.user_id, self.username)
                                await interaction.edit_original_response(content="No mutual servers found for this user.", embed=None, view=no_mutual_view)
                                return

                            pages = []
                            for server in user_servers:
                                server_embed = discord.Embed(
                                    title=f"Mutual Server: {server['server_name']}",
                                    color=discord.Color.green(),
                                    timestamp=discord.utils.utcnow()
                                )
                                
                                if server['icon_url']:
                                    server_embed.set_thumbnail(url=server['icon_url'])
                                
                                if server['created_at']:
                                    created_at_display = isotodiscordtimestamp(server['created_at'], "D")
                                else:
                                    created_at_display = "Unknown"
                                
                                server_info = f"""
                                > **Name:** {server['server_name']}
                                > **ID:** `{server['server_id']}`
                                > **Created:** {created_at_display}
                                > **Owner:** {server['owner']} (`{server['owner_id']}`)
                                > **Verification:** {server['verification_level'].title()}
                                """
                                
                                server_stats = f"""
                                > **Members:** {server['member_count'] + server['bot_count']:,}
                                > **People:** {server['member_count']:,}
                                > **Bots:** {server['bot_count']:,}
                                > **Channels:** {server['channel_count']}
                                > **Roles:** {server['roles']}
                                """
                                
                                server_embed.add_field(
                                    name="Server Information",
                                    value=server_info,
                                    inline=False
                                )
                                
                                server_embed.add_field(
                                    name="Server Stats",
                                    value=server_stats,
                                    inline=False
                                )
                                
                                server_embed.set_footer(text=f"Page {len(pages) + 1}/{len(user_servers)} | {MainURL}")
                                pages.append(server_embed)

                            if len(pages) == 1:
                                class SingleServerView(discord.ui.View):
                                    def __init__(self, main_embed, requester_id, user_id, username):
                                        super().__init__(timeout=120)
                                        self.main_embed = main_embed
                                        self.requester_id = requester_id
                                        self.user_id = user_id
                                        self.username = username

                                    async def interaction_check(self, interaction: discord.Interaction) -> bool:
                                        return interaction.user.id == self.requester_id

                                    @discord.ui.button(label="Back to User Info", style=discord.ButtonStyle.gray)
                                    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                                        view = MutualServersView(self.user_id, self.username, self.requester_id, self.main_embed)
                                        await interaction.response.edit_message(content="", embed=self.main_embed, view=view)
                                
                                single_view = SingleServerView(self.main_embed, self.requester_id, self.user_id, self.username)
                                await interaction.edit_original_response(content="", embed=pages[0], view=single_view)
                            else:
                                class PaginationView(discord.ui.View):
                                    def __init__(self, pages, main_embed, requester_id, user_id, username):
                                        super().__init__(timeout=120)
                                        self.pages = pages
                                        self.current_page = 0
                                        self.main_embed = main_embed
                                        self.requester_id = requester_id
                                        self.user_id = user_id
                                        self.username = username

                                    async def interaction_check(self, interaction: discord.Interaction) -> bool:
                                        return interaction.user.id == self.requester_id

                                    @discord.ui.button(emoji="⬅️", style=discord.ButtonStyle.gray)
                                    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                                        self.current_page = (self.current_page - 1) % len(self.pages)
                                        await interaction.response.edit_message(content="", embed=self.pages[self.current_page], view=self)
                                    
                                    @discord.ui.button(emoji="➡️", style=discord.ButtonStyle.gray)
                                    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                                        self.current_page = (self.current_page + 1) % len(self.pages)
                                        await interaction.response.edit_message(content="", embed=self.pages[self.current_page], view=self)
                                    
                                    @discord.ui.button(label="Back to User Info", style=discord.ButtonStyle.gray)
                                    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                                        view = MutualServersView(self.user_id, self.username, self.requester_id, self.main_embed)
                                        await interaction.response.edit_message(content="", embed=self.main_embed, view=view)
                                
                                pagination_view = PaginationView(pages, self.main_embed, self.requester_id, self.user_id, self.username)
                                await interaction.edit_original_response(content="", embed=pages[0], view=pagination_view)
                                
                        else:
                            class ErrorView(discord.ui.View):
                                def __init__(self, main_embed, requester_id, user_id, username):
                                    super().__init__(timeout=120)
                                    self.main_embed = main_embed
                                    self.requester_id = requester_id
                                    self.user_id = user_id
                                    self.username = username

                                async def interaction_check(self, interaction: discord.Interaction) -> bool:
                                    return interaction.user.id == self.requester_id

                                @discord.ui.button(label="Back to User Info", style=discord.ButtonStyle.gray)
                                async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                                    view = MutualServersView(self.user_id, self.username, self.requester_id, self.main_embed)
                                    await interaction.response.edit_message(content="", embed=self.main_embed, view=view)
                            
                            error_view = ErrorView(self.main_embed, self.requester_id, self.user_id, self.username)
                            await interaction.edit_original_response(content="Failed to fetch mutual servers data.", embed=None, view=error_view)
            except Exception as e:
                class ExceptionView(discord.ui.View):
                    def __init__(self, main_embed, requester_id, user_id, username):
                        super().__init__(timeout=120)
                        self.main_embed = main_embed
                        self.requester_id = requester_id
                        self.user_id = user_id
                        self.username = username

                    async def interaction_check(self, interaction: discord.Interaction) -> bool:
                        return interaction.user.id == self.requester_id

                    @discord.ui.button(label="Back to User Info", style=discord.ButtonStyle.gray)
                    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                        view = MutualServersView(self.user_id, self.username, self.requester_id, self.main_embed)
                        await interaction.response.edit_message(content="", embed=self.main_embed, view=view)
                
                exception_view = ExceptionView(self.main_embed, self.requester_id, self.user_id, self.username)
                await interaction.edit_original_response(content=f"Error fetching mutual servers: {str(e)}", embed=None, view=exception_view)

    view = MutualServersView(user.id, user.name, interaction.user.id, embed)
    await interaction.followup.send(content="", embed=embed, view=view)


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


@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@bot.tree.command(name="discordavatar", description="Get a user's Discord avatar")
@app_commands.describe(user="The user to get the avatar from (leave empty for yourself)")
async def discordavatar(interaction: discord.Interaction, user: Optional[discord.User] = None):
    """Get a user's Discord avatar with high quality options"""
    await interaction.response.defer()
    target = user or interaction.user
    
    try:
        avatar_url = target.display_avatar.with_size(4096).url
        
        embed = discord.Embed(
            title=f"{target.name}'s Avatar",
            color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue(),
            timestamp=interaction.created_at
        )
        embed.set_image(url=avatar_url)
        embed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
        
        view = discord.ui.View(timeout=120)
        view.add_item(discord.ui.Button(
            label="Download Avatar",
            style=discord.ButtonStyle.link,
            url=avatar_url
        ))
        
        await interaction.followup.send(embed=embed, view=view)
        
    except Exception as e:
        errorembed = discord.Embed(
            title="❌ Failed to fetch avatar",
            description=f"An error occurred: {str(e)}",
            color=discord.Color.red()
        )
        errorembed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
        await interaction.followup.send(embed=errorembed)


@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@bot.tree.command(name="discordbanner", description="Get a user's Discord banner")
@app_commands.describe(user="The user to get the banner from (leave empty for yourself)")
async def discordbanner(interaction: discord.Interaction, user: Optional[discord.User] = None):
    """Get a user's Discord banner using direct API call"""
    await interaction.response.defer()
    target = user or interaction.user
    
    try:
        connector = aiohttp.TCPConnector(family=socket.AF_INET)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(
                f'https://discord.com/api/v9/users/{target.id}',
                headers={'Authorization': f'Bot {token}'}
            ) as resp:
                if resp.status != 200:
                    errorembed = discord.Embed(
                        title="User Not Found",
                        description="Could not fetch user data ",
                        color=discord.Color.red()
                    )
                    errorembed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
                    await interaction.followup.send(embed=errorembed)
                    return
                
                user_data = await resp.json()

        if user_data.get('banner'):
            banner_hash = user_data['banner']
            
            if banner_hash.startswith('a_'):
                banner_url = f"https://cdn.discordapp.com/banners/{target.id}/{banner_hash}.gif?size=4096"
            else:
                banner_url = f"https://cdn.discordapp.com/banners/{target.id}/{banner_hash}.webp?size=4096"
            
            embed = discord.Embed(
                title=f"{target.name}'s Banner",
                color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue(),
                timestamp=interaction.created_at
            )
            embed.set_image(url=banner_url)
            embed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
            
            view = discord.ui.View(timeout=120)
            view.add_item(discord.ui.Button(
                label="Download Banner",
                style=discord.ButtonStyle.link,
                url=banner_url
            ))
            
            await interaction.followup.send(embed=embed, view=view)
            
        else:
            errorembed = discord.Embed(
                title="No Banner Found",
                description=f"{target.name} doesn't have a banner image.",
                color=discord.Color.orange()
            )
            errorembed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
            await interaction.followup.send(embed=errorembed)
        
    except aiohttp.ClientError as e:
        errorembed = discord.Embed(
            title="❌ error",
            description=f"api request failed{str(e)}",
            color=discord.Color.red()
        )
        errorembed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
        await interaction.followup.send(embed=errorembed)
    except Exception as e:
        errorembed = discord.Embed(
            title="❌ Unexpected Error",
            description=f"Failed to fetch banner: {str(e)}",
            color=discord.Color.red()
        )
        errorembed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
        await interaction.followup.send(embed=errorembed)


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


@bot.tree.context_menu(name="discord2spook")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def discord2spook(interaction: discord.Interaction, user: discord.User):
    url = f"https://spook.bio/api/profiles?discordId={user.id}"
    print(user.id)
    print(url)
    response = requests.get(url)
    data = response.json()
    if response.status_code == 200:
        await interaction.response.send_message(f"{user.mention}'s [spook.bio Profile](https://spook.bio/@{data.get('username')})", ephemeral=False)
        print(f"Fetched {data['username']} successfully!")
    else:
        if interaction.user.name == user.name:
            await interaction.response.send_message(f":x: You don't have a spook.bio profile linked to your account {user.mention}! :x: To link your profile to your account create a spook.bio account [here](https://spook.bio/login)")
            return
        await interaction.response.send_message(f":x: {user.mention} doesn't have a spook.bio profile linked to their account! :x:", ephemeral=False)
        print(f"Error fetching data: {response.status_code}")


_MENUS = [("discord2spook", discord.AppCommandType.user)]       # right-click (context menu) commands in this file

_COMMANDS = [
    (bot.tree, "spookpfp"),
    (bot.tree, "discord2spook"),
    (bot.tree, "discorduser"),
    (bot.tree, "github"),
    (bot.tree, "discordavatar"),
    (bot.tree, "discordbanner"),
    (bot.tree, "instagram"),
]


class Profiles(commands.Cog, name="Profiles"):
    """profile lookups: Discord users, avatars and banners, spook.bio, GitHub and Instagram."""

    async def cog_unload(self):                  # so a reload (bot.reload_extension) re-registers cleanly
        for parent, name in _COMMANDS:
            parent.remove_command(name)
        for name, kind in _MENUS:
            bot.tree.remove_command(name, type=kind)


async def setup(bot):
    await bot.add_cog(Profiles(bot))

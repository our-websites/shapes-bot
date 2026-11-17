"""Cog: Roblox lookups: profiles, items, places, badges, outfits, limiteds, bundles, assets, avatars."""
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Optional
import aiohttp
import asyncio
import json
import re
import requests
import socket
import time
import urllib.parse
import yapper

m = sys.modules.get("main") or sys.modules["__main__"]   # this bot's main.py: shared helpers, data and the bot itself
Emojis = m.Emojis
MainURL = m.MainURL
app_commands = m.app_commands
bot = m.bot
discord = m.discord
embedDB = m.embedDB
fetch_user = m.fetch_user
get_badge_thumbnail = m.get_badge_thumbnail
isotodiscordtimestamp = m.isotodiscordtimestamp
roblox_colors = m.roblox_colors
shapes_api = m.shapes_api


def owner_of_roblox(roblox):
    """{"yapin_id", "discord_id"} of whoever linked this Roblox account; {} when nobody did, it is hidden, or the API is down."""
    data = shapes_api("GET", f"/internal/lookup/roblox/{urllib.parse.quote(str(roblox), safe='')}")
    if not data or not data.get("success") or data.get("hidden"):
        return {}
    return {"yapin_id": data.get("yapin_id") or data.get("data"), "discord_id": data.get("discord_id")}


# last online cache (Roblox)
def load_cached_timestamps():
    try:
        with open('lastonline.json', 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def update_cached_timestamp(user_id, timestamp):
    try:
        cached_timestamps = load_cached_timestamps()
        cached_timestamps[str(user_id)] = timestamp
        with open('lastonline.json', 'w') as f:
            json.dump(cached_timestamps, f, indent=2)
    except Exception as e:
        print(f"Error updating cached timestamp: {e}")


# --- robloxinfo ---
@bot.tree.command(name="robloxinfo", description="Get a Roblox user's profile information.")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def robloxinfo(interaction: discord.Interaction, user: str = "Roblox"):
    await interaction.response.defer(thinking=True)

    print(f"Searching For {user}'s profile")
    thinkingembed = discord.Embed(
        title=f"{Emojis.get('loading')} {interaction.user.mention} Searching For {user}'s Roblox profile!",
        color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
    )
    await interaction.followup.send(embed=thinkingembed)

    url = "https://users.roblox.com/v1/usernames/users"

    request_payload = {
        "usernames": [user],
        "excludeBannedUsers": False
    }

    async def check_ownership(session: aiohttp.ClientSession, user_id: str, asset_id: str) -> bool:
        url = f"https://inventory.roblox.com/v1/users/{user_id}/items/0/{asset_id}/is-owned"
        try:
            async with session.get(url, headers={
                'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36',
                'accept': 'application/json',
            }) as response:
                data = await response.json()
                return data is True
        except Exception:
            return False

    async def check_verification_items(session: aiohttp.ClientSession, user_id: str) -> bool:
        items = ['1567446', '18824203', '93078560', '102611803']   # the verified sign, then the hats
        results = await asyncio.gather(*(check_ownership(session, user_id, item) for item in items))
        return any(results)

    async def check_inventory_visibility(session: aiohttp.ClientSession, user_id: str) -> str:
        try:
            inventory_url = f"https://inventory.roblox.com/v1/users/{user_id}/can-view-inventory"
            async with session.get(inventory_url) as response:
                if response.status == 200:
                    inventory_data = await response.json()
                    return "Public" if inventory_data.get('canView', False) else "Private"
        except Exception as e:
            print(f"Error checking inventory visibility: {e}")
        return "Private"

    async def check_presence(session: aiohttp.ClientSession, user_id: str) -> tuple:
        try:
            try:
                with open('roblosecuritytoken.txt', 'r') as f:
                    roblosecurity_token = f.read().strip()
            except FileNotFoundError:
                return (None, None)

            presence_url = 'https://presence.roblox.com/v1/presence/users'
            headers = {
                "Content-Type": "application/json",
                "Cookie": f".ROBLOSECURITY={roblosecurity_token}"
            }

            async with session.post(presence_url, headers=headers, json={'userIds': [user_id]}) as response:
                if response.status == 200:
                    presence_data = await response.json()
                    if presence_data.get('userPresences') and len(presence_data['userPresences']) > 0:
                        user_presence = presence_data['userPresences'][0]
                        presence_type = user_presence.get('userPresenceType', 0)
                        place_id = user_presence.get('placeId')
                        return (presence_type, place_id)
        except Exception as e:
            print(f"Error checking presence: {e}")
        return (None, None)

    class BadgeService:
        def __init__(self, session: aiohttp.ClientSession):
            self.session = session

        async def get_user_badges(self, user_id: str):
            try:
                url = f"https://badges.roblox.com/v1/users/{user_id}/badges?sortOrder=Desc&limit=10"
                async with self.session.get(url) as response:
                    if response.status == 200:
                        data = await response.json()
                        return data.get('data', []), 200
                    return [], response.status
            except Exception as e:
                print(f"Error getting user badges: {e}")
                return [], 500

        async def get_awarded_dates(self, user_id: str, badge_ids: list):
            try:
                if not badge_ids:
                    return {}

                badge_ids_str = ",".join(map(str, badge_ids))
                url = f"https://badges.roblox.com/v1/users/{user_id}/badges/awarded-dates?badgeIds={badge_ids_str}"
                async with self.session.get(url) as response:
                    if response.status == 200:
                        data = await response.json()
                        awarded_dates = {}
                        for item in data.get('data', []):
                            awarded_dates[item['badgeId']] = item.get('awardedDate')
                        return awarded_dates
                    return {}
            except Exception as e:
                print(f"Error getting awarded dates: {e}")
                return {}

    class BadgeFormatter:
        @staticmethod
        def parse_iso_timestamp(iso_timestamp: str) -> int:
            try:
                if iso_timestamp:
                    dt = datetime.fromisoformat(iso_timestamp.replace('Z', '+00:00'))
                    return int(dt.timestamp())
            except (ValueError, AttributeError):
                pass
            return None

    async def get_last_online_from_badges(session: aiohttp.ClientSession, user_id: str) -> tuple:
        try:
            badge_service = BadgeService(session)
            badges, status_code = await badge_service.get_user_badges(user_id)

            if status_code == 200 and badges:
                badge_ids = [badge['id'] for badge in badges]
                awarded_dates = await badge_service.get_awarded_dates(user_id, badge_ids)

                for badge in badges:
                    badge_id = badge['id']
                    badge['awardedDate'] = awarded_dates.get(badge_id)

                valid_timestamps = []
                latest_place_id = None

                for badge in badges:
                    awarded_date = badge.get('awardedDate')
                    if awarded_date:
                        unix_timestamp = BadgeFormatter.parse_iso_timestamp(awarded_date)
                        if unix_timestamp:
                            valid_timestamps.append((unix_timestamp, badge))

                if valid_timestamps:
                    valid_timestamps.sort(key=lambda x: x[0], reverse=True)
                    latest_timestamp, latest_badge = valid_timestamps[0]

                    awarder = latest_badge.get('awarder', {})
                    if awarder.get('type') == 'Place':
                        latest_place_id = awarder.get('id')

                    return (latest_timestamp, latest_place_id)
        except Exception as e:
            print(f"Error fetching badge last online: {e}")
        return (None, None)

    async def get_friends_count(session: aiohttp.ClientSession, user_id: str) -> int:
        try:
            friends_url = f"https://friends.roblox.com/v1/users/{user_id}/friends/count"
            async with session.get(friends_url) as response:
                if response.status == 200:
                    friends_data = await response.json()
                    return friends_data.get('count', 0)
        except Exception as e:
            print(f"Error fetching friends count: {e}")
        return None

    async def get_followers_count(session: aiohttp.ClientSession, user_id: str) -> int:
        try:
            followers_url = f"https://friends.roblox.com/v1/users/{user_id}/followers/count"
            async with session.get(followers_url) as response:
                if response.status == 200:
                    followers_data = await response.json()
                    return followers_data.get('count', 0)
        except Exception as e:
            print(f"Error fetching followers count: {e}")
        return None

    async def get_followings_count(session: aiohttp.ClientSession, user_id: str) -> int:
        try:
            followings_url = f"https://friends.roblox.com/v1/users/{user_id}/followings/count"
            async with session.get(followings_url) as response:
                if response.status == 200:
                    followings_data = await response.json()
                    return followings_data.get('count', 0)
        except Exception as e:
            print(f"Error fetching followings count: {e}")
        return None

    async def get_user_games_visits(session: aiohttp.ClientSession, user_id: str) -> int:
        try:
            user_games_url = f"https://games.roblox.com/v2/users/{user_id}/games?accessFilter=2&limit=50"
            async with session.get(user_games_url) as response:
                if response.status == 200:
                    games_data = await response.json()
                    total_visits = 0

                    if games_data.get('data'):
                        games_list = games_data.get('data', [])
                        universe_ids = [game.get('id') for game in games_list if game.get('id') is not None]

                        if universe_ids:
                            visit_tasks = []
                            chunk_size = 50
                            for i in range(0, len(universe_ids), chunk_size):
                                chunk = universe_ids[i:i + chunk_size]
                                universe_ids_str = ",".join(map(str, chunk))
                                games_info_url = f"https://games.roblox.com/v1/games?universeIds={universe_ids_str}"
                                visit_tasks.append(session.get(games_info_url))

                            visit_responses = await asyncio.gather(*visit_tasks)
                            for response in visit_responses:
                                if response.status == 200:
                                    result = await response.json()
                                    for game in result.get('data', []):
                                        total_visits += game.get('visits', 0) or 0

                    return total_visits
        except Exception as e:
            print(f"Error fetching user games visits: {e}")
        return None

    async def get_profile_api_data(session: aiohttp.ClientSession, user_id: str) -> dict:
        try:
            with open('roblosecuritytoken.txt', 'r') as f:
                roblosecurity_token = f.read().strip()

            url = "https://apis.roblox.com/profile-platform-api/v1/profiles/get"
            payload = {
                "profileId": str(user_id),
                "profileType": "User",
                "components": [
                    {"component": "UserProfileHeader"},
                    {"component": "About"},
                    {"component": "RobloxBadges"},
                    {"component": "Statistics"}
                ],
                "includeComponentOrdering": True
            }

            headers = {
                "Content-Type": "application/json",
                "Cookie": f".ROBLOSECURITY={roblosecurity_token}",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            }

            async with session.post(url, json=payload, headers=headers) as response:
                if response.status == 200:
                    data = await response.json()
                    components = data.get('components', {})

                    profile_data = {}

                    user_profile_header = components.get('UserProfileHeader', {}) or {}
                    profile_data['is_premium'] = user_profile_header.get('isPremium', False)
                    profile_data['is_verified'] = user_profile_header.get('isVerified', False)

                    counts = user_profile_header.get('counts', {}) or {}
                    profile_data['friends_count'] = counts.get('friendsCount', 0)
                    profile_data['followers_count'] = counts.get('followersCount', 0)
                    profile_data['followings_count'] = counts.get('followingsCount', 0)

                    statistics = components.get('Statistics', {}) or {}
                    profile_data['visits'] = statistics.get('numberOfVisits', 0)
                    profile_data['join_date'] = statistics.get('userJoinedDate', '')

                    about = components.get('About', {}) or {}
                    profile_data['description'] = about.get('description', '')

                    roblox_badges = components.get('RobloxBadges', {}) or {}
                    badge_list = roblox_badges.get('robloxBadgeList', [])

                    excluded_badges = {'Homestead', 'Bricksmith'}
                    valid_badges = []

                    for badge in badge_list:
                        badge_type = badge.get('type', {})
                        badge_name = badge_type.get('value', '')

                        if badge_name not in excluded_badges:
                            valid_badges.append(badge)

                    profile_data['last_online'] = None
                    if valid_badges:
                        latest_timestamp = 0
                        for badge in valid_badges:
                            created_time = badge.get('createdTime', {})
                            seconds = created_time.get('seconds', 0)
                            if seconds > latest_timestamp:
                                latest_timestamp = seconds

                        if latest_timestamp > 0:
                            profile_data['last_online'] = latest_timestamp

                    return profile_data

        except FileNotFoundError:
            pass   # no .ROBLOSECURITY cookie file: the public endpoints fill the numbers in below
        except Exception as e:
            print(f"Error fetching profile API data: {e}")

        return {
            'visits': None,
            'friends_count': None,
            'followers_count': None,
            'followings_count': None,
            'is_verified': False,
            'is_premium': False,
            'description': '',
            'join_date': '',
            'last_online': None
        }

    def load_cached_timestamps():
        try:
            with open('lastonline.json', 'r') as f:
                return json.load(f)
        except FileNotFoundError:
            return {}

    def update_cached_timestamp(user_id, timestamp):
        try:
            cached_timestamps = load_cached_timestamps()
            cached_timestamps[str(user_id)] = timestamp
            with open('lastonline.json', 'w') as f:
                json.dump(cached_timestamps, f, indent=2)
        except Exception as e:
            print(f"Error updating cached timestamp: {e}")

    try:
        response = requests.post(url, json=request_payload)
        response.raise_for_status()
        data = response.json()
        if data.get("data") and len(data["data"]) > 0:
            userinfo = data["data"][0]
            UserID = userinfo["id"]
            Display = userinfo["displayName"]
            hasVerifiedBadge = userinfo.get("hasVerifiedBadge", False)

            profile_data = {'last_online': None}
            rap_value = None
            value_value = None
            rolimons_last_online = None
            badge_last_online = None
            badge_place_id = None
            is_premium = False
            is_verified = False
            roblox_badges = []
            total_visits = None
            inventory_visibility = "Private"
            friends_count = None
            followers_count = None
            followings_count = None
            presence_type = None
            current_place_id = None
            profile_description = ""

            try:
                connector = aiohttp.TCPConnector(family=socket.AF_INET)
                async with aiohttp.ClientSession(connector=connector) as session:
                    profile_data = await get_profile_api_data(session, UserID)

                    is_premium = profile_data['is_premium']
                    is_verified = profile_data['is_verified']
                    friends_count = profile_data['friends_count']
                    followers_count = profile_data['followers_count']
                    followings_count = profile_data['followings_count']
                    total_visits = profile_data['visits']
                    profile_description = profile_data['description']

                    # Without the profile API (no cookie) fall back to the public endpoints for every user.
                    if None in (friends_count, followers_count, followings_count, total_visits):
                        pub = await asyncio.gather(get_friends_count(session, UserID), get_followers_count(session, UserID),
                                                   get_followings_count(session, UserID), get_user_games_visits(session, UserID))
                        friends_count = pub[0] if friends_count is None else friends_count
                        followers_count = pub[1] if followers_count is None else followers_count
                        followings_count = pub[2] if followings_count is None else followings_count
                        total_visits = pub[3] if total_visits is None else total_visits

                    tasks = [
                        check_verification_items(session, UserID),
                        check_inventory_visibility(session, UserID),
                        check_presence(session, UserID),
                        get_last_online_from_badges(session, UserID)
                    ]

                    results = await asyncio.gather(*tasks)
                    is_verified = results[0]
                    inventory_visibility = results[1]
                    presence_result = results[2]
                    presence_type = presence_result[0]
                    current_place_id = presence_result[1]
                    badge_result = results[3]
                    badge_last_online = badge_result[0]
                    badge_place_id = badge_result[1]

                    badges_url = f"https://accountinformation.roblox.com/v1/users/{UserID}/roblox-badges"
                    async with session.get(badges_url) as response:
                        if response.status == 200:
                            badges_data = await response.json()
                            roblox_badges = badges_data

                    rolimons_stats_url = f"https://api.rolimons.com/players/v1/playerinfo/{UserID}"
                    async with session.get(rolimons_stats_url, headers={'User-Agent': 'Shapes (Yapin bot)'}) as response:
                        if response.status == 200:
                            rolimons_stats_data = await response.json()
                            if rolimons_stats_data.get('success'):
                                rap_value = rolimons_stats_data.get('rap') or 0
                                value_value = rolimons_stats_data.get('value') or 0
                                rolimons_last_online = rolimons_stats_data.get('last_online')
            except Exception as e:
                print(f"Error fetching data: {e}")

            rolimonsurl = f"https://rolimons.com/player/{UserID}"

            url = f"https://users.roblox.com/v1/users/{UserID}"
            try:
                response = requests.get(url)
                response.raise_for_status()
                playerdata = response.json()
                Description = profile_description or playerdata["description"]
                Banned = playerdata["isBanned"]
                user = playerdata["name"]
                JoinDate = playerdata["created"]
                created_timestamp = isotodiscordtimestamp(JoinDate)
            except requests.exceptions.RequestException as e:
                print(f"Error fetching user data for ID {UserID}: {e}")
                failedembed = discord.Embed(
                    title=f":x: An error occurred while fetching data for user ID: {UserID}. Please try again later.",
                    color=discord.Color.red()
                )
                await interaction.edit_original_response(embed=failedembed)
                return

            if Display == user:
                Username = Display
            else:
                Username = f"{Display} (@{user})"

            if UserID == 124767284:
                hasVerifiedBadge = True

            if hasVerifiedBadge:
                Username += " <:RobloxVerified:1416951927513677874>"

            if is_premium:
                Username += " <:RobloxPremium:1416951078200541378>"

            is_terminated = False
            avatar_image = None

            try:
                avatar_url = f"https://thumbnails.roblox.com/v1/users/avatar?userIds={UserID}&size=420x420&format=Png&isCircular=false"
                response = await asyncio.to_thread(requests.get, avatar_url, timeout=10)
                response.raise_for_status()
                data = response.json()
                if data and data.get("data") and len(data["data"]) > 0:
                    avatar_image = data["data"][0].get("imageUrl")
                    if avatar_image and avatar_image.startswith("https://t7.rbxcdn.com"):
                        is_terminated = True

                        connector = aiohttp.TCPConnector(family=socket.AF_INET)
                        async with aiohttp.ClientSession(connector=connector) as session:
                            custom_avatar = await render_custom_avatar(session, UserID)
                            if custom_avatar:
                                avatar_image = custom_avatar
            except requests.exceptions.RequestException as e:
                print(f"Error fetching avatar: {e}")

            if is_terminated:
                Username = f":warning: [Banned] {Username}"

                try:
                    connector = aiohttp.TCPConnector(family=socket.AF_INET)
                    async with aiohttp.ClientSession(connector=connector) as session:
                        terminated_tasks = [
                            get_friends_count(session, UserID),
                            get_followers_count(session, UserID),
                            get_followings_count(session, UserID),
                            get_user_games_visits(session, UserID)
                        ]

                        terminated_results = await asyncio.gather(*terminated_tasks)
                        friends_count = terminated_results[0]
                        followers_count = terminated_results[1]
                        followings_count = terminated_results[2]
                        total_visits = terminated_results[3]
                except Exception as e:
                    print(f"Error fetching terminated user data: {e}")

            Discord = None
            owner = await asyncio.to_thread(owner_of_roblox, UserID)       # hidden accounts come back empty
            if owner.get("yapin_id"):
                person = await asyncio.to_thread(fetch_user, owner["yapin_id"])
                Discord = f"@{person['username']}" if person.get("username") else owner["yapin_id"]
            if owner.get("discord_id"):
                Discord = f"{Discord}\nDiscord: {owner['discord_id']}" if Discord else f"Discord: {owner['discord_id']}"

            profileurl = f"https://www.roblox.com/users/{UserID}/profile"

            cached_timestamps = load_cached_timestamps()
            cached_timestamp = cached_timestamps.get(str(UserID))

            if presence_type in [1, 2, 3]:
                current_time = int(time.time())
                update_cached_timestamp(UserID, current_time)
                cached_timestamp = current_time

            timestamps = []

            if cached_timestamp:
                timestamps.append((cached_timestamp, "Cached"))

            if badge_last_online:
                timestamps.append((badge_last_online, "Badge Activity"))

            if profile_data['last_online']:
                timestamps.append((profile_data['last_online'], "Profile API"))

            if rolimons_last_online:
                timestamps.append((rolimons_last_online, "Rolimons Data"))

            formatted_last_online = "Unknown"
            if timestamps:
                timestamps.sort(key=lambda x: x[0], reverse=True)
                last_online_timestamp, last_online_source = timestamps[0]
                formatted_last_online = f"<t:{last_online_timestamp}:D>"

            badges = {
                "Combat Initiation": "<:CombatInitiation:1430627878898368632>",
                "Administrator": "<:RobloxAdmin:1416951128876122152>",
                "Bloxxer": "<:Bloxxer:1430627881301704736>",
                "Warrior": "<:Warrior:1430640757403943063>",
                "Official Model Maker": "<:RobloxModelMaker:1416952360852263013>",
                "Bricksmith": "<:Roblox1000Visits:1416952101229170698>",
                "Homestead": "<:Roblox100Visits:1416952056324952184>",
                "Inviter": "<:RobloxInviter:1416952415772479559>",
                "Ambassador": "<:Ambassador:1430627877337960548>",
                "Friendship": "<:Friendship:1430641140679577630>",
                "Veteran": "<:RobloxVeteran:1416952185094406264>",
                "Welcome To The Club": "<:WelcomeToTheClub:1430627875337273525>"
            }

            badges_display = "None"
            if roblox_badges and len(roblox_badges) > 0:
                # Yapin has none of the Discord custom emoji, so badges are listed by name.
                badge_names = [b.get("name", "") for b in roblox_badges if b.get("name")]
                badges_display = ", ".join(badge_names) if badge_names else "None"

            presence_status_map = {      # the status shows as an emoji in the footer text (footer pictures from other hosts do not load)
                0: {'text': '⚫ Offline'},
                1: {'text': '🟢 Online'},
                2: {'text': '🎮 In Game'},
                3: {'text': '🛠️ In Studio'},
                None: {'text': '⚪ Status unknown'},
            }
            current_status = presence_status_map.get(presence_type, presence_status_map[None])

            def num(n):
                return f"{n:,}" if isinstance(n, (int, float)) else "N/A"


            view = discord.ui.View()
            if not is_terminated:
                view.add_item(discord.ui.Button(
                    label="Roblox Profile",
                    style=discord.ButtonStyle.link,
                    emoji="<:RobloxLogo:1416951004607418398>",
                    url=profileurl
                ))
            view.add_item(discord.ui.Button(
                label="View Rolimons",
                style=discord.ButtonStyle.link,
                emoji="<:RolimonsLogo:1417258794974711901>",
                url=rolimonsurl
            ))

            friends_followers_text = f"**{num(friends_count)}** Friends | **{num(followers_count)}** Followers | **{num(followings_count)}** Following"

            full_description = f"{friends_followers_text}\n\n{Description}"

            embed = discord.Embed(
                title=Username,
                url=profileurl,
                description=full_description,
                color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
            )

            if is_terminated:
                embed.add_field(name="ID", value=f"{UserID}", inline=True)
                embed.add_field(name="Terminated", value="True", inline=True)
                embed.add_field(name="Visits", value=num(total_visits), inline=True)

                embed.add_field(name="Created", value=created_timestamp if created_timestamp else "Unknown", inline=True)
                embed.add_field(name="Last Online", value=formatted_last_online, inline=True)
                embed.add_field(name="Badges", value=badges_display, inline=True)
            else:
                embed.add_field(name="ID", value=f"{UserID}", inline=True)
                embed.add_field(name="Verified", value="Hat" if is_verified else ("Unknown" if inventory_visibility == "Private" else "False"), inline=True)
                embed.add_field(name="Inventory", value=inventory_visibility, inline=True)

                embed.add_field(name="RAP", value=f"[`{num(rap_value)}`]({rolimonsurl})", inline=True)
                embed.add_field(name="Value", value=f"[`{num(value_value)}`]({rolimonsurl})", inline=True)
                embed.add_field(name="Visits", value=num(total_visits), inline=True)

                embed.add_field(name="Created", value=created_timestamp if created_timestamp else "Unknown", inline=True)
                embed.add_field(name="Last Online", value=formatted_last_online, inline=True)
                embed.add_field(name="Badges", value=badges_display, inline=True)

            if current_place_id and presence_type == 2:
                game_url = f"https://www.roblox.com/games/{current_place_id}"
                embed.add_field(
                    name="Current Game",
                    value=f"[Click to View Game]({game_url})",
                    inline=False
                )

            if Discord != None:
                embed.add_field(
                    name="Owner",
                    value=f"```txt\n{Discord}\n```",
                    inline=False
                )

            if avatar_image:
                embed.set_thumbnail(url=avatar_image)

            embed.set_footer(
                text=f"{current_status['text']} | Requested by {interaction.user.name} | {MainURL}"
            )

            await interaction.edit_original_response(embed=embed, view=view)
            return
        else:
            print(f"{user} not found.")
            failedembed7 = discord.Embed(
                title=f":warning: {user} not found.",
                color=discord.Color.yellow()
            )
            await interaction.edit_original_response(embed=failedembed7)
            return
    except requests.exceptions.RequestException as e:
        print(f"An error occurred during the request: {e}")
        failedembed8 = discord.Embed(
            title=f":warning: {user} not found.",
            color=discord.Color.yellow()
        )
        await interaction.edit_original_response(embed=failedembed8)
        return


# --- iteminfo ---
@bot.tree.command(name="iteminfo", description="Get detailed information about a Roblox item")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def item(interaction: discord.Interaction, item_query: str = "Dominus Empyreus"):
    await interaction.response.defer(thinking=True)

    print(f"Searching For {item_query}'s item info")
    thinkingembed = discord.Embed(
        title=f"{Emojis.get('loading')}  {interaction.user.mention} Searching For {item_query}'s Item Information!",
        color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
    )
    await interaction.followup.send(embed=thinkingembed)

    if not item_query.isdigit():
        search_url = f"https://catalog.roblox.com/v1/search/items?category=All&limit=10&keyword={urllib.parse.quote(item_query)}"

        try:
            response = requests.get(search_url)
            response.raise_for_status()
            search_data = response.json()

            if search_data.get("data") and len(search_data["data"]) > 0:
                item_id = None
                for item in search_data["data"]:
                    if item.get("name", "").lower() == item_query.lower():
                        item_id = item.get("id")
                        break

                if not item_id:
                    item_id = search_data["data"][0].get("id")

                if not item_id:
                    failedembed = discord.Embed(
                        title=f":warning: {item_query} not found.",
                        color=discord.Color.yellow()
                    )
                    await interaction.edit_original_response(embed=failedembed)
                    return
            else:
                failedembed = discord.Embed(
                    title=f":warning: {item_query} not found.",
                    color=discord.Color.yellow()
                )
                await interaction.edit_original_response(embed=failedembed)
                return

        except requests.exceptions.RequestException as e:
            print(f"Error searching for item {item_query}: {e}")
            failedembed = discord.Embed(
                title=f":x: An error occurred while searching for {item_query}. Please try again later.",
                color=discord.Color.red()
            )
            await interaction.edit_original_response(embed=failedembed)
            return
    else:
        item_id = item_query

    url = f"https://catalog.roblox.com/v1/catalog/items/{item_id}/details?itemType=Asset"

    try:
        response = requests.get(url)
        response.raise_for_status()
        item_data = response.json()
        print(f"ItemData: {item_data}")

        name = item_data.get("name", "Unknown Item")
        description = item_data.get("description", "No description available")
        creator_name = item_data.get("creatorName", "Unknown Creator")
        creator_type = item_data.get("creatorType", "User")
        creator_verified = item_data.get("creatorHasVerifiedBadge", False)
        creator_target_id = item_data.get("creatorTargetId")
        favorite_count = item_data.get("favoriteCount", 0)
        lowest_price = item_data.get("lowestPrice", 0)
        is_purchasable = item_data.get("isPurchasable", False)
        item_type = item_data.get("itemType", "Asset")

        created_date = item_data.get("itemCreatedUtc")

        if creator_type == "Group" and creator_target_id == 5544706:
            creator_verified = True

        if creator_type == "User" and creator_target_id == 124767284:
            creator_verified = True

        if created_date:
            created_timestamp = isotodiscordtimestamp(created_date, "F")
        else:
            created_timestamp = "Unknown"

        updated_timestamp = created_timestamp

        creator_display = creator_name
        if creator_verified:
            creator_display += " <:RobloxVerified:1416951927513677874>"
        else:
            creator_display += f" ({creator_type})"

        price_display = "Not for sale"
        if is_purchasable and lowest_price is not None:
            price_display = "Free" if lowest_price == 0 else f"{lowest_price:,} Robux"

        thumbnail_url = f"https://thumbnails.roblox.com/v1/assets?assetIds={item_id}&size=420x420&format=Png"
        try:
            thumb_response = requests.get(thumbnail_url)
            thumb_response.raise_for_status()
            thumb_data = thumb_response.json()
            if thumb_data and thumb_data.get("data") and len(thumb_data["data"]) > 0:
                image_url = thumb_data["data"][0].get("imageUrl")
            else:
                image_url = None
        except requests.exceptions.RequestException as e:
            print(f"Error fetching item thumbnail: {e}")
            image_url = None

        embed = discord.Embed(
            title=name,
            url=f"https://www.roblox.com/catalog/{item_id}/",
            description=description,
            color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
        )

        if image_url:
            embed.set_thumbnail(url=image_url)

        embed.add_field(name="Item ID", value=item_id, inline=True)
        embed.add_field(name="Item Type", value=item_type, inline=True)
        embed.add_field(name="Price", value=price_display, inline=True)
        embed.add_field(name="Creator", value=creator_display, inline=True)
        embed.add_field(name="Favorites", value=f"{favorite_count:,}", inline=True)
        embed.add_field(name="Created", value=created_timestamp, inline=True)

        view = discord.ui.View()
        view.add_item(discord.ui.Button(
            label="View Item",
            style=discord.ButtonStyle.link,
            emoji="<:RobloxLogo:1416951004607418398>",
            url=f"https://www.roblox.com/catalog/{item_id}/"
        ))

        embed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
        await interaction.edit_original_response(embed=embed, view=view)

    except requests.exceptions.RequestException as e:
        print(f"Error fetching item data for ID {item_id}: {e}")
        failedembed = discord.Embed(
            title=f":x: An error occurred while fetching data for item ID: {item_id}. Please try again later.",
            color=discord.Color.red()
        )
        await interaction.edit_original_response(embed=failedembed)
        return


# --- placeinfo+badge ---
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@bot.tree.command(name="placeinfo", description="Get detailed information about a Roblox place")
@app_commands.describe(game_input="Roblox place ID or game URL")
async def placeinfo(interaction: discord.Interaction, game_input: str):
    await interaction.response.defer(thinking=True)

    thinkingembed = discord.Embed(
        title=f"{Emojis.get('loading')}  {interaction.user.mention} Searching For Place!",
        color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
    )
    await interaction.followup.send(embed=thinkingembed)

    try:
        connector = aiohttp.TCPConnector(family=socket.AF_INET)
        async with aiohttp.ClientSession(connector=connector) as session:
            place_id = None

            if "roblox.com/games/" in game_input:
                match = re.search(r'roblox\.com/games/(\d+)', game_input)
                if match:
                    place_id = match.group(1)
                else:
                    errorembed = discord.Embed(
                        title=":x: Invalid URL :x:",
                        description="Could not extract place ID from the URL",
                        color=discord.Color.red()
                    )
                    await interaction.edit_original_response(embed=errorembed)
                    return
            else:
                if not game_input.isdigit():
                    errorembed = discord.Embed(
                        title=":x: Invalid Input :x:",
                        description="Please provide a valid place ID or Roblox game URL",
                        color=discord.Color.red()
                    )
                    await interaction.edit_original_response(embed=errorembed)
                    return
                place_id = game_input

            universe_url = f"https://apis.roblox.com/universes/v1/places/{place_id}/universe"
            async with session.get(universe_url) as response:
                if response.status != 200:
                    errorembed = discord.Embed(
                        title=":x: API Error :x:",
                        description=f"Failed to fetch universe information (Status: {response.status})",
                        color=discord.Color.red()
                    )
                    await interaction.edit_original_response(embed=errorembed)
                    return

                universe_data = await response.json()
                universe_id = universe_data.get('universeId')

                if not universe_id:
                    errorembed = discord.Embed(
                        title=":x: Not Found :x:",
                        description="Could not find universe for this place ID",
                        color=discord.Color.red()
                    )
                    await interaction.edit_original_response(embed=errorembed)
                    return

            games_url = f"https://games.roblox.com/v1/games?universeIds={universe_id}"
            async with session.get(games_url) as response:
                if response.status != 200:
                    errorembed = discord.Embed(
                        title=":x: API Error :x:",
                        description=f"Failed to fetch game details (Status: {response.status})",
                        color=discord.Color.red()
                    )
                    errorembed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
                    await interaction.edit_original_response(embed=errorembed)
                    return

                games_data = await response.json()
                if not games_data.get('data') or len(games_data['data']) == 0:
                    errorembed = discord.Embed(
                        title=":x: Not Found :x:",
                        description="Could not find game details for this universe",
                        color=discord.Color.red()
                    )
                    errorembed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
                    await interaction.edit_original_response(embed=errorembed)
                    return

                game_info = games_data['data'][0]

            name = game_info.get('name', 'Unknown')
            description = game_info.get('description', 'No description available')
            creator_id = game_info.get('creator', {}).get('id', 'Unknown')
            creator_name = game_info.get('creator', {}).get('name', 'Unknown')
            creator_type = game_info.get('creator', {}).get('type', 'User')
            current_players = game_info.get('playing', 0)
            visits = game_info.get('visits', 0)
            max_players = game_info.get('maxPlayers', 0)
            created = game_info.get('created', 'Unknown')
            updated = game_info.get('updated', 'Unknown')
            genre = game_info.get('genre', 'Unknown')
            favorites_count = game_info.get('favoritedCount', 0)

            created_timestamp = isotodiscordtimestamp(created, "F") if created != 'Unknown' else "Unknown"
            updated_timestamp = isotodiscordtimestamp(updated, "F") if updated != 'Unknown' else "Unknown"

            thumbnail_url = f"https://thumbnails.roblox.com/v1/games/icons?universeIds={universe_id}&size=512x512&format=Png&isCircular=false"
            thumbnail_image = None
            async with session.get(thumbnail_url) as response:
                if response.status == 200:
                    thumbnail_data = await response.json()
                    if thumbnail_data.get('data') and len(thumbnail_data['data']) > 0:
                        thumbnail_image = thumbnail_data['data'][0]['imageUrl']

            embed = discord.Embed(
                title=name,
                url=f"https://www.roblox.com/games/{place_id}/",
                description=description,
                color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
            )

            embed.add_field(name="Place ID", value=f"`{place_id}`", inline=True)
            embed.add_field(name="Universe ID", value=f"`{universe_id}`", inline=True)

            if creator_type.lower() == "user":
                creator_display = f"[{creator_name}](https://www.roblox.com/users/{creator_id}/profile)"
            else:
                creator_display = f"[{creator_name}](https://www.roblox.com/groups/{creator_id}/)"
            embed.add_field(name="Creator", value=creator_display, inline=True)

            embed.add_field(name="Visits", value=f"{visits:,}", inline=True)
            embed.add_field(name="Current Players", value=f"{current_players:,}", inline=True)
            embed.add_field(name="Max Players", value=f"{max_players}", inline=True)

            embed.add_field(name="Favorites", value=f"{favorites_count:,}", inline=True)
            embed.add_field(name="Genre", value=genre, inline=True)

            embed.add_field(name="Created", value=created_timestamp, inline=True)
            embed.add_field(name="Updated", value=updated_timestamp, inline=True)

            if description and description != "No description available":
                if len(description) > 1024:
                    description = description[:1021] + "..."
                if len(embed.fields) % 3 != 0:
                    embed.add_field(name="\u200b", value="\u200b", inline=True)
                embed.add_field(name="Description", value=description, inline=False)

            if thumbnail_image:
                embed.set_thumbnail(url=thumbnail_image)

            view = discord.ui.View()
            view.add_item(discord.ui.Button(
                label="View Game",
                style=discord.ButtonStyle.link,
                emoji="<:RobloxLogo:1416951004607418398>",
                url=f"https://www.roblox.com/games/{place_id}/"
            ))

            embed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
            await interaction.edit_original_response(embed=embed, view=view)

    except Exception as e:
        print(f"Error in placeinfo command: {e}")
        errorembed = discord.Embed(
            title=":x: An error occurred :x:",
            description="Failed to fetch place information. Please try again later.",
            color=discord.Color.red()
        )
        errorembed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
        await interaction.edit_original_response(embed=errorembed)
        return


async def send_error_embed(interaction: discord.Interaction, title: str, description: str):
    embed = discord.Embed(
        title=f":x: {title}",
        description=description,
        color=discord.Color.red()
    )
    #embed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
    await interaction.followup.send(embed=embed)


async def create_badge_embed(badge_data: dict, thumbnail_url: Optional[str], badge_id: str, 
                           start_time: float, requester: discord.User) -> discord.Embed:
    badge_id = badge_data.get('id', 'N/A')
    name = badge_data.get('name', 'Unknown')
    display_name = badge_data.get('displayName', name)
    description = badge_data.get('description') or badge_data.get('displayDescription') or "No description"

    statistics = badge_data.get('statistics', {})
    past_day_awarded = statistics.get('pastDayAwardedCount', 0)
    awarded_count = statistics.get('awardedCount', 0)
    win_rate = statistics.get('winRatePercentage', 0)

    created = badge_data.get('created', 'Unknown')
    updated = badge_data.get('updated', 'Unknown')

    awarding_universe = badge_data.get('awardingUniverse', {})
    universe_name = awarding_universe.get('name', 'Unknown')
    universe_id = awarding_universe.get('id', 'N/A')
    root_place_id = awarding_universe.get('rootPlaceId', 'N/A')

    embed_color = embedDB.get(f"{requester.id}") if embedDB.get(f"{requester.id}") else discord.Color.blue()

    embed = discord.Embed(
        title=f"Badge: {display_name}",
        color=embed_color,
        timestamp=datetime.now(),
        url=f"https://www.roblox.com/badges/{badge_id}"
    )

    if thumbnail_url:
        embed.set_thumbnail(url=thumbnail_url)

    embed.add_field(name="Badge ID", value=f"`{badge_id}`", inline=True)
    embed.add_field(name="Internal Name", value=name, inline=True)
    embed.add_field(name="Enabled", value="Yes" if badge_data.get('enabled') else "No", inline=True)

    embed.add_field(name="Description", value=description, inline=False)

    embed.add_field(name="Awarded Today", value=f"{past_day_awarded:,}", inline=True)
    embed.add_field(name="Obtained Total", value=f"{awarded_count:,}", inline=True)
    embed.add_field(name="Win Rate", value=f"{win_rate}%", inline=True)

    created_timestamp = isotodiscordtimestamp(created)
    updated_timestamp = isotodiscordtimestamp(updated)

    if created_timestamp:
        embed.add_field(
            name="Created",
            value=f"{created_timestamp} {created_timestamp})", 
            inline=True
        )
    else:
        embed.add_field(name="Created", value=created, inline=True)

    if updated_timestamp:
        embed.add_field(
            name="Last Updated", 
            value=f"<t:{updated_timestamp}:f> (<t:{updated_timestamp}:R>)", 
            inline=True
        )
    else:
        embed.add_field(name="Last Updated", value=updated, inline=True)

    if universe_id != 'N/A':
        universe_field = f"[{universe_name}](https://www.roblox.com/games/{root_place_id}/)"
        universe_field += f"\nUniverse ID: `{universe_id}`"
        if root_place_id != 'N/A':
            universe_field += f"\nPlace ID: `{root_place_id}`"

        embed.add_field(name="Awarding Universe", value=universe_field, inline=False)

    elapsed_time = asyncio.get_event_loop().time() - start_time
    embed.set_footer(text=f"Load time: {elapsed_time:.2f}s • Requested by {requester.display_name} | {MainURL}")

    return embed


@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@bot.tree.command(name="badge", description="Get detailed information about a Roblox badge")
@app_commands.describe(badge_id="The ID of the Roblox badge")
async def badge_info(interaction: discord.Interaction, badge_id: str):
    await interaction.response.defer(thinking=True)
    start_time = asyncio.get_event_loop().time()

    thinkingembed = discord.Embed(
    title=f"{Emojis.get('loading')}  {interaction.user.mention} Searching For Place!",
    color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
    )
    await interaction.followup.send(embed=thinkingembed)

    if not badge_id.isdigit():
        await send_error_embed(
            interaction,
            "Invalid Badge ID",
            "Please provide a valid numeric badge ID."
        )
        return

    connector = aiohttp.TCPConnector(family=socket.AF_INET)
    async with aiohttp.ClientSession(connector=connector) as session:
        try:
            badge_url = f"https://badges.roblox.com/v1/badges/{badge_id}"

            async with session.get(badge_url) as response:
                if response.status == 404:
                    await send_error_embed(
                        interaction,
                        "Badge Not Found",
                        f"Could not find a badge with ID `{badge_id}`"
                    )
                    return
                elif response.status != 200:
                    await send_error_embed(
                        interaction,
                        "API Error",
                        f"Failed to fetch badge information (Status: {response.status})"
                    )
                    return

                badge_data = await response.json()

            thumbnail_url = await get_badge_thumbnail(session, badge_id)

            embed = await create_badge_embed(badge_data, thumbnail_url, badge_id, start_time, interaction.user)

            view = discord.ui.View()
            view.add_item(discord.ui.Button(
                label="View Badge",
                style=discord.ButtonStyle.link,
                emoji="<:RobloxLogo:1416951004607418398>",
                url=f"https://www.roblox.com/badges/{badge_id}"
            ))

            await interaction.edit_original_response(embed=embed, view=view)

        except Exception as e:
            await send_error_embed(interaction, "Unexpected Error", f"An error occurred: {str(e)}")


# --- outfits ---
@bot.tree.command(name="outfits", description="Get all outfits for a Roblox user")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def outfits(interaction: discord.Interaction, user: str = "Roblox"):
    await interaction.response.defer(thinking=True)
    
    print(f"Searching For {user}'s outfits")
    thinkingembed = discord.Embed(
        title=f"{Emojis.get('loading')}  {interaction.user.mention} Searching For {user}'s Roblox outfits!",
        color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
    )
    await interaction.followup.send(embed=thinkingembed)

    url = "https://users.roblox.com/v1/usernames/users"
    
    request_payload = {
        "usernames": [user],
        "excludeBannedUsers": False
    }

    async def get_outfit_thumbnails(session: aiohttp.ClientSession, outfit_ids: list) -> dict:
        """Get thumbnails for multiple outfits"""
        thumbnails = {}
        if not outfit_ids:
            return thumbnails
            
        for i in range(0, len(outfit_ids), 100):
            chunk = outfit_ids[i:i+100]
            ids_param = ",".join(str(outfit_id) for outfit_id in chunk)
            
            thumbnails_url = f"https://thumbnails.roblox.com/v1/users/outfits?userOutfitIds={ids_param}&size=420x420&format=Png"
            
            try:
                async with session.get(thumbnails_url) as response:
                    if response.status == 200:
                        thumbnails_data = await response.json()
                        for item in thumbnails_data.get('data', []):
                            thumbnails[item.get("targetId")] = item.get("imageUrl", "")
            except Exception as e:
                print(f"Error fetching thumbnails: {e}")
            
            if i + 100 < len(outfit_ids):
                await asyncio.sleep(1)
                
        return thumbnails

    try:
        response = requests.post(url, json=request_payload)
        response.raise_for_status()
        data = response.json()
        if data.get("data") and len(data["data"]) > 0:
            userinfo = data["data"][0]
            UserID = userinfo["id"]
            Display = userinfo["displayName"]
            hasVerifiedBadge = userinfo.get("hasVerifiedBadge", False)

            username = Display
            user_id = UserID

            if UserID == 124767284:
                hasVerifiedBadge = True

            outfits_url = f"https://avatar.roblox.com/v1/users/{user_id}/outfits?itemsPerPage=150&isEditable=true"
            
            try:
                connector = aiohttp.TCPConnector(family=socket.AF_INET)
                async with aiohttp.ClientSession(connector=connector) as session:
                    async with session.get(outfits_url) as response:
                        if response.status == 429:
                            failedembed = discord.Embed(
                                title=f":warning: Rate Limited",
                                description="Too many requests. Please try again in a few seconds.",
                                color=discord.Color.yellow()
                            )
                            await interaction.edit_original_response(embed=failedembed)
                            return
                        elif response.status != 200:
                            failedembed = discord.Embed(
                                title=f":warning: Failed to fetch outfits",
                                description=f"API returned status code: {response.status}",
                                color=discord.Color.yellow()
                            )
                            await interaction.edit_original_response(embed=failedembed)
                            return
                        
                        outfits_data = await response.json()
            except Exception as e:
                print(f"Error fetching outfits: {e}")
                failedembed = discord.Embed(
                    title=f":x: An error occurred while fetching outfits",
                    description="Please try again later.",
                    color=discord.Color.red()
                )
                await interaction.edit_original_response(embed=failedembed)
                return

            outfits = outfits_data.get('data', [])
            
            if not outfits:
                embed = discord.Embed(
                    title=f"{username}'s Outfits",
                    description="This user has no outfits.",
                    color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
                )
                embed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
                await interaction.edit_original_response(embed=embed)
                return

            outfit_ids = [outfit.get('id') for outfit in outfits if outfit.get('id')]
            outfit_thumbnails = {}
            
            try:
                connector = aiohttp.TCPConnector(family=socket.AF_INET)
                async with aiohttp.ClientSession(connector=connector) as session:
                    outfit_thumbnails = await get_outfit_thumbnails(session, outfit_ids)
            except Exception as e:
                print(f"Error fetching outfit thumbnails: {e}")

            class OutfitsView(discord.ui.View):
                def __init__(self, outfits, thumbnails, username, user_id, requester):
                    super().__init__(timeout=120)
                    self.outfits = outfits
                    self.thumbnails = thumbnails
                    self.current_page = 0
                    self.username = username
                    self.user_id = user_id
                    self.requester = requester
                    self.message = None
                    self.update_buttons()
                    
                async def create_embed(self) -> discord.Embed:
                    outfit = self.outfits[self.current_page]
                    outfit_id = outfit.get('id')
                    outfit_name = outfit.get('name', 'Unnamed Outfit')
                    created_date = outfit.get('created', '')
                    is_editable = outfit.get('isEditable', False)
                    
                    thumbnail_url = self.thumbnails.get(outfit_id, "")
                    
                    embed = discord.Embed(
                        title=f"{self.username}'s Outfits",
                        color=embedDB.get(f"{self.requester.id}") if embedDB.get(f"{self.requester.id}") else discord.Color.blue()
                    )
                    
                    if thumbnail_url:
                        embed.set_image(url=thumbnail_url)
                    else:
                        embed.description = "*Thumbnail not available*"
                    
                    embed.add_field(name="Outfit Name", value=outfit_name, inline=False)
                    embed.add_field(name="Outfit ID", value=f"`{outfit_id}`", inline=True)
                    embed.add_field(name="Editable", value="Yes" if is_editable else "No", inline=True)
                    
                    if created_date:
                        try:
                            created_timestamp = isotodiscordtimestamp(created_date, "F")
                            if created_timestamp:
                                embed.add_field(name="Created", value=created_timestamp, inline=True)
                        except:
                            pass
                    
                    embed.set_footer(
                        text=f"Outfit {self.current_page + 1}/{len(self.outfits)} | Requested by {self.requester.name} | {MainURL}"
                    )
                    
                    return embed
                
                def update_buttons(self):
                    self.clear_items()
                    
                    if self.current_page > 0:
                        previous_btn = discord.ui.Button(style=discord.ButtonStyle.primary, emoji="⬅️", custom_id="previous")
                        previous_btn.callback = self.previous_callback
                        self.add_item(previous_btn)
                        
                    if self.current_page < len(self.outfits) - 1:
                        next_btn = discord.ui.Button(style=discord.ButtonStyle.primary, emoji="➡️", custom_id="next")
                        next_btn.callback = self.next_callback
                        self.add_item(next_btn)
                    
                    current_outfit = self.outfits[self.current_page]
                    outfit_id = current_outfit.get('id')
                    if outfit_id:
                        link_btn = discord.ui.Button(
                            style=discord.ButtonStyle.link,
                            label="View Outfit",
                            url=f"https://www.roblox.com/outfits/{outfit_id}"
                        )
                        self.add_item(link_btn)
                
                async def previous_callback(self, interaction: discord.Interaction):
                    if interaction.user != self.requester:
                        await interaction.response.send_message("This is not your command!", ephemeral=True)
                        return
                        
                    self.current_page -= 1
                    embed = await self.create_embed()
                    self.update_buttons()
                    await interaction.response.edit_message(embed=embed, view=self)
                
                async def next_callback(self, interaction: discord.Interaction):
                    if interaction.user != self.requester:
                        await interaction.response.send_message("This is not your command!", ephemeral=True)
                        return
                        
                    self.current_page += 1
                    embed = await self.create_embed()
                    self.update_buttons()
                    await interaction.response.edit_message(embed=embed, view=self)
                
                async def on_timeout(self):
                    for item in self.children:
                        if isinstance(item, discord.ui.Button) and item.style != discord.ButtonStyle.link:
                            item.disabled = True
                    try:
                        await self.message.edit(view=self)
                    except Exception:
                        pass

            view = OutfitsView(outfits, outfit_thumbnails, username, user_id, interaction.user)
            embed = await view.create_embed()
            message = await interaction.edit_original_response(embed=embed, view=view)
            view.message = message
            return
        else:
            print(f"{user} not found.")
            failedembed = discord.Embed(
                title=f":warning: {user} not found.",
                color=discord.Color.yellow()
            )
            await interaction.edit_original_response(embed=failedembed)
            return
    except requests.exceptions.RequestException as e:
        print(f"An error occurred during the request: {e}")
        failedembed = discord.Embed(
            title=f":warning: {user} not found.",
            color=discord.Color.yellow()
        )
        await interaction.edit_original_response(embed=failedembed)
        return


# --- badges ---
@bot.tree.command(name="badges", description="Check if a user has a specific badge and get detailed information")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(
    username="Roblox username to check",
    badge_id="The badge ID to look for (optional)"
)
async def badges(interaction: discord.Interaction, username: str, badge_id: str = None):
    await interaction.response.defer(thinking=True)
    
    thinkingembed = discord.Embed(
        title=f"{Emojis.get('loading')}  {interaction.user.mention} Checking {username}'s badges!",
        color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
    )
    await interaction.followup.send(embed=thinkingembed)

    async def get_user_id():
        user_url = "https://users.roblox.com/v1/usernames/users"
        request_payload = {"usernames": [username], "excludeBannedUsers": False}
        async with aiohttp.ClientSession() as session:
            async with session.post(user_url, json=request_payload) as response:
                if response.status == 200:
                    data = await response.json()
                    return data, None
                return None, f"Failed to fetch user data (Status: {response.status})"

    async def get_user_badges(user_id):
        user_badges_url = f"https://badges.roblox.com/v1/users/{user_id}/badges?limit=100&sortOrder=Asc"
        async with aiohttp.ClientSession() as session:
            async with session.get(user_badges_url) as response:
                if response.status == 200:
                    data = await response.json()
                    return data, None
                return None, f"Failed to fetch badges (Status: {response.status})"

    async def get_badge_info(badge_id):
        badge_info_url = f"https://badges.roblox.com/v1/badges/{badge_id}"
        async with aiohttp.ClientSession() as session:
            async with session.get(badge_info_url) as response:
                if response.status == 200:
                    data = await response.json()
                    return data, None
                return None, f"Failed to fetch badge info (Status: {response.status})"

    async def get_awarded_date(user_id, badge_id):
        awarded_date_url = f"https://badges.roblox.com/v1/users/{user_id}/badges/awarded-dates?badgeIds={badge_id}"
        async with aiohttp.ClientSession() as session:
            async with session.get(awarded_date_url) as response:
                if response.status == 200:
                    data = await response.json()
                    return data, None
                return None, f"Failed to fetch awarded date (Status: {response.status})"

    async def get_badge_thumbnail(badge_id):
        thumbnail_url = f"https://thumbnails.roblox.com/v1/badges/icons?badgeIds={badge_id}&size=150x150&format=Png"
        async with aiohttp.ClientSession() as session:
            async with session.get(thumbnail_url) as response:
                if response.status == 200:
                    data = await response.json()
                    if data.get('data') and len(data['data']) > 0:
                        return data['data'][0]['imageUrl']
        return None

    try:
        user_data, error = await get_user_id()
        
        if error or not user_data.get("data") or len(user_data["data"]) == 0:
            errorembed = discord.Embed(
                title=f":x: User Not Found :x:",
                description=f"Could not find a Roblox user with the username `{username}`",
                color=discord.Color.red()
            )
            errorembed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
            await interaction.edit_original_response(embed=errorembed)
            return
            
        user_info = user_data["data"][0]
        user_id = user_info["id"]
        roblox_username = user_info["name"]
        
        badges_data, error = await get_user_badges(user_id)
        
        if error:
            errorembed = discord.Embed(
                title=f":x: Error Fetching Badges :x:",
                description=f"Failed to fetch badges for user `{username}`",
                color=discord.Color.red()
            )
            errorembed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
            await interaction.edit_original_response(embed=errorembed)
            return
        
        user_badges = badges_data.get("data", [])
        
        if not user_badges:
            errorembed = discord.Embed(
                title=f":x: No Badges Found :x:",
                description=f"User `{username}` has no badges",
                color=discord.Color.red()
            )
            errorembed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
            await interaction.edit_original_response(embed=errorembed)
            return
        
        if badge_id:
            target_badge = None
            for badge in user_badges:
                if str(badge.get("id")) == badge_id:
                    target_badge = badge
                    break
            
            if not target_badge:
                errorembed = discord.Embed(
                    title=f":x: Badge Not Found :x:",
                    description=f"User `{username}` does not have badge ID `{badge_id}`",
                    color=discord.Color.red()
                )
                errorembed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
                await interaction.edit_original_response(embed=errorembed)
                return
            
            badge_info_task = asyncio.create_task(get_badge_info(badge_id))
            awarded_date_task = asyncio.create_task(get_awarded_date(user_id, badge_id))
            thumbnail_task = asyncio.create_task(get_badge_thumbnail(badge_id))
            
            badge_info, badge_error = await badge_info_task
            awarded_data, awarded_error = await awarded_date_task
            badge_thumbnail = await thumbnail_task
            
            if badge_error:
                errorembed = discord.Embed(
                    title=f":x: Error Fetching Badge Info :x:",
                    description=f"Failed to fetch detailed information for badge ID `{badge_id}`",
                    color=discord.Color.red()
                )
                errorembed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
                await interaction.edit_original_response(embed=errorembed)
                return
            
            awarded_date = "Unknown"
            if awarded_data and awarded_data.get("data") and len(awarded_data["data"]) > 0:
                awarded_date_str = awarded_data["data"][0].get("awardedDate")
                if awarded_date_str:
                    awarded_timestamp = isotodiscordtimestamp(awarded_date_str, "F")
                    awarded_date = awarded_timestamp if awarded_timestamp else awarded_date_str
            
            badge_name = badge_info.get("displayName") or badge_info.get("name", "Unknown Badge")
            badge_description = badge_info.get("displayDescription") or badge_info.get("description", "No description available")
            badge_enabled = badge_info.get("enabled", False)
            
            created_date = badge_info.get("created", "Unknown")
            created_timestamp = isotodiscordtimestamp(created_date, "F") if created_date != "Unknown" else "Unknown"
            
            embed_color = embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
            
            embed = discord.Embed(
                title=f"✅ {username} has this badge!",
                description=f"**{badge_name}**\n{badge_description}",
                color=embed_color
            )
            
            if badge_thumbnail:
                embed.set_thumbnail(url=badge_thumbnail)
            
            embed.add_field(name="Badge Information", value=f"**ID:** `{badge_id}`\n**Status:** {'Enabled' if badge_enabled else 'Disabled'}", inline=True)
            embed.add_field(name="Awarded", value=f"**Date:** {awarded_date}", inline=True)
            embed.add_field(name="Created", value=f"**Date:** {created_timestamp}", inline=True)
            
            view = discord.ui.View()
            view.add_item(discord.ui.Button(
                label="View Badge",
                style=discord.ButtonStyle.link,
                emoji="<:RobloxLogo:1416951004607418398>",
                url=f"https://www.roblox.com/badges/{badge_id}"
            ))
            
            embed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
            await interaction.edit_original_response(embed=embed, view=view)
            return

        first_badge = user_badges[0]
        first_badge_id = str(first_badge.get("id"))
        
        badge_info_task = asyncio.create_task(get_badge_info(first_badge_id))
        awarded_date_task = asyncio.create_task(get_awarded_date(user_id, first_badge_id))
        thumbnail_task = asyncio.create_task(get_badge_thumbnail(first_badge_id))
        
        badge_info, badge_error = await badge_info_task
        awarded_data, awarded_error = await awarded_date_task
        badge_thumbnail = await thumbnail_task
        
        if badge_error:
            errorembed = discord.Embed(
                title=f":x: Error Fetching Badge Info :x:",
                description=f"Failed to fetch detailed information for badge ID `{first_badge_id}`",
                color=discord.Color.red()
            )
            errorembed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
            await interaction.edit_original_response(embed=errorembed)
            return
        
        awarded_date = "Unknown"
        if awarded_data and awarded_data.get("data") and len(awarded_data["data"]) > 0:
            awarded_date_str = awarded_data["data"][0].get("awardedDate")
            if awarded_date_str:
                awarded_timestamp = isotodiscordtimestamp(awarded_date_str, "F")
                awarded_date = awarded_timestamp if awarded_timestamp else awarded_date_str
        
        badge_name = badge_info.get("displayName") or badge_info.get("name", "Unknown Badge")
        badge_description = badge_info.get("displayDescription") or badge_info.get("description", "No description available")
        badge_enabled = badge_info.get("enabled", False)
        
        created_date = badge_info.get("created", "Unknown")
        created_timestamp = isotodiscordtimestamp(created_date, "F") if created_date != "Unknown" else "Unknown"
        
        embed_color = embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
        
        class BadgePaginator(discord.ui.View):
            def __init__(self, badges, user_id, username, requester_id, current_index=0):
                super().__init__(timeout=60)
                self.badges = badges
                self.user_id = user_id
                self.username = username
                self.requester_id = requester_id
                self.current_index = current_index
            
            async def update_badge_display(self, interaction: discord.Interaction):
                badge = self.badges[self.current_index]
                badge_id = str(badge.get("id"))
                
                badge_info_task = asyncio.create_task(get_badge_info(badge_id))
                awarded_date_task = asyncio.create_task(get_awarded_date(self.user_id, badge_id))
                thumbnail_task = asyncio.create_task(get_badge_thumbnail(badge_id))
                
                badge_info, badge_error = await badge_info_task
                awarded_data, awarded_error = await awarded_date_task
                badge_thumbnail = await thumbnail_task
                
                if badge_error:
                    await interaction.response.send_message("Failed to fetch badge information.", ephemeral=True)
                    return
                
                awarded_date = "Unknown"
                if awarded_data and awarded_data.get("data") and len(awarded_data["data"]) > 0:
                    awarded_date_str = awarded_data["data"][0].get("awardedDate")
                    if awarded_date_str:
                        awarded_timestamp = isotodiscordtimestamp(awarded_date_str, "F")
                        awarded_date = awarded_timestamp if awarded_timestamp else awarded_date_str
                
                badge_name = badge_info.get("displayName") or badge_info.get("name", "Unknown Badge")
                badge_description = badge_info.get("displayDescription") or badge_info.get("description", "No description available")
                badge_enabled = badge_info.get("enabled", False)
                
                created_date = badge_info.get("created", "Unknown")
                created_timestamp = isotodiscordtimestamp(created_date, "F") if created_date != "Unknown" else "Unknown"
                
                embed_color = embedDB.get(f"{self.requester_id}") if embedDB.get(f"{self.requester_id}") else discord.Color.blue()
                
                embed = discord.Embed(
                    title=f"✅ {self.username}'s Badges",
                    description=f"**{badge_name}**\n{badge_description}",
                    color=embed_color
                )
                
                if badge_thumbnail:
                    embed.set_thumbnail(url=badge_thumbnail)
                
                embed.add_field(name="Badge Information", value=f"**ID:** `{badge_id}`\n**Status:** {'Enabled' if badge_enabled else 'Disabled'}", inline=True)
                embed.add_field(name="Awarded", value=f"**Date:** {awarded_date}", inline=True)
                embed.add_field(name="Created", value=f"**Date:** {created_timestamp}", inline=True)
                embed.set_footer(text=f"Badge {self.current_index + 1}/{len(self.badges)} | Requested By {interaction.user.name} | {MainURL}")
                
                view = BadgePaginator(self.badges, self.user_id, self.username, self.requester_id, self.current_index)
                await interaction.response.edit_message(embed=embed, view=view)
            
            @discord.ui.button(emoji="⬅️", style=discord.ButtonStyle.secondary)
            async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                if interaction.user.id != self.requester_id:
                    await interaction.response.send_message("This is not your command!", ephemeral=True)
                    return
                
                self.current_index = (self.current_index - 1) % len(self.badges)
                await self.update_badge_display(interaction)
            
            @discord.ui.button(emoji="➡️", style=discord.ButtonStyle.secondary)
            async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                if interaction.user.id != self.requester_id:
                    await interaction.response.send_message("This is not your command!", ephemeral=True)
                    return
                
                self.current_index = (self.current_index + 1) % len(self.badges)
                await self.update_badge_display(interaction)
            
        
        embed = discord.Embed(
            title=f"✅ {username}'s Badges",
            description=f"**{badge_name}**\n{badge_description}",
            color=embed_color
        )
        
        if badge_thumbnail:
            embed.set_thumbnail(url=badge_thumbnail)
        
        embed.add_field(name="Badge Information", value=f"**ID:** `{first_badge_id}`\n**Status:** {'Enabled' if badge_enabled else 'Disabled'}", inline=True)
        embed.add_field(name="Awarded", value=f"**Date:** {awarded_date}", inline=True)
        embed.add_field(name="Created", value=f"**Date:** {created_timestamp}", inline=True)
        embed.set_footer(text=f"Badge 1/{len(user_badges)} | Requested By {interaction.user.name} | {MainURL}")
        
        view = BadgePaginator(user_badges, user_id, roblox_username, interaction.user.id, 0)
        await interaction.edit_original_response(embed=embed, view=view)
        
    except Exception as e:
        errorembed = discord.Embed(
            title=f":x: Error :x:",
            description=f"An error occurred: {str(e)}",
            color=discord.Color.red()
        )
        errorembed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
        await interaction.edit_original_response(embed=errorembed)


# --- limiteds ---
@bot.tree.command(name="limiteds", description="Scan a roblox users inventory for limiteds")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def limiteds(interaction: discord.Interaction, username: str):
    await interaction.response.defer()
    
    thinkingembed = discord.Embed(
        title=f"{Emojis.get('loading')} {interaction.user.mention} Scanning for {username}'s limiteds!",
        color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
    )
    await interaction.followup.send(embed=thinkingembed)

    user_url = "https://users.roblox.com/v1/usernames/users"
    request_payload = {
        "usernames": [username],
        "excludeBannedUsers": False
    }

    try:
        response = requests.post(user_url, json=request_payload)
        response.raise_for_status()
        user_data = response.json()
        
        if not user_data.get("data") or len(user_data["data"]) == 0:
            errorembed = discord.Embed(
                title=":x: User Not Found :x:",
                description=f"Could not find a Roblox user with the username `{username}`",
                color=discord.Color.red()
            )
            errorembed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
            await interaction.edit_original_response(embed=errorembed)
            return
            
        user_info = user_data["data"][0]
        user_id = user_info["id"]
        display_name = user_info["displayName"]

        connector = aiohttp.TCPConnector(family=socket.AF_INET)
        async with aiohttp.ClientSession(connector=connector) as session:
            all_limiteds_items = []
            cursor = ""
            
            while True:
                limiteds_url = f"https://inventory.roblox.com/v1/users/{user_id}/assets/collectibles?sortOrder=Asc&limit=100"
                if cursor:
                    limiteds_url += f"&cursor={cursor}"
                
                async with session.get(limiteds_url) as response:
                    if response.status != 200:
                        errorembed = discord.Embed(
                            title=":x: Error Fetching Limiteds :x:",
                            description=f"Failed to fetch limiteds for user `{username}` (Status: {response.status})",
                            color=discord.Color.red()
                        )
                        errorembed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
                        await interaction.edit_original_response(embed=errorembed)
                        return
                    
                    limiteds_data = await response.json()
                    limiteds_items = limiteds_data.get("data", [])
                    all_limiteds_items.extend(limiteds_items)
                    
                    next_cursor = limiteds_data.get("nextPageCursor")
                    if not next_cursor:
                        break
                    
                    cursor = next_cursor
                    
                    if len(all_limiteds_items) >= 100:
                        await asyncio.sleep(2.5)
            
            rap = 0
            value = 0
            try:
                rolimons_url = f"https://api.rolimons.com/players/v1/playerinfo/{user_id}"
                async with session.get(rolimons_url, headers={'User-Agent': 'Shapes (Yapin bot)'}) as response:
                    if response.status == 200:
                        rolimons_data = await response.json()
                        if rolimons_data.get('success'):
                            rap = rolimons_data.get('rap', 0) or 0
                            value = rolimons_data.get('value', 0) or 0
            except Exception:
                pass
            
            inventory_url = f"https://inventory.roblox.com/v1/users/{user_id}/can-view-inventory"
            inventory_visibility = "Private"
            try:
                async with session.get(inventory_url) as response:
                    if response.status == 200:
                        inventory_data = await response.json()
                        inventory_visibility = "Public" if inventory_data.get('canView', False) else "Private"
            except Exception:
                pass

            avatar_url = f"https://thumbnails.roblox.com/v1/users/avatar?userIds={user_id}&size=420x420&format=Png&isCircular=false"
            avatar_thumbnail = None
            try:
                async with session.get(avatar_url) as response:
                    if response.status == 200:
                        avatar_data = await response.json()
                        if avatar_data.get("data") and len(avatar_data["data"]) > 0:
                            avatar_thumbnail = avatar_data["data"][0].get("imageUrl")
            except Exception:
                pass

            limited_count = len(all_limiteds_items)
            limiteds_by_name = {}
            
            for item in all_limiteds_items:
                name = item.get("name", "Unknown Item")
                user_asset_id = item.get("userAssetId")
                
                if name in limiteds_by_name:
                    limiteds_by_name[name]["count"] += 1
                else:
                    limiteds_by_name[name] = {
                        "count": 1,
                        "user_asset_id": user_asset_id
                    }
            
            limiteds_list = []
            for name, data in limiteds_by_name.items():
                count = data["count"]
                user_asset_id = data["user_asset_id"]
                
                if count > 1:
                    display_text = f"{count}x [{name}](https://www.rolimons.com/uaid/{user_asset_id})"
                else:
                    display_text = f"[{name}](https://www.rolimons.com/uaid/{user_asset_id})"
                
                limiteds_list.append(display_text)
            
            limiteds_chunks = [limiteds_list[i:i+10] for i in range(0, len(limiteds_list), 10)]
            
            class LimitedsPaginationView(discord.ui.View):
                def __init__(self, limiteds_chunks, display_name, user_id, rap, value, limited_count, inventory_visibility, requester_id, avatar_thumbnail):
                    super().__init__(timeout=120)
                    self.limiteds_chunks = limiteds_chunks
                    self.current_page = 0
                    self.display_name = display_name
                    self.user_id = user_id
                    self.rap = rap
                    self.value = value
                    self.limited_count = limited_count
                    self.inventory_visibility = inventory_visibility
                    self.requester_id = requester_id
                    self.avatar_thumbnail = avatar_thumbnail
                
                async def create_embed(self):
                    embed = discord.Embed(
                        title=f"{self.display_name}'s Limiteds",
                        url=f"https://www.roblox.com/users/{self.user_id}/profile",
                        color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
                    )
                    
                    if self.avatar_thumbnail:
                        embed.set_thumbnail(url=self.avatar_thumbnail)
                    
                    stats_info = f"""
                    > **RAP:** {self.rap:,}
                    > **Value:** {self.value:,}
                    > **Limited Count:** {self.limited_count}
                    > **Inventory:** {self.inventory_visibility}
                    """
                    
                    embed.add_field(
                        name="Statistics",
                        value=stats_info,
                        inline=False
                    )
                    
                    if self.limiteds_chunks:
                        current_chunk = self.limiteds_chunks[self.current_page]
                        limiteds_text = "\n".join(current_chunk)
                        field_name = f"Limiteds (Page {self.current_page + 1}/{len(self.limiteds_chunks)})"
                        embed.add_field(
                            name=field_name,
                            value=limiteds_text,
                            inline=False
                        )
                    else:
                        embed.add_field(
                            name="Limiteds",
                            value="No limiteds found",
                            inline=False
                        )
                    
                    embed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
                    return embed
                
                def update_buttons(self):
                    self.clear_items()
                    
                    if self.current_page > 0:
                        previous_btn = discord.ui.Button(style=discord.ButtonStyle.gray, emoji="⬅️", custom_id="previous")
                        previous_btn.callback = self.previous_callback
                        self.add_item(previous_btn)
                    
                    if self.current_page < len(self.limiteds_chunks) - 1:
                        next_btn = discord.ui.Button(style=discord.ButtonStyle.gray, emoji="➡️", custom_id="next")
                        next_btn.callback = self.next_callback
                        self.add_item(next_btn)
                    
                    self.add_item(discord.ui.Button(
                        label="View Rolimons Profile",
                        style=discord.ButtonStyle.link,
                        emoji="<:RolimonsLogo:1417258794974711901>",
                        url=f"https://rolimons.com/player/{self.user_id}"
                    ))
                    
                    self.add_item(discord.ui.Button(
                        label="View Roblox Profile",
                        style=discord.ButtonStyle.link,
                        emoji="<:RobloxLogo:1416951004607418398>",
                        url=f"https://www.roblox.com/users/{self.user_id}/profile"
                    ))
                
                async def previous_callback(self, interaction: discord.Interaction):
                    if interaction.user.id != self.requester_id:
                        await interaction.response.send_message("This is not your command!", ephemeral=True)
                        return
                    
                    self.current_page -= 1
                    embed = await self.create_embed()
                    self.update_buttons()
                    await interaction.response.edit_message(embed=embed, view=self)
                
                async def next_callback(self, interaction: discord.Interaction):
                    if interaction.user.id != self.requester_id:
                        await interaction.response.send_message("This is not your command!", ephemeral=True)
                        return
                    
                    self.current_page += 1
                    embed = await self.create_embed()
                    self.update_buttons()
                    await interaction.response.edit_message(embed=embed, view=self)
            
            view = LimitedsPaginationView(limiteds_chunks, display_name, user_id, rap, value, limited_count, inventory_visibility, interaction.user.id, avatar_thumbnail)
            embed = await view.create_embed()
            view.update_buttons()
            await interaction.edit_original_response(embed=embed, view=view)
            
    except requests.exceptions.RequestException as e:
        errorembed = discord.Embed(
            title=":x: API Error :x:",
            description=f"An error occurred while fetching data: {str(e)}",
            color=discord.Color.red()
        )
        errorembed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
        await interaction.edit_original_response(embed=errorembed)
    except Exception as e:
        errorembed = discord.Embed(
            title=":x: Unexpected Error :x:",
            description=f"An unexpected error occurred: {str(e)}",
            color=discord.Color.red()
        )
        errorembed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
        await interaction.edit_original_response(embed=errorembed)


# --- bundle ---
@bot.tree.command(name="bundle", description="Get information about a Roblox bundle")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(bundleid="The ID of the bundle")
async def bundle(interaction: discord.Interaction, bundleid: str):
    await interaction.response.defer(thinking=True)
    
    print(f"Searching for bundle {bundleid}")
    thinkingembed = discord.Embed(
        title=f"{Emojis.get('loading')} {interaction.user.mention} Searching for bundle {bundleid}!",
        color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
    )
    await interaction.followup.send(embed=thinkingembed)

    try:
        url = f"https://catalog.roblox.com/v1/catalog/items/{bundleid}/details?itemType=Bundle"
        
        connector = aiohttp.TCPConnector(family=socket.AF_INET)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(url, headers={
                'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36',
                'accept': 'application/json',
            }) as response:
                if response.status != 200:
                    failedembed = discord.Embed(
                        title=f"❌ Bundle not found",
                        description=f"Could not find a bundle with ID `{bundleid}`",
                        color=discord.Color.red()
                    )
                    failedembed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
                    await interaction.edit_original_response(embed=failedembed)
                    return
                
                data = await response.json()
                
                name = data.get("name", "Unknown")
                description = data.get("description", "No description available")
                item_type = data.get("itemType", "Unknown")
                creator_name = data.get("creatorName", "Unknown")
                creator_has_verified_badge = data.get("creatorHasVerifiedBadge", False)
                lowest_price = data.get("lowestPrice", 0)
                price_status = data.get("priceStatus", "Unknown")
                favorite_count = data.get("favoriteCount", 0)
                item_created_utc = data.get("itemCreatedUtc", "")
                
                created_timestamp = "Unknown"
                if item_created_utc:
                    created_timestamp = isotodiscordtimestamp(item_created_utc, "D")
                
                creator_display = creator_name
                if creator_has_verified_badge:
                    creator_display += " <:RobloxVerified:1416951927513677874>"
                
                price_display = f"{lowest_price} Robux" if price_status != "Off Sale" else "Off Sale"
                
                bundle_info = f"""
                > **Name:** {name}
                > **Type:** {item_type}
                > **Creator:** {creator_display}
                > **Price:** {price_display}
                > **Favorites:** {favorite_count:,}
                > **Created:** {created_timestamp}
                """
                
                embed = discord.Embed(
                    title=name,
                    color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue(),
                    timestamp=interaction.created_at,
                    url=f"https://www.roblox.com/bundles/{bundleid}"
                )
                
                embed.add_field(
                    name="Bundle Information",
                    value=bundle_info,
                    inline=False
                )
                
                if description and description != "No description available":
                    embed.add_field(
                        name="Description",
                        value=f"> {description}",
                        inline=False
                    )
                
                thumbnail_url = f"https://thumbnails.roblox.com/v1/bundles/thumbnails?bundleIds={bundleid}&size=420x420&format=Png"
                async with session.get(thumbnail_url) as thumb_response:
                    if thumb_response.status == 200:
                        thumb_data = await thumb_response.json()
                        if thumb_data.get('data') and len(thumb_data['data']) > 0:
                            image_url = thumb_data['data'][0].get('imageUrl')
                            if image_url:
                                embed.set_thumbnail(url=image_url)
                
                embed.set_footer(text=f"Requested by {interaction.user.display_name} | {MainURL}")
                
                class BundleView(discord.ui.View):
                    def __init__(self, bundle_id: str):
                        super().__init__(timeout=120)
                        self.add_item(discord.ui.Button(
                            label="View Bundle on Roblox",
                            url=f"https://www.roblox.com/bundles/{bundle_id}",
                            style=discord.ButtonStyle.link,
                            emoji="<:RobloxLogo:1416951004607418398>"
                        ))
                
                view = BundleView(bundleid)
                await interaction.edit_original_response(embed=embed, view=view)
                
    except Exception as e:
        failedembed = discord.Embed(
            title=f"❌ Error fetching bundle",
            description=f"An error occurred: {str(e)}",
            color=discord.Color.red()
        )
        failedembed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
        await interaction.edit_original_response(embed=failedembed)


# --- asset ---
@bot.tree.command(name="asset", description="Get information about a Roblox asset")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def asset(interaction: discord.Interaction, id: str):
    await interaction.response.defer()
    
    print(f"Searching For Asset {id} Information!")
    thinkingembed = discord.Embed(
        title=f"{Emojis.get('loading')} {interaction.user.mention} Searching for Asset {id}",
        color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
    )
    await interaction.followup.send(embed=thinkingembed)

    url = f"https://catalog.roblox.com/v1/catalog/items/{id}/details?itemType=Asset"
    
    try:
        response = requests.get(url)
        response.raise_for_status()
        asset_data = response.json()
        
        name = asset_data.get("name", "Unknown Asset")
        description = asset_data.get("description", "No description available")
        creator_name = asset_data.get("creatorName", "Unknown Creator")
        creator_verified = asset_data.get("creatorHasVerifiedBadge", False)
        lowest_price = asset_data.get("lowestPrice", 0)
        favorite_count = asset_data.get("favoriteCount", 0)
        created_date = asset_data.get("itemCreatedUtc")
        item_type = asset_data.get("itemType", "Asset")
        
        if created_date:
            created_timestamp = isotodiscordtimestamp(created_date, "D")
        else:
            created_timestamp = "Unknown"
        
        creator_display = creator_name
        if creator_verified:
            creator_display += " <:RobloxVerified:1416951927513677874>"
        
        price_display = "Free" if lowest_price == 0 else f"{lowest_price:,} Robux"
        
        thumbnail_url = f"https://thumbnails.roblox.com/v1/assets?assetIds={id}&size=420x420&format=Png"
        thumbnail_response = requests.get(thumbnail_url)
        thumbnail_data = thumbnail_response.json()
        
        image_url = None
        if thumbnail_data and thumbnail_data.get("data") and len(thumbnail_data["data"]) > 0:
            image_url = thumbnail_data["data"][0].get("imageUrl")
        
        embed = discord.Embed(
            title=name,
            url=f"https://www.roblox.com/catalog/{id}/",
            color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue(),
            timestamp=interaction.created_at
        )
        
        if image_url:
            embed.set_thumbnail(url=image_url)
        
        asset_info = f"""
        > **Name:** {name}
        > **Asset ID:** `{id}`
        > **Type:** {item_type}
        > **Creator:** {creator_display}
        > **Price:** {price_display}
        > **Favorites:** {favorite_count:,}
        > **Created:** {created_timestamp}
        """
        
        embed.add_field(
            name="Asset Information",
            value=asset_info,
            inline=False
        )
        
        if description and description != "No description available":
            if len(description) > 1024:
                description = description[:1021] + "..."
            embed.add_field(
                name="Description",
                value=f"> {description}",
                inline=False
            )
        
        view = discord.ui.View(timeout=120)
        view.add_item(discord.ui.Button(
            label="View Asset",
            style=discord.ButtonStyle.link,
            emoji="<:RobloxLogo:1416951004607418398>",
            url=f"https://www.roblox.com/catalog/{id}/"
        ))
        
        embed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
        await interaction.edit_original_response(embed=embed, view=view)
        
    except requests.exceptions.RequestException as e:
        errorembed = discord.Embed(
            title=":x: Error Fetching Asset :x:",
            description=f"Failed to fetch asset information for ID `{id}`. Please check if the asset ID is valid.",
            color=discord.Color.red()
        )
        errorembed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
        await interaction.edit_original_response(embed=errorembed)
    except Exception as e:
        errorembed = discord.Embed(
            title=":x: Unexpected Error :x:",
            description=f"An unexpected error occurred while fetching asset information: {str(e)}",
            color=discord.Color.red()
        )
        errorembed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
        await interaction.edit_original_response(embed=errorembed)


async def get_csrf_token(session):
    try:
        with open('roblosecuritytoken.txt', 'r') as f:
            roblosecurity_token = f.read().strip()
            
        async with session.post("https://auth.roblox.com/v2/logout", 
                              headers={'Cookie': f'.ROBLOSECURITY={roblosecurity_token}'}) as response:
            token = response.headers.get("x-csrf-token")
            return token
    except Exception:
        return None


async def render_custom_avatar(session, user_id):
    try:
        with open('roblosecuritytoken.txt', 'r') as f:
            roblosecurity_token = f.read().strip()
        
        headers = {'Cookie': f'.ROBLOSECURITY={roblosecurity_token}'}
        
        async with session.get(f'https://avatar.roblox.com/v1/users/{user_id}/avatar', 
                             headers=headers) as response:
            if response.status == 200:
                v1_avatar_details = await response.json()
                
                if v1_avatar_details and 'bodyColors' in v1_avatar_details:
                    avatar_definition = {
                        "assets": [{"id": asset["id"]} for asset in v1_avatar_details["assets"]],
                        "bodyColors": {
                            "headColor": roblox_colors.get(v1_avatar_details["bodyColors"]["headColorId"], "FFFFFF"),
                            "torsoColor": roblox_colors.get(v1_avatar_details["bodyColors"]["torsoColorId"], "FFFFFF"),
                            "leftArmColor": roblox_colors.get(v1_avatar_details["bodyColors"]["leftArmColorId"], "FFFFFF"),
                            "rightArmColor": roblox_colors.get(v1_avatar_details["bodyColors"]["rightArmColorId"], "FFFFFF"),
                            "leftLegColor": roblox_colors.get(v1_avatar_details["bodyColors"]["leftLegColorId"], "FFFFFF"),
                            "rightLegColor": roblox_colors.get(v1_avatar_details["bodyColors"]["rightLegColorId"], "FFFFFF"),
                        },
                        "scales": v1_avatar_details["scales"],
                        "playerAvatarType": {"playerAvatarType": v1_avatar_details["playerAvatarType"]}
                    }
                else:
                    return None
            else:
                return None

        render_payload = {
            "thumbnailConfig": {"thumbnailId": 1, "thumbnailType": "2dWebp", "size": "420x420"},
            "avatarDefinition": avatar_definition
        }

        csrf_token = await get_csrf_token(session)
        if not csrf_token:
            return None

        request_headers = {
            'Cookie': f'.ROBLOSECURITY={roblosecurity_token}',
            'x-csrf-token': csrf_token,
            'Content-Type': 'application/json'
        }
        
        for i in range(5):
            async with session.post("https://avatar.roblox.com/v1/avatar/render", 
                                  json=render_payload, headers=request_headers) as render_response:
                if render_response.status == 200:
                    render_data = await render_response.json()
                    if render_data.get('state') == 'Completed' and render_data.get('imageUrl'):
                        return render_data['imageUrl']
            
            if i < 4:
                await asyncio.sleep(2)
        
        return None

    except Exception:
        return None


@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@bot.tree.command(name="avatar", description="Get a Roblox user's avatar")
@app_commands.describe(user="Roblox username or user ID")
async def avatar_command(interaction: discord.Interaction, user: str):
    await interaction.response.defer()
    
    async def get_user_id(session, user_input):
        if user_input.isdigit():
            return user_input
        url = "https://users.roblox.com/v1/usernames/users"
        try:
            async with session.post(url, json={"usernames": [user_input]}) as response:
                if response.status == 200:
                    data = await response.json()
                    if data.get("data") and len(data["data"]) > 0:
                        return str(data["data"][0]["id"])
        except Exception:
            pass
        return None

    async def get_username_from_id(session, user_id):
        url = f"https://users.roblox.com/v1/users/{user_id}"
        try:
            async with session.get(url) as response:
                if response.status == 200:
                    data = await response.json()
                    return data.get("name")
        except Exception:
            pass
        return None

    connector = aiohttp.TCPConnector(family=socket.AF_INET)
    async with aiohttp.ClientSession(connector=connector) as session:
        user_id = await get_user_id(session, user)
        
        if not user_id:
            await interaction.followup.send("❌ Cannot find user")
            return

        username = await get_username_from_id(session, user_id)
        if not username:
            await interaction.followup.send("❌ Cannot find user")
            return

        try:
            user_url = f"https://users.roblox.com/v1/users/{user_id}"
            async with session.get(user_url) as response:
                if response.status == 200:
                    user_data = await response.json()
                    is_banned = user_data.get("isBanned", False)
                    
                    if is_banned:
                        image_url = await render_custom_avatar(session, user_id)
                        if image_url:
                            embed = discord.Embed(
                                title=f"{username}'s Avatar",
                                color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
                            )
                            embed.set_image(url=image_url)
                            embed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
                            await interaction.followup.send(embed=embed)
                        else:
                            await interaction.followup.send("❌ User is terminated")
                        return
                    else:
                        avatar_url = f"https://thumbnails.roblox.com/v1/users/avatar?userIds={user_id}&size=420x420&format=Png&isCircular=false"
                        async with session.get(avatar_url) as avatar_response:
                            if avatar_response.status == 200:
                                avatar_data = await avatar_response.json()
                                if avatar_data.get('data') and len(avatar_data['data']) > 0:
                                    image_url = avatar_data['data'][0]['imageUrl']
                                    embed = discord.Embed(
                                        title=f"{username}'s Avatar",
                                        color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
                                    )
                                    embed.set_image(url=image_url)
                                    embed.set_footer(text=f"Requested by {interaction.user.name} | {MainURL}")
                                    await interaction.followup.send(embed=embed)
                                    return
                        await interaction.followup.send("❌ Failed to fetch avatar")
                        return
                else:
                    await interaction.followup.send("❌ Cannot find user")
                    return
                    
        except Exception:
            await interaction.followup.send("Failed to fetch user data")
            return


class BadgeService:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
    
    async def get_user_id(self, user: str):
        if user.isdigit():
            return user
        return await self._get_id_from_username(user)
    
    async def _get_id_from_username(self, username: str):
        url = "https://users.roblox.com/v1/usernames/users"
        try:
            async with self.session.post(url, json={"usernames": [username]}) as response:
                if response.status == 200:
                    data = await response.json()
                    if data.get("data") and len(data["data"]) > 0:
                        return str(data["data"][0]["id"])
        except Exception:
            pass
        return None
    
    async def get_username(self, user_id: str):
        url = f"https://users.roblox.com/v1/users/{user_id}"
        try:
            async with self.session.get(url) as response:
                if response.status == 200:
                    data = await response.json()
                    return data.get("name")
        except Exception:
            pass
        return None
    
    async def get_user_badges(self, user_id: str, limit: int = 10):
        badges_url = f"https://badges.roblox.com/v1/users/{user_id}/badges?sortOrder=Desc&limit={limit}"
        
        async with self.session.get(badges_url) as response:
            if response.status != 200:
                return [], response.status
            
            data = await response.json()
            return data.get('data', []), response.status
    
    async def get_awarded_dates(self, user_id: str, badge_ids: list):
        if not badge_ids:
            return {}
            
        awarded_dates_url = f"https://badges.roblox.com/v1/users/{user_id}/badges/awarded-dates?badgeIds={','.join(map(str, badge_ids))}"
        
        try:
            async with self.session.get(awarded_dates_url) as response:
                if response.status == 200:
                    data = await response.json()
                    return {item['badgeId']: item.get('awardedDate') for item in data.get('data', [])}
        except Exception:
            pass
        return {}
    
    async def get_badge_thumbnail(self, badge):
        icon_image_id = badge.get('iconImageId') or badge.get('displayIconImageId')
        if not icon_image_id:
            return None
            
        icon_url = f"https://thumbnails.roblox.com/v1/assets?assetIds={icon_image_id}&size=150x150&format=Png&isCircular=false"
        
        try:
            async with self.session.get(icon_url) as response:
                if response.status == 200:
                    data = await response.json()
                    if data.get('data') and len(data['data']) > 0:
                        return data['data'][0]['imageUrl']
        except Exception:
            pass
        return None


class BadgeFormatter:
    @staticmethod
    def parse_iso_timestamp(timestamp_str):
        if not timestamp_str:
            return None
            
        try:
            timestamp_str = timestamp_str.split('.')[0]
            
            if timestamp_str.endswith('Z'):
                dt = datetime.fromisoformat(timestamp_str[:-1]).replace(
                    tzinfo=datetime.timezone.utc
                )
            else:
                if '+' not in timestamp_str and 'Z' not in timestamp_str:
                    timestamp_str += '+00:00'
                dt = datetime.fromisoformat(timestamp_str)
            
            return int(dt.timestamp())
        except (ValueError, AttributeError, TypeError):
            return None
    
    @staticmethod
    def format_creator_info(creator):
        creator_name = creator.get('name', 'Unknown')
        creator_type = creator.get('type', 'User')
        creator_id = creator.get('id')
        
        if not creator_id:
            return creator_name
            
        if creator_type.lower() == 'user':
            return f"[{creator_name}](https://www.roblox.com/users/{creator_id}/profile)"
        else:
            return f"[{creator_name}](https://www.roblox.com/groups/{creator_id}/)"
    
    @staticmethod
    def format_awarder_info(awarder):
        awarder_type = awarder.get('type', 'Unknown')
        awarder_id = awarder.get('id')
        
        if not awarder_id:
            return "Unknown"
            
        if awarder_type.lower() == 'place':
            return f"[Place #{awarder_id}](https://www.roblox.com/games/{awarder_id}/)"
        else:
            return f"{awarder_type} #{awarder_id}"


class BadgesView(discord.ui.View):
    def __init__(self, badges, username, user_id, requester, start_time, badge_service):
        super().__init__(timeout=120)
        self.badges = badges
        self.current_page = 0
        self.username = username
        self.user_id = user_id
        self.requester = requester
        self.start_time = start_time
        self.badge_service = badge_service
        self.message = None
        self.thumbnail_cache = {}
        self.update_buttons()
    
    async def preload_thumbnails(self):
        for i, badge in enumerate(self.badges):
            thumbnail_url = await self.badge_service.get_badge_thumbnail(badge)
            if thumbnail_url:
                self.thumbnail_cache[i] = thumbnail_url
    
    async def create_embed(self):
        badge = self.badges[self.current_page]
        
        embed = discord.Embed(
            title=f"{self.username}'s Recent Badges",
            color=embedDB.get(f"{self.requester.id}") if embedDB.get(f"{self.requester.id}") else discord.Color.blue(),
            timestamp=datetime.now(),
            url=f"https://www.roblox.com/users/{self.user_id}/badges"
        )
        
        thumbnail_url = self.thumbnail_cache.get(self.current_page)
        if thumbnail_url:
            embed.set_thumbnail(url=thumbnail_url)
        
        badge_name = badge.get('displayName') or badge.get('name', 'Unknown Badge')
        badge_description = badge.get('displayDescription') or badge.get('description', 'No description')
        badge_id = badge.get('id', 'N/A')
        
        embed.add_field(name="Badge Name", value=badge_name, inline=False)
        embed.add_field(name="Description", value=badge_description or "No description", inline=False)
        embed.add_field(name="Badge ID", value=f"`{badge_id}`", inline=True)
        
        awarded_date = badge.get('awardedDate')
        if awarded_date:
            unix_timestamp = BadgeFormatter.parse_iso_timestamp(awarded_date)
            if unix_timestamp:
                embed.add_field(
                    name="Awarded Date", 
                    value=f"<t:{unix_timestamp}:D> (<t:{unix_timestamp}:R>)",  
                    inline=False
                )
            else:
                embed.add_field(name="Awarded Date", value=awarded_date, inline=False)
        
        creator = badge.get('creator', {})
        creator_text = BadgeFormatter.format_creator_info(creator)
        embed.add_field(name="Creator", value=creator_text, inline=True)
        
        awarder = badge.get('awarder', {})
        awarder_text = BadgeFormatter.format_awarder_info(awarder)
        embed.add_field(name="Awarded By", value=awarder_text, inline=True)
        
        stats = badge.get('statistics', {})
        awarded_count = stats.get('awardedCount', 0)
        win_rate = stats.get('winRatePercentage', 0)
        
        embed.add_field(name="Times Awarded", value=f"{awarded_count:,}", inline=True)
        embed.add_field(name="Win Rate", value=f"{win_rate}%", inline=True)
        
        elapsed_time = asyncio.get_event_loop().time() - self.start_time
        embed.set_footer(
            text=f"Badge {self.current_page + 1}/{len(self.badges)} • Load time: {elapsed_time:.2f}s • Requested by {self.requester.display_name}"
        )
        
        return embed
        
    def update_buttons(self):
        self.clear_items()
        
        if self.current_page > 0:
            previous_btn = discord.ui.Button(style=discord.ButtonStyle.primary, label="<<", custom_id="previous")
            previous_btn.callback = self.previous_callback
            self.add_item(previous_btn)
            
        if self.current_page < len(self.badges) - 1:
            next_btn = discord.ui.Button(style=discord.ButtonStyle.primary, label=">>", custom_id="next")
            next_btn.callback = self.next_callback
            self.add_item(next_btn)
        
        current_badge = self.badges[self.current_page]
        badge_id = current_badge.get('id')
        if badge_id:
            link_btn = discord.ui.Button(
                style=discord.ButtonStyle.link,
                label="View Badge",
                url=f"https://www.roblox.com/badges/{badge_id}"
            )
            self.add_item(link_btn)
    
    async def _handle_navigation(self, interaction: discord.Interaction, direction: int):
        if interaction.user != self.requester:
            await interaction.response.send_message("You can't interact with this command!", ephemeral=True)
            return
        
        self.current_page += direction
        embed = await self.create_embed()
        self.update_buttons()
        await interaction.response.edit_message(embed=embed, view=self)
    
    async def previous_callback(self, interaction: discord.Interaction):
        await self._handle_navigation(interaction, -1)
    
    async def next_callback(self, interaction: discord.Interaction):
        await self._handle_navigation(interaction, 1)
    
    async def on_timeout(self):
        for item in self.children:
            if isinstance(item, discord.ui.Button) and item.style != discord.ButtonStyle.link:
                item.disabled = True
        try:
            await self.message.edit(view=self)
        except Exception:
            pass


@bot.tree.command(name="recentbadges", description="Get a user's most recently earned Roblox badges")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def recent_badges(interaction: discord.Interaction, user: str):
    await interaction.response.defer()
    
    thinkingembed = discord.Embed(
        title=f"{Emojis.get('loading')} {interaction.user.mention} Searching For {user}'s recent badges",
        color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
    )
    await interaction.followup.send(embed=thinkingembed)
    
    start_time = asyncio.get_event_loop().time()
    
    connector = aiohttp.TCPConnector(family=socket.AF_INET)
    async with aiohttp.ClientSession(connector=connector) as session:
        badge_service = BadgeService(session)
        
        try:
            user_id = await badge_service.get_user_id(user)
            if not user_id:
                embed = discord.Embed(
                    description=f"Cannot find user",
                    color=discord.Color.red()
                )
                await interaction.edit_original_response(embed=embed)
                return
            
            username = await badge_service.get_username(user_id)
            if not username:
                embed = discord.Embed(
                    description=f"Cannot find user",
                    color=discord.Color.red()
                )
                await interaction.edit_original_response(embed=embed)
                return
            
            badges, status_code = await badge_service.get_user_badges(user_id)
            
            if status_code == 403:
                embed = discord.Embed(
                    description="This user's inventory is private. Badges cannot be viewed.",
                    color=discord.Color.red()
                )
                await interaction.edit_original_response(embed=embed)
                return
            elif status_code != 200:
                embed = discord.Embed(
                    description=f"Failed to fetch badges",
                    color=discord.Color.red()
                )
                await interaction.edit_original_response(embed=embed)
                return
            
            if not badges:
                embed = discord.Embed(
                    description="This user has no badges.",
                    color=discord.Color.red()
                )
                await interaction.edit_original_response(embed=embed)
                return
            
            badge_ids = [badge['id'] for badge in badges]
            awarded_dates = await badge_service.get_awarded_dates(user_id, badge_ids)
            
            for badge in badges:
                badge_id = badge['id']
                badge['awardedDate'] = awarded_dates.get(badge_id)
            
            view = BadgesView(badges, username, user_id, interaction.user, start_time, badge_service)
            await view.preload_thumbnails()
            embed = await view.create_embed()
            view.update_buttons()
            message = await interaction.edit_original_response(embed=embed, view=view)
            view.message = message
            
        except asyncio.TimeoutError:
            embed = discord.Embed(
                description=f"The request timed out while fetching badge data.",
                color=discord.Color.red()
            )
            await interaction.edit_original_response(embed=embed)
        except aiohttp.ClientError as e:
            embed = discord.Embed(
                description=f"Failed to connect",
                color=discord.Color.red()
            )
            await interaction.edit_original_response(embed=embed)
        except Exception as e:
            print(f"Unexpected error details: {type(e).__name__}: {str(e)}")
            embed = discord.Embed(
                description=f"An unexpected error occurred",
                color=discord.Color.red()
            )
            await interaction.edit_original_response(embed=embed)


class RobloxLookup(yapper.Cog):
    name = "Roblox lookup"


def setup(bot):
    bot.add_cog(RobloxLookup(bot))

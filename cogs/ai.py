"""Cog: /ai and /google."""
import sys
from discord import app_commands
from discord.ext import commands
import discord
import requests

m = sys.modules.get("main") or sys.modules["__main__"]   # this bot's main.py: shared helpers, data and the bot itself
AIModel = m.AIModel
AI_DB = m.AI_DB
Emojis = m.Emojis
GoogleAPIKey = m.GoogleAPIKey
MainURL = m.MainURL
bot = m.bot
chatgpt = m.chatgpt
embedDB = m.embedDB
searchengine = m.searchengine


@bot.tree.command(name="ai", description="Chat with an AI assistant.")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def ai(interaction: discord.Interaction, *, prompt: str):
    await interaction.response.defer(thinking=True)

    loading = discord.Embed(
    title=f"{Emojis.get('loading')} {interaction.user.mention} Getting AI Response For: {prompt}",
    color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
)

    await interaction.followup.send(embed=loading)
    
    user_id = str(interaction.user.id)
    username = interaction.user.name

    # Load or initialize user data
    user_data = AI_DB.get(user_id) or {"username": username, "user_messages": [], "ai_responses": []}
    user_data["username"] = username
    user_data["user_messages"].append(prompt)
    
    # Keep last 50 messages
    user_data["user_messages"] = user_data["user_messages"][-50:]
    user_data["ai_responses"] = user_data["ai_responses"][-50:]

    # System instructions for the AI
    messages_for_ai = [
        {
            "role": "system",
            "content": (
                f"You are a helpful Discord assistant chatting with {username}. "
                "Always respond in a single concise paragraph. Otherwise your response will not be recieved due to the embed text limit"
                "Follow Discord TOS. Do not provide instructions for illegal activity. "
                "Stay safe, respectful, and friendly."
            )
        }
    ]

    # Include previous conversation
    for u_msg, a_msg in zip(user_data["user_messages"], user_data["ai_responses"]):
        messages_for_ai.append({"role": "user", "content": u_msg[:1024]})
        messages_for_ai.append({"role": "assistant", "content": a_msg[:1024]})

    # Add latest message
    messages_for_ai.append({"role": "user", "content": prompt})

    try:
        response = chatgpt.chat.completions.create(
            model=AIModel,
            messages=messages_for_ai
        )
        ai_reply = response.choices[0].message.content.strip()
    except Exception as e:
        ai_reply = f"⚠️ API error: {e}"

    # Save AI response
    user_data["ai_responses"].append(ai_reply)
    AI_DB.set(user_id, user_data)
    AI_DB.save()

    # Create embed
    embed = discord.Embed(
        title=f"💬 Chat with {username}",
        color=embedDB.get(user_id) or discord.Color.blue()
    )
    embed.add_field(name="🧍 You said:", value=prompt, inline=False)
    embed.add_field(name="🤖 AI replied:", value=ai_reply, inline=False)
    embed.set_footer(text=f"Requested by {username} | {MainURL}")

    await interaction.edit_original_response(embed=embed)


@bot.tree.command(name="google", description="Search Something On Google.")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def google(interaction: discord.Interaction, query: str = "Shapes"):
    await interaction.response.defer()
    
    thinkingembed = discord.Embed(
        title=f"{Emojis.get('loading')} {interaction.user.mention} Searching Google For {query}",
        color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
    )
    await interaction.followup.send(embed=thinkingembed)
    
    # replace spaces with + in query for google search link
    properquery = query.replace(" ", "+")
    
    url = f"https://www.googleapis.com/customsearch/v1?key={GoogleAPIKey}&cx={searchengine}&q={properquery}"
    try:
        response = requests.get(url)
        response.raise_for_status()
        data = response.json()

        # Get First 5 results
        if "items" in data and len(data["items"]) >= 5:
            first_result = data["items"][0]
            title = first_result.get("title", "No Title")
            snippet = first_result.get("snippet", "No Description")
            link = first_result.get("link", "No Link")
            print(f"First Result: {title} - {link}")
            second_result = data["items"][1]
            second_result_title = second_result.get("title", "No Title")
            second_result_snippet = second_result.get("snippet", "No Description")
            second_result_link = second_result.get("link", "No Link")
            print(f"Second Result: {second_result_title} - {second_result_link}")
            third_result = data["items"][2]
            third_result_title = third_result.get("title", "No Title")
            third_result_snippet = third_result.get("snippet", "No Description")
            third_result_link = third_result.get("link", "No Link")
            print(f"Third Result: {third_result_title} - {third_result_link}")
            fourth_result = data["items"][3]
            fourth_result_title = fourth_result.get("title", "No Title")
            fourth_result_snippet = fourth_result.get("snippet", "No Description")
            fourth_result_link = fourth_result.get("link", "No Link")
            print(f"Fourth Result: {fourth_result_title} - {fourth_result_link}")
            fifth_result = data["items"][4]
            fifth_result_title = fifth_result.get("title", "No Title")
            fifth_result_snippet = fifth_result.get("snippet", "No Description")
            fifth_result_link = fifth_result.get("link", "No Link")
            print(f"Fifth Result: {fifth_result_title} - {fifth_result_link}")
            
            embed = discord.Embed(
                title=f"Google Results For {query}",
                description=f"**1. [{title}]({link})**\n{snippet}\n\n**2. [{second_result_title}]({second_result_link})**\n{second_result_snippet}\n\n**3. [{third_result_title}]({third_result_link})**\n{third_result_snippet}\n\n**4. [{fourth_result_title}]({fourth_result_link})**\n{fourth_result_snippet}\n\n**5. [{fifth_result_title}]({fifth_result_link})**\n{fifth_result_snippet}\n\n[Search For More Results](https://google.com/search?q={properquery})",
                color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
            )
            embed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
            await interaction.edit_original_response(embed=embed)
        else:
            noresultembed = discord.Embed(
                title=":x: No results found! :x:",
                description=f"No Results for {query} | [Search on Google Yourself For More Results](https://google.com/search?q={properquery})",
                color=discord.Color.red()
            )
            noresultembed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
            await interaction.edit_original_response(embed=noresultembed)
    except requests.exceptions.RequestException as e:
        print(f"An error occurred during the API request: {e}")
        errorembed = discord.Embed(
            title=":x: An error occurred while searching Google. Please try again later. :x:",
            color=discord.Color.red()
        )
        errorembed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
        await interaction.edit_original_response(embed=errorembed)


@bot.tree.context_menu(name="ai")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def ai(interaction: discord.Interaction, prompt: discord.Message):
    await interaction.response.defer(thinking=True)

    loading = discord.Embed(
    title=f"{Emojis.get('loading')} {interaction.user.mention} Getting AI Response For: {prompt}",
    color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
)

    await interaction.followup.send(embed=loading)
    
    user_id = str(interaction.user.id)
    username = interaction.user.name

    # Load or initialize user data
    user_data = AI_DB.get(user_id) or {"username": username, "user_messages": [], "ai_responses": []}
    user_data["username"] = username
    user_data["user_messages"].append(prompt)
    
    # Keep last 50 messages
    user_data["user_messages"] = user_data["user_messages"][-50:]
    user_data["ai_responses"] = user_data["ai_responses"][-50:]

    # System instructions for the AI
    messages_for_ai = [
        {
            "role": "system",
            "content": (
                f"You are a helpful Discord assistant chatting with {username}. "
                "Always respond in a single concise paragraph. Otherwise your response will not be recieved due to the embed text limit"
                "Follow Discord TOS. Do not provide instructions for illegal activity. "
                "Stay safe, respectful, and friendly."
            )
        }
    ]

    # Include previous conversation
    for u_msg, a_msg in zip(user_data["user_messages"], user_data["ai_responses"]):
        messages_for_ai.append({"role": "user", "content": u_msg})
        messages_for_ai.append({"role": "assistant", "content": a_msg})

    # Add latest message
    messages_for_ai.append({"role": "user", "content": prompt})

    try:
        response = chatgpt.chat.completions.create(
            model=AIModel,
            messages=messages_for_ai
        )
        ai_reply = response.choices[0].message.content.strip()
    except Exception as e:
        ai_reply = f"⚠️ API error: {e}"

    # Save AI response
    user_data["ai_responses"].append(ai_reply)
    AI_DB.set(user_id, user_data)
    AI_DB.save()

    # Create embed
    embed = discord.Embed(
        title=f"💬 Chat with {username}",
        color=embedDB.get(user_id) or discord.Color.blue()
    )
    embed.add_field(name="🧍 You said:", value=prompt, inline=False)
    embed.add_field(name="🤖 AI replied:", value=ai_reply, inline=False)
    embed.set_footer(text=f"Requested by {username} | {MainURL}")

    await interaction.edit_original_response(embed=embed)


@bot.tree.context_menu(name="google")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def google(interaction: discord.Interaction, message: discord.Message):
    await interaction.response.defer(thinking=True)
    query = message.content
    thinkingembed = discord.Embed(
        title=f"{Emojis.get('loading')} {interaction.user.mention} Searching Google For {query}!",
        color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
    )
    await interaction.followup.send(embed=thinkingembed)
    # replace spaces with + in query for google search link
    properquery = query.replace(" ", "+")
    
    url = f"https://www.googleapis.com/customsearch/v1?key={GoogleAPIKey}&cx={searchengine}&q={properquery}"
    try:
        response = requests.get(url)
        response.raise_for_status()
        data = response.json()

        # Get First 5 results
        if "items" in data and len(data["items"]) >= 5:
            first_result = data["items"][0]
            title = first_result.get("title", "No Title")
            snippet = first_result.get("snippet", "No Description")
            link = first_result.get("link", "No Link")
            print(f"First Result: {title} - {link}")
            second_result = data["items"][1]
            second_result_title = second_result.get("title", "No Title")
            second_result_snippet = second_result.get("snippet", "No Description")
            second_result_link = second_result.get("link", "No Link")
            print(f"Second Result: {second_result_title} - {second_result_link}")
            third_result = data["items"][2]
            third_result_title = third_result.get("title", "No Title")
            third_result_snippet = third_result.get("snippet", "No Description")
            third_result_link = third_result.get("link", "No Link")
            print(f"Third Result: {third_result_title} - {third_result_link}")
            fourth_result = data["items"][3]
            fourth_result_title = fourth_result.get("title", "No Title")
            fourth_result_snippet = fourth_result.get("snippet", "No Description")
            fourth_result_link = fourth_result.get("link", "No Link")
            print(f"Fourth Result: {fourth_result_title} - {fourth_result_link}")
            fifth_result = data["items"][4]
            fifth_result_title = fifth_result.get("title", "No Title")
            fifth_result_snippet = fifth_result.get("snippet", "No Description")
            fifth_result_link = fifth_result.get("link", "No Link")
            print(f"Fifth Result: {fifth_result_title} - {fifth_result_link}")
            
            embed = discord.Embed(
                title=f"Google Results For {query}",
                description=f"**1. [{title}]({link})**\n{snippet}\n\n**2. [{second_result_title}]({second_result_link})**\n{second_result_snippet}\n\n**3. [{third_result_title}]({third_result_link})**\n{third_result_snippet}\n\n**4. [{fourth_result_title}]({fourth_result_link})**\n{fourth_result_snippet}\n\n**5. [{fifth_result_title}]({fifth_result_link})**\n{fifth_result_snippet}\n\n[Search For More Results](https://google.com/search?q={properquery})",
                color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
            )
            embed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
            await interaction.edit_original_response(embed=embed)
        else:
            if "items" in data and len(data["items"]) > 0:
                notenoughresultsembed = discord.Embed(
                title=":x: Not enough results found! :x:",
                description=f"Please search on google yourself as there wasn't enough results to generate an embed | [Search on Google Yourself For More Results](https://google.com/search?q={properquery})",
                color=embedDB.get(f"{interaction.user.id}") if embedDB.get(f"{interaction.user.id}") else discord.Color.blue()
                )
                notenoughresultsembed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")

                await interaction.edit_original_response(embed=notenoughresultsembed)
                return
            noresultembed = discord.Embed(
                title=":x: No results found! :x:",
                description=f"No Results for {query} | [Search on Google Yourself For More Results](https://google.com/search?q={properquery})",
                color=discord.Color.red()
            )
            noresultembed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
            await interaction.edit_original_response(embed=noresultembed)
    except requests.exceptions.RequestException as e:
        print(f"An error occurred during the API request: {e}")
        errorembed = discord.Embed(
            title=":x: An error occurred while searching Google. Please try again later. :x:",
            color=discord.Color.red()
        )
        errorembed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
        await interaction.edit_original_response(embed=errorembed)


_MENUS = [("ai", discord.AppCommandType.message), ("google", discord.AppCommandType.message)]       # right-click (context menu) commands in this file

_COMMANDS = [
    (bot.tree, "ai"),
    (bot.tree, "google"),
]


class AI(commands.Cog, name="AI"):
    """/ai and /google."""

    async def cog_unload(self):                  # so a reload (bot.reload_extension) re-registers cleanly
        for parent, name in _COMMANDS:
            parent.remove_command(name)
        for name, kind in _MENUS:
            bot.tree.remove_command(name, type=kind)


async def setup(bot):
    await bot.add_cog(AI(bot))

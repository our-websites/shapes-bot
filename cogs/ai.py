"""Cog: /ai, /clearai and /search."""
import sys
import aiohttp
import re
import urllib.parse
import yapper

m = sys.modules.get("main") or sys.modules["__main__"]   # this bot's main.py: shared helpers, data and the bot itself
AIModel = m.AIModel
AI_DB = m.AI_DB
BraveAPIKey = m.BraveAPIKey
Emojis = m.Emojis
GoogleAPIKey = m.GoogleAPIKey
MainURL = m.MainURL
app_commands = m.app_commands
bot = m.bot
chatgpt = m.chatgpt
discord = m.discord
ecolor = m.ecolor
embedDB = m.embedDB
searchengine = m.searchengine


# --- ai ---
@bot.tree.command(name="clearai", description="Delete your AI chat history")
async def clearai(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    had = AI_DB.exists(user_id)
    AI_DB.remove(user_id)
    await interaction.response.send_message("Your AI chat history was deleted." if had else "You don't have any AI chat history.", ephemeral=True)


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
                f"You are a helpful Yapper assistant chatting with {username}. "
                "Always respond in a single concise paragraph. Otherwise your response will not be recieved due to the embed text limit"
                "Follow Yapper's rules. Do not provide instructions for illegal activity. "
                "Stay safe, respectful, and friendly."
                "Your name is Shapes, the support server is https://yapin.gg/invite/shapes if a user requests it or if you think the prompt is support related."
                "Please stay away from nsfw/bad topics, it will get you deleted/banned."
                "You're created/owned by Spooky Inc. with leandre as your main developer."
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


def _clean_snippet(text):
    text = re.sub(r"<[^>]+>", "", text or "")
    return re.sub(r"&(?:amp|quot|#x27|#39|lt|gt);", lambda m: {"&amp;": "&", "&quot;": '"', "&lt;": "<", "&gt;": ">"}.get(m.group(0), "'"), text).strip()


async def _search_brave(session, query):
    async with session.get(
        "https://api.search.brave.com/res/v1/web/search",
        params={"q": query, "count": 5},
        headers={"X-Subscription-Token": BraveAPIKey, "Accept": "application/json"},
        timeout=aiohttp.ClientTimeout(total=15),
    ) as resp:
        resp.raise_for_status()
        data = await resp.json()
    return [{"title": r.get("title") or "No Title", "link": r.get("url") or "", "snippet": _clean_snippet(r.get("description"))}
            for r in (data.get("web") or {}).get("results", [])[:5]]


async def _search_google_cse(session, query):
    async with session.get(
        "https://www.googleapis.com/customsearch/v1",
        params={"key": GoogleAPIKey, "cx": searchengine, "q": query, "num": 5},
        timeout=aiohttp.ClientTimeout(total=15),
    ) as resp:
        resp.raise_for_status()
        data = await resp.json()
    return [{"title": r.get("title") or "No Title", "link": r.get("link") or "", "snippet": _clean_snippet(r.get("snippet"))}
            for r in data.get("items", [])[:5]]


def _search_providers():
    """Providers that have a key set, in order of preference."""
    found = []
    if BraveAPIKey:
        found.append(("Brave", _search_brave))
    if GoogleAPIKey:
        found.append(("Google", _search_google_cse))
    return found


@bot.tree.command(name="search", description="Search the web.")
async def search(interaction: discord.Interaction, query: str = "Yapin'"):
    await interaction.response.defer()

    thinkingembed = discord.Embed(
        title=f"{Emojis.get('loading')} {interaction.user.mention} Searching For {query}",
        color=ecolor(interaction.user.id)
    )
    await interaction.followup.send(embed=thinkingembed)

    more = f"https://www.google.com/search?q={urllib.parse.quote_plus(query)}"
    providers = _search_providers()
    results, used, failed = None, None, False
    async with aiohttp.ClientSession() as session:
        for name, fn in providers:
            try:
                results, used = await fn(session, query), name
                break
            except Exception as e:
                failed = True
                print(f"[google] {name} search failed: {type(e).__name__}: {e}")

    if results:
        lines = [f"**{i}. [{r['title']}]({r['link']})**\n{r['snippet']}" for i, r in enumerate(results, 1) if r["link"]]
        embed = discord.Embed(
            title=f"Search Results For {query}",
            description="\n\n".join(lines) + f"\n\n[Search For More Results]({more})",
            color=ecolor(interaction.user.id)
        )
        embed.set_footer(text=f"Requested By {interaction.user.name} via {used} | {MainURL}")
        await interaction.edit_original_response(embed=embed)
        return

    if not providers:
        title, desc = ":x: Search isn't set up :x:", f"The bot owner needs to set BRAVE_API_KEY. [Search it yourself]({more})"
    elif failed:
        title, desc = ":x: An error occurred while searching. Please try again later. :x:", f"[Search it yourself]({more})"
    else:
        title, desc = ":x: No results found! :x:", f"No Results for {query} | [Search on Google Yourself For More Results]({more})"
    embed = discord.Embed(title=title, description=desc, color=discord.Color.red())
    embed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
    await interaction.edit_original_response(embed=embed)


class AI(yapper.Cog):
    name = "AI"


def setup(bot):
    bot.add_cog(AI(bot))

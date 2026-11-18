"""Cog: small fun commands."""
import sys
from discord import app_commands
from discord.ext import commands
import aiohttp
import discord
import random
import re
import socket

m = sys.modules.get("main") or sys.modules["__main__"]   # this bot's main.py: shared helpers, data and the bot itself
MainURL = m.MainURL
_embed_color = m._embed_color


EIGHT_BALL = ["It is certain.", "Without a doubt.", "Yes, definitely.", "You may rely on it.", "As I see it, yes.", "Most likely.", "Outlook good.",
              "Signs point to yes.", "Reply hazy, try again.", "Ask again later.", "Better not tell you now.", "Cannot predict now.",
              "Concentrate and ask again.", "Don't count on it.", "My reply is no.", "My sources say no.", "Outlook not so good.", "Very doubtful."]


def roll_dice(spec):
    """'2d6+3' -> (rolls, modifier, total). 1 to 20 dice with 2 to 1000 sides; a bare number means that many sides of one die."""
    spec = str(spec or "1d6").lower().replace(" ", "")
    m = re.fullmatch(r"(\d{0,2})d?(\d{1,4})([+-]\d{1,6})?", spec)
    if not m:
        raise ValueError("Use dice like `2d6`, `d20` or `3d8+2`.")
    count, sides, mod = int(m.group(1) or 1), int(m.group(2)), int(m.group(3) or 0)
    if not 1 <= count <= 20 or not 2 <= sides <= 1000:
        raise ValueError("Roll 1 to 20 dice with 2 to 1000 sides.")
    rolls = [random.randint(1, sides) for _ in range(count)]
    return rolls, mod, sum(rolls) + mod


def safe_calc(text):
    """Evaluate arithmetic without eval(): + - * / // % ** ( ), pi, e and sqrt/abs/round/floor/ceil/sin/cos/tan/log/min/max."""
    import ast
    import math
    text = str(text or "").strip().replace("^", "**").replace("×", "*").replace("÷", "/")
    if not text or len(text) > 200:
        raise ValueError("Give me a short sum, like `(3 + 4) * 2`.")
    names = {"pi": math.pi, "e": math.e, "tau": math.tau}
    funcs = {"sqrt": math.sqrt, "abs": abs, "round": round, "floor": math.floor, "ceil": math.ceil, "sin": math.sin, "cos": math.cos,
             "tan": math.tan, "log": math.log, "ln": math.log, "min": min, "max": max}
    ops = {ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b, ast.Mult: lambda a, b: a * b, ast.Div: lambda a, b: a / b,
           ast.FloorDiv: lambda a, b: a // b, ast.Mod: lambda a, b: a % b, ast.Pow: lambda a, b: a ** b}

    def ev(node, depth=0):
        if depth > 30:
            raise ValueError("That's too deeply nested.")
        if isinstance(node, ast.Expression):
            return ev(node.body, depth + 1)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return node.value
        if isinstance(node, ast.Name) and node.id in names:
            return names[node.id]
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            v = ev(node.operand, depth + 1)
            return v if isinstance(node.op, ast.UAdd) else -v
        if isinstance(node, ast.BinOp) and type(node.op) in ops:
            a, b = ev(node.left, depth + 1), ev(node.right, depth + 1)
            if isinstance(node.op, ast.Pow) and (abs(b) > 1000 or abs(a) > 1e9):
                raise ValueError("That power is too big.")
            return ops[type(node.op)](a, b)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in funcs and not node.keywords and len(node.args) <= 8:
            return funcs[node.func.id](*[ev(a, depth + 1) for a in node.args])
        raise ValueError("I can only do numbers, + - * / // % ** and a few functions (sqrt, abs, round, floor, ceil, sin, cos, tan, log, min, max).")
    try:
        result = ev(ast.parse(text, mode="eval"))
    except ZeroDivisionError:
        raise ValueError("You can't divide by zero.")
    except (SyntaxError, TypeError, OverflowError):
        raise ValueError("I couldn't read that sum.")
    if isinstance(result, float):
        if result != result or result in (float("inf"), float("-inf")):
            raise ValueError("That has no sensible answer.")
        result = round(result, 10)
        if result == int(result) and abs(result) < 1e15:
            result = int(result)
    return result


def split_choices(text):
    """'pizza, tacos | sushi' -> ['pizza', 'tacos', 'sushi'] (commas, pipes, semicolons or ' or ')."""
    parts = re.split(r"\s*(?:,|\||;|\bor\b)\s*", str(text or ""), flags=re.I)
    return [p.strip() for p in parts if p.strip()][:25]


async def fetch_json(url, timeout=8):
    connector = aiohttp.TCPConnector(family=socket.AF_INET)
    async with aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=timeout)) as session:
        async with session.get(url, headers={"User-Agent": "ShapesBot/1.0"}) as r:
            if r.status != 200:
                raise RuntimeError(f"{url.split('/')[2]} answered {r.status}")
            return await r.json(content_type=None)


async def random_cat():
    data = await fetch_json("https://api.thecatapi.com/v1/images/search")
    return data[0]["url"]


async def random_dog():
    data = await fetch_json("https://dog.ceo/api/breeds/image/random")
    return data["message"]


async def random_fact():
    data = await fetch_json("https://uselessfacts.jsph.pl/api/v2/facts/random?language=en")
    return data["text"]


def _everywhere(fn):
    """Usable in servers, DMs and group chats, and as a user-installed app."""
    fn = app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)(fn)
    return app_commands.allowed_installs(guilds=True, users=True)(fn)


class FunCog(commands.Cog, name="Fun"):
    """/roll /coinflip /8ball /choose /calc /cat /dog /fact."""

    def __init__(self, bot):
        self.bot = bot

    @staticmethod
    def _embed(interaction, title=None, description=None):
        embed = discord.Embed(title=title, description=description, color=_embed_color(interaction.user.id))
        embed.set_footer(text=f"Requested By {interaction.user.name} | {MainURL}")
        return embed

    @staticmethod
    async def _error(interaction, message):
        await interaction.response.send_message(embed=discord.Embed(description=f"❌ {message}", color=discord.Color.red()), ephemeral=True)

    @app_commands.command(name="roll", description="Roll dice, like 2d6 or d20+3")
    @app_commands.describe(dice="For example 2d6, d20 or 3d8+2 (default 1d6)")
    @_everywhere
    async def roll(self, interaction: discord.Interaction, dice: str = "1d6"):
        try:
            rolls, mod, total = roll_dice(dice)
        except ValueError as e:
            return await self._error(interaction, str(e))
        shown = " + ".join(str(r) for r in rolls) + (f" {'+' if mod > 0 else '-'} {abs(mod)}" if mod else "")
        await interaction.response.send_message(embed=self._embed(interaction, f"🎲 {dice.strip() or '1d6'}", f"{shown}\n**Total: {total:,}**" if len(rolls) > 1 or mod else f"**{total:,}**"))

    @app_commands.command(name="coinflip", description="Flip a coin")
    @_everywhere
    async def coinflip(self, interaction: discord.Interaction):
        await interaction.response.send_message(embed=self._embed(interaction, "🪙 Coin flip", f"It's **{random.choice(['heads', 'tails'])}**!"))

    @app_commands.command(name="8ball", description="Ask the magic 8-ball a question")
    @app_commands.describe(question="Your yes/no question")
    @_everywhere
    async def eightball(self, interaction: discord.Interaction, question: str):
        await interaction.response.send_message(embed=self._embed(interaction, "🎱 Magic 8-ball", f"**{question[:200]}**\n{random.choice(EIGHT_BALL)}"))

    @app_commands.command(name="choose", description="Let me pick between options")
    @app_commands.describe(options="Options separated by commas, | or the word or")
    @_everywhere
    async def choose(self, interaction: discord.Interaction, options: str):
        picks = split_choices(options)
        if len(picks) < 2:
            return await self._error(interaction, "Give me at least two options, like `pizza, tacos, sushi`.")
        await interaction.response.send_message(embed=self._embed(interaction, "🤔 I choose…", f"**{random.choice(picks)}**\n-# out of {len(picks)} options"))

    @app_commands.command(name="calc", description="Do some maths")
    @app_commands.describe(expression="For example (3 + 4) * 2, 2^10 or sqrt(144)")
    @_everywhere
    async def calc(self, interaction: discord.Interaction, expression: str):
        try:
            result = safe_calc(expression)
        except ValueError as e:
            return await self._error(interaction, str(e))
        shown = f"{result:,}" if isinstance(result, int) else str(result)
        await interaction.response.send_message(embed=self._embed(interaction, "🧮 Calculator", f"`{expression.strip()[:200]}`\n= **{shown}**"))

    async def _picture(self, interaction, title, getter):
        await interaction.response.defer()
        try:
            url = await getter()
        except Exception as e:
            print(f"[{title}] {e}")
            return await interaction.followup.send(embed=discord.Embed(description="❌ I couldn't fetch one right now. Try again in a moment.", color=discord.Color.red()))
        await interaction.followup.send(embed=self._embed(interaction, title).set_image(url=url))

    @app_commands.command(name="cat", description="A random cat picture")
    @_everywhere
    async def cat(self, interaction: discord.Interaction):
        await self._picture(interaction, "🐱 Cat", random_cat)

    @app_commands.command(name="dog", description="A random dog picture")
    @_everywhere
    async def dog(self, interaction: discord.Interaction):
        await self._picture(interaction, "🐶 Dog", random_dog)

    @app_commands.command(name="fact", description="A random fact")
    @_everywhere
    async def fact(self, interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            text = await random_fact()
        except Exception as e:
            print(f"[fact] {e}")
            return await interaction.followup.send(embed=discord.Embed(description="❌ I couldn't fetch a fact right now. Try again in a moment.", color=discord.Color.red()))
        await interaction.followup.send(embed=self._embed(interaction, "💡 Random fact", text[:1500]))

async def setup(bot):
    await bot.add_cog(FunCog(bot))

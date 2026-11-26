"""Cog: Roblox verification, linked accounts and group info: the /roblox group."""
import sys
from datetime import datetime, timezone
from discord import app_commands
from discord.ext import commands
from flask import Flask, render_template_string, request, redirect, url_for, session, jsonify, Response
import aiohttp
import asyncio
import discord
import hmac
import os
import re
import requests
import time
import urllib.parse

m = sys.modules.get("main") or sys.modules["__main__"]   # this bot's main.py: shared helpers, data and the bot itself
APIBaseURL_Key = m.APIBaseURL_Key
API_DOWN_TEXT = m.API_DOWN_TEXT
_ROBLOX = m._ROBLOX
_api_mark = m._api_mark
_tasks = m._tasks
roblox_group = m.roblox_group
roblox_panel = m.roblox_panel


# =============================================================================================================
# Roblox verification (/roblox ...), command logs (/cmdlog, /botlog), Vortex (/vortex ...), /server info.
# OAuth2 through the Shapes API, never bio text. Same system as the Yapin' bot on the same API data: people link Roblox
# accounts once (up to 100, no Yapin' account needed) and every server that uses verification sees them. A server admin
# points the bot at a Roblox group with /roblox panel and the bot mirrors it: one Discord role per group role (created,
# renamed and deleted as the group changes, when mirroring is on), a verified role and nicknames. Members verify with
# /roblox verify or the Verify button of a panel. They are synced when they verify, on join, on /roblox sync and by a
# backup sync every 24 hours (elapsed UTC seconds, so daylight saving can't move it). Roblox is only asked during a sync.
# Command logs post through a webhook (shared by the logs in one channel), falling back to the bot posting directly.
# =============================================================================================================
PLATFORM = "discord"


SYNC_EVERY = 24 * 60 * 60           # seconds of real elapsed time


CHECK_EVERY = 300                   # how often the scheduler looks at the clock


YAPIN_DASH = os.environ.get("YAPIN_DASH_URL", "https://shapes-yapper.oriaxen.com/dashboard/roblox")


_locks = {}


_prompted = {}


_clicked = {}


class Issues:
    """What went wrong during one sync, grouped so the person gets one clear explanation instead of 500 identical errors."""

    def __init__(self, role_names=None):
        self.role_names = role_names or {}
        self.kinds = {}               # kind -> {"n": count, "names": set, "sample": text}
        self.blocked = set()          # role ids that were refused: stop trying them for everyone else
        self.nick_ok = self.nick_denied = 0

    @staticmethod
    def denied(e):
        return isinstance(e, discord.Forbidden) or getattr(e, "status", None) in (401, 403)

    def add(self, kind, e, name=None):
        """kind: create-role | edit-role | delete-role | give-role | take-role | nickname. Returns True when it was a refusal."""
        refused = self.denied(e)
        key = kind if refused else "other"
        d = self.kinds.setdefault(key, {"n": 0, "names": set(), "sample": ""})
        d["n"] += 1
        if name:
            d["names"].add(str(name))
        d["sample"] = d["sample"] or f"{kind}: {e}" + (f" (HTTP {e.status})" if getattr(e, "status", None) else "")
        return refused

    def name(self, rid):
        return self.role_names.get(str(rid), str(rid))

    def lines(self):
        """Plain-language explanations, most important first."""
        out = []

        def names(k):
            n = sorted(self.kinds[k]["names"])
            return (" (" + ", ".join(n[:8]) + (", …" if len(n) > 8 else "") + ")") if n else ""
        manage = [k for k in ("create-role", "edit-role", "delete-role") if k in self.kinds]
        if manage:
            what = {"create-role": "create", "edit-role": "rename", "delete-role": "delete"}
            out.append("I'm not allowed to " + "/".join(what[k] for k in manage) + f" roles{names(manage[0])}. Give my role the **Manage Roles** permission "
                       "(Server Settings → Roles → my role → Permissions), then run `/roblox sync` again. Without it I can only use roles that already exist.")
        given = [k for k in ("give-role", "take-role") if k in self.kinds]
        if given:
            n = sum(self.kinds[k]["n"] for k in given)
            nm = sorted(set().union(*(self.kinds[k]["names"] for k in given)))
            out.append(f"I couldn't change {n} role assignment{'s' if n != 1 else ''}" + (f" for **{', '.join(nm[:8])}**" if nm else "") +
                       ". A bot can only hand out roles that sit **below its own highest role**: drag my role above "
                       + ("those roles" if nm else "them") + " in Server Settings → Roles, and make sure it has **Manage Roles**.")
        if "nickname" in self.kinds:
            n = self.kinds["nickname"]["n"]
            out.append(f"I couldn't change {n} nickname{'s' if n != 1 else ''}. I need the **Manage Nicknames** permission and my role must be above "
                       "the member's highest role. Discord never lets a bot rename the server owner.")
        if "other" in self.kinds:
            d = self.kinds["other"]
            out.append(f"{d['n']} other error{'s' if d['n'] != 1 else ''} from Discord, for example: `{d['sample'][:200]}`")
        return out


def roblox_init(m):
    """m is the bot's main module (bot, embedDB, APIBaseURL, app...)."""
    bot = m.bot
    stateDB = m.rverifyDB                          # guild id -> {"roles": {roblox role id: discord role id}, "made": [...], "last": {...}}

    token = APIBaseURL_Key
    headers = {"Authorization": f"Bearer {token}"}
    http = requests.Session()

    def api_sync(method, path, body=None):
        """The Shapes API's JSON, or None when it can't be reached. Blocking: call through api()."""
        if not token:
            return None
        try:
            r = m.api_link.request(method, path, session=http, json=body, headers=headers, timeout=30)
            if r.status_code >= 502:
                raise requests.RequestException(f"HTTP {r.status_code}")
            data = r.json()
            _api_mark(True)
            return data if isinstance(data, dict) else None
        except (requests.RequestException, ValueError):
            _api_mark(False)
            return None

    async def api(method, path, body=None):
        return await asyncio.to_thread(api_sync, method, path, body)

    def color_of(user_id):
        saved = m.embedDB.get(f"{user_id}")
        return saved if saved else discord.Color.blue()

    # ------------------------------------------------------------------ helpers
    async def cfg_of(gid):
        d = await api("GET", f"/internal/guild/{PLATFORM}/{gid}/config")
        c = (d or {}).get("config") or {}
        return c if c.get("group_id") else None

    def template(cfg, acct, role_name):
        t = (cfg.get("nickname_template") or "").strip()
        if not t:
            return None
        out = (t.replace("{name}", acct.get("name") or "").replace("{display}", acct.get("display_name") or acct.get("name") or "")
                .replace("{id}", str(acct.get("id"))).replace("{rank}", role_name or ""))
        return out.strip(" |-–—·/:")[:32] if not role_name else out.strip()[:32]

    async def group_role(cfg, rid):
        d = await api("GET", f"/internal/roblox/group/{cfg['group_id']}/member/{rid}")
        return (d or {}).get("role") if d and d.get("success") else False       # False = Roblox didn't answer

    async def mirror_roles(guild, cfg, report=None, issues=None):
        """Make the Discord roles match the group's roles when mirroring is on: create missing ones, rename changed ones, delete
        ones the group dropped (only roles this bot made, never one that was already there). Returns {roblox role id: discord
        role id}; `report` (a dict) gets counts of what changed."""
        gid = str(guild.id)
        report = report if report is not None else {}
        issues = issues if issues is not None else Issues()
        for k in ("created", "renamed", "deleted"):
            report.setdefault(k, 0)
        st = dict(stateDB.get(gid) or {})
        mapping = dict(st.get("roles") or {})
        made = set(st.get("made") or [])
        d = await api("GET", f"/internal/roblox/group/{cfg['group_id']}/roles")
        if not d or not d.get("success"):
            return mapping
        group = {str(r["id"]): r for r in d["roles"] if int(r.get("rank") or 0) > 0}      # rank 0 is "Guest" = not in the group
        have = {str(r.id): r for r in guild.roles}
        by_name = {r.name.lower(): str(r.id) for r in guild.roles if not r.is_default() and not r.managed}
        mirror = bool(cfg.get("mirror_roles"))
        for rid, r in group.items():
            current = mapping.get(rid)
            if current and current in have:
                if mirror and have[current].name != r["name"]:       # renamed in the group
                    try:
                        await have[current].edit(name=r["name"], reason="Roblox group role renamed")
                        report["renamed"] += 1
                    except discord.HTTPException as e:
                        issues.add("edit-role", e, have[current].name)
                        if Issues.denied(e):
                            mirror = False                           # no point asking again for every other role
                continue
            if r["name"].lower() in by_name:                         # a role with that name already exists: use it
                mapping[rid] = by_name[r["name"].lower()]
            elif mirror:
                try:
                    new = await guild.create_role(name=r["name"], reason="Mirroring the Roblox group's roles")
                    mapping[rid] = str(new.id)
                    made.add(str(new.id))
                    have[str(new.id)] = new
                    report["created"] += 1
                except discord.HTTPException as e:
                    issues.add("create-role", e, r["name"])
                    if Issues.denied(e):
                        mirror = False                               # missing Manage Roles: stop here, the rest would fail the same way
        for rid in [k for k in mapping if k not in group]:           # the group dropped this role
            yid = mapping[rid]
            if mirror and yid in made and yid in have:
                try:
                    await have[yid].delete(reason="The Roblox group no longer has this role")
                    report["deleted"] += 1
                except discord.HTTPException as e:
                    issues.add("delete-role", e, issues.name(yid))
                    continue                                         # keep it in the map so the next sync tries again
            made.discard(yid)
            mapping.pop(rid, None)
        st["roles"], st["made"], st["at"] = mapping, sorted(made & {str(x) for x in mapping.values()}), time.time()
        stateDB.set(gid, st)
        stateDB.save()
        return mapping

    async def mapping_for(guild, cfg):
        """The saved role map; a fresh mirror only when it is older than ten minutes (so a busy panel doesn't hammer Roblox)."""
        st = stateDB.get(str(guild.id)) or {}
        if st.get("roles") and time.time() - float(st.get("at") or 0) < 600:
            return dict(st["roles"])
        return await mirror_roles(guild, cfg)

    async def ensure_verified_role(guild, cfg, issues=None):
        if cfg.get("verified_role_id") == "create":
            try:
                role = await guild.create_role(name="Verified", reason="Roblox verification")
                await api("PUT", f"/internal/guild/{PLATFORM}/{guild.id}/config", {"verified_role_id": str(role.id)})
                cfg["verified_role_id"] = str(role.id)
            except discord.HTTPException as e:
                if issues is not None:
                    issues.add("create-role", e, "Verified")
                cfg["verified_role_id"] = ""
        return cfg.get("verified_role_id") or None

    async def sync_member(guild, member, acct, cfg, mapping, issues):
        """Bring one member in line: roles from their group rank, the verified role, the nickname. acct=None unverifies."""
        mine = {str(r.id) for r in member.roles}
        want, drop = set(), set(mapping.values())
        vr, ur = cfg.get("verified_role_id") or None, cfg.get("unverified_role_id") or None
        role_name = None
        if acct:
            if vr and vr != "create":
                want.add(vr)
            role = await group_role(cfg, acct["id"])
            if role is False:
                return "roblox-unreachable"
            if role and str(role.get("id")) in mapping and int(role.get("rank") or 0) > 0:
                want.add(mapping[str(role["id"])])
                role_name = role.get("name")
        elif ur:
            want.add(ur)
        drop |= {x for x in (vr, ur) if x and x != "create"}
        roles = {str(r.id): r for r in guild.roles}
        for rid in (want - mine) - issues.blocked:
            if rid not in roles:
                continue
            try:
                await member.add_roles(roles[rid], reason="Roblox verification")
            except discord.HTTPException as e:
                if issues.add("give-role", e, issues.name(rid)):
                    issues.blocked.add(rid)                          # refused once for this role: skip it for everyone else
        for rid in ((drop - want) & mine) - issues.blocked:
            if rid not in roles:
                continue
            try:
                await member.remove_roles(roles[rid], reason="Roblox verification")
            except discord.HTTPException as e:
                if issues.add("take-role", e, issues.name(rid)):
                    issues.blocked.add(rid)
        if acct:
            nick = template(cfg, acct, role_name)
            if nick and member.nick != nick and member.id != member.guild.owner_id and not (issues.nick_denied >= 5 and not issues.nick_ok):
                try:
                    await member.edit(nick=nick, reason="Roblox verification")
                    issues.nick_ok += 1
                except discord.HTTPException as e:
                    if issues.add("nickname", e):
                        issues.nick_denied += 1
        return "ok"

    async def dm_verified(user_id, acct, how, guild_name=None):
        """The message every verification gets: avatar, username, user ID and a profile link."""
        embed = discord.Embed(title=f"✅ {'Auto-verified' if how == 'auto' else 'Verified'} as {acct['name']}", color=discord.Color.green(),
                              description=f"[{acct['name']}]({acct['profile']})\nUser ID: `{acct['id']}`" + (f"\nServer: **{guild_name}**" if guild_name else ""))
        if acct.get("avatar"):
            embed.set_thumbnail(url=acct["avatar"])
        embed.set_footer(text="Manage your accounts with /roblox accounts")
        try:
            user = bot.get_user(int(user_id)) or await bot.fetch_user(int(user_id))
            await user.send(embed=embed)
        except (discord.HTTPException, ValueError) as e:
            print(f"[verify] couldn't DM {user_id}: {e}")

    async def remember(gid, counts, issues):
        """Keep the last sync's result and problems so the Yapin' dashboard (or /roblox panel status) can show them."""
        st = dict(stateDB.get(gid) or {})
        st["last"] = {"at": int(time.time()), "counts": counts, "problems": issues.lines()}
        stateDB.set(gid, st)
        stateDB.save()

    async def apply_one(guild, member, quiet=False):
        """Verify/auto-verify one member of a configured server, then sync them. Returns the account or None."""
        gid = str(guild.id)
        cfg = await cfg_of(gid)
        if not cfg:
            return None
        res = ((await api("POST", f"/internal/guild/{PLATFORM}/{gid}/resolve", {"user_ids": [str(member.id)]})) or {}).get("members", {}).get(str(member.id))
        if not res:
            return None
        issues = Issues({str(r.id): r.name for r in guild.roles})
        await ensure_verified_role(guild, cfg, issues)
        await sync_member(guild, member, res, cfg, await mapping_for(guild, cfg), issues)
        if issues.kinds:
            print(f"[verify] {member.id} in {gid}: " + " | ".join(issues.lines()))
            await remember(gid, {"single": True}, issues)
        if not quiet:
            await dm_verified(member.id, res, res.get("how"), guild.name)
        return res

    # ------------------------------------------------------------------ full sync
    async def sync_guild(guild):
        """Sync every member of a server. One at a time per server. Returns counts."""
        gid = str(guild.id)
        lock = _locks.setdefault(gid, asyncio.Lock())
        if lock.locked():
            return {"busy": True}
        async with lock:
            counts = {"synced": 0, "unverified": 0, "skipped": 0, "errors": 0, "created": 0, "renamed": 0, "deleted": 0}
            cfg = await cfg_of(gid)
            if not cfg:
                return {"error": "no group set"}
            issues = Issues({str(r.id): r.name for r in guild.roles})
            await ensure_verified_role(guild, cfg, issues)
            mapping = await mirror_roles(guild, cfg, counts, issues)
            members = [mem async for mem in guild.fetch_members(limit=None) if not mem.bot]
            found = {}
            ids = [str(mem.id) for mem in members]
            for i in range(0, len(ids), 500):
                found.update(((await api("POST", f"/internal/guild/{PLATFORM}/{gid}/resolve", {"user_ids": ids[i:i + 500]})) or {}).get("members", {}))
            for mem in members:
                try:
                    r = await sync_member(guild, mem, found.get(str(mem.id)), cfg, mapping, issues)
                    counts["synced" if found.get(str(mem.id)) and r == "ok" else "unverified" if r == "ok" else "skipped"] += 1
                except Exception as e:
                    counts["errors"] += 1
                    print(f"[verify] sync {mem.id} in {gid}: {e}")
                await asyncio.sleep(0.3)
            await api("POST", f"/internal/guild/{PLATFORM}/{gid}/synced", {})
            counts["problems"] = issues.lines()
            await remember(gid, {k: v for k, v in counts.items() if k != "problems"}, issues)
            return counts

    def summary_text(counts):
        if counts.get("busy"):
            return "A sync is already running here."
        if "synced" not in counts:
            return f"Sync failed: {counts.get('error')}"
        text = (f"Sync finished: {counts['synced']} verified members updated, {counts['unverified']} not verified, "
                f"{counts['skipped']} skipped, {counts['errors']} errors.")
        if counts["created"] or counts["renamed"] or counts["deleted"]:
            text += f" Roles: {counts['created']} created, {counts['renamed']} renamed, {counts['deleted']} deleted."
        if counts.get("problems"):
            text += "\n\n**Things that need fixing:**\n" + "\n".join(f"• {l}" for l in counts["problems"])
        return text

    async def scheduler():
        """Backup sync: a server is due when 24 hours of real time have passed since its last sync (UTC epoch seconds)."""
        while True:
            await asyncio.sleep(CHECK_EVERY)
            try:
                for cfg in ((await api("GET", f"/internal/guilds/{PLATFORM}")) or {}).get("guilds", []):
                    if time.time() - int(cfg.get("last_sync_at") or 0) >= SYNC_EVERY:
                        guild = bot.get_guild(int(cfg["guild_id"]))
                        if guild:
                            await sync_guild(guild)
            except Exception as e:
                print(f"[verify] scheduler: {e}")

    started = {"scheduler": False}

    async def on_ready_start():
        if not started["scheduler"]:
            started["scheduler"] = True
            bot.loop.create_task(scheduler())
    bot.add_listener(on_ready_start, "on_ready")

    # ------------------------------------------------------------------ joins and the API's notice
    async def start_link(uid, gid=""):
        """(url, message): a one-time Roblox sign-in link, or why there isn't one."""
        d = await api("POST", "/internal/roblox/start", {"platform": PLATFORM, "user_id": str(uid), "guild": f"{PLATFORM}:{gid}" if gid else ""})
        if d and d.get("success") and d.get("url"):
            return d["url"], None
        if d and d.get("data"):
            return None, d["data"]
        return None, "I couldn't make a Roblox sign-in link right now."

    def yapin_note():
        return (f"You don't need a Yapin' account. If you have one, open {YAPIN_DASH} and press **Link Discord** to merge them, "
                "and your accounts show up in both places.")

    async def on_member_join(member):
        """A member joined: auto-verify them if they chose that, else DM the server's prompt (once a day per server)."""
        if member.bot:
            return
        gid, uid = str(member.guild.id), str(member.id)
        cfg = await cfg_of(gid)
        if not cfg:
            return
        if await apply_one(member.guild, member):
            return
        if cfg.get("prompt_dm") and time.time() - _prompted.get((gid, uid), 0) > 86400:
            _prompted[(gid, uid)] = time.time()
            link, why = await start_link(uid, gid)
            text = cfg.get("prompt_text") or f"**{member.guild.name}** uses Roblox verification."
            body = f"{text}\n\nSign in with Roblox here (no bio changes needed): {link}" if link else f"{text}\n\nRun `/roblox verify` in the server to get started."
            try:
                await member.send(embed=discord.Embed(title="Verify your Roblox account", description=body, color=discord.Color.blurple()))
            except discord.HTTPException as e:
                print(f"[verify] couldn't DM the prompt to {uid}: {e}")
    bot.add_listener(on_member_join, "on_member_join")

    def verified_notice():
        if not token or not hmac.compare_digest(request.headers.get("Authorization", "").removeprefix("Bearer ").strip(), token):
            return jsonify({"success": False}), 401
        b = request.get_json(silent=True) or {}
        uid = str(b.get("discord_id") or "")
        if not uid:
            return jsonify({"success": True, "ignored": True})
        acct = {"id": b.get("roblox_id"), "name": b.get("roblox_name"), "avatar": b.get("avatar"), "profile": b.get("profile")}

        async def work():
            await dm_verified(uid, acct, "auto" if b.get("auto_verify") else "verified")
            guild_hint = str(b.get("guild") or "")
            if guild_hint.startswith(f"{PLATFORM}:"):               # they came from a server's prompt: verify them there too
                await api("POST", f"/internal/guild/{PLATFORM}/{guild_hint.split(':', 1)[1]}/verify", {"user_id": uid, "roblox_id": acct["id"]})
            for cfg in ((await api("GET", f"/internal/guilds/{PLATFORM}")) or {}).get("guilds", []):      # keep every server they're in current
                guild = bot.get_guild(int(cfg["guild_id"]))
                member = guild.get_member(int(uid)) if guild else None
                if member:
                    await apply_one(guild, member, quiet=True)
        asyncio.run_coroutine_threadsafe(work(), bot.loop)
        return jsonify({"success": True})

    # ------------------------------------------------------------------ the panel (a message with a Verify button)
    def panel_embed(cfg, guild):
        title = (cfg.get("panel_title") or "").strip() or "Verify with Roblox"
        text = (cfg.get("panel_text") or "").strip() or (
            f"Press **Verify** to link your Roblox account to {guild.name}. You sign in with Roblox "
            "(nothing goes in your bio) and I'll give you your roles and nickname.\n"
            "Already linked? You're verified in one click. Pick another account with `/roblox verify`.")
        return discord.Embed(title=title[:256], description=text[:3000], color=discord.Color.blurple())

    async def send_panel(guild, channel, cfg):
        """Post the panel (or update the one posted before when it is in the same channel)."""
        embed = panel_embed(cfg, guild)
        old_ch, old_msg = cfg.get("panel_channel_id"), cfg.get("panel_message_id")
        if old_msg and str(old_ch) == str(channel.id):
            try:
                msg = await channel.fetch_message(int(old_msg))
                await msg.edit(embed=embed, view=PanelView())
                return msg
            except discord.HTTPException:
                pass                                                 # deleted in the meantime: post a new one
        msg = await channel.send(embed=embed, view=PanelView())
        await api("PUT", f"/internal/guild/{PLATFORM}/{guild.id}/config", {"panel_channel_id": str(channel.id), "panel_message_id": str(msg.id)})
        return msg

    class PanelView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=None)

        @discord.ui.button(label="Verify", style=discord.ButtonStyle.primary, custom_id="rv:verify")
        async def verify(self, interaction: discord.Interaction, button: discord.ui.Button):
            uid, guild = str(interaction.user.id), interaction.guild
            if not guild:
                return await interaction.response.send_message("Press the button in a server.", ephemeral=True)
            if time.time() - _clicked.get(uid, 0) < 5:
                return await interaction.response.send_message("One moment, I'm still working on your last click.", ephemeral=True)
            _clicked[uid] = time.time()
            if len(_clicked) > 2000:
                _clicked.clear()
            await interaction.response.defer(ephemeral=True)
            cfg = await cfg_of(str(guild.id))
            if not cfg:
                return await interaction.followup.send("This server hasn't finished setting up Roblox verification yet.", ephemeral=True)
            res = await api("POST", f"/internal/guild/{PLATFORM}/{guild.id}/verify", {"user_id": uid, "roblox_id": None})
            if not res or not res.get("success"):
                if res is None:
                    return await interaction.followup.send("Verification isn't available right now. Try again in a moment.", ephemeral=True)
                link, why = await start_link(uid, str(guild.id))
                if not link:
                    return await interaction.followup.send(why or "Couldn't verify you.", ephemeral=True)
                return await interaction.followup.send(f"Sign in with Roblox to verify (OAuth2, nothing goes in your bio). The link works for 10 minutes and only for you:\n{link}\n"
                                                       "When you finish I'll verify you here automatically.", ephemeral=True)
            acct = res.get("account") or {}
            who = f"[{acct.get('name')}]({acct.get('profile')}) (`{acct.get('id')}`)" if acct.get("id") else "your Roblox account"
            await interaction.followup.send(f"Verified as {who}. I'm updating your roles and nickname now.", ephemeral=True)
            member = interaction.user if isinstance(interaction.user, discord.Member) else guild.get_member(interaction.user.id)
            if member:
                await apply_one(guild, member)

    bot.add_view(PanelView())


    # ------------------------------------------------------------------ the /roblox command group
    def is_manager(interaction):
        user = interaction.user
        return bool(interaction.guild) and (user.id == interaction.guild.owner_id or user.guild_permissions.administrator or user.guild_permissions.manage_guild)

    def split_names(value):
        return [t for t in (value or "").replace(",", " ").split() if t]

    roblox, panel = roblox_group, roblox_panel

    async def say(interaction, text, **kw):
        return await interaction.response.send_message(text, ephemeral=True, **kw)

    async def my_person(interaction):
        """(accounts list, error text). The API makes the person record on first contact, so no Yapin' account is needed."""
        person = await api("GET", f"/internal/person/{PLATFORM}/{interaction.user.id}") or {}
        if not person.get("success"):
            return None, API_DOWN_TEXT
        return person.get("accounts") or [], None

    def pick(accts, q):
        return next((x for x in accts if str(x["id"]) == q or x["name"].lower() == q.lower()), None)

    @roblox.command(name="verify", description="Verify in this server (as your default account, or the one you name)")
    @app_commands.describe(account="A linked account's name or id (empty = your default)")
    async def roblox_verify(interaction: discord.Interaction, account: str = ""):
        uid, gid = str(interaction.user.id), str(interaction.guild_id or "")
        if not interaction.guild:
            return await say(interaction, "Run this in a server to verify there. To link an account from here, use `/roblox link`.")
        if not await cfg_of(gid):
            return await say(interaction, "This server hasn't set up Roblox verification.")
        accts, err = await my_person(interaction)
        if err:
            return await say(interaction, err)
        if not accts:
            link, why = await start_link(uid, gid)
            return await say(interaction, f"Sign in with Roblox to verify (OAuth2, nothing goes in your bio). The link works for 10 minutes and only for you:\n{link}\n"
                                          "When you finish I'll verify you here automatically." if link else why)
        res = await api("POST", f"/internal/guild/{PLATFORM}/{gid}/verify", {"user_id": uid, "roblox_id": account or None})
        if not res or not res.get("success"):
            return await say(interaction, (res or {}).get("data") or "Couldn't verify you.")
        await interaction.response.defer(ephemeral=True)
        member = interaction.user if isinstance(interaction.user, discord.Member) else interaction.guild.get_member(interaction.user.id)
        if member:
            await apply_one(interaction.guild, member)
        await interaction.followup.send(f"Verified as **{res['account']['name']}**. I've sent you a message with the details.", ephemeral=True)

    @roblox.command(name="link", description="Link a Roblox account by signing in with Roblox (no Yapin' account needed)")
    async def roblox_link(interaction: discord.Interaction):
        link, why = await start_link(interaction.user.id, interaction.guild_id or "")
        if not link:
            return await say(interaction, why)
        view = discord.ui.View(timeout=600)
        view.add_item(discord.ui.Button(label="Link with Roblox", style=discord.ButtonStyle.link, url=link))
        await say(interaction, "Sign in with Roblox (OAuth2, nothing goes in your bio). The link works for 10 minutes and only for you.\n" + yapin_note(), view=view)

    @roblox.command(name="accounts", description="List the Roblox accounts you've linked")
    async def roblox_accounts(interaction: discord.Interaction):
        accts, err = await my_person(interaction)
        if err:
            return await say(interaction, err)
        if not accts:
            return await say(interaction, "You haven't linked a Roblox account yet. Use `/roblox link`.")
        lines = [f"{'⭐ ' if x['auto_verify'] else ''}{'🙈 ' if x.get('hidden') else ''}[{x['name']}]({x['profile']}) · `{x['id']}`" for x in accts]
        await say(interaction, f"**Your Roblox accounts ({len(accts)}/100)**\n" + "\n".join(lines[:40]) + ("\n…" if len(lines) > 40 else "")
                  + "\n⭐ = auto-verify · 🙈 = hidden from lookups and the API. Add more with `/roblox link`.")

    @roblox.command(name="auto", description="Verify you automatically as one account in servers that use Roblox verification")
    @app_commands.describe(account="A linked account's name or id, or off")
    async def roblox_auto(interaction: discord.Interaction, account: str = "off"):
        uid = str(interaction.user.id)
        if account.lower() in ("off", "none", ""):
            await api("POST", f"/internal/person/{PLATFORM}/{uid}/auto", {"roblox_id": None})
            return await say(interaction, "Auto-verify is off.")
        accts, err = await my_person(interaction)
        x = pick(accts or [], account)
        if err or not x:
            return await say(interaction, err or "That isn't one of your linked accounts. See `/roblox accounts`.")
        await api("POST", f"/internal/person/{PLATFORM}/{uid}/auto", {"roblox_id": x["id"]})
        await say(interaction, f"**{x['name']}** will now verify you automatically in servers that use Roblox verification.")

    @roblox.command(name="remove", description="Unlink one of your Roblox accounts")
    @app_commands.describe(account="A linked account's name or id")
    async def roblox_remove(interaction: discord.Interaction, account: str):
        accts, err = await my_person(interaction)
        x = pick(accts or [], account)
        if err or not x:
            return await say(interaction, err or "That isn't one of your linked accounts. See `/roblox accounts`.")
        await api("DELETE", f"/internal/person/{PLATFORM}/{interaction.user.id}/roblox/{x['id']}")
        await say(interaction, f"Removed **{x['name']}**.")

    async def set_hidden(interaction, accounts, hide):
        names = split_names(accounts)
        verb = "hide" if hide else "show"
        if not names:
            return await say(interaction, f"Say which accounts to {verb}: one name, several names, or `all`. See `/roblox accounts`.")
        accts, err = await my_person(interaction)
        if err:
            return await say(interaction, err)
        body = {"hidden": hide}
        if any(n.lower() == "all" for n in names):
            body["all"] = True
            shown = "all your accounts"
        else:
            chosen = [pick(accts, n) for n in names]
            missing = [n for n, x in zip(names, chosen) if not x]
            if missing:
                return await say(interaction, f"Not one of your linked accounts: {', '.join(missing)}. See `/roblox accounts`.")
            body["roblox_ids"] = [x["id"] for x in chosen]
            shown = ", ".join(f"**{x['name']}**" for x in chosen)
        d = await api("POST", f"/internal/person/{PLATFORM}/{interaction.user.id}/hide", body)
        if not d or not d.get("success"):
            return await say(interaction, "Couldn't change that right now. Try again in a moment.")
        await say(interaction, f"Hidden: {shown}. They no longer show in lookups or the API, but servers still see them when you verify." if hide else f"Visible again: {shown}.")

    @roblox.command(name="hide", description="Hide accounts from lookups and the API (one, several, or all)")
    @app_commands.describe(accounts="Account names or ids, several separated by spaces, or all")
    async def roblox_hide(interaction: discord.Interaction, accounts: str):
        await set_hidden(interaction, accounts, True)

    @roblox.command(name="show", description="Show hidden accounts again (one, several, or all)")
    @app_commands.describe(accounts="Account names or ids, several separated by spaces, or all")
    async def roblox_show(interaction: discord.Interaction, accounts: str):
        await set_hidden(interaction, accounts, False)

    @roblox.command(name="group", description="Info about a Roblox group (ID, link or name)")
    @app_commands.describe(group="The group's ID, link or name")
    async def roblox_group_info(interaction: discord.Interaction, group: str):
        await interaction.response.defer(thinking=True)
        info, err = await fetch_group(group)
        if err:
            return await interaction.followup.send(err)
        await interaction.followup.send(embed=group_embed(info, color_of(interaction.user.id)))

    @roblox.command(name="sync", description="Sync every member's Roblox roles and nickname with the group (server admins)")
    async def roblox_sync(interaction: discord.Interaction):
        if not is_manager(interaction):
            return await say(interaction, "Only server admins can run a sync.")
        if not await cfg_of(str(interaction.guild_id)):
            return await say(interaction, "Set the group first: `/roblox panel setgroup`.")
        await say(interaction, "Syncing every member now. This can take a few minutes; I'll send you the result, including anything I'm not allowed to do "
                               "(missing permissions or a role that sits too high).")
        guild, user = interaction.guild, interaction.user

        async def job():
            text = summary_text(await sync_guild(guild))
            try:
                await user.send(text[:1900])
            except discord.HTTPException:
                pass
        task = asyncio.create_task(job())
        _tasks.add(task)                                 # keep a reference so it isn't collected mid-sync
        task.add_done_callback(_tasks.discard)

    # ---- /roblox panel ... (server admins)
    async def stats_line(gid):
        d = await api("GET", f"/internal/guild/{PLATFORM}/{gid}/stats")
        if not d or not d.get("success"):
            return None
        return f"Verified here: **{d['verified']}** now · **{d['ever_verified']}** ever · last 24h {d['last_24h']} · 7 days {d['last_7d']} · 30 days {d['last_30d']}"

    async def panel_apply(interaction, body=None):
        """Save a change (or just read the config) and answer with the server's current setup."""
        if not is_manager(interaction):
            return await say(interaction, "Only server admins can set up verification.")
        guild, gid = interaction.guild, str(interaction.guild_id)
        d = await api("PUT" if body else "GET", f"/internal/guild/{PLATFORM}/{gid}/config", body or None)
        c = (d or {}).get("config") or {}
        if (body or {}).get("verified_role_id") == "create":
            await ensure_verified_role(guild, c, Issues())
        lines = [f"Group: `{c.get('group_id') or 'not set'}`", f"Nickname: `{c.get('nickname_template') or 'off'}` (use {{name}}, {{display}}, {{id}}, {{rank}})",
                 f"Verified role: {'<@&' + str(c['verified_role_id']) + '>' if c.get('verified_role_id') and c.get('verified_role_id') != 'create' else 'none'} · "
                 f"Unverified role: {'<@&' + str(c['unverified_role_id']) + '>' if c.get('unverified_role_id') else 'none'}",
                 f"Mirror group roles: {'on' if c.get('mirror_roles') else 'off'} · DM prompt on join: {'on' if c.get('prompt_dm') else 'off'}",
                 f"Panel: {'posted in <#' + str(c['panel_channel_id']) + '>' if c.get('panel_message_id') else 'not posted. Use `/roblox panel send`'}"]
        last = (stateDB.get(gid) or {}).get("last")
        if last and last.get("problems"):
            lines.append("**The last sync hit problems:**\n" + "\n".join(f"• {l}" for l in last["problems"]))
        line = await stats_line(gid) if c.get("group_id") else None
        if line:
            lines.append(line)
        lines.append("Run `/roblox sync` to apply it to everyone. A backup sync runs every 24 hours.")
        await say(interaction, "\n".join(lines))

    ONOFF = [app_commands.Choice(name="on", value="on"), app_commands.Choice(name="off", value="off")]

    @panel.command(name="status", description="Show this server's Roblox verification setup")
    async def panel_status(interaction: discord.Interaction):
        await panel_apply(interaction)

    @panel.command(name="setgroup", description="Set the Roblox group this server mirrors")
    @app_commands.describe(group_id="The number in the group's URL")
    async def panel_setgroup(interaction: discord.Interaction, group_id: str):
        if not group_id.strip().isdigit():
            return await say(interaction, "Give the Roblox group ID (the number in the group's URL).")
        await panel_apply(interaction, {"group_id": group_id.strip()})

    @panel.command(name="nickname", description="Set the nickname template, or off")
    @app_commands.describe(template="Use {name}, {display}, {id}, {rank}; or off")
    async def panel_nickname(interaction: discord.Interaction, template: str):
        v = template.strip()
        await panel_apply(interaction, {"nickname_template": "" if v.lower() in ("off", "none", "") else "{name}" if v.lower() in ("on", "true", "yes") else v})

    @panel.command(name="role", description="Set the verified role (pick one, or leave empty to create it)")
    async def panel_role(interaction: discord.Interaction, role: discord.Role = None):
        await panel_apply(interaction, {"verified_role_id": "create" if role is None else str(role.id)})

    @panel.command(name="unverified", description="Set the role people get until they verify (pick one, or leave empty to create it)")
    async def panel_unverified(interaction: discord.Interaction, role: discord.Role = None):
        await panel_apply(interaction, {"unverified_role_id": "create" if role is None else str(role.id)})

    @panel.command(name="mirror", description="Make Discord roles follow the group's roles (create, rename, delete)")
    @app_commands.choices(state=ONOFF)
    async def panel_mirror(interaction: discord.Interaction, state: app_commands.Choice[str]):
        await panel_apply(interaction, {"mirror_roles": state.value == "on"})

    @panel.command(name="prompt", description="DM new members a Roblox verification prompt when they join")
    @app_commands.choices(state=ONOFF)
    @app_commands.describe(text="Optional message to put in the DM")
    async def panel_prompt(interaction: discord.Interaction, state: app_commands.Choice[str], text: str = ""):
        body = {"prompt_dm": state.value == "on"}
        if text.strip():
            body["prompt_text"] = text.strip()
        await panel_apply(interaction, body)

    @panel.command(name="title", description="Set the panel's title (empty = default)")
    async def panel_title(interaction: discord.Interaction, title: str = ""):
        await panel_apply(interaction, {"panel_title": "" if title.lower() in ("off", "default") else title.strip()})

    @panel.command(name="text", description="Set the panel's text (empty = default)")
    async def panel_text(interaction: discord.Interaction, text: str = ""):
        await panel_apply(interaction, {"panel_text": "" if text.lower() in ("off", "default") else text.strip()})

    @panel.command(name="stats", description="How many people verified here")
    async def panel_stats(interaction: discord.Interaction):
        if not is_manager(interaction):
            return await say(interaction, "Only server admins can see verification stats.")
        await say(interaction, (await stats_line(str(interaction.guild_id))) or "Stats aren't available right now.")

    @panel.command(name="send", description="Post the verification panel (a message with a Verify button)")
    @app_commands.describe(channel="Where to post it (default: here)")
    async def panel_send(interaction: discord.Interaction, channel: discord.TextChannel = None):
        if not is_manager(interaction):
            return await say(interaction, "Only server admins can set up verification.")
        cfg = await cfg_of(str(interaction.guild_id))
        if not cfg:
            return await say(interaction, "Set the group first: `/roblox panel setgroup`.")
        target = channel or interaction.channel
        try:
            await send_panel(interaction.guild, target, cfg)
        except discord.HTTPException as e:
            why = ("I'm missing **View Channel**, **Send Messages** or **Embed Links** there. Give my role those permissions in that channel."
                   if Issues.denied(e) else str(e))
            return await say(interaction, f"I couldn't post the panel in {target.mention}: {why}")
        await say(interaction, f"The verification panel is posted in {target.mention}. People press **Verify** to get their roles.")

    m.rverify_sync = sync_guild
    _ROBLOX["notice"] = verified_notice
    return roblox


# --- Roblox group info (used by /roblox group) ---
GROUP_UA = {"user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
            "accept": "application/json"}


GROUP_URL = re.compile(r"roblox\.com/(?:[a-z-]+/)?(?:groups|communities)/(\d+)", re.I)


async def _group_json(session, url):
    try:
        async with session.get(url, headers=GROUP_UA, timeout=aiohttp.ClientTimeout(total=12)) as r:
            data = await r.json(content_type=None)
            return (data if r.status < 400 else None), r.status
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return None, 0


async def find_group_id(session, query):
    """(group id, error text). Accepts an ID, a group link, or a name (the best match wins, an exact name first)."""
    q = (query or "").strip()
    if q.isdigit():
        return q, None
    found = GROUP_URL.search(q)
    if found:
        return found.group(1), None
    if not q:
        return None, "Give a group ID, a roblox.com group link, or a group name."
    data, status = await _group_json(session, f"https://groups.roblox.com/v1/groups/search?keyword={urllib.parse.quote(q, safe='')}&limit=10")
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
        base, status = await _group_json(session, f"https://groups.roblox.com/v1/groups/{gid}")
        if not base or not base.get("id"):
            return None, "Roblox couldn't be reached right now." if status in (0, 429) or status >= 500 else f"I couldn't find a group with the ID `{gid}`."
        created, icon, roles = await asyncio.gather(
            _group_json(session, f"https://groups.roblox.com/v2/groups?groupIds={gid}"),
            _group_json(session, f"https://thumbnails.roblox.com/v1/groups/icons?groupIds={gid}&size=420x420&format=Png"),
            _group_json(session, f"https://groups.roblox.com/v1/groups/{gid}/roles"))
        v2 = next(iter((created[0] or {}).get("data") or []), {}) or {}
        base["created"] = v2.get("created")
        base["icon"] = next(iter((icon[0] or {}).get("data") or []), {}).get("imageUrl")
        base["role_count"] = len((roles[0] or {}).get("roles") or []) or None
        return base, None


def _group_date(iso):
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except ValueError:
        return None


def group_embed(info, color):
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
    created = _group_date(info.get("created"))
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
        when = _group_date(shout.get("updated"))
        embed.add_field(name="Shout", value=shout["body"][:500] + (f"\n— {who}" if who else "") + (f", {when}" if when else ""), inline=False)
    if info.get("icon"):
        embed.set_thumbnail(url=info["icon"])
    embed.set_footer(text="Roblox group")
    return embed


class RobloxCog(commands.Cog, name="Roblox"):
    """Roblox verification, linked accounts and group info: the /roblox group."""

    def __init__(self, bot):
        self.bot = bot
        self.group = roblox_init(m)

    async def cog_load(self):
        self.bot.tree.add_command(self.group)

    async def cog_unload(self):
        self.bot.tree.remove_command(self.group.name)

async def setup(bot):
    await bot.add_cog(RobloxCog(bot))

"""Roblox verification for Discord servers (OAuth2 through the Shapes API, never bio text).

Same system as the Yapin' bot, on the same API data: people link Roblox accounts once (up to 100) and every server that
uses verification sees them. A server admin points the bot at a Roblox group with /robloxpanel and the bot mirrors it:
one Discord role per group role (created, renamed and deleted as the group changes, when mirroring is on), a verified
role and nicknames. Members verify with /robloxverify or the Verify button of a panel the server posted. They are synced
when they verify, on join, on /sync and by a backup sync every 24 hours (measured in elapsed UTC seconds, so daylight saving
and leap years can't move it). Roblox is only asked during a sync, never constantly.

People are identified by their Yapin' account (the API's one person record); a Discord account is attached to it from the
Yapin' dashboard, so Discord ids never create people on their own.
"""
import asyncio
import hmac
import json
import os
import threading
import time

import discord
import requests
from discord import app_commands
from flask import jsonify, request

PLATFORM = "discord"
SYNC_EVERY = 24 * 60 * 60           # seconds of real elapsed time
CHECK_EVERY = 300                   # how often the scheduler looks at the clock
YAPIN_DASH = os.environ.get("YAPIN_DASH_URL", "https://shapes-yapper.oriaxen.com/dashboard/roblox")
_locks = {}
_prompted = {}
_clicked = {}
_tasks = set()


class JsonState:
    """A tiny dict kept in a JSON file (get/set/save). Plain files, so it doesn't depend on which pickleDB release is installed."""

    def __init__(self, path):
        self.path, self._lock = path, threading.Lock()
        try:
            with open(path, "r", encoding="utf-8") as f:
                self._data = json.load(f)
        except (OSError, ValueError):
            self._data = {}

    def get(self, key, default=None):
        with self._lock:
            return json.loads(json.dumps(self._data.get(str(key), default)))      # a copy, so callers can edit it freely

    def set(self, key, value):
        with self._lock:
            self._data[str(key)] = value

    def save(self):
        with self._lock:
            tmp = f"{self.path}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f)
            os.replace(tmp, self.path)


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
                       "(Server Settings → Roles → my role → Permissions), then run `/sync` again. Without it I can only use roles that already exist.")
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


def init(m):
    """m is the bot's main module (bot, embedDB, APIBaseURL, app...)."""
    bot = m.bot
    stateDB = JsonState("rverify.json")            # guild id -> {"roles": {roblox role id: discord role id}, "made": [...], "last": {...}}
    m.rverifyDB = stateDB

    token = (os.environ.get("SHAPES_API_TOKEN", "").strip() or os.environ.get("Shapes_API_Key", "")).strip()
    headers = {"Authorization": f"Bearer {token}"}
    http = requests.Session()

    def api_sync(method, path, body=None):
        """The Shapes API's JSON, or None when it can't be reached. Blocking: call through api()."""
        if not token:
            return None
        try:
            r = http.request(method, f"{m.APIBaseURL}{path}", json=body, headers=headers, timeout=30)
            data = r.json()
            return data if isinstance(data, dict) else None
        except (requests.RequestException, ValueError):
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
            if nick and member.nick != nick and not (issues.nick_denied >= 5 and not issues.nick_ok):
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
        embed.set_footer(text="Manage your accounts with /robloxverify")
        try:
            user = bot.get_user(int(user_id)) or await bot.fetch_user(int(user_id))
            await user.send(embed=embed)
        except (discord.HTTPException, ValueError) as e:
            print(f"[verify] couldn't DM {user_id}: {e}")

    async def remember(gid, counts, issues):
        """Keep the last sync's result and problems so the Yapin' dashboard (or /robloxpanel status) can show them."""
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
        if d and d.get("success"):
            return d.get("url"), None
        if d and d.get("data"):
            return None, d["data"]
        return None, "I couldn't make a Roblox sign-in link right now."

    def needs_yapin_text():
        return ("Roblox accounts are linked through your Yapin' account. Sign up at Yapin', then open "
                f"{YAPIN_DASH} and press **Link Discord**. After that you can verify here. "
                "If you only want to show your Roblox account on Discord, use `/linkroblox`.")

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
            body = f"{text}\n\nSign in with Roblox here (no bio changes needed): {link}" if link else f"{text}\n\n{needs_yapin_text()}"
            try:
                await member.send(embed=discord.Embed(title="Verify your Roblox account", description=body, color=discord.Color.blurple()))
            except discord.HTTPException as e:
                print(f"[verify] couldn't DM the prompt to {uid}: {e}")
    bot.add_listener(on_member_join, "on_member_join")

    @m.app.route("/internal/verified", methods=["POST"])
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
            "Already linked? You're verified in one click. Pick another account with `/robloxverify`.")
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
                    return await interaction.followup.send(why if res.get("linked") is False and "Yapin'" in (why or "") else needs_yapin_text() if why else "Couldn't verify you.", ephemeral=True)
                return await interaction.followup.send(f"Sign in with Roblox to verify (OAuth2, nothing goes in your bio). The link works for 10 minutes and only for you:\n{link}\n"
                                                       "When you finish I'll verify you here automatically.", ephemeral=True)
            acct = res.get("account") or {}
            who = f"[{acct.get('name')}]({acct.get('profile')}) (`{acct.get('id')}`)" if acct.get("id") else "your Roblox account"
            await interaction.followup.send(f"Verified as {who}. I'm updating your roles and nickname now.", ephemeral=True)
            member = interaction.user if isinstance(interaction.user, discord.Member) else guild.get_member(interaction.user.id)
            if member:
                await apply_one(guild, member)

    bot.add_view(PanelView())

    # ------------------------------------------------------------------ commands
    def is_manager(interaction):
        user = interaction.user
        return bool(interaction.guild) and (user.id == interaction.guild.owner_id or user.guild_permissions.administrator or user.guild_permissions.manage_guild)

    def split_names(value):
        return [t for t in (value or "").replace(",", " ").split() if t]

    VERIFY_ACTIONS = [app_commands.Choice(name=n, value=v) for n, v in (
        ("Verify here as a named account (leave action empty to use your default)", "here"), ("Link a Roblox account", "link"), ("List my accounts", "accounts"),
        ("Auto-verify as an account", "auto"), ("Remove an account", "remove"), ("Hide accounts from lookups and the API", "hide"), ("Show hidden accounts again", "show"))]

    @bot.tree.command(name="robloxverify", description="Verify with Roblox, list or hide your linked accounts")
    @app_commands.describe(action="What to do (empty = verify in this server)", value="Account name(s) or id(s); several with spaces, or all")
    @app_commands.choices(action=VERIFY_ACTIONS)
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def robloxverify(interaction: discord.Interaction, action: app_commands.Choice[str] = None, value: str = ""):
        uid = str(interaction.user.id)
        gid = str(interaction.guild_id or "")
        low = action.value if action else ""
        person = await api("GET", f"/internal/person/{PLATFORM}/{uid}") or {}
        if not person.get("success"):
            return await interaction.response.send_message("Verification isn't available right now. Try again in a moment.", ephemeral=True)
        accts = person.get("accounts") or []
        if low == "link" or (not accts and low in ("", "here")):
            if not person.get("linked"):
                return await interaction.response.send_message(needs_yapin_text(), ephemeral=True)
            link, why = await start_link(uid, gid)
            return await interaction.response.send_message(
                f"Sign in with Roblox to verify (OAuth2, nothing goes in your bio). The link works for 10 minutes and only for you:\n{link}" if link else why, ephemeral=True)
        if not person.get("linked"):
            return await interaction.response.send_message(needs_yapin_text(), ephemeral=True)
        pick = lambda q: next((x for x in accts if str(x["id"]) == q or x["name"].lower() == q.lower()), None)
        if low == "accounts":
            lines = [f"{'⭐ ' if x['auto_verify'] else ''}{'🙈 ' if x.get('hidden') else ''}[{x['name']}]({x['profile']}) · `{x['id']}`" for x in accts]
            return await interaction.response.send_message(
                f"**Your Roblox accounts ({len(accts)}/{person.get('max', 100)})**\n" + "\n".join(lines[:40]) + ("\n…" if len(lines) > 40 else "")
                + "\n⭐ = auto-verify · 🙈 = hidden from lookups and the API. Add more with `/robloxverify link`.", ephemeral=True)
        if low in ("hide", "show"):
            names = split_names(value)
            if not names:
                return await interaction.response.send_message(f"Say which accounts to {low}: one name, several names, or `all`. See `/robloxverify accounts`.", ephemeral=True)
            body = {"hidden": low == "hide"}
            if any(n.lower() == "all" for n in names):
                body["all"] = True
                shown = "all your accounts"
            else:
                chosen = [pick(n) for n in names]
                missing = [n for n, x in zip(names, chosen) if not x]
                if missing:
                    return await interaction.response.send_message(f"Not one of your linked accounts: {', '.join(missing)}. See `/robloxverify accounts`.", ephemeral=True)
                body["roblox_ids"] = [x["id"] for x in chosen]
                shown = ", ".join(f"**{x['name']}**" for x in chosen)
            d = await api("POST", f"/internal/person/{PLATFORM}/{uid}/hide", body)
            if not d or not d.get("success"):
                return await interaction.response.send_message("Couldn't change that right now. Try again in a moment.", ephemeral=True)
            return await interaction.response.send_message(
                f"Hidden: {shown}. They no longer show in lookups, the API or `/robloxinfo`, but servers still see them when you verify." if low == "hide" else f"Visible again: {shown}.", ephemeral=True)
        if low == "auto":
            if value.lower() in ("off", "none", ""):
                await api("POST", f"/internal/person/{PLATFORM}/{uid}/auto", {"roblox_id": None})
                return await interaction.response.send_message("Auto-verify is off.", ephemeral=True)
            x = pick(value)
            if not x:
                return await interaction.response.send_message("That isn't one of your linked accounts. See `/robloxverify accounts`.", ephemeral=True)
            await api("POST", f"/internal/person/{PLATFORM}/{uid}/auto", {"roblox_id": x["id"]})
            return await interaction.response.send_message(f"**{x['name']}** will now verify you automatically in servers that use Roblox verification.", ephemeral=True)
        if low == "remove":
            x = pick(value)
            if not x:
                return await interaction.response.send_message("That isn't one of your linked accounts. See `/robloxverify accounts`.", ephemeral=True)
            await api("DELETE", f"/internal/person/{PLATFORM}/{uid}/roblox/{x['id']}")
            return await interaction.response.send_message(f"Removed **{x['name']}**.", ephemeral=True)
        # verify in this server, as the named account or the default one
        if not interaction.guild:
            return await interaction.response.send_message("Run this in a server to verify there.", ephemeral=True)
        if not await cfg_of(gid):
            return await interaction.response.send_message("This server hasn't set up Roblox verification.", ephemeral=True)
        res = await api("POST", f"/internal/guild/{PLATFORM}/{gid}/verify", {"user_id": uid, "roblox_id": value or None})
        if not res or not res.get("success"):
            return await interaction.response.send_message((res or {}).get("data") or "Couldn't verify you.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        member = interaction.user if isinstance(interaction.user, discord.Member) else interaction.guild.get_member(interaction.user.id)
        if member:
            await apply_one(interaction.guild, member)
        await interaction.followup.send(f"Verified as **{res['account']['name']}**. I've sent you a message with the details.", ephemeral=True)

    async def stats_line(gid):
        d = await api("GET", f"/internal/guild/{PLATFORM}/{gid}/stats")
        if not d or not d.get("success"):
            return None
        return f"Verified here: **{d['verified']}** now · **{d['ever_verified']}** ever · last 24h {d['last_24h']} · 7 days {d['last_7d']} · 30 days {d['last_30d']}"

    PANEL_ACTIONS = [app_commands.Choice(name=n, value=n) for n in (
        "status", "send", "group", "nickname", "role", "unverified", "mirror", "prompt", "title", "text", "stats")]

    @bot.tree.command(name="robloxpanel", description="Set up Roblox verification (server admins)")
    @app_commands.describe(action="What to set up", value="The new value (group id, template, on/off, text...)",
                           channel="Where to post the panel (action: send)", role="The role to use (action: role or unverified)")
    @app_commands.choices(action=PANEL_ACTIONS)
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.allowed_installs(guilds=True, users=False)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
    async def robloxpanel(interaction: discord.Interaction, action: app_commands.Choice[str] = None, value: str = "",
                          channel: discord.TextChannel = None, role: discord.Role = None):
        if not is_manager(interaction):
            return await interaction.response.send_message("Only server admins can set up verification.", ephemeral=True)
        guild, gid = interaction.guild, str(interaction.guild_id)
        a = action.value if action else "status"
        body = {}
        if a == "group":
            if not value.strip().isdigit():
                return await interaction.response.send_message("Give the Roblox group ID (the number in the group's URL).", ephemeral=True)
            body["group_id"] = value.strip()
        elif a == "nickname":
            v = value.strip()
            body["nickname_template"] = "" if v.lower() in ("off", "none", "") else "{name}" if v.lower() in ("on", "true", "yes") else v
        elif a in ("role", "unverified"):
            if role is None and value.lower() != "create":
                return await interaction.response.send_message("Pick a role in the `role` option, or put `create` in `value` to make a new one.", ephemeral=True)
            body["verified_role_id" if a == "role" else "unverified_role_id"] = "create" if role is None else str(role.id)
        elif a == "mirror":
            body["mirror_roles"] = value.lower() in ("on", "true", "yes", "1")
        elif a == "prompt":
            on, _, text = value.partition(" ")
            body["prompt_dm"] = on.lower() in ("on", "true", "yes", "1")
            if text.strip():
                body["prompt_text"] = text.strip()
        elif a in ("title", "text"):
            body["panel_title" if a == "title" else "panel_text"] = "" if value.lower() in ("off", "default", "") else value.strip()
        elif a == "stats":
            return await interaction.response.send_message((await stats_line(gid)) or "Stats aren't available right now.", ephemeral=True)
        elif a == "send":
            cfg = await cfg_of(gid)
            if not cfg:
                return await interaction.response.send_message("Set the group first: `/robloxpanel group <id>`.", ephemeral=True)
            target = channel or interaction.channel
            try:
                await send_panel(guild, target, cfg)
            except discord.HTTPException as e:
                why = ("I'm missing **View Channel**, **Send Messages** or **Embed Links** there. Give my role those permissions in that channel."
                       if Issues.denied(e) else str(e))
                return await interaction.response.send_message(f"I couldn't post the panel in {target.mention}: {why}", ephemeral=True)
            return await interaction.response.send_message(f"The verification panel is posted in {target.mention}. People press **Verify** to get their roles.", ephemeral=True)
        d = await api("PUT" if body else "GET", f"/internal/guild/{PLATFORM}/{gid}/config", body or None)
        c = (d or {}).get("config") or {}
        if body.get("verified_role_id") == "create":
            await ensure_verified_role(guild, c, Issues())
        lines = [f"Group: `{c.get('group_id') or 'not set'}`", f"Nickname: `{c.get('nickname_template') or 'off'}` (use {{name}}, {{display}}, {{id}}, {{rank}})",
                 f"Verified role: {'<@&' + str(c['verified_role_id']) + '>' if c.get('verified_role_id') and c.get('verified_role_id') != 'create' else 'none'} · "
                 f"Unverified role: {'<@&' + str(c['unverified_role_id']) + '>' if c.get('unverified_role_id') else 'none'}",
                 f"Mirror group roles: {'on' if c.get('mirror_roles') else 'off'} · DM prompt on join: {'on' if c.get('prompt_dm') else 'off'}",
                 f"Panel: {'posted in <#' + str(c['panel_channel_id']) + '>' if c.get('panel_message_id') else 'not posted. Use `/robloxpanel send`'}"]
        last = (stateDB.get(gid) or {}).get("last")
        if last and last.get("problems"):
            lines.append("**The last sync hit problems:**\n" + "\n".join(f"• {l}" for l in last["problems"]))
        line = await stats_line(gid) if c.get("group_id") else None
        if line:
            lines.append(line)
        lines.append("Run `/sync` to apply it to everyone. A backup sync runs every 24 hours.")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @bot.tree.command(name="sync", description="Sync every member's Roblox roles and nickname with the group (server admins)")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.allowed_installs(guilds=True, users=False)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
    async def sync(interaction: discord.Interaction):
        if not is_manager(interaction):
            return await interaction.response.send_message("Only server admins can run a sync.", ephemeral=True)
        if not await cfg_of(str(interaction.guild_id)):
            return await interaction.response.send_message("Set the group first: `/robloxpanel group <id>`.", ephemeral=True)
        await interaction.response.send_message("Syncing every member now. This can take a few minutes; I'll send you the result, including anything I'm not allowed to do "
                                                "(missing permissions or a role that sits too high).", ephemeral=True)
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

    m.rverify_sync = sync_guild

"""Roblox verification for Yapin' servers (OAuth2 through the Shapes API, never bio text).

People link Roblox accounts once (up to 50, shared with Discord through the API). A server owner points the bot at a
Roblox group and the bot mirrors it 1:1: one Yapin' role per group role, a verified role, nicknames. Members are synced
when they verify or auto-verify, on /sync, and by a backup sync every 24 hours (measured in elapsed UTC seconds, so
daylight saving and leap years can't move it). Roblox is only asked during a sync, never constantly.
"""
import hmac
import threading
import time
import urllib.parse

from flask import jsonify, request

SYNC_EVERY = 24 * 60 * 60           # seconds of real elapsed time
CHECK_EVERY = 300                   # how often the scheduler looks at the clock
_locks = {}
_prompted = {}


def init(m):
    bot, yshim, discord = m.bot, m.yshim, m.discord
    from tables import Table
    stateDB = Table("rverify")        # server id -> {"roles": {roblox role id: yapin role id}}
    m.rverifyDB = stateDB
    def api(*a, **k):
        return m.shapes_api(*a, **k)

    # ------------------------------------------------------------------ helpers
    def cfg_of(sid):
        d = api("GET", f"/internal/guild/yapin/{sid}/config")
        c = (d or {}).get("config") or {}
        return c if c.get("group_id") else None

    def role_ids_of(member):
        return {str(r.get("id") if isinstance(r, dict) else r) for r in (member.get("roles") or member.get("role_ids") or [])}

    def template(cfg, acct, role_name):
        t = (cfg.get("nickname_template") or "").strip()
        if not t:
            return None
        out = (t.replace("{name}", acct.get("name") or "").replace("{display}", acct.get("display_name") or acct.get("name") or "")
                .replace("{id}", str(acct.get("id"))).replace("{rank}", role_name or ""))
        return out.strip(" |-–—·/:")[:32] if not role_name else out.strip()[:32]

    def group_role(cfg, rid):
        d = api("GET", f"/internal/roblox/group/{cfg['group_id']}/member/{rid}")
        return (d or {}).get("role") if d and d.get("success") else False       # False = Roblox didn't answer

    def mirror_roles(sid, cfg):
        """Make the Yapin' roles match the group's roles: create missing ones, rename changed ones, delete ones the group
        dropped (only roles this bot made). Returns {roblox role id: yapin role id}."""
        st = dict(stateDB.get(sid) or {})
        mapping = dict(st.get("roles") or {})
        d = api("GET", f"/internal/roblox/group/{cfg['group_id']}/roles")
        if not d or not d.get("success"):
            return mapping
        group = {str(r["id"]): r for r in d["roles"] if int(r.get("rank") or 0) > 0}      # rank 0 is "Guest" = not in the group
        have = {str(r["id"]): r for r in bot.roles(sid)}
        by_name = {str(r.get("name", "")).lower(): str(r["id"]) for r in have.values()}
        for rid, r in group.items():
            current = mapping.get(rid)
            if current and current in have:
                if have[current].get("name") != r["name"]:
                    try:
                        bot.edit_role(sid, current, name=r["name"])
                    except Exception as e:
                        print(f"[verify] rename {r['name']}: {e}")
                continue
            if r["name"].lower() in by_name:                     # a role with that name already exists: use it
                mapping[rid] = by_name[r["name"].lower()]
            elif cfg.get("mirror_roles"):
                try:
                    mapping[rid] = str(bot.create_role(sid, r["name"]).get("id"))
                    time.sleep(2.2)                              # 30 role changes a minute
                except Exception as e:
                    print(f"[verify] couldn't create role {r['name']}: {e}")
        for rid in [k for k in mapping if k not in group]:       # the group dropped this role
            if cfg.get("mirror_roles") and mapping[rid] in have:
                try:
                    bot.delete_role(sid, mapping[rid])
                    time.sleep(2.2)
                except Exception as e:
                    print(f"[verify] couldn't delete role {mapping[rid]}: {e}")
            mapping.pop(rid, None)
        st["roles"] = mapping
        stateDB.set(sid, st)
        return mapping

    def ensure_verified_role(sid, cfg):
        if cfg.get("verified_role_id") == "create":
            try:
                rid = str(bot.create_role(sid, "Verified").get("id"))
                api("PUT", f"/internal/guild/yapin/{sid}/config", {"verified_role_id": rid})
                cfg["verified_role_id"] = rid
            except Exception as e:
                print(f"[verify] couldn't create the Verified role: {e}")
                cfg["verified_role_id"] = ""
        return cfg.get("verified_role_id") or None

    def sync_member(sid, uid, acct, cfg, mapping):
        """Bring one member in line: roles from their group rank, the verified role, the nickname. acct=None unverifies."""
        try:
            member = bot.member(sid, uid)
        except Exception:
            return "absent"
        mine = role_ids_of(member)
        want, drop = set(), set(mapping.values())
        vr, ur = cfg.get("verified_role_id") or None, cfg.get("unverified_role_id") or None
        role_name = None
        if acct:
            if vr and vr != "create":
                want.add(vr)
            role = group_role(cfg, acct["id"])
            if role is False:
                return "roblox-unreachable"
            if role and str(role.get("id")) in mapping and int(role.get("rank") or 0) > 0:
                want.add(mapping[str(role["id"])])
                role_name = role.get("name")
        elif ur:
            want.add(ur)
        drop |= {x for x in (vr, ur) if x and x != "create"}
        for r in want - mine:
            bot.add_role(sid, uid, r)
        for r in (drop - want) & mine:
            bot.remove_role(sid, uid, r)
        if acct:
            nick = template(cfg, acct, role_name)
            if nick and (member.get("nickname") or member.get("nick")) != nick:
                try:
                    bot.set_nickname(sid, uid, nick)
                except Exception:
                    pass                                         # e.g. the server owner can't be renamed
        return "ok"

    def dm_verified(uid, acct, how, server_name=None):
        """The message every verification gets: avatar, username, user ID and a profile link."""
        title = "Auto-verified" if how == "auto" else "Verified"
        embed = {"title": f"✅ {title} as {acct['name']}", "color": int(0x2ecc71),
                 "description": f"[{acct['name']}]({acct['profile']})\nUser ID: `{acct['id']}`" + (f"\nServer: **{server_name}**" if server_name else ""),
                 "footer": {"text": "Manage your accounts with /verify · shapes-yapper.oriaxen.com"}}
        if acct.get("avatar"):
            embed["thumbnail"] = {"url": acct["avatar"]}
        try:
            bot.send(bot.dm(uid), "", embeds=[embed])
        except Exception as e:
            print(f"[verify] couldn't DM {uid}: {e}")

    def server_name(sid):
        try:
            return bot.server(sid).get("name")
        except Exception:
            return None

    def apply_one(sid, uid, how_hint=None, notify=True):
        """Verify/auto-verify one member of a configured server, then sync them. Returns the account or None."""
        cfg = cfg_of(sid)
        if not cfg:
            return None
        res = (api("POST", f"/internal/guild/yapin/{sid}/resolve", {"user_ids": [str(uid)]}) or {}).get("members", {}).get(str(uid))
        if not res:
            return None
        ensure_verified_role(sid, cfg)
        sync_member(sid, uid, res, cfg, mirror_roles(sid, cfg))
        if notify and how_hint != "quiet":
            dm_verified(uid, res, res.get("how"), server_name(sid))
        return res

    # ------------------------------------------------------------------ full sync
    def sync_guild(sid, progress=None):
        """Sync every member of a server. One at a time per server. Returns counts."""
        lock = _locks.setdefault(sid, threading.Lock())
        if not lock.acquire(blocking=False):
            return {"busy": True}
        counts = {"synced": 0, "unverified": 0, "skipped": 0, "errors": 0}
        try:
            cfg = cfg_of(sid)
            if not cfg:
                return {"error": "no group set"}
            ensure_verified_role(sid, cfg)
            mapping = mirror_roles(sid, cfg)
            after, ids = None, []
            while True:
                page = bot.members(sid, after=after)
                if not page:
                    break
                for mem in page:
                    uid = str(mem.get("user_id") or (mem.get("user") or {}).get("id") or mem.get("id"))
                    if not (mem.get("bot") or (mem.get("user") or {}).get("bot")):
                        ids.append(uid)
                after = str(page[-1].get("user_id") or (page[-1].get("user") or {}).get("id") or page[-1].get("id"))
                if len(page) < 200:
                    break
            found = {}
            for i in range(0, len(ids), 500):
                found.update((api("POST", f"/internal/guild/yapin/{sid}/resolve", {"user_ids": ids[i:i + 500]}) or {}).get("members", {}))
            for n, uid in enumerate(ids):
                try:
                    r = sync_member(sid, uid, found.get(uid), cfg, mapping)
                    counts["synced" if found.get(uid) and r == "ok" else "unverified" if r == "ok" else "skipped"] += 1
                except Exception as e:
                    counts["errors"] += 1
                    print(f"[verify] sync {uid} in {sid}: {e}")
                time.sleep(0.3)
                if progress and n % 25 == 0:
                    progress(n, len(ids))
            api("POST", f"/internal/guild/yapin/{sid}/synced", {})
            return counts
        finally:
            lock.release()

    def scheduler():
        """Backup sync: a server is due when 24 hours of real time have passed since its last sync (UTC epoch seconds)."""
        while True:
            time.sleep(CHECK_EVERY)
            try:
                for cfg in (api("GET", "/internal/guilds/yapin") or {}).get("guilds", []):
                    last = int(cfg.get("last_sync_at") or 0)
                    if time.time() - last >= SYNC_EVERY:
                        sync_guild(cfg["guild_id"])
            except Exception as e:
                print(f"[verify] scheduler: {e}")

    def start_scheduler():
        threading.Thread(target=scheduler, name="verify-scheduler", daemon=True).start()
    m.start_verify_scheduler = start_scheduler

    # ------------------------------------------------------------------ joins and the API's notice
    def on_join(ev):
        """A member joined: auto-verify them if they chose that, else DM the server's prompt (once a day per server)."""
        sid, uid = str(ev.server_id), str(ev.user_id)
        cfg = cfg_of(sid)
        if not cfg:
            return
        if apply_one(sid, uid):
            return
        if cfg.get("prompt_dm") and time.time() - _prompted.get((sid, uid), 0) > 86400:
            _prompted[(sid, uid)] = time.time()
            link = start_link(uid, f"yapin:{sid}")
            if link:
                text = cfg.get("prompt_text") or f"**{server_name(sid) or 'This server'}** uses Roblox verification."
                try:
                    bot.send(bot.dm(uid), "", embeds=[{"title": "Verify your Roblox account", "color": 0x5865F2,
                             "description": f"{text}\n\nSign in with Roblox here (no bio changes needed): {link}"}])
                except Exception as e:
                    print(f"[verify] couldn't DM the prompt to {uid}: {e}")
    m.verify_on_join = on_join

    def start_link(uid, guild=""):
        d = api("POST", "/internal/roblox/start", {"platform": "yapin", "user_id": str(uid), "guild": guild})
        return (d or {}).get("url")

    @m.app.route("/internal/verified", methods=["POST"])
    def verified_notice():
        if not m._is_shapes_api_call():
            return jsonify({"success": False}), 401
        b = request.get_json(silent=True) or {}
        uid = str(b.get("yapin_id") or "")
        if not uid:
            return jsonify({"success": True, "ignored": True})
        acct = {"id": b.get("roblox_id"), "name": b.get("roblox_name"), "avatar": b.get("avatar"), "profile": b.get("profile")}

        def work():
            dm_verified(uid, acct, "auto" if b.get("auto_verify") else "verified")
            guild = str(b.get("guild") or "")
            if guild.startswith("yapin:"):                  # they came from a server's prompt: verify them there too
                sid = guild.split(":", 1)[1]
                api("POST", f"/internal/guild/yapin/{sid}/verify", {"user_id": uid, "roblox_id": acct["id"]})
            for cfg in (api("GET", "/internal/guilds/yapin") or {}).get("guilds", []):     # keep every server they're in current
                sid = cfg["guild_id"]
                try:
                    bot.member(sid, uid)
                except Exception:
                    continue
                apply_one(sid, uid, how_hint="quiet")
        threading.Thread(target=work, daemon=True).start()
        return jsonify({"success": True})

    # ------------------------------------------------------------------ commands
    def is_manager(i):
        return bool(i.guild_id) and (m.is_bot_owner(str(i.user.id)) or yshim.is_admin(str(i.guild_id), str(i.user.id)))

    async def say(i, text, ephemeral=True):
        await i.response.send_message(text, ephemeral=ephemeral)

    @bot.tree.command(name="verify", description="Verify with Roblox: /verify [link | accounts | auto <name|off> | remove <name> | <account name>]")
    async def verify(interaction: discord.Interaction, action: str = "", value: str = ""):
        import asyncio
        uid, sid = str(interaction.user.id), str(interaction.guild_id or "")
        a = (action or "").strip()
        person = await asyncio.to_thread(api, "GET", f"/internal/person/yapin/{uid}") or {}
        accts = person.get("accounts") or []
        if not person.get("success"):
            return await say(interaction, "Verification isn't available right now. Try again in a moment.")
        if a.lower() == "link" or (not accts and a.lower() not in ("accounts", "auto", "remove")):
            link = await asyncio.to_thread(start_link, uid, f"yapin:{sid}" if sid else "")
            if not link:
                return await say(interaction, "I couldn't make a Roblox sign-in link right now.")
            return await say(interaction, f"Sign in with Roblox to verify (OAuth2, nothing goes in your bio). The link works for 10 minutes and only for you:\n{link}")
        if a.lower() == "accounts":
            lines = [f"{'⭐ ' if x['auto_verify'] else ''}[{x['name']}]({x['profile']}) · `{x['id']}`" for x in accts]
            return await say(interaction, f"**Your Roblox accounts ({len(accts)}/{person.get('max', 50)})**\n" + "\n".join(lines[:30]) + "\n⭐ = auto-verify. `/verify link` adds another.")
        pick = lambda q: next((x for x in accts if str(x["id"]) == q or x["name"].lower() == q.lower()), None)
        if a.lower() == "auto":
            if value.lower() in ("off", "none", ""):
                await asyncio.to_thread(api, "POST", f"/internal/person/yapin/{uid}/auto", {"roblox_id": None})
                return await say(interaction, "Auto-verify is off.")
            x = pick(value)
            if not x:
                return await say(interaction, "That isn't one of your linked accounts. See `/verify accounts`.")
            await asyncio.to_thread(api, "POST", f"/internal/person/yapin/{uid}/auto", {"roblox_id": x["id"]})
            return await say(interaction, f"**{x['name']}** will now verify you automatically in servers that use Roblox verification.")
        if a.lower() == "remove":
            x = pick(value)
            if not x:
                return await say(interaction, "That isn't one of your linked accounts. See `/verify accounts`.")
            await asyncio.to_thread(api, "DELETE", f"/internal/person/yapin/{uid}/roblox/{x['id']}")
            return await say(interaction, f"Removed **{x['name']}**.")
        # verify in this server, as the named account or the default one
        if not sid:
            return await say(interaction, "Run this in a server to verify there.")
        if not await asyncio.to_thread(cfg_of, sid):
            return await say(interaction, "This server hasn't set up Roblox verification.")
        chosen = a if a and a.lower() not in ("here", "me") else value
        res = await asyncio.to_thread(api, "POST", f"/internal/guild/yapin/{sid}/verify", {"user_id": uid, "roblox_id": chosen or None})
        if not res or not res.get("success"):
            return await say(interaction, (res or {}).get("data") or "Couldn't verify you.")
        await interaction.response.defer(ephemeral=True)
        await asyncio.to_thread(apply_one, sid, uid)
        await interaction.followup.send(f"Verified as **{res['account']['name']}**. I've sent you a message with the details.", ephemeral=True)

    @bot.tree.command(name="verifysetup", description="Set up Roblox verification (server admins): group <id> | nickname <template|off> | role <name|create> | unverified <name> | mirror on|off | prompt on|off [text] | status")
    async def verifysetup(interaction: discord.Interaction, action: str = "status", value: str = ""):
        import asyncio
        if not is_manager(interaction):
            return await say(interaction, "Only server admins can set up verification.")
        sid, a = str(interaction.guild_id), (action or "status").lower()
        body = {}
        if a == "group":
            if not value.strip().isdigit():
                return await say(interaction, "Give the Roblox group ID (the number in the group's URL).")
            body["group_id"] = value.strip()
        elif a == "nickname":
            body["nickname_template"] = "" if value.lower() in ("off", "none", "") else value
        elif a in ("role", "unverified"):
            if value.lower() == "create":
                rid = "create"
            else:
                r = next((r for r in bot.roles(sid) if str(r.get("id")) == value.strip() or str(r.get("name", "")).lower() == value.strip().lower().lstrip("@")), None)
                if not r:
                    return await say(interaction, "I couldn't find that role. Use its name, its ID, or `create`.")
                rid = str(r["id"])
            body["verified_role_id" if a == "role" else "unverified_role_id"] = rid
        elif a == "mirror":
            body["mirror_roles"] = value.lower() in ("on", "true", "yes", "1")
        elif a == "prompt":
            on, _, text = value.partition(" ")
            body["prompt_dm"] = on.lower() in ("on", "true", "yes", "1")
            if text.strip():
                body["prompt_text"] = text.strip()
        elif a != "status":
            return await say(interaction, "Options: group, nickname, role, unverified, mirror, prompt, status.")
        d = await asyncio.to_thread(api, "PUT" if body else "GET", f"/internal/guild/yapin/{sid}/config", body or None)
        c = (d or {}).get("config") or {}
        if body.get("verified_role_id") == "create":
            await asyncio.to_thread(ensure_verified_role, sid, c)
        lines = [f"Group: `{c.get('group_id') or 'not set'}`", f"Nickname: `{c.get('nickname_template') or 'off'}` (use {{name}}, {{display}}, {{id}}, {{rank}})",
                 f"Verified role: `{c.get('verified_role_id') or 'none'}` · Unverified role: `{c.get('unverified_role_id') or 'none'}`",
                 f"Mirror group roles: {'on' if c.get('mirror_roles') else 'off'} · DM prompt on join: {'on' if c.get('prompt_dm') else 'off'}",
                 "Run `/sync` to apply it to everyone. A backup sync runs every 24 hours."]
        await say(interaction, "\n".join(lines))

    @bot.tree.command(name="sync", description="Sync every member's Roblox roles and nickname with the group (server admins)")
    async def sync(interaction: discord.Interaction):
        import asyncio
        if not is_manager(interaction):
            return await say(interaction, "Only server admins can run a sync.")
        sid = str(interaction.guild_id)
        if not await asyncio.to_thread(cfg_of, sid):
            return await say(interaction, "Set the group first: `/verifysetup group <id>`.")
        await say(interaction, "Syncing every member now. This can take a few minutes; I'll send you the result.")

        def job():
            counts = sync_guild(sid)
            text = "A sync is already running here." if counts.get("busy") else (
                f"Sync finished: {counts['synced']} verified members updated, {counts['unverified']} not verified, "
                f"{counts['skipped']} skipped, {counts['errors']} errors." if "synced" in counts else f"Sync failed: {counts.get('error')}")
            try:
                bot.send(bot.dm(str(interaction.user.id)), text)
            except Exception:
                pass
        threading.Thread(target=job, daemon=True).start()

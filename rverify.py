"""Roblox verification for Yapin' servers (OAuth2 through the Shapes API, never bio text).

People link Roblox accounts once (up to 100, shared with Discord through the API). A server owner points the bot at a
Roblox group with /robloxpanel and the bot mirrors it 1:1: one Yapin' role per group role (created, renamed and deleted as
the group changes, when mirroring is on), a verified role, nicknames. Members verify with /robloxverify or the Verify button
of a panel the server posted. They are synced when they verify or auto-verify, on /sync, and by a backup sync every 24
hours (measured in elapsed UTC seconds, so daylight saving and leap years can't move it). Roblox is only asked during a
sync, never constantly.
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
    bot, discord = m.bot, m.discord
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

    def mirror_roles(sid, cfg, report=None):
        """Make the Yapin' roles match the group's roles when mirroring is on: create missing ones, rename changed ones, delete
        ones the group dropped (only roles this bot made, never one that was already there). Returns {roblox role id: yapin
        role id}; `report` (a dict) gets counts of what changed."""
        report = report if report is not None else {}
        for k in ("created", "renamed", "deleted"):
            report.setdefault(k, 0)
        st = dict(stateDB.get(sid) or {})
        mapping = dict(st.get("roles") or {})
        made = set(st.get("made") or [])
        d = api("GET", f"/internal/roblox/group/{cfg['group_id']}/roles")
        if not d or not d.get("success"):
            return mapping
        group = {str(r["id"]): r for r in d["roles"] if int(r.get("rank") or 0) > 0}      # rank 0 is "Guest" = not in the group
        have = {str(r["id"]): r for r in bot.roles(sid)}
        by_name = {str(r.get("name", "")).lower(): str(r["id"]) for r in have.values()}
        mirror = bool(cfg.get("mirror_roles"))
        for rid, r in group.items():
            current = mapping.get(rid)
            if current and current in have:
                if mirror and have[current].get("name") != r["name"]:       # renamed in the group
                    try:
                        bot.edit_role(sid, current, name=r["name"])
                        report["renamed"] += 1
                        time.sleep(2.2)
                    except Exception as e:
                        print(f"[verify] rename {r['name']}: {e}")
                continue
            if r["name"].lower() in by_name:                     # a role with that name already exists: use it
                mapping[rid] = by_name[r["name"].lower()]
            elif mirror:
                try:
                    new = str(bot.create_role(sid, r["name"]).get("id"))
                    mapping[rid] = new
                    made.add(new)
                    report["created"] += 1
                    time.sleep(2.2)                              # 30 role changes a minute
                except Exception as e:
                    print(f"[verify] couldn't create role {r['name']}: {e}")
        for rid in [k for k in mapping if k not in group]:       # the group dropped this role
            yid = mapping[rid]
            if mirror and yid in made and yid in have:
                try:
                    bot.delete_role(sid, yid)
                    report["deleted"] += 1
                    time.sleep(2.2)
                except Exception as e:
                    print(f"[verify] couldn't delete role {yid}: {e}")
                    continue                                     # keep it in the map so the next sync tries again
            made.discard(yid)
            mapping.pop(rid, None)
        st["roles"], st["made"], st["at"] = mapping, sorted(made & {str(x) for x in mapping.values()}), time.time()
        stateDB.set(sid, st)
        return mapping

    def mapping_for(sid, cfg):
        """The saved role map; a fresh mirror only when it is older than ten minutes (so a busy panel doesn't hammer Roblox)."""
        st = stateDB.get(sid) or {}
        if st.get("roles") and time.time() - float(st.get("at") or 0) < 600:
            return dict(st["roles"])
        return mirror_roles(sid, cfg)

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
                 "footer": {"text": "Manage your accounts with /robloxverify · shapes-yapper.oriaxen.com"}}
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
        sync_member(sid, uid, res, cfg, mapping_for(sid, cfg))
        if notify and how_hint != "quiet":
            dm_verified(uid, res, res.get("how"), server_name(sid))
        return res

    # ------------------------------------------------------------------ full sync
    def sync_guild(sid, progress=None):
        """Sync every member of a server. One at a time per server. Returns counts."""
        lock = _locks.setdefault(sid, threading.Lock())
        if not lock.acquire(blocking=False):
            return {"busy": True}
        counts = {"synced": 0, "unverified": 0, "skipped": 0, "errors": 0, "created": 0, "renamed": 0, "deleted": 0}
        try:
            cfg = cfg_of(sid)
            if not cfg:
                return {"error": "no group set"}
            ensure_verified_role(sid, cfg)
            mapping = mirror_roles(sid, cfg, counts)
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

    # ------------------------------------------------------------------ the panel (a message with a Verify button)
    PANEL_ID = "rv:verify"
    _clicked = {}

    def panel_embed(cfg, sid):
        title = (cfg.get("panel_title") or "").strip() or "Verify with Roblox"
        text = (cfg.get("panel_text") or "").strip() or (
            f"Press **Verify** to link your Roblox account to {server_name(sid) or 'this server'}. You sign in with Roblox "
            "(nothing goes in your bio) and I'll give you your roles and nickname.\n"
            "Already linked? You're verified in one click. Pick another account with `/robloxverify <name>`.")
        return {"title": title[:256], "description": text[:3000], "color": 0x5865F2}

    def panel_components():
        yapper = m.yapper
        return [yapper.row(yapper.btn("Verify", PANEL_ID, "primary"))]

    def send_panel(sid, channel_id, cfg):
        """Post the panel (or update the one posted before when it is in the same channel). Returns the message id."""
        embed, comps = panel_embed(cfg, sid), panel_components()
        old_ch, old_msg = cfg.get("panel_channel_id"), cfg.get("panel_message_id")
        if old_msg and str(old_ch) == str(channel_id):
            try:
                bot.edit(channel_id, old_msg, content="", embeds=[embed], components=comps)
                return str(old_msg)
            except Exception:
                pass                                              # deleted in the meantime: post a new one
        sent = bot.send(channel_id, "", embeds=[embed], components=comps)
        mid = str((sent or {}).get("id") or getattr(sent, "id", "") or "")
        api("PUT", f"/internal/guild/yapin/{sid}/config", {"panel_channel_id": str(channel_id), "panel_message_id": mid})
        return mid
    m.rverify_send_panel = send_panel

    def on_panel_click(click):
        uid = str(click.get("user_id") or "")
        sid = str(click.get("server_id") or click.get("guild_id") or "")

        def reply(text):
            try:
                click.ephemeral(text)
            except Exception as e:
                print(f"[verify] couldn't answer a panel click: {e}")
        if not uid or not sid:
            return reply("Press the button in a server.")
        if time.time() - _clicked.get(uid, 0) < 5:
            return reply("One moment, I'm still working on your last click.")
        _clicked[uid] = time.time()
        if len(_clicked) > 2000:
            _clicked.clear()
        cfg = cfg_of(sid)
        if not cfg:
            return reply("This server hasn't finished setting up Roblox verification yet.")
        res = api("POST", f"/internal/guild/yapin/{sid}/verify", {"user_id": uid, "roblox_id": None})
        if not res or not res.get("success"):
            if res is None:
                return reply("Verification isn't available right now. Try again in a moment.")
            link = start_link(uid, f"yapin:{sid}")
            if not link:
                return reply("I couldn't make a Roblox sign-in link right now.")
            return reply(f"Sign in with Roblox to verify (OAuth2, nothing goes in your bio). The link works for 10 minutes and only for you:\n{link}\n"
                         "When you finish I'll verify you here automatically.")
        acct = res["account"]
        reply(f"Verified as [{acct['name']}]({acct['profile']}) (`{acct['id']}`). I'm updating your roles and nickname now.")
        threading.Thread(target=apply_one, args=(sid, uid), daemon=True).start()
    bot.button("rv:")(on_panel_click)

    # ------------------------------------------------------------------ commands
    def is_manager(i):
        return bool(i.guild_id) and (m.is_bot_owner(str(i.user.id)) or m.is_admin(str(i.guild_id), str(i.user.id)))

    async def say(i, text, ephemeral=True):
        await i.response.send_message(text, ephemeral=ephemeral)

    def split_names(value):
        return [t for t in (value or "").replace(",", " ").split() if t]

    @bot.tree.command(name="robloxverify", description="Verify with Roblox: link | accounts | auto <name|off> | remove <name> | hide/show <names|all> | <name>")
    async def robloxverify(interaction: discord.Interaction, action: str = "", value: str = ""):
        import asyncio
        uid, sid = str(interaction.user.id), str(interaction.guild_id or "")
        a = (action or "").strip()
        low = a.lower()
        person = await asyncio.to_thread(api, "GET", f"/internal/person/yapin/{uid}") or {}
        accts = person.get("accounts") or []
        if not person.get("success"):
            return await say(interaction, "Verification isn't available right now. Try again in a moment.")
        if low == "link" or (not accts and low not in ("accounts", "auto", "remove", "hide", "show")):
            link = await asyncio.to_thread(start_link, uid, f"yapin:{sid}" if sid else "")
            if not link:
                return await say(interaction, "I couldn't make a Roblox sign-in link right now.")
            return await say(interaction, f"Sign in with Roblox to verify (OAuth2, nothing goes in your bio). The link works for 10 minutes and only for you:\n{link}")
        if low == "accounts":
            lines = [f"{'⭐ ' if x['auto_verify'] else ''}{'🙈 ' if x.get('hidden') else ''}[{x['name']}]({x['profile']}) · `{x['id']}`" for x in accts]
            return await say(interaction, f"**Your Roblox accounts ({len(accts)}/{person.get('max', 100)})**\n" + "\n".join(lines[:40])
                             + ("\n…" if len(lines) > 40 else "") + "\n⭐ = auto-verify · 🙈 = hidden from lookups and the API. `/robloxverify link` adds another.")
        pick = lambda q: next((x for x in accts if str(x["id"]) == q or x["name"].lower() == q.lower()), None)
        if low in ("hide", "show"):
            names = split_names(value)
            if not names:
                return await say(interaction, f"Say which accounts to {low}: one name, several names, or `all`. See `/robloxverify accounts`.")
            body = {"hidden": low == "hide"}
            if any(n.lower() == "all" for n in names):
                body["all"] = True
                shown = "all your accounts"
            else:
                chosen = [pick(n) for n in names]
                missing = [n for n, x in zip(names, chosen) if not x]
                if missing:
                    return await say(interaction, f"Not one of your linked accounts: {', '.join(missing)}. See `/robloxverify accounts`.")
                body["roblox_ids"] = [x["id"] for x in chosen]
                shown = ", ".join(f"**{x['name']}**" for x in chosen)
            d = await asyncio.to_thread(api, "POST", f"/internal/person/yapin/{uid}/hide", body)
            if not d or not d.get("success"):
                return await say(interaction, "Couldn't change that right now. Try again in a moment.")
            return await say(interaction, (f"Hidden: {shown}. They no longer show in lookups, the API or `/robloxinfo`, but servers still see them when you verify."
                                           if low == "hide" else f"Visible again: {shown}."))
        if low == "auto":
            if value.lower() in ("off", "none", ""):
                await asyncio.to_thread(api, "POST", f"/internal/person/yapin/{uid}/auto", {"roblox_id": None})
                return await say(interaction, "Auto-verify is off.")
            x = pick(value)
            if not x:
                return await say(interaction, "That isn't one of your linked accounts. See `/robloxverify accounts`.")
            await asyncio.to_thread(api, "POST", f"/internal/person/yapin/{uid}/auto", {"roblox_id": x["id"]})
            return await say(interaction, f"**{x['name']}** will now verify you automatically in servers that use Roblox verification.")
        if low == "remove":
            x = pick(value)
            if not x:
                return await say(interaction, "That isn't one of your linked accounts. See `/robloxverify accounts`.")
            await asyncio.to_thread(api, "DELETE", f"/internal/person/yapin/{uid}/roblox/{x['id']}")
            return await say(interaction, f"Removed **{x['name']}**.")
        # verify in this server, as the named account or the default one
        if not sid:
            return await say(interaction, "Run this in a server to verify there.")
        if not await asyncio.to_thread(cfg_of, sid):
            return await say(interaction, "This server hasn't set up Roblox verification.")
        chosen = a if a and low not in ("here", "me") else value
        res = await asyncio.to_thread(api, "POST", f"/internal/guild/yapin/{sid}/verify", {"user_id": uid, "roblox_id": chosen or None})
        if not res or not res.get("success"):
            return await say(interaction, (res or {}).get("data") or "Couldn't verify you.")
        await interaction.response.defer(ephemeral=True)
        await asyncio.to_thread(apply_one, sid, uid)
        await interaction.followup.send(f"Verified as **{res['account']['name']}**. I've sent you a message with the details.", ephemeral=True)

    def stats_line(sid):
        d = api("GET", f"/internal/guild/yapin/{sid}/stats")
        if not d or not d.get("success"):
            return None
        return (f"Verified here: **{d['verified']}** now · **{d['ever_verified']}** ever · last 24h {d['last_24h']} · 7 days {d['last_7d']} · 30 days {d['last_30d']}")

    @bot.tree.command(name="robloxpanel", description="Set up Roblox verification (admins): status | send [#channel] | group <id> | nickname | role | unverified | mirror | prompt | title | text | stats")
    async def robloxpanel(interaction: discord.Interaction, action: str = "status", value: str = ""):
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
        elif a in ("title", "text"):
            body["panel_title" if a == "title" else "panel_text"] = "" if value.lower() in ("off", "default", "") else value.strip()
        elif a == "stats":
            line = await asyncio.to_thread(stats_line, sid)
            return await say(interaction, line or "Stats aren't available right now.")
        elif a == "send":
            cfg = await asyncio.to_thread(cfg_of, sid)
            if not cfg:
                return await say(interaction, "Set the group first: `/robloxpanel group <id>`.")
            ch = await asyncio.to_thread(m.resolve_channel, sid, value) if value.strip() else {"id": interaction.channel_id}
            if not ch:
                return await say(interaction, "I couldn't find that channel.")
            try:
                await asyncio.to_thread(send_panel, sid, ch["id"], cfg)
            except Exception as e:
                return await say(interaction, f"I couldn't post the panel there: {e}")
            return await say(interaction, f"The verification panel is posted in <#{ch['id']}>. People press **Verify** to get their roles.")
        elif a != "status":
            return await say(interaction, "Options: status, send, group, nickname, role, unverified, mirror, prompt, title, text, stats.")
        d = await asyncio.to_thread(api, "PUT" if body else "GET", f"/internal/guild/yapin/{sid}/config", body or None)
        c = (d or {}).get("config") or {}
        if body.get("verified_role_id") == "create":
            await asyncio.to_thread(ensure_verified_role, sid, c)
        lines = [f"Group: `{c.get('group_id') or 'not set'}`", f"Nickname: `{c.get('nickname_template') or 'off'}` (use {{name}}, {{display}}, {{id}}, {{rank}})",
                 f"Verified role: `{c.get('verified_role_id') or 'none'}` · Unverified role: `{c.get('unverified_role_id') or 'none'}`",
                 f"Mirror group roles: {'on' if c.get('mirror_roles') else 'off'} · DM prompt on join: {'on' if c.get('prompt_dm') else 'off'}",
                 f"Panel: {'posted in <#' + str(c['panel_channel_id']) + '>' if c.get('panel_message_id') else 'not posted. Use `/robloxpanel send [#channel]`'}"]
        line = await asyncio.to_thread(stats_line, sid) if c.get("group_id") else None
        if line:
            lines.append(line)
        lines.append("Run `/sync` to apply it to everyone. A backup sync runs every 24 hours. More settings: /dashboard/roblox/manage")
        await say(interaction, "\n".join(lines))

    @bot.tree.command(name="sync", description="Sync every member's Roblox roles and nickname with the group (server admins)")
    async def sync(interaction: discord.Interaction):
        import asyncio
        if not is_manager(interaction):
            return await say(interaction, "Only server admins can run a sync.")
        sid = str(interaction.guild_id)
        if not await asyncio.to_thread(cfg_of, sid):
            return await say(interaction, "Set the group first: `/robloxpanel group <id>`.")
        await say(interaction, "Syncing every member now. This can take a few minutes; I'll send you the result.")

        def job():
            counts = sync_guild(sid)
            if counts.get("busy"):
                text = "A sync is already running here."
            elif "synced" in counts:
                text = (f"Sync finished: {counts['synced']} verified members updated, {counts['unverified']} not verified, "
                        f"{counts['skipped']} skipped, {counts['errors']} errors.")
                if counts["created"] or counts["renamed"] or counts["deleted"]:
                    text += f" Roles: {counts['created']} created, {counts['renamed']} renamed, {counts['deleted']} deleted."
            else:
                text = f"Sync failed: {counts.get('error')}"
            try:
                bot.send(bot.dm(str(interaction.user.id)), text)
            except Exception:
                pass
        threading.Thread(target=job, daemon=True).start()

    m.rverify_sync = sync_guild
    m.rverify_stats = lambda sid: api("GET", f"/internal/guild/yapin/{sid}/stats")

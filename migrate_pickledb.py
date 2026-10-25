"""One-time copy of your old pickledb files (counting.db, embed.db, ...) into Cloudflare D1.

    python migrate_pickledb.py            # looks for the .db files in the current folder
"""
import json
import os

from dotenv import load_dotenv

load_dotenv()
from cfstore import CloudflareD1  # noqa: E402  (after load_dotenv so the env vars are set)

FILES = {"counting.db": "counting", "embed.db": "embed", "users.db": "users", "autorole.db": "autorole", "ai.db": "ai"}

for path, name in FILES.items():
    if not os.path.exists(path):
        print(f"skip {path} (not found)")
        continue
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    db = CloudflareD1(name)
    for key, value in data.items():
        db.set(key, value)
    ok = db.save()
    print(f"{path}: {len(data)} keys -> {name} ({db.backend}) {'ok' if ok else 'WITH ERRORS'}")

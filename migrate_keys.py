#!/usr/bin/env python3
"""One-time migration: move config.json API keys to user's user_models, set admin."""
import json
import sqlite3
from pathlib import Path
from datetime import datetime

DB_PATH = Path(__file__).parent / "debate.db"
CONFIG_PATH = Path(__file__).parent / "config.json"

with open(CONFIG_PATH) as f:
    config = json.load(f)

models_to_migrate = [
    ("mimo-v2.5-pro", config["models"].get("mimo-v2.5-pro")),
    ("kimi-coding", config["models"].get("kimi-coding")),
    ("deepseek-reasoner", config["models"].get("deepseek-reasoner")),
    ("glm-5-turbo", config["models"].get("glm-5-turbo")),
]

# Filter out models without API keys
models_to_migrate = [(mid, m) for mid, m in models_to_migrate if m and m.get("api_key")]

if not models_to_migrate:
    print("No API keys found in config.json — nothing to migrate.")
    exit(0)

conn = sqlite3.connect(str(DB_PATH))
conn.row_factory = sqlite3.Row

# Ask which user to migrate to
rows = conn.execute("SELECT id, username FROM users ORDER BY id").fetchall()
if not rows:
    print("No users found in database. Please register a user first.")
    conn.close()
    exit(1)

print("Available users:")
for r in rows:
    print(f"  [{r['id']}] {r['username']}")

try:
    choice = input(f"\nMigrate API keys to which user? (enter id, default=last): ").strip()
    if choice:
        user_id = int(choice)
    else:
        user_id = rows[-1]["id"]
    user_row = next((r for r in rows if r["id"] == user_id), None)
    if not user_row:
        print(f"User id {user_id} not found.")
        conn.close()
        exit(1)
except (ValueError, KeyboardInterrupt):
    print("\nCancelled.")
    conn.close()
    exit(1)

print(f"\nTarget user: {user_row['username']} (id={user_id})")

# Check existing
existing = conn.execute("SELECT COUNT(*) as cnt FROM user_models WHERE user_id = ?", (user_id,)).fetchone()
if existing["cnt"] > 0:
    print(f"User already has {existing['cnt']} model(s). Skipping migration to avoid duplicates.")
    conn.close()
    exit(0)

# Generate Fernet key
from cryptography.fernet import Fernet
fernet_key = Fernet.generate_key().decode()
fernet = Fernet(fernet_key.encode())

print(f"\n{'='*60}")
print(f"IMPORTANT: Set this environment variable before starting the server:")
print(f"  export DEBATE_ENCRYPTION_KEY={fernet_key}")
print(f"{'='*60}\n")

# Migrate keys
for mid, m in models_to_migrate:
    encrypted_key = fernet.encrypt(m["api_key"].encode()).decode()
    conn.execute(
        "INSERT INTO user_models (user_id, name, base_url, api_key, model, auth_type, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user_id, m["name"], m["base_url"], encrypted_key, m["model"], m.get("auth_type", "bearer"), datetime.now().isoformat())
    )
    print(f"  Migrated: {m['name']}")

# Add is_admin column and set user as admin
try:
    conn.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER DEFAULT 0")
except sqlite3.OperationalError:
    pass
conn.execute("UPDATE users SET is_admin = 1 WHERE id = ?", (user_id,))
print(f"\n  Set {user_row['username']} (id={user_id}) as admin")

conn.commit()
conn.close()

print("\nMigration complete!")
print("Next steps:")
print("  1. Set DEBATE_ENCRYPTION_KEY env var (see above)")
print("  2. Optionally share models via env vars:")
for mid, _ in models_to_migrate:
    env_name = mid.upper().replace("-", "_") + "_API_KEY"
    print(f"     export {env_name}=sk-...")
print("  3. Restart the server")

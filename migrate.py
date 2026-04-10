"""
migrate.py — Migrate accounts from accounts.json to SQLite
"""

import json
import time
from pathlib import Path
from database import get_db

DATA_DIR = Path(__file__).parent / "data"
ACCOUNTS_FILE = DATA_DIR / "accounts.json"


def migrate():
    if not ACCOUNTS_FILE.exists():
        print("No accounts.json found, skipping migration")
        return 0

    db = get_db()

    # Check if already migrated
    existing = db.fetchone("SELECT COUNT(*) as c FROM accounts")
    if existing["c"] > 0:
        print(f"Database already has {existing['c']} accounts, skipping migration")
        return existing["c"]

    raw = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
    count = 0
    for item in raw:
        db.execute(
            """INSERT OR IGNORE INTO accounts
               (id, name, token, visitorid, enabled, email, password,
                mail_provider, mail_api_key, created_at, quota_remain)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                item.get("id", ""),
                item.get("name", ""),
                item.get("token", ""),
                item.get("visitorid", ""),
                1 if item.get("enabled", True) else 0,
                item.get("email", ""),
                item.get("password", ""),
                item.get("mail_provider", ""),
                item.get("mail_api_key", ""),
                item.get("created_at", time.time()),
                100,  # default quota
            )
        )
        count += 1

    # Also migrate .env config values if they exist
    import os
    from dotenv import load_dotenv
    load_dotenv()

    env_mappings = {
        "RITA_UPSTREAM": os.getenv("RITA_UPSTREAM", ""),
        "RITA_ORIGIN": os.getenv("RITA_ORIGIN", ""),
        "AUTH_TOKEN": os.getenv("AUTH_TOKEN", ""),
        "DISABLE_SSL_VERIFY": os.getenv("DISABLE_SSL_VERIFY", ""),
        "HEALTH_CHECK_INTERVAL": os.getenv("HEALTH_CHECK_INTERVAL", ""),
        "AUTO_REGISTER_ENABLED": os.getenv("AUTO_REGISTER_ENABLED", ""),
        "YESCAPTCHA_KEY": os.getenv("YESCAPTCHA_KEY", ""),
        "GPTMAIL_API_KEY": os.getenv("GPTMAIL_API_KEY", ""),
        "YYDSMAIL_API_KEY": os.getenv("YYDSMAIL_API_KEY", ""),
    }
    for key, val in env_mappings.items():
        if val:  # Only override if .env has a non-empty value
            db.set_config(key, val)

    print(f"Migrated {count} accounts from accounts.json to SQLite")
    return count


if __name__ == "__main__":
    migrate()

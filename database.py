"""
database.py — SQLite database layer for rita2api

Replaces JSON file storage with SQLite.
Thread-safe with connection-per-thread pattern.
"""

import sqlite3
import threading
import time
import os
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "rita.db"


class DB:
    """Thread-safe SQLite wrapper using connection-per-thread."""

    def __init__(self, db_path=None):
        self._db_path = str(db_path or DB_PATH)
        self._local = threading.local()
        self._init_tables()

    def _get_conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, 'conn') or self._local.conn is None:
            self._local.conn = sqlite3.connect(self._db_path)
            self._local.conn.row_factory = sqlite3.Row
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA foreign_keys=ON")
        return self._local.conn

    def execute(self, sql, params=None):
        conn = self._get_conn()
        cur = conn.execute(sql, params or ())
        conn.commit()
        return cur

    def executemany(self, sql, params_list):
        conn = self._get_conn()
        cur = conn.executemany(sql, params_list)
        conn.commit()
        return cur

    def fetchone(self, sql, params=None):
        return self._get_conn().execute(sql, params or ()).fetchone()

    def fetchall(self, sql, params=None):
        return self._get_conn().execute(sql, params or ()).fetchall()

    def _init_tables(self):
        conn = self._get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS accounts (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL DEFAULT '',
                token TEXT NOT NULL DEFAULT '',
                visitorid TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                email TEXT NOT NULL DEFAULT '',
                password TEXT NOT NULL DEFAULT '',
                mail_provider TEXT NOT NULL DEFAULT '',
                mail_api_key TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL DEFAULT 0,
                quota_remain INTEGER NOT NULL DEFAULT 100,
                total_requests INTEGER NOT NULL DEFAULT 0,
                total_success INTEGER NOT NULL DEFAULT 0,
                total_fail INTEGER NOT NULL DEFAULT 0,
                last_used REAL NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                token_valid INTEGER NOT NULL DEFAULT 1,
                disabled_reason TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS usage_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                account_id TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                tokens_approx INTEGER NOT NULL DEFAULT 0,
                success INTEGER NOT NULL DEFAULT 1
            );
        """)
        # Seed default config values (INSERT OR IGNORE = don't overwrite existing)
        defaults = [
            ("RITA_UPSTREAM", "https://api_v2.rita.ai", "Rita.ai upstream API URL"),
            ("RITA_ORIGIN", "https://www.rita.ai", "Rita.ai origin for headers"),
            ("AUTH_TOKEN", "981115", "Admin panel password / API auth token"),
            ("DISABLE_SSL_VERIFY", "1", "Disable SSL verification for upstream"),
            ("HEALTH_CHECK_INTERVAL", "600", "Health check interval in seconds"),
            ("AUTO_REGISTER_ENABLED", "0", "Enable auto-registration"),
            ("AUTO_REGISTER_MIN_ACTIVE", "2", "Minimum active accounts"),
            ("AUTO_REGISTER_BATCH", "1", "Accounts per registration batch"),
            ("AUTO_REGISTER_PASSWORD", "@qazwsx123456", "Default password for new accounts"),
            ("REGISTER_PROXY", "", "Proxy URL for Rita registration requests"),
            ("MAIL_USE_PROXY", "0", "Whether mail provider requests should use REGISTER_PROXY"),
            ("CAPTCHA_PROVIDER", "yescaptcha", "Captcha provider: yescaptcha or ohmycaptcha_local"),
            ("YESCAPTCHA_KEY", "", "YesCaptcha API key"),
            ("OHMYCAPTCHA_LOCAL_API_URL", "http://127.0.0.1:8001", "OhMyCaptcha Local API URL"),
            ("OHMYCAPTCHA_LOCAL_KEY", "", "OhMyCaptcha Local client key"),
            ("MAIL_PROVIDER_DEFAULT", "gptmail", "Default mail provider for auto-registration"),
            ("GPTMAIL_API_KEY", "", "GPTMail API key"),
            ("GPTMAIL_API_BASE", "https://mail.chatgpt.org.uk", "GPTMail API base URL"),
            ("YYDSMAIL_API_KEY", "", "YYDS Mail API key"),
            ("YYDSMAIL_API_BASE", "https://maliapi.215.im/v1", "YYDS Mail API base URL"),
            ("MOEMAIL_API_KEY", "", "MoeMail API key"),
            ("MOEMAIL_API_BASE", "", "MoeMail API base URL"),
            ("AUTO_REGISTER_MIN_QUOTA", "50", "Auto-register when total quota below this"),
        ]
        conn.executemany(
            "INSERT OR IGNORE INTO config (key, value, description) VALUES (?, ?, ?)",
            defaults
        )
        conn.commit()
        print(f"[Database] Initialized: {self._db_path}")

    # ===================== Config helpers =====================
    def get_config(self, key, default=""):
        row = self.fetchone("SELECT value FROM config WHERE key=?", (key,))
        return row["value"] if row else default

    def set_config(self, key, value, description=""):
        self.execute(
            "INSERT INTO config (key, value, description) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value, description)
        )

    def get_all_config(self):
        rows = self.fetchall("SELECT key, value, description FROM config ORDER BY key")
        return [dict(r) for r in rows]

    # ===================== Usage log helpers =====================
    def log_usage(self, account_id, model, tokens_approx=0, success=True):
        self.execute(
            "INSERT INTO usage_log (timestamp, account_id, model, tokens_approx, success) VALUES (?,?,?,?,?)",
            (time.time(), account_id, model, tokens_approx, 1 if success else 0)
        )

    def get_usage_stats(self):
        today_start = time.mktime(time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d"))
        total = self.fetchone("SELECT COUNT(*) as c, SUM(tokens_approx) as t FROM usage_log")
        today = self.fetchone("SELECT COUNT(*) as c FROM usage_log WHERE timestamp >= ?", (today_start,))
        by_model = self.fetchall(
            "SELECT model, COUNT(*) as c FROM usage_log GROUP BY model ORDER BY c DESC LIMIT 20"
        )
        return {
            "total_requests": total["c"] or 0,
            "total_tokens_approx": total["t"] or 0,
            "requests_today": today["c"] or 0,
            "requests_by_model": {r["model"]: r["c"] for r in by_model},
        }


# Singleton
_db = None
def get_db():
    global _db
    if _db is None:
        _db = DB()
    return _db


if __name__ == "__main__":
    db = DB()
    tables = db.fetchall("SELECT name FROM sqlite_master WHERE type='table'")
    print(f"Database initialized: {len(tables)} tables created")
    for t in tables:
        print(f"  - {t['name']}")

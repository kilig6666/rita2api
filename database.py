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
                request_type TEXT NOT NULL DEFAULT 'unknown',
                tokens_approx INTEGER NOT NULL DEFAULT 0,
                success INTEGER NOT NULL DEFAULT 1
            );
        """)
        usage_log_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(usage_log)")
        }
        if "request_type" not in usage_log_columns:
            conn.execute(
                "ALTER TABLE usage_log ADD COLUMN request_type TEXT NOT NULL DEFAULT 'unknown'"
            )
        # Seed default config values (INSERT OR IGNORE = don't overwrite existing)
        defaults = [
            ("RITA_UPSTREAM", "https://api_v2.rita.ai", "Rita.ai upstream API URL"),
            ("RITA_ORIGIN", "https://www.rita.ai", "Rita.ai origin for headers"),
            ("AUTH_TOKEN", "981115", "Admin panel / WebUI password for /api/* management endpoints"),
            ("PROXY_API_KEY", "", "External model invocation API key for /v1 OpenAI and Anthropic proxy requests"),
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
            ("MOEMAIL_CHANNELS_JSON", "", "MoeMail channel list JSON"),
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
    def log_usage(self, account_id, model, tokens_approx=0, success=True, request_type="unknown"):
        request_type_text = str(request_type or "unknown").strip() or "unknown"
        self.execute(
            "INSERT INTO usage_log (timestamp, account_id, model, request_type, tokens_approx, success) "
            "VALUES (?,?,?,?,?,?)",
            (time.time(), account_id, model, request_type_text, tokens_approx, 1 if success else 0)
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

    def get_request_logs(self, page=1, page_size=20, request_type="", model="", date_range="all"):
        try:
            page_num = max(1, int(page))
        except Exception:
            page_num = 1
        try:
            page_size_num = int(page_size)
        except Exception:
            page_size_num = 20
        if page_size_num not in (20, 50, 100):
            page_size_num = 20

        where_clauses = []
        params = []
        now_ts = time.time()

        request_type_text = str(request_type or "").strip()
        if request_type_text and request_type_text != "__all__":
            where_clauses.append("ul.request_type = ?")
            params.append(request_type_text)

        model_text = str(model or "").strip()
        if model_text:
            where_clauses.append("ul.model LIKE ?")
            params.append(f"%{model_text}%")

        date_range_text = str(date_range or "all").strip().lower() or "all"
        if date_range_text not in {"all", "today", "7d", "30d"}:
            date_range_text = "all"
        if date_range_text == "today":
            today_start = time.mktime(time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d"))
            where_clauses.append("ul.timestamp >= ?")
            params.append(today_start)
        elif date_range_text == "7d":
            where_clauses.append("ul.timestamp >= ?")
            params.append(now_ts - 7 * 24 * 60 * 60)
        elif date_range_text == "30d":
            where_clauses.append("ul.timestamp >= ?")
            params.append(now_ts - 30 * 24 * 60 * 60)

        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        params_tuple = tuple(params)
        summary = self.fetchone(
            f"""
            SELECT
                COUNT(*) AS total,
                COALESCE(SUM(tokens_approx), 0) AS total_tokens,
                COALESCE(SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END), 0) AS success_count,
                COALESCE(SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END), 0) AS failed_count
            FROM usage_log ul
            {where_sql}
            """,
            params_tuple,
        )
        total = int(summary["total"] or 0) if summary else 0
        total_pages = max(1, (total + page_size_num - 1) // page_size_num) if total else 1
        if page_num > total_pages:
            page_num = total_pages
        offset = (page_num - 1) * page_size_num

        by_request_type_rows = self.fetchall(
            f"""
            SELECT
                ul.request_type,
                COUNT(*) AS total,
                COALESCE(SUM(ul.tokens_approx), 0) AS total_tokens
            FROM usage_log ul
            {where_sql}
            GROUP BY ul.request_type
            ORDER BY total DESC, ul.request_type ASC
            """,
            params_tuple,
        )
        by_model_rows = self.fetchall(
            f"""
            SELECT
                ul.model,
                COUNT(*) AS total,
                COALESCE(SUM(ul.tokens_approx), 0) AS total_tokens
            FROM usage_log ul
            {where_sql}
            GROUP BY ul.model
            ORDER BY total DESC, total_tokens DESC, ul.model ASC
            LIMIT 10
            """,
            params_tuple,
        )

        rows = self.fetchall(
            f"""
            SELECT
                ul.id,
                ul.timestamp,
                ul.account_id,
                COALESCE(a.name, '') AS account_name,
                ul.model,
                ul.request_type,
                ul.tokens_approx,
                ul.success
            FROM usage_log ul
            LEFT JOIN accounts a ON a.id = ul.account_id
            {where_sql}
            ORDER BY ul.timestamp DESC, ul.id DESC
            LIMIT ? OFFSET ?
            """,
            (*params_tuple, page_size_num, offset),
        )
        return {
            "logs": [
                {
                    "id": row["id"],
                    "timestamp": row["timestamp"],
                    "account_id": row["account_id"],
                    "account_name": row["account_name"],
                    "model": row["model"],
                    "request_type": row["request_type"] or "unknown",
                    "tokens_approx": row["tokens_approx"] or 0,
                    "success": bool(row["success"]),
                }
                for row in rows
            ],
            "summary": {
                "total_requests": total,
                "total_tokens_approx": int(summary["total_tokens"] or 0) if summary else 0,
                "success_count": int(summary["success_count"] or 0) if summary else 0,
                "failed_count": int(summary["failed_count"] or 0) if summary else 0,
            },
            "breakdown": {
                "by_request_type": [
                    {
                        "request_type": row["request_type"] or "unknown",
                        "total_requests": int(row["total"] or 0),
                        "total_tokens_approx": int(row["total_tokens"] or 0),
                    }
                    for row in by_request_type_rows
                ],
                "by_model": [
                    {
                        "model": row["model"] or "",
                        "total_requests": int(row["total"] or 0),
                        "total_tokens_approx": int(row["total_tokens"] or 0),
                    }
                    for row in by_model_rows
                ],
            },
            "pagination": {
                "page": page_num,
                "page_size": page_size_num,
                "total": total,
                "total_pages": total_pages,
            },
            "filters": {
                "request_type": request_type_text,
                "model": model_text,
                "date_range": date_range_text,
            },
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

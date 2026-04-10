"""
accounts.py — 账号管理模块 (SQLite版)

基于 SQLite 持久化 + Round-robin 轮换 + 自动故障转移
支持运行时增删改查，包含点数系统和健康检查。
"""

import os
import time
import uuid
import requests
import urllib3
import threading
from threading import Lock

from database import get_db

_KEY_FAIL_THRESHOLD = 3
_DISABLE_SSL_VERIFY = os.getenv("DISABLE_SSL_VERIFY", "0") == "1"
_HEALTH_CHECK_INTERVAL = int(os.getenv("HEALTH_CHECK_INTERVAL", "600"))

if _DISABLE_SSL_VERIFY:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def _mask(s: str) -> str:
    if not s:
        return ""
    if len(s) <= 8:
        return s[:2] + "***"
    return s[:4] + "***" + s[-4:]


def _row_to_status(row) -> dict:
    """Convert a sqlite3.Row to the API status dict."""
    d = dict(row)
    enabled = bool(d.get("enabled", 0))
    failures = d.get("failures", 0) or 0
    return {
        "id": d["id"],
        "name": d.get("name", ""),
        "email": d.get("email", ""),
        "token_preview": _mask(d.get("token", "")),
        "visitorid_preview": _mask(d.get("visitorid", "")),
        "enabled": enabled,
        "active": enabled and failures < _KEY_FAIL_THRESHOLD,
        "failures": failures,
        "quota_remain": d.get("quota_remain", 100),
        "total_requests": d.get("total_requests", 0),
        "total_success": d.get("total_success", 0),
        "total_fail": d.get("total_fail", 0),
        "last_used": d.get("last_used", 0),
        "last_error": d.get("last_error", ""),
        "created_at": d.get("created_at", 0),
        "token_valid": bool(d.get("token_valid", 1)),
        "disabled_reason": d.get("disabled_reason", ""),
        "password_set": bool(d.get("password", "")),
        "mail_provider": d.get("mail_provider", ""),
        "mail_api_key_set": bool(d.get("mail_api_key", "")),
        "refreshable": bool(d.get("email", "")),
    }


class Account:
    """Lightweight account object for in-flight use (not persisted directly)."""

    def __init__(self, row):
        d = dict(row)
        self.id = d["id"]
        self.name = d.get("name", "")
        self.token = d.get("token", "")
        self.visitorid = d.get("visitorid", "")
        self.enabled = bool(d.get("enabled", 1))
        self.email = d.get("email", "")
        self.password = d.get("password", "")
        self.mail_provider = d.get("mail_provider", "")
        self.mail_api_key = d.get("mail_api_key", "")
        self.quota_remain = d.get("quota_remain", 100)
        self.failures = d.get("failures", 0) or 0
        self.total_requests = d.get("total_requests", 0)
        self.total_success = d.get("total_success", 0)
        self.total_fail = d.get("total_fail", 0)
        self.last_used = d.get("last_used", 0)
        self.last_error = d.get("last_error", "")
        self.token_valid = bool(d.get("token_valid", 1))
        self.disabled_reason = d.get("disabled_reason", "")
        self.created_at = d.get("created_at", 0)

    def to_status(self) -> dict:
        return _row_to_status({
            "id": self.id, "name": self.name, "token": self.token,
            "visitorid": self.visitorid, "enabled": self.enabled,
            "email": self.email, "password": self.password,
            "mail_provider": self.mail_provider, "mail_api_key": self.mail_api_key,
            "quota_remain": self.quota_remain, "failures": self.failures,
            "total_requests": self.total_requests, "total_success": self.total_success,
            "total_fail": self.total_fail, "last_used": self.last_used,
            "last_error": self.last_error, "token_valid": self.token_valid,
            "disabled_reason": self.disabled_reason, "created_at": self.created_at,
        })


class AccountManager:
    """
    多账号管理器 (SQLite版)：
    - 持久化到 data/rita.db
    - Round-robin 轮换 + 自动故障转移
    - 运行时增删改查
    - 点数系统 (quota_remain)
    """

    def __init__(self):
        self._db = get_db()
        self._lock = Lock()
        self._robin_index = 0
        self._health_check_results: dict = {}
        # Add failures column if missing (migration safety)
        try:
            self._db.execute("ALTER TABLE accounts ADD COLUMN failures INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass  # column already exists
        count = self._db.fetchone("SELECT COUNT(*) as c FROM accounts")["c"]
        print(f"[AccountManager] Loaded {count} accounts from SQLite")

    # ===================== CRUD =====================
    def list_all(self) -> list[dict]:
        rows = self._db.fetchall("SELECT * FROM accounts ORDER BY created_at DESC")
        return [_row_to_status(r) for r in rows]

    def get(self, account_id: str) -> Account | None:
        row = self._db.fetchone("SELECT * FROM accounts WHERE id=?", (account_id,))
        return Account(row) if row else None

    def add(self, token: str, visitorid: str = "", name: str = "",
            email: str = "", password: str = "",
            mail_provider: str = "", mail_api_key: str = "") -> Account:
        aid = uuid.uuid4().hex[:8]
        name = name or f"account-{aid}"
        self._db.execute(
            """INSERT INTO accounts (id,name,token,visitorid,enabled,email,password,
               mail_provider,mail_api_key,created_at,quota_remain) VALUES (?,?,?,?,1,?,?,?,?,?,100)""",
            (aid, name, token, visitorid, email, password,
             mail_provider, mail_api_key, time.time())
        )
        return self.get(aid)

    def add_batch(self, accounts: list[dict]) -> list[Account]:
        added = []
        for item in accounts:
            token = item.get("token", "").strip()
            if not token:
                continue
            acc = self.add(
                token=token,
                visitorid=item.get("visitorid", "").strip(),
                name=item.get("name", "").strip(),
                email=item.get("email", "").strip(),
                password=item.get("password", "").strip(),
                mail_provider=item.get("mail_provider", "").strip(),
                mail_api_key=item.get("mail_api_key", "").strip(),
            )
            if acc:
                added.append(acc)
        return added

    def update(self, account_id: str, **fields) -> Account | None:
        allowed = {"name", "token", "visitorid", "enabled",
                    "email", "password", "mail_provider", "mail_api_key"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return self.get(account_id)
        set_clause = ", ".join(f"{k}=?" for k in updates)
        values = list(updates.values()) + [account_id]
        self._db.execute(f"UPDATE accounts SET {set_clause} WHERE id=?", values)
        return self.get(account_id)

    def delete(self, account_id: str) -> bool:
        cur = self._db.execute("DELETE FROM accounts WHERE id=?", (account_id,))
        return cur.rowcount > 0

    def delete_all(self) -> int:
        cur = self._db.execute("DELETE FROM accounts")
        return cur.rowcount

    def toggle(self, account_id: str) -> Account | None:
        self._db.execute(
            "UPDATE accounts SET enabled = CASE WHEN enabled=1 THEN 0 ELSE 1 END WHERE id=?",
            (account_id,)
        )
        return self.get(account_id)

    def reset_failures(self, account_id: str = "") -> int:
        if account_id:
            cur = self._db.execute(
                "UPDATE accounts SET failures=0, last_error='' WHERE id=? AND failures>0",
                (account_id,)
            )
        else:
            cur = self._db.execute(
                "UPDATE accounts SET failures=0, last_error='' WHERE failures>0"
            )
        return cur.rowcount

    # ===================== Round-Robin =====================
    def next(self) -> tuple[Account | None, int]:
        with self._lock:
            rows = self._db.fetchall(
                "SELECT * FROM accounts WHERE enabled=1 AND quota_remain>0 ORDER BY id"
            )
            n = len(rows)
            if n == 0:
                return None, -1

            for _ in range(n):
                row = rows[self._robin_index % n]
                self._robin_index += 1
                acc = Account(row)
                if acc.failures < _KEY_FAIL_THRESHOLD:
                    return acc, self._robin_index - 1

            # All exhausted — reset failures and return first
            self._db.execute(
                "UPDATE accounts SET failures=0, last_error='' WHERE enabled=1"
            )
            print("[AccountManager] All accounts hit failure threshold, reset all")
            return Account(rows[0]), 0

    def mark_ok(self, acc: Account):
        self._db.execute(
            """UPDATE accounts SET failures=0, total_success=total_success+1,
               total_requests=total_requests+1, last_used=?, last_error=''
               WHERE id=?""",
            (time.time(), acc.id)
        )

    def mark_fail(self, acc: Account, error: str = ""):
        self._db.execute(
            """UPDATE accounts SET failures=failures+1, total_fail=total_fail+1,
               total_requests=total_requests+1, last_used=?, last_error=?
               WHERE id=?""",
            (time.time(), error, acc.id)
        )

    # ===================== Quota =====================
    def deduct_quota(self, account_id: str, cost: int):
        self._db.execute(
            "UPDATE accounts SET quota_remain=MAX(0, quota_remain-?) WHERE id=?",
            (cost, account_id)
        )

    def get_total_quota(self) -> int:
        row = self._db.fetchone(
            "SELECT COALESCE(SUM(quota_remain),0) as total FROM accounts WHERE enabled=1"
        )
        return row["total"]

    # ===================== Health Check =====================
    def test_account(self, account_id: str, upstream_url: str, origin: str) -> dict:
        acc = self.get(account_id)
        if not acc:
            return {"ok": False, "error": "account not found"}

        headers = {
            "Content-Type": "application/json",
            "Origin": origin, "Referer": origin,
        }
        if acc.token:
            headers["token"] = acc.token
        if acc.visitorid:
            headers["visitorid"] = acc.visitorid

        try:
            start = time.time()
            r = requests.post(
                f"{upstream_url}/aichat/categoryModels",
                headers=headers, json={"language": "zh"},
                timeout=10, verify=not _DISABLE_SSL_VERIFY,
            )
            latency = round((time.time() - start) * 1000)

            if r.status_code != 200:
                return {"ok": False, "http_status": r.status_code,
                        "message": f"HTTP {r.status_code}", "latency_ms": latency}

            body = r.json()
            code = body.get("code", -1)
            if code == 0:
                models_count = sum(
                    len(cat.get("models", []))
                    for cat in body.get("data", {}).get("category_models", [])
                )
                return {"ok": True, "latency_ms": latency, "models": models_count}
            else:
                return {"ok": False, "code": code,
                        "message": body.get("message", ""), "latency_ms": latency}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ===================== Summary =====================
    def summary(self) -> dict:
        rows = self._db.fetchall(
            """SELECT
                COUNT(*) as total,
                SUM(CASE WHEN enabled=1 THEN 1 ELSE 0 END) as enabled,
                SUM(CASE WHEN enabled=1 AND failures<? THEN 1 ELSE 0 END) as active,
                COALESCE(SUM(quota_remain),0) as total_quota
            FROM accounts""",
            (_KEY_FAIL_THRESHOLD,)
        )
        r = rows[0] if rows else {}
        total = r["total"] or 0
        enabled = r["enabled"] or 0
        active = r["active"] or 0
        return {
            "total": total,
            "enabled": enabled,
            "active": active,
            "disabled": total - enabled,
            "failed": enabled - active,
            "total_quota": r["total_quota"] or 0,
        }

    # ===================== Headers Helper =====================
    def upstream_headers(self, acc: Account, origin: str) -> dict:
        h = {
            "Content-Type": "application/json",
            "Origin": origin, "Referer": origin,
        }
        if acc.token:
            h["token"] = acc.token
        if acc.visitorid:
            h["visitorid"] = acc.visitorid
        return h

    # ===================== Background Health Check =====================
    def start_health_checker(self, upstream_url: str, origin: str, log_fn=None):
        self._upstream_url = upstream_url
        self._origin = origin
        self._log_fn = log_fn or (lambda msg, level="INFO": print(f"[HealthCheck] {msg}"))

        t = threading.Thread(target=self._health_check_loop, daemon=True,
                             name="token-health-checker")
        t.start()
        self._log_fn(f"Token health checker started (interval: {_HEALTH_CHECK_INTERVAL}s)", "INFO")

    def _health_check_loop(self):
        time.sleep(30)
        while True:
            try:
                self._run_health_check()
            except Exception as e:
                self._log_fn(f"Health check error: {e}", "ERROR")
            time.sleep(_HEALTH_CHECK_INTERVAL)

    def _run_health_check(self):
        rows = self._db.fetchall("SELECT id FROM accounts WHERE enabled=1")
        if not rows:
            return

        self._log_fn(f"Health check: testing {len(rows)} accounts...", "INFO")
        ok_count = fail_count = disabled_count = 0

        for row in rows:
            aid = row["id"]
            result = self.test_account(aid, self._upstream_url, self._origin)

            if result.get("ok"):
                self._db.execute(
                    "UPDATE accounts SET failures=0, last_error='', token_valid=1, disabled_reason='' WHERE id=?",
                    (aid,)
                )
                ok_count += 1
            else:
                error_code = result.get("code", result.get("http_status", 0))
                error_msg = result.get("message", result.get("error", "unknown"))

                if error_code == 401:
                    self._db.execute(
                        """UPDATE accounts SET token_valid=0, enabled=0,
                           disabled_reason='token_expired_401',
                           last_error='Token expired (auto-disabled)' WHERE id=?""",
                        (aid,)
                    )
                    disabled_count += 1
                    self._log_fn(f"Auto-disabled account {aid}: token expired (401)", "WARNING")
                else:
                    self._db.execute(
                        "UPDATE accounts SET failures=failures+1, last_error=? WHERE id=?",
                        (str(error_msg), aid)
                    )
                    fail_count += 1

            time.sleep(1)

        self._health_check_results = {
            "last_check": time.time(),
            "total_checked": len(rows),
            "ok": ok_count, "failed": fail_count,
            "auto_disabled": disabled_count,
        }
        self._log_fn(
            f"Health check done: ok={ok_count} fail={fail_count} disabled={disabled_count}",
            "INFO" if disabled_count == 0 else "WARNING"
        )

    def get_health_status(self) -> dict:
        return self._health_check_results

    def purge_invalid(self) -> int:
        cur = self._db.execute("DELETE FROM accounts WHERE token_valid=0")
        return cur.rowcount

    def reactivate_account(self, account_id: str, new_token: str = "") -> Account | None:
        if new_token:
            self._db.execute(
                """UPDATE accounts SET token=?, enabled=1, token_valid=1,
                   failures=0, last_error='', disabled_reason='', quota_remain=100
                   WHERE id=?""",
                (new_token, account_id)
            )
        else:
            self._db.execute(
                """UPDATE accounts SET enabled=1, token_valid=1,
                   failures=0, last_error='', disabled_reason=''
                   WHERE id=?""",
                (account_id,)
            )
        return self.get(account_id)

    # Compatibility shim - server.py calls acm._save() in health check run endpoint
    def _save(self):
        pass  # No-op: SQLite auto-persists

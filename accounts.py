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
_DEFAULT_HEALTH_CHECK_INTERVAL = int(os.getenv("HEALTH_CHECK_INTERVAL", "600"))
_FAILURE_RETRY_COOLDOWN_SECONDS = 60

# SSL verify: read from DB if available, fallback to env
def _get_ssl_verify_disabled() -> bool:
    try:
        db = get_db()
        val = db.get_config("DISABLE_SSL_VERIFY", "")
        if val:
            return val == "1"
    except Exception:
        pass
    return os.getenv("DISABLE_SSL_VERIFY", "0") == "1"

# Disable SSL warnings globally if configured
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def _mask(s: str) -> str:
    if not s:
        return ""
    if len(s) <= 8:
        return s[:2] + "***"
    return s[:4] + "***" + s[-4:]


def _coerce_text(value) -> str:
    """把任意输入转成去首尾空格的字符串。"""
    if value is None:
        return ""
    return str(value).strip()


def _normalize_email_key(value) -> str:
    return _coerce_text(value).lower()


def _coerce_enabled_value(value, default: int = 1) -> int:
    """将 enabled 风格输入统一成 0/1。"""
    if value is None:
        return default
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "y", "on"}:
            return 1
        if text in {"0", "false", "no", "n", "off"}:
            return 0
    return 1 if bool(value) else 0


def _coerce_non_negative_int(value, default: int) -> int:
    """将 quota 等输入统一成非负整数。"""
    if value in (None, ""):
        return default
    try:
        return max(0, int(float(value)))
    except Exception:
        return default


def _get_health_check_interval() -> int:
    """健康检查间隔优先读 DB 运行时配置，读不到再回退 env 默认值。"""
    try:
        db = get_db()
        raw = str(db.get_config("HEALTH_CHECK_INTERVAL", "") or "").strip()
        if raw:
            return max(1, int(float(raw)))
    except Exception:
        pass
    return max(1, _DEFAULT_HEALTH_CHECK_INTERVAL)


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
        "inflight_count": d.get("inflight_count", 0) or 0,
        "last_selected_at": d.get("last_selected_at", 0) or 0,
        "disabled_at": d.get("disabled_at", 0) or 0,
        "password_set": bool(d.get("password", "")),
        "mail_provider": d.get("mail_provider", ""),
        "mail_api_key_set": bool(d.get("mail_api_key", "")),
        "refreshable": bool(d.get("email", "")),
    }


def _row_to_import_payload(row) -> dict:
    """将账号行转换成可直接重新导入的 JSON 载荷。"""
    d = dict(row)
    return {
        "token": d.get("token", ""),
        "visitorid": d.get("visitorid", ""),
        "name": d.get("name", ""),
        "email": d.get("email", ""),
        "password": d.get("password", ""),
        "mail_provider": d.get("mail_provider", ""),
        "mail_api_key": d.get("mail_api_key", ""),
        "quota_remain": d.get("quota_remain", 100),
        "enabled": bool(d.get("enabled", 1)),
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
        self.inflight_count = d.get("inflight_count", 0) or 0
        self.last_selected_at = d.get("last_selected_at", 0) or 0
        self.disabled_at = d.get("disabled_at", 0) or 0
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
            "disabled_reason": self.disabled_reason,
            "inflight_count": self.inflight_count,
            "last_selected_at": self.last_selected_at,
            "disabled_at": self.disabled_at,
            "created_at": self.created_at,
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
        for ddl in (
            "ALTER TABLE accounts ADD COLUMN inflight_count INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE accounts ADD COLUMN last_selected_at REAL NOT NULL DEFAULT 0",
            "ALTER TABLE accounts ADD COLUMN disabled_at REAL NOT NULL DEFAULT 0",
        ):
            try:
                self._db.execute(ddl)
            except Exception:
                pass
        count = self._db.fetchone("SELECT COUNT(*) as c FROM accounts")["c"]
        print(f"[AccountManager] Loaded {count} accounts from SQLite")

    # ===================== CRUD =====================
    def list_all(self) -> list[dict]:
        rows = self._db.fetchall("SELECT * FROM accounts ORDER BY created_at DESC")
        return [_row_to_status(r) for r in rows]

    def list_page(self, page=1, page_size=20) -> dict:
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

        total_row = self._db.fetchone("SELECT COUNT(*) AS total FROM accounts")
        total = total_row["total"] or 0
        total_pages = max(1, (total + page_size_num - 1) // page_size_num) if total else 1
        page_num = min(page_num, total_pages)
        offset = (page_num - 1) * page_size_num
        rows = self._db.fetchall(
            "SELECT * FROM accounts ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (page_size_num, offset),
        )
        return {
            "accounts": [_row_to_status(r) for r in rows],
            "pagination": {
                "page": page_num,
                "page_size": page_size_num,
                "total": total,
                "total_pages": total_pages,
            },
        }

    def list_all_ids(self) -> list[str]:
        rows = self._db.fetchall("SELECT id FROM accounts ORDER BY created_at DESC")
        return [r["id"] for r in rows]

    def export_for_import(self, account_ids: list[str] | None = None) -> list[dict]:
        """导出可直接回灌到 /api/accounts/batch 的 JSON 数组。"""
        if account_ids:
            normalized_ids = []
            seen = set()
            for account_id in account_ids:
                key = str(account_id or "").strip()
                if not key or key in seen:
                    continue
                seen.add(key)
                normalized_ids.append(key)
            exported = []
            for account_id in normalized_ids:
                row = self._db.fetchone("SELECT * FROM accounts WHERE id=?", (account_id,))
                if row is not None:
                    exported.append(_row_to_import_payload(row))
            return exported

        rows = self._db.fetchall("SELECT * FROM accounts ORDER BY created_at DESC")
        return [_row_to_import_payload(r) for r in rows]

    def get(self, account_id: str) -> Account | None:
        row = self._db.fetchone("SELECT * FROM accounts WHERE id=?", (account_id,))
        return Account(row) if row else None

    def add(self, token: str, visitorid: str = "", name: str = "",
            email: str = "", password: str = "",
            mail_provider: str = "", mail_api_key: str = "",
            quota_remain=None, enabled=None) -> Account:
        aid = uuid.uuid4().hex[:8]
        name = name or f"account-{aid}"
        enabled_value = _coerce_enabled_value(enabled, default=1)
        quota_value = _coerce_non_negative_int(quota_remain, default=100)
        self._db.execute(
            """INSERT INTO accounts (id,name,token,visitorid,enabled,email,password,
               mail_provider,mail_api_key,created_at,quota_remain) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (aid, name, token, visitorid, enabled_value, email, password,
             mail_provider, mail_api_key, time.time(), quota_value)
        )
        return self.get(aid)

    def _build_import_candidates(self, accounts: list[dict]) -> tuple[list[dict], dict]:
        total = len(accounts or [])
        missing_token = 0
        duplicate_token_in_payload = 0
        duplicate_email_in_payload = 0
        payload_token_seen = set()
        payload_email_seen = set()
        candidates: list[dict] = []

        for raw_item in accounts or []:
            item = raw_item if isinstance(raw_item, dict) else {}
            token = _coerce_text(item.get("token"))
            email = _coerce_text(item.get("email"))
            email_key = _normalize_email_key(email)

            if not token:
                missing_token += 1
                continue

            duplicate_token_payload = token in payload_token_seen
            if duplicate_token_payload:
                duplicate_token_in_payload += 1
            else:
                payload_token_seen.add(token)

            duplicate_email_payload = bool(email_key) and email_key in payload_email_seen
            if duplicate_email_payload:
                duplicate_email_in_payload += 1
            elif email_key:
                payload_email_seen.add(email_key)

            candidates.append({
                "token": token,
                "visitorid": _coerce_text(item.get("visitorid")),
                "name": _coerce_text(item.get("name")),
                "email": email,
                "email_key": email_key,
                "password": _coerce_text(item.get("password")),
                "mail_provider": _coerce_text(item.get("mail_provider")),
                "mail_api_key": _coerce_text(item.get("mail_api_key")),
                "quota_remain": _coerce_non_negative_int(item.get("quota_remain"), default=100),
                "enabled": bool(_coerce_enabled_value(item.get("enabled"), default=1)),
                "duplicate_token_in_payload": duplicate_token_payload,
                "duplicate_email_in_payload": duplicate_email_payload,
            })

        rows = self._db.fetchall("SELECT token, email FROM accounts")
        existing_token_set = {_coerce_text(r["token"]) for r in rows if _coerce_text(r["token"])}
        existing_email_set = {
            _normalize_email_key(r["email"])
            for r in rows
            if _normalize_email_key(r["email"])
        }

        duplicate_token_existing = 0
        duplicate_email_existing = 0
        duplicate_candidates_total = 0
        importable_total = 0

        for item in candidates:
            duplicate_token_existing_flag = item["token"] in existing_token_set
            duplicate_email_existing_flag = bool(item["email_key"]) and item["email_key"] in existing_email_set
            item["duplicate_token_existing"] = duplicate_token_existing_flag
            item["duplicate_email_existing"] = duplicate_email_existing_flag
            item["is_duplicate"] = any((
                item["duplicate_token_in_payload"],
                item["duplicate_email_in_payload"],
                duplicate_token_existing_flag,
                duplicate_email_existing_flag,
            ))

            if duplicate_token_existing_flag:
                duplicate_token_existing += 1
            if duplicate_email_existing_flag:
                duplicate_email_existing += 1
            if item["is_duplicate"]:
                duplicate_candidates_total += 1
            else:
                importable_total += 1

        summary = {
            "total": total,
            "valid": len(candidates),
            "missing_token": missing_token,
            "duplicate_token_in_payload": duplicate_token_in_payload,
            "duplicate_email_in_payload": duplicate_email_in_payload,
            "duplicate_token_existing": duplicate_token_existing,
            "duplicate_email_existing": duplicate_email_existing,
            "duplicate_candidates_total": duplicate_candidates_total,
            "importable_total": importable_total,
        }
        return candidates, summary

    def preview_batch_import(self, accounts: list[dict]) -> dict:
        """预检查导入账号，统计缺失与重复。"""
        _, summary = self._build_import_candidates(accounts)
        return summary

    def add_batch(self, accounts: list[dict], dedupe: bool = False) -> tuple[list[Account], dict]:
        candidates, summary = self._build_import_candidates(accounts)
        added = []
        skipped_duplicate_token = 0
        skipped_duplicate_email = 0

        for item in candidates:
            if dedupe and item["is_duplicate"]:
                if item["duplicate_token_in_payload"] or item["duplicate_token_existing"]:
                    skipped_duplicate_token += 1
                if item["duplicate_email_in_payload"] or item["duplicate_email_existing"]:
                    skipped_duplicate_email += 1
                continue
            acc = self.add(
                token=item["token"],
                visitorid=item["visitorid"],
                name=item["name"],
                email=item["email"],
                password=item["password"],
                mail_provider=item["mail_provider"],
                mail_api_key=item["mail_api_key"],
                quota_remain=item.get("quota_remain"),
                enabled=item.get("enabled"),
            )
            if acc:
                added.append(acc)

        result = {
            **summary,
            "dedupe_applied": bool(dedupe),
            "added": len(added),
            "skipped_duplicate_token": skipped_duplicate_token if dedupe else 0,
            "skipped_duplicate_email": skipped_duplicate_email if dedupe else 0,
        }
        return added, result

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

            start_index = self._robin_index % n
            exhausted: list[tuple[int, Account]] = []

            for offset in range(n):
                row_index = (start_index + offset) % n
                row = rows[row_index]
                acc = Account(row)
                if acc.failures < _KEY_FAIL_THRESHOLD:
                    self._robin_index = row_index + 1
                    return acc, row_index
                exhausted.append((row_index, acc))

            cooldown_before = time.time() - _FAILURE_RETRY_COOLDOWN_SECONDS
            cooled_exhausted = [
                item for item in exhausted
                if (item[1].last_used or 0) <= cooldown_before
            ]
            fallback_pool = cooled_exhausted or exhausted
            fallback_index, fallback_acc = min(
                fallback_pool,
                key=lambda item: (
                    item[1].failures,
                    item[1].last_used or 0,
                    (item[0] - start_index) % n,
                ),
            )
            self._robin_index = fallback_index + 1
            print(
                "[AccountManager] All accounts exceeded failure threshold, "
                f"fallback to {fallback_acc.id} "
                f"(failures={fallback_acc.failures}, last_used={fallback_acc.last_used})"
            )
            return fallback_acc, fallback_index

    def reserve_next(self, min_quota: int = 0, exclude_ids: list[str] | set[str] | tuple[str, ...] | None = None) -> Account | None:
        """原子保留一个账号，避免并发请求反复撞到同一账号。"""
        min_quota_value = max(0, int(min_quota or 0))
        exclude_list = [str(item).strip() for item in (exclude_ids or []) if str(item).strip()]

        with self._lock:
            conn = self._db._get_conn()
            placeholders = ""
            params: list = [min_quota_value]
            exclude_sql = ""
            if exclude_list:
                placeholders = ",".join("?" for _ in exclude_list)
                exclude_sql = f" AND id NOT IN ({placeholders})"
                params.extend(exclude_list)

            now = time.time()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    f"""
                    SELECT *
                    FROM accounts
                    WHERE enabled=1
                      AND quota_remain>=?
                      {exclude_sql}
                    ORDER BY inflight_count ASC, last_selected_at ASC, id ASC
                    LIMIT 1
                    """,
                    tuple(params),
                ).fetchone()
                if row is None:
                    conn.commit()
                    return None

                account_id = row["id"]
                update_params = (now, account_id, min_quota_value)
                cur = conn.execute(
                    """
                    UPDATE accounts
                    SET inflight_count=inflight_count+1,
                        last_selected_at=?
                    WHERE id=?
                      AND enabled=1
                      AND quota_remain>=?
                    """,
                    update_params,
                )
                if cur.rowcount != 1:
                    conn.rollback()
                    return None

                reserved_row = conn.execute(
                    "SELECT * FROM accounts WHERE id=?",
                    (account_id,),
                ).fetchone()
                conn.commit()
                return Account(reserved_row) if reserved_row else None
            except Exception:
                conn.rollback()
                raise

    def release_reservation(self, account_id: str):
        if not str(account_id or "").strip():
            return
        self._db.execute(
            """
            UPDATE accounts
            SET inflight_count=CASE
                WHEN inflight_count > 0 THEN inflight_count - 1
                ELSE 0
            END
            WHERE id=?
            """,
            (account_id,),
        )

    def disable_quota_exhausted(self, account_id: str, error: str = "") -> Account | None:
        now = time.time()
        error_text = str(error or "").strip() or "quota exhausted (auto-disabled)"
        self._db.execute(
            """
            UPDATE accounts
            SET enabled=0,
                quota_remain=0,
                disabled_reason='quota_exhausted',
                disabled_at=?,
                last_error=?,
                last_used=?
            WHERE id=?
            """,
            (now, error_text, now, account_id),
        )
        return self.get(account_id)

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
            headers["Cookie"] = f"token={acc.token}"
        if acc.visitorid:
            headers["visitorid"] = acc.visitorid

        try:
            start = time.time()
            r = requests.post(
                f"{upstream_url}/aichat/categoryModels",
                headers=headers, json={"language": "zh"},
                timeout=10, verify=not _get_ssl_verify_disabled(),
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
            h["Cookie"] = f"token={acc.token}"
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
        self._log_fn(
            f"Token health checker started (interval: {_get_health_check_interval()}s)",
            "INFO",
        )

    def _health_check_loop(self):
        time.sleep(30)
        while True:
            try:
                self._run_health_check()
            except Exception as e:
                self._log_fn(f"Health check error: {e}", "ERROR")
            time.sleep(_get_health_check_interval())

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
                   failures=0, last_error='', disabled_reason='',
                   disabled_at=0, quota_remain=100
                   WHERE id=?""",
                (new_token, account_id)
            )
        else:
            self._db.execute(
                """UPDATE accounts SET enabled=1, token_valid=1,
                   failures=0, last_error='', disabled_reason='',
                   disabled_at=0
                   WHERE id=?""",
                (account_id,)
            )
        return self.get(account_id)

    # Compatibility shim - server.py calls acm._save() in health check run endpoint
    def _save(self):
        pass  # No-op: SQLite auto-persists

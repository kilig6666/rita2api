"""
accounts.py — 账号管理模块

持久化 JSON 存储 + 内存缓存 + Round-robin 轮换 + 自动故障转移
支持运行时增删改查，无需重启服务。
包含后台 Token 健康检查与自动禁用机制。
"""

import json
import os
import time
import uuid
import requests
import urllib3
import threading
from threading import Lock
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)
ACCOUNTS_FILE = DATA_DIR / "accounts.json"

_KEY_FAIL_THRESHOLD = 3  # 连续失败 N 次后暂跳该账号
_DISABLE_SSL_VERIFY = os.getenv("DISABLE_SSL_VERIFY", "0") == "1"
# Health check interval in seconds (default: 10 minutes)
_HEALTH_CHECK_INTERVAL = int(os.getenv("HEALTH_CHECK_INTERVAL", "600"))

# Suppress InsecureRequestWarning when SSL verification is disabled
if _DISABLE_SSL_VERIFY:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class Account:
    """单个 Rita 账号"""

    def __init__(self, token: str, visitorid: str = "",
                 name: str = "", enabled: bool = True, id: str = "",
                 email: str = "", password: str = "",
                 mail_provider: str = "", mail_api_key: str = "",
                 **_kw):
        self.id = id or uuid.uuid4().hex[:8]
        self.name = name or f"account-{self.id}"
        self.token = token
        self.visitorid = visitorid
        self.enabled = enabled

        # Credentials for token refresh
        self.email = email
        self.password = password
        self.mail_provider = mail_provider      # "gptmail" | "yydsmail" | ""
        self.mail_api_key = mail_api_key        # per-account mail API key (optional)

        # Runtime stats (not persisted)
        self.failures = 0
        self.total_requests = 0
        self.total_success = 0
        self.total_fail = 0
        self.last_used: float = 0
        self.last_error: str = ""
        self.created_at: float = _kw.get("created_at", time.time())

        # Health check state
        self.last_health_check: float = 0
        self.last_health_ok: bool = False
        self.token_valid: bool = True  # assume valid until checked
        self.disabled_reason: str = ""  # why auto-disabled

    def to_dict(self) -> dict:
        """Serialize for JSON persistence"""
        return {
            "id": self.id,
            "name": self.name,
            "token": self.token,
            "visitorid": self.visitorid,
            "enabled": self.enabled,
            "created_at": self.created_at,
            "email": self.email,
            "password": self.password,
            "mail_provider": self.mail_provider,
            "mail_api_key": self.mail_api_key,
        }

    def to_status(self) -> dict:
        """Serialize for API/WebUI display (sensitive data masked)"""
        return {
            "id": self.id,
            "name": self.name,
            "token_preview": self._mask(self.token),
            "visitorid_preview": self._mask(self.visitorid),
            "enabled": self.enabled,
            "active": self.enabled and self.failures < _KEY_FAIL_THRESHOLD,
            "failures": self.failures,
            "total_requests": self.total_requests,
            "total_success": self.total_success,
            "total_fail": self.total_fail,
            "last_used": self.last_used,
            "last_error": self.last_error,
            "created_at": self.created_at,
            "token_valid": self.token_valid,
            "last_health_check": self.last_health_check,
            "disabled_reason": self.disabled_reason,
            "email": self.email,
            "password_set": bool(self.password),
            "mail_provider": self.mail_provider,
            "mail_api_key_set": bool(self.mail_api_key),
            "refreshable": bool(self.email),
        }

    @staticmethod
    def _mask(s: str) -> str:
        if not s:
            return ""
        if len(s) <= 8:
            return s[:2] + "***"
        return s[:4] + "***" + s[-4:]


class AccountManager:
    """
    多账号管理器：
    - 持久化到 data/accounts.json
    - Round-robin 轮换 + 自动故障转移
    - 运行时增删改查
    - 从环境变量初始化（首次运行）
    """

    def __init__(self):
        self._accounts: list[Account] = []
        self._lock = Lock()
        self._robin_index = 0
        self._load()

    # ===================== Persistence =====================
    def _load(self):
        """Load accounts from JSON file, merge with env vars"""
        if ACCOUNTS_FILE.exists():
            try:
                raw = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
                for item in raw:
                    self._accounts.append(Account(**item))
                print(f"[AccountManager] Loaded {len(self._accounts)} accounts from {ACCOUNTS_FILE}")
            except Exception as e:
                print(f"[AccountManager] ⚠️ Failed to load {ACCOUNTS_FILE}: {e}")

        # If no accounts loaded, try env vars
        if not self._accounts:
            self._import_from_env()

    def _import_from_env(self):
        """Import accounts from RITA_TOKENS / RITA_VISITOR_IDS env vars"""
        raw_tokens = os.getenv("RITA_TOKENS", os.getenv("RITA_TOKEN", ""))
        raw_vids = os.getenv("RITA_VISITOR_IDS", os.getenv("RITA_VISITOR_ID", ""))

        tokens = [s.strip() for s in raw_tokens.split(",") if s.strip()]
        vids = [s.strip() for s in raw_vids.split(",") if s.strip()]

        if not tokens:
            return

        for i, token in enumerate(tokens):
            vid = vids[i] if i < len(vids) else ""
            self._accounts.append(Account(
                token=token,
                visitorid=vid,
                name=f"env-account-{i}",
            ))

        if self._accounts:
            self._save()
            print(f"[AccountManager] Imported {len(self._accounts)} accounts from env vars")

    def _save(self):
        """Persist accounts to JSON"""
        try:
            data = [acc.to_dict() for acc in self._accounts]
            ACCOUNTS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            print(f"[AccountManager] ⚠️ Failed to save: {e}")

    # ===================== CRUD =====================
    def list_all(self) -> list[dict]:
        with self._lock:
            return [acc.to_status() for acc in self._accounts]

    def get(self, account_id: str) -> Account | None:
        with self._lock:
            for acc in self._accounts:
                if acc.id == account_id:
                    return acc
        return None

    def add(self, token: str, visitorid: str = "", name: str = "",
            email: str = "", password: str = "",
            mail_provider: str = "", mail_api_key: str = "") -> Account:
        """Add a single account"""
        with self._lock:
            acc = Account(token=token, visitorid=visitorid, name=name,
                          email=email, password=password,
                          mail_provider=mail_provider, mail_api_key=mail_api_key)
            self._accounts.append(acc)
            self._save()
            return acc

    def add_batch(self, accounts: list[dict]) -> list[Account]:
        """Add multiple accounts at once"""
        added = []
        with self._lock:
            for item in accounts:
                token = item.get("token", "").strip()
                if not token:
                    continue
                acc = Account(
                    token=token,
                    visitorid=item.get("visitorid", "").strip(),
                    name=item.get("name", "").strip(),
                    email=item.get("email", "").strip(),
                    password=item.get("password", "").strip(),
                    mail_provider=item.get("mail_provider", "").strip(),
                    mail_api_key=item.get("mail_api_key", "").strip(),
                )
                self._accounts.append(acc)
                added.append(acc)
            if added:
                self._save()
        return added

    def update(self, account_id: str, **fields) -> Account | None:
        with self._lock:
            for acc in self._accounts:
                if acc.id == account_id:
                    for key in ("name", "token", "visitorid", "enabled",
                                "email", "password", "mail_provider", "mail_api_key"):
                        if key in fields:
                            setattr(acc, key, fields[key])
                    self._save()
                    return acc
        return None

    def delete(self, account_id: str) -> bool:
        with self._lock:
            before = len(self._accounts)
            self._accounts = [a for a in self._accounts if a.id != account_id]
            if len(self._accounts) < before:
                self._save()
                return True
        return False

    def delete_all(self) -> int:
        with self._lock:
            count = len(self._accounts)
            self._accounts.clear()
            self._save()
            return count

    def toggle(self, account_id: str) -> Account | None:
        with self._lock:
            for acc in self._accounts:
                if acc.id == account_id:
                    acc.enabled = not acc.enabled
                    self._save()
                    return acc
        return None

    def reset_failures(self, account_id: str = "") -> int:
        """Reset failure counters. If account_id empty, reset all."""
        with self._lock:
            count = 0
            for acc in self._accounts:
                if not account_id or acc.id == account_id:
                    if acc.failures > 0:
                        acc.failures = 0
                        acc.last_error = ""
                        count += 1
            return count

    # ===================== Round-Robin =====================
    def next(self) -> tuple[Account | None, int]:
        """
        Round-robin next available account.
        Returns (Account, index) or (None, -1) if no accounts.
        """
        with self._lock:
            candidates = [a for a in self._accounts if a.enabled]
            n = len(candidates)
            if n == 0:
                return None, -1

            for _ in range(n):
                acc = candidates[self._robin_index % n]
                self._robin_index += 1
                if acc.failures < _KEY_FAIL_THRESHOLD:
                    return acc, self._robin_index - 1

            # All exhausted — reset and return first
            for acc in candidates:
                acc.failures = 0
                acc.last_error = ""
            print("[AccountManager] ⚠️ All accounts hit failure threshold, reset all")
            return candidates[0], 0

    def mark_ok(self, acc: Account):
        with self._lock:
            acc.failures = 0
            acc.total_success += 1
            acc.total_requests += 1
            acc.last_used = time.time()
            acc.last_error = ""

    def mark_fail(self, acc: Account, error: str = ""):
        with self._lock:
            acc.failures += 1
            acc.total_fail += 1
            acc.total_requests += 1
            acc.last_used = time.time()
            acc.last_error = error

    # ===================== Health Check =====================
    def test_account(self, account_id: str, upstream_url: str, origin: str) -> dict:
        """Test a single account by calling /aichat/categoryModels"""
        acc = self.get(account_id)
        if not acc:
            return {"ok": False, "error": "account not found"}

        headers = {
            "Content-Type": "application/json",
            "Origin": origin,
            "Referer": origin,
        }
        if acc.token:
            headers["token"] = acc.token
        if acc.visitorid:
            headers["visitorid"] = acc.visitorid

        try:
            start = time.time()
            r = requests.post(
                f"{upstream_url}/aichat/categoryModels",
                headers=headers,
                json={"language": "zh"},
                timeout=10,
                verify=not _DISABLE_SSL_VERIFY,
            )
            latency = round((time.time() - start) * 1000)

            # Handle HTTP-level errors gracefully instead of raising
            if r.status_code != 200:
                return {"ok": False, "http_status": r.status_code, "message": f"HTTP {r.status_code}", "latency_ms": latency}

            body = r.json()
            code = body.get("code", -1)
            if code == 0:
                models_count = sum(
                    len(cat.get("models", []))
                    for cat in body.get("data", {}).get("category_models", [])
                )
                return {"ok": True, "latency_ms": latency, "models": models_count}
            else:
                return {"ok": False, "code": code, "message": body.get("message", ""), "latency_ms": latency}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ===================== Summary =====================
    def summary(self) -> dict:
        with self._lock:
            total = len(self._accounts)
            enabled = sum(1 for a in self._accounts if a.enabled)
            active = sum(1 for a in self._accounts if a.enabled and a.failures < _KEY_FAIL_THRESHOLD)
            return {
                "total": total,
                "enabled": enabled,
                "active": active,
                "disabled": total - enabled,
                "failed": enabled - active,
            }

    # ===================== Headers Helper =====================
    def upstream_headers(self, acc: Account, origin: str) -> dict:
        h = {
            "Content-Type": "application/json",
            "Origin": origin,
            "Referer": origin,
        }
        if acc.token:
            h["token"] = acc.token
        if acc.visitorid:
            h["visitorid"] = acc.visitorid
        return h

    # ===================== Background Health Check =====================
    def start_health_checker(self, upstream_url: str, origin: str, log_fn=None):
        """Start background thread that periodically checks all account tokens."""
        self._upstream_url = upstream_url
        self._origin = origin
        self._log_fn = log_fn or (lambda msg, level="INFO": print(f"[HealthCheck] {msg}"))
        self._health_check_results: dict = {}

        t = threading.Thread(target=self._health_check_loop, daemon=True, name="token-health-checker")
        t.start()
        self._log_fn(f"🏥 Token health checker started (interval: {_HEALTH_CHECK_INTERVAL}s)", "INFO")

    def _health_check_loop(self):
        """Background loop: test all tokens periodically."""
        # Wait 30s after startup before first check
        time.sleep(30)
        while True:
            try:
                self._run_health_check()
            except Exception as e:
                self._log_fn(f"❌ Health check error: {e}", "ERROR")
            time.sleep(_HEALTH_CHECK_INTERVAL)

    def _run_health_check(self):
        """Test all enabled accounts, auto-disable those returning 401."""
        with self._lock:
            accounts = [a for a in self._accounts if a.enabled]

        if not accounts:
            return

        self._log_fn(f"🏥 Health check: testing {len(accounts)} enabled accounts...", "INFO")

        ok_count = 0
        fail_count = 0
        disabled_count = 0

        for acc in accounts:
            result = self.test_account(acc.id, self._upstream_url, self._origin)
            now = time.time()

            with self._lock:
                acc.last_health_check = now

                if result.get("ok"):
                    acc.last_health_ok = True
                    acc.token_valid = True
                    acc.failures = 0
                    acc.last_error = ""
                    acc.disabled_reason = ""
                    ok_count += 1
                else:
                    acc.last_health_ok = False
                    error_code = result.get("code", result.get("http_status", 0))
                    error_msg = result.get("message", result.get("error", "unknown"))

                    # 401 = token expired/invalid → auto-disable
                    if error_code == 401:
                        acc.token_valid = False
                        acc.enabled = False
                        acc.disabled_reason = "token_expired_401"
                        acc.last_error = "Token expired (auto-disabled)"
                        disabled_count += 1
                        self._log_fn(f"🔴 Auto-disabled {acc.name}: token expired (401)", "WARNING")
                    else:
                        acc.failures += 1
                        acc.last_error = str(error_msg)
                        fail_count += 1

            # Small delay between checks to avoid rate limiting
            time.sleep(1)

        # Save if any accounts were disabled
        if disabled_count > 0:
            with self._lock:
                self._save()

        # Store results summary
        self._health_check_results = {
            "last_check": time.time(),
            "total_checked": len(accounts),
            "ok": ok_count,
            "failed": fail_count,
            "auto_disabled": disabled_count,
        }

        self._log_fn(
            f"🏥 Health check done: ✓{ok_count} ✗{fail_count} 🔴{disabled_count} disabled",
            "INFO" if disabled_count == 0 else "WARNING"
        )

    def get_health_status(self) -> dict:
        """Return the most recent health check results."""
        return getattr(self, "_health_check_results", {})

    def purge_invalid(self) -> int:
        """Remove all accounts with invalid (expired) tokens."""
        with self._lock:
            before = len(self._accounts)
            self._accounts = [a for a in self._accounts if a.token_valid]
            removed = before - len(self._accounts)
            if removed > 0:
                self._save()
            return removed

    def reactivate_account(self, account_id: str, new_token: str = "") -> Account | None:
        """Re-enable a disabled account, optionally with a new token."""
        with self._lock:
            for acc in self._accounts:
                if acc.id == account_id:
                    if new_token:
                        acc.token = new_token
                    acc.enabled = True
                    acc.token_valid = True
                    acc.failures = 0
                    acc.last_error = ""
                    acc.disabled_reason = ""
                    self._save()
                    return acc
        return None

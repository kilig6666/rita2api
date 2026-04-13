"""
auto_register.py — Rita.ai 自动注册 & Token 获取模块

完整流程:
1. 使用 GPTMail 创建临时邮箱
2. 通过 accountapi.gosplit.net 注册 Rita.ai 账号
3. 使用 YesCaptcha 解决 reCAPTCHA Enterprise
4. 等待邮箱验证码并提交
5. 获取 token 并自动添加到 AccountManager

依赖: pip install requests curl_cffi
外部服务: YesCaptcha (reCAPTCHA), GPTMail (临时邮箱)
"""

import json
import os
import re
import time
import random
import string
import requests
import threading
from pathlib import Path

# curl_cffi for browser TLS fingerprint (anti-bot)
try:
    from curl_cffi import requests as curl_requests
    _HAS_CURL_CFFI = True
except ImportError:
    _HAS_CURL_CFFI = False

# ===================== Configuration =====================
_DISABLE_SSL_VERIFY = os.getenv("DISABLE_SSL_VERIFY", "0") == "1"

# YesCaptcha config
YESCAPTCHA_KEY = os.getenv("YESCAPTCHA_KEY", "")
YESCAPTCHA_API = "https://api.yescaptcha.com"
DEFAULT_OHMYCAPTCHA_LOCAL_CLIENT_KEY = "ohmycaptcha-local-key"
CAPTCHA_PROVIDER = os.getenv("CAPTCHA_PROVIDER", "yescaptcha")
OHMYCAPTCHA_LOCAL_API_URL = os.getenv("OHMYCAPTCHA_LOCAL_API_URL", "http://127.0.0.1:8001")
OHMYCAPTCHA_LOCAL_KEY = os.getenv("OHMYCAPTCHA_LOCAL_KEY", "")

# GPTMail config
GPTMAIL_API_KEY = os.getenv("GPTMAIL_API_KEY", "")
GPTMAIL_API_BASE = os.getenv("GPTMAIL_API_BASE", "https://mail.chatgpt.org.uk")

# YYDS Mail config
YYDSMAIL_API_KEY = os.getenv("YYDSMAIL_API_KEY", "")
YYDSMAIL_API_BASE = os.getenv("YYDSMAIL_API_BASE", "https://maliapi.215.im/v1")

# MoeMail config
MOEMAIL_API_KEY = os.getenv("MOEMAIL_API_KEY", "")
MOEMAIL_API_BASE = os.getenv("MOEMAIL_API_BASE", "")
MAIL_PROVIDER_DEFAULT = os.getenv("MAIL_PROVIDER_DEFAULT", "gptmail")
REGISTER_PROXY = os.getenv("REGISTER_PROXY", "")
MAIL_USE_PROXY = os.getenv("MAIL_USE_PROXY", "0") == "1"

# Rita.ai reCAPTCHA v2 sitekey (from account.rita.ai JS bundle)
RECAPTCHA_SITEKEY = "6Lej6N4hAAAAANgkiQRXxLrlue_J_y035Dm6UhPk"
RECAPTCHA_URL = "https://account.rita.ai"

# Gosplit auth API
GOSPLIT_API = "https://accountapi.gosplit.net"

# Auto-register settings
AUTO_REGISTER_ENABLED = os.getenv("AUTO_REGISTER_ENABLED", "0") == "1"
AUTO_REGISTER_MIN_ACTIVE = int(os.getenv("AUTO_REGISTER_MIN_ACTIVE", "2"))
AUTO_REGISTER_BATCH = int(os.getenv("AUTO_REGISTER_BATCH", "1"))
AUTO_REGISTER_PASSWORD = os.getenv("AUTO_REGISTER_PASSWORD", "@qazwsx123456")

# ===================== Logging =====================
_log_fn = None
_thread_local = threading.local()

def _log(msg, level="INFO"):
    thread_log_fn = getattr(_thread_local, "log_fn", None)
    if thread_log_fn:
        thread_log_fn(msg, level)
    elif _log_fn:
        _log_fn(msg, level)
    else:
        print(f"[AutoRegister] {msg}")


def set_thread_log_fn(log_fn=None):
    _thread_local.log_fn = log_fn


class RegistrationStopped(Exception):
    """手动注册被用户请求停止。"""

    def __init__(self, message: str = "Registration stopped", results: list[dict] | None = None):
        super().__init__(message)
        self.results = list(results or [])


def _should_stop(should_stop=None) -> bool:
    if not should_stop:
        return False
    try:
        return bool(should_stop())
    except Exception:
        return False


def _ensure_not_stopped(should_stop=None, stage: str = ""):
    if _should_stop(should_stop):
        suffix = f" @ {stage}" if stage else ""
        raise RegistrationStopped(f"Registration stopped by user{suffix}")


def _sleep_with_stop(seconds: float, should_stop=None, chunk: float = 0.25):
    deadline = time.time() + max(0.0, float(seconds or 0))
    while True:
        _ensure_not_stopped(should_stop, "sleep")
        remaining = deadline - time.time()
        if remaining <= 0:
            return
        time.sleep(min(chunk, remaining))


def _normalize_proxy_value(value: str = "") -> str:
    proxy = str(value or "").strip()
    if not proxy:
        return ""
    if "://" in proxy:
        return proxy
    return f"http://{proxy}"


def _build_http_proxies(proxy: str = "") -> dict | None:
    proxy_url = _normalize_proxy_value(proxy)
    if not proxy_url:
        return None
    return {"http": proxy_url, "https": proxy_url}


def _http_get(url: str, *, proxy: str = "", **kwargs):
    proxies = _build_http_proxies(proxy)
    if proxies:
        kwargs.setdefault("proxies", proxies)
    return requests.get(url, **kwargs)


def _http_post(url: str, *, proxy: str = "", **kwargs):
    proxies = _build_http_proxies(proxy)
    if proxies:
        kwargs.setdefault("proxies", proxies)
    return requests.post(url, **kwargs)


def _normalize_mail_provider(value: str = "") -> str:
    provider = str(value or "").strip().lower()
    if provider in {"gptmail", "yydsmail", "moemail"}:
        return provider
    fallback = str(MAIL_PROVIDER_DEFAULT or "gptmail").strip().lower()
    return fallback if fallback in {"gptmail", "yydsmail", "moemail"} else "gptmail"


def _parse_mail_api_payload(mail_provider: str, raw_value: str = "", cfg: dict | None = None) -> dict:
    provider = _normalize_mail_provider(mail_provider)
    raw_text = str(raw_value or "").strip()
    payload = {}
    if raw_text.startswith("{"):
        try:
            loaded = json.loads(raw_text)
            if isinstance(loaded, dict):
                payload = loaded
        except Exception:
            payload = {}
    elif raw_text:
        payload = {"api_key": raw_text}

    live_cfg = cfg or _get_live_config()
    if provider == "gptmail":
        payload.setdefault("api_key", raw_text or live_cfg.get("GPTMAIL_API_KEY", ""))
        payload.setdefault("api_base", live_cfg.get("GPTMAIL_API_BASE", ""))
    elif provider == "yydsmail":
        payload.setdefault("api_key", live_cfg.get("YYDSMAIL_API_KEY", ""))
        payload.setdefault("api_base", live_cfg.get("YYDSMAIL_API_BASE", ""))
        payload.setdefault("auth_credential", str(payload.get("token") or payload.get("auth_credential") or "").strip())
    elif provider == "moemail":
        payload.setdefault("api_key", live_cfg.get("MOEMAIL_API_KEY", ""))
        payload.setdefault("api_base", live_cfg.get("MOEMAIL_API_BASE", ""))
        payload.setdefault("auth_credential", str(payload.get("mailbox_id") or payload.get("auth_credential") or "").strip())
    return payload


def _serialize_mail_api_payload(mail_provider: str, payload: dict | None = None) -> str:
    provider = _normalize_mail_provider(mail_provider)
    if provider == "gptmail":
        return str((payload or {}).get("api_key") or "").strip()
    safe_payload = {k: v for k, v in (payload or {}).items() if v not in (None, "")}
    return json.dumps(safe_payload, ensure_ascii=False) if safe_payload else ""


def _get_default_mail_provider(cfg: dict | None = None) -> str:
    live_cfg = cfg or _get_live_config()
    return _normalize_mail_provider(live_cfg.get("MAIL_PROVIDER_DEFAULT"))


def _resolve_register_proxy(cfg: dict | None = None) -> str:
    live_cfg = cfg or _get_live_config()
    return _normalize_proxy_value(live_cfg.get("REGISTER_PROXY"))


def _mail_should_use_proxy(cfg: dict | None = None) -> bool:
    live_cfg = cfg or _get_live_config()
    value = live_cfg.get("MAIL_USE_PROXY", False)
    return value is True or str(value).strip().lower() in {"1", "true", "yes", "on"}


def _resolve_mail_proxy(cfg: dict | None = None) -> str:
    live_cfg = cfg or _get_live_config()
    return _resolve_register_proxy(live_cfg) if _mail_should_use_proxy(live_cfg) else ""


def _is_mail_provider_configured(mail_provider: str, cfg: dict | None = None) -> bool:
    live_cfg = cfg or _get_live_config()
    provider = _normalize_mail_provider(mail_provider)
    if provider == "gptmail":
        return bool(live_cfg.get("GPTMAIL_API_KEY"))
    if provider == "yydsmail":
        return bool(live_cfg.get("YYDSMAIL_API_KEY"))
    if provider == "moemail":
        return get_moemail_channel_stats(live_cfg)["configured"]
    return False


def _get_mail_provider_missing_keys(mail_provider: str, cfg: dict | None = None) -> list[str]:
    live_cfg = cfg or _get_live_config()
    provider = _normalize_mail_provider(mail_provider)
    missing = []
    if provider == "gptmail":
        if not live_cfg.get("GPTMAIL_API_KEY"):
            missing.append("GPTMAIL_API_KEY")
    elif provider == "yydsmail":
        if not live_cfg.get("YYDSMAIL_API_KEY"):
            missing.append("YYDSMAIL_API_KEY")
    elif provider == "moemail":
        moe_stats = get_moemail_channel_stats(live_cfg)
        if moe_stats["configured"]:
            return []
        if str(live_cfg.get("MOEMAIL_CHANNELS_JSON") or "").strip():
            missing.append("MOEMAIL_CHANNELS_JSON")
        else:
            if not live_cfg.get("MOEMAIL_API_KEY"):
                missing.append("MOEMAIL_API_KEY")
            if not live_cfg.get("MOEMAIL_API_BASE"):
                missing.append("MOEMAIL_API_BASE")
    return missing


_moemail_rr_lock = threading.Lock()
_moemail_rr_cursor = 0


def _parse_moemail_channels(cfg: dict | None = None) -> tuple[list[dict], str]:
    live_cfg = cfg or _get_live_config()
    raw = str(live_cfg.get("MOEMAIL_CHANNELS_JSON") or "").strip()
    if not raw:
        return [], ""
    try:
        loaded = json.loads(raw)
    except Exception as e:
        return [], str(e)
    if not isinstance(loaded, list):
        return [], "MOEMAIL_CHANNELS_JSON must be a JSON array"

    channels = []
    for idx, item in enumerate(loaded, start=1):
        if not isinstance(item, dict):
            continue
        channels.append({
            "id": str(item.get("id") or f"moemail-{idx}").strip() or f"moemail-{idx}",
            "name": str(item.get("name") or f"MoeMail {idx}").strip() or f"MoeMail {idx}",
            "enabled": bool(item.get("enabled", True)),
            "api_key": str(item.get("api_key") or "").strip(),
            "api_base": str(item.get("api_base") or "").strip().rstrip("/"),
        })
    return channels, ""


def get_moemail_channel_stats(cfg: dict | None = None) -> dict:
    live_cfg = cfg or _get_live_config()
    channels, parse_error = _parse_moemail_channels(live_cfg)
    if channels:
        enabled_channels = [c for c in channels if c["enabled"] and c["api_key"] and c["api_base"]]
        return {
            "configured": bool(enabled_channels),
            "using_json": True,
            "total": len(channels),
            "enabled": len(enabled_channels),
            "parse_error": parse_error,
        }

    legacy_ready = bool(live_cfg.get("MOEMAIL_API_KEY") and live_cfg.get("MOEMAIL_API_BASE"))
    return {
        "configured": legacy_ready,
        "using_json": False,
        "total": 1 if legacy_ready else 0,
        "enabled": 1 if legacy_ready else 0,
        "parse_error": parse_error,
    }


def _get_legacy_moemail_channel(cfg: dict | None = None) -> dict | None:
    live_cfg = cfg or _get_live_config()
    api_key = str(live_cfg.get("MOEMAIL_API_KEY") or "").strip()
    api_base = str(live_cfg.get("MOEMAIL_API_BASE") or "").strip().rstrip("/")
    if not api_key or not api_base:
        return None
    return {
        "id": "legacy-moemail",
        "name": "Legacy MoeMail",
        "enabled": True,
        "api_key": api_key,
        "api_base": api_base,
    }


def _get_moemail_channel_candidates(cfg: dict | None = None) -> list[dict]:
    global _moemail_rr_cursor
    live_cfg = cfg or _get_live_config()
    channels, _ = _parse_moemail_channels(live_cfg)
    eligible = [c for c in channels if c["enabled"] and c["api_key"] and c["api_base"]]
    if eligible:
        with _moemail_rr_lock:
            start = _moemail_rr_cursor % len(eligible)
            ordered = eligible[start:] + eligible[:start]
            _moemail_rr_cursor = (_moemail_rr_cursor + 1) % len(eligible)
        return ordered

    legacy = _get_legacy_moemail_channel(live_cfg)
    return [legacy] if legacy else []


# ===================== Live Config (DB > env) =====================
def _get_live_config() -> dict:
    """Read config from database first, fallback to module-level env vars."""
    try:
        from database import get_db
        db = get_db()
        return {
            "YESCAPTCHA_KEY": db.get_config("YESCAPTCHA_KEY") or YESCAPTCHA_KEY,
            "CAPTCHA_PROVIDER": db.get_config("CAPTCHA_PROVIDER") or CAPTCHA_PROVIDER,
            "OHMYCAPTCHA_LOCAL_API_URL": db.get_config("OHMYCAPTCHA_LOCAL_API_URL") or OHMYCAPTCHA_LOCAL_API_URL,
            "OHMYCAPTCHA_LOCAL_KEY": db.get_config("OHMYCAPTCHA_LOCAL_KEY") or OHMYCAPTCHA_LOCAL_KEY,
            "MAIL_PROVIDER_DEFAULT": db.get_config("MAIL_PROVIDER_DEFAULT") or MAIL_PROVIDER_DEFAULT,
            "GPTMAIL_API_KEY": db.get_config("GPTMAIL_API_KEY") or GPTMAIL_API_KEY,
            "GPTMAIL_API_BASE": db.get_config("GPTMAIL_API_BASE") or GPTMAIL_API_BASE,
            "YYDSMAIL_API_KEY": db.get_config("YYDSMAIL_API_KEY") or YYDSMAIL_API_KEY,
            "YYDSMAIL_API_BASE": db.get_config("YYDSMAIL_API_BASE") or YYDSMAIL_API_BASE,
            "MOEMAIL_API_KEY": db.get_config("MOEMAIL_API_KEY") or MOEMAIL_API_KEY,
            "MOEMAIL_API_BASE": db.get_config("MOEMAIL_API_BASE") or MOEMAIL_API_BASE,
            "MOEMAIL_CHANNELS_JSON": db.get_config("MOEMAIL_CHANNELS_JSON", ""),
            "REGISTER_PROXY": db.get_config("REGISTER_PROXY") or REGISTER_PROXY,
            "MAIL_USE_PROXY": db.get_config("MAIL_USE_PROXY", "0") == "1" or MAIL_USE_PROXY,
            "AUTO_REGISTER_ENABLED": db.get_config("AUTO_REGISTER_ENABLED", "0") == "1" or AUTO_REGISTER_ENABLED,
            "AUTO_REGISTER_MIN_ACTIVE": int(db.get_config("AUTO_REGISTER_MIN_ACTIVE") or AUTO_REGISTER_MIN_ACTIVE),
            "AUTO_REGISTER_BATCH": int(db.get_config("AUTO_REGISTER_BATCH") or AUTO_REGISTER_BATCH),
            "AUTO_REGISTER_PASSWORD": db.get_config("AUTO_REGISTER_PASSWORD") or AUTO_REGISTER_PASSWORD,
            "DISABLE_SSL_VERIFY": db.get_config("DISABLE_SSL_VERIFY", "0") == "1" or _DISABLE_SSL_VERIFY,
        }
    except Exception:
        return {
            "YESCAPTCHA_KEY": YESCAPTCHA_KEY,
            "CAPTCHA_PROVIDER": CAPTCHA_PROVIDER,
            "OHMYCAPTCHA_LOCAL_API_URL": OHMYCAPTCHA_LOCAL_API_URL,
            "OHMYCAPTCHA_LOCAL_KEY": OHMYCAPTCHA_LOCAL_KEY,
            "MAIL_PROVIDER_DEFAULT": MAIL_PROVIDER_DEFAULT,
            "GPTMAIL_API_KEY": GPTMAIL_API_KEY,
            "GPTMAIL_API_BASE": GPTMAIL_API_BASE,
            "YYDSMAIL_API_KEY": YYDSMAIL_API_KEY,
            "YYDSMAIL_API_BASE": YYDSMAIL_API_BASE,
            "MOEMAIL_API_KEY": MOEMAIL_API_KEY,
            "MOEMAIL_API_BASE": MOEMAIL_API_BASE,
            "MOEMAIL_CHANNELS_JSON": os.getenv("MOEMAIL_CHANNELS_JSON", ""),
            "REGISTER_PROXY": REGISTER_PROXY,
            "MAIL_USE_PROXY": MAIL_USE_PROXY,
            "AUTO_REGISTER_ENABLED": AUTO_REGISTER_ENABLED,
            "AUTO_REGISTER_MIN_ACTIVE": AUTO_REGISTER_MIN_ACTIVE,
            "AUTO_REGISTER_BATCH": AUTO_REGISTER_BATCH,
            "AUTO_REGISTER_PASSWORD": AUTO_REGISTER_PASSWORD,
            "DISABLE_SSL_VERIFY": _DISABLE_SSL_VERIFY,
        }


# ===================== GPTMail — Temporary Email =====================
def _gptmail_headers(api_key=""):
    h = {"Accept": "application/json"}
    key = api_key or _get_live_config()["GPTMAIL_API_KEY"]
    if key:
        h["X-API-Key"] = key
    return h


def create_temp_email(mail_provider: str = "") -> str:
    """Create a temporary email via the selected provider. Returns email address."""
    mailbox = create_temp_email_by_provider(mail_provider or _get_default_mail_provider())
    return str(mailbox.get("email") or "")


def _gptmail_get_detail(api_base, headers, mail_id, ssl_verify):
    """Fetch a single email's detail from GPTMail. Returns parsed dict or None."""
    cfg = _get_live_config()
    try:
        r = _http_get(
            f"{api_base}/api/email/{mail_id}",
            headers=headers,
            timeout=15,
            verify=ssl_verify,
            proxy=_resolve_mail_proxy(cfg),
        )
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def _moemail_headers(api_key: str = "") -> dict:
    cfg = _get_live_config()
    key = api_key or cfg["MOEMAIL_API_KEY"]
    headers = {"Accept": "application/json"}
    if key:
        headers["X-API-Key"] = key
    return headers


def _moemail_get_domain(api_base: str, headers: dict, ssl_verify: bool) -> str:
    cfg = _get_live_config()
    resp = _http_get(
        f"{api_base.rstrip('/')}/api/config",
        headers=headers,
        timeout=10,
        verify=ssl_verify,
        proxy=_resolve_mail_proxy(cfg),
    )
    resp.raise_for_status()
    data = resp.json()
    domains_str = str(data.get("emailDomains") or "").strip()
    domains = [item.strip() for item in domains_str.split(",") if item.strip()]
    if not domains:
        raise Exception("MoeMail create failed: no domains available")
    return random.choice(domains)


def _moemail_create_email(api_key: str = "", api_base: str = "") -> tuple[str, str]:
    cfg = _get_live_config()
    base = str(api_base or cfg["MOEMAIL_API_BASE"] or "").strip().rstrip("/")
    if not base:
        raise Exception("MOEMAIL_API_BASE not configured")
    headers = _moemail_headers(api_key)
    ssl_verify = not cfg["DISABLE_SSL_VERIFY"]
    domain = _moemail_get_domain(base, headers, ssl_verify)
    prefix = "".join(random.choices(string.ascii_lowercase + string.digits, k=random.randint(8, 13)))
    resp = _http_post(
        f"{base}/api/emails/generate",
        json={"name": prefix, "domain": domain, "expiryTime": 0},
        headers=headers,
        timeout=15,
        verify=ssl_verify,
        proxy=_resolve_mail_proxy(cfg),
    )
    resp.raise_for_status()
    data = resp.json()
    email = str(data.get("email") or "").strip()
    auth_credential = str(data.get("id") or "").strip()
    if not email or not auth_credential:
        raise Exception(f"MoeMail create failed: {data}")
    return email, auth_credential


def create_temp_email_by_provider(mail_provider: str = "") -> dict:
    cfg = _get_live_config()
    provider = _normalize_mail_provider(mail_provider or cfg.get("MAIL_PROVIDER_DEFAULT"))
    if provider == "yydsmail":
        email, auth_credential = _yydsmail_create_email()
        payload = {
            "api_key": cfg["YYDSMAIL_API_KEY"],
            "api_base": cfg["YYDSMAIL_API_BASE"],
            "auth_credential": auth_credential,
        }
        return {
            "email": email,
            "auth_credential": auth_credential,
            "mail_provider": "yydsmail",
            "mail_api_key": _serialize_mail_api_payload("yydsmail", payload),
            "channel_label": "YYDSMail",
            "api_base": cfg["YYDSMAIL_API_BASE"],
        }
    if provider == "moemail":
        candidates = _get_moemail_channel_candidates(cfg)
        if not candidates:
            raise Exception("MoeMail create failed: no enabled channel configured")
        last_error = None
        for idx, channel in enumerate(candidates, start=1):
            _log(
                f"📮 MoeMail channel {channel['name']} create mailbox ({idx}/{len(candidates)})...",
                "INFO",
            )
            try:
                email, auth_credential = _moemail_create_email(channel["api_key"], channel["api_base"])
                payload = {
                    "api_key": channel["api_key"],
                    "api_base": channel["api_base"],
                    "auth_credential": auth_credential,
                    "channel_id": channel["id"],
                    "channel_name": channel["name"],
                }
                return {
                    "email": email,
                    "auth_credential": auth_credential,
                    "mail_provider": "moemail",
                    "mail_api_key": _serialize_mail_api_payload("moemail", payload),
                    "channel_label": f"MoeMail/{channel['name']}",
                    "api_base": channel["api_base"],
                    "channel_id": channel["id"],
                    "channel_name": channel["name"],
                }
            except Exception as e:
                last_error = e
                _log(f"⚠️ MoeMail channel {channel['name']} create failed: {e}", "WARNING")
        raise Exception(f"MoeMail create failed across {len(candidates)} channel(s): {last_error}")

    resp = _http_post(
        f"{cfg['GPTMAIL_API_BASE']}/api/generate-email",
        headers=_gptmail_headers(cfg["GPTMAIL_API_KEY"]),
        json={},
        timeout=15,
        verify=not cfg["DISABLE_SSL_VERIFY"],
        proxy=_resolve_mail_proxy(cfg),
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise Exception(f"GPTMail create failed: {data}")
    email = str(data["data"]["email"]).strip()
    return {
        "email": email,
        "auth_credential": "",
        "mail_provider": "gptmail",
        "mail_api_key": _serialize_mail_api_payload("gptmail", {"api_key": cfg["GPTMAIL_API_KEY"]}),
        "channel_label": "GPTMail",
        "api_base": cfg["GPTMAIL_API_BASE"],
    }


def wait_for_verification_code(email: str, timeout: int = 120, should_stop=None) -> str | None:
    """Poll GPTMail for verification code.
    Optimized: extract from subject first (fast path), then from content/html.
    """
    cfg = _get_live_config()
    hdrs = _gptmail_headers(cfg["GPTMAIL_API_KEY"])
    api_base = cfg["GPTMAIL_API_BASE"]
    ssl_verify = not cfg["DISABLE_SSL_VERIFY"]

    start = time.time()
    while time.time() - start < timeout:
        _ensure_not_stopped(should_stop, "gptmail_poll")
        try:
            resp = _http_get(
                f"{api_base}/api/emails",
                params={"email": email},
                headers=hdrs,
                timeout=15,
                verify=ssl_verify,
                proxy=_resolve_mail_proxy(cfg),
            )
            if resp.status_code == 200:
                data = resp.json()
                emails_list = data.get("data", {}).get("emails", [])
                if not emails_list:
                    raw = data.get("data")
                    emails_list = raw if isinstance(raw, list) else []

                for mail in emails_list:
                    # Fast path: try subject first (no extra request)
                    code = _extract_code(mail.get("subject", ""))
                    if code:
                        return code

                    # Slow path: fetch detail
                    mail_id = mail.get("id")
                    if not mail_id:
                        continue
                    detail = _gptmail_get_detail(api_base, hdrs, mail_id, ssl_verify)
                    if detail:
                        d = detail.get("data", {})
                        content = d.get("content", "") or ""
                        html = d.get("html_content", "") or ""
                        code = _extract_code(content) or _extract_code(html)
                        if code:
                            return code
        except Exception as e:
            _log(f"📧 Mail poll error: {e}", "DEBUG")

        elapsed = int(time.time() - start)
        _log(f"📧 Waiting for verification code... ({elapsed}s/{timeout}s)", "DEBUG")
        _sleep_with_stop(OTP_POLL_INTERVAL, should_stop)

    return None


def _extract_code(content) -> str | None:
    """Extract 4-6 digit verification code from email content.
    Handles str and list content. Multi-pattern matching.
    """
    if not content:
        return None
    # Handle list content (some mail APIs return lists)
    if isinstance(content, list):
        content = " ".join(str(c) for c in content)
    if not isinstance(content, str):
        content = str(content)

    patterns = [
        r"(?:verification|verify|code|验证码|验证|代码)[\s:：]*(\d{4,6})",
        r">\s*(\d{4,6})\s*<",
        r"\b(\d{4,6})\b",
    ]
    for pat in patterns:
        matches = re.findall(pat, content, re.IGNORECASE)
        for code in matches:
            if len(code) >= 4 and not code.startswith("0"):
                return code
    return None


# ===================== YesCaptcha — reCAPTCHA Enterprise Solver =====================

# 任务类型优先级：优先恢复历史稳定链路，标准 V2 先跑，Enterprise 仅作兜底
_RECAPTCHA_TASK_TYPES = [
    "NoCaptchaTaskProxyless",              # 历史稳定链路，优先尝试
    "RecaptchaV2EnterpriseTaskProxyless",  # 企业版兜底
]


_RECAPTCHA_PROVIDER_LABELS = {
    "yescaptcha": "YesCaptcha",
    "ohmycaptcha_local": "OhMyCaptcha Local",
}


def normalize_recaptcha_provider(value: str = "") -> str:
    provider = str(value or "").strip().lower()
    if provider in {"ohmycaptcha", "ohmycaptcha-local", "ohmycaptcha_local"}:
        return "ohmycaptcha_local"
    return "yescaptcha"


def _resolve_recaptcha_provider_config(cfg: dict | None = None, provider_override: str = "") -> dict:
    live_cfg = cfg or _get_live_config()
    provider = normalize_recaptcha_provider(provider_override or live_cfg.get("CAPTCHA_PROVIDER"))
    if provider == "ohmycaptcha_local":
        api_url = str(
            live_cfg.get("OHMYCAPTCHA_LOCAL_API_URL")
            or OHMYCAPTCHA_LOCAL_API_URL
            or "http://127.0.0.1:8001"
        ).strip() or "http://127.0.0.1:8001"
        client_key = str(
            live_cfg.get("OHMYCAPTCHA_LOCAL_KEY")
            or OHMYCAPTCHA_LOCAL_KEY
            or DEFAULT_OHMYCAPTCHA_LOCAL_CLIENT_KEY
        ).strip() or DEFAULT_OHMYCAPTCHA_LOCAL_CLIENT_KEY
    else:
        provider = "yescaptcha"
        api_url = YESCAPTCHA_API
        client_key = str(live_cfg.get("YESCAPTCHA_KEY") or YESCAPTCHA_KEY).strip()
    return {
        "provider": provider,
        "label": _RECAPTCHA_PROVIDER_LABELS.get(provider, provider),
        "api_url": api_url.rstrip("/"),
        "client_key": client_key,
    }


def probe_recaptcha_provider(raw: dict | None = None) -> dict:
    """测试当前打码服务连通性。支持传入未保存的表单值覆盖。"""
    live_cfg = _get_live_config()
    source = raw if isinstance(raw, dict) else {}
    provider_override = str(source.get("provider") or source.get("captcha_provider") or "").strip()
    cfg = {
        **live_cfg,
        "CAPTCHA_PROVIDER": provider_override or live_cfg.get("CAPTCHA_PROVIDER"),
        "YESCAPTCHA_KEY": str(source.get("yescaptcha_key") or source.get("YESCAPTCHA_KEY") or live_cfg.get("YESCAPTCHA_KEY") or "").strip(),
        "OHMYCAPTCHA_LOCAL_API_URL": str(
            source.get("ohmycaptcha_local_api_url")
            or source.get("OHMYCAPTCHA_LOCAL_API_URL")
            or live_cfg.get("OHMYCAPTCHA_LOCAL_API_URL")
            or ""
        ).strip(),
        "OHMYCAPTCHA_LOCAL_KEY": str(
            source.get("ohmycaptcha_local_key")
            or source.get("OHMYCAPTCHA_LOCAL_KEY")
            or live_cfg.get("OHMYCAPTCHA_LOCAL_KEY")
            or ""
        ).strip(),
        "REGISTER_PROXY": str(source.get("register_proxy") or source.get("REGISTER_PROXY") or live_cfg.get("REGISTER_PROXY") or "").strip(),
        "DISABLE_SSL_VERIFY": (
            str(source.get("disable_ssl_verify") or source.get("DISABLE_SSL_VERIFY") or "").strip().lower() in {"1", "true", "yes", "on"}
            if source.get("disable_ssl_verify") is not None or source.get("DISABLE_SSL_VERIFY") is not None
            else bool(live_cfg.get("DISABLE_SSL_VERIFY"))
        ),
    }
    provider_cfg = _resolve_recaptcha_provider_config(cfg, provider_override)
    provider = provider_cfg["provider"]
    provider_label = provider_cfg["label"]
    api_url = provider_cfg["api_url"]
    client_key = provider_cfg["client_key"]
    ssl_verify = not bool(cfg.get("DISABLE_SSL_VERIFY"))
    register_proxy = "" if provider == "ohmycaptcha_local" else _resolve_register_proxy(cfg)

    result = {
        "ok": False,
        "provider": provider,
        "provider_label": provider_label,
        "api_url": api_url,
        "using_proxy": bool(register_proxy),
        "proxy": register_proxy,
        "health": None,
        "balance": None,
        "error": "",
        "message": "",
    }

    if not api_url:
        result["error"] = "API URL 未配置"
        result["message"] = f"{provider_label} 测试失败：{result['error']}"
        return result
    if not client_key:
        result["error"] = "client key 未配置"
        result["message"] = f"{provider_label} 测试失败：{result['error']}"
        return result

    errors = []
    health_ok = False
    balance_ok = False

    if provider == "ohmycaptcha_local":
        try:
            health_resp = _http_get(
                f"{api_url}/api/v1/health",
                timeout=10,
                verify=ssl_verify,
            )
            health_ok = health_resp.ok
            try:
                result["health"] = health_resp.json()
            except Exception:
                result["health"] = {"status_code": health_resp.status_code, "text": health_resp.text[:300]}
            if not health_ok:
                errors.append(f"/api/v1/health 返回 {health_resp.status_code}")
        except Exception as e:
            errors.append(f"/api/v1/health 不可用: {e}")

    try:
        balance_resp = _http_post(
            f"{api_url}/getBalance",
            json={"clientKey": client_key},
            timeout=15,
            verify=ssl_verify,
            proxy=register_proxy,
        )
        balance_resp.raise_for_status()
        result["balance"] = balance_resp.json()
        balance_ok = int((result["balance"] or {}).get("errorId") or 0) == 0
        if not balance_ok:
            errors.append(
                str(
                    (result["balance"] or {}).get("errorDescription")
                    or (result["balance"] or {}).get("errorCode")
                    or "getBalance 返回失败"
                ).strip()
            )
    except Exception as e:
        errors.append(f"/getBalance 失败: {e}")

    result["ok"] = balance_ok and (health_ok or provider != "ohmycaptcha_local")
    result["error"] = "; ".join(item for item in errors if item)
    if result["ok"]:
        extra = "，已通过 /api/v1/health 与 /getBalance" if provider == "ohmycaptcha_local" else "，已通过 /getBalance"
        result["message"] = f"{provider_label} 连通正常{extra}"
    else:
        result["message"] = f"{provider_label} 测试失败：{result['error'] or 'unknown'}"
    return result


def _solve_one_type(provider_cfg: dict, task_type: str, ssl_verify: bool) -> str:
    """用指定 provider + task_type 执行一次完整的 reCAPTCHA 验证流程."""
    cfg = _get_live_config()
    provider = str(provider_cfg.get("provider") or "yescaptcha").strip()
    provider_label = str(provider_cfg.get("label") or provider).strip()
    api_url = str(provider_cfg.get("api_url") or "").strip().rstrip("/")
    client_key = str(provider_cfg.get("client_key") or "").strip()

    if not api_url:
        raise Exception(f"{provider_label} API URL 未配置")
    if not client_key:
        if provider == "yescaptcha":
            raise Exception("YESCAPTCHA_KEY not configured")
        raise Exception(f"{provider_label} client key 未配置")

    # 本地打码默认不走 Rita 注册代理，避免 127.0.0.1 被错误转发到外部代理。
    register_proxy = "" if provider == "ohmycaptcha_local" else _resolve_register_proxy(cfg)
    task_payload = {
        "type": task_type,
        "websiteURL": RECAPTCHA_URL,
        "websiteKey": RECAPTCHA_SITEKEY,
    }

    # reCAPTCHA Enterprise 需要额外参数 (from HAR 抓包)
    if task_type == "RecaptchaV2EnterpriseTaskProxyless":
        task_payload["enterprisePayload"] = {
            "s": "ENTERPRISE",
            "co": "aHR0cHM6Ly9hY2NvdW50LnJpdGEuYWk6NDQz",
            "hl": "zh-CN",
        }
        task_payload["apiDomain"] = "https://www.google.com/recaptcha/enterprise.js"

    # Create task
    create_resp = _http_post(
        f"{api_url}/createTask",
        json={"clientKey": client_key, "task": task_payload},
        timeout=30,
        verify=ssl_verify,
        proxy=register_proxy,
    )
    create_resp.raise_for_status()
    create_data = create_resp.json()

    if create_data.get("errorId", 0) != 0:
        raise Exception(f"[{create_data.get('errorCode')}] {create_data.get('errorDescription', create_data)}")

    task_id = create_data.get("taskId")
    if not task_id:
        raise Exception(f"{provider_label} createTask failed: {create_data}")

    _log(f"🔐 {provider_label} reCAPTCHA task created: {task_id} (type={task_type})", "DEBUG")

    # Poll for result (max 120s, interval 1s)
    for attempt in range(120):
        time.sleep(1)
        try:
            result_resp = _http_post(
                f"{api_url}/getTaskResult",
                json={"clientKey": client_key, "taskId": task_id},
                timeout=15,
                verify=ssl_verify,
                proxy=register_proxy,
            )
            result_resp.raise_for_status()
            result_data = result_resp.json()

            if result_data.get("errorId", 0) != 0:
                raise Exception(f"[{result_data.get('errorCode')}] {result_data.get('errorDescription', result_data)}")

            status = result_data.get("status", "")
            if status == "ready":
                solution = result_data.get("solution", {})
                token = (
                    solution.get("gRecaptchaResponse")
                    or solution.get("g-recaptcha-response")
                    or solution.get("token", "")
                )
                if token:
                    _log(f"🔐 {provider_label} reCAPTCHA solved! ({len(token)} chars, {attempt + 1}s)", "DEBUG")
                    return token
                raise Exception(f"ready but no token: {result_data}")

            if status == "failed":
                raise Exception(f"{provider_label} failed: {result_data}")

            if attempt % 10 == 0:
                _log(f"🔐 {provider_label} waiting... status={status} ({attempt + 1}s)", "DEBUG")

        except requests.RequestException as e:
            if attempt % 10 == 0:
                _log(f"🔐 {provider_label} network error: {e} ({attempt + 1}s)", "DEBUG")

    raise Exception(f"{provider_label} timeout (120s) for {task_type}")


def solve_recaptcha(provider_override: str = "") -> str | None:
    """Solve reCAPTCHA Enterprise via selected provider.
    Tries Enterprise task type first, falls back to standard V2.
    Returns g-recaptcha-response token.
    """
    cfg = _get_live_config()
    provider_cfg = _resolve_recaptcha_provider_config(cfg, provider_override)
    ssl_verify = not cfg["DISABLE_SSL_VERIFY"]

    last_error = None
    for task_type in _RECAPTCHA_TASK_TYPES:
        _log(f"🔐 Trying {provider_cfg['label']} task type: {task_type}", "DEBUG")
        try:
            return _solve_one_type(provider_cfg, task_type, ssl_verify)
        except Exception as e:
            last_error = e
            _log(f"🔐 {provider_cfg['label']} {task_type} failed: {e}", "WARNING")

    raise Exception(f"All {provider_cfg['label']} task types failed. Last error: {last_error}")


# ===================== Browser Fingerprint (anti-bot) =====================
_CHROME_IMPERSONATE = "chrome120"

_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.6099.{patch} Safari/537.36"
)

_SEC_CH_UA = '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"'

_ACCEPT_LANGUAGES = [
    "zh-CN,zh;q=0.9,en;q=0.8",
    "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
]


def _random_browser_headers() -> dict:
    """Generate Chrome-like browser headers to pass Rita anti-bot checks."""
    patch = random.randint(109, 234)
    ua = _CHROME_UA.format(patch=patch)
    return {
        "User-Agent": ua,
        "Accept-Language": random.choice(_ACCEPT_LANGUAGES),
        "sec-ch-ua": _SEC_CH_UA,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
    }


def _create_rita_session(ssl_verify: bool = True, proxy: str = ""):
    """Create an HTTP session with browser TLS fingerprint.
    Uses curl_cffi for Chrome TLS fingerprint if available, falls back to requests.
    """
    proxies = _build_http_proxies(proxy)
    if _HAS_CURL_CFFI:
        session = curl_requests.Session(impersonate=_CHROME_IMPERSONATE, proxies=proxies)
        try:
            session.verify = ssl_verify
        except Exception:
            pass
        try:
            session.trust_env = False
        except Exception:
            pass
        return session, _CHROME_IMPERSONATE
    else:
        _log("⚠️ curl_cffi not available, using plain requests (may be detected as bot)", "WARNING")
        session = requests.Session()
        session.verify = ssl_verify
        session.trust_env = False
        if proxies:
            session.proxies = proxies
        return session, None


# ===================== Rita.ai Registration Flow =====================
def _gosplit_headers():
    return {
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://account.rita.ai",
        "Referer": "https://account.rita.ai/",
        **_random_browser_headers(),
    }


def _update_session_headers(headers: dict, resp_data: dict):
    """Extract token/visitorid from API response and update headers.
    Rita API requires these headers to maintain session state across steps.
    """
    if not isinstance(resp_data, dict):
        return
    data = resp_data.get("data", resp_data)
    if not isinstance(data, dict):
        return
    for key in ("token", "access_token", "session_token"):
        t = data.get(key, "")
        if t and isinstance(t, str) and len(t) > 8:
            headers["token"] = t
            _log(f"   -> session token: {t[:8]}***{t[-4:]}", "DEBUG")
            break
    for key in ("visitorid", "visitor_id"):
        v = data.get(key, "")
        if v and isinstance(v, str) and len(v) > 8:
            headers["visitorid"] = v
            _log(f"   -> visitorid: {v[:8]}***", "DEBUG")
            break


# ===================== Registration Constants =====================
MAX_CAPTCHA_ATTEMPTS = 4      # captcha 提交最多重试次数
MAX_RESEND_ATTEMPTS = 2       # OTP 邮件最多重发次数
OTP_WAIT_TIMEOUT = 90         # 每轮 OTP 等待超时（秒）
OTP_POLL_INTERVAL = 3         # OTP 轮询间隔（秒）


def register_rita_account(
    email: str,
    mail_provider: str = "",
    mail_api_key: str = "",
    captcha_provider_override: str = "",
    should_stop=None,
) -> dict:
    """
    Full registration flow for Rita.ai.
    Key: tracks session token/visitorid from each response and passes
    them in subsequent request headers (required by Rita API).

    Returns: {"token": "...", "email": "...", "ticket": "..."}
    Raises: Exception on failure
    """
    _ensure_not_stopped(should_stop, "register_start")
    cfg = _get_live_config()
    register_proxy = _resolve_register_proxy(cfg)
    session, impersonate = _create_rita_session(
        ssl_verify=not cfg["DISABLE_SSL_VERIFY"],
        proxy=register_proxy,
    )
    headers = _gosplit_headers()
    redirect_uri = "https://www.rita.ai/zh/ai-chat"

    def _post(path, payload):
        """POST helper that auto-updates session headers from response."""
        _ensure_not_stopped(should_stop, path)
        kwargs = {"headers": headers, "json": payload, "timeout": 30}
        if impersonate:
            kwargs["impersonate"] = impersonate
        r = session.post(f"{GOSPLIT_API}{path}", **kwargs)
        try:
            resp = r.json()
        except Exception:
            resp = {"_raw": r.text[:500], "_status": r.status_code}
        _update_session_headers(headers, resp)
        return resp, r

    # ---- Step 1: authenticate (init session) ----
    _log("Step 1/6: authenticate (init)", "DEBUG")
    resp, _ = _post("/authorize/authenticate", {"redirect_uri": redirect_uri})

    # ---- Step 2: sign_process (email + agree) ----
    _log(f"Step 2/6: sign_process (email={email})", "DEBUG")
    resp, _ = _post("/authorize/sign_process", {
        "redirect_uri": redirect_uri, "language": "zh",
        "email": email, "agree": 1,
    })
    _log(f"   code={resp.get('code', '?')} need_captcha={resp.get('data', {}).get('need_captcha', 0)}", "DEBUG")

    # Human-like delay before captcha
    _sleep_with_stop(random.uniform(2.0, 4.0), should_stop)

    # ---- Step 3: solve reCAPTCHA (with retry) ----
    captcha_token = None
    if resp.get("data", {}).get("need_captcha"):
        for cap_attempt in range(1, MAX_CAPTCHA_ATTEMPTS + 1):
            if cap_attempt > 1:
                _log(f"   captcha submit failed, retrying ({cap_attempt}/{MAX_CAPTCHA_ATTEMPTS})...", "WARNING")
                _sleep_with_stop(random.uniform(5.0, 8.0), should_stop)

            _log(f"Step 3/6: solving reCAPTCHA (attempt {cap_attempt})...", "INFO")
            try:
                captcha_token = solve_recaptcha(captcha_provider_override)
            except Exception as e:
                _log(f"   reCAPTCHA solve error: {e}", "WARNING")
                if cap_attempt >= MAX_CAPTCHA_ATTEMPTS:
                    raise Exception(f"reCAPTCHA failed after {MAX_CAPTCHA_ATTEMPTS} attempts: {e}")
                continue

            if not captcha_token:
                if cap_attempt >= MAX_CAPTCHA_ATTEMPTS:
                    raise Exception(f"reCAPTCHA failed after {MAX_CAPTCHA_ATTEMPTS} attempts")
                continue

            # Submit captcha with human-like delay
            _sleep_with_stop(random.uniform(1.5, 3.5), should_stop)
            resp, _ = _post("/authorize/sign_process", {
                "redirect_uri": redirect_uri, "language": "zh",
                "email": email, "agree": 1,
                "g-recaptcha-response": captcha_token,
            })
            resp_code = resp.get("code", -1)
            resp_type = resp.get("type", "")
            _log(f"   code={resp_code} type={resp_type}", "DEBUG")

            if resp_code == 0 and resp_type == "success":
                _log("   captcha verified!", "DEBUG")
                break
            elif cap_attempt >= MAX_CAPTCHA_ATTEMPTS:
                raise Exception(f"captcha verification failed after {MAX_CAPTCHA_ATTEMPTS} attempts: {resp}")
    else:
        _log("Step 3/6: no captcha needed, skipped", "DEBUG")

    _sleep_with_stop(random.uniform(0.5, 1.5), should_stop)

    # ---- Step 4: trigger emailCode (explicit send) ----
    _log("Step 4/6: emailCode (send OTP)...", "INFO")
    ec_payload = {"email": email, "redirect_uri": redirect_uri, "language": "zh"}
    if captcha_token:
        ec_payload["g-recaptcha-response"] = captcha_token
    resp, _ = _post("/authorize/emailCode", ec_payload)
    ec_code = resp.get("code", -1)
    _log(f"   code={ec_code} type={resp.get('type', '?')}", "DEBUG")
    # emailCode may return non-0 but OTP was still sent
    if ec_code != 0 and resp.get("type") != "success":
        _log("   emailCode returned non-success, but OTP may still have been sent", "WARNING")

    _sleep_with_stop(random.uniform(1.0, 2.5), should_stop)

    # ---- Step 5: wait for OTP with resend support ----
    _log("Step 5/6: waiting for verification code...", "INFO")
    otp_code = None

    for attempt in range(1 + MAX_RESEND_ATTEMPTS):
        if attempt > 0:
            _log(f"   Resending verification email ({attempt}/{MAX_RESEND_ATTEMPTS})...", "INFO")
            # Resend: no captcha needed (session already verified)
            resp, _ = _post("/authorize/emailCode", {
                "email": email, "redirect_uri": redirect_uri, "language": "zh",
            })
            _log(f"   resend code={resp.get('code', '?')}", "DEBUG")
            _sleep_with_stop(random.uniform(2.0, 4.0), should_stop)

        otp_code = wait_for_code_by_provider(
            email,
            mail_provider=mail_provider,
            mail_api_key=mail_api_key,
            timeout=OTP_WAIT_TIMEOUT,
            should_stop=should_stop,
        )
        if otp_code:
            _log(f"   Got OTP: {otp_code}", "DEBUG")
            break
        _log(f"   OTP wait timed out ({attempt + 1}/{1 + MAX_RESEND_ATTEMPTS})", "WARNING")

    if not otp_code:
        raise Exception(f"Verification code not received after {1 + MAX_RESEND_ATTEMPTS} attempts ({OTP_WAIT_TIMEOUT}s each)")

    # ---- Submit OTP via code_sign ----
    resp, r = _post("/authorize/code_sign", {
        "email": email, "code": otp_code,
        "redirect_uri": redirect_uri, "language": "zh", "agreeTC": 1,
    })

    # ---- OTP verification failure: resend and retry once ----
    if resp.get("code") != 0 and resp.get("type") != "success":
        _log(f"   OTP verification failed (code={resp.get('code')}), resending...", "WARNING")
        _sleep_with_stop(random.uniform(3.0, 5.0), should_stop)
        _post("/authorize/emailCode", {
            "email": email, "redirect_uri": redirect_uri, "language": "zh",
        })
        _sleep_with_stop(random.uniform(3.0, 5.0), should_stop)

        new_code = wait_for_code_by_provider(
            email,
            mail_provider=mail_provider,
            mail_api_key=mail_api_key,
            timeout=OTP_WAIT_TIMEOUT,
            should_stop=should_stop,
        )
        if new_code and new_code != otp_code:
            _log(f"   New OTP: {new_code}, re-submitting...", "DEBUG")
            resp, r = _post("/authorize/code_sign", {
                "email": email, "code": new_code,
                "redirect_uri": redirect_uri, "language": "zh", "agreeTC": 1,
            })

    if resp.get("code") != 0 and resp.get("type") != "success":
        raise Exception(f"code_sign failed: {resp}")

    # ---- Extract token ----
    token = headers.get("token", "")
    if not token:
        data = resp.get("data", {})
        token = data.get("token", "") if isinstance(data, dict) else ""
    if not token:
        token = session.cookies.get("token", "")
    if not token:
        for cookie in r.cookies:
            if cookie.name == "token":
                token = cookie.value
                break
    if not token:
        raise Exception(f"No token in code_sign response: {resp}")

    _log(f"Got token: {token[:8]}...", "SUCCESS")

    # ---- Step 6: authenticate with token to get ticket ----
    _log("Step 6/6: authenticate (get ticket)", "DEBUG")
    headers["token"] = token
    resp, _ = _post("/authorize/authenticate", {"redirect_uri": redirect_uri})
    ticket = resp.get("data", {}).get("ticket", "") if isinstance(resp.get("data"), dict) else ""

    # Set password silently
    try:
        _post("/user/silent_edit", {"password": cfg["AUTO_REGISTER_PASSWORD"], "language": "zh"})
    except Exception:
        pass

    # ---- Step 7: activate account on api_v2 via ticket ----
    # The gosplit registration creates the account in the auth system, but
    # api_v2.rita.ai (the chat API) needs a separate activation via ticket.
    # In browser flow, this happens when redirected to www.rita.ai/zh/ai-chat?ticket=xxx
    if ticket:
        _log("Step 7: activating account on api_v2 via ticket...", "DEBUG")
        try:
            _ensure_not_stopped(should_stop, "activate_ticket")
            activate_resp = _http_get(
                f"https://www.rita.ai/zh/ai-chat",
                params={"ticket": ticket},
                headers={
                    **_random_browser_headers(),
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Referer": "https://account.rita.ai/",
                },
                timeout=15,
                allow_redirects=True,
                verify=not cfg["DISABLE_SSL_VERIFY"],
                proxy=register_proxy,
            )
            _log(f"   activate status={activate_resp.status_code}", "DEBUG")
        except Exception as e:
            _log(f"   activate warning: {e}", "WARNING")

    _log(f"Registration complete! token={token[:8]}..., ticket={ticket[:8] if ticket else 'N/A'}...", "SUCCESS")

    return {"token": token, "email": email, "ticket": ticket}


# ===================== Orchestrator =====================
def auto_register_one(account_manager=None, upstream_url="", origin="", captcha_provider_override: str = "",
                      should_stop=None) -> dict | None:
    """
    Register one new Rita.ai account and optionally add it to AccountManager.

    Returns: {"token": ..., "email": ..., "account_id": ...} or None on failure
    """
    cfg = _get_live_config()
    try:
        _ensure_not_stopped(should_stop, "auto_register_one_init")
        selected_provider = _get_default_mail_provider(cfg)
        captcha_cfg = _resolve_recaptcha_provider_config(cfg, captcha_provider_override)
        register_proxy = _resolve_register_proxy(cfg)
        mail_proxy = _resolve_mail_proxy(cfg)
        if register_proxy:
            _log(f"🌐 当前注册代理: {register_proxy}", "INFO")
        else:
            _log("🌐 当前注册按直连执行", "INFO")
        _log(f"🧩 当前打码服务: {captcha_cfg['label']} ({captcha_cfg['provider']})", "INFO")
        if mail_proxy:
            _log(f"📧 邮箱请求复用注册代理: {mail_proxy}", "INFO")
        else:
            _log("📧 邮箱请求按直连执行", "DEBUG")

        # 1. Create temp email
        _ensure_not_stopped(should_stop, "create_temp_email")
        _log(f"🔄 Creating temporary email via {selected_provider}...", "INFO")
        mailbox = create_temp_email_by_provider(selected_provider)
        email = str(mailbox.get("email") or "").strip()
        _log(f"📧 Email: {email}", "INFO")
        if mailbox.get("channel_label"):
            _log(f"📨 Mail channel: {mailbox['channel_label']}", "INFO")

        # 2. Register
        _ensure_not_stopped(should_stop, "register_account")
        _log("🔄 Starting registration...", "INFO")
        result = register_rita_account(
            email,
            mail_provider=selected_provider,
            mail_api_key=str(mailbox.get("mail_api_key") or "").strip(),
            captcha_provider_override=captcha_cfg["provider"],
            should_stop=should_stop,
        )
        token = result["token"]

        # 3. Add to AccountManager
        account_id = None
        if account_manager:
            _ensure_not_stopped(should_stop, "before_account_add")
            name_part = email.split("@")[0]
            acc = account_manager.add(
                token=token, name=f"auto-{name_part}",
                email=email, password=cfg["AUTO_REGISTER_PASSWORD"],
                mail_provider=selected_provider,
                mail_api_key=str(mailbox.get("mail_api_key") or "").strip(),
            )
            account_id = acc.id
            _log(f"➕ Account added: {acc.name} ({acc.id})", "SUCCESS")

            # Verify the new token works
            if upstream_url and origin and not _should_stop(should_stop):
                test = account_manager.test_account(acc.id, upstream_url, origin)
                if test.get("ok"):
                    _log(f"✅ Token verified: {test.get('models', 0)} models available", "SUCCESS")
                else:
                    _log(f"⚠️ Token test failed: {test}", "WARNING")

        return {
            "token": token,
            "email": email,
            "account_id": account_id,
            "mail_provider": selected_provider,
            "captcha_provider": captcha_cfg["provider"],
        }

    except RegistrationStopped:
        raise
    except Exception as e:
        _log(f"❌ Auto-register failed: {e}", "ERROR")
        return None


def auto_register_batch(count: int = 1, account_manager=None,
                        upstream_url="", origin="", captcha_provider_override: str = "",
                        should_stop=None) -> list[dict]:
    """Register multiple accounts. Returns list of results."""
    results = []
    for i in range(count):
        _ensure_not_stopped(should_stop, f"batch_before_{i+1}")
        _log(f"📋 Registering account {i+1}/{count}...", "INFO")
        try:
            result = auto_register_one(
                account_manager,
                upstream_url,
                origin,
                captcha_provider_override,
                should_stop=should_stop,
            )
        except RegistrationStopped as e:
            raise RegistrationStopped(str(e), results=results) from e
        if result:
            results.append(result)
        # Delay between registrations
        if i < count - 1:
            delay = random.uniform(5, 15)
            _log(f"⏳ Waiting {delay:.0f}s before next registration...", "DEBUG")
            try:
                _sleep_with_stop(delay, should_stop)
            except RegistrationStopped as e:
                raise RegistrationStopped(str(e), results=results) from e
    return results


# ===================== Background Auto-Replenish =====================
_replenish_lock = threading.Lock()


def start_auto_replenish(account_manager, upstream_url: str, origin: str,
                         check_interval: int = 300, log_fn=None):
    """
    Background thread: when active accounts drop below min_active,
    automatically register new accounts to replenish the pool.
    Now reads config from DB each loop iteration so changes take effect live.
    """
    global _log_fn
    if log_fn:
        _log_fn = log_fn

    # Check initial config from DB
    cfg = _get_live_config()
    if not cfg["AUTO_REGISTER_ENABLED"]:
        _log("⏸ Auto-register disabled (set AUTO_REGISTER_ENABLED=1 to enable)", "INFO")
        return

    captcha_cfg = _resolve_recaptcha_provider_config(cfg)
    if not captcha_cfg["client_key"]:
        _log(f"⚠️ Auto-register disabled: {captcha_cfg['label']} 未配置", "WARNING")
        return
    _log(f"🧩 Auto-register captcha provider: {captcha_cfg['label']} ({captcha_cfg['provider']})", "INFO")

    selected_provider = _get_default_mail_provider(cfg)
    if not _is_mail_provider_configured(selected_provider, cfg):
        missing = _get_mail_provider_missing_keys(selected_provider, cfg)
        _log(f"⚠️ Auto-register disabled: missing mail config for {selected_provider}: {', '.join(missing)}", "WARNING")
        return

    def loop():
        time.sleep(60)
        _log(f"🔄 Auto-replenish started (interval={check_interval}s)", "INFO")

        while True:
            try:
                # Re-read config each iteration so DB changes are picked up
                live_cfg = _get_live_config()
                min_active = live_cfg["AUTO_REGISTER_MIN_ACTIVE"]
                batch_size = live_cfg["AUTO_REGISTER_BATCH"]

                with _replenish_lock:
                    summary = account_manager.summary()
                    active = summary.get("active", 0)

                    if active < min_active:
                        need = min_active - active
                        to_create = min(need, batch_size)
                        _log(f"⚠️ Active accounts ({active}) below minimum ({min_active}), "
                             f"registering {to_create} new account(s)...", "WARNING")
                        auto_register_batch(to_create, account_manager, upstream_url, origin)
                    else:
                        _log(f"✅ Active accounts: {active} (min: {min_active})", "DEBUG")
            except Exception as e:
                _log(f"❌ Auto-replenish error: {e}", "ERROR")

            time.sleep(check_interval)

    t = threading.Thread(target=loop, daemon=True, name="auto-replenish")
    t.start()


# ===================== Config Check =====================
def check_config(captcha_provider_override: str = "") -> dict:
    """Check if auto-register dependencies are configured.
    Reads from database for live config, falls back to module-level env vars.
    """
    try:
        from database import get_db
        db = get_db()
        yescaptcha_key = db.get_config("YESCAPTCHA_KEY") or YESCAPTCHA_KEY
        captcha_provider = db.get_config("CAPTCHA_PROVIDER") or CAPTCHA_PROVIDER
        ohmycaptcha_local_api_url = db.get_config("OHMYCAPTCHA_LOCAL_API_URL") or OHMYCAPTCHA_LOCAL_API_URL
        ohmycaptcha_local_key = db.get_config("OHMYCAPTCHA_LOCAL_KEY") or OHMYCAPTCHA_LOCAL_KEY
        default_provider = db.get_config("MAIL_PROVIDER_DEFAULT") or MAIL_PROVIDER_DEFAULT
        gptmail_key = db.get_config("GPTMAIL_API_KEY") or GPTMAIL_API_KEY
        gptmail_base = db.get_config("GPTMAIL_API_BASE") or GPTMAIL_API_BASE
        yydsmail_key = db.get_config("YYDSMAIL_API_KEY") or YYDSMAIL_API_KEY
        yydsmail_base = db.get_config("YYDSMAIL_API_BASE") or YYDSMAIL_API_BASE
        moemail_key = db.get_config("MOEMAIL_API_KEY") or MOEMAIL_API_KEY
        moemail_base = db.get_config("MOEMAIL_API_BASE") or MOEMAIL_API_BASE
        moemail_channels_json = db.get_config("MOEMAIL_CHANNELS_JSON", "")
        register_proxy = db.get_config("REGISTER_PROXY") or REGISTER_PROXY
        mail_use_proxy = db.get_config("MAIL_USE_PROXY", "0") == "1" or MAIL_USE_PROXY
        auto_enabled = db.get_config("AUTO_REGISTER_ENABLED", "0") == "1" or AUTO_REGISTER_ENABLED
        min_active = int(db.get_config("AUTO_REGISTER_MIN_ACTIVE") or AUTO_REGISTER_MIN_ACTIVE)
        batch_size = int(db.get_config("AUTO_REGISTER_BATCH") or AUTO_REGISTER_BATCH)
    except Exception:
        # Fallback to env vars if DB is unavailable
        yescaptcha_key = YESCAPTCHA_KEY
        captcha_provider = CAPTCHA_PROVIDER
        ohmycaptcha_local_api_url = OHMYCAPTCHA_LOCAL_API_URL
        ohmycaptcha_local_key = OHMYCAPTCHA_LOCAL_KEY
        default_provider = MAIL_PROVIDER_DEFAULT
        gptmail_key = GPTMAIL_API_KEY
        gptmail_base = GPTMAIL_API_BASE
        yydsmail_key = YYDSMAIL_API_KEY
        yydsmail_base = YYDSMAIL_API_BASE
        moemail_key = MOEMAIL_API_KEY
        moemail_base = MOEMAIL_API_BASE
        moemail_channels_json = os.getenv("MOEMAIL_CHANNELS_JSON", "")
        register_proxy = REGISTER_PROXY
        mail_use_proxy = MAIL_USE_PROXY
        auto_enabled = AUTO_REGISTER_ENABLED
        min_active = AUTO_REGISTER_MIN_ACTIVE
        batch_size = AUTO_REGISTER_BATCH

    provider = _normalize_mail_provider(default_provider)
    recaptcha_provider_cfg = _resolve_recaptcha_provider_config({
        "CAPTCHA_PROVIDER": captcha_provider,
        "YESCAPTCHA_KEY": yescaptcha_key,
        "OHMYCAPTCHA_LOCAL_API_URL": ohmycaptcha_local_api_url,
        "OHMYCAPTCHA_LOCAL_KEY": ohmycaptcha_local_key,
    }, captcha_provider_override)
    captcha_missing = []
    if recaptcha_provider_cfg["provider"] == "yescaptcha":
        if not recaptcha_provider_cfg["client_key"]:
            captcha_missing.append("YESCAPTCHA_KEY")
    elif not recaptcha_provider_cfg["api_url"]:
        captcha_missing.append("OHMYCAPTCHA_LOCAL_API_URL")

    provider_cfg = {
        "gptmail": {"GPTMAIL_API_KEY": gptmail_key, "GPTMAIL_API_BASE": gptmail_base},
        "yydsmail": {"YYDSMAIL_API_KEY": yydsmail_key, "YYDSMAIL_API_BASE": yydsmail_base},
        "moemail": {
            "MOEMAIL_API_KEY": moemail_key,
            "MOEMAIL_API_BASE": moemail_base,
            "MOEMAIL_CHANNELS_JSON": moemail_channels_json,
        },
    }
    provider_ready = _is_mail_provider_configured(provider, {
        **provider_cfg.get(provider, {}),
        "GPTMAIL_API_KEY": gptmail_key,
        "GPTMAIL_API_BASE": gptmail_base,
        "YYDSMAIL_API_KEY": yydsmail_key,
        "YYDSMAIL_API_BASE": yydsmail_base,
        "MOEMAIL_API_KEY": moemail_key,
        "MOEMAIL_API_BASE": moemail_base,
        "MOEMAIL_CHANNELS_JSON": moemail_channels_json,
    })
    provider_missing = _get_mail_provider_missing_keys(provider, {
        "GPTMAIL_API_KEY": gptmail_key,
        "GPTMAIL_API_BASE": gptmail_base,
        "YYDSMAIL_API_KEY": yydsmail_key,
        "YYDSMAIL_API_BASE": yydsmail_base,
        "MOEMAIL_API_KEY": moemail_key,
        "MOEMAIL_API_BASE": moemail_base,
        "MOEMAIL_CHANNELS_JSON": moemail_channels_json,
    })
    moemail_stats = get_moemail_channel_stats({
        "MOEMAIL_API_KEY": moemail_key,
        "MOEMAIL_API_BASE": moemail_base,
        "MOEMAIL_CHANNELS_JSON": moemail_channels_json,
    })

    return {
        "auto_register_enabled": auto_enabled,
        "yescaptcha_configured": bool(yescaptcha_key),
        "captcha_provider": recaptcha_provider_cfg["provider"],
        "captcha_provider_label": recaptcha_provider_cfg["label"],
        "captcha_api_url": recaptcha_provider_cfg["api_url"],
        "captcha_missing": captcha_missing,
        "captcha_configured": not captcha_missing and bool(recaptcha_provider_cfg["client_key"] and recaptcha_provider_cfg["api_url"]),
        "ohmycaptcha_local_configured": bool(ohmycaptcha_local_api_url),
        "ohmycaptcha_local_api_url": ohmycaptcha_local_api_url or "http://127.0.0.1:8001",
        "mail_provider_default": provider,
        "mail_provider_configured": provider_ready,
        "mail_provider_missing": provider_missing,
        "gptmail_configured": bool(gptmail_key),
        "gptmail_api_base": gptmail_base,
        "yydsmail_configured": bool(yydsmail_key),
        "yydsmail_api_base": yydsmail_base,
        "moemail_configured": moemail_stats["configured"],
        "moemail_api_base": moemail_base,
        "moemail_channels_total": moemail_stats["total"],
        "moemail_channels_enabled": moemail_stats["enabled"],
        "moemail_using_channels_json": moemail_stats["using_json"],
        "moemail_channels_parse_error": moemail_stats["parse_error"],
        "register_proxy": _normalize_proxy_value(register_proxy),
        "register_proxy_configured": bool(_normalize_proxy_value(register_proxy)),
        "mail_use_proxy": bool(mail_use_proxy),
        "recaptcha_sitekey": RECAPTCHA_SITEKEY,
        "min_active_accounts": min_active,
        "batch_size": batch_size,
        "ready": bool((not captcha_missing) and bool(recaptcha_provider_cfg["client_key"]) and provider_ready),
    }


# ===================== YYDS Mail Support =====================
def _yydsmail_create_email(api_key: str = "") -> tuple[str, str]:
    """Create a temporary email via YYDS Mail.
    Returns: (email_address, session_token)
    The session token is needed for subsequent message queries.
    """
    cfg = _get_live_config()
    key = api_key or cfg["YYDSMAIL_API_KEY"]
    api_base = cfg["YYDSMAIL_API_BASE"]
    ssl_verify = not cfg["DISABLE_SSL_VERIFY"]
    mail_proxy = _resolve_mail_proxy(cfg)

    if not key:
        raise Exception("YYDSMAIL_API_KEY not configured")

    headers = {"Accept": "application/json", "Content-Type": "application/json",
               "X-API-Key": key}

    # Fetch available domains
    domains = []
    try:
        r = _http_get(
            f"{api_base}/domains",
            headers=headers,
            timeout=15,
            verify=ssl_verify,
            proxy=mail_proxy,
        )
        if r.status_code == 200:
            raw = r.json()
            data = raw if isinstance(raw, list) else raw.get("data", [])
            domains = [d.get("domain") if isinstance(d, dict) else d
                       for d in data if (d.get("domain") if isinstance(d, dict) else d)]
    except Exception as e:
        _log(f"📧 YYDS Mail: fetch domains failed: {e}", "WARNING")

    if not domains:
        raise Exception("YYDS Mail: no domains available")

    domain = random.choice(domains)
    prefix = "".join(random.choices(string.ascii_lowercase + string.digits,
                                    k=random.randint(8, 12)))

    r = _http_post(
        f"{api_base}/accounts",
        headers=headers,
        json={"address": prefix, "domain": domain},
        timeout=15, verify=ssl_verify,
        proxy=mail_proxy,
    )
    if r.status_code not in (200, 201):
        raise Exception(f"YYDS Mail create failed: {r.status_code} {r.text[:200]}")

    resp = r.json()
    data = resp.get("data", resp) if isinstance(resp, dict) else resp
    email = data.get("address", f"{prefix}@{domain}")
    token = data.get("token", "")
    if not token:
        raise Exception("YYDS Mail: no token returned from account creation")

    return email, token


def _yydsmail_wait_for_code(email: str, mail_api_key: str = "",
                            mail_token: str = "",
                            timeout: int = 120,
                            should_stop=None) -> str | None:
    """Poll YYDS Mail for verification code.
    Uses the session token from _yydsmail_create_email (not the API key).
    Falls back to api_key as Bearer if no mail_token.
    """
    cfg = _get_live_config()
    payload = _parse_mail_api_payload("yydsmail", mail_api_key, cfg)
    bearer = mail_token or payload.get("auth_credential") or payload.get("api_key") or cfg["YYDSMAIL_API_KEY"]
    if not bearer:
        _log("⚠️ YYDS Mail: no token/key for message query", "WARNING")
        return None

    ssl_verify = not cfg["DISABLE_SSL_VERIFY"]
    api_base = str(payload.get("api_base") or cfg["YYDSMAIL_API_BASE"]).rstrip("/")
    mail_proxy = _resolve_mail_proxy(cfg)
    headers = {"Accept": "application/json",
               "Authorization": f"Bearer {bearer}"}

    start = time.time()
    while time.time() - start < timeout:
        _ensure_not_stopped(should_stop, "yydsmail_poll")
        try:
            resp = _http_get(
                f"{api_base}/messages",
                headers=headers,
                timeout=15,
                verify=ssl_verify,
                proxy=mail_proxy,
            )
            if resp.status_code == 200:
                raw = resp.json()
                msgs = raw if isinstance(raw, list) else (
                    raw.get("data", {}).get("messages", [])
                    if isinstance(raw.get("data"), dict)
                    else raw.get("data", [])
                )
                for msg in (msgs or []):
                    msg_id = msg.get("id")
                    if not msg_id:
                        continue
                    # Fetch detail
                    try:
                        dr = _http_get(
                            f"{api_base}/messages/{msg_id}",
                            headers=headers,
                            timeout=15,
                            verify=ssl_verify,
                            proxy=mail_proxy,
                        )
                        if dr.status_code == 200:
                            detail = dr.json()
                            d = detail.get("data", detail) if isinstance(detail, dict) else detail
                            content = d.get("text", "") or d.get("html", "") or ""
                            code = _extract_code(content)
                            if code:
                                return code
                    except Exception:
                        pass
        except Exception as e:
            _log(f"📧 YYDS Mail poll error: {e}", "DEBUG")

        elapsed = int(time.time() - start)
        _log(f"📧 YYDS Mail waiting... ({elapsed}s/{timeout}s)", "DEBUG")
        _sleep_with_stop(OTP_POLL_INTERVAL, should_stop)

    return None


def _moemail_wait_for_code(email: str, mail_api_key: str = "", timeout: int = 120, should_stop=None) -> str | None:
    cfg = _get_live_config()
    payload = _parse_mail_api_payload("moemail", mail_api_key, cfg)
    api_key = str(payload.get("api_key") or "").strip()
    api_base = str(payload.get("api_base") or cfg["MOEMAIL_API_BASE"] or "").strip().rstrip("/")
    auth_credential = str(payload.get("auth_credential") or "").strip()
    if not api_key or not api_base:
        _log("⚠️ MoeMail: missing API key or base URL", "WARNING")
        return None
    if not auth_credential:
        _log(f"⚠️ MoeMail: mailbox credential missing for {email}", "WARNING")
        return None

    ssl_verify = not cfg["DISABLE_SSL_VERIFY"]
    mail_proxy = _resolve_mail_proxy(cfg)
    headers = _moemail_headers(api_key)
    channel_name = str(payload.get("channel_name") or "MoeMail").strip() or "MoeMail"
    start = time.time()
    seen_ids = set()
    while time.time() - start < timeout:
        _ensure_not_stopped(should_stop, "moemail_poll")
        try:
            resp = _http_get(
                f"{api_base}/api/emails/{auth_credential}",
                headers=headers,
                timeout=15,
                verify=ssl_verify,
                proxy=mail_proxy,
            )
            if resp.status_code == 200:
                messages = resp.json().get("messages") or []
                for msg in messages:
                    if not isinstance(msg, dict):
                        continue
                    msg_id = str(msg.get("id") or "").strip()
                    if not msg_id or msg_id in seen_ids:
                        continue
                    detail_resp = _http_get(
                        f"{api_base}/api/emails/{auth_credential}/{msg_id}",
                        headers=headers,
                        timeout=15,
                        verify=ssl_verify,
                        proxy=mail_proxy,
                    )
                    if detail_resp.status_code != 200:
                        continue
                    seen_ids.add(msg_id)
                    detail = detail_resp.json()
                    msg_obj = detail.get("message") if isinstance(detail.get("message"), dict) else {}
                    content = msg_obj.get("content") or msg_obj.get("html") or detail.get("text") or detail.get("html") or ""
                    code = _extract_code(content)
                    if code:
                        return code
        except Exception as e:
            _log(f"📧 {channel_name} poll error: {e}", "DEBUG")

        elapsed = int(time.time() - start)
        _log(f"📧 {channel_name} waiting... ({elapsed}s/{timeout}s)", "DEBUG")
        _sleep_with_stop(OTP_POLL_INTERVAL, should_stop)

    return None


def wait_for_code_by_provider(email: str, mail_provider: str = "",
                              mail_api_key: str = "",
                              timeout: int = 120,
                              should_stop=None) -> str | None:
    """Wait for verification code using the appropriate mail provider."""
    provider = _normalize_mail_provider(mail_provider or _get_default_mail_provider())

    if provider == "yydsmail":
        return _yydsmail_wait_for_code(email, mail_api_key=mail_api_key, timeout=timeout, should_stop=should_stop)
    if provider == "moemail":
        return _moemail_wait_for_code(email, mail_api_key=mail_api_key, timeout=timeout, should_stop=should_stop)
    else:
        # Default to GPTMail
        return wait_for_verification_code(email, timeout, should_stop=should_stop)


# ===================== Token Refresh (Re-login existing account) =====================
def refresh_account_token(email: str, password: str = "",
                          mail_provider: str = "", mail_api_key: str = "") -> dict:
    """
    Re-login an existing Rita.ai account to get a fresh token.
    Optimized: explicit emailCode, resend on timeout, captcha retry.

    Returns: {"token": "...", "email": "...", "ticket": "..."}
    """
    cfg = _get_live_config()
    register_proxy = _resolve_register_proxy(cfg)
    session, impersonate = _create_rita_session(
        ssl_verify=not cfg["DISABLE_SSL_VERIFY"],
        proxy=register_proxy,
    )
    headers = _gosplit_headers()
    redirect_uri = "https://www.rita.ai/zh/ai-chat"

    def _session_post(url, **kwargs):
        """POST with optional impersonate for curl_cffi."""
        if impersonate:
            kwargs["impersonate"] = impersonate
        return session.post(url, **kwargs)

    # Step 1: authenticate (init)
    _log(f"🔄 Refresh: authenticate (email={email})", "DEBUG")
    r = _session_post(
        f"{GOSPLIT_API}/authorize/authenticate",
        headers=headers,
        json={"redirect_uri": redirect_uri},
        timeout=30,
    )
    r.raise_for_status()

    # Step 2: sign_process (email + agree)
    _log("🔄 Refresh: sign_process", "DEBUG")
    r = _session_post(
        f"{GOSPLIT_API}/authorize/sign_process",
        headers=headers,
        json={"redirect_uri": redirect_uri, "language": "zh",
              "email": email, "agree": 1},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()

    # Step 3: handle captcha if needed (with retry)
    captcha_token = None
    if data.get("data", {}).get("need_captcha"):
        for cap_attempt in range(1, MAX_CAPTCHA_ATTEMPTS + 1):
            if cap_attempt > 1:
                time.sleep(random.uniform(3.0, 5.0))
            _log(f"🔄 Refresh: solving reCAPTCHA (attempt {cap_attempt})...", "INFO")
            try:
                captcha_token = solve_recaptcha()
            except Exception as e:
                if cap_attempt >= MAX_CAPTCHA_ATTEMPTS:
                    raise Exception(f"reCAPTCHA failed for refresh: {e}")
                continue

            time.sleep(random.uniform(1.0, 2.0))
            r = _session_post(
                f"{GOSPLIT_API}/authorize/sign_process",
                headers=headers,
                json={"redirect_uri": redirect_uri, "language": "zh",
                      "email": email, "agree": 1,
                      "g-recaptcha-response": captcha_token},
                timeout=30,
            )
            r.raise_for_status()
            data = r.json()
            if data.get("code") == 0 or data.get("type") == "success":
                break
            if cap_attempt >= MAX_CAPTCHA_ATTEMPTS:
                raise Exception(f"Refresh captcha failed: {data}")

    # Step 4: trigger emailCode
    _log("🔄 Refresh: sending verification email...", "INFO")
    try:
        r = _session_post(
            f"{GOSPLIT_API}/authorize/emailCode",
            headers=headers,
            json={"email": email, "redirect_uri": redirect_uri, "language": "zh",
                  **({"g-recaptcha-response": captcha_token} if captcha_token else {})},
            timeout=30,
        )
        r.raise_for_status()
    except Exception as e:
        _log(f"   ⚠️ emailCode failed: {e}, continuing...", "WARNING")
    time.sleep(random.uniform(1.0, 2.0))

    # Step 5: wait for OTP with resend
    code = None
    for attempt in range(1 + MAX_RESEND_ATTEMPTS):
        if attempt > 0:
            _log(f"   📧 Resending verification email ({attempt}/{MAX_RESEND_ATTEMPTS})...", "INFO")
            try:
                _session_post(
                    f"{GOSPLIT_API}/authorize/emailCode",
                    headers=headers,
                    json={"email": email, "redirect_uri": redirect_uri, "language": "zh"},
                    timeout=30,
                )
            except Exception:
                pass
            time.sleep(random.uniform(2.0, 3.0))

        code = wait_for_code_by_provider(email, mail_provider, mail_api_key, timeout=OTP_WAIT_TIMEOUT)
        if code:
            break
        _log(f"   ⏰ OTP wait timed out ({attempt + 1}/{1 + MAX_RESEND_ATTEMPTS})", "WARNING")

    if not code:
        raise Exception(f"Failed to receive verification code for {email}")

    _log(f"📧 Refresh: got code {code}", "DEBUG")
    r = _session_post(
        f"{GOSPLIT_API}/authorize/code_sign",
        headers=headers,
        json={"email": email, "code": code,
              "redirect_uri": redirect_uri, "language": "zh", "agreeTC": 1},
        timeout=30,
    )
    r.raise_for_status()
    code_sign_data = r.json()

    # OTP verification failure: resend once
    if code_sign_data.get("code") != 0 and code_sign_data.get("type") != "success":
        _log("   ⚠️ OTP verification failed, resending...", "WARNING")
        time.sleep(random.uniform(2.0, 4.0))
        try:
            _session_post(
                f"{GOSPLIT_API}/authorize/emailCode",
                headers=headers,
                json={"email": email, "redirect_uri": redirect_uri, "language": "zh"},
                timeout=30,
            )
        except Exception:
            pass
        time.sleep(random.uniform(2.0, 3.0))
        new_code = wait_for_code_by_provider(email, mail_provider, mail_api_key, timeout=OTP_WAIT_TIMEOUT)
        if new_code and new_code != code:
            r = _session_post(
                f"{GOSPLIT_API}/authorize/code_sign",
                headers=headers,
                json={"email": email, "code": new_code,
                      "redirect_uri": redirect_uri, "language": "zh", "agreeTC": 1},
                timeout=30,
            )
            r.raise_for_status()
            code_sign_data = r.json()

    if code_sign_data.get("code") != 0 and code_sign_data.get("type") != "success":
        raise Exception(f"Refresh code_sign failed: {code_sign_data}")

    # Extract token
    token = code_sign_data.get("data", {}).get("token", "")
    if not token:
        try:
            token = session.cookies.get("token", "")
        except Exception:
            pass
    if not token:
        for cookie in r.cookies:
            if cookie.name == "token":
                token = cookie.value
                break
    if not token:
        raise Exception(f"No token in refresh response: {code_sign_data}")

    # Step 6: authenticate with new token
    _log("🔄 Refresh: authenticate (get ticket)", "DEBUG")
    auth_headers = {**headers, "token": token}
    r = _session_post(
        f"{GOSPLIT_API}/authorize/authenticate",
        headers=auth_headers,
        json={"redirect_uri": redirect_uri},
        timeout=30,
    )
    r.raise_for_status()
    auth_data = r.json()
    ticket = auth_data.get("data", {}).get("ticket", "")

    _log(f"✅ Token refreshed! token={token[:8]}...", "SUCCESS")
    return {"token": token, "email": email, "ticket": ticket}


# ===================== Ticket Re-login (use existing token) =====================
def relogin_for_ticket(token: str) -> dict:
    """
    Re-authenticate with an existing token to get a fresh ticket.
    This is the simplest flow — just call authenticate with the token header.

    Returns: {"ticket": "..."} or raises Exception
    """
    cfg = _get_live_config()
    register_proxy = _resolve_register_proxy(cfg)
    session, impersonate = _create_rita_session(
        ssl_verify=not cfg["DISABLE_SSL_VERIFY"],
        proxy=register_proxy,
    )
    headers = _gosplit_headers()
    headers["token"] = token

    def _session_post(url, **kwargs):
        if impersonate:
            kwargs["impersonate"] = impersonate
        return session.post(url, **kwargs)

    # Step 1: authorize/url
    r = _session_post(
        f"{GOSPLIT_API}/authorize/url",
        headers=_gosplit_headers(),
        json={"login_url": "https://account.rita.ai", "source_url": "https://www.rita.ai"},
        timeout=15,
    )
    r.raise_for_status()

    # Step 2: authenticate with token
    r = _session_post(
        f"{GOSPLIT_API}/authorize/authenticate",
        headers=headers,
        json={"redirect_uri": "https://www.rita.ai/zh/ai-chat"},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()

    ticket = data.get("data", {}).get("ticket", "")
    status = data.get("data", {}).get("status", False)

    if ticket:
        _log(f"🎫 Got ticket: {ticket[:8]}...", "SUCCESS")
        return {"ticket": ticket}
    elif status:
        # status=true means token is valid but no ticket in response
        return {"ticket": "", "message": "Token valid but no ticket returned"}
    else:
        raise Exception(f"Authentication failed: {data}")

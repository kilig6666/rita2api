"""
auto_register.py — Rita.ai 自动注册 & Token 获取模块

完整流程:
1. 使用 GPTMail 创建临时邮箱
2. 通过 accountapi.gosplit.net 注册 Rita.ai 账号
3. 使用 YesCaptcha 解决 reCAPTCHA v2
4. 等待邮箱验证码并提交
5. 获取 token 并自动添加到 AccountManager

依赖: pip install requests
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

# ===================== Configuration =====================
_DISABLE_SSL_VERIFY = os.getenv("DISABLE_SSL_VERIFY", "0") == "1"

# YesCaptcha config
YESCAPTCHA_KEY = os.getenv("YESCAPTCHA_KEY", "")
YESCAPTCHA_API = "https://api.yescaptcha.com"

# GPTMail config
GPTMAIL_API_KEY = os.getenv("GPTMAIL_API_KEY", "")
GPTMAIL_API_BASE = os.getenv("GPTMAIL_API_BASE", "https://mail.chatgpt.org.uk")

# YYDS Mail config
YYDSMAIL_API_KEY = os.getenv("YYDSMAIL_API_KEY", "")
YYDSMAIL_API_BASE = os.getenv("YYDSMAIL_API_BASE", "https://maliapi.215.im/v1")

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

def _log(msg, level="INFO"):
    if _log_fn:
        _log_fn(msg, level)
    else:
        print(f"[AutoRegister] {msg}")


# ===================== Live Config (DB > env) =====================
def _get_live_config() -> dict:
    """Read config from database first, fallback to module-level env vars."""
    try:
        from database import get_db
        db = get_db()
        return {
            "YESCAPTCHA_KEY": db.get_config("YESCAPTCHA_KEY") or YESCAPTCHA_KEY,
            "GPTMAIL_API_KEY": db.get_config("GPTMAIL_API_KEY") or GPTMAIL_API_KEY,
            "GPTMAIL_API_BASE": db.get_config("GPTMAIL_API_BASE") or GPTMAIL_API_BASE,
            "YYDSMAIL_API_KEY": db.get_config("YYDSMAIL_API_KEY") or YYDSMAIL_API_KEY,
            "YYDSMAIL_API_BASE": db.get_config("YYDSMAIL_API_BASE") or YYDSMAIL_API_BASE,
            "AUTO_REGISTER_ENABLED": db.get_config("AUTO_REGISTER_ENABLED", "0") == "1" or AUTO_REGISTER_ENABLED,
            "AUTO_REGISTER_MIN_ACTIVE": int(db.get_config("AUTO_REGISTER_MIN_ACTIVE") or AUTO_REGISTER_MIN_ACTIVE),
            "AUTO_REGISTER_BATCH": int(db.get_config("AUTO_REGISTER_BATCH") or AUTO_REGISTER_BATCH),
            "AUTO_REGISTER_PASSWORD": db.get_config("AUTO_REGISTER_PASSWORD") or AUTO_REGISTER_PASSWORD,
            "DISABLE_SSL_VERIFY": db.get_config("DISABLE_SSL_VERIFY", "0") == "1" or _DISABLE_SSL_VERIFY,
        }
    except Exception:
        return {
            "YESCAPTCHA_KEY": YESCAPTCHA_KEY,
            "GPTMAIL_API_KEY": GPTMAIL_API_KEY,
            "GPTMAIL_API_BASE": GPTMAIL_API_BASE,
            "YYDSMAIL_API_KEY": YYDSMAIL_API_KEY,
            "YYDSMAIL_API_BASE": YYDSMAIL_API_BASE,
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


def create_temp_email() -> tuple[str, None]:
    """Create a temporary email via GPTMail. Returns email address."""
    cfg = _get_live_config()
    resp = requests.post(
        f"{cfg['GPTMAIL_API_BASE']}/api/generate-email",
        headers=_gptmail_headers(cfg["GPTMAIL_API_KEY"]),
        json={},
        timeout=15,
        verify=not cfg["DISABLE_SSL_VERIFY"],
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise Exception(f"GPTMail create failed: {data}")
    email = data["data"]["email"]
    return email


def _gptmail_get_detail(api_base, headers, mail_id, ssl_verify):
    """Fetch a single email's detail from GPTMail. Returns parsed dict or None."""
    try:
        r = requests.get(
            f"{api_base}/api/email/{mail_id}",
            headers=headers, timeout=15, verify=ssl_verify,
        )
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def wait_for_verification_code(email: str, timeout: int = 120) -> str | None:
    """Poll GPTMail for verification code.
    Optimized: extract from subject first (fast path), then from content/html.
    """
    cfg = _get_live_config()
    hdrs = _gptmail_headers(cfg["GPTMAIL_API_KEY"])
    api_base = cfg["GPTMAIL_API_BASE"]
    ssl_verify = not cfg["DISABLE_SSL_VERIFY"]

    start = time.time()
    while time.time() - start < timeout:
        try:
            resp = requests.get(
                f"{api_base}/api/emails",
                params={"email": email},
                headers=hdrs, timeout=15, verify=ssl_verify,
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
        time.sleep(OTP_POLL_INTERVAL)

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


# ===================== YesCaptcha — reCAPTCHA v2 Solver =====================
def solve_recaptcha() -> str | None:
    """Solve reCAPTCHA v2 via YesCaptcha. Returns g-recaptcha-response."""
    cfg = _get_live_config()
    yescaptcha_key = cfg["YESCAPTCHA_KEY"]
    ssl_verify = not cfg["DISABLE_SSL_VERIFY"]

    if not yescaptcha_key:
        raise Exception("YESCAPTCHA_KEY not configured")

    # Create task
    create_resp = requests.post(
        f"{YESCAPTCHA_API}/createTask",
        json={
            "clientKey": yescaptcha_key,
            "task": {
                "type": "NoCaptchaTaskProxyless",
                "websiteURL": RECAPTCHA_URL,
                "websiteKey": RECAPTCHA_SITEKEY,
            },
        },
        timeout=30,
        verify=ssl_verify,
    )
    create_resp.raise_for_status()
    create_data = create_resp.json()
    task_id = create_data.get("taskId")
    if not task_id:
        raise Exception(f"YesCaptcha create failed: {create_data}")

    _log(f"🔐 reCAPTCHA task created: {task_id}", "DEBUG")

    # Poll for result (max 120s)
    for _ in range(40):
        time.sleep(3)
        result_resp = requests.post(
            f"{YESCAPTCHA_API}/getTaskResult",
            json={"clientKey": yescaptcha_key, "taskId": task_id},
            timeout=15,
            verify=ssl_verify,
        )
        result_resp.raise_for_status()
        result_data = result_resp.json()

        solution = result_data.get("solution", {})
        if solution:
            token = solution.get("gRecaptchaResponse")
            if token:
                _log("🔐 reCAPTCHA solved!", "DEBUG")
                return token

        status = result_data.get("status", "")
        if status == "failed":
            raise Exception(f"YesCaptcha failed: {result_data}")

    raise Exception("YesCaptcha timeout (120s)")


# ===================== Rita.ai Registration Flow =====================
def _gosplit_headers():
    return {
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://account.rita.ai",
        "Referer": "https://account.rita.ai/",
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


def register_rita_account(email: str) -> dict:
    """
    Full registration flow for Rita.ai.
    Key: tracks session token/visitorid from each response and passes
    them in subsequent request headers (required by Rita API).

    Returns: {"token": "...", "email": "...", "ticket": "..."}
    Raises: Exception on failure
    """
    cfg = _get_live_config()
    session = requests.Session()
    session.verify = not cfg["DISABLE_SSL_VERIFY"]
    headers = _gosplit_headers()
    redirect_uri = "https://www.rita.ai/zh/ai-chat"

    def _post(path, payload):
        """POST helper that auto-updates session headers from response."""
        r = session.post(f"{GOSPLIT_API}{path}", headers=headers,
                         json=payload, timeout=30)
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
    time.sleep(random.uniform(2.0, 4.0))

    # ---- Step 3: solve reCAPTCHA (with retry) ----
    captcha_token = None
    if resp.get("data", {}).get("need_captcha"):
        for cap_attempt in range(1, MAX_CAPTCHA_ATTEMPTS + 1):
            if cap_attempt > 1:
                _log(f"   captcha submit failed, retrying ({cap_attempt}/{MAX_CAPTCHA_ATTEMPTS})...", "WARNING")
                time.sleep(random.uniform(5.0, 8.0))

            _log(f"Step 3/6: solving reCAPTCHA (attempt {cap_attempt})...", "INFO")
            try:
                captcha_token = solve_recaptcha()
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
            time.sleep(random.uniform(1.5, 3.5))
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

    time.sleep(random.uniform(0.5, 1.5))

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

    time.sleep(random.uniform(1.0, 2.5))

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
            time.sleep(random.uniform(2.0, 4.0))

        otp_code = wait_for_verification_code(email, timeout=OTP_WAIT_TIMEOUT)
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
        time.sleep(random.uniform(3.0, 5.0))
        _post("/authorize/emailCode", {
            "email": email, "redirect_uri": redirect_uri, "language": "zh",
        })
        time.sleep(random.uniform(3.0, 5.0))

        new_code = wait_for_verification_code(email, timeout=OTP_WAIT_TIMEOUT)
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

    _log(f"Registration complete! token={token[:8]}..., ticket={ticket[:8] if ticket else 'N/A'}...", "SUCCESS")

    return {"token": token, "email": email, "ticket": ticket}


# ===================== Orchestrator =====================
def auto_register_one(account_manager=None, upstream_url="", origin="") -> dict | None:
    """
    Register one new Rita.ai account and optionally add it to AccountManager.

    Returns: {"token": ..., "email": ..., "account_id": ...} or None on failure
    """
    cfg = _get_live_config()
    try:
        # 1. Create temp email
        _log("🔄 Creating temporary email...", "INFO")
        email = create_temp_email()
        _log(f"📧 Email: {email}", "INFO")

        # 2. Register
        _log("🔄 Starting registration...", "INFO")
        result = register_rita_account(email)
        token = result["token"]

        # 3. Add to AccountManager
        account_id = None
        if account_manager:
            name_part = email.split("@")[0]
            acc = account_manager.add(
                token=token, name=f"auto-{name_part}",
                email=email, password=cfg["AUTO_REGISTER_PASSWORD"],
                mail_provider="gptmail", mail_api_key=cfg["GPTMAIL_API_KEY"],
            )
            account_id = acc.id
            _log(f"➕ Account added: {acc.name} ({acc.id})", "SUCCESS")

            # Verify the new token works
            if upstream_url and origin:
                test = account_manager.test_account(acc.id, upstream_url, origin)
                if test.get("ok"):
                    _log(f"✅ Token verified: {test.get('models', 0)} models available", "SUCCESS")
                else:
                    _log(f"⚠️ Token test failed: {test}", "WARNING")

        return {"token": token, "email": email, "account_id": account_id}

    except Exception as e:
        _log(f"❌ Auto-register failed: {e}", "ERROR")
        return None


def auto_register_batch(count: int = 1, account_manager=None,
                        upstream_url="", origin="") -> list[dict]:
    """Register multiple accounts. Returns list of results."""
    results = []
    for i in range(count):
        _log(f"📋 Registering account {i+1}/{count}...", "INFO")
        result = auto_register_one(account_manager, upstream_url, origin)
        if result:
            results.append(result)
        # Delay between registrations
        if i < count - 1:
            delay = random.uniform(5, 15)
            _log(f"⏳ Waiting {delay:.0f}s before next registration...", "DEBUG")
            time.sleep(delay)
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

    if not cfg["YESCAPTCHA_KEY"]:
        _log("⚠️ Auto-register disabled: YESCAPTCHA_KEY not set", "WARNING")
        return

    if not cfg["GPTMAIL_API_KEY"] and not cfg["YYDSMAIL_API_KEY"]:
        _log("⚠️ Auto-register disabled: no mail API key set", "WARNING")
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
def check_config() -> dict:
    """Check if auto-register dependencies are configured.
    Reads from database for live config, falls back to module-level env vars.
    """
    try:
        from database import get_db
        db = get_db()
        yescaptcha_key = db.get_config("YESCAPTCHA_KEY") or YESCAPTCHA_KEY
        gptmail_key = db.get_config("GPTMAIL_API_KEY") or GPTMAIL_API_KEY
        gptmail_base = db.get_config("GPTMAIL_API_BASE") or GPTMAIL_API_BASE
        yydsmail_key = db.get_config("YYDSMAIL_API_KEY") or YYDSMAIL_API_KEY
        yydsmail_base = db.get_config("YYDSMAIL_API_BASE") or YYDSMAIL_API_BASE
        auto_enabled = db.get_config("AUTO_REGISTER_ENABLED", "0") == "1" or AUTO_REGISTER_ENABLED
        min_active = int(db.get_config("AUTO_REGISTER_MIN_ACTIVE") or AUTO_REGISTER_MIN_ACTIVE)
        batch_size = int(db.get_config("AUTO_REGISTER_BATCH") or AUTO_REGISTER_BATCH)
    except Exception:
        # Fallback to env vars if DB is unavailable
        yescaptcha_key = YESCAPTCHA_KEY
        gptmail_key = GPTMAIL_API_KEY
        gptmail_base = GPTMAIL_API_BASE
        yydsmail_key = YYDSMAIL_API_KEY
        yydsmail_base = YYDSMAIL_API_BASE
        auto_enabled = AUTO_REGISTER_ENABLED
        min_active = AUTO_REGISTER_MIN_ACTIVE
        batch_size = AUTO_REGISTER_BATCH

    return {
        "auto_register_enabled": auto_enabled,
        "yescaptcha_configured": bool(yescaptcha_key),
        "gptmail_configured": bool(gptmail_key),
        "gptmail_api_base": gptmail_base,
        "yydsmail_configured": bool(yydsmail_key),
        "yydsmail_api_base": yydsmail_base,
        "recaptcha_sitekey": RECAPTCHA_SITEKEY,
        "min_active_accounts": min_active,
        "batch_size": batch_size,
        "ready": bool(yescaptcha_key and (gptmail_key or yydsmail_key)),
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

    if not key:
        raise Exception("YYDSMAIL_API_KEY not configured")

    headers = {"Accept": "application/json", "Content-Type": "application/json",
               "X-API-Key": key}

    # Fetch available domains
    domains = []
    try:
        r = requests.get(f"{api_base}/domains", headers=headers,
                         timeout=15, verify=ssl_verify)
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

    r = requests.post(
        f"{api_base}/accounts",
        headers=headers,
        json={"address": prefix, "domain": domain},
        timeout=15, verify=ssl_verify,
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
                            timeout: int = 120) -> str | None:
    """Poll YYDS Mail for verification code.
    Uses the session token from _yydsmail_create_email (not the API key).
    Falls back to api_key as Bearer if no mail_token.
    """
    cfg = _get_live_config()
    bearer = mail_token or mail_api_key or cfg["YYDSMAIL_API_KEY"]
    if not bearer:
        _log("⚠️ YYDS Mail: no token/key for message query", "WARNING")
        return None

    ssl_verify = not cfg["DISABLE_SSL_VERIFY"]
    api_base = cfg["YYDSMAIL_API_BASE"]
    headers = {"Accept": "application/json",
               "Authorization": f"Bearer {bearer}"}

    start = time.time()
    while time.time() - start < timeout:
        try:
            resp = requests.get(
                f"{api_base}/messages",
                headers=headers, timeout=15, verify=ssl_verify,
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
                        dr = requests.get(
                            f"{api_base}/messages/{msg_id}",
                            headers=headers, timeout=15, verify=ssl_verify,
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
        time.sleep(OTP_POLL_INTERVAL)

    return None


def wait_for_code_by_provider(email: str, mail_provider: str = "",
                              mail_api_key: str = "",
                              timeout: int = 120) -> str | None:
    """Wait for verification code using the appropriate mail provider."""
    provider = (mail_provider or "gptmail").lower().strip()

    if provider == "yydsmail":
        return _yydsmail_wait_for_code(email, mail_api_key=mail_api_key, timeout=timeout)
    else:
        # Default to GPTMail
        return wait_for_verification_code(email, timeout)


# ===================== Token Refresh (Re-login existing account) =====================
def refresh_account_token(email: str, password: str = "",
                          mail_provider: str = "", mail_api_key: str = "") -> dict:
    """
    Re-login an existing Rita.ai account to get a fresh token.
    Optimized: explicit emailCode, resend on timeout, captcha retry.

    Returns: {"token": "...", "email": "...", "ticket": "..."}
    """
    cfg = _get_live_config()
    session = requests.Session()
    session.verify = not cfg["DISABLE_SSL_VERIFY"]
    headers = _gosplit_headers()
    redirect_uri = "https://www.rita.ai/zh/ai-chat"

    # Step 1: authenticate (init)
    _log(f"🔄 Refresh: authenticate (email={email})", "DEBUG")
    r = session.post(
        f"{GOSPLIT_API}/authorize/authenticate",
        headers=headers,
        json={"redirect_uri": redirect_uri},
        timeout=30,
    )
    r.raise_for_status()

    # Step 2: sign_process (email + agree)
    _log("🔄 Refresh: sign_process", "DEBUG")
    r = session.post(
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
            r = session.post(
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
        r = session.post(
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
                session.post(
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
    r = session.post(
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
            session.post(
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
            r = session.post(
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
        token = session.cookies.get("token", "")
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
    r = session.post(
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
    session = requests.Session()
    session.verify = not cfg["DISABLE_SSL_VERIFY"]
    headers = _gosplit_headers()
    headers["token"] = token

    # Step 1: authorize/url
    r = session.post(
        f"{GOSPLIT_API}/authorize/url",
        headers=_gosplit_headers(),
        json={"login_url": "https://account.rita.ai", "source_url": "https://www.rita.ai"},
        timeout=15,
    )
    r.raise_for_status()

    # Step 2: authenticate with token
    r = session.post(
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

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


# ===================== GPTMail — Temporary Email =====================
def _gptmail_headers():
    h = {"Accept": "application/json"}
    if GPTMAIL_API_KEY:
        h["X-API-Key"] = GPTMAIL_API_KEY
    return h


def create_temp_email() -> tuple[str, None]:
    """Create a temporary email via GPTMail. Returns email address."""
    resp = requests.post(
        f"{GPTMAIL_API_BASE}/api/generate-email",
        headers=_gptmail_headers(),
        json={},
        timeout=15,
        verify=not _DISABLE_SSL_VERIFY,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise Exception(f"GPTMail create failed: {data}")
    email = data["data"]["email"]
    return email


def wait_for_verification_code(email: str, timeout: int = 120) -> str | None:
    """Poll GPTMail for verification code from Rita.ai."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            resp = requests.get(
                f"{GPTMAIL_API_BASE}/api/emails",
                params={"email": email},
                headers=_gptmail_headers(),
                timeout=15,
                verify=not _DISABLE_SSL_VERIFY,
            )
            resp.raise_for_status()
            data = resp.json()
            emails = data.get("data", {}).get("emails", [])
            if not emails:
                emails = data.get("data", []) if isinstance(data.get("data"), list) else []

            for mail in emails:
                # Get email detail
                mail_id = mail.get("id")
                if not mail_id:
                    continue
                detail_resp = requests.get(
                    f"{GPTMAIL_API_BASE}/api/email/{mail_id}",
                    headers=_gptmail_headers(),
                    timeout=15,
                    verify=not _DISABLE_SSL_VERIFY,
                )
                detail_resp.raise_for_status()
                detail = detail_resp.json().get("data", {})
                content = detail.get("content", "") or detail.get("html_content", "") or ""

                code = _extract_code(content)
                if code:
                    return code
        except Exception as e:
            _log(f"📧 Mail poll error: {e}", "DEBUG")

        elapsed = int(time.time() - start)
        _log(f"📧 Waiting for verification code... ({elapsed}s/{timeout}s)", "DEBUG")
        time.sleep(4)

    return None


def _extract_code(content: str) -> str | None:
    """Extract 4-6 digit verification code from email content."""
    if not content:
        return None
    patterns = [
        r"(?:code|验证码|Code|CODE)[\s:：]*(\d{4,6})",
        r">(\d{4,6})<",
        r"(?<![#&\d])\b(\d{4})\b(?!\d)",
    ]
    for p in patterns:
        matches = re.findall(p, content, re.IGNORECASE)
        for code in matches:
            if len(code) >= 4 and code not in ("0000", "1234"):
                return code
    return None


# ===================== YesCaptcha — reCAPTCHA v2 Solver =====================
def solve_recaptcha() -> str | None:
    """Solve reCAPTCHA v2 via YesCaptcha. Returns g-recaptcha-response."""
    if not YESCAPTCHA_KEY:
        raise Exception("YESCAPTCHA_KEY not configured")

    # Create task
    create_resp = requests.post(
        f"{YESCAPTCHA_API}/createTask",
        json={
            "clientKey": YESCAPTCHA_KEY,
            "task": {
                "type": "NoCaptchaTaskProxyless",
                "websiteURL": RECAPTCHA_URL,
                "websiteKey": RECAPTCHA_SITEKEY,
            },
        },
        timeout=30,
        verify=not _DISABLE_SSL_VERIFY,
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
            json={"clientKey": YESCAPTCHA_KEY, "taskId": task_id},
            timeout=15,
            verify=not _DISABLE_SSL_VERIFY,
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
        "Origin": "https://account.rita.ai",
        "Referer": "https://account.rita.ai/",
    }


def register_rita_account(email: str) -> dict:
    """
    Full registration flow for Rita.ai.

    Returns: {"token": "...", "email": "...", "ticket": "..."}
    Raises: Exception on failure
    """
    session = requests.Session()
    session.verify = not _DISABLE_SSL_VERIFY
    headers = _gosplit_headers()

    # Step 1: authorize/url
    _log("📝 Step 1/7: authorize/url", "DEBUG")
    r = session.post(
        f"{GOSPLIT_API}/authorize/url",
        headers=headers,
        json={"login_url": "https://account.rita.ai", "source_url": "https://www.rita.ai"},
        timeout=15,
    )
    r.raise_for_status()

    # Step 2: authenticate (init)
    _log("📝 Step 2/7: authenticate init", "DEBUG")
    r = session.post(
        f"{GOSPLIT_API}/authorize/authenticate",
        headers=headers,
        json={"redirect_uri": "https://www.rita.ai/zh/ai-chat"},
        timeout=15,
    )
    r.raise_for_status()

    # Step 3: sign_process — submit email
    _log(f"📝 Step 3/7: sign_process (email: {email})", "DEBUG")
    r = session.post(
        f"{GOSPLIT_API}/authorize/sign_process",
        headers=headers,
        json={"redirect_uri": "https://www.rita.ai/zh/ai-chat", "language": "zh", "email": email},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    _log(f"   process={data.get('data', {}).get('process')}, register_type={data.get('data', {}).get('register_type')}", "DEBUG")

    # Step 4: sign_process — agree terms
    _log("📝 Step 4/7: sign_process (agree)", "DEBUG")
    r = session.post(
        f"{GOSPLIT_API}/authorize/sign_process",
        headers=headers,
        json={"redirect_uri": "https://www.rita.ai/zh/ai-chat", "language": "zh", "email": email, "agree": 1},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()

    if data.get("data", {}).get("need_captcha"):
        # Step 5: solve reCAPTCHA and submit
        _log("📝 Step 5/7: solving reCAPTCHA...", "INFO")
        captcha_response = solve_recaptcha()
        if not captcha_response:
            raise Exception("Failed to solve reCAPTCHA")

        r = session.post(
            f"{GOSPLIT_API}/authorize/sign_process",
            headers=headers,
            json={
                "redirect_uri": "https://www.rita.ai/zh/ai-chat",
                "language": "zh",
                "g-recaptcha-response": captcha_response,
            },
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        if data.get("code") != 0:
            raise Exception(f"sign_process captcha failed: {data}")
        _log("✅ reCAPTCHA verified", "DEBUG")
    else:
        _log("📝 Step 5/7: no captcha needed, skipped", "DEBUG")

    # Step 6: wait for email code and submit
    _log("📝 Step 6/7: waiting for email verification code...", "INFO")
    code = wait_for_verification_code(email, timeout=120)
    if not code:
        raise Exception("Failed to receive verification code")

    _log(f"📧 Got verification code: {code}", "DEBUG")
    r = session.post(
        f"{GOSPLIT_API}/authorize/code_sign",
        headers=headers,
        json={
            "email": email,
            "code": code,
            "redirect_uri": "https://www.rita.ai/zh/ai-chat",
            "language": "zh",
            "agreeTC": 1,
        },
        timeout=15,
    )
    r.raise_for_status()
    code_sign_data = r.json()
    if code_sign_data.get("code") != 0:
        raise Exception(f"code_sign failed: {code_sign_data}")

    # Extract token from code_sign response
    token = code_sign_data.get("data", {}).get("token", "")
    if not token:
        # Token might be in cookies
        token = session.cookies.get("token", "")
    if not token:
        # Check response headers
        for cookie in r.cookies:
            if cookie.name == "token":
                token = cookie.value
                break

    if not token:
        raise Exception(f"No token in code_sign response: {code_sign_data}")

    _log(f"🔑 Got token: {token[:8]}...", "SUCCESS")

    # Step 7: authenticate with token to get ticket
    _log("📝 Step 7/7: authenticate (get ticket)", "DEBUG")
    auth_headers = {**headers, "token": token}
    r = session.post(
        f"{GOSPLIT_API}/authorize/authenticate",
        headers=auth_headers,
        json={"redirect_uri": "https://www.rita.ai/zh/ai-chat"},
        timeout=15,
    )
    r.raise_for_status()
    auth_data = r.json()
    ticket = auth_data.get("data", {}).get("ticket", "")

    # Set password silently
    try:
        session.post(
            f"{GOSPLIT_API}/user/silent_edit",
            headers=auth_headers,
            json={"password": AUTO_REGISTER_PASSWORD, "language": "zh"},
            timeout=15,
        )
    except Exception:
        pass  # Non-critical

    _log(f"✅ Registration complete! token={token[:8]}..., ticket={ticket[:8] if ticket else 'N/A'}...", "SUCCESS")

    return {"token": token, "email": email, "ticket": ticket}


# ===================== Orchestrator =====================
def auto_register_one(account_manager=None, upstream_url="", origin="") -> dict | None:
    """
    Register one new Rita.ai account and optionally add it to AccountManager.

    Returns: {"token": ..., "email": ..., "account_id": ...} or None on failure
    """
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
            # Derive a name from email
            name_part = email.split("@")[0]
            acc = account_manager.add(
                token=token, name=f"auto-{name_part}",
                email=email, password=AUTO_REGISTER_PASSWORD,
                mail_provider="gptmail", mail_api_key=GPTMAIL_API_KEY,
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
    Background thread: when active accounts drop below AUTO_REGISTER_MIN_ACTIVE,
    automatically register new accounts to replenish the pool.
    """
    global _log_fn
    if log_fn:
        _log_fn = log_fn

    if not AUTO_REGISTER_ENABLED:
        _log("⏸ Auto-register disabled (set AUTO_REGISTER_ENABLED=1 to enable)", "INFO")
        return

    if not YESCAPTCHA_KEY:
        _log("⚠️ Auto-register disabled: YESCAPTCHA_KEY not set", "WARNING")
        return

    if not GPTMAIL_API_KEY:
        _log("⚠️ Auto-register disabled: GPTMAIL_API_KEY not set", "WARNING")
        return

    def loop():
        # Wait 60s after startup
        time.sleep(60)
        _log(f"🔄 Auto-replenish started (min_active={AUTO_REGISTER_MIN_ACTIVE}, "
             f"batch={AUTO_REGISTER_BATCH}, interval={check_interval}s)", "INFO")

        while True:
            try:
                with _replenish_lock:
                    summary = account_manager.summary()
                    active = summary.get("active", 0)

                    if active < AUTO_REGISTER_MIN_ACTIVE:
                        need = AUTO_REGISTER_MIN_ACTIVE - active
                        to_create = min(need, AUTO_REGISTER_BATCH)
                        _log(f"⚠️ Active accounts ({active}) below minimum ({AUTO_REGISTER_MIN_ACTIVE}), "
                             f"registering {to_create} new account(s)...", "WARNING")
                        auto_register_batch(to_create, account_manager, upstream_url, origin)
                    else:
                        _log(f"✅ Active accounts: {active} (min: {AUTO_REGISTER_MIN_ACTIVE})", "DEBUG")
            except Exception as e:
                _log(f"❌ Auto-replenish error: {e}", "ERROR")

            time.sleep(check_interval)

    t = threading.Thread(target=loop, daemon=True, name="auto-replenish")
    t.start()


# ===================== Config Check =====================
def check_config() -> dict:
    """Check if auto-register dependencies are configured."""
    return {
        "auto_register_enabled": AUTO_REGISTER_ENABLED,
        "yescaptcha_configured": bool(YESCAPTCHA_KEY),
        "gptmail_configured": bool(GPTMAIL_API_KEY),
        "gptmail_api_base": GPTMAIL_API_BASE,
        "yydsmail_configured": bool(YYDSMAIL_API_KEY),
        "yydsmail_api_base": YYDSMAIL_API_BASE,
        "recaptcha_sitekey": RECAPTCHA_SITEKEY,
        "min_active_accounts": AUTO_REGISTER_MIN_ACTIVE,
        "batch_size": AUTO_REGISTER_BATCH,
        "ready": bool(YESCAPTCHA_KEY and (GPTMAIL_API_KEY or YYDSMAIL_API_KEY)),
    }


# ===================== YYDS Mail Support =====================
def _yydsmail_wait_for_code(email: str, mail_api_key: str = "",
                            timeout: int = 120) -> str | None:
    """Poll YYDS Mail for verification code. Needs a mail_token from account creation."""
    api_key = mail_api_key or YYDSMAIL_API_KEY
    if not api_key:
        _log("⚠️ YYDS Mail: no API key", "WARNING")
        return None

    headers = {"X-API-Key": api_key}
    start = time.time()

    # For YYDS Mail we need to create a session for the email first
    # We'll look up messages using the address as identifier
    while time.time() - start < timeout:
        try:
            resp = requests.get(
                f"{YYDSMAIL_API_BASE}/messages",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=15,
                verify=not _DISABLE_SSL_VERIFY,
            )
            if resp.status_code == 200:
                data = resp.json()
                messages = data if isinstance(data, list) else data.get("data", [])
                for msg in messages:
                    msg_id = msg.get("id")
                    if not msg_id:
                        continue
                    detail_resp = requests.get(
                        f"{YYDSMAIL_API_BASE}/messages/{msg_id}",
                        headers={"Authorization": f"Bearer {api_key}"},
                        timeout=15,
                        verify=not _DISABLE_SSL_VERIFY,
                    )
                    if detail_resp.status_code == 200:
                        detail = detail_resp.json()
                        detail_data = detail.get("data", detail) if isinstance(detail, dict) else detail
                        content = (detail_data.get("text", "") or
                                   detail_data.get("html", "") or "")
                        code = _extract_code(content)
                        if code:
                            return code
        except Exception as e:
            _log(f"📧 YYDS Mail poll error: {e}", "DEBUG")

        elapsed = int(time.time() - start)
        _log(f"📧 YYDS Mail waiting... ({elapsed}s/{timeout}s)", "DEBUG")
        time.sleep(4)

    return None


def wait_for_code_by_provider(email: str, mail_provider: str = "",
                              mail_api_key: str = "",
                              timeout: int = 120) -> str | None:
    """Wait for verification code using the appropriate mail provider."""
    provider = (mail_provider or "gptmail").lower().strip()

    if provider == "yydsmail":
        return _yydsmail_wait_for_code(email, mail_api_key, timeout)
    else:
        # Default to GPTMail
        return wait_for_verification_code(email, timeout)


# ===================== Token Refresh (Re-login existing account) =====================
def refresh_account_token(email: str, password: str = "",
                          mail_provider: str = "", mail_api_key: str = "") -> dict:
    """
    Re-login an existing Rita.ai account to get a fresh token.

    For existing accounts, the sign_process will detect the account exists
    (process != 1) and may allow login with email code without reCAPTCHA,
    or may still require reCAPTCHA.

    Returns: {"token": "...", "email": "...", "ticket": "..."}
    """
    session = requests.Session()
    session.verify = not _DISABLE_SSL_VERIFY
    headers = _gosplit_headers()

    # Step 1: authorize/url
    _log(f"🔄 Refresh: authorize/url (email={email})", "DEBUG")
    r = session.post(
        f"{GOSPLIT_API}/authorize/url",
        headers=headers,
        json={"login_url": "https://account.rita.ai", "source_url": "https://www.rita.ai"},
        timeout=15,
    )
    r.raise_for_status()

    # Step 2: authenticate (init)
    _log("🔄 Refresh: authenticate init", "DEBUG")
    r = session.post(
        f"{GOSPLIT_API}/authorize/authenticate",
        headers=headers,
        json={"redirect_uri": "https://www.rita.ai/zh/ai-chat"},
        timeout=15,
    )
    r.raise_for_status()

    # Step 3: sign_process — submit email
    _log("🔄 Refresh: sign_process (email)", "DEBUG")
    r = session.post(
        f"{GOSPLIT_API}/authorize/sign_process",
        headers=headers,
        json={"redirect_uri": "https://www.rita.ai/zh/ai-chat", "language": "zh", "email": email},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    process = data.get("data", {}).get("process")
    _log(f"   process={process}", "DEBUG")

    # Step 4: sign_process — agree
    _log("🔄 Refresh: sign_process (agree)", "DEBUG")
    r = session.post(
        f"{GOSPLIT_API}/authorize/sign_process",
        headers=headers,
        json={"redirect_uri": "https://www.rita.ai/zh/ai-chat", "language": "zh",
              "email": email, "agree": 1},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()

    # Step 5: handle captcha if needed
    if data.get("data", {}).get("need_captcha"):
        _log("🔄 Refresh: solving reCAPTCHA...", "INFO")
        captcha_response = solve_recaptcha()
        if not captcha_response:
            raise Exception("Failed to solve reCAPTCHA for refresh")

        r = session.post(
            f"{GOSPLIT_API}/authorize/sign_process",
            headers=headers,
            json={
                "redirect_uri": "https://www.rita.ai/zh/ai-chat",
                "language": "zh",
                "g-recaptcha-response": captcha_response,
            },
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        if data.get("code") != 0:
            raise Exception(f"Refresh captcha failed: {data}")

    # Step 6: wait for email verification code
    _log("🔄 Refresh: waiting for email code...", "INFO")
    code = wait_for_code_by_provider(email, mail_provider, mail_api_key, timeout=120)
    if not code:
        raise Exception(f"Failed to receive verification code for {email}")

    _log(f"📧 Refresh: got code {code}", "DEBUG")
    r = session.post(
        f"{GOSPLIT_API}/authorize/code_sign",
        headers=headers,
        json={
            "email": email, "code": code,
            "redirect_uri": "https://www.rita.ai/zh/ai-chat",
            "language": "zh", "agreeTC": 1,
        },
        timeout=15,
    )
    r.raise_for_status()
    code_sign_data = r.json()
    if code_sign_data.get("code") != 0:
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

    # Step 7: authenticate with new token
    _log("🔄 Refresh: authenticate (get ticket)", "DEBUG")
    auth_headers = {**headers, "token": token}
    r = session.post(
        f"{GOSPLIT_API}/authorize/authenticate",
        headers=auth_headers,
        json={"redirect_uri": "https://www.rita.ai/zh/ai-chat"},
        timeout=15,
    )
    r.raise_for_status()
    auth_data = r.json()
    ticket = auth_data.get("data", {}).get("ticket", "")

    _log(f"✅ Token refreshed! token={token[:8]}...", "SUCCESS")
    return {"token": token, "email": email, "ticket": ticket}

"""
rita2api — OpenAI-compatible reverse proxy for rita.ai

Bridges the OpenAI Chat Completions API to rita.ai's /aichat/completions endpoint.
Supports streaming SSE, multi-account rotation with WebUI management, and tool calling.
"""

import json
import os
import re
import time
import hashlib
import datetime
import requests
from dotenv import load_dotenv
from flask import Flask, request, Response, jsonify, render_template, session
from threading import Lock

# MUST load .env before importing AccountManager (which reads env vars at module level)
load_dotenv()

from accounts import AccountManager
import auto_register
from quota import get_cost, get_all_costs
from database import get_db

# ===================== Configuration =====================
DEBUG_MODE = os.getenv("DEBUG", "1") == "1"
UPSTREAM_URL = os.getenv("RITA_UPSTREAM", "https://api_v2.rita.ai")
RITA_ORIGIN = os.getenv("RITA_ORIGIN", "https://www.rita.ai")
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "10089"))
# Disable SSL verification for api_v2.rita.ai (hostname mismatch in upstream cert)
DISABLE_SSL_VERIFY = os.getenv("DISABLE_SSL_VERIFY", "0") == "1"
AUTH_TOKEN = os.getenv("AUTH_TOKEN", "")

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", os.urandom(24).hex())
acm = AccountManager()
_conv_lock = Lock()

# ===================== Auth Middleware =====================
def _check_auth() -> bool:
    """Return True if the request is authenticated (or auth is disabled)."""
    if not AUTH_TOKEN:
        return True
    # Check Authorization header
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer ") and auth_header[7:] == AUTH_TOKEN:
        return True
    # Check query param
    if request.args.get("auth") == AUTH_TOKEN:
        return True
    # Check session cookie
    if session.get("auth_token") == AUTH_TOKEN:
        return True
    return False

@app.before_request
def require_auth():
    path = request.path
    # Always allow: root WebUI page and /v1/* (client-facing proxy)
    if path == "/" or path.startswith("/v1/") or path == "/health":
        return None
    # Require auth for /api/* and /debug/*
    if path.startswith("/api/") or path.startswith("/debug/"):
        # Allow the login and auth-check endpoints without auth
        if path in ("/api/login", "/api/auth/check"):
            return None
        if not _check_auth():
            return jsonify({"error": "Unauthorized", "auth_required": True}), 401
    return None

# ===================== Usage Statistics =====================
_stats_lock = Lock()
_usage_stats = {
    "total_requests": 0,
    "total_tokens_approx": 0,
    "requests_by_model": {},
    "requests_today": 0,
    "requests_today_date": datetime.date.today().isoformat(),
    "last_reset": time.time(),
    "uptime_start": time.time(),
}

def _increment_stats(model: str, messages: list, response_text: str = ""):
    with _stats_lock:
        # Reset today counter if day changed
        today = datetime.date.today().isoformat()
        if _usage_stats["requests_today_date"] != today:
            _usage_stats["requests_today"] = 0
            _usage_stats["requests_today_date"] = today

        _usage_stats["total_requests"] += 1
        _usage_stats["requests_today"] += 1
        _usage_stats["requests_by_model"][model] = \
            _usage_stats["requests_by_model"].get(model, 0) + 1

        # Rough token estimate: chars / 4
        msg_chars = sum(len(str(m.get("content", ""))) for m in messages)
        resp_chars = len(response_text)
        _usage_stats["total_tokens_approx"] += (msg_chars + resp_chars) // 4

# In-memory conversation state
_conversation_state: dict[str, dict] = {}

# Cache
_models_cache: dict = {}
_models_cache_ts: float = 0
MODELS_CACHE_TTL = 3600

_ai_tools_cache: dict = {}
_ai_tools_cache_ts: float = 0
AI_TOOLS_CACHE_TTL = 3600

# ===================== Logging =====================
def log(msg, level="INFO"):
    if not DEBUG_MODE and level == "DEBUG":
        return
    color = {"INFO": "\033[94m", "DEBUG": "\033[92m", "WARNING": "\033[93m",
             "ERROR": "\033[91m", "SUCCESS": "\033[92m"}.get(level, "")
    print(f"{color}[{time.strftime('%H:%M:%S')}] {msg}\033[0m")

# ===================== Message Format Translation =====================
def extract_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif block.get("type") == "image_url":
                    parts.append("[image]")
        return "".join(parts)
    return str(content) if content else ""

def build_rita_messages(messages: list) -> list:
    rita_msgs = []
    system_parts = []
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if role == "system":
            system_parts.append(extract_text(content))
        elif role in ("user", "assistant"):
            text = extract_text(content)
            if text:
                rita_msgs.append({"type": "text", "text": text})
    if system_parts and rita_msgs:
        rita_msgs[0]["text"] = "<system>\n" + "\n".join(system_parts) + "\n</system>\n\n" + rita_msgs[0]["text"]
    return rita_msgs

# ===================== Model Resolution =====================
def resolve_rita_model(model: str) -> str:
    model_lower = model.lower()
    mappings = {
        "rita": "model_25",
        "gpt-4o": "gpt-4o", "gpt-4o-mini": "gpt-4o-mini",
        "gpt-4-turbo": "gpt-4-turbo", "gpt-4": "gpt-4",
        "gpt-3.5-turbo": "gpt-3.5-turbo", "chatgpt-4o-latest": "chatgpt-4o-latest",
        "claude-sonnet-4-6": "claude-4.6", "claude-opus-4-6": "claude-opus-4-6",
        "claude-4.6": "claude-4.6", "claude-haiku-4-5": "claude-4.5-haiku",
        "claude-3.5-sonnet": "claude-3.5-sonnet", "claude-3.5-haiku": "claude-3.5-haiku",
        "gemini-2.0-flash": "gemini-2.0-flash", "gemini-2.5-pro": "gemini-2.5-pro",
        "gemini-2.5-flash": "gemini-2.5-flash", "gemini-1.5-flash": "gemini-1.5-flash",
        "gemini-1.5-pro": "gemini-1.5-pro",
        "o1-preview": "reasoning-preview", "o1-mini": "reasoning-mini", "o1": "reasoning",
        "grok-3": "grok-3", "grok-2": "grok-2",
        "mistral-large": "mistral-large", "mixtral-8x7b": "mixtral-8x7b",
        "deepseek-v3": "deepseek-v3", "deepseek-r1": "deepseek-r1",
    }
    if model_lower in mappings:
        return mappings[model_lower]
    for key, value in mappings.items():
        if model_lower.startswith(key):
            return value
    return model

# ===================== Tool Calling =====================
_TOOL_PROMPT_TMPL = (
    "You have access to the following tools. When you need to use a tool, "
    "respond ONLY with a JSON object (no other text):\n"
    "{tool_defs}\n"
    "Format:\n"
    '  Single tool: {{"tool":"tool_name","args":{{"param":"value"}}}}\n'
    '  Multiple tools: {{"calls":[{{"tool":"name","args":{{}}}}]}}\n'
    "If no tool is needed, respond normally.\n\n"
    "{query}"
)
_tool_prompt_cache: dict[str, str] = {}

def _tools_hash(tools: list) -> str:
    key = json.dumps([
        t.get("name") or t.get("function", {}).get("name", "")
        for t in tools
    ], ensure_ascii=False)
    return hashlib.md5(key.encode()).hexdigest()[:12]

def _compact_tools(tools: list) -> str:
    parts = []
    for t in tools:
        if t.get("type") == "function":
            fn = t["function"]
            name = fn.get("name", "")
            props = fn.get("parameters", {}).get("properties", {})
            req = set(fn.get("parameters", {}).get("required", []))
        elif "input_schema" in t:
            name = t.get("name", "")
            props = t.get("input_schema", {}).get("properties", {})
            req = set(t.get("input_schema", {}).get("required", []))
        else:
            continue
        params = [f"{p}{'*' if p in req else '?'}" for p in props]
        parts.append(f"{name}({','.join(params)})" if params else name)
    return " | ".join(parts)

def inject_tool_prompt(user_text: str, tools: list) -> str:
    th = _tools_hash(tools)
    if th not in _tool_prompt_cache:
        _tool_prompt_cache[th] = _compact_tools(tools)
    return _TOOL_PROMPT_TMPL.format(tool_defs=_tool_prompt_cache[th], query=user_text)

_JSON_RE = re.compile(r'\{.*\}', re.DOTALL)

def parse_tool_response(raw: str) -> dict:
    m = _JSON_RE.search(raw)
    if not m:
        return {"type": "text", "text": raw}
    try:
        obj = json.loads(m.group())
    except json.JSONDecodeError:
        return {"type": "text", "text": raw}
    if "tool" in obj and "args" in obj:
        return {"type": "tool_calls", "calls": [{"name": obj["tool"], "input": obj["args"]}]}
    calls_raw = obj.get("calls", [])
    if calls_raw:
        calls = []
        for c in calls_raw:
            name = c.get("tool") or c.get("name", "")
            inp = c.get("args") or c.get("parameters") or c.get("input") or {}
            calls.append({"name": name, "input": inp})
        return {"type": "tool_calls", "calls": calls}
    return {"type": "text", "text": raw}

# ===================== Conversation State =====================
def get_conv_key(messages: list) -> str:
    key_parts = []
    for msg in messages[:-1]:
        role = msg.get("role", "")
        content = extract_text(msg.get("content", ""))[:100]
        key_parts.append(f"{role}:{content}")
    return hashlib.md5("|".join(key_parts).encode()).hexdigest()[:16]

def get_or_create_conversation(messages: list) -> tuple[int, str]:
    key = get_conv_key(messages)
    with _conv_lock:
        state = _conversation_state.get(key, {})
        return state.get("chat_id", 0), state.get("parent", "0")

def update_conversation_state(messages: list, assistant_msg_id: str, chat_id: int):
    key = get_conv_key(messages)
    with _conv_lock:
        _conversation_state[key] = {
            "chat_id": chat_id,
            "parent": assistant_msg_id,
            "last_updated": time.time(),
        }

def _detect_chinese(messages: list) -> bool:
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, list):
            for block in content:
                if block.get("type") == "text":
                    if re.search(r"[\u4e00-\u9fff]", block.get("text", "")):
                        return True
        elif isinstance(content, str):
            if re.search(r"[\u4e00-\u9fff]", content):
                return True
    return False


# =========================================================================
#  WebUI
# =========================================================================
@app.route("/", methods=["GET"])
def webui():
    return render_template("index.html")

# =========================================================================
#  Auth API
# =========================================================================
@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.json or {}
    token = data.get("token", "").strip()
    if not AUTH_TOKEN:
        # No auth configured — always succeed
        return jsonify({"ok": True, "auth_required": False})
    if token == AUTH_TOKEN:
        session["auth_token"] = AUTH_TOKEN
        return jsonify({"ok": True, "auth_required": True})
    return jsonify({"ok": False, "error": "Invalid token"}), 401

@app.route("/api/auth/check", methods=["GET"])
def api_auth_check():
    return jsonify({
        "auth_required": bool(AUTH_TOKEN),
        "authenticated": _check_auth(),
    })

# =========================================================================
#  Stats API
# =========================================================================
@app.route("/api/stats", methods=["GET"])
def api_stats():
    db = get_db()
    db_stats = db.get_usage_stats()
    with _stats_lock:
        uptime_secs = int(time.time() - _usage_stats["uptime_start"])
    summary = acm.summary()
    return jsonify({
        **db_stats,
        "uptime_seconds": uptime_secs,
        "uptime_start": _usage_stats["uptime_start"],
        "total_quota": summary.get("total_quota", 0),
        "active_accounts": summary.get("active", 0),
        "model_costs": get_all_costs(),
    })

# =========================================================================
#  Account Management API  (/api/accounts/*)
# =========================================================================
@app.route("/api/accounts", methods=["GET"])
def api_list_accounts():
    return jsonify({"accounts": acm.list_all()})

@app.route("/api/accounts/summary", methods=["GET"])
def api_account_summary():
    return jsonify(acm.summary())

@app.route("/api/accounts", methods=["POST"])
def api_add_account():
    """Add a single account: {token, visitorid?, name?, email?, password?, mail_provider?, mail_api_key?}"""
    data = request.json or {}
    token = data.get("token", "").strip()
    if not token:
        return jsonify({"error": "token is required"}), 400
    acc = acm.add(
        token=token,
        visitorid=data.get("visitorid", "").strip(),
        name=data.get("name", "").strip(),
        email=data.get("email", "").strip(),
        password=data.get("password", "").strip(),
        mail_provider=data.get("mail_provider", "").strip(),
        mail_api_key=data.get("mail_api_key", "").strip(),
    )
    log(f"➕ Account added: {acc.name} ({acc.id})", "SUCCESS")
    return jsonify({"ok": True, "account": acc.to_status()}), 201

@app.route("/api/accounts/batch", methods=["POST"])
def api_batch_add():
    """
    Batch add accounts.
    Body: { "accounts": [ {token, visitorid?, name?}, ... ] }
    """
    data = request.json or {}
    items = data.get("accounts", [])
    if not items:
        return jsonify({"error": "accounts array is required"}), 400
    added = acm.add_batch(items)
    log(f"📦 Batch added {len(added)} accounts", "SUCCESS")
    return jsonify({
        "ok": True,
        "added": len(added),
        "accounts": [a.to_status() for a in added],
    }), 201

@app.route("/api/accounts/<account_id>", methods=["PUT"])
def api_update_account(account_id):
    data = request.json or {}
    acc = acm.update(account_id, **data)
    if not acc:
        return jsonify({"error": "not found"}), 404
    log(f"✏️ Account updated: {acc.name} ({acc.id})", "INFO")
    return jsonify({"ok": True, "account": acc.to_status()})

@app.route("/api/accounts/<account_id>", methods=["DELETE"])
def api_delete_account(account_id):
    if acm.delete(account_id):
        log(f"🗑 Account deleted: {account_id}", "WARNING")
        return jsonify({"ok": True})
    return jsonify({"error": "not found"}), 404

@app.route("/api/accounts/<account_id>/toggle", methods=["POST"])
def api_toggle_account(account_id):
    acc = acm.toggle(account_id)
    if not acc:
        return jsonify({"error": "not found"}), 404
    state = "enabled" if acc.enabled else "disabled"
    log(f"⏯ Account {acc.name} → {state}", "INFO")
    return jsonify({"ok": True, "enabled": acc.enabled})

@app.route("/api/accounts/<account_id>/test", methods=["POST"])
def api_test_account(account_id):
    result = acm.test_account(account_id, UPSTREAM_URL, RITA_ORIGIN)
    return jsonify(result)

@app.route("/api/accounts/test-all", methods=["POST"])
def api_test_all():
    accounts = acm.list_all()
    results = {}
    ok_count = 0
    for a in accounts:
        r = acm.test_account(a["id"], UPSTREAM_URL, RITA_ORIGIN)
        results[a["id"]] = r
        if r.get("ok"):
            ok_count += 1
    return jsonify({"results": results, "total": len(accounts), "ok_count": ok_count})

@app.route("/api/accounts/<account_id>/refresh", methods=["POST"])
def api_refresh_account(account_id):
    """Re-login to get a fresh token using stored email credentials."""
    acc = acm.get(account_id)
    if not acc:
        return jsonify({"error": "not found"}), 404
    if not acc.email:
        return jsonify({"error": "no email stored for this account, cannot refresh"}), 400

    log(f"🔄 Refreshing token for {acc.name} (email={acc.email})...", "INFO")
    try:
        result = auto_register.refresh_account_token(
            email=acc.email,
            password=acc.password,
            mail_provider=acc.mail_provider,
            mail_api_key=acc.mail_api_key,
        )
        new_token = result["token"]
        # Update account with new token and re-enable
        acm.reactivate_account(account_id, new_token=new_token)
        log(f"✅ Token refreshed for {acc.name}", "SUCCESS")
        return jsonify({"ok": True, "account": acc.to_status()})
    except Exception as e:
        log(f"❌ Token refresh failed for {acc.name}: {e}", "ERROR")
        return jsonify({"error": str(e)}), 500

@app.route("/api/accounts/reset", methods=["POST"])
def api_reset_failures():
    count = acm.reset_failures()
    log(f"🔄 Reset failures for {count} accounts", "INFO")
    return jsonify({"ok": True, "reset": count})

@app.route("/api/accounts/clear", methods=["DELETE"])
def api_clear_all():
    count = acm.delete_all()
    log(f"🧹 Cleared all {count} accounts", "WARNING")
    return jsonify({"ok": True, "deleted": count})

@app.route("/api/accounts/<account_id>/reactivate", methods=["POST"])
def api_reactivate_account(account_id):
    """Re-enable a disabled account, optionally with a new token."""
    data = request.json or {}
    new_token = data.get("token", "").strip()
    acc = acm.reactivate_account(account_id, new_token)
    if not acc:
        return jsonify({"error": "not found"}), 404
    log(f"♻️ Account reactivated: {acc.name} ({acc.id})" +
        (" with new token" if new_token else ""), "SUCCESS")
    return jsonify({"ok": True, "account": acc.to_status()})

@app.route("/api/accounts/purge-invalid", methods=["POST"])
def api_purge_invalid():
    """Remove all accounts with expired/invalid tokens."""
    count = acm.purge_invalid()
    log(f"🗑 Purged {count} invalid accounts", "WARNING")
    return jsonify({"ok": True, "purged": count})

@app.route("/api/health-check", methods=["GET"])
def api_health_check_status():
    """Return the latest background health check results."""
    return jsonify(acm.get_health_status())

@app.route("/api/health-check/run", methods=["POST"])
def api_run_health_check():
    """Trigger an immediate health check on all accounts."""
    accounts = acm.list_all()
    results = {}
    ok_count = 0
    disabled_count = 0
    for a in accounts:
        r = acm.test_account(a["id"], UPSTREAM_URL, RITA_ORIGIN)
        results[a["id"]] = r
        if r.get("ok"):
            ok_count += 1
        elif r.get("code") == 401:
            # Auto-disable expired token
            acc = acm.get(a["id"])
            if acc:
                acc.token_valid = False
                acc.enabled = False
                acc.disabled_reason = "token_expired_401"
                acc.last_error = "Token expired (manual check)"
                disabled_count += 1
                log(f"🔴 Auto-disabled {acc.name}: token expired (401)", "WARNING")
    if disabled_count > 0:
        acm._save()
    return jsonify({
        "results": results,
        "total": len(accounts),
        "ok_count": ok_count,
        "auto_disabled": disabled_count,
    })


# =========================================================================
#  Mail Verification Code Query API
# =========================================================================
@app.route("/api/mail/check-code", methods=["POST"])
def api_check_mail_code():
    """Query the latest verification code for an email address.
    Body: { "email": "...", "mail_provider": "gptmail", "mail_api_key": "..." }
    """
    data = request.json or {}
    email = data.get("email", "").strip()
    if not email:
        return jsonify({"error": "email is required"}), 400

    mail_provider = data.get("mail_provider", "gptmail").strip()
    mail_api_key = data.get("mail_api_key", "").strip()

    try:
        code = auto_register.wait_for_code_by_provider(
            email=email,
            mail_provider=mail_provider,
            mail_api_key=mail_api_key,
            timeout=15,  # Quick check, not full wait
        )
        if code:
            return jsonify({"ok": True, "code": code, "email": email})
        else:
            return jsonify({"ok": False, "message": "No verification code found", "email": email})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/mail/status", methods=["GET"])
def api_mail_status():
    """Return mail service configuration status."""
    db = get_db()
    return jsonify({
        "gptmail": {
            "configured": bool(db.get_config("GPTMAIL_API_KEY")),
            "api_base": db.get_config("GPTMAIL_API_BASE", "https://mail.chatgpt.org.uk"),
        },
        "yydsmail": {
            "configured": bool(db.get_config("YYDSMAIL_API_KEY")),
            "api_base": db.get_config("YYDSMAIL_API_BASE", "https://maliapi.215.im/v1"),
        },
    })


# =========================================================================
#  Ticket API (re-login to get ticket)
# =========================================================================
@app.route("/api/accounts/<account_id>/ticket", methods=["POST"])
def api_get_ticket(account_id):
    """Get a fresh ticket for an account by re-authenticating with its token."""
    acc = acm.get(account_id)
    if not acc:
        return jsonify({"error": "not found"}), 404

    if not acc.token:
        return jsonify({"error": "no token set"}), 400

    try:
        result = auto_register.relogin_for_ticket(acc.token)
        if result.get("ticket"):
            return jsonify({"ok": True, "ticket": result["ticket"]})
        else:
            return jsonify({"ok": False, "message": "Could not get ticket, token may be expired"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# =========================================================================
#  Config Management API
# =========================================================================
@app.route("/api/config", methods=["GET"])
def api_get_config():
    """Return all config key-value pairs."""
    db = get_db()
    configs = db.get_all_config()
    # Mask sensitive values for display
    for c in configs:
        key = c["key"].upper()
        if any(s in key for s in ("TOKEN", "KEY", "PASSWORD", "SECRET")):
            val = c["value"]
            if val:
                c["value_masked"] = val[:4] + "***" + val[-4:] if len(val) > 8 else "***"
            else:
                c["value_masked"] = ""
        else:
            c["value_masked"] = c["value"]
    return jsonify({"configs": configs})

@app.route("/api/config", methods=["PUT"])
def api_set_config():
    """Update config values. Body: { "configs": { "key": "value", ... } }"""
    data = request.json or {}
    configs = data.get("configs", {})
    if not configs:
        return jsonify({"error": "configs dict is required"}), 400
    db = get_db()
    for key, value in configs.items():
        db.set_config(key, value)
    log(f"Config updated: {list(configs.keys())}", "INFO")
    return jsonify({"ok": True, "updated": list(configs.keys())})


# =========================================================================
#  Auto Registration API
# =========================================================================
@app.route("/api/auto-register/config", methods=["GET"])
def api_auto_register_config():
    """Check auto-register configuration status."""
    return jsonify(auto_register.check_config())

@app.route("/api/auto-register", methods=["POST"])
def api_auto_register():
    """Manually trigger registration of new account(s).
    Body: { "count": 1 }
    """
    data = request.json or {}
    count = min(data.get("count", 1), 5)  # Max 5 at a time

    config = auto_register.check_config()
    if not config["ready"]:
        missing = []
        if not config["yescaptcha_configured"]:
            missing.append("YESCAPTCHA_KEY")
        if not config["gptmail_configured"]:
            missing.append("GPTMAIL_API_KEY")
        return jsonify({
            "error": f"Auto-register not configured. Missing: {', '.join(missing)}",
            "config": config,
        }), 400

    log(f"🔄 Manual auto-register triggered: {count} account(s)", "INFO")
    results = auto_register.auto_register_batch(
        count=count,
        account_manager=acm,
        upstream_url=UPSTREAM_URL,
        origin=RITA_ORIGIN,
    )
    return jsonify({
        "ok": True,
        "registered": len(results),
        "requested": count,
        "accounts": results,
    })


# =========================================================================
#  Health
# =========================================================================
@app.route("/health", methods=["GET"])
def health():
    s = acm.summary()
    return jsonify({
        "status": "ok",
        **s,
        "upstream": UPSTREAM_URL,
    })


# =========================================================================
#  Model Catalog
# =========================================================================
@app.route("/v1/models", methods=["GET"])
def list_models():
    global _models_cache, _models_cache_ts
    now = time.time()
    if _models_cache and now - _models_cache_ts < MODELS_CACHE_TTL:
        return jsonify(_models_cache)

    acc, _ = acm.next()
    if not acc:
        return jsonify({"error": "no accounts configured"}), 500

    try:
        r = requests.post(
            f"{UPSTREAM_URL}/aichat/categoryModels",
            headers=acm.upstream_headers(acc, RITA_ORIGIN),
            json={"language": "zh"}, timeout=15,
            verify=not DISABLE_SSL_VERIFY,
        )
        r.raise_for_status()
        upstream = r.json()
        acm.mark_ok(acc)

        data = []
        for cat in upstream.get("data", {}).get("category_models", []):
            cat_name = cat.get("name", "")
            for m in cat.get("models", []):
                mid = m.get("key", "")
                data.append({
                    "id": mid, "object": "model", "created": 0, "owned_by": "rita",
                    "name": m.get("name", mid), "description": m.get("desc", ""),
                    "quota": m.get("quota", 0), "tool": m.get("tool", ""),
                    "ability": m.get("ability", ""), "category": cat_name,
                })

        result = {"object": "list", "data": data}
        _models_cache, _models_cache_ts = result, now
        return jsonify(result)

    except Exception as e:
        log(f"⚠️ Failed to fetch model catalog: {e}", "WARNING")
        acm.mark_fail(acc, str(e))
        return jsonify({"error": str(e)}), 502


# =========================================================================
#  AI Tools
# =========================================================================
@app.route("/v1/tools", methods=["GET"])
def list_ai_tools():
    global _ai_tools_cache, _ai_tools_cache_ts
    now = time.time()
    if _ai_tools_cache and now - _ai_tools_cache_ts < AI_TOOLS_CACHE_TTL:
        return jsonify(_ai_tools_cache)

    acc, _ = acm.next()
    if not acc:
        return jsonify({"error": "no accounts configured"}), 500

    try:
        r = requests.post(
            f"{UPSTREAM_URL}/gamsai_api/v1/page_service/aiTools",
            headers=acm.upstream_headers(acc, RITA_ORIGIN),
            json={"language": "zh"}, timeout=15,
            verify=not DISABLE_SSL_VERIFY,
        )
        r.raise_for_status()
        upstream = r.json()
        acm.mark_ok(acc)

        tools = upstream.get("data", [])
        data = [{
            "id": t.get("id"), "name": t.get("name"),
            "description": t.get("description"),
            "tool_type": t.get("tool_type"), "price": t.get("price", 0),
            "rules": t.get("rules", {}),
        } for t in tools]

        result = {"object": "list", "data": data}
        _ai_tools_cache, _ai_tools_cache_ts = result, now
        return jsonify(result)

    except Exception as e:
        log(f"⚠️ Failed to fetch AI tools: {e}", "WARNING")
        acm.mark_fail(acc, str(e))
        return jsonify({"error": str(e)}), 502


@app.route("/v1/tools/execute", methods=["POST"])
def execute_ai_tool():
    data = request.json or {}
    tool_id = data.get("tool_id")
    if not tool_id:
        return jsonify({"error": "tool_id is required"}), 400

    acc, _ = acm.next()
    if not acc:
        return jsonify({"error": "no accounts configured"}), 500

    try:
        payload = {"action": data.get("action", "edit_prompt"), "prompt": data.get("prompt", "")}
        if data.get("image_url"):
            payload["image_url"] = data["image_url"]

        r = requests.post(
            f"{UPSTREAM_URL}/gamsai_api/v1/page_service/aiTools/{tool_id}/execute",
            headers=acm.upstream_headers(acc, RITA_ORIGIN),
            json=payload, timeout=60,
            verify=not DISABLE_SSL_VERIFY,
        )
        r.raise_for_status()
        acm.mark_ok(acc)
        return jsonify(r.json())

    except Exception as e:
        log(f"❌ AI tool execution error: {e}", "ERROR")
        acm.mark_fail(acc, str(e))
        return jsonify({"error": str(e)}), 502


# =========================================================================
#  Conversation Management
# =========================================================================
@app.route("/v1/conversations", methods=["POST"])
def list_conversations():
    data = request.json or {}
    acc, _ = acm.next()
    if not acc:
        return jsonify({"error": "no accounts configured"}), 500

    try:
        r = requests.post(
            f"{UPSTREAM_URL}/aichat/conversations",
            headers=acm.upstream_headers(acc, RITA_ORIGIN),
            json={"page": data.get("page", 1), "limit": data.get("limit", 20)},
            timeout=15,
            verify=not DISABLE_SSL_VERIFY,
        )
        r.raise_for_status()
        acm.mark_ok(acc)
        return jsonify(r.json())
    except Exception as e:
        acm.mark_fail(acc, str(e))
        return jsonify({"error": str(e)}), 502


@app.route("/v1/chat/init", methods=["POST"])
def new_conversation():
    acc, _ = acm.next()
    if not acc:
        return jsonify({"error": "no accounts configured"}), 500
    try:
        r = requests.post(
            f"{UPSTREAM_URL}/chatgpt/newConversation",
            headers=acm.upstream_headers(acc, RITA_ORIGIN),
            json={}, timeout=15,
            verify=not DISABLE_SSL_VERIFY,
        )
        r.raise_for_status()
        acm.mark_ok(acc)
        return jsonify(r.json())
    except Exception as e:
        acm.mark_fail(acc, str(e))
        return jsonify({"error": str(e)}), 502


@app.route("/v1/chat/title", methods=["POST"])
def get_title():
    data = request.json or {}
    acc, _ = acm.next()
    if not acc:
        return jsonify({"error": "no accounts configured"}), 500
    try:
        r = requests.post(
            f"{UPSTREAM_URL}/aichat/getTitle",
            headers=acm.upstream_headers(acc, RITA_ORIGIN),
            json={"chat_id": data.get("chat_id"), "messages": build_rita_messages(data.get("messages", []))},
            timeout=15,
            verify=not DISABLE_SSL_VERIFY,
        )
        r.raise_for_status()
        acm.mark_ok(acc)
        return jsonify(r.json())
    except Exception as e:
        acm.mark_fail(acc, str(e))
        return jsonify({"error": str(e)}), 502


# =========================================================================
#  Core Chat Completions
# =========================================================================
@app.route("/v1/chat/completions", methods=["POST"])
def chat_completions():
    data = request.json or {}
    messages = data.get("messages", [])
    model = data.get("model", "gpt-4o")
    stream = data.get("stream", False)
    client_tools = data.get("tools", [])

    log(f"📥 /v1/chat/completions model={model} stream={stream} msgs={len(messages)} tools={len(client_tools)}", "INFO")
    _increment_stats(model, messages)

    if not messages:
        return jsonify({"error": {"message": "messages is required", "type": "invalid_request_error"}}), 400

    # Get account (client header override or rotation)
    client_token = request.headers.get("token", "")
    client_visitorid = request.headers.get("visitorid", "")

    acc, _ = acm.next()
    if not acc and not client_token:
        return jsonify({"error": {"message": "no accounts configured", "type": "config_error"}}), 500

    try:
        rita_model = resolve_rita_model(model)
        chat_id, parent = get_or_create_conversation(messages)

        rita_messages = build_rita_messages(messages)

        if client_tools and rita_messages:
            last_text = rita_messages[-1]["text"]
            rita_messages[-1]["text"] = inject_tool_prompt(last_text, client_tools)
            log(f"🔧 tool prompt injected ({len(client_tools)} tools)", "DEBUG")

        payload = {
            "model": rita_model,
            "language": "zh" if _detect_chinese(messages) else "en",
            "messages": rita_messages,
            "online": 0,
            "model_type_id": 0,
            "chat_id": chat_id,
            "parent": parent,
        }

        # Build headers: prefer client-provided auth, otherwise use rotated account
        if client_token:
            hdrs = {
                "Content-Type": "application/json",
                "Origin": RITA_ORIGIN, "Referer": RITA_ORIGIN,
                "token": client_token,
            }
            if client_visitorid:
                hdrs["visitorid"] = client_visitorid
        else:
            hdrs = acm.upstream_headers(acc, RITA_ORIGIN)

        resp = requests.post(
            f"{UPSTREAM_URL}/aichat/completions",
            headers=hdrs, json=payload, stream=stream, timeout=120,
            verify=not DISABLE_SSL_VERIFY,
        )

        if resp.status_code >= 500:
            log(f"💥 Upstream 500: {resp.text[:200]}", "ERROR")
            if acc:
                acm.mark_fail(acc, f"HTTP {resp.status_code}")
            return jsonify({"error": {"message": "upstream error", "type": "upstream_error"}}), 502
        resp.raise_for_status()
        if acc:
            acm.mark_ok(acc)
            # Deduct quota points based on model
            cost = get_cost(rita_model)
            acm.deduct_quota(acc.id, cost)
            # Log usage to database
            db = get_db()
            msg_chars = sum(len(str(m.get("content", ""))) for m in messages)
            db.log_usage(acc.id, model, msg_chars // 4)

        if stream:
            def gen():
                cid = f"chatcmpl-{int(time.time()*1000)}"
                captured_msg_id = None

                with resp:
                    for line in resp.iter_lines(decode_unicode=True):
                        if not line or not line.startswith("data: "):
                            continue
                        raw = line[6:]
                        if raw == "[DONE]":
                            break
                        try:
                            obj = json.loads(raw)
                        except json.JSONDecodeError:
                            continue

                        etype = obj.get("type", "")
                        if etype == "quota_remain":
                            yield f'data: {json.dumps({"id": cid, "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {}, "finish_reason": None}], "x_quota": {"quota_remain": obj.get("quota_remain"), "service_quota_remain": obj.get("service_quota_remain")}})}\n\n'
                            continue
                        if etype == "assistant_complete":
                            break

                        rid = obj.get("id", "")
                        if not captured_msg_id and rid.startswith("ai"):
                            captured_msg_id = rid

                        choices = obj.get("choices", [])
                        if not choices:
                            continue
                        delta = choices[0].get("delta", {})
                        content = delta.get("content", "")
                        if content:
                            yield f'data: {json.dumps({"id": cid, "object": "chat.completion.chunk", "created": obj.get("created", int(time.time())), "model": model, "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}]})}\n\n'

                    if captured_msg_id:
                        update_conversation_state(messages, captured_msg_id, 0)

                yield f'data: {json.dumps({"id": cid, "object": "chat.completion.chunk", "created": int(time.time()), "model": model, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})}\n\n'
                yield 'data: [DONE]\n\n'

            return Response(gen(), mimetype="text/event-stream",
                           headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
        else:
            # Non-streaming
            upstream_data = resp.json()
            content = ""
            choices = upstream_data.get("choices", [])
            if choices:
                delta = choices[0].get("delta", {})
                content = delta.get("content", "")

            rid = upstream_data.get("id", "")
            captured_msg_id = rid if rid.startswith("ai") else f"msg_{int(time.time()*1000)}"
            update_conversation_state(messages, captured_msg_id, 0)

            if client_tools:
                parsed = parse_tool_response(content)
                if parsed["type"] == "tool_calls":
                    log(f"🔧 tool_calls: {[c['name'] for c in parsed['calls']]}", "INFO")
                    oai_tc = [{
                        "id": f"call_{i}", "type": "function",
                        "function": {"name": c["name"], "arguments": json.dumps(c["input"], ensure_ascii=False)},
                    } for i, c in enumerate(parsed["calls"])]
                    return jsonify({
                        "id": f"chatcmpl-{int(time.time()*1000)}", "object": "chat.completion",
                        "created": upstream_data.get("created", int(time.time())), "model": model,
                        "choices": [{"index": 0, "message": {"role": "assistant", "content": None, "tool_calls": oai_tc}, "finish_reason": "tool_calls"}],
                        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                    })
                content = parsed.get("text", content)

            return jsonify({
                "id": f"chatcmpl-{int(time.time()*1000)}", "object": "chat.completion",
                "created": upstream_data.get("created", int(time.time())), "model": model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            })

    except requests.RequestException as e:
        log(f"❌ Request error: {e}", "ERROR")
        if acc:
            acm.mark_fail(acc, str(e))
        return jsonify({"error": {"message": str(e), "type": "upstream_error"}}), 502
    except Exception as e:
        log(f"❌ Unexpected error: {e}", "ERROR")
        import traceback; traceback.print_exc()
        return jsonify({"error": {"message": str(e), "type": "internal_error"}}), 500


# =========================================================================
#  Debug
# =========================================================================
@app.route("/debug/state", methods=["GET"])
def debug_state():
    with _conv_lock:
        return jsonify({
            "conversations": len(_conversation_state),
            **acm.summary(),
            "upstream": UPSTREAM_URL,
        })

@app.route("/debug/clear", methods=["POST"])
def debug_clear():
    with _conv_lock:
        _conversation_state.clear()
    log("🧹 Cleared conversation state", "WARNING")
    return jsonify({"status": "ok"})


# =========================================================================
#  Startup
# =========================================================================
if __name__ == "__main__":
    s = acm.summary()
    print("\n" + "=" * 60)
    print("🚀 rita2api starting")
    print(f"📍 Port: {PORT}  Upstream: {UPSTREAM_URL}")
    print(f"🔑 Accounts: {s['total']} total, {s['active']} active, {s['disabled']} disabled")
    print(f"🌐 WebUI: http://localhost:{PORT}/")
    print("=" * 60 + "\n")

    # Start background token health checker
    acm.start_health_checker(UPSTREAM_URL, RITA_ORIGIN, log_fn=log)

    # Start auto-replenish (registers new accounts when pool runs low)
    auto_register.start_auto_replenish(acm, UPSTREAM_URL, RITA_ORIGIN, log_fn=log)

    app.run(host=HOST, port=PORT, debug=False, threaded=True)

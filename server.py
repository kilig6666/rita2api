"""
rita2api — OpenAI-compatible reverse proxy for rita.ai

Bridges the OpenAI Chat Completions API to rita.ai's /aichat/completions endpoint.
Supports streaming SSE, multi-account rotation with WebUI management, and tool calling.
"""

import json
import os
import re
import time
import base64
import hashlib
import datetime
import threading
import requests
from pathlib import Path
from dotenv import load_dotenv, dotenv_values
from flask import Flask, request, Response, jsonify, render_template, session
from threading import Lock

# MUST load .env before importing AccountManager (which reads env vars at module level)
load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"
ENV_EXAMPLE_FILE = BASE_DIR / ".env.example"
PRICE_DOC_FILE = BASE_DIR / "docs" / "价格.md"
_price_doc_cache: tuple[dict[str, int], dict[str, int], dict] | None = None
_price_doc_cache_mtime: float | None = None

from accounts import AccountManager
import auto_register
from quota import get_cost, get_all_costs
from database import get_db
from adapters.openai_protocol import (
    build_rita_messages as build_rita_messages_v2,
    inject_tool_prompt as inject_tool_prompt_v2,
    parse_tool_response as parse_tool_response_v2,
    responses_input_to_messages,
    make_responses_base,
    split_embedded_thinking,
    split_text_chunks,
)
from adapters.anthropic_protocol import (
    anthropic_messages_to_openai_chat,
    estimate_anthropic_tokens,
    build_anthropic_message_response,
    build_anthropic_stream_events,
    parse_tool_calls_from_text,
)
from services.rita_gateway import RitaGateway, collect_rita_response, iter_rita_sse
from services.rita_dispatch import (
    NoAvailableAccountError,
    acquire_lease,
    disable_quota_exhausted,
    is_quota_exhausted_message,
    mark_failure,
    mark_success,
    release_lease,
)
from routes.protocol_handlers import (
    handle_chat_completions_api,
    handle_anthropic_count_tokens,
    handle_anthropic_messages_api,
    handle_responses_api,
)

# ===================== Configuration =====================
DEBUG_MODE = os.getenv("DEBUG", "1") == "1"
UPSTREAM_URL = os.getenv("RITA_UPSTREAM", "https://api_v2.rita.ai")
RITA_ORIGIN = os.getenv("RITA_ORIGIN", "https://www.rita.ai")
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "10089"))
# Disable SSL verification for api_v2.rita.ai (hostname mismatch in upstream cert)
DISABLE_SSL_VERIFY = os.getenv("DISABLE_SSL_VERIFY", "0") == "1"
AUTH_TOKEN = os.getenv("AUTH_TOKEN", "981115")

rita_gateway = RitaGateway(UPSTREAM_URL, disable_ssl_verify=DISABLE_SSL_VERIFY)

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", os.urandom(24).hex())
acm = AccountManager()
_conv_lock = Lock()
_responses_lock = Lock()
_responses_state: dict[str, dict] = {}

# ===================== Auth Middleware =====================
def _load_env_file_values(path: Path) -> dict:
    """读取 .env 文件中的键值对。"""
    if not path.exists():
        return {}
    try:
        values = dotenv_values(path)
    except Exception:
        return {}
    result = {}
    for key, value in values.items():
        key_text = str(key or "").strip()
        if not key_text:
            continue
        result[key_text] = "" if value is None else str(value)
    return result


def _format_env_value(value) -> str:
    """将值格式化为可写回 .env 的字符串。"""
    text = "" if value is None else str(value)
    if not text:
        return ""
    if "\n" in text or text != text.strip() or "#" in text:
        return json.dumps(text, ensure_ascii=False)
    return text


def _upsert_env_value(key: str, value, path: Path = ENV_FILE):
    """更新 .env 中的单个键；若不存在则追加。"""
    key_text = str(key or "").strip()
    if not key_text:
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        content = path.read_text(encoding="utf-8")
        lines = content.splitlines()
    else:
        content = ""
        lines = []

    pattern = re.compile(rf"^(\s*(?:export\s+)?{re.escape(key_text)}\s*=\s*).*$")
    replacement = f"{key_text}={_format_env_value(value)}"
    replaced = False
    new_lines = []
    for line in lines:
        if pattern.match(line):
            new_lines.append(replacement)
            replaced = True
        else:
            new_lines.append(line)

    if not replaced:
        if new_lines and new_lines[-1].strip():
            new_lines.append("")
        new_lines.append(replacement)

    new_content = "\n".join(new_lines).rstrip("\n") + "\n"
    if new_content != content:
        path.write_text(new_content, encoding="utf-8")


def _get_merged_config_rows() -> list[dict]:
    """合并 DB、.env、.env.example，供面板统一展示。"""
    db = get_db()
    db_rows = db.get_all_config()
    merged = {}
    runtime_defaults = {
        "HOST": str(HOST or ""),
        "PORT": str(PORT or ""),
        "DEBUG": "1" if DEBUG_MODE else "0",
        "FLASK_SECRET": str(os.getenv("FLASK_SECRET", "") or ""),
    }

    for row in db_rows:
        key = str(row["key"] or "").strip()
        if not key:
            continue
        merged[key] = {
            "key": key,
            "value": "" if row["value"] is None else str(row["value"]),
            "description": str(row.get("description", "") or ""),
        }

    for source in (_load_env_file_values(ENV_EXAMPLE_FILE), _load_env_file_values(ENV_FILE)):
        for key, value in source.items():
            if key not in merged:
                merged[key] = {"key": key, "value": value, "description": ""}
            elif not str(merged[key].get("value", "") or "").strip() and str(value or "").strip():
                merged[key]["value"] = value

    for key, value in runtime_defaults.items():
        if key not in merged:
            merged[key] = {"key": key, "value": value, "description": ""}

    return [merged[key] for key in sorted(merged.keys())]


def _parse_bool_value(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _parse_cf_trace(text: str) -> dict:
    result = {}
    for line in str(text or "").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key_text = str(key or "").strip()
        if not key_text:
            continue
        result[key_text] = str(value or "").strip()
    return result


def _probe_register_proxy(proxy: str, disable_ssl_verify: bool = False) -> dict:
    proxy_url = auto_register._normalize_proxy_value(proxy)
    if not proxy_url:
        return {
            "ok": False,
            "proxy": "",
            "message": "代理地址未设置",
            "error": "proxy is empty",
            "loc": None,
            "ip": None,
            "colo": None,
            "trace": None,
        }

    session = requests.Session()
    session.trust_env = False
    try:
        resp = session.get(
            "https://cloudflare.com/cdn-cgi/trace",
            timeout=8,
            verify=not disable_ssl_verify,
            proxies=auto_register._build_http_proxies(proxy_url),
            headers={"User-Agent": "Mozilla/5.0"},
        )
        resp.raise_for_status()
        trace = _parse_cf_trace(resp.text)
        loc = trace.get("loc")
        ip = trace.get("ip")
        colo = trace.get("colo")
        return {
            "ok": True,
            "proxy": proxy_url,
            "message": f"代理连通正常，地区={loc or '?'}，出口 IP={ip or '?'}",
            "error": "",
            "loc": loc,
            "ip": ip,
            "colo": colo,
            "trace": trace,
        }
    except Exception as exc:
        return {
            "ok": False,
            "proxy": proxy_url,
            "message": f"代理测试失败：{exc}",
            "error": str(exc),
            "loc": None,
            "ip": None,
            "colo": None,
            "trace": None,
        }
    finally:
        session.close()


def _get_auth_token() -> str:
    """实时读取管理面板密码，优先数据库配置，回退环境变量默认值。"""
    try:
        row = get_db().fetchone("SELECT value FROM config WHERE key=?", ("AUTH_TOKEN",))
        if row is not None:
            return str(row["value"] or "").strip()
    except Exception:
        pass
    return str(AUTH_TOKEN or "").strip()


def _get_proxy_api_key() -> str:
    """读取对外模型调用 API Key。"""
    try:
        row = get_db().fetchone("SELECT value FROM config WHERE key=?", ("PROXY_API_KEY",))
        if row is not None and str(row["value"] or "").strip():
            return str(row["value"] or "").strip()
    except Exception:
        pass
    return str(os.getenv("PROXY_API_KEY", "") or "").strip()


def _get_runtime_upstream_url() -> str:
    try:
        row = get_db().fetchone("SELECT value FROM config WHERE key=?", ("RITA_UPSTREAM",))
        if row is not None and str(row["value"] or "").strip():
            return str(row["value"] or "").strip()
    except Exception:
        pass
    return str(UPSTREAM_URL or "").strip()


def _get_runtime_origin() -> str:
    try:
        row = get_db().fetchone("SELECT value FROM config WHERE key=?", ("RITA_ORIGIN",))
        if row is not None and str(row["value"] or "").strip():
            return str(row["value"] or "").strip()
    except Exception:
        pass
    return str(RITA_ORIGIN or "").strip()


def _get_runtime_disable_ssl_verify() -> bool:
    try:
        row = get_db().fetchone("SELECT value FROM config WHERE key=?", ("DISABLE_SSL_VERIFY",))
        if row is not None:
            return _parse_bool_value(row["value"], default=DISABLE_SSL_VERIFY)
    except Exception:
        pass
    return bool(DISABLE_SSL_VERIFY)


def _get_rita_gateway() -> RitaGateway:
    return RitaGateway(
        _get_runtime_upstream_url(),
        disable_ssl_verify=_get_runtime_disable_ssl_verify(),
    )


def _normalize_model_price_key(value: str) -> str:
    """将模型名归一化，便于和 docs/价格.md 做宽松匹配。"""
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _load_price_doc_index() -> tuple[dict[str, int], dict[str, int], dict]:
    """解析 docs/价格.md 中的模型积分表。"""
    global _price_doc_cache, _price_doc_cache_mtime
    exact: dict[str, int] = {}
    normalized: dict[str, int] = {}
    meta = {
        "path": str(PRICE_DOC_FILE.relative_to(BASE_DIR)),
        "source": "",
        "updated_at": "",
        "total": 0,
    }
    try:
        current_mtime = PRICE_DOC_FILE.stat().st_mtime
        if _price_doc_cache is not None and _price_doc_cache_mtime == current_mtime:
            return _price_doc_cache
        lines = PRICE_DOC_FILE.read_text(encoding="utf-8").splitlines()
    except Exception as e:
        meta["error"] = str(e)
        return exact, normalized, meta

    row_pattern = re.compile(r"^\|\s*\d+\s*\|\s*(.*?)\s*\|\s*(\d+)\s*\|$")
    for raw_line in lines:
        line = raw_line.strip()
        if line.startswith("> 数据来源："):
            meta["source"] = line.split("：", 1)[1].strip()
            continue
        if line.startswith("> 更新时间："):
            meta["updated_at"] = line.split("：", 1)[1].strip()
            continue
        match = row_pattern.match(line)
        if not match:
            continue
        model_name = match.group(1).strip()
        points = int(match.group(2))
        exact[model_name.lower()] = points
        normalized[_normalize_model_price_key(model_name)] = points

    meta["total"] = len(exact)
    _price_doc_cache = (exact, normalized, meta)
    _price_doc_cache_mtime = current_mtime
    return _price_doc_cache


def _lookup_model_points(model_name: str, exact: dict[str, int], normalized: dict[str, int]) -> int | None:
    """优先按原名匹配，不命中时走归一化匹配。"""
    exact_key = str(model_name or "").strip().lower()
    if exact_key in exact:
        return exact[exact_key]
    normalized_key = _normalize_model_price_key(model_name)
    if normalized_key and normalized_key in normalized:
        return normalized[normalized_key]
    return None


def _check_auth() -> bool:
    """管理面板鉴权。"""
    auth_token = _get_auth_token()
    if not auth_token:
        return True
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer ") and auth_header[7:] == auth_token:
        return True
    if request.args.get("auth") == auth_token:
        return True
    if session.get("auth_token") == auth_token:
        return True
    return False


def _check_proxy_auth() -> bool:
    """/v1 协议调用鉴权：支持 OpenAI Bearer 与 Anthropic x-api-key。"""
    # 管理面板首页的对话/模型请求走同源 fetch，会自动带上登录 session。
    # 这里补一个 session 兜底，避免面板已登录却仍被 /v1/* 当成外部调用拦下。
    auth_token = _get_auth_token()
    if auth_token and session.get("auth_token") == auth_token:
        return True
    proxy_api_key = _get_proxy_api_key()
    if not proxy_api_key:
        return False
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer ") and auth_header[7:] == proxy_api_key:
        return True
    if request.headers.get("x-api-key", "").strip() == proxy_api_key:
        return True
    if request.args.get("auth") == proxy_api_key:
        return True
    return False

@app.before_request
def require_auth():
    path = request.path
    # Always allow without auth
    if path == "/" or path == "/health":
        return None
    # Allow login and auth-check endpoints
    if path in ("/api/login", "/api/auth/check"):
        return None
    # /v1/* proxy endpoints: check PROXY_API_KEY (OpenAI / Anthropic style)
    if path.startswith("/v1/"):
        proxy_api_key = _get_proxy_api_key()
        if not _check_proxy_auth():
            if not proxy_api_key:
                return jsonify({
                    "error": {
                        "message": "PROXY_API_KEY not configured",
                        "type": "config_error",
                        "code": "proxy_api_key_missing",
                    }
                }), 503
            return jsonify({"error": {"message": "Incorrect API key provided", "type": "invalid_request_error", "code": "invalid_api_key"}}), 401
        return None
    # /api/* and /debug/*: require auth
    if path.startswith("/api/") or path.startswith("/debug/"):
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
_TEXT_PROXY_UNSUPPORTED_ABILITIES = {"image"}
_TEXT_PROXY_KNOWN_UNSUPPORTED_MODEL_IDS = {
    "model_1080",
    "model_1114",
    "model_1118",
    "model_1121",
    "model_1123",
}
_IMAGE_MODEL_CACHE_TTL = 3600
_image_model_types_cache: list[dict] = []
_image_model_types_cache_ts: float = 0
_image_model_details_cache: dict[int, list[dict]] = {}
_image_model_details_cache_ts: dict[int, float] = {}
_IMAGE_ACCOUNT_COOLDOWN_SECONDS = 600
_image_account_cooldowns: dict[str, float] = {}

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


_manual_register_lock = Lock()
_manual_register_task: dict | None = None
_MANUAL_REGISTER_MAX_COUNT = 50
_MANUAL_REGISTER_MAX_THREADS = 10


def _normalize_manual_register_count(value, default: int = 1) -> int:
    try:
        count = int(value)
    except Exception:
        count = default
    return min(max(count, 1), _MANUAL_REGISTER_MAX_COUNT)


def _normalize_manual_register_threads(value, count: int, default: int = 1) -> int:
    try:
        threads = int(value)
    except Exception:
        threads = default
    threads = min(max(threads, 1), _MANUAL_REGISTER_MAX_THREADS)
    return min(threads, max(count, 1))


def _manual_register_public(task: dict | None, include_logs: bool = False) -> dict | None:
    if not task:
        return None
    success_count = int(task.get("success_count", len(task.get("accounts") or [])) or 0)
    failed_count = int(task.get("failed_count", 0) or 0)
    requested = int(task.get("requested", 0) or 0)
    active_workers = max(0, int(task.get("active_workers", 0) or 0))
    remaining = max(0, requested - success_count - failed_count)
    result = {
        "id": task["id"],
        "status": task["status"],
        "requested": requested,
        "registered": success_count,
        "success_count": success_count,
        "failed_count": failed_count,
        "remaining_count": remaining,
        "active_workers": active_workers,
        "threads": task.get("threads") or 1,
        "stop_requested": bool(task.get("stop_requested")),
        "captcha_provider": task.get("captcha_provider") or "yescaptcha",
        "created_at": task["created_at"],
        "updated_at": task["updated_at"],
        "error": task.get("error") or "",
        "accounts": list(task.get("accounts") or []),
    }
    if include_logs:
        result["logs"] = [dict(item) for item in task.get("logs") or []]
    return result


def _get_manual_register_task(task_id: str | None = None, include_logs: bool = False) -> dict | None:
    with _manual_register_lock:
        if not _manual_register_task:
            return None
        if task_id and _manual_register_task["id"] != task_id:
            return None
        return _manual_register_public(_manual_register_task, include_logs=include_logs)


def _append_manual_register_log(task_id: str, msg: str, level: str = "INFO"):
    global _manual_register_task
    text = str(msg or "")
    level_name = str(level or "INFO").upper()
    with _manual_register_lock:
        task = _manual_register_task
        if not task or task["id"] != task_id:
            return
        task["seq"] += 1
        task["updated_at"] = time.time()
        task.setdefault("logs", []).append({
            "seq": task["seq"],
            "ts": task["updated_at"],
            "level": level_name,
            "message": text,
        })
        if len(task["logs"]) > 1000:
            task["logs"] = task["logs"][-1000:]
    log(f"[manual-register:{task_id[:8]}] {text}", level_name)


def _append_manual_register_result(task_id: str, result: dict):
    global _manual_register_task
    if not result:
        return
    with _manual_register_lock:
        task = _manual_register_task
        if not task or task["id"] != task_id:
            return
        task["updated_at"] = time.time()
        task["success_count"] = int(task.get("success_count", 0) or 0) + 1
        task.setdefault("accounts", []).append(dict(result))


def _mark_manual_register_failure(task_id: str, count: int = 1):
    global _manual_register_task
    if count <= 0:
        return
    with _manual_register_lock:
        task = _manual_register_task
        if not task or task["id"] != task_id:
            return
        task["updated_at"] = time.time()
        task["failed_count"] = int(task.get("failed_count", 0) or 0) + int(count)


def _set_manual_register_active_workers(task_id: str, active_workers: int):
    global _manual_register_task
    with _manual_register_lock:
        task = _manual_register_task
        if not task or task["id"] != task_id:
            return
        task["updated_at"] = time.time()
        task["active_workers"] = max(0, int(active_workers or 0))


def _set_manual_register_status(task_id: str, status: str, **updates):
    global _manual_register_task
    with _manual_register_lock:
        task = _manual_register_task
        if not task or task["id"] != task_id:
            return
        task["status"] = status
        task["updated_at"] = time.time()
        for key, value in updates.items():
            task[key] = value


def _manual_register_should_stop(task_id: str) -> bool:
    with _manual_register_lock:
        task = _manual_register_task
        return bool(task and task["id"] == task_id and task.get("stop_requested"))


def _run_manual_register_task(task_id: str, count: int, threads: int, captcha_provider: str):
    def task_log(message: str, level: str = "INFO"):
        _append_manual_register_log(task_id, message, level)

    try:
        task_log(
            f"🚀 手动注册任务已启动，请求数量={count}，线程={threads}，打码={captcha_provider or 'yescaptcha'}",
            "INFO",
        )
        auto_register.set_thread_log_fn(task_log)
        results = auto_register.auto_register_batch(
            count=count,
            threads=threads,
            account_manager=acm,
            upstream_url=_get_runtime_upstream_url(),
            origin=_get_runtime_origin(),
            captcha_provider_override=captcha_provider,
            should_stop=lambda: _manual_register_should_stop(task_id),
            on_result=lambda item: _append_manual_register_result(task_id, item),
            on_failure=lambda *_args, **_kwargs: _mark_manual_register_failure(task_id),
            on_active_workers_change=lambda current: _set_manual_register_active_workers(task_id, current),
        )
        _set_manual_register_status(
            task_id,
            "completed",
            accounts=list(results or []),
            success_count=len(results or []),
            active_workers=0,
            error="",
        )
        task_log(f"✅ 手动注册完成：成功 {len(results)}/{count}", "SUCCESS")
    except auto_register.RegistrationStopped as e:
        partial = list(getattr(e, "results", []) or [])
        _set_manual_register_status(
            task_id,
            "stopped",
            accounts=partial,
            success_count=len(partial),
            active_workers=0,
            error="",
        )
        task_log(f"⛔ 已按请求停止手动注册，已成功 {len(partial)}/{count}", "WARNING")
    except Exception as e:
        _set_manual_register_status(task_id, "failed", active_workers=0, error=str(e))
        task_log(f"❌ 手动注册异常: {e}", "ERROR")
    finally:
        auto_register.set_thread_log_fn(None)


def _start_manual_register_task(count: int, threads: int, captcha_provider: str) -> tuple[dict | None, str | None]:
    global _manual_register_task
    with _manual_register_lock:
        if _manual_register_task and _manual_register_task["status"] in {"running", "stopping"}:
            return _manual_register_public(_manual_register_task), "busy"
        task_id = hashlib.md5(f"{time.time()}-{count}-{threads}-{captcha_provider}".encode()).hexdigest()[:16]
        _manual_register_task = {
            "id": task_id,
            "status": "running",
            "requested": count,
            "threads": threads,
            "captcha_provider": captcha_provider or "yescaptcha",
            "stop_requested": False,
            "created_at": time.time(),
            "updated_at": time.time(),
            "seq": 0,
            "logs": [],
            "accounts": [],
            "success_count": 0,
            "failed_count": 0,
            "active_workers": 0,
            "error": "",
        }
    threading.Thread(
        target=_run_manual_register_task,
        args=(task_id, count, threads, captcha_provider),
        daemon=True,
        name=f"manual-register-{task_id[:8]}",
    ).start()
    return _get_manual_register_task(task_id), None


def _request_stop_manual_register(task_id: str | None = None) -> dict | None:
    global _manual_register_task
    with _manual_register_lock:
        task = _manual_register_task
        if not task:
            return None
        if task_id and task["id"] != task_id:
            return None
        if task["status"] not in {"running", "stopping"}:
            return _manual_register_public(task)
        task["stop_requested"] = True
        task["status"] = "stopping"
        task["updated_at"] = time.time()
    _append_manual_register_log(task["id"], "⛔ 收到强行终止请求，正在安全停止当前流程...", "WARNING")
    return _get_manual_register_task(task["id"])


def _approx_tokens(messages: list, response_text: str = "") -> int:
    msg_chars = sum(len(str(m.get("content", ""))) for m in messages)
    return (msg_chars + len(response_text or "")) // 4


def _ensure_conversation(headers: dict, rita_model: str, existing_chat_id: int = 0) -> int:
    if existing_chat_id:
        return existing_chat_id
    try:
        chat_id = _get_rita_gateway().create_conversation(headers, rita_model)
        log(f"📝 Auto-created conversation: chat_id={chat_id}", "DEBUG")
        return chat_id
    except Exception as e:
        log(f"⚠️ Failed to create conversation: {e}", "WARNING")
        return 0


def _should_reset_conversation(error_message: str) -> bool:
    message = str(error_message or "").lower()
    return "conversation does not exist" in message or "param invalid" in message


def _make_anthropic_error(message: str, error_type: str = "invalid_request_error", status: int = 400):
    return jsonify({"type": "error", "error": {"type": error_type, "message": message}}), status


def _build_protocol_deps() -> dict:
    return {
        "acm": acm,
        "RITA_ORIGIN": _get_runtime_origin(),
        "rita_gateway": _get_rita_gateway(),
        "resolve_model": _resolve_rita_model_for_request,
        "validate_text_model": _validate_text_proxy_model,
        "generate_image": _generate_openai_image_result,
        "get_or_create_conversation": get_or_create_conversation,
        "update_conversation_state": update_conversation_state,
        "get_response_state": get_response_state,
        "update_response_state": update_response_state,
        "build_rita_messages": build_rita_messages_v2,
        "inject_tool_prompt": inject_tool_prompt_v2,
        "tool_prompt_cache": _tool_prompt_cache,
        "parse_tool_response": parse_tool_response_v2,
        "split_embedded_thinking": split_embedded_thinking,
        "log": log,
        "get_cost": get_cost,
        "acquire_lease": acquire_lease,
        "disable_quota_exhausted": disable_quota_exhausted,
        "is_quota_exhausted_message": is_quota_exhausted_message,
        "mark_success": mark_success,
        "mark_failure": mark_failure,
        "NoAvailableAccountError": NoAvailableAccountError,
        "release_lease": release_lease,
        "estimate_anthropic_tokens": estimate_anthropic_tokens,
        "anthropic_messages_to_openai_chat": anthropic_messages_to_openai_chat,
        "build_anthropic_message_response": build_anthropic_message_response,
        "build_anthropic_stream_events": build_anthropic_stream_events,
        "parse_tool_calls_from_text": parse_tool_calls_from_text,
        "responses_input_to_messages": responses_input_to_messages,
        "make_responses_base": make_responses_base,
        "split_text_chunks": split_text_chunks,
        "should_reset_conversation": _should_reset_conversation,
        "ensure_conversation": _ensure_conversation,
        "increment_stats": _increment_stats,
        "collect_rita_response": collect_rita_response,
        "iter_rita_sse": iter_rita_sse,
        "make_anthropic_error": _make_anthropic_error,
    }

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
def _model_alias_signature(value: str) -> tuple[str, ...]:
    return tuple(sorted(re.findall(r"[a-z]+|\d+", str(value or "").lower())))


def _find_cached_model_item(model: str) -> dict | None:
    model_text = str(model or "").strip()
    if not model_text or not _models_cache:
        return None

    model_lower = model_text.lower()
    signature = _model_alias_signature(model_text)
    for item in _models_cache.get("data", []):
        item_id = str(item.get("id") or "").strip()
        item_name = str(item.get("name") or item_id).strip()
        item_id_lower = item_id.lower()
        item_name_lower = item_name.lower()
        if model_lower in {item_id_lower, item_name_lower}:
            return item
        if signature and signature == _model_alias_signature(item_name):
            return item
    return None


def _is_text_proxy_supported_model(item: dict) -> bool:
    ability = str(item.get("ability") or "").strip().lower()
    model_id = str(item.get("id") or "").strip().lower()
    if ability in _TEXT_PROXY_UNSUPPORTED_ABILITIES:
        return False
    if model_id in _TEXT_PROXY_KNOWN_UNSUPPORTED_MODEL_IDS:
        return False
    return True


def _text_proxy_model_error_message(model_name: str, model_id: str) -> str:
    display_name = str(model_name or model_id or "该模型").strip()
    return (
        f"{display_name} 属于 Rita 图像模型，当前代理只桥接文本对话接口。"
        "请不要在 /v1/chat/completions 或 /v1/messages 中使用它；"
        "请改用 /v1/images/generations，或在 /v1/responses 中按图像生成协议调用。"
    )


def _validate_text_proxy_model(requested_model: str, resolved_model: str, headers: dict | None = None) -> str | None:
    item = _find_cached_model_item(resolved_model) or _find_cached_model_item(requested_model)
    if not item and headers:
        try:
            _refresh_model_cache(headers)
        except Exception as e:
            log(f"⚠️ Failed to refresh model cache for support check: {e}", "WARNING")
        item = _find_cached_model_item(resolved_model) or _find_cached_model_item(requested_model)

    if item and not _is_text_proxy_supported_model(item):
        return _text_proxy_model_error_message(item.get("name", requested_model), item.get("id", resolved_model))

    if str(resolved_model or "").strip().lower() in _TEXT_PROXY_KNOWN_UNSUPPORTED_MODEL_IDS:
        return _text_proxy_model_error_message(requested_model, resolved_model)
    return None


def _filter_text_proxy_models(catalog: dict) -> dict:
    data = [item for item in catalog.get("data", []) if _is_text_proxy_supported_model(item)]
    return {**catalog, "data": data}


def _refresh_model_cache(headers: dict) -> None:
    global _models_cache, _models_cache_ts
    now = time.time()
    upstream = _get_rita_gateway().fetch_models(headers)
    data = []
    for cat in upstream.get("data", {}).get("category_models", []):
        cat_name = cat.get("name", "")
        for model in cat.get("models", []):
            model_id = model.get("key", "")
            data.append({
                "id": model_id,
                "object": "model",
                "created": 0,
                "owned_by": "rita",
                "name": model.get("name", model_id),
                "description": model.get("desc", ""),
                "quota": model.get("quota", 0),
                "tool": model.get("tool", ""),
                "ability": model.get("ability", ""),
                "model_type_id": model.get("model_type_id"),
                "category": cat_name,
            })
    _models_cache = {"object": "list", "data": data}
    _models_cache_ts = now


def _get_model_catalog(force_refresh: bool = False) -> dict:
    """统一获取 Rita 模型目录，供 /v1/models 和管理面板复用。"""
    global _models_cache, _models_cache_ts
    now = time.time()
    if not force_refresh and _models_cache and now - _models_cache_ts < MODELS_CACHE_TTL:
        return _models_cache

    acc, _ = acm.next()
    if not acc:
        raise RuntimeError("no accounts configured")

    try:
        _refresh_model_cache(acm.upstream_headers(acc, _get_runtime_origin()))
        acm.mark_ok(acc)
        return _models_cache
    except Exception as e:
        acm.mark_fail(acc, str(e))
        raise


def _get_text_proxy_model_catalog(force_refresh: bool = False) -> dict:
    """仅返回当前文本代理真正可用的模型，避免把图像模型暴露给聊天接口。"""
    return _filter_text_proxy_models(_get_model_catalog(force_refresh=force_refresh))


def _cleanup_image_account_cooldowns() -> None:
    now = time.time()
    expired_ids = [account_id for account_id, expires_at in _image_account_cooldowns.items() if expires_at <= now]
    for account_id in expired_ids:
        _image_account_cooldowns.pop(account_id, None)


def _mark_image_account_cooldown(account_id: str, seconds: int = _IMAGE_ACCOUNT_COOLDOWN_SECONDS) -> None:
    account_text = str(account_id or "").strip()
    if not account_text:
        return
    _image_account_cooldowns[account_text] = time.time() + max(60, int(seconds or _IMAGE_ACCOUNT_COOLDOWN_SECONDS))


def _get_cached_image_cooldown_ids() -> set[str]:
    _cleanup_image_account_cooldowns()
    return set(_image_account_cooldowns.keys())


def _get_image_model_types(headers: dict, *, force_refresh: bool = False) -> list[dict]:
    global _image_model_types_cache, _image_model_types_cache_ts
    now = time.time()
    if not force_refresh and _image_model_types_cache and now - _image_model_types_cache_ts < _IMAGE_MODEL_CACHE_TTL:
        return list(_image_model_types_cache)

    upstream = _get_rita_gateway().fetch_image_model_types(headers)
    _image_model_types_cache = list(upstream.get("data", []) or [])
    _image_model_types_cache_ts = now
    return list(_image_model_types_cache)


def _get_image_model_details(headers: dict, model_type_id: int, *, force_refresh: bool = False) -> list[dict]:
    type_id = int(model_type_id or 0)
    if type_id <= 0:
        return []

    now = time.time()
    last_refresh = _image_model_details_cache_ts.get(type_id, 0)
    cached = _image_model_details_cache.get(type_id, [])
    if not force_refresh and cached and now - last_refresh < _IMAGE_MODEL_CACHE_TTL:
        return list(cached)

    upstream = _get_rita_gateway().fetch_image_model_details(headers, type_id)
    details = list(upstream.get("data", []) or [])
    _image_model_details_cache[type_id] = details
    _image_model_details_cache_ts[type_id] = now
    return list(details)


def _resolve_image_model_metadata(model: str, headers: dict) -> tuple[str, dict]:
    resolved_model = _resolve_rita_model_for_request(model, headers)
    item = _find_cached_model_item(resolved_model) or _find_cached_model_item(model)
    if not item:
        _refresh_model_cache(headers)
        item = _find_cached_model_item(resolved_model) or _find_cached_model_item(model)
    if not item:
        raise ValueError(f"model not found: {model}")

    ability = str(item.get("ability") or "").strip().lower()
    if ability != "image":
        raise ValueError(f"{item.get('name', model)} 不是图像模型，请改用文本对话接口")

    model_type_id = int(item.get("model_type_id") or 0)
    if model_type_id <= 0:
        image_types = _get_image_model_types(headers, force_refresh=True)
        matched_type = next((row for row in image_types if str(row.get("name") or "").strip() == str(item.get("category") or "").strip()), None)
        model_type_id = int((matched_type or {}).get("id") or 0)
    if model_type_id <= 0:
        raise ValueError(f"无法解析图像模型类型: {item.get('name', model)}")

    details = _get_image_model_details(headers, model_type_id)
    item_numeric_id = re.sub(r"^model_", "", str(item.get("id") or "").strip())
    detail = next((row for row in details if str(row.get("id")) == item_numeric_id), None)
    if not detail:
        details = _get_image_model_details(headers, model_type_id, force_refresh=True)
        detail = next((row for row in details if str(row.get("id")) == item_numeric_id), None)
    if not detail:
        raise ValueError(f"无法加载图像模型详情: {item.get('name', model)}")

    merged = {
        **detail,
        "key": item.get("id"),
        "ability": item.get("ability"),
        "model_type_id": model_type_id,
        "model_type_name": detail.get("model_type_name") or next(
            (row.get("name") for row in _image_model_types_cache if int(row.get("id") or 0) == model_type_id),
            item.get("category") or "",
        ),
    }
    return resolved_model, merged


def _normalize_image_count(value, default: int = 1) -> int:
    try:
        count = int(value)
    except Exception:
        count = default
    return min(max(count, 1), 4)


def _normalize_image_response_format(value: str | None, default: str = "b64_json") -> str:
    text = str(value or "").strip().lower()
    if not text:
        return default
    if text in {"b64_json", "url"}:
        return text
    raise ValueError("response_format 仅支持 b64_json 或 url")


def _select_image_size_options(model_detail: dict, size: str | None = None, quality: str | None = None) -> tuple[str | None, str | None]:
    requested_size = str(size or "").strip().lower()
    requested_quality = str(quality or "").strip().lower()
    ratios = [str(item).strip() for item in (model_detail.get("ratio") or []) if str(item).strip()]
    resolutions = [
        str(item.get("resolution") or "").strip()
        for item in (model_detail.get("resolution") or [])
        if isinstance(item, dict) and str(item.get("resolution") or "").strip()
    ]

    ratio = ratios[0] if ratios else None
    resolution = resolutions[0] if resolutions else None

    size_ratio_map = {
        "1024x1024": "1:1",
        "1024x1536": "2:3",
        "1536x1024": "3:2",
        "1024x1792": "9:16",
        "1792x1024": "16:9",
    }
    if requested_size and requested_size not in {"auto", ""}:
        mapped_ratio = size_ratio_map.get(requested_size)
        if not mapped_ratio:
            raise ValueError("size 暂仅支持 1024x1024、1024x1536、1536x1024、1024x1792、1792x1024 或 auto")
        if ratios and mapped_ratio not in ratios:
            raise ValueError(f"模型 {model_detail.get('name', '')} 不支持尺寸 {requested_size}")
        ratio = mapped_ratio
        try:
            width_text, height_text = requested_size.split("x", 1)
            max_edge = max(int(width_text), int(height_text))
        except Exception:
            max_edge = 1024
        preferred_resolution = "4K" if max_edge >= 3072 else "2K" if max_edge > 1024 else "1K"
        if resolutions:
            resolution = preferred_resolution if preferred_resolution in resolutions else resolutions[0]

    if requested_quality == "high" and resolutions:
        resolution = "4K" if "4K" in resolutions else resolutions[-1]

    return ratio, resolution


def _extract_rita_image_urls(response: requests.Response) -> list[str]:
    urls: list[str] = []
    with response:
        for event in iter_rita_sse(response):
            choices = event.get("choices", []) or []
            if not choices:
                continue
            choice = choices[0] or {}
            delta = choice.get("delta", {}) or {}
            content = str(delta.get("content") or "").strip()
            if content.startswith("http"):
                urls.append(content)
            if delta.get("result") == "error":
                raise RuntimeError("图像生成被上游拒绝")
            if choice.get("finish_reason") == "stop" and urls:
                break
    if not urls:
        raise RuntimeError("图像生成完成，但未拿到图片结果")
    return urls


def _download_image_payload(image_url: str, response_format: str) -> dict:
    image_text = str(image_url or "").strip()
    if not image_text:
        raise ValueError("empty image url")
    if response_format == "url":
        return {"url": image_text}

    response = requests.get(image_text, timeout=120)
    response.raise_for_status()
    return {
        "b64_json": base64.b64encode(response.content).decode("ascii"),
    }


def _generate_openai_image_result(
    requested_model: str,
    prompt: str,
    *,
    size: str | None = None,
    n: int = 1,
    response_format: str = "b64_json",
    quality: str | None = None,
    client_token: str = "",
    client_visitorid: str = "",
) -> dict:
    prompt_text = str(prompt or "").strip()
    if not prompt_text:
        raise ValueError("prompt is required")

    normalized_count = _normalize_image_count(n)
    normalized_format = _normalize_image_response_format(response_format)
    excluded_ids = _get_cached_image_cooldown_ids()
    last_error: Exception | None = None

    while True:
        lease = None
        try:
            lease = acquire_lease(
                acm,
                _get_runtime_origin(),
                client_token=client_token,
                client_visitorid=client_visitorid,
                exclude_account_ids=excluded_ids,
            )
        except NoAvailableAccountError as e:
            if last_error:
                raise last_error
            raise e

        account_id = lease.account.id if lease.account else ""
        if account_id and account_id in excluded_ids:
            release_lease(acm, lease)
            continue

        try:
            resolved_model, model_detail = _resolve_image_model_metadata(requested_model, lease.headers)
            ratio, resolution = _select_image_size_options(model_detail, size=size, quality=quality)
            payload = {
                "model_type_id": int(model_detail.get("model_type_id") or 0),
                "model_type_name": str(model_detail.get("model_type_name") or "").strip(),
                "model_name": str(model_detail.get("name") or requested_model).strip(),
                "type": "generate",
                "generate": {
                    "model_id": int(model_detail.get("id") or 0),
                    "prompt": prompt_text,
                    "image_num": normalized_count,
                },
            }
            if ratio:
                payload["generate"]["ratio"] = ratio
            if resolution:
                payload["generate"]["resolution"] = resolution

            submit_result = _get_rita_gateway().submit_image_generation(lease.headers, payload)
            submit_code = int(submit_result.get("code", 0) or 0)
            if submit_code != 0:
                message = str(submit_result.get("message") or submit_result.get("error") or "image generation failed")
                quota_insufficient = submit_code == 2018 or "配额不足" in message or is_quota_exhausted_message(message)
                if quota_insufficient and lease.account and not lease.used_client_token:
                    _mark_image_account_cooldown(lease.account.id)
                    excluded_ids.add(lease.account.id)
                    release_lease(acm, lease)
                    lease = None
                    last_error = RuntimeError(message)
                    continue
                raise RuntimeError(message)

            submit_data = submit_result.get("data", {}) or {}
            parent_message_id = str(submit_data.get("parent_message_id") or "").strip()
            if not parent_message_id:
                raise RuntimeError("image generation accepted but parent_message_id is missing")

            stream_response = _get_rita_gateway().stream_image_records(lease.headers, parent_message_id, timeout=240)
            stream_response.raise_for_status()
            image_urls = _extract_rita_image_urls(stream_response)
            images = [_download_image_payload(url, normalized_format) for url in image_urls]

            mark_success(
                acm,
                lease,
                model=requested_model,
                request_type="images_generate",
                tokens_approx=max(1, len(prompt_text) // 4),
                cost=get_cost(resolved_model),
            )
            if account_id:
                _image_account_cooldowns.pop(account_id, None)
            return {
                "created": int(time.time()),
                "data": images,
                "urls": image_urls,
                "resolved_model": resolved_model,
                "prompt": prompt_text,
            }
        except requests.RequestException as e:
            last_error = e
            if lease:
                mark_failure(acm, lease, str(e), model=requested_model, request_type="images_generate")
                lease = None
            raise
        except ValueError as e:
            last_error = e
            if lease:
                release_lease(acm, lease)
                lease = None
            raise
        except Exception as e:
            last_error = e if isinstance(e, Exception) else RuntimeError(str(e))
            if lease:
                if lease.account and not lease.used_client_token and ("配额不足" in str(e) or is_quota_exhausted_message(str(e))):
                    _mark_image_account_cooldown(lease.account.id)
                    excluded_ids.add(lease.account.id)
                    release_lease(acm, lease)
                    lease = None
                    continue
                mark_failure(acm, lease, str(e), model=requested_model, request_type="images_generate")
                lease = None
            raise


def _resolve_rita_model_for_request(model: str, headers: dict | None = None) -> str:
    resolved = resolve_rita_model(model)
    if resolved.startswith("model_"):
        return resolved
    if headers:
        try:
            _refresh_model_cache(headers)
            resolved = resolve_rita_model(model)
            if resolved.startswith("model_"):
                return resolved
        except Exception as e:
            log(f"⚠️ Failed to refresh model cache for alias resolution: {e}", "WARNING")
    return resolved


def resolve_rita_model(model: str) -> str:
    """Resolve client model name to Rita's model_xxx format."""
    if model.startswith("model_"):
        return model

    model_lower = model.lower()
    mappings = {
        "rita": "model_25",
        "rita-pro": "model_37",
    }
    if model_lower in mappings:
        return mappings[model_lower]
    for key, value in mappings.items():
        if model_lower.startswith(key):
            return value

    requested_signature = _model_alias_signature(model)
    if _models_cache:
        for item in _models_cache.get("data", []):
            model_id = item.get("id", "")
            model_name = item.get("name", "")
            model_name_lower = str(model_name).lower()
            if model_lower == model_name_lower or model_lower in model_name_lower or model_name_lower in model_lower:
                return model_id
            if requested_signature and requested_signature == _model_alias_signature(model_name):
                return model_id

    return model


# ===================== Tool Calling =====================
_tool_prompt_cache: dict[str, str] = {}

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


def get_response_state(response_id: str) -> dict | None:
    response_key = str(response_id or "").strip()
    if not response_key:
        return None
    with _responses_lock:
        state = _responses_state.get(response_key)
        return dict(state) if state else None


def update_response_state(response_id: str, chat_id: int, parent: str | None, model: str, created_at: float):
    response_key = str(response_id or "").strip()
    if not response_key:
        return
    with _responses_lock:
        _responses_state[response_key] = {
            "chat_id": int(chat_id or 0),
            "parent": str(parent or "0"),
            "model": str(model or ""),
            "created_at": float(created_at or time.time()),
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
    current_auth_token = _get_auth_token()
    if not current_auth_token:
        # No auth configured — always succeed
        return jsonify({"ok": True, "auth_required": False})
    if token == current_auth_token:
        session["auth_token"] = current_auth_token
        return jsonify({"ok": True, "auth_required": True})
    return jsonify({"ok": False, "error": "Invalid token"}), 401

@app.route("/api/auth/check", methods=["GET"])
def api_auth_check():
    current_auth_token = _get_auth_token()
    return jsonify({
        "auth_required": bool(current_auth_token),
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


@app.route("/api/request-logs", methods=["GET"])
def api_request_logs():
    db = get_db()
    page_raw = request.args.get("page", "1")
    page_size_raw = request.args.get("page_size", request.args.get("pageSize", "20"))
    try:
        page = int(str(page_raw or "1"))
    except Exception:
        page = 1
    try:
        page_size = int(str(page_size_raw or "20"))
    except Exception:
        page_size = 20
    request_type = str(request.args.get("request_type", "") or "").strip()
    model = str(request.args.get("model", "") or "").strip()
    date_range = str(request.args.get("date_range", request.args.get("dateRange", "all")) or "").strip()
    return jsonify(
        db.get_request_logs(
            page=page,
            page_size=page_size,
            request_type=request_type,
            model=model,
            date_range=date_range,
        )
    )


@app.route("/api/model-plaza", methods=["GET"])
def api_model_plaza():
    """管理面板模型广场：聚合 Rita 模型目录 + docs/价格.md 积分价格。"""
    force_refresh = str(request.args.get("refresh", "")).lower() in {"1", "true", "yes", "force"}
    try:
        catalog = _get_model_catalog(force_refresh=force_refresh)
    except Exception as e:
        log(f"⚠️ Failed to load model plaza catalog: {e}", "WARNING")
        status = 500 if "no accounts configured" in str(e) else 502
        return jsonify({"error": str(e)}), status

    exact_prices, normalized_prices, price_meta = _load_price_doc_index()
    categories: dict[str, int] = {}
    models: list[dict] = []
    priced_total = 0

    for item in catalog.get("data", []):
        category = str(item.get("category") or "未分类").strip() or "未分类"
        categories[category] = categories.get(category, 0) + 1
        name = str(item.get("name") or item.get("id") or "").strip()
        points = _lookup_model_points(name, exact_prices, normalized_prices)
        if points is not None:
            priced_total += 1
        models.append({
            "id": item.get("id", ""),
            "name": name,
            "description": item.get("description", ""),
            "category": category,
            "tool": item.get("tool", ""),
            "ability": item.get("ability", ""),
            "upstream_quota": item.get("quota", 0),
            "points": points,
            "price_label": f"{points} 积分" if points is not None else "待补充",
            "price_source": "docs/价格.md" if points is not None else "",
        })

    return jsonify({
        "total": len(models),
        "priced_total": priced_total,
        "unpriced_total": max(len(models) - priced_total, 0),
        "categories": [{"name": name, "count": count} for name, count in categories.items()],
        "models": models,
        "price_doc": price_meta,
        "synced_at": _models_cache_ts,
    })


def _normalize_email_key(email: str) -> str:
    return str(email or "").strip().lower()


def _normalize_remote_base_url(remote_base_url: str) -> str:
    """归一化远程服务地址，兼容误填 /api/accounts。"""
    text = str(remote_base_url or "").strip().rstrip("/")
    if text.endswith("/api/accounts"):
        text = text[:-len("/api/accounts")]
    if not text.startswith(("http://", "https://")):
        raise ValueError("远程服务地址必须以 http:// 或 https:// 开头")
    return text


def _extract_api_error_message(response: requests.Response) -> str:
    try:
        payload = response.json()
    except Exception:
        text = response.text.strip()
        return text or f"HTTP {response.status_code}"
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error.get("code") or f"HTTP {response.status_code}")
        if error:
            return str(error)
        if payload.get("message"):
            return str(payload.get("message"))
    return f"HTTP {response.status_code}"


def _load_accounts_for_remote_sync(scope: str, selected_ids: list[str]) -> tuple[list, int]:
    """按 scope 读取本地真实账号对象，并统计找不到的 id。"""
    if scope == "all":
        source_ids = acm.list_all_ids()
    else:
        seen = set()
        source_ids = []
        for raw_id in selected_ids:
            aid = str(raw_id or "").strip()
            if not aid or aid in seen:
                continue
            seen.add(aid)
            source_ids.append(aid)

    accounts = []
    missing_count = 0
    for account_id in source_ids:
        acc = acm.get(account_id)
        if not acc:
            missing_count += 1
            continue
        accounts.append(acc)
    return accounts, missing_count


def _build_remote_sync_payload(acc) -> dict:
    """构造发往远端导入接口的账号载荷。"""
    return {
        "name": acc.name,
        "token": acc.token,
        "visitorid": acc.visitorid,
        "email": acc.email,
        "password": acc.password,
        "mail_provider": acc.mail_provider,
        "mail_api_key": acc.mail_api_key,
        "quota_remain": acc.quota_remain,
        "enabled": acc.enabled,
    }

# =========================================================================
#  Account Management API  (/api/accounts/*)
# =========================================================================
@app.route("/api/accounts", methods=["GET"])
def api_list_accounts():
    ids_only = str(request.args.get("ids_only", "")).strip().lower() in {"1", "true", "yes"}
    if ids_only:
        ids = acm.list_all_ids()
        return jsonify({"ids": ids, "total": len(ids)})

    paginate_requested = any(k in request.args for k in ("page", "page_size", "pageSize"))
    if not paginate_requested:
        return jsonify({"accounts": acm.list_all()})

    page = request.args.get("page", "1")
    page_size = request.args.get("page_size", request.args.get("pageSize", "20"))
    return jsonify(acm.list_page(page=page, page_size=page_size))

@app.route("/api/accounts/summary", methods=["GET"])
def api_account_summary():
    return jsonify(acm.summary())

@app.route("/api/accounts", methods=["POST"])
def api_add_account():
    """Add a single account: {token, visitorid?, name?, email?, password?, mail_provider?, mail_api_key?, quota_remain?, enabled?}"""
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
        quota_remain=data.get("quota_remain"),
        enabled=data.get("enabled"),
    )
    log(f"➕ Account added: {acc.name} ({acc.id})", "SUCCESS")
    return jsonify({"ok": True, "account": acc.to_status()}), 201

@app.route("/api/accounts/batch", methods=["POST"])
def api_batch_add():
    """
    Batch add accounts.
    Body: { "accounts": [ {token, visitorid?, name?, quota_remain?, enabled?}, ... ] }
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


@app.route("/api/accounts/sync-remote", methods=["POST"])
def api_sync_accounts_to_remote():
    """将本地账号按邮箱去重后同步到远端管理服务。"""
    data = request.json or {}
    remote_base_url_raw = data.get("remote_base_url", "")
    remote_auth_token = str(data.get("remote_auth_token", "") or "").strip()
    scope = str(data.get("scope", "selected") or "selected").strip().lower()
    selected_ids = data.get("ids", [])

    if scope not in {"selected", "all"}:
        return jsonify({"error": "scope must be selected or all"}), 400
    if scope == "selected" and not selected_ids:
        return jsonify({"error": "请选择至少一个账号后再同步"}), 400
    if not remote_auth_token:
        return jsonify({"error": "remote_auth_token is required"}), 400

    try:
        remote_base_url = _normalize_remote_base_url(remote_base_url_raw)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    local_accounts, missing_count = _load_accounts_for_remote_sync(
        scope,
        selected_ids if isinstance(selected_ids, list) else [],
    )
    result = {
        "ok": True,
        "scope": scope,
        "remote_base_url": remote_base_url,
        "scanned_total": len(local_accounts) + missing_count,
        "eligible_total": 0,
        "synced": 0,
        "failed": 0,
        "skipped_not_found": missing_count,
        "skipped_no_email": 0,
        "skipped_low_quota": 0,
        "skipped_local_duplicate": 0,
        "skipped_remote_exists": 0,
        "errors": [],
    }

    deduped_payloads = []
    local_email_seen = set()
    for acc in local_accounts:
        email_key = _normalize_email_key(acc.email)
        if not email_key:
            result["skipped_no_email"] += 1
            continue
        if (acc.quota_remain or 0) < 10:
            result["skipped_low_quota"] += 1
            continue
        if email_key in local_email_seen:
            result["skipped_local_duplicate"] += 1
            continue
        local_email_seen.add(email_key)
        deduped_payloads.append(_build_remote_sync_payload(acc))

    result["eligible_total"] = len(deduped_payloads)

    if not deduped_payloads:
        result["message"] = "没有符合条件的账号可同步"
        return jsonify(result)

    headers = {
        "Authorization": f"Bearer {remote_auth_token}",
        "Content-Type": "application/json",
    }
    remote_accounts_url = f"{remote_base_url}/api/accounts"

    try:
        remote_list_resp = requests.get(remote_accounts_url, headers=headers, timeout=20)
    except requests.RequestException as e:
        return jsonify({"error": f"拉取远端账号列表失败: {e}"}), 502

    if remote_list_resp.status_code == 401:
        return jsonify({"error": "远端鉴权失败，请检查远程管理 API Key"}), 400
    if not remote_list_resp.ok:
        return jsonify({"error": f"拉取远端账号列表失败: {_extract_api_error_message(remote_list_resp)}"}), 502

    try:
        remote_accounts_payload = remote_list_resp.json()
    except Exception:
        return jsonify({"error": "远端账号列表返回了非 JSON 响应"}), 502

    remote_accounts = remote_accounts_payload.get("accounts", []) if isinstance(remote_accounts_payload, dict) else []
    remote_email_set = {
        _normalize_email_key(item.get("email", ""))
        for item in remote_accounts
        if isinstance(item, dict) and _normalize_email_key(item.get("email", ""))
    }

    to_sync = []
    for payload in deduped_payloads:
        email_key = _normalize_email_key(payload.get("email", ""))
        if email_key in remote_email_set:
            result["skipped_remote_exists"] += 1
            continue
        remote_email_set.add(email_key)
        to_sync.append(payload)

    if not to_sync:
        result["message"] = "远端已存在全部可同步账号，无需新增"
        return jsonify(result)

    try:
        remote_create_resp = requests.post(
            f"{remote_base_url}/api/accounts/batch",
            headers=headers,
            json={"accounts": to_sync},
            timeout=30,
        )
    except requests.RequestException as e:
        return jsonify({"error": f"提交远端批量导入失败: {e}"}), 502

    if remote_create_resp.status_code == 401:
        return jsonify({"error": "远端鉴权失败，请检查远程管理 API Key"}), 400
    if not remote_create_resp.ok:
        return jsonify({"error": f"远端批量导入失败: {_extract_api_error_message(remote_create_resp)}"}), 502

    try:
        remote_create_payload = remote_create_resp.json()
    except Exception:
        return jsonify({"error": "远端批量导入返回了非 JSON 响应"}), 502

    synced = int(remote_create_payload.get("added", 0) or 0)
    result["synced"] = synced
    result["failed"] = max(len(to_sync) - synced, 0)
    result["message"] = f"同步完成：新增 {result['synced']} 个，跳过 {result['skipped_remote_exists']} 个远端已存在账号"
    log(
        f"☁️ Remote sync done: scope={scope} synced={result['synced']} "
        f"skip_exists={result['skipped_remote_exists']} skip_low_quota={result['skipped_low_quota']}",
        "SUCCESS" if result["failed"] == 0 else "WARNING",
    )
    return jsonify(result)

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
    result = acm.test_account(account_id, _get_runtime_upstream_url(), _get_runtime_origin())
    return jsonify(result)

@app.route("/api/accounts/test-all", methods=["POST"])
def api_test_all():
    accounts = acm.list_all()
    results = {}
    ok_count = 0
    for a in accounts:
        r = acm.test_account(a["id"], _get_runtime_upstream_url(), _get_runtime_origin())
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
        refreshed = acm.reactivate_account(account_id, new_token=new_token)
        log(f"✅ Token refreshed for {acc.name}", "SUCCESS")
        return jsonify({"ok": True, "account": refreshed.to_status() if refreshed else acc.to_status()})
    except Exception as e:
        log(f"❌ Token refresh failed for {acc.name}: {e}", "ERROR")
        return jsonify({"error": str(e)}), 500

@app.route("/api/accounts/reset", methods=["POST"])
def api_reset_failures():
    count = acm.reset_failures()
    log(f"🔄 Reset failures for {count} accounts", "INFO")
    return jsonify({"ok": True, "reset": count})

@app.route("/api/accounts/batch-action", methods=["POST"])
def api_batch_action():
    """Batch operations on selected accounts.
    Body: { "ids": ["id1","id2",...], "action": "enable|disable|delete|test|refresh" }
    Or: { "all": true, "action": "enable|disable|delete|test|refresh" }
    """
    data = request.json or {}
    action = data.get("action", "").strip()
    use_all = data.get("all", False)

    if use_all:
        ids = [a["id"] for a in acm.list_all()]
    else:
        ids = data.get("ids", [])

    if not ids:
        return jsonify({"error": "no accounts specified"}), 400
    if action not in ("enable", "disable", "delete", "test", "refresh"):
        return jsonify({"error": f"unknown action: {action}"}), 400

    results = {"action": action, "total": len(ids), "success": 0, "failed": 0, "details": {}}

    for aid in ids:
        try:
            if action == "enable":
                acm.reactivate_account(aid)
                results["success"] += 1
            elif action == "disable":
                acc = acm.get(aid)
                if acc and acc.enabled:
                    acm.toggle(aid)
                results["success"] += 1
            elif action == "delete":
                if acm.delete(aid):
                    results["success"] += 1
                else:
                    results["failed"] += 1
            elif action == "test":
                r = acm.test_account(aid, _get_runtime_upstream_url(), _get_runtime_origin())
                results["details"][aid] = r
                if r.get("ok"):
                    results["success"] += 1
                else:
                    results["failed"] += 1
            elif action == "refresh":
                acc = acm.get(aid)
                if not acc or not acc.email:
                    results["details"][aid] = {"ok": False, "error": "no email"}
                    results["failed"] += 1
                    continue
                try:
                    result = auto_register.refresh_account_token(
                        email=acc.email, password=acc.password,
                        mail_provider=acc.mail_provider, mail_api_key=acc.mail_api_key,
                    )
                    acm.reactivate_account(aid, new_token=result["token"])
                    results["details"][aid] = {"ok": True}
                    results["success"] += 1
                except Exception as e:
                    results["details"][aid] = {"ok": False, "error": str(e)}
                    results["failed"] += 1
        except Exception as e:
            results["details"][aid] = {"ok": False, "error": str(e)}
            results["failed"] += 1

    log(f"📦 Batch {action}: {results['success']}/{results['total']} success", "INFO")
    return jsonify({"ok": True, **results})

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
        r = acm.test_account(a["id"], _get_runtime_upstream_url(), _get_runtime_origin())
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
    Or:   { "account_id": "abc123" }  — looks up email/provider from account
    注意: moemail 的 mail_api_key 可为自动注册生成的 JSON 凭据。
    """
    data = request.json or {}
    account_id = data.get("account_id", "").strip()
    email = data.get("email", "").strip()
    mail_provider = data.get("mail_provider", "").strip()
    mail_api_key = data.get("mail_api_key", "").strip()

    # If account_id is given, look up the account's email and provider
    if account_id and not email:
        acc = acm.get(account_id)
        if not acc:
            return jsonify({"ok": False, "error": "account not found"}), 404
        if not acc.email:
            return jsonify({"ok": False, "error": "account has no email set"}), 400
        email = acc.email
        mail_provider = mail_provider or acc.mail_provider or get_db().get_config("MAIL_PROVIDER_DEFAULT", "gptmail")
        mail_api_key = mail_api_key or acc.mail_api_key or ""

    if not email:
        return jsonify({"error": "email or account_id is required"}), 400

    mail_provider = mail_provider or get_db().get_config("MAIL_PROVIDER_DEFAULT", "gptmail") or "gptmail"

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

@app.route("/api/accounts/emails", methods=["GET"])
def api_list_account_emails():
    """Return a list of accounts that have emails configured (for mail code lookup)."""
    accounts = acm.list_all()
    result = [
        {"id": a["id"], "name": a["name"], "email": a["email"],
         "mail_provider": a.get("mail_provider", "")}
        for a in accounts if a.get("email")
    ]
    return jsonify({"accounts": result})


@app.route("/api/mail/status", methods=["GET"])
def api_mail_status():
    """Return mail service configuration status."""
    db = get_db()
    moe_stats = auto_register.get_moemail_channel_stats({
        "MOEMAIL_API_KEY": db.get_config("MOEMAIL_API_KEY"),
        "MOEMAIL_API_BASE": db.get_config("MOEMAIL_API_BASE"),
        "MOEMAIL_CHANNELS_JSON": db.get_config("MOEMAIL_CHANNELS_JSON", ""),
    })
    return jsonify({
        "default_provider": db.get_config("MAIL_PROVIDER_DEFAULT", "gptmail") or "gptmail",
        "gptmail": {
            "configured": bool(db.get_config("GPTMAIL_API_KEY")),
            "api_base": db.get_config("GPTMAIL_API_BASE", "https://mail.chatgpt.org.uk"),
        },
        "yydsmail": {
            "configured": bool(db.get_config("YYDSMAIL_API_KEY")),
            "api_base": db.get_config("YYDSMAIL_API_BASE", "https://maliapi.215.im/v1"),
        },
        "moemail": {
            "configured": moe_stats["configured"],
            "api_base": db.get_config("MOEMAIL_API_BASE", ""),
            "channels_total": moe_stats["total"],
            "channels_enabled": moe_stats["enabled"],
            "using_channels_json": moe_stats["using_json"],
            "channels_parse_error": moe_stats["parse_error"],
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
    configs = _get_merged_config_rows()
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
    existing_desc = {
        str(item["key"] or "").strip(): str(item.get("description", "") or "")
        for item in _get_merged_config_rows()
    }
    for key, value in configs.items():
        key_text = str(key or "").strip()
        value_text = "" if value is None else str(value)
        db.set_config(key_text, value_text, existing_desc.get(key_text, ""))
        _upsert_env_value(key_text, value_text)
    log(f"Config updated: {list(configs.keys())}", "INFO")
    return jsonify({"ok": True, "updated": list(configs.keys())})


@app.route("/api/captcha/test", methods=["POST"])
def api_test_captcha():
    """测试当前验证码服务连通性。支持传入未保存的表单值。"""
    data = request.json or {}
    try:
        result = auto_register.probe_recaptcha_provider(data)
        level = "INFO" if result.get("ok") else "WARNING"
        log(
            f"Captcha connectivity test: provider={result.get('provider')} ok={result.get('ok')} error={result.get('error', '')}",
            level,
        )
        return jsonify(result)
    except Exception as e:
        log(f"Captcha connectivity test crashed: {e}", "ERROR")
        return jsonify({
            "ok": False,
            "error": str(e),
            "message": f"测试失败: {e}",
        }), 500


@app.route("/api/proxy/test", methods=["POST"])
def api_test_proxy():
    """测试 REGISTER_PROXY 是否可用，并返回 Cloudflare Trace 地区信息。"""
    data = request.json or {}
    try:
        if "proxy" in data:
            proxy = str(data.get("proxy") or "").strip()
        elif "register_proxy" in data:
            proxy = str(data.get("register_proxy") or "").strip()
        else:
            proxy = str(get_db().get_config("REGISTER_PROXY") or os.getenv("REGISTER_PROXY", "") or "").strip()
        disable_ssl_verify = _parse_bool_value(
            data.get("disable_ssl_verify"),
            default=_parse_bool_value(get_db().get_config("DISABLE_SSL_VERIFY", os.getenv("DISABLE_SSL_VERIFY", "0"))),
        )
        result = _probe_register_proxy(proxy, disable_ssl_verify=disable_ssl_verify)
        log(
            f"Register proxy test: proxy={result.get('proxy') or '(empty)'} ok={result.get('ok')} loc={result.get('loc') or '-'} error={result.get('error') or ''}",
            "INFO" if result.get("ok") else "WARNING",
        )
        return jsonify(result)
    except Exception as e:
        log(f"Register proxy test crashed: {e}", "ERROR")
        return jsonify({
            "ok": False,
            "error": str(e),
            "message": f"代理测试失败: {e}",
        }), 500


# =========================================================================
#  Auto Registration API
# =========================================================================
@app.route("/api/auto-register/config", methods=["GET"])
def api_auto_register_config():
    """Check auto-register configuration status."""
    cfg = auto_register.check_config()
    cfg["current_task"] = _get_manual_register_task()
    return jsonify(cfg)


@app.route("/api/auto-register/start", methods=["POST"])
def api_auto_register_start():
    """Start a manual register task and return immediately."""
    data = request.json or {}
    count = _normalize_manual_register_count(data.get("count", 1))
    threads = _normalize_manual_register_threads(data.get("threads", 1), count)
    captcha_provider = str(data.get("captcha_provider") or "").strip()

    config = auto_register.check_config(captcha_provider)
    if not config["ready"]:
        missing = list(config.get("mail_provider_missing") or [])
        for item in config.get("captcha_missing") or []:
            if item not in missing:
                missing.insert(0, item)
        return jsonify({
            "error": f"Auto-register not configured. Missing: {', '.join(missing)}",
            "config": config,
        }), 400

    task, err = _start_manual_register_task(
        count,
        threads,
        config.get("captcha_provider") or captcha_provider,
    )
    if err == "busy":
        return jsonify({
            "error": "Manual register task already running",
            "task": task,
        }), 409
    return jsonify({
        "ok": True,
        "task": task,
        "config": config,
    })


@app.route("/api/auto-register/stop", methods=["POST"])
def api_auto_register_stop():
    data = request.json or {}
    task_id = str(data.get("task_id") or "").strip()
    task = _request_stop_manual_register(task_id or None)
    if not task:
        return jsonify({"error": "no matching manual register task"}), 404
    return jsonify({"ok": True, "task": task})


@app.route("/api/auto-register/stream", methods=["GET"])
def api_auto_register_stream():
    task_id = str(request.args.get("task_id") or "").strip()
    if not task_id:
        return jsonify({"error": "task_id is required"}), 400

    def event_stream():
        last_seq = 0
        last_status = None
        while True:
            task = _get_manual_register_task(task_id, include_logs=True)
            if not task:
                payload = {"message": "task not found", "task_id": task_id}
                yield f"event: error\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                break

            if task["status"] != last_status:
                state_payload = {k: v for k, v in task.items() if k != "logs"}
                yield f"event: state\ndata: {json.dumps(state_payload, ensure_ascii=False)}\n\n"
                last_status = task["status"]

            logs = task.get("logs") or []
            for item in logs:
                if item["seq"] <= last_seq:
                    continue
                yield f"event: log\ndata: {json.dumps(item, ensure_ascii=False)}\n\n"
                last_seq = item["seq"]

            if task["status"] in {"completed", "failed", "stopped"}:
                done_payload = {k: v for k, v in task.items() if k != "logs"}
                yield f"event: done\ndata: {json.dumps(done_payload, ensure_ascii=False)}\n\n"
                break

            yield ": ping\n\n"
            time.sleep(1)

    return Response(
        event_stream(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )

@app.route("/api/auto-register", methods=["POST"])
def api_auto_register():
    """Manually trigger registration of new account(s).
    Body: { "count": 1 }
    """
    current_task = _get_manual_register_task()
    if current_task and current_task["status"] in {"running", "stopping"}:
        return jsonify({
            "error": "Manual register task already running",
            "task": current_task,
        }), 409

    data = request.json or {}
    count = _normalize_manual_register_count(data.get("count", 1))
    threads = _normalize_manual_register_threads(data.get("threads", 1), count)
    captcha_provider = str(data.get("captcha_provider") or "").strip()

    config = auto_register.check_config(captcha_provider)
    if not config["ready"]:
        missing = list(config.get("mail_provider_missing") or [])
        for item in config.get("captcha_missing") or []:
            if item not in missing:
                missing.insert(0, item)
        return jsonify({
            "error": f"Auto-register not configured. Missing: {', '.join(missing)}",
            "config": config,
        }), 400

    log(
        f"🔄 Manual auto-register triggered: {count} account(s), threads={threads}, captcha={config.get('captcha_provider')}",
        "INFO",
    )
    try:
        results = auto_register.auto_register_batch(
            count=count,
            threads=threads,
            account_manager=acm,
            upstream_url=_get_runtime_upstream_url(),
            origin=_get_runtime_origin(),
            captcha_provider_override=captcha_provider,
        )
    except Exception as e:
        log(f"❌ Manual auto-register crashed: {e}", "ERROR")
        return jsonify({
            "error": str(e),
            "config": auto_register.check_config(captcha_provider),
        }), 500
    return jsonify({
        "ok": True,
        "registered": len(results),
        "requested": count,
        "threads": threads,
        "accounts": results,
        "captcha_provider": config.get("captcha_provider"),
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
        "upstream": _get_runtime_upstream_url(),
    })


# =========================================================================
#  Model Catalog
# =========================================================================
@app.route("/v1/models", methods=["GET"])
def list_models():
    try:
        return jsonify(_get_model_catalog(force_refresh=False))
    except Exception as e:
        log(f"⚠️ Failed to fetch model catalog: {e}", "WARNING")
        status = 500 if "no accounts configured" in str(e) else 502
        return jsonify({"error": str(e)}), status


@app.route("/v1/images/generations", methods=["POST"])
def image_generations():
    body = request.json or {}
    model = str(body.get("model") or "").strip()
    prompt = str(body.get("prompt") or "").strip()
    if not model:
        return jsonify({"error": {"message": "model is required", "type": "invalid_request_error"}}), 400
    if not prompt:
        return jsonify({"error": {"message": "prompt is required", "type": "invalid_request_error"}}), 400

    try:
        result = _generate_openai_image_result(
            model,
            prompt,
            size=body.get("size"),
            n=body.get("n", 1),
            response_format=body.get("response_format", "b64_json"),
            quality=body.get("quality"),
            client_token=request.headers.get("token", ""),
            client_visitorid=request.headers.get("visitorid", ""),
        )
        payload = {
            "created": result["created"],
            "data": [],
        }
        for item in result.get("data", []):
            row = dict(item)
            row["revised_prompt"] = prompt
            payload["data"].append(row)
        return jsonify(payload)
    except ValueError as e:
        return jsonify({"error": {"message": str(e), "type": "invalid_request_error"}}), 400
    except requests.RequestException as e:
        return jsonify({"error": {"message": str(e), "type": "upstream_error"}}), 502
    except NoAvailableAccountError:
        return jsonify({"error": {"message": "no accounts configured", "type": "config_error"}}), 500
    except Exception as e:
        log(f"❌ Image generation failed: {e}", "ERROR")
        return jsonify({"error": {"message": str(e), "type": "upstream_error"}}), 502


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
            f"{_get_runtime_upstream_url()}/gamsai_api/v1/page_service/aiTools",
            headers=acm.upstream_headers(acc, _get_runtime_origin()),
            json={"language": "zh"}, timeout=15,
            verify=not _get_runtime_disable_ssl_verify(),
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
            f"{_get_runtime_upstream_url()}/gamsai_api/v1/page_service/aiTools/{tool_id}/execute",
            headers=acm.upstream_headers(acc, _get_runtime_origin()),
            json=payload, timeout=60,
            verify=not _get_runtime_disable_ssl_verify(),
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
            f"{_get_runtime_upstream_url()}/aichat/conversations",
            headers=acm.upstream_headers(acc, _get_runtime_origin()),
            json={"page": data.get("page", 1), "limit": data.get("limit", 20)},
            timeout=15,
            verify=not _get_runtime_disable_ssl_verify(),
        )
        r.raise_for_status()
        acm.mark_ok(acc)
        return jsonify(r.json())
    except Exception as e:
        acm.mark_fail(acc, str(e))
        return jsonify({"error": str(e)}), 502


@app.route("/v1/chat/init", methods=["POST"])
def new_conversation():
    data = request.json or {}
    acc, _ = acm.next()
    if not acc:
        return jsonify({"error": "no accounts configured"}), 500
    try:
        model = data.get("model", "model_25")
        r = requests.post(
            f"{_get_runtime_upstream_url()}/chatgpt/newConversation",
            headers=acm.upstream_headers(acc, _get_runtime_origin()),
            json={"model": resolve_rita_model(model)}, timeout=15,
            verify=not _get_runtime_disable_ssl_verify(),
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
            f"{_get_runtime_upstream_url()}/aichat/getTitle",
            headers=acm.upstream_headers(acc, _get_runtime_origin()),
            json={"chat_id": data.get("chat_id"), "messages": build_rita_messages(data.get("messages", []))},
            timeout=15,
            verify=not _get_runtime_disable_ssl_verify(),
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
    return handle_chat_completions_api(request, _build_protocol_deps())


@app.route("/v1/responses", methods=["POST"])
def responses_api():
    return handle_responses_api(request, _build_protocol_deps())


@app.route("/v1/messages/count_tokens", methods=["POST"])
@app.route("/v1/v1/messages/count_tokens", methods=["POST"])
def anthropic_count_tokens():
    return handle_anthropic_count_tokens(request, _build_protocol_deps())


@app.route("/v1/messages", methods=["POST"])
@app.route("/v1/v1/messages", methods=["POST"])
def anthropic_messages_api():
    return handle_anthropic_messages_api(request, _build_protocol_deps())


# =========================================================================
#  Debug
# =========================================================================
@app.route("/debug/state", methods=["GET"])
def debug_state():
    with _conv_lock, _responses_lock:
        return jsonify({
            "conversations": len(_conversation_state),
            "responses": len(_responses_state),
            **acm.summary(),
            "upstream": _get_runtime_upstream_url(),
        })

@app.route("/debug/clear", methods=["POST"])
def debug_clear():
    with _conv_lock:
        _conversation_state.clear()
    with _responses_lock:
        _responses_state.clear()
    log("🧹 Cleared conversation state", "WARNING")
    return jsonify({"status": "ok"})


# =========================================================================
#  Startup
# =========================================================================
def main():
    s = acm.summary()
    runtime_upstream = _get_runtime_upstream_url()
    runtime_origin = _get_runtime_origin()
    print("\n" + "=" * 60)
    print("🚀 rita2api starting")
    print(f"📍 Port: {PORT}  Upstream: {runtime_upstream}")
    print(f"🔑 Accounts: {s['total']} total, {s['active']} active, {s['disabled']} disabled")
    print(f"🌐 WebUI: http://localhost:{PORT}/")
    print("=" * 60 + "\n")

    # Start background token health checker
    acm.start_health_checker(runtime_upstream, runtime_origin, log_fn=log)

    # Start auto-replenish (registers new accounts when pool runs low)
    auto_register.start_auto_replenish(acm, runtime_upstream, runtime_origin, log_fn=log)

    app.run(host=HOST, port=PORT, debug=False, threaded=True)


if __name__ == "__main__":
    main()

"""
rita-register — Rita.ai 批量自动注册工具

注册流程 (accountapi.gosplit.net):
  1. POST /authorize/authenticate              → 初始化会话
  2. POST /authorize/sign_process             → 提交邮箱 + agree (可能触发 need_captcha)
  3. [YesCaptcha 过 reCAPTCHA Enterprise]     → 如果 step2 返回 need_captcha=1
  4. POST /authorize/emailCode (captcha)       → 发送邮箱 OTP (首次需要 captcha token)
  5. 等待邮箱验证码 (GPTMail / YYDS Mail)
  6. POST /authorize/emailCode (resend)       → 重发验证码 (无需 captcha，会话已验证)
  7. POST /authorize/code_sign                 → 提交 OTP → 注册成功
  8. POST /authorize/authenticate              → 获取 session token
  9. POST /user/silent_edit                    → 设置密码

依赖: curl_cffi (随机浏览器指纹)
"""

import json
import os
import re
import time
import random
import string
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests as stdlib_requests  # 仅用于 YesCaptcha / upload
from curl_cffi import requests as curl_requests

# ===================== Config =====================
def _load_config() -> dict:
    defaults = {
        "total_accounts": 3,
        "max_workers": 2,
        "password": "@qazwsx123456",
        "mail_provider": "gptmail",
        "gptmail_api_base": "https://mail.chatgpt.org.uk",
        "gptmail_api_key": "gpt-test",
        "yydsmail_api_base": "https://maliapi.215.im/v1",
        "yydsmail_api_key": "",
        "yescaptcha_client_key": "",
        "yescaptcha_website_key": "6Lej6N4hAAAAANgkiQRXxLrlue_J_y035Dm6UhPk",
        "yescaptcha_website_url": "https://account.rita.ai",
        "yescaptcha_task_type": "NoCaptchaTaskProxyless",
        "rita_account_api": "https://accountapi.gosplit.net",
        "rita_origin": "https://account.rita.ai",
        "rita_redirect_uri": "https://www.rita.ai/zh/ai-chat",
        "rita_language": "zh",
        "proxy": "",
        "mail_use_proxy": False,
        "upload_enabled": True,
        "upload_api_url": "http://localhost:10089/api/accounts/batch",
        "output_file": "registered_accounts.txt",
    }
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                file_config = json.load(f)
                file_config = {k: v for k, v in file_config.items() if not k.startswith("_comment")}
                defaults.update(file_config)
        except Exception as e:
            print(f"[WARN] 加载 config.json 失败: {e}")
    # Env overrides
    for key in list(defaults.keys()):
        env_val = os.environ.get(key.upper())
        if env_val is not None:
            if isinstance(defaults[key], bool):
                defaults[key] = env_val.lower() in ("1", "true", "yes")
            elif isinstance(defaults[key], int):
                try:
                    defaults[key] = int(env_val)
                except ValueError:
                    pass
            else:
                defaults[key] = env_val
    return defaults


CFG = _load_config()

_print_lock = threading.Lock()
_file_lock = threading.Lock()


def log(msg, tag=""):
    prefix = f"[{tag}] " if tag else ""
    with _print_lock:
        print(f"[{time.strftime('%H:%M:%S')}] {prefix}{msg}", flush=True)


# ===================== 浏览器指纹 =====================

_CHROME_PROFILES = [
    # 只保留 curl_cffi 支持且验证过可通过 Rita 风控的版本
    {"major": 120, "impersonate": "chrome120", "build": 6099, "patch": (109, 234),
     "sec_ch_ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"'},
]

_OS_PROFILES = [
    # 锁定 Windows — 验证过可通过 Rita 风控
    {"platform": "Windows NT 10.0; Win64; x64", "sec_ch_ua_platform": '"Windows"'},
]

_ACCEPT_LANGUAGES = [
    "zh-CN,zh;q=0.9,en;q=0.8",
    "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
    "en-US,en;q=0.9",
]


def _random_fingerprint():
    """返回 (impersonate, ua, headers_dict)"""
    chrome = random.choice(_CHROME_PROFILES)
    os_p = random.choice(_OS_PROFILES)
    major = chrome["major"]
    build = chrome["build"]
    patch = random.randint(*chrome["patch"])
    full_ver = f"{major}.0.{build}.{patch}"
    ua = (
        f"Mozilla/5.0 ({os_p['platform']}) "
        f"AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{full_ver} Safari/537.36"
    )
    hdrs = {
        "User-Agent": ua,
        "Accept-Language": random.choice(_ACCEPT_LANGUAGES),
        "sec-ch-ua": chrome["sec_ch_ua"],
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": os_p["sec_ch_ua_platform"],
    }
    return chrome["impersonate"], ua, hdrs


# ===================== Mail Providers =====================

def _extract_code(text: str) -> str | None:
    """从邮件内容提取 4-6 位验证码"""
    if not text:
        return None
    patterns = [
        r"(?:verification|verify|code|验证码|验证|代码)[\s:：]*(\d{4,6})",
        r">\s*(\d{4,6})\s*<",
        r"\b(\d{4,6})\b",
    ]
    for pat in patterns:
        matches = re.findall(pat, text, re.IGNORECASE)
        for code in matches:
            if len(code) >= 4 and not code.startswith("0"):
                return code
    return None


class GPTMailProvider:
    """GPTMail (mail.chatgpt.org.uk)"""

    def __init__(self, impersonate: str):
        self.base = CFG["gptmail_api_base"].rstrip("/")
        self.api_key = CFG["gptmail_api_key"]
        self.imp = impersonate
        self.session = curl_requests.Session(impersonate=self.imp)
        if CFG.get("mail_use_proxy") and CFG.get("proxy"):
            self.session.proxies = {"http": CFG["proxy"], "https": CFG["proxy"]}

    def _headers(self) -> dict:
        h = {"Accept": "application/json"}
        if self.api_key:
            h["X-API-Key"] = self.api_key
        return h

    def create_email(self) -> tuple[str, dict]:
        r = self.session.post(
            f"{self.base}/api/generate-email",
            headers=self._headers(), json={}, timeout=15, impersonate=self.imp,
        )
        r.raise_for_status()
        data = r.json()
        email = data.get("data", {}).get("email", "")
        if not email:
            raise Exception(f"GPTMail 创建失败: {data}")
        return email, {"email": email}

    def wait_for_code(self, ctx: dict, timeout: int = 120) -> str | None:
        email = ctx["email"]
        start = time.time()
        while time.time() - start < timeout:
            try:
                r = self.session.get(
                    f"{self.base}/api/emails", params={"email": email},
                    headers=self._headers(), timeout=15, impersonate=self.imp,
                )
                if r.status_code == 200:
                    emails_list = r.json().get("data", {}).get("emails", [])
                    for mail in emails_list:
                        code = _extract_code(mail.get("subject", ""))
                        if code:
                            return code
                        mail_id = mail.get("id")
                        if mail_id:
                            detail = self._detail(mail_id)
                            if detail:
                                content = detail.get("data", {}).get("content", "")
                                html = detail.get("data", {}).get("html_content", "")
                                code = _extract_code(content) or _extract_code(html)
                                if code:
                                    return code
            except Exception:
                pass
            time.sleep(3)
        return None

    def _detail(self, mail_id: str):
        try:
            r = self.session.get(
                f"{self.base}/api/email/{mail_id}",
                headers=self._headers(), timeout=15, impersonate=self.imp,
            )
            return r.json() if r.status_code == 200 else None
        except Exception:
            return None


class YYDSMailProvider:
    """YYDS Mail (maliapi.215.im)"""

    def __init__(self, impersonate: str):
        self.base = CFG["yydsmail_api_base"].rstrip("/")
        self.api_key = CFG["yydsmail_api_key"]
        self.imp = impersonate
        self.session = curl_requests.Session(impersonate=self.imp)
        if CFG.get("mail_use_proxy") and CFG.get("proxy"):
            self.session.proxies = {"http": CFG["proxy"], "https": CFG["proxy"]}
        self._domains: list[str] = []

    def _headers(self, token: str = "") -> dict:
        h = {"Accept": "application/json", "Content-Type": "application/json"}
        if token:
            h["Authorization"] = f"Bearer {token}"
        elif self.api_key:
            h["X-API-Key"] = self.api_key
        return h

    def _fetch_domains(self) -> list[str]:
        if self._domains:
            return self._domains
        try:
            r = self.session.get(f"{self.base}/domains", headers=self._headers(), timeout=15, impersonate=self.imp)
            if r.status_code == 200:
                raw = r.json()
                data = raw if isinstance(raw, list) else raw.get("data", [])
                names = [d.get("domain") or d if isinstance(d, str) else d.get("domain", "") for d in data]
                self._domains = [n for n in names if n]
        except Exception:
            pass
        return self._domains

    def create_email(self) -> tuple[str, dict]:
        domains = self._fetch_domains()
        if not domains:
            raise Exception("YYDS Mail: 无可用域名")
        domain = random.choice(domains)
        prefix = "".join(random.choices(string.ascii_lowercase + string.digits, k=random.randint(8, 12)))
        r = self.session.post(
            f"{self.base}/accounts", headers=self._headers(),
            json={"address": prefix, "domain": domain}, timeout=15, impersonate=self.imp,
        )
        if r.status_code not in (200, 201):
            raise Exception(f"YYDS Mail 创建失败: {r.status_code} {r.text[:200]}")
        resp = r.json()
        data = resp.get("data", resp) if isinstance(resp, dict) else resp
        email = data.get("address", f"{prefix}@{domain}")
        token = data.get("token", "")
        if not token:
            raise Exception(f"YYDS Mail 未返回 token")
        return email, {"token": token}

    def wait_for_code(self, ctx: dict, timeout: int = 120) -> str | None:
        token = ctx["token"]
        start = time.time()
        while time.time() - start < timeout:
            try:
                r = self.session.get(
                    f"{self.base}/messages", headers=self._headers(token),
                    timeout=15, impersonate=self.imp,
                )
                if r.status_code == 200:
                    resp = r.json()
                    msgs = resp if isinstance(resp, list) else (resp.get("data", {}).get("messages", []) if isinstance(resp.get("data"), dict) else resp.get("data", []))
                    for msg in (msgs or []):
                        msg_id = msg.get("id")
                        if not msg_id:
                            continue
                        detail = self._detail(token, msg_id)
                        if detail:
                            content = detail.get("text", "") or detail.get("html", "")
                            if isinstance(content, list):
                                content = " ".join(content)
                            code = _extract_code(content)
                            if code:
                                return code
            except Exception:
                pass
            time.sleep(3)
        return None

    def _detail(self, token: str, msg_id: str):
        try:
            r = self.session.get(
                f"{self.base}/messages/{msg_id}", headers=self._headers(token),
                timeout=15, impersonate=self.imp,
            )
            if r.status_code == 200:
                resp = r.json()
                return resp.get("data", resp) if isinstance(resp, dict) else resp
        except Exception:
            pass
        return None


def get_mail_provider(impersonate: str):
    if CFG.get("mail_provider", "").lower() == "yydsmail":
        return YYDSMailProvider(impersonate)
    return GPTMailProvider(impersonate)


# ===================== YesCaptcha =====================

# ===================== YesCaptcha =====================

# YesCaptcha 支持的任务类型，按优先级尝试
_RECAPTCHA_TASK_TYPES = [
    "RecaptchaV2EnterpriseTaskProxyless",  # 企业版 (需要完整参数)
    "NoCaptchaTaskProxyless",              # 标准 V2 兜底
]


def solve_recaptcha(tag: str = "") -> str:
    """
    用 YesCaptcha 过 reCAPTCHA Enterprise.
    返回 g-recaptcha-response token.

    HAR 解析出的完整参数:
      sitekey:  6Lej6N4hAAAAANgkiQRXxLrlue_J_y035Dm6UhPk
      subdomain: account.rita.ai:443
      version:  kUYUkUlSyqkjTSMaN2w3RaOh
      language: zh-CN

    策略: 依次尝试多个任务类型，直到成功。确保每种类型都有足够的 API 调用次数。
    """
    client_key = CFG.get("yescaptcha_client_key", "")
    if not client_key:
        raise Exception("yescaptcha_client_key 未配置")

    website_key = CFG.get("yescaptcha_website_key", "6Lej6N4hAAAAANgkiQRXxLrlue_J_y035Dm6UhPk")
    website_url = CFG.get("yescaptcha_website_url", "https://account.rita.ai")

    # 优先使用配置的 task_type，否则依次尝试 _RECAPTCHA_TASK_TYPES
    task_types_to_try = [
        CFG.get("yescaptcha_task_type", _RECAPTCHA_TASK_TYPES[0])
    ] + [t for t in _RECAPTCHA_TASK_TYPES if t != CFG.get("yescaptcha_task_type", "")]

    # 去重
    seen = set()
    task_types_to_try = [x for x in task_types_to_try if not (x in seen or seen.add(x))]

    last_error = None
    for task_type in task_types_to_try:
        log(f"[YesCaptcha] 尝试任务类型: {task_type}", tag)
        try:
            token = _solve_one_type(
                client_key, website_key, website_url, task_type, tag
            )
            return token
        except Exception as e:
            last_error = e
            log(f"[YesCaptcha] {task_type} 失败: {e}", tag)

    raise Exception(f"All YesCaptcha task types failed. Last error: {last_error}")


def _solve_one_type(
    client_key: str, website_key: str, website_url: str,
    task_type: str, tag: str
) -> str:
    """用指定任务类型执行一次完整的 YesCaptcha 验证流程."""

    # 根据任务类型构造不同的 task payload
    task_payload = {
        "websiteURL": website_url,
        "websiteKey": website_key,
        "type": task_type,
    }

    # reCAPTCHA Enterprise 需要额外参数 (from HAR)
    if task_type == "RecaptchaV2EnterpriseTaskProxyless":
        task_payload["enterprisePayload"] = {
            "s": "ENTERPRISE",           # 明确告知是 Enterprise 版
            "co": "aHR0cHM6Ly9hY2NvdW50LnJpdGEuYWk6NDQz",
            "hl": "zh-CN",
        }
        task_payload["apiDomain"] = "https://www.google.com/recaptcha/enterprise.js"

    log(f"[YesCaptcha] 创建任务 (type={task_type})...", tag)

    # Create task
    create_payload = {
        "clientKey": client_key,
        "task": task_payload,
    }
    r = stdlib_requests.post(
        "https://api.yescaptcha.com/createTask",
        json=create_payload,
        timeout=30,
    )
    result = r.json()
    log(f"[YesCaptcha] createTask 响应: errorId={result.get('errorId', 0)} taskId={result.get('taskId', 'N/A')}", tag)

    if result.get("errorId", 0) != 0:
        raise Exception(
            f"[{result.get('errorCode')}] {result.get('errorDescription', result)}"
        )

    task_id = result.get("taskId")
    if not task_id:
        raise Exception(f"无 taskId: {result}")

    # Poll for result (间隔 1s; 最多 120s)
    max_attempts = 120
    for attempt in range(max_attempts):
        time.sleep(1)
        try:
            r = stdlib_requests.post(
                "https://api.yescaptcha.com/getTaskResult",
                json={"clientKey": client_key, "taskId": task_id},
                timeout=15,
            )
            result = r.json()

            if result.get("errorId", 0) != 0:
                raise Exception(
                    f"[{result.get('errorCode')}] {result.get('errorDescription', result)}"
                )

            status = result.get("status", "")

            if status == "ready":
                solution = result.get("solution", {})
                # YesCaptcha 返回字段兼容: gRecaptchaResponse / g-recaptcha-response / token
                token = (
                    solution.get("gRecaptchaResponse") or
                    solution.get("g-recaptcha-response") or
                    solution.get("token", "")
                )
                if token:
                    log(f"[YesCaptcha] OK ({len(token)} chars, {attempt + 1}s)", tag)
                    return token
                else:
                    log(f"[YesCaptcha] ready 但无 token, keys={list(solution.keys())}", tag)
                    raise Exception(f"ready 但无 token: {result}")

            if attempt % 10 == 0:
                log(f"[YesCaptcha] 等待... status={status} ({attempt + 1}s)", tag)

        except stdlib_requests.RequestException as e:
            if attempt % 10 == 0:
                log(f"[YesCaptcha] 网络错误: {e} ({attempt + 1}s)", tag)

    raise Exception(f"YesCaptcha {task_type} 超时 ({max_attempts}s)")


# ===================== Rita Registration =====================

class RitaRegistration:
    """
    Rita.ai 注册流程 (基于 HAR 抓包还原):
      1. sign_process(email+agree)  → 提交邮箱，返回 need_captcha=1
      2. solve_recaptcha             → 过 reCAPTCHA Enterprise
      3. sign_process(captcha)       → 提交 captcha 完成邮箱验证
      4. emailCode(captcha)          → 首次发送 OTP (需要 captcha token)
      5. wait OTP                    → 从临时邮箱获取验证码
      6. emailCode(resend)           → 重发 OTP (无需 captcha，会话已验证)
      7. code_sign                   → 提交 OTP 完成注册
      8. authenticate                → 获取 session token
      9. silent_edit                 → 设置密码
    """

    MAX_RESEND_ATTEMPTS = 2  # 最多重发验证码次数
    MAX_CAPTCHA_ATTEMPTS = 4  # 最多 captcha 提交次数 (首次 + 3次重试)

    def __init__(self, tag: str = ""):
        self.tag = tag
        self.api = CFG["rita_account_api"].rstrip("/")
        self.origin = CFG["rita_origin"]
        self.redirect_uri = CFG["rita_redirect_uri"]
        self.language = CFG["rita_language"]

        # 随机浏览器指纹
        self.impersonate, self.ua, self.fp_headers = _random_fingerprint()
        self.session = curl_requests.Session(impersonate=self.impersonate)

        proxy = CFG.get("proxy", "")
        if proxy:
            self.session.proxies = {"http": proxy, "https": proxy}

        # State
        self.token: str = ""
        self.visitorid: str = ""

    def _log(self, msg):
        log(msg, self.tag)

    def _post(self, path: str, payload: dict) -> dict:
        hdrs = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "Origin": self.origin,
            "Referer": f"{self.origin}/",
            **self.fp_headers,
        }
        if self.token:
            hdrs["token"] = self.token
        if self.visitorid:
            hdrs["visitorid"] = self.visitorid

        r = self.session.post(
            f"{self.api}{path}", json=payload, headers=hdrs,
            timeout=30, impersonate=self.impersonate,
        )
        try:
            return r.json()
        except Exception:
            return {"_raw": r.text[:500], "_status": r.status_code}

    def _update_auth(self, resp: dict):
        """从响应中提取 token / visitorid"""
        if not isinstance(resp, dict):
            return
        data = resp.get("data", resp)
        if not isinstance(data, dict):
            return
        for key in ("token", "access_token", "session_token"):
            t = data.get(key, "")
            if t and isinstance(t, str) and len(t) > 8:
                self.token = t
                self._log(f"   → token: {t[:8]}***{t[-4:]}")
                break
        for key in ("visitorid", "visitor_id"):
            v = data.get(key, "")
            if v and isinstance(v, str) and len(v) > 8:
                self.visitorid = v
                self._log(f"   → visitorid: {v[:8]}***")
                break

    def _delay(self, lo=0.3, hi=0.8):
        time.sleep(random.uniform(lo, hi))

    # ---- API steps ----

    def step_sign_process(self, email: str, captcha_token: str = "") -> dict:
        """
        POST /authorize/sign_process
        - 首次调用 (无 captcha): 提交邮箱 + agree，触发 need_captcha=1
        - 带 captcha 调用: 完成验证，建立会话
        """
        payload = {
            "redirect_uri": self.redirect_uri,
            "language": self.language,
            "email": email,
            "agree": 1,
        }
        if captcha_token:
            payload["g-recaptcha-response"] = captcha_token
        resp = self._post("/authorize/sign_process", payload)
        self._update_auth(resp)
        return resp

    def step_email_code(self, email: str, captcha_token: str = "") -> dict:
        """
        POST /authorize/emailCode — 发送 / 重发邮箱验证码
        - 首次: 需要 g-recaptcha-response
        - 重发: 无需 captcha (会话已通过验证)
        """
        payload = {
            "email": email,
            "language": self.language,
            "redirect_uri": self.redirect_uri,
        }
        if captcha_token:
            payload["g-recaptcha-response"] = captcha_token
        resp = self._post("/authorize/emailCode", payload)
        self._update_auth(resp)
        return resp

    def step_code_sign(self, email: str, code: str) -> dict:
        """POST /authorize/code_sign — 提交 OTP 完成注册"""
        resp = self._post("/authorize/code_sign", {
            "email": email,
            "code": code,
            "redirect_uri": self.redirect_uri,
            "language": self.language,
            "agreeTC": 1,
        })
        self._update_auth(resp)
        return resp

    def step_authenticate(self) -> dict:
        """POST /authorize/authenticate — 获取 session token"""
        resp = self._post("/authorize/authenticate", {"redirect_uri": self.redirect_uri})
        self._update_auth(resp)
        return resp

    def step_set_password(self, password: str) -> dict:
        """POST /user/silent_edit — 设置密码"""
        resp = self._post("/user/silent_edit", {
            "password": password,
            "language": self.language,
        })
        self._update_auth(resp)
        return resp

    def _wait_otp(self, mail_provider, mail_ctx: dict, timeout: int = 90) -> str | None:
        """等待邮箱验证码"""
        return mail_provider.wait_for_code(mail_ctx, timeout=timeout)

    # ---- Full flow with OTP retry ----

    def register(self, email: str, password: str, mail_provider, mail_ctx: dict) -> dict:
        result = {"ok": False, "token": "", "error": ""}
        captcha_token: str = ""

        try:
            # === Phase 1: 邮箱验证 + reCAPTCHA ===

            # 1. 提交邮箱 (无 captcha → 返回 need_captcha=1)
            self._log("Step 1: sign_process (email + agree)")
            resp1 = self.step_sign_process(email)
            self._log(f"   → code={resp1.get('code', '?')} need_captcha={resp1.get('data', {}).get('need_captcha', 0)}")

            # 人性化等待: Rita 风控检测连续快速操作
            time.sleep(random.uniform(2.5, 4.5))

            # 2. 过 reCAPTCHA (最多尝试 MAX_CAPTCHA_ATTEMPTS 次)
            for cap_attempt in range(1, self.MAX_CAPTCHA_ATTEMPTS + 1):
                if cap_attempt > 1:
                    self._log(f"   [WARN] captcha 提交失败 (第{cap_attempt-1}次)，重新求解...")
                    # 失败后等待更长时间再重试
                    time.sleep(random.uniform(5.0, 8.0))
                    self._log(f"Step 2b: solve reCAPTCHA (attempt {cap_attempt})")
                else:
                    self._log("Step 2: solve reCAPTCHA")

                captcha_token = solve_recaptcha(self.tag)

                # captcha 解出后，等待短暂时间再提交（模拟人类操作延迟）
                time.sleep(random.uniform(1.5, 3.5))

                # 3. 提交 sign_process + captcha
                if cap_attempt == 1:
                    self._log("Step 3: sign_process (with captcha)")
                else:
                    self._log(f"Step 3b: sign_process (with captcha, attempt {cap_attempt})")

                resp3 = self.step_sign_process(email, captcha_token)
                resp3_code = resp3.get("code", -1)
                resp3_type = resp3.get("type", "")
                self._log(f"   → code={resp3_code} type={resp3_type} msg={resp3.get('message', '')}")

                if resp3_code == 0 and resp3_type == "success":
                    # captcha 验证成功！
                    self._log("   → captcha 验证成功！")
                    break
                elif cap_attempt >= self.MAX_CAPTCHA_ATTEMPTS:
                    result["error"] = f"captcha 验证失败 ({self.MAX_CAPTCHA_ATTEMPTS} 次尝试): code={resp3_code} {resp3.get('message', '')}"
                    return result
                # else: continue to next captcha attempt

            self._delay(0.5, 1.5)

            # === Phase 2: 发送 OTP + 等待验证码 ===

            # 4. 通过 emailCode 发送验证码 (使用最新验证通过的 captcha_token)
            self._log("Step 4: emailCode (send OTP)")
            resp4 = self.step_email_code(email, captcha_token)
            resp4_code = resp4.get("code", -1)
            self._log(f"   → code={resp4_code} type={resp4.get('type', '?')}")
            # emailCode 即使返回错误也可能已发送了 OTP（HAR 中成功流程也返回非0 code）
            if resp4_code != 0 and resp4.get("type") != "success":
                self._log(f"   [WARN] emailCode 返回异常，继续等待 OTP...")
            self._delay(1.0, 2.5)

            # 5. 等待 OTP (带重发机制)
            otp_code = None
            for attempt in range(1 + self.MAX_RESEND_ATTEMPTS):
                if attempt == 0:
                    self._log(f"Step 5: 等待验证码邮件 (最多 90s)...")
                else:
                    # 重发: 直接调用 emailCode，无需 captcha (会话已验证)
                    self._log(f"   [WARN] 第 {attempt} 次重发验证码...")
                    resp_resend = self.step_email_code(email)
                    resp_r_code = resp_resend.get("code", -1)
                    self._log(f"   → resend code={resp_r_code}")
                    # 等待一段时间让邮件到达
                    time.sleep(random.uniform(2.0, 4.0))

                otp_code = self._wait_otp(mail_provider, mail_ctx, timeout=90)
                if otp_code:
                    break
                self._log(f"   [WARN] 第 {attempt + 1} 次等待 OTP 超时")

            if not otp_code:
                result["error"] = f"验证码获取超时 (已重发 {self.MAX_RESEND_ATTEMPTS} 次)"
                return result

            self._log(f"   [MAIL] OTP: {otp_code}")
            self._delay()

            # === Phase 3: 提交 OTP + 获取 Token ===

            # 6. 提交 OTP
            self._log(f"Step 6: code_sign (code={otp_code})")
            resp6 = self.step_code_sign(email, otp_code)
            resp6_code = resp6.get("code", -1)
            self._log(f"   → code={resp6_code} type={resp6.get('type', '?')}")

            # OTP 验证失败？尝试重发一次再验证
            if resp6_code != 0 and resp6.get("type") != "success":
                self._log(f"   [WARN] OTP 验证失败，尝试重发...")
                time.sleep(random.uniform(3.0, 5.0))
                self.step_email_code(email)
                time.sleep(random.uniform(3.0, 5.0))
                # 再等一次 OTP
                otp_code2 = self._wait_otp(mail_provider, mail_ctx, timeout=60)
                if otp_code2 and otp_code2 != otp_code:
                    self._log(f"   [MAIL] 新 OTP: {otp_code2}，重新提交...")
                    resp6 = self.step_code_sign(email, otp_code2)
                    resp6_code = resp6.get("code", -1)
                    self._log(f"   → retry code={resp6_code} type={resp6.get('type', '?')}")

            if resp6_code != 0 and resp6.get("type") != "success":
                result["error"] = f"OTP 验证失败: {resp6.get('message', resp6)}"
                return result
            self._delay()

            # 7. 获取 session token
            self._log("Step 7: authenticate (get token)")
            resp7 = self.step_authenticate()
            self._log(f"   → code={resp7.get('code', '?')}")
            self._delay()

            # 8. 设置密码
            self._log("Step 8: set password")
            resp8 = self.step_set_password(password)
            self._log(f"   → code={resp8.get('code', '?')}")

            if not self.token:
                result["error"] = "流程完成但未获取 token"
                return result

            result["ok"] = True
            result["token"] = self.token
            return result

        except Exception as e:
            result["error"] = str(e)
            self._log(f"   [FAIL] 异常: {e}")
            traceback.print_exc()
            return result


# ===================== Upload to rita2api =====================

def upload_to_rita2api(accounts: list[dict]) -> bool:
    url = CFG.get("upload_api_url", "")
    if not url or not accounts:
        return False
    try:
        r = stdlib_requests.post(
            url, json={"accounts": accounts},
            headers={"Content-Type": "application/json"}, timeout=15,
        )
        if r.status_code in (200, 201):
            data = r.json()
            log(f"[OK] 已上传 {data.get('added', len(accounts))} 个账号到 rita2api")
            return True
        log(f"[FAIL] 上传失败: {r.status_code} {r.text[:200]}")
        return False
    except Exception as e:
        log(f"[FAIL] 上传异常: {e}")
        return False


# ===================== Single Task =====================

def _register_one(idx: int, total: int) -> tuple[bool, dict | None, str]:
    tag = f"{idx}/{total}"

    # Random fingerprint for this task
    impersonate, ua, fp_hdrs = _random_fingerprint()

    mail = get_mail_provider(impersonate)
    reg = RitaRegistration(tag=tag)
    password = CFG.get("password", "@qazwsx123456")

    try:
        log(f"[MAIL] 创建临时邮箱...", tag)
        email, mail_ctx = mail.create_email()
        short = email.split("@")[0][:12]
        reg.tag = short
        log(f"[MAIL] {email}", short)

        log(f"[ROCKET] 开始注册", short)
        result = reg.register(email, password, mail, mail_ctx)

        if result["ok"]:
            account = {
                "token": result["token"],
                "name": f"auto-{short}",
            }
            with _file_lock:
                with open(CFG["output_file"], "a", encoding="utf-8") as f:
                    f.write(f"{email}----{password}----token={result['token']}\n")
            log(f"[OK] 注册成功!", short)
            return True, account, ""
        else:
            log(f"[FAIL] 失败: {result['error']}", short)
            return False, None, result["error"]

    except Exception as e:
        log(f"[FAIL] 异常: {e}", tag)
        traceback.print_exc()
        return False, None, str(e)


# ===================== Batch =====================

def run_batch():
    total = CFG.get("total_accounts", 3)
    max_workers = min(CFG.get("max_workers", 2), total)
    upload_enabled = CFG.get("upload_enabled", True)
    proxy = CFG.get("proxy", "")

    print("\n" + "=" * 60)
    print("  Rita.ai 批量自动注册工具")
    print(f"  数量: {total} | 并发: {max_workers}")
    print(f"  邮箱: {CFG.get('mail_provider', 'gptmail')}")
    print(f"  代理: {proxy or '无'}{' (邮箱也走代理)' if proxy and CFG.get('mail_use_proxy') else ''}")
    print(f"  验证码: YesCaptcha ({'[OK]' if CFG.get('yescaptcha_client_key') else '[MISSING]'})")
    print(f"  上传: {'[OK] ' + CFG.get('upload_api_url', '') if upload_enabled else '[SKIP]'}")
    print("=" * 60 + "\n")

    if not CFG.get("yescaptcha_client_key"):
        print("[WARN] 警告: yescaptcha_client_key 未配置，注册将失败!")
        input("按 Enter 继续...")

    success_count = 0
    fail_count = 0
    registered: list[dict] = []
    start = time.time()

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_register_one, i, total): i for i in range(1, total + 1)}
        for future in as_completed(futures):
            try:
                ok, account, err = future.result()
                if ok and account:
                    success_count += 1
                    registered.append(account)
                else:
                    fail_count += 1
            except Exception as e:
                fail_count += 1
                log(f"线程异常: {e}")

    elapsed = time.time() - start
    print("\n" + "=" * 60)
    print(f"  完成! 耗时 {elapsed:.1f}s")
    print(f"  成功: {success_count} | 失败: {fail_count} | 总数: {total}")
    if success_count:
        print(f"  输出: {CFG['output_file']}")
    print("=" * 60)

    if upload_enabled and registered:
        print(f"\n[UPLOAD] 上传 {len(registered)} 个账号到 rita2api...")
        upload_to_rita2api(registered)

    return success_count, fail_count


# ===================== Main =====================

def main():
    print("=" * 60)
    print("  Rita.ai 批量自动注册工具")
    print("=" * 60)

    count_input = input(f"\n注册数量 (默认 {CFG['total_accounts']}): ").strip()
    if count_input.isdigit() and int(count_input) > 0:
        CFG["total_accounts"] = int(count_input)

    workers_input = input(f"并发数 (默认 {CFG['max_workers']}): ").strip()
    if workers_input.isdigit() and int(workers_input) > 0:
        CFG["max_workers"] = int(workers_input)

    if not CFG.get("proxy"):
        proxy_input = input("代理地址 (留空=不使用): ").strip()
        if proxy_input:
            CFG["proxy"] = proxy_input

    run_batch()


if __name__ == "__main__":
    main()

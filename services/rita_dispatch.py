from __future__ import annotations

"""Rita 账号调度层。

统一处理：
- 客户端直传 token 与账号池轮训的分流；
- 成功/失败回写；
- 扣费与 usage 记录。

约束：
- 协议路由层不应直接改账号状态；
- 是否计费、记到哪个账号，由 lease 统一携带。
"""

from dataclasses import dataclass
from typing import Any

from accounts import Account, AccountManager
from database import get_db

JsonDict = dict[str, Any]


@dataclass(slots=True)
class RitaDispatchLease:
    """一次上游请求对应的一份调度租约。"""

    account: Account | None
    headers: JsonDict
    billed_account: Account | None
    used_client_token: bool = False


class NoAvailableAccountError(RuntimeError):
    """当前没有可用 Rita 账号。"""


def _build_client_headers(origin: str, client_token: str, client_visitorid: str = "") -> JsonDict:
    headers: JsonDict = {
        "Content-Type": "application/json",
        "Origin": origin,
        "Referer": origin,
        "token": client_token,
        "Cookie": f"token={client_token}",
    }
    if client_visitorid:
        headers["visitorid"] = client_visitorid
    return headers


def acquire_lease(
    account_manager: AccountManager,
    origin: str,
    *,
    client_token: str = "",
    client_visitorid: str = "",
) -> RitaDispatchLease:
    """获取一次请求的账号租约。

    优先级：
    1. 客户端显式传 token -> 不占用本地账号池，也不做扣费；
    2. 否则从账号池轮训一个可用账号。
    """

    normalized_token = str(client_token or "").strip()
    normalized_visitorid = str(client_visitorid or "").strip()

    if normalized_token:
        return RitaDispatchLease(
            account=None,
            headers=_build_client_headers(origin, normalized_token, normalized_visitorid),
            billed_account=None,
            used_client_token=True,
        )

    account, _ = account_manager.next()
    if not account:
        raise NoAvailableAccountError("no accounts configured")

    return RitaDispatchLease(
        account=account,
        headers=account_manager.upstream_headers(account, origin),
        billed_account=account,
        used_client_token=False,
    )


def mark_success(
    account_manager: AccountManager,
    lease: RitaDispatchLease,
    *,
    model: str = "",
    request_type: str = "unknown",
    tokens_approx: int = 0,
    cost: int = 0,
) -> None:
    """在请求成功结束后回写账号健康、扣费和 usage。"""

    billed = lease.billed_account
    if not billed:
        return

    safe_cost = max(0, int(cost or 0))
    safe_tokens = max(0, int(tokens_approx or 0))

    account_manager.mark_ok(billed)
    if safe_cost > 0:
        account_manager.deduct_quota(billed.id, safe_cost)
    if model:
        try:
            get_db().log_usage(
                billed.id,
                model,
                safe_tokens,
                success=True,
                request_type=request_type,
            )
        except Exception:
            # usage 记录失败不应反过来污染主请求结果
            pass


def mark_failure(
    account_manager: AccountManager,
    lease: RitaDispatchLease,
    error: str = "",
    *,
    model: str = "",
    request_type: str = "unknown",
) -> None:
    """在请求失败时回写账号失败次数。"""

    billed = lease.billed_account
    if not billed:
        return
    account_manager.mark_fail(billed, str(error or ""))
    if model:
        try:
            get_db().log_usage(
                billed.id,
                model,
                0,
                success=False,
                request_type=request_type,
            )
        except Exception:
            # 失败日志落盘失败时也不能污染主链路
            pass


__all__ = [
    "NoAvailableAccountError",
    "RitaDispatchLease",
    "acquire_lease",
    "mark_failure",
    "mark_success",
]

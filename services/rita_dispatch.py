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
    reservation_released: bool = False


class NoAvailableAccountError(RuntimeError):
    """当前没有可用 Rita 账号。"""


_QUOTA_EXHAUSTED_KEYWORDS = (
    "quota",
    "insufficient",
    "credit",
    "balance",
    "积分",
    "点数",
    "余额不足",
    "用量不足",
)


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
    required_quota: int = 0,
    exclude_account_ids: list[str] | set[str] | tuple[str, ...] | None = None,
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

    account = account_manager.reserve_next(
        min_quota=max(0, int(required_quota or 0)),
        exclude_ids=exclude_account_ids,
    )
    if not account:
        raise NoAvailableAccountError("no available account")

    return RitaDispatchLease(
        account=account,
        headers=account_manager.upstream_headers(account, origin),
        billed_account=account,
        used_client_token=False,
    )


def release_lease(account_manager: AccountManager, lease: RitaDispatchLease | None) -> None:
    """释放账号租约。"""
    if not lease or lease.reservation_released or lease.used_client_token:
        return
    account = lease.account
    if not account:
        lease.reservation_released = True
        return
    account_manager.release_reservation(account.id)
    lease.reservation_released = True


def is_quota_exhausted_message(message: str) -> bool:
    """判断错误文案是否表示积分/额度耗尽。"""
    text = str(message or "").strip().lower()
    if not text:
        return False
    return any(keyword in text for keyword in _QUOTA_EXHAUSTED_KEYWORDS)


def disable_quota_exhausted(
    account_manager: AccountManager,
    lease: RitaDispatchLease,
    *,
    error: str = "",
) -> None:
    """把额度耗尽账号软禁用，并释放租约。"""
    billed = lease.billed_account
    if billed:
        account_manager.disable_quota_exhausted(billed.id, error=error)
    release_lease(account_manager, lease)


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
    try:
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
    finally:
        release_lease(account_manager, lease)


def mark_failure(
    account_manager: AccountManager,
    lease: RitaDispatchLease,
    error: str = "",
    *,
    model: str = "",
    request_type: str = "unknown",
) -> None:
    """在请求失败时回写账号失败次数。"""
    try:
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
    finally:
        release_lease(account_manager, lease)


__all__ = [
    "NoAvailableAccountError",
    "RitaDispatchLease",
    "acquire_lease",
    "disable_quota_exhausted",
    "is_quota_exhausted_message",
    "mark_failure",
    "mark_success",
    "release_lease",
]

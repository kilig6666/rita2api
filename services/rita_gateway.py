from __future__ import annotations

"""Rita 上游网关层。

这里只封装和 Rita HTTP/SSE 通讯的最小公共逻辑，避免协议路由里重复：
- 会话创建
- 聊天补全流请求
- 模型/工具列表查询
- SSE 逐事件解析
- SSE 汇总成完整文本
"""

import json
import time
from collections.abc import Generator, Mapping
from typing import Any

import requests

JsonDict = dict[str, Any]


class RitaGateway:
    """对 Rita 上游接口做轻量封装。"""

    def __init__(self, upstream_url: str, *, disable_ssl_verify: bool = False):
        self.upstream_url = str(upstream_url or "").rstrip("/")
        self.disable_ssl_verify = bool(disable_ssl_verify)

    @property
    def verify_ssl(self) -> bool:
        return not self.disable_ssl_verify

    def _post(
        self,
        path: str,
        *,
        headers: Mapping[str, str],
        json_body: JsonDict,
        timeout: int,
        stream: bool = False,
    ) -> requests.Response:
        return requests.post(
            f"{self.upstream_url}{path}",
            headers=dict(headers),
            json=json_body,
            stream=stream,
            timeout=timeout,
            verify=self.verify_ssl,
        )

    def create_conversation(self, headers: Mapping[str, str], model: str, timeout: int = 15) -> int:
        """创建 Rita 会话，失败时抛异常。"""

        response = self._post(
            "/chatgpt/newConversation",
            headers=headers,
            json_body={"model": model},
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") == 0:
            return int(payload.get("data", {}).get("chat_id", 0) or 0)
        raise RuntimeError(payload.get("error") or payload.get("message") or "failed to create conversation")

    def request_completion_stream(
        self,
        headers: Mapping[str, str],
        payload: JsonDict,
        timeout: int = 120,
    ) -> requests.Response:
        """发起 Rita SSE 对话请求。

        这里不主动 `raise_for_status()`，因为上层还要先检查：
        - JSON 错误体
        - Rita 自定义 code/message
        - SSE 正常流
        """

        return self._post(
            "/aichat/completions",
            headers=headers,
            json_body=payload,
            timeout=timeout,
            stream=True,
        )

    def fetch_models(self, headers: Mapping[str, str], timeout: int = 15) -> JsonDict:
        response = self._post(
            "/aichat/categoryModels",
            headers=headers,
            json_body={"language": "zh"},
            timeout=timeout,
        )
        response.raise_for_status()
        return response.json()

    def fetch_tools(self, headers: Mapping[str, str], timeout: int = 15) -> JsonDict:
        response = self._post(
            "/gamsai_api/v1/page_service/aiTools",
            headers=headers,
            json_body={"language": "zh"},
            timeout=timeout,
        )
        response.raise_for_status()
        return response.json()

    def execute_tool(
        self,
        headers: Mapping[str, str],
        tool_id: str,
        payload: JsonDict,
        timeout: int = 60,
    ) -> JsonDict:
        response = self._post(
            f"/gamsai_api/v1/page_service/aiTools/{tool_id}/execute",
            headers=headers,
            json_body=payload,
            timeout=timeout,
        )
        response.raise_for_status()
        return response.json()


def iter_rita_sse(response: requests.Response) -> Generator[JsonDict, None, None]:
    """把 Rita SSE 响应解析为逐个 JSON 事件。

    兼容：
    - `data: {...}`
    - 多行 `data:` 拼接
    - 空行分隔事件
    - `[DONE]`
    """

    response.encoding = response.encoding or "utf-8"
    data_lines: list[str] = []

    def flush_event() -> Generator[JsonDict, None, None]:
        nonlocal data_lines
        if not data_lines:
            return
        payload = "\n".join(data_lines).strip()
        data_lines = []
        if not payload or payload == "[DONE]":
            return
        try:
            yield json.loads(payload)
        except json.JSONDecodeError:
            return

    for raw_line in response.iter_lines(decode_unicode=True):
        line = (raw_line or "").lstrip("\ufeff")
        if line == "":
            yield from flush_event()
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())

    yield from flush_event()


def collect_rita_response(response: requests.Response) -> JsonDict:
    """把 Rita SSE 汇总成完整文本，供非流式接口或 tool 解析复用。"""

    created_ts = int(time.time())
    captured_msg_id: str | None = None
    content_parts: list[str] = []
    final_event: JsonDict | None = None

    with response:
        for event in iter_rita_sse(response):
            event_type = str(event.get("type", ""))
            if event_type in {"quota_remain", "conv_title"}:
                continue
            if event_type == "assistant_complete":
                final_event = event
                break

            event_id = event.get("id", "")
            if not captured_msg_id and isinstance(event_id, str) and event_id.startswith("ai"):
                captured_msg_id = event_id

            created_ts = int(event.get("created", created_ts) or created_ts)
            choices = event.get("choices", []) or []
            if not choices:
                continue
            delta = choices[0].get("delta", {}) or {}
            content = delta.get("content", "")
            if content:
                content_parts.append(str(content))

    return {
        "content": "".join(content_parts),
        "created": created_ts,
        "message_id": captured_msg_id,
        "final_event": final_event,
    }


__all__ = [
    "RitaGateway",
    "collect_rita_response",
    "iter_rita_sse",
]

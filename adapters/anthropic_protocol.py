from __future__ import annotations

"""Anthropic 协议适配辅助。

目标不是伪造完整 Claude 后端，而是把当前 Rita 文本上游
尽量包装成 Anthropic SDK 可消费的请求/响应形态：
- Messages -> OpenAI chat 风格消息
- tool_use / tool_result <-> tool_calls
- count_tokens 估算
- 非流式/流式响应骨架
"""

import json
import time
from typing import Any, Iterable

from .openai_protocol import extract_text, parse_tool_response, split_embedded_thinking, split_text_chunks

JsonDict = dict[str, Any]
JsonValue = Any


def _extract_system_text(system: JsonValue) -> str:
    """Anthropic 的 system 可以是字符串或文本块数组。"""

    if isinstance(system, str):
        return system
    if isinstance(system, list):
        parts: list[str] = []
        for block in system:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            elif block is not None:
                parts.append(extract_text(block))
        return "".join(parts)
    return ""


def anthropic_tools_to_openai(tools: list[JsonDict] | None) -> list[JsonDict]:
    """把 Anthropic tools 转成 OpenAI function tools。"""

    result: list[JsonDict] = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        input_schema = tool.get("input_schema")
        if not isinstance(input_schema, dict):
            input_schema = {"type": "object", "properties": {}}
        result.append(
            {
                "type": "function",
                "function": {
                    "name": str(tool.get("name", "")),
                    "description": str(tool.get("description", "")),
                    "parameters": input_schema,
                },
            }
        )
    return result


def anthropic_tool_choice_to_openai(tool_choice: JsonValue) -> JsonValue:
    """把 Anthropic `tool_choice` 转成 OpenAI 常见写法。"""

    if isinstance(tool_choice, str):
        if tool_choice in {"auto", "none", "required"}:
            return tool_choice
        return None

    if not isinstance(tool_choice, dict):
        return None

    choice_type = str(tool_choice.get("type", "")).strip()
    if choice_type == "auto":
        return "auto"
    if choice_type == "any":
        return "required"
    if choice_type == "tool":
        return {
            "type": "function",
            "function": {"name": str(tool_choice.get("name", ""))},
        }
    if choice_type == "none":
        return "none"
    return None


def anthropic_messages_to_openai_chat(body: JsonDict) -> JsonDict:
    """把 Anthropic `/v1/messages` 请求大致映射为 OpenAI chat 请求。"""

    messages: list[JsonDict] = []
    system_text = _extract_system_text(body.get("system"))
    if system_text:
        messages.append({"role": "system", "content": system_text})

    for msg in body.get("messages", []) or []:
        if not isinstance(msg, dict):
            continue

        role = str(msg.get("role", "user"))
        content = msg.get("content", "")

        if isinstance(content, str):
            messages.append({"role": role, "content": content})
            continue

        if not isinstance(content, list):
            messages.append({"role": role, "content": extract_text(content)})
            continue

        if role == "user":
            user_parts: list[JsonValue] = []
            for block in content:
                if not isinstance(block, dict):
                    user_parts.append({"type": "text", "text": str(block)})
                    continue

                block_type = str(block.get("type", ""))
                if block_type == "tool_result":
                    tool_content = block.get("content", "")
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": str(block.get("tool_use_id", "")),
                            "content": extract_text(tool_content),
                        }
                    )
                    continue

                if block_type == "image":
                    user_parts.append({"type": "image_url", "image_url": "anthropic://image"})
                    continue

                if block_type == "text":
                    user_parts.append({"type": "text", "text": str(block.get("text", ""))})
                    continue

                user_parts.append({"type": "text", "text": extract_text(block)})

            if user_parts:
                simplified = user_parts[0]["text"] if len(user_parts) == 1 and user_parts[0].get("type") == "text" else user_parts
                messages.append({"role": "user", "content": simplified})
            continue

        if role == "assistant":
            assistant_parts: list[str] = []
            tool_calls: list[JsonDict] = []
            for index, block in enumerate(content):
                if not isinstance(block, dict):
                    assistant_parts.append(str(block))
                    continue

                block_type = str(block.get("type", ""))
                if block_type == "tool_use":
                    tool_calls.append(
                        {
                            "id": str(block.get("id") or f"call_{index}"),
                            "type": "function",
                            "function": {
                                "name": str(block.get("name", "")),
                                "arguments": json.dumps(block.get("input", {}), ensure_ascii=False),
                            },
                        }
                    )
                    continue

                if block_type == "thinking":
                    thinking = str(block.get("thinking", ""))
                    if thinking:
                        assistant_parts.append(f"<thinking>{thinking}</thinking>")
                    continue

                if block_type == "text":
                    assistant_parts.append(str(block.get("text", "")))
                    continue

                assistant_parts.append(extract_text(block))

            assistant_message: JsonDict = {
                "role": "assistant",
                "content": "".join(assistant_parts) or None,
            }
            if tool_calls:
                assistant_message["tool_calls"] = tool_calls
            messages.append(assistant_message)
            continue

        messages.append({"role": role, "content": extract_text(content)})

    converted: JsonDict = {
        "messages": messages,
        "tools": anthropic_tools_to_openai(body.get("tools")),
        "tool_choice": anthropic_tool_choice_to_openai(body.get("tool_choice")),
        "max_tokens": body.get("max_tokens"),
        "stream": bool(body.get("stream", False)),
    }

    # 这些字段当前 server.py 还没全部消费，但保留给后续协议层扩展。
    for field in ("temperature", "top_p", "top_k", "metadata", "stop_sequences"):
        if field in body:
            converted[field] = body.get(field)

    return converted


def estimate_anthropic_tokens(body: JsonDict) -> JsonDict:
    """本地近似估算 token。

    Rita 当前没有暴露等价 token count 接口，因此这里只做稳定近似：
    - 文本按 4 chars ≈ 1 token
    - 每张图补一个固定开销，避免返回 0
    """

    total_chars = 0
    total_messages = 0
    total_images = 0

    total_chars += len(_extract_system_text(body.get("system")))

    for msg in body.get("messages", []) or []:
        if not isinstance(msg, dict):
            continue
        total_messages += 1
        content = msg.get("content", "")
        if isinstance(content, str):
            total_chars += len(content)
            continue
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    total_chars += len(str(block))
                    continue
                if block.get("type") == "image":
                    total_images += 1
                    total_chars += 256
                else:
                    total_chars += len(extract_text(block))
            continue
        total_chars += len(extract_text(content))

    input_tokens = max(1, total_chars // 4)
    return {
        "input_tokens": input_tokens,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "message_count": total_messages,
        "image_count": total_images,
        "estimated": True,
    }


def build_anthropic_message_response(
    model: str,
    text: str,
    *,
    tool_calls: list[JsonDict] | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    message_id: str | None = None,
) -> JsonDict:
    """构造 Anthropic 非流式 message 响应。"""

    visible_text, thinking_parts = split_embedded_thinking(text)
    content: list[JsonDict] = []
    for thinking in thinking_parts:
        content.append({"type": "thinking", "thinking": thinking})
    if visible_text:
        content.append({"type": "text", "text": visible_text})

    for index, call in enumerate(tool_calls or []):
        function_data = call.get("function", {}) if isinstance(call, dict) else {}
        try:
            parsed_args = json.loads(function_data.get("arguments", "{}") or "{}")
        except Exception:
            parsed_args = {}
        content.append(
            {
                "type": "tool_use",
                "id": str(call.get("id") or f"call_{index}"),
                "name": str(function_data.get("name", "")),
                "input": parsed_args,
            }
        )

    return {
        "id": message_id or f"msg_{int(time.time() * 1000)}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": "tool_use" if tool_calls else "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": max(0, int(input_tokens or 0)),
            "output_tokens": max(0, int(output_tokens or 0)),
        },
    }


def build_anthropic_stream_events(
    model: str,
    text: str,
    *,
    tool_calls: list[JsonDict] | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    message_id: str | None = None,
) -> Iterable[str]:
    """构造 Anthropic SSE 事件序列。"""

    message_id = message_id or f"msg_{int(time.time() * 1000)}"
    visible_text, thinking_parts = split_embedded_thinking(text)
    yield (
        "event: message_start\n"
        f"data: {json.dumps({'type': 'message_start', 'message': {'id': message_id, 'type': 'message', 'role': 'assistant', 'content': [], 'model': model, 'stop_reason': None, 'stop_sequence': None, 'usage': {'input_tokens': max(0, int(input_tokens or 0)), 'output_tokens': 0}}}, ensure_ascii=False)}\n\n"
    )

    if tool_calls:
        for index, call in enumerate(tool_calls):
            function_data = call.get("function", {}) if isinstance(call, dict) else {}
            yield (
                "event: content_block_start\n"
                f"data: {json.dumps({'type': 'content_block_start', 'index': index, 'content_block': {'type': 'tool_use', 'id': str(call.get('id') or f'call_{index}'), 'name': str(function_data.get('name', '')), 'input': {}}}, ensure_ascii=False)}\n\n"
            )
            yield (
                "event: content_block_delta\n"
                f"data: {json.dumps({'type': 'content_block_delta', 'index': index, 'delta': {'type': 'input_json_delta', 'partial_json': function_data.get('arguments', '{}') or '{}'}}, ensure_ascii=False)}\n\n"
            )
            yield (
                "event: content_block_stop\n"
                f"data: {json.dumps({'type': 'content_block_stop', 'index': index}, ensure_ascii=False)}\n\n"
            )
        yield (
            "event: message_delta\n"
            f"data: {json.dumps({'type': 'message_delta', 'delta': {'stop_reason': 'tool_use', 'stop_sequence': None}, 'usage': {'output_tokens': max(0, int(output_tokens or 0))}}, ensure_ascii=False)}\n\n"
        )
        yield f"event: message_stop\ndata: {json.dumps({'type': 'message_stop'}, ensure_ascii=False)}\n\n"
        return

    block_index = 0
    for thinking in thinking_parts:
        yield (
            "event: content_block_start\n"
            f"data: {json.dumps({'type': 'content_block_start', 'index': block_index, 'content_block': {'type': 'thinking', 'thinking': ''}}, ensure_ascii=False)}\n\n"
        )
        for chunk in split_text_chunks(thinking, 80):
            yield (
                "event: content_block_delta\n"
                f"data: {json.dumps({'type': 'content_block_delta', 'index': block_index, 'delta': {'type': 'thinking_delta', 'thinking': chunk}}, ensure_ascii=False)}\n\n"
            )
        yield (
            "event: content_block_stop\n"
            f"data: {json.dumps({'type': 'content_block_stop', 'index': block_index}, ensure_ascii=False)}\n\n"
        )
        block_index += 1

    yield (
        "event: content_block_start\n"
        f"data: {json.dumps({'type': 'content_block_start', 'index': block_index, 'content_block': {'type': 'text', 'text': ''}}, ensure_ascii=False)}\n\n"
    )
    for chunk in split_text_chunks(visible_text or "", 80):
        yield (
            "event: content_block_delta\n"
            f"data: {json.dumps({'type': 'content_block_delta', 'index': block_index, 'delta': {'type': 'text_delta', 'text': chunk}}, ensure_ascii=False)}\n\n"
        )
    yield (
        "event: content_block_stop\n"
        f"data: {json.dumps({'type': 'content_block_stop', 'index': block_index}, ensure_ascii=False)}\n\n"
    )
    yield (
        "event: message_delta\n"
        f"data: {json.dumps({'type': 'message_delta', 'delta': {'stop_reason': 'end_turn', 'stop_sequence': None}, 'usage': {'output_tokens': max(0, int(output_tokens or 0))}}, ensure_ascii=False)}\n\n"
    )
    yield f"event: message_stop\ndata: {json.dumps({'type': 'message_stop'}, ensure_ascii=False)}\n\n"


def parse_tool_calls_from_text(text: str) -> tuple[str, list[JsonDict]]:
    """从 Rita 文本输出里抽取 Anthropic `tool_use` 对应的调用列表。"""

    parsed = parse_tool_response(text or "")
    if parsed.get("type") == "tool_calls":
        tool_calls: list[JsonDict] = []
        for index, call in enumerate(parsed.get("calls", [])):
            tool_calls.append(
                {
                    "id": f"call_{index}",
                    "type": "function",
                    "function": {
                        "name": str(call.get("name", "")),
                        "arguments": json.dumps(call.get("input", {}), ensure_ascii=False),
                    },
                }
            )
        return "", tool_calls
    return str(parsed.get("text", text or "")), []


__all__ = [
    "anthropic_messages_to_openai_chat",
    "anthropic_tool_choice_to_openai",
    "anthropic_tools_to_openai",
    "build_anthropic_message_response",
    "build_anthropic_stream_events",
    "estimate_anthropic_tokens",
    "parse_tool_calls_from_text",
]

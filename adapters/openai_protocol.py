from __future__ import annotations

"""OpenAI 协议适配辅助。

这里不直接依赖 Flask / requests，只负责：
1. 从 OpenAI/Responses 风格消息中抽文本或图片占位；
2. 把工具声明压缩成提示词，给当前 Rita 文本上游兜底；
3. 从模型文本输出中尽量稳健地解析 tool call JSON；
4. 生成 Responses API 需要的基础结构。
"""

import hashlib
import json
import re
from typing import Any, Iterable, Sequence

JsonDict = dict[str, Any]
JsonValue = Any

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
_CODE_FENCE_RE = re.compile(r"^```(?:json|JSON)?\s*|\s*```$", re.DOTALL)
_THINKING_TAG_RE = re.compile(r"<thinking>(.*?)</thinking>", re.DOTALL | re.IGNORECASE)


def extract_text(content: JsonValue) -> str:
    """把多协议 content 折叠为 Rita 可接受的纯文本。

    当前上游还不支持原生多模态/tool role，因此这里保守地：
    - 文本块保留原文；
    - 图片块统一输出 `[image]` 占位；
    - 工具结果递归提取其文本内容；
    - 其它结构退化为 JSON 字符串，尽量别丢信息。
    """

    if isinstance(content, str):
        return content

    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            parts.append(extract_text(block))
        return "".join(part for part in parts if part)

    if isinstance(content, dict):
        block_type = str(content.get("type", ""))
        if block_type in {"text", "input_text", "output_text"}:
            return str(content.get("text", ""))
        if block_type in {"image", "input_image", "image_url", "image_file"}:
            return "[image]"
        if block_type == "tool_result":
            return extract_text(content.get("content", ""))
        if block_type == "tool_use":
            tool_name = str(content.get("name", "")).strip()
            tool_input = content.get("input", {})
            tool_args = json.dumps(tool_input, ensure_ascii=False)
            return f"<tool_use name=\"{tool_name}\">{tool_args}</tool_use>"
        if "text" in content:
            return str(content.get("text", ""))
        if "content" in content:
            return extract_text(content.get("content", ""))
        return json.dumps(content, ensure_ascii=False)

    if content is None:
        return ""
    return str(content)


def _tools_hash(tools: Sequence[JsonDict]) -> str:
    """给工具列表做稳定 hash，便于上层缓存压缩后的 prompt。"""

    key = json.dumps(
        [
            tool.get("name") or (tool.get("function") or {}).get("name", "")
            for tool in tools
            if isinstance(tool, dict)
        ],
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.md5(key.encode("utf-8")).hexdigest()[:12]


def _compact_tools(tools: Sequence[JsonDict]) -> str:
    """把 OpenAI/Anthropic 风格工具声明压成短文本，降低 prompt 污染。"""

    parts: list[str] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue

        if tool.get("type") == "function":
            fn = tool.get("function", {}) or {}
            name = str(fn.get("name", "")).strip()
            schema = fn.get("parameters", {}) or {}
        elif "input_schema" in tool:
            name = str(tool.get("name", "")).strip()
            schema = tool.get("input_schema", {}) or {}
        else:
            continue

        props = schema.get("properties", {}) if isinstance(schema, dict) else {}
        req = set(schema.get("required", []) if isinstance(schema, dict) else [])
        params = [f"{prop}{'*' if prop in req else '?'}" for prop in props]
        parts.append(f"{name}({','.join(params)})" if params else name)

    return " | ".join(part for part in parts if part)


def inject_tool_prompt(
    user_text: str,
    tools: Sequence[JsonDict],
    cache: dict[str, str] | None = None,
) -> str:
    """把工具定义注入到最后一条用户消息，维持当前 Rita 上游可工作。"""

    cache = cache if cache is not None else {}
    tool_hash = _tools_hash(tools)
    if tool_hash not in cache:
        cache[tool_hash] = _compact_tools(tools)
    return _TOOL_PROMPT_TMPL.format(
        tool_defs=cache[tool_hash],
        query=str(user_text or ""),
    )


def _strip_code_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        return _CODE_FENCE_RE.sub("", stripped).strip()
    return stripped


def _iter_json_candidates(text: str) -> Iterable[JsonValue]:
    """在任意文本中尽量提取可解析的 JSON 片段。

    这里不用贪婪正则，而是用 `raw_decode` 从每个 `{`/`[` 起点尝试，
    避免把整段自然语言里的多个花括号一把吞掉。
    """

    decoder = json.JSONDecoder()
    cleaned = _strip_code_fence(text)

    if not cleaned:
        return []

    try:
        return [decoder.decode(cleaned)]
    except json.JSONDecodeError:
        pass

    candidates: list[JsonValue] = []
    for index, char in enumerate(cleaned):
        if char not in "[{":
            continue
        try:
            obj, _end = decoder.raw_decode(cleaned[index:])
        except json.JSONDecodeError:
            continue
        candidates.append(obj)
    return candidates


def _coerce_tool_calls(obj: JsonValue) -> list[JsonDict]:
    """把不同 JSON 形态规范成统一 tool call 列表。"""

    if isinstance(obj, dict):
        if "tool" in obj:
            tool_name = obj.get("tool") or obj.get("name")
            tool_args = obj.get("args") or obj.get("parameters") or obj.get("input") or {}
            if tool_name:
                return [{"name": str(tool_name), "input": tool_args}]

        calls_raw = obj.get("calls")
        if isinstance(calls_raw, list):
            result: list[JsonDict] = []
            for call in calls_raw:
                if not isinstance(call, dict):
                    continue
                tool_name = call.get("tool") or call.get("name")
                tool_args = call.get("args") or call.get("parameters") or call.get("input") or {}
                if tool_name:
                    result.append({"name": str(tool_name), "input": tool_args})
            return result

    if isinstance(obj, list):
        result = []
        for item in obj:
            if not isinstance(item, dict):
                continue
            tool_name = item.get("tool") or item.get("name")
            tool_args = item.get("args") or item.get("parameters") or item.get("input") or {}
            if tool_name:
                result.append({"name": str(tool_name), "input": tool_args})
        return result

    return []


def parse_tool_response(raw: JsonValue) -> JsonDict:
    """从模型文本中尽量稳健地抽出 tool call JSON。

    返回两种形态：
    - `{"type": "tool_calls", "calls": [...]}`
    - `{"type": "text", "text": "..."}`
    """

    if not isinstance(raw, str):
        return {"type": "text", "text": extract_text(raw)}

    cleaned = raw.strip()
    for candidate in _iter_json_candidates(cleaned):
        tool_calls = _coerce_tool_calls(candidate)
        if tool_calls:
            return {"type": "tool_calls", "calls": tool_calls}

    return {"type": "text", "text": raw}


def split_embedded_thinking(raw: JsonValue) -> tuple[str, list[str]]:
    """提取 `<thinking>...</thinking>` 片段，并返回去标签后的正文。

    约定：
    - thinking 片段按出现顺序返回；
    - 正文会移除 thinking 标签并做轻量空白收口；
    - 非字符串输入先走 `extract_text`。
    """

    text = raw if isinstance(raw, str) else extract_text(raw)
    if not text:
        return "", []

    thoughts = [match.strip() for match in _THINKING_TAG_RE.findall(text) if match and match.strip()]
    visible = _THINKING_TAG_RE.sub("", text)
    visible = re.sub(r"\n{3,}", "\n\n", visible).strip()
    return visible, thoughts


def build_rita_messages(messages: Sequence[JsonDict]) -> list[JsonDict]:
    """把 OpenAI 风格消息转换为 Rita `messages`。

    约束：
    - Rita 当前只接受 `{type: text, text: ...}`；
    - system / developer 合并到首条消息前缀；
    - assistant 的 tool_calls 以 XML-like 标签写回，便于后续继续对话；
    - tool 角色转成 `<tool_result ...>`，避免完全丢失工具结果。
    """

    rita_messages: list[JsonDict] = []
    system_parts: list[str] = []

    for msg in messages:
        if not isinstance(msg, dict):
            continue

        role = str(msg.get("role", "")).strip()
        content = msg.get("content", "")

        if role in {"system", "developer"}:
            system_text = extract_text(content)
            if system_text:
                system_parts.append(system_text)
            continue

        if role == "tool":
            tool_id = str(msg.get("tool_call_id", "")).strip()
            tool_name = str(msg.get("name", "")).strip()
            tool_text = extract_text(content)
            attrs = []
            if tool_id:
                attrs.append(f'id="{tool_id}"')
            if tool_name:
                attrs.append(f'name="{tool_name}"')
            attr_text = " " + " ".join(attrs) if attrs else ""
            rita_messages.append(
                {
                    "type": "text",
                    "text": f"<tool_result{attr_text}>\n{tool_text}\n</tool_result>",
                }
            )
            continue

        if role == "assistant":
            blocks: list[str] = []
            assistant_text = extract_text(content)
            if assistant_text:
                blocks.append(assistant_text)

            for tool_call in msg.get("tool_calls", []) or []:
                if not isinstance(tool_call, dict):
                    continue
                function_data = tool_call.get("function", {}) or {}
                tool_name = str(function_data.get("name", "")).strip()
                tool_args = function_data.get("arguments", "{}")
                if not isinstance(tool_args, str):
                    tool_args = json.dumps(tool_args, ensure_ascii=False)
                call_id = str(tool_call.get("id", "")).strip()
                attrs = [f'name="{tool_name}"'] if tool_name else []
                if call_id:
                    attrs.append(f'id="{call_id}"')
                blocks.append(
                    f"<assistant_tool_call {' '.join(attrs)}>{tool_args}</assistant_tool_call>"
                )

            if blocks:
                rita_messages.append({"type": "text", "text": "\n".join(blocks)})
            continue

        if role == "user":
            user_text = extract_text(content)
            if user_text:
                rita_messages.append({"type": "text", "text": user_text})

    if system_parts:
        system_prefix = "<system>\n" + "\n".join(system_parts) + "\n</system>"
        if rita_messages:
            rita_messages[0]["text"] = system_prefix + "\n\n" + str(rita_messages[0].get("text", ""))
        else:
            rita_messages.append({"type": "text", "text": system_prefix})

    return rita_messages


def _stringify_json_value(value: JsonValue) -> str:
    """把任意 JSON-ish 值稳定转成字符串。"""

    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


_TEXT_PART_TYPES = {"input_text", "output_text", "text"}
_IMAGE_PART_TYPES = {"input_image", "image", "image_url", "image_file"}



def _normalize_response_image_value(part: JsonDict) -> JsonValue:
    """把不同图片字段压成 Chat Completions 可接受的 `image_url` 值。"""

    image_value = part.get("image_url")
    if isinstance(image_value, dict):
        return image_value.get("url") or image_value.get("uri") or image_value
    if image_value:
        return image_value

    if part.get("type") == "image_file":
        file_id = part.get("file_id") or part.get("id") or ""
        return f"file://{file_id}" if file_id else ""

    return part.get("url") or part.get("uri") or ""



def _normalize_responses_content(content: JsonValue) -> JsonValue:
    """把 Responses API content 规范成更接近 Chat Completions 的结构。"""

    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return extract_text(content)

    normalized: list[JsonDict] = []
    for part in content:
        if isinstance(part, str):
            normalized.append({"type": "text", "text": part})
            continue
        if not isinstance(part, dict):
            normalized.append({"type": "text", "text": str(part)})
            continue

        part_type = str(part.get("type", ""))
        if part_type in _TEXT_PART_TYPES:
            normalized.append({"type": "text", "text": str(part.get("text", ""))})
        elif part_type in _IMAGE_PART_TYPES:
            normalized.append(
                {
                    "type": "image_url",
                    "image_url": _normalize_response_image_value(part),
                }
            )
        elif part_type == "function_call_output":
            normalized.append(
                {
                    "type": "text",
                    "text": _normalize_tool_output_content(
                        part.get("output", part.get("content", ""))
                    ),
                }
            )
        elif part_type == "function_call":
            normalized.append(
                {
                    "type": "text",
                    "text": _stringify_json_value(
                        {
                            "type": "function_call",
                            "name": part.get("name", ""),
                            "call_id": part.get("call_id") or part.get("id", ""),
                            "arguments": part.get("arguments", {}),
                        }
                    ),
                }
            )
        else:
            normalized.append({"type": "text", "text": extract_text(part)})

    if len(normalized) == 1 and normalized[0].get("type") == "text":
        return normalized[0].get("text", "")
    return normalized



def _normalize_tool_output_content(output: JsonValue) -> str:
    """把 Responses tool output 规范成 Chat tool message 的 content。"""

    if isinstance(output, str):
        return output
    if isinstance(output, (list, tuple)):
        return extract_text(list(output))
    if isinstance(output, dict):
        if output.get("type") in _TEXT_PART_TYPES | _IMAGE_PART_TYPES or "content" in output or "text" in output:
            return extract_text(output)
        return json.dumps(output, ensure_ascii=False)
    if output is None:
        return ""
    return str(output)



def _build_response_tool_call(item: JsonDict, fallback_index: int = 0) -> JsonDict:
    """把 Responses `function_call` 项规范成 Chat Completions `tool_calls`。"""

    call_id = str(item.get("call_id", "") or item.get("id", "") or f"call_{fallback_index}")
    arguments = item.get("arguments")
    if arguments in (None, ""):
        arguments = {}
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments, ensure_ascii=False)
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": str(item.get("name", "")),
            "arguments": arguments,
        },
    }



def _normalize_assistant_responses_content(content: JsonValue) -> tuple[JsonValue, list[JsonDict]]:
    """把 assistant content array 里的文本/图片与 function_call 分拆。"""

    if not isinstance(content, list):
        return _normalize_responses_content(content), []

    visible_parts: list[JsonValue] = []
    tool_calls: list[JsonDict] = []
    for index, part in enumerate(content):
        if isinstance(part, dict) and str(part.get("type", "")) == "function_call":
            tool_calls.append(_build_response_tool_call(part, fallback_index=index))
            continue
        visible_parts.append(part)

    normalized_content = _normalize_responses_content(visible_parts)
    if normalized_content == "" and tool_calls:
        normalized_content = None
    return normalized_content, tool_calls



def responses_input_to_messages(data: JsonDict) -> list[JsonDict]:
    """把 `/v1/responses` 的 input 规范成 Chat Completions 风格消息。"""

    raw_input = data.get("input", "")
    instructions = data.get("instructions", "")
    messages: list[JsonDict] = []

    if instructions:
        messages.append({"role": "system", "content": instructions})

    if isinstance(raw_input, str):
        if raw_input:
            messages.append({"role": "user", "content": raw_input})
        return messages

    if not isinstance(raw_input, list):
        return messages

    for index, item in enumerate(raw_input):
        if isinstance(item, str):
            messages.append({"role": "user", "content": item})
            continue
        if not isinstance(item, dict):
            continue

        item_type = str(item.get("type", ""))
        role = str(item.get("role", "user"))

        if item_type == "function_call_output":
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": str(item.get("tool_call_id", item.get("call_id", "") or item.get("id", ""))),
                    "content": _normalize_tool_output_content(
                        item.get("output", item.get("content", ""))
                    ),
                }
            )
            continue

        if item_type == "function_call":
            messages.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [_build_response_tool_call(item, fallback_index=index)],
                }
            )
            continue

        if role == "developer":
            role = "system"

        if role not in {"user", "assistant", "system", "tool"}:
            continue

        if role == "assistant":
            normalized_content, tool_calls = _normalize_assistant_responses_content(
                item.get("content", "")
            )
            message: JsonDict = {"role": role, "content": normalized_content}
            if tool_calls:
                message["tool_calls"] = tool_calls
            messages.append(message)
            continue

        normalized_content = _normalize_responses_content(item.get("content", item.get("output", "")))
        message = {"role": role, "content": normalized_content}
        if role == "tool":
            message["tool_call_id"] = str(
                item.get("tool_call_id", item.get("call_id", "") or item.get("id", ""))
            )
        messages.append(message)

    return messages


def make_responses_base(
    resp_id: str,
    model: str,
    created: float,
    instructions: JsonValue = None,
    status: str = "completed",
    request_options: JsonDict | None = None,
) -> JsonDict:
    """生成 Responses API 共用骨架。"""

    options = request_options or {}
    max_output_tokens = options.get("max_output_tokens")
    if max_output_tokens is None:
        max_output_tokens = options.get("max_tokens")

    return {
        "id": resp_id,
        "object": "response",
        "created_at": created,
        "status": status,
        "model": model,
        "output": [],
        "parallel_tool_calls": bool(options.get("parallel_tool_calls", True)),
        "tool_choice": options.get("tool_choice", "auto"),
        "tools": [],
        "temperature": options.get("temperature", 1.0),
        "top_p": options.get("top_p", 1.0),
        "max_output_tokens": max_output_tokens,
        "truncation": options.get("truncation", "disabled"),
        "instructions": instructions,
        "metadata": options.get("metadata", {}) or {},
        "incomplete_details": None,
        "error": None,
        "usage": None,
    }


def split_text_chunks(text: str, chunk_size: int = 80) -> list[str]:
    """把文本按固定长度切块，供 SSE delta 循环输出。"""

    safe_text = str(text or "")
    safe_chunk_size = max(1, int(chunk_size or 80))
    if not safe_text:
        return []
    return [safe_text[i : i + safe_chunk_size] for i in range(0, len(safe_text), safe_chunk_size)]


__all__ = [
    "build_rita_messages",
    "extract_text",
    "inject_tool_prompt",
    "make_responses_base",
    "parse_tool_response",
    "responses_input_to_messages",
    "split_embedded_thinking",
    "split_text_chunks",
]

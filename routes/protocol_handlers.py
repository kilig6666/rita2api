from __future__ import annotations

import json
import time
from typing import Any

import requests
from flask import Response, jsonify


JsonDict = dict[str, Any]


def _responses_usage(messages: list, text: str = "") -> dict:
    input_tokens = max(0, sum(len(str(m.get("content", ""))) for m in messages) // 4)
    output_tokens = max(0, len(text or "") // 4)
    return {
        "input_tokens": input_tokens,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": output_tokens,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": input_tokens + output_tokens,
    }


def _responses_function_call_items(parsed_calls: list[dict]) -> list[dict]:
    items = []
    for index, call in enumerate(parsed_calls or []):
        items.append({
            "id": f"fc_{int(time.time() * 1000)}_{index}",
            "type": "function_call",
            "status": "completed",
            "call_id": f"call_{index}",
            "name": call.get("name", ""),
            "arguments": json.dumps(call.get("input", {}), ensure_ascii=False),
        })
    return items


def _chat_tokens(messages: list, text: str = "") -> int:
    return max(0, sum(len(str(msg.get("content", ""))) for msg in messages) + len(text or "")) // 4


def handle_chat_completions_api(request, deps: JsonDict):
    data = request.json or {}
    messages = data.get("messages", [])
    model = data.get("model", "gpt-4o")
    stream = bool(data.get("stream", False))
    client_tools = data.get("tools", []) or []
    request_type = "chat_completions"

    deps["log"](
        f"📥 /v1/chat/completions model={model} stream={stream} msgs={len(messages)} tools={len(client_tools)}",
        "INFO",
    )
    deps["increment_stats"](model, messages)

    if not messages:
        return jsonify({"error": {"message": "messages is required", "type": "invalid_request_error"}}), 400

    client_token = request.headers.get("token", "")
    client_visitorid = request.headers.get("visitorid", "")

    try:
        lease = deps["acquire_lease"](
            deps["acm"],
            deps["RITA_ORIGIN"],
            client_token=client_token,
            client_visitorid=client_visitorid,
        )
    except deps["NoAvailableAccountError"]:
        return jsonify({"error": {"message": "no accounts configured", "type": "config_error"}}), 500

    try:
        rita_model = deps["resolve_model"](model, lease.headers)
        chat_id, parent = deps["get_or_create_conversation"](messages)
        rita_messages = deps["build_rita_messages"](messages)

        if client_tools and rita_messages:
            last_text = rita_messages[-1]["text"]
            rita_messages[-1]["text"] = deps["inject_tool_prompt"](last_text, client_tools, deps["tool_prompt_cache"])
            deps["log"](f"🔧 tool prompt injected ({len(client_tools)} tools)", "DEBUG")

        chat_id = deps["ensure_conversation"](lease.headers, rita_model, chat_id)
        payload = {
            "model": rita_model,
            "messages": rita_messages,
            "online": 0,
            "model_type_id": 0,
            "chat_id": chat_id,
            "parent": parent,
        }

        resp = deps["rita_gateway"].request_completion_stream(lease.headers, payload)

        if resp.status_code >= 500:
            error_text = resp.text[:200]
            deps["mark_failure"](
                deps["acm"],
                lease,
                error_text,
                model=model,
                request_type=request_type,
            )
            return jsonify({"error": {"message": "upstream error", "type": "upstream_error"}}), 502

        ct = resp.headers.get("content-type", "")
        if "application/json" in ct:
            try:
                err_body = resp.json()
                err_code = err_body.get("code", 0)
                if err_code and err_code != 0:
                    err_msg = err_body.get("error") or err_body.get("message") or "upstream error"
                    if deps["should_reset_conversation"](err_msg):
                        chat_id = deps["ensure_conversation"](lease.headers, rita_model, 0)
                        payload["chat_id"] = chat_id
                        payload["parent"] = "0"
                        resp = deps["rita_gateway"].request_completion_stream(lease.headers, payload)
                        ct = resp.headers.get("content-type", "")
                        if "application/json" in ct:
                            retry_err = resp.json()
                            retry_code = retry_err.get("code", 0)
                            if retry_code and retry_code != 0:
                                retry_msg = retry_err.get("error") or retry_err.get("message") or err_msg
                                deps["log"](
                                    f"⚠️ Upstream JSON error after conversation reset: code={retry_code} msg={retry_msg}",
                                    "WARNING",
                                )
                                deps["mark_failure"](
                                    deps["acm"],
                                    lease,
                                    str(retry_msg),
                                    model=model,
                                    request_type=request_type,
                                )
                                return jsonify({"error": {"message": str(retry_msg), "type": "upstream_error"}}), 502
                    else:
                        deps["log"](f"⚠️ Upstream JSON error: code={err_code} msg={err_msg}", "WARNING")
                        deps["mark_failure"](
                            deps["acm"],
                            lease,
                            str(err_msg),
                            model=model,
                            request_type=request_type,
                        )
                        return jsonify({"error": {"message": str(err_msg), "type": "upstream_error"}}), 502
            except Exception:
                pass

        resp.raise_for_status()
        response_headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

        if stream and client_tools:
            collected = deps["collect_rita_response"](resp)
            if collected.get("message_id"):
                deps["update_conversation_state"](messages, collected["message_id"], chat_id)
            parsed = deps["parse_tool_response"](collected.get("content", ""))
            created_ts = int(collected.get("created") or time.time())
            cid = f"chatcmpl-{int(time.time() * 1000)}"

            def gen_tool_buffered():
                if parsed.get("type") == "tool_calls":
                    init_chunk = {
                        "id": cid,
                        "object": "chat.completion.chunk",
                        "created": created_ts,
                        "model": model,
                        "choices": [{"index": 0, "delta": {"role": "assistant", "content": None}, "finish_reason": None}],
                    }
                    yield f"data: {json.dumps(init_chunk)}\n\n"
                    for index, call in enumerate(parsed.get("calls", [])):
                        chunk = {
                            "id": cid,
                            "object": "chat.completion.chunk",
                            "created": created_ts,
                            "model": model,
                            "choices": [{
                                "index": 0,
                                "delta": {
                                    "tool_calls": [{
                                        "index": index,
                                        "id": f"call_{index}",
                                        "type": "function",
                                        "function": {
                                            "name": call.get("name", ""),
                                            "arguments": json.dumps(call.get("input", {}), ensure_ascii=False),
                                        },
                                    }],
                                },
                                "finish_reason": None,
                            }],
                        }
                        yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                    deps["mark_success"](
                        deps["acm"],
                        lease,
                        model=model,
                        request_type=request_type,
                        tokens_approx=_chat_tokens(messages, collected.get("content", "")),
                        cost=deps["get_cost"](rita_model),
                    )
                    final_chunk = {
                        "id": cid,
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": model,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                    }
                    yield f"data: {json.dumps(final_chunk)}\n\n"
                    yield "data: [DONE]\n\n"
                    return

                final_text = parsed.get("text", collected.get("content", ""))
                for piece in deps["split_text_chunks"](final_text, 80):
                    chunk = {
                        "id": cid,
                        "object": "chat.completion.chunk",
                        "created": created_ts,
                        "model": model,
                        "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}],
                    }
                    yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                deps["mark_success"](
                    deps["acm"],
                    lease,
                    model=model,
                    request_type=request_type,
                    tokens_approx=_chat_tokens(messages, final_text),
                    cost=deps["get_cost"](rita_model),
                )
                stop_chunk = {
                    "id": cid,
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                }
                yield f"data: {json.dumps(stop_chunk)}\n\n"
                yield "data: [DONE]\n\n"

            return Response(gen_tool_buffered(), mimetype="text/event-stream", headers=response_headers)

        if stream:
            def gen():
                cid = f"chatcmpl-{int(time.time() * 1000)}"
                captured_msg_id = None
                output_parts = []

                with resp:
                    for obj in deps["iter_rita_sse"](resp):
                        event_type = obj.get("type", "")
                        if event_type == "quota_remain":
                            quota_chunk = {
                                "id": cid,
                                "object": "chat.completion.chunk",
                                "choices": [{"index": 0, "delta": {}, "finish_reason": None}],
                                "x_quota": {
                                    "quota_remain": obj.get("quota_remain"),
                                    "service_quota_remain": obj.get("service_quota_remain"),
                                },
                            }
                            yield f"data: {json.dumps(quota_chunk)}\n\n"
                            continue
                        if event_type in ("assistant_complete", "conv_title"):
                            if event_type == "assistant_complete":
                                break
                            continue
                        rid = obj.get("id", "")
                        if not captured_msg_id and isinstance(rid, str) and rid.startswith("ai"):
                            captured_msg_id = rid
                        choices = obj.get("choices", []) or []
                        if not choices:
                            continue
                        delta = choices[0].get("delta", {}) or {}
                        content = delta.get("content", "")
                        if content:
                            output_parts.append(str(content))
                            chunk = {
                                "id": cid,
                                "object": "chat.completion.chunk",
                                "created": obj.get("created", int(time.time())),
                                "model": model,
                                "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}],
                            }
                            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

                if captured_msg_id:
                    deps["update_conversation_state"](messages, captured_msg_id, chat_id)
                final_text = "".join(output_parts)
                deps["mark_success"](
                    deps["acm"],
                    lease,
                    model=model,
                    request_type=request_type,
                    tokens_approx=_chat_tokens(messages, final_text),
                    cost=deps["get_cost"](rita_model),
                )
                stop_chunk = {
                    "id": cid,
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                }
                yield f"data: {json.dumps(stop_chunk)}\n\n"
                yield "data: [DONE]\n\n"

            return Response(gen(), mimetype="text/event-stream", headers=response_headers)

        cid = f"chatcmpl-{int(time.time() * 1000)}"
        collected = deps["collect_rita_response"](resp)
        created_ts = int(collected.get("created") or time.time())
        if collected.get("message_id"):
            deps["update_conversation_state"](messages, collected["message_id"], chat_id)

        content = collected.get("content", "")
        if client_tools and content:
            parsed = deps["parse_tool_response"](content)
            if parsed["type"] == "tool_calls":
                deps["mark_success"](
                    deps["acm"],
                    lease,
                    model=model,
                    request_type=request_type,
                    tokens_approx=_chat_tokens(messages, content),
                    cost=deps["get_cost"](rita_model),
                )
                oai_tool_calls = [{
                    "id": f"call_{index}",
                    "type": "function",
                    "function": {
                        "name": call["name"],
                        "arguments": json.dumps(call["input"], ensure_ascii=False),
                    },
                } for index, call in enumerate(parsed["calls"])]
                return jsonify({
                    "id": cid,
                    "object": "chat.completion",
                    "created": created_ts,
                    "model": model,
                    "choices": [{
                        "index": 0,
                        "message": {"role": "assistant", "content": None, "tool_calls": oai_tool_calls},
                        "finish_reason": "tool_calls",
                    }],
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                })
            content = parsed.get("text", content)

        deps["mark_success"](
            deps["acm"],
            lease,
            model=model,
            request_type=request_type,
            tokens_approx=_chat_tokens(messages, content),
            cost=deps["get_cost"](rita_model),
        )
        return jsonify({
            "id": cid,
            "object": "chat.completion",
            "created": created_ts,
            "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        })

    except requests.RequestException as e:
        deps["log"](f"❌ Request error: {e}", "ERROR")
        deps["mark_failure"](deps["acm"], lease, str(e), model=model, request_type=request_type)
        return jsonify({"error": {"message": str(e), "type": "upstream_error"}}), 502
    except Exception as e:
        deps["log"](f"❌ Unexpected error: {e}", "ERROR")
        deps["mark_failure"](deps["acm"], lease, str(e), model=model, request_type=request_type)
        return jsonify({"error": {"message": str(e), "type": "internal_error"}}), 500


def handle_anthropic_count_tokens(request, deps: JsonDict):
    body = request.json or {}
    if not body.get("model"):
        return deps["make_anthropic_error"]("model is required")
    if not body.get("messages"):
        return deps["make_anthropic_error"]("messages is required")
    return jsonify(deps["estimate_anthropic_tokens"](body))


def handle_anthropic_messages_api(request, deps: JsonDict):
    body = request.json or {}
    requested_model = body.get("model", "")
    if not requested_model:
        return deps["make_anthropic_error"]("model is required")
    request_type = "messages"

    converted = deps["anthropic_messages_to_openai_chat"](body)
    messages = converted.get("messages", [])
    client_tools = converted.get("tools", [])
    stream = bool(body.get("stream", False))

    if not messages:
        return deps["make_anthropic_error"]("messages is required")

    client_token = request.headers.get("token", "")
    client_visitorid = request.headers.get("visitorid", "")

    try:
        lease = deps["acquire_lease"](
            deps["acm"],
            deps["RITA_ORIGIN"],
            client_token=client_token,
            client_visitorid=client_visitorid,
        )
    except deps["NoAvailableAccountError"]:
        return deps["make_anthropic_error"]("no accounts configured", "api_error", 500)

    try:
        rita_model = deps["resolve_model"](requested_model, lease.headers)
        chat_id, parent = deps["get_or_create_conversation"](messages)
        rita_messages = deps["build_rita_messages"](messages)
        if client_tools and rita_messages:
            last_text = rita_messages[-1]["text"]
            rita_messages[-1]["text"] = deps["inject_tool_prompt"](last_text, client_tools, deps["tool_prompt_cache"])
            deps["log"](f"🔧 anthropic tool prompt injected ({len(client_tools)} tools)", "DEBUG")

        chat_id = deps["ensure_conversation"](lease.headers, rita_model, chat_id)
        payload = {
            "model": rita_model,
            "messages": rita_messages,
            "online": 0,
            "model_type_id": 0,
            "chat_id": chat_id,
            "parent": parent,
        }
        resp = deps["rita_gateway"].request_completion_stream(lease.headers, payload)

        if resp.status_code >= 500:
            error_text = resp.text[:200]
            deps["mark_failure"](
                deps["acm"],
                lease,
                error_text,
                model=requested_model,
                request_type=request_type,
            )
            return deps["make_anthropic_error"]("upstream error", "api_error", 502)

        ct = resp.headers.get("content-type", "")
        if "application/json" in ct:
            try:
                err_body = resp.json()
                err_code = err_body.get("code", 0)
                if err_code and err_code != 0:
                    err_msg = err_body.get("error") or err_body.get("message") or "upstream error"
                    if deps["should_reset_conversation"](err_msg):
                        chat_id = deps["ensure_conversation"](lease.headers, rita_model, 0)
                        payload["chat_id"] = chat_id
                        payload["parent"] = "0"
                        resp = deps["rita_gateway"].request_completion_stream(lease.headers, payload)
                        ct = resp.headers.get("content-type", "")
                        if "application/json" in ct:
                            retry_err = resp.json()
                            retry_code = retry_err.get("code", 0)
                            if retry_code and retry_code != 0:
                                retry_msg = retry_err.get("error") or retry_err.get("message") or err_msg
                                deps["mark_failure"](
                                    deps["acm"],
                                    lease,
                                    str(retry_msg),
                                    model=requested_model,
                                    request_type=request_type,
                                )
                                return deps["make_anthropic_error"](str(retry_msg), "api_error", 502)
                    else:
                        deps["mark_failure"](
                            deps["acm"],
                            lease,
                            str(err_msg),
                            model=requested_model,
                            request_type=request_type,
                        )
                        return deps["make_anthropic_error"](str(err_msg), "api_error", 502)
            except Exception:
                pass

        resp.raise_for_status()
        input_tokens = int(deps["estimate_anthropic_tokens"](body).get("input_tokens", 0))
        response_headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

        if stream and client_tools:
            collected = deps["collect_rita_response"](resp)
            if collected.get("message_id"):
                deps["update_conversation_state"](messages, collected["message_id"], chat_id)
            parsed_text, tool_calls = deps["parse_tool_calls_from_text"](collected.get("content", ""))
            output_tokens = max(1, len((parsed_text or collected.get("content", ""))) // 4)
            deps["mark_success"](
                deps["acm"],
                lease,
                model=requested_model,
                request_type=request_type,
                tokens_approx=input_tokens + output_tokens,
                cost=deps["get_cost"](rita_model),
            )
            return Response(
                deps["build_anthropic_stream_events"](
                    requested_model,
                    parsed_text,
                    tool_calls=tool_calls,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    message_id=collected.get("message_id"),
                ),
                mimetype="text/event-stream",
                headers=response_headers,
            )

        if stream:
            def gen():
                message_id = f"msg_{int(time.time() * 1000)}"
                output_parts = []
                captured_msg_id = None
                yield f"event: message_start\ndata: {json.dumps({'type': 'message_start', 'message': {'id': message_id, 'type': 'message', 'role': 'assistant', 'content': [], 'model': requested_model, 'stop_reason': None, 'stop_sequence': None, 'usage': {'input_tokens': input_tokens, 'output_tokens': 0}}}, ensure_ascii=False)}\n\n"
                yield f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}}, ensure_ascii=False)}\n\n"
                with resp:
                    for event in deps["iter_rita_sse"](resp):
                        event_type = event.get("type", "")
                        if event_type in ("quota_remain", "conv_title"):
                            continue
                        if event_type == "assistant_complete":
                            break
                        rid = event.get("id", "")
                        if not captured_msg_id and isinstance(rid, str) and rid.startswith("ai"):
                            captured_msg_id = rid
                            message_id = rid
                        choices = event.get("choices", []) or []
                        if not choices:
                            continue
                        delta = choices[0].get("delta", {}) or {}
                        content = delta.get("content", "")
                        if content:
                            output_parts.append(str(content))
                            yield f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': content}}, ensure_ascii=False)}\n\n"
                if captured_msg_id:
                    deps["update_conversation_state"](messages, captured_msg_id, chat_id)
                final_text = "".join(output_parts)
                output_tokens = max(1, len(final_text) // 4) if final_text else 0
                deps["mark_success"](
                    deps["acm"],
                    lease,
                    model=requested_model,
                    request_type=request_type,
                    tokens_approx=input_tokens + output_tokens,
                    cost=deps["get_cost"](rita_model),
                )
                yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': 0}, ensure_ascii=False)}\n\n"
                yield f"event: message_delta\ndata: {json.dumps({'type': 'message_delta', 'delta': {'stop_reason': 'end_turn', 'stop_sequence': None}, 'usage': {'output_tokens': output_tokens}}, ensure_ascii=False)}\n\n"
                yield f"event: message_stop\ndata: {json.dumps({'type': 'message_stop'}, ensure_ascii=False)}\n\n"

            return Response(gen(), mimetype="text/event-stream", headers=response_headers)

        collected = deps["collect_rita_response"](resp)
        if collected.get("message_id"):
            deps["update_conversation_state"](messages, collected["message_id"], chat_id)
        parsed_text, tool_calls = deps["parse_tool_calls_from_text"](collected.get("content", ""))
        output_tokens = max(1, len((parsed_text or collected.get("content", ""))) // 4) if (parsed_text or collected.get("content", "")) else 0
        deps["mark_success"](
            deps["acm"],
            lease,
            model=requested_model,
            request_type=request_type,
            tokens_approx=input_tokens + output_tokens,
            cost=deps["get_cost"](rita_model),
        )
        return jsonify(
            deps["build_anthropic_message_response"](
                requested_model,
                parsed_text,
                tool_calls=tool_calls,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                message_id=collected.get("message_id"),
            )
        )

    except requests.RequestException as e:
        deps["mark_failure"](deps["acm"], lease, str(e), model=requested_model, request_type=request_type)
        return deps["make_anthropic_error"](str(e), "api_error", 502)
    except Exception as e:
        deps["mark_failure"](deps["acm"], lease, str(e), model=requested_model, request_type=request_type)
        deps["log"](f"❌ Anthropic messages unexpected error: {e}", "ERROR")
        return deps["make_anthropic_error"](str(e), "api_error", 500)


def handle_responses_api(request, deps: JsonDict):
    data = request.json or {}
    model = data.get("model", "model_25")
    stream = bool(data.get("stream", False))
    instructions = data.get("instructions")
    client_tools = data.get("tools", []) or []
    request_type = "responses"

    messages = deps["responses_input_to_messages"](data)
    if not messages:
        return jsonify({"error": {"message": "input is required", "type": "invalid_request_error"}}), 400

    deps["log"](f"📥 /v1/responses model={model} stream={stream} msgs={len(messages)} tools={len(client_tools)}", "INFO")
    deps["increment_stats"](model, messages)

    client_token = request.headers.get("token", "")
    client_visitorid = request.headers.get("visitorid", "")
    try:
        lease = deps["acquire_lease"](
            deps["acm"],
            deps["RITA_ORIGIN"],
            client_token=client_token,
            client_visitorid=client_visitorid,
        )
    except deps["NoAvailableAccountError"]:
        return jsonify({"error": {"message": "no accounts configured", "type": "config_error"}}), 500

    try:
        rita_model = deps["resolve_model"](model, lease.headers)
        rita_messages = deps["build_rita_messages"](messages)
        if client_tools and rita_messages:
            last_text = rita_messages[-1]["text"]
            rita_messages[-1]["text"] = deps["inject_tool_prompt"](last_text, client_tools, deps["tool_prompt_cache"])
            deps["log"](f"🔧 responses tool prompt injected ({len(client_tools)} tools)", "DEBUG")

        chat_id = deps["ensure_conversation"](lease.headers, rita_model, 0)
        payload = {
            "model": rita_model,
            "messages": rita_messages,
            "online": 0,
            "model_type_id": 0,
            "chat_id": chat_id,
            "parent": "0",
        }
        resp = deps["rita_gateway"].request_completion_stream(lease.headers, payload)

        ct = resp.headers.get("content-type", "")
        if "application/json" in ct:
            try:
                err = resp.json()
                if err.get("code") and err.get("code") != 0:
                    err_msg = err.get("error") or err.get("message") or "upstream error"
                    deps["mark_failure"](
                        deps["acm"],
                        lease,
                        str(err_msg),
                        model=model,
                        request_type=request_type,
                    )
                    return jsonify({"error": {"message": str(err_msg), "type": "upstream_error"}}), 502
            except Exception:
                pass

        resp.raise_for_status()
        resp_id = f"resp_{int(time.time() * 1000)}"
        created = time.time()

        def make_message_item(text: str, message_id: str | None = None) -> dict:
            return {
                "id": message_id or f"msg_{int(time.time() * 1000)}",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }

        if stream and client_tools:
            collected = deps["collect_rita_response"](resp)
            parsed = deps["parse_tool_response"](collected.get("content", ""))
            content_text = parsed.get("text", collected.get("content", "")) if isinstance(parsed, dict) else collected.get("content", "")
            usage = _responses_usage(messages, content_text)
            deps["mark_success"](
                deps["acm"],
                lease,
                model=model,
                request_type=request_type,
                tokens_approx=usage["total_tokens"],
                cost=deps["get_cost"](rita_model),
            )

            def gen_tool_stream():
                seq = 0
                base = deps["make_responses_base"](resp_id, model, created, instructions, "in_progress")
                yield f"event: response.created\ndata: {json.dumps({'type':'response.created','sequence_number':seq,'response':base}, ensure_ascii=False)}\n\n"; seq += 1
                yield f"event: response.in_progress\ndata: {json.dumps({'type':'response.in_progress','sequence_number':seq,'response':base}, ensure_ascii=False)}\n\n"; seq += 1
                if parsed.get("type") == "tool_calls":
                    output_items = _responses_function_call_items(parsed.get("calls", []))
                    for output_index, item in enumerate(output_items):
                        yield f"event: response.output_item.added\ndata: {json.dumps({'type':'response.output_item.added','sequence_number':seq,'output_index':output_index,'item':item}, ensure_ascii=False)}\n\n"; seq += 1
                        yield f"event: response.output_item.done\ndata: {json.dumps({'type':'response.output_item.done','sequence_number':seq,'output_index':output_index,'item':item}, ensure_ascii=False)}\n\n"; seq += 1
                    final = deps["make_responses_base"](resp_id, model, created, instructions, "completed")
                    final["output"] = output_items
                    final["usage"] = usage
                    final["tools"] = client_tools
                    yield f"event: response.completed\ndata: {json.dumps({'type':'response.completed','sequence_number':seq,'response':final}, ensure_ascii=False)}\n\n"
                    return

                item = make_message_item(content_text, collected.get("message_id"))
                yield f"event: response.output_item.added\ndata: {json.dumps({'type':'response.output_item.added','sequence_number':seq,'output_index':0,'item': {'id': item['id'], 'type': 'message', 'role': 'assistant', 'status': 'in_progress', 'content': []}}, ensure_ascii=False)}\n\n"; seq += 1
                yield f"event: response.content_part.added\ndata: {json.dumps({'type':'response.content_part.added','sequence_number':seq,'item_id':item['id'],'output_index':0,'content_index':0,'part': {'type': 'output_text', 'text': '', 'annotations': []}}, ensure_ascii=False)}\n\n"; seq += 1
                for piece in deps["split_text_chunks"](content_text, 80):
                    yield f"event: response.output_text.delta\ndata: {json.dumps({'type':'response.output_text.delta','sequence_number':seq,'item_id':item['id'],'output_index':0,'content_index':0,'delta':piece}, ensure_ascii=False)}\n\n"; seq += 1
                yield f"event: response.output_text.done\ndata: {json.dumps({'type':'response.output_text.done','sequence_number':seq,'item_id':item['id'],'output_index':0,'content_index':0,'text':content_text}, ensure_ascii=False)}\n\n"; seq += 1
                yield f"event: response.content_part.done\ndata: {json.dumps({'type':'response.content_part.done','sequence_number':seq,'item_id':item['id'],'output_index':0,'content_index':0,'part': item['content'][0]}, ensure_ascii=False)}\n\n"; seq += 1
                yield f"event: response.output_item.done\ndata: {json.dumps({'type':'response.output_item.done','sequence_number':seq,'output_index':0,'item':item}, ensure_ascii=False)}\n\n"; seq += 1
                final = deps["make_responses_base"](resp_id, model, created, instructions, "completed")
                final["output"] = [item]
                final["usage"] = usage
                final["tools"] = client_tools
                yield f"event: response.completed\ndata: {json.dumps({'type':'response.completed','sequence_number':seq,'response':final}, ensure_ascii=False)}\n\n"

            return Response(gen_tool_stream(), mimetype="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

        if stream:
            def gen_responses_stream():
                seq = 0
                full_text_parts = []
                base = deps["make_responses_base"](resp_id, model, created, instructions, "in_progress")
                yield f"event: response.created\ndata: {json.dumps({'type':'response.created','sequence_number':seq,'response':base}, ensure_ascii=False)}\n\n"; seq += 1
                yield f"event: response.in_progress\ndata: {json.dumps({'type':'response.in_progress','sequence_number':seq,'response':base}, ensure_ascii=False)}\n\n"; seq += 1
                item_id = f"msg_{int(time.time() * 1000)}"
                yield f"event: response.output_item.added\ndata: {json.dumps({'type':'response.output_item.added','sequence_number':seq,'output_index':0,'item': {'id': item_id, 'type': 'message', 'role': 'assistant', 'status': 'in_progress', 'content': []}}, ensure_ascii=False)}\n\n"; seq += 1
                yield f"event: response.content_part.added\ndata: {json.dumps({'type':'response.content_part.added','sequence_number':seq,'item_id':item_id,'output_index':0,'content_index':0,'part': {'type': 'output_text', 'text': '', 'annotations': []}}, ensure_ascii=False)}\n\n"; seq += 1
                with resp:
                    for event in deps["iter_rita_sse"](resp):
                        event_type = event.get("type", "")
                        if event_type in ("quota_remain", "conv_title"):
                            continue
                        if event_type == "assistant_complete":
                            break
                        choices = event.get("choices", []) or []
                        if not choices:
                            continue
                        delta_content = choices[0].get("delta", {}).get("content", "")
                        if delta_content:
                            full_text_parts.append(delta_content)
                            yield f"event: response.output_text.delta\ndata: {json.dumps({'type':'response.output_text.delta','sequence_number':seq,'item_id':item_id,'output_index':0,'content_index':0,'delta':delta_content}, ensure_ascii=False)}\n\n"; seq += 1
                full_text = "".join(full_text_parts)
                usage = _responses_usage(messages, full_text)
                deps["mark_success"](
                    deps["acm"],
                    lease,
                    model=model,
                    request_type=request_type,
                    tokens_approx=usage["total_tokens"],
                    cost=deps["get_cost"](rita_model),
                )
                part = {"type": "output_text", "text": full_text, "annotations": []}
                item = {"id": item_id, "type": "message", "role": "assistant", "status": "completed", "content": [part]}
                yield f"event: response.output_text.done\ndata: {json.dumps({'type':'response.output_text.done','sequence_number':seq,'item_id':item_id,'output_index':0,'content_index':0,'text':full_text}, ensure_ascii=False)}\n\n"; seq += 1
                yield f"event: response.content_part.done\ndata: {json.dumps({'type':'response.content_part.done','sequence_number':seq,'item_id':item_id,'output_index':0,'content_index':0,'part': part}, ensure_ascii=False)}\n\n"; seq += 1
                yield f"event: response.output_item.done\ndata: {json.dumps({'type':'response.output_item.done','sequence_number':seq,'output_index':0,'item':item}, ensure_ascii=False)}\n\n"; seq += 1
                final = deps["make_responses_base"](resp_id, model, created, instructions, "completed")
                final["output"] = [item]
                final["usage"] = usage
                final["tools"] = client_tools
                yield f"event: response.completed\ndata: {json.dumps({'type':'response.completed','sequence_number':seq,'response':final}, ensure_ascii=False)}\n\n"

            return Response(gen_responses_stream(), mimetype="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

        collected = deps["collect_rita_response"](resp)
        content = collected.get("content", "")
        parsed = deps["parse_tool_response"](content) if client_tools else {"type": "text", "text": content}
        final_text = parsed.get("text", content) if isinstance(parsed, dict) else content
        usage = _responses_usage(messages, final_text)
        deps["mark_success"](
            deps["acm"],
            lease,
            model=model,
            request_type=request_type,
            tokens_approx=usage["total_tokens"],
            cost=deps["get_cost"](rita_model),
        )
        result = deps["make_responses_base"](resp_id, model, created, instructions, "completed")
        result["tools"] = client_tools
        result["usage"] = usage
        if isinstance(parsed, dict) and parsed.get("type") == "tool_calls":
            result["output"] = _responses_function_call_items(parsed.get("calls", []))
            return jsonify(result)
        result["output"] = [make_message_item(final_text, collected.get("message_id"))]
        return jsonify(result)

    except requests.RequestException as e:
        deps["log"](f"❌ Responses API error: {e}", "ERROR")
        deps["mark_failure"](deps["acm"], lease, str(e), model=model, request_type=request_type)
        return jsonify({"error": {"message": str(e), "type": "upstream_error"}}), 502
    except Exception as e:
        deps["log"](f"❌ Responses API unexpected error: {e}", "ERROR")
        deps["mark_failure"](deps["acm"], lease, str(e), model=model, request_type=request_type)
        return jsonify({"error": {"message": str(e), "type": "internal_error"}}), 500

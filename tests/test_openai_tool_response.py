import json
import unittest

from auto_register import _build_auto_replenish_plan
from adapters.anthropic_protocol import parse_tool_calls_from_text
from adapters.openai_protocol import parse_tool_response, split_embedded_thinking
from services.rita_gateway import iter_rita_sse


class ParseToolResponseTests(unittest.TestCase):
    def test_parse_single_tool_call_from_fenced_json(self):
        raw = """```json
{"tool":"weather_lookup","args":{"city":"上海","unit":"c"}}
```"""

        parsed = parse_tool_response(raw)

        self.assertEqual(
            parsed,
            {
                "type": "tool_calls",
                "calls": [
                    {
                        "name": "weather_lookup",
                        "input": {"city": "上海", "unit": "c"},
                    }
                ],
            },
        )

    def test_parse_multiple_tool_calls_and_convert_for_anthropic(self):
        raw = json.dumps(
            {
                "calls": [
                    {"name": "search_docs", "parameters": {"query": "quota"}},
                    {"tool": "fetch_user", "input": {"id": "u_1"}},
                ]
            },
            ensure_ascii=False,
        )

        parsed = parse_tool_response(raw)
        rendered_text, tool_calls = parse_tool_calls_from_text(raw)

        self.assertEqual(parsed["type"], "tool_calls")
        self.assertEqual(
            parsed["calls"],
            [
                {"name": "search_docs", "input": {"query": "quota"}},
                {"name": "fetch_user", "input": {"id": "u_1"}},
            ],
        )
        self.assertEqual(rendered_text, "")
        self.assertEqual(
            tool_calls,
            [
                {
                    "id": "call_0",
                    "type": "function",
                    "function": {
                        "name": "search_docs",
                        "arguments": json.dumps({"query": "quota"}, ensure_ascii=False),
                    },
                },
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "fetch_user",
                        "arguments": json.dumps({"id": "u_1"}, ensure_ascii=False),
                    },
                },
            ],
        )


class RitaSseEncodingTests(unittest.TestCase):
    class _FakeResponse:
        def __init__(self, body: bytes, encoding: str):
            self._body = body
            self.encoding = encoding

        def iter_lines(self, decode_unicode: bool = False):
            for line in self._body.splitlines():
                if decode_unicode:
                    yield line.decode(self.encoding)
                else:
                    yield line

    def test_iter_rita_sse_forces_utf8_even_when_response_declares_latin1(self):
        payload = {
            "choices": [
                {
                    "delta": {
                        "content": "I'm Claude — 我可以帮你处理代码问题",
                    }
                }
            ]
        }
        body = f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")
        response = self._FakeResponse(body, "ISO-8859-1")

        events = list(iter_rita_sse(response))

        self.assertEqual(len(events), 1)
        self.assertEqual(
            events[0]["choices"][0]["delta"]["content"],
            "I'm Claude — 我可以帮你处理代码问题",
        )


class EmbeddedThinkingTests(unittest.TestCase):
    def test_split_embedded_thinking_returns_visible_text_and_reasoning_parts(self):
        visible, thinking = split_embedded_thinking(
            "<thinking>先规划工具调用</thinking>\n最终答案"
        )

        self.assertEqual(visible, "最终答案")
        self.assertEqual(thinking, ["先规划工具调用"])


class AutoReplenishPlanTests(unittest.TestCase):
    def test_build_plan_triggers_when_active_accounts_below_threshold(self):
        plan = _build_auto_replenish_plan(
            {"active": 1, "total_quota": 500},
            {"AUTO_REGISTER_MIN_ACTIVE": 3, "AUTO_REGISTER_MIN_QUOTA": 50, "AUTO_REGISTER_BATCH": 5},
        )

        self.assertTrue(plan["should_replenish"])
        self.assertEqual(plan["to_create"], 2)
        self.assertIn("active 1 < min_active 3", plan["reason_text"])

    def test_build_plan_triggers_when_total_quota_below_threshold(self):
        plan = _build_auto_replenish_plan(
            {"active": 5, "total_quota": 10},
            {"AUTO_REGISTER_MIN_ACTIVE": 2, "AUTO_REGISTER_MIN_QUOTA": 50, "AUTO_REGISTER_BATCH": 3},
        )

        self.assertTrue(plan["should_replenish"])
        self.assertEqual(plan["to_create"], 1)
        self.assertIn("total_quota 10 < min_quota 50", plan["reason_text"])


if __name__ == "__main__":
    unittest.main()

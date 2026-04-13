import json
import unittest

from adapters.anthropic_protocol import parse_tool_calls_from_text
from adapters.openai_protocol import parse_tool_response


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


if __name__ == "__main__":
    unittest.main()

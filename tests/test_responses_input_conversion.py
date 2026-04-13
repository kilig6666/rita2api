import json
import unittest

from adapters.openai_protocol import make_responses_base, responses_input_to_messages


class ResponsesInputToMessagesTests(unittest.TestCase):
    def test_converts_top_level_function_call_and_function_call_output(self):
        data = {
            "instructions": "遵守系统规则",
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "帮我查上海天气"},
                        {"type": "input_image", "image_url": "https://img.example.com/weather.png"},
                    ],
                },
                {
                    "type": "function_call",
                    "call_id": "call_weather",
                    "name": "weather_lookup",
                    "arguments": {"city": "上海", "unit": "c"},
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_weather",
                    "output": {"status": "ok", "weather": "晴"},
                },
            ],
        }

        messages = responses_input_to_messages(data)

        self.assertEqual(
            messages,
            [
                {"role": "system", "content": "遵守系统规则"},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "帮我查上海天气"},
                        {
                            "type": "image_url",
                            "image_url": "https://img.example.com/weather.png",
                        },
                    ],
                },
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_weather",
                            "type": "function",
                            "function": {
                                "name": "weather_lookup",
                                "arguments": json.dumps(
                                    {"city": "上海", "unit": "c"}, ensure_ascii=False
                                ),
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_weather",
                    "content": json.dumps(
                        {"status": "ok", "weather": "晴"}, ensure_ascii=False
                    ),
                },
            ],
        )

    def test_extracts_function_call_from_assistant_content_array(self):
        data = {
            "input": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "output_text", "text": "我先调用工具。"},
                        {
                            "type": "function_call",
                            "id": "call_calc",
                            "name": "calculator",
                            "arguments": {"expression": "2+2"},
                        },
                        {"type": "output_text", "text": "调用后再回复你。"},
                    ],
                }
            ]
        }

        messages = responses_input_to_messages(data)

        self.assertEqual(
            messages,
            [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "我先调用工具。"},
                        {"type": "text", "text": "调用后再回复你。"},
                    ],
                    "tool_calls": [
                        {
                            "id": "call_calc",
                            "type": "function",
                            "function": {
                                "name": "calculator",
                                "arguments": json.dumps(
                                    {"expression": "2+2"}, ensure_ascii=False
                                ),
                            },
                        }
                    ],
                }
            ],
        )

    def test_uses_content_array_as_function_call_output_fallback(self):
        data = {
            "input": [
                {
                    "type": "function_call_output",
                    "call_id": "call_search",
                    "content": [
                        {"type": "output_text", "text": "第一段结果"},
                        {"type": "text", "text": "第二段结果"},
                    ],
                }
            ]
        }

        messages = responses_input_to_messages(data)

        self.assertEqual(
            messages,
            [
                {
                    "role": "tool",
                    "tool_call_id": "call_search",
                    "content": "第一段结果第二段结果",
                }
            ],
        )

    def test_make_responses_base_uses_request_options(self):
        base = make_responses_base(
            "resp_1",
            "gpt-5.4",
            123.0,
            "遵守系统规则",
            "completed",
            {
                "tool_choice": {"type": "function", "function": {"name": "weather_lookup"}},
                "parallel_tool_calls": False,
                "temperature": 0.2,
                "top_p": 0.8,
                "max_output_tokens": 4096,
                "metadata": {"trace_id": "abc"},
            },
        )

        self.assertEqual(base["tool_choice"], {"type": "function", "function": {"name": "weather_lookup"}})
        self.assertFalse(base["parallel_tool_calls"])
        self.assertEqual(base["temperature"], 0.2)
        self.assertEqual(base["top_p"], 0.8)
        self.assertEqual(base["max_output_tokens"], 4096)
        self.assertEqual(base["metadata"], {"trace_id": "abc"})


if __name__ == "__main__":
    unittest.main()

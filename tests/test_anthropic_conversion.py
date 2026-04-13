import json
import unittest

from adapters.anthropic_protocol import anthropic_messages_to_openai_chat


class AnthropicMessagesToOpenAIChatTests(unittest.TestCase):
    def test_convert_messages_tools_and_tool_choice(self):
        body = {
            "system": [{"type": "text", "text": "follow policy"}],
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "先查天气"},
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": "abc",
                            },
                        },
                    ],
                },
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "need tool"},
                        {
                            "type": "tool_use",
                            "id": "toolu_1",
                            "name": "weather_lookup",
                            "input": {"city": "上海"},
                        },
                        {"type": "text", "text": "我来查"},
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_1",
                            "content": [{"type": "text", "text": "晴"}],
                        }
                    ],
                },
            ],
            "tools": [
                {
                    "name": "weather_lookup",
                    "description": "查询天气",
                    "input_schema": {
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                        "required": ["city"],
                    },
                }
            ],
            "tool_choice": {"type": "tool", "name": "weather_lookup"},
            "max_tokens": 1024,
            "stream": True,
        }

        converted = anthropic_messages_to_openai_chat(body)

        self.assertEqual(
            converted["messages"],
            [
                {"role": "system", "content": "follow policy"},
                {"role": "user", "content": [{"type": "text", "text": "先查天气"}, {"type": "image_url", "image_url": "anthropic://image"}]},
                {
                    "role": "assistant",
                    "content": "<thinking>need tool</thinking>我来查",
                    "tool_calls": [
                        {
                            "id": "toolu_1",
                            "type": "function",
                            "function": {
                                "name": "weather_lookup",
                                "arguments": json.dumps({"city": "上海"}, ensure_ascii=False),
                            },
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "toolu_1", "content": "晴"},
            ],
        )
        self.assertEqual(
            converted["tools"],
            [
                {
                    "type": "function",
                    "function": {
                        "name": "weather_lookup",
                        "description": "查询天气",
                        "parameters": {
                            "type": "object",
                            "properties": {"city": {"type": "string"}},
                            "required": ["city"],
                        },
                    },
                }
            ],
        )
        self.assertEqual(
            converted["tool_choice"],
            {"type": "function", "function": {"name": "weather_lookup"}},
        )
        self.assertEqual(converted["max_tokens"], 1024)
        self.assertTrue(converted["stream"])


if __name__ == "__main__":
    unittest.main()

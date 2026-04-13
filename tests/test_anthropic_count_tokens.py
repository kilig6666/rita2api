import unittest

from adapters.anthropic_protocol import estimate_anthropic_tokens


class EstimateAnthropicTokensTests(unittest.TestCase):
    def test_estimate_counts_strings_and_images(self):
        body = {
            "system": "sys",
            "messages": [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "ok"},
                {
                    "role": "user",
                    "content": [
                        "图像说明",
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
            ],
        }

        result = estimate_anthropic_tokens(body)

        self.assertEqual(result["input_tokens"], (len("sys") + len("hello") + len("ok") + len("图像说明") + 256) // 4)
        self.assertEqual(result["message_count"], 3)
        self.assertEqual(result["image_count"], 1)
        self.assertEqual(result["cache_creation_input_tokens"], 0)
        self.assertEqual(result["cache_read_input_tokens"], 0)
        self.assertTrue(result["estimated"])

    def test_estimate_has_minimum_one_input_token(self):
        body = {"messages": [{"role": "user", "content": ""}]}

        result = estimate_anthropic_tokens(body)

        self.assertEqual(result["input_tokens"], 1)
        self.assertEqual(result["message_count"], 1)
        self.assertEqual(result["image_count"], 0)


if __name__ == "__main__":
    unittest.main()

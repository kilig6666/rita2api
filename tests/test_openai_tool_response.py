import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import accounts as accounts_module
from auto_register import _build_auto_replenish_plan
from adapters.anthropic_protocol import parse_tool_calls_from_text
from adapters.openai_protocol import parse_tool_response, split_embedded_thinking
from database import DB
from services.rita_dispatch import (
    acquire_lease,
    disable_quota_exhausted,
    is_quota_exhausted_message,
    mark_success,
)
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


class ManualRegisterTaskTests(unittest.TestCase):
    def test_manual_register_task_logs_once_and_does_not_recurse(self):
        try:
            import server
        except ModuleNotFoundError as exc:
            self.skipTest(f"server import dependency missing: {exc}")

        task_id = "manualtest123456"
        original_task = server._manual_register_task
        server._manual_register_task = {
            "id": task_id,
            "status": "running",
            "requested": 1,
            "threads": 1,
            "captcha_provider": "yescaptcha",
            "stop_requested": False,
            "created_at": 0,
            "updated_at": 0,
            "seq": 0,
            "logs": [],
            "accounts": [],
            "success_count": 0,
            "failed_count": 0,
            "active_workers": 0,
            "error": "",
        }

        def fake_batch(**_kwargs):
            server.auto_register._log("trigger one log", "INFO")
            return []

        try:
            with mock.patch.object(server.auto_register, "auto_register_batch", side_effect=fake_batch), \
                 mock.patch.object(server, "log", return_value=None):
                server._run_manual_register_task(task_id, 1, 1, "yescaptcha")

            task = server._manual_register_task
            self.assertEqual(task["status"], "completed")
            self.assertEqual(task["error"], "")
            messages = [item["message"] for item in task["logs"]]
            self.assertEqual(
                messages.count("🚀 手动注册任务已启动，请求数量=1，线程=1，打码=yescaptcha"),
                1,
            )
            self.assertIn("trigger one log", messages)
        finally:
            server._manual_register_task = original_task


class AccountReservationTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db = DB(Path(self._tmpdir.name) / "rita-test.db")
        self.get_db_patch = mock.patch.object(accounts_module, "get_db", return_value=self.db)
        self.get_db_patch.start()
        self.manager = accounts_module.AccountManager()

    def tearDown(self):
        self.get_db_patch.stop()
        self._tmpdir.cleanup()

    def test_reserve_next_respects_min_quota_and_release(self):
        low = self.manager.add(token="tok-low", name="low", quota_remain=1)
        high = self.manager.add(token="tok-high", name="high", quota_remain=10)

        lease = acquire_lease(self.manager, "https://www.rita.ai", required_quota=5)

        self.assertEqual(lease.account.id, high.id)
        self.assertEqual(self.manager.get(high.id).inflight_count, 1)
        self.assertEqual(self.manager.get(low.id).inflight_count, 0)

        mark_success(self.manager, lease, model="model_15", request_type="chat_completions", cost=5)

        refreshed_high = self.manager.get(high.id)
        self.assertEqual(refreshed_high.inflight_count, 0)
        self.assertEqual(refreshed_high.quota_remain, 5)

    def test_disable_quota_exhausted_soft_disables_account(self):
        first = self.manager.add(token="tok-first", name="first", quota_remain=5)
        second = self.manager.add(token="tok-second", name="second", quota_remain=5)

        lease = acquire_lease(self.manager, "https://www.rita.ai", required_quota=1)
        disable_quota_exhausted(self.manager, lease, error="积分不足")

        disabled = self.manager.get(lease.account.id)
        self.assertFalse(disabled.enabled)
        self.assertEqual(disabled.quota_remain, 0)
        self.assertEqual(disabled.disabled_reason, "quota_exhausted")
        self.assertEqual(disabled.inflight_count, 0)

        next_lease = acquire_lease(
            self.manager,
            "https://www.rita.ai",
            required_quota=1,
            exclude_account_ids={disabled.id},
        )
        self.assertIn(next_lease.account.id, {first.id, second.id} - {disabled.id})


class RitaDispatchQuotaMessageTests(unittest.TestCase):
    def test_is_quota_exhausted_message_supports_english_and_chinese(self):
        self.assertTrue(is_quota_exhausted_message("quota insufficient for this request"))
        self.assertTrue(is_quota_exhausted_message("当前积分不足，请稍后再试"))
        self.assertFalse(is_quota_exhausted_message("conversation does not exist"))


if __name__ == "__main__":
    unittest.main()

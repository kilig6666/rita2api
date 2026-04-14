import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import accounts as accounts_module
import auto_register
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


class RegisterProxyProbeTests(unittest.TestCase):
    def test_probe_register_proxy_exit_parses_ip_and_region(self):
        fake_session = mock.Mock()
        fake_response = mock.Mock()
        fake_response.text = "ip=1.2.3.4\nloc=US\ncolo=LAX\n"
        fake_response.raise_for_status.return_value = None
        fake_session.get.return_value = fake_response

        with mock.patch.object(auto_register.requests, "Session", return_value=fake_session):
            result = auto_register._probe_register_proxy_exit(
                "http://user:pass@127.0.0.1:8080",
                disable_ssl_verify=True,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["ip"], "1.2.3.4")
        self.assertEqual(result["loc"], "US")
        self.assertEqual(result["colo"], "LAX")
        fake_session.close.assert_called_once()

    def test_auto_register_one_logs_proxy_exit_before_registering(self):
        logged = []

        def fake_log(message, level="INFO"):
            logged.append((level, message))

        cfg = {
            "MAIL_PROVIDER_DEFAULT": "gptmail",
            "CAPTCHA_PROVIDER": "yescaptcha",
            "REGISTER_PROXY": "http://user:pass@127.0.0.1:8080",
            "MAIL_USE_PROXY": False,
            "DISABLE_SSL_VERIFY": True,
            "AUTO_REGISTER_PASSWORD": "@qazwsx123456",
            "YESCAPTCHA_KEY": "dummy",
            "OHMYCAPTCHA_LOCAL_API_URL": "http://127.0.0.1:8001",
            "OHMYCAPTCHA_LOCAL_KEY": "",
            "GPTMAIL_API_KEY": "test",
            "GPTMAIL_API_BASE": "https://mail.example.com",
            "YYDSMAIL_API_KEY": "",
            "YYDSMAIL_API_BASE": "https://maliapi.215.im/v1",
            "MOEMAIL_API_KEY": "",
            "MOEMAIL_API_BASE": "",
            "MOEMAIL_CHANNELS_JSON": "",
        }

        with mock.patch.object(auto_register, "_get_live_config", return_value=cfg), \
             mock.patch.object(auto_register, "_probe_register_proxy_exit", return_value={
                 "ok": True,
                 "ip": "8.8.8.8",
                 "loc": "US",
                 "colo": "SJC",
                 "proxy": cfg["REGISTER_PROXY"],
                 "trace": {},
                 "error": "",
             }), \
             mock.patch.object(auto_register, "create_temp_email_by_provider", return_value={
                 "email": "foo@example.com",
                 "mail_api_key": "mail-token",
             }), \
             mock.patch.object(auto_register, "register_rita_account", return_value={
                 "token": "tok_123",
                 "email": "foo@example.com",
                 "ticket": "ticket_123",
             }), \
             mock.patch.object(auto_register, "_log", side_effect=fake_log):
            result = auto_register.auto_register_one(account_manager=None)

        self.assertEqual(result["email"], "foo@example.com")
        messages = [item[1] for item in logged]
        self.assertIn("🌐 当前注册代理: http://user:pass@127.0.0.1:8080", messages)
        self.assertIn("🌍 本次代理出口: ip=8.8.8.8 loc=US colo=SJC", messages)


class ManualRegisterProxyExitStateTests(unittest.TestCase):
    def test_append_manual_register_log_updates_current_proxy_exit(self):
        try:
            import server
        except ModuleNotFoundError as exc:
            self.skipTest(f"server import dependency missing: {exc}")

        original_task = server._manual_register_task
        server._manual_register_task = {
            "id": "proxytask123456",
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
            "current_proxy_exit": None,
            "error": "",
        }

        try:
            with mock.patch.object(server, "log", return_value=None):
                server._append_manual_register_log(
                    "proxytask123456",
                    "🌍 本次代理出口: ip=151.242.36.81 loc=JP colo=NRT",
                    "INFO",
                )

            task = server._manual_register_task
            self.assertEqual(
                task["current_proxy_exit"],
                {"ip": "151.242.36.81", "loc": "JP", "colo": "NRT"},
            )
            public = server._manual_register_public(task)
            self.assertEqual(
                public["current_proxy_exit"],
                {"ip": "151.242.36.81", "loc": "JP", "colo": "NRT"},
            )
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

    def test_export_for_import_returns_rehydratable_json_array(self):
        self.manager.add(
            token="tok-export-1",
            visitorid="visitor-1",
            name="导出账号1",
            email="user1@example.com",
            password="pwd-1",
            mail_provider="gptmail",
            mail_api_key="mail-key-1",
            quota_remain=88,
            enabled=False,
        )
        second = self.manager.add(
            token="tok-export-2",
            visitorid="visitor-2",
            name="导出账号2",
            email="user2@example.com",
            password="pwd-2",
            mail_provider="moemail",
            mail_api_key="mail-key-2",
            quota_remain=66,
            enabled=True,
        )

        exported = self.manager.export_for_import([second.id])

        self.assertEqual(exported, [{
            "token": "tok-export-2",
            "visitorid": "visitor-2",
            "name": "导出账号2",
            "email": "user2@example.com",
            "password": "pwd-2",
            "mail_provider": "moemail",
            "mail_api_key": "mail-key-2",
            "quota_remain": 66,
            "enabled": True,
        }])

    def test_preview_batch_import_counts_existing_and_payload_duplicates(self):
        self.manager.add(
            token="tok-existing",
            visitorid="visitor-existing",
            name="已存在账号",
            email="existing@example.com",
        )

        summary = self.manager.preview_batch_import([
            {"token": "tok-existing", "email": "new@example.com"},
            {"token": "tok-new", "email": "existing@example.com"},
            {"token": "tok-new", "email": "dup@example.com"},
            {"token": "", "email": "missing@example.com"},
            {"token": "tok-unique", "email": "unique@example.com"},
        ])

        self.assertEqual(summary["total"], 5)
        self.assertEqual(summary["valid"], 4)
        self.assertEqual(summary["missing_token"], 1)
        self.assertEqual(summary["duplicate_token_in_payload"], 1)
        self.assertEqual(summary["duplicate_email_in_payload"], 0)
        self.assertEqual(summary["duplicate_token_existing"], 1)
        self.assertEqual(summary["duplicate_email_existing"], 1)
        self.assertEqual(summary["duplicate_candidates_total"], 3)
        self.assertEqual(summary["importable_total"], 1)

    def test_add_batch_with_dedupe_skips_duplicates(self):
        self.manager.add(
            token="tok-existing",
            name="已存在账号",
            email="existing@example.com",
        )

        added, summary = self.manager.add_batch([
            {"token": "tok-existing", "email": "another@example.com"},
            {"token": "tok-new-1", "email": "existing@example.com"},
            {"token": "tok-new-2", "email": "fresh@example.com"},
            {"token": "tok-new-2", "email": "dup-token@example.com"},
        ], dedupe=True)

        self.assertEqual(len(added), 1)
        self.assertEqual(added[0].token, "tok-new-2")
        self.assertEqual(summary["added"], 1)
        self.assertEqual(summary["skipped_duplicate_token"], 2)
        self.assertEqual(summary["skipped_duplicate_email"], 1)


class RitaDispatchQuotaMessageTests(unittest.TestCase):
    def test_is_quota_exhausted_message_supports_english_and_chinese(self):
        self.assertTrue(is_quota_exhausted_message("quota insufficient for this request"))
        self.assertTrue(is_quota_exhausted_message("当前积分不足，请稍后再试"))
        self.assertFalse(is_quota_exhausted_message("conversation does not exist"))


class ImageGenerationOptionTests(unittest.TestCase):
    def test_normalize_reference_images_accepts_string_and_list(self):
        try:
            import server
        except ModuleNotFoundError as exc:
            self.skipTest(f"server import dependency missing: {exc}")

        self.assertEqual(
            server._normalize_reference_images("data:image/png;base64,abc"),
            ["data:image/png;base64,abc"],
        )
        self.assertEqual(
            server._normalize_reference_images([
                {"url": "data:image/png;base64,aaa"},
                "data:image/png;base64,bbb",
            ]),
            ["data:image/png;base64,aaa", "data:image/png;base64,bbb"],
        )

    def test_select_image_size_options_prefers_explicit_ratio_resolution(self):
        try:
            import server
        except ModuleNotFoundError as exc:
            self.skipTest(f"server import dependency missing: {exc}")

        model_detail = {
            "name": "Nano-banana 2",
            "ratio": ["1:1", "4:3", "16:9"],
            "resolution": [
                {"resolution": "1K"},
                {"resolution": "2K"},
                {"resolution": "4K"},
            ],
        }

        ratio, resolution = server._select_image_size_options(
            model_detail,
            size="1024x1024",
            ratio="4:3",
            resolution="2k",
            quality="high",
        )
        self.assertEqual((ratio, resolution), ("4:3", "2K"))

        ratio, resolution = server._select_image_size_options(
            model_detail,
            ratio="4:3",
            quality="high",
        )
        self.assertEqual((ratio, resolution), ("4:3", "4K"))

    def test_api_image_model_options_returns_supported_choices(self):
        try:
            import server
        except ModuleNotFoundError as exc:
            self.skipTest(f"server import dependency missing: {exc}")

        fake_account = mock.Mock(id="acc-image")
        model_detail = {
            "name": "Nano-banana 2",
            "ratio": ["1:1", "4:3"],
            "resolution": [
                {"resolution": "1K"},
                {"resolution": "2K"},
            ],
            "image_reference_flg": 1,
        }

        with mock.patch.object(server, "_get_auth_token", return_value="panel-token"), \
             mock.patch.object(server.acm, "next", return_value=(fake_account, 0)), \
             mock.patch.object(server.acm, "upstream_headers", return_value={"Authorization": "Bearer upstream"}), \
             mock.patch.object(server.acm, "mark_ok"), \
             mock.patch.object(server, "_resolve_image_model_metadata", return_value=("model_888", model_detail)):
            client = server.app.test_client()
            response = client.get("/api/image-model-options?auth=panel-token&model=model_888")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["model"], "model_888")
        self.assertEqual(payload["ratio_options"], ["1:1", "4:3"])
        self.assertEqual(payload["resolution_options"], ["1K", "2K"])
        self.assertEqual(payload["default_ratio"], "1:1")
        self.assertEqual(payload["default_resolution"], "1K")
        self.assertEqual(payload["count_options"], [1, 2, 3, 4])
        self.assertTrue(payload["reference_image_supported"])

    def test_image_generations_route_forwards_ratio_resolution_count_and_image(self):
        try:
            import server
        except ModuleNotFoundError as exc:
            self.skipTest(f"server import dependency missing: {exc}")

        with mock.patch.object(server, "_get_proxy_api_key", return_value="proxy-token"), \
             mock.patch.object(
                 server,
                 "_generate_openai_image_result",
                 return_value={"created": 1710000000, "data": [{"url": "https://img.example/test.png"}]},
             ) as mocked_generate:
            client = server.app.test_client()
            response = client.post(
                "/v1/images/generations?auth=proxy-token",
                json={
                    "model": "model_888",
                    "prompt": "draw a banana",
                    "ratio": "4:3",
                    "resolution": "2K",
                    "n": 3,
                    "image": ["data:image/png;base64,abc"],
                    "response_format": "url",
                },
            )

        self.assertEqual(response.status_code, 200)
        mocked_generate.assert_called_once()
        self.assertEqual(mocked_generate.call_args.args[:2], ("model_888", "draw a banana"))
        self.assertEqual(mocked_generate.call_args.kwargs["ratio"], "4:3")
        self.assertEqual(mocked_generate.call_args.kwargs["resolution"], "2K")
        self.assertEqual(mocked_generate.call_args.kwargs["n"], 3)
        self.assertEqual(mocked_generate.call_args.kwargs["image"], ["data:image/png;base64,abc"])


class AccountExportApiTests(unittest.TestCase):
    def test_api_accounts_export_requires_ids_for_selected_scope(self):
        try:
            import server
        except ModuleNotFoundError as exc:
            self.skipTest(f"server import dependency missing: {exc}")

        with mock.patch.object(server, "_get_auth_token", return_value="panel-token"):
            client = server.app.test_client()
            response = client.post(
                "/api/accounts/export?auth=panel-token",
                json={"scope": "selected", "ids": []},
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("请选择至少一个账号后再导出", response.get_json()["error"])

    def test_api_accounts_export_returns_json_array_for_all_scope(self):
        try:
            import server
        except ModuleNotFoundError as exc:
            self.skipTest(f"server import dependency missing: {exc}")

        exported = [{
            "token": "tok-export",
            "visitorid": "visitor-export",
            "name": "导出账号",
            "email": "export@example.com",
            "password": "pwd-export",
            "mail_provider": "gptmail",
            "mail_api_key": "mail-key-export",
            "quota_remain": 123,
            "enabled": True,
        }]
        with mock.patch.object(server, "_get_auth_token", return_value="panel-token"), \
             mock.patch.object(server.acm, "export_for_import", return_value=exported) as mocked_export:
            client = server.app.test_client()
            response = client.post(
                "/api/accounts/export?auth=panel-token",
                json={"scope": "all"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), exported)
        mocked_export.assert_called_once_with()

    def test_api_accounts_batch_preview_returns_summary(self):
        try:
            import server
        except ModuleNotFoundError as exc:
            self.skipTest(f"server import dependency missing: {exc}")

        preview = {
            "total": 5,
            "valid": 4,
            "missing_token": 1,
            "duplicate_token_in_payload": 1,
            "duplicate_email_in_payload": 0,
            "duplicate_token_existing": 1,
            "duplicate_email_existing": 1,
            "duplicate_candidates_total": 3,
            "importable_total": 1,
        }
        with mock.patch.object(server, "_get_auth_token", return_value="panel-token"), \
             mock.patch.object(server.acm, "preview_batch_import", return_value=preview) as mocked_preview:
            client = server.app.test_client()
            response = client.post(
                "/api/accounts/batch-preview?auth=panel-token",
                json={"accounts": [{"token": "tok"}], "dedupe": True},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["dedupe"])
        self.assertEqual(payload["would_add"], 1)
        mocked_preview.assert_called_once()

    def test_api_accounts_batch_with_dedupe_returns_skip_counts(self):
        try:
            import server
        except ModuleNotFoundError as exc:
            self.skipTest(f"server import dependency missing: {exc}")

        fake_account = mock.Mock()
        fake_account.to_status.return_value = {"id": "acc-1"}
        summary = {
            "skipped_duplicate_token": 2,
            "skipped_duplicate_email": 1,
            "duplicate_candidates_total": 3,
        }
        with mock.patch.object(server, "_get_auth_token", return_value="panel-token"), \
             mock.patch.object(server.acm, "add_batch", return_value=([fake_account], summary)) as mocked_add_batch:
            client = server.app.test_client()
            response = client.post(
                "/api/accounts/batch?auth=panel-token",
                json={"accounts": [{"token": "tok"}], "dedupe": True},
            )

        self.assertEqual(response.status_code, 201)
        payload = response.get_json()
        self.assertTrue(payload["dedupe"])
        self.assertEqual(payload["added"], 1)
        self.assertEqual(payload["skipped_duplicate_token"], 2)
        self.assertEqual(payload["skipped_duplicate_email"], 1)
        self.assertEqual(payload["duplicate_candidates_total"], 3)
        mocked_add_batch.assert_called_once_with([{"token": "tok"}], dedupe=True)


if __name__ == "__main__":
    unittest.main()

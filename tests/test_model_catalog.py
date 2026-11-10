"""Model discovery is explicit, bounded, credential-safe and never inference."""

import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from askai_cli import core, providers, setup, preferences


class TerminalText(io.StringIO):
    def isatty(self):
        return True


class CatalogTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="askai-catalog-credentials-")
        self.addCleanup(temporary.cleanup)
        store = patch("askai_cli.credentials.store_path", return_value=Path(temporary.name) / "credentials.json")
        store.start()
        self.addCleanup(store.stop)
        self.provider = {"type": "api", "base_url": "https://api.example.test/v1/",
                         "api_key_env": "CATALOG_TEST_KEY", "model": "saved-model"}
        self.secret = "FAKE_CATALOG_SECRET"
        patcher = patch.dict(os.environ, {"CATALOG_TEST_KEY": self.secret})
        patcher.start()
        self.addCleanup(patcher.stop)

    def response(self, rows):
        return {"status": 200, "body": json.dumps({"data": rows}).encode()}

    def test_discovery_is_a_get_to_the_configured_base_and_deduplicates_safe_ids(self):
        response = self.response([{"id": "flash"}, {"id": "pro"}, {"id": "flash"},
                                  {"id": "unsafe\x1b[2J"}, {"id": "has spaces"}, {"id": "x" * 257},
                                  {"id": 42}, {}, None])
        with patch.object(providers, "fetch", return_value=response) as fetch:
            self.assertEqual(providers.list_api_models(self.provider), ["flash", "pro"])
        args, kwargs = fetch.call_args
        self.assertEqual(args, ("https://api.example.test/v1/models",
                                {"Accept": "application/json", "Authorization": "Bearer " + self.secret}, b"", 10))
        self.assertEqual(kwargs, {"method": "GET"})
        self.assertEqual(self.provider["model"], "saved-model")

    def test_cli_missing_keys_and_bad_urls_never_start_transport(self):
        invalid = [{"type": "dsh"}, {**self.provider, "base_url": "https://["},
                   {**self.provider, "base_url": "https://name:password@example.test"},
                   {**self.provider, "base_url": "http://remote.example.test"},
                   {**self.provider, "api_key_env": "MISSING_CATALOG_KEY"}]
        with patch.dict(os.environ, {}, clear=True), patch.object(providers, "fetch") as fetch:
            for provider in invalid:
                with self.subTest(provider=provider), self.assertRaises(core.AskError):
                    providers.list_api_models(provider)
            fetch.assert_not_called()

    def test_catalog_cannot_echo_the_request_key_as_a_model_id(self):
        response = self.response([{"id": self.secret}, {"id": "prefix-" + self.secret}, {"id": "flash"}])
        with patch.object(providers, "fetch", return_value=response):
            self.assertEqual(providers.list_api_models(self.provider), ["flash"])
        with patch.object(providers, "fetch", return_value=self.response([{"id": self.secret}])):
            with self.assertRaises(core.AskError) as caught:
                providers.list_api_models(self.provider)
            self.assertNotIn(self.secret, str(caught.exception))

    def test_bad_status_and_payload_do_not_echo_bodies_or_modify_provider(self):
        responses = [{"status": status, "body": self.secret.encode()} for status in (302, 401, 403, 404, 429, 500)]
        responses += [{"status": 200, "body": raw} for raw in (
            b"not json", b'[]', b'{"data":{}}', b'{"data":[]}', b'{"data":[{"id":"bad id"}]}',
            ("[" * 10000 + "0" + "]" * 10000).encode())]
        responses.append(self.response([{"id": "model"}] * 5001))
        for response in responses:
            with self.subTest(status=response["status"]), patch.object(providers, "fetch", return_value=response):
                with self.assertRaises(core.AskError) as caught:
                    providers.list_api_models(self.provider)
                self.assertNotIn(self.secret, str(caught.exception))
                self.assertEqual(self.provider["model"], "saved-model")


class CatalogWizardTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="askai-model-picker-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.config = self.root / "config.toml"
        self.original = ('# preserve this comment\nprovider="dsh"\ntimeout=45\n'
                         '[providers.deepseek]\nmodel="saved-model"\n'
                         'base_url="https://api.example.test/v1"\napi_key_env="CATALOG_TEST_KEY"\n'
                         '[providers.dsh]\nmodel="original-dsh-model"\n')
        self.config.write_text(self.original)
        env = {"ASKAI_CONFIG": str(self.config), "XDG_STATE_HOME": str(self.root / "state"),
               "CATALOG_TEST_KEY": "FAKE_CATALOG_SECRET", "TERM": "dumb"}
        patcher = patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def wizard(self, answers, models=None):
        # Enter model selection from the API service page.
        if answers.startswith("3\n2\n"):
            answers = answers[:4] + "1\n" + answers[4:]
        response = {"status": 200, "body": json.dumps({"data": [{"id": value} for value in
                     (models if models is not None else ["deepseek-flash", "deepseek-v4-pro"])]}).encode()}
        output = TerminalText()
        with patch.object(providers, "fetch", return_value=response) as fetch, \
             patch.object(providers, "answer") as inference:
            result = setup.main(["--shell", "fish", "--color", "never"], TerminalText(answers), output)
            inference.assert_not_called()
        self.assertEqual(result, 0)
        return output.getvalue(), fetch

    def test_filtered_selection_saves_only_model_after_confirmation_and_returns_to_list(self):
        output, fetch = self.wizard("3\n2\n2\n/PRO\n1\ny\n0\n0\n")
        config = core.load_config()
        self.assertEqual(config["providers"]["deepseek"]["model"], "deepseek-v4-pro")
        self.assertEqual(config["provider"], "dsh")
        self.assertEqual(config["providers"]["dsh"]["model"], "original-dsh-model")
        self.assertEqual(config["timeout"], 45)
        self.assertIn("# preserve this comment", self.config.read_text())
        self.assertIn("不保证当前账号有调用权限", output)
        self.assertGreaterEqual(output.count("AI 模型与后端\n"), 2)
        self.assertNotIn("FAKE_CATALOG_SECRET", output)
        self.assertEqual(fetch.call_count, 1)
        backups = list(self.root.glob("config.toml.askai-backup-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), self.original)

    def test_common_models_are_choices_even_without_an_api_key(self):
        self.config.write_text('provider="dsh"\n')
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": ""}):
            output, fetch = self.wizard("3\n2\n2\ny\n0\n0\n")
        fetch.assert_not_called()
        self.assertIn("1 DeepSeek Flash", output)
        self.assertIn("2 DeepSeek Pro", output)
        self.assertNotIn("模型 ID（", output)
        self.assertEqual(core.load_config()["providers"]["deepseek"]["model"], "deepseek-v4-pro")
        self.assertEqual(core.load_config()["provider"], "dsh")

    def test_custom_endpoint_does_not_offer_the_original_services_presets(self):
        output, fetch = self.wizard("3\n2\n0\n0\n0\n")
        fetch.assert_not_called()
        self.assertIn("当前配置：saved-model", output)
        self.assertNotIn("DeepSeek Flash", output)
        self.assertNotIn("DeepSeek Pro", output)
        self.assertNotIn("手动输入模型 ID", output)

    def test_custom_endpoint_without_saved_model_never_offers_an_inherited_preset(self):
        self.config.write_text('provider="dsh"\n[providers.deepseek]\nbase_url="https://gateway.example.test/v1"\napi_key_env="CATALOG_TEST_KEY"\n')
        output, fetch = self.wizard("3\n2\n0\n0\n0\n")
        fetch.assert_not_called()
        self.assertIn("当前具体模型：未配置", output)
        self.assertNotIn("deepseek-flash", output)
        self.assertNotIn("DeepSeek Flash", output)
        self.assertNotIn("DeepSeek Pro", output)

    def test_manual_input_is_only_inside_advanced_settings_and_can_cancel(self):
        output, fetch = self.wizard("3\n2\n3\n0\n0\n0\n0\n")
        fetch.assert_not_called()
        self.assertGreater(output.index("手动输入模型 ID"), output.index("高级设置"))
        self.assertNotIn("模型 ID（", output)
        self.assertEqual(self.config.read_text(), self.original)

    def test_cli_current_configuration_can_be_selected_without_filling_anything(self):
        output, fetch = self.wizard("3\n3\n1\ny\n0\n0\n")
        fetch.assert_not_called()
        self.assertIn("1 沿用 DSH 当前模型", output)
        self.assertNotIn("模型 ID（", output)
        self.assertEqual(core.load_config()["providers"]["dsh"]["model"], "")
        self.assertEqual(core.load_config()["provider"], "dsh")

    def test_api_key_mistakenly_saved_as_model_is_not_displayed_as_a_choice(self):
        self.config.write_text(self.original.replace('model="saved-model"', 'model="FAKE_CATALOG_SECRET"'))
        output, fetch = self.wizard("3\n2\n0\n0\n0\n")
        fetch.assert_not_called()
        self.assertNotIn("FAKE_CATALOG_SECRET", output)
        self.assertIn("已隐藏疑似密钥", output)

    def test_advanced_input_rejects_the_current_api_key_without_saving_it(self):
        output, fetch = self.wizard("3\n2\n3\n1\nFAKE_CATALOG_SECRET\n0\n0\n0\n0\n")
        fetch.assert_not_called()
        self.assertNotIn("FAKE_CATALOG_SECRET", output)
        self.assertIn("包含敏感内容", output)
        self.assertEqual(self.config.read_text(), self.original)

    def test_cancel_list_cancel_save_and_eof_never_change_configuration(self):
        for answers in ("3\n2\n2\n0\n0\n0\n0\n", "3\n2\n2\n1\nn\n0\n0\n", "3\n2\n2\n"):
            with self.subTest(answers=answers):
                self.wizard(answers)
                self.assertEqual(self.config.read_text(), self.original)
                self.assertFalse(list(self.root.glob("config.toml.askai-backup-*")))

    def test_failed_query_allows_manual_input_without_losing_navigation(self):
        output = TerminalText()
        with patch.object(setup, "list_api_models", side_effect=core.AskError("fixture unavailable")):
            setup.main(["--shell", "fish", "--color", "never"],
                       TerminalText("3\n2\n1\n2\n3\n1\nmanual-model\ny\n0\n0\n"), output)
        self.assertIn("现有设置未改变", output.getvalue())
        self.assertEqual(core.load_config()["providers"]["deepseek"]["model"], "manual-model")

    def test_echoed_key_never_reaches_the_menu_or_saved_model(self):
        output, _ = self.wizard("3\n2\n2\n0\n0\n0\n", ["FAKE_CATALOG_SECRET", "prefix-FAKE_CATALOG_SECRET"])
        self.assertNotIn("FAKE_CATALOG_SECRET", output)
        self.assertIn("未能获取模型列表", output)
        self.assertEqual(self.config.read_text(), self.original)

    def test_failed_query_retry_reads_the_updated_connection(self):
        observed = []
        def retry(provider):
            observed.append(provider["base_url"])
            if len(observed) == 1:
                self.config.write_text(self.original.replace("api.example.test", "other.example.test"))
                raise core.AskError("fixture unavailable")
            return ["new-model"]
        with patch.object(setup, "list_api_models", side_effect=retry):
            self.wizard("3\n2\n2\n2\n1\ny\n0\n0\n")
        self.assertEqual(observed, ["https://api.example.test/v1", "https://other.example.test/v1"])
        self.assertEqual(core.load_config()["providers"]["deepseek"]["model"], "new-model")

    def test_failed_query_connection_changed_to_cli_has_no_retry_query(self):
        def changed(provider):
            self.config.write_text(self.original.replace('[providers.deepseek]\n',
                                   '[providers.deepseek]\ntype="command"\nexecutable="fixture"\n'))
            raise core.AskError("fixture unavailable")
        with patch.object(setup, "list_api_models", side_effect=changed) as query:
            output, _ = self.wizard("3\n2\n2\n0\n0\n")
        self.assertEqual(query.call_count, 1)
        self.assertIn("不支持单独的模型参数", output)
        self.assertEqual(core.load_config()["providers"]["deepseek"]["model"], "saved-model")

    def test_paging_only_accepts_visible_numbers_and_search_can_be_cleared(self):
        output, _ = self.wizard("3\n2\n2\n/no-match\nn\np\n/\n11\nn\n11\ny\n0\n0\n",
                                [f"model-{number:02d}" for number in range(1, 26)])
        self.assertIn("没有匹配的模型", output)
        self.assertIn("请输入当前页显示的编号", output)
        self.assertIn("第 2/3 页", output)
        self.assertEqual(core.load_config()["providers"]["deepseek"]["model"], "model-11")

    def test_cli_menu_never_queries_a_model_catalog(self):
        output, fetch = self.wizard("3\n3\n0\n0\n0\n")
        fetch.assert_not_called()
        self.assertIn("沿用 DSH 当前模型", output)
        self.assertEqual(self.config.read_text(), self.original)

    def test_changed_connection_after_query_cannot_receive_a_stale_model(self):
        def changed(provider):
            self.config.write_text(self.original.replace('api.example.test', 'other.example.test'))
            return ["new-model"]
        output = TerminalText()
        with patch.object(setup, "list_api_models", side_effect=changed):
            setup.main(["--shell", "fish", "--color", "never"], TerminalText("3\n2\n1\n2\n1\n0\n0\n"), output)
        self.assertEqual(core.load_config()["providers"]["deepseek"]["model"], "saved-model")
        self.assertIn("未保存旧目录", output.getvalue())

    def test_connection_change_between_selection_and_preview_is_also_rejected(self):
        plan = preferences.plan
        def changed(*args):
            self.config.write_text(self.original.replace('api.example.test', 'other.example.test'))
            return plan(*args)
        with patch.object(preferences, "plan", side_effect=changed):
            output, _ = self.wizard("3\n2\n2\n1\n0\n0\n0\n")
        self.assertIn("未保存旧目录中的型号", output)
        self.assertEqual(core.load_config()["providers"]["deepseek"]["model"], "saved-model")


if __name__ == "__main__":
    unittest.main()

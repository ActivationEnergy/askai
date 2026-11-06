"""User-visible settings, conservative TOML edits, and saved backend routing."""

import io
import os
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from askai_cli import core, preferences, setup, startup
from askai_cli.providers import cli_answer, providers, select_provider
from askai_cli.shells import SHORTCUTS


class TerminalText(io.StringIO):
    def isatty(self):
        return True


class PreferencesTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="askai-ai-settings-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = self.root / "config.toml"
        self.rc = self.root / "Config.Fish"
        for patcher in (
            patch.dict(os.environ, {"ASKAI_CONFIG": str(self.config), "XDG_STATE_HOME": str(self.root / "state")}),
            patch("askai_cli.setup.state_path", return_value=self.root / "state/askai/setup-state.json"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def wizard(self, answers):
        output = TerminalText()
        result = setup.main(["--shell", "fish", "--startup-file", str(self.rc)], TerminalText(answers), output)
        self.assertEqual(result, 0)
        return output.getvalue()

    def test_local_edit_preserves_comments_multiline_data_and_crlf(self):
        for newline in ("\n", "\r\n"):
            text = '''# user comment
provider = "auto" # root note
timeout = 45
description = """multiline
[not.a.table]
model = 'unrelated'
"""
[providers.codex]
model = 'old' # model note
args = [
  "foo", # preserve array
  "bar",
]
[providers.deepseek]
api_key_env = "MY_SECRET_ENV"
'''.replace("\n", newline)
            result = preferences.edit(text, ("providers", "codex", "model"), "new", newline)
            self.assertEqual(result, text.replace("model = 'old'", 'model = "new"'))
            result = preferences.edit(result, ("provider",), "codex", newline)
            self.assertIn('provider = "codex" # root note', result)
            result = preferences.edit(result, ("providers", "dsh", "model"), "dsh/model", newline)
            parsed = tomllib.loads(result)
            self.assertEqual(parsed["providers"]["dsh"]["model"], "dsh/model")
            self.assertEqual(parsed["providers"]["deepseek"]["api_key_env"], "MY_SECRET_ENV")

    def test_dotted_quoted_keys_and_inline_parent_safe_refusal(self):
        for text in ('providers.codex.model = "old"\n', '["providers"."codex"]\n"model"="old"\n',
                     '[providers]\ncodex.model="old"\n'):
            parsed = tomllib.loads(preferences.edit(text, ("providers", "codex", "model"), "new", "\n"))
            self.assertEqual(parsed["providers"]["codex"]["model"], "new")
        for text in ('providers = {codex = {model = "old", extra = 3}}\n',
                     '[providers]\ncodex = {model="old"}\n'):
            with self.assertRaises(core.AskError):
                preferences.edit(text, ("providers", "codex", "model"), "new", "\n")

    def test_atomic_backup_and_preview_race(self):
        original = b'# secret remains private\nprovider="auto"\n'
        self.config.write_bytes(original)
        change = preferences.plan(("provider",), "codex")
        self.config.write_text('# concurrent editor\nprovider="dsh"\n')
        with self.assertRaisesRegex(core.AskError, "预览后发生变化"):
            startup.apply([change])
        self.assertIn("concurrent editor", self.config.read_text())
        self.config.write_bytes(original)
        change = preferences.plan(("provider",), "codex")
        backups = startup.apply([change])
        self.assertEqual(Path(backups[0]).read_bytes(), original)
        self.assertEqual(Path(backups[0]).stat().st_mode & 0o777, 0o600)

    def test_menu_save_cancel_and_default_return_to_ai_list(self):
        output = self.wizard("3\n2\n4\n1\napi-test\ny\n3\n2\n1\ndsh-test\ny\nd3\ny\nd2\nn\n0\n0\n")
        config = core.load_config()
        self.assertEqual(config["provider"], "dsh")
        self.assertEqual(config["providers"]["deepseek"]["model"], "api-test")
        self.assertEqual(config["providers"]["dsh"]["model"], "dsh-test")
        self.assertEqual(output.count("askai 设置\n"), 2)
        self.assertGreaterEqual(output.count("AI 模型与后端\n"), 5)
        self.assertEqual(SHORTCUTS["askds"], "deepseek")
        self.assertEqual(SHORTCUTS["askdsh"], "dsh")
        with patch("askai_cli.providers.available", return_value=True):
            name, provider = select_provider(config, None)
        self.assertEqual(name, "dsh")
        self.assertEqual(provider["model"], "dsh-test")

    def test_cancel_eof_and_invalid_choices_leave_files_untouched(self):
        for answers in ("3\nd3\nn\n0\n0\n", "3\nd3\n", "3\n1\n1\n", "nonsense\n0\n"):
            self.wizard(answers)
            self.assertFalse(self.config.exists())
            self.assertFalse(self.rc.exists())

    def test_api_and_cli_reset_and_restore_automatic_default(self):
        self.config.write_text('provider="dsh"\n[providers.codex]\nmodel="custom"\n[providers.deepseek]\nmodel="api-custom"\n')
        self.wizard("3\n1\n1\ny\n2\n1\ny\nd8\ny\n0\n0\n")
        config = core.load_config()
        self.assertEqual(config["provider"], "auto")
        self.assertEqual(config["providers"]["codex"]["model"], "")
        self.assertEqual(config["providers"]["deepseek"]["model"], providers({})["deepseek"]["model"])

    def test_failure_can_return_to_ai_list_and_continue(self):
        with patch("askai_cli.setup.startup.apply", side_effect=core.AskError("disk full")):
            output = self.wizard("3\nd3\ny\n0\n8\n0\n0\n")
        self.assertIn("disk full", output)
        self.assertGreaterEqual(output.count("AI 模型与后端\n"), 3)
        self.assertFalse(self.config.exists())

    def test_status_is_readonly_and_environment_override_not_secret(self):
        with patch.dict(os.environ, {"ASKAI_MODEL": "private-model", "DEEPSEEK_API_KEY": "SECRET-NEVER-PRINT"}):
            output = TerminalText()
            setup.main(["--status", "--shell", "fish", "--startup-file", str(self.rc)], TerminalText(), output)
        self.assertIn("ASKAI_MODEL 环境变量会覆盖", output.getvalue())
        self.assertNotIn("SECRET-NEVER-PRINT", output.getvalue())
        self.assertFalse(self.config.exists())
        self.assertFalse(self.root.joinpath("state").exists())

    def test_shell_menu_continues_without_changing_login_shell(self):
        output = self.wizard("2\n2\n1\n0\n0\n")
        self.assertIn("现在设置 zsh", output)
        self.assertIn("现在设置 fish", output)
        self.assertFalse(self.rc.exists())

    def test_dsh_adapter_policy_uses_patch_without_unsupported_flags(self):
        observed = {}
        def fake_run(argv, payload, timeout, trace=None):
            observed["argv"] = argv
            observed["policy"] = Path(argv[argv.index("--patch") + 1]).read_text()
            observed["payload"] = payload
            return "final answer"
        with patch("askai_cli.providers.shutil.which", return_value="/fake/dsh"), \
             patch("askai_cli.providers.run_cli", side_effect=fake_run):
            self.assertEqual(cli_answer({"type": "dsh", "executable": "dsh"}, 'model/test', "system", "question", 10), "final answer")
        self.assertNotIn("--permission-mode", observed["argv"])
        self.assertNotIn("--approval-policy", observed["argv"])
        self.assertIn("defaultPreset: read-only", observed["policy"])
        self.assertIn("policy: never", observed["policy"])
        self.assertIn('model: "model/test"', observed["policy"])
        self.assertEqual(observed["payload"], "question")
        self.assertIn('personaPrefix: "system"', observed["policy"])


if __name__ == "__main__":
    unittest.main()

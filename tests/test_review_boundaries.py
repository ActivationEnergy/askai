"""Reproduce the 2026-11-05 review's boundaries without user state or AI calls."""

import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from askai_cli import core, providers, scripts, setup, startup
from askai_cli.debug import DebugTrace
import test_ask as fixtures


class TerminalText(io.StringIO):
    def isatty(self):
        return True


class ReviewBoundaries(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="askai-review-boundary-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()


    def test_cli_stdout_is_strict_but_stderr_diagnostics_can_decode_lossily(self):
        for stream in ("stdout", "stderr"):
            code = "import sys;sys.stdin.buffer.read();sys.stdout.buffer.write(b'valid');sys." + stream + ".buffer.write(bytes([255]))"
            with self.subTest(stream=stream):
                if stream == "stdout":
                    with self.assertRaisesRegex(core.AskError, "UTF-8"):
                        providers.run_cli([sys.executable, "-I", "-c", code], "输入é", 5)
                else:
                    self.assertEqual(providers.run_cli([sys.executable, "-I", "-c", code], "输入é", 5), "valid")
        secret = "FAKE_BAD_ENCODING_SECRET"
        code = "import sys;sys.stderr.buffer.write(b'\\xffAuthorization: Bearer " + secret + "');sys.exit(7)"
        with self.assertRaises(core.AskError) as caught:
            providers.run_cli([sys.executable, "-I", "-c", code], "", 5)
        self.assertIn("退出码 7", str(caught.exception))
        self.assertNotIn(secret, str(caught.exception))

    def test_cli_rejects_bad_request_encoding_before_starting_process(self):
        with patch("askai_cli.providers.subprocess.Popen") as process, self.assertRaisesRegex(core.AskError, "未启动"):
            providers.run_cli(["unused"], "\ud800", 5)
        process.assert_not_called()

    def test_codex_final_file_also_rejects_invalid_utf8(self):
        def fake_cli(argv, payload, timeout, trace=None):
            if "mcp" in argv:
                return "[]"
            Path(argv[argv.index("-o") + 1]).write_bytes(b"\xff")
            return ""
        with patch("askai_cli.providers.run_cli", side_effect=fake_cli), self.assertRaisesRegex(core.AskError, "最终响应文件"):
            providers.cli_answer({"type": "codex", "executable": sys.executable}, None, "system", "question", 5)

    def test_bad_stdout_has_no_candidate_or_traceback_in_ordinary_cli(self):
        config = self.root / "config.toml"
        config.write_text('provider="fixture"\n[providers.fixture]\ntype="command"\nexecutable=' + json.dumps(sys.executable) + '\nargs=["-I","-c","import sys;sys.stdout.buffer.write(bytes([255]))"]\n')
        selected = self.root / "selected"
        selected.write_text("stale command")
        env = {**os.environ, "ASKAI_CONFIG": str(config), "ASKAI_PROVIDER": "fixture", "ASKAI_NO_SETUP": "1", "ASKAI_COMMAND_FILE": str(selected), "ASKAI_WRAPPER_PROTOCOL": "2"}
        result = subprocess.run([str(fixtures.ASKAI), "--print", "fixture question"], env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("UTF-8", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(selected.read_text(), "")

    def test_process_lookup_races_preserve_timeout_and_interrupt(self):
        for interrupted, force_kill in ((False, False), (False, True), (True, False)):
            process = MagicMock()
            process.pid = 12345  # No actual PID is signalled: killpg is mocked.
            first = KeyboardInterrupt() if interrupted else subprocess.TimeoutExpired(["fixture"], .1)
            process.wait.side_effect = [subprocess.TimeoutExpired(["fixture"], 2), None] if force_kill else [None, None]
            expected = KeyboardInterrupt if interrupted else core.AskError
            with self.subTest(interrupted=interrupted, force_kill=force_kill), \
                 patch("askai_cli.providers.subprocess.Popen") as popen, \
                 patch("askai_cli.providers._communicate_bounded", side_effect=first) as communicate, \
                 patch("askai_cli.providers.os.killpg", side_effect=ProcessLookupError) as signal_group, \
                 patch("sys.stderr", io.StringIO()), self.assertRaises(expected) as caught:
                popen.return_value.__enter__.return_value = process
                providers.run_cli(["fixture"], "request", .1, DebugTrace(True))
            self.assertEqual(communicate.call_count, 1)
            self.assertEqual(process.wait.call_count, 2)
            self.assertEqual(signal_group.call_count, 2)
            if not interrupted:
                self.assertIn("超时", str(caught.exception))

    def test_api_key_env_is_validated_in_config_adapter_and_ordinary_cli(self):
        config = self.root / "config.toml"
        for value in (123, True, ["NAME"], "", "BAD=NAME"):
            with self.subTest(value=value):
                config.write_text('[providers.fixture]\ntype="command"\nexecutable=' + json.dumps(sys.executable) + '\napi_key_env=' + json.dumps(value) + '\n')
                with self.assertRaisesRegex(core.AskError, "api_key_env"):
                    core.load_config(config)
                with patch("askai_cli.providers.cli_answer") as backend, self.assertRaises(core.AskError):
                    providers.answer({"type": "command", "api_key_env": value}, None, "", "", 5)
                backend.assert_not_called()
        config.write_text('provider="fixture"\n[providers.fixture]\ntype="command"\nexecutable=' + json.dumps(sys.executable) + '\napi_key_env=123\n')
        env = {**os.environ, "ASKAI_CONFIG": str(config), "ASKAI_PROVIDER": "fixture", "ASKAI_NO_SETUP": "1"}
        env.pop("ASKAI_COMMAND_FILE", None)
        result = subprocess.run([str(fixtures.ASKAI), "--print", "fixture question"], env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertIn("api_key_env", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_empty_explicit_config_is_rejected_but_relative_choice_is_retained(self):
        with patch.dict(os.environ, {"ASKAI_CONFIG": ""}), self.assertRaisesRegex(core.AskError, "不能为空"):
            core.config_path()
        with patch.dict(os.environ, {"ASKAI_CONFIG": "chosen/config.toml"}):
            self.assertEqual(core.config_path(), Path("chosen/config.toml"))

    def test_zdotdir_auto_location_never_guesses_home_for_native_relative_values(self):
        self.assertEqual(startup.default_paths("zsh", self.home, {}), [self.home / ".zshrc"])
        self.assertEqual(startup.default_paths("zsh", self.home, {"ZDOTDIR": str(self.home)}), [self.home / ".zshrc"])
        for value in ("", "relative", "~/relative", "/bad\0path"):
            with self.subTest(value=repr(value)), self.assertRaisesRegex(core.AskError, "ZDOTDIR"):
                startup.default_paths("zsh", self.home, {"ZDOTDIR": value})

    @unittest.skipUnless(shutil.which("zsh"), "zsh is not installed")
    def test_unknown_zdotdir_can_be_configured_with_an_explicit_path_in_wizard(self):
        target = self.root / "chosen/.zshrc"
        config = self.root / "model.toml"
        config.write_text('provider="auto"\n')
        env = {"HOME": str(self.home), "ASKAI_CONFIG": str(config), "XDG_STATE_HOME": str(self.root / "state"), "ZDOTDIR": "relative"}
        answers = TerminalText("2\n4\n" + str(target) + "\n\ny\n0\n0\n")
        output = TerminalText()
        with patch.dict(os.environ, env):
            self.assertEqual(setup.main(["--shell", "zsh", "--color=never"], answers, output), 0)
        self.assertIsNotNone(startup.locate(target.read_text()))
        self.assertIn("确认Shell会读取该文件", output.getvalue())
        self.assertNotIn("Traceback", output.getvalue())
        self.assertFalse((self.root / ".zshrc").exists())

    def test_unknown_zdotdir_status_is_reported_without_claiming_unconfigured(self):
        output = io.StringIO()
        with patch.dict(os.environ, {"ZDOTDIR": ""}):
            setup.show_status(["zsh"], {"locations": {}}, output)
        self.assertIn("无法自动定位", output.getvalue())
        self.assertNotIn("未配置", output.getvalue())

    def test_unknown_zdotdir_path_edit_can_cancel_without_writing(self):
        config = self.root / "model.toml"
        config.write_text('provider="auto"\n')
        state = self.root / "state"
        with patch.dict(os.environ, {"ASKAI_CONFIG": str(config), "ZDOTDIR": "", "XDG_STATE_HOME": str(state)}):
            output = TerminalText()
            self.assertEqual(setup.main(["--shell", "zsh", "--color=never"], TerminalText("2\n4\n\n0\n0\n"), output), 0)
        self.assertIn("已取消路径修改", output.getvalue())
        self.assertFalse(state.exists())
        self.assertFalse((self.home / ".zshrc").exists())

    def test_bom_is_rejected_on_all_script_entries_but_no_bom_utf8_sig_is_valid(self):
        source = "\ufeffprint('must not run')\n"
        item = fixtures.python_script(source)
        target = self.root / "bom.py"
        operations = [lambda: core.parse_answer(json.dumps(fixtures.response(commands=[item]))),
                      lambda: core.validate_python_encoding(source), lambda: scripts.validate_candidate(item, "fish"),
                      lambda: scripts.standalone_command(item, "fish"), lambda: scripts.run_script(item),
                      lambda: scripts.save_script(item, str(target), "fish")]
        for operation in operations:
            with self.assertRaisesRegex(core.AskError, "BOM"):
                operation()
        self.assertFalse(target.exists())
        valid = fixtures.python_script('# coding: utf-8-sig\nprint("é 中文")\n')
        scripts.validate_candidate(valid, "bash")
        saved, _ = scripts.save_script(valid, str(self.root / "valid.py"), "bash")
        command = scripts.standalone_command(valid, "bash")
        inline = subprocess.run(["bash", "--noprofile", "--norc", "-c", command], capture_output=True, text=True)
        stored = subprocess.run([sys.executable, str(saved)], capture_output=True, text=True)
        for result in (inline, stored):
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "é 中文\n")


if __name__ == "__main__":
    unittest.main()

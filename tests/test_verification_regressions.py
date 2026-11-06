"""Controlled regressions for the second review; no live models or user files."""

import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from askai_cli import core, providers, scripts, setup, startup
from askai_cli.debug import DebugTrace


class VerificationRegressions(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="askai-verification-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()

    def test_invalid_api_urls_fail_before_transport_without_echoing_credentials(self):
        for base in ("https://[", "https://", "https://host:bad", "https://host:99999",
                     "https://user:FAKE_SECRET@host", "https://host/?token=FAKE_SECRET", "https://host/\n"):
            with self.subTest(base=base), patch.object(providers, "fetch") as transport:
                with self.assertRaisesRegex(core.AskError, "base_url") as caught:
                    providers.api_answer({"base_url": base, "model": "fixture"}, None, "", "", 1)
                self.assertNotIn("FAKE_SECRET", str(caught.exception))
                transport.assert_not_called()

    def test_explicit_bad_paths_are_controlled_at_all_three_entries(self):
        for destination in ("~askai_nonexistent_user_78264/x", "bad\0path"):
            with self.subTest(destination=repr(destination)):
                with patch.object(core.os, "environ", {"ASKAI_CONFIG": destination}), self.assertRaises(core.AskError):
                    core.config_path()
                with self.assertRaises(core.AskError):
                    scripts.save_script({"code": "print(1)"}, destination, "bash")
                config = self.root / "config.toml"
                config.write_text("")
                with patch.dict(os.environ, {"ASKAI_CONFIG": str(config)}), self.assertRaises(core.AskError):
                    setup.main(["--status", "--startup-file", destination], io.StringIO(), io.StringIO())
        with patch.dict(os.environ, {"ASKAI_CONFIG": "relative/config.toml"}):
            self.assertEqual(core.config_path(), Path("relative/config.toml"))

    def test_invalid_provider_type_fails_at_config_boundary(self):
        config = self.root / "config.toml"
        for value in ("[]", "{}", "true", "1"):
            config.write_text("provider = " + value)
            with self.subTest(value=value), self.assertRaisesRegex(core.AskError, "provider.*字符串"):
                core.load_config(config)

    def test_ordinary_cli_returns_clean_errors_and_empty_stdout(self):
        executable = Path(__file__).resolve().parents[1] / "bin/askai"
        config = self.root / "config.toml"
        env = {**os.environ, "ASKAI_CONFIG": str(config), "ASKAI_PROVIDER": "fixture",
               "ASKAI_NO_SETUP": "1", "FAKE_ASKAI_TEST_KEY": "fake", "XDG_STATE_HOME": str(self.root / "state")}
        env.pop("ASKAI_COMMAND_FILE", None)
        fixtures = [('provider=[]', ["providers"]),
                    ('[providers.fixture]\ntype="api"\nbase_url="https://["\nmodel="fake"\napi_key_env="FAKE_ASKAI_TEST_KEY"', ["--print", "fixture question"])]
        for text, arguments in fixtures:
            config.write_text(text)
            result = subprocess.run([str(executable), *arguments], env=env, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stdout, "")
            self.assertNotIn("Traceback", result.stderr)
        env["ASKAI_CONFIG"] = "~askai_nonexistent_user_78264/config.toml"
        result = subprocess.run([str(executable), "providers"], env=env, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertNotIn("Traceback", result.stderr)

    def test_deep_input_is_controlled_at_config_state_and_api_boundaries(self):
        config = self.root / "config.toml"
        config.write_text("x = " + "[" * 2000 + "0" + "]" * 2000)
        with self.assertRaises(core.AskError):
            core.load_config(config)
        state = self.root / "askai/setup-state.json"
        state.parent.mkdir()
        state.write_text("[" * 20000 + "0" + "]" * 20000)
        state.chmod(0o600)
        with patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root)}), self.assertRaises(core.AskError):
            setup.read_state()
        response = {"status": 200, "body": ("[" * 50000 + "0" + "]" * 50000).encode()}
        with patch.object(providers, "fetch", return_value=response), self.assertRaises(core.AskError):
            providers.api_answer({"base_url": "https://example.invalid", "model": "fixture"}, None, "", "", 1)

    def test_link_loop_after_preview_has_standard_report_and_keeps_recovery_index(self):
        target = self.root / "startup"
        target.write_text("original\n")
        alias = self.root / "alias"
        alias.symlink_to(target)
        change = startup.Change([alias], target, b"original\n", b"new\n", None)
        alias.unlink()
        alias.symlink_to(alias)
        with patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state")}):
            with self.assertRaises(core.AskError) as caught:
                setup.commit_changes([change], "bash")
            message = str(caught.exception)
            self.assertIn("已变更: 无", message)
            self.assertIn("备份: 无", message)
            self.assertIn("位置记录已保留", message)
            self.assertIn(str(alias), setup.read_state()["locations"]["bash"])
        self.assertEqual(target.read_text(), "original\n")

    def test_cli_caps_both_streams_and_handles_full_duplex_input(self):
        # Child fills output before reading input: sequential pipe reads deadlock.
        code = "import sys;sys.stdout.buffer.write(b'x'*65536);sys.stdout.flush();data=sys.stdin.buffer.read();sys.stderr.buffer.write(data)"
        self.assertEqual(providers.run_cli([sys.executable, "-I", "-c", code], "a" * 100000, 5), "x" * 65536)
        for stream in ("stdout", "stderr"):
            code = f"import sys,time;sys.{stream}.buffer.write(b'x'*65536);sys.{stream}.flush();time.sleep(30)"
            with self.subTest(stream=stream), patch.object(providers, "MAX_RESPONSE_BYTES", 1024):
                with self.assertRaisesRegex(core.AskError, stream + ".*上限"):
                    providers.run_cli([sys.executable, "-I", "-c", code], "", 5)
        code = "import sys;sys.stdin.close();print('ok')"
        self.assertEqual(providers.run_cli([sys.executable, "-I", "-c", code], "x" * 100000, 5), "ok\n")

    def test_partial_write_keeps_both_locations_and_reports_actual_completed_file(self):
        paths = [self.root / "first", self.root / "second"]
        changes = []
        for path in paths:
            path.write_bytes(b"original\n")
            changes.append(startup.Change([path], path, b"original\n", b"updated\n", startup.identity(path.stat())))
        replace = os.replace
        def fail_second(source, destination):
            if Path(destination) == paths[1]:
                raise OSError("fixture disk failure")
            return replace(source, destination)
        with patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state")}), patch.object(startup.os, "replace", side_effect=fail_second):
            with self.assertRaises(core.AskError) as caught:
                setup.commit_changes(changes, "bash")
            self.assertIn("已变更: " + str(paths[0]), str(caught.exception))
            self.assertIn("备份:", str(caught.exception))
            self.assertEqual(setup.read_state()["locations"]["bash"], sorted(map(str, paths)))
        self.assertEqual(paths[0].read_bytes(), b"updated\n")
        self.assertEqual(paths[1].read_bytes(), b"original\n")
        for change in changes:
            self.assertEqual(change.backup.read_bytes(), b"original\n")

    def test_codex_and_parser_limits_precede_decoding_and_json_parsing(self):
        def fake_cli(argv, payload, timeout, trace=None):
            if "mcp" in argv:
                return "[]"
            Path(argv[argv.index("-o") + 1]).write_bytes(b"x" * 1025)
            return ""
        with patch.object(providers, "run_cli", side_effect=fake_cli), patch.object(providers, "MAX_RESPONSE_BYTES", 1024):
            with self.assertRaisesRegex(core.AskError, "最终响应文件.*上限"):
                providers.cli_answer({"type": "codex", "executable": sys.executable}, None, "", "", 5)
        with patch.object(core, "MAX_RESPONSE_BYTES", 1024), patch.object(core.json, "loads") as loads:
            with self.assertRaisesRegex(core.AskError, "上限"):
                core.parse_answer("中" * 400)
            loads.assert_not_called()

    def test_diagnostic_redacts_secret_before_truncation(self):
        secret = "FAKE_SECRET_" + "a" * 4000
        code = "import sys;sys.stderr.write('Authorization: Bearer '+sys.stdin.read());sys.exit(7)"
        with self.assertRaises(core.AskError) as caught:
            providers.run_cli([sys.executable, "-I", "-c", code], secret, 5, DebugTrace(secrets=[secret]))
        self.assertNotIn("a" * 50, str(caught.exception))
        self.assertIn("退出码 7", str(caught.exception))


if __name__ == "__main__":
    unittest.main()

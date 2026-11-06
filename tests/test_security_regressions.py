"""Concrete review regressions: isolated paths, fake CLIs and harmless scripts."""

import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from askai_cli import __version__, core, providers, scripts, setup, shells, startup
from askai_cli.cli import choose, request_suggestions
from askai_cli.debug import DebugTrace
from askai_cli.terminal import ColorPolicy, Terminal
import test_ask as fixtures


class TerminalText(io.StringIO):
    def isatty(self):
        return True


class SecurityRegressions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="askai-security-fix-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()

    def test_all_shell_runners_ignore_project_and_pythonpath_modules(self):
        project = self.root / "project"
        project.mkdir()
        marker = self.root / "unexpected-import"
        trap = "from pathlib import Path\nPath(" + repr(str(marker)) + ").write_text('executed')\n"
        for name in ("json.py", "unicodedata.py", "sitecustomize.py"):
            (project / name).write_text(trap)
        env = {**os.environ, "PYTHONPATH": str(project), "HOME": str(self.home), "ASKAI_NO_SETUP": "1"}
        for shell in core.SHELLS:
            if not shutil.which(shell):
                continue
            with self.subTest(shell=shell):
                init = self.root / ("init." + shell)
                text = shells.integration(shell, hook=True)
                init.write_text(text)
                self.assertIn(" -I -c ", text)
                flags = {"fish": ["--no-config"], "zsh": ["-f"], "bash": ["--noprofile", "--norc"]}[shell]
                result = subprocess.run([shell, *flags, "-c", f"source {shlex.quote(str(init))}; askai --version"],
                                        env=env, cwd=project, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(__version__, result.stdout)
                self.assertFalse(marker.exists())
                if shell in ("zsh", "bash"):
                    # Exercise the Han probe without making a model request.
                    result = subprocess.run([shell, *flags, "-i", "-c", f"source {shlex.quote(str(init))}; 查CPU"],
                                            env=env, cwd=project, capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, 127, result.stderr)
                    self.assertFalse(marker.exists())

    def test_xdg_empty_and_relative_values_do_not_read_project_configuration(self):
        original = Path.cwd()
        self.addCleanup(os.chdir, original)
        os.chdir(self.root)
        (self.root / "askai").mkdir()
        (self.root / "askai/config.toml").write_text('provider="untrusted-project"\n')
        for value in ("", "relative", "~/relative"):
            with self.subTest(value=value), patch.dict(os.environ, {"XDG_CONFIG_HOME": value, "XDG_STATE_HOME": value}), \
                 patch("pathlib.Path.home", return_value=self.home), patch.dict(os.environ):
                os.environ.pop("ASKAI_CONFIG", None)
                self.assertEqual(core.config_path(), self.home / ".config/askai/config.toml")
                self.assertEqual(core.load_config(), {})
                self.assertEqual(startup.default_paths("fish", home=self.home), [self.home / ".config/fish/config.fish"])
                self.assertEqual(setup.state_path(), self.home / ".local/state/askai/setup-state.json")
        with patch.dict(os.environ, {"ASKAI_CONFIG": "askai/config.toml"}):
            self.assertEqual(core.load_config()["provider"], "untrusted-project")  # Explicit user choice.

    def test_failure_diagnostics_are_redacted_with_or_without_debug(self):
        executable = self.root / "fake-cli"
        secret = "FAKE_REVIEW_SECRET_DO_NOT_USE"
        executable.write_text(f"#!{sys.executable}\nimport sys\nprint('Authorization: Bearer {secret}',file=sys.stderr)\nsys.exit(7)\n")
        executable.chmod(0o755)
        for trace in (None, DebugTrace(False), DebugTrace(True)):
            with self.subTest(trace=trace), patch("sys.stderr", io.StringIO()), self.assertRaises(core.AskError) as caught:
                providers.run_cli([str(executable)], "", 5, trace)
            self.assertNotIn(secret, str(caught.exception))
            self.assertIn("REDACTED", str(caught.exception))
        for enabled in (False, True):
            with patch("askai_cli.providers.cli_answer", side_effect=core.AskError("Bearer " + secret)), \
                 patch("sys.stderr", io.StringIO()), self.assertRaises(core.AskError) as caught:
                providers.answer({"type": "command"}, None, "", "", 5, DebugTrace(enabled))
            self.assertNotIn(secret, str(caught.exception))


    def test_long_failure_is_redacted_before_display_truncation(self):
        secret = "sk-" + "A" * 37
        executable = self.root / "long-error-cli"
        diagnostic = "Authorization: Bearer " + secret + "x" * 2963
        executable.write_text(f"#!{sys.executable}\nimport sys\nsys.stderr.write({diagnostic!r})\nsys.exit(7)\n")
        executable.chmod(0o755)
        with self.assertRaises(core.AskError) as caught:
            providers.run_cli([str(executable)], "", 5, DebugTrace(False, secrets=[secret]))
        self.assertNotIn(secret[3:], str(caught.exception))

    def test_codex_invalid_tool_metadata_cannot_start_a_model_request(self):
        for metadata in ("{}", '[{"name":null}]', "not json"):
            with self.subTest(metadata=metadata), patch("askai_cli.providers.run_cli", return_value=metadata) as run, self.assertRaises(core.AskError):
                providers.cli_answer({"type": "codex", "executable": sys.executable}, None, "", "", 5)
            self.assertEqual(run.call_count, 1)
            self.assertIn("mcp", run.call_args.args[0])
            self.assertNotIn("exec", run.call_args.args[0])

    @unittest.skipUnless(shutil.which("codex"), "Codex not installed; no model call is substituted")
    def test_actual_codex_config_disables_special_named_mcp_and_native_local_features(self):
        home = self.root / "isolated-codex"
        home.mkdir()
        marker = self.root / "mcp-must-not-start"
        config = ""
        for name in ("fixture", "foo.bar", 'quote"name'):
            config += '[mcp_servers.' + json.dumps(name) + ']\ncommand=' + json.dumps(sys.executable) + '\nargs=["-c",' + json.dumps('from pathlib import Path;Path(' + repr(str(marker)) + ').touch()') + ']\n'
        (home / "config.toml").write_text(config)
        with patch.dict(os.environ, {"CODEX_HOME": str(home)}):
            flags = providers._codex_policy(shutil.which("codex"), 5)
            servers = subprocess.run([shutil.which("codex"), *flags, "mcp", "list", "--json"], capture_output=True, text=True, timeout=10)
            self.assertEqual(servers.returncode, 0, servers.stderr)
            self.assertTrue(all(not s["enabled"] for s in json.loads(servers.stdout)))
            features = subprocess.run([shutil.which("codex"), *flags, "features", "list"], capture_output=True, text=True, timeout=10)
            self.assertEqual(features.returncode, 0, features.stderr)
            entries = {line.split()[0]: line.split()[-1] for line in features.stdout.splitlines() if line.split()}
            for feature in ("shell_tool", "view_image", "apps", "plugins", "multi_agent", "hooks"):
                self.assertEqual(entries[feature], "false")
        self.assertFalse(marker.exists())



    def test_surrogates_in_all_model_fields_are_controlled_format_errors(self):
        samples = []
        for field in ("explanation", "label", "warning", "details", "command"):
            value = fixtures.response()
            if field == "explanation":
                value[field] = "\ud800"
            else:
                value["commands"][0][field] = "\udc00"
            samples.append(value)
        samples.append(fixtures.response(commands=[fixtures.python_script("\ud800")]))
        samples.append({**fixtures.response(), "\ud800": "unknown metadata"})
        samples.append({**fixtures.response(), "extra": ["\ud800"]})
        for value in samples:
            raw = json.dumps(value)
            with self.subTest(value=repr(value)), self.assertRaisesRegex(core.AskError, "Unicode"):
                core.parse_answer(raw)
            with patch("askai_cli.cli.answer", side_effect=[raw, json.dumps(fixtures.response())]) as backend:
                repaired = request_suggestions({}, None, "system", "question", 5, DebugTrace(),
                                               Terminal(io.StringIO(), ColorPolicy("never")))
            self.assertEqual(repaired, fixtures.response())
            self.assertEqual(backend.call_count, 2)

    def test_nonstandard_constants_and_numeric_overflow_trigger_format_repair(self):
        for constant in ("NaN", "Infinity", "-Infinity", "1e999", "-1e999"):
            raw = json.dumps(fixtures.response())[:-1] + ', "extra": [' + constant + ']}'
            with self.subTest(constant=constant), self.assertRaises(core.AskError):
                core.parse_answer(raw)
            with patch("askai_cli.cli.answer", side_effect=[raw, json.dumps(fixtures.response())]) as backend:
                result = request_suggestions({}, None, "system", "question", 5, DebugTrace(),
                                            Terminal(io.StringIO(), ColorPolicy("never")))
            self.assertEqual(result, fixtures.response())
            self.assertEqual(backend.call_count, 2)
            json.dumps(result, allow_nan=False)

    def test_invalid_menu_numbers_continue_in_candidate_and_settings_menus(self):
        malformed = "²\n" + "9" * 5000 + "\n"
        self.assertEqual(choose(fixtures.response(), io.StringIO(malformed + "q\n"), io.StringIO()), ("cancel", None))
        config = self.root / "config.toml"
        config.write_text('provider="auto"\n')
        args = ["--shell", "fish", "--color=never", "--startup-file", str(self.root / "unused.fish")]
        with patch.dict(os.environ, {"ASKAI_CONFIG": str(config)}):
            output = TerminalText()
            self.assertEqual(setup.main(args, TerminalText("3\n" + malformed + "0\n0\n"), output), 0)
        self.assertFalse((self.root / "unused.fish").exists())

    def test_reset_cli_model_never_restores_a_different_api_preset(self):
        config = self.root / "config.toml"
        config.write_text('[providers.deepseek]\ntype="codex"\nmodel="custom-cli-model"\n')
        args = ["--shell", "fish", "--color=never", "--startup-file", str(self.root / "unused.fish")]
        with patch.dict(os.environ, {"ASKAI_CONFIG": str(config), "XDG_STATE_HOME": str(self.root / "state")}):
            self.assertEqual(setup.main(args, TerminalText("3\n2\n1\ny\n0\n0\n"), TerminalText()), 0)
            self.assertEqual(core.load_config()["providers"]["deepseek"]["model"], "")

    def test_incompatible_source_encodings_are_rejected_on_every_path(self):
        marker = self.root / "encoding-marker"
        code = '# coding: unicode_escape\n# harmless' + r'\n' + 'from pathlib import Path' + r'\n' + 'Path(' + repr(str(marker)) + ').touch()\n'
        for source in (code, '# coding: latin-1\nprint("é")\n', '# coding: utf-7\nprint("hi")\n'):
            item = fixtures.python_script(source)
            with self.subTest(source=source):
                for operation in (lambda: core.parse_answer(json.dumps(fixtures.response(commands=[item]))),
                                  lambda: scripts.validate_candidate(item, "fish"),
                                  lambda: scripts.standalone_command(item, "fish"),
                                  lambda: scripts.run_script(item),
                                  lambda: scripts.save_script(item, str(self.root / "saved.py"), "fish")):
                    with self.assertRaises(core.AskError):
                        operation()
                self.assertFalse(marker.exists())
                self.assertFalse((self.root / "saved.py").exists())

    def test_utf8_source_has_identical_temporary_inline_and_saved_behavior(self):
        code = '# coding: utf-8\nimport __main__\nvalue="é 中文"\nprint(__main__.value)\n'
        item = fixtures.python_script(code)
        scripts.validate_candidate(item, "bash")
        command = scripts.standalone_command(item, "bash")
        copied = subprocess.run(["bash", "--noprofile", "--norc", "-c", command], capture_output=True, text=True)
        saved, _ = scripts.save_script(item, str(self.root / "saved.py"), "bash")
        stored = subprocess.run([sys.executable, str(saved)], capture_output=True, text=True)
        runner = "import sys;sys.path.insert(0," + repr(str(fixtures.ROOT / "src")) + ");from askai_cli.scripts import run_script;raise SystemExit(run_script(" + repr(item) + "))"
        temporary = subprocess.run([sys.executable, "-I", "-c", runner], capture_output=True, text=True)
        for result in (copied, stored, temporary):
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "é 中文\n")

    def test_script_bootstrap_never_imports_unapproved_project_pkgutil(self):
        project = self.root / "project"
        project.mkdir()
        marker = self.root / "unexpected-pkgutil"
        (project / "pkgutil.py").write_text("from pathlib import Path\nPath(" + repr(str(marker)) + ").touch()\ndef get_importer(path): return None\ndef read_code(stream): return None\n")
        item = fixtures.python_script("print('approved source')\n")
        runner = "import sys;sys.path.insert(0," + repr(str(fixtures.ROOT / "src")) + ");from askai_cli.scripts import run_script;raise SystemExit(run_script(" + repr(item) + "))"
        result = subprocess.run([sys.executable, "-I", "-c", runner], cwd=project, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "approved source\n")
        self.assertFalse(marker.exists())

    def test_debug_nested_content_is_bounded_and_keys_are_redacted_after_json_decode(self):
        secret = "FAKE_REVIEW_SECRET_DO_NOT_USE"
        encoded_key = "".join("\\u%04x" % ord(c) for c in secret)
        encoded = '{"' + encoded_key + '":"fixture value"}'
        deep = '{"extra":' + '[' * 2000 + '"PRIVATE_DEEP_VALUE"' + ']' * 2000 + '}'
        output = io.StringIO()
        with patch("sys.stderr", output):
            trace = DebugTrace(True, secrets=[secret])
            trace.emit("HTTP raw response", {"content": encoded})
            trace.emit("AI raw response", {"content": deep})
            trace.emit("fixture", {"wide": list(range(4000))})
        rendered = output.getvalue()
        self.assertNotIn(secret, rendered)
        self.assertNotIn("PRIVATE_DEEP_VALUE", rendered)
        self.assertIn("REDACTED", rendered)
        self.assertIn("已省略", rendered)
        self.assertLess(len(rendered), 100000)
        output = io.StringIO()
        with patch("sys.stderr", output), patch("askai_cli.debug.json.loads", side_effect=RecursionError):
            DebugTrace(True).emit("AI raw response", {"content": '{"value":"PRIVATE_PARSE_OVERFLOW"}'})
        self.assertNotIn("PRIVATE_PARSE_OVERFLOW", output.getvalue())
        self.assertIn("已省略", output.getvalue())


if __name__ == "__main__":
    unittest.main()

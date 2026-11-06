"""Color behavior and command-data boundaries, without real model requests."""

import contextlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from askai_cli import setup, terminal
from askai_cli.cli import run
from askai_cli.debug import DebugTrace
import test_ask as fixtures
from test_ask import ASKAI, ROOT, python_script, response, terminal as pty_terminal


SGR = re.compile(r"\x1b\[[0-9;]*m")


class Stream(io.StringIO):
    def __init__(self, tty=True):
        super().__init__()
        self.tty = tty

    def isatty(self):
        return self.tty

    def fileno(self):
        return 10


class ColorTests(unittest.TestCase):
    def setUp(self):
        token = terminal._policy.set(None)
        self.addCleanup(terminal._policy.reset, token)
        env = patch.dict(os.environ, {"TERM": "xterm-256color", "NO_COLOR": ""})
        env.start()
        self.addCleanup(env.stop)

    def test_precedence_and_independent_streams(self):
        cases = [
            (None, None, "", "xterm", True, True),
            (None, None, "1", "xterm", True, False),
            (None, "always", "1", "dumb", False, True),
            (None, "never", "", "xterm", True, False),
            ("auto", "always", "1", "xterm", True, False),
            ("auto", "never", "", "xterm", True, True),
            ("always", "never", "1", "dumb", False, True),
            ("never", "always", "", "xterm", True, False),
            (None, "auto", "", "xterm", False, False),
            (None, None, "", "dumb", True, False),
            (None, None, "", "", True, False),
        ]
        for explicit, saved, no_color, term, tty, expected in cases:
            with self.subTest(explicit=explicit, saved=saved, no_color=no_color, term=term, tty=tty):
                with patch.dict(os.environ, {"NO_COLOR": no_color, "TERM": term}):
                    policy = terminal.configure({"ui": {"color": saved}} if saved else {}, explicit)
                    self.assertEqual(policy.status(Stream(tty))[0], expected)
        policy = terminal.configure({})
        self.assertFalse(policy.status(Stream(False))[0])
        self.assertTrue(policy.status(Stream(True))[0])
        self.assertFalse(policy.status(io.StringIO())[0])

    def test_closed_fragments_and_theme_foreground_for_essential_text(self):
        output = Stream()
        ui = terminal.Terminal(output, terminal.ColorPolicy("always"))
        ui.notice("保存失败", "文件不可写，请修改路径", "error")
        ui.menu("1 查看当前配置\n0 退出设置")
        ui.prompt("请选择 [0]: ")
        text = output.getvalue()
        self.assertIn("\x1b[31m!\x1b[22;39m", text)
        self.assertIn("\x1b[31m保存失败\x1b[22;39m", text)
        self.assertNotIn("\x1b[31m文件不可写", text)
        self.assertNotIn("\x1b[36m1", text)
        self.assertTrue(text.endswith("\x1b[22;39m"))
        self.assertEqual(SGR.sub("", text), "! 保存失败：文件不可写，请修改路径\n1 查看当前配置\n0 退出设置\n请选择 [0]: ")
        self.assertEqual(ui.text("first\nsecond", "title"),
                         "\x1b[1;36mfirst\x1b[22;39m\n\x1b[1;36msecond\x1b[22;39m")

    def test_untrusted_escape_sequences_and_debug_secrets_are_not_styling(self):
        output = Stream()
        ui = terminal.Terminal(output, terminal.ColorPolicy("always"))
        ui.notice("错误\x1b[31m", "\x1b]4;1;bad\a原始内容", "error")
        self.assertNotIn("\x1b]", output.getvalue())
        self.assertIn("\\u001b", output.getvalue())
        terminal.configure(explicit="always")
        output = Stream()
        with patch("sys.stderr", output):
            DebugTrace(True, secrets=["TOP-SECRET"]).emit("AI failure", {"error": "TOP-SECRET\x1b[32m"})
        self.assertNotIn("TOP-SECRET", output.getvalue())
        self.assertIn("[REDACTED]", output.getvalue())
        self.assertIn("\x1b[31m", output.getvalue())
        self.assertNotIn("\x1b[32m", output.getvalue())

    def test_invalid_color_preference_falls_back_without_echoing_value(self):
        for ui in ({"color": "private-invalid-value"}, {"color": 42}, "not-a-table"):
            warning = Stream()
            policy = terminal.configure({"ui": ui}, warning_writer=warning)
            self.assertEqual(policy.mode, "never")
            self.assertNotIn("private-invalid-value", warning.getvalue())
            self.assertNotIn("\x1b", warning.getvalue())
            warning = Stream()
            policy = terminal.configure({"ui": ui}, "auto", warning_writer=warning)
            self.assertEqual(policy.mode, "auto")
            self.assertEqual(warning.getvalue(), "")


class ColorCliTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.EndToEndTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.env, self.path = fixture.env, fixture.path
        self.env.update(TERM="xterm-256color", NO_COLOR="", XDG_STATE_HOME=str(self.path / "state"))
        # Guard every subprocess test against accidentally falling back to a
        # user's installed model CLI, even if a malformed fixture loses provider.
        self.env["ASKAI_PROVIDER"] = "test"
        self.config = Path(self.env["ASKAI_CONFIG"])

    def invoke(self, *args, executable=ASKAI):
        return subprocess.run([str(executable), *args], env=self.env, capture_output=True, text=True, timeout=15)

    def test_always_never_and_machine_modes(self):
        result = self.invoke("--color", "always", "question")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("\x1b[1;36m", result.stdout)
        result = self.invoke("--color", "never", "question")
        self.assertNotIn("\x1b", result.stdout + result.stderr)
        for mode in ("--print", "--json"):
            result = self.invoke("--color", "always", "--debug", mode, "question")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("\x1b", result.stdout)
            self.assertIn("\x1b", result.stderr)
        self.assertEqual(self.invoke("--color=always", "--print", "question").stdout, "printf 'hello\\n'\n")

    def test_management_options_duplicate_errors_and_literal_questions(self):
        result = self.invoke("--color", "never", "doctor", "--color", "always")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("\x1b", result.stdout)
        for args in (("--color=bad", "doctor", "--color=always"), ("doctor", "--color"), ("--colo", "always", "doctor")):
            result = self.invoke(*args)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertNotIn("\x1b", result.stderr)
        capture = self.path / "capture.json"
        self.env["ASKAI_TEST_CAPTURE"] = str(capture)
        result = self.invoke("--color=always", "--print", "--", "doctor", "--color", "never")
        self.assertEqual(result.returncode, 0, result.stderr)
        sent = json.loads(capture.read_text())["stdin"].splitlines()[-1]
        self.assertEqual(json.loads(sent)["question"], "doctor --color never")
        self.config.write_text(self.config.read_text() + '\n[providers."--color"]\ntype="command"\nexecutable=' + json.dumps(str(self.fixture.fake)) + '\n')
        result = self.invoke("--provider=--color", "--color=always", "--print", "question")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_init_help_and_version_ignore_broken_config(self):
        self.config.write_text("invalid TOML [[")
        for args in (("--color=always", "init", "fish"), ("--color=always", "--help"), ("--version",)):
            result = self.invoke(*args)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("\x1b", result.stdout + result.stderr)
        result = self.invoke("--color=always", "--help", executable=ROOT / "bin/askai-config")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("\x1b", result.stdout + result.stderr)

    def test_invalid_ui_does_not_block_question_or_leak_into_command(self):
        original = self.config.read_text()
        for ui in ('[ui]\ncolor="typo"\n', 'ui={color="typo",other="keep"}\n', 'ui="typo"\n'):
            self.config.write_text(original + "\n" + ui if ui.startswith("[") else ui + original)
            result = self.invoke("--print", "question")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("ui.color 无效", result.stderr)
            self.assertEqual(result.stdout, "printf 'hello\\n'\n")
            self.assertNotIn("\x1b", result.stdout + result.stderr)

    def test_p_action_payload_and_clipboard_stay_raw(self):
        captured = self.fixture.clipboard_fixture()
        for action in ("p1\n", "c1\n"):
            code, text = pty_terminal([str(ASKAI), "--color=always", "question"], self.env, (action,))
            self.assertEqual(code, 0, text)
            self.assertIn("\x1b", text)
            if action.startswith("p"):
                payload = text.split("p1\r\n", 1)[1]
                self.assertIn("未复制到剪贴板", payload)
                self.assertEqual(payload.splitlines()[-1], "printf 'hello\\n'")
                self.assertNotIn("\x1b", payload.splitlines()[-1])
            else:
                self.assertEqual(captured.read_text(), "printf 'hello\\n'")

    def test_color_integration_and_passive_questions_in_real_shells(self):
        self.config.write_text(self.config.read_text() + '\n[ui]\ncolor="always"\n')
        for shell in ("fish", "zsh", "bash"):
            with self.subTest(shell=shell):
                executable = shutil.which(shell)
                if not executable:
                    continue
                generated = self.invoke("init", shell, "--hook", "--aliases", "--color=always")
                self.assertEqual(generated.returncode, 0, generated.stderr)
                self.assertNotIn("\x1b", generated.stdout)
                script = self.path / ("init." + shell)
                script.write_text(generated.stdout)
                flags = {"fish": ["--no-config"], "zsh": ["-f"], "bash": ["--noprofile", "--norc"]}[shell]
                env = {**self.env, "PATH": str(ROOT / "bin") + os.pathsep + self.env["PATH"]}
                for question in ("askai --color=always question", "怎样查看磁盘占用"):
                    code, output = pty_terminal([executable, *flags, "-i", "-c",
                                                f"source {shlex.quote(str(script))}; {question}"], env, ("q\n",))
                    self.assertIn(code, (0, 127), output)
                    self.assertIn("正在询问 test", SGR.sub("", output))
                    self.assertIn("\x1b", output)

    def test_script_source_saved_file_and_execution_file_remain_raw(self):
        source = "print('color-free')\n"
        self.env["ASKAI_TEST_ANSWER"] = json.dumps(response(commands=[python_script(source)]))
        captured = self.fixture.clipboard_fixture()
        code, output = pty_terminal([str(ASKAI), "--color=always", "question"], self.env, ("r1\n",))
        self.assertEqual(code, 0, output)
        self.assertEqual(captured.read_text(), source)
        saved = self.path / "saved.py"
        code, output = pty_terminal([str(ASKAI), "--color=always", "question"], self.env, ("s1\n", str(saved) + "\n"))
        self.assertEqual(code, 0, output)
        self.assertEqual(saved.read_text(), source)
        selected = self.path / "execute.fish"
        self.env.update(ASKAI_COMMAND_FILE=str(selected), ASKAI_SHELL="fish",
                        ASKAI_TEST_ANSWER=json.dumps(response("printf 'color-free'")))
        code, output = pty_terminal([str(ASKAI), "--color=always", "question"], self.env, ("1\n",))
        self.assertEqual(code, 0, output)
        self.assertNotIn("\x1b", selected.read_text())
        self.assertEqual(selected.read_text(), "printf 'color-free'\n")

    def test_missing_controlling_terminal_json_fallback_is_raw(self):
        payload = response()
        output, diagnostic = Stream(), Stream()
        with patch.dict(os.environ, self.env), patch("sys.stdout", output), patch("sys.stderr", diagnostic), \
             patch("sys.stdin", Stream(False)), patch("askai_cli.cli.answer", return_value=json.dumps(payload)), \
             patch("builtins.open", side_effect=OSError("no tty")):
            self.assertEqual(run(["--color=always", "question"]), 0)
        self.assertEqual(json.loads(output.getvalue()), payload)
        self.assertNotIn("\x1b", output.getvalue())
        self.assertIn("\x1b", diagnostic.getvalue())

    def test_color_preference_save_cancel_and_safe_refusal(self):
        args = ["--shell", "fish", "--startup-file", str(self.path / "unused.fish"), "--color=always"]
        with patch.dict(os.environ, self.env):
            output = Stream()
            setup.main(args, StreamWithInput("1\n3\n3\ny\n0\n0\n"), output)
            self.assertEqual(setup.core.load_config()["ui"]["color"], "never")
            # Saving does not switch the current session's rendering policy.
            self.assertIn("\x1b", output.getvalue().split("设置已保存", 1)[1])
            self.assertFalse((self.path / "unused.fish").exists())
            before = self.config.read_bytes()
            setup.main(args, StreamWithInput("1\n3\n2\nn\n0\n0\n"), Stream())
            self.assertEqual(self.config.read_bytes(), before)
            for content in ('ui={color="typo",other="keep"}\n', 'ui="typo"\n'):
                self.config.write_text(content + 'provider="test"\n')
                original = self.config.read_bytes()
                output = Stream()
                setup.main(args, StreamWithInput("1\n3\n1\n0\n0\n0\n"), output)
                self.assertIn("本次操作未完成", output.getvalue())
                self.assertEqual(self.config.read_bytes(), original)


class StreamWithInput(Stream):
    def __init__(self, value):
        super().__init__()
        self.write(value)
        self.seek(0)


if __name__ == "__main__":
    unittest.main()

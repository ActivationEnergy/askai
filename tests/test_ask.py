"""Behavioral tests use fake CLIs and an actual controlling terminal; no AI credits."""

import contextlib
import io
import json
import os
from pathlib import Path
import pty
import select
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.error import HTTPError
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from askai_cli.cli import choose, run
from askai_cli.clipboard import copy_command
from askai_cli.core import AskError, instructions, load_config, parse_answer, validate_command
from askai_cli.providers import api_answer, cli_answer, run_cli, select_provider
from askai_cli.shells import SHORTCUTS, integration
from askai_cli.debug import DebugTrace
from askai_cli.scripts import MAX_INLINE_BYTES, standalone_command, validate_candidate

ROOT = Path(__file__).resolve().parents[1]
ASKAI = ROOT / "bin" / "askai"


def response(command="printf 'hello\\n'", **kwargs):
    return {"explanation": "测试建议", "commands": [
        {"label": "测试命令", "command": command, "warning": ""}], **kwargs}


def python_script(code="print('hello')", args=None):
    return {"kind": "script", "language": "python", "label": "Python 处理",
            "code": code, "args": args or [], "warning": ""}


def terminal(argv, env, inputs=("1\n",), timeout=15, prompt_markers=None):
    """Launch in a PTY, answering only after the selection prompt is displayed."""
    pid, fd = pty.fork()
    if pid == 0:
        try:
            os.execvpe(argv[0], argv, env)
        except OSError as exc:
            os.write(2, str(exc).encode())
            os._exit(127)
    output = b""
    sent = 0
    observed = 0
    exit_status = None
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            if select.select([fd], [], [], .1)[0]:
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                output += chunk
                if prompt_markers is not None:
                    if sent < len(inputs):
                        marker = prompt_markers[sent].encode()
                        position = output.find(marker, observed)
                        if position >= 0:
                            os.write(fd, inputs[sent].encode())
                            sent += 1
                            observed = position + len(marker)
                else:
                    count = output.count("[默认 q]:".encode()) + output.count("保存路径（无需引号，回车取消）:".encode())
                    if count > observed and sent < len(inputs):
                        keys = inputs[sent]
                        # Single-key menus no longer echo or require a newline;
                        # text prompts and basic-terminal menus still do.
                        menu_at = output.rfind("[默认 q]:".encode())
                        text_at = output.rfind("保存路径（无需引号，回车取消）:".encode())
                        if menu_at > text_at and any(hint.encode() in output for hint in ("按编号立即执行", "按键选择")):
                            keys = keys.rstrip("\n") or "\r"
                        os.write(fd, keys.encode())
                        sent += 1
                        observed = count
            waited, status = os.waitpid(pid, os.WNOHANG)
            if waited:
                exit_status = status
                # Drain any output still queued on the PTY.
                while select.select([fd], [], [], 0)[0]:
                    try:
                        chunk = os.read(fd, 65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    output += chunk
                break
        else:
            raise AssertionError("PTY timeout:\n" + output.decode(errors="replace"))
    finally:
        os.close(fd)
        if exit_status is None:
            waited, status = os.waitpid(pid, os.WNOHANG)
            if waited:
                exit_status = status
            else:
                os.kill(pid, signal.SIGTERM)
                _, exit_status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(exit_status), output.decode(errors="replace")


class ModelOutputTests(unittest.TestCase):
    def test_python_protocol_rejects_ambiguous_or_unsupported_scripts(self):
        item = python_script()
        self.assertEqual(parse_answer(json.dumps(response(commands=[item])))["commands"], [item])
        for update in ({"language": "javascript"}, {"command": "echo unexpected"},
                       {"args": "not-a-list"}, {"code": ""}, {"args": ["bad\x00argument"]}):
            with self.subTest(update=update), self.assertRaises(AskError):
                parse_answer(json.dumps(response(commands=[{**item, **update}])))

    def test_large_python_copy_redirects_to_source_or_save(self):
        item = python_script("# " + "x" * MAX_INLINE_BYTES)
        with self.assertRaisesRegex(AskError, "脚本过长"):
            standalone_command(item, "fish")
        output = io.StringIO()
        choice = choose(response(commands=[item]), io.StringIO("c1\nr1\n"), output, "fish")
        self.assertEqual(choice, ("source", 0))
        self.assertIn("脚本过长", output.getvalue())

    def test_python_syntax_check_never_executes_source(self):
        with tempfile.TemporaryDirectory() as folder:
            marker = Path(folder) / "not-executed"
            validate_candidate(python_script(f"from pathlib import Path\nPath({str(marker)!r}).touch()"), "fish")
            self.assertFalse(marker.exists())
            with self.assertRaisesRegex(AskError, "Python 语法检查失败"):
                validate_candidate(python_script("if True\n print('bad')"), "fish")

    def test_copied_python_is_self_contained_for_all_shells(self):
        with tempfile.TemporaryDirectory() as folder:
            marker = Path(folder) / "not-executed"
            literal = "single ' double \" backslash \\ dollar $HOME 中文"
            args = ["two words", literal, f"$(touch {marker})", "line one\nline two"]
            code = "import json,sys\nvalue = " + repr(literal) + "\nprint(json.dumps([value, sys.argv[1:]], ensure_ascii=False))"
            for shell in ("fish", "zsh", "bash"):
                if not shutil.which(shell):
                    continue
                with self.subTest(shell=shell):
                    command = standalone_command(python_script(code, args), shell)
                    validate_command(command, shell)
                    flags = {"fish": ["--no-config"], "zsh": ["-f"], "bash": ["--noprofile", "--norc"]}[shell]
                    result = subprocess.run([shell, *flags, "-c", command], capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(json.loads(result.stdout), [literal, args])
                    self.assertFalse(marker.exists())

    def test_temporary_python_preserves_stdin_cwd_explicit_imports_args_and_exit_code(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder).resolve()
            (path / "local_helper.py").write_text("VALUE = 'local import works'\n")
            code = ("import json,sys,os\nsys.path.insert(0,os.getcwd())\nimport local_helper\n"
                    "print(json.dumps({'file':__file__, 'cwd':os.getcwd(), 'args':sys.argv[1:], "
                    "'input':input(), 'local':local_helper.VALUE, 'env':os.environ['ASKAI_SCRIPT_TEST']}))\n"
                    "sys.exit(7)")
            item = python_script(code, ["two words", "$literal"])
            runner = (f"import sys;sys.path.insert(0,{str(ROOT / 'src')!r});"
                      "from askai_cli.scripts import run_script;"
                      f"raise SystemExit(run_script({item!r}))")
            result = subprocess.run([sys.executable, "-c", runner], input="user input\n", cwd=path,
                                    env={**os.environ, "ASKAI_SCRIPT_TEST": "preserved"}, capture_output=True, text=True)
            self.assertEqual(result.returncode, 7, result.stderr)
            actual = json.loads(result.stdout)
            self.assertEqual(actual["cwd"], str(path))
            self.assertEqual(actual["args"], item["args"])
            self.assertEqual(actual["input"], "user input")
            self.assertEqual(actual["local"], "local import works")
            self.assertEqual(actual["env"], "preserved")
            self.assertFalse(Path(actual["file"]).exists())

    def test_shared_prompt_injects_shell_without_breaking_json_schema(self):
        for shell in ("fish", "zsh", "bash"):
            prompt = instructions(shell)
            self.assertIn(f"目标 shell 是 {shell}", prompt)
            self.assertNotIn("{{shell}}", prompt)
            self.assertIn('"commands"', prompt)

    def test_multiline_stays_one_candidate(self):
        command = "cat <<'END'\nhello\nworld\nEND"
        raw = json.dumps(response(command))
        self.assertEqual(parse_answer("```json\n" + raw + "\n```")["commands"][0]["command"], command)

    def test_malformed_or_hidden_command_rejected(self):
        for raw in (None, [], '```bash\nrm -rf /\n```', '{}', '[]',
                    json.dumps(response("echo \x1b[2J")), json.dumps(response("echo \u202ehidden"))):
            with self.subTest(raw=raw), self.assertRaises(AskError):
                parse_answer(raw)

    def test_missing_info_can_have_no_commands(self):
        self.assertEqual(parse_answer(json.dumps(response(commands=[])))["commands"], [])

    def test_candidate_explanations_are_optional_but_must_be_text(self):
        self.assertEqual(parse_answer(json.dumps(response())), response())
        for field in ("explanation", "details"):
            item = {**response()["commands"][0], field: ["invalid"]}
            with self.subTest(field=field), self.assertRaises(AskError):
                parse_answer(json.dumps(response(commands=[item])))

    def test_menu_default_cancels_and_print_never_runs(self):
        self.assertEqual(choose(response(), io.StringIO("\n"), io.StringIO()), ("cancel", None))
        self.assertEqual(choose(response(), io.StringIO("p1\n"), io.StringIO()), ("print", 0))
        self.assertEqual(choose(response(), io.StringIO("c1\n"), io.StringIO()), ("copy", 0))

    def test_clipboard_uses_exact_input_without_shell_or_added_newline(self):
        command = "printf one\nprintf two"
        with patch("askai_cli.clipboard.platform.system", return_value="Darwin"), \
             patch("askai_cli.clipboard.shutil.which", return_value="/usr/bin/pbcopy"), \
             patch("askai_cli.clipboard.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")) as process:
            copy_command(command)
            self.assertEqual(process.call_args.args[0], ["/usr/bin/pbcopy"])
            self.assertEqual(process.call_args.kwargs["input"], command)
            self.assertNotIn("shell", process.call_args.kwargs)
        with patch("askai_cli.clipboard.shutil.which", return_value=None):
            with self.assertRaisesRegex(AskError, "找不到可用的剪贴板工具"):
                copy_command(command)

    def test_shell_syntax_check_does_not_execute(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "marker"
            for shell in ("fish", "zsh", "bash"):
                if not shutil.which(shell):
                    continue
                validate_command(f"touch {shlex.quote(str(marker))}", shell)
                self.assertFalse(marker.exists())
                with self.assertRaises(AskError):
                    validate_command("if", shell)

    def test_claude_is_never_auto_invoked(self):
        with patch("askai_cli.providers.shutil.which", side_effect=lambda x: x if x == "claude" else None), patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(AskError):
                select_provider({}, None)
            self.assertEqual(select_provider({}, "claude")[0], "claude")

    def test_auto_prefers_codex_and_explicit_error_does_not_fallback(self):
        with patch("askai_cli.providers.shutil.which", return_value="/fake/cli"), patch.dict(os.environ, {}, clear=True):
            self.assertEqual(select_provider({}, None)[0], "codex")
            with self.assertRaises(AskError):
                select_provider({}, "unknown")
            self.assertEqual(select_provider({}, "dsh")[0], "dsh")


    def test_bad_configuration_is_diagnostic(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            for value in ('[', 'providers = []', '[providers]\nbroken = "wrong"'):
                path.write_text(value)
                with self.assertRaises(AskError):
                    load_config(path)


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="askai-test-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name).resolve()
        self.fake = self.path / "fake-model"
        self.fake.write_text(f"#!{sys.executable}\n" + '''import json, os, pathlib, sys
if "mcp" in sys.argv and "list" in sys.argv:
    print("[]")
    sys.exit(0)
payload = sys.stdin.read()
if os.environ.get("ASKAI_TEST_CAPTURE"):
    pathlib.Path(os.environ["ASKAI_TEST_CAPTURE"]).write_text(json.dumps({"argv":sys.argv, "stdin":payload}))
if os.environ.get("ASKAI_TEST_REQUEST_COUNT"):
    count = pathlib.Path(os.environ["ASKAI_TEST_REQUEST_COUNT"])
    count.write_text(str(int(count.read_text()) + 1 if count.exists() else 1))
if os.environ.get("ASKAI_TEST_STDERR"):
    print(os.environ["ASKAI_TEST_STDERR"], file=sys.stderr)
if os.environ.get("ASKAI_TEST_FAIL"):
    print("fake authentication failed", file=sys.stderr)
    sys.exit(7)
raw = os.environ["ASKAI_TEST_ANSWER"]
if "-o" in sys.argv:
    pathlib.Path(sys.argv[sys.argv.index("-o") + 1]).write_text(raw)
elif "--output-format" in sys.argv:
    print(json.dumps({"result": raw, "is_error": bool(os.environ.get("ASKAI_TEST_CLAUDE_ERROR"))}))
else:
    print(raw)
''')
        self.fake.chmod(0o755)
        config = self.path / "config.toml"
        config.write_text('provider = "test"\n[providers.test]\ntype = "command"\n'
                          f'executable = {json.dumps(str(self.fake))}\n')
        self.env = {**os.environ, "ASKAI_CONFIG": str(config), "ASKAI_TEST_ANSWER": json.dumps(response()),
                    "SHELL": "/bin/bash", "ASKAI_NO_SETUP": "1", "ASKAI_WRAPPER_PROTOCOL": "2"}
        for name in ("ASKAI_PROVIDER", "ASKAI_MODEL", "ASKAI_SHELL", "ASKAI_COMMAND_FILE", "BASH_ENV", "ENV"):
            self.env.pop(name, None)

    def invoke(self, *args):
        return subprocess.run([str(ASKAI), *args], env=self.env, capture_output=True, text=True, timeout=15)

    def test_print_and_json_never_write_or_execute(self):
        marker = self.path / "executed"
        output = self.path / "selected"
        self.env.update(ASKAI_COMMAND_FILE=str(output), ASKAI_TEST_ANSWER=json.dumps(response(f"touch {marker}")))
        for args in (("--print",), ("--json",), ()):
            output.write_text("stale old command")
            result = self.invoke(*args, "test question")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(str(marker), result.stdout)
            self.assertEqual(output.read_text(), "")
            self.assertFalse(marker.exists())

    def test_debug_keeps_stdout_clean_and_redacts_interaction(self):
        secret = "fixture-secret-43523"
        self.env.update(ASKAI_TEST_KEY=secret,
                        ASKAI_TEST_STDERR=f"model: fixture-model\nAuthorization: Bearer {secret}")
        ordinary = self.invoke("--print", "test question")
        self.assertEqual(ordinary.stderr, "")
        result = self.invoke("--debug", "--print", f"inspect file.txt; secret={secret}")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, ordinary.stdout)
        self.assertNotIn(secret, result.stderr)
        self.assertIn("[REDACTED]", result.stderr)
        for text in ("后端与模型", "发送给 AI 的内容", "调用模型 CLI", "模型 CLI 返回", "AI 回答内容", "fixture-model", "file.txt", "耗时"):
            self.assertIn(text, result.stderr)
        result = self.invoke("--debug", "--json", "test question")
        self.assertEqual(json.loads(result.stdout), response())

    def test_debug_includes_invalid_response_and_failure_without_running_commands(self):
        selected = self.path / "selected"
        self.env.update(ASKAI_TEST_ANSWER="invalid model response", ASKAI_COMMAND_FILE=str(selected))
        result = self.invoke("--debug", "--print", "test question")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("invalid model response", result.stderr)
        self.assertEqual(selected.read_text(), "")
        self.env.update(ASKAI_TEST_FAIL="1")
        result = self.invoke("--debug", "test question")
        self.assertEqual(result.returncode, 1)
        self.assertIn("AI 调用失败", result.stderr)
        self.assertIn("authentication failed", result.stderr)

    def clipboard_fixture(self):
        captured = self.path / "clipboard"
        fixture = f"#!{sys.executable}\nimport os,pathlib,sys\npathlib.Path(os.environ['ASKAI_TEST_CLIPBOARD']).write_text(sys.stdin.read())\n"
        for name in ("pbcopy", "wl-copy", "xclip", "xsel", "clip.exe"):
            executable = self.path / name
            executable.write_text(fixture)
            executable.chmod(0o755)
        self.env.update(PATH=str(self.path) + os.pathsep + self.env["PATH"],
                        ASKAI_TEST_CLIPBOARD=str(captured))
        return captured

    def test_python_print_json_and_noninteractive_never_execute(self):
        marker = self.path / "not-executed"
        item = python_script(f"from pathlib import Path\nPath({str(marker)!r}).touch()")
        self.env["ASKAI_TEST_ANSWER"] = json.dumps(response(commands=[item]))
        for mode in (("--print",), ("--json",), ()):
            result = self.invoke(*mode, "Python task")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(marker.exists())
            if mode == ("--print",):
                self.assertEqual(shlex.split(result.stdout)[5], item["code"])
                self.assertNotIn("askai-python-", result.stdout)
            elif mode == ("--json",):
                self.assertEqual(json.loads(result.stdout)["commands"], [item])
            else:
                self.assertIn("Python 脚本", result.stdout)

    def test_python_copy_source_and_save_are_durable_and_do_not_execute(self):
        captured = self.clipboard_fixture()
        marker = self.path / "not-executed"
        selected = self.path / "selected"
        source = f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
        item = python_script(source, ["two words", "$literal"])
        self.env.update(ASKAI_COMMAND_FILE=str(selected), ASKAI_TEST_ANSWER=json.dumps(response(commands=[item])))
        for choice in ("c1\n", "r1\n"):
            code, output = terminal([str(ASKAI), "Python task"], self.env, (choice,))
            self.assertEqual(code, 0, output)
            self.assertFalse(marker.exists())
            self.assertEqual(selected.read_text(), "")
            copied = captured.read_text()
            if choice.startswith("r"):
                self.assertEqual(copied, source)
            else:
                copied_argv = shlex.split(copied)
                self.assertEqual(copied_argv[:5], ["python3", "-I", "-X", "utf8", "-c"])
                self.assertEqual(copied_argv[5], source)
                self.assertEqual(copied_argv[6:], item["args"])
                self.assertNotIn("askai-python-", copied)
        saved = self.path / "saved script.py"
        code, output = terminal([str(ASKAI), "Python task"], self.env, ("s1\n", str(saved) + "\n"))
        self.assertEqual(code, 0, output)
        self.assertEqual(saved.read_text(), source)
        self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
        self.assertFalse(marker.exists())
        self.assertEqual(selected.read_text(), "")
        self.assertIn("运行命令:", output)
        saved_command = output.split("运行命令:", 1)[1].strip().splitlines()[0]
        self.assertEqual(shlex.split(saved_command), ["python3", "-I", "-X", "utf8", str(saved), *item["args"]])
        result = subprocess.run(["bash", "--noprofile", "--norc", "-c", saved_command],
                                env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(marker.exists())
        marker.unlink()
        saved.write_text("KEEP EXISTING FILE")
        code, output = terminal([str(ASKAI), "Python task"], {**self.env, "TERM": "xterm-256color"}, ("s1", str(saved) + "\n", "q"),
                                prompt_markers=("[默认 q]:", "保存路径（无需引号，回车取消）:", "[默认 q]:"))
        self.assertEqual(code, 0, output)
        self.assertIn("未覆盖", output)
        self.assertEqual(saved.read_text(), "KEEP EXISTING FILE")
        self.assertFalse(marker.exists())

    def test_python_executes_with_each_shell_integration_and_cleans_tempfile(self):
        item = python_script("import json,os,sys\nprint('SCRIPT:' + json.dumps({'file':__file__, 'cwd':os.getcwd(), 'args':sys.argv[1:]}))\nsys.exit(3)",
                             ["two words", "$literal"])
        self.env["ASKAI_TEST_ANSWER"] = json.dumps(response(commands=[item]))
        for shell in ("fish", "zsh", "bash"):
            if not shutil.which(shell):
                continue
            with self.subTest(shell=shell):
                integration_path = self.path / ("script-integration." + shell)
                integration_path.write_text(integration(shell))
                flags = {"fish": ["--no-config"], "zsh": ["-f"], "bash": ["--noprofile", "--norc"]}[shell]
                code, output = terminal([shell, *flags, "-c", f"source {shlex.quote(str(integration_path))}; askai Python task"], self.env)
                self.assertEqual(code, 3, output)
                line = next(line for line in output.splitlines() if line.startswith("SCRIPT:"))
                actual = json.loads(line[len("SCRIPT:"):])
                self.assertEqual(actual["args"], item["args"])
                self.assertEqual(actual["cwd"], str(ROOT))
                self.assertFalse(Path(actual["file"]).exists())

    def test_copy_menu_copies_without_execution_or_selection_file(self):
        captured = self.clipboard_fixture()
        selected = self.path / "selected"
        marker = self.path / "must-not-execute"
        item = {**response(f"touch {marker}")["commands"][0], "explanation": "只解释，不附加到复制内容",
                "details": "这段详细解释不会被复制或执行。"}
        self.env.update(ASKAI_COMMAND_FILE=str(selected),
                        ASKAI_TEST_ANSWER=json.dumps(response(commands=[item])))
        code, output = terminal([str(ASKAI), "test question"], self.env, ("c1\n",))
        self.assertEqual(code, 0, output)
        self.assertEqual(captured.read_text(), f"touch {marker}")
        self.assertEqual(selected.read_text(), "")
        self.assertFalse(marker.exists())
        self.assertIn("第 1 条命令已放入剪贴板", output)
        for name in ("pbcopy", "wl-copy", "xclip", "xsel", "clip.exe"):
            (self.path / name).write_text(f"#!{sys.executable}\nimport sys\nprint('clipboard unavailable', file=sys.stderr)\nsys.exit(1)\n")
        code, output = terminal([str(ASKAI), "test question"], self.env, ("c1\n", "q\n"))
        self.assertEqual(code, 0, output)
        self.assertIn("clipboard unavailable", output)
        self.assertEqual(selected.read_text(), "")
        self.assertFalse(marker.exists())

    def test_explanation_expands_without_execution_or_second_model_call(self):
        marker = self.path / "not-executed"
        selected = self.path / "selected"
        count = self.path / "request-count"
        item = {**response(f"touch {marker}")["commands"][0], "explanation": "先收集键，再过滤匹配的行。",
                "details": "1. 先处理文件 B，将第一列加入查找表。\n2. 处理文件 A，仅输出第二列不在查找表中的行。"}
        self.env.update(ASKAI_COMMAND_FILE=str(selected), ASKAI_TEST_REQUEST_COUNT=str(count),
                        ASKAI_TEST_ANSWER=json.dumps(response(commands=[item])))
        code, output = terminal([str(ASKAI), "filter rows"], self.env, ("e1\n", "q\n"))
        self.assertEqual(code, 0, output)
        self.assertIn("原理: " + item["explanation"], output)
        self.assertIn("先处理文件 B", output)
        self.assertGreaterEqual(output.count("[默认 q]"), 2)
        self.assertFalse(marker.exists())
        self.assertEqual(selected.read_text(), "")
        self.assertEqual(count.read_text(), "1")
        result = self.invoke("--print", "filter rows")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f"touch {marker}\n")

    def test_failure_never_reuses_stale_selection(self):
        output = self.path / "selected"
        output.write_text("touch dangerous")
        self.env.update(ASKAI_COMMAND_FILE=str(output), ASKAI_TEST_FAIL="1")
        result = self.invoke("test question")
        self.assertEqual(result.returncode, 1)
        self.assertIn("authentication failed", result.stderr)
        self.assertEqual(output.read_text(), "")

    def test_clarification_is_not_reported_as_a_successful_command(self):
        selected = self.path / "selected"
        explanation = "请提供你所说的 DeepSeek DSH 的官方页面，以确认安装方式。"
        self.env.update(ASKAI_COMMAND_FILE=str(selected),
                        ASKAI_TEST_ANSWER=json.dumps(response(explanation=explanation, commands=[])))
        for mode in ((), ("--print",), ("--json",)):
            with self.subTest(mode=mode):
                selected.write_text("stale command")
                result = self.invoke(*mode, "怎样安装DeepSeek的DSH？")
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(selected.read_text(), "")
                if mode == ("--print",):
                    self.assertEqual(result.stdout, "")
                    self.assertIn(explanation, result.stderr)
                elif mode == ("--json",):
                    self.assertEqual(json.loads(result.stdout)["commands"], [])
                else:
                    self.assertIn("未生成可执行命令", result.stdout)
                    self.assertIn(explanation, result.stdout)
        code, output = terminal([str(ASKAI), "怎样安装DeepSeek的DSH？"], self.env, ())
        self.assertEqual(code, 2, output)
        self.assertIn("未生成可执行命令", output)
        self.assertNotIn("输入编号执行", output)

    def test_wrong_shell_cannot_execute_through_current_shell_wrapper(self):
        selected = self.path / "selected"
        self.env.update(ASKAI_COMMAND_FILE=str(selected), ASKAI_SHELL="fish")
        code, output = terminal([str(ASKAI), "--shell", "bash", "test question"], self.env, ("1\n", "q\n"))
        self.assertEqual(code, 0, output)
        self.assertIn("目标 shell 与当前集成不同", output)
        self.assertEqual(selected.read_text(), "")

    def test_real_terminal_selection_print_cancel_and_execution(self):
        marker = self.path / "executed"
        self.env["ASKAI_TEST_ANSWER"] = json.dumps(response(f"touch {marker}"))
        for choice in ("\n", "q\n", "p1\n"):
            code, output = terminal([str(ASKAI), "test question"], self.env, (choice,))
            self.assertEqual(code, 0, output)
            self.assertFalse(marker.exists(), output)
        code, output = terminal([str(ASKAI), "test question"], self.env)
        self.assertEqual(code, 0, output)
        self.assertTrue(marker.exists(), output)

    def test_explicit_wrappers_preserve_directory_and_environment(self):
        for shell in ("fish", "zsh", "bash"):
            if not shutil.which(shell):
                continue
            with self.subTest(shell=shell):
                script = self.path / ("integration." + shell)
                script.write_text(integration(shell))
                setenv = "set -gx ASKAI_TEST_PERSIST yes" if shell == "fish" else "export ASKAI_TEST_PERSIST=yes"
                self.env["ASKAI_TEST_ANSWER"] = json.dumps(response(f"cd {self.path}\n{setenv}"))
                command = f"source {shlex.quote(str(script))}; askai test question; printf 'PERSIST:%s:%s\\n' \"$PWD\" \"$ASKAI_TEST_PERSIST\""
                flags = {"fish": ["--no-config"], "zsh": ["-f"], "bash": ["--noprofile", "--norc"]}[shell]
                code, output = terminal([shell, *flags, "-c", command], self.env)
                self.assertEqual(code, 0, output)
                self.assertIn(f"PERSIST:{self.path}:yes", output)

    def test_wrapper_multiline_and_exit_status(self):
        for shell in ("fish", "zsh", "bash"):
            if not shutil.which(shell):
                continue
            with self.subTest(shell=shell):
                script = self.path / ("integration." + shell)
                script.write_text(integration(shell))
                self.env["ASKAI_TEST_ANSWER"] = json.dumps(response("printf 'line one\\nline two\\n'\nfalse"))
                flags = {"fish": ["--no-config"], "zsh": ["-f"], "bash": ["--noprofile", "--norc"]}[shell]
                code, output = terminal([shell, *flags, "-c", f"source {shlex.quote(str(script))}; askai test question"], self.env)
                self.assertEqual(code, 1, output)
                self.assertIn("line one\r\nline two", output)

    def test_passive_hook_and_single_token_typo(self):
        for shell in ("fish", "zsh", "bash"):
            if not shutil.which(shell):
                continue
            with self.subTest(shell=shell):
                script = self.path / ("hook." + shell)
                script.write_text(integration(shell, hook=True))
                flags = {"fish": ["--no-config"], "zsh": ["-f"], "bash": ["--noprofile", "--norc"]}[shell]
                command = f"source {shlex.quote(str(script))}; asktestmissing natural question"
                code, output = terminal([shell, *flags, "-i", "-c", command], self.env)
                # fish always reports 127 for the original missing command,
                # even when its handler successfully runs a suggestion.
                self.assertEqual(code, 127 if shell == "fish" else 0, output)
                self.assertIn("hello", output)
                code, output = terminal([shell, *flags, "-i", "-c", f"source {shlex.quote(str(script))}; asktesttypo"], self.env, ())
                self.assertEqual(code, 127, output)
                self.assertNotIn("正在询问", output)

    def test_noninteractive_hook_never_calls_provider(self):
        for shell in ("fish", "zsh", "bash"):
            if not shutil.which(shell):
                continue
            script = self.path / ("hook." + shell)
            script.write_text(integration(shell, hook=True))
            result = subprocess.run([shell, "-c", f"source {shlex.quote(str(script))}; asktestmissing words"], env=self.env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 127, result.stderr)
            self.assertNotIn("测试建议", result.stdout)

    def test_fish_hook_counts_han_characters_in_unspaced_questions(self):
        if not shutil.which("fish"):
            self.skipTest("fish is not installed")
        capture = self.path / "han-request.json"
        self.env["ASKAI_TEST_CAPTURE"] = str(capture)
        # Reinitialization must not make the saved original handler recursive.
        setup = (f"{shlex.quote(str(ASKAI))} init fish --hook --aliases | source; "
                 "askai init fish --hook --aliases | source; ")
        for question in ("怎样查看磁盘占用", "查盘", "查disk看", "如何查看CPU占用"):
            with self.subTest(question=question):
                capture.unlink(missing_ok=True)
                code, output = terminal(["fish", "--no-config", "-i", "-c", setup + question], self.env, ("q\n",))
                self.assertEqual(code, 127, output)
                self.assertIn("正在询问", output)
                self.assertIn(question, json.loads(capture.read_text())["stdin"])
        for question in ("查", "查disk", "gti"):
            with self.subTest(question=question):
                capture.unlink(missing_ok=True)
                code, output = terminal(["fish", "--no-config", "-i", "-c", setup + question], self.env, ())
                self.assertEqual(code, 127, output)
                self.assertFalse(capture.exists(), output)
                self.assertNotIn("正在询问", output)
        result = subprocess.run(["fish", "--no-config", "-c", setup + "怎样查看磁盘占用"], env=self.env,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 127, result.stderr)
        self.assertFalse(capture.exists(), result.stdout)

    def test_codex_text_adapter_reads_final_file_and_disables_local_tools(self):
        capture = self.path / "capture.json"
        with patch.dict(os.environ, {**self.env, "ASKAI_TEST_CAPTURE": str(capture)}):
            raw = cli_answer({"type": "codex", "executable": str(self.fake)}, "test-model",
                             "system", "question", 5)
        self.assertEqual(parse_answer(raw), response())
        args = json.loads(capture.read_text())
        self.assertNotIn("--image", args["argv"])
        self.assertIn("view_image", args["argv"])
        self.assertFalse(Path(args["argv"][args["argv"].index("-o") + 1]).exists())
        self.assertIn("read-only", args["argv"])
        self.assertIn("shell_tool", args["argv"])
        self.assertIn('web_search="live"', args["argv"])
        self.assertEqual(args["stdin"], "system\n\nquestion")

    def test_codex_web_search_can_be_disabled_without_enabling_shell(self):
        capture = self.path / "codex-capture.json"
        provider = {"type": "codex", "executable": str(self.fake), "web_search": False}
        with patch.dict(os.environ, {**self.env, "ASKAI_TEST_CAPTURE": str(capture)}):
            cli_answer(provider, None, "system", "question", 5)
            args = json.loads(capture.read_text())["argv"]
            self.assertIn('web_search="disabled"', args)
            self.assertIn("shell_tool", args)
            self.assertIn("read-only", args)
            provider["web_search"] = "false"
            with self.assertRaisesRegex(AskError, "web_search 必须"):
                cli_answer(provider, None, "system", "question", 5)

    def test_cli_timeout_is_bounded(self):
        with self.assertRaisesRegex(AskError, "超时"):
            run_cli([sys.executable, "-c", "import time; time.sleep(30)"], "test", .1)

    def test_claude_native_cli_text_and_error_without_real_claude(self):
        capture = self.path / "claude-capture.json"
        provider = {"type": "claude", "executable": str(self.fake)}
        with patch.dict(os.environ, {**self.env, "ASKAI_TEST_CAPTURE": str(capture)}):
            self.assertEqual(parse_answer(cli_answer(provider, None, "system", "question", 5)), response())
            data = json.loads(capture.read_text())
            self.assertEqual(data["stdin"], "question")
            self.assertIn("-p", data["argv"])
            self.assertEqual(data["argv"][data["argv"].index("--tools") + 1], "")
            self.assertNotIn("--input-format", data["argv"])
            with patch.dict(os.environ, {"ASKAI_TEST_CLAUDE_ERROR": "1"}):
                with self.assertRaisesRegex(AskError, "Claude 返回错误"):
                    cli_answer(provider, None, "system", "question", 5)

    def configure_shortcut_fixtures(self):
        """Route every shortcut to a distinct local fixture, never a real model."""
        config = Path(self.env["ASKAI_CONFIG"])
        text = config.read_text()
        for name in SHORTCUTS.values():
            executable = self.path / ("fake-" + name)
            executable.write_text(self.fake.read_text())
            executable.chmod(0o755)
            text += f'\n[providers.{name}]\ntype = "command"\nexecutable = {json.dumps(str(executable))}\n'
        config.write_text(text)
        capture = self.path / "shortcut-prompt"
        self.env["ASKAI_TEST_CAPTURE"] = str(capture)
        return capture

    def test_shortcut_executables_work_without_shell_initialization(self):
        capture = self.configure_shortcut_fixtures()
        for name, provider in SHORTCUTS.items():
            with self.subTest(shortcut=name):
                result = subprocess.run([str(ROOT / "bin" / name), "--print", "file with spaces and $literal"],
                                        env=self.env, capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), "printf 'hello\\n'")
                captured = json.loads(capture.read_text())
                self.assertEqual(Path(captured["argv"][0]).name, "fake-" + provider)
                self.assertIn("file with spaces and $literal", captured["stdin"])

    def test_shortcuts_route_all_providers_and_preserve_arguments(self):
        capture = self.configure_shortcut_fixtures()
        for shell in ("fish", "zsh", "bash"):
            if not shutil.which(shell):
                continue
            script = self.path / ("shortcuts." + shell)
            script.write_text(integration(shell, aliases=True))
            flags = {"fish": ["--no-config"], "zsh": ["-f"], "bash": ["--noprofile", "--norc"]}[shell]
            for name in SHORTCUTS:
                with self.subTest(shell=shell, shortcut=name):
                    result = subprocess.run([shell, *flags, "-c", f"source {shlex.quote(str(script))}; {name} --print 'file with spaces and $literal'"],
                                            env=self.env, capture_output=True, text=True, timeout=15)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout.strip(), "printf 'hello\\n'")
                    captured = json.loads(capture.read_text())
                    self.assertEqual(Path(captured["argv"][0]).name, "fake-" + SHORTCUTS[name])
                    prompt = captured["stdin"]
                    self.assertIn("file with spaces and $literal", prompt)


class ApiTests(unittest.TestCase):
    def test_debug_renders_nested_json_as_readable_fields_and_multiline_commands(self):
        command = 'cd "/tmp/a b"\nprintf "%s" "$PWD"'
        payload = {"model": "served-model", "choices": [{"message": {
            "role": "assistant", "content": json.dumps(response(command), ensure_ascii=True)}}]}
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            trace = DebugTrace(True)
            trace.emit("backend", {"name": "codex", "requested_model": "configured-model", "timeout_seconds": 120})
            trace.emit("HTTP raw response", {"content": json.dumps(payload), "elapsed_seconds": 1.25})
            trace.emit("CLI invocation", {"argv": ["/bin/codex", "exec", "--model", "configured-model"]})
        text = output.getvalue()
        for expected in ("后端与模型", "后端: codex", "请求模型: configured-model", "超时: 120 秒",
                         "模型: served-model", "候选命令:", "方案 1:", "用途: 测试命令", "耗时: 1.25 秒",
                         "调用命令: /bin/codex exec --model configured-model"):
            self.assertIn(expected, text)
        self.assertRegex(text, r'命令:\n( +)cd "/tmp/a b"\n\1printf "%s" "\$PWD"')
        self.assertNotIn('"choices":', text)
        self.assertNotIn('"commands":', text)
        self.assertNotIn('\\u6d4b', text)
        self.assertNotIn('\\n', text)

    def test_debug_api_shows_reported_model_and_redacts_credentials(self):
        secret = "api-fixture-secret-432"
        body = {"model": "actual-served-model", "usage": {"total_tokens": 42},
                "choices": [{"message": {"content": json.dumps(response())}}]}
        provider = {"base_url": "https://api.example.test/v1", "model": "requested-alias", "api_key_env": "ASKAI_TEST_KEY"}
        debug = io.StringIO()
        with patch.dict(os.environ, {"ASKAI_TEST_KEY": secret}), \
             patch("askai_cli.providers.fetch", return_value={"status": 200, "body": json.dumps(body).encode()}) as transport, contextlib.redirect_stderr(debug):
            result = api_answer(provider, None, "system", "question", 5,
                                trace=DebugTrace(True))
        self.assertEqual(parse_answer(result), response())
        text = debug.getvalue()
        for expected in ("requested-alias", "actual-served-model", "总 token 数", "发送 API 请求", "API 模型与用量"):
            self.assertIn(expected, text)
        self.assertNotIn(secret, text)

    def test_debug_redacts_custom_cli_tokens_and_nested_credentials(self):
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            DebugTrace(True).emit("fixture", {
                "argv": ["tool", "--token", "unknown-secret", "--api-key=other-secret"],
                "url": "https://name:password@api.example.test/v1?token=url-secret",
                "headers": {"X-API-Key": "header-secret"},
                "stdin": json.dumps({"data": {"access_token": "nested-secret"}})})
        text = output.getvalue()
        for secret in ("unknown-secret", "other-secret", "name:password", "url-secret", "header-secret", "nested-secret"):
            self.assertNotIn(secret, text)
        self.assertIn("REDACTED", text)

    def test_debug_preserves_invalid_http_response_and_redacts_error_body(self):
        provider = {"base_url": "https://api.example.test/v1", "model": "m", "api_key_env": "ASKAI_TEST_KEY"}
        secret = 'secret-with-"quote'
        with patch.dict(os.environ, {"ASKAI_TEST_KEY": secret}), \
             patch("askai_cli.providers.fetch") as transport:
            output = io.StringIO()
            transport.return_value = {"status": 200, "body": b"broken-json-response"}
            with contextlib.redirect_stderr(output), self.assertRaisesRegex(AskError, "响应不符合"):
                api_answer(provider, None, "system", "question", 5, trace=DebugTrace(True))
            self.assertIn("broken-json-response", output.getvalue())
            output = io.StringIO()
            transport.return_value = {"status": 401, "reason": "Unauthorized", "body": json.dumps({"error": secret}).encode()}
            with contextlib.redirect_stderr(output), self.assertRaisesRegex(AskError, "HTTP 401"):
                api_answer(provider, None, "system", "question", 5, trace=DebugTrace(True))
            self.assertIn("API 请求失败", output.getvalue())
            self.assertNotIn(secret, output.getvalue())
            self.assertNotIn(json.dumps(secret)[1:-1], output.getvalue())

    def test_api_payload_contains_only_system_and_user_text(self):
        payload = json.dumps({"choices": [{"message": {"content": json.dumps(response())}}]}).encode()
        provider = {"base_url": "https://api.example.test/v1", "model": "text-model", "api_key_env": "ASKAI_TEST_KEY"}
        with patch.dict(os.environ, {"ASKAI_TEST_KEY": "test-key"}), patch("askai_cli.providers.fetch", return_value={"status": 200, "body": payload}) as transport:
            result = api_answer(provider, None, "system", "question", 5)
            url, headers, data, remaining = transport.call_args.args
            self.assertEqual(url, "https://api.example.test/v1/chat/completions")
            self.assertEqual(headers["Authorization"], "Bearer test-key")
            self.assertTrue(0 < remaining <= 5)
            body = json.loads(data)
            self.assertEqual(body["messages"], [{"role": "system", "content": "system"},
                                                 {"role": "user", "content": "question"}])
            self.assertFalse(body["stream"])
            self.assertEqual(parse_answer(result), response())

    def test_nonlocal_plain_http_rejected(self):
        with self.assertRaisesRegex(AskError, "HTTPS"):
            api_answer({"base_url": "http://evil.test", "model": "m"}, None, "", "", 5)


if __name__ == "__main__":
    unittest.main()

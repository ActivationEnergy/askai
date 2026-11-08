"""Settings tests use explicit temporary paths, never real user rc files."""

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
from askai_cli import setup, startup
from askai_cli.core import AskError
from askai_cli.shells import integration
import test_ask as fixtures


class TerminalText(io.StringIO):
    def isatty(self):
        return True


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="askai-settings-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.state = self.root / "state/askai/setup-state.json"
        patcher = patch("askai_cli.setup.state_path", return_value=self.state)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_byte_preservation_backup_and_created_file_lifecycle(self):
        for original in (b"# no final newline", b"# original\r\n"):
            path = self.root / "config.fish"
            path.write_bytes(original)
            path.chmod(0o640)
            changes = startup.plan([path], "fish", True, True)
            backups = startup.apply(changes)
            self.assertEqual(Path(backups[0]).read_bytes(), original)
            self.assertEqual(Path(backups[0]).stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.stat().st_mode & 0o777, 0o640)
            self.assertEqual(startup.plan([path], "fish", True, True), [])
            startup.apply(startup.plan([path], "fish", False, False, True))
            self.assertEqual(path.read_bytes(), original)
        fresh = self.root / "new.fish"
        startup.apply(startup.plan([fresh], "fish", True, True))
        startup.apply(startup.plan([fresh], "fish", False, False))
        self.assertTrue(startup.locate(fresh.read_text())[2]["created_file"])
        startup.apply(startup.plan([fresh], "fish", False, False, True))
        self.assertFalse(fresh.exists())

    def test_removal_keeps_user_content_added_to_a_created_file(self):
        path = self.root / "config.fish"
        startup.apply(startup.plan([path], "fish", False, True))
        path.write_text(path.read_text() + "# user added\n")
        startup.apply(startup.plan([path], "fish", False, False, True))
        self.assertEqual(path.read_text(), "# user added\n")

    def test_symlink_dedup_and_hardlink_rejection(self):
        target = self.root / "dotfile"
        target.write_text("# shared\n")
        first, second = self.root / ".bashrc", self.root / ".profile"
        first.symlink_to(target)
        second.symlink_to(target)
        changes = startup.plan([first, second], "bash", False, True)
        self.assertEqual(len(changes), 1)
        startup.apply(changes)
        self.assertTrue(first.is_symlink())
        self.assertTrue(second.is_symlink())
        self.assertIsNotNone(startup.locate(target.read_text()))
        os.link(target, self.root / "hardlink")
        with self.assertRaisesRegex(AskError, "硬链接"):
            startup.plan([target], "bash", False, True)

    def test_unsafe_or_ambiguous_content_is_not_rewritten(self):
        path = self.root / "config.fish"
        samples = (b"\xef\xbb\xbf# BOM\n", b"# mixed\r\n# endings\n", b"# >>> askai shell integration broken\n",
                   b"# documentation: askai init fish --hook | source\n")
        for raw in samples:
            path.write_bytes(raw)
            with self.subTest(raw=raw), self.assertRaises(AskError):
                startup.plan([path], "fish", True, True)
            self.assertEqual(path.read_bytes(), raw)
        path.write_text("# okay\n")
        changes = startup.plan([path], "fish", False, True)
        path.write_text("# concurrent editor\n")
        with self.assertRaisesRegex(AskError, "预览后发生变化"):
            startup.apply(changes)
        self.assertEqual(path.read_text(), "# concurrent editor\n")

    def test_partial_update_reports_completed_files_and_backups(self):
        paths = [self.root / "first.fish", self.root / "second.fish"]
        for path in paths:
            path.write_text("# original\n")
        changes = startup.plan(paths, "fish", False, True)
        replace = os.replace
        def fail_second(source, destination):
            if Path(destination) == paths[1]:
                raise OSError("fixture disk error")
            return replace(source, destination)
        with patch("askai_cli.startup.os.replace", side_effect=fail_second):
            with self.assertRaisesRegex(AskError, "已变更: .*first.fish"):
                startup.apply(changes)
        self.assertIsNotNone(startup.locate(paths[0].read_text()))
        self.assertEqual(paths[1].read_text(), "# original\n")
        self.assertFalse(list(self.root.glob(".askai-write-*")))

    def test_bash_profile_priority_and_old_locations(self):
        profile = self.root / ".profile"
        profile.write_text("# profile\n")
        self.assertEqual(startup.default_paths("bash", self.root, {})[-1], profile)
        startup.apply(startup.plan([profile], "bash", False, True))
        higher = self.root / ".bash_profile"
        higher.write_text("# now first\n")
        self.assertEqual(startup.default_paths("bash", self.root, {})[-1], higher)
        self.assertIn(profile, startup.default_paths("bash", self.root, {}, all_bash=True))
        shared = startup.loader("bash", True, True)
        self.assertEqual(subprocess.run(["/bin/sh", "-n"], input=shared, text=True).returncode, 0)
        result = subprocess.run(["/bin/sh", "-c", shared], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_local_lock_and_index_registration_failure(self):
        with setup.settings_lock():
            with self.assertRaises(AskError):
                with setup.settings_lock():
                    pass
        path = self.root / "custom.fish"
        changes = startup.plan([path], "fish", True, True)
        with patch("askai_cli.setup.write_state", side_effect=OSError("state full")):
            with self.assertRaises(OSError):
                setup.commit_changes(changes, "fish")
        self.assertFalse(path.exists())

    def test_custom_path_can_be_found_and_removed_later(self):
        path = self.root / "custom/custom.fish"
        setup.commit_changes(startup.plan([path], "fish", True, True), "fish")
        state = setup.read_state()
        self.assertIn(str(path), state["locations"]["fish"])
        with patch("askai_cli.setup.startup.default_paths", return_value=[]):
            self.assertEqual(setup.known_paths("fish", state), [path])
            changes = setup.build_changes("fish", [], state, False, False, True, shutil.which("fish"))
            setup.commit_changes(changes, "fish")
        self.assertFalse(path.exists())
        self.assertEqual(setup.read_state()["locations"]["fish"], [])
        self.assertTrue(setup.read_state()["first_hint_seen"])

    def test_hint_is_once_nonblocking_and_excluded_modes_leave_no_state(self):
        class NoRead(TerminalText):
            def readline(self, *args):
                raise AssertionError("first-use hint must never read input")
        env = {**os.environ}
        for key in ("CI", "ASKAI_NO_SETUP", "ASKAI_HOOK_INVOCATION"):
            env.pop(key, None)
        output = TerminalText()
        with patch.dict(os.environ, env, clear=True), patch("sys.stdin", NoRead()), \
             patch("sys.stdout", TerminalText()), patch("sys.stderr", output), \
             patch("askai_cli.setup.startup.default_paths", return_value=[]):
            setup.maybe_hint()
            setup.maybe_hint()
        self.assertEqual(output.getvalue().count("可选设置"), 1)
        self.assertTrue(setup.read_state()["first_hint_seen"])
        self.state.unlink()
        for flag in ("CI", "ASKAI_NO_SETUP", "ASKAI_HOOK_INVOCATION"):
            with patch.dict(os.environ, {flag: "1"}), patch("sys.stdin", NoRead()), \
                 patch("sys.stdout", TerminalText()), patch("sys.stderr", TerminalText()):
                setup.maybe_hint()
            self.assertFalse(self.state.exists())

    def test_decline_and_eof_do_not_write_startup_or_state(self):
        path = self.root / "config.fish"
        args = ["--shell", "fish", "--startup-file", str(path)]
        for answers in ("4\n1\nn\n0\n0\n", "4\n1\n", "0\n"):
            result = setup.main(args, TerminalText(answers), TerminalText())
            self.assertEqual(result, 0)
            self.assertFalse(path.exists())
            self.assertFalse(self.state.exists())

    def test_custom_path_preview_edit_and_existing_block_locator_recovery(self):
        first = self.root / "first.fish"
        chosen = self.root / "chosen.fish"
        args = ["--shell", "fish", "--startup-file", str(first)]
        answers = TerminalText(f"4\n1\np\n{chosen}\n\ny\n0\n0\n")
        with patch("askai_cli.setup.startup.default_paths", side_effect=AssertionError("must not scan real defaults")):
            self.assertEqual(setup.main(args, answers, TerminalText()), 0)
        self.assertFalse(first.exists())
        self.assertIsNotNone(startup.locate(chosen.read_text()))
        original = chosen.read_bytes()
        self.state.unlink()
        args = ["--shell", "fish", "--startup-file", str(chosen)]
        self.assertEqual(setup.main(args, TerminalText("2\n5\n0\n0\n"), TerminalText()), 0)
        self.assertEqual(chosen.read_bytes(), original)
        self.assertIn(str(chosen), setup.read_state()["locations"]["fish"])

    def test_bash32_and_precompiled_zsh_block_hook_configuration(self):
        version = subprocess.check_output(["/bin/bash", "--version"], text=True)
        if "version 3." in version:
            with self.assertRaisesRegex(AskError, "Bash 低于 4"):
                startup.plan([self.root / ".bashrc"], "bash", True, True, executable="/bin/bash")
        path = self.root / ".zshrc"
        Path(str(path) + ".zwc").write_text("compiled fixture")
        with self.assertRaisesRegex(AskError, "预编译"):
            startup.plan([path], "zsh", True, True)


class ShellSetupIntegrationTests(unittest.TestCase):
    def test_managed_loader_retries_after_path_setup_and_does_not_export_marker(self):
        with tempfile.TemporaryDirectory(prefix="askai-loader-") as folder:
            path = Path(folder).resolve()
            binary = path / "bin"
            binary.mkdir()
            counter = path / "calls"
            tool = binary / "askai"
            tool.write_text(f"#!{sys.executable}\nimport os,pathlib,sys\np=pathlib.Path(os.environ['ASKAI_LOADER_COUNTER'])\np.write_text(str(int(p.read_text())+1 if p.exists() else 1))\nif os.environ.get('ASKAI_LOADER_FAIL'):\n print(\"printf SHOULD_NOT_RUN\");sys.exit(2)\nprint(\"printf 'LOADED\\\\n'\")\n")
            tool.chmod(0o755)
            env = {**os.environ, "PATH": str(binary) + ":/usr/bin:/bin", "ASKAI_NO_SETUP": "1",
                   "ASKAI_LOADER_COUNTER": str(counter)}
            for shell in ("fish", "zsh", "bash"):
                with self.subTest(shell=shell):
                    counter.unlink(missing_ok=True)
                    rc = path / ("loader." + shell)
                    rc.write_text(startup.loader(shell, False, False))
                    executable = shutil.which(shell)
                    flags = {"fish": ["--no-config"], "zsh": ["-f"], "bash": ["--noprofile", "--norc"]}[shell]
                    nested = " ".join(shlex.quote(arg) for arg in [executable, *flags, "-i", "-c", f"source {shlex.quote(str(rc))}"])
                    command = f"source {shlex.quote(str(rc))}; source {shlex.quote(str(rc))}; {nested}"
                    code, output = fixtures.terminal([executable, *flags, "-i", "-c", command], env, ())
                    self.assertEqual(code, 0, output)
                    self.assertEqual(counter.read_text(), "2", output)
                    self.assertEqual(output.count("LOADED"), 2, output)
            counter.unlink(missing_ok=True)
            bashrc = path / "bashrc"
            profile = path / "profile"
            bashrc.write_text(startup.loader("bash", False, False))
            profile.write_text(f". {shlex.quote(str(bashrc))}\nPATH={shlex.quote(str(binary))}:$PATH\n" + startup.loader("bash", False, False))
            code, output = fixtures.terminal([shutil.which("bash"), "--noprofile", "--norc", "-i", "-c",
                                             f". {shlex.quote(str(profile))}"], {**env, "PATH": "/usr/bin:/bin"}, ())
            self.assertEqual(code, 0, output)
            self.assertEqual(counter.read_text(), "1", output)
            code, output = fixtures.terminal(["/bin/sh", "-i", "-c", startup.loader("bash", False, False)], env, ())
            self.assertNotIn("LOADED", output)
            self.assertEqual(counter.read_text(), "1", output)
            counter.unlink()
            rc = path / "loader.fish"
            command = (f"set -gx ASKAI_LOADER_FAIL 1; source {shlex.quote(str(rc))}; "
                       f"set -e ASKAI_LOADER_FAIL; source {shlex.quote(str(rc))}")
            code, output = fixtures.terminal([shutil.which("fish"), "--no-config", "-i", "-c", command], env, ())
            self.assertEqual(code, 0, output)
            self.assertNotIn("SHOULD_NOT_RUN", output)
            self.assertEqual(counter.read_text(), "2", output)

    def test_settings_cli_then_real_shells_without_prefix(self):
        with tempfile.TemporaryDirectory(prefix="askai-wizard-") as folder:
            path = Path(folder)
            env = {**os.environ, "XDG_STATE_HOME": str(path / "state"), "ASKAI_NO_SETUP": "1",
                   "PATH": str(fixtures.ROOT / "bin") + os.pathsep + os.environ["PATH"]}
            model = path / "model.py"
            model.write_text("import sys,json\nsys.stdin.read()\nprint(" + repr(json.dumps(fixtures.response())) + ")\n")
            config = path / "model.toml"
            config.write_text('provider="fixture"\n[providers.fixture]\ntype="command"\nexecutable=' +
                              json.dumps(sys.executable) + '\nargs=[' + json.dumps(str(model)) + ']\n')
            env["ASKAI_CONFIG"] = str(config)
            for key in ("ASKAI_PROVIDER", "ASKAI_MODEL", "ASKAI_COMMAND_FILE", "BASH_ENV", "ENV"):
                env.pop(key, None)
            for shell in ("fish", "zsh", "bash"):
                with self.subTest(shell=shell):
                    rc = path / ("config." + shell)
                    executable = shutil.which(shell)
                    args = [str(fixtures.ROOT / "bin/askai-config"), "--shell", shell, "--startup-file", str(rc), "--executable", executable]
                    code, output = fixtures.terminal(args, env, ("4\n", "1\n", "y\n", "0\n", "0\n"),
                                                    prompt_markers=("[0]: ", "[0]: ", "确认应用以上变更？[y/N]: ", "[0]: ", "[0]: "))
                    self.assertEqual(code, 0, output)
                    self.assertIsNotNone(startup.locate(rc.read_text()))
                    flags = {"fish": ["--no-config"], "zsh": ["-f"], "bash": ["--noprofile", "--norc"]}[shell]
                    code, output = fixtures.terminal([executable, *flags, "-i", "-c", f"source {shlex.quote(str(rc))}; 怎样查看磁盘占用"],
                                                    env, ("q\n",))
                    self.assertEqual(code, 127, output)
                    self.assertIn("正在询问 fixture", output)
                    result = subprocess.run([*args, "--status"], env=env, capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("已配置 " + shell, result.stdout)
                    code, output = fixtures.terminal(args, env, ("2\n", "6\n", "y\n", "0\n", "0\n"),
                                                    prompt_markers=("[0]: ", "[0]: ", "确认应用以上变更？[y/N]: ", "[0]: ", "[0]: "))
                    self.assertEqual(code, 0, output)
                    self.assertFalse(rc.exists())

    def test_legacy_fish_handler_is_not_backed_up_over_its_own_fallback(self):
        with tempfile.TemporaryDirectory(prefix="askai-legacy-hook-") as folder:
            script = Path(folder) / "init.fish"
            script.write_text(integration("fish", True, True))
            legacy = ("function __askai_original_not_found; echo ORIGINAL_FALLBACK; end; "
                      "set -g __askai_hook_installed 1; "
                      "function fish_command_not_found; __askai_original_not_found $argv; end; ")
            code, output = fixtures.terminal(["fish", "--no-config", "-i", "-c",
                                             legacy + f"source {shlex.quote(str(script))}; asktesttypo"],
                                            {**os.environ, "ASKAI_NO_SETUP": "1"}, ())
            self.assertEqual(code, 127, output)
            self.assertIn("ORIGINAL_FALLBACK", output)
            self.assertIn("旧版", output)

    def test_unspaced_chinese_calls_model_in_zsh_and_modern_bash(self):
        with tempfile.TemporaryDirectory(prefix="askai-chinese-hook-") as folder:
            path = Path(folder)
            capture = path / "request"
            model = path / "model.py"
            model.write_text("import sys,json,pathlib\npathlib.Path(" + repr(str(capture)) + ").write_text(sys.stdin.read())\nprint(" +
                             repr(json.dumps(fixtures.response(commands=[]))) + ")\n")
            config = path / "config.toml"
            config.write_text('provider="fixture"\n[providers.fixture]\ntype="command"\nexecutable=' +
                              json.dumps(sys.executable) + '\nargs=[' + json.dumps(str(model)) + ']\n')
            env = {**os.environ, "ASKAI_CONFIG": str(config), "ASKAI_NO_SETUP": "1"}
            for key in ("ASKAI_PROVIDER", "ASKAI_MODEL", "ASKAI_COMMAND_FILE"):
                env.pop(key, None)
            for shell in ("zsh", "bash"):
                script = path / ("init." + shell)
                script.write_text(integration(shell, True, True))
                flags = ["-f"] if shell == "zsh" else ["--noprofile", "--norc"]
                for question in ("怎样查看磁盘占用", "查CPU看", "查CPU", "gti"):
                    with self.subTest(shell=shell, question=question):
                        capture.unlink(missing_ok=True)
                        code, output = fixtures.terminal([shell, *flags, "-i", "-c",
                                                         f"source {shlex.quote(str(script))}; {question}"], env, ())
                        if question in ("查CPU", "gti"):
                            self.assertFalse(capture.exists(), output)
                            self.assertEqual(code, 127, output)
                        else:
                            self.assertEqual(code, 2, output)
                            self.assertIn(question, capture.read_text())

    def test_handler_reload_preserves_later_plugin_and_chinese_all_shells(self):
        with tempfile.TemporaryDirectory(prefix="askai-hooks-", dir="/private/tmp") as folder:
            path = Path(folder)
            for shell in ("fish", "zsh", "bash"):
                with self.subTest(shell=shell):
                    script = path / (shell + ".init")
                    script.write_text(integration(shell, True, True))
                    if shell == "fish":
                        definition = "function fish_command_not_found; echo LATER_PLUGIN; end"
                        flags = ["--no-config"]
                    else:
                        name = "command_not_found_handler" if shell == "zsh" else "command_not_found_handle"
                        definition = f"{name}() {{ echo LATER_PLUGIN; }}"
                        flags = ["-f"] if shell == "zsh" else ["--noprofile", "--norc"]
                    command = f"source {shlex.quote(str(script))}; {definition}; source {shlex.quote(str(script))}; asktesttypo"
                    code, output = fixtures.terminal([shell, *flags, "-i", "-c", command], {**os.environ, "ASKAI_NO_SETUP": "1"}, ())
                    self.assertIn("LATER_PLUGIN", output)
                    self.assertNotIn("正在询问", output)


if __name__ == "__main__":
    unittest.main()

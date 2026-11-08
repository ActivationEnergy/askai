"""A loaded shell function must survive replacement of its Homebrew keg."""

from pathlib import Path
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ShellUpgradeTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("fish"), "fish is not installed")
    def test_legacy_fish_function_is_migrated_and_then_survives_reinstallation(self):
        with tempfile.TemporaryDirectory(prefix="askai-legacy-upgrade-") as directory:
            root = Path(directory)
            old, new = root / "Cellar/askai/0.1.0", root / "Cellar/askai/0.1.2"
            for prefix, version in ((old, "0.1.0"), (new, "0.1.2")):
                package = prefix / "libexec/askai_cli"
                shutil.copytree(ROOT / "src/askai_cli", package, ignore=shutil.ignore_patterns("__pycache__"))
                (package / "__init__.py").write_text(f"__version__ = {version!r}\n")
            opt = root / "opt/askai"
            opt.parent.mkdir()
            opt.symlink_to(old, target_is_directory=True)
            # Reproduce the old wrapper's physical Cellar import path exactly.
            bootstrap = ("import sys;sys.path.insert(0," + repr(str(old / "libexec")) + ");"
                         "from askai_cli.shells import integration;print(integration('fish',aliases=True))")
            legacy = root / "legacy.fish"
            # Do not let another installed askai mask the missing legacy keg.
            legacy.write_text(subprocess.check_output([sys.executable, "-I", "-c", bootstrap], text=True)
                              .replace(" -I -c ", " -I -S -c "))
            (new / "bin").mkdir()
            entry = new / "bin/askai"
            entry.write_text(f"#!{sys.executable}\nimport sys\nsys.path.insert(0,{str(opt / 'libexec')!r})\n"
                             f"from askai_cli.cli import main\nmain(launcher=({str(opt / 'bin/askai')!r},))\n")
            entry.chmod(0o755)
            (new / "bin/askdsh").symlink_to("askai")
            backup = root / "download"
            shutil.copytree(new, backup, symlinks=True)
            upgrade = root / "upgrade.py"
            upgrade.write_text("from pathlib import Path\nimport shutil\n"
                               + f"p=Path({str(opt)!r});p.unlink();p.symlink_to({str(new)!r},target_is_directory=True)\n"
                               + f"shutil.rmtree({str(old)!r})\n")
            reinstall = root / "reinstall.py"
            reinstall.write_text("import shutil\n" + f"shutil.rmtree({str(new)!r})\n"
                                 + f"shutil.copytree({str(backup)!r},{str(new)!r},symlinks=True)\n")
            python = shlex.quote(sys.executable)
            app = shlex.quote(str(opt / "bin/askai"))
            error = root / "legacy-error"
            commands = (f"source {shlex.quote(str(legacy))}; askdsh --version; "
                        f"{python} -I {shlex.quote(str(upgrade))}; "
                        f"{shlex.quote(str(opt / 'bin/askdsh'))} --version; "
                        f"askdsh --version 2>{shlex.quote(str(error))}; "
                        "printf 'legacy-status:%s\\n' $status; functions -e askai askdsh; "
                        f"{app} init fish --aliases | source; askdsh --version; "
                        f"{python} -I {shlex.quote(str(reinstall))}; askdsh --version")
            env = {**os.environ, "HOME": str(root), "ASKAI_NO_SETUP": "1", "TERM": "dumb"}
            result = subprocess.run(["fish", "--no-config", "-c", commands], env=env,
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines(),
                             ["0.1.0", "0.1.2", "legacy-status:1", "0.1.2", "0.1.2"])
            self.assertIn("ModuleNotFoundError", error.read_text())

    def test_loaded_functions_and_han_probe_follow_replaced_installation(self):
        for shell in ("fish", "zsh", "bash"):
            if not shutil.which(shell):
                continue
            with self.subTest(shell=shell), tempfile.TemporaryDirectory(prefix="askai-upgrade-") as directory:
                root = Path(directory)
                old, new = root / "Cellar/askai/old", root / "Cellar/askai/new"
                opt = root / "opt/askai"
                opt.parent.mkdir()
                for prefix, version in ((old, "0.0.1"), (new, "0.0.2")):
                    package = prefix / "libexec/askai_cli"
                    shutil.copytree(ROOT / "src/askai_cli", package, ignore=shutil.ignore_patterns("__pycache__"))
                    (package / "__init__.py").write_text(f"__version__ = {version!r}\n")
                    (prefix / "bin").mkdir()
                    # The old interpreter path disappears too, not just modules.
                    (prefix / "bin/python").symlink_to(sys.executable)
                    entry = prefix / "bin/askai"
                    entry.write_text(f"#!{opt}/bin/python -I\nimport sys\n"
                                     f"sys.path.insert(0,{str(opt / 'libexec')!r})\n"
                                     "from askai_cli.cli import main\n"
                                     f"main(launcher=({str(opt / 'bin/askai')!r},))\n")
                    entry.chmod(0o755)
                opt.symlink_to(old, target_is_directory=True)
                init = root / "init"
                init.write_text(subprocess.check_output(
                    [str(opt / "bin/askai"), "init", shell, "--hook", "--aliases"], text=True))
                self.assertNotIn(str(old), init.read_text())
                self.assertNotIn(sys.executable, init.read_text())
                backup = root / "download"
                shutil.copytree(new, backup, symlinks=True)
                upgrade = root / "upgrade.py"
                upgrade.write_text("from pathlib import Path\nimport shutil\n"
                                   + f"p=Path({str(opt)!r});p.unlink();p.symlink_to({str(new)!r},target_is_directory=True)\n"
                                   + f"shutil.rmtree({str(old)!r})\n")
                reinstall = root / "reinstall.py"
                reinstall.write_text("import shutil\n" + f"shutil.rmtree({str(new)!r})\n"
                                     + f"shutil.copytree({str(backup)!r},{str(new)!r},symlinks=True)\n")
                remove = root / "remove.py"
                remove.write_text(f"import shutil;shutil.rmtree({str(new)!r})\n")
                python = shlex.quote(sys.executable)
                # Hijacked PATH and Python import directories must not take over.
                marker = root / "unexpected"
                trap = f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
                for name in ("sitecustomize.py", "json.py", "unicodedata.py"):
                    (root / name).write_text(trap)
                fake = root / "askai"
                fake.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\n")
                fake.chmod(0o755)
                commands = (f"source {shlex.quote(str(init))}; askcc --version; "
                            f"{python} -I {shlex.quote(str(upgrade))}; askcc --version; "
                            f"{python} -I {shlex.quote(str(reinstall))}; askcc --version; ")
                if shell != "fish":
                    # Call the actual interactive handler, substituting only the
                    # model invocation. The Chinese eligibility probe stays real.
                    handler = "command_not_found_handler" if shell == "zsh" else "command_not_found_handle"
                    commands += ("askai() { printf 'han:%s\\n' \"$2\"; }; "
                                 f"{handler} 查盘; {handler} 查CPU; "
                                 f"source {shlex.quote(str(init))}; ")
                commands += f"{python} -I {shlex.quote(str(remove))}; askcc --version"
                flags = {"fish": ["--no-config"], "zsh": ["-f"], "bash": ["--noprofile", "--norc"]}[shell]
                env = {**os.environ, "HOME": str(root), "ASKAI_NO_SETUP": "1", "TERM": "dumb",
                       "PATH": str(root) + os.pathsep + os.environ["PATH"], "PYTHONPATH": str(root),
                       "ASKAI_LAUNCHER": str(fake)}
                result = subprocess.run([shell, *flags, "-i", "-c", commands], env=env, cwd=root,
                                        capture_output=True, text=True, timeout=20)
                self.assertEqual(result.returncode, 127, result.stderr)
                expected = ["0.0.1", "0.0.2", "0.0.2"]
                if shell != "fish":
                    expected += ["han:查盘"]
                self.assertEqual(result.stdout.splitlines(), expected, result.stderr)
                self.assertFalse(marker.exists())
                self.assertFalse(old.exists())

    def test_virtualenv_launcher_survives_recreation_at_the_same_path(self):
        with tempfile.TemporaryDirectory(prefix="askai-venv-upgrade-") as directory:
            root = Path(directory)
            envpath = root / "tool"
            install = root / "install.py"
            install.write_text("import pathlib,shutil,sys,venv,subprocess\n"
                              f"p=pathlib.Path({str(envpath)!r})\n"
                              "if p.exists(): shutil.rmtree(p)\n"
                              "venv.EnvBuilder(with_pip=False, symlinks=True).create(p)\n"
                              "dest=subprocess.check_output([str(p/'bin/python'),'-I','-c',"
                              "\"import sysconfig;print(sysconfig.get_path('purelib'))\"],text=True).strip()\n"
                              f"pkg=pathlib.Path(dest)/'askai_cli';shutil.copytree({str(ROOT / 'src/askai_cli')!r},pkg,ignore=shutil.ignore_patterns('__pycache__'))\n"
                              "(pkg/'__init__.py').write_text('__version__ = '+repr(sys.argv[1])+'\\n')\n")
            subprocess.run([sys.executable, "-I", str(install), "0.0.1"], check=True)
            python = envpath / "bin/python"
            init = root / "init"
            init.write_text(subprocess.check_output([str(python), "-I", "-m", "askai_cli", "init", "zsh", "--aliases"], text=True))
            self.assertIn(shlex.quote(str(python)) + " -I -m askai_cli", init.read_text())
            if not shutil.which("zsh"):
                self.skipTest("zsh is not installed")
            # When available, rebuild with a different Python minor version:
            # site-packages moves, while the loaded function must remain usable.
            replacement = next((path for minor in range(11, 16)
                                if minor != sys.version_info.minor
                                and (path := shutil.which(f"python3.{minor}"))), sys.executable)
            command = (f"source {shlex.quote(str(init))}; askcc --version; "
                       f"{shlex.quote(replacement)} -I {shlex.quote(str(install))} 0.0.2; askcc --version")
            result = subprocess.run(["zsh", "-f", "-c", command], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines(), ["0.0.1", "0.0.2"])

    def test_source_entry_stays_bound_and_ignores_project_python_modules(self):
        with tempfile.TemporaryDirectory(prefix="askai-source-launcher-") as directory:
            root = Path(directory)
            marker = root / "unexpected"
            trap = f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
            for name in ("sitecustomize.py", "json.py", "unicodedata.py"):
                (root / name).write_text(trap)
            fake = root / "askai"
            fake.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\n")
            fake.chmod(0o755)
            env = {**os.environ, "PYTHONPATH": str(root),
                   "PATH": str(root) + os.pathsep + os.environ["PATH"]}
            for shell in ("fish", "zsh", "bash"):
                if not shutil.which(shell):
                    continue
                with self.subTest(shell=shell):
                    init = root / "init"
                    init.write_text(subprocess.check_output(
                        [str(ROOT / "bin/askai"), "init", shell, "--aliases"],
                        env=env, cwd=root, text=True))
                    self.assertIn(shlex.quote(str(ROOT / "bin/askai")), init.read_text())
                    command = f"source {shlex.quote(str(init))}; askcc --version"
                    flags = {"fish": ["--no-config"], "zsh": ["-f"], "bash": ["--noprofile", "--norc"]}[shell]
                    result = subprocess.run([shell, *flags, "-c", command], cwd=root, env=env,
                                            capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertRegex(result.stdout.strip(), r"^\d+\.\d+\.\d+$")
                    self.assertFalse(marker.exists())

    def test_han_probe_has_no_config_or_model_side_effects(self):
        with tempfile.TemporaryDirectory(prefix="askai-han-") as directory:
            broken = Path(directory) / "broken.toml"
            broken.write_text("not a valid config [")
            env = {**os.environ, "ASKAI_CONFIG": str(broken)}
            for args, status in ((["--", "查盘"], 0), (["--", "查CPU"], 1), (["--", "abc"], 1), ([], 2)):
                result = subprocess.run([str(ROOT / "bin/askai"), "__askai_han_probe", *args],
                                        env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, status, result.stderr)
                self.assertEqual(result.stdout + result.stderr, "")


if __name__ == "__main__":
    unittest.main()

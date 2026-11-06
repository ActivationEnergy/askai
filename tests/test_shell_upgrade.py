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
    def test_loaded_shortcuts_follow_opt_after_old_version_is_removed(self):
        for shell in ("fish", "zsh", "bash"):
            if not shutil.which(shell):
                continue
            with self.subTest(shell=shell), tempfile.TemporaryDirectory(prefix="askai-upgrade-") as directory:
                root = Path(directory)
                old, new = root / "Cellar/askai/0.0.1", root / "Cellar/askai/0.0.2"
                for prefix, version in ((old, "0.0.1"), (new, "0.0.2")):
                    package = prefix / "libexec/askai_cli"
                    shutil.copytree(ROOT / "src/askai_cli", package, ignore=shutil.ignore_patterns("__pycache__"))
                    (package / "__init__.py").write_text(f"__version__ = {version!r}\n")
                opt = root / "opt/askai"
                opt.parent.mkdir()
                opt.symlink_to(old, target_is_directory=True)
                bootstrap = ("import sys;sys.path.insert(0," + repr(str(opt / "libexec")) + ");"
                             "from askai_cli.shells import integration;print(integration(" + repr(shell) + ",aliases=True))")
                result = subprocess.run([sys.executable, "-I", "-c", bootstrap], capture_output=True, text=True, check=True)
                init = root / "init"
                init.write_text(result.stdout)
                upgrade = root / "upgrade.py"
                upgrade.write_text("from pathlib import Path\nimport shutil\n"
                                   + f"opt=Path({str(opt)!r});opt.unlink();opt.symlink_to({str(new)!r},target_is_directory=True)\n"
                                   + f"shutil.rmtree({str(old)!r})\n")
                commands = ("source " + shlex.quote(str(init)) + "; askcc --version; "
                            + shlex.quote(sys.executable) + " -I " + shlex.quote(str(upgrade))
                            + "; askcc --version")
                flags = {"fish": ["--no-config"], "zsh": ["-f"], "bash": ["--noprofile", "--norc"]}[shell]
                env = {**os.environ, "HOME": str(root), "ASKAI_NO_SETUP": "1", "TERM": "dumb"}
                result = subprocess.run([shell, *flags, "-c", commands], env=env,
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.splitlines(), ["0.0.1", "0.0.2"])
                self.assertFalse(old.exists())


if __name__ == "__main__":
    unittest.main()

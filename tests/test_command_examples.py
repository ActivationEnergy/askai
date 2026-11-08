"""Run fixed, harmless reference commands; these do not grade real AI replies."""

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class LineNumberExamples(unittest.TestCase):
    @unittest.skipUnless(shutil.which("cat"), "cat is not installed")
    def test_cat_numbering_includes_blank_lines_and_keeps_the_file_unchanged(self):
        contents = "first\n\n第三行\n  leading spaces\n"
        with tempfile.TemporaryDirectory(prefix="askai-cat-example-") as folder:
            path = Path(folder) / "input file.txt"
            path.write_text(contents, encoding="utf-8")
            before = path.read_bytes()
            result = subprocess.run([shutil.which("cat"), "-n", str(path)], capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            numbered = [line.split("\t", 1) for line in result.stdout.splitlines()]
            self.assertEqual([int(number.strip()) for number, _ in numbered], [1, 2, 3, 4])
            self.assertEqual([text for _, text in numbered], contents.splitlines())
            self.assertEqual(path.read_bytes(), before)

    @unittest.skipUnless(shutil.which("nl"), "nl is not installed")
    def test_nl_text_mode_keeps_blank_lines_without_numbering_them(self):
        result = subprocess.run([shutil.which("nl"), "-bt"], input="first\n\nthird\n", capture_output=True,
                                text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        numbered = [line.split("\t", 1) for line in result.stdout.splitlines()]
        self.assertEqual([(number.strip(), text) for number, text in numbered], [("1", "first"), ("", ""), ("2", "third")])

    @unittest.skipUnless(shutil.which("nl"), "nl is not installed")
    def test_nl_body_style_option_requires_an_argument(self):
        result = subprocess.run([shutil.which("nl"), "-b"], input="first\n\nthird\n",
                                capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(result.stderr)

    @unittest.skipUnless(shutil.which("nl"), "nl is not installed")
    def test_minimal_and_custom_format_number_the_same_ordinary_lines(self):
        contents = "first\n\n第三行\n  leading spaces\n"
        with tempfile.TemporaryDirectory(prefix="askai-line-example-") as folder:
            path = Path(folder) / "file with spaces.txt"
            path.write_text(contents, encoding="utf-8")
            before = path.read_bytes()
            for options, separator in ((["-ba"], "\t"), (["-ba", "-w", "4", "-s", ". "], ". ")):
                with self.subTest(options=options):
                    result = subprocess.run([shutil.which("nl"), *options, str(path)], capture_output=True,
                                            text=True, timeout=5)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    numbered = [line.split(separator, 1) for line in result.stdout.splitlines()]
                    self.assertEqual([int(number.strip()) for number, _ in numbered], [1, 2, 3, 4])
                    self.assertEqual([text for _, text in numbered], contents.splitlines())
                    self.assertEqual(path.read_bytes(), before)

    @unittest.skipUnless(shutil.which("awk"), "awk is not installed")
    def test_awk_stdin_reference_keeps_blank_lines_and_literal_text(self):
        lines = ["first", "", "第三行", r"\:\:\:", "  leading spaces"]
        result = subprocess.run([shutil.which("awk"), '{print NR "\t" $0}'], input="\n".join(lines) + "\n",
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [f"{i}\t{line}" for i, line in enumerate(lines, 1)])


class FileCountExamples(unittest.TestCase):
    @unittest.skipUnless(all(shutil.which(name) for name in ("find", "ls", "wc")), "required tools unavailable")
    def test_ordinary_file_count_is_not_visible_entry_count(self):
        # No embedded newlines in this fixture: this checks object selection,
        # not whether newline-delimited output handles every possible filename.
        with tempfile.TemporaryDirectory(prefix="askai-file-count-") as folder:
            root = Path(folder)
            for name in ("plain", "two words", ".hidden"):
                (root / name).write_text("keep")
            (root / "sub").mkdir()
            (root / "empty").mkdir()
            (root / "sub/nested").write_text("keep")
            (root / "link").symlink_to("plain")
            for shell in ("fish", "zsh", "bash"):
                if not shutil.which(shell):
                    continue
                flags = {"fish": ["--no-config"], "zsh": ["-f"], "bash": ["--noprofile", "--norc"]}[shell]
                for command, expected in (("find . -maxdepth 1 -type f | wc -l", 3), ("ls -1 | wc -l", 5)):
                    with self.subTest(shell=shell, command=command):
                        result = subprocess.run([shell, *flags, "-c", command], cwd=root,
                                                capture_output=True, text=True, timeout=5)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(int(result.stdout.strip()), expected)


if __name__ == "__main__":
    unittest.main()

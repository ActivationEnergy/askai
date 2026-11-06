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


if __name__ == "__main__":
    unittest.main()

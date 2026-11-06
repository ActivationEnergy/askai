"""Text-only input must not attach files or invoke models for removed options."""

import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from askai_cli import providers
import test_ask as fixtures


class TextOnlyTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.EndToEndTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.path, self.env = fixture.path, fixture.env
        self.capture = self.path / "captured-request"
        self.env["ASKAI_TEST_CAPTURE"] = str(self.capture)

    def invoke(self, *args):
        return subprocess.run([str(fixtures.ASKAI), *args], env=self.env,
                              capture_output=True, text=True, timeout=10)

    def test_removed_attachment_flags_fail_before_model_invocation(self):
        attachment = self.path / "attachment.png"
        attachment.write_text("FILE_CONTENT_MUST_NOT_BE_SENT")
        for flag in ("--image", "-i", "--paste-image"):
            with self.subTest(flag=flag):
                result = self.invoke(flag, str(attachment), "explain this problem")
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertNotIn("Traceback", result.stderr)
                self.assertFalse(self.capture.exists())

    def test_help_and_empty_invocation_do_not_request_input_or_call_a_model(self):
        for arguments in (("--help",), ()):
            result = self.invoke(*arguments)
            self.assertEqual(result.returncode, 0)
            self.assertIn("--provider", result.stdout)
            self.assertNotIn("--image", result.stdout)
            self.assertFalse(self.capture.exists())

    def test_a_path_in_a_text_question_does_not_upload_file_contents(self):
        source = self.path / "data.txt"
        secret = "FILE_CONTENT_MUST_NOT_BE_SENT"
        source.write_text(secret)
        question = "How do I count lines in " + str(source) + "?"
        result = self.invoke("--print", question)
        self.assertEqual(result.returncode, 0, result.stderr)
        captured = json.loads(self.capture.read_text())
        self.assertIn(question, captured["stdin"])
        self.assertNotIn(secret, captured["stdin"])
        self.assertEqual(source.read_text(), secret)

    def test_legacy_vision_setting_does_not_change_text_backend_selection(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(providers, "available", return_value=True):
            for setting in (True, False):
                config = {"providers": {"codex": {"vision": setting}}}
                self.assertEqual(providers.select_provider(config, None)[0], "codex")


if __name__ == "__main__":
    unittest.main()

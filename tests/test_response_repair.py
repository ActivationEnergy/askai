"""Protocol compatibility and bounded repairs use fake backends only."""

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from askai_cli.cli import request_suggestions
from askai_cli.core import AskError, parse_answer
from askai_cli.debug import DebugTrace
from askai_cli.terminal import ColorPolicy, Terminal
import test_ask as fixtures


class ProtocolRepairTests(unittest.TestCase):
    def test_empty_script_command_is_removed_but_real_conflicts_are_rejected(self):
        script = fixtures.python_script()
        for empty in (None, "", " \n\t"):
            with self.subTest(empty=empty):
                result = parse_answer(json.dumps(fixtures.response(commands=[{**script, "command": empty}])))
                self.assertEqual(result["commands"], [script])
                self.assertNotIn("command", result["commands"][0])
        for conflict in ("echo wrong", "print('hello')", [], {}, False, 0, "\x1b[0m"):
            with self.subTest(conflict=conflict), self.assertRaises(AskError):
                parse_answer(json.dumps(fixtures.response(commands=[{**script, "command": conflict}])))

    def test_repair_preserves_backend_model_and_remaining_timeout(self):
        provider = {"type": "dsh", "executable": "fake"}
        invalid = json.dumps(fixtures.response(commands=[{**fixtures.python_script(), "command": "echo ambiguous"}]))
        valid = fixtures.response("printf repaired")
        output = io.StringIO()
        with patch("askai_cli.cli.answer", side_effect=[invalid, json.dumps(valid)]) as backend, \
             patch("askai_cli.cli.time.monotonic", side_effect=[10.0, 12.0]):
            result = request_suggestions(provider, "same-model", "system", "original-question", 10,
                                         DebugTrace(), Terminal(output, ColorPolicy("never")))
        self.assertEqual(result, valid)
        self.assertEqual(backend.call_count, 2)
        for call in backend.call_args_list:
            self.assertIs(call.args[0], provider)
            self.assertEqual(call.args[1], "same-model")
        second = backend.call_args_list[1].args
        self.assertEqual(second[4], 8.0)
        self.assertTrue(second[3].startswith("original-question\n\n"))
        correction = json.loads(second[3].split("\n\n", 1)[1])["format_repair"]
        self.assertEqual(correction["previous_response"], invalid)
        self.assertIn("同一后端最多重试一次", output.getvalue())

    def test_one_repair_only_and_no_retry_for_transport_or_valid_clarification(self):
        ui = Terminal(io.StringIO(), ColorPolicy("never"))
        for answers, expected in ((["not json", "still not json"], 2), ([AskError("authentication failed")], 1)):
            with patch("askai_cli.cli.answer", side_effect=answers) as backend, self.assertRaises(AskError):
                try:
                    request_suggestions({}, None, "system", "question", 10, DebugTrace(), ui)
                finally:
                    self.assertEqual(backend.call_count, expected)
        clarification = fixtures.response(commands=[])
        with patch("askai_cli.cli.answer", return_value=json.dumps(clarification)) as backend:
            self.assertEqual(request_suggestions({}, None, "system", "question", 10, DebugTrace(), ui), clarification)
            self.assertEqual(backend.call_count, 1)

    def test_large_invalid_output_and_exhausted_budget_do_not_add_requests(self):
        ui = Terminal(io.StringIO(), ColorPolicy("never"))
        for raw, times in (("x" * (64 * 1024 + 1), [0, 1]), ("invalid", [0, 11]), (None, [0, 1])):
            with self.subTest(size=len(raw) if raw else 0), \
                 patch("askai_cli.cli.answer", return_value=raw) as backend, \
                 patch("askai_cli.cli.time.monotonic", side_effect=times), self.assertRaises(AskError):
                try:
                    request_suggestions({}, None, "system", "question", 10, DebugTrace(), ui)
                finally:
                    self.assertEqual(backend.call_count, 1)


class RepairCliTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.EndToEndTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.path, self.env = fixture.path, fixture.env
        self.env["ASKAI_PROVIDER"] = "test"
        self.count = self.path / "request-count"
        self.capture = self.path / "repair-prompt"
        self.marker = self.path / "must-not-execute"
        self.selected = self.path / "selected"
        self.env.update(ASKAI_TEST_REQUEST_COUNT=str(self.count), ASKAI_TEST_REPAIR_PROMPT=str(self.capture),
                        ASKAI_COMMAND_FILE=str(self.selected))
        fixture.fake.write_text(f"#!{sys.executable}\n" + '''import json,os,pathlib,sys
payload=sys.stdin.read()
counter=pathlib.Path(os.environ['ASKAI_TEST_REQUEST_COUNT'])
number=int(counter.read_text())+1 if counter.exists() else 1
counter.write_text(str(number))
pathlib.Path(os.environ['ASKAI_TEST_REPAIR_PROMPT']).write_text(payload)
print(os.environ['ASKAI_TEST_ANSWER'] if number==1 else os.environ['ASKAI_TEST_REPAIRED_ANSWER'])
''')
        self.invalid = fixtures.response(commands=[{**fixtures.python_script(f"from pathlib import Path\nPath({str(self.marker)!r}).touch()"),
                                                     "command": "touch " + str(self.marker)}])
        self.env.update(ASKAI_TEST_ANSWER=json.dumps(self.invalid),
                        ASKAI_TEST_REPAIRED_ANSWER=json.dumps(fixtures.response("printf repaired")))

    def invoke(self, *args):
        self.count.unlink(missing_ok=True)
        self.selected.write_text("stale unsafe command")
        return subprocess.run([str(fixtures.ASKAI), *args], env=self.env, capture_output=True, text=True, timeout=15)

    def test_command_repair_print_and_json_stay_clean_and_do_not_execute(self):
        for mode in ("--print", "--json"):
            result = self.invoke("--debug", mode, "给文本每行加行号")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self.count.read_text(), "2")
            self.assertEqual(self.selected.read_text(), "")
            self.assertFalse(self.marker.exists())
            self.assertIn("修正回答格式", result.stderr)
            self.assertIn("format_repair", self.capture.read_text())
            if mode == "--print":
                self.assertEqual(result.stdout, "printf repaired\n")
            else:
                self.assertEqual(json.loads(result.stdout), fixtures.response("printf repaired"))

    def test_failed_repair_never_exposes_a_candidate_or_writes_execution_file(self):
        self.env["ASKAI_TEST_REPAIRED_ANSWER"] = json.dumps(self.invalid)
        result = self.invoke("--print", "给文本每行加行号")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(self.count.read_text(), "2")
        self.assertEqual(self.selected.read_text(), "")
        self.assertFalse(self.marker.exists())
        self.assertIn("修正后仍不符合格式要求", result.stderr)

    def test_empty_placeholder_needs_no_second_request_and_json_is_normalized(self):
        script = fixtures.python_script()
        self.env["ASKAI_TEST_ANSWER"] = json.dumps(fixtures.response(commands=[{**script, "command": None}]))
        result = self.invoke("--json", "给文本每行加行号")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.count.read_text(), "1")
        self.assertEqual(json.loads(result.stdout)["commands"], [script])
        self.assertFalse(self.marker.exists())


if __name__ == "__main__":
    unittest.main()

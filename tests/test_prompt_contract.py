"""Quality cases stay outside the shared system policy and request protocol."""

import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from askai_cli import core, terminal
from askai_cli.cli import run


CASES = Path(__file__).with_name("fixtures") / "answer_quality_cases.json"


class PromptContractTests(unittest.TestCase):
    def test_common_policy_is_separate_from_task_and_evaluation_criteria(self):
        cases = json.loads(CASES.read_text(encoding="utf-8"))
        self.assertEqual(len({case["id"] for case in cases}), len(cases))
        self.assertGreaterEqual(len({case["category"] for case in cases}), 5)
        token = terminal._policy.set(None)
        self.addCleanup(terminal._policy.reset, token)
        reply = {"explanation": "offline transport fixture, not a quality judgement", "commands": []}
        with tempfile.TemporaryDirectory(prefix="askai-prompt-contract-") as folder:
            config = Path(folder) / "config.toml"
            config.write_text('provider="fixture"\n[providers.fixture]\ntype="api"\nbase_url="https://example.invalid"\napi_key_env="OFFLINE_KEY"\nmodel="fixture"\n')
            env = {"ASKAI_CONFIG": str(config), "ASKAI_PROVIDER": "fixture", "OFFLINE_KEY": "OFFLINE_FAKE_KEY_DO_NOT_SEND",
                   "ASKAI_NO_SETUP": "1", "TERM": "dumb"}
            for shell in core.SHELLS:
                expected_system = core.instructions(shell)
                for case in cases:
                    output = io.StringIO()
                    with self.subTest(shell=shell, case=case["id"]), patch.dict(os.environ, env, clear=True), \
                         patch("sys.stdout", output), patch("askai_cli.cli.answer", return_value=json.dumps(reply)) as backend:
                        self.assertEqual(run(["--json", "--shell", shell, "--", case["question"]]), 2)
                    provider, model, system, context, timeout = backend.call_args.args
                    self.assertEqual(system, expected_system)
                    self.assertEqual(json.loads(context)["question"], case["question"])
                    self.assertEqual(json.loads(context)["shell"], shell)
                    self.assertNotIn(case["question"], system)
                    for criterion in case["criteria"]:
                        self.assertNotIn(criterion, system)
                        self.assertNotIn(criterion, context)
                    self.assertEqual(json.loads(output.getvalue()), reply)
                    self.assertEqual(backend.call_count, 1)


if __name__ == "__main__":
    unittest.main()

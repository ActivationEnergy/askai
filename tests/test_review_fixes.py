"""Regression coverage for review findings beyond the shell exchange boundary."""
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_ask as fixtures
from askai_cli.cli import run
from askai_cli.core import AskError, parse_answer, validate_command
from askai_cli.debug import DebugTrace
from askai_cli.providers import answer, run_cli
from askai_cli.scripts import run_script, save_script, standalone_command
from askai_cli.retry import review_answers


class ReviewFixTests(unittest.TestCase):
    def test_new_provider_secret_is_added_before_debug_output(self):
        trace = DebugTrace(True, secrets=['earlier-secret'])
        provider = {'type': 'api', 'api_key_env': 'CUSTOM_CREDENTIAL'}
        output = io.StringIO()
        with patch.dict(os.environ, {'CUSTOM_CREDENTIAL': 'new-unmarked-value'}), \
             patch('askai_cli.providers.api_answer', return_value='new-unmarked-value'), \
             patch('sys.stderr', output):
            answer(provider, None, 'system', 'question', 1, trace)
        self.assertNotIn('new-unmarked-value', output.getvalue())
        self.assertEqual(trace.redact_text('earlier-secret'), '[REDACTED]')
        trace.add_secrets(['quote"\\value'])
        count = len(trace.secrets)
        for _ in range(20):
            trace.add_secrets(['quote"\\value'])
        self.assertEqual(len(trace.secrets), count)

    def test_action_failure_preserves_answer_and_does_not_request_again(self):
        data = fixtures.response()
        choices = iter([('copy', 0, None), ('print', 0, None)])
        calls = []
        def perform(answer, action, index, destination):
            calls.append(answer)
            if action == 'copy':
                raise AskError('clipboard unavailable')
            return 0
        result = review_answers(data, 'fixture', {'type': 'command'}, None, 'question', {},
            io.StringIO(), io.StringIO(), 'fish', lambda _: self.fail('unexpected request'),
            lambda *args, **kwargs: next(choices), perform=perform)
        self.assertEqual(result, 0)
        self.assertEqual(calls, [data, data])

    def test_hook_cancel_after_failed_process_start_is_not_success(self):
        class Tty(io.StringIO):
            def isatty(self):
                return True
        data = fixtures.response(commands=[fixtures.python_script()])
        config = {'provider': 'fixture', 'providers': {'fixture': {'type': 'command', 'executable': sys.executable}}}
        with patch.dict(os.environ, {'ASKAI_HOOK_INVOCATION': '1', 'ASKAI_NO_SETUP': '1'}, clear=True), \
             patch('sys.stdin', Tty()), patch('sys.stdout', Tty()), \
             patch('askai_cli.cli.load_config', return_value=config), \
             patch('askai_cli.cli.answer', return_value=json.dumps(data)), \
             patch('askai_cli.cli.validate_candidate'), \
             patch('askai_cli.cli.select_action', side_effect=[('run', 0, None), ('cancel', None, None)]), \
             patch('askai_cli.cli.run_script', side_effect=AskError('cannot start')):
            self.assertEqual(run(['test']), 127)

    def test_bom_is_accepted_without_repair(self):
        parsed = parse_answer('\ufeff' + json.dumps(fixtures.response()))
        self.assertEqual(parsed['commands'][0]['command'], "printf 'hello\\n'")

    def test_nul_cli_argument_fails_before_process_creation(self):
        with patch('askai_cli.providers.subprocess.Popen') as popen, self.assertRaises(AskError):
            run_cli([sys.executable, 'bad\0argument'], '', 1)
        popen.assert_not_called()

    def test_dangling_continuation_is_rejected(self):
        for shell in ('fish', 'zsh', 'bash'):
            with self.subTest(shell=shell), self.assertRaises(AskError):
                validate_command('printf hello' + '\\', shell)

    def test_python_actions_share_isolation_and_utf8(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'askai_local_fixture.py').write_text('raise RuntimeError("must not import")')
            code = ("import sys,os,importlib.util,json\n"
                    "print(json.dumps([importlib.util.find_spec('askai_local_fixture') is None, "
                    "sys.flags.isolated,sys.flags.utf8_mode,os.getcwd(),sys.argv[1:]],ensure_ascii=False))")
            item = fixtures.python_script(code, ['中文参数'])
            saved, command = save_script(item, str(root / 'saved.py'), 'bash')
            env = {**os.environ, 'PYTHONPATH': str(root), 'LC_ALL': 'en_US.ISO8859-1'}
            outputs = []
            for cmd in (command, standalone_command(item, 'bash')):
                result = subprocess.run(['bash', '--noprofile', '--norc', '-c', cmd],
                                        cwd=root, env=env, capture_output=True, encoding='utf-8')
                self.assertEqual(result.returncode, 0, result.stderr)
                outputs.append(json.loads(result.stdout))
            runner = ('import sys;sys.path.insert(0,' + repr(str(fixtures.ROOT / 'src')) + ');'
                      'from askai_cli.scripts import run_script;raise SystemExit(run_script(' + repr(item) + '))')
            result = subprocess.run([sys.executable, '-I', '-X', 'utf8', '-c', runner],
                                    cwd=root, env=env, capture_output=True, encoding='utf-8')
            self.assertEqual(result.returncode, 0, result.stderr)
            outputs.append(json.loads(result.stdout))
            self.assertTrue(all(row == outputs[0] for row in outputs))
            self.assertEqual(outputs[0][:3], [True, 1, 1])
            self.assertEqual(outputs[0][-1], ['中文参数'])


if __name__ == '__main__':
    unittest.main()

"""Review regressions using dummy keys, local fake CLIs and harmless scripts."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from askai_cli import core, providers, scripts
from askai_cli.debug import DebugTrace


class DebugReviewTests(unittest.TestCase):
    def test_sensitive_option_variants_and_equals_values_are_hidden(self):
        flags = ['--key', '--refresh-token', '--API_KEY', '--client_secret',
                 '--passphrase', '--accessToken', '--auth-token']
        for flag in flags:
            for joined in (False, True):
                with self.subTest(flag=flag, joined=joined):
                    secret = 'DUMMY_REVIEW_ONLY_CREDENTIAL'
                    argv = ['fake-cli', flag + '=' + secret] if joined else ['fake-cli', flag, secret]
                    trace = DebugTrace(True)
                    value = json.dumps(trace.sanitize(argv))
                    self.assertNotIn(secret, value)
                    self.assertIn('[REDACTED]', value)
                    trace.add_argv_secrets(argv)
                    self.assertNotIn(secret, trace.redact_text('backend echoed ' + secret))
        self.assertEqual(DebugTrace().sanitize(['tool', '--monkey', 'banana', '--model', 'test']),
                         ['tool', '--monkey', 'banana', '--model', 'test'])

    def test_backend_credential_echo_is_redacted_even_without_debug(self):
        # The subprocess is a local Python script and does not access a model.
        code = ('import sys\n'
                'sys.stdin.read()\n'
                'print("echo " + " ".join(sys.argv[1:]))\n'
                'print("echo " + " ".join(sys.argv[1:]), file=sys.stderr)\n'
                'sys.exit(7)\n')
        with tempfile.TemporaryDirectory(prefix='askai-debug-review-') as directory:
            path = Path(directory) / 'fake.py'
            path.write_text(code)
            for trace in (None, DebugTrace(False), DebugTrace(True)):
                for argv_extra in (['--key', 'DUMMY_SEPARATED_REVIEW_KEY'],
                                   ['--refresh_token=DUMMY_EQUALS_REVIEW_KEY']):
                    with self.subTest(debug=trace.enabled if trace else None, argv=argv_extra):
                        output = io.StringIO()
                        with contextlib.redirect_stderr(output), self.assertRaises(core.AskError) as caught:
                            providers.run_cli([sys.executable, '-I', str(path), *argv_extra], '', 5, trace)
                        text = str(caught.exception) + output.getvalue()
                        for secret in ['DUMMY_SEPARATED_REVIEW_KEY', 'DUMMY_EQUALS_REVIEW_KEY']:
                            self.assertNotIn(secret, text)
                        self.assertIn('[REDACTED]', text)
                        self.assertIn('退出码 7', text)

    def test_success_debug_redacts_echo_without_altering_provider_result(self):
        secret = 'DUMMY_SUCCESS_REVIEW_KEY'
        code = 'import sys; sys.stdin.read(); print(sys.argv[-1])'
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            result = providers.run_cli([sys.executable, '-I', '-c', code, '--token', secret], '', 5, DebugTrace(True))
        self.assertEqual(result.strip(), secret)
        self.assertNotIn(secret, output.getvalue())

    def test_args_preview_is_unambiguous_and_source_suffix_is_exact(self):
        args = ['', 'first\n  2. forged', 'tab\tvalue', 'quote"slash\\', '中文文件']
        item = {'kind': 'script', 'language': 'python', 'code': 'print("safe")\n', 'args': args}
        shown = scripts.preview(item)
        self.assertTrue(shown.endswith(item['code']))
        metadata = shown.removesuffix(item['code'])
        for index, value in enumerate(args, 1):
            self.assertIn(f'  {index}. ' + json.dumps(value, ensure_ascii=False), metadata)
        self.assertEqual(metadata.count('\n'), len(args) + 3)
        for shell in core.SHELLS:
            with self.subTest(shell=shell):
                executable = shutil.which(shell)
                if not executable:
                    continue
                candidate = {**item, 'code': 'import json,sys; print(json.dumps(sys.argv[1:]))'}
                copied = scripts.standalone_command(candidate, shell)
                flags = {"fish": ["--no-config"], "zsh": ["-f"], "bash": ["--noprofile", "--norc"]}[shell]
                with tempfile.TemporaryDirectory(prefix="askai-script-home-") as home:
                    result = subprocess.check_output([executable, *flags, '-c', copied], text=True,
                        env={**os.environ, "HOME": home, "ZDOTDIR": home, "XDG_CONFIG_HOME": home})
                self.assertEqual(json.loads(result), args)
        with patch.object(scripts.subprocess, 'call', return_value=0) as execute:
            scripts.run_script(item)
        self.assertEqual(execute.call_args.args[0][-len(args):], args)


if __name__ == '__main__':
    unittest.main()

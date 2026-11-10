"""CLI error and display boundaries use dummy data and no real model calls."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from askai_cli import cli, core

ROOT = Path(__file__).resolve().parents[1]


class CliReviewTests(unittest.TestCase):
    def test_unicode_error_exits_one_without_secondary_crash(self):
        output = io.StringIO()
        with patch.object(cli, 'run', side_effect=UnicodeError('dummy')), \
             patch.object(sys, 'argv', ['askai', 'question']), patch.object(sys, 'stderr', output), \
             self.assertRaises(SystemExit) as result:
            cli.main()
        self.assertEqual(result.exception.code, 1)
        self.assertIn('UTF-8', output.getvalue())

    def test_closed_output_and_error_pipes_exit_quietly(self):
        prefix = f'import sys;sys.path.insert(0,{str(ROOT / "src")!r});from unittest.mock import patch;from askai_cli import cli\n'
        cases = [
            ('stdout', 'with patch.object(cli,"run",side_effect=lambda *a,**k: print("small")): cli.main()'),
            ('stdout', 'with patch.object(cli,"run",side_effect=lambda *a,**k: print("x"*100000)): cli.main()'),
            ('stderr', 'with patch.object(cli,"run",side_effect=cli.AskError("dummy")): cli.main()'),
            ('stdout', 'sys.argv=["askai","--help"];cli.main()'),
            ('stdout', 'sys.argv=["askai-config","--help"];cli.main()'),
        ]
        for stream, code in cases:
            read, write = os.pipe()
            os.close(read)
            try:
                result = subprocess.run([sys.executable, '-I', '-c', prefix + code],
                    **{stream: write, 'stderr' if stream == 'stdout' else 'stdout': subprocess.PIPE}, timeout=10)
            finally:
                os.close(write)
            with self.subTest(stream=stream, code=code):
                self.assertEqual(result.returncode, 0)
                self.assertNotIn(b'BrokenPipe', (result.stderr or b'') + (result.stdout or b''))

    def test_explicit_blank_options_stop_before_provider(self):
        for option in ('--provider', '--model'):
            for value in ('', '  '):
                with patch.object(cli, 'select_provider') as backend, self.assertRaises(core.AskError):
                    cli.run([option, value, 'question'])
                backend.assert_not_called()

    def test_json_escapes_controls_but_preserves_decoded_data(self):
        text = '中文' + ''.join(chr(c) for c in (0x7f, 0x85, 0x202e, 0xe0001))
        value = {'explanation': text, 'commands': [{'label': text, 'details': text}], text: 'value'}
        encoded = core.json_for_output(value)
        self.assertEqual(json.loads(encoded), value)
        self.assertIn('中文', encoded)
        for c in text[2:]:
            self.assertNotIn(c, encoded)
        class Tty(io.StringIO):
            def isatty(self):
                return True
        answer = {'explanation': text, 'commands': [{'label': text, 'command': 'printf ok', 'warning': ''}]}
        for args, output in ((['--json', '--shell', 'bash', 'q'], io.StringIO()),
                             (['--shell', 'bash', 'q'], Tty())):
            with patch.dict(os.environ, {'ASKAI_NO_SETUP': '1'}, clear=True), \
                 patch.object(cli, 'load_config', return_value={}), \
                 patch.object(cli, 'select_provider', return_value=('test', {'type': 'command'})), \
                 patch.object(cli, 'request_suggestions', return_value=answer), \
                 patch.object(sys, 'stdout', output), patch.object(sys, 'stderr', io.StringIO()), \
                 patch.object(sys, 'stdin', io.StringIO()), patch('builtins.open', side_effect=OSError('no tty')):
                self.assertEqual(cli.run(args), 0)
            self.assertEqual(json.loads(output.getvalue()), answer)
            self.assertNotIn(chr(0x202e), output.getvalue())



if __name__ == '__main__':
    unittest.main()

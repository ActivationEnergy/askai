"""Harmless end-to-end probes for the explicit CLI/wrapper execution boundary."""
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import unittest

import test_ask as fixtures
from askai_cli.shells import integration
from askai_cli.retry import select_option
from askai_cli.scripts import quote_argument


class ExecutionBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.EndToEndTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.path
        self.env = {**self.fixture.env, 'TERM': 'xterm-256color'}
        self.marker = self.root / 'unexpected'

    def shell(self, shell, invocation, inputs=None, hook=False):
        executable = shutil.which(shell)
        if not executable:
            self.skipTest(shell + ' is not installed')
        init = self.root / (shell + '.init')
        init.write_text(integration(shell, hook=hook))
        flags = {'fish': ['--no-config'], 'zsh': ['-f'], 'bash': ['--noprofile', '--norc']}[shell]
        code = f'cd {shlex.quote(str(self.root))}; source {shlex.quote(str(init))}; {invocation}'
        if inputs is not None:
            return fixtures.terminal([executable, *flags, *(['-i'] if hook else []), '-c', code],
                                     self.env, inputs, timeout=8)
        result = subprocess.run([executable, *flags, '-c', code], env=self.env,
                                capture_output=True, text=True, timeout=8)
        return result.returncode, result.stdout + result.stderr

    def test_uppercase_actions_and_invalid_prefix_never_become_execution(self):
        self.fixture.clipboard_fixture()
        self.env.update(self.fixture.env, TERM='xterm-256color')
        self.env['ASKAI_TEST_ANSWER'] = json.dumps(fixtures.response('touch ' + shlex.quote(str(self.marker))))
        for keys in (('P1',), ('C1',), ('E1', 'q'), ('X1', 'q')):
            with self.subTest(keys=keys):
                status, output = fixtures.terminal([str(fixtures.ASKAI), 'test'], self.env, keys, timeout=5)
                self.assertEqual(status, 0, output)
                self.assertFalse(self.marker.exists())

    def test_backend_cannot_inject_on_cancel_print_copy_or_machine_output(self):
        self.fixture.clipboard_fixture()
        self.env.update(self.fixture.env, TERM='xterm-256color')
        evil = self.root / 'backend.py'
        evil.write_text('import os,json\nfrom pathlib import Path\n'
            'p=os.environ.get("ASKAI_COMMAND_FILE")\n'
            'if p: Path(p).write_text(' + repr('touch ' + shlex.quote(str(self.marker)) + '\n') + ')\n'
            + 'print(' + repr(json.dumps(fixtures.response('printf visible'))) + ')\n')
        config = self.root / 'backend.toml'
        config.write_text('provider="fixture"\n[providers.fixture]\ntype="command"\nexecutable='
                         + json.dumps(sys.executable) + '\nargs=[' + json.dumps(str(evil)) + ']\n')
        self.env['ASKAI_CONFIG'] = str(config)
        for shell in ('fish', 'zsh', 'bash'):
            for mode in ('--print', '--json', 'q', 'p1', 'c1'):
                with self.subTest(shell=shell, mode=mode):
                    status, output = self.shell(shell, 'askai ' + (mode if mode.startswith('--') else '') + ' test',
                                                None if mode.startswith('--') else (mode,))
                    self.assertEqual(status, 0, output)
                    self.assertFalse(self.marker.exists(), output)

    def test_selected_python_does_not_inherit_exchange_and_preserves_reserved_statuses(self):
        code = ('import os,sys\nfrom pathlib import Path\np=os.environ.get("ASKAI_COMMAND_FILE")\n'
                'if p: Path(p).write_text(' + repr('touch ' + shlex.quote(str(self.marker)) + '\n') + ')\n'
                'assert not any(k in os.environ for k in ("ASKAI_COMMAND_FILE","ASKAI_WRAPPER_PROTOCOL","ASKAI_HOOK_INVOCATION"))\n'
                'sys.exit(int(sys.argv[1]))')
        for shell in ('fish', 'zsh', 'bash'):
            for status in (0, 7, 80, 81):
                with self.subTest(shell=shell, status=status):
                    self.env['ASKAI_TEST_ANSWER'] = json.dumps(fixtures.response(commands=[fixtures.python_script(code, [str(status)])]))
                    actual, output = self.shell(shell, 'askai test', ('1',))
                    self.assertEqual(actual, status, output)
                    self.assertFalse(self.marker.exists())

    def test_zsh_executes_snapshot_not_appended_source_file(self):
        command = 'printf "%s\\n" ' + shlex.quote('touch ' + shlex.quote(str(self.marker))) + ' >> "$0"'
        self.env['ASKAI_TEST_ANSWER'] = json.dumps(fixtures.response(command))
        status, output = self.shell('zsh', 'askai test', ('1',))
        self.assertEqual(status, 0, output)
        self.assertFalse(self.marker.exists())

    def test_hook_non_execution_cannot_continue_success_chain(self):
        self.fixture.clipboard_fixture()
        self.env.update(self.fixture.env, TERM='xterm-256color')
        for shell in ('zsh', 'bash'):
            for action in ('q', 'p1', 'c1'):
                with self.subTest(shell=shell, action=action):
                    status, output = self.shell(shell, 'askai_missing_probe natural question && printf CHAIN_MARKER',
                                                (action,), hook=True)
                    self.assertEqual(status, 127, output)
                    self.assertNotIn('CHAIN_MARKER', output)

    def test_selected_shell_child_has_no_exchange_environment(self):
        program = "import os; assert not any(k in os.environ for k in ('ASKAI_COMMAND_FILE','ASKAI_WRAPPER_PROTOCOL','ASKAI_HOOK_INVOCATION')); print('CHILD_OK')"
        for shell in ('fish', 'zsh', 'bash'):
            with self.subTest(shell=shell):
                command = quote_argument(sys.executable, shell) + ' -I -c ' + quote_argument(program, shell)
                self.env['ASKAI_TEST_ANSWER'] = json.dumps(fixtures.response(command))
                self.env['ASKAI_HOOK_INVOCATION'] = '1'
                status, output = self.shell(shell, 'askai test', ('1',))
                self.assertEqual(status, 0, output)
                self.assertIn('CHILD_OK', output)

    def test_old_wrapper_is_rejected_before_model_call(self):
        file = self.root / 'exchange'
        file.write_text('touch unexpected')
        env = {**self.env, 'ASKAI_COMMAND_FILE': str(file)}
        env.pop('ASKAI_WRAPPER_PROTOCOL', None)
        result = subprocess.run([str(fixtures.ASKAI), '--print', 'test'], env=env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('重新打开终端', result.stderr)
        self.assertEqual(file.read_text(), '')

    def test_long_menu_number_is_rejected_without_traceback(self):
        self.assertIsNone(select_option('models', ['one'], io.StringIO('9' * 5000 + '\nq\n'), io.StringIO()))

    def test_non_utf8_locale_outputs_valid_utf8(self):
        result = subprocess.run([str(fixtures.ASKAI), 'doctor'],
                                env={**self.env, 'LC_ALL': 'en_US.ISO8859-1'}, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('配置', result.stdout.decode('utf-8'))


if __name__ == '__main__':
    unittest.main()

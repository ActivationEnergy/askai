"""Review regressions use isolated config, state and startup files only."""
import argparse
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from askai_cli import setup, startup, providers
from askai_cli.core import AskError


class TerminalText(io.StringIO):
    def isatty(self):
        return True


class SetupReviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='askai-setup-review-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.config = self.root / 'config.toml'
        self.config.write_text('provider="codex"\n')
        self.rc = self.root / 'config.fish'
        self.rc.write_text('# user content\n')
        env = patch.dict(os.environ, {'ASKAI_CONFIG': str(self.config),
            'XDG_STATE_HOME': str(self.root / 'state'), 'XDG_CONFIG_HOME': str(self.root / 'config')})
        env.start(); self.addCleanup(env.stop)

    def status(self):
        output = io.StringIO()
        code = setup.main(['--status', '--shell', 'fish', '--startup-file', str(self.rc),
                           '--color', 'never'], io.StringIO(), output)
        return code, output.getvalue()

    def test_bad_ai_config_still_shows_shell_status_without_writes(self):
        self.config.write_text('broken = [')
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        code, text = self.status()
        self.assertEqual(code, 1)
        self.assertIn('已保存的终端设置', text)
        self.assertIn(str(self.rc), text)
        self.assertIn('AI 配置异常', text)
        self.assertEqual(before, {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_bad_state_and_credentials_are_errors_but_cli_status_is_preserved(self):
        state = setup.state_path()
        state.parent.mkdir(parents=True)
        state.write_text('{bad state')
        secret = self.root / 'credentials.json'
        secret.write_text('{bad credentials')
        secret.chmod(0o600)
        code, text = self.status()
        self.assertEqual(code, 1)
        self.assertIn('设置状态异常', text)
        self.assertIn('已保存的终端设置', text)
        self.assertIn('codex：模型', text)
        self.assertIn('dsh：模型', text)
        self.assertIn('deepseek 配置异常', text)
        self.assertEqual(secret.read_text(), '{bad credentials')
        self.assertEqual(state.read_text(), '{bad state')

    def test_bad_startup_file_makes_status_nonzero(self):
        self.rc.write_text('# >>> askai shell integration broken\n')
        code, text = self.status()
        self.assertEqual(code, 1)
        self.assertIn('配置异常', text)
        self.assertIn('codex：模型', text)

    def test_missing_shell_can_be_removed_through_real_wizard(self):
        outside = '# before\n'
        managed = startup.block('fish', False, True, False, '\n')
        original = outside + managed + '# after\n'
        self.rc.write_text(original)
        output = TerminalText()
        with patch('askai_cli.startup.shutil.which', return_value=None):
            code = setup.main(['--shell', 'fish', '--startup-file', str(self.rc), '--executable',
                               str(self.root / 'missing/fish'), '--color', 'never'],
                              TerminalText('2\n6\ny\n0\n0\n'), output)
        self.assertEqual(code, 0)
        self.assertEqual(self.rc.read_text(), outside + '# after\n')
        self.assertIn('无法进行该 Shell 的语法检查', output.getvalue())
        backups = list(self.root.glob('config.fish.askai-backup-*'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), original)

    def test_missing_shell_removal_does_not_bypass_marker_or_interpreter_errors(self):
        self.rc.write_text('# >>> askai shell integration broken\n')
        with patch('askai_cli.startup.shutil.which', return_value=None):
            with self.assertRaises(AskError):
                startup.plan([self.rc], 'fish', False, False, remove=True,
                             executable=str(self.root / 'missing/fish'))
            with self.assertRaises(AskError):
                startup.interpreter('fish', '/missing/python3', allow_missing=True)
        self.rc.write_text(startup.block('fish', False, True, False, '\n'))
        with patch('askai_cli.startup.shutil.which', return_value='/dummy/fish'), \
             patch('askai_cli.startup.check_syntax', side_effect=AskError('syntax invalid')):
            with self.assertRaisesRegex(AskError, 'syntax invalid'):
                startup.plan([self.rc], 'fish', False, False, remove=True)

    def test_existing_nonexecutable_shell_is_not_treated_as_missing(self):
        candidate = self.root / 'fish'
        candidate.write_text('# dummy nonexecutable\n')
        candidate.chmod(0o600)
        with self.assertRaisesRegex(AskError, '不可执行'):
            startup.interpreter('fish', str(candidate), allow_missing=True)
        with self.assertRaisesRegex(AskError, '控制字符'):
            startup.interpreter('fish', '/missing\n/fish', allow_missing=True)

    def test_relative_key_storage_confirmation_shows_absolute_destination(self):
        args = argparse.Namespace(color='never', shell='fish', startup_file=[], executable=None)
        output = io.StringIO()
        ui = setup.Wizard(args, io.StringIO('n\n'), output)
        with patch('askai_cli.setup.store_path', return_value=Path('local/credentials.json')), \
             patch('askai_cli.setup.read_secret', return_value='DUMMY_NOT_REAL_KEY'):
            ui.configure_api_key('kimi', providers.providers({})['kimi'], paste=True)
        self.assertIn(str(Path('local/credentials.json').absolute()), output.getvalue())
        self.assertNotIn('DUMMY_NOT_REAL_KEY', output.getvalue())
        self.assertFalse((self.root / 'credentials.json').exists())


if __name__ == '__main__':
    unittest.main()

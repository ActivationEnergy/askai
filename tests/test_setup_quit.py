"""Global settings quit exits navigation without confirming pending writes."""
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from askai_cli import setup


class Tty(io.StringIO):
    def isatty(self):
        return True


class SetupQuitTests(unittest.TestCase):
    def test_quit_from_nested_menus_and_confirmations_never_writes(self):
        prefixes = ['', '1\n', '1\n3\n', '2\n', '3\n', '4\n',
                    '3\n5\n', '3\n5\n1\n', '3\n5\n1\n5\n', '3\n5\n2\n',
                    '3\n5\n1\n2\n', '3\n5\n2\n1\n', '3\nd5\n']
        for prefix in prefixes:
            with self.subTest(prefix=prefix), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                config = root / 'config.toml'
                config.write_text('provider="codex"\n')
                rc = root / 'config.fish'
                rc.write_text('# original\n')
                output = Tty()
                with patch.dict(os.environ, {'ASKAI_CONFIG': str(config), 'XDG_CONFIG_HOME': str(root / 'config'),
                    'XDG_STATE_HOME': str(root / 'state'), 'MOONSHOT_API_KEY': ''}), \
                     patch('askai_cli.setup.read_secret', return_value='DUMMY_KEY_FOR_QUIT_TEST'), \
                     patch('askai_cli.setup.list_api_models') as network:
                    reader = Tty(prefix + 'q\nTHIS_MUST_REMAIN_UNREAD\n')
                    code = setup.main(['--shell', 'fish', '--startup-file', str(rc), '--color', 'never'], reader, output)
                network.assert_not_called()
                self.assertEqual(code, 0)
                self.assertEqual(reader.readline(), 'THIS_MUST_REMAIN_UNREAD\n')
                self.assertIn('q 退出设置', output.getvalue())
                self.assertIn('已退出设置', output.getvalue())
                self.assertEqual(config.read_text(), 'provider="codex"\n')
                self.assertEqual(rc.read_text(), '# original\n')
                self.assertFalse((root / 'credentials.json').exists())
                self.assertEqual(sorted(p.name for p in root.iterdir()), ['config.fish', 'config.toml'])

    def test_zero_returns_to_parent_and_uppercase_q_exits(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.dict(os.environ, {'ASKAI_CONFIG': str(root / 'config.toml'),
                 'XDG_CONFIG_HOME': str(root / 'config'), 'XDG_STATE_HOME': str(root / 'state')}):
                output = Tty()
                self.assertEqual(setup.main(['--shell', 'fish', '--color', 'never'], Tty('2\n0\nQ\n'), output), 0)
            self.assertEqual(output.getvalue().count('askai 设置\n'), 2)
            self.assertIn('已退出设置', output.getvalue())

    def test_quitting_preserves_previously_confirmed_settings(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / 'config.toml'
            config.write_text('provider="codex"\n')
            with patch.dict(os.environ, {'ASKAI_CONFIG': str(config),
                 'XDG_CONFIG_HOME': str(root / 'config'), 'XDG_STATE_HOME': str(root / 'state')}):
                self.assertEqual(setup.main(['--shell', 'fish', '--color', 'never'],
                    Tty('3\nd3\ny\nq\n'), Tty()), 0)
            self.assertIn('provider="dsh"', config.read_text().replace(' ', ''))

    def test_model_catalog_quit_exits_all_levels(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.dict(os.environ, {'ASKAI_CONFIG': str(root / 'config.toml'),
                 'XDG_CONFIG_HOME': str(root / 'config'), 'XDG_STATE_HOME': str(root / 'state')}), \
                 patch('askai_cli.setup.list_api_models', return_value=['test-model']) as catalog:
                reader, output = Tty('3\n5\n1\n4\nq\nremaining\n'), Tty()
                self.assertEqual(setup.main(['--shell', 'fish', '--color', 'never'], reader, output), 0)
                catalog.assert_called_once()
                self.assertEqual(reader.readline(), 'remaining\n')
                self.assertIn('已退出设置', output.getvalue())

    def test_q_is_literal_in_free_text_fields(self):
        args = type('Args', (), dict(color='never', shell='fish', startup_file=[], executable=None))()
        wizard = setup.Wizard(args, Tty('q\n'), Tty())
        self.assertEqual(wizard.ask('path: ', allow_quit=False), 'q')


if __name__ == '__main__':
    unittest.main()

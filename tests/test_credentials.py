"""Dummy credentials only: masked entry, persistence, source switching and removal."""
import argparse
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from askai_cli import credentials, providers, setup
from askai_cli.core import AskError
from askai_cli.debug import DebugTrace
from askai_cli.interaction import read_secret
import test_ask as fixtures


_DEFAULT_SECRET = object()


class CredentialTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='askai-credentials-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config = self.root / 'config.toml'
        self.config.write_text('provider="kimi"\n')
        self.env = patch.dict(os.environ, {'ASKAI_CONFIG': str(self.config), 'XDG_STATE_HOME': str(self.root / 'state'), 'MOONSHOT_API_KEY': '', 'TERM': 'dumb'})
        self.env.start(); self.addCleanup(self.env.stop)
        self.provider = providers.providers({})['kimi']
        self.key = 'DUMMY_KEY_NOT_A_REAL_CREDENTIAL_123'

    def wizard(self, answers, secret=_DEFAULT_SECRET):
        output = io.StringIO()
        args = argparse.Namespace(color='never', shell='fish', startup_file=[], executable=None)
        ui = setup.Wizard(args, io.StringIO(answers), output)
        with patch('askai_cli.setup.read_secret', return_value=self.key if secret is _DEFAULT_SECRET else secret), \
             patch('askai_cli.providers.fetch') as network:
            ui.key_menu('kimi')
        network.assert_not_called()
        self.assertNotIn(self.key, output.getvalue())
        return output.getvalue()

    def test_paste_persists_without_changing_ordinary_config(self):
        before = self.config.read_bytes()
        output = self.wizard('1\ny\n0\n')
        self.assertIn('密钥设置已更新', output)
        self.assertEqual(credentials.api_key(self.provider), self.key)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(credentials.store_path().stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.root.glob('.credentials-*.tmp')), [])
        code = ('import sys;sys.path.insert(0,' + repr(str(fixtures.ROOT / 'src')) + ');'
                'from askai_cli.credentials import api_key;from askai_cli.providers import providers;'
                'print(bool(api_key(providers({})["kimi"])))')
        self.assertEqual(subprocess.check_output([sys.executable, '-I', '-c', code], text=True).strip(), 'True')

    def test_environment_import_persists_after_environment_is_removed(self):
        with patch.dict(os.environ, {'MOONSHOT_API_KEY': self.key}):
            self.assertEqual(credentials.api_key(self.provider), self.key)
            self.assertFalse(credentials.store_path().exists())
            output = self.wizard('2\ny\n0\n')
            self.assertIn('以后无需再次设置环境变量', output)
        self.assertEqual(credentials.api_key(self.provider), self.key)

    def test_cancel_does_not_create_or_replace_a_key(self):
        self.wizard('1\nn\n0\n')
        self.assertFalse(credentials.store_path().exists())
        credentials.save_key(self.provider, self.key)
        before = credentials.store_path().read_bytes()
        self.wizard('1\nn\n0\n', 'OTHER_DUMMY_KEY')
        self.assertEqual(credentials.store_path().read_bytes(), before)

    def test_replace_and_delete_preserve_other_connections(self):
        other = {**self.provider, 'api_key_env': 'OTHER_KEY'}
        credentials.save_key(other, 'OTHER_DUMMY_KEY')
        credentials.save_key(self.provider, 'OLD_DUMMY_KEY')
        self.wizard('1\ny\n0\n')
        self.assertNotIn('OLD_DUMMY_KEY', credentials.store_path().read_text())
        self.wizard('3\ny\n0\n')
        self.assertEqual(credentials.api_key(self.provider), '')
        self.assertEqual(credentials.api_key(other), 'OTHER_DUMMY_KEY')
        self.assertNotIn(self.key, credentials.store_path().read_text())

    def test_delete_explains_environment_fallback(self):
        credentials.save_key(self.provider, self.key)
        with patch.dict(os.environ, {'MOONSHOT_API_KEY': 'ENV_DUMMY_KEY'}):
            self.assertEqual(credentials.api_key(self.provider), self.key)
            output = self.wizard('3\ny\n0\n')
            self.assertIn('删除后仍会读取环境变量 MOONSHOT_API_KEY', output)
            self.assertEqual(credentials.api_key(self.provider), 'ENV_DUMMY_KEY')
            self.assertEqual(os.environ['MOONSHOT_API_KEY'], 'ENV_DUMMY_KEY')

    def test_environment_import_replacement_requires_confirmation(self):
        credentials.save_key(self.provider, self.key)
        with patch.dict(os.environ, {'MOONSHOT_API_KEY': 'ENV_DUMMY_KEY'}):
            self.wizard('2\nn\n0\n')
            self.assertEqual(credentials.api_key(self.provider), self.key)
            self.wizard('2\ny\n0\n')
        self.assertEqual(credentials.api_key(self.provider), 'ENV_DUMMY_KEY')
        self.assertNotIn(self.key, credentials.store_path().read_text())

    def test_saved_key_does_not_follow_endpoint_changes(self):
        credentials.save_key(self.provider, self.key)
        self.assertEqual(credentials.api_key({**self.provider, 'base_url': 'https://other.example.invalid/v1'}), '')

    def test_unsafe_file_permissions_and_symlinks_are_rejected(self):
        credentials.save_key(self.provider, self.key)
        path = credentials.store_path()
        path.chmod(0o644)
        with self.assertRaises(AskError): credentials.api_key(self.provider)
        path.chmod(0o600)
        destination = self.root / 'original'
        path.rename(destination)
        path.symlink_to(destination)
        with self.assertRaises(AskError): credentials.save_key(self.provider, 'NEW_DUMMY_KEY')
        self.assertTrue(path.is_symlink())
        self.assertIn(self.key, destination.read_text())

    def test_actual_saved_key_is_used_and_redacted(self):
        credentials.save_key(self.provider, self.key)
        trace, output = DebugTrace(True), io.StringIO()
        response = {'status': 401, 'body': json.dumps({'error': self.key}).encode()}
        with patch('askai_cli.providers.fetch', return_value=response) as fetch, patch('sys.stderr', output):
            with self.assertRaises(AskError):
                providers.api_answer(self.provider, None, 'system', 'question', 5, trace)
        self.assertEqual(fetch.call_args.args[1]['Authorization'], 'Bearer ' + self.key)
        self.assertNotIn(self.key, output.getvalue())
        with patch('askai_cli.providers.fetch', side_effect=AskError(self.key)):
            with self.assertRaises(AskError) as error: providers.list_api_models(self.provider)
        self.assertNotIn(self.key, str(error.exception))

    def test_current_model_guides_missing_key_and_returns_to_service(self):
        output = io.StringIO()
        args = argparse.Namespace(color='never', shell='fish', startup_file=[], executable=None)
        ui = setup.Wizard(args, io.StringIO('1\n1\n1\ny\n0\n0\n'), output)
        with patch('askai_cli.setup.read_secret', return_value=self.key):
            ui.api_menu('kimi')
        self.assertEqual(credentials.api_key(self.provider), self.key)
        self.assertIn('还需配置 API 密钥', output.getvalue())
        self.assertGreaterEqual(output.getvalue().count('1 选择模型'), 2)

    def test_cancel_key_setup_reports_incomplete(self):
        output = io.StringIO()
        args = argparse.Namespace(color='never', shell='fish', startup_file=[], executable=None)
        ui = setup.Wizard(args, io.StringIO('1\n1\n0\n0\n'), output)
        ui.api_menu('kimi')
        self.assertIn('尚未完成配置', output.getvalue())
        self.assertFalse(credentials.store_path().exists())

    def test_new_model_saved_then_key_setup_and_existing_key_skips_setup(self):
        for saved in (False, True):
            with self.subTest(saved=saved):
                self.config.write_text('provider="kimi"\n')
                if saved:
                    credentials.save_key(self.provider, self.key)
                output = io.StringIO()
                args = argparse.Namespace(color='never', shell='fish', startup_file=[], executable=None)
                answers = '1\n2\ny\n0\n0\n' if not saved else '1\n2\ny\n0\n'
                ui = setup.Wizard(args, io.StringIO(answers), output)
                ui.api_menu('kimi')
                self.assertNotEqual(providers.providers(setup.core.load_config())['kimi']['model'], self.provider['model'])
                self.assertEqual('还需配置 API 密钥' in output.getvalue(), not saved)

    def test_nonterminal_secret_input_is_not_read_or_echoed(self):
        source, output = io.StringIO(self.key), io.StringIO()
        with self.assertRaises(AskError): read_secret(source, output)
        self.assertEqual(source.tell(), 0)
        self.assertEqual(output.getvalue(), '')

    def test_masked_prompt_hides_paste(self):
        code = ('import sys;sys.path.insert(0,' + repr(str(fixtures.ROOT / 'src')) + ');'
                'from askai_cli.interaction import read_secret;'
                'value=read_secret(sys.stdin,sys.stdout);print("RECEIVED:"+str(bool(value)))')
        status, output = fixtures.terminal([sys.executable, '-I', '-c', code],
            {**os.environ, 'TERM': 'xterm-256color'}, (self.key + '\n',), timeout=5,
            prompt_markers=('粘贴 API Key（输入隐藏，空白取消）:',))
        self.assertEqual(status, 0, output)
        self.assertIn('RECEIVED:True', output)
        self.assertNotIn(self.key, output)


if __name__ == '__main__': unittest.main()

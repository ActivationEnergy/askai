"""Settings menus use immediate keys with the existing safe terminal boundary."""
import os
from pathlib import Path
import sys
import tempfile
import unittest

import test_ask as fixtures

ROOT = Path(__file__).resolve().parents[1]


class SetupKeyTests(unittest.TestCase):
    def run_settings(self, keys, markers):
        with tempfile.TemporaryDirectory(prefix='askai-setup-keys-') as temporary:
            root = Path(temporary)
            config = root / 'config.toml'
            config.write_text('provider="codex"\n')
            env = {**os.environ, 'ASKAI_CONFIG': str(config), 'XDG_CONFIG_HOME': str(root / 'config'),
                   'XDG_STATE_HOME': str(root / 'state'), 'TERM': 'xterm-256color', 'NO_COLOR': '1'}
            code = f'''import sys,termios
sys.path.insert(0,{str(ROOT / 'src')!r})
from askai_cli import setup
before=termios.tcgetattr(0)
try:
    print('EXIT:'+str(setup.main(['--shell','fish','--color','never'])))
finally:
    after=termios.tcgetattr(0)
    before[3] &= ~getattr(termios,'PENDIN',0)
    after[3] &= ~getattr(termios,'PENDIN',0)
    print('RESTORED:'+str(before==after))
'''
            status, output = fixtures.terminal([sys.executable, '-I', '-c', code], env, keys,
                timeout=8, prompt_markers=markers)
            self.assertEqual(status, 0, output)
            self.assertIn('RESTORED:True', output)
            return output, config.read_text()

    def test_navigation_default_prefix_confirmation_and_quit_need_no_enter(self):
        output, config = self.run_settings(('3', 'd3', 'y', 'q'),
            ('请选择 [0]:', '请选择 [0]:', '保存？[y/N]:', '请选择 [0]:'))
        self.assertIn('provider="dsh"', config.replace(' ', ''))
        self.assertIn('已选择：d3 · 设为默认', output)
        self.assertIn('已选择：y · 确认保存', output)
        self.assertIn('已退出设置', output)

    def test_enter_defaults_to_no_at_confirmation(self):
        output, config = self.run_settings(('3', 'd3', '\r', 'q'),
            ('请选择 [0]:', '请选择 [0]:', '保存？[y/N]:', '请选择 [0]:'))
        self.assertEqual(config, 'provider="codex"\n')
        self.assertIn('已取消本次修改', output)

    def test_paste_and_typeahead_cannot_confirm_unseen_settings(self):
        for keys, markers in [
            (('\x1b[200~3d3y\x1b[201~', 'q'), ('请选择 [0]:', '已忽略粘贴内容')),
            (('3d3y', 'q'), ('请选择 [0]:', '请选择 [0]:')),
        ]:
            with self.subTest(keys=keys):
                output, config = self.run_settings(keys, markers)
                self.assertEqual(config, 'provider="codex"\n')
                self.assertNotIn('设置已保存', output)


if __name__ == '__main__':
    unittest.main()

"""UI consistency regressions use isolated config and real PTYs, never AI."""
import argparse
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import test_ask as fixtures
from askai_cli import setup

ROOT = Path(__file__).resolve().parents[1]


class Tty(io.StringIO):
    def isatty(self):
        return True
    def fileno(self):
        return 99


class ConsistencyTests(unittest.TestCase):
    def terminal(self, body, keys, markers, term='xterm-256color'):
        with tempfile.TemporaryDirectory(prefix='askai-ui-consistency-') as temporary:
            root = Path(temporary)
            config = root / 'config.toml'
            config.write_text('provider="codex"\n')
            env = {**os.environ, 'ASKAI_CONFIG': str(config), 'XDG_STATE_HOME': str(root/'state'),
                   'XDG_CONFIG_HOME': str(root/'config'), 'TERM': term, 'NO_COLOR': '1'}
            code = f'import sys;sys.path.insert(0,{str(ROOT/"src")!r})\n' + body
            rc, output = fixtures.terminal([sys.executable, '-I', '-c', code], env, keys,
                                           timeout=8, prompt_markers=markers)
            return rc, output, config.read_text()

    def test_default_prefix_has_no_timeout(self):
        body = '''import time,threading
from askai_cli import setup
# Delay the next visible marker past prompt_toolkit's old default 1s timeout.
threading.Thread(target=lambda:(time.sleep(1.5),print('LATE-MARKER',flush=True)),daemon=True).start()
print('RESULT:'+str(setup.main(['--shell','fish','--color','never'])))
'''
        rc, output, config = self.terminal(body, ('3','d','3','n','q'),
            ('请选择 [0]:','请选择 [0]:','LATE-MARKER','保存？[y/N]:','请选择 [0]:'))
        self.assertEqual(rc, 0, output)
        self.assertIn('设为默认：请选择后端编号', output)
        self.assertIn('已选择：d3', output)
        self.assertNotIn('无效按键', output)
        self.assertEqual(config, 'provider="codex"\n')

    def test_default_prefix_escape_does_not_change_numeric_action(self):
        body = """from askai_cli.interaction import read_choice
result=read_choice(sys.stdin,sys.stdout,0,menu_options={'3':'选择后端','d3':'设为默认','0':'返回'},menu_prompt='MENU:')
print('RESULT:'+result)
"""
        rc, output, _ = self.terminal(body, ('d','\x1b','3'),
            ('MENU:', '设为默认：请选择后端编号', 'MENU:'))
        self.assertEqual(rc, 0, output)
        self.assertIn('RESULT:3', output)
        self.assertIn('已选择：3 · 选择后端', output)
        self.assertNotIn('已选择：d3', output)

    def test_line_mode_clears_batch_and_during_preparation_input(self):
        body = '''import time
from askai_cli import setup
original=setup.preferences.plan
def delayed(*a,**k):
    print('PREPARING',flush=True)
    time.sleep(.35)
    return original(*a,**k)
setup.preferences.plan=delayed
print('RESULT:'+str(setup.main(['--shell','fish','--color','never'])))
'''
        for keys, markers in [
            (('3\nd3\ny\nq\n','q\n'), ('请选择 [0]:','请选择 [0]:')),
            (('3\n','d3\n','y\n','n\n','q\n'),
             ('请选择 [0]:','请选择 [0]:','PREPARING','保存？[y/N]:','请选择 [0]:')),
        ]:
            rc, output, config = self.terminal(body, keys, markers, term='dumb')
            self.assertEqual(rc, 0, output)
            self.assertEqual(config, 'provider="codex"\n')
            self.assertNotIn('设置已保存', output)
            self.assertIn('输入后回车', output)
            self.assertIn('已选择：', output)

    def test_noncanonical_line_mode_refuses_without_retry_loop(self):
        body = '''import tty
from askai_cli import cli
tty.setraw(0)
sys.argv=['askai-config','--shell','fish']
cli.main()
'''
        rc, output, config = self.terminal(body, (), (), term='dumb')
        self.assertEqual(rc, 1, output)
        self.assertIn('不处于标准行输入模式', output)
        self.assertEqual(config, 'provider="codex"\n')
        self.assertNotIn('Traceback', output)

    def test_esc_record_matches_root_exit(self):
        body = "from askai_cli import setup;setup.main(['--shell','fish','--color','never'])"
        rc, output, _ = self.terminal(body, ('\x1b',), ('请选择 [0]:',))
        self.assertEqual(rc, 0, output)
        self.assertIn('已选择：Esc · 退出设置', output)
        self.assertIn('按键选择（无需回车）', output)

    def test_saved_color_and_line_choice_records(self):
        for mode in ('never','always'):
            with tempfile.TemporaryDirectory() as temporary:
                config = Path(temporary)/'config.toml'
                config.write_text('[ui]\ncolor="'+mode+'"\n')
                env = {k:v for k,v in os.environ.items() if k!='NO_COLOR'}
                env.update(ASKAI_CONFIG=str(config),XDG_STATE_HOME=str(Path(temporary)/'state'),
                           XDG_CONFIG_HOME=str(Path(temporary)/'config'),TERM='xterm-256color')
                for args in ([], ['--status']):
                    output=Tty()
                    with patch.dict(os.environ,env,clear=True):
                        self.assertEqual(setup.main(['--shell','fish',*args],Tty('q\n'),output),0)
                    self.assertEqual('\x1b[' in output.getvalue(),mode=='always')
        output=io.StringIO()
        args=argparse.Namespace(color='never',shell='fish',startup_file=[],executable=None)
        wizard=setup.Wizard(args,io.StringIO('invalid\n2\n'),output)
        wizard.menu('1 选择模型\n2 管理密钥\n0 返回')
        self.assertEqual(wizard.ask(),'invalid')
        self.assertNotIn('已选择：',output.getvalue())
        wizard.menu('1 选择模型\n2 管理密钥\n0 返回')
        self.assertEqual(wizard.ask(),'2')
        self.assertIn('已选择：2 · 管理密钥',output.getvalue())


if __name__ == '__main__':
    unittest.main()

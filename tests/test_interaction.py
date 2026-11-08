"""Exercise immediate keys, paste rejection and terminal restoration in a PTY."""
import os
import shlex
import subprocess
from pathlib import Path
import sys
import unittest

import test_ask as fixtures

ROOT = Path(__file__).resolve().parents[1]


# macOS may set PENDIN when restoring canonical input. It requests retyping
# pending input, not a change to echo, signal or canonical input behavior.
class InteractionTests(unittest.TestCase):
    def run_menu(self, inputs, markers=None):
        code = f'''import sys,termios
sys.path.insert(0,{str(ROOT / 'src')!r})
from askai_cli.cli import choose
before=termios.tcgetattr(0)
try:
    result=choose({{'explanation':'test','commands':[{{'label':'one','command':'printf one','warning':''}},{{'label':'two','command':'printf two','warning':''}}]}},sys.stdin,sys.stdout)
    print('RESULT:'+repr(result))
except KeyboardInterrupt:
    print('INTERRUPTED')
finally:
    after=termios.tcgetattr(0)
    before[3] &= ~getattr(termios,'PENDIN',0)
    after[3] &= ~getattr(termios,'PENDIN',0)
    print('RESTORED:'+str(after==before))
'''
        return fixtures.terminal([sys.executable, '-I', '-c', code],
                                 {**os.environ, 'TERM': 'xterm-256color'}, inputs,
                                 timeout=5, prompt_markers=markers)

    def test_single_keys_need_no_enter(self):
        for inputs, result in [(('1',), "('run', 0)"), (('c2',), "('copy', 1)"),
                               (('p1',), "('print', 0)"), (('q',), "('cancel', None)"),
                               (('c\x1bq',), "('cancel', None)")]:
            with self.subTest(inputs=inputs):
                code, output = self.run_menu(inputs)
                self.assertEqual(code, 0, output)
                self.assertIn('RESULT:' + result, output)
                self.assertIn('RESTORED:True', output)

    def test_bracketed_paste_cannot_select_or_execute(self):
        code, output = self.run_menu(('\x1b[200~1q\n\x1b[201~', 'q'),
                                     ('[默认 q]:', '已忽略粘贴内容'))
        self.assertEqual(code, 0, output)
        self.assertIn("RESULT:('cancel', None)", output)
        self.assertIn('RESTORED:True', output)

    def test_keys_queued_before_menu_do_not_execute_new_answer(self):
        code = f"""import sys,time
sys.path.insert(0,{str(ROOT / 'src')!r})
from askai_cli.interaction import read_choice
print('WAITING',flush=True)
time.sleep(.2)
print('RESULT:'+read_choice(sys.stdin,sys.stdout,1))
"""
        status, output = fixtures.terminal([sys.executable, '-I', '-c', code],
            {**os.environ, 'TERM': 'xterm-256color'}, ('1\n', 'q'), timeout=5,
            prompt_markers=('WAITING', '[默认 q]:'))
        self.assertEqual(status, 0, output)
        self.assertIn('RESULT:q', output)
        self.assertNotIn('RESULT:1', output)

    def test_plain_menu_discards_input_queued_before_display(self):
        code = f"""import sys,time
sys.path.insert(0,{str(ROOT / 'src')!r})
from askai_cli.cli import choose
print('WAITING',flush=True)
time.sleep(.2)
print('RESULT:'+repr(choose({{'explanation':'test','commands':[{{'label':'one','command':'printf one','warning':''}}]}},sys.stdin,sys.stdout)))
"""
        status, output = fixtures.terminal([sys.executable, '-I', '-c', code],
            {**os.environ, 'TERM': 'dumb'}, ('1\n', 'q\n'), timeout=5,
            prompt_markers=('WAITING', '[默认 q]:'))
        self.assertEqual(status, 0, output)
        self.assertIn("RESULT:('cancel', None)", output)

    def test_menu_does_not_capture_mouse_selection(self):
        status, output = self.run_menu(('q',))
        self.assertEqual(status, 0, output)
        self.assertNotRegex(output, r"\x1b\[\?(?:1000|1002|1003|1006)h")

    def test_ctrl_c_restores_terminal(self):
        code, output = self.run_menu(('\x03',))
        self.assertEqual(code, 0, output)
        self.assertIn('INTERRUPTED', output)
        self.assertIn('RESTORED:True', output)

    def test_source_integration_retains_venv_after_path_changes(self):
        if not (Path(sys.prefix) / "pyvenv.cfg").is_file():
            self.skipTest("requires a virtual environment")
        fixture = fixtures.EndToEndTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        init = fixture.path / "init.zsh"
        init.write_text(subprocess.check_output(
            [sys.executable, str(ROOT / 'bin/askai'), 'init', 'zsh'], text=True))
        env = {**fixture.env, 'TERM': 'xterm-256color', 'PATH': '/usr/bin:/bin:/opt/homebrew/bin'}
        status, output = fixtures.terminal(['/bin/zsh', '-f', '-c',
            f'source {shlex.quote(str(init))}; askai test question'], env, ('q',), timeout=5)
        self.assertEqual(status, 0, output)
        self.assertIn('测试建议', output)

    def test_editable_chinese_input(self):
        code = f'''import sys,termios
sys.path.insert(0,{str(ROOT / 'src')!r})
from askai_cli.interaction import read_text
before=termios.tcgetattr(0)
print('TEXT:'+read_text('输入文字: ',sys.stdin,sys.stdout))
after=termios.tcgetattr(0)
before[3] &= ~getattr(termios,'PENDIN',0)
after[3] &= ~getattr(termios,'PENDIN',0)
print('RESTORED:'+str(after==before))
'''
        status, output = fixtures.terminal([sys.executable, '-I', '-c', code],
            {**os.environ, 'TERM': 'xterm-256color'}, ('中文 路径X\x7f\n',),
            timeout=5, prompt_markers=('输入文字:',))
        self.assertEqual(status, 0, output)
        self.assertIn('TEXT:中文 路径', output)
        self.assertNotIn('TEXT:中文 路径X', output)
        self.assertIn('RESTORED:True', output)

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
    def run_menu(self, inputs, markers=None, count=2, script=False):
        commands = ([fixtures.python_script()] if script else [
            {'label': 'one', 'command': 'printf one', 'warning': ''},
            {'label': 'two', 'command': 'printf two', 'warning': ''}])[:count]
        code = f'''import sys,termios
sys.path.insert(0,{str(ROOT / 'src')!r})
from askai_cli.cli import choose
before=termios.tcgetattr(0)
try:
    result=choose({{'explanation':'test','commands':{commands!r}}},sys.stdin,sys.stdout)
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

    def test_accepted_choices_remain_in_scrollback_once(self):
        for keys, record in ((('1',), '已选择：1 · 执行方案 1'),
                             (('C2',), '已选择：C2 · 复制方案 2'),
                             (('q',), '已选择：q · 退出'),
                             (('\r',), '已选择：Enter · 退出')):
            with self.subTest(keys=keys):
                status, output = self.run_menu(keys)
                self.assertEqual(status, 0, output)
                self.assertEqual(output.count(record), 1, output)
                self.assertLess(output.index(record), output.index('RESULT:'))
        status, output = self.run_menu(('e', 'q'), ('[默认 q]:', '[默认 q]:'), count=1)
        self.assertEqual(status, 0, output)
        self.assertEqual(output.count('已选择：e · 解释方案 1'), 1, output)
        self.assertLess(output.index('已选择：e · 解释方案 1'), output.index('one — 解释'))

    def test_option_choice_records_its_meaning(self):
        code = f"""import sys
sys.path.insert(0,{str(ROOT / 'src')!r})
from askai_cli.retry import select_option
print('RESULT:'+str(select_option('推理强度',['低','高'],sys.stdin,sys.stdout)))
"""
        status, output = fixtures.terminal([sys.executable, '-I', '-c', code],
            {**os.environ, 'TERM': 'xterm-256color'}, ('2',), timeout=5,
            prompt_markers=('按编号选择，Esc 返回:',))
        self.assertEqual(status, 0, output)
        self.assertEqual(output.count('已选择：2 · 高'), 1, output)
        self.assertIn('RESULT:1', output)

    def test_plain_choices_record_input_and_label(self):
        import io
        from askai_cli.cli import choose
        from askai_cli.retry import select_option
        output = io.StringIO()
        result = choose(fixtures.response(), io.StringIO('P\n'), output)
        self.assertEqual(result, ('print', 0))
        self.assertEqual(output.getvalue().count('已选择：P · 打印方案 1'), 1)
        for entered, expected in (('2\n', '已选择：2 · 高'), ('', '已选择：EOF · 返回')):
            output = io.StringIO()
            select_option('推理强度', ['低', '高'], io.StringIO(entered), output)
            self.assertIn(expected, output.getvalue())

    def test_single_answer_explains_without_number(self):
        code, output = self.run_menu(('e', 'q'), ('[默认 q]:', '[默认 q]:'), count=1)
        self.assertEqual(code, 0, output)
        self.assertIn('one — 解释', output)
        self.assertNotIn('解释哪个方案', output)
        self.assertIn("RESULT:('cancel', None)", output)
        self.assertIn('RESTORED:True', output)

    def test_single_answer_number_suffix_cannot_execute_next_menu(self):
        code, output = self.run_menu(('e1', 'q'), ('[默认 q]:', '[默认 q]:'), count=1)
        self.assertEqual(code, 0, output)
        self.assertIn('one — 解释', output)
        self.assertIn("RESULT:('cancel', None)", output)
        self.assertNotIn("RESULT:('run'", output)

    def test_multiple_answers_still_require_explanation_number(self):
        code, output = self.run_menu(('e', '2', 'q'),
                                     ('[默认 q]:', '解释哪个方案', '[默认 q]:'))
        self.assertEqual(code, 0, output)
        self.assertIn('two — 解释', output)
        self.assertIn("RESULT:('cancel', None)", output)

    def test_pending_invalid_key_shows_text_recovery_without_color(self):
        from unittest.mock import patch
        for key in ('9', 'x'):
            with self.subTest(key=key), patch.dict(os.environ, {'NO_COLOR': '1'}):
                status, output = self.run_menu(('e', key, '1\x1bq'),
                    ('[默认 q]:', '解释哪个方案', '无效按键，请按 Esc 重选。'))
                self.assertEqual(status, 0, output)
                self.assertIn('无效按键，请按 Esc 重选。', output)
                self.assertIn("RESULT:('cancel', None)", output)
                self.assertNotIn('已选择：e1', output)
                self.assertIn('RESTORED:True', output)

    def test_single_answer_actions_need_no_number(self):
        for script in (False, True):
            actions = {'p': 'print', 'c': 'copy'}
            if script:
                actions.update(v='source', s='save', r='source')
            for key, action in actions.items():
                with self.subTest(script=script, key=key):
                    code, output = self.run_menu((key,), count=1, script=script)
                    self.assertEqual(code, 0, output)
                    self.assertIn('RESULT:' + repr((action, 0)), output)
                    self.assertNotIn('哪个方案', output)
                    self.assertIn('RESTORED:True', output)

    def test_plain_single_answer_actions_and_numbered_forms(self):
        import io
        from askai_cli.cli import choose
        for key, action in {'p': 'print', 'c': 'copy', 'v': 'source', 's': 'save', 'r': 'source'}.items():
            for suffix in ('', '1'):
                with self.subTest(key=key, suffix=suffix):
                    result = choose(fixtures.response(commands=[fixtures.python_script()]),
                                    io.StringIO(key + suffix + '\n'), io.StringIO())
                    self.assertEqual(result, (action, 0))

    def test_single_script_reasoning_shortcut_keeps_priority(self):
        import io
        from askai_cli.cli import choose
        result = choose(fixtures.response(commands=[fixtures.python_script()]),
                        io.StringIO('r\n'), io.StringIO(), options={'r': '推理强度'})
        self.assertEqual(result, ('r', None))

    def test_plain_single_answer_accepts_e_and_legacy_e1(self):
        import io
        from askai_cli.cli import choose
        for key in ('e', 'e1'):
            with self.subTest(key=key):
                output = io.StringIO()
                result = choose({'explanation': 'test', 'commands': [
                    {'label': 'one', 'command': 'printf one', 'warning': '', 'details': 'detail text'}]},
                    io.StringIO(key + '\nq\n'), output)
                self.assertEqual(result, ('cancel', None))
                self.assertIn('detail text', output.getvalue())

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

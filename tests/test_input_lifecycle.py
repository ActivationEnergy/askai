"""Cross-prompt input isolation, including complete settings and answer flows."""
import os
from pathlib import Path
import sys
import tempfile
import unittest

import test_ask as fixtures

ROOT = Path(__file__).resolve().parents[1]


class InputLifecycleTests(unittest.TestCase):
    def terminal(self, code, keys, markers, term='xterm-256color'):
        with tempfile.TemporaryDirectory(prefix='askai-input-stage-') as temporary:
            root = Path(temporary)
            config=root/'config.toml'
            config.write_text('provider="codex"\n')
            env={**os.environ,'ASKAI_CONFIG':str(config),'XDG_CONFIG_HOME':str(root/'config'),
                 'XDG_STATE_HOME':str(root/'state'),'TERM':term,'NO_COLOR':'1'}
            source=f'import sys;sys.path.insert(0,{str(ROOT/"src")!r})\n'+code
            rc, output=fixtures.terminal([sys.executable,'-I','-c',source],env,keys,
                                         timeout=8,prompt_markers=markers)
            self.assertEqual(rc,0,output)
            return output,(root/'credentials.json').exists()

    def test_password_cannot_confirm_its_following_save_page(self):
        code="from askai_cli import setup;setup.main(['--shell','fish','--color','never'])"
        output,saved=self.terminal(code,('3','5','2','1','DUMMY_STAGE_KEY\ny','n','q'),
            ('请选择 [0]:','请选择 [0]:','请选择 [0]:','请选择 [0]:',
             '粘贴 API Key（输入隐藏，空白取消）:','确认密钥设置？[y/N]:','请选择 [0]:'))
        self.assertFalse(saved)
        self.assertNotIn('DUMMY_STAGE_KEY',output)
        self.assertNotIn('密钥设置已更新',output)
        self.assertIn('已选择：n',output)

    def test_supplement_cannot_execute_the_new_answer(self):
        code='''import time
from askai_cli.cli import select_action
from askai_cli.retry import review_answers
old={'explanation':'old','commands':[{'label':'old','command':'printf old','warning':''}]}
new={'explanation':'new','commands':[{'label':'new','command':'printf new','warning':''}]}
def request(state):
    time.sleep(.2)
    return new
def perform(data,action,index,destination):
    print('PERFORM:'+action,flush=True)
    return 0
review_answers(old,'test',{'type':'command'},None,'question',{},sys.stdin,sys.stdout,
               'bash',request,select_action,perform=perform)
'''
        output,_=self.terminal(code,('a','details\n1','q'),
            ('[默认 q]:','补充要求（回车提交，空白返回）:','[默认 q]:'))
        self.assertIn('PERFORM:cancel',output)
        self.assertNotIn('PERFORM:run',output)
        self.assertIn('new',output)

    def test_text_and_password_cancel_eof_and_return_do_not_leave_keys(self):
        for kind in ('text','secret'):
            for ending in ('\x03','\x04','value\n'):
                with self.subTest(kind=kind,ending=repr(ending)):
                    code=f'''from askai_cli.interaction import read_text,read_secret,read_choice
try:
    value=read_text('TEXT:',sys.stdin,sys.stdout) if {kind!r}=='text' else read_secret(sys.stdin,sys.stdout)
except KeyboardInterrupt:
    print('CANCELLED',flush=True)
print('CHOICE:'+read_choice(sys.stdin,sys.stdout,1))
'''
                    marker='TEXT:' if kind=='text' else '粘贴 API Key（输入隐藏，空白取消）:'
                    output,_=self.terminal(code,(ending+'1','q'),(marker,'[默认 q]:'))
                    self.assertIn('CHOICE:q',output)
                    self.assertNotIn('CHOICE:1',output)

    def test_script_path_and_text_to_basic_menu_are_isolated(self):
        for basic in (False,True):
            with self.subTest(basic=basic):
                code=f'''import os
from askai_cli.interaction import read_text,read_choice,read_line
value=read_text('PATH:',sys.stdin,sys.stdout)
print('PATH-VALUE:'+value,flush=True)
if {basic!r}:
    os.environ['TERM']='dumb'
    print('CHOICE:'+str(read_line('BASIC:',sys.stdin,sys.stdout)))
else:
    print('CHOICE:'+read_choice(sys.stdin,sys.stdout,1))
'''
                output,_=self.terminal(code,('script.py\n1','q\n' if basic else 'q'),
                    ('PATH:','BASIC:' if basic else '[默认 q]:'))
                self.assertIn('PATH-VALUE:script.py',output)
                self.assertIn('CHOICE:q',output)

    def test_menu_entry_discards_old_library_cache(self):
        code='''from contextlib import closing
from prompt_toolkit.input import create_input
from prompt_toolkit.input.typeahead import store_typeahead
from prompt_toolkit.key_binding import KeyPress
from askai_cli.interaction import read_choice
with closing(create_input(stdin=sys.stdin)) as source:
    store_typeahead(source,[KeyPress('1')])
print('CHOICE:'+read_choice(sys.stdin,sys.stdout,1))
'''
        output,_=self.terminal(code,('q',),('[默认 q]:',))
        self.assertIn('CHOICE:q',output)

    def test_prompt_exception_also_cleans_owned_cache(self):
        code='''from contextlib import closing
from unittest.mock import patch
from prompt_toolkit.input import create_input
from prompt_toolkit.input.typeahead import store_typeahead,get_typeahead
from prompt_toolkit.key_binding import KeyPress
from askai_cli.interaction import read_text,read_secret

def fail(prompt,*args,**kwargs):
    store_typeahead(prompt.app.input,[KeyPress('1')])
    raise RuntimeError('dummy failure')
for read in (lambda:read_text('TEXT:',sys.stdin,sys.stdout),lambda:read_secret(sys.stdin,sys.stdout)):
    try:
        with patch('prompt_toolkit.PromptSession.prompt',fail):
            read()
    except RuntimeError:
        pass
    with closing(create_input(stdin=sys.stdin)) as source:
        print('CACHE_EMPTY:'+str(not get_typeahead(source)),flush=True)
'''
        output,_=self.terminal(code,(),())
        self.assertEqual(output.count('CACHE_EMPTY:True'),2)

    def test_bracketed_multiline_text_remains_intact_within_one_prompt(self):
        code="from askai_cli.interaction import read_text;print('TEXT-VALUE:'+repr(read_text('TEXT:',sys.stdin,sys.stdout)))"
        output,_=self.terminal(code,('\x1b[200~first\nsecond\x1b[201~\r',),('TEXT:',))
        self.assertIn("TEXT-VALUE:'first\\nsecond'",output)


if __name__=='__main__':
    unittest.main()

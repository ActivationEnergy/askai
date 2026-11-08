"""Command text remains selectable verbatim; shortcut layout stays compact."""
import io
import re
import sys
from pathlib import Path
import unittest
import unicodedata

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from askai_cli.cli import display_command
from askai_cli.terminal import ColorPolicy, Terminal

SGR = re.compile(r'\x1b\[[0-9;]*m')


class PaletteTests(unittest.TestCase):
    def test_multiline_command_has_no_added_prefix_or_indentation(self):
        code = "printf '%s\\n' one\nprintf '%s\\n' two"
        for mode in ('always', 'never'):
            output = io.StringIO()
            display_command(Terminal(output, ColorPolicy(mode)), {'command': code})
            self.assertEqual(SGR.sub('', output.getvalue()), '\n' + code + '\n\n')
            if mode == 'always':
                self.assertIn('\x1b[1;32m', output.getvalue())

    def test_script_indentation_is_not_changed(self):
        code = 'for value in range(2):\n    print(value)\n'
        output = io.StringIO()
        display_command(Terminal(output, ColorPolicy('always')),
                        {'kind': 'script', 'code': code, 'args': []})
        text = SGR.sub('', output.getvalue())
        self.assertTrue(text.endswith('\n\n' + code + '\n\n'))
        self.assertIn('Python 脚本', text)

    def test_shortcuts_fit_one_wide_row_or_two_normal_rows(self):
        groups = [[('1', '立即执行'), ('e', '解释'), ('p', '打印'), ('c', '复制')],
                  [('r', '推理强度'), ('m', '换模型'), ('a', '补充要求'), ('q', '退出')]]
        for width, expected in ((120, 1), (80, 2), (30, 4)):
            with self.subTest(width=width):
                output = io.StringIO()
                Terminal(output, ColorPolicy('always')).action_bar(groups, width=width)
                self.assertIn('\x1b[1;35m', output.getvalue())
                lines = SGR.sub('', output.getvalue()).splitlines()
                self.assertEqual(len(lines), expected)
                for line in lines:
                    cells = sum(2 if unicodedata.east_asian_width(c) in ('W', 'F') else 1 for c in line)
                    self.assertLessEqual(cells, width)
                for key, label in sum(groups, []):
                    self.assertIn(f'[{key}] {label}', '\n'.join(lines))


if __name__ == '__main__':
    unittest.main()

"""Observe CLI lifecycle without exposing content or changing response bytes."""
import contextlib
import io
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from askai_cli.core import AskError
from askai_cli.debug import DebugTrace
from askai_cli.providers import run_cli


class RecordingTrace(DebugTrace):
    def __init__(self, enabled=True):
        super().__init__(enabled)
        self.records = []

    def emit(self, label, value):
        if self.enabled:
            self.records.append((label, value))


class TimingTests(unittest.TestCase):
    def test_split_dsh_markers_preserve_response_and_order(self):
        trace = RecordingTrace()
        code = '''import sys,time
sys.stdin.read()
sys.stderr.write('[llm-deepseek-api-key] model list');sys.stderr.flush()
time.sleep(.05)
sys.stderr.write('ing from https://example.invalid/models loaded 2 model(s)\\ndsh: rea');sys.stderr.flush()
time.sleep(.05)
sys.stderr.write('soning: private reasoning\\n');sys.stderr.flush()
time.sleep(.05)
sys.stdout.write('result');sys.stdout.flush()
'''
        self.assertEqual(run_cli([sys.executable, '-I', '-c', code], 'question', 5, trace), 'result')
        report = next(value for label, value in trace.records if label == 'CLI 阶段计时')
        names = ['子进程已创建', '首次诊断输出', '观察到 DSH 模型目录结果',
                 '观察到 DSH 推理输出', '首次标准输出', '调用结束（含退出或中断清理）']
        values = [float(report[name].split()[0]) for name in names]
        self.assertEqual(values, sorted(values))
        self.assertNotIn('private reasoning', str(report))
        self.assertNotIn('https://example.invalid', str(report))

    def test_timeout_retains_observed_events_without_fabricating_output(self):
        trace = RecordingTrace()
        code = "import sys,time;sys.stderr.write('ready\\n');sys.stderr.flush();time.sleep(10)"
        with self.assertRaises(AskError):
            run_cli([sys.executable, '-I', '-c', code], '', .5, trace)
        report = next(value for label, value in trace.records if label == 'CLI 阶段计时')
        self.assertIn('首次诊断输出', report)
        self.assertNotIn('首次标准输出', report)
        self.assertNotIn('观察到 DSH 推理输出', report)

    def test_disabled_debug_keeps_output_clean(self):
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            result = run_cli([sys.executable, '-I', '-c', "print('ok')"], '', 5, DebugTrace(False))
        self.assertEqual(result, 'ok\n')
        self.assertEqual(output.getvalue(), '')


if __name__ == '__main__':
    unittest.main()

"""Retries change only the current question and commit only successful answers."""
import copy
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import sys
import unittest
import tempfile
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from askai_cli.core import AskError
from askai_cli.dsh_policy import dsh_patch
from askai_cli.retry import review_answers, select_option, select_model, AnswerState
from askai_cli.registry import DEFAULTS
import test_ask as fixtures


def response(text):
    return fixtures.response('printf ' + text)


class RetryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="askai-unit-credentials-")
        self.addCleanup(temporary.cleanup)
        store = patch("askai_cli.credentials.store_path", return_value=Path(temporary.name) / "credentials.json")
        store.start()
        self.addCleanup(store.stop)

    def review(self, actions, request, provider=None, **kwargs):
        original = response('old')
        provider = provider or dict(DEFAULTS['dsh'])
        config = {'provider': 'dsh'}
        before = copy.deepcopy((provider, config))
        reader, writer = io.StringIO(), io.StringIO()
        select = Mock(side_effect=[(a, 0 if a == 'run' else None, None) for a in actions])
        result = review_answers(original, 'dsh', provider, None, 'original question', config,
                                reader, writer, 'fish', request, select)
        self.assertEqual((provider, config), before)
        return result, select, writer.getvalue()

    def test_default_low_and_high_are_encoded_in_process_patch(self):
        self.assertEqual(DEFAULTS['dsh']['reasoning_effort'], 'low')
        self.assertIn('reasoningEffort: "low"', dsh_patch())
        self.assertIn('reasoningEffort: "high"', dsh_patch(reasoning_effort='high'))
        with self.assertRaises(AskError):
            dsh_patch(reasoning_effort='unsupported')

    def test_higher_effort_retries_original_question_then_back_restores_answer(self):
        request = Mock(return_value=response('new'))
        with patch('askai_cli.retry.select_option', return_value=2):
            result, select, output = self.review(['r', 'b', 'run'], request)
        state = request.call_args.args[0]
        self.assertEqual(state.provider['reasoning_effort'], 'high')
        self.assertEqual(state.question, 'original question')
        self.assertNotIn('printf old', state.question)
        self.assertEqual(result[0], response('old'))
        self.assertEqual(select.call_count, 3)
        self.assertIn('b', select.call_args_list[1].kwargs['options'])
        self.assertNotIn('b', select.call_args_list[2].kwargs['options'])

    def test_accepted_supplements_accumulate_but_failure_preserves_current_answer(self):
        request = Mock(side_effect=[response('new'), AskError('network failure')])
        with patch('askai_cli.retry.read_text', side_effect=['require A', 'require B']):
            result, _, output = self.review(['a', 'a', 'run'], request)
        self.assertEqual(result[0], response('new'))
        self.assertEqual(request.call_args_list[1].args[0].question,
                         'original question\n补充要求：require A\n补充要求：require B')
        self.assertIn('已保留当前答案和设置', output)

    def test_back_and_forward_restore_answers_without_new_requests(self):
        request = Mock(return_value=response('new'))
        with patch('askai_cli.retry.select_option', return_value=2):
            result, select, output = self.review(['r', 'b', 'f', 'run'], request)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(result[0], response('new'))
        self.assertNotIn('f', select.call_args_list[1].kwargs['options'])
        self.assertIn('f', select.call_args_list[2].kwargs['options'])
        self.assertNotIn('b', select.call_args_list[2].kwargs['options'])
        self.assertNotIn('f', select.call_args_list[3].kwargs['options'])
        self.assertEqual(output.count('推理：高'), 2)

    def test_retry_from_old_answer_branches_from_its_question(self):
        request = Mock(side_effect=[response('discarded'), response('branch')])
        with patch('askai_cli.retry.read_text', side_effect=['first', 'second']):
            result, select, output = self.review(['a', 'b', 'a', 'b', 'f', 'run'], request)
        self.assertEqual(request.call_args_list[1].args[0].question,
                         'original question\n补充要求：second')
        self.assertEqual(result[0], response('branch'))
        self.assertNotIn('f', select.call_args_list[3].kwargs['options'])
        self.assertIn('回答 3', output)
        self.assertEqual(request.call_count, 2)

    def test_failed_retry_from_history_preserves_forward_answer(self):
        for failure in (AskError('failure'), KeyboardInterrupt(), response('unused') | {'commands': []}):
            with self.subTest(failure=type(failure)):
                request = Mock(side_effect=[response('new'), failure])
                with patch('askai_cli.retry.read_text', side_effect=['first', 'failed']):
                    result, select, _ = self.review(['a', 'b', 'a', 'f', 'run'], request)
                self.assertEqual(result[0], response('new'))
                self.assertIn('f', select.call_args_list[3].kwargs['options'])

    def test_switch_to_codex_keeps_reasoning_and_restores_backend_in_history(self):
        request = Mock(side_effect=[response('codex'), response('high')])
        def choose(state, *args):
            return replace(state, name='codex', provider=dict(DEFAULTS['codex']), model=None)
        with patch('askai_cli.retry.select_model', side_effect=choose), \
             patch('askai_cli.retry.select_option', return_value=3):
            result, select, output = self.review(['m', 'r', 'b', 'b', 'f', 'f', 'run'], request)
        self.assertEqual(result[0], response('high'))
        self.assertNotIn('reasoning_effort', request.call_args_list[0].args[0].provider)
        self.assertEqual(request.call_args_list[1].args[0].provider['reasoning_effort'], 'high')
        self.assertTrue(all('r' in call.kwargs['options'] for call in select.call_args_list))
        self.assertIn('推理：沿用 Codex 配置', output)
        self.assertEqual(request.call_count, 2)

    def test_codex_can_restore_native_effort_without_overriding_config(self):
        request = Mock(return_value=response('native'))
        with patch('askai_cli.retry.select_option', return_value=0):
            self.review(['r', 'run'], request, {**DEFAULTS['codex'], 'reasoning_effort': 'high'})
        self.assertNotIn('reasoning_effort', request.call_args.args[0].provider)
        request = Mock()
        with patch('askai_cli.retry.select_option', return_value=0):
            self.review(['r', 'run'], request, dict(DEFAULTS['codex']))
        request.assert_not_called()

    def test_cancelled_retry_and_empty_answer_do_not_replace_current_answer(self):
        for failure in (KeyboardInterrupt(), AskError('bad response'), response('unused') | {'commands': []}):
            request = Mock(side_effect=failure) if isinstance(failure, BaseException) else Mock(return_value=failure)
            with self.subTest(failure=type(failure)), patch('askai_cli.retry.read_text', return_value='extra'):
                result, _, _ = self.review(['a', 'run'], request)
                self.assertEqual(result[0], response('old'))

    def test_cancel_or_current_effort_does_not_call_model(self):
        for selected in (None, 1):
            request = Mock()
            with patch('askai_cli.retry.select_option', return_value=selected):
                self.review(['r', 'run'], request)
            request.assert_not_called()

    def test_unsupported_adapter_has_no_reasoning_action(self):
        request = Mock()
        result, select, _ = self.review(['run'], request, {'type': 'command', 'executable': 'fixture'})
        self.assertNotIn('r', select.call_args.kwargs['options'])
        request.assert_not_called()

    def test_switching_api_model_does_not_carry_old_override(self):
        current = AnswerState('dsh', dict(DEFAULTS['dsh']), 'old-cli-model', 'question', response('old'))
        config = {'providers': {'deepseek': {}}}
        with patch('askai_cli.retry.available', return_value=True), \
             patch('askai_cli.retry.select_option', side_effect=[1, 1]), \
             patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'test-key'}):
            state = select_model(current, config, io.StringIO(), io.StringIO())
        self.assertEqual(state.name, 'deepseek')
        self.assertEqual(state.model, 'deepseek-v4-pro')
        self.assertNotIn('reasoning_effort', state.provider)
        self.assertEqual(current.model, 'old-cli-model')

    def test_dsh_adapter_passes_selected_reasoning_in_temporary_patch(self):
        from askai_cli.providers import cli_answer
        seen = []
        def backend(argv, payload, timeout, trace=None):
            patch_path = Path(argv[argv.index('--patch') + 1])
            seen.append(patch_path.read_text())
            return '{}'
        for effort in ('low', 'high'):
            with patch('askai_cli.providers.shutil.which', return_value='/fixture/dsh'), \
                 patch('askai_cli.providers.run_cli', side_effect=backend):
                cli_answer({'type': 'dsh', 'executable': 'dsh', 'reasoning_effort': effort},
                           None, 'system', 'question', 5)
            self.assertIn('reasoningEffort: ' + json.dumps(effort), seen[-1])

    def test_model_failure_restores_original_backend(self):
        request = Mock(side_effect=AskError('authentication failed'))
        def choose(state, *args):
            return replace(state, name='codex', provider={'type': 'codex'}, model='another-model')
        with patch('askai_cli.retry.select_model', side_effect=choose):
            result, _, output = self.review(['m', 'run'], request)
        self.assertEqual(result[0], response('old'))
        self.assertEqual(request.call_args.args[0].name, 'codex')
        self.assertEqual(output.count('本次使用：DSH'), 2)

    def test_selecting_effectively_same_model_does_not_retry(self):
        request = Mock()
        provider = {'type': 'api', 'base_url': 'https://example.invalid',
                    'model': 'one', 'api_key_env': 'RETRY_TEST_KEY'}
        with patch('askai_cli.retry.select_model', side_effect=lambda state, *args: replace(state, model='one')):
            self.review(['m', 'run'], request, provider)
        request.assert_not_called()

    def test_paged_selection(self):
        self.assertEqual(select_option('models', [str(i) for i in range(12)],
                                       io.StringIO('n\n2\n'), io.StringIO()), 10)


class RetryPtyTests(unittest.TestCase):
    def test_switch_to_codex_adjust_effort_and_browse_both_directions(self):
        fixture = fixtures.EndToEndTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        config = Path(fixture.env['ASKAI_CONFIG'])
        executable = json.dumps(str(fixture.fake))
        config.write_text(f'provider="dsh"\n[providers.dsh]\nexecutable={executable}\n'
                          f'[providers.codex]\nexecutable={executable}\n')
        count, capture = fixture.path / 'requests', fixture.path / 'captured.json'
        env = {**fixture.env, 'TERM': 'xterm-256color', 'ASKAI_TEST_REQUEST_COUNT': str(count),
               'ASKAI_TEST_CAPTURE': str(capture)}
        status, output = fixtures.terminal([str(fixtures.ASKAI), 'question'], env,
            ('m', '1', 'r', '4', 'b', 'f', 'p'), timeout=15,
            prompt_markers=('[默认 q]:', '按编号选择，Esc 返回:', '[默认 q]:',
                            '按编号选择，Esc 返回:', '[默认 q]:', '[默认 q]:', '[默认 q]:'))
        self.assertEqual(status, 0, output)
        self.assertEqual(count.read_text(), '3')
        self.assertIn('model_reasoning_effort="high"', json.loads(capture.read_text())['argv'])
        for text in ('已选择：m · 换模型', '已选择：1 · Codex', '已选择：r · 推理强度',
                     '已选择：4 · 高', '已选择：b · 上一版', '已选择：f · 下一版', '已选择：p · 打印方案 1'):
            self.assertIn(text, output)
        self.assertEqual(output.count('回答 3 · 本次使用：Codex'), 2)

    def test_dsh_reasoning_is_a_first_level_single_key_action(self):
        fixture = fixtures.EndToEndTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        config = Path(fixture.env['ASKAI_CONFIG'])
        config.write_text('provider="dsh"\n[providers.dsh]\nexecutable=' + json.dumps(str(fixture.fake)) + '\n')
        count = fixture.path / 'requests'
        env = {**fixture.env, 'TERM': 'xterm-256color', 'ASKAI_TEST_REQUEST_COUNT': str(count)}
        status, output = fixtures.terminal([str(fixtures.ASKAI), 'question'], env,
            ('r', '3', 'p1'), timeout=10,
            prompt_markers=('[默认 q]:', '按编号选择，Esc 返回:', '[默认 q]:'))
        self.assertEqual(status, 0, output)
        self.assertEqual(count.read_text(), '2')
        self.assertIn('推理：低', output)
        self.assertIn('推理：高', output)
        self.assertIn('上一版', output)

    def test_supplement_then_print_uses_new_request_without_execution(self):
        fixture = fixtures.EndToEndTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        count = fixture.path / 'requests'
        capture = fixture.path / 'captured.json'
        env = {**fixture.env, 'TERM': 'xterm-256color', 'ASKAI_TEST_REQUEST_COUNT': str(count),
               'ASKAI_TEST_CAPTURE': str(capture)}
        status, output = fixtures.terminal([str(fixtures.ASKAI), 'original question'], env,
            ('a', '只用系统自带命令\n', 'p1'), timeout=10,
            prompt_markers=('[默认 q]:', '补充要求（回车提交，空白返回）:', '[默认 q]:'))
        self.assertEqual(status, 0, output)
        self.assertEqual(count.read_text(), '2')
        self.assertIn('只用系统自带命令', json.loads(capture.read_text())['stdin'])
        self.assertIn('未复制到剪贴板', output)
        self.assertIn('上一版', output)

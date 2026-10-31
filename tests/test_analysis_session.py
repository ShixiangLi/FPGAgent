import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import main as cli
from fpga_agent import analysis_session as analysis
from fpga_agent import python_tool, tools, waveform, workflow
from fpga_agent.agent import DeepSeekClient, GenerationTruncated

ROOT = Path(__file__).resolve().parent.parent
GOOD = '''import argparse,csv,json
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--input');p.add_argument('--output');a=p.parse_args()
rows=list(csv.DictReader(open(a.input)))
values=[int(r['value'],16) for r in rows]
Path(a.output,'report.json').write_text(json.dumps({'values':values}))
'''


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.task = self.root / 'case'
        self.task.mkdir()
        self.config = workflow.load_config(ROOT / 'config.json')
        self.config.update(workspace_path=str(self.root), analysis_max_steps=12)
        key = patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'test-only'})
        key.start()
        self.addCleanup(key.stop)
        tools.write_json(self.task / 'state.json', {'task_id': 'case', 'status': 'interrupted'})
        self.candidate = {'spec': {'summary': 'analyze saved evidence'}, 'project': {
            'files': {'rtl/top.sv': 'module top; endmodule', 'scripts/check.py': GOOD},
            'hardware_analysis': {'script': 'scripts/check.py', 'outputs': ['report.json']}}}
        self.save_candidate()
        (self.task / 'samples.csv').write_text('elapsed_ms,value\n0,0a\n100,ff\n')
        self.parent_state = (self.task / 'state.json').read_bytes()

    def save_candidate(self):
        tools.write_json(self.task / 'candidate.json', self.candidate)

    def run_analysis(self, **kwargs):
        if not kwargs:
            kwargs = {'candidate': 'candidate.json', 'source': 'samples.csv', 'script': 'scripts/check.py', 'request': 'Analyze the captured values'}
        return analysis.run('case', self.config, **kwargs)

    def test_actual_error_edit_execution_and_model_assessment(self):
        self.candidate['project']['files']['scripts/check.py'] = GOOD.replace("int(r['value'],16)", "int(r['value'])")
        self.save_candidate()
        actions = [{'action': 'execute'}, {'action': 'read_files', 'paths': ['input_preview.txt']},
                   {'action': 'edit', 'instructions': 'Read the actual input and fix the parser'},
                   {'action': 'deliver', 'files': {'scripts/check.py': GOOD}},
                   {'action': 'execute'}, {'action': 'finish', 'approved': True, 'message': 'Captured values analyzed'}]
        with patch.object(DeepSeekClient, 'complete', side_effect=actions) as llm, patch.object(
                tools, 'run_build', side_effect=AssertionError('must not build')), patch.object(
                tools, 'program_device', side_effect=AssertionError('must not program')):
            state = self.run_analysis()
        self.assertEqual(state['status'], 'reviewed')
        self.assertEqual([r['passed'] for r in state['results']], [False, True])
        failure = json.loads(llm.call_args_list[1].args[0][1]['content'])['last_result']
        self.assertNotEqual(failure['exit_code'], 0)
        self.assertIn('ValueError', failure['diagnostic'])
        self.assertIn('input_preview.txt', json.loads(llm.call_args_list[1].args[0][1]['content'])['file_index'])
        edited = json.loads(llm.call_args_list[4].args[0][1]['content'])['last_edit']
        self.assertTrue(edited['written'])
        self.assertEqual(edited['script_sha256'], state['results'][-1]['script_sha256'])
        self.assertEqual(json.loads(state['results'][-1]['artifact_excerpts']['report.json'])['values'], [10, 255])
        self.assertEqual((self.task / 'state.json').read_bytes(), self.parent_state)
        self.assertEqual(json.loads((self.task / 'candidate.json').read_text())['project'], self.candidate['project'])

    def test_successful_process_does_not_auto_approve_or_retry(self):
        with patch.object(DeepSeekClient, 'complete', side_effect=[{'action': 'execute'},
                {'action': 'stop', 'message': 'Evidence is insufficient'}]) as llm:
            state = self.run_analysis()
        self.assertEqual(state['status'], 'stopped')
        self.assertTrue(state['results'][0]['passed'])
        self.assertEqual(llm.call_count, 2)

    def test_missing_execution_and_user_question_resume(self):
        with patch.object(DeepSeekClient, 'complete', side_effect=[{'action': 'finish', 'approved': True},
                {'action': 'clarify', 'message': 'Is the capture the requested version?'}]) as llm:
            waiting = self.run_analysis()
        self.assertIn('no successful execution', json.loads(llm.call_args.args[0][1]['content'])['feedback'])
        with patch.object(DeepSeekClient, 'complete') as llm:
            state = self.run_analysis(resume=waiting['analysis_id'])
        llm.assert_not_called()
        self.assertEqual(state['status'], 'waiting_for_user')
        with patch.object(DeepSeekClient, 'complete', side_effect=[{'action': 'execute'},
                {'action': 'finish', 'approved': True}]) as llm:
            state = self.run_analysis(resume=waiting['analysis_id'], answer='Yes, this capture')
        self.assertEqual(json.loads(llm.call_args_list[0].args[0][1]['content'])['user_reply']['answer'], 'Yes, this capture')
        self.assertEqual(state['status'], 'reviewed')

    def test_edit_invalidates_old_success(self):
        with patch.object(DeepSeekClient, 'complete', side_effect=[{'action': 'execute'},
                {'action': 'edit', 'instructions': 'Add a comment'},
                {'action': 'deliver', 'files': {'scripts/check.py': GOOD + '\n# changed\n'}},
                {'action': 'finish', 'approved': True}, {'action': 'stop', 'message': 'Need a new execution'}]) as llm:
            state = self.run_analysis()
        self.assertEqual(state['status'], 'stopped')
        self.assertIn('older script result', json.loads(llm.call_args.args[0][1]['content'])['feedback'])

    def test_editor_interrupt_resumes_staged_work(self):
        checkpoint = {'action': 'checkpoint', 'summary': 'comment staged', 'next_step': 'deliver',
                      'edits': [{'path': 'scripts/check.py', 'old': GOOD, 'new': GOOD + '\n# staged\n'}]}
        with patch.object(DeepSeekClient, 'complete', side_effect=[{'action': 'edit', 'instructions': 'Add a comment'}, checkpoint, KeyboardInterrupt()]), self.assertRaises(KeyboardInterrupt):
            self.run_analysis()
        folder = next((self.task / 'analysis').iterdir())
        state = json.loads((folder / 'state.json').read_text())
        self.assertEqual(state['status'], 'interrupted')
        self.assertIn('pending_edit', state)
        self.assertFalse((folder / 'active.lock').exists())
        with patch.object(DeepSeekClient, 'complete', side_effect=[{'action': 'deliver'}, {'action': 'execute'},
                {'action': 'finish', 'approved': True}]):
            state = self.run_analysis(resume=folder.name)
        self.assertEqual(state['status'], 'reviewed')
        self.assertTrue((folder / 'project/scripts/check.py').read_text().endswith('# staged\n'))

    def test_read_paths_and_budget_are_bounded(self):
        self.config['analysis_max_steps'] = 2
        with patch.object(DeepSeekClient, 'complete', side_effect=[{'action': 'read_files', 'paths': ['../../.env']},
                {'action': 'read_files', 'paths': ['input_preview.txt']}]) as llm:
            state = self.run_analysis()
        self.assertIn('indexed paths', json.loads(llm.call_args.args[0][1]['content'])['feedback'])
        self.assertEqual(state['status'], 'paused')

    def test_vcd_session_without_hardware_metadata(self):
        self.candidate['project']['hardware_analysis'] = None
        self.save_candidate()
        (self.task / 'waveform.vcd').write_text('$enddefinitions $end\n#0\n')
        folder = analysis.create(self.task, 'candidate.json', 'waveform.vcd', 'scripts/check.py', 'Analyze VCD')
        state = json.loads((folder / 'state.json').read_text())
        self.assertEqual(state['outputs'], [])
        self.assertEqual(state['baseline']['input'], 'input.vcd')
        with self.assertRaisesRegex(ValueError, 'saved scope'):
            self.run_analysis(resume=folder.name, request='Replace scope')

    def test_protected_source_change_stops_before_llm(self):
        folder = analysis.create(self.task, 'candidate.json', 'samples.csv', 'scripts/check.py', 'Analyze')
        (folder / 'project/rtl/top.sv').write_text('changed')
        with patch.object(DeepSeekClient, 'complete') as llm, self.assertRaisesRegex(RuntimeError, 'Read-only baseline'):
            self.run_analysis(resume=folder.name)
        llm.assert_not_called()
        self.assertEqual(json.loads((folder / 'state.json').read_text())['status'], 'error')

    def test_input_tampering_is_not_success(self):
        self.candidate['project']['files']['scripts/check.py'] = GOOD + "\nPath(a.input).write_text('tampered')\n"
        self.save_candidate()
        with patch.object(DeepSeekClient, 'complete', side_effect=[{'action': 'execute'}, {'action': 'stop'}]):
            state = self.run_analysis()
        self.assertFalse(state['results'][0]['passed'])
        self.assertIn('modified its input', state['results'][0]['diagnostic'])
        self.assertIn('elapsed_ms', (self.task / 'samples.csv').read_text())

    def test_prepare_preserves_exception_when_log_was_not_created(self):
        with patch.object(python_tool, '_commands', side_effect=OSError('cannot create process')):
            result = python_tool.prepare(self.task, self.config, {'scripts/a.py': 'pass'}, {'script': 'scripts/a.py', 'outputs': ['data/a.txt']})
        self.assertFalse(result['passed'])
        self.assertEqual(result['error_type'], 'OSError')
        self.assertIn('cannot create process', result['diagnostic'])

    def test_large_waveform_keeps_exact_preview_and_full_counts(self):
        path = self.task / 'small.vcd'
        path.write_text('$timescale 1ns $end\n$scope module tb $end\n$var wire 1 ! q $end\n$upscope $end\n$enddefinitions $end\n#0\n0!\n#1\n1!\n#2\nx!\n#3\n0!\n#4\n1!\n')
        variables, samples, end, scale = waveform.read_vcd(path, max_events=2)
        self.assertEqual(samples['!'], [(0, '0'), (1, '1')])
        self.assertEqual((end, scale), (4, '1ns'))
        self.assertEqual(variables['!']['events'], 5)
        self.assertEqual(variables['!']['unknown_events'], 1)
        self.assertTrue(variables['!']['preview_truncated'])
        self.assertEqual(variables['!']['display_end_tick'], 2)

    def test_diagnosis_cannot_replace_raw_error(self):
        state = {'status': 'hardware_failed', 'error': 'actual error', 'results': [
            {'stage': 'program', 'passed': False, 'diagnostic': 'actual error', 'run_dir': 'original'}]}
        tools.write_json(self.task / 'state.json', state)
        with patch.object(workflow, 'explain_failure', return_value={'message': 'possible explanation'}):
            result = workflow.diagnose('case', self.config)
        self.assertEqual(result, state)
        self.assertEqual(json.loads((self.task / 'state.json').read_text()), state)

    def test_cli_waits_and_resumes_analysis_question(self):
        waiting = {'status': 'waiting_for_user', 'analysis_id': 'abc', 'question': 'Confirm scope?',
                   'report': str(self.task / 'report.md')}
        finished = {**waiting, 'status': 'reviewed', 'assessment': {'approved': True}}
        with patch.object(analysis, 'run', side_effect=[waiting, finished]) as run, patch(
                'builtins.input', side_effect=['', 'confirmed']) as ask, patch('sys.stdout', new_callable=io.StringIO):
            code = cli.main(['analyze', 'case', '--resume', 'abc'])
        self.assertEqual(code, 0)
        self.assertEqual(ask.call_count, 2)
        self.assertEqual(run.call_args.kwargs, {'resume': 'abc', 'answer': 'confirmed'})

    def test_cli_batch_saves_question_without_reading_stdin(self):
        waiting = {'status': 'waiting_for_user', 'analysis_id': 'abc', 'question': 'Confirm scope?',
                   'report': str(self.task / 'report.md')}
        with patch.object(analysis, 'run', return_value=waiting) as run, patch('builtins.input') as ask, patch(
                'sys.stdout', new_callable=io.StringIO):
            code = cli.main(['analyze', 'case', '--resume', 'abc', '--non-interactive'])
        self.assertEqual(code, 2)
        self.assertEqual(run.call_count, 1)
        ask.assert_not_called()

    def test_analysis_decision_truncation_resumes_saved_session(self):
        with patch.object(DeepSeekClient, 'complete', side_effect=GenerationTruncated({'finish_reason': 'length'})):
            paused = self.run_analysis()
        self.assertEqual(paused['status'], 'model_paused')
        self.assertEqual(paused['model_pause']['stage'], 'analysis')
        with patch.object(DeepSeekClient, 'complete', side_effect=[
                {'action': 'checkpoint', 'summary': 'Input and script retained', 'next_step': 'Execute current script'},
                {'action': 'execute'}, {'action': 'finish', 'approved': True}]) as llm:
            finished = self.run_analysis(resume=paused['analysis_id'])
        self.assertEqual(finished['status'], 'reviewed')
        self.assertEqual(len(finished['results']), 1)
        self.assertIsNotNone(json.loads(llm.call_args_list[0].args[0][1]['content'])['recovery'])


if __name__ == '__main__':
    unittest.main()

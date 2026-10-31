"""Real Python execution through the repair loop, with controlled model decisions."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fpga_agent import development, python_tool, tools, workflow
from fpga_agent.agent import DeepSeekClient, GenerationTruncated

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = '''import argparse,csv,json
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--input');p.add_argument('--output');a=p.parse_args()
values=[int(r['value'],16) for r in csv.DictReader(Path(a.input).open())]
Path(a.output,'report.json').write_text(json.dumps({'values':values}))
print('ANALYSIS_EXECUTED', values)
'''
EXECUTE = {'action': 'execute', 'script': 'scripts/check.py', 'input': 'data/mock.csv', 'outputs': ['report.json']}
APPROVAL = {'approved': True, 'coverage': {'A1': 'evidence'}, 'blocking_findings': [], 'warnings': [], 'pending_checks': []}
FAILURE = {'stage': 'review', 'passed': False, 'run_dir': 'review', 'diagnostic': 'Run scripts/check.py on data/mock.csv',
           'blocking_findings': ['Missing actual Python execution']}


class ExecutionDispatchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.task = Path(temporary.name) / 'case'
        self.task.mkdir()
        self.config = workflow.load_config(ROOT / 'config.json')
        self.config.update(workspace_path=temporary.name, target='build', visualization=False, incremental=True, max_repairs=2)
        self.files = {'rtl/unit.sv': 'module unit; endmodule\n', 'scripts/check.py': SCRIPT,
                      'data/mock.csv': 'elapsed_ms,value\n0,0a\n100,ff\n'}
        self.response = {'action': 'design', 'spec': {'summary': 'unit', 'requirements': ['unit'], 'acceptance': ['unit']},
                         'project': {'top': 'unit', 'testbench': 'tb', 'files': self.files}}
        self.state = {'task_id': 'case', 'status': 'repairing', 'request': 'unit', 'results': [],
                      'spec': self.response['spec'], 'plan': {'development_tasks': []}}
        workflow.save(self.task, self.state)
        env = patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'test-only'})
        env.start()
        self.addCleanup(env.stop)

    def repair(self, actions, response=None):
        with patch.object(DeepSeekClient, 'complete', side_effect=actions) as llm:
            result = development.repair(self.task, self.state, self.config, response or self.response, FAILURE)
        return result, llm

    def test_executes_staged_source_returns_raw_error_and_continues(self):
        # The active project must not be overwritten by a repair-time script run.
        active = self.task / 'project/rtl/unit.sv'
        active.parent.mkdir(parents=True)
        active.write_text('ACTIVE_BUILD_SOURCE')
        checkpoint = {'action': 'checkpoint', 'summary': 'Test bad parser', 'next_step': 'Run parser', 'edits': [
            {'path': 'scripts/check.py', 'old': "int(r['value'],16)", 'new': "int(r['value'])"}]}
        fixed = {'action': 'checkpoint', 'summary': 'Use hexadecimal data', 'next_step': 'Run corrected script', 'edits': [
            {'path': 'scripts/check.py', 'old': "int(r['value'])", 'new': "int(r['value'],16)"}]}
        result, llm = self.repair([checkpoint, EXECUTE, fixed, EXECUTE, {'action': 'verify'}])
        failed, passed = self.state['results']
        self.assertEqual(failed['exit_code'], 1)
        self.assertIn("ValueError: invalid literal for int() with base 10: '0a'", failed['diagnostic'])
        error_feedback = json.loads(llm.call_args_list[2].args[0][1]['content'])['read_error']['execution']
        self.assertEqual(error_feedback['diagnostic'], failed['diagnostic'])
        self.assertEqual(passed['exit_code'], 0)
        self.assertEqual(passed['source_files_sha256'], python_tool.files_hash(self.files))
        self.assertNotEqual(failed['source_files_sha256'], passed['source_files_sha256'])
        self.assertEqual(json.loads(Path(passed['artifacts'][0]).read_text()), {'values': [10, 255]})
        self.assertEqual(result['project'], self.response['project'])
        self.assertEqual(active.read_text(), 'ACTIVE_BUILD_SOURCE')
        payload = json.loads(llm.call_args_list[4].args[0][1]['content'])
        self.assertIn('execute', payload['available_tools'])
        self.assertTrue(any(n.endswith('report.json') for n in payload['file_index']))
        self.assertNotIn('hardware_verified', passed)
        self.assertEqual(passed['input_origin'], 'project_file')

    def test_execution_is_retained_across_question_and_resume(self):
        paused, _ = self.repair([EXECUTE, {'action': 'clarify', 'message': 'Continue with these results?'}])
        self.assertEqual(paused['action'], 'clarify')
        self.state['development_reply'] = {'answer': 'Continue'}
        with patch.object(python_tool, 'execute', side_effect=AssertionError('must not re-execute')):
            result, llm = self.repair([{'action': 'verify'}])
        self.assertEqual(result['action'], 'design')
        self.assertEqual(len(self.state['results']), 1)
        payload = json.loads(llm.call_args.args[0][1]['content'])
        self.assertEqual(payload['tool_results'][0]['run_dir'], self.state['results'][0]['run_dir'])
        self.assertEqual(payload['user_reply']['answer'], 'Continue')

    def test_task_evidence_input_and_missing_output(self):
        source = self.task / 'runs/capture/input.csv'
        source.parent.mkdir(parents=True)
        source.write_text(self.files['data/mock.csv'])
        request = {**EXECUTE, 'input': 'runs/capture/input.csv', 'outputs': ['missing.png']}
        result = python_tool.execute(self.task, self.config, self.files, request)
        self.assertFalse(result['passed'])
        self.assertEqual(result['exit_code'], 0)
        self.assertIn('Analysis did not generate required artifacts: missing.png', result['error'])
        self.assertEqual(result['input_origin'], 'saved_task_file')
        self.assertEqual(source.read_text(), self.files['data/mock.csv'])
        self.assertTrue(result['artifacts'])  # Partial outputs survive failure.

    def test_rejects_invalid_execution_paths_before_starting_a_process(self):
        requests = [dict(EXECUTE, input='../secret.csv'), dict(EXECUTE, input='F:/secret.csv'),
                    dict(EXECUTE, input='.env'), dict(EXECUTE, input='missing.csv'),
                    dict(EXECUTE, script='rtl/unit.sv'), dict(EXECUTE, outputs=['../report.json'])]
        for request in requests:
            with self.subTest(request=request), patch.object(python_tool, '_commands') as commands:
                with self.assertRaises(ValueError):
                    python_tool.execute(self.task, self.config, self.files, request)
                commands.assert_not_called()

    def test_unavailable_execute_is_feedback_not_a_silent_verify(self):
        with patch.object(DeepSeekClient, 'complete', side_effect=[EXECUTE, {'action': 'verify'}]) as llm:
            result = development.edit(self.task, self.config, {'goal': 'Edit only'}, self.files, [])
        payload = json.loads(llm.call_args_list[1].args[0][1]['content'])
        self.assertIn('execute is not available', payload['read_error']['error'])
        self.assertEqual(result['action'], 'deliver')
        self.assertFalse(self.state['results'])

    def test_truncation_recovery_must_checkpoint_before_execution(self):
        truncated = GenerationTruncated({'finish_reason': 'length', 'content_chars': 0})
        actions = [truncated, EXECUTE,
                   {'action': 'checkpoint', 'summary': 'Recover the saved work', 'next_step': 'Execute one script'},
                   EXECUTE, {'action': 'verify'}]
        result, llm = self.repair(actions)
        blocked = json.loads(llm.call_args_list[2].args[0][1]['content'])
        self.assertIn('recovery', blocked['read_error']['error'])
        self.assertEqual(len(self.state['results']), 1)
        self.assertEqual(result['action'], 'design')

    def run_workflow(self, actions):
        def materialize(task, config, spec, design):
            project = task / 'project'
            for name, text in design['files'].items():
                path = project / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding='utf-8')
            tools.write_json(project / 'project.json', {'top': 'unit'})
            return {**config, 'project_path': str(project)}

        with patch.object(workflow, 'validate_design', side_effect=lambda r, c: (r['spec'], r['project'])), patch.object(
                workflow, 'prepare_project', side_effect=materialize), patch.object(tools, 'run_simulation',
                return_value={'stage': 'simulation', 'passed': True, 'run_dir': 'sim'}) as sim, patch.object(
                tools, 'run_build', return_value={'stage': 'build', 'passed': True, 'run_dir': 'build', 'artifact': {}}) as build, patch.object(
                DeepSeekClient, 'complete', side_effect=actions) as llm:
            state = workflow.develop(self.task, self.state, self.config, self.response)
        return state, llm, sim.call_count, build.call_count

    def test_reviewer_receives_new_evidence_without_rebuilding_unchanged_sources(self):
        rejected = {**APPROVAL, 'approved': False, 'blocking_findings': ['Run Python on mock CSV']}
        state, llm, sims, builds = self.run_workflow([rejected, EXECUTE, {'action': 'verify'}, APPROVAL])
        self.assertEqual(state['status'], 'ready_to_program')
        self.assertEqual((sims, builds), (1, 1))
        payload = json.loads(llm.call_args_list[3].args[0][1]['content'])
        execution = payload['additional_executions'][0]
        self.assertEqual(execution['exit_code'], 0)
        self.assertEqual(execution['requested_input'], 'data/mock.csv')
        self.assertTrue(execution['artifacts'])
        self.assertNotIn('program', [r['stage'] for r in state['results']])

    def test_source_changes_repeat_tools_and_exclude_stale_execution_evidence(self):
        rejected = {**APPROVAL, 'approved': False, 'blocking_findings': ['Need a change']}
        changed = {'action': 'checkpoint', 'summary': 'RTL modified', 'next_step': 'Integrated verification', 'edits': [
            {'path': 'rtl/unit.sv', 'old': self.files['rtl/unit.sv'], 'new': self.files['rtl/unit.sv'] + '// changed\n'}]}
        state, llm, sims, builds = self.run_workflow([rejected, EXECUTE, changed, {'action': 'verify'}, APPROVAL])
        self.assertEqual(state['status'], 'ready_to_program')
        self.assertEqual((sims, builds), (2, 2))
        payload = json.loads(llm.call_args_list[4].args[0][1]['content'])
        self.assertEqual(payload['additional_executions'], [])

    def test_review_pause_after_execution_resumes_same_evidence_without_reexecuting(self):
        rejected = {**APPROVAL, 'approved': False, 'blocking_findings': ['Run Python']}
        truncation = GenerationTruncated({'finish_reason': 'length', 'content_chars': 0})
        state, _, sims, builds = self.run_workflow([rejected, EXECUTE, {'action': 'verify'}, truncation, truncation])
        self.assertEqual(state['status'], 'model_paused')
        self.assertEqual((sims, builds), (1, 1))
        pending = state['verification_checkpoint']
        response = json.loads((self.task / pending['candidate']).read_text(encoding='utf-8'))
        with patch.object(workflow, 'validate_design', side_effect=lambda r, c: (r['spec'], r['project'])), patch.object(
                tools, 'run_simulation', side_effect=AssertionError('no simulation')), patch.object(
                tools, 'run_build', side_effect=AssertionError('no build')), patch.object(
                python_tool, 'execute', side_effect=AssertionError('no Python rerun')), patch.object(
                DeepSeekClient, 'complete', side_effect=[
                    {'action': 'checkpoint', 'summary': 'Evidence retained', 'next_step': 'Decide'}, APPROVAL]):
            result = workflow.develop(self.task, state, self.config, response)
        self.assertEqual(result['status'], 'ready_to_program')
        self.assertEqual(sum(r['stage'] == 'analysis' for r in result['results']), 1)


if __name__ == '__main__':
    unittest.main()

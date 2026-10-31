import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import main as cli
from fpga_agent import board_research, model_steps, session, tools, workflow
from fpga_agent.agent import DeepSeekClient, GenerationTruncated, review_project

ROOT = Path(__file__).resolve().parent.parent
REVIEW = {'approved': True, 'blocking_findings': [], 'warnings': [], 'pending_checks': [], 'coverage': {'A1': 'actual evidence'}}


def truncated():
    return GenerationTruncated({'finish_reason': 'length', 'usage': {'completion_tokens': 32768}, 'response_file': 'partial.txt'})


class ModelStepsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.task = Path(temporary.name) / 'case'
        self.task.mkdir()
        self.config = workflow.load_config(ROOT / 'config.json')
        self.config['workspace_path'] = temporary.name
        env = patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'test-only'})
        env.start()
        self.addCleanup(env.stop)

    def run_steps(self, **kwargs):
        return model_steps.run(self.task, self.config, 'test', {'version': 1}, 'Return JSON', {'goal': 'test'}, **kwargs)

    def test_draft_survives_truncation_and_merges_small_updates(self):
        actions = [
            {'action': 'checkpoint', 'summary': 'first part', 'next_step': 'next item',
             'updates': {'action': 'plan', 'coverage': {'A1': 'checked'}}, 'append': {'development_tasks': [{'id': 'rtl'}]}},
            truncated(),
            {'action': 'checkpoint', 'summary': 'first part saved', 'next_step': 'one remaining item'},
            {'action': 'checkpoint', 'summary': 'complete', 'next_step': 'submit',
             'updates': {'coverage': {'A2': 'checked'}}, 'append': {'development_tasks': [{'id': 'tb'}]}},
            {'action': 'submit'}]
        with patch.object(DeepSeekClient, 'complete', side_effect=actions) as llm:
            result = self.run_steps()
        self.assertEqual(result['coverage'], {'A1': 'checked', 'A2': 'checked'})
        self.assertEqual([t['id'] for t in result['development_tasks']], ['rtl', 'tb'])
        recovery = json.loads(llm.call_args_list[2].args[0][1]['content'])
        self.assertEqual(recovery['draft']['development_tasks'], [{'id': 'rtl'}])
        self.assertEqual(recovery['recovery']['finish_reason'], 'length')

    def test_pause_and_resume_preserves_phase_and_user_selected_budget(self):
        before = copy.deepcopy(self.config)
        with patch.object(DeepSeekClient, 'complete', side_effect=[truncated(), truncated()]), self.assertRaises(model_steps.ModelPaused) as error:
            self.run_steps()
        self.assertEqual(self.config, before)
        checkpoint = Path(error.exception.details['checkpoint'])
        self.assertTrue(checkpoint.is_file())
        self.config['llm'] = {**self.config['llm'], 'reasoning_effort': 'low', 'max_tokens': 40000}
        with patch.object(DeepSeekClient, 'complete', side_effect=[
                {'action': 'checkpoint', 'summary': 'saved', 'next_step': 'one finding'},
                {'approved': False, 'blocking_findings': ['not enough evidence']}]) as llm:
            result = self.run_steps()
        self.assertFalse(result['approved'])
        self.assertIsNotNone(json.loads(llm.call_args_list[0].args[0][1]['content'])['recovery'])
        self.assertEqual(len(list((self.task / 'model_steps').glob('*.json'))), 1)

    def test_interruption_keeps_committed_draft(self):
        with patch.object(DeepSeekClient, 'complete', side_effect=[
                {'action': 'checkpoint', 'summary': 'saved', 'next_step': 'submit', 'updates': {'approved': False}},
                KeyboardInterrupt()]), self.assertRaises(KeyboardInterrupt):
            self.run_steps()
        with patch.object(DeepSeekClient, 'complete', return_value={'action': 'submit'}):
            self.assertEqual(self.run_steps(), {'approved': False})

    def test_invalid_final_document_is_kept_for_model_repair(self):
        def validate(document):
            return [] if document.get('value') == 42 else ['value must be 42']

        with patch.object(DeepSeekClient, 'complete', side_effect=[{'value': 10},
                {'action': 'checkpoint', 'summary': 'fixed field', 'next_step': 'submit', 'updates': {'value': 42}},
                {'action': 'submit'}]) as llm:
            self.assertEqual(self.run_steps(validate=validate), {'value': 42})
        payload = json.loads(llm.call_args_list[1].args[0][1]['content'])
        self.assertEqual(payload['draft'], {'value': 10})
        self.assertEqual(payload['feedback'], ['value must be 42'])

    def test_large_feedback_remains_readable_after_pause_without_filling_context(self):
        self.config['model_max_steps'] = 2

        def validate(document):
            return [] if document.get('value') == 42 else ['actual interface error: ' + 'x' * 20000]

        with patch.object(DeepSeekClient, 'complete', side_effect=[{'value': 1},
                {'action': 'read_files', 'paths': ['feedback.json'], 'limit': 100}]), self.assertRaises(model_steps.ModelPaused):
            self.run_steps(validate=validate)
        with patch.object(DeepSeekClient, 'complete', return_value={'value': 42}) as llm:
            self.run_steps(validate=validate)
        payload = llm.call_args.args[0][1]['content']
        self.assertLess(len(payload), 12000)
        self.assertIn('actual interface error', json.loads(payload)['file_views']['feedback.json']['text'])

    def test_invalid_checkpoint_is_atomic_and_empty_submit_is_not_completion(self):
        actions = [{'action': 'submit'},
                   {'action': 'checkpoint', 'summary': 'bad', 'next_step': 'submit',
                    'updates': {'approved': True}, 'append': {'approved': ['bad']}},
                   {'approved': False}]
        with patch.object(DeepSeekClient, 'complete', side_effect=actions) as llm:
            result = self.run_steps()
        payload = json.loads(llm.call_args_list[2].args[0][1]['content'])
        self.assertEqual(payload['draft'], {})
        self.assertIn('append', payload['feedback']['error'])
        self.assertFalse(result['approved'])

    def test_review_uses_same_bounded_context_and_can_read_full_report(self):
        report = 'BEGIN\n' + 'z' * 600000 + '\nEND'
        design = {'top': 'unit', 'testbench': 'tb', 'files': {'rtl/unit.sv': 'module unit; endmodule'}}
        build = {'stage': 'build', 'passed': True, 'reports': {'io.rpt': report}, 'run_dir': 'evidence'}
        with patch.object(DeepSeekClient, 'complete', side_effect=[
                {'action': 'read_files', 'paths': ['build/reports/io.rpt'], 'offset': 600000, 'limit': 1000}, REVIEW]) as llm:
            review_project('review', {}, design, self.config, self.task / 'llm_calls.jsonl', build_result=build)
        first = llm.call_args_list[0].args[0][1]['content']
        self.assertLess(len(first), 15000)
        second = json.loads(llm.call_args_list[1].args[0][1]['content'])
        self.assertIn('END', second['file_views']['build/reports/io.rpt']['text'])
        other = self.task / 'other'
        other.mkdir()
        with patch.object(DeepSeekClient, 'complete', return_value=REVIEW) as llm:
            review_project('review', {}, design, {**self.config, 'task_memory_path': str(other)}, other / 'llm_calls.jsonl', build_result=build)
        self.assertEqual(first, llm.call_args.args[0][1]['content'])

    def test_clarification_can_resume_without_cached_question_loop(self):
        with patch.object(DeepSeekClient, 'complete', return_value={'action': 'clarify', 'message': 'Which evidence?'}):
            self.run_steps()
        with patch.object(DeepSeekClient, 'complete', return_value=REVIEW) as llm:
            result = self.run_steps(user_reply={'answer': 'Use saved data'})
        self.assertTrue(result['approved'])
        self.assertEqual(json.loads(llm.call_args.args[0][1]['content'])['user_reply']['answer'], 'Use saved data')

    def test_requirements_resume_before_any_confirmed_spec_exists(self):
        spec = {'summary': 'counter', 'requirements': ['counter'], 'acceptance': ['check counter']}
        analysis = {'action': 'requirements', 'spec': spec, 'target': 'sim', 'program': False, 'visualization': False}
        with patch.object(DeepSeekClient, 'complete', side_effect=[
                {'action': 'checkpoint', 'summary': 'counter task', 'next_step': 'finish spec', 'updates': {'spec': spec}},
                truncated(), truncated()]):
            paused = session.run('Simulate a counter', self.config)
        self.assertEqual(paused['status'], 'model_paused')
        self.assertEqual(paused['model_pause']['stage'], 'requirements')
        with patch.object(DeepSeekClient, 'complete', side_effect=[
                {'action': 'checkpoint', 'summary': 'spec retained', 'next_step': 'submit requirements'},
                {'action': 'checkpoint', 'summary': 'requirements ready', 'next_step': 'submit', 'updates': analysis},
                {'action': 'submit'}]) as llm:
            finished = session.run('', self.config, task_id=paused['task_id'], continue_existing=True)
        self.assertEqual(finished['status'], 'awaiting_confirmation')
        self.assertEqual(finished['pending_confirmation'], 'requirements')
        self.assertEqual(finished['spec'], spec)
        self.assertEqual(json.loads(llm.call_args_list[0].args[0][1]['content'])['draft']['spec'], spec)
        self.assertEqual(len(list((Path(self.config['workspace_path']) / paused['task_id'] / 'model_steps').glob('requirements-*.json'))), 1)

    def test_requirements_interruption_can_continue_without_replanning_from_scratch(self):
        spec = {'summary': 'counter', 'requirements': ['counter'], 'acceptance': ['check counter']}
        with patch.object(DeepSeekClient, 'complete', side_effect=[
                {'action': 'checkpoint', 'summary': 'spec staged', 'next_step': 'submit', 'updates': {'spec': spec}},
                KeyboardInterrupt()]), self.assertRaises(KeyboardInterrupt):
            session.run('Simulate a counter', self.config)
        task = next(p for p in Path(self.config['workspace_path']).glob('*/state.json'))
        paused = json.loads(task.read_text(encoding='utf-8'))
        self.assertEqual(paused['status'], 'interrupted')
        with patch.object(DeepSeekClient, 'complete', return_value={'action': 'requirements', 'spec': spec,
                'target': 'sim', 'program': False, 'visualization': False}) as llm:
            result = session.run('', self.config, task_id=paused['task_id'], continue_existing=True)
        self.assertEqual(result['status'], 'awaiting_confirmation')
        self.assertEqual(json.loads(llm.call_args.args[0][1]['content'])['draft']['spec'], spec)

    def test_planning_resume_retains_confirmed_requirements(self):
        spec = {'summary': 'counter', 'requirements': ['counter'], 'acceptance': ['check counter']}
        analysis = {'action': 'requirements', 'spec': spec, 'target': 'sim', 'program': False, 'visualization': False}
        plan = {**analysis, 'action': 'plan', 'design': 'counter', 'test_plan': 'assert counter',
                'project': {'top': 'unit', 'testbench': 'tb', 'simulation_timeout_ns': 1000, 'bindings': {}, 'debug': None},
                'development_tasks': [{'id': name, 'kind': 'files', 'goal': name, 'completion': name,
                                       'inputs': [], 'outputs': [file]} for name, file in (
                                           ('rtl', 'rtl/unit.sv'), ('tb', 'sim/tb.sv'), ('readme', 'README.md'))]}
        with patch.object(DeepSeekClient, 'complete', side_effect=[analysis,
                {'action': 'checkpoint', 'summary': 'plan started', 'next_step': 'add tasks', 'updates': {'design': 'counter'}},
                truncated(), truncated()]):
            paused = session.run('Simulate a counter', self.config, ask=lambda _: 'y')
        self.assertEqual(paused['status'], 'model_paused')
        approval = copy.deepcopy(paused['confirmations']['requirements'])
        with patch.object(DeepSeekClient, 'complete', side_effect=[
                {'action': 'checkpoint', 'summary': 'design retained', 'next_step': 'finish plan'},
                {'action': 'checkpoint', 'summary': 'plan ready', 'next_step': 'submit', 'updates': plan},
                {'action': 'submit'}]) as llm:
            finished = session.run('', self.config, task_id=paused['task_id'], continue_existing=True)
        self.assertEqual(finished['status'], 'awaiting_confirmation')
        self.assertEqual(finished['pending_confirmation'], 'test_plan')
        self.assertEqual(finished['confirmations']['requirements'], approval)
        self.assertEqual(json.loads(llm.call_args_list[0].args[0][1]['content'])['draft']['design'], 'counter')

    def test_board_research_truncation_does_not_install_profile(self):
        profile = ROOT / 'boards/test_pause_board.json'
        before = profile.read_bytes() if profile.exists() else None
        with patch.object(board_research, 'Documents') as documents, patch.object(
                board_research.research, 'import_sources', return_value=[]), patch.object(
                board_research.research, 'available_sources', return_value=[]):
            documents.return_value.inventory.return_value = []
            with patch.object(DeepSeekClient, 'complete', side_effect=[truncated(), truncated()]):
                paused = board_research.run(self.config, 'test_pause_board', 'Research board')
            self.assertEqual(paused['status'], 'model_paused')
            with patch.object(DeepSeekClient, 'complete', side_effect=[
                    {'action': 'checkpoint', 'summary': 'no source yet', 'next_step': 'ask version'},
                    {'action': 'clarify', 'message': 'Which revision?'}]):
                waiting = board_research.run(self.config, 'test_pause_board', '', resume=paused['task_id'])
        self.assertEqual(waiting['status'], 'waiting_for_user')
        self.assertEqual(profile.read_bytes() if profile.exists() else None, before)

    def test_cli_budget_override_is_explicit_and_does_not_edit_config(self):
        import io
        before = (ROOT / 'config.json').read_bytes()
        paused = {'task_id': 'case', 'status': 'model_paused', 'model_pause': {'stage': 'review'}}
        with patch.object(workflow, 'verify_task', return_value=paused) as verify, patch('sys.stdout', new_callable=io.StringIO):
            code = cli.main(['verify', 'case', '--continue', '--effort', 'low', '--max-tokens', '40000'])
        self.assertEqual(code, 2)
        self.assertEqual(verify.call_args.args[1]['llm']['max_tokens'], 40000)
        self.assertEqual(verify.call_args.args[1]['llm']['reasoning_effort'], 'low')
        self.assertTrue(verify.call_args.args[3])
        self.assertEqual((ROOT / 'config.json').read_bytes(), before)


class VerificationResumeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.task = Path(temporary.name) / 'case'
        self.task.mkdir()
        self.config = workflow.load_config(ROOT / 'config.json')
        self.config.update(workspace_path=temporary.name, target='build', visualization=False, incremental=True, max_repairs=2)
        self.spec = {'summary': 'unit', 'requirements': ['unit'], 'acceptance': ['unit']}
        self.response = {'action': 'design', 'spec': self.spec,
                         'project': {'top': 'unit', 'testbench': 'tb', 'files': {'rtl/unit.sv': 'module unit; endmodule\n'}}}
        self.state = {'task_id': 'case', 'status': 'planned', 'request': 'unit', 'spec': self.spec, 'results': [],
                      'plan': {'visualization': False, 'development_tasks': []}}
        workflow.save(self.task, self.state)
        env = patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'test-only'})
        env.start()
        self.addCleanup(env.stop)
        for name, replacement in [('validate_design', lambda r, c: (r['spec'], r['project'])), ('prepare_project', self.materialize)]:
            mocked = patch.object(workflow, name, side_effect=replacement)
            mocked.start()
            self.addCleanup(mocked.stop)

    def materialize(self, task, config, spec, design):
        project = task / 'project'
        for name, text in design['files'].items():
            path = project / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding='utf-8')
        tools.write_json(project / 'project.json', {'top': 'unit'})
        return {**config, 'project_path': str(project)}

    def test_review_resume_does_not_repeat_tools_and_rejects_changed_source(self):
        simulation = {'stage': 'simulation', 'passed': True, 'run_dir': 'sim'}
        build = {'stage': 'build', 'passed': True, 'run_dir': 'build', 'artifact': {'bit': 'artifact'}}
        with patch.object(tools, 'run_simulation', return_value=simulation) as sim, patch.object(
                tools, 'run_build', return_value=build) as implement:
            with patch.object(DeepSeekClient, 'complete', side_effect=[truncated(), truncated()]):
                paused = workflow.develop(self.task, self.state, self.config, self.response)
            self.assertEqual(paused['status'], 'model_paused')
            self.assertEqual(paused['verification_checkpoint']['phase'], 'review')
            with patch.object(DeepSeekClient, 'complete', side_effect=[
                    {'action': 'checkpoint', 'summary': 'tool evidence retained', 'next_step': 'one review'}, REVIEW]):
                finished = workflow.verify_task('case', self.config, continue_existing=True)
            self.assertEqual(finished['status'], 'ready_to_program')
            self.assertEqual((sim.call_count, implement.call_count), (1, 1))
            self.assertNotIn('verification_checkpoint', finished)

    def test_source_change_invalidates_paused_review(self):
        with patch.object(tools, 'run_simulation', return_value={'stage': 'simulation', 'passed': True, 'run_dir': 'sim'}), patch.object(
                tools, 'run_build', return_value={'stage': 'build', 'passed': True, 'run_dir': 'build'}), patch.object(
                DeepSeekClient, 'complete', side_effect=[truncated(), truncated()]):
            workflow.develop(self.task, self.state, self.config, self.response)
        (self.task / 'project/rtl/unit.sv').write_text('changed', encoding='utf-8')
        with patch.object(DeepSeekClient, 'complete') as llm, self.assertRaisesRegex(RuntimeError, '源码已变化'):
            workflow.verify_task('case', self.config, continue_existing=True)
        llm.assert_not_called()

    def test_resume_does_not_expand_original_verify_repair_budget(self):
        self.config['max_repairs'] = 0
        with patch.object(tools, 'run_simulation', return_value={'stage': 'simulation', 'passed': True, 'run_dir': 'sim'}) as sim, patch.object(
                tools, 'run_build', return_value={'stage': 'build', 'passed': True, 'run_dir': 'build'}) as build:
            with patch.object(DeepSeekClient, 'complete', side_effect=[truncated(), truncated()]):
                workflow.develop(self.task, self.state, self.config, self.response)
            self.config['max_repairs'] = 10
            rejected = {**REVIEW, 'approved': False, 'blocking_findings': ['Missing required evidence']}
            with patch.object(DeepSeekClient, 'complete', side_effect=[
                    {'action': 'checkpoint', 'summary': 'evidence retained', 'next_step': 'one decision'}, rejected]):
                result = workflow.verify_task('case', self.config, continue_existing=True)
        self.assertEqual(result['status'], 'verification_failed')
        self.assertEqual((sim.call_count, build.call_count), (1, 1))

    def test_repair_resume_uses_same_failure_and_staged_edit(self):
        self.config['target'] = 'sim'
        actions = [{'action': 'checkpoint', 'summary': 'fix staged', 'next_step': 'deliver', 'edits': [
            {'path': 'rtl/unit.sv', 'old': 'module unit; endmodule\n', 'new': 'module unit; endmodule\n// fixed\n'}]},
            truncated(), truncated()]
        with patch.object(tools, 'run_simulation', side_effect=[
                {'stage': 'simulation', 'passed': False, 'run_dir': 'first_failure', 'diagnostic': 'rtl/unit.sv failed'},
                {'stage': 'simulation', 'passed': True, 'run_dir': 'second_run'}]) as sim:
            with patch.object(DeepSeekClient, 'complete', side_effect=actions):
                paused = workflow.develop(self.task, self.state, self.config, self.response)
            self.assertEqual(paused['verification_checkpoint']['phase'], 'repair')
            with patch.object(DeepSeekClient, 'complete', side_effect=[
                    {'action': 'checkpoint', 'summary': 'fix retained', 'next_step': 'deliver'}, {'action': 'deliver'}, REVIEW]):
                finished = workflow.verify_task('case', self.config, continue_existing=True)
        self.assertEqual(finished['status'], 'simulation_verified')
        self.assertEqual(sim.call_count, 2)
        self.assertTrue((self.task / 'project/rtl/unit.sv').read_text().endswith('// fixed\n'))


if __name__ == '__main__':
    unittest.main()

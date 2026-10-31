import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fpga_agent import development, python_tool, session, tools, workflow
from fpga_agent.agent import DeepSeekClient
from fpga_agent.confirmation import digest

ROOT = Path(__file__).resolve().parent.parent
RTL = '`timescale 1ns/1ps\nmodule unit; endmodule\n'
TB = '`timescale 1ns/1ps\nmodule tb_unit(output logic done=0); initial #10 done=1; endmodule\n'


class DevelopmentQuestionsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.task = Path(temp.name) / 'case'
        self.task.mkdir()
        self.config = workflow.load_config(ROOT / 'config.json')
        self.config.update(workspace_path=temp.name, target='sim', program_default=False)
        env = patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'test-only'})
        env.start()
        self.addCleanup(env.stop)
        self.spec = {'summary': 'Full two-file project', 'requirements': ['RTL and testbench'], 'acceptance': ['Simulation passes']}
        self.analysis = {'action': 'requirements', 'spec': self.spec, 'target': 'sim', 'program': False, 'visualization': False}
        self.plan = {**self.analysis, 'action': 'plan', 'design': 'Two sequential file tasks', 'test_plan': 'Assert completion',
                     'project': {'top': 'unit', 'testbench': 'tb_unit', 'simulation_timeout_ns': 1000, 'bindings': {}, 'debug': None},
                     'development_tasks': [
                         {'id': 'readme', 'kind': 'files', 'goal': 'Deliver README', 'completion': 'Document ready',
                          'inputs': [], 'outputs': ['README.md'], 'depends_on': []},
                         {'id': 'rtl', 'kind': 'files', 'goal': 'Deliver RTL', 'completion': 'RTL ready',
                          'inputs': [], 'outputs': ['rtl/unit.sv'], 'depends_on': []},
                         {'id': 'tb', 'kind': 'files', 'goal': 'Deliver testbench', 'completion': 'TB ready',
                          'inputs': ['rtl/unit.sv'], 'outputs': ['sim/tb_unit.sv'], 'depends_on': ['rtl']}]}
        approval = {'requirements': self.analysis, 'design': self.plan['design'], 'test_plan': self.plan['test_plan'],
                    'project': self.plan['project'], 'development_tasks': self.plan['development_tasks']}
        self.state = {'task_id': 'case', 'request': 'Build the full project', 'status': 'planned', 'results': [],
                      'conversation': [], 'project_format': 2, 'spec': self.spec, 'requirements_analysis': self.analysis,
                      'plan': self.plan, 'target': 'sim', 'execution_plan_digest': digest(approval),
                      'confirmations': {'requirements': {'digest': digest(self.analysis)}, 'test_plan': {'digest': digest(approval)}}}
        workflow.save(self.task, self.state)
        tools.write_json(self.task / 'draft.json', {'spec': self.spec,
                         'project': {**self.plan['project'], 'files': {'README.md': 'Project usage'}},
                         'deliveries': {'readme': {'status': 'delivered', 'outputs': ['README.md']}}})

    def run_session(self, actions, ask=None, model=None):
        def verified(task, state, config, response):
            state['status'] = 'simulation_verified'
            workflow.save(task, state)
            return state

        with patch.object(DeepSeekClient, 'complete', side_effect=actions) as llm, patch.object(
                session, '_model', model or Mock(side_effect=AssertionError('must not replan'))) as planner, patch.object(
                workflow, 'develop', side_effect=verified):
            result = session.run('', self.config, ask=ask, task_id='case', continue_existing=True)
        return result, llm, planner

    def stage_and_question(self):
        return [{'action': 'checkpoint', 'summary': 'RTL staged', 'next_step': 'Create the later testbench',
                 'edits': [{'path': 'rtl/unit.sv', 'old': '', 'new': RTL}]},
                {'action': 'clarify', 'message': 'Expand the write scope?'}]

    def assert_preserved(self, state):
        self.assertEqual(state['spec'], self.spec)
        self.assertEqual(state['plan'], self.plan)
        self.assertEqual(state['request'], self.state['request'])
        self.assertEqual(state['confirmations'], self.state['confirmations'])
        draft = json.loads((self.task / 'draft.json').read_text(encoding='utf-8'))
        self.assertEqual(draft['project']['files'], {'README.md': 'Project usage', 'rtl/unit.sv': RTL, 'sim/tb_unit.sv': TB})
        self.assertEqual(len(list((self.task / 'editing').glob('*.json'))), 2)
        self.assertNotIn('development_question', state)
        self.assertFalse((self.task / 'active.lock').exists())

    def test_out_of_scope_feedback_then_question_returns_to_same_editor(self):
        actions = self.stage_and_question()
        actions.insert(1, {'action': 'checkpoint', 'summary': 'Wrong next task', 'next_step': 'Write TB',
                           'edits': [{'path': 'sim/tb_unit.sv', 'old': '', 'new': TB}]})
        actions += [{'action': 'deliver'}, {'action': 'deliver', 'files': {'sim/tb_unit.sv': TB}}]
        state, llm, planner = self.run_session(actions, ask=Mock(return_value='Keep scope and deliver staged RTL'))
        planner.assert_not_called()
        feedback = json.loads(llm.call_args_list[2].args[0][1]['content'])['read_error']
        self.assertEqual(feedback['allowed_outputs'], ['rtl/unit.sv'])
        self.assertEqual(feedback['staged_files'], ['rtl/unit.sv'])
        resumed = json.loads(llm.call_args_list[3].args[0][1]['content'])
        self.assertEqual(resumed['user_reply']['answer'], 'Keep scope and deliver staged RTL')
        self.assertEqual(resumed['staged_files'], ['rtl/unit.sv'])
        self.assertEqual(resumed['full_task']['id'], 'rtl')
        next_task = json.loads(llm.call_args_list[4].args[0][1]['content'])
        self.assertEqual(next_task['allowed_outputs'], ['sim/tb_unit.sv'])
        self.assert_preserved(state)

    def check_paused_resume(self, legacy=False):
        waiting, _, _ = self.run_session(self.stage_and_question())
        self.assertEqual(waiting['status'], 'waiting_for_user')
        key = waiting['development_question']['editing_session']
        if legacy:
            waiting.pop('development_question')
            tools.write_json(self.task / 'state.json', waiting)
        state, llm, planner = self.run_session([
            {'action': 'deliver'}, {'action': 'deliver', 'files': {'sim/tb_unit.sv': TB}}],
            ask=Mock(return_value='Proceed within current task'))
        planner.assert_not_called()
        payload = json.loads(llm.call_args_list[0].args[0][1]['content'])
        self.assertEqual(payload['staged_files'], ['rtl/unit.sv'])
        checkpoint = json.loads((self.task / 'editing' / (key + '.json')).read_text(encoding='utf-8'))
        self.assertIn('result', checkpoint)
        self.assert_preserved(state)

    def test_paused_question_survives_restart(self):
        self.check_paused_resume()

    def test_legacy_paused_question_resumes_without_replanning(self):
        self.check_paused_resume(legacy=True)

    def test_requirements_question_keeps_existing_replanning_behavior(self):
        state = copy.deepcopy(self.state)
        state.update(status='waiting_for_user', question='Clarify overall requirement', current_work_item=None)
        tools.write_json(self.task / 'state.json', state)
        planner = Mock(side_effect=[self.analysis, self.plan])
        answers = Mock(side_effect=['Overall clarification', 'y', 'y'])
        result, _, planner = self.run_session([
            {'action': 'deliver', 'files': {'rtl/unit.sv': RTL}},
            {'action': 'deliver', 'files': {'sim/tb_unit.sv': TB}}], ask=answers, model=planner)
        self.assertEqual(result['status'], 'simulation_verified')
        self.assertEqual(planner.call_count, 2)
        self.assertEqual(planner.call_args_list[0].args[3], 'Overall clarification')

    def test_explicit_requirement_update_clears_old_development_question(self):
        state = copy.deepcopy(self.state)
        state.update(status='waiting_for_user', question='Old editor question', current_work_item='rtl',
                     work_items={'rtl': {'status': 'paused'}}, development_question={'work_item': 'rtl'},
                     development_reply={'answer': 'old'})
        tools.write_json(self.task / 'state.json', state)
        with patch.object(session, '_model', return_value={'action': 'clarify', 'message': 'Clarify new requirement'}):
            waiting = session.run('Change the overall requirement', self.config, task_id='case')
        self.assertEqual(waiting['status'], 'waiting_for_user')
        self.assertIsNone(waiting['current_work_item'])
        self.assertNotIn('development_question', waiting)
        self.assertNotIn('development_reply', waiting)
        self.assertFalse(session._development_question(waiting))

    def test_preparation_question_reuses_failed_run_and_editor(self):
        script = 'scripts/coeff.py'
        self.state['plan']['development_tasks'] = [
            {'id': 'generator', 'kind': 'files', 'goal': 'Generator', 'completion': 'Script ready',
             'inputs': [], 'outputs': [script]},
            {'id': 'data', 'kind': 'python', 'goal': 'Generate data', 'completion': 'Data ready',
             'inputs': [script], 'outputs': ['data/value.txt'], 'script': script},
            *self.plan['development_tasks'][1:]]
        tools.write_json(self.task / 'draft.json', {'spec': self.spec,
                         'project': {**self.plan['project'], 'files': {script: "raise ValueError('need clarification')"}},
                         'deliveries': {'generator': {'status': 'delivered', 'outputs': [script]}}})
        workflow.save(self.task, self.state)
        with patch.object(python_tool, 'prepare', wraps=python_tool.prepare) as prepare:
            with patch.object(DeepSeekClient, 'complete', return_value={'action': 'clarify', 'message': 'Which data value?'}):
                self.assertIsNone(development.build_project(self.task, self.state, self.config))
            key = self.state['development_question']['editing_session']
            session._answer_development(self.task, self.state, Mock(return_value='Use 42'))
            fixed = "from pathlib import Path\nPath('data/value.txt').write_text('42')\n"
            with patch.object(DeepSeekClient, 'complete', side_effect=[
                {'action': 'deliver', 'files': {script: fixed}},
                {'action': 'deliver', 'files': {'rtl/unit.sv': RTL}},
                {'action': 'deliver', 'files': {'sim/tb_unit.sv': TB}}]) as llm:
                result = development.build_project(self.task, self.state, self.config)
        self.assertEqual(prepare.call_count, 2)  # One original failure, then execute the fixed script.
        self.assertEqual(result['project']['files']['data/value.txt'], '42')
        payload = json.loads(llm.call_args_list[0].args[0][1]['content'])
        self.assertEqual(payload['user_reply']['answer'], 'Use 42')
        self.assertIn('result', json.loads((self.task / 'editing' / (key + '.json')).read_text(encoding='utf-8')))
        self.assertNotIn('development_question', self.state)


if __name__ == '__main__':
    unittest.main()

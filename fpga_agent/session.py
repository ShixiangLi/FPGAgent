"""Document-driven planning and interactive development entry point."""
import json
import uuid
from datetime import datetime
from pathlib import Path

from . import contract, model_steps, tools, workflow
from . import task_memory as memory
from .agent import project_context
from .confirmation import confirm, digest
from .development import build_project
from .interaction import read_reply
from .project import board_profile
from .prompts.catalog import (
    ANALYST,
    PLAN_CONTRACT,
    PLAN_REPAIR,
    PLANNER,
    RESEARCH_ACCESS,
)


def _model(task, state, config, request, system, confirmed=None, proposed=None, errors=None):
    from .research import READ_ACTIONS, read_tool, tool_context
    request = request or state.get('latest_user_input', state['request'])
    plan = state.get('plan') or {}
    payload = {"current_documents": {'requirements': state.get('requirements_analysis'),
                                    'design': plan.get('design'), 'test_plan': plan.get('test_plan'),
                                    'project': plan.get('project')}, "latest_user_input": request,
               "confirmed_requirements": confirmed, "execution_contract": contract.describe(board_profile(config)),
               "proposed_plan": proposed, "validation_errors": errors,
               "defaults": {"target": config.get("target"), "program": config.get("program_default", False)}}
    payload['research'] = tool_context(task, config)

    def handle(response):
        if response.get('action') not in READ_ACTIONS:
            return False
        read_tool(task, config, response)
        return {'research': tool_context(task, config)}

    def validate(response):
        if confirmed is not None:
            errors = contract.plan_issues(response, confirmed, board_profile(config))
            state['plan_contract_errors'] = errors
            folder = task / 'planning'
            folder.mkdir(exist_ok=True)
            tools.write_json(folder / (uuid.uuid4().hex + '.json'), {'plan': response, 'errors': errors})
            if errors:
                memory.event(task, '计划接口反馈', json.dumps(errors, ensure_ascii=False))
            return errors
        try:
            if response.get('action') != 'requirements':
                return ['Return a requirements result or clarify']
            contract.validate_requirements(response)
            return []
        except ValueError as exc:
            return [str(exc)]

    stage = 'planning' if confirmed is not None else 'requirements'
    state['model_phase'] = stage
    workflow.save(task, state)
    files = {p.name: p for p in task.glob('*.md')}
    result = model_steps.run(task, config, stage,
                           {'request': state['request'], 'input': request, 'confirmed': confirmed,
                            'proposed': proposed, 'errors': errors},
                           system + PLAN_CONTRACT + RESEARCH_ACCESS + project_context(config), payload,
                           files=files, validate=validate, handle_action=handle)
    if state.get('model_pause', {}).get('stage') == stage:
        state.pop('model_pause', None)
    state.pop('model_phase', None)
    return result


def _answer(task, state, ask):
    state["status"] = "waiting_for_user"
    workflow.save(task, state)
    memory.event(task, "提问", state["question"])
    return read_reply(task, ask, "请回答")


def _development_question(state):
    current = state.get("current_work_item")
    # Older checkpoints predate the explicit question origin.
    return bool(state.get('verification_checkpoint') or state.get("development_question") or (
        current and state.get("work_items", {}).get(current, {}).get("status") == "paused"))


def _answer_development(task, state, ask):
    question = state["question"]
    reply = _answer(task, state, ask)
    if not reply:
        return False
    state.update(development_reply={"question": question, "answer": reply}, question=None, status="developing")
    with (task / "conversation.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"role": "user", "stage": "development", "work_item": state.get("current_work_item"),
                            "content": reply}, ensure_ascii=False) + "\n")
    workflow.save(task, state)
    return True


def _build_with_questions(task, state, config, ask):
    while True:
        response = build_project(task, state, config)
        if response is not None or state["status"] != "waiting_for_user":
            return response
        if not _answer_development(task, state, ask):
            return None


def _executable_plan(task, state, config, request, analysis, plan):
    """Validate old plans; new drafts are checked by the resumable model loop."""
    tools.write_json(task / "execution_contract.json", contract.describe(board_profile(config)))
    errors = contract.plan_issues(plan, analysis, board_profile(config)) if plan else []
    state['plan_contract_errors'] = errors
    if plan is None or errors:
        plan = _model(task, state, config, request, PLANNER + (PLAN_REPAIR if errors else ''), analysis, plan, errors or None)
    if plan.get('action') != 'clarify':
        tools.write_json(task / 'plan_response.json', plan)
    return plan


def run(request, config, ask=None, task_id=None, continue_existing=False, sources=()):
    task = workflow.task_path(config, task_id) if task_id else Path(config["workspace_path"]) / (
        datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6])
    task.mkdir(exist_ok=True)
    state = json.loads((task / "state.json").read_text(encoding="utf-8")) if task_id else {
        "task_id": task.name, "request": request, "status": "planning", "results": [], "conversation": [], "project_format": 2}
    # Board selection belongs to the task and survives a CLI resume.
    config = {**config, "board": state.setdefault("board_id", config["board"])}
    checks = workflow.environment_checks(config)
    if not all(checks[k] for k in ("vivado", "board_profile", "templates")):
        raise ValueError("Local environment is incomplete; run doctor.")
    if continue_existing and not task_id:
        raise ValueError("--continue requires an existing task.")
    if continue_existing and state["status"] in {"completed", "awaiting_acceptance"}:
        return state
    resuming_question = continue_existing and state["status"] == "waiting_for_user"
    analysis = state.get("requirements_analysis") if continue_existing else None
    plan = state.get("plan") if continue_existing else None
    if continue_existing and (state.get('model_pause', {}).get('stage') == 'requirements' or state.get('model_phase') == 'requirements'):
        analysis, plan = None, None
    # Existing incremental tasks retain their draft, but now require explicit confirmation.
    if analysis is None and plan is not None:
        analysis = {k: plan[k] for k in ("spec", "target", "program", "visualization")}
    if continue_existing and analysis is None and not resuming_question and not state.get('model_pause') and not state.get('model_phase'):
        raise ValueError("No saved requirements; resume with --request to plan again.")
    with tools.lock(task / "active.lock"):
        try:
            state["research_sources"] = list(dict.fromkeys(state.get("research_sources", []) + [str(s) for s in sources]))
            if resuming_question:
                if _development_question(state):
                    if not _answer_development(task, state, ask):
                        return state
                    request = ""
                else:
                    request = _answer(task, state, ask)
                    if not request:
                        return state
                    analysis, plan, continue_existing = None, None, False
            if task_id and request:
                state["request"] += "\nUser update: " + request
                state.pop("development_question", None)
                state.pop("development_reply", None)
                state["current_work_item"] = None
                state.pop('model_pause', None)
                state.pop('verification_checkpoint', None)
                state.pop('model_phase', None)
            state.pop("error", None)
            for key in ("simulation_hash", "build_hash", "artifact", "acceptance"):
                state.pop(key, None)
            while True:
                if request:
                    state["latest_user_input"] = request
                    with (task / "conversation.jsonl").open("a", encoding="utf-8") as f:
                        f.write(json.dumps({"role": "user", "content": request}, ensure_ascii=False) + "\n")
                if analysis is None:
                    state["status"] = "analyzing_requirements"
                    workflow.save(task, state)
                    analysis = _model(task, state, config, request, ANALYST)
                    state.pop("requirements_analysis", None)
                    state.pop("plan", None)
                    tools.write_json(task / "requirements_response.json", analysis)
                    if analysis.get("action") == "clarify":
                        state["question"] = analysis.get("message", "Please clarify requirements.")
                        request = _answer(task, state, ask)
                        if not request:
                            return state
                        analysis = None
                        continue
                    if analysis.get("action") != "requirements":
                        raise ValueError("Model did not return requirements analysis.")
                contract.validate_requirements(analysis)
                state.update(requirements_analysis=analysis, spec=analysis["spec"], question=None)
                decision, revision = confirm(task, state, "requirements", "需求分析", analysis, ask)
                if decision == "paused":
                    return state
                if decision == "revision":
                    request, analysis, plan = revision, None, None
                    continue
                if plan is None:
                    state["status"] = "planning"
                    workflow.save(task, state)
                # Old tasks can present their existing design and verification approach for approval.
                if plan and (not isinstance(plan.get("test_plan"), str) or not plan["test_plan"].strip()):
                    if continue_existing:
                        plan = {**plan, "test_plan": plan["design"]}
                plan = _executable_plan(task, state, config, request, analysis, plan)
                if plan.get("action") == "clarify":
                    state["question"] = plan.get("message", "Please clarify the plan.")
                    request = _answer(task, state, ask)
                    if not request:
                        return state
                    analysis, plan = None, None
                    continue
                state.update(plan=plan, target=plan["target"], question=None)
                approval_content = {"requirements": analysis, "design": plan["design"], "test_plan": plan["test_plan"],
                                    "project": plan.get("project"), "development_tasks": plan.get("development_tasks")}
                decision, revision = confirm(task, state, "test_plan", "设计与测试方案", approval_content, ask)
                if decision == "paused":
                    return state
                if decision == "revision":
                    request, analysis, plan = revision, None, None
                    continue
                execution_digest = digest(approval_content)
                old_digest = state.get("execution_plan_digest")
                reuse_draft = old_digest == execution_digest or (continue_existing and old_digest is None)
                if not reuse_draft:
                    if (task / "draft.json").exists():
                        history = task / "history"
                        history.mkdir(exist_ok=True)
                        (task / "draft.json").replace(history / (uuid.uuid4().hex + "-draft.json"))
                    state["work_items"] = {}
                state.update(execution_plan_digest=execution_digest, status="planned", pending_confirmation=None)
                workflow.save(task, state)
                runtime = {**config, "target": plan["target"], "task_memory_path": str(task),
                           "visualization": plan["visualization"], "incremental": True}
                pending = state.get('verification_checkpoint')
                response = (json.loads((task / pending['candidate']).read_text(encoding='utf-8')) if pending
                            else _build_with_questions(task, state, runtime, ask))
                if response is None:
                    return state
                tools.write_json(task / "design_response.json", response)
                state = workflow.develop(task, state, runtime, response)
                if state["status"] in {"simulation_verified", "ready_to_program"}:
                    for entry in state.get("work_items", {}).values():
                        entry["validation"] = "integrated simulation and review passed"
                    workflow.save(task, state)
                if state["status"] == "waiting_for_user":
                    if state.get('verification_checkpoint'):
                        if _answer_development(task, state, ask):
                            request = ''
                            continue
                    else:
                        request = _answer(task, state, ask)
                        if request:
                            analysis, plan = None, None
                            continue
                break
            if state['status'] != 'model_paused':
                state.pop('model_pause', None)
        except model_steps.ModelPaused as exc:
            state.update(status='model_paused', model_pause=exc.details, error=exc.details['reason'])
            workflow.save(task, state)
            return state
        except BaseException as exc:
            state.update(status="interrupted" if isinstance(exc, KeyboardInterrupt) else "error", error=str(exc))
            workflow.save(task, state)
            raise
    if state["status"] == "ready_to_program" and state["plan"]["program"]:
        memory.event(task, "Programming", "Approved plan: probing, programming and optional VIO sampling")
        state = workflow.deploy(task.name, config)
    return state

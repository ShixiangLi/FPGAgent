"""Document-driven planning and interactive development entry point."""
import json
import uuid
from datetime import datetime
from pathlib import Path

from .agent import DeepSeekClient, project_context
from . import workflow, tools, task_memory as memory
from .development import build_project
from .confirmation import confirm, digest
from .interaction import read_reply

from .prompts.catalog import ANALYST, PLANNER


def _model(task, state, config, request, system, confirmed=None):
    payload = {"current_documents": memory.context(task), "latest_user_input": request,
               "confirmed_requirements": confirmed,
               "defaults": {"target": config.get("target"), "program": config.get("program_default", False)}}
    return DeepSeekClient(config).complete([
        {"role": "system", "content": system + project_context(config)},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}], task / "llm_calls.jsonl")


def _validate_requirements(analysis):
    spec = analysis.get("spec")
    if not isinstance(spec, dict) or not isinstance(spec.get("summary"), str):
        raise ValueError("Requirements must contain spec.summary.")
    for key in ("requirements", "acceptance"):
        if not isinstance(spec.get(key), list) or not spec[key] or any(not isinstance(x, str) or not x.strip() for x in spec[key]):
            raise ValueError(f"spec.{key} must contain nonempty strings.")
    if analysis.get("target") not in {"sim", "build"} or any(type(analysis.get(k)) is not bool for k in ("program", "visualization")):
        raise ValueError("Requirements must specify target, program and visualization.")
    if analysis["program"] and analysis["target"] != "build":
        raise ValueError("Programming requires a build target.")


def _answer(task, state, ask):
    state["status"] = "waiting_for_user"
    workflow.save(task, state)
    memory.event(task, "提问", state["question"])
    return read_reply(task, ask, "请回答")


def run(request, config, ask=None, task_id=None, continue_existing=False):
    checks = workflow.environment_checks(config)
    if not all(checks[k] for k in ("vivado", "board_profile", "templates")):
        raise ValueError("Local environment is incomplete; run doctor.")
    task = workflow.task_path(config, task_id) if task_id else Path(config["workspace_path"]) / (
        datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6])
    task.mkdir(exist_ok=True)
    state = json.loads((task / "state.json").read_text(encoding="utf-8")) if task_id else {
        "task_id": task.name, "request": request, "status": "planning", "results": [], "conversation": [], "project_format": 2}
    if continue_existing and not task_id:
        raise ValueError("--continue requires an existing task.")
    if continue_existing and state["status"] in {"completed", "awaiting_acceptance"}:
        return state
    resuming_question = continue_existing and state["status"] == "waiting_for_user"
    analysis = state.get("requirements_analysis") if continue_existing else None
    plan = state.get("plan") if continue_existing else None
    # Existing incremental tasks retain their draft, but now require explicit confirmation.
    if analysis is None and plan is not None:
        analysis = {k: plan[k] for k in ("spec", "target", "program", "visualization")}
    if continue_existing and analysis is None and not resuming_question:
        raise ValueError("No saved requirements; resume with --request to plan again.")
    with tools.lock(task / "active.lock"):
        try:
            if resuming_question:
                request = _answer(task, state, ask)
                if not request:
                    return state
                analysis, plan, continue_existing = None, None, False
            if task_id and request:
                state["request"] += "\nUser update: " + request
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
                _validate_requirements(analysis)
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
                    plan = _model(task, state, config, request, PLANNER, analysis)
                    tools.write_json(task / "plan_response.json", plan)
                    if plan.get("action") == "clarify":
                        state["question"] = plan.get("message", "Please clarify the plan.")
                        request = _answer(task, state, ask)
                        if not request:
                            return state
                        analysis, plan = None, None
                        continue
                if plan.get("action") != "plan" or not isinstance(plan.get("design"), str):
                    raise ValueError("Model did not return a valid design plan.")
                if any(plan.get(k) != analysis[k] for k in ("spec", "target", "program", "visualization")):
                    raise ValueError("Plan changed confirmed requirements; revise the requirements first.")
                # Old tasks can present their existing design and verification approach for approval.
                if not isinstance(plan.get("test_plan"), str) or not plan["test_plan"].strip():
                    if continue_existing:
                        plan = {**plan, "test_plan": plan["design"]}
                    else:
                        raise ValueError("Plan must include a concrete test_plan.")
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
                response = build_project(task, state, runtime)
                if response is None:
                    if state["status"] == "waiting_for_user":
                        request = _answer(task, state, ask)
                        if request:
                            analysis, plan = None, None
                            continue
                    return state
                tools.write_json(task / "design_response.json", response)
                state = workflow.develop(task, state, runtime, response)
                if state["status"] in {"simulation_verified", "ready_to_program"}:
                    for entry in state.get("work_items", {}).values():
                        entry["validation"] = "integrated simulation and review passed"
                    workflow.save(task, state)
                if state["status"] == "waiting_for_user":
                    request = _answer(task, state, ask)
                    if request:
                        analysis, plan = None, None
                        continue
                break
        except BaseException as exc:
            state.update(status="interrupted" if isinstance(exc, KeyboardInterrupt) else "error", error=str(exc))
            workflow.save(task, state)
            raise
    if state["status"] == "ready_to_program" and state["plan"]["program"]:
        memory.event(task, "Programming", "Approved plan: probing, programming and optional VIO sampling")
        state = workflow.deploy(task.name, config)
    return state

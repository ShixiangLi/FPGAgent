"""Sequential, checkpointed file delivery. No parallel scheduler or plugin system."""
import hashlib
import json
from pathlib import Path

from . import model_steps, tools
from . import task_memory as memory
from .agent import DeepSeekClient, GenerationTruncated, project_context
from .contract import describe, file_path, validate_tasks
from .project import board_profile
from .prompts.catalog import (
    EDIT_BOUNDARY_FEEDBACK,
    EDIT_RECOVERY_TASK,
    EDITOR,
    INCREMENTAL_REPAIR_GOAL,
    READ_ALREADY_PROVIDED,
    READ_UNAVAILABLE,
    read_instruction,
)


def _signature(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _check_files(changes, allowed, read_only):
    if not isinstance(changes, dict):
        raise ValueError("files必须为路径到文件内容的对象。")
    for name, text in changes.items():
        file_path(name)
        if name in read_only or (allowed is not None and name not in allowed):
            raise ValueError(f"文件不在当前任务可写范围内：{name}")
        if not isinstance(text, str) or not text.strip() or len(text) > 160000:
            raise ValueError(f"文件内容无效或过大：{name}")


def _stage_edits(edits, readable):
    if not isinstance(edits, list):
        raise ValueError("edits必须为path/old/new对象列表。")
    changes = {}
    for edit in edits:
        if not isinstance(edit, dict):
            raise ValueError("每个edit必须是path/old/new对象。")
        name, old, new = edit.get("path"), edit.get("old"), edit.get("new")
        if not isinstance(name, str) or not isinstance(old, str) or not isinstance(new, str):
            raise ValueError("edit.path/old/new必须为字符串。")
        text = changes.get(name, readable.get(name))
        if text is None and old == "":
            changes[name] = new
        elif text is not None and old and text.count(old) == 1:
            changes[name] = text.replace(old, new, 1)
        else:
            raise ValueError(f"{name}: old必须唯一匹配现有文本；old为空只能创建新文件。")
    return changes


def _views(work, readable):
    return {name: readable[name][v["offset"]:v["offset"]+v["limit"]] for name, v in work["selected"].items()}


def _compact(work, task, reason, readable=None, clear_reads=False):
    work["recent"] = work["recent"][-2:]
    if clear_reads:
        work["selected"] = {}
    else:
        while sum(len(text) for text in _views(work, readable).values()) > 48000:
            del work["selected"][next(iter(work["selected"]))]
    work["compactions"] += 1
    memory.event(task, "上下文压缩", reason + "；保留摘要、下一步和全部工作文件，按需重新读取。")


def _save_work(task, path, work, goal):
    tools.write_json(path, work)
    memory.document(task, "working.md", "# 当前开发检查点\n\n" + json.dumps({
        "session": str(path.relative_to(task)), "task": goal, "summary": work["summary"],
        "next_step": work["next_step"], "staged_files": list(work["staged"]),
        "compactions": work["compactions"], "recovery": work["recovery"], "feedback": work["feedback"],
        "delivered": "result" in work}, ensure_ascii=False, indent=2) + "\n")


def _edit_payload(work, goal, documents, readable, allowed, read_only, last_turn):
    contents = _views(work, readable)
    return {"current_documents": documents, "full_task": goal,
        "task": EDIT_RECOVERY_TASK if work["recovery"] is not None else work["next_step"] or goal,
        "summary": work["summary"], "recovery": work["recovery"],
        "recent_steps": work["recent"], "staged_files": list(work["staged"]),
        "file_index": list(readable), "read_only_files": list(read_only), "read_error": work["feedback"],
        "related_files": contents, "file_views": {name: {**v, "total_chars": len(readable[name]),
            "next_offset": v["offset"]+len(contents[name]) if v["offset"]+len(contents[name]) < len(readable[name]) else None}
            for name, v in work["selected"].items()}, "allowed_outputs": allowed,
        "read_instruction": read_instruction(last_turn=last_turn)}


def _apply_action(task, work, response, readable, allowed, read_only):
    """Apply one complete action atomically; recovery is independent of tool feedback."""
    action = response.get("action")
    if work["recovery"] is not None and (
            action in {"deliver", "verify"} or (action == "checkpoint" and (response.get("edits") or response.get("files")))):
        raise ValueError("recovery待完成：先提交不含代码修改的checkpoint，明确一个更小next_step，再继续实现或交付。")
    feedback = None
    if action == "read_files":
        names = response.get("paths")
        offset, limit = response.get("offset", 0), response.get("limit", 8000)
        if (not isinstance(names, list) or not 1 <= len(names) <= 6 or any(not isinstance(n, str) for n in names)
                or type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 8000):
            raise ValueError("read_files需要1..6个paths；offset>=0，limit为1..8000字符。")
        missing = [name for name in names if name not in readable]
        feedback = {"unavailable_paths": missing, "message": READ_UNAVAILABLE} if missing else None
        previous = dict(work["selected"])
        for name in names:
            if name in readable:
                start, end = offset, offset + limit
                old = work["selected"].pop(name, None)
                if old and start <= old["offset"] + old["limit"] and old["offset"] <= end:
                    merged_start, merged_end = min(start, old["offset"]), max(end, old["offset"] + old["limit"])
                    if merged_end - merged_start <= 48000:
                        start, end = merged_start, merged_end
                work["selected"][name] = {"offset": start, "limit": end - start}
        if not missing and previous == work["selected"]:
            feedback = {"message": READ_ALREADY_PROVIDED}
        memory.event(task, "读取反馈", f"available={[n for n in names if n not in missing]}, unavailable={missing}; offset={offset} limit={limit}")
    elif action == "checkpoint":
        if any(not isinstance(response.get(k), str) or not response[k].strip() for k in ("summary", "next_step")):
            raise ValueError("checkpoint需要累计summary和一个具体next_step。")
        if len(response["summary"]) > 8000 or len(response["next_step"]) > 2000:
            raise ValueError("summary最多8000字符，next_step最多2000字符，请保留决策和来源路径并精简重复描述。")
        changes = _stage_edits(response.get("edits", []), readable)
        if len(changes) > 3:
            raise ValueError("每个动作最多修改3个文件，请分步checkpoint暂存。")
        staged = {**work["staged"], **changes}
        _check_files(staged, allowed, read_only)
        work.update(staged=staged, summary=response["summary"], next_step=response["next_step"], recovery=None)
        if response.get("compact") is True:
            _compact(work, task, "模型已更新累计摘要，主动释放读取上下文", clear_reads=True)
        memory.event(task, "开发小步", "下一步：" + work["next_step"] + "；暂存：" + ", ".join(changes))
    elif action in {"deliver", "verify"}:
        changes = response.get("files", {})
        if not isinstance(changes, dict):
            raise ValueError("deliver.files必须为路径到文件内容的对象。")
        if len(changes) > 3:
            raise ValueError("每个动作最多修改3个文件，请分步checkpoint暂存。")
        changes = {**work["staged"], **changes}
        _check_files(changes, allowed, read_only)
        if (not changes and action != "verify") or (allowed is not None and set(changes) != set(allowed)):
            raise ValueError("最终交付必须包含当前任务的所有输出文件。")
        work["result"] = {**response, "action": "deliver", "files": changes}
        return work["result"]
    elif action == "clarify":
        return response
    else:
        raise ValueError("开发动作只能为read_files/checkpoint/deliver/verify/clarify。")
    work["feedback"] = feedback
    return None


def edit(task, config, goal, files, initial, allowed=None, read_only=None, evidence=None, user_reply=None, execute=None):
    """Small durable actions; compact views, never source files or confirmed requirements."""
    read_only = read_only or {}
    documents = memory.work_context(task)
    key = _signature({"goal": goal, "files": files, "allowed": allowed, "read_only": read_only,
                      "documents": documents, "project_context": project_context(config)})[:20]
    read_only = {**read_only, **(evidence or {})}
    folder = task / "editing"
    folder.mkdir(exist_ok=True)
    path = folder / (key + ".json")
    work = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {
        "selected": {name: {"offset": 0, "limit": 8000} for name in initial}, "staged": {},
        "summary": "", "next_step": "先用短checkpoint明确当前任务边界、接口与接下来一个小步骤；本轮不推演完整实现。", "recent": [], "feedback": None, "compactions": 0, "stalled": 0,
        "recovery": None}
    if "result" in work:
        return work["result"]
    work.setdefault('tool_results', [])
    if user_reply is not None:
        work.update(user_reply=user_reply, stalled=0, feedback=None)
        work.pop("question", None)
    # Migrate existing paused checkpoints without changing their identity or draft files.
    if "recovery" not in work:
        work["recovery"] = (work["feedback"] or {}).get("generation_truncated")
        if work["recovery"] is not None:
            work["feedback"] = None
    truncations = 0
    progress_fields = ("selected", "staged", "summary", "next_step", "recovery", "tool_results")
    pause_reason = "本批开发已执行16轮"
    for turn in range(16):
        for result in work['tool_results']:
            read_only.update(read_evidence(task, result))
        readable = {**files, **read_only, **work["staged"]}
        if sum(len(text) for text in _views(work, readable).values()) > 60000 or len(work["recent"]) >= 6:
            _compact(work, task, "读取窗口或近期动作达到整理阈值", readable)
        _save_work(task, path, work, goal)
        payload = _edit_payload(work, goal, documents, readable, allowed, read_only, last_turn=turn == 15)
        payload.update(execution_contract=describe(board_profile(config)), user_reply=work.get("user_reply"))
        payload.update(available_tools=['execute'] if execute else [], tool_results=[
            model_steps.evidence_context(task, r, {}) for r in work['tool_results'][-3:]])
        try:
            response = DeepSeekClient(config).complete([
                {"role": "system", "content": EDITOR + project_context(config)},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}], task / "llm_calls.jsonl")
        except GenerationTruncated as exc:
            truncations += 1
            work.update(recovery=exc.details, feedback=None)
            work["stalled"] += 1
            _compact(work, task, "生成截断，从最近成功小步重新组织任务", clear_reads=True)
            _save_work(task, path, work, goal)
            if truncations >= 2:
                pause_reason = "本批两次生成截断，当前小步尚未交付"
                break
            continue
        before = _signature({k: work[k] for k in progress_fields})
        try:
            if response.get('action') == 'execute':
                if execute is None:
                    raise ValueError('execute is not available in this editing task; deliver files to the scheduler first')
                if work['recovery'] is not None:
                    raise ValueError('recovery待完成：先提交不含代码修改的短checkpoint，再执行工具。')
                if response.get('files') or response.get('edits'):
                    raise ValueError('execute does not apply edits; checkpoint changes before executing')
                execution = execute(response, {**files, **work['staged']})
                work['tool_results'].append(execution)
                work['feedback'] = {'execution': model_steps.evidence_context(task, execution, {})}
                result = None
            else:
                result = _apply_action(task, work, response, readable, allowed, read_only)
            if result is not None:
                if result["action"] == "clarify":
                    work["question"] = result.get("message", "请补充")
                    result = {**result, "editing_session": key}
                _save_work(task, path, work, goal)
                return result
        except ValueError as exc:
            work["feedback"] = {"error_type": type(exc).__name__, "error": str(exc)}
            if allowed is not None:
                work["feedback"].update(current_task=goal.get("id", goal.get("goal")),
                                        allowed_outputs=allowed, staged_files=list(work["staged"]),
                                        instruction=EDIT_BOUNDARY_FEEDBACK)
            memory.event(task, "小步接口反馈", str(exc))
        work["recent"].append({"action": response.get("action"), "message": response.get("message", ""),
                               "feedback": work["feedback"]})
        after = _signature({k: work[k] for k in progress_fields})
        work["stalled"] = work["stalled"] + 1 if before == after else 0
        _save_work(task, path, work, goal)
        if work["stalled"] >= 3:
            pause_reason = "连续3轮没有新增读取窗口、摘要或文件修改"
            break
    message = pause_reason + "，检查点已保存。用--resume <id> --continue续接；预算不足时可由用户调整--max-tokens或--effort。"
    return {"action": "pause", "message": message, "editing_session": key,
            "model_pause": model_steps.pause_details('editing', path, message, config, work['recovery'])}


def _defer(task, state, response, prepare_failure=None):
    if response.get("action") not in {"clarify", "pause"}:
        return False
    from .workflow import save
    waiting = response["action"] == "clarify"
    state.update(status="waiting_for_user" if waiting else "model_paused",
                 question=response.get("message", "请补充") if waiting else None,
                 message=response.get("message", ""))
    current = state.get("current_work_item")
    if waiting:
        state["development_question"] = {"work_item": current, "editing_session": response.get("editing_session"),
                                         "prepare_failure": prepare_failure}
    else:
        state.pop("development_question", None)
        state['model_pause'] = response.get('model_pause', {})
    if current in state.get("work_items", {}):
        state["work_items"][current]["status"] = "paused"
    save(task, state)
    memory.event(task, "等待回答" if waiting else "开发暂停", response.get("message", ""))
    return True


def build_project(task, state, config):
    from . import python_tool, workflow
    plan = state["plan"]
    items = validate_tasks(plan.get("development_tasks"))
    checkpoint = task / "draft.json"
    draft = json.loads(checkpoint.read_text(encoding="utf-8")) if checkpoint.exists() else {
        "spec": state["spec"], "project": {**plan["project"], "files": {}}, "deliveries": {}}
    if draft["spec"] != state["spec"]:
        raise ValueError("草稿规格不匹配，请重新规划。")
    files = draft["project"]["files"]
    state["work_items"] = state.get("work_items", {})
    for index, item in enumerate(items):
        saved = draft["deliveries"].get(item["id"])
        # Checkpoint owns file bodies; no model call is repeated for delivered work.
        if saved:
            if any(name not in files for name in item["outputs"]):
                raise ValueError("检查点缺少已交付文件，请重新规划。")
            state["work_items"][item["id"]] = saved
            continue
        state.update(status="developing", current_work_item=item["id"])
        state["work_items"][item["id"]] = {"status": "running", "goal": item["goal"]}
        workflow.save(task, state)
        memory.event(task, "开发任务", f"{index+1}/{len(items)} {item['id']}: {item['goal']}")
        reply = state.pop("development_reply", None)
        pending = state.get("development_question", {})
        if item.get("kind") == "python":
            failed = pending.get("prepare_failure") if pending.get("work_item") == item["id"] else None
            for attempt in range(config["max_repairs"] + 1):
                if failed is not None:
                    result, failed = failed, None
                else:
                    result = python_tool.prepare(task, config, files, item)
                    state["results"].append(result)
                    workflow.save(task, state)
                if result["passed"] or attempt == config["max_repairs"]:
                    break
                update = edit(task, config, {**item, "failure": result["diagnostic"]}, files,
                              item["inputs"], [item["script"]], user_reply=reply)
                reply = None
                if _defer(task, state, update, prepare_failure=result):
                    return None
                state.pop("development_question", None)
                files.update(update["files"])
                tools.write_json(checkpoint, draft)
            if not result["passed"]:
                state.update(status="verification_failed", error=result["diagnostic"])
                workflow.save(task, state)
                return None
            files.update(result["output_files"])
            evidence = {"status": "delivered", "goal": item["goal"], "evidence": result["run_dir"]}
        else:
            response = edit(task, config, item, files, item.get("inputs", []), item["outputs"], user_reply=reply)
            if _defer(task, state, response):
                return None
            state.pop("development_question", None)
            state.pop('model_pause', None)
            files.update(response["files"])
            evidence = {"status": "delivered", "goal": item["goal"], "message": response.get("message", ""),
                        "outputs": item["outputs"], "validation": "pending integrated verification"}
        draft["deliveries"][item["id"]] = evidence
        state["work_items"][item["id"]] = evidence
        tools.write_json(checkpoint, draft)
        workflow.save(task, state)
        memory.event(task, "交付", ", ".join(item["outputs"]) + "；集成验证尚未完成")
    state["current_work_item"] = None
    return {"action": "design", "message": plan["design"], "spec": state["spec"], "project": draft["project"]}


def read_evidence(task, result):
    evidence = {}
    for name in result.get("logs", []) + result.get("evidence_files", []):
        path = Path(name).resolve()
        if path.is_file() and path.is_relative_to(task.resolve()):
            evidence[path.relative_to(task.resolve()).as_posix()] = path.read_text(encoding="utf-8", errors="replace")
    return evidence


def repair(task, state, config, response, result):
    """Edit only relevant files; existing files and frozen specification are preserved."""
    files = response["project"]["files"]
    diagnostic = json.dumps({**{k: result[k] for k in ("stage", "passed", "exit_code", "command", "active_command", "error", "error_type", "logs", "evidence_files") if k in result},
        "errors": result.get("errors", [])[:4],
        "blocking_findings": result.get("blocking_findings", []),
        "observations": result.get("observations", {}),
        "log_excerpt": result.get("diagnostic", "")[-5000:]}, ensure_ascii=False)
    initial = [name for name in files if name in diagnostic][:3]
    harness = task / "project/sim/agent_sim.sv"
    read_only = {"sim/agent_sim.sv": harness.read_text(encoding="utf-8")} if harness.is_file() else {}

    def execute(request, sources):
        from . import python_tool, workflow
        memory.event(task, 'Python执行', f"{request.get('script')} --input {request.get('input')}")
        execution = python_tool.execute(task, config, sources, request)
        state['results'].append(execution)
        workflow.save(task, state)
        memory.event(task, '执行结果', f"exit_code={execution.get('exit_code')}, passed={execution['passed']}; {execution['run_dir']}")
        return execution

    update = edit(task, config, {"goal": INCREMENTAL_REPAIR_GOAL, "failure": diagnostic,
                   "stage": result["stage"]}, files, initial, read_only=read_only, evidence=read_evidence(task, result),
                  user_reply=state.pop('development_reply', None), execute=execute)
    if update["action"] in {"clarify", "pause"}:
        return update
    merged = {**response, "project": {**response["project"], "files": {**files, **update["files"]}}}
    memory.event(task, "增量修复", update.get("message", "") + "；修改：" + ", ".join(update["files"]))
    if merged['project'] == response['project']:
        return merged
    path = task / "draft.json"
    draft = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"deliveries": {}}
    draft.update(spec=merged["spec"], project=merged["project"])
    for record in state.get("work_items", {}).values():
        record["validation"] = "pending integrated verification"
    tools.write_json(path, draft)
    # Preprocessing data depend on scripts/RTL; recompute before next verification.
    for item in state["plan"]["development_tasks"]:
        if item.get("kind") == "python":
            from .python_tool import prepare
            draft["deliveries"].pop(item["id"], None)
            tools.write_json(path, draft)
            generated = prepare(task, config, merged["project"]["files"], item)
            state["results"].append(generated)
            if not generated["passed"]:
                raise ValueError(generated["diagnostic"])
            merged["project"]["files"].update(generated["output_files"])
            draft["deliveries"][item["id"]] = {"status": "delivered", "goal": item["goal"], "evidence": generated["run_dir"]}
            tools.write_json(path, draft)
    for item in state["plan"]["development_tasks"]:
        if set(item["outputs"]).intersection(update["files"]):
            record = draft["deliveries"].get(item["id"], {})
            record.update(message=update.get("message", ""), validation="pending integrated verification")
            state.setdefault("work_items", {})[item["id"]] = record
    tools.write_json(path, draft)
    return merged

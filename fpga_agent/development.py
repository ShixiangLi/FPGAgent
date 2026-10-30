"""Sequential, checkpointed file delivery. No parallel scheduler or plugin system."""
import json

from .agent import DeepSeekClient, project_context
from .contract import file_path, validate_tasks
from . import task_memory as memory, tools

from .prompts.catalog import (
    EDITOR, READ_UNAVAILABLE, READ_ALREADY_PROVIDED, INCREMENTAL_REPAIR_GOAL,
    read_instruction,
)



def edit(task, config, goal, files, initial, allowed=None, read_only=None):
    """A bounded read/edit loop. File contents are supplied only on explicit selection."""
    selected = list(dict.fromkeys(initial))
    readable = {**files, **(read_only or {})}
    read_error = None
    for turn in range(6):
        contents = {name: readable[name] for name in selected}
        if sum(len(text) for text in contents.values()) > 100000:
            raise ValueError("本轮读取文件过大，请拆分开发任务。")
        payload = {"current_documents": memory.context(task), "task": goal,
                   "file_index": list(readable), "read_only_files": list(read_only or {}),
                   "read_error": read_error, "related_files": contents,
                   "read_instruction": read_instruction(last_turn=turn == 5),
                   "allowed_outputs": allowed}
        response = DeepSeekClient(config).complete([
            {"role": "system", "content": EDITOR + project_context(config)},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}], task / "llm_calls.jsonl")
        if response.get("action") == "read_files":
            names = response.get("paths")
            if not isinstance(names, list) or not 1 <= len(names) <= 6:
                raise ValueError("read_files每轮需选择1至6个已有文件。")
            if any(not isinstance(name, str) for name in names):
                raise ValueError("read_files路径必须为字符串。")
            missing = [name for name in names if name not in readable]
            read_error = {"unavailable_paths": missing,
                          "message": READ_UNAVAILABLE} if missing else None
            previous = set(selected)
            selected = list(dict.fromkeys(selected + [name for name in names if name in readable]))
            if not missing and set(selected) == previous:
                read_error = {"message": READ_ALREADY_PROVIDED}
            memory.event(task, "读取", ", ".join(selected))
            continue
        if response.get("action") == "clarify":
            return response
        changes = response.get("files")
        if response.get("action") != "deliver" or not isinstance(changes, dict) or not 1 <= len(changes) <= 3:
            raise ValueError("增量交付应返回1至3个文件，不是整个工程。")
        for name, text in changes.items():
            file_path(name)
            if allowed is not None and name not in allowed:
                raise ValueError(f"交付文件不属于当前任务：{name}")
            if not isinstance(text, str) or not text.strip() or len(text) > 160000:
                raise ValueError(f"文件内容无效或过大：{name}")
        return response
    raise ValueError("当前任务读取轮次达到上限，检查点已保留。")


def build_project(task, state, config):
    from . import workflow, python_tool
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
        if item.get("kind") == "python":
            for attempt in range(config["max_repairs"] + 1):
                result = python_tool.prepare(task, config, files, item)
                state["results"].append(result)
                workflow.save(task, state)
                if result["passed"] or attempt == config["max_repairs"]:
                    break
                update = edit(task, config, {**item, "failure": result["diagnostic"]}, files,
                              item["inputs"], [item["script"]])
                if update["action"] == "clarify":
                    state.update(status="waiting_for_user", question=update.get("message", "请补充"))
                    workflow.save(task, state)
                    return None
                files.update(update["files"])
                tools.write_json(checkpoint, draft)
            if not result["passed"]:
                state.update(status="verification_failed", error=result["diagnostic"])
                workflow.save(task, state)
                return None
            files.update(result["output_files"])
            evidence = {"status": "delivered", "goal": item["goal"], "evidence": result["run_dir"]}
        else:
            response = edit(task, config, item, files, item.get("inputs", []), item["outputs"])
            if response["action"] == "clarify":
                state.update(status="waiting_for_user", question=response.get("message", "请补充"))
                workflow.save(task, state)
                return None
            if set(response["files"]) != set(item["outputs"]):
                raise ValueError("本轮交付缺少任务声明的产物。")
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


def repair(task, state, config, response, result):
    """Edit only relevant files; existing files and frozen specification are preserved."""
    files = response["project"]["files"]
    diagnostic = json.dumps({"errors": result.get("errors", [])[:4],
        "blocking_findings": result.get("blocking_findings", []),
        "observations": result.get("observations", {}),
        "log_excerpt": result.get("diagnostic", "")[-5000:]}, ensure_ascii=False)
    initial = [name for name in files if name in diagnostic][:3]
    harness = task / "project/sim/agent_sim.sv"
    read_only = {"sim/agent_sim.sv": harness.read_text(encoding="utf-8")} if harness.is_file() else {}
    update = edit(task, config, {"goal": INCREMENTAL_REPAIR_GOAL, "failure": diagnostic,
                   "stage": result["stage"]}, files, initial, read_only=read_only)
    if update["action"] == "clarify":
        return update
    merged = {**response, "project": {**response["project"], "files": {**files, **update["files"]}}}
    memory.event(task, "增量修复", update.get("message", "") + "；修改：" + ", ".join(update["files"]))
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

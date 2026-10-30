"""A small persisted workflow: DeepSeek RTL -> simulation -> implementation -> hardware."""
from datetime import datetime
import json
from pathlib import Path
import re
import uuid

from .agent import handle_message, analyze_result, explain_failure, review_project
from . import project as project_model
from . import tools, task_memory, python_tool
from .interaction import read_reply

from .prompts.catalog import (
    RESUME_REQUEST_PREFIX, RESUME_REQUEST_SUFFIX,
    CURRENT_PROJECT_PREFIX, LAST_FAILURE_PREFIX,
)


def load_config(path: Path) -> dict:
    path = path.resolve()
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    root = path.parent
    config["root"] = str(root)
    # Optional historical example; new development never reads these source files.
    config["project_dir"] = str((root / config.get("project_dir", "projects/led")).resolve())
    config["workspace_path"] = str((root / config["workspace_dir"]).resolve())
    if not re.fullmatch(r"[a-z][a-z0-9_]*", config["board"]):
        raise ValueError("board必须是boards目录中的小写配置id。")
    Path(config["workspace_path"]).mkdir(parents=True, exist_ok=True)
    return config


def task_path(config: dict, task_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", task_id):
        raise ValueError("无效任务编号。")
    path = Path(config["workspace_path"]) / task_id
    if not (path / "state.json").is_file():
        raise ValueError(f"找不到任务：{task_id}")
    return path


def environment_checks(config: dict) -> dict:
    profile = Path(config["root"]) / "boards" / (config["board"] + ".json")
    missing_templates = [str(Path(config["root"]) / "templates" / p)
                         for p in (*config["scripts"].values(), "vio.tcl")
                         if not (Path(config["root"]) / "templates" / p).is_file()]
    return {"vivado": Path(config["vivado_path"]).is_file(),
            "board_profile": profile.is_file(), "board_profile_path": str(profile),
            "templates": not missing_templates, "missing_templates": missing_templates}


def save(task: Path, state: dict) -> None:
    previous = json.loads((task / "state.json").read_text(encoding="utf-8")) if (task / "state.json").exists() else {}
    if previous.get("status") != state["status"]:
        task_memory.event(task, "阶段", state["status"])
    state["updated_at"] = datetime.now().astimezone().isoformat()
    tools.write_json(task / "state.json", state)
    task_memory.sync(task, state)
    model_description = "基准RTL工具测试，无LLM调用。"
    audit_path = task / "llm_calls.jsonl"
    if audit_path.is_file():
        calls = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines() if line]
        complete_calls = [c for c in calls if c.get("finish_reason") == "stop"]
        thought_calls = sum(bool(call.get("reasoning_received")) for call in calls)
        efforts = sorted({c.get("reasoning_effort", "unknown") for c in calls})
        models = sorted({c.get("model") or "unknown" for c in calls})
        model_description = (f"{','.join(models)}，thinking enabled，effort {','.join(efforts)}。"
                             f"记录调用 {len(calls)} 次，完整返回 {len(complete_calls)} 次，{thought_calls} 次包含思考内容。")
    report = [f"# FPGA 任务 {task.name}", f"\n状态：{state['status']}",
              f"\n需求：{state.get('request', '')}",
              f"\n模型：{model_description}",
              f"\n规格：`{json.dumps(state.get('spec', {}), ensure_ascii=False)}`",
              f"\n设计说明：{state.get('message', '')}", "\n## 验证记录"]
    for result in state.get("results", []):
        report.append(f"\n- {result['stage']}: {'PASS' if result['passed'] else 'FAIL'}；"
                      f"日志 `{result['run_dir']}`")
        if "timing" in result:
            report.append(f"  时序：`{result['timing']}`")
        if "transitions" in result:
            report.append(f"  实板样本 {result['samples']} 个，切换 {result['transitions']} 次。")
        if "hardware_verified" in result:
            report.append(f"  自动探针规则验证：{result['hardware_verified']}。未启用规则时PASS仅表示下载成功。")
    report.extend(["\n## 用户验收", f"\n{state.get('acceptance', '待用户按本任务验收范围确认结果。')}",
                   "\n模型生成测试和同模型独立上下文审查不是形式证明。VIO仅检查已配置的探针规则，现场观察不能替代精确仪器测量。",
                   "\n下载策略为易失 PL 配置，不写 QSPI/eMMC；是否已执行下载以program阶段记录为准。"])
    if state.get("build_hash"):
        report.extend(["\n## 产物追溯", f"\n源码版本摘要：`{state['build_hash']}`",
                       f"\n产物摘要：`{json.dumps(state.get('artifact', {}))}`"])
    if state.get("error"):
        report.append(f"\n最近错误：{state['error']}")
    (task / "report.md").write_text("\n".join(report) + "\n", encoding="utf-8")


def validate_design(response: dict, config: dict) -> tuple[dict, dict]:
    return project_model.validate(response, project_model.board_profile(config), config.get("target", "build"))


def prepare_project(task: Path, config: dict, spec: dict, design: dict) -> dict:
    return project_model.materialize(task, config, spec, design)


def runtime_config(task: Path, config: dict, state: dict) -> dict:
    generic = (task / "project/project.json").is_file()
    metadata = json.loads((task / "project/project.json").read_text(encoding="utf-8")) if generic else {}
    scripts = ({"simulation": "generic_sim.tcl", "build": "generic_build.tcl", "probe": "probe.tcl", "program": "generic_program.tcl"}
               if generic else {"simulation": "sim.tcl", "build": "build.tcl", "probe": "probe.tcl", "program": "program.tcl"})
    return {**config, "project_path": str(task / "project"), "generic": generic, "scripts": scripts,
            "hardware_analysis": metadata.get("hardware_analysis"),
            "step_ms": state.get("spec", {}).get("step_ms", 500)}


def develop(task: Path, state: dict, config: dict, response: dict) -> dict:
    state.pop("error", None)
    revision_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    revision_dir = task / "revisions" / revision_id
    revision_dir.mkdir(parents=True)
    state["revision_id"] = revision_id
    frozen_spec = state.get("spec") if state.get("plan") else None
    for attempt in range(config["max_repairs"] + 1):
        for key in ("simulation_hash", "build_hash", "artifact", "acceptance"):
            state.pop(key, None)
        tools.write_json(revision_dir / f"candidate-{attempt}.json", response)
        try:
            spec, design = validate_design(response, config)
            if frozen_spec is not None and spec != frozen_spec:
                raise ValueError("自动修复不能改变冻结的用户规格。")
            frozen_spec = spec
            state.update(spec=spec, message=response.get("message", ""), status="design_ready",
                         project_format=2, target=config.get("target", "build"))
            runtime = prepare_project(task, config, spec, design)
            revision = tools.design_hash(task / "project")
            task_memory.event(task, "工程", f"版本 {revision_id}，候选 {attempt}；源码已保存至 project/")
            state["status"] = "simulating"
            save(task, state)
            result = tools.run_simulation(runtime)
            state["results"].append(result)
            simulation_result = result
            build_result = None
            analysis_result = None
            if result["passed"] and config.get("visualization"):
                state["status"] = "analyzing"
                save(task, state)
                task_memory.event(task, "Python", "根据本次真实仿真VCD生成波形图片和信号摘要")
                result = python_tool.run(runtime)
                state["results"].append(result)
                analysis_result = result
            if result["passed"] and config.get("target") != "sim":
                state["status"] = "building"
                save(task, state)
                result = tools.run_build(runtime)
                state["results"].append(result)
                build_result = result
            if result["passed"]:
                state["status"] = "reviewing"
                save(task, state)
                review_request = json.dumps(task_memory.context(task), ensure_ascii=False) if state.get("plan") else state["request"]
                review = review_project(review_request, spec, design, config, task / "llm_calls.jsonl", simulation_result, build_result, analysis_result)
                review_dir = task / "runs" / f"review-{revision_id}-{attempt}"
                review_dir.mkdir(parents=True, exist_ok=True)
                tools.write_json(review_dir / "review.json", review)
                result = {"stage": "review", "passed": review.get("approved") is True,
                          "phase": "simulation_review" if config.get("target") == "sim" else "pre_program_review",
                          "blocking_findings": review.get("blocking_findings", []),
                          "warnings": review.get("warnings", []),
                          "pending_checks": review.get("pending_checks", []),
                          "run_dir": str(review_dir), "diagnostic": json.dumps(review, ensure_ascii=False)}
                state["results"].append(result)
                if result["passed"]:
                    if tools.design_hash(task / "project") != revision:
                        raise RuntimeError("验证过程中源码已被外部修改，已停止并保留现场文件。")
                    state["simulation_hash"] = revision
                    if config.get("target") == "sim":
                        state["status"] = "simulation_verified"
                        save(task, state)
                        return state
                    state.update(build_hash=revision, artifact=build_result["artifact"], status="ready_to_program")
                    save(task, state)
                    return state
        except ValueError as exc:
            result = {"stage": "project_validation", "passed": False, "diagnostic": str(exc),
                      "run_dir": str(revision_dir / f"candidate-{attempt}.json")}
            state["results"].append(result)
        state["status"] = "verification_failed"
        state["error"] = result.get("diagnostic", "工程检查失败。")
        save(task, state)
        if attempt >= config["max_repairs"]:
            return state
        print(f"[修复] 工程/测试/工具失败，DeepSeek分析 {attempt+1}/{config['max_repairs']}", flush=True)
        state["status"] = "repairing"
        save(task, state)
        task_memory.event(task, "修复", str(result.get("errors") or result.get("blocking_findings") or result.get("diagnostic", ""))[:1600])
        if config.get("incremental"):
            from .development import repair
            response = repair(task, state, config, response, result)
        else:
            response = analyze_result(result, config, response.get("project", {}),
                                      frozen_spec or response.get("spec", {}), task / "llm_calls.jsonl")
        if response.get("action") != "design":
            state["error"] = response.get("message", "模型未提供修复。")
            if response.get("action") == "clarify":
                state.update(status="waiting_for_user", question=state["error"])
                state["conversation"].append({"role": "assistant", "content": json.dumps(response, ensure_ascii=False)})
            elif response.get("action") == "pause":
                state.update(status="development_paused", question=None)
            save(task, state)
            return state
        state.pop("error", None)
    return state


def verify_task(task_id: str, config: dict, candidate: str | None = None) -> dict:
    """Re-validate current generated/manual source files without asking the LLM to regenerate them."""
    task = task_path(config, task_id)
    with tools.lock(task / "active.lock"):
        state = json.loads((task / "state.json").read_text(encoding="utf-8"))
        if candidate:
            snapshot = (task / candidate).resolve()
            if not snapshot.is_relative_to(task.resolve()) or not snapshot.is_file():
                raise ValueError("candidate必须是该任务目录中的工程响应JSON。")
            response = json.loads(snapshot.read_text(encoding="utf-8"))
            if state.get("spec") and response.get("spec") != state["spec"]:
                raise ValueError("候选工程规格与当前任务不一致，请通过run --resume修改需求。")
            runtime = {**config, "target": state.get("target", "build"), "max_repairs": 0,
                       "visualization": state.get("plan", {}).get("visualization", False)}
            try:
                return develop(task, state, runtime, response)
            except BaseException as exc:
                state.update(status="interrupted" if isinstance(exc, KeyboardInterrupt) else "error", error=str(exc))
                save(task, state)
                raise
        path = task / "project/project.json"
        if not path.is_file():
            raise ValueError("该任务没有通用工程，不能verify。")
        design = json.loads(path.read_text(encoding="utf-8"))
        files = {}
        for folder in ("rtl", "sim", "scripts", "data"):
            for source in sorted((task / "project" / folder).glob("*")):
                if source.is_file() and source.suffix in {".v", ".sv", ".py", ".hex", ".mem", ".txt", ".json", ".csv"} and source.stem != "agent_sim":
                    text = source.read_text(encoding="utf-8")
                    text = re.sub(r"\n`ifdef HARDWARE_DEBUG.*?`endif\n", "\n", text, flags=re.S)
                    files[source.relative_to(task / "project").as_posix()] = text
        readme = task / "project/README.md"
        if readme.is_file():
            files["README.md"] = readme.read_text(encoding="utf-8")
        design["files"] = files
        runtime = {**config, "target": design.get("target", "build"), "max_repairs": 0,
                   "visualization": state.get("plan", {}).get("visualization", False)}
        try:
            return develop(task, state, runtime, {"action": "design", "message": state.get("message", ""),
                                                 "spec": state["spec"], "project": design})
        except BaseException as exc:
            state.update(status="interrupted" if isinstance(exc, KeyboardInterrupt) else "error", error=str(exc))
            save(task, state)
            raise


def run_task(request: str, config: dict, ask=input, task_id: str | None = None) -> dict:
    checks = environment_checks(config)
    if not all(checks[k] for k in ("vivado", "board_profile", "templates")):
        raise ValueError("本地开发环境不完整，请先运行 doctor：" + json.dumps(checks, ensure_ascii=False))
    if task_id:
        task = task_path(config, task_id)
        state = json.loads((task / "state.json").read_text(encoding="utf-8"))
        if state["status"] not in {"waiting_for_user", "verification_failed", "error", "interrupted", "ready_to_program", "simulation_verified", "completed", "acceptance_failed", "hardware_failed", "awaiting_acceptance"}:
            raise ValueError("当前任务仍在运行，不能继续。")
        if state.get("project_format") != 2:
            raise ValueError("旧流水灯任务可继续program/accept，但修改需求请新建任务。")
        state["request"] += "\n用户补充或修改：" + request
        request = RESUME_REQUEST_PREFIX + state["request"] + RESUME_REQUEST_SUFFIX
        metadata_path = task / "project/project.json"
        if metadata_path.is_file():
            current = json.loads(metadata_path.read_text(encoding="utf-8"))
            files = {}
            for folder in ("rtl", "sim"):
                for path in sorted((task / "project" / folder).glob("*")):
                    if path.is_file() and path.suffix in {".v", ".sv"} and path.stem != "agent_sim":
                        text = path.read_text(encoding="utf-8")
                        text = re.sub(r"\n`ifdef HARDWARE_DEBUG.*?`endif\n", "\n", text, flags=re.S)
                        files[path.relative_to(task / "project").as_posix()] = text
            current["files"] = files
            request += CURRENT_PROJECT_PREFIX + json.dumps(current, ensure_ascii=False)
        if state.get("results") and not state["results"][-1]["passed"]:
            request += LAST_FAILURE_PREFIX + json.dumps(state["results"][-1], ensure_ascii=False)
    else:
        task = Path(config["workspace_path"]) / (datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6])
        task.mkdir()
        state = {"task_id": task.name, "request": request, "status": "planning", "results": [], "conversation": [],
                 "project_format": 2, "target": config.get("target", "build")}
    print(f"[任务] {task.name}", flush=True)
    with tools.lock(task / "active.lock"):
        try:
            # A new user requirement invalidates prior build/deployment evidence immediately.
            for key in ("simulation_hash", "build_hash", "artifact", "acceptance", "error"):
                state.pop(key, None)
            save(task, state)
            while True:
                response = handle_message(request, config, state["conversation"], task / "llm_calls.jsonl")
                tools.write_json(task / "design_response.json", response)
                print("[智能体] " + response.get("message", ""), flush=True)
                if response.get("action") == "clarify":
                    state.update(status="waiting_for_user", question=response.get("message", ""))
                    save(task, state)
                    request = read_reply(task, ask, "请补充")
                    if not request.strip():
                        return state
                elif response.get("action") == "design":
                    return develop(task, state, config, response)
                elif response.get("action") == "unsupported":
                    state.update(status="unsupported", error=response.get("message", ""))
                    save(task, state)
                    return state
                else:
                    raise ValueError("模型返回未知 action。")
        except BaseException as exc:
            state.update(status="interrupted" if isinstance(exc, KeyboardInterrupt) else "error", error=str(exc))
            save(task, state)
            raise


def deploy(task_id: str, config: dict) -> dict:
    task = task_path(config, task_id)
    with tools.lock(task / "active.lock"):
        state = json.loads((task / "state.json").read_text(encoding="utf-8"))
        runtime = runtime_config(task, config, state)
        tools.verify_artifact(task / "project", state)
        state.pop("acceptance", None)
        state.pop("error", None)
        state["status"] = "probing"
        save(task, state)
        try:
            result = tools.probe_hardware(runtime)
            state["results"].append(result)
            if result["passed"]:
                state["status"] = "programming"
                save(task, state)
                result = tools.program_device(runtime, state)
                state["results"].append(result)
            if result["passed"] and runtime.get("hardware_analysis"):
                state["status"] = "analyzing_hardware"
                save(task, state)
                task_memory.event(task, "硬件分析", "将本次VIO实际采样CSV交给已交付Python脚本")
                result = python_tool.hardware(runtime, result)
                state["results"].append(result)
            state["status"] = "awaiting_acceptance" if result["passed"] else "hardware_failed"
            if not result["passed"]:
                state["error"] = f"{result['stage']} 失败，详见 {result['run_dir']}"
                try:
                    diagnosis = explain_failure(result, config, task / "llm_calls.jsonl")
                    tools.write_json(task / "hardware_diagnosis.json", diagnosis)
                    state["error"] = diagnosis.get("message", state["error"])
                    print("[诊断] " + state["error"], flush=True)
                except (RuntimeError, ValueError, OSError, KeyError) as exc:
                    state["diagnosis_error"] = str(exc)
            save(task, state)
            return state
        except BaseException as exc:
            state.update(status="hardware_failed", error=str(exc))
            save(task, state)
            raise


def diagnose(task_id: str, config: dict) -> dict:
    task = task_path(config, task_id)
    with tools.lock(task / "active.lock"):
        state = json.loads((task / "state.json").read_text(encoding="utf-8"))
        if state["status"] not in {"error", "hardware_failed", "verification_failed"}:
            raise ValueError("当前任务不处于失败状态，无需诊断旧的失败记录。")
        failed = next((r for r in reversed(state.get("results", [])) if not r["passed"]), None)
        if not failed:
            raise ValueError("当前任务没有失败记录。")
        diagnosis = explain_failure(failed, config, task / "llm_calls.jsonl")
        tools.write_json(task / "failure_diagnosis.json", diagnosis)
        state["error"] = diagnosis.get("message", "模型未返回诊断说明。")
        save(task, state)
        print("[诊断] " + state["error"], flush=True)
        return state


def accept(task_id: str, config: dict, passed: bool, note: str) -> dict:
    task = task_path(config, task_id)
    with tools.lock(task / "active.lock"):
        state = json.loads((task / "state.json").read_text(encoding="utf-8"))
        if state["status"] not in {"awaiting_acceptance", "completed", "acceptance_failed"}:
            raise ValueError("实板自动验证通过后才可记录现场验收。")
        tools.verify_artifact(task / "project", state)
        last = state.get("results", [{}])[-1] if state.get("results") else {}
        source = "user_review_of_automated_evidence" if last.get("stage") == "hardware_analysis" and last.get("passed") else "user_observation"
        state.update(status="completed" if passed else "acceptance_failed",
                     acceptance={"passed": passed, "note": note, "source": source})
        save(task, state)
        return state

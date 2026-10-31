"""Model-directed analysis of saved evidence, isolated from build/program gates."""
import json
import shutil
import uuid
from pathlib import Path

from . import development, model_steps, python_tool, tools
from .agent import DeepSeekClient, GenerationTruncated
from .contract import describe, file_path
from .prompts.catalog import ANALYSIS_CONTROLLER
from .task_memory import event
from .workflow import task_path


def _inside(root, name):
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError(f"File is not inside the selected task: {name}")
    return path


def _save(folder, state):
    tools.write_json(folder / "state.json", state)
    report = {k: state.get(k) for k in ("status", "request", "baseline", "summary", "assessment", "question", "feedback", 'model_pause')}
    report["executions"] = [
        {k: result.get(k) for k in ("passed", "exit_code", "run_dir", "source_sha256", "script_sha256", "artifacts")}
        for result in state["results"]
    ]
    (folder / "report.md").write_text(
        "# Saved-evidence analysis\n\nThis record does not approve another source version or authorize programming.\n\n"
        + json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def create(parent, candidate, source, script, request):
    candidate_path, source_path = _inside(parent, candidate), _inside(parent, source)
    baseline = json.loads(candidate_path.read_text(encoding="utf-8"))
    files = baseline["project"]["files"]
    file_path(script)
    if not script.startswith("scripts/") or not script.endswith(".py") or script not in files:
        raise ValueError("Select an existing scripts/*.py in the candidate")
    if source_path.suffix.lower() not in {".csv", ".vcd"}:
        raise ValueError("Saved analysis input must be CSV or VCD")
    metadata = baseline["project"]
    hardware = metadata.get("hardware_analysis") or {}
    outputs = hardware.get("outputs", []) if hardware.get("script") == script else []
    if not isinstance(outputs, list) or any(not isinstance(n, str) or Path(n).name != n or n in {".", ".."} for n in outputs):
        raise ValueError("Declared analysis outputs must be flat filenames")
    # Validate paths before creating a session; no generated RTL is executed or changed.
    for name in files:
        file_path(name)
    folder = parent / "analysis" / uuid.uuid4().hex[:12]
    folder.mkdir(parents=True)
    for name, text in files.items():
        path = folder / "project" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    saved_input = folder / ("input" + source_path.suffix)
    shutil.copy2(source_path, saved_input)
    with saved_input.open("rb") as handle:
        preview = handle.read(16384).decode("utf-8", errors="replace")
    (folder / "input_preview.txt").write_text("First at most 16384 bytes, not necessarily the full input.\n" + preview, encoding="utf-8")
    tools.write_json(folder / "baseline.json", baseline)
    state = {"status": "deciding", "request": request, "script": script, "outputs": outputs,
             "spec": baseline["spec"], "summary": "", "results": [], "decisions": [], "views": {},
             "baseline": {"candidate": candidate, "candidate_sha256": tools.file_hash(candidate_path),
                          "source": source, "input": saved_input.name, "source_sha256": tools.file_hash(saved_input)},
             "protected_files": {n: tools.file_hash(folder / "project" / n) for n in files if n != script}}
    _save(folder, state)
    return folder


def _files(folder, state):
    names = ["input_preview.txt", state["baseline"]["input"]]
    names += ["project/" + n for n in (*state["protected_files"], state["script"])]
    for result in state["results"]:
        for name in result.get("logs", []) + result.get("evidence_files", []):
            path = Path(name).resolve()
            if path.is_relative_to(folder.resolve()) and path.is_file():
                names.append(path.relative_to(folder.resolve()).as_posix())
    return list(dict.fromkeys(names))


def _read(folder, views):
    result = {}
    for name, view in views.items():
        with _inside(folder, name).open("rb") as handle:
            handle.seek(view["offset"])
            data = handle.read(view["limit"])
        result[name] = {**view, "units": "bytes", "text": data.decode("utf-8", errors="replace"),
                        "size": (folder / name).stat().st_size}
    return result


def _check_baseline(folder, state):
    if tools.file_hash(folder / state["baseline"]["input"]) != state["baseline"]["source_sha256"]:
        raise RuntimeError("Saved input evidence changed")
    for name, digest in state["protected_files"].items():
        if tools.file_hash(folder / "project" / name) != digest:
            raise RuntimeError(f"Read-only baseline changed: {name}")


def run(task_id, config, *, candidate=None, source=None, script=None, request=None, resume=None, answer=None):
    parent = task_path(config, task_id)
    if resume:
        if any((candidate, source, script, request)):
            raise ValueError("Resume uses the saved scope and evidence; create a new session to change them")
        if not resume.isalnum():
            raise ValueError("Invalid analysis session id")
        folder = parent / "analysis" / resume
        _inside(folder, "state.json")
    else:
        if not all((candidate, source, script, request)):
            raise ValueError("New analysis requires candidate, source, script and request")
        # Copy a consistent baseline while the task is not being modified.
        with tools.lock(parent / "active.lock"):
            folder = create(parent, candidate, source, script, request)
    with tools.lock(folder / "active.lock"):
        state = json.loads((folder / "state.json").read_text(encoding="utf-8"))
        if state["status"] in {"reviewed", "stopped"}:
            return {**state, "analysis_id": folder.name, "report": str(folder / "report.md")}
        if state["status"] == "waiting_for_user" and not answer:
            return {**state, "analysis_id": folder.name, "report": str(folder / "report.md")}
        if answer:
            state["user_reply"] = {"question": state.get("question"), "answer": answer}
            state.pop("question", None)
        baseline = json.loads((folder / "baseline.json").read_text(encoding="utf-8"))
        runtime = {**config, "project_path": str(folder / "project"),
                   "board_snapshot": baseline["project"].get("board")}
        if not runtime["board_snapshot"]:
            runtime.pop("board_snapshot")
        try:
            for _ in range(config.get("analysis_max_steps", 12)):
                _check_baseline(folder, state)
                state["status"] = "deciding"
                _save(folder, state)
                index = _files(folder, state)
                if state.get("pending_edit"):
                    decision = state["pending_edit"]
                else:
                    payload = {"request": state["request"], "spec": state["spec"], "baseline": state["baseline"],
                               "summary": state["summary"], "user_reply": state.get("user_reply"),
                               "script": state["script"], "declared_outputs": state["outputs"],
                               "execution_contract": describe(baseline["project"].get("board")),
                               "file_index": index, "file_views": _read(folder, state["views"]),
                               "last_result": model_steps.evidence_context(folder, state['results'][-1], {}) if state['results'] else None,
                               'recovery': state.get('recovery'), 'next_step': state.get('next_step'),
                               "last_edit": state.get("last_edit"),
                               "feedback": state.get("feedback"), "recent_decisions": state["decisions"][-4:]}
                    try:
                        decision = DeepSeekClient(config).complete([
                            {"role": "system", "content": ANALYSIS_CONTROLLER},
                            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}], folder / "llm_calls.jsonl")
                    except GenerationTruncated as exc:
                        state.update(status='model_paused', recovery=exc.details,
                                     model_pause=model_steps.pause_details('analysis', folder / 'state.json',
                                         '分析决策输出超限，保留当前数据、脚本及执行结果，可原地续接', config, exc.details))
                        _save(folder, state)
                        return {**state, 'analysis_id': folder.name, 'report': str(folder / 'report.md')}
                    state.pop('model_pause', None)
                    state["decisions"].append(decision)
                action = decision.get("action")
                event(folder, "分析决策", str(action) + ": " + str(decision.get("message", ""))[:1200])
                state["feedback"] = None
                if isinstance(decision.get("summary"), str):
                    state["summary"] = decision["summary"][:8000]
                if state.get('recovery') and action not in {'read_files', 'checkpoint', 'clarify', 'stop'}:
                    state['feedback'] = 'After truncation first submit a short checkpoint with summary and one next_step'
                elif action == 'checkpoint':
                    if all(isinstance(decision.get(k), str) and decision[k].strip() for k in ('summary', 'next_step')):
                        state.update(summary=decision['summary'][:8000], next_step=decision['next_step'][:2000], recovery=None)
                    else:
                        state['feedback'] = 'checkpoint requires summary and next_step'
                elif action == "read_files":
                    names = decision.get("paths", [])
                    offset, limit = decision.get("offset", 0), decision.get("limit", 8000)
                    if (not isinstance(names, list) or not 1 <= len(names) <= 6 or any(n not in index for n in names)
                            or type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 8000):
                        state["feedback"] = "read_files requires 1..6 indexed paths, byte offset>=0 and limit=1..8000"
                    else:
                        state["views"] = {n: {"offset": offset, "limit": limit} for n in names}
                elif action == "edit":
                    if not isinstance(decision.get("instructions"), str) or not decision["instructions"].strip():
                        state["feedback"] = "edit requires instructions"
                        continue
                    state.update(status="editing", pending_edit=decision)
                    _save(folder, state)
                    files = {n: (folder / "project" / n).read_text(encoding="utf-8") for n in (*state["protected_files"], state["script"])}
                    evidence = development.read_evidence(folder, state["results"][-1]) if state["results"] else {}
                    evidence["input_preview.txt"] = (folder / "input_preview.txt").read_text(encoding="utf-8")
                    update = development.edit(folder, runtime, {"goal": decision["instructions"], "scope": state["request"]},
                                              files, [state["script"]], [state["script"]], evidence=evidence,
                                              user_reply=state.get("user_reply"))
                    if update["action"] in {"clarify", "pause"}:
                        state.update(status="waiting_for_user" if update["action"] == "clarify" else "paused",
                                     question=update.get("message"))
                        if update.get('model_pause'):
                            state.update(status='model_paused', model_pause=update['model_pause'])
                        break
                    (folder / "project" / state["script"]).write_text(update["files"][state["script"]], encoding="utf-8")
                    state["last_edit"] = {"path": state["script"], "written": True,
                                          "script_sha256": tools.file_hash(folder / "project" / state["script"])}
                    state.pop("pending_edit", None)
                    state.pop("user_reply", None)
                elif action == "execute":
                    state["status"] = "executing"
                    _save(folder, state)
                    digest = tools.file_hash(folder / "project" / state["script"])
                    result = python_tool.analyze_file(runtime, folder / state["baseline"]["input"], state["script"], state["outputs"])
                    result["script_sha256"] = digest
                    state["results"].append(result)
                    event(folder, "实际执行", f"exit_code={result.get('exit_code')}, passed={result['passed']}; {result['run_dir']}")
                elif action == "clarify":
                    state.update(status="waiting_for_user", question=decision.get("message", "Please clarify"))
                    break
                elif action == "stop":
                    state.update(status="stopped", assessment=decision)
                    break
                elif action == "finish":
                    last = state["results"][-1] if state["results"] else {}
                    if type(decision.get("approved")) is not bool:
                        state["feedback"] = "finish requires approved:boolean and an evidence-based message"
                    elif decision["approved"] and (not last.get("passed") or last.get("script_sha256") != tools.file_hash(folder / "project" / state["script"])):
                        state["feedback"] = "The current script has no successful execution; approval cannot use an older script result"
                    else:
                        state.update(status="reviewed", assessment=decision)
                        break
                else:
                    state["feedback"] = "Actions: read_files, edit, execute, clarify, stop, finish"
                _save(folder, state)
            else:
                state.update(status="paused", feedback="Decision budget reached; evidence and editor checkpoints retained")
            _check_baseline(folder, state)
        except BaseException as exc:
            state.update(status="interrupted" if isinstance(exc, KeyboardInterrupt) else "error", feedback=f"{type(exc).__name__}: {exc}")
            _save(folder, state)
            raise
        _save(folder, state)
    return {**state, "analysis_id": folder.name, "report": str(folder / "report.md")}

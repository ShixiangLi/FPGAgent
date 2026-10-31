"""Python analysis execution with captured real errors, timeout and versioned artifacts."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

from .contract import file_path
from .tools import _stop, file_hash, write_json


def prepare(task, config, files, item):
    """Run a declared preprocessing script in a fresh directory, never reuse old outputs."""
    run = task / "runs" / ("prepare-" + uuid.uuid4().hex[:12])
    project = run / "project"
    project.mkdir(parents=True)
    result = {"stage": "prepare_python", "passed": False, "run_dir": str(run)}
    for name, text in files.items():
        file_path(name)
        if name not in item["outputs"]:
            path = project / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
    for name in item["outputs"]:
        file_path(name)
        (project / name).parent.mkdir(parents=True, exist_ok=True)
    log = run / "console.log"
    result["logs"] = [str(log)]
    command = [sys.executable, str(project / item["script"])]
    result["command"] = command
    try:
        result["exit_code"] = _commands([command], project, log, config)
        if result["exit_code"] == 0:
            outputs = {}
            for name in item["outputs"]:
                path = project / name
                if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(project.resolve()):
                    raise ValueError(f"Python没有生成声明的普通文件：{name}")
                if path.stat().st_size > 160000:
                    raise ValueError(f"预处理输出过大：{name}")
                outputs[name] = path.read_text(encoding="utf-8")
            result.update(passed=True, output_files=outputs)
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        result.update(error_type=type(exc).__name__, error=str(exc))
    result["diagnostic"] = result.get("error", "") + "\n" + (log.read_text(encoding="utf-8", errors="replace")[-8000:] if log.exists() else "")
    write_json(run / "result.json", result)
    return result


def _commands(commands, project, log, config, result=None):
    env = {k: v for k, v in os.environ.items() if k != "DEEPSEEK_API_KEY"}
    env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1", MPLBACKEND="Agg")
    options = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
               if os.name == "nt" else {"start_new_session": True})
    with log.open("w", encoding="utf-8") as output:
        for command in commands:
            if result is not None:
                result["active_command"] = command
            process = subprocess.Popen(command, cwd=project, env=env, stdout=output, stderr=subprocess.STDOUT, **options)
            try:
                code = process.wait(timeout=config.get("python_timeout_seconds", 120))
            except BaseException:
                _stop(process)
                raise
            if code:
                return code
    return 0


def files_hash(files):
    """Identify the generated sources used by an execution, including staged edits."""
    return hashlib.sha256(json.dumps(files, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def execute(task, config, files, request):
    """Run a model-selected analysis on a source snapshot; never materialize over a build."""
    script, source, outputs = request.get('script'), request.get('input'), request.get('outputs', [])
    file_path(script)
    if not script.startswith('scripts/') or not script.endswith('.py') or script not in files:
        raise ValueError('execute.script must be an existing scripts/*.py file')
    if not isinstance(source, str) or not source or '\\' in source:
        raise ValueError('execute.input must be a project or task relative data path')
    relative = Path(source)
    if relative.is_absolute() or '..' in relative.parts or ':' in source:
        raise ValueError('execute.input must stay inside the task')
    if relative.suffix.lower() not in {'.vcd', '.csv', '.json', '.txt', '.hex', '.mem'}:
        raise ValueError('execute.input must be a VCD or text data file')
    if source.startswith('project/'):
        source = source[len('project/'):]
    origin = 'project_file' if source in files else 'saved_task_file'
    if origin == 'saved_task_file':
        relative = (task / request['input']).resolve()
        if not relative.is_relative_to(task.resolve()) or not relative.is_file():
            raise ValueError(f'execute.input is not a file inside the task: {request["input"]}')
        source = relative
    if (not isinstance(outputs, list) or len(outputs) > 8 or any(
            not isinstance(n, str) or not n or n in {'.', '..'} or
            any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-' for c in n)
            for n in outputs)):
        raise ValueError('execute.outputs must contain at most 8 plain artifact filenames')
    result = _analyze({**config, 'project_path': str(task / 'project')}, source,
                      [script], outputs, 'analysis', files=files)
    result.update(input_origin=origin, requested_input=request['input'])
    write_json(Path(result['run_dir']) / 'result.json', result)
    return result


def _analyze(config, source, scripts, required, stage, *, files=None):
    project = Path(config["project_path"])
    task = project.parent
    run = task / "runs" / (stage + "-" + uuid.uuid4().hex[:12])
    output = run / "artifacts"
    output.mkdir(parents=True)
    if files is not None:
        project = run / 'project'
        for name, text in files.items():
            file_path(name)
            path = project / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding='utf-8')
        scripts = [project / script for script in scripts]
        source = project / source if isinstance(source, str) and source in files else Path(source)
    saved_source = run / source.name
    log = run / "console.log"
    commands = [[sys.executable, str(script), "--input", str(saved_source), "--output", str(output)] for script in scripts]
    result = {"stage": stage, "passed": False, "run_dir": str(run), "logs": [str(log)], "commands": commands}
    if files is not None:
        result['source_files_sha256'] = files_hash(files)
    try:
        shutil.copy2(source, saved_source)
        result["source_sha256"] = file_hash(saved_source)
        preview = run / "input_preview.txt"
        with saved_source.open("rb") as handle:
            prefix = handle.read(16384)
        preview.write_text(f"Source: {saved_source}\nBytes: {saved_source.stat().st_size}\n"
                           "First at most 16384 bytes; a preview, not the complete input.\n\n"
                           + prefix.decode("utf-8", errors="replace"), encoding="utf-8")
        result["evidence_files"] = [str(preview)]
        result["input_preview"] = preview.read_text(encoding="utf-8")
        code = _commands(commands, project, log, config, result)
        result["exit_code"] = code
        if file_hash(saved_source) != result["source_sha256"]:
            raise ValueError("Analysis modified its input evidence")
        if code == 0:
            missing = [name for name in required if not (output / name).is_file() or not (output / name).stat().st_size]
            if missing:
                raise ValueError("Analysis did not generate required artifacts: " + ", ".join(missing))
            destination = task / "artifacts" / run.name
            destination.parent.mkdir(exist_ok=True)
            shutil.copytree(output, destination)
            result.update(passed=True, artifacts=[str(p) for p in destination.iterdir() if p.is_file()])
            summary = destination / "waveform_summary.json"
            if summary.exists():
                result["waveform_summary"] = json.loads(summary.read_text(encoding="utf-8"))
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        result.update(passed=False, error_type=type(exc).__name__, error=str(exc))
    if not result["passed"]:
        result["artifacts"] = [str(p) for p in output.iterdir() if p.is_file()]
    reports = [p for p in sorted(output.iterdir()) if p.is_file() and p.suffix in {".md", ".json", ".txt"}]
    result.setdefault("evidence_files", []).extend(str(p) for p in reports)
    result["artifact_excerpts"] = {}
    for path in reports[:4]:
        with path.open(encoding="utf-8", errors="replace") as handle:
            result["artifact_excerpts"][path.name] = handle.read(6000)
    result["excerpt_note"] = "Log tails, input preview and report excerpts are bounded; full files remain at the listed paths."
    result["diagnostic"] = result.get("error", "") + "\n" + (log.read_text(encoding="utf-8", errors="replace")[-12000:] if log.exists() else "")
    write_json(run / "result.json", result)
    return result


def analyze_file(config, source, script, required):
    """Execute a declared analysis against supplied evidence; no build or programming."""
    file_path(script)
    if not script.startswith("scripts/") or not script.endswith(".py"):
        raise ValueError("Analysis script must be scripts/*.py")
    return _analyze(config, Path(source), [Path(config["project_path"]) / script], required, "analysis")


def run(config):
    project = Path(config["project_path"])
    scripts = [Path(__file__).with_name("waveform.py")]
    if config.get("analysis_script"):
        scripts.append(project / config["analysis_script"])
    return _analyze(config, project / "build/sim/waveform.vcd", scripts, ["waveform.png", "waveform_summary.json"], "python")


def hardware(config, program_result):
    metadata = config["hardware_analysis"]
    # Use the immutable CSV from this successful programming run, never a previous build.
    source = Path(program_result["run_dir"]) / "hardware_samples.csv"
    return _analyze(config, source, [Path(config["project_path"]) / metadata["script"]], metadata["outputs"], "hardware_analysis")

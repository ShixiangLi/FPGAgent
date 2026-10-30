"""Python analysis execution with captured real errors, timeout and versioned artifacts."""
import os
import json
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

from .tools import write_json, file_hash, _stop
from .contract import file_path


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
        result["error"] = str(exc)
    result["diagnostic"] = result.get("error", "") + "\n" + log.read_text(encoding="utf-8", errors="replace")[-8000:]
    write_json(run / "result.json", result)
    return result


def _commands(commands, project, log, config):
    env = {k: v for k, v in os.environ.items() if k != "DEEPSEEK_API_KEY"}
    env.update(PYTHONDONTWRITEBYTECODE="1", MPLBACKEND="Agg")
    with log.open("w", encoding="utf-8") as output:
        for command in commands:
            process = subprocess.Popen(command, cwd=project, env=env, stdout=output, stderr=subprocess.STDOUT)
            try:
                code = process.wait(timeout=config.get("python_timeout_seconds", 120))
            except BaseException:
                _stop(process)
                raise
            if code:
                return code
    return 0


def _analyze(config, source, scripts, required, stage):
    project = Path(config["project_path"])
    task = project.parent
    run = task / "runs" / (stage + "-" + uuid.uuid4().hex[:12])
    output = run / "artifacts"
    output.mkdir(parents=True)
    saved_source = run / source.name
    log = run / "console.log"
    commands = [[sys.executable, str(script), "--input", str(saved_source), "--output", str(output)] for script in scripts]
    result = {"stage": stage, "passed": False, "run_dir": str(run), "logs": [str(log)], "commands": commands}
    try:
        shutil.copy2(source, saved_source)
        result["source_sha256"] = file_hash(saved_source)
        code = _commands(commands, project, log, config)
        result["exit_code"] = code
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
        result.update(passed=False, error=str(exc))
    result["diagnostic"] = result.get("error", "") + "\n" + (log.read_text(encoding="utf-8", errors="replace")[-12000:] if log.exists() else "")
    write_json(run / "result.json", result)
    return result


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

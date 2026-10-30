"""Python analysis execution with captured real errors, timeout and versioned artifacts."""
import os
import json
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

from .tools import write_json, file_hash, _stop


def prepare(task, config, files, item):
    """Run a declared preprocessing script in a fresh directory, never reuse old outputs."""
    from .development import file_path
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
    env = {k: v for k, v in os.environ.items() if k != "DEEPSEEK_API_KEY"}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        with log.open("w", encoding="utf-8") as output:
            process = subprocess.Popen(command, cwd=project, env=env, stdout=output, stderr=subprocess.STDOUT)
            try:
                result["exit_code"] = process.wait(timeout=config.get("python_timeout_seconds", 120))
            except BaseException:
                _stop(process)
                raise
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


def run(config):
    project = Path(config["project_path"])
    task = project.parent
    run = task / "runs" / ("python-" + uuid.uuid4().hex[:12])
    run.mkdir(parents=True)
    output = run / "artifacts"
    output.mkdir()
    source = project / "build/sim/waveform.vcd"
    saved_source = run / "waveform.vcd"
    log = run / "console.log"
    env = {k: v for k, v in os.environ.items() if k != "DEEPSEEK_API_KEY"}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["MPLBACKEND"] = "Agg"
    commands = [[sys.executable, str(Path(__file__).with_name("waveform.py")), "--input", str(saved_source), "--output", str(output)]]
    script = config.get("analysis_script")
    if script:
        commands.append([sys.executable, str(project / script), "--input", str(saved_source), "--output", str(output)])
    result = {"stage": "python", "passed": False, "run_dir": str(run), "logs": [str(log)], "commands": commands}
    try:
        shutil.copy2(source, saved_source)
        result["source_sha256"] = file_hash(saved_source)
        with log.open("w", encoding="utf-8") as f:
            for command in commands:
                process = subprocess.Popen(command, cwd=project, env=env, stdout=f, stderr=subprocess.STDOUT)
                try:
                    code = process.wait(timeout=config.get("python_timeout_seconds", 120))
                except BaseException:
                    _stop(process)
                    raise
                result["exit_code"] = code
                if code:
                    break
        result["passed"] = code == 0 and (output / "waveform.png").is_file()
        if result["passed"]:
            destination = task / "artifacts" / run.name
            destination.parent.mkdir(exist_ok=True)
            shutil.copytree(output, destination)
            result["artifacts"] = [str(p) for p in destination.iterdir() if p.is_file()]
            result["waveform_summary"] = json.loads((destination / "waveform_summary.json").read_text(encoding="utf-8"))
    except (OSError, subprocess.TimeoutExpired) as exc:
        result["error"] = str(exc)
    result["diagnostic"] = result.get("error", "") + "\n" + (log.read_text(encoding="utf-8", errors="replace")[-12000:] if log.exists() else "")
    write_json(run / "result.json", result)
    return result

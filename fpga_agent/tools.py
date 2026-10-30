"""Fixed Tcl execution, fresh evidence, timeouts and deployment gates."""
import contextlib
import csv
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time


def write_json(path: Path, value: dict) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def design_hash(project: Path) -> str:
    digest = hashlib.sha256()
    files = [project / "spec.json", project / "project.json"]
    for folder in ("rtl", "sim", "pins", "scripts", "data"):
        files.extend(sorted((project / folder).rglob("*")))
    for path in files:
        if path.is_file() and "__pycache__" not in path.parts:
            digest.update(path.relative_to(project).as_posix().encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


@contextlib.contextmanager
def lock(path: Path):
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise RuntimeError(f"资源已被占用：{path}。若上次异常退出，请确认进程已停止后再删除锁。") from None
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(str(os.getpid()))
        yield
    finally:
        path.unlink(missing_ok=True)


def verify_artifact(project: Path, state: dict) -> dict:
    revision = design_hash(project)
    if state.get("simulation_hash") != revision or state.get("build_hash") != revision:
        raise ValueError("当前源码/约束/脚本未通过同版本仿真与构建，拒绝下载。")
    artifact = state.get("artifact", {})
    generic = (project / "project.json").is_file()
    metadata = json.loads((project / "project.json").read_text(encoding="utf-8")) if generic else {}
    expected = ["design.bit"] + (["design.ltx"] if metadata.get("debug") else []) if generic else ["led_top.bit", "led_top.ltx"]
    if set(artifact) != set(expected):
        raise ValueError("产物清单与当前工程不匹配。")
    for name in expected:
        path = project / "build/vivado" / name
        if not path.is_file() or artifact.get(name) != file_hash(path):
            raise ValueError(f"下载产物缺失或被修改：{name}")
    return artifact


def _stop(process: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        import signal
        os.killpg(process.pid, signal.SIGKILL)
    process.wait()


def _execute(config: dict, stage: str, marker: str) -> dict:
    project = Path(config["project_path"])
    task = project.parent
    run = task / "runs" / f"{stage}-{time.time_ns()}"
    run.mkdir(parents=True)
    if stage == "simulation":
        # A compile failure must not feed a previous run's PASS log to the model.
        for old_log in (project / "build/sim").glob("*.log"):
            old_log.unlink()
    vivado = Path(config["vivado_path"]).resolve()
    if not vivado.is_file():
        raise ValueError(f"找不到 Vivado：{vivado}")
    script = project / "scripts" / config["scripts"][stage]
    args = [str(vivado), "-mode", "batch", "-source", str(script),
            "-log", str(run / "vivado.log"), "-journal", str(run / "vivado.jou")]
    if os.name == "nt":
        if any(re.search(r'[&|<>^%"\r\n]', a) for a in args):
            raise ValueError("本机批处理路径含不支持的 shell 特殊字符。")
        command = 'cmd.exe /d /s /c "' + subprocess.list2cmdline(args) + '"'
        options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
    else:
        command, options = args, {"start_new_session": True}
    env = os.environ.copy()
    env.pop("DEEPSEEK_API_KEY", None)
    env["FPGA_STEP_MS"] = str(config.get("step_ms", 500))
    started = time.monotonic()
    print(f"[{stage}] 开始，日志：{run}", flush=True)
    with (run / "console.log").open("w", encoding="utf-8") as output:
        process = subprocess.Popen(command, cwd=project, env=env, stdout=output,
                                   stderr=subprocess.STDOUT, **options)
        try:
            while True:
                remaining = config["tool_timeout_seconds"] - (time.monotonic() - started)
                if remaining <= 0:
                    _stop(process)
                    raise TimeoutError(f"{stage} 超时，已终止所属进程树；日志：{run}")
                try:
                    code = process.wait(timeout=min(15, remaining))
                    break
                except subprocess.TimeoutExpired:
                    print(f"[{stage}] 运行中 {int(time.monotonic()-started)} 秒", flush=True)
        except BaseException:
            if process.poll() is None:
                _stop(process)
            raise
    text = (run / "console.log").read_text(encoding="utf-8", errors="replace")
    passed = code == 0 and bool(re.search(marker, text, re.M))
    passed = passed and not re.search(r"^\s*(ERROR:|Error:|FATAL:|Fatal:)", text, re.M)
    # Preserve nested simulator errors before a repair overwrites the working build.
    extra = []
    log_files = [run / "console.log"]
    for path in sorted((project / "build/sim").glob("*.log")) if stage == "simulation" else []:
        shutil.copy2(path, run / path.name)
        log_files.append(run / path.name)
        extra.append(path.read_text(encoding="utf-8", errors="replace")[-6000:])
    errors = []
    for log in log_files:
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
        for index, line in enumerate(lines):
            if re.search(r"^\s*(ERROR:|Error:|FATAL:|Fatal:)", line):
                errors.append({"file": str(log), "line": index + 1, "text": line,
                               "context": "\n".join(lines[max(0, index-1):index+3])})
    result = {"stage": stage, "passed": bool(passed), "exit_code": code,
              "elapsed_seconds": round(time.monotonic()-started, 2),
              "logs": [str(p) for p in log_files], "errors": errors,
              "run_dir": str(run), "diagnostic": (text[-10000:] + "\n" + "\n".join(extra))[-24000:]}
    write_json(run / "result.json", result)
    print(f"[{stage}] {'PASS' if passed else 'FAIL'}", flush=True)
    return result


def run_simulation(config: dict) -> dict:
    marker = r"^AGENT_SIM_PASS\s*$" if config.get("generic") else r"^SIMULATION PASS: all checks completed\s*$"
    result = _execute(config, "simulation", marker)
    log = Path(result["run_dir"]) / "simulation.log"
    if log.is_file():
        text = log.read_text(encoding="utf-8", errors="replace")
        result["observations"] = {"pass_marker_found": bool(re.search(marker, text, re.M)),
            "termination_lines": [line for line in text.splitlines()
                if "$finish called" in line or re.match(r"\s*(Fatal:|ERROR:)", line)][:12]}
        write_json(Path(result["run_dir"]) / "result.json", result)
    return result


def run_build(config: dict) -> dict:
    result = _execute(config, "build", r"^BUILD PASS\s*$")
    out = Path(config["project_path"]) / "build/vivado"
    if result["passed"]:
        try:
            timing_path = out / "timing_summary.rpt"
            timing = timing_path.read_text(encoding="utf-8", errors="replace") if timing_path.exists() else ""
            row = re.search(r"WNS\(ns\).*?\n\s*-+[^\n]*\n\s*([^\n]+)", timing, re.S)
            values = row.group(1).split() if row else []
            metrics_path = out / "checks.json"
            metrics = json.loads(metrics_path.read_text()) if metrics_path.exists() else {}
            if config.get("generic"):
                metadata = json.loads((Path(config["project_path"]) / "project.json").read_text(encoding="utf-8"))
                names = ["design.bit"] + (["design.ltx"] if metadata.get("debug") else [])
            else:
                names = ["led_top.bit", "led_top.ltx"]
            artifact = {name: file_hash(out / name) for name in names}
            result.update(artifact=artifact, checks=metrics)
            try:
                result["timing"] = {"wns_ns": float(values[0]), "whs_ns": float(values[4]), "wpws_ns": float(values[8])}
            except (ValueError, IndexError):
                result["timing"] = {"parse_status": "unavailable; inspect raw timing report"}
            result["reports"] = {p.name: p.read_text(encoding="utf-8", errors="replace") for p in out.glob("*.rpt")}

            for path in out.glob("*.rpt"):
                shutil.copy2(path, Path(result["run_dir"]) / path.name)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            result.update(passed=False, diagnostic=str(exc) + "\n" + result["diagnostic"])
    write_json(Path(result["run_dir"]) / "result.json", result)
    return result


def probe_hardware(config: dict) -> dict:
    return _execute(config, "probe", r"^PROBE PASS\s*$")


def program_device(config: dict, state: dict) -> dict:
    project = Path(config["project_path"])
    verify_artifact(project, state)
    with lock(Path(config["workspace_path"]) / "hardware.lock"):
        verify_artifact(project, state)
        marker = r"^PROGRAM PASS\s*$" if config.get("generic") else r"^HARDWARE LOGIC PASS:"
        result = _execute(config, "program", marker)
        if config.get("generic"):
            result["hardware_verified"] = False
            if result["passed"]:
                metadata = json.loads((project / "project.json").read_text(encoding="utf-8"))
                debug = metadata.get("debug")
                if debug:
                    try:
                        source = project / "build/hardware_samples.csv"
                        with source.open(encoding="utf-8") as f:
                            rows = list(csv.DictReader(f))
                        if len(rows) != debug.get("samples", 30):
                            raise ValueError("VIO采样数量不完整。")
                        times = [int(row["elapsed_ms"]) for row in rows]
                        if not times or any(b <= a for a, b in zip(times, times[1:])):
                            raise ValueError("VIO采样时间戳不完整或非递增。")
                        if times[-1] - times[0] < debug.get("min_duration_ms", 0):
                            raise ValueError("VIO实际采样跨度未达到min_duration_ms。")
                        checked = 0
                        for probe in debug["probes"]:
                            values = [int(row[probe["name"]], 16) for row in rows]
                            if any(v >= 2**probe["width"] for v in values):
                                raise ValueError("探针值超出位宽。")
                            allowed = probe.get("allowed_values", [])
                            if allowed and any(v not in allowed for v in values):
                                raise ValueError(f"{probe['name']} 出现不允许的值。")
                            changes = sum(a != b for a, b in zip(values, values[1:]))
                            if changes < probe.get("min_changes", 0):
                                raise ValueError(f"{probe['name']} 切换次数不足。")
                            checked += bool(allowed or probe.get("min_changes"))
                        shutil.copy2(source, Path(result["run_dir"]) / source.name)
                        result.update(samples=len(rows), duration_ms=times[-1] - times[0],
                                      hardware_verified=bool(checked), checked_probes=checked)
                    except (OSError, ValueError, KeyError) as exc:
                        result.update(passed=False, diagnostic=str(exc))
            write_json(Path(result["run_dir"]) / "result.json", result)
            return result
        if result["passed"]:
            try:
                source = project / "build/hardware_samples.csv"
                rows = list(csv.DictReader(source.open(encoding="utf-8")))
                values = [int(row["led_n_hex"], 16) for row in rows]
                transitions = sum(a != b for a, b in zip(values, values[1:]))
                if len(rows) < 20 or set(values) != {1, 2} or transitions < 4:
                    raise ValueError("实板采样不完整或切换次数不足。")
                if any(int(row["reset_hex"], 16) for row in rows):
                    raise ValueError("实板复位未释放。")
                shutil.copy2(source, Path(result["run_dir"]) / source.name)
                result.update(samples=len(rows), transitions=transitions)
            except (OSError, ValueError, KeyError) as exc:
                result.update(passed=False, diagnostic=str(exc))
        write_json(Path(result["run_dir"]) / "result.json", result)
        return result

"""One execution contract shared by planning, file delivery and project assembly."""
import re
from pathlib import PurePosixPath

IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
PORT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\[[0-9]+\])?$")
FILE_TYPES = {"rtl": (".sv", ".v"), "sim": (".sv", ".v"), "scripts": (".py",),
              "data": (".hex", ".mem", ".txt", ".json", ".csv")}
SAMPLE_LIMITS = (("sample_ms", 100, 10, 1000), ("samples", 30, 10, 300))


def validate_spec(spec):
    if not isinstance(spec, dict) or not isinstance(spec.get("summary"), str):
        raise ValueError("spec must contain summary, requirements and acceptance")
    for key in ("requirements", "acceptance"):
        if not isinstance(spec.get(key), list) or not 1 <= len(spec[key]) <= 30 or any(
                not isinstance(x, str) or not x.strip() for x in spec[key]):
            raise ValueError(f"spec.{key} must contain 1..30 nonempty strings")


def validate_requirements(analysis):
    validate_spec(analysis.get("spec"))
    if analysis.get("target") not in {"sim", "build"} or any(type(analysis.get(k)) is not bool for k in ("program", "visualization")):
        raise ValueError("Requirements must specify target, program and visualization")
    if analysis["program"] and analysis["target"] != "build":
        raise ValueError("Programming requires a build target")


def file_path(name):
    if not isinstance(name, str):
        raise ValueError("文件路径必须是字符串。")
    p = PurePosixPath(name)
    allowed = name == "README.md" or (len(p.parts) == 2 and p.suffix in FILE_TYPES.get(p.parts[0], ()))
    if not re.fullmatch(r"[A-Za-z0-9_./-]+", name) or ".." in p.parts or p.is_absolute() or not allowed or p.stem.startswith("agent_"):
        raise ValueError(f"非法工程文件路径：{name}")
    return name


def validate_tasks(items):
    if not isinstance(items, list) or not 1 <= len(items) <= 24:
        raise ValueError("development_tasks需要1至24个小任务。")
    known, owners = set(), set()
    for item in items:
        if not isinstance(item, dict) or not re.fullmatch(r"[A-Za-z0-9_-]+", str(item.get("id", ""))) or item["id"] in known:
            raise ValueError("开发任务id必须唯一。")
        if item.get("kind", "files") not in {"files", "python"}:
            raise ValueError("任务kind只能为files或python。")
        if not isinstance(item.get("depends_on", []), list) or any(d not in known for d in item.get("depends_on", [])):
            raise ValueError("任务依赖必须指向清单中更早的任务，不能有环。")
        for key in ("goal", "completion"):
            if not isinstance(item.get(key), str) or not item[key].strip():
                raise ValueError(f"任务缺少{key}。")
        for key in ("inputs", "outputs"):
            if not isinstance(item.get(key, []), list):
                raise ValueError(f"{key}必须是路径列表。")
            for name in item.get(key, []):
                file_path(name)
        outputs = item.get("outputs", [])
        if not 1 <= len(outputs) <= 3 or len(set(outputs)) != len(outputs) or owners.intersection(outputs):
            raise ValueError("每个任务应声明1至3个独占输出文件。")
        if any(name not in owners for name in item.get("inputs", [])):
            raise ValueError("任务输入必须由更早的任务交付。")
        if item.get("kind") == "python":
            if item.get("script") not in item.get("inputs", []) or not item["script"].startswith("scripts/"):
                raise ValueError("Python任务script必须是已交付的输入脚本。")
            if any(not name.startswith("data/") for name in outputs):
                raise ValueError("预处理Python任务只能声明data/输出。")
        owners.update(outputs)
        known.add(item["id"])
    return items


def project_fields(project, board, target):
    if not isinstance(project, dict):
        raise ValueError("project must be an object")
    for key in ("top", "testbench"):
        if not isinstance(project.get(key), str) or not IDENT.fullmatch(project[key]) or project[key].startswith("agent_"):
            raise ValueError(f"无效的 {key} 模块名。")
    timeout = project.get("simulation_timeout_ns", 100000)
    if type(timeout) is not int or not 100 <= timeout <= 100000000:
        raise ValueError("simulation_timeout_ns范围为100至100000000。")
    bindings = project.get("bindings") or {}
    if not isinstance(bindings, dict) or any(not PORT.fullmatch(p) for p in bindings):
        raise ValueError("bindings必须是顶层端口名到板卡资源名的映射。")
    if any(not isinstance(r, str) or r not in board["resources"] for r in bindings.values()):
        raise ValueError("使用了未核实的板卡资源；请询问用户并补充boards配置，不能猜引脚。")
    if len(set(bindings.values())) != len(bindings):
        raise ValueError("多个端口不能占用同一个板卡资源。")
    for resource in bindings.values():
        pair = board["resources"][resource].get("pair")
        if pair and pair not in bindings.values():
            raise ValueError("差分时钟必须同时绑定P和N。")
    if target == "build" and not any(board["resources"][r].get("period_ns") for r in bindings.values()):
        raise ValueError("上板工程必须绑定已核实的板载时钟。纯逻辑仿真可使用--target sim。")
    debug = project.get("debug")
    if debug is not None:
        if not isinstance(debug, dict) or not IDENT.fullmatch(str(debug.get("clock", ""))):
            raise ValueError("debug.clock必须是顶层内的时钟信号名。")
        probes = debug.get("probes")
        if not isinstance(probes, list) or not 1 <= len(probes) <= 16:
            raise ValueError("debug.probes需要1至16个信号。")
        names = set()
        for probe in probes:
            if not isinstance(probe, dict):
                raise ValueError("每个debug探针必须是对象。")
            name, width = probe.get("name"), probe.get("width")
            if not isinstance(name, str) or not IDENT.fullmatch(name) or name in names or type(width) is not int or not 1 <= width <= 32:
                raise ValueError("debug探针必须为不重复的信号名，宽度1至32。")
            names.add(name)
            values = probe.get("allowed_values", [])
            if not isinstance(values, list) or any(type(v) is not int or not 0 <= v < 2**width for v in values):
                raise ValueError("探针allowed_values超出位宽。")
            changes = probe.get("min_changes", 0)
            if type(changes) is not int or not 0 <= changes <= 100:
                raise ValueError("min_changes必须为0至100。")
        for key, default, low, high in SAMPLE_LIMITS:
            v = debug.get(key, default)
            if type(v) is not int or not low <= v <= high:
                raise ValueError(f"debug.{key}超出范围。")
        duration = debug.get("min_duration_ms", 0)
        if type(duration) is not int or not 0 <= duration <= 60000:
            raise ValueError("debug.min_duration_ms must be an integer in 0..60000")
        if (debug.get("samples", 30) - 1) * debug.get("sample_ms", 100) < duration:
            raise ValueError("debug samples and sample_ms do not cover min_duration_ms")
    return {"top": project["top"], "testbench": project["testbench"],
            "simulation_timeout_ns": timeout, "bindings": bindings, "debug": debug}


def script_fields(project, files):
    result = {}
    for key in ("analysis_script", "hardware_analysis"):
        value = project.get(key)
        script = value.get("script") if key == "hardware_analysis" and isinstance(value, dict) else value
        if value is not None:
            if not isinstance(script, str) or script not in files or not script.startswith("scripts/") or not script.endswith(".py"):
                raise ValueError(f"{key} must reference a delivered scripts/*.py file")
            if key == "hardware_analysis":
                outputs = value.get("outputs") if isinstance(value, dict) else None
                if not isinstance(outputs, list) or not outputs or any(
                        not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*\.(png|svg|md|json|csv|txt)", name) for name in outputs) or len(set(outputs)) != len(outputs):
                    raise ValueError("hardware_analysis.outputs must list distinct flat artifact filenames")
        result[key] = value
    return result


def describe():
    """Machine-readable capabilities derived from the same execution rules."""
    return {"file_types": FILE_TYPES, "extra_file": "README.md", "max_tasks": 24,
            "max_files_per_task": 3, "max_project_files": 32,
            "framework_owned": ["XDC/Tcl generation from bindings", "VIO injection from debug",
                                "VCD export at build/sim/waveform.vcd", "normal simulation finish after testbench.done"],
            "project": {"top": "module identifier, not file path", "testbench": "module identifier, not file path",
                "bindings": "top port to verified board resource; build requires board clock",
                "debug": "null or {clock,probes,sample_ms,samples,min_duration_ms}; required for VIO sampling",
                "analysis_script": "optional scripts/*.py: --input real VCD --output artifact directory",
                "hardware_analysis": "optional {script:scripts/*.py,outputs:[artifact filenames]}; --input real VIO CSV --output artifact directory"},
            "hardware_analysis_requires": ["program=true", "debug configured", "successful current programming and sampling"],
            "vio_sampling_limits": {k: {"default": d, "min": lo, "max": hi} for k, d, lo, hi in SAMPLE_LIMITS},
            "hardware_csv": "elapsed_ms decimal timestamp; each probe column contains hexadecimal values",
            "unsupported": ["model-generated XDC/Tcl or arbitrary shell commands", "automatic physical LED light verification"]}


def plan_errors(plan, confirmed, board):
    """Check execution interfaces only, never infer HDL or algorithm correctness."""
    errors = []
    if not isinstance(plan, dict):
        return ["Plan must be an object"]
    for key in ("spec", "target", "program", "visualization"):
        if plan.get(key) != confirmed.get(key):
            errors.append(f"Plan changed confirmed requirements: {key}")
    if plan.get("action") != "plan":
        errors.append("action must be plan or clarify")
    for key in ("design", "test_plan"):
        if not isinstance(plan.get(key), str) or not plan[key].strip():
            errors.append(f"{key} must be nonempty text")
    items = plan.get("development_tasks")
    try:
        validate_tasks(items)
    except (ValueError, TypeError, KeyError) as exc:
        errors.append(str(exc))
    files = set()
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            for name in item.get("outputs", []) if isinstance(item.get("outputs"), list) else []:
                try:
                    file_path(name)
                    files.add(name)
                except ValueError as exc:
                    errors.append(str(exc))
    if "README.md" not in files or not any(name.startswith("rtl/") for name in files) or not any(name.startswith("sim/") for name in files):
        errors.append("Tasks must deliver RTL, testbench and README.md")
    if len(files) > 32:
        errors.append("Project exceeds 32 files")
    if len({name.casefold() for name in files}) != len(files):
        errors.append("Project filenames conflict ignoring case")
    project = plan.get("project")
    if not isinstance(project, dict):
        return errors + ["project must be an object"]
    checked = dict(project)
    for key in ("top", "testbench"):
        value = project.get(key)
        if not isinstance(value, str) or not IDENT.fullmatch(value) or value.startswith("agent_"):
            errors.append(f"project.{key} must be a module identifier, not a file path")
            checked[key] = "placeholder"
    try:
        project_fields(checked, board, confirmed["target"])
    except (ValueError, TypeError, KeyError) as exc:
        errors.append(str(exc))
    try:
        script_fields(project, files)
    except (ValueError, TypeError) as exc:
        errors.append(str(exc))
    if project.get("hardware_analysis") and (not plan.get("program") or not project.get("debug")):
        errors.append("hardware_analysis requires program=true and debug/VIO probes")
    return list(dict.fromkeys(errors))

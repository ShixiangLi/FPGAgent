"""Validate generated projects and render trusted build metadata and constraints."""
import json
from pathlib import Path, PurePosixPath
import re
import shutil

from .tools import write_json

IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
PORT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\[[0-9]+\])?$")


def board_profile(config: dict) -> dict:
    path = Path(config["root"]) / "boards" / (config["board"] + ".json")
    return json.loads(path.read_text(encoding="utf-8"))


def validate(response: dict, board: dict, target: str = "build") -> tuple[dict, dict]:
    spec = response.get("spec")
    if not isinstance(spec, dict) or not isinstance(spec.get("summary"), str):
        raise ValueError("spec 必须包含 summary、requirements、acceptance。")
    for key in ("requirements", "acceptance"):
        if not isinstance(spec.get(key), list) or not 1 <= len(spec[key]) <= 30 or any(
                not isinstance(x, str) or not x.strip() for x in spec[key]):
            raise ValueError(f"spec.{key} 必须是非空字符串列表。")
    project = response.get("project")
    if not isinstance(project, dict):
        raise ValueError("缺少 project 对象。")
    for key in ("top", "testbench"):
        if not isinstance(project.get(key), str) or not IDENT.fullmatch(project[key]) or project[key].startswith("agent_"):
            raise ValueError(f"无效的 {key} 模块名。")
    files = project.get("files")
    if not isinstance(files, dict) or not 2 <= len(files) <= 32:
        raise ValueError("files 必须包含2至32个RTL/仿真文件，可附README.md。")
    modules = {}
    folded = set()
    for name, content in files.items():
        path = PurePosixPath(name)
        if not re.fullmatch(r"[A-Za-z0-9_./-]+", name) or path.is_absolute() or ".." in path.parts:
            raise ValueError(f"文件路径越界或非法：{name}")
        if name.casefold() in folded:
            raise ValueError("文件名存在大小写冲突。")
        if path.stem.startswith("agent_"):
            raise ValueError("文件名使用框架保留前缀。")
        folded.add(name.casefold())
        if not isinstance(content, str) or not content.strip() or len(content) > 160000:
            raise ValueError(f"文件内容无效：{name}")
        if name == "README.md":
            continue
        if len(path.parts) == 2 and path.parts[0] == "scripts" and path.suffix == ".py":
            continue
        if len(path.parts) == 2 and path.parts[0] == "data" and path.suffix in {".hex", ".mem", ".txt", ".json", ".csv"}:
            continue
        if len(path.parts) != 2 or path.parts[0] not in {"rtl", "sim"} or path.suffix not in {".v", ".sv"}:
            raise ValueError("模型只能生成rtl/、sim/中的Verilog/SystemVerilog及README.md。")
        # Locate modules only to assemble optional instrumentation, never to judge HDL.
        clean = re.sub(r'"(?:\\.|[^"\\])*"|/\*.*?\*/|//[^\n]*', lambda m: " " * len(m.group()), content, flags=re.S)
        for module in re.findall(r"\bmodule\s+(\w+)", clean):
            modules.setdefault(module, name)
    if len("".join(files.values())) > 600000:
        raise ValueError("工程文件总量过大。")
    if not files.get("README.md"):
        raise ValueError("完整工程必须包含README.md说明和验收步骤。")
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
        for key, default, low, high in (("sample_ms", 100, 10, 1000), ("samples", 30, 10, 300)):
            v = debug.get(key, default)
            if type(v) is not int or not low <= v <= high:
                raise ValueError(f"debug.{key}超出范围。")
    normalized = {"top": project["top"], "testbench": project["testbench"],
                  "simulation_timeout_ns": timeout, "bindings": bindings, "debug": debug,
                  "files": files, "top_file": modules.get(project["top"])}
    script = project.get("analysis_script")
    if script is not None and (script not in files or not script.startswith("scripts/") or not script.endswith(".py")):
        raise ValueError("analysis_script必须指向files中的scripts/*.py文件。")
    normalized["analysis_script"] = script
    return spec, normalized


def materialize(task: Path, config: dict, spec: dict, design: dict) -> dict:
    root = task / "project"
    root.mkdir(exist_ok=True)
    # Remove only previous generated source files under this exact task workspace.
    for folder in ("rtl", "sim", "pins", "scripts", "data"):
        path = root / folder
        if path.exists():
            if path.is_symlink() or path.resolve().parent != root.resolve():
                raise ValueError("任务目录包含异常链接。")
            shutil.rmtree(path)
        path.mkdir()
    for name, text in design["files"].items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    board = board_profile(config)
    metadata = {k: v for k, v in design.items() if k != "files"}
    metadata.update(format_version=2, board=board, spec=spec, target=config.get("target", "build"))
    write_json(root / "project.json", metadata)
    write_json(root / "spec.json", spec)
    constraints = []
    for port, resource in design["bindings"].items():
        info = board["resources"][resource]
        obj = f"[get_ports {{{port}}}]"
        constraints += [f"set_property PACKAGE_PIN {info['pin']} {obj}", f"set_property IOSTANDARD {info['standard']} {obj}"]
        if info.get("pullup"):
            constraints.append(f"set_property PULLUP true {obj}")
        if info.get("period_ns"):
            constraints.append(f"create_clock -name board_clock -period {info['period_ns']} {obj}")
        elif info["direction"] == "input" and resource == "pl_key":
            constraints.append(f"set_false_path -from {obj}")
        if info["direction"] == "output" and resource in {"pl_led1", "pl_led2"}:
            constraints += [f"set_property SLEW SLOW {obj}", f"set_property DRIVE 8 {obj}", f"set_false_path -to {obj}"]
    (root / "pins/board.xdc").write_text("\n".join(constraints) + "\n", encoding="utf-8")
    harness = f'''`timescale 1ns/1ps
module agent_sim;
wire done;
{design['testbench']} test_instance(.done(done));
initial begin
  wait(done === 1'b1);
  #1;
  $display("AGENT_SIM_PASS");
  $finish;
end
initial begin
  #{design['simulation_timeout_ns']};
  $fatal(1, "Agent simulation timeout: testbench did not finish");
end
endmodule
'''
    (root / "sim/agent_sim.sv").write_text(harness, encoding="utf-8")
    debug = design["debug"]
    if debug:
        connections = [f".clk({debug['clock']})"] + [f".probe_in{i}({p['name']})" for i, p in enumerate(debug['probes'])]
        insertion = "\n`ifdef HARDWARE_DEBUG\nagent_vio agent_monitor(" + ",".join(connections) + ");\n`endif\n"
        if not design["top_file"]:
            raise ValueError("Cannot locate top module for VIO insertion; set debug=null or provide the top module.")
        top = root / design["top_file"]
        text = top.read_text(encoding="utf-8")
        # Mask comments while retaining offsets before locating the actual endmodule.
        masked = re.sub(r"/\*.*?\*/|//[^\n]*", lambda m: " " * len(m.group()), text, flags=re.S)
        module = re.search(r"\bmodule\s+" + re.escape(design["top"]) + r"\b.*?\bendmodule\b", masked, re.S)
        if module is None:
            raise ValueError("Cannot locate top endmodule for VIO insertion.")
        index = module.end() - len("endmodule")
        top.write_text(text[:index] + insertion + text[index:], encoding="utf-8")
    scripts = {"simulation": "generic_sim.tcl", "build": "generic_build.tcl", "probe": "probe.tcl", "program": "generic_program.tcl"}
    for name in scripts.values():
        shutil.copy2(Path(config["root"]) / "templates" / name, root / "scripts" / name)
    # Small Tcl data file written by the controller, never arbitrary LLM Tcl.
    lines = [f"set agent_top {{{design['top']}}}", f"set agent_part {{{board['part']}}}",
             "set agent_ports {" + " ".join(design["bindings"]) + "}",
             "set agent_directions {" + " ".join(board['resources'][r]['direction'] for r in design['bindings'].values()) + "}",
             f"set agent_debug {1 if debug else 0}"]
    if debug:
        lines += ["set agent_probe_names {" + " ".join(p['name'] for p in debug['probes']) + "}",
                  "set agent_probe_widths {" + " ".join(str(p['width']) for p in debug['probes']) + "}",
                  f"set agent_samples {debug.get('samples', 30)}", f"set agent_sample_ms {debug.get('sample_ms', 100)}"]
    (root / "scripts/project_data.tcl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {**config, "project_path": str(root), "scripts": scripts, "generic": True,
            "analysis_script": design.get("analysis_script")}

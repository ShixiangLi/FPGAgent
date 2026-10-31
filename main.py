"""Interactive FPGA development and board-document research CLI. Python 3.10+."""
import argparse
import json
import os
import sys
from pathlib import Path

from fpga_agent import analysis_session, board_research, session, workflow
from fpga_agent.agent import DeepSeekClient, load_env
from fpga_agent.interaction import read_reply
from fpga_agent.prompts.catalog import HEALTH_CHECK


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="DeepSeek 驱动的多文件 FPGA 工程开发智能体")
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config.json"))
    commands = parser.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor", help="检查环境，不启动构建")
    doctor.add_argument("--llm", action="store_true", help="额外发起一次真实模型连通性请求")
    run = commands.add_parser("run", help="自然语言需求 -> RTL -> 仿真 -> 构建")
    run.add_argument("--request", help="开发需求；续接时修改整体需求，回答开发澄清请使用--continue")
    run.add_argument("--resume", help="继续或修改已有通用任务")
    run.add_argument("--continue", dest="continue_existing", action="store_true", help="配合--resume恢复已保存的小任务，不重新规划")
    run.add_argument("--program", action="store_true", help="上板意图的默认提示；最终以任务需求和规划为准")
    run.add_argument("--target", choices=("sim", "build"), default="build", help="初始目标提示；用户后续要求可动态改变")
    run.add_argument("--board", help="新任务选择已确认安装的boards配置id；续接沿用任务板卡")
    run.add_argument("--source", action="append", default=[], help="本任务参考文件或URL，可重复指定；续接时可补充")
    run.add_argument("--effort", choices=("low", "high", "max"), help="本任务思考强度，默认读取config.json")
    run.add_argument("--non-interactive", action="store_true", help="需要用户回答时保存并暂停")
    program = commands.add_parser("program", help="下载已有任务的已验证产物")
    program.add_argument("task_id")
    acceptance = commands.add_parser("accept", help="记录用户对当前上板版本的现场验收")
    acceptance.add_argument("task_id")
    group = acceptance.add_mutually_exclusive_group(required=True)
    group.add_argument("--passed", action="store_true")
    group.add_argument("--failed", action="store_true")
    acceptance.add_argument("--note", required=True)
    status = commands.add_parser("status", help="查看任务状态及报告位置")
    status.add_argument("task_id")
    diagnosis = commands.add_parser("diagnose", help="让DeepSeek解释已有失败，不修改代码或下载")
    diagnosis.add_argument("task_id")
    analysis = commands.add_parser("analyze", help="由DeepSeek决定如何分析已有CSV/VCD；不修改RTL或重新下载")
    analysis.add_argument("task_id")
    analysis.add_argument("--candidate", help="任务内基线candidate JSON相对路径")
    analysis.add_argument("--source", help="任务内真实CSV/VCD相对路径")
    analysis.add_argument("--script", help="candidate内的scripts/*.py")
    analysis.add_argument("--request", help="本轮分析范围与用户要求")
    analysis.add_argument("--resume", help="继续已有分析会话ID")
    analysis.add_argument("--answer", help="回答该分析会话的待澄清问题")
    analysis.add_argument("--non-interactive", action="store_true", help="需要用户回答时保存并退出")
    verify = commands.add_parser("verify", help="重新验证现有通用工程，不调用模型重新生成代码")
    verify.add_argument("task_id")
    verify.add_argument("--effort", choices=("low", "high", "max"))
    verify.add_argument("--candidate", help="复核任务内某个candidate JSON，避免重新生成代码")
    verify.add_argument('--continue', dest='continue_existing', action='store_true', help='继续暂停的审查/修复阶段，复用未变更源码的工具证据')
    research = commands.add_parser("board-research", help="自主搜索、读取手册并生成待确认板卡配置")
    research.add_argument("--board", required=True, help="板卡配置id，例如axu15egb")
    research.add_argument("--request", default="", help="型号、PCB版本、需要核实的资源和资料要求")
    research.add_argument("--source", action="append", default=[], help="本地PDF/文本或官方URL，可重复指定")
    research.add_argument("--resume", help="继续已有板卡资料任务")
    research.add_argument("--non-interactive", action="store_true", help="需要确认时保存并退出")
    for command in (run, verify, analysis, research):
        command.add_argument('--max-tokens', type=int, help='本次运行的模型输出预算，由用户显式选择，不改写config.json')
    for command in (analysis, research):
        command.add_argument('--effort', choices=('low', 'high', 'max'), help='本次运行的思考强度')
    args = parser.parse_args(argv)
    try:
        config = workflow.load_config(args.config)
        load_env(Path(config["root"]) / ".env")
        if getattr(args, 'max_tokens', None) is not None:
            if args.max_tokens < 1:
                raise ValueError('--max-tokens必须为正整数。')
            config['llm']['max_tokens'] = args.max_tokens
        if getattr(args, 'effort', None):
            config['llm']['reasoning_effort'] = args.effort
        if args.command == "doctor":
            checks = {"vivado": Path(config["vivado_path"]).is_file(),
                      "api_key_present": bool(os.environ.get("DEEPSEEK_API_KEY")),
                      "model": config["llm"]["model"], "thinking": config["llm"]["thinking"],
                      "reasoning_effort": config["llm"]["reasoning_effort"]}
            checks["proxy_mode"] = config["llm"].get("proxy_mode", "system")
            import importlib.util
            checks["python_plotting"] = bool(importlib.util.find_spec("matplotlib"))
            checks["pdf_reading"] = bool(importlib.util.find_spec("pymupdf"))
            checks.update(workflow.environment_checks(config))
            print(json.dumps(checks, ensure_ascii=False, indent=2))
            if args.llm:
                response = DeepSeekClient(config).complete([
                    {"role": "user", "content": HEALTH_CHECK}
                ], Path(config["workspace_path"]) / "connection.jsonl")
                if response.get("ready") is not True:
                    raise ValueError("模型连接返回内容不符合预期。")
                print("[LLM] 真实连接通过；调用元数据已记录，未保存密钥或原始思考文本。")
            return 0 if all(checks[k] for k in ("vivado", "api_key_present", "board_profile", "templates")) else 1
        if args.command == "board-research":
            state = board_research.run(config, args.board, args.request, args.source,
                                       ask=None if args.non_interactive else input, resume=args.resume)
        elif args.command == "run":
            if args.board:
                config["board"] = args.board
            config["target"] = args.target
            config["program_default"] = args.program
            request = args.request
            if args.continue_existing and (not args.resume or request):
                raise ValueError("--continue需要--resume且不能同时提供新需求；修改需求请使用--request。")
            if args.continue_existing:
                request = ""
            if not request and not args.continue_existing:
                if args.non_interactive:
                    raise ValueError("非交互运行必须提供 --request。")
                request = input("请描述FPGA开发需求：").strip()
            if not request and not args.continue_existing:
                raise ValueError("需求不能为空。")
            state = session.run(request, config, ask=None if args.non_interactive else input,
                                task_id=args.resume, continue_existing=args.continue_existing, sources=args.source)
            if state["status"] == "awaiting_acceptance" and not args.non_interactive:
                automated = state.get("results", [{}])[-1].get("stage") == "hardware_analysis"
                prompt = ("请核对硬件采样分析报告，是否接受自动验证结果？[y接受/n不接受，不代表目视验收]"
                          if automated else "按本任务的验收条件，现场功能正常？[y正常/n异常]")
                answer = read_reply(Path(config["workspace_path"]) / state["task_id"], input,
                                    prompt).lower()
                if answer in {"y", "n"}:
                    note = ("用户接受自动采样分析结果（未进行目视验收）" if automated else "用户按本任务验收条件确认功能正常") if answer == "y" else "用户反馈异常"
                    state = workflow.accept(state["task_id"], config, answer == "y",
                                            note)
        elif args.command == "program":
            state = workflow.deploy(args.task_id, config)
        elif args.command == "accept":
            state = workflow.accept(args.task_id, config, args.passed, args.note)
        elif args.command == "analyze":
            state = analysis_session.run(args.task_id, config, candidate=args.candidate, source=args.source,
                                         script=args.script, request=args.request, resume=args.resume, answer=args.answer)
            while state["status"] == "waiting_for_user" and not args.non_interactive:
                folder = Path(state["report"]).parent
                reply = read_reply(folder, input, state.get("question") or "请补充")
                if not reply:
                    break
                state = analysis_session.run(args.task_id, config, resume=state["analysis_id"], answer=reply)
            print(json.dumps({k: state.get(k) for k in ("analysis_id", "status", "assessment", "question", "feedback", "model_pause", "report")}, ensure_ascii=False, indent=2))
            if state["status"] in {"waiting_for_user", "paused", "model_paused", "interrupted"}:
                return 2
            return 0 if state["status"] == "reviewed" and state.get("assessment", {}).get("approved") else 1
        elif args.command == "diagnose":
            state = workflow.diagnose(args.task_id, config)
        elif args.command == "verify":
            state = workflow.verify_task(args.task_id, config, args.candidate, args.continue_existing)
        else:
            task = workflow.task_path(config, args.task_id)
            state = json.loads((task / "state.json").read_text(encoding="utf-8"))
        print(json.dumps({"task_id": state["task_id"], "status": state["status"],
                          "spec": state.get("spec"), "error": state.get("error"),
                          "pending_confirmation": state.get("pending_confirmation"),
                          'model_pause': state.get('model_pause'),
                          "report": str(Path(config["workspace_path"]) / state["task_id"] / "report.md")},
                         ensure_ascii=False, indent=2))
        if state["status"] in {"waiting_for_user", "awaiting_confirmation", "research_paused", "development_paused", 'model_paused'}:
            return 2
        return 0 if state["status"] in {"simulation_verified", "ready_to_program", "awaiting_acceptance", "completed"} else 1
    except KeyboardInterrupt:
        print("\n已中断；所属工具进程已清理，任务记录保留。", file=sys.stderr)
        return 130
    except (ValueError, RuntimeError, OSError, KeyError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

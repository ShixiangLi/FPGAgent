"""Durable task documents and compact model context."""
import json
import uuid
from datetime import datetime
from pathlib import Path

from .tools import write_json


def event(task, phase, message):
    entry = {"time": datetime.now().astimezone().isoformat(), "phase": phase, "message": message}
    with (task / "events.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    print(f"[{phase}] {message}", flush=True)


def document(task, name, text):
    path = task / name
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return
    history = task / "history"
    history.mkdir(exist_ok=True)
    if path.exists():
        (history / f"{uuid.uuid4().hex}-{name}").write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    path.write_text(text, encoding="utf-8")


def sync(task, state):
    plan = state.get("plan", {})
    spec = state.get("spec", plan.get("spec", {}))
    original = state.get("request", "").split("\n用户", 1)[0]
    document(task, "requirements.md", "# 当前需求\n\n## 初始目标\n" + original[:6000]
             + "\n\n## 最新用户输入\n" + state.get("latest_user_input", "")[:3000]
             + "\n\n## 当前有效规格（后续用户修改优先）\n" + json.dumps(spec, ensure_ascii=False, indent=2)
             + "\n\n## 待澄清\n" + (state.get("question") or "无") + "\n")
    document(task, "design.md", "# 当前方案\n\n" + plan.get("design", state.get("message", "尚未生成"))
             + "\n\n## 执行计划\n" + json.dumps(plan, ensure_ascii=False, indent=2) + "\n")
    document(task, "test_plan.md", "# 当前测试方案\n\n" + plan.get("test_plan", "等待需求确认后生成。") + "\n")
    recent = [{k: r[k] for k in ("stage", "passed", "run_dir", "artifacts", "waveform_summary", "errors", "timing") if k in r}
              for r in state.get("results", [])[-5:]]
    progress = {"status": state["status"], "recent_results": recent,
                "error": state.get("error", "")[-3000:], "acceptance": state.get("acceptance"),
                "plan_contract_errors": state.get("plan_contract_errors", []),
                "pending_confirmation": state.get("pending_confirmation"), "confirmations": state.get("confirmations", {})}
    document(task, "progress.md", "# 当前进度\n\n" + json.dumps(progress, ensure_ascii=False, indent=2) + "\n")
    document(task, "tasks.md", "# 开发任务清单\n\n交付文件不等于功能验证通过；集成验证结果另行记录。\n\n"
             + json.dumps({"tasks": plan.get("development_tasks", []), "current": state.get("current_work_item"),
                           "results": state.get("work_items", {})}, ensure_ascii=False, indent=2) + "\n")
    write_json(task / "task.json", {"task_id": task.name, "status": state["status"], "plan": plan,
               "documents": ["requirements.md", "design.md", "test_plan.md", "tasks.md", "progress.md"]
                            + [name for name in ("research.md", "working.md") if (task / name).is_file()],
               "confirmations": state.get("confirmations", {}), "pending_confirmation": state.get("pending_confirmation"),
               "work_items": state.get("work_items", {}), "current_work_item": state.get("current_work_item"),
               "artifacts": [str(p.relative_to(task)) for folder in ("artifacts",)
                             for p in (task / folder).rglob("*") if p.is_file()]})


def context(task):
    return {name: (task / name).read_text(encoding="utf-8")[:14000]
            for name in ("requirements.md", "design.md", "test_plan.md", "progress.md", "research.md") if (task / name).is_file()}


def work_context(task):
    """Keep confirmed inputs exact; omit the full task list and old progress logs."""
    path = task / "state.json"
    if not path.is_file():
        return context(task)
    state = json.loads(path.read_text(encoding="utf-8"))
    plan = state.get("plan", {})
    return {"confirmed_requirements": state.get("requirements_analysis") or {"spec": state.get("spec")},
            "approved_design": plan.get("design", ""), "approved_test_plan": plan.get("test_plan", ""),
            "project_contract": plan.get("project", {}),
            "research.md": (task / "research.md").read_text(encoding="utf-8")[:14000] if (task / "research.md").is_file() else ""}

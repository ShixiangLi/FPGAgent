"""Bounded research loop; install a cited board profile only after explicit review."""
import json
from pathlib import Path
import re
import uuid
from datetime import datetime

from .agent import DeepSeekClient
from .confirmation import confirm
from .documents import Documents
from .interaction import read_reply
from .prompts.catalog import BOARD_RESEARCH
from . import task_memory as memory, tools
from . import research


def validate_candidate(candidate, board_id, documents):
    profile = candidate.get("profile")
    if candidate.get("unresolved") != []:
        raise ValueError("Resolve uncertainties before proposing an installable profile")
    if not isinstance(profile, dict) or profile.get("id") != board_id:
        raise ValueError("profile.id must match requested board_id")
    if not isinstance(profile.get("part"), str) or not re.fullmatch(r"[A-Za-z0-9_-]+", profile["part"]):
        raise ValueError("profile.part must be a Vivado part identifier")
    resources = profile.get("resources")
    if not isinstance(resources, dict) or not resources:
        raise ValueError("profile.resources must be a nonempty object")
    evidence = candidate.get("evidence", {})
    citations = {"part": evidence.get("part", [])}
    resource_evidence = evidence.get("resources", {})
    pins = set()
    for name, info in resources.items():
        if not re.fullmatch(r"[a-z][a-z0-9_]*", name) or not isinstance(info, dict):
            raise ValueError("Resource names must be lowercase identifiers")
        if info.get("direction") not in {"input", "output"}:
            raise ValueError("This initial profile supports input/output PL resources")
        for key in ("pin", "standard"):
            if not isinstance(info.get(key), str) or not re.fullmatch(r"[A-Za-z0-9_]+", info[key]):
                raise ValueError(f"resources.{name}.{key} must be an identifier")
        if info["pin"].upper() in pins:
            raise ValueError("Multiple resources use the same pin")
        pins.add(info["pin"].upper())
        if "period_ns" in info and (type(info["period_ns"]) not in {int, float} or not 0 < info["period_ns"] <= 1000000):
            raise ValueError("Clock period must be a positive number of nanoseconds")
        for key in ("active_low", "pullup"):
            if key in info and type(info[key]) is not bool:
                raise ValueError(f"{name}.{key} must be boolean")
        if info.get("pair") and (info["pair"] not in resources or resources[info["pair"]].get("pair") != name):
            raise ValueError("Differential resources need a symmetric pair")
        citations[name] = resource_evidence.get(name, [])
    for field, entries in citations.items():
        if not isinstance(entries, list) or not entries:
            raise ValueError(f"Missing evidence for {field}")
        for citation in entries:
            documents.evidence_text(citation)
    return profile


def run(config, board_id, request, sources=(), ask=None, resume=None):
    from .workflow import save, task_path
    if not re.fullmatch(r"[a-z][a-z0-9_]*", board_id):
        raise ValueError("board_id must be a lowercase identifier")
    task = task_path(config, resume) if resume else Path(config["workspace_path"]) / (
        "board-" + datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6])
    task.mkdir(exist_ok=True)
    state = json.loads((task / "state.json").read_text(encoding="utf-8")) if resume else {
        "task_id": task.name, "kind": "board_research", "board_id": board_id, "request": request,
        "status": "researching_board", "results": [], "research_log": []}
    if state.get("kind") != "board_research" or state["board_id"] != board_id:
        raise ValueError("Resume must reference research for the same board")
    if state["status"] == "completed":
        return state
    with tools.lock(task / "active.lock"):
        try:
            documents = Documents(task, config)
            if resume and state["status"] == "waiting_for_user" and not request:
                memory.event(task, "提问", state.get("question", "请补充资料"))
                request = read_reply(task, ask, "请回答")
                if not request:
                    return state
            state["research_sources"] = list(dict.fromkeys(state.get("research_sources", []) + [str(s) for s in sources]))
            save(task, state)
            source_errors = research.import_sources(documents, research.available_sources(task, config))
            if source_errors:
                state["research_log"].append({"call": {"action": "import_sources"}, "source_errors": source_errors})
            if request and resume:
                state["request"] += "\nUser update: " + request
                state.pop("candidate", None)
            for _ in range(config.get("research", {}).get("max_steps", 24)):
                candidate = state.get("candidate")
                if candidate is None:
                    state["status"] = "researching_board"
                    save(task, state)
                    response = DeepSeekClient(config).complete([
                        {"role": "system", "content": BOARD_RESEARCH},
                        {"role": "user", "content": json.dumps({"board_id": board_id, "goal": state["request"],
                         "sources": documents.inventory(), "notes": state.get("research_notes", ""),
                         "recent_tool_results": state["research_log"][-6:]}, ensure_ascii=False)}],
                        task / "llm_calls.jsonl")
                    action = response.get("action")
                    if isinstance(response.get("notes"), str):
                        state["research_notes"] = response["notes"][:16000]
                    memory.event(task, "板卡资料", str(action))
                    if action == "clarify":
                        state.update(status="waiting_for_user", question=response.get("message", "请补充板卡资料"))
                        save(task, state)
                        memory.event(task, "提问", state["question"])
                        answer = read_reply(task, ask, "请回答")
                        if not answer:
                            return state
                        state["request"] += "\nUser answer: " + answer
                        continue
                    try:
                        if action == "propose":
                            validate_candidate(response, board_id, documents)
                            candidate = response
                            state["candidate"] = candidate
                            tools.write_json(task / "board_candidate.json", candidate)
                            memory.document(task, "board_review.md", "# 待确认板卡配置\n\n" + json.dumps(
                                {"candidate": candidate, "source_index": documents.inventory()}, ensure_ascii=False, indent=2))
                        else:
                            result = research.dispatch(documents, response)
                            state["research_log"].append({"call": response, "result": result})
                            save(task, state)
                            continue
                    except Exception as exc:
                        # Preserve tool facts. Errors are evidence for the next action, not invented board facts.
                        state["research_log"].append({"call": response, "error_type": type(exc).__name__, "error": str(exc)[:1500]})
                        save(task, state)
                        continue
                profile = validate_candidate(candidate, board_id, documents)
                destination = Path(config["root"]) / "boards" / (board_id + ".json")
                previous = json.loads(destination.read_text(encoding="utf-8")) if destination.exists() else None
                decision, answer = confirm(task, state, "board_profile", "板卡资料及配置（确认后安装）",
                    {"candidate": candidate, "source_index": documents.inventory(), "destination": str(destination), "previous_profile": previous}, ask)
                if decision == "paused":
                    return state
                if decision == "revision":
                    state["request"] += "\nUser revision: " + answer
                    state.pop("candidate", None)
                    continue
                if previous:
                    tools.write_json(task / "previous_profile.json", previous)
                installed = {**profile, "source": "User-confirmed document research; not hardware verification",
                             "evidence": candidate["evidence"], "sources": documents.inventory(), "research_task": task.name}
                tools.write_json(destination, installed)
                state.update(status="completed", installed_profile=str(destination), question=None)
                save(task, state)
                return state
            state.update(status="research_paused", error="Research step limit reached; sources and evidence preserved")
            save(task, state)
            return state
        except BaseException as exc:
            state.update(status="interrupted" if isinstance(exc, KeyboardInterrupt) else "error", error=str(exc))
            save(task, state)
            raise

"""Shared research tools and durable notes for any development reference material."""
import json
from pathlib import Path
import urllib.parse

from .documents import Documents, TEXT_EXTENSIONS
from . import task_memory as memory, tools

READ_ACTIONS = {"list_sources", "search_web", "fetch_source", "search_documents", "read_page", "inspect_page"}


def available_sources(task, config):
    """Discover reference files plus explicitly configured/task-supplied sources."""
    root = Path(config["root"])
    sources = [str(p.resolve()) for folder in ("references", "boards")
               for p in sorted((root / folder).rglob("*"))
               if p.is_file() and p.suffix.lower() in TEXT_EXTENSIONS | {".pdf"}
               and (folder != "boards" or p.suffix.lower() == ".pdf")]
    state_path = task / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    configured = config.get("research", {}).get("sources", [])
    if not isinstance(configured, list):
        raise ValueError("research.sources must be a list of file paths or HTTP(S) URLs")
    for source in configured + state.get("research_sources", []):
        source = str(source)
        if urllib.parse.urlsplit(source).scheme not in {"http", "https"}:
            source = str((root / source).resolve())
        sources.append(source)
    return list(dict.fromkeys(sources))


def import_sources(documents, sources):
    """A broken source must remain visible without hiding other usable sources."""
    loaded = {r["origin"] for r in documents.records.values()}
    errors = []
    for source in sources:
        if str(source) in loaded:
            continue
        try:
            documents.add(source)
            loaded.add(str(source))
        except Exception as exc:
            errors.append({"source": str(source), "error_type": type(exc).__name__, "error": str(exc)[:1500]})
    return errors


def tool_context(task, config):
    path = task / "research_context.json"
    context = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"recent_tool_results": []}
    index = task / "sources/index.json"
    return {**context, "available_sources": available_sources(task, config),
            "sources": Documents(task, config).inventory() if index.exists() else []}


def read_tool(task, config, response):
    documents = Documents(task, config)
    context = tool_context(task, config)
    source_errors = import_sources(documents, context["available_sources"])
    try:
        result = {"call": response, "result": dispatch(documents, response)}
    except Exception as exc:
        result = {"call": response, "error_type": type(exc).__name__, "error": str(exc)[:1500]}
    if source_errors:
        result["source_errors"] = source_errors
    context["recent_tool_results"] = (context["recent_tool_results"] + [result])[-6:]
    context["sources"] = documents.inventory()
    if isinstance(response.get("notes"), str):
        context["notes"] = response["notes"][:16000]
    tools.write_json(task / "research_context.json", context)
    with (task / "research_tools.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(result, ensure_ascii=False) + "\n")
    memory.document(task, "research.md", "# 资料研究\n\n" + context.get("notes", "尚未整理研究笔记。")
                    + "\n\n## 来源索引\n\n" + json.dumps(documents.inventory(), ensure_ascii=False, indent=2) + "\n")
    memory.event(task, "资料工具", response["action"])
    return context


def dispatch(documents, response):
    action = response["action"]
    if action == "list_sources":
        return documents.inventory()
    if action == "search_web":
        return documents.search(response["query"])
    if action == "fetch_source":
        if urllib.parse.urlsplit(response["url"]).scheme not in {"http", "https"}:
            raise ValueError("fetch_source accepts only HTTP(S) URLs; local files must be supplied by the user")
        record = documents.add(response["url"])
        return {k: record[k] for k in ("id", "title", "origin", "sha256", "pages", "links")}
    if action == "search_documents":
        return documents.find(response["query"], response.get("source_id"))
    if action == "read_page":
        return documents.read(response["source_id"], response["page"], response.get("offset", 0))
    if action == "inspect_page":
        return documents.inspect(response["source_id"], response["page"], response.get("question", "Read the relevant text, tables and diagrams"))
    raise ValueError("Unknown research action")

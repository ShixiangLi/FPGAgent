"""Explicit user confirmation bound to the exact document content."""
from datetime import datetime
import hashlib
import json

from . import task_memory as memory
from .interaction import read_reply


def digest(content):
    return hashlib.sha256(json.dumps(content, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def confirm(task, state, stage, title, content, ask):
    """Return approved, paused or user revision; never infer consent from no answer."""
    from .workflow import save
    version = digest(content)
    approvals = state.setdefault("confirmations", {})
    if approvals.get(stage, {}).get("digest") == version:
        return "approved", ""
    state.update(status="awaiting_confirmation", pending_confirmation=stage,
                 question=f"请确认{title}，或输入修改意见。")
    save(task, state)
    memory.event(task, "用户确认", title + "\n" + json.dumps(content, ensure_ascii=False, indent=2))
    answer = read_reply(task, ask, "输入 y/确认 继续，或输入修改意见重新整理")
    if not answer:
        return "paused", ""
    with (task / "conversation.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"role": "user", "stage": stage, "content": answer, "document_digest": version}, ensure_ascii=False) + "\n")
    if answer.lower() not in {"y", "yes", "确认", "同意"}:
        # The submitted document was not approved. Drop downstream approval as well.
        approvals.pop(stage, None)
        approvals.pop("test_plan", None)
        return "revision", answer
    approvals[stage] = {"digest": version, "confirmed_at": datetime.now().astimezone().isoformat(), "answer": answer}
    state.update(pending_confirmation=None, question=None)
    save(task, state)
    memory.event(task, "已确认", title)
    return "approved", ""

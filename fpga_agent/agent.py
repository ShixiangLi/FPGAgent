"""DeepSeek thinking-mode JSON conversations, using only Python's standard library."""
import json
import os
import socket
import ssl
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from .prompts.catalog import (
    DIAGNOSTIC,
    LEGACY_REPAIR_INSTRUCTION,
    PROJECT_CONTEXT_PREFIX,
    REVIEWER,
    SYSTEM,
    json_repair_instruction,
)


def load_env(path: Path) -> None:
    """Load simple KEY=value entries without overriding existing environment values."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.removeprefix("export ").partition("=")
        if sep:
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


class GenerationTruncated(RuntimeError):
    """The caller owns task recovery; transport never applies or retries partial output."""
    def __init__(self, details):
        self.details = details
        super().__init__("DeepSeek生成截断，未应用残缺响应：" + json.dumps(details, ensure_ascii=False))


class DeepSeekClient:
    def __init__(self, config: dict):
        self.config = config["llm"]
        self.key = os.environ.get("DEEPSEEK_API_KEY", "")
        if not self.key or self.key.startswith("replace-with"):
            raise ValueError("请在 .env 或环境变量中配置 DEEPSEEK_API_KEY。")
        if self.config["model"] not in {"deepseek-v4-pro", "deepseek-flash"} or not self.config["thinking"]:
            raise ValueError("支持 deepseek-v4-pro 或 deepseek-flash，且需开启 thinking。")
        if self.config.get("proxy_mode", "system") not in {"system", "direct"}:
            raise ValueError("llm.proxy_mode必须为system或direct。")

    def _open_request(self, request):
        if self.config.get("proxy_mode", "system") == "direct":
            # Explicit per-application selection; do not alter Windows or environment proxy settings.
            return urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                request, timeout=self.config["timeout_seconds"])
        return urllib.request.urlopen(request, timeout=self.config["timeout_seconds"])

    def complete(self, messages: list, audit_path: Path | None = None, _format_attempt: int = 0) -> dict:
        started = time.monotonic()
        payload = {
            "model": self.config["model"], "messages": messages,
            "thinking": {"type": "enabled"},
            "reasoning_effort": self.config["reasoning_effort"],
            "max_tokens": self.config["max_tokens"],
            "response_format": {"type": "json_object"}, "stream": self.config.get("stream", True),
        }
        request = urllib.request.Request(
            self.config["base_url"].rstrip("/") + "/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + self.key},
        )
        print(f"[LLM] {self.config['model']} thinking=enabled effort={self.config['reasoning_effort']} max_tokens={self.config['max_tokens']} "
              f"network={self.config.get('proxy_mode', 'system')}，等待响应……", flush=True)
        for attempt in range(3):
            try:
                with self._open_request(request) as response:
                    if "text/event-stream" in str(getattr(response, "headers", {}).get("Content-Type", "")):
                        data = self._read_stream(response, started)
                    else:
                        data = json.load(response)
                break
            except urllib.error.HTTPError as exc:
                if exc.code in (429, 500, 502, 503, 504) and attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RuntimeError(f"DeepSeek HTTP {exc.code}，请检查密钥、余额、接口配置。") from None
            except (urllib.error.URLError, TimeoutError) as exc:
                reason = getattr(exc, "reason", exc)
                if isinstance(reason, ssl.SSLCertVerificationError):
                    hint = "TLS证书校验失败；检查代理证书及Python信任配置，不要关闭证书校验。"
                elif isinstance(reason, socket.gaierror):
                    hint = "DNS解析失败；检查网络和DNS设置。"
                elif isinstance(reason, (TimeoutError, socket.timeout)):
                    hint = "连接或响应超时；检查网络，可稍后重试。"
                else:
                    hint = "连接失败；若使用代理，请检查代理服务和端口是否可用。"
                detail = str(reason).replace(self.key, "[REDACTED]")
                # Proxy URLs may embed credentials. Keep useful errno text, redact URLs.
                import re
                detail = re.sub(r"https?://\S+", "[URL]", detail)
                raise RuntimeError(f"DeepSeek 网络错误：{type(reason).__name__}: {detail}；{hint}") from None
        choices = data.get("choices") if isinstance(data, dict) else None
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict) or not isinstance(choices[0].get("message"), dict):
            error = data.get("error", {}) if isinstance(data, dict) else {}
            detail = str(error.get("message", error) if isinstance(error, dict) else error)
            detail = detail.replace(self.key, "[REDACTED]")[:500]
            raise RuntimeError("DeepSeek未返回有效choices，可能为服务超时或异常响应：" + detail)
        choice, message = choices[0], choices[0]["message"]
        content = message.get("content") or ""
        raw_path = None
        if audit_path:
            raw_path = audit_path.parent / f"llm-response-{uuid.uuid4().hex}.txt"
            raw_path.write_text(content.replace(self.key, "[REDACTED]"), encoding="utf-8")
        if audit_path:
            audit = {
                "model": data.get("model"), "thinking": True,
                "reasoning_effort": self.config["reasoning_effort"], "max_tokens": self.config["max_tokens"],
                "reasoning_received": bool(message.get("reasoning_content")),
                "usage": data.get("usage", {}), "finish_reason": choice.get("finish_reason"),
                "format_attempt": _format_attempt, "response_file": str(raw_path),
                "elapsed_seconds": round(time.monotonic() - started, 2),
            }
            with audit_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(audit, ensure_ascii=False) + "\n")
        if choice.get("finish_reason") != "stop":
            details = {"finish_reason": choice.get("finish_reason"), "usage": data.get("usage", {}),
                       "max_tokens": self.config["max_tokens"], "reasoning_effort": self.config["reasoning_effort"],
                       "response_file": str(raw_path) if raw_path else None}
            if choice.get("finish_reason") == "length":
                raise GenerationTruncated(details)
            raise RuntimeError("DeepSeek未完整结束，未应用残缺响应：" + json.dumps(details, ensure_ascii=False))
        # Preserve the original response; only unwrap an entire Markdown fence.
        candidate = content.strip()
        if candidate.startswith("```") and candidate.endswith("```") and "\n" in candidate:
            candidate = candidate.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        try:
            result = json.loads(candidate)
        except json.JSONDecodeError as exc:
            detail = f"{exc.msg} (line {exc.lineno}, column {exc.colno})"
            if _format_attempt < 2:
                print(f"[LLM] JSON格式错误：{detail}；请求模型修正 {_format_attempt+1}/2", flush=True)
                return self.complete(messages + [
                    {"role": "assistant", "content": content},
                    {"role": "user", "content": json_repair_instruction(detail)},
                ], audit_path, _format_attempt + 1)
            raise RuntimeError(f"DeepSeek JSON格式修复2次仍失败：{detail}；原文：{raw_path}；没有应用代码。") from None
        if not isinstance(result, dict):
            raise RuntimeError("DeepSeek 返回值必须为 JSON 对象。")
        # With no tools parameter, reasoning_content need not be sent back (DeepSeek docs).
        return result

    def _read_stream(self, response, started: float) -> dict:
        content, reasoning_received, finish, model, usage = [], False, None, self.config["model"], {}
        answer_chars = 0
        last_progress = started
        done = False
        for raw in response:
            elapsed = time.monotonic() - started
            if elapsed > self.config["timeout_seconds"]:
                raise TimeoutError("LLM整体请求超时")
            if time.monotonic() - last_progress >= 15:
                phase = "生成正文" if answer_chars else "思考/等待正文"
                print(f"[LLM] {phase}，已运行 {int(elapsed)} 秒，已接收正文 {answer_chars} 字符", flush=True)
                last_progress = time.monotonic()
            line = raw.decode("utf-8").strip()
            if not line.startswith("data:"):
                continue
            body = line[5:].strip()
            if body == "[DONE]":
                done = True
                break
            chunk = json.loads(body)
            if chunk.get("error"):
                raise RuntimeError("DeepSeek流式服务返回错误，未应用任何代码。")
            model = chunk.get("model", model)
            usage = chunk.get("usage") or usage
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                if delta.get("reasoning_content"):
                    reasoning_received = True
                if delta.get("content"):
                    content.append(delta["content"])
                    answer_chars += len(delta["content"])
                finish = choice.get("finish_reason") or finish
        if not done:
            raise RuntimeError("DeepSeek流提前断开，未应用部分代码。")
        return {"model": model, "usage": usage, "choices": [{"finish_reason": finish,
                "message": {"content": "".join(content), "reasoning_content": "received" if reasoning_received else ""}}]}


def project_context(config: dict) -> str:
    from .project import board_profile
    return PROJECT_CONTEXT_PREFIX + json.dumps({"target": config.get("target", "build"),
                                                   "board": board_profile(config)}, ensure_ascii=False)


def handle_message(message: str, config: dict, conversation: list | None = None,
                   audit_path: Path | None = None) -> dict:
    history = conversation if conversation is not None else []
    history.append({"role": "user", "content": message})
    result = DeepSeekClient(config).complete([{"role": "system", "content": SYSTEM + project_context(config)}] + history, audit_path)
    history.append({"role": "assistant", "content": json.dumps(result, ensure_ascii=False)})
    return result


def analyze_result(result: dict, config: dict, design: dict, spec: dict,
                   audit_path: Path | None = None) -> dict:
    prompt = {"spec": spec, "current_project": design, "failed_run": result,
              "execution_context": review_context(config),
              "request": LEGACY_REPAIR_INSTRUCTION}
    if config.get("task_memory_path"):
        from .task_memory import context
        prompt["current_task_documents"] = context(Path(config["task_memory_path"]))
        # Raw logs remain on disk. Send exact errors plus bounded tail, not duplicate full logs.
        prompt["failed_run"] = {k: v for k, v in result.items() if k not in {"reports", "diagnostic", "errors"}}
        prompt["failed_run"]["diagnostic"] = result.get("diagnostic", "")[-5000:]
        prompt["failed_run"]["errors"] = result.get("errors", [])[:8]
    return DeepSeekClient(config).complete([
        {"role": "system", "content": SYSTEM + project_context(config)},
        {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
    ], audit_path)


def review_context(config: dict) -> dict:
    """Describe the stage; approval never means future hardware checks have passed."""
    simulation_only = config.get("target", "build") == "sim"
    return {"phase": "simulation_review" if simulation_only else "pre_program_review",
            "decision": "approve current simulation evidence" if simulation_only else "allow proceeding to hardware programming",
            "hardware_verification": "not_requested_by_target" if simulation_only else "pending_after_approval",
            "next_steps": [] if simulation_only else ["framework: JTAG probe and programming",
                "framework: VIO sampling when configured", "user: physical functional acceptance"],
            "hardware_access": "not yet probed; do not infer unavailable from missing results"}


def review_project(request: str, spec: dict, design: dict, config: dict, audit_path: Path,
                   simulation_result: dict | None = None, build_result: dict | None = None,
                   analysis_result: dict | None = None, previous_review: dict | None = None, user_reply=None,
                   additional_executions=None) -> dict:
    """Separate model context checks requirements/test coverage; not a proof of correctness."""
    from . import model_steps
    from .contract import describe
    from .project import board_profile
    task = audit_path.parent
    files = {'project/' + name: text for name, text in design.get('files', {}).items()}
    evidence = {name: model_steps.evidence_context(task, result, files) for name, result in (
        ('actual_simulation', simulation_result), ('actual_build', build_result), ('actual_analysis', analysis_result))}
    evidence['additional_executions'] = [model_steps.evidence_context(task, r, files)
                                         for r in (additional_executions or [])]
    payload = {'request': request, 'target': config.get('target', 'build'), 'spec': spec,
               'project': {k: v for k, v in design.items() if k != 'files'},
               'execution_contract': describe(board_profile(config)), 'previous_review': previous_review,
               'execution_context': review_context(config), **evidence}

    def validate(result):
        if (type(result.get('approved')) is not bool or not isinstance(result.get('coverage'), dict)
                or any(not isinstance(result.get(k), list) for k in ('blocking_findings', 'warnings', 'pending_checks'))):
            return ['Review requires approved:boolean, blocking_findings, warnings, pending_checks and coverage']
        return []

    identity = {'request': request, 'spec': spec, 'project': design, 'runs': [
        r.get('run_dir') if r else None for r in (simulation_result, build_result, analysis_result)],
        'previous_review': previous_review}
    if additional_executions:
        identity['additional_executions'] = [r['run_dir'] for r in additional_executions]
    return model_steps.run(task, config, 'review', identity,
                           REVIEWER, payload, files=files, validate=validate, user_reply=user_reply)


def explain_failure(result: dict, config: dict, audit_path: Path | None = None) -> dict:
    """Explain environmental/hardware failures without changing code or retrying writes."""
    return DeepSeekClient(config).complete([
        {"role": "system", "content": DIAGNOSTIC},
        {"role": "user", "content": json.dumps(result, ensure_ascii=False)},
    ], audit_path)

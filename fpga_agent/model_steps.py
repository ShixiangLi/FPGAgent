"""Durable, bounded model decisions shared by planning and review stages."""
import copy
import hashlib
import json
from pathlib import Path

from . import task_memory, tools
from .agent import DeepSeekClient, GenerationTruncated
from .prompts.catalog import MODEL_STEPS, REPORT_EXCERPT_NOTE


class ModelPaused(RuntimeError):
    def __init__(self, details):
        self.details = details
        super().__init__(details['reason'])


def pause_details(stage, checkpoint, reason, config, recovery=None):
    return {'stage': stage, 'checkpoint': str(checkpoint), 'reason': reason,
            'recovery': recovery, 'model': config['llm']['model'],
            'reasoning_effort': config['llm']['reasoning_effort'], 'max_tokens': config['llm']['max_tokens'],
            'options': '继续当前检查点；或由用户选择增大输出预算/降低思考强度。配置不会自动改变。'}


def excerpt(text, limit=3000):
    if len(text) <= limit:
        return text
    half = limit // 2
    return text[:half] + '\n...[excerpt; full text remains in file_index]...\n' + text[-half:]


def evidence_context(task, result, files):
    """Expose full evidence by index while keeping the default model input bounded."""
    if result is None:
        return None
    stage = result.get('stage', 'tool')
    for field in ('logs', 'evidence_files'):
        for name in result.get(field, []):
            path = Path(name).resolve()
            if path.is_file() and path.is_relative_to(task.resolve()):
                files[path.relative_to(task.resolve()).as_posix()] = path
    for name, text in result.get('reports', {}).items():
        files[f'{stage}/reports/{name}'] = text
    for name, text in result.get('artifact_excerpts', {}).items():
        if not any(Path(n).name == name for n in files):
            files[f'{stage}/excerpts/{name}'] = text
    files[f'{stage}/diagnostic.txt'] = result.get('diagnostic', '')
    compact = {k: v for k, v in result.items() if k not in {
        'reports', 'diagnostic', 'errors', 'artifact_excerpts', 'input_preview', 'output_files'}}
    compact['diagnostic'] = excerpt(result.get('diagnostic', ''))
    compact['errors'] = [{**e, 'context': excerpt(e.get('context', ''), 800)}
                         if isinstance(e, dict) else e for e in result.get('errors', [])[:8]]
    compact['report_note'] = REPORT_EXCERPT_NOTE
    return compact


def _read(source, offset, limit):
    if isinstance(source, Path):
        with source.open('rb') as handle:
            handle.seek(offset)
            data = handle.read(limit)
        size = source.stat().st_size
    else:
        encoded = source.encode('utf-8')
        data, size = encoded[offset:offset + limit], len(encoded)
    return {'offset': offset, 'limit': limit, 'size_bytes': size, 'units': 'bytes',
            'next_offset': offset + len(data) if offset + len(data) < size else None,
            'text': data.decode('utf-8', errors='replace')}


def _merge(destination, updates):
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(destination.get(key), dict):
            _merge(destination[key], value)
        else:
            destination[key] = value


def run(task, config, stage, identity, system, context, *, files=None, validate=None, handle_action=None, user_reply=None):
    key = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:20]
    folder = task / 'model_steps'
    folder.mkdir(exist_ok=True)
    path = folder / f'{stage}-{key}.json'
    work = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {
        'context': context, 'draft': {}, 'summary': '',
        'next_step': '先返回简短checkpoint选择一个更小步骤，不推演或生成整个阶段结果。', 'views': {},
        'feedback': None, 'recovery': None, 'recent': [], 'truncations': 0}
    if 'result' in work:
        return work['result']
    if user_reply is not None:
        work['context']['user_reply'] = user_reply
        work['feedback'] = None
    sources = dict(files or {})
    truncations = 0
    reason = '本批模型动作达到上限，草案及读取窗口已保存'
    for _ in range(config.get('model_max_steps', 16)):
        draft_text = json.dumps(work['draft'], ensure_ascii=False, indent=2)
        sources['draft.json'] = draft_text
        feedback = work['feedback']
        feedback_text = json.dumps(feedback, ensure_ascii=False)
        feedback_path = path.with_suffix('.feedback.txt')
        if feedback_path.exists():
            sources['feedback.json'] = feedback_path
        if len(feedback_text) > 8000:
            feedback_path.write_text(feedback_text, encoding='utf-8')
            sources['feedback.json'] = feedback_path
            feedback = {'file': 'feedback.json', 'excerpt': excerpt(feedback_text)}
        tools.write_json(path, work)
        payload = {**work['context'], 'draft': work['draft'] if len(draft_text) <= 12000 else None,
                   'draft_index': {k: {'type': type(v).__name__, 'items': len(v) if isinstance(v, (dict, list)) else None}
                                   for k, v in work['draft'].items()},
                   'summary': work['summary'], 'next_step': work['next_step'], 'recovery': work['recovery'],
                   'file_index': list(sources), 'file_views': {n: _read(sources[n], **v) for n, v in work['views'].items() if n in sources},
                   'feedback': feedback, 'recent_actions': work['recent'][-2:]}
        try:
            response = DeepSeekClient(config).complete([
                {'role': 'system', 'content': system + MODEL_STEPS},
                {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)}], task / 'llm_calls.jsonl')
        except GenerationTruncated as exc:
            truncations += 1
            work.update(recovery=exc.details, feedback=None, views={})
            work['truncations'] += 1
            tools.write_json(path, work)
            task_memory.event(task, '模型续接', f'{stage}: 输出预算耗尽；保留草案，下一轮由模型选择更小步骤')
            if truncations >= 2:
                reason = '本批两次输出预算耗尽，保留当前阶段；可原地续接或由用户调整预算/思考强度'
                break
            continue
        action = response.get('action')
        work['feedback'] = None
        try:
            if action == 'read_files':
                names, offset, limit = response.get('paths'), response.get('offset', 0), response.get('limit', 8000)
                if (not isinstance(names, list) or not 1 <= len(names) <= 6 or any(n not in sources for n in names)
                        or type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 8000):
                    raise ValueError(f'read_files: indexed paths=1..6, offset>=0, limit=1..8000 bytes; received offset={offset}, limit={limit}')
                work['views'] = {n: {'offset': offset, 'limit': limit} for n in names}
                task_memory.event(task, '模型读取', f'{stage}: {names} offset={offset} limit={limit}')
            elif action == 'checkpoint':
                if any(not isinstance(response.get(k), str) or not response[k].strip() for k in ('summary', 'next_step')):
                    raise ValueError('checkpoint requires summary and one next_step')
                if len(response['summary']) > 8000 or len(response['next_step']) > 2000:
                    raise ValueError('summary<=8000 and next_step<=2000 characters')
                updates, append = response.get('updates', {}), response.get('append', {})
                if not isinstance(updates, dict) or not isinstance(append, dict):
                    raise TypeError('checkpoint updates/append must be objects')
                if work['recovery'] and (updates or append):
                    raise ValueError('After truncation first submit a short checkpoint without updates/append')
                draft = copy.deepcopy(work['draft'])
                _merge(draft, updates)
                for name, values in append.items():
                    if not isinstance(values, list) or not isinstance(draft.get(name, []), list):
                        raise TypeError('append only accepts arrays for array fields')
                    draft.setdefault(name, []).extend(values)
                work.update(draft=draft, summary=response['summary'], next_step=response['next_step'], recovery=None, views={})
                task_memory.event(task, '模型检查点', f'{stage}: {work["next_step"]}')
            elif handle_action and (extra := handle_action(response)):
                if isinstance(extra, dict):
                    work['context'].update(extra)
                work['views'] = {}
            else:
                candidate = work['draft'] if action == 'submit' else response
                if work['recovery'] and action != 'clarify':
                    raise ValueError('After truncation return a short checkpoint before submitting a result')
                if not candidate:
                    raise ValueError('Cannot submit an empty draft')
                errors = validate(candidate) if validate and candidate.get('action') != 'clarify' else []
                if errors:
                    work['draft'] = copy.deepcopy(candidate)
                    work['feedback'] = errors
                else:
                    if candidate.get('action') != 'clarify':
                        work['result'] = candidate
                    tools.write_json(path, work)
                    return candidate
        except (TypeError, ValueError) as exc:
            work['feedback'] = {'error_type': type(exc).__name__, 'error': str(exc)}
        work['recent'].append({'action': action, 'feedback': excerpt(json.dumps(work['feedback'], ensure_ascii=False), 1000)})
        work['recent'] = work['recent'][-2:]
        tools.write_json(path, work)
    details = pause_details(stage, path, reason, config, work['recovery'])
    tools.write_json(path, work)
    raise ModelPaused(details)

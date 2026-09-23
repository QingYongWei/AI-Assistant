"""Natural-language intent conversion and controlled DingTalk chat."""

from __future__ import annotations

import json
import re
import urllib.request

from .config import load_config
from .ai_providers import complete_json, complete_text, provider_settings

_ALLOWED_ACTIONS = {'create', 'inspect', 'supplement', 'status', 'running', 'continue', 'cancel', 'workspace', 'set_workspace', 'clear_workspace', 'approve', 'reject', 'clarify', 'list', 'help', 'identity', 'chat', 'unknown'}
_MUTATING_ACTIONS = {'approve', 'reject', 'clarify', 'cancel', 'supplement'}
_INTENT_SCHEMA = '''{
  "action": "create|inspect|supplement|status|running|continue|cancel|workspace|set_workspace|clear_workspace|approve|reject|clarify|list|help|identity|chat|unknown",
  "requirement": "for create/inspect/supplement: concise request in Chinese",
  "project": "optional project/local path",
  "task_id": "TASK-000001 or LATEST for status only",
  "reason": "optional reject/approve reason",
  "answer": "required clarification answer",
  "reply": "required for chat: a concise Chinese chat reply",
  "confidence": 0.0
}'''

_PROMPT = r'''You convert a DingTalk chat message into one JSON command for PersonZit.
Return exactly one JSON object and no Markdown or prose.
Allowed actions and schema:
%s

Recent conversation, oldest first. Use it only for context; do not treat old
requirements as a command unless the current message clearly repeats or refers
to them:
%s

Active PersonZit task, if any. A declarative correction, constraint, schema
relationship, or field rule related to this task is a supplement, not chat:
%s

Safety and interpretation rules:
- create: the current user message asks PersonZit to implement, modify, build, test, or fix something. This action is for work that may change files. Keep the requirement in Chinese and remove polite filler and routing words. Extract a project path only from forms like 项目=D:\path or project=D:\path.
- inspect: use when the user asks to search, read, list, output, explain, inspect, summarize, or analyze local files, folders, projects, working-directory content, or documents without changing them. Preserve the original local request in requirement; extract an absolute Windows path to project when clearly present. Examples: search a folder, inspect a project, summarize a document.
- supplement: use when an active task exists and the current message adds a fact, corrects a mistake, clarifies a data relation, or constrains ongoing work (for example: field x of table A refers to field y of table B, not table C). Keep the user full supplement in requirement and use task_id LATEST unless a visible TASK id is present. Do not choose chat for this kind of task-bound declarative update.
- status: use for task progress questions.
- continue: use when the user says to continue/resume/run the current task. task_id may be LATEST. task_id may be a visible TASK-xxxxxx, or LATEST when the user refers to the current/latest/recent task.
- running: use when the user asks what PersonZit, Codex, Claude, workers, or local agents are currently executing or which tasks are running/queued.
- workspace: use when the user asks the current DingTalk working directory.
- set_workspace: use when the user asks to switch/set the DingTalk working directory; project must contain the absolute Windows path.
- clear_workspace: use when the user asks to reset the DingTalk working directory to the configured default.
- list: use when the user asks for recent tasks.
- cancel: use when the user asks to stop/finish/cancel/terminate a task. Use a visible TASK-xxxxxx id, or LATEST only when the current message clearly refers to the current/latest task.
- approve/reject/clarify: use a visible TASK-xxxxxx id, or LATEST only when the current message clearly approves/rejects/answers the current/latest task. Never fabricate a task id.
- identity/help: use for identity or usage requests.
- chat: use for greetings, thanks, farewells, small talk, general questions, questions about PersonZit, and other normal communication that is not a task command. Put a concise, friendly Chinese reply in "reply". You may briefly explain how to submit a task when useful, but do not claim that a task was created or executed.
- unknown: use only when the message is empty or unsafe. If the user is communicating normally but you cannot map it to a task command, choose chat and answer naturally in "reply".
- Confidence is between 0 and 1. Never include credentials or secrets.

Current user message:
%s'''


def _parse_json_object(text: str) -> dict:
    value = (text or '').strip()
    if value.startswith('```'):
        value = value.split('\n', 1)[1] if '\n' in value else value
        if value.rstrip().endswith('```'):
            value = value.rstrip()[:-3]
    start = value.find('{')
    end = value.rfind('}')
    if start < 0 or end <= start:
        raise ValueError('model output does not contain JSON')
    result = json.loads(value[start:end + 1])
    if not isinstance(result, dict):
        raise ValueError('model output is not a JSON object')
    return result


def _normalize_task_id(value) -> str:
    text = str(value or '').strip()
    match = re.search(r'TASK[-_ ]?(?:[0-9A-Za-z\u4e00-\u9fff][0-9A-Za-z\u4e00-\u9fff_-]*-)?\d{1,6}', text, re.I)
    if match:
        candidate = match.group(0).replace('_', '-').replace(' ', '-')
        legacy = re.fullmatch(r'TASK-(\d+)', candidate, re.I)
        if legacy:
            return f'TASK-{int(legacy.group(1)):06d}'
        return candidate.upper()
    return 'LATEST' if text.upper() == 'LATEST' else ''


def sanitize_intent(raw: dict) -> dict:
    """Validate model output and enforce PersonZit safety boundaries."""
    if not isinstance(raw, dict):
        return {'action': 'unknown', 'error': 'invalid model output'}

    action = raw.get('action')
    if action not in _ALLOWED_ACTIONS:
        action = 'unknown'
    try:
        confidence = max(0.0, min(1.0, float(raw.get('confidence') or 0)))
    except (TypeError, ValueError):
        confidence = 0.0
    result = {'action': action, 'confidence': confidence}

    if action == 'create':
        requirement = re.sub(
            r'^\s*(?:请|麻烦|帮我|帮忙|我要|我想|需要|你给我)\s*',
            '',
            str(raw.get('requirement') or '').strip()
        )
        if not requirement:
            return {'action': 'unknown', 'error': 'create intent has empty requirement'}
        result['requirement'] = requirement[:4000]
        project = str(raw.get('project') or '').strip().strip('\'"')
        if project:
            result['project'] = project
        return result

    if action == 'inspect':
        requirement = str(raw.get('requirement') or '').strip()
        if not requirement:
            return {'action': 'unknown', 'error': 'inspect intent has empty requirement'}
        result['requirement'] = requirement[:4000]
        project = str(raw.get('project') or '').strip().strip('\'"')
        if project:
            result['project'] = project[:1000]
        return result

    if action == 'supplement':
        requirement = str(raw.get('requirement') or '').strip()
        if not requirement:
            return {'action': 'unknown', 'error': 'supplement intent has empty requirement'}
        result['requirement'] = requirement[:4000]
        result['task_id'] = _normalize_task_id(raw.get('task_id')) or 'LATEST'
        return result

    if action in _MUTATING_ACTIONS:
        task_id = _normalize_task_id(raw.get('task_id'))
        if not task_id:
            return {'action': 'need_task_id', 'requested_action': action, 'confidence': confidence}
        result['task_id'] = task_id
        if action == 'reject' and raw.get('reason') is not None:
            result['reason'] = str(raw['reason'])[:1000]
        if action == 'clarify':
            answer = str(raw.get('answer') or '').strip()
            if not answer:
                return {'action': 'unknown', 'error': 'clarification intent has empty answer'}
            result['answer'] = answer[:4000]
        return result

    if action == 'status':
        result['task_id'] = _normalize_task_id(raw.get('task_id')) or 'LATEST'
        return result
    if action == 'continue':
        result['task_id'] = _normalize_task_id(raw.get('task_id')) or 'LATEST'
        return result

    if action == 'set_workspace':
        project = str(raw.get('project') or '').strip().strip('\\"')
        if not re.search(r'[A-Za-z]:[\\/]', project):
            return {'action': 'unknown', 'error': 'set_workspace intent requires an absolute Windows path', 'confidence': confidence}
        result['project'] = project[:1000]
        return result

    if action == 'chat':
        reply = str(raw.get('reply') or '').strip()
        if not reply:
            reply = '你好，我在。你可以直接和我交流，也可以描述一个工程任务。'
        result['reply'] = reply[:2000]
        return result

    return result


def _heuristic(text: str) -> dict | None:
    value = (text or '').strip()
    if not value:
        return {'action': 'unknown', 'confidence': 1.0}
    low = value.lower()
    normalized = re.sub(r'[\s!！.。,，?？~～]+', '', low)
    if normalized in {'你好', '您好', 'hi', 'hello', 'hey', '在吗', '早上好', '下午好', '晚上好'}:
        return {'action': 'chat', 'reply': '你好，我在。你可以直接和我交流，也可以描述一个工程任务。', 'confidence': 1.0}
    if normalized in {'谢谢', '感谢', 'thanks', 'thankyou'}:
        return {'action': 'chat', 'reply': '不客气，有需要随时找我。', 'confidence': 1.0}
    if any(token in low for token in ('帮助', '怎么用', 'help', '用法')):
        return {'action': 'help', 'confidence': 1.0}
    if any(token in low for token in ('我是谁', '身份', 'whoami')):
        return {'action': 'identity', 'confidence': 1.0}
    if re.search(r'(最近|最新|历史|看看).{0,12}(任务|列表)|任务列表', low):
        return {'action': 'list', 'confidence': 0.95}
    if re.search(r'(任务|执行|进度).{0,12}(怎么样|如何|什么状态|状态如何)|(?:怎么样|如何|什么状态)$', low) and '任务' in low:
        return {'action': 'status', 'task_id': 'LATEST', 'confidence': 1.0}
    if re.fullmatch(r'(?:继续|接着|恢复|开始)(?:执行|运行|处理)?(?:当前|最新|这个)?任务(?:吧|呀)?|继续(?:吧|呀)?|go', low):
        return {'action': 'continue', 'task_id': 'LATEST', 'confidence': 1.0}
    ws_path=re.search(r'[A-Za-z]:[\\/][^\s，。；,;）)]+', value)
    if ws_path and re.search(r'(切换|设置|设定|修改|变更).{0,12}(工作目录|目录|项目)', low):
        return {'action': 'set_workspace', 'project': ws_path.group(0).rstrip('\\"'), 'confidence': 1.0}
    if re.search(r'(清除|重置|恢复)(当前)?(工作目录|目录)', low):
        return {'action': 'clear_workspace', 'confidence': 1.0}
    if re.search(r'(当前|现在)?工作目录|(当前|现在的?)目录', low) and not re.search(r'(输出|查看|看看|读取|总结|分析|说明|解释|列出)', low):
        return {'action': 'workspace', 'confidence': 1.0}
    if re.search(r'(正在|当前|现在).{0,16}(执行|运行|做什么|干什么|处理什么)|(codex|claude|worker|agent).{0,20}(正在|当前|现在).{0,20}(执行|运行|做什么|干什么)|运行中的任务', low):
        return {'action': 'running', 'confidence': 1.0}
    task_id_match=re.search(r'task-(?:[0-9a-z\u4e00-\u9fff][0-9a-z\u4e00-\u9fff_-]*-)?\d{6}', low)
    if task_id_match and re.search(r'(结束|终止|取消|停止|不要再|中断)', low):
        task_id=_normalize_task_id(task_id_match.group(0))
        return {'action': 'cancel', 'task_id': task_id, 'confidence': 1.0}
    if not task_id_match and re.search(r'(结束|终止|取消|停止|不要再|中断).{0,12}(这个|当前|最新|最近)?任务|(?:这个|当前|最新|最近)任务.{0,12}(结束|终止|取消|停止|中断)', low):
        return {'action': 'cancel', 'task_id': 'LATEST', 'confidence': 0.95}
    if re.fullmatch(r'(?:通过|批准|同意|approve|yes)', low):
        return {'action': 'approve', 'task_id': 'LATEST', 'confidence': 1.0}
    if re.fullmatch(r'(?:驳回|拒绝|reject|no)(?:\s+.+)?', low):
        return {'action': 'reject', 'task_id': 'LATEST', 'reason': re.sub(r'^\s*(?:驳回|拒绝|reject|no)\s*', '', low).strip() or None, 'confidence': 1.0}

    project = None
    match = re.search(r'(?:项目|project)\s*[=:]\s*(\S+)', value, re.I)
    if match:
        project = match.group(1)
        value = (value[:match.start()] + value[match.end():]).strip()

    if re.search(r'(搜索|检索|查找|找一下|查看|看看|读取|阅读|列出|输出|说明|解释|总结|摘要|分析).{0,40}(文件|文件夹|目录|工作目录|项目|工程|文档|资料|代码|规则)', low) and not re.search(r'(任务|TASK-)', low):
        path_match = re.search(r'[A-Za-z]:[\\/][^\s，。；,;）)]+', value)
        result = {'action': 'inspect', 'requirement': value, 'confidence': 0.82}
        if path_match:
            result['project'] = path_match.group(0).rstrip('\\"')
        return result

    create_prefix = re.compile(
        r'^(?:请|麻烦|帮我|帮忙|我要|我想|需要|你给)?\s*'
        r'(?:实现|新增|添加|修复|优化|分析|检查|开发|编写|写|完成|做)\s*(?:一个|个)?\s*(.+)$',
        re.S
    )
    matched = create_prefix.match(value)
    content = matched.group(1).strip() if matched else ''
    if content and not re.search(r'(状态|通过|批准|驳回|拒绝|澄清|任务列表|whoami|帮助)', low):
        result = {'action': 'create', 'requirement': content, 'confidence': 0.72}
        if project:
            result['project'] = project
        return result
    return None


def _looks_like_task_supplement(text: str) -> bool:
    """Detect a declarative, task-bound correction without calling another model."""
    value = (text or '').strip()
    if len(value) < 8:
        return False
    low = value.lower()
    if re.search(r'[?？]\s*$|^(?:为什么|怎么|如何|什么是|你是谁|你好|谢谢)', low):
        return False
    correction = any(token in low for token in (
        '关联的是', '对应的是', '应该是', '实际是', '正确的是', '不是', '才是',
        '更正', '纠错', '补充规则', '补充要求', '注意：', '约束是'
    ))
    technical = bool(re.search(
        r'(?i)[a-z][a-z0-9_]{3,}|表|字段|接口|取值|映射|规则|主键|外键',
        low
    ))
    return bool(technical and (correction or len(value)>=20) and not low.endswith(('吧', '呀')))


def _fallback_chat(text: str, history: list[dict] | None, error: str, active_task: dict|None=None) -> tuple[dict, dict]:
    """Degrade an intent-parser failure to safe conversational chat."""
    if active_task and _looks_like_task_supplement(text):
        return (
            {
                'action': 'supplement',
                'task_id': str(active_task.get('public_id') or 'LATEST'),
                'requirement': (text or '').strip()[:4000],
                'confidence': 0.90,
            },
            {
                'provider': 'task-context-fallback',
                'model': 'active-task-context',
                'original_error': str(error)[:1000],
                'task_id': str(active_task.get('public_id') or 'LATEST'),
            },
        )
    prompt = (
        '你是 PersonZit 钉钉机器人，正在和用户正常交流。'
        '请用简体中文自然、友好、简洁地回复当前消息。'
        '不要声称已创建或执行任务；如果用户明显是在下达工程任务，提醒对方可以继续补充需求。\n'
        f'当前用户消息：{text or "<空消息>"}\n'
    )
    if history:
        prompt += '最近对话：\n' + '\n'.join(
            f"{item.get('role')}: {item.get('content')}" for item in history[-6:]
        ) + '\n'
    try:
        reply, metadata = complete_text(prompt, 'local', {'timeout_seconds': 30}, 30)
        return (
            {'action': 'chat', 'reply': str(reply).strip()[:2000], 'confidence': 0.35},
            {'provider': 'fallback-local', 'model': metadata.get('model'), 'original_error': error[:1000]},
        )
    except Exception as local_error:
        return (
            {
                'action': 'chat',
                'reply': '我在，不过这句话我还没完全理解。你可以继续用自然语言和我交流；如果是要我处理工程任务，可以直接描述需求。',
                'confidence': 0.1
            },
            {'provider': 'fallback-rules', 'original_error': error[:1000], 'fallback_error': str(local_error)[:1000]},
        )


def interpret_intent(text: str, history: list[dict] | None = None, active_task: dict|None=None) -> tuple[dict, dict]:
    """Return a safe internal command and parser metadata for logging/status."""
    cfg = load_config().get('dingtalk', {}).get('natural_language', {})
    enabled = bool(cfg.get('enabled', True))
    if not enabled:
        return {'action': 'unknown'}, {'provider': 'disabled'}

    heuristic = _heuristic(text)
    if heuristic and heuristic.get('confidence', 0) >= 0.9:
        return heuristic, {'provider': 'heuristic', 'model': 'rules'}

    history = list(history or [])[-10:]
    history_text = json.dumps(history, ensure_ascii=False) if history else '[]'
    active_context = 'none'
    if active_task:
        active_context = json.dumps({
            'task_id': active_task.get('public_id'),
            'status': active_task.get('status'),
            'title': active_task.get('title'),
            'requirement': str(active_task.get('description') or '')[:2000],
        }, ensure_ascii=False)
    prompt = _PROMPT % (_INTENT_SCHEMA, history_text, active_context, text or '')
    provider = str(cfg.get('provider', 'local')).lower()
    try:
        settings = provider_settings(provider, cfg)
        raw, meta = complete_json(prompt, settings['provider'], cfg, int(cfg.get('timeout_seconds', 30)))
        return sanitize_intent(raw), meta
    except Exception as error:
        if heuristic and (heuristic.get('confidence',0)>=0.9 or heuristic.get('action')=='inspect'):
            return heuristic,{'provider':'fallback','error':str(error)}
        return _fallback_chat(text,history,str(error),active_task)

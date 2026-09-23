from __future__ import annotations

import re
import unicodedata

_ALLOWED_RE = re.compile(r'[^0-9A-Za-z\u4e00-\u9fff_-]+')
_FILLER_RE = re.compile(r'^(?:请|麻烦|帮我|帮忙|我要|我想|需要|你给我|任务|一个|个|实现|修复|新增|添加|优化|检查|开发|编写|完成)+')
_FILE_RE = re.compile(r'\b[A-Za-z0-9_.-]+\.(?:xml|java|kt|py|ts|tsx|js|jsx|vue|go|rs|sql|md|yaml|yml|json)\b', re.I)
_ERROR_RE = re.compile(r'\b[A-Za-z]{2,}[-_]?\d{2,}\b')
_IDENTIFIER_RE = re.compile(r'\b[A-Za-z][A-Za-z0-9_]{2,}\b')


def _component(value: str, max_length: int = 28) -> str:
    value = unicodedata.normalize('NFKC', str(value or '')).strip()
    value = _ALLOWED_RE.sub('-', value)
    value = re.sub(r'-{2,}', '-', value).strip('-_')
    return value[:max_length].rstrip('-_')


def _headline(title: str) -> str:
    # The first clause usually carries the user's business problem.
    value = re.split(r'[，。；;：:（(【\[]', title or '', maxsplit=1)[0]
    value = _FILLER_RE.sub('', value.strip())
    value = re.sub(r'\s+', '-', value)
    component = _component(value, 24)
    return component


def _technical_keys(title: str) -> list[str]:
    keys: list[str] = []
    for match in _FILE_RE.findall(title or ''):
        stem = re.sub(r'\.[A-Za-z0-9]+$', '', match, flags=re.I)
        key = _component(stem, 32)
        if key and key.lower() not in keys:
            keys.append(key)
    for match in _ERROR_RE.findall(title or ''):
        key = _component(match, 24)
        if key and key.lower() not in keys:
            keys.append(key)
    if not keys:
        # Fall back to meaningful English identifiers in a mostly Chinese request.
        for match in _IDENTIFIER_RE.findall(title or ''):
            key = _component(match, 24)
            if key and key.lower() not in keys:
                keys.append(key)
            if len(keys) >= 2:
                break
    return keys


def semantic_task_id(title: str, sequence: int) -> str:
    """Build a readable task ID from the issue headline and technical clues.

    The trailing sequence is retained only for uniqueness and traceability; it is
    not the sole identity as in the legacy TASK-000001 format.
    """
    parts = [part for part in [_headline(title), *_technical_keys(title)] if part]
    slug = '-'.join(dict.fromkeys(parts))[:64].rstrip('-_') or 'task'
    return f'TASK-{slug}-{sequence:06d}'

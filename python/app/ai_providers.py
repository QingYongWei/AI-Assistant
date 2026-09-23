"""Shared provider resolution for local AI CLIs and OpenAI-compatible APIs."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request

try:
    import winreg
except ImportError:
    winreg=None

from .config import load_config

PROVIDER_DEFAULTS={
    'zhipu': {'base_url':'https://open.bigmodel.cn/api/paas/v4','api_key_env':'ZHIPU_API_KEY'},
    'glm': {'base_url':'https://open.bigmodel.cn/api/paas/v4','api_key_env':'ZHIPU_API_KEY'},
    'bailian': {'base_url':'https://dashscope.aliyuncs.com/compatible-mode/v1','api_key_env':'DASHSCOPE_API_KEY'},
    'dashscope': {'base_url':'https://dashscope.aliyuncs.com/compatible-mode/v1','api_key_env':'DASHSCOPE_API_KEY'},
    'openai_compatible': {'base_url':'','api_key_env':'OPENAI_API_KEY'},
}


def parse_json_object(text: str) -> dict:
    value=(text or '').strip()
    if value.startswith('```'):
        value=value.split('\n',1)[1] if '\n' in value else value
        if value.rstrip().endswith('```'): value=value.rstrip()[:-3]
    start=value.find('{'); end=value.rfind('}')
    if start<0 or end<=start: raise ValueError('model output does not contain a JSON object')
    result=json.loads(value[start:end+1])
    if not isinstance(result,dict): raise ValueError('model output is not a JSON object')
    return result


def environment_value(name: str) -> str|None:
    """Read a process env var, then Windows User/Machine env on stale parents."""
    value=os.environ.get(name)
    if value or winreg is None or not name:
        return value
    locations=(
        (winreg.HKEY_CURRENT_USER, 'Environment'),
        (winreg.HKEY_LOCAL_MACHINE, r'SYSTEM\CurrentControlSet\Control\Session Manager\Environment'),
    )
    for root,path in locations:
        try:
            with winreg.OpenKey(root,path) as key:
                result,_=winreg.QueryValueEx(key,name)
                if result:
                    return str(result)
        except OSError:
            continue
    return None


def provider_settings(provider: str, scope: dict|None=None):
    """Merge global provider defaults with a usage-specific override."""
    scope=scope or {}
    configured=(load_config().get('ai_providers',{}) or {}).get(provider,{})
    preset=PROVIDER_DEFAULTS.get(provider,PROVIDER_DEFAULTS['openai_compatible'])
    result={**preset,**(configured if isinstance(configured,dict) else {}),**scope}
    result['provider']=provider
    return result


def resolve_local_agent(scope: dict):
    agents=load_config().get('agents',{})
    preferred=scope.get('agent') or load_config().get('planner',{}).get('agent') or 'claude'
    for name in [preferred,'claude','codex']:
        agent_cfg=agents.get(name,{})
        executable=agent_cfg.get('executable',name)
        if agent_cfg.get('enabled',False) and shutil.which(executable):
            return name,shutil.which(executable)
    return None,None


def local_command(agent: str,executable: str,prompt: str):
    # Intent parsing and planning are prompt-only calls. read-only keeps local
    # supervisor-mode parsing from granting filesystem write access.
    if agent=='codex': return [executable,'exec','--sandbox','read-only','--skip-git-repo-check',prompt]
    if agent=='claude': return [executable,'-p',prompt]
    return [executable,prompt]


def complete_local_text(prompt: str, working_directory: str, agent_name: str|None=None, timeout: int=300) -> tuple[str,dict]:
    """Run a local coding agent in read-only mode for file/project/document inspection."""
    config=load_config()
    agents=config.get('agents',{}) or {}
    preferred=agent_name or agents.get('default') or config.get('planner',{}).get('agent') or 'codex'
    agent,executable=resolve_local_agent({'agent':preferred})
    if not executable:
        raise RuntimeError('no enabled local codex/claude agent')

    if agent == 'codex':
        argv=[executable,'exec','--sandbox','read-only','--skip-git-repo-check',prompt]
    elif agent == 'claude':
        argv=[executable,'-p',prompt,'--permission-mode','plan']
    else:
        raise RuntimeError(f'unsupported local inspection agent: {agent}')

    started=time.monotonic()
    completed=subprocess.run(argv,cwd=working_directory,capture_output=True,text=True,
        encoding='utf8',errors='replace',timeout=max(5,int(timeout)),
        creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    if completed.returncode != 0:
        detail=(completed.stderr or completed.stdout or '').strip()[-1200:]
        raise RuntimeError(f'local {agent} exited {completed.returncode}: {detail}')
    return completed.stdout, {
        'provider':'local-agent',
        'model':agent,
        'working_directory':str(working_directory),
        'mode':'read-only' if agent=='codex' else 'plan',
        'duration_ms':round((time.monotonic()-started)*1000)
    }


def _chat_endpoint(base_url: str) -> str:
    url=str(base_url or '').rstrip('/')
    if not url: raise RuntimeError(f'{base_url!r} is not a valid AI provider base_url')
    if not url.endswith('/chat/completions'): url+='/chat/completions'
    return url


def complete_text(prompt: str,provider: str,scope: dict|None=None,timeout: int=60) -> tuple[str,dict]:
    """Call a configured provider and return plain model text plus safe metadata."""
    settings=provider_settings(provider,scope)
    started=time.monotonic()
    if settings['provider'] in ('local','claude','codex'):
        local_scope=dict(settings)
        if settings['provider'] in ('claude','codex'): local_scope['agent']=settings['provider']
        agent,executable=resolve_local_agent(local_scope)
        if not executable: raise RuntimeError('no enabled local claude/codex model')
        argv=local_command(agent,executable,prompt)
        completed=subprocess.run(argv,capture_output=True,text=True,encoding='utf8',errors='replace',
            timeout=max(5,int(settings.get('timeout_seconds') or timeout)),
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        if completed.returncode!=0:
            raise RuntimeError(f'local model exited {completed.returncode}: {(completed.stderr or completed.stdout or "").strip()[-500:]}')
        return completed.stdout,{'provider':'local','model':agent,'mode':'read-only','duration_ms':round((time.monotonic()-started)*1000)}

    provider_name=settings['provider']
    model=str(settings.get('model') or '').strip()
    if not model: raise RuntimeError(f'model is required for AI provider {provider_name!r}')
    env_name=str(settings.get('api_key_env') or preset_for(provider_name)['api_key_env'])
    api_key=environment_value(env_name)
    if not api_key: raise RuntimeError(f'environment variable {env_name} is not set')
    endpoint=_chat_endpoint(settings.get('base_url'))
    payload={'model':model,'temperature':float(settings.get('temperature',0)),'max_tokens':int(settings.get('max_tokens',4096)),
             'messages':[{'role':'user','content':prompt}]}
    request=urllib.request.Request(endpoint,data=json.dumps(payload,ensure_ascii=False).encode('utf8'),
        headers={'Content-Type':'application/json','Authorization':f'Bearer {api_key}'})
    try:
        with urllib.request.urlopen(request,timeout=max(5,int(settings.get('timeout_seconds') or timeout))) as response:
            body=json.loads(response.read().decode('utf8'))
    except urllib.error.HTTPError as error:
        detail=error.read().decode('utf8',errors='replace')[:800]
        raise RuntimeError(f'AI provider HTTP {error.code}: {detail}') from error
    try: content=body['choices'][0]['message']['content']
    except (KeyError,IndexError,TypeError) as error: raise RuntimeError('AI provider response has no message content') from error
    return content,{'provider':provider_name,'model':model,'duration_ms':round((time.monotonic()-started)*1000)}


def complete_json(prompt: str,provider: str,scope: dict|None=None,timeout: int=60) -> tuple[dict,dict]:
    text,metadata=complete_text(prompt,provider,scope,timeout)
    return parse_json_object(text),metadata


def preset_for(provider: str) -> dict:
    return PROVIDER_DEFAULTS.get(provider,PROVIDER_DEFAULTS['openai_compatible'])

def provider_status() -> list[dict]:
    result=[]
    configured=load_config().get('ai_providers',{}) or {}
    names=['local']+[x for x in ['zhipu','bailian','openai_compatible'] if x in configured]
    for provider in names:
        if provider=='local':
            agent,_=resolve_local_agent({'agent':load_config().get('planner',{}).get('agent','claude')})
            result.append({'name':'local','type':'local','configured':bool(agent),'model':agent or ''})
            continue
        settings=provider_settings(provider,{})
        env_name=str(settings.get('api_key_env') or '')
        result.append({'name':provider,'type':'openai_compatible','configured':bool(settings.get('model') and environment_value(env_name)),
            'model':str(settings.get('model') or ''),'base_url':str(settings.get('base_url') or ''),
            'api_key_env':env_name})
    return result


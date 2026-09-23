from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

from .config import home as personzit_home, load_config

_LOCK=threading.RLock()
_EXTERNAL_RE=re.compile(r'^EXT-(?:CODEX|CLAUDE)-\d{4}$',re.I)


@dataclass
class ExternalEvent:
    kind: str
    agent: str
    external_id: str
    session_id: str
    title: str
    workspace: str
    summary: str
    status: str


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: float|datetime|str|None) -> str:
    if isinstance(value,datetime): return value.astimezone(timezone.utc).isoformat()
    if isinstance(value,(int,float)):
        try: return datetime.fromtimestamp(value,tz=timezone.utc).isoformat()
        except (OSError,ValueError): return _utcnow().isoformat()
    return str(value or _utcnow().isoformat())


def _parse_ts(value: str|None) -> datetime|None:
    if not value: return None
    try:
        result=datetime.fromisoformat(str(value).replace('Z','+00:00'))
        if result.tzinfo is None: result=result.replace(tzinfo=timezone.utc)
        return result.astimezone(timezone.utc)
    except (TypeError,ValueError): return None


def _state_path(state_home: Path|None) -> Path:
    return (state_home or personzit_home())/'runtime'/'external-agent-monitor.json'


def _empty_state() -> dict:
    return {'version':1,'next_external_sequence':1,'sessions':{},'pending_notifications':[]}

def _load_state(state_home: Path|None=None) -> dict:
    """Load monitor state; fall back to the backup if the primary file is torn.

    Windows 上状态文件可能被杀毒/备份/并读短暂锁住。以前一旦读到半截 JSON
    就返回空状态并继续保存，会把全部会话重置为 baseline，导致进行中的
    task_started/task_complete 被当成历史吞掉（多窗口时表现为漏通知）。
    """
    path=_state_path(state_home)
    found_any=False
    for candidate in (path,path.with_name(path.name+'.bak')):
        try:
            if not candidate.exists(): continue
            found_any=True
            value=json.loads(candidate.read_text(encoding='utf8'))
            if isinstance(value,dict) and isinstance(value.get('sessions'),dict): return value
        except (OSError,ValueError,TypeError):
            continue
    state=_empty_state()
    # 只有“文件存在但解析失败”才视为不可读；全新环境没有状态文件属正常初始化。
    if found_any: state['unreadable']=True
    return state

def _save_state(state: dict,state_home: Path|None=None) -> None:
    """Atomically replace the state file and keep the previous copy as backup."""
    path=_state_path(state_home); path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name(path.name+'.tmp')
    payload=dict(state); payload.pop('unreadable',None)
    tmp.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf8',newline='')
    backup=path.with_name(path.name+'.bak')
    try:
        if path.exists(): os.replace(path,backup)
    except OSError:
        pass
    os.replace(tmp,path)


def _settings() -> dict:
    cfg=load_config().get('external_monitor',{}) or {}
    return {
        'enabled':bool(cfg.get('enabled',True)),
        'lookback_hours':max(1,int(cfg.get('lookback_hours',24*7))),
        'list_hours':max(1,int(cfg.get('list_hours',24))),
        'notify_started':bool(cfg.get('notify_started',True)),
        'notify_claude_updates':bool(cfg.get('notify_claude_updates',True)),
        # Claude 每条 end-turn 输出不再立即推送：静默 quiet 秒后合并为一条；
        # 持续活跃超过 max_wait 秒也强制推一条，避免长任务全程无更新。
        'claude_update_quiet_seconds':max(30,int(cfg.get('claude_update_quiet_seconds',90))),
        'claude_update_max_wait_seconds':max(120,int(cfg.get('claude_update_max_wait_seconds',900))),
        'command_timeout_seconds':max(30,int(cfg.get('command_timeout_seconds',1800))),
    }


def _clean_text(value,limit=1600) -> str:
    if isinstance(value,list):
        parts=[]
        for item in value:
            if isinstance(item,dict) and item.get('type') in ('output_text','input_text','text'):
                parts.append(str(item.get('text') or ''))
            elif isinstance(item,str): parts.append(item)
        value='\n'.join(x for x in parts if x.strip())
    text=str(value or '').strip()
    text=re.sub(r'\n{3,}','\n\n',text)
    return text[:limit].rstrip()


def _record_snapshot(record: dict) -> dict:
    return {
        'external_id':record.get('external_id'),
        'agent':record.get('agent'),
        'session_id':record.get('session_id'),
        'title':record.get('title') or '未记录任务标题',
        'workspace':record.get('workspace') or record.get('cwd') or '',
        'status':record.get('status') or 'IDLE',
        'summary':record.get('summary') or '',
        'path':record.get('path') or '',
        'updated_at':record.get('updated_at'),
    }


def _new_record(agent: str,session_id: str,path: Path,sequence: int) -> dict:
    prefix='CODEX' if agent=='codex' else 'CLAUDE'
    now=_utcnow().isoformat()
    return {'external_id':f'EXT-{prefix}-{sequence:04d}','agent':agent,'session_id':session_id,
            'path':str(path),'offset':0,'cwd':'','title':'','summary':'','status':'DISCOVERED',
            'created_at':now,'updated_at':now,'last_event_at':now,'manual':False}


def _codex_files(user_home: Path,lookback_hours: int) -> list[Path]:
    root=user_home/'.codex'/'sessions'
    if not root.exists(): return []
    cutoff=time.time()-lookback_hours*3600
    return sorted((p for p in root.rglob('*.jsonl') if p.is_file() and p.stat().st_mtime>=cutoff),key=lambda p:p.stat().st_mtime)


def _claude_files(user_home: Path,lookback_hours: int) -> list[Path]:
    root=user_home/'.claude'/'projects'
    if not root.exists(): return []
    cutoff=time.time()-lookback_hours*3600
    return sorted((p for p in root.glob('*/*.jsonl') if p.is_file() and p.stat().st_mtime>=cutoff),key=lambda p:p.stat().st_mtime)


def _json_line(line: bytes) -> dict|None:
    try:
        value=json.loads(line.decode('utf8',errors='replace'))
        return value if isinstance(value,dict) else None
    except (ValueError,UnicodeDecodeError): return None


def _read_new_lines(path: Path,offset: int) -> tuple[list[dict],int]:
    try:
        size=path.stat().st_size
        if size<offset: offset=0
        with path.open('rb') as f:
            f.seek(offset); data=f.read()
        if data and not data.endswith(b'\n'):
            complete_end=data.rfind(b'\n')+1
            data=data[:complete_end]; size=offset+complete_end
        return [_json_line(line) for line in data.splitlines() if line.strip()],size
    except OSError:
        return [],offset


def _codex_metadata(path: Path) -> tuple[str,str,str]:
    try:
        with path.open('rb') as f: first=f.readline()
    except OSError: return '','',''
    value=_json_line(first) or {}
    payload=value.get('payload') or {}
    session_id=str(payload.get('session_id') or payload.get('id') or path.stem)
    originator=str(payload.get('originator') or '')
    cwd=str(payload.get('cwd') or '')
    return session_id,originator,cwd


def _apply_codex_event(record: dict,event: dict) -> ExternalEvent|None:
    kind=str(event.get('type') or ''); payload=event.get('payload') or {}
    timestamp=_iso(event.get('timestamp'))
    if kind=='response_item':
        message=payload if payload.get('type')=='message' else {}
        if message.get('role')=='user':
            text=_clean_text(message.get('content'),500)
            if text and not text.startswith('<'):
                record['title']=text
        return None
    event_type=str(payload.get('type') or '')
    if event_type=='task_started':
        record['status']='RUNNING'; record['summary']=''; record['updated_at']=timestamp; record['last_event_at']=timestamp
        return ExternalEvent('started','codex',record['external_id'],record['session_id'],record['title'],record['cwd'],'',record['status'])
    if event_type=='task_complete':
        summary=_clean_text(payload.get('last_agent_message'))
        record['status']='COMPLETED'; record['summary']=summary or 'Codex 已结束本轮任务，但日志中没有最终消息。'
        record['updated_at']=timestamp; record['last_event_at']=timestamp
        return ExternalEvent('completed','codex',record['external_id'],record['session_id'],record['title'],record['cwd'],record['summary'],record['status'])
    return None


def _claude_is_manual(event: dict) -> bool:
    if event.get('type')!='user': return False
    if str(event.get('entrypoint') or '').lower()!='cli': return False
    if str(event.get('promptSource') or '').lower() not in ('typed',''): return False
    message=event.get('message') or {}
    if message.get('role')!='user': return False
    text=_clean_text(message.get('content'),300)
    return bool(text and not text.startswith('<task-notification') and not text.startswith('<'))


def _apply_claude_event(record: dict,event: dict,notify_updates: bool=True) -> ExternalEvent|None:
    timestamp=_iso(event.get('timestamp'))
    if event.get('type')=='user':
        if not _claude_is_manual(event): return None
        record['manual']=True
        message=event.get('message') or {}
        record['title']=_clean_text(message.get('content'),500)
        record['status']='RUNNING'; record['summary']=''
        record['updated_at']=timestamp; record['last_event_at']=timestamp
        return ExternalEvent('started','claude',record['external_id'],record['session_id'],record['title'],record['cwd'],'',record['status'])
    if event.get('type')!='assistant' or not record.get('manual'): return None
    message=event.get('message') or {}
    text=''
    for item in message.get('content') or []:
        if isinstance(item,dict) and item.get('type')=='text':
            text=_clean_text(item.get('text'))
            if text: break
    if not text: return None
    record['cwd']=str(event.get('cwd') or record.get('cwd') or '')
    record['summary']=text
    # Claude 没有可靠的“本轮结束”标记（后台 subagent 可能续写同一会话）。
    # 每条 end-turn 输出仍会更新会话状态，但通知在 scan_external_agents
    # 中按会话缓冲合并，等输出静默后只推最后一条，避免逐步推理刷屏。
    record['status']='WAITING_INPUT_OR_BACKGROUND'
    record['updated_at']=timestamp; record['last_event_at']=timestamp
    if not notify_updates: return None
    return ExternalEvent('update','claude',record['external_id'],record['session_id'],record['title'],record['cwd'],text,record['status'])


def scan_external_agents(user_home: Path|None=None,state_home: Path|None=None) -> tuple[list[dict],list[dict]]:
    """Discover and incrementally parse manual Codex/Claude sessions.

    Returned values are current snapshots and pending notifications. Existing
    sessions are baselined on first discovery so PersonZit does not replay old
    history as new DingTalk messages.
    """
    settings=_settings()
    if not settings['enabled']: return [],[]
    user_home=Path(user_home or Path.home())
    with _LOCK:
        state=_load_state(state_home)
        if state.get('unreadable'):
            # 主/备状态均不可读：宁可本轮不扫描，也不能用空状态覆盖真实进度。
            return [],[]
        sessions=state['sessions']
        candidates:list[tuple[str,Path,str,str]]=[]
        for path in _codex_files(user_home,settings['lookback_hours']):
            session_id,originator,cwd=_codex_metadata(path)
            if originator in ('codex-tui','codex_cli','cli'):
                candidates.append(('codex',path,session_id,cwd))
        for path in _claude_files(user_home,settings['lookback_hours']):
            # Session id and cwd are learned while parsing; path identity is stable.
            candidates.append(('claude',path,f'claude:{path.stem}',''))

        discovered:dict[tuple[str,str],dict]={}
        for agent,path,session_id,fallback_cwd in candidates:
            key=f'{agent}:{session_id}'
            record=sessions.get(key)
            if not record:
                record=_new_record(agent,session_id,path,state['next_external_sequence'])
                state['next_external_sequence']+=1
                sessions[key]=record
                record['baseline']=True
            record['path']=str(path)
            if fallback_cwd and not record.get('cwd'): record['cwd']=fallback_cwd
            discovered[key]=record

        pending=state.setdefault('pending_notifications',[])
        for key,record in discovered.items():
            path=Path(record['path'])
            events,offset=_read_new_lines(path,int(record.get('offset') or 0))
            baseline=bool(record.get('baseline'))
            produced=[]
            for event in events:
                if event is None: continue
                if record['agent']=='codex':
                    item=_apply_codex_event(record,event)
                else:
                    if not record.get('session_id') or record['session_id']==f"claude:{path.stem}":
                        record['session_id']=str(event.get('sessionId') or record['session_id'])
                    if not record.get('cwd'): record['cwd']=str(event.get('cwd') or '')
                    item=_apply_claude_event(record,event,settings['notify_claude_updates'])
                if item: produced.append(item)
            record['offset']=offset
            if baseline:
                # Do not notify history that predates monitor ownership.
                record.pop('baseline',None)
                if record.get('status')=='DISCOVERED': record['status']='IDLE'
                record['updated_at']=record.get('updated_at') or _utcnow().isoformat()
                if record['agent']=='claude' and not record.get('manual'):
                    # 保留 IGNORED 记录而不是从状态中删除：删除会导致每轮扫描
                    # 重新发现、重新编号并重复 baseline（EXT ID 漂移 + 序号膨胀）。
                    record['status']='IGNORED'
                elif record.get('status')=='RUNNING' and settings['notify_started']:
                    # 发现时会话已在运行（例如用户刚打开的多个 Codex 窗口）：
                    # 历史可以静默基线化，但进行中任务的 started 必须补发通知。
                    for item in produced:
                        if item.kind!='started': continue
                        pending.append({
                            'kind':'started','agent':item.agent,'external_id':item.external_id,
                            'session_id':item.session_id,'title':record.get('title') or item.title,
                            'workspace':item.workspace,'summary':item.summary,'status':item.status,
                        })
            else:
                for item in produced:
                    if item.kind=='started' and not settings['notify_started']: continue
                    if item.kind=='update':
                        # 每条 end-turn 输出先缓冲（保留最早入队时间），不立即推送。
                        buffered=record.get('pending_update') or {'buffered_at':_utcnow().isoformat()}
                        buffered['summary']=item.summary
                        record['pending_update']=buffered
                        continue
                    pending.append({
                        'kind':item.kind,'agent':item.agent,'external_id':item.external_id,
                        'session_id':item.session_id,'title':item.title,'workspace':item.workspace,
                        'summary':item.summary,'status':item.status,
                    })
                if record.get('title'):
                    for item in pending:
                        if item.get('external_id')==record.get('external_id') and item.get('kind')=='started':
                            item['title']=record['title']
        # 会话静默（或活跃超过 max_wait）后，把缓冲的 update 合并为一条通知。
        now=time.time()
        for record in sessions.values():
            buffered=record.get('pending_update')
            if not buffered: continue
            last=_parse_ts(record.get('last_event_at'))
            last_age=now-(last.timestamp() if last else 0)
            buffered_at=_parse_ts(buffered.get('buffered_at'))
            buffered_age=now-(buffered_at.timestamp() if buffered_at else 0)
            if last_age>=settings['claude_update_quiet_seconds'] or buffered_age>=settings['claude_update_max_wait_seconds']:
                pending.append({
                    'kind':'update','agent':record['agent'],'external_id':record['external_id'],
                    'session_id':record['session_id'],'title':record.get('title') or '',
                    'workspace':record.get('cwd') or '','summary':buffered.get('summary') or '',
                    'status':record.get('status'),
                })
                record.pop('pending_update',None)
        # Keep bounded state; old sessions remain queryable by ID until pruned.
        if len(sessions)>500:
            ordered=sorted(sessions.items(),key=lambda kv:str(kv[1].get('last_event_at') or ''),reverse=True)
            state['sessions']=sessions=dict(ordered[:500])
        if len(pending)>100: del pending[:-100]
        state['scanned_at']=_utcnow().isoformat()
        _save_state(state,state_home)
        cutoff=time.time()-settings['list_hours']*3600
        snapshots=[]
        for record in sessions.values():
            if record.get('status')=='IGNORED': continue
            try: recent=Path(record.get('path') or '').stat().st_mtime>=cutoff
            except OSError: recent=False
            if recent: snapshots.append(_record_snapshot(record))
        snapshots.sort(key=lambda x:str(x.get('updated_at') or ''),reverse=True)
        notifications=[dict(x) for x in pending]
        return snapshots,notifications


def drain_external_notifications(state_home: Path|None=None,sender: Callable[[str,str],bool]|None=None) -> list[dict]:
    """Send queued notifications; only successful sends are removed."""
    with _LOCK:
        state=_load_state(state_home)
        if state.get('unreadable'):
            return []
        pending=state.setdefault('pending_notifications',[])
        remaining=[]
        for item in pending:
            title,markdown=notification_message(item)
            ok=bool(sender(title,markdown)) if sender else False
            if not ok: remaining.append(item)
        state['pending_notifications']=remaining
        _save_state(state,state_home)
        return [x for x in pending if x not in remaining]


_EXTERNAL_STATUS_LABELS={
    'RUNNING':'执行中',
    'COMPLETED':'已完成',
    'WAITING_INPUT_OR_BACKGROUND':'等待输入或后台运行',
    'IDLE':'空闲',
    'DISCOVERED':'已发现',
    'IGNORED':'已忽略',
    'INTERRUPTED':'已中断',
}

def _external_status_label(status) -> str:
    value=str(status or '')
    return _EXTERNAL_STATUS_LABELS.get(value,value)

def _distill(text,limit=400) -> str:
    """提纯通知正文：去代码块与 markdown 修饰，只保留前几行可读文本。"""
    value=re.sub(r'```[\s\S]*?```','（代码已省略）',str(text or ''))
    value=re.sub(r'`([^`\n]+)`',r'\1',value)
    lines=[]
    for raw in value.splitlines():
        line=re.sub(r'^\s{0,3}#{1,6}\s+','',raw)
        line=re.sub(r'^\s*[-*+]\s+','',line)
        line=re.sub(r'^\s*>\s?','',line).strip()
        if line: lines.append(line)
    if len(lines)>3: lines=lines[:3]+['…']
    body='\n'.join(lines)
    return body[:limit].rstrip()+('…' if len(body)>limit else '')


def notification_message(event: dict) -> tuple[str,str]:
    agent=str(event.get('agent') or 'agent').upper()
    external_id=str(event.get('external_id') or '')
    kind=str(event.get('kind') or 'update')
    label={'started':'🚀 手动任务开始','update':'📢 手动会话输出','completed':'✅ 手动任务完成'}.get(kind,'📢 手动会话事件')
    lines=[
        f'### {label}',
        f'**会话**：`{external_id}` ({agent})',
        f'**任务**：{_distill(event.get("title") or "未记录任务标题",80)}',
    ]
    if event.get('workspace'): lines.append(f'**工作目录**：{event["workspace"]}')
    if event.get('status'): lines.append(f'**状态**：{_external_status_label(event["status"])}')
    summary=_distill(event.get('summary'),400)
    if summary: lines+=['',summary]
    lines+=['',f'进一步指挥：回复 `指挥 {external_id} <修订要求>`']
    return f'{external_id} {agent} 手动任务','\n\n'.join(lines)


def list_external_sessions(state_home: Path|None=None,max_age_hours: int|None=None) -> list[dict]:
    settings=_settings(); hours=settings['list_hours'] if max_age_hours is None else max_age_hours
    with _LOCK:
        state=_load_state(state_home); cutoff=time.time()-hours*3600; result=[]
        for record in state.get('sessions',{}).values():
            if record.get('status')=='IGNORED': continue
            try: recent=Path(record.get('path') or '').stat().st_mtime>=cutoff
            except OSError: recent=False
            if recent: result.append(_record_snapshot(record))
        result.sort(key=lambda x:str(x.get('updated_at') or ''),reverse=True)
        return result


def get_external_session(external_id: str,state_home: Path|None=None) -> dict|None:
    value=str(external_id or '').strip().upper()
    with _LOCK:
        for record in _load_state(state_home).get('sessions',{}).values():
            if str(record.get('external_id') or '').upper()==value:
                return _record_snapshot(record)
    return None


def _no_window() -> int:
    return subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0


def send_instruction(external_id: str,instruction: str,state_home: Path|None=None) -> str:
    """Send a follow-up to a discovered manual session.

    Codex uses its native queue API for a live TUI thread. Claude resumes the
    selected session in print mode and returns the result to DingTalk.
    """
    record=get_external_session(external_id,state_home)
    if not record or not _EXTERNAL_RE.match(str(record.get('external_id') or '')):
        return f'未找到外部会话 {external_id}。可先发送“运行”查看 EXT 会话列表。'
    text=str(instruction or '').strip()[:4000]
    if not text: return '请输入要发送给外部会话的指令内容。'
    agent=str(record.get('agent') or '').lower(); session_id=str(record.get('session_id') or '')
    executable=shutil.which('codex' if agent=='codex' else 'claude')
    if not executable: return f'未找到本机 {agent} 可执行文件。'
    cwd=str(record.get('workspace') or '') or None
    try:
        if agent=='codex':
            command=[executable,'queue','--thread',session_id,'--message',text]
            result=subprocess.run(command,capture_output=True,text=True,encoding='utf8',errors='replace',
                                  timeout=min(60,_settings()['command_timeout_seconds']),cwd=cwd,creationflags=_no_window())
            output=(result.stdout or result.stderr or '').strip()
            suffix=f'\n\n输出：{output[:1200]}' if output else ''
            return f'已把修订指令发送到 {record["external_id"]}（Codex 原会话）。{suffix}\n\n后续完成时会通过钉钉通知。'
        command=[executable,'--resume',session_id,'--print',text]
        result=subprocess.run(command,capture_output=True,text=True,encoding='utf8',errors='replace',
                              timeout=_settings()['command_timeout_seconds'],cwd=cwd,creationflags=_no_window())
        output=(result.stdout or '').strip(); error=(result.stderr or '').strip()
        if result.returncode!=0:
            return f'Claude 会话 {record["external_id"]} 指令执行失败（exit={result.returncode}）：{error[:1200]}'
        return f'Claude 会话 {record["external_id"]} 已执行修订指令：\n\n{output[:3500]}'
    except subprocess.TimeoutExpired:
        return f'{record["external_id"]} 指令执行超时，请稍后发送“运行”查看状态。'
    except OSError as e:
        return f'发送外部会话指令失败：{e}'

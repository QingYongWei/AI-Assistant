"""钉钉双向集成：Stream 模式入站（发任务/审批/澄清/查询）+ Webhook 出站通知。
未启用或未配置时安全降级为本地记录，不影响本地任务执行。"""
import hashlib,hmac,base64,time,json,re,threading,os,logging,asyncio,uuid
import urllib.request,urllib.parse
from pathlib import Path
from datetime import datetime,timezone,timedelta
from sqlalchemy import select
from .config import home,load_config
from .logging_config import configure_logger,log_file_path
from .database import SessionLocal
from .models import Task,SubTask,Approval,EventLog,Job,AgentRun,VerificationRun
from .state_machine import transition,status_label
from .queue import SqliteQueue
from .task_ids import semantic_task_id
from .intents import interpret_intent,_looks_like_task_supplement
from .ai_providers import complete_local_text
from . import process_registry
from . import external_agents

logger=configure_logger('dingtalk')

_state_lock=threading.RLock()
_conversation_history={}
_conversation_history_limit=10

def _route_payload(**values):
    # Build a compact, secret-free route payload for service logs.
    result={k:v for k,v in values.items() if v is not None}
    for key in ('text','summary','intent_reply'):
        if key in result: result[key]=str(result[key])[:500].replace('\n',' ')
    if 'error' in result: result['error']=str(result['error'])[:500]
    return result

def action_if_defined(cmd):
    try: return str(cmd.get('action') or 'unknown')
    except Exception: return 'unknown'


def _log_route(message_id,**values):
    logger.info('route message_id=%s %s',message_id,json.dumps(_route_payload(**values),ensure_ascii=False,separators=(',',':')))

def _local_agent_mode(agent: str) -> str:
    return {'codex':'read-only','claude':'plan'}.get(str(agent).lower(),'unknown')


def _builtin_executor(action: str) -> str:
    return {
        'help':'builtin-help',
        'identity':'dingtalk-identity',
        'list':'task-db:list-recent',
        'status':'task-db:status',
        'running':'task-db:running-jobs',
        'continue':'task-state-machine:continue',
        'retry':'task-state-machine:retry',
        'reverify':'task-state-machine:reverify',
        'workspace':'conversation-state:workspace',
        'set_workspace':'conversation-state:set-workspace',
        'clear_workspace':'conversation-state:clear-workspace',
        'cancel':'task-process-registry+task-db:cancel',
        'supplement':'task-db+queue:supplement',
        'external_command':'external-agent-session:resume-or-queue',
        'approve':'task-db:approve',
        'reject':'task-db:reject',
        'clarify':'task-db:clarify',
        'need_task_id':'validation:task-id-required',
    }.get(str(action or ''),f'task-db:{action or "unknown"}')


def _route_decision(action: str,executor: str) -> str:
    action=str(action or 'unknown')
    if action=='chat': return 'cloud-model generated chat reply'
    if action=='inspect': return 'path-whitelist -> local read-only agent'
    if action=='create': return 'task queue -> planner -> clarification-or-auto-execute'
    if action=='supplement': return 'append task context and queue revision for local agent re-execution'
    if action=='help': return 'built-in help text'
    if action=='identity': return 'built-in identity text'
    if action=='list': return 'query recent tasks from SQLite'
    if action=='status': return 'query task status from SQLite'
    if action=='running': return 'query running/queued jobs and live local-agent processes'
    if action=='continue': return 'resolve latest task state and approve/resume when safely allowed'
    if action=='retry': return 'replan a task stuck waiting for human with all supplements'
    if action=='reverify': return 're-run verification commands without re-executing agents'
    if action=='workspace': return 'read persisted conversation workspace'
    if action=='set_workspace': return 'validate path whitelist and persist conversation workspace'
    if action=='clear_workspace': return 'reset persisted conversation workspace'
    if action=='cancel': return 'terminate registered agent processes and update task through SQLite'
    if action=='external_command': return 'send follow-up instruction to a discovered manual Codex/Claude session'
    if action in ('approve','reject','clarify'): return f'update task through SQLite ({action})'
    return 'built-in command handler'

def _conversation_key(sender,conversation):
    return f'{sender or "<unknown>"}|{conversation or "<unknown>"}'

def _conversation_state_path():
    directory=home()/'runtime'; directory.mkdir(parents=True,exist_ok=True)
    return directory/'conversations.json'

def _load_conversation_state():
    path=_conversation_state_path()
    try:
        value=json.loads(path.read_text(encoding='utf8'))
        if isinstance(value,dict) and isinstance(value.get('conversations'),dict):
            return value
    except (OSError,ValueError,TypeError):
        pass
    return {'conversations':{}}

def _save_conversation_state(state):
    path=_conversation_state_path()
    path.write_text(json.dumps(state,ensure_ascii=False,indent=2),encoding='utf8')

def _conversation_record(sender,conversation):
    key=_conversation_key(sender,conversation)
    state=_load_conversation_state()
    record=state['conversations'].setdefault(key,{'history':[],'workspace':None})
    if not isinstance(record.get('history'),list): record['history']=[]
    return state,record

def _get_conversation_history(sender,conversation):
    with _state_lock:
        state,record=_conversation_record(sender,conversation)
        return list(record.get('history',[]))

def _remember_conversation(sender,conversation,user_text,bot_reply):
    with _state_lock:
        state,record=_conversation_record(sender,conversation)
        history=record.setdefault('history',[])
        history.append({'role':'user','content':str(user_text or '')[:4000]})
        history.append({'role':'assistant','content':str(bot_reply or '')[:4000]})
        del history[:-(_conversation_history_limit*2)]
        _save_conversation_state(state)

def _remember_assistant_message(sender,conversation,bot_reply):
    """Persist an intermediate reply without duplicating the inbound user turn."""
    with _state_lock:
        state,record=_conversation_record(sender,conversation)
        history=record.setdefault('history',[])
        history.append({'role':'assistant','content':str(bot_reply or '')[:4000]})
        del history[:-(_conversation_history_limit*2)]
        _save_conversation_state(state)


def _priority_ack_text(action,cmd):
    """Return an immediate pre-execution acknowledgement for slow local work."""
    action=str(action or '')
    if action=='create':
        requirement=str(cmd.get('requirement') or '').strip()
        if not requirement: return None
        project=str(cmd.get('project') or '').strip()
        lines=[
            '已理解你的工程任务：',
            requirement[:600],
            '',
            '接下来我会：',
            '1. 创建语义任务 ID 并加入规划队列',
            '2. 需求明确时自动调用本地 Codex/Claude 执行，不再等待人工审批',
            '3. 信息不足时向你追问，你的后续回复会自动并入当前任务上下文',
            '4. 执行、验证完成或失败后主动推送结果',
        ]
        if project: lines.append('\n项目/工作目录：'+project)
        return '\n'.join(lines)
    if action=='supplement':
        requirement=str(cmd.get('requirement') or '').strip()
        if not requirement: return None
        lines=[
            '已理解这是对当前执行任务的补充/纠错：',
            requirement[:600],
            '',
            f'目标任务：{cmd.get("task_id") or "LATEST"}',
            '',
            '接下来我会：',
            '1. 把这条规则写入该任务的需求上下文和事件日志',
            '2. 当前已在运行的 Agent 完成本轮后，带着补充规则重新执行相关子任务',
            '3. 若处于待审批/待澄清阶段，则将补充规则并入后续规划',
            '4. 修订执行、验证完成或失败后主动推送结果',
        ]
        return '\n'.join(lines)
    if action=='clarify':
        answer=str(cmd.get('answer') or cmd.get('requirement') or '').strip()
        if not answer: return None
        return '\n'.join([
            '已把这条回复识别为当前任务的澄清答复：',
            answer[:600],
            '',
            f'目标任务：{cmd.get("task_id") or "LATEST"}',
            '',
            '接下来我会带着完整上下文重新规划；需求明确后自动进入执行。',
        ])
    if action=='external_command':
        instruction=str(cmd.get('instruction') or '').strip()
        if not instruction: return None
        lines=[
            '已理解你的修订指挥：',
            instruction[:600],
            '',
            f'接下来我会把这条指令发送到外部会话 {cmd.get("external_id")}；Codex 会插入原会话队列，Claude 会恢复原会话执行。',
        ]
        return '\n'.join(lines)
    if action=='inspect':
        requirement=str(cmd.get('requirement') or '').strip()
        if not requirement: return None
        project=str(cmd.get('project') or '').strip()
        lines=[
            '已理解你的本地查看请求：',
            requirement[:600],
            '',
            '接下来我会调用本地只读 Agent 检索/阅读相关内容；此阶段不修改文件，完成后直接返回结果。',
        ]
        if project: lines.append('\n项目/工作目录：'+project)
        return '\n'.join(lines)
    return None


_ACTIVE_TASK_STATUSES = {
    'ANALYZING', 'WAITING_FOR_CLARIFICATION', 'WAITING_FOR_APPROVAL', 'QUEUED',
    'EXECUTING', 'VERIFYING', 'REPAIRING', 'WAITING_FOR_HUMAN', 'COMPLETED',
}
# 只有这些状态的任务才允许把普通消息强制绑定为“补充”：
# WAITING_FOR_HUMAN 多为验收修复耗尽或高风险待批的滞留任务，强制绑定会把
# 用户的新提问/新需求吞进旧任务（见 TASK-000005 事故），因此不参与绑定。
_SUPPLEMENT_BINDABLE_STATUSES = {
    'ANALYZING', 'WAITING_FOR_CLARIFICATION', 'WAITING_FOR_APPROVAL', 'QUEUED',
    'EXECUTING', 'VERIFYING', 'REPAIRING',
}


def _clarification_binding(active_task,text):
    """任务等待澄清时，把普通回复强制绑定到该任务，避免被误判为新任务或闲聊。"""
    if active_task and active_task.get('status')=='WAITING_FOR_CLARIFICATION' and str(text or '').strip():
        return {
            'action':'clarify',
            'task_id':active_task.get('public_id') or 'LATEST',
            'answer':str(text).strip(),
            'confidence':0.95,
        }
    return None

def _active_task_context():
    """Return a secret-free snapshot of the newest non-terminal PersonZit task."""
    try:
        with SessionLocal() as db:
            tasks=db.scalars(
                select(Task).where(Task.status.in_(_ACTIVE_TASK_STATUSES))
                .order_by(Task.id.desc()).limit(10)
            ).all()
            cutoff=datetime.now(timezone.utc)-timedelta(hours=2)
            task=next((t for t in tasks if t.status!='COMPLETED' or (
                t.completed_at and (t.completed_at if t.completed_at.tzinfo else t.completed_at.replace(tzinfo=timezone.utc))>=cutoff
            )),None)
            if task is None:
                return None
            return {
                'public_id': task.public_id,
                'status': task.status,
                'title': task.title,
                'description': task.description,
                'project_path': task.project_path,
                'supplement_bindable': task.status in _SUPPLEMENT_BINDABLE_STATUSES,
            }
    except Exception as e:
        logger.warning('failed to load active task context: %s',e)
        return None


def _get_conversation_workspace(sender,conversation):
    with _state_lock:
        _,record=_conversation_record(sender,conversation)
        return record.get('workspace')

def _set_conversation_workspace(sender,conversation,path_text):
    try:
        path=Path(str(path_text)).expanduser().resolve()
    except (OSError,ValueError) as e:
        return False,f'工作目录无效：{e}'
    if not path.is_dir():
        return False,f'工作目录不存在或不是文件夹：{path}'
    cfg=load_config().get('dingtalk',{}).get('local_actions',{}) or {}
    roots=[Path(str(x)).expanduser().resolve() for x in cfg.get('allowed_roots',[]) if str(x).strip()]
    if roots and not any(_within_path(path,root) for root in roots):
        return False,'路径不在允许的本地工作区内：'+str(path)
    with _state_lock:
        state,record=_conversation_record(sender,conversation)
        record['workspace']=str(path)
        _save_conversation_state(state)
    return True,f'当前工作目录已切换为：{path}'

def _clear_conversation_workspace(sender,conversation):
    with _state_lock:
        state,record=_conversation_record(sender,conversation)
        record['workspace']=None
        _save_conversation_state(state)
    default=load_config().get('dingtalk',{}).get('local_actions',{}).get('default_path','')
    return f'会话工作目录已清除，恢复默认：{default}'


def _within_path(path: Path, root: Path) -> bool:
    try:
        return path == root or root in path.parents
    except (OSError, ValueError):
        return False

def _resolve_local_inspection_path(requested: str|None,session_workspace: str|None=None) -> Path:
    cfg=load_config().get('dingtalk',{}).get('local_actions',{}) or {}
    roots=[Path(str(x)).expanduser().resolve() for x in cfg.get('allowed_roots',[]) if str(x).strip()]
    if not roots:
        raise RuntimeError('未配置 local_actions.allowed_roots，本地文件查询已禁用')

    default=session_workspace or cfg.get('default_path')
    raw=str(requested or default or '').strip().strip('\'"')
    if not raw:
        if len(roots) == 1: return roots[0]
        raise RuntimeError('请指定要查看的目录/项目/文档路径。可用根目录：' + '；'.join(str(x) for x in roots))

    path=Path(raw).expanduser()
    if not path.is_absolute():
        matches=[root/path for root in roots if (root/path).exists()]
        if len(matches) != 1:
            raise RuntimeError('相对路径必须能唯一对应到一个允许的根目录')
        path=matches[0]
    path=path.resolve()
    if not any(_within_path(path,root) for root in roots):
        raise RuntimeError('路径不在允许的本地工作区内：'+str(path))

    target=path
    while not target.exists() and target.parent != target:
        target=target.parent
    return target

def _search_inspection_target(requirement: str,root: Path) -> Path|None:
    """Find a strongly matching local file for salient request keywords."""
    cleaned=re.sub(r'(输出|查看|看看|读取|阅读|列出|说明|解释|总结|摘要|分析|一下|工作目录|当前目录|项目|工程|规则|请|帮我|什么|这个|当前)', ' ',str(requirement or ''))
    terms=[]
    for part in re.findall(r'[\u4e00-\u9fffA-Za-z0-9_-]{2,}',cleaned):
        if part.lower() not in {'personzit','files','file'}:
            terms.append(part)
    terms=sorted(set(terms),key=lambda x:(-len(x),x))[:8]
    if not terms: return None
    best=None; best_score=0
    for path in root.rglob('*'):
        if not path.is_file() or '.git' in path.parts or 'node_modules' in path.parts or '历史版本' in path.parts: continue
        try:
            if path.stat().st_size>5*1024*1024: continue
            text=path.read_text(encoding='utf8',errors='ignore')
        except OSError:
            continue
        name=str(path).lower()
        score=0
        for term in terms:
            weight=min(len(term),8)
            score+=text.count(term)*weight+name.count(term.lower())*weight*20
        score=int(score/max(1,len(text)//5000))
        if path.suffix.lower() in {'.md','.txt'}: score*=3
        elif path.suffix.lower() in {'.html','.htm'}: score*=0.5
        if any(x in name for x in ('需求设计文档','研发需求')): score*=0.5
        if score>best_score:
            best,best_score=path,score
    return best if best_score>=min(8,max(2,len(terms[0])))*2 else None

def _run_local_inspection(cmd: dict,message_id: str='unknown',session_workspace: str|None=None) -> str:
    cfg=load_config().get('dingtalk',{}).get('local_actions',{}) or {}
    if not cfg.get('enabled',True):
        return '本地文件查询未启用。'

    requirement=str(cmd.get('requirement') or '').strip()
    if not requirement:
        return '请描述要搜索或查看的本地内容。'
    path=_resolve_local_inspection_path(cmd.get('project'),session_workspace)
    target=path if path.is_file() else None
    if target is None:
        target=_search_inspection_target(cmd.get('requirement'),path)
    working_directory=target.parent if target is not None else path
    agent=str(cfg.get('agent') or 'codex')
    mode=_local_agent_mode(agent)
    _log_route(message_id,stage='local-inspection-start',action='inspect',executor=agent,mode=mode,
        workspace=str(working_directory),target=str(target) if target else None)

    prompt=(
        f'请基于下面目标文件内容回答用户问题，并引用文件中的规则。\n'
        f'用户请求：{requirement}\n'
    )
    if target:
        prompt += f'目标文件：{target}\n'
        try:
            if target.suffix.lower() in {'.md','.txt'} and target.stat().st_size<=200*1024:
                prompt += '\n目标文件内容：\n'+target.read_text(encoding='utf8',errors='replace')[:60000]+'\n\n'
        except OSError:
            pass
    prompt += (
        '要求：只读；不要创建、修改、删除文件；直接输出答案；'
        '如果目标内容不足，明确说明缺少什么。\n'
    )
    started=time.monotonic()
    result,metadata=complete_local_text(
        prompt,
        str(working_directory),
        agent_name=agent,
        timeout=int(cfg.get('timeout_seconds',300))
    )
    duration_ms=round((time.monotonic()-started)*1000)
    answer=str(result or '').strip() or '本地代理没有返回内容。'
    _log_route(message_id,stage='local-inspection-finished',action='inspect',executor=metadata.get('model') or agent,
        mode=metadata.get('mode') or mode,workspace=str(working_directory),outcome='success',
        duration_ms=duration_ms,reply_chars=len(answer))
    footer=f"\n\n— 本地只读代理：{metadata.get('model')}，目录：{working_directory}"
    return (answer[:1800]+('\n…结果已截断' if len(answer)>1800 else ''))+footer

# ---------------- 出站通知 ----------------
def sign(secret:str,timestamp=None):
    timestamp=str(timestamp or int(time.time()*1000)); raw=f'{timestamp}\n{secret}'.encode()
    digest=hmac.new(secret.encode(),raw,hashlib.sha256).digest()
    return timestamp,base64.b64encode(digest).decode()

def notify_config():
    """返回钉钉配置状态（不暴露 secret）。"""
    cfg=load_config().get('dingtalk',{})
    return {
        'enabled':bool(cfg.get('enabled')),
        'webhook':cfg.get('webhook',''),
        'client_id':cfg.get('client_id',''),
        'client_secret':cfg.get('client_secret',''),
        'default_project':cfg.get('default_project',''),
        'allowed_users':cfg.get('allowed_users',[]),
        'allowed_conversations':cfg.get('allowed_conversations',[]),
        'natural_language':cfg.get('natural_language',{}),
    }

def send_notification(title,text):
    """发送 Markdown 通知到钉钉群机器人；未配置时返回 (False, 原因) 安全降级。"""
    cfg=load_config().get('dingtalk',{})
    if not cfg.get('enabled'): return False,'钉钉未启用'
    webhook=cfg.get('webhook')
    if not webhook: return False,'未配置 webhook'
    url=webhook; secret=cfg.get('secret')
    if secret:
        ts,sig=sign(secret); sep='&' if '?' in url else '?'
        url=f'{url}{sep}timestamp={ts}&sign={urllib.parse.quote(sig)}'
    data=json.dumps({'msgtype':'markdown','markdown':{'title':title,'text':text}},ensure_ascii=False).encode()
    req=urllib.request.Request(url,data=data,headers={'Content-Type':'application/json'})
    try:
        with urllib.request.urlopen(req,timeout=10) as response:
            result=json.loads(response.read().decode())
        ok=result.get('errcode')==0
        detail=f"errcode={result.get('errcode')} errmsg={result.get('errmsg')}"
        logger.info('outbound notification %s: %s title=%r','sent' if ok else 'rejected',detail,title)
        return ok,detail
    except Exception as e:
        logger.warning('outbound notification failed: %s title=%r',e,title)
        return False,str(e)

def notify_task_event(event,task,extra_lines=None):
    """关键节点通知：approval/clarification/completed/waiting_human/failed。"""
    cfg=load_config().get('dingtalk',{})
    if not cfg.get('notify',{}).get(event,True): return
    title={'approval':'📝 待审批','clarification':'❓ 需要澄清','auto_started':'🚀 自动开始执行','completed':'✅ 任务完成',
           'waiting_human':'🧑‍💻 等待人工介入','failed':'❌ 任务失败'}.get(event,event)
    lines=[f'### {title}',f'**任务**：{task.public_id} {task.title}',f'**状态**：{status_label(task.status)}']
    if task.plan_json:
        try:
            plan=json.loads(task.plan_json)
            if plan.get('summary'): lines.append(f'**摘要**：{plan["summary"]}')
            qs=plan.get('clarification_questions') or []
            if event=='clarification' and qs:
                lines+=['',f'**请回答以下问题（直接回复内容即可，也可回复：澄清 {task.public_id} 你的答复）**']+[f'{i+1}. {q}' for i,q in enumerate(qs)]
            if event=='approval':
                lines+=['',f'**风险等级**：{plan.get("risk_level","medium")}','**批准**：回复 `通过 {task.public_id}`','**驳回**：回复 `驳回 {task.public_id} 理由`']
        except Exception: pass
    if extra_lines: lines+=['']+list(extra_lines)
    ok,detail=send_notification(f'{task.public_id} {title}','\n\n'.join(lines))
    if ok:
        logger.info('task event notification sent task_id=%s event=%s',task.public_id,event)
    elif '未启用' not in detail and '未配置' not in detail:
        logger.warning('task event notification failed task_id=%s event=%s detail=%s',task.public_id,event,detail)
    return ok

# ---------------- 入站指令解析（纯函数，可测试） ----------------
def parse_message(text):
    t=(text or '').strip()
    if not t: return None
    low=t.lower()
    if low in ('帮助','help','?','？'): return {'action':'help'}
    if re.fullmatch(r'(?:你|您)(?:能|会|可以)(?:做点什么|做什么|干什么|干啥|什么|啥)(?:事情|活儿|活)?',t.strip()): return {'action':'help'}
    if low in ('身份','whoami','id'): return {'action':'identity'}
    if low in ('列表','list','tasks'): return {'action':'list'}
    if re.search(r'(任务|执行|进度).{0,12}(怎么样|如何|什么状态|状态如何)',low): return {'action':'status','task_id':'LATEST'}
    cm_continue=re.search(r'(task-(?:[0-9a-z\u4e00-\u9fff][0-9a-z\u4e00-\u9fff_-]*-)?\d{6})',low)
    if re.fullmatch(r'(?:继续|接着|恢复|开始)(?:执行|运行|处理)?(?:当前|最新|这个)?任务(?:吧|呀)?|继续(?:吧|呀)?|go',low):
        return {'action':'continue','task_id':(cm_continue.group(1).upper() if cm_continue else 'LATEST')}
    if cm_continue and re.search(r'(继续|接着|恢复|开始|执行|运行)',low): return {'action':'continue','task_id':cm_continue.group(1).upper()}
    m=re.match(r'^(?:指挥|修订|跟进|继续)(?:外部|手动)?(?:会话|任务)?[\s:：]+(EXT-(?:CODEX|CLAUDE)-\d{4})[\s:：]+(.+)$',t,re.I|re.S)
    if m: return {'action':'external_command','external_id':m.group(1).upper(),'instruction':m.group(2).strip()}
    m=re.match(r'^(?:状态|详情|查看)[\s:：]+(EXT-(?:CODEX|CLAUDE)-\d{4})$',t,re.I)
    if m:
        record=external_agents.get_external_session(m.group(1))
        if record: return {'action':'chat','reply':_format_external_session(record)}
    if low in ('运行','当前任务','运行任务','外部','外部会话','手动会话') or re.search(r'(正在|当前|现在).{0,16}(执行|运行|做什么|干什么|处理什么)|运行中的任务|外部会话|手动会话',low): return {'action':'running'}
    if (re.search(r'(输出|查看|看看|读取|阅读|列出|说明|解释|总结|摘要|分析).{0,40}(工作目录|当前目录|项目|工程|文档|资料|代码|规则)',low)
            and not re.search(r'(任务|task-)',low)):
        return {'action':'inspect','requirement':t}
    ws_path=re.search(r'[A-Za-z]:[\\/][^\s，。；,;）)]+',t)
    if ws_path and re.search(r'(切换|设置|设定|修改|变更).{0,12}(工作目录|目录|项目)',low):
        return {'action':'set_workspace','project':ws_path.group(0).rstrip('\\"')}
    if re.search(r'(清除|重置|恢复)(当前)?(工作目录|目录)',low): return {'action':'clear_workspace'}
    if re.search(r'(当前|现在)?工作目录|(当前|现在的?)目录',low) and not re.search(r'(输出|查看|看看|读取|总结|分析|说明|解释|列出)',low):
        return {'action':'workspace'}
    m=re.match(r'^(?:状态|status|查询)\s+(task-(?:[0-9a-z\u4e00-\u9fff][0-9a-z\u4e00-\u9fff_-]*-)?\d{6})$',low)
    if m: return {'action':'status','task_id':m.group(1).upper()}
    cm=re.search(r'(task-(?:[0-9a-z\u4e00-\u9fff][0-9a-z\u4e00-\u9fff_-]*-)?\d{6})',low)
    if cm and re.search(r'(结束|终止|取消|停止|不要再|中断)',low): return {'action':'cancel','task_id':cm.group(1).upper()}
    if re.search(r'(结束|终止|取消|停止|不要再|中断).{0,12}(这个|当前|最新|最近)?任务|(?:这个|当前|最新|最近)任务.{0,12}(结束|终止|取消|停止|中断)',low): return {'action':'cancel','task_id':'LATEST'}
    m=re.match(r'^(?:通过|approve)\s+(task-(?:[0-9a-z\u4e00-\u9fff][0-9a-z\u4e00-\u9fff_-]*-)?\d{6})(?:[\s:：]+(.+))?$',t,re.I|re.S)
    if m: return {'action':'approve','task_id':m.group(1).upper(),'reason':(m.group(2) or '').strip() or None}
    m=re.match(r'^(?:驳回|reject)\s+(task-(?:[0-9a-z\u4e00-\u9fff][0-9a-z\u4e00-\u9fff_-]*-)?\d{6})(?:[\s:：]+(.+))?$',t,re.I|re.S)
    if m: return {'action':'reject','task_id':m.group(1).upper(),'reason':(m.group(2) or '').strip() or None}
    if re.fullmatch(r'(?:通过|批准|同意|approve|yes)',low): return {'action':'approve','task_id':'LATEST'}
    if re.fullmatch(r'(?:驳回|拒绝|reject|no)(?:\s+.+)?',t,re.I|re.S): return {'action':'reject','task_id':'LATEST','reason':re.sub(r'^\s*(?:驳回|拒绝|reject|no)\s*','',t,flags=re.I).strip() or None}
    m=re.match(r'^(?:重试|retry)\s+(task-(?:[0-9a-z一-鿿][0-9a-z一-鿿_-]*-)?\d{6})$',low)
    if m: return {'action':'retry','task_id':m.group(1).upper()}
    if low in ('重试','重试任务','retry'): return {'action':'retry','task_id':'LATEST'}
    m=re.match(r'^(?:重新验收|再次验收|重跑验收|reverify)\s+(task-(?:[0-9a-z一-鿿][0-9a-z一-鿿_-]*-)?\d{6})$',low)
    if m: return {'action':'reverify','task_id':m.group(1).upper()}
    if low in ('重新验收','再次验收','重跑验收'): return {'action':'reverify','task_id':'LATEST'}
    m=re.match(r'^(?:补充|更正|修正)\s+(task-(?:[0-9a-z\u4e00-\u9fff][0-9a-z\u4e00-\u9fff_-]*-)?\d{6})[\s:：]+(.+)$',t,re.I|re.S)
    if m: return {'action':'supplement','task_id':m.group(1).upper(),'requirement':m.group(2).strip()}
    m=re.match(r'^(?:澄清|clarify)\s+(task-(?:[0-9a-z\u4e00-\u9fff][0-9a-z\u4e00-\u9fff_-]*-)?\d{6})[\s:：]+(.+)$',t,re.I|re.S)
    if m: return {'action':'clarify','task_id':m.group(1).upper(),'answer':m.group(2).strip()}
    m=re.match(r'^(?:任务|task|run)[\s:：]+(.+)$',t,re.I|re.S)
    if m:
        requirement=m.group(1).strip(); project=None
        pm=re.search(r'项目[=:：]\s*(\S+)',requirement)
        if pm:
            project=pm.group(1); requirement=(requirement[:pm.start()]+requirement[pm.end():]).strip()
        return {'action':'create','requirement':requirement,'project':project}
    return None

HELP_TEXT='''PersonZit 钉钉用法：
身份                            查看 userId / conversationId
任务 <需求描述> [项目=D:\路径]  发布工程任务（明确自动执行，不明确会追问）
通过 <任务ID> [理由]             批准计划
驳回 <任务ID> <理由>             驳回重新规划
澄清 <任务ID> <答复>             回复澄清问题（等待澄清时直接回复也可）
补充 <任务ID> <补充/纠错>        修订当前任务上下文
状态 <任务ID>                    查询任务
重试 <任务ID>                    重新规划并执行等待人工的任务（带上全部补充）
重新验收 <任务ID>                只重跑验收命令，不重新执行 Agent
运行                            查看 PersonZit 队列和手动 Codex/Claude 会话
指挥 <EXT会话ID> <修订要求>      指挥手动打开的 Codex/Claude 原会话
结束 <任务ID> [理由]             取消/终止任务
列表                            最近任务
帮助                            显示本帮助

也支持自然语言：
· 直接聊天，例如“你好”“谢谢”“这个问题你怎么看”
· 本地只读查看，例如“搜索 D:\\Workspace 里的部署文档”“总结 D:\\Docworkspace\\需求.md”
· 描述工程需求，例如“帮我实现一个登录功能”
· 监管手动 CLI，例如“运行”“指挥 EXT-CODEX-0001 修改登录超时处理”

说明：智谱/百炼负责自然语言意图解析与普通聊天；Codex/Claude 负责本地规划、查看和执行。日志会输出完整链路。'''

# ---------------- 指令执行（操作数据库） ----------------
def _get_task(db,task_id,statuses=None):
    if task_id=='LATEST':
        query=select(Task).order_by(Task.id.desc()).limit(1)
        if statuses:
            query=select(Task).where(Task.status.in_(statuses)).order_by(Task.id.desc()).limit(1)
        return db.scalar(query)
    return db.scalar(select(Task).where(Task.public_id==task_id))

def _waiting_human_reason(db,t):
    """取任务最近一次进入 WAITING_FOR_HUMAN 的原因（真实卡点）。"""
    for evt in db.scalars(select(EventLog).where(EventLog.task_id==t.id,EventLog.event_type=='TASK_STATUS_CHANGED')
            .order_by(EventLog.id.desc())).all():
        try: payload=json.loads(evt.payload_json or '{}')
        except Exception: payload={}
        if payload.get('to')=='WAITING_FOR_HUMAN':
            return str(payload.get('reason') or '等待人工处理')
    return '等待人工处理'

def _waiting_human_block(db,t):
    """汇总 WAITING_FOR_HUMAN 任务的真实卡点与下一步指令，替代指向“运行”的环形指引。"""
    lines=[f'卡点：{_waiting_human_reason(db,t)}']
    failed=db.scalars(select(VerificationRun).where(VerificationRun.task_id==t.id,VerificationRun.passed==False)
        .order_by(VerificationRun.id.desc()).limit(2)).all()
    for run in reversed(failed):
        try: command=str(json.loads(run.command_json or '""'))
        except Exception: command=str(run.command_json or '')
        detail=str(run.summary or '未通过').strip()
        stderr=''
        try:
            if run.stderr_path and Path(run.stderr_path).exists():
                stderr=Path(run.stderr_path).read_text(encoding='utf-8',errors='replace').strip()
        except OSError: stderr=''
        if stderr: detail=f'{detail}（{stderr[-120:]}）'
        lines.append(f'失败验收：{command} → {detail}')
    pending_va=db.scalar(select(Approval).where(Approval.task_id==t.id,Approval.approval_type=='VERIFICATION',
        Approval.status=='PENDING').order_by(Approval.id.desc()))
    if pending_va:
        lines.append(f'下一步：回复“通过 {t.public_id}”批准高风险验收命令')
    else:
        lines.append(f'下一步：回复“重试 {t.public_id}”重新规划执行，或“重新验收 {t.public_id}”只重跑验收，或“结束 {t.public_id}”取消')
    return lines

def _job_status_label(status):
    return {'QUEUED':'排队中','RUNNING':'执行中','SUCCEEDED':'已完成','FAILED':'失败','RETRY_WAIT':'等待重试'}.get(str(status or ''),str(status or ''))

def _format_external_session(record):
    summary=str(record.get('summary') or '').strip()
    if summary: summary=summary[:800]
    lines=[f'{record.get("external_id")} [{external_agents._external_status_label(record.get("status"))}] {record.get("agent")} 手动会话',
           f'任务：{record.get("title") or "未记录任务标题"}']
    if record.get('workspace'): lines.append(f'目录：{record["workspace"]}')
    lines.append(f'原始会话：{record.get("session_id")}')
    if summary: lines.append(f'最新输出：\n{summary}')
    lines.append(f'下一步：指挥 {record.get("external_id")} <修订要求>')
    return '\n'.join(lines)[:2500]


def dispatch_command(cmd,actor='dingtalk',message_id='unknown'):
    action=cmd.get('action')
    try:
        with SessionLocal() as db:
            if action=='help': return HELP_TEXT
            if action=='list':
                tasks=db.scalars(select(Task).order_by(Task.id.desc()).limit(5)).all()
                return '最近任务：\n'+'\n'.join(f'{t.public_id} [{status_label(t.status)}] {t.title[:40]}' for t in tasks) or '暂无任务'
            if action=='running':
                jobs=db.scalars(select(Job).where(Job.status.in_(['QUEUED','RUNNING'])).order_by(Job.id.desc()).limit(20)).all()
                live=process_registry.active_tasks()
                externals=external_agents.list_external_sessions()
                active_externals=[r for r in externals if r.get('status')!='COMPLETED'][:10]
                done_externals=[r for r in externals if r.get('status')=='COMPLETED'][:5]
                waiting=db.scalars(select(Task).where(Task.status.in_(
                    ['WAITING_FOR_HUMAN','WAITING_FOR_APPROVAL','WAITING_FOR_CLARIFICATION','PAUSED'])
                    ).order_by(Task.id.desc()).limit(5)).all()
                lines=['当前运行链路：']
                if not jobs and not live and not active_externals and not waiting:
                    lines.append('没有 QUEUED/RUNNING 队列任务，也没有可监管的本地/手动 Agent 会话。')
                if active_externals:
                    lines.append('手动 Codex/Claude 会话：')
                    for record in active_externals:
                        lines.append(f'- {record["external_id"]} [{external_agents._external_status_label(record["status"])}] {record["agent"]} / {(record.get("title") or "")[:100]} / {record.get("workspace") or "未记录目录"}')
                        lines.append(f'  指挥：指挥 {record["external_id"]} <修订要求>')
                if waiting:
                    lines.append('等待人工处理的任务：')
                    for wt in waiting:
                        if wt.status=='WAITING_FOR_HUMAN':
                            lines.append(f'- {wt.public_id} {_waiting_human_reason(db,wt)}。可回复“状态 {wt.public_id}”查看卡点，“重试 {wt.public_id}”重新执行')
                        elif wt.status=='WAITING_FOR_APPROVAL':
                            lines.append(f'- {wt.public_id} 等待审批。可回复“通过 {wt.public_id}”批准')
                        elif wt.status=='WAITING_FOR_CLARIFICATION':
                            lines.append(f'- {wt.public_id} 等待澄清。直接回复答复内容即可')
                        else:
                            lines.append(f'- {wt.public_id} 已暂停。可回复“继续执行任务”恢复')
                if done_externals:
                    lines.append('近期已完成的手动会话（不再运行）：')
                    for record in done_externals:
                        lines.append(f'- {record["external_id"]} [已完成] {record["agent"]} / {(record.get("title") or "")[:60]} / {record.get("workspace") or "未记录目录"}')
                for job in jobs:
                    task=db.get(Task,job.task_id)
                    if not task: continue
                    duration=''
                    if job.locked_at:
                        locked_at=job.locked_at.replace(tzinfo=timezone.utc) if job.locked_at and job.locked_at.tzinfo is None else job.locked_at
                        duration=f'，已运行 {round((datetime.now(timezone.utc)-locked_at).total_seconds())}s'
                    lines.append(f'- {task.public_id} [{status_label(task.status)}] {job.job_type} / {_job_status_label(job.status)}{duration} / {job.locked_by or "未领取"} / {task.project_path}')
                    run=db.scalar(select(AgentRun).where(AgentRun.task_id==task.id,AgentRun.status=='RUNNING').order_by(AgentRun.id.desc()))
                    if run:
                        sub=db.get(SubTask,run.subtask_id)
                        lines.append(f'  Agent={run.agent_type}，子任务={sub.public_id if sub else run.subtask_id}，Prompt={(run.prompt or "")[:120].replace(chr(10)," ")}')
                for task_id,records in live.items():
                    for record in records:
                        lines.append(f'- {task_id} live-agent pid={record["pid"]} started={record["started_at"]}')
                return '\n'.join(lines)[:4000]
            if action=='cancel':
                t=_get_task(db,cmd['task_id'])
                if not t: return f'未找到 {cmd["task_id"]}'
                if t.status in ('CANCELLED','COMPLETED','FAILED'):
                    return f'{t.public_id} 已经是终态（{status_label(t.status)}），无需结束'
                terminated=process_registry.request_cancel(t.public_id)
                transition(db,t,'CANCEL_REQUESTED',actor=actor,reason=cmd.get('reason') or 'dingtalk cancel requested')
                active_jobs=db.scalars(select(Job).where(Job.task_id==t.id,Job.status=='RUNNING')).all()
                for job in db.scalars(select(Job).where(Job.task_id==t.id,Job.status=='QUEUED')).all():
                    job.status='FAILED'; job.last_error='task cancelled from DingTalk'
                if not active_jobs:
                    transition(db,t,'CANCELLED',actor=actor,reason='no active worker job after cancellation')
                db.commit()
                detail=f'已向 {len(terminated)} 个本地 Agent 进程发送终止信号' if terminated else '没有已登记的可终止 Agent 进程'
                suffix='；当前任务已取消' if t.status=='CANCELLED' else '；Worker 将在当前调用退出后落地取消状态'
                _log_route(message_id,stage='task-cancel',action='cancel',task_id=t.public_id,outcome='requested',
                    terminated_processes=terminated,active_jobs=len(active_jobs),final_status=t.status)
                return f'{t.public_id} 结束请求已受理。{detail}{suffix}'
            if action=='status':
                t=_get_task(db,cmd['task_id'])
                if not t: return f'未找到 {cmd["task_id"]}'
                subs=db.scalars(select(SubTask).where(SubTask.task_id==t.id).order_by(SubTask.order_index)).all()
                lines=[f'{t.public_id} {t.title}',f'状态：{status_label(t.status)}',f'项目：{t.project_path}']
                if t.plan_json:
                    try:
                        p=json.loads(t.plan_json)
                        if p.get('summary'): lines.append(f'摘要：{str(p["summary"])[:300]}')
                        if p.get('clarification_questions') and t.status=='WAITING_FOR_CLARIFICATION':
                            lines+=['澄清问题：']+[f'{i+1}. {q}' for i,q in enumerate(p['clarification_questions'])]
                    except Exception: pass
                next_actions={
                    'WAITING_FOR_APPROVAL':f'下一步：回复“通过 {t.public_id}”批准执行，或“继续执行任务”',
                    'WAITING_FOR_CLARIFICATION':f'下一步：回复“澄清 {t.public_id} <答复>”',
                    'ANALYZING':'下一步：规划中，可稍后发送“运行”查看进度',
                    'QUEUED':'下一步：已排队，可发送“运行”查看 Worker',
                    'EXECUTING':'下一步：执行中，可发送“运行”查看 Agent',
                    'VERIFYING':'下一步：验证中，可发送“运行”查看 Worker',
                    'REPAIRING':'下一步：修复中，可发送“运行”查看 Agent',
                }
                if t.status=='WAITING_FOR_HUMAN':
                    lines+=_waiting_human_block(db,t)
                elif t.status in next_actions: lines.append(next_actions[t.status])
                elif t.status in ('CANCELLED','COMPLETED','FAILED'): lines.append('该任务已是终态。')
                lines+=['子任务：']+[f'- {s.public_id} [{status_label(s.status)}] {s.title}' for s in subs]
                return '\n'.join(lines)
            if action=='create':
                project=cmd.get('project') or load_config().get('dingtalk',{}).get('default_project')
                if not project: return '未指定项目路径：请回复“任务 <需求> 项目=D:\\路径”或在配置中设置 default_project'
                req=cmd['requirement']
                t=Task(public_id='PENDING',title=req[:80],description=req,project_path=project,priority=0,source=actor)
                db.add(t); db.flush(); t.public_id=semantic_task_id(req,t.id)
                transition(db,t,'ANALYZING',reason='dingtalk task created'); db.commit()
                SqliteQueue(db).enqueue('PLAN_TASK',t.id,payload=json.dumps({'title':req[:80],'description':req,'project_path':project},ensure_ascii=False))
                _log_route(message_id,stage='task-created',action='create',task_id=t.public_id,
                    workspace=project,outcome='queued',next_stage='PLAN_TASK')
                return f'已创建 {t.public_id}，正在规划；需求明确会自动进入执行，信息不足时会继续向你追问'
            if action=='supplement':
                t=_get_task(db,cmd.get('task_id') or 'LATEST')
                if not t: return f'未找到要补充的任务 {cmd.get("task_id") or "LATEST"}'
                if t.status in ('CANCELLED','FAILED'):
                    return f'{t.public_id} 已是终态（{status_label(t.status)}），不能追加修订。请重新发布任务'
                reopened=False
                if t.status=='COMPLETED':
                    transition(db,t,'REPAIRING',actor=actor,reason='dingtalk supplement reopened completed task')
                    t.completed_at=None; reopened=True
                requirement=str(cmd.get('requirement') or '').strip()
                marker='\n\n## 钉钉补充要求\n'
                existing=t.description or ''
                if requirement and requirement not in existing:
                    t.description=(existing+marker+'- '+requirement).strip()
                db.add(EventLog(task_id=t.id,event_type='TASK_SUPPLEMENT_RECEIVED',actor_type='human',actor_id=actor,
                    payload_json=json.dumps({'requirement':requirement,'source_message_id':message_id},ensure_ascii=False)))
                db.commit()
                _log_route(message_id,stage='task-supplement-accepted',action='supplement',task_id=t.public_id,
                    task_status=t.status,outcome='persisted',reopened=reopened,next_stage='REVISE_TASK')
                if t.status=='WAITING_FOR_APPROVAL':
                    transition(db,t,'ANALYZING',actor=actor,reason='dingtalk supplement requires replanning')
                    SqliteQueue(db).enqueue('PLAN_TASK',t.id,payload=json.dumps({'title':t.title,'description':t.description,'project_path':t.project_path},ensure_ascii=False)); db.commit()
                    return f'已把补充规则并入 {t.public_id}，将重新规划后再审批'
                if t.status=='WAITING_FOR_CLARIFICATION':
                    transition(db,t,'ANALYZING',actor=actor,reason='dingtalk supplement treated as clarification answer')
                    SqliteQueue(db).enqueue('PLAN_TASK',t.id,payload=json.dumps({'title':t.title,'description':t.description,'project_path':t.project_path,'answers':[requirement]},ensure_ascii=False)); db.commit()
                    return f'已把补充内容并入 {t.public_id} 的澄清上下文，正在重新规划；需求明确后会自动进入执行'
                if t.status=='WAITING_FOR_HUMAN':
                    return '\n'.join([f'已记录 {t.public_id} 的补充要求，会并入后续执行。当前等待人工处理：',*_waiting_human_block(db,t)])
                if t.status=='PAUSED':
                    return f'已记录 {t.public_id} 的补充要求。当前任务已暂停，回复“继续执行任务”可恢复执行'
                SqliteQueue(db).enqueue('REVISE_TASK',t.id,payload=json.dumps({'requirement':requirement},ensure_ascii=False),priority=100)
                db.commit()
                return f'已记录 {t.public_id} 的补充要求，并排入修订队列；当前 Agent 完成本轮后会带新规则重跑相关子任务' if not reopened else f'已重新打开 {t.public_id}，补充规则已入修订队列，将带新规则重跑相关子任务'
            if action=='continue':
                t=_get_task(db,cmd.get('task_id') or 'LATEST')
                if not t: return '没有可继续的任务'
                if t.status=='WAITING_FOR_APPROVAL':
                    transition(db,t,'QUEUED',actor=actor,reason='dingtalk continue approved plan')
                    t.approved_at=datetime.now(timezone.utc)
                    a=db.scalar(select(Approval).where(Approval.task_id==t.id,Approval.status=='PENDING').order_by(Approval.id.desc()))
                    if a:
                        a.status='APPROVED'; a.approved_by=actor; a.resolved_at=datetime.now(timezone.utc)
                    SqliteQueue(db).enqueue('EXECUTE_SUBTASK',t.id); db.commit()
                    _log_route(message_id,stage='task-continue',action='continue',task_id=t.public_id,from_status='WAITING_FOR_APPROVAL',to_status='QUEUED')
                    return f'{t.public_id} 已按“继续执行”批准计划，进入执行队列'
                if t.status=='WAITING_FOR_HUMAN':
                    va=db.scalar(select(Approval).where(Approval.task_id==t.id,Approval.approval_type=='VERIFICATION',Approval.status=='PENDING').order_by(Approval.id.desc()))
                    if va:
                        va.status='APPROVED'; va.approved_by=actor; va.resolved_at=datetime.now(timezone.utc)
                        try: command=json.loads(va.request_payload_json or '{}').get('command')
                        except Exception: command=None
                        transition(db,t,'VERIFYING',actor=actor,reason='钉钉继续执行并批准高风险验收')
                        SqliteQueue(db).enqueue('VERIFY_SUBTASK',t.id,payload=json.dumps({'approval_id':va.id,'approved_command':command},ensure_ascii=False)); db.commit()
                        return f'{t.public_id} 高风险验收已批准，继续验证'
                    return '\n'.join([f'{t.public_id} 等待人工处理：',*_waiting_human_block(db,t)])
                if t.status=='WAITING_FOR_CLARIFICATION':
                    return f'{t.public_id} 需要先补充澄清信息。请回复：澄清 {t.public_id} <答复>'
                if t.status=='PAUSED':
                    target=t.paused_from or 'QUEUED'
                    transition(db,t,target,actor=actor,reason='dingtalk continue resumed')
                    db.commit(); return f'{t.public_id} 已恢复为{status_label(t.status)}'
                if t.status in ('CANCELLED','COMPLETED','FAILED'):
                    return f'{t.public_id} 已是终态（{status_label(t.status)}），不能继续。可重新发布任务'
                return f'{t.public_id} 正在{status_label(t.status)}，无需重复触发。可发送“运行”查看详细链路'
            if action=='retry':
                t=_get_task(db,cmd.get('task_id') or 'LATEST')
                if not t: return '没有可重试的任务'
                if t.status!='WAITING_FOR_HUMAN':
                    return f'{t.public_id} 当前状态为{status_label(t.status)}，无需重试；仅等待人工处理的任务可重试'
                t.retry_count=0
                transition(db,t,'ANALYZING',actor=actor,reason='钉钉重试：带着补充要求重新规划')
                SqliteQueue(db).enqueue('PLAN_TASK',t.id,payload=json.dumps({'title':t.title,'description':t.description,'project_path':t.project_path},ensure_ascii=False))
                db.commit()
                _log_route(message_id,stage='task-retry',action='retry',task_id=t.public_id,from_status='WAITING_FOR_HUMAN',to_status='ANALYZING')
                return f'{t.public_id} 已重新进入规划，将带着全部补充要求重新执行，结果会主动推送'
            if action=='reverify':
                t=_get_task(db,cmd.get('task_id') or 'LATEST')
                if not t: return '没有可重新验收的任务'
                if t.status!='WAITING_FOR_HUMAN':
                    return f'{t.public_id} 当前状态为{status_label(t.status)}，无需重新验收'
                transition(db,t,'VERIFYING',actor=actor,reason='钉钉请求重新验收')
                SqliteQueue(db).enqueue('VERIFY_SUBTASK',t.id); db.commit()
                _log_route(message_id,stage='task-reverify',action='reverify',task_id=t.public_id,from_status='WAITING_FOR_HUMAN',to_status='VERIFYING')
                return f'{t.public_id} 已重新进入验收，结果会主动推送'
            if action=='approve':
                t=_get_task(db,cmd['task_id'],['WAITING_FOR_APPROVAL','WAITING_FOR_HUMAN'])
                if not t: return f'未找到待审批任务 {cmd["task_id"]}'
                if t.status=='WAITING_FOR_HUMAN':
                    va=db.scalar(select(Approval).where(Approval.task_id==t.id,Approval.approval_type=='VERIFICATION',Approval.status=='PENDING').order_by(Approval.id.desc()))
                    if not va: return f'{t.public_id} 当前没有待批准的高风险验收命令'
                    va.status='APPROVED'; va.approved_by=actor; va.resolved_at=datetime.now(timezone.utc)
                    try: command=json.loads(va.request_payload_json or '{}').get('command')
                    except Exception: command=None
                    db.add(EventLog(task_id=t.id,event_type='APPROVAL_RESOLVED',actor_type='human',actor_id=actor,
                        payload_json=json.dumps({'approval_id':va.id,'decision':'APPROVED','type':'VERIFICATION','comment':cmd.get('reason')},ensure_ascii=False)))
                    transition(db,t,'VERIFYING',actor=actor,reason='钉钉批准高风险验收')
                    SqliteQueue(db).enqueue('VERIFY_SUBTASK',t.id,payload=json.dumps({'approval_id':va.id,'approved_command':command},ensure_ascii=False)); db.commit()
                    return f'{t.public_id} 高风险验收已批准，继续验证'
                if t.status!='WAITING_FOR_APPROVAL': return f'{t.public_id} 当前状态为{status_label(t.status)}，无法批准'
                transition(db,t,'QUEUED',actor=actor,reason=cmd.get('reason') or 'dingtalk approved')
                t.approved_at=datetime.now(timezone.utc)
                a=db.scalar(select(Approval).where(Approval.task_id==t.id,Approval.status=='PENDING').order_by(Approval.id.desc()))
                if a: a.status='APPROVED'; a.approved_by=actor; a.resolved_at=datetime.now(timezone.utc)
                SqliteQueue(db).enqueue('EXECUTE_SUBTASK',t.id); db.commit()
                return f'{t.public_id} 已批准，进入执行队列'
            if action=='reject':
                t=_get_task(db,cmd['task_id'],['WAITING_FOR_APPROVAL'])
                if not t: return f'未找到 {cmd["task_id"]}'
                if t.status!='WAITING_FOR_APPROVAL': return f'{t.public_id} 当前状态为{status_label(t.status)}，无法驳回'
                a=db.scalar(select(Approval).where(Approval.task_id==t.id,Approval.status=='PENDING').order_by(Approval.id.desc()))
                if a: a.status='REJECTED'; a.comment=cmd.get('reason'); a.resolved_at=datetime.now(timezone.utc)
                transition(db,t,'ANALYZING',actor=actor,reason=cmd.get('reason') or 'dingtalk rejected')
                SqliteQueue(db).enqueue('PLAN_TASK',t.id,payload=json.dumps({'title':t.title,'description':t.description,'project_path':t.project_path},ensure_ascii=False))
                db.commit(); return f'{t.public_id} 已驳回，将重新规划'
            if action=='clarify':
                t=_get_task(db,cmd['task_id'])
                if not t: return f'未找到 {cmd["task_id"]}'
                if t.status!='WAITING_FOR_CLARIFICATION': return f'{t.public_id} 当前状态为{status_label(t.status)}，无需澄清'
                answer=str(cmd.get('answer') or cmd.get('requirement') or '').strip()
                if not answer: return f'请提供 {t.public_id} 的澄清答复内容'
                marker='\n\n## 钉钉澄清答复\n'
                existing=t.description or ''
                if answer not in existing:
                    t.description=(existing+marker+'- '+answer).strip()
                db.add(EventLog(task_id=t.id,event_type='TASK_CLARIFICATION_RECEIVED',actor_type='human',actor_id=actor,
                    payload_json=json.dumps({'answer':answer,'source_message_id':message_id},ensure_ascii=False)))
                transition(db,t,'ANALYZING',actor=actor,reason='dingtalk clarification')
                SqliteQueue(db).enqueue('PLAN_TASK',t.id,payload=json.dumps({'title':t.title,'description':t.description,'project_path':t.project_path,'answers':[answer]},ensure_ascii=False))
                db.commit(); return f'{t.public_id} 已收到澄清答复，正在重新规划；需求明确后会自动进入执行'
        return '未知指令'
    except Exception as e: return f'指令执行失败：{e}'

# ---------------- 白名单与 Stream 服务 ----------------
# ---------------- Whitelist, intent parsing, and Stream service ----------------
def _extract_incoming_text(incoming) -> str:
    """Extract plain text from both text and richText DingTalk messages."""
    try:
        parts=incoming.get_text_list()
    except Exception:
        parts=[]
    if not parts and incoming.text and incoming.text.content:
        parts=[incoming.text.content]
    return ' '.join(str(x).strip() for x in parts if str(x).strip()).strip()

def _allowed(incoming):
    cfg=load_config().get('dingtalk',{}); users=cfg.get('allowed_users') or []; convs=cfg.get('allowed_conversations') or []
    if not users and not convs: return False
    sender=incoming.sender_staff_id or incoming.sender_id or ''
    conv=incoming.conversation_id or ''
    return (users and sender in users) or (convs and conv in convs)

def _runtime_state_path():
    return home()/'runtime'/'dingtalk-stream.json'

def _write_stream_state(**values):
    with _state_lock:
        state={'log_file':str(log_file_path('dingtalk'))}
        try:
            if _runtime_state_path().exists():
                state.update(json.loads(_runtime_state_path().read_text(encoding='utf-8')))
        except Exception: pass
        state.update(values); _runtime_state_path().parent.mkdir(parents=True,exist_ok=True)
        _runtime_state_path().write_text(json.dumps(state,ensure_ascii=False,indent=2),encoding='utf-8')

def stream_runtime_state():
    try: state=json.loads(_runtime_state_path().read_text(encoding='utf-8'))
    except Exception:
        state={'running':False,'status':'Worker \u6216\u9489\u9489 Stream \u672a\u8fd0\u884c','log_file':str(log_file_path('dingtalk'))}
    try:
        heartbeat=datetime.fromisoformat(state['heartbeat_at'])
        age=(datetime.now(timezone.utc)-heartbeat).total_seconds()
        state['heartbeat_age_seconds']=round(age,1)
        state['responsive']=bool(state.get('running') and age<=max(30,int(load_config().get('dingtalk',{}).get('heartbeat_seconds',15))*3))
    except (KeyError,TypeError,ValueError):
        state['heartbeat_age_seconds']=None; state['responsive']=False
    return state

def start_stream_service():
    """Run the DingTalk Stream client in the Worker process."""
    cfg=load_config().get('dingtalk',{})
    if not cfg.get('enabled'):
        logger.info('Stream not started: dingtalk.enabled=false')
        _write_stream_state(running=False,status='\u9489\u9489\u672a\u542f\u7528')
        return None,'\u9489\u9489\u672a\u542f\u7528'
    if not cfg.get('client_id') or not cfg.get('client_secret'):
        logger.warning('Stream not started: client_id/client_secret missing')
        message='\u672a\u914d\u7f6e client_id/client_secret\uff0c\u5165\u7ad9\u4e0d\u53ef\u7528\uff08\u51fa\u7ad9\u901a\u77e5\u4e0d\u53d7\u5f71\u54cd\uff09'
        _write_stream_state(running=False,status=message)
        return None,message
    try: import dingtalk_stream
    except ImportError:
        logger.warning('Stream not started: dingtalk-stream is not installed')
        _write_stream_state(running=False,status='dingtalk-stream \u672a\u5b89\u88c5')
        return None,'dingtalk-stream \u672a\u5b89\u88c5'

    class PersonZitHandler(dingtalk_stream.ChatbotHandler):
        async def process(self,message):
            incoming=None
            try:
                incoming=dingtalk_stream.ChatbotMessage.from_dict(message.data)
                message_id=uuid.uuid4().hex[:12]
                started=time.monotonic()
                text=_extract_incoming_text(incoming)
                sender=incoming.sender_staff_id or incoming.sender_id or ''
                conv=incoming.conversation_id or ''
                low=(text or '').strip().lower()
                logger.info('inbound message message_id=%s sender=%s conversation=%s message_type=%s text=%r',message_id,sender or '<unknown>',conv or '<unknown>',getattr(incoming,'message_type','unknown'),text)
                _log_route(message_id,stage='received',inbound='dingtalk-stream',outcome='accepted',text=text)
                if low in ('\u8eab\u4efd','whoami','id'):
                    _log_route(message_id,stage='route-selected',inbound='dingtalk-stream',text=text,action='identity',
                        intent_provider='builtin-rules',intent_model='builtin-rules',executor='personzit-database',mode='command')
                    reply=f'\u53d1\u9001\u8005 userId\uff08\u586b\u5165 allowed_users\uff09\uff1a{sender}\n\u5f53\u524d\u7fa4 conversationId\uff08\u586b\u5165 allowed_conversations\uff09\uff1a{conv}'
                elif not _allowed(incoming):
                    logger.warning('unauthorized inbound message_id=%s sender=%s conversation=%s',message_id,sender or '<unknown>',conv or '<unknown>')
                    _log_route(message_id,stage='authorization',inbound='dingtalk-stream',outcome='rejected',reason='sender/conversation not allowed')
                    reply='未授权：你或本群不在 PersonZit 白名单中（allowed_users/allowed_conversations）'
                else:
                    _log_route(message_id,stage='authorization',inbound='dingtalk-stream',outcome='authorized')
                    if not text.strip():
                        with SessionLocal() as db:
                            pending=db.scalar(select(Task).where(Task.status.in_(['WAITING_FOR_APPROVAL','WAITING_FOR_CLARIFICATION','WAITING_FOR_HUMAN'])).order_by(Task.id.desc()).limit(1))
                        if pending:
                            reply=(f'收到一条空文本消息。当前最新等待任务：{pending.public_id}（{status_label(pending.status)}）。\n'
                                   f'如需批准请回复：通过 {pending.public_id}\n如需查看请回复：状态 {pending.public_id}')
                        else:
                            reply='收到一条空文本消息。请发送文字内容；如果使用引用、卡片或富文本，请同时包含文字说明。'
                        _log_route(message_id,stage='empty-content',inbound='dingtalk-stream',outcome='clarified',message_type=getattr(incoming,'message_type','unknown'))
                        _remember_conversation(sender,conv,text,reply)
                        _log_route(message_id,stage='reply-start',channel='dingtalk-stream',reply_chars=len(reply))
                        self.reply_text(reply,incoming)
                        _log_route(message_id,stage='reply-finished',channel='dingtalk-stream',outcome='success',reply_chars=len(reply))
                        return dingtalk_stream.AckMessage.STATUS_OK, 'OK'
                    intent_started=time.monotonic()
                    cmd=parse_message(text)
                    if cmd:
                        intent_meta={'provider':'exact-rules','model':'builtin-rules'}
                        _log_route(message_id,stage='intent-precheck',text=text,outcome='matched',
                            parser='exact-rules',action=cmd.get('action'))
                    else:
                        active_task=_active_task_context()
                        # 任务等待澄清时，普通补充内容必须绑定当前任务：
                        # 无论模型是否误判为 chat/create，都不能拆成新任务或普通聊天。
                        clarification_reply=_clarification_binding(active_task,text)
                        if clarification_reply:
                            cmd=clarification_reply
                            intent_meta={
                                'provider':'clarification-context',
                                'model':'active-task-context',
                                'task_id':cmd['task_id'],
                            }
                            _log_route(message_id,stage='intent-precheck',text=text,outcome='matched',
                                parser='clarification-context',action='clarify',task_id=cmd['task_id'])
                        elif active_task and active_task.get('supplement_bindable') and _looks_like_task_supplement(text):
                            cmd={
                                'action':'supplement',
                                'task_id':active_task.get('public_id') or 'LATEST',
                                'requirement':text.strip(),
                                'confidence':0.95,
                            }
                            intent_meta={
                                'provider':'task-context-precheck',
                                'model':'active-task-context',
                                'task_id':cmd['task_id'],
                            }
                            _log_route(message_id,stage='intent-precheck',text=text,outcome='matched',
                                parser='active-task-context',action='supplement',task_id=cmd['task_id'])
                        else:
                            nl_cfg=load_config().get('dingtalk',{}).get('natural_language',{}) or {}
                            nl_provider=str(nl_cfg.get('provider') or 'local')
                            _log_route(message_id,stage='intent-precheck',text=text,outcome='no-match',
                                parser='exact-rules',next='natural-language')
                            _log_route(message_id,stage='intent-start',text=text,processor='natural-language',
                                provider=nl_provider,model=nl_cfg.get('model') or 'configured-default')
                            cmd,intent_meta=await asyncio.to_thread(interpret_intent,text,_get_conversation_history(sender,conv),active_task)
                        clarification_reply=_clarification_binding(active_task,text)
                        if clarification_reply and action_if_defined(cmd) in ('chat','unknown','create','supplement'):
                            cmd=clarification_reply
                            intent_meta={'provider':'clarification-context-rescue','model':'active-task-context','task_id':cmd['task_id']}
                        elif action_if_defined(cmd) in ('chat','unknown') and active_task and active_task.get('supplement_bindable') and _looks_like_task_supplement(text):
                            cmd={'action':'supplement','task_id':active_task.get('public_id') or 'LATEST','requirement':text.strip(),'confidence':0.9}
                            intent_meta={'provider':'task-context-rescue','model':'active-task-context','task_id':cmd['task_id']}
                    intent_duration_ms=round((time.monotonic()-intent_started)*1000)
                    action=str(cmd.get('action') or 'unknown')
                    _log_route(message_id,stage='intent-finished',text=text,action=action,
                        intent_provider=intent_meta.get('provider'),intent_model=intent_meta.get('model'),
                        confidence=cmd.get('confidence'),duration_ms=intent_duration_ms,outcome='success',
                        intent_error=intent_meta.get('original_error') or intent_meta.get('error'))
                    # Priority acknowledgement: send what PersonZit understood and will do
                    # before any queueing or local-agent operation that can take minutes.
                    priority_ack=_priority_ack_text(action,cmd)
                    if priority_ack:
                        _log_route(message_id,stage='priority-ack',inbound='dingtalk-stream',action=action,
                            outcome='sent',before_operation=True,summary=priority_ack)
                        _log_route(message_id,stage='reply-start',channel='dingtalk-stream',reply_kind='priority-ack',
                            reply_chars=len(priority_ack))
                        self.reply_text(priority_ack,incoming)
                        _log_route(message_id,stage='reply-finished',channel='dingtalk-stream',reply_kind='priority-ack',
                            outcome='success',reply_chars=len(priority_ack))
                        _remember_assistant_message(sender,conv,priority_ack)
                    planner_cfg=load_config().get('planner',{}) or {}
                    local_cfg=load_config().get('dingtalk',{}).get('local_actions',{}) or {}
                    intent_provider=str(intent_meta.get('provider') or 'unknown')
                    intent_model=str(intent_meta.get('model') or 'unknown')
                    chat_executor=intent_provider
                    if intent_provider in ('local','fallback-local'):
                        chat_executor=f'{intent_provider}->{intent_model}'
                    executor={
                        'inspect':str(local_cfg.get('agent') or 'codex'),
                        'chat':chat_executor,
                        'create':f"task-queue->planner({planner_cfg.get('provider') or planner_cfg.get('agent') or 'local'})",
                    }.get(action,_builtin_executor(action))
                    mode={
                        'inspect':_local_agent_mode(executor),
                        'chat':'chat',
                        'create':'plan',
                    }.get(action,'command')
                    intent_fields={key:cmd.get(key) for key in (
                        'requirement','project','task_id','requested_action','reason','answer','reply','error','external_id','instruction'
                    ) if cmd.get(key) is not None}
                    _log_route(message_id,stage='route-selected',inbound='dingtalk-stream',text=text,action=action,
                        intent_provider=intent_meta.get('provider'),intent_model=intent_meta.get('model'),
                        confidence=cmd.get('confidence'),executor=executor,mode=mode,
                        decision=_route_decision(action,executor),
                        workspace=cmd.get('project') or (local_cfg.get('default_path') if action=='inspect' else None),
                        intent_duration_ms=intent_meta.get('duration_ms'),
                        intent_error=intent_meta.get('original_error') or intent_meta.get('error'),
                        intent_requirement=intent_fields.get('requirement'),
                        intent_project=intent_fields.get('project'),
                        intent_task_id=intent_fields.get('task_id'),
                        external_id=intent_fields.get('external_id'),
                        intent_instruction=intent_fields.get('instruction'),
                        intent_reply=intent_fields.get('reply'))
                    if action=='need_task_id':
                        reply=f'该操作需要明确任务编号。请回复：{cmd.get("requested_action")} TASK-000001'
                    elif action=='workspace':
                        reply=f'当前工作目录：{_get_conversation_workspace(sender,conv) or local_cfg.get("default_path") or "未配置"}'
                    elif action=='set_workspace':
                        ok,message=_set_conversation_workspace(sender,conv,str(cmd.get('project') or ''))
                        reply=message
                    elif action=='clear_workspace':
                        reply=_clear_conversation_workspace(sender,conv)
                    elif action=='external_command':
                        command_executor='external-agent-session:resume-or-queue'
                        _log_route(message_id,stage='command-start',action=action,executor=command_executor,
                            external_id=cmd.get('external_id'),mode='external-session')
                        reply=str(await asyncio.to_thread(
                            external_agents.send_instruction,str(cmd.get('external_id') or ''),str(cmd.get('instruction') or '')
                        ))[:4000]
                        _log_route(message_id,stage='command-finished',action=action,executor=command_executor,
                            external_id=cmd.get('external_id'),mode='external-session',outcome='success',summary=reply)
                    elif action=='inspect':
                        reply=str(await asyncio.to_thread(_run_local_inspection,cmd,message_id,_get_conversation_workspace(sender,conv)))[:2000]
                    elif action=='chat':
                        reply=str(cmd.get('reply') or '你好，我在。你可以直接和我交流，也可以描述一个工程任务。')[:2000]
                    elif action=='unknown':
                        reply=str(cmd.get('reply') or '我在，不过这句话我还没完全理解。你可以继续和我交流，也可以直接描述工程任务。')[:2000]
                    else:
                        if action=='create' and not cmd.get('project'):
                            session_workspace=_get_conversation_workspace(sender,conv)
                            if session_workspace: cmd['project']=session_workspace
                        command_executor=_builtin_executor(action)
                        _log_route(message_id,stage='command-start',action=action,executor=command_executor,mode='command')
                        reply=str(dispatch_command(cmd,message_id=message_id))[:2000]
                        _log_route(message_id,stage='command-finished',action=action,executor=command_executor,
                            mode='command',outcome='success',summary=reply)
                if locals().get('priority_ack'):
                    _remember_assistant_message(sender,conv,reply)
                else:
                    _remember_conversation(sender,conv,text,reply)
                _log_route(message_id,stage='reply-start',channel='dingtalk-stream',reply_chars=len(reply))
                self.reply_text(reply,incoming)
                _log_route(message_id,stage='reply-finished',channel='dingtalk-stream',outcome='success',
                    reply_chars=len(reply))
                duration_ms=round((time.monotonic()-started)*1000)
                resolved_intent_duration=intent_duration_ms if 'intent_duration_ms' in locals() else None
                execution_duration_ms=max(0,duration_ms-(resolved_intent_duration or 0))
                final_action=str(locals().get('action','identity' if incoming and text and text.strip().lower() in ('身份','whoami','id') else 'unknown'))
                final_intent_provider=str(locals().get('intent_meta',{}).get('provider') or 'exact-rules')
                final_executor=str(locals().get('executor') or _builtin_executor(final_action))
                processing_path=' -> '.join(x for x in (
                    'received','authorized',
                    f"intent:{final_intent_provider}",f"action:{final_action}",
                    'ack:dingtalk-stream' if locals().get('priority_ack') else None,
                    f"executor:{final_executor}",f"mode:{locals().get('mode','command')}",
                    'reply:dingtalk-stream') if x)
                _log_route(message_id,stage='trace-summary',inbound='dingtalk-stream',outcome='success',
                    processing_path=processing_path,duration_ms=duration_ms,
                    intent_duration_ms=resolved_intent_duration,
                    execution_duration_ms=execution_duration_ms,
                    performance='slow' if duration_ms>=3000 else 'normal',
                    reply_chars=len(reply),summary=reply)
                logger.info('inbound handled message_id=%s sender=%s reply=%r',message_id,sender or '<unknown>',reply)
            except Exception as e:
                duration=round((time.monotonic()-started)*1000) if 'started' in locals() else None
                _log_route(locals().get('message_id','unknown'),stage='finished',inbound='dingtalk-stream',
                    outcome='failed',duration_ms=duration,error=e)
                logger.exception('inbound message failed message_id=%s: %s',locals().get('message_id','unknown'),e)
                try: self.reply_text(f'\u5904\u7406\u5931\u8d25\uff1a{e}',incoming or dingtalk_stream.ChatbotMessage.from_dict(message.data))
                except Exception: logger.exception('failed to send inbound error reply')
            return dingtalk_stream.AckMessage.STATUS_OK,'OK'

    def _run():
        retry=max(1,int(cfg.get('stream_retry_seconds',5)))
        interval=max(5,int(cfg.get('heartbeat_seconds',15)))
        stream_thread=threading.current_thread()
        def _heartbeat():
            while stream_thread.is_alive():
                _write_stream_state(running=True,status='\u9489\u9489 Stream \u5df2\u542f\u52a8',pid=os.getpid(),heartbeat_at=datetime.now(timezone.utc).isoformat())
                time.sleep(interval)
        threading.Thread(target=_heartbeat,daemon=True,name='dingtalk-stream-heartbeat').start()
        while True:
            started_at=datetime.now(timezone.utc).isoformat()
            _write_stream_state(running=True,status='\u9489\u9489 Stream \u5df2\u542f\u52a8',pid=os.getpid(),started_at=started_at,heartbeat_at=started_at)
            logger.info('dingtalk stream connecting (pid=%s)',os.getpid())
            try:
                credential=dingtalk_stream.Credential(cfg['client_id'],cfg['client_secret'])
                client=dingtalk_stream.DingTalkStreamClient(credential)
                root=configure_logger('dingtalk'); handler=root.handlers[0]
                for name in ('dingtalk_stream','dingtalk_stream.client'):
                    third_party=logging.getLogger(name); third_party.addHandler(handler)
                    third_party.setLevel(logging.INFO); third_party.propagate=False
                client.register_callback_handler(dingtalk_stream.ChatbotMessage.TOPIC,PersonZitHandler())
                client.start_forever()
                logger.warning('dingtalk stream exited; reconnecting in %ss',retry)
            except Exception as e:
                logger.exception('dingtalk stream failed: %s; reconnecting in %ss',e,retry)
                _write_stream_state(running=False,status=f'Stream \u5f02\u5e38\uff1a{e}',last_error=str(e))
            time.sleep(retry)
    thread=threading.Thread(target=_run,daemon=True,name='dingtalk-stream'); thread.start()
    return thread,'\u9489\u9489 Stream \u5df2\u542f\u52a8'

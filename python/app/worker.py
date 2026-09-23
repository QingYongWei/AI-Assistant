import time, socket, json, os, re, subprocess, threading
from pathlib import Path
from datetime import datetime, timezone
from sqlalchemy import select
from .database import init_db,SessionLocal
from .queue import SqliteQueue
from .models import Task,SubTask,EventLog,AgentRun,VerificationRun,Approval,Job
from .state_machine import transition
from .agents import AgentRequest,resolve_agent
from .security import is_high_risk
from .planner import make_planner,MockPlanner
from .verification import run_verification
from .logging_config import configure_logger
from .workspace import GitWorkspace
from .dingtalk import start_stream_service,notify_task_event,send_notification
from . import external_agents
from . import config
from .workspace import ensure_default_workspace
from .config import load_config,workspace_root,workspace_tasks,workspace_documents

worker_logger=configure_logger('worker')
TERMINAL={'CANCELLED','COMPLETED','FAILED'}
STOP_STATES=TERMINAL|{'PAUSED','WAITING_FOR_HUMAN','WAITING_FOR_APPROVAL','WAITING_FOR_CLARIFICATION'}

def _task_route(task,stage,**values):
    payload={k:v for k,v in values.items() if v is not None}
    if 'summary' in payload: payload['summary']=str(payload['summary'])[:240].replace('\n',' ')
    if 'error' in payload: payload['error']=str(payload['error'])[:500]
    for key in ('duration_ms','changed_files'): 
        if key in payload and payload[key] is not None: payload[key]=int(payload[key])
    worker_logger.info('task route task_id=%s stage=%s %s',task.public_id,stage,json.dumps(payload,ensure_ascii=False,separators=(',',':')))

def _planner_info():
    cfg=config.load_config(); planner=cfg.get('planner',{}) or {}
    provider=str(planner.get('provider') or planner.get('agent') or 'local')
    model=str(planner.get('model') or (cfg.get('ai_providers',{}).get(provider,{}) or {}).get('model') or planner.get('agent') or provider)
    return planner,provider,model

def _agent_mode(name):
    cfg=config.load_config().get('agents',{}).get(name,{}) or {}
    args=[str(x) for x in cfg.get('arguments',[])]
    if name=='codex':
        defaults=['exec','--sandbox','workspace-write','{prompt}']; args=args or defaults
        for i,value in enumerate(args):
            if value=='--sandbox' and i+1<len(args): return args[i+1]
        return 'workspace-write'
    if name=='claude':
        defaults=['-p','{prompt}','--permission-mode','acceptEdits']; args=args or defaults
        for i,value in enumerate(args):
            if value=='--permission-mode' and i+1<len(args): return args[i+1]
        return 'acceptEdits'
    return 'unknown'

def log_event(db,task,event_type,payload):
    db.add(EventLog(task_id=task.id,event_type=event_type,actor_type='system',actor_id='worker',
                    payload_json=json.dumps(payload,ensure_ascii=False)))


def _locked_worker_pid(lock_id):
    if not lock_id:
        return None
    match=re.search(r'-(\d+)$',str(lock_id))
    return int(match.group(1)) if match else None

def _worker_process_alive(pid):
    """判断锁上的 Worker PID 是否仍是本机 Python 进程，避免 PID 文件残留导致误判。"""
    if not pid or pid<=0:
        return False
    if pid==os.getpid():
        return True
    try:
        if os.name=='nt':
            flags=subprocess.CREATE_NO_WINDOW
            result=subprocess.run(['tasklist','/FI',f'PID eq {pid}','/FO','CSV','/NH'],
                                  capture_output=True,text=True,timeout=5,creationflags=flags)
            row=next((line for line in (result.stdout or '').splitlines() if line.strip() and '"INFO:' not in line),None)
            if not row:
                return False
            fields=row.split('","')
            process_name=(fields[0].strip('"') if fields else '').lower()
            return process_name in ('python.exe','pythonw.exe')
        os.kill(pid,0)
        return True
    except (OSError,subprocess.SubprocessError,ValueError):
        return False

def recover_orphaned_jobs(db,current_worker):
    """服务重启后立即回收死 Worker 留下的 RUNNING Job，不等 30 分钟心跳超时。"""
    recovered=[]; skipped=[]; recovered_by_task={}
    candidates=db.scalars(select(Job).where(Job.status=='RUNNING')).all()
    for job in candidates:
        if job.locked_by==current_worker:
            continue
        pid=_locked_worker_pid(job.locked_by)
        if not pid or _worker_process_alive(pid):
            skipped.append({'job':job.public_id,'locked_by':job.locked_by,'pid':pid})
            continue
        job.status='QUEUED'; job.locked_by=None; job.locked_at=None; job.heartbeat_at=None
        job.available_at=datetime.now(timezone.utc)
        recovered.append(job.public_id); recovered_by_task.setdefault(job.task_id,[]).append(job.public_id)
    if not recovered:
        return recovered,skipped
    now_utc=datetime.now(timezone.utc)
    for task_id,recovered_jobs in recovered_by_task.items():
        for run in db.scalars(select(AgentRun).where(AgentRun.task_id==task_id,AgentRun.status=='RUNNING')).all():
            run.status='INTERRUPTED'; run.finished_at=now_utc
            run.result_json=json.dumps({'summary':'worker process exited; job safely requeued'},ensure_ascii=False)
        for sub in db.scalars(select(SubTask).where(SubTask.task_id==task_id,SubTask.status=='RUNNING')).all():
            sub.status='PENDING'
        task=db.get(Task,task_id)
        if task:
            log_event(db,task,'ORPHAN_JOB_RECOVERED',{
                'reason':'locked worker process is no longer alive',
                'jobs':sorted(recovered_jobs),
            })
            _task_route(task,'orphan-recovered',jobs=sorted(recovered_jobs),outcome='requeued')
    db.commit()
    return recovered,skipped

def safe_transition(db,task,target,reason=''):
    if task.status!=target: transition(db,task,target,actor='worker',reason=reason)

def _refresh_cancelled(db,task):
    try: db.refresh(task)
    except (AttributeError,TypeError): pass
    return task.status in ('CANCEL_REQUESTED','CANCELLED')

def _mark_cancelled_agent_run(db,task,run,sub):
    run.status='CANCELLED'; run.finished_at=datetime.now(timezone.utc)
    run.result_json=json.dumps({'summary':'task cancelled from DingTalk'},ensure_ascii=False)
    sub.status='CANCELLED'; sub.result_summary='任务已取消'
    if task.status=='CANCEL_REQUESTED':
        transition(db,task,'CANCELLED',actor='worker',reason='local agent process returned after cancel')
    db.commit(); _task_route(task,'agent-cancelled',subtask=sub.public_id,executor=run.agent_type,outcome='cancelled')

def ensure_workspace_dir(db,task):
    """默认直接使用项目当前分支；仅在 master 分支创建 Git Worktree 隔离执行。"""
    try:
        repo=GitWorkspace(task.project_path,workspace_tasks())
        policy=str((config.load_config().get('workspace',{}) or {}).get('isolation_policy','current-branch-except-master')).lower()
        if repo.is_repo() and policy=='current-branch-except-master':
            branch=repo.current_branch()
            if branch and branch.lower()!='master':
                task.execution_branch=branch
                log_event(db,task,'WORKSPACE_SELECTED',{
                    'mode':'current-branch','branch':branch,'workspace':str(repo.root),
                    'requested_project':task.project_path,
                })
                return repo.root,False
        path=repo.create(task.public_id)
        task.execution_branch=f'personzit/{task.public_id.lower()}'
        log_event(db,task,'WORKSPACE_SELECTED',{
            'mode':'worktree','reason':'master branch or detached HEAD',
            'workspace':str(path),'requested_project':task.project_path,
        })
        return path,True
    except Exception as e:
        fallback=workspace_tasks()/task.public_id
        fallback.mkdir(parents=True,exist_ok=True)
        log_event(db,task,'WORKSPACE_FALLBACK',{'reason':str(e),'requested_project':task.project_path,'workspace':str(fallback)})
        return fallback,False

def ensure_subtasks(db,task):
    subs=db.scalars(select(SubTask).where(SubTask.task_id==task.id).order_by(SubTask.order_index,SubTask.id)).all()
    if subs: return list(subs)
    plan=json.loads(task.plan_json or '{}')
    for i,spec in enumerate(plan.get('subtasks',[]),1):
        db.add(SubTask(public_id=f'{task.public_id}-ST-{i:03d}',task_id=task.id,key=str(spec.get('key',f'st{i}')),title=spec.get('title',f'子任务{i}'),
            description=spec.get('description',''),role=spec.get('role','implementation'),
            agent_type=spec.get('suggested_agent','codex'),order_index=i-1,
            dependencies_json=json.dumps(spec.get('dependencies',[])),
            acceptance_criteria_json=json.dumps(spec.get('acceptance_criteria',[]),ensure_ascii=False)))
    db.commit()
    return list(db.scalars(select(SubTask).where(SubTask.task_id==task.id).order_by(SubTask.order_index,SubTask.id)).all())

def build_prompt(task,sub,workspace,repair_failures=None,workspace_isolated=True):
    criteria=json.loads(sub.acceptance_criteria_json or '[]')
    lines=[f'# 任务：{task.title}','',task.description or '','',f'## 子任务：{sub.title }',sub.description or '','',
        '## 验收条件']
    lines+= [f'- {c}' for c in criteria] or ['- 完成子任务要求']
    lines+=['','## 执行要求',
        f'- 工作目录：{workspace}（{"已通过 Git Worktree 隔离" if workspace_isolated else "使用项目当前分支"}，只允许修改该目录内的文件）',
        '- 直接修改代码完成需求，不要只输出建议或说明',
        '- 如验收需要依赖或测试环境，先在工作区内自行准备',
        '- 完成后确保相关测试或检查可以运行']
    if repair_failures:
        lines+=['','## 上次验收失败（请针对这些问题修复）']+[f'- {f}' for f in repair_failures]
    return '\n'.join(lines)

def run_agent_for_subtask(db,task,sub,workspace,repair_failures=None,workspace_isolated=True):
    prompt=build_prompt(task,sub,workspace,repair_failures,workspace_isolated=workspace_isolated)
    agent=resolve_agent(sub.role,sub.agent_type)
    if agent is None:
        safe_transition(db,task,'WAITING_FOR_HUMAN',reason=f'无可用 Agent 执行 {sub.public_id}'); db.commit(); return None
    run=AgentRun(task_id=task.id,subtask_id=sub.id,agent_type=agent.name,command_json=json.dumps([agent.name]),
        prompt=prompt,status='RUNNING')
    db.add(run); sub.status='RUNNING'; sub.agent_type=agent.name; sub.workspace_path=str(workspace)
    mode=_agent_mode(agent.name)
    _task_route(task,'agent-start',subtask=sub.public_id,executor=agent.name,mode=mode,workspace=str(workspace),role=sub.role)
    log_event(db,task,'AGENT_RUN_STARTED',{'subtask':sub.public_id,'agent':agent.name,'mode':mode,'workspace':str(workspace)}); db.commit()
    result=agent.run(AgentRequest(task.public_id,sub.public_id,prompt,workspace))  # 长耗时执行，不持有数据库事务
    if _refresh_cancelled(db,task):
        _mark_cancelled_agent_run(db,task,run,sub); return None
    duration_ms=int((result.finished_at-result.started_at).total_seconds()*1000) if result.started_at and result.finished_at else None
    _task_route(task,'agent-finished',subtask=sub.public_id,executor=agent.name,mode=mode,workspace=str(workspace),
        outcome='success' if result.success else 'failed',duration_ms=duration_ms,
        summary=result.summary,changed_files=len(result.changed_files),stdout=str(result.stdout_path),stderr=str(result.stderr_path))
    run.status='SUCCEEDED' if result.success else 'FAILED'; run.exit_code=result.exit_code
    run.stdout_path=str(result.stdout_path); run.stderr_path=str(result.stderr_path); run.finished_at=result.finished_at
    run.result_json=json.dumps({'summary':result.summary,'changed_files':result.changed_files},ensure_ascii=False)
    sub.status='COMPLETED' if result.success else 'FAILED'; sub.result_summary=result.summary
    log_event(db,task,'AGENT_RUN_FINISHED',{'subtask':sub.public_id,'agent':agent.name,'success':result.success,
        'summary':result.summary,'changed_files':result.changed_files}); db.commit()
    return result



def _write_plan_document(task,plan):
    documents=workspace_documents(); documents.mkdir(parents=True,exist_ok=True)
    path=documents/f'{task.public_id}-plan.md'
    criteria='\n'.join(f'- {item}' for item in plan.get('acceptance_criteria',[])) or '- 无'
    questions='\n'.join(f'{index}. {item}' for index,item in enumerate(plan.get('clarification_questions') or [],1)) or '无'
    subtasks=plan.get('subtasks',[])
    subtask_lines='\n'.join(
        f'{index}. {item.get("title", "")}（{item.get("role", "implementation")} / {item.get("suggested_agent", "codex")}）\n   {item.get("description", "")}'
        for index,item in enumerate(subtasks,1)
    ) or '无'
    path.write_text(
        f'# {task.public_id} 需求与方案\n\n'
        f'- 标题：{task.title}\n'
        f'- 项目路径：{task.project_path}\n'
        f'- 风险等级：{plan.get("risk_level", "medium")}\n\n'
        f'## 需求\n\n{task.description or ""}\n\n'
        f'## 摘要\n\n{plan.get("summary", "")}\n\n'
        f'## 验收标准\n\n{criteria}\n\n'
        f'## 澄清问题\n\n{questions}\n\n'
        f'## 子任务\n\n{subtask_lines}\n',
        encoding='utf8'
    )
    return path

def handle_plan(db,task,job):
    payload=json.loads(job.payload_json or '{}')
    title=payload.get('title',task.title); description=payload.get('description',task.description)
    project_path=payload.get('project_path',task.project_path)
    verification=payload.get('verification_commands') or []
    answers=payload.get('answers') or []
    planner=make_planner()
    _,planner_provider,planner_model=_planner_info()
    _task_route(task,'planner-start',executor=planner_provider,model=planner_model,workspace=project_path,
        stage_type=type(planner).__name__)
    try:
        plan=planner.plan(title,description,project_path=project_path,verification_commands=verification,
                          task_id=task.public_id,clarification=answers or None)
        planner_name=planner.name
        _task_route(task,'planner-finished',executor=planner_provider,model=planner_model,outcome='success',
            subtasks=len(plan.get('subtasks',[])),risk_level=plan.get('risk_level'))
        if _refresh_cancelled(db,task):
            if task.status=='CANCEL_REQUESTED': transition(db,task,'CANCELLED',actor='worker',reason='planner returned after cancel')
            db.commit(); _task_route(task,'planner-cancelled',executor=planner_provider,outcome='cancelled'); return
    except Exception as e:
        _task_route(task,'planner-finished',executor=planner_provider,model=planner_model,outcome='failed',error=e)
        plan=MockPlanner().plan(title,description,project_path=project_path,verification_commands=verification)
        planner_name='mock(fallback)'
        _task_route(task,'planner-fallback',executor='mock',model='rules',reason='primary planner failed')
        log_event(db,task,'PLANNER_FALLBACK',{'error':str(e)})
    task.plan_json=json.dumps(plan,ensure_ascii=False)
    task.acceptance_criteria_json=json.dumps(plan.get('acceptance_criteria',[]),ensure_ascii=False)
    plan_document=_write_plan_document(task,plan)
    log_event(db,task,'PLAN_GENERATED',{'planner':planner_name,'subtasks':len(plan.get('subtasks',[])),
        'requires_clarification':plan.get('requires_clarification',False),'plan_document':str(plan_document)})
    if plan.get('requires_clarification') and plan.get('clarification_questions'):
        transition(db,task,'WAITING_FOR_CLARIFICATION',actor='worker',reason='需求信息不足，等待澄清'); db.commit()
        notify_task_event('clarification',task); return
    transition(db,task,'PLAN_PROPOSED',actor='worker',reason=f'{planner_name} 规划完成')
    # 钉钉群消息任务：需求明确时不再等待人工审批，直接进入执行阶段。
    # CLI/API 任务保留原审批模式，便于本地高风险操作仍可人工把关。
    if str(task.source or '').lower()=='dingtalk':
        transition(db,task,'QUEUED',actor='system',reason='钉钉任务需求明确，自动进入执行')
        task.approved_at=datetime.now(timezone.utc)
        log_event(db,task,'AUTO_STARTED',{'reason':'requirement is clear','source':task.source,'planner':planner_name})
        db.commit()
        SqliteQueue(db).enqueue('EXECUTE_SUBTASK',task.id)
        notify_task_event('auto_started',task)
        _task_route(task,'planner-auto-start',executor=planner_provider,model=planner_model,
            outcome='queued',next_stage='EXECUTE_SUBTASK',risk_level=plan.get('risk_level'))
        return
    transition(db,task,'WAITING_FOR_APPROVAL',actor='worker',reason='approval required')
    db.add(Approval(task_id=task.id,request_reason='计划需要人工批准',risk_level=plan.get('risk_level','medium'),
        request_payload_json=json.dumps(plan,ensure_ascii=False)))
    db.commit(); notify_task_event('approval',task)

def handle_execute(db,task,job,repair_failures=None):
    if _refresh_cancelled(db,task): return
    safe_transition(db,task,'EXECUTING',reason='repair started' if repair_failures else 'worker claimed job'); db.commit()
    _task_route(task,'execute-start',source_project=task.project_path,repair=bool(repair_failures))
    workspace,isolated=ensure_workspace_dir(db,task)
    log_event(db,task,'WORKSPACE_PREPARED',{'workspace':str(workspace),'isolated':isolated}); db.commit()
    _task_route(task,'workspace-prepared',workspace=str(workspace),isolated=isolated,source_project=task.project_path)
    subs=ensure_subtasks(db,task)
    targets=[s for s in subs if s.status!='COMPLETED']
    if repair_failures:
        targets=[s for s in subs if s.role=='implementation'] or targets
        for s in targets: s.status='PENDING'
        db.commit()
    key_to_sub={s.key:s for s in subs if s.key}
    remaining=list(targets)
    while remaining:
        progressed=False
        for sub in list(remaining):
            if _refresh_cancelled(db,task): return
            deps=json.loads(sub.dependencies_json or '[]')
            if any((d:=key_to_sub.get(dep)) is not None and d.status!='COMPLETED' for dep in deps): continue
            result=run_agent_for_subtask(db,task,sub,workspace,repair_failures,workspace_isolated=isolated)
            if result is None: return  # 已进入 WAITING_FOR_HUMAN
            if not result.success: raise RuntimeError(f'{sub.public_id} Agent 执行失败：{result.summary}')
            remaining.remove(sub); progressed=True
        if not progressed: raise RuntimeError('子任务存在循环依赖或无法调度，请调整计划')
    if _refresh_cancelled(db,task): return
    safe_transition(db,task,'VERIFYING',reason='所有子任务执行完成'); db.commit()
    _task_route(task,'execute-finished',workspace=str(workspace),outcome='success',next_stage='VERIFY_SUBTASK')
    SqliteQueue(db).enqueue('VERIFY_SUBTASK',task.id)

def _revision_job_pending(db,task_id,exclude_job_id=None):
    query=select(Job).where(
        Job.task_id==task_id,Job.job_type=='REVISE_TASK',
        Job.status.in_(['QUEUED','RETRY_WAIT','RUNNING'])
    )
    if exclude_job_id is not None:
        query=query.where(Job.id!=exclude_job_id)
    return db.scalar(query.order_by(Job.id.desc()).limit(1))


def handle_revision(db,task,job):
    """Apply a DingTalk supplement after the currently claimed stage finishes."""
    payload=json.loads(job.payload_json or '{}')
    requirement=str(payload.get('requirement') or '').strip()
    if task.status in TERMINAL:
        _task_route(task,'revision-skipped',reason='task already terminal',requirement=requirement)
        return
    _task_route(task,'revision-start',requirement=requirement,from_status=task.status)
    if task.status=='WAITING_FOR_APPROVAL':
        transition(db,task,'ANALYZING',reason='supplement requires replanning')
        SqliteQueue(db).enqueue('PLAN_TASK',task.id,payload=json.dumps({'title':task.title,'description':task.description,'project_path':task.project_path},ensure_ascii=False))
        db.commit(); return
    if task.status in ('ANALYZING','WAITING_FOR_CLARIFICATION','WAITING_FOR_HUMAN','PAUSED'):
        _task_route(task,'revision-deferred',reason='task is waiting for planner/human/clarification',requirement=requirement)
        db.commit(); return
    if task.status=='VERIFYING':
        transition(db,task,'REPAIRING',reason='supplement received before completion')
        db.commit()
    subs=db.scalars(select(SubTask).where(SubTask.task_id==task.id)).all()
    for sub in subs:
        sub.status='PENDING'; sub.result_summary=None
    db.commit()
    _task_route(task,'revision-queued',subtasks=len(subs),to_status='EXECUTING',requirement=requirement)
    return handle_execute(db,task,job,repair_failures=[f'钉钉补充要求：{requirement}'] if requirement else None)


def handle_verify(db,task,job):
    payload=json.loads(job.payload_json or '{}')
    approved_command=payload.get('approved_command')
    approval_id=payload.get('approval_id')
    safe_transition(db,task,'VERIFYING',reason='worker claimed verify job'); db.commit()
    workspace,_=ensure_workspace_dir(db,task)
    commands=json.loads(task.plan_json or '{}').get('verification_commands',[])
    failures=[]
    _task_route(task,'verification-start',workspace=str(workspace),commands=len(commands))
    for command in commands:
        if _refresh_cancelled(db,task): return
        # 高风险命令必须先落库为 VERIFICATION 审批，得到人工批准后才允许执行。
        if is_high_risk(command) and not (approved_command==command and approval_id):
            approval=Approval(task_id=task.id,approval_type='VERIFICATION',risk_level='high',
                request_reason=f'高风险验收命令需要人工确认：{command}',
                request_payload_json=json.dumps({'command':command,'working_directory':str(workspace)},ensure_ascii=False))
            db.add(approval); db.flush()
            log_event(db,task,'HIGH_RISK_PENDING',{'command':command,'approval_id':approval.id,'workspace':str(workspace)})
            transition(db,task,'WAITING_FOR_HUMAN',actor='worker',reason='等待高风险验收命令人工审批')
            db.commit(); notify_task_event('waiting_human',task,extra_lines=[f'高风险命令：{command}'])
            return
        vr=run_verification(command,str(workspace),allow_high_risk=bool(approved_command==command and approval_id))
        if _refresh_cancelled(db,task):
            _task_route(task,'verification-cancelled',workspace=str(workspace),command=command,outcome='cancelled'); return
        _task_route(task,'verification-finished',executor='personzit-worker',workspace=str(workspace),command=command,
            outcome='passed' if vr.passed else 'failed',exit_code=vr.exit_code,duration_ms=vr.duration_ms,summary=vr.summary)
        artifacts=config.home()/'artifacts'/task.public_id; artifacts.mkdir(parents=True,exist_ok=True)
        stamp=datetime.now().strftime('%Y%m%d-%H%M%S-%f')
        out=artifacts/f'verify-{stamp}.stdout.log'; err=artifacts/f'verify-{stamp}.stderr.log'
        out.write_text(vr.stdout or '',encoding='utf8',errors='replace')
        err.write_text(vr.stderr or '',encoding='utf8',errors='replace')
        db.add(VerificationRun(task_id=task.id,command_json=json.dumps(command),working_directory=str(workspace),
            status='PASSED' if vr.passed else 'FAILED',exit_code=vr.exit_code,passed=vr.passed,
            duration_ms=vr.duration_ms,stdout_path=str(out),stderr_path=str(err),summary=vr.summary))
        log_event(db,task,'VERIFICATION_FINISHED',{'command':command,'passed':vr.passed,'summary':vr.summary,
            'approval_id':approval_id if approved_command==command else None})
        if not vr.passed: failures.append(f'{command}：{vr.summary}；输出：{(vr.stderr or vr.stdout or "").strip()[-400:]}')
    if not commands: log_event(db,task,'VERIFICATION_SKIPPED',{'reason':'计划中无验收命令'})
    if _refresh_cancelled(db,task): return
    if not failures:
        if _revision_job_pending(db,task.id,exclude_job_id=job.id):
            transition(db,task,'REPAIRING',actor='worker',reason='验收通过但存在钉钉补充要求，先修订再完成')
            db.commit(); _task_route(task,'verification-deferred',reason='supplement pending',next_stage='REVISE_TASK'); return
        transition(db,task,'COMPLETED',actor='worker',reason='所有验收命令通过' if commands else '无验收命令，按计划完成')
        task.completed_at=datetime.now(timezone.utc); db.commit()
        notify_task_event('completed',task,extra_lines=[f'验收命令：{c}' for c in commands] or ['（无验收命令，按计划完成）']); return
    db.commit()
    task.retry_count+=1
    if task.retry_count<=task.max_retries:
        transition(db,task,'REPAIRING',actor='worker',reason=f'验收失败，开始第 {task.retry_count} 次自动修复')
        db.commit()
        SqliteQueue(db).enqueue('REPAIR_SUBTASK',task.id,payload=json.dumps({'failures':failures},ensure_ascii=False))
    else:
        transition(db,task,'WAITING_FOR_HUMAN',actor='worker',reason=f'自动修复 {task.max_retries} 次后仍未通过'); db.commit()
        notify_task_event('waiting_human',task,extra_lines=[f'- {f}' for f in failures[-3:]])

def handle(job,db):
    task=db.get(Task,job.task_id)
    if not task: return
    if task.status=='CANCEL_REQUESTED':
        transition(db,task,'CANCELLED',actor='worker',reason='cancel requested'); db.commit(); return
    if job.job_type=='REVISE_TASK': return handle_revision(db,task,job)
    if task.status in STOP_STATES: return
    if job.job_type=='PLAN_TASK': return handle_plan(db,task,job)
    if job.job_type=='EXECUTE_SUBTASK': return handle_execute(db,task,job)
    if job.job_type=='REPAIR_SUBTASK':
        return handle_execute(db,task,job,repair_failures=json.loads(job.payload_json or '{}').get('failures'))
    if job.job_type=='VERIFY_SUBTASK': return handle_verify(db,task,job)

def _stale_threshold():
    from .config import load_config as _lc
    cfg=_lc(); base=int(cfg.get("worker",{}).get("stale_after_seconds",300))
    agents=cfg.get("agents",{})
    timeouts=[int(a.get("timeout_seconds",1800)) for a in agents.values() if isinstance(a,dict)]
    return max(base,(max(timeouts) if timeouts else 1800)+60)

def _external_monitor_loop() -> None:
    """Watch manually opened Codex/Claude sessions and push DingTalk events."""
    while True:
        try:
            snapshots,pending=external_agents.scan_external_agents()
            if pending:
                worker_logger.info('external agent monitor scan sessions=%s pending_notifications=%s',len(snapshots),len(pending))
            sent=external_agents.drain_external_notifications(
                sender=lambda title,text: send_notification(title,text)[0]
            )
            for event in sent:
                worker_logger.info('external agent notification sent external_id=%s agent=%s kind=%s',
                    event.get('external_id'),event.get('agent'),event.get('kind'))
        except Exception as e:
            worker_logger.warning('external agent monitor scan failed: %s',e)
        interval=max(2,int(load_config().get('external_monitor',{}).get('interval_seconds',5)))
        time.sleep(interval)


def main():
    init_db(); ensure_default_workspace(); worker=f'worker-{socket.gethostname()}-{os.getpid()}'
    with SessionLocal() as db:
        recovered,skipped=recover_orphaned_jobs(db,worker)
        worker_logger.info('startup orphan recovery recovered=%s skipped=%s',json.dumps(recovered,ensure_ascii=False),json.dumps(skipped,ensure_ascii=False))
    try: start_stream_service()
    except Exception as e: print(f'钉钉 Stream 启动失败：{e}',flush=True)
    monitor=threading.Thread(target=_external_monitor_loop,name='external-agent-monitor',daemon=True)
    monitor.start()
    worker_logger.info('external agent monitor started interval_seconds=%s',max(2,int(load_config().get('external_monitor',{}).get('interval_seconds',5))))
    while True:
        with SessionLocal() as db:
            q=SqliteQueue(db); q.recover_stale(seconds=_stale_threshold(),worker_id=worker); job=q.claim_next(worker)
            if job:
                try:
                    handle(job,db); q.complete(job)
                except Exception as e:
                    try: db.rollback()
                    except Exception: pass
                    try:
                        q.fail(job,e)
                        if job.status=='FAILED':
                            t=db.get(Task,job.task_id)
                            if t and t.status not in STOP_STATES:
                                transition(db,t,'WAITING_FOR_HUMAN',reason=f'Job 重试次数耗尽：{e}'); db.commit()
                    except Exception: db.rollback()
        time.sleep(1)
if __name__=='__main__': main()







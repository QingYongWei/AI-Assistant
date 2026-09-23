from fastapi import FastAPI, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from sqlalchemy import select
from datetime import datetime, timezone
import json
import time
from .database import init_db,get_db
from .models import Task,SubTask,Approval,EventLog,VerificationRun
from .state_machine import transition
from .planner import MockPlanner
from .queue import SqliteQueue
from .task_ids import semantic_task_id
from .agents import registry
from .logging_config import configure_logger
from .ai_providers import environment_value,provider_settings

logger=configure_logger('api')
app=FastAPI(title='PersonZit API',version='0.1.0'); init_db()

@app.middleware('http')
async def log_http_requests(request,call_next):
    started=time.perf_counter()
    try:
        response=await call_next(request)
    except Exception:
        logger.exception('HTTP %s %s failed after %.1fms',request.method,request.url.path,(time.perf_counter()-started)*1000)
        raise
    logger.info('HTTP %s %s -> %s (%.1fms)',request.method,request.url.path,response.status_code,(time.perf_counter()-started)*1000)
    return response
class TaskCreate(BaseModel): title:str=Field(min_length=1,max_length=255); description:str=Field(min_length=1); project_path:str; priority:int=0; source:str='cli'; verification_commands:list[str]=Field(default_factory=list)
class Action(BaseModel): reason:str|None=None
def task_dict(t): return {'id':t.id,'public_id':t.public_id,'title':t.title,'description':t.description,'status':t.status,'priority':t.priority,'project_path':t.project_path,'plan':json.loads(t.plan_json) if t.plan_json else None,'acceptance_criteria':json.loads(t.acceptance_criteria_json) if t.acceptance_criteria_json else [],'created_at':t.created_at.isoformat() if t.created_at else None}
def get_task(id,db):
    t=db.scalar(select(Task).where(Task.public_id==id))
    if not t: raise HTTPException(404,'任务不存在')
    return t
@app.get('/health')
def health(): return {'status':'ok','service':'personzit-api','version':'0.1.0'}
@app.get('/api/tasks')
def tasks(db:Session=Depends(get_db)): return [task_dict(t) for t in db.scalars(select(Task).order_by(Task.id.desc())).all()]
@app.post('/api/tasks',status_code=201)
def create_task(body:TaskCreate,db:Session=Depends(get_db)):
    t=Task(public_id='PENDING',title=body.title,description=body.description,project_path=body.project_path,priority=body.priority,source=body.source); db.add(t); db.flush(); t.public_id=semantic_task_id(body.title,t.id); transition(db,t,'ANALYZING',reason='task created'); db.commit()
    SqliteQueue(db).enqueue('PLAN_TASK',t.id,payload=json.dumps({'title':body.title,'description':body.description,'project_path':body.project_path,'verification_commands':body.verification_commands},ensure_ascii=False))
    return task_dict(t)
@app.get('/api/tasks/{id}')
def show(id:str,db:Session=Depends(get_db)): return task_dict(get_task(id,db))
@app.get('/api/tasks/{id}/events')
def events(id:str,db:Session=Depends(get_db)):
    t=get_task(id,db); return [{'type':e.event_type,'actor':e.actor_id,'payload':json.loads(e.payload_json),'created_at':e.created_at.isoformat()} for e in db.scalars(select(EventLog).where(EventLog.task_id==t.id).order_by(EventLog.id)).all()]
@app.post('/api/tasks/{id}/approve')
def approve(id:str,body:Action=Action(),db:Session=Depends(get_db)):
    t=get_task(id,db)
    if t.status=='WAITING_FOR_APPROVAL':
        transition(db,t,'QUEUED',actor='local-user',reason=body.reason or 'approved'); t.approved_at=datetime.now(timezone.utc)
        a=db.scalar(select(Approval).where(Approval.task_id==t.id,Approval.status=='PENDING').order_by(Approval.id.desc()))
        if a:
            a.status='APPROVED'; a.approved_by='local-user'; a.resolved_at=datetime.now(timezone.utc)
            db.add(EventLog(task_id=t.id,event_type='APPROVAL_RESOLVED',actor_type='human',actor_id='local-user',
                payload_json=json.dumps({'approval_id':a.id,'decision':'APPROVED','comment':body.reason},ensure_ascii=False)))
        SqliteQueue(db).enqueue('EXECUTE_SUBTASK',t.id); db.commit(); return task_dict(t)
    if t.status=='WAITING_FOR_HUMAN':
        va=db.scalar(select(Approval).where(Approval.task_id==t.id,Approval.approval_type=='VERIFICATION',Approval.status=='PENDING').order_by(Approval.id.desc()))
        if not va: raise HTTPException(409,'当前没有待批准的高风险验收命令')
        va.status='APPROVED'; va.approved_by='local-user'; va.resolved_at=datetime.now(timezone.utc)
        db.add(EventLog(task_id=t.id,event_type='APPROVAL_RESOLVED',actor_type='human',actor_id='local-user',
            payload_json=json.dumps({'approval_id':va.id,'decision':'APPROVED','type':'VERIFICATION','comment':body.reason},ensure_ascii=False)))
        transition(db,t,'VERIFYING',actor='local-user',reason='高风险验收命令已获批准')
        try: command=json.loads(va.request_payload_json or '{}').get('command')
        except Exception: command=None
        SqliteQueue(db).enqueue('VERIFY_SUBTASK',t.id,payload=json.dumps({'approval_id':va.id,'approved_command':command},ensure_ascii=False)); db.commit(); return task_dict(t)
    raise HTTPException(409,f'当前状态 {t.status} 无法批准')
@app.get('/api/tasks/{id}/approvals')
def approvals(id:str,db:Session=Depends(get_db)):
    t=get_task(id,db); return [{'id':a.id,'type':a.approval_type,'risk_level':a.risk_level,'status':a.status,'reason':a.request_reason,'created_at':a.created_at.isoformat()} for a in db.scalars(select(Approval).where(Approval.task_id==t.id).order_by(Approval.id.desc())).all()]
class Clarify(BaseModel): answers:list[str]=Field(default_factory=list)
@app.post('/api/tasks/{id}/clarify')
def clarify(id:str,body:Clarify,db:Session=Depends(get_db)):
    t=get_task(id,db)
    if t.status!='WAITING_FOR_CLARIFICATION': raise HTTPException(409,'任务当前不在等待澄清状态')
    transition(db,t,'ANALYZING',actor='local-user',reason='收到澄清答复，重新规划')
    SqliteQueue(db).enqueue('PLAN_TASK',t.id,payload=json.dumps({'answers':body.answers},ensure_ascii=False)); db.commit(); return task_dict(t)
@app.post('/api/tasks/{id}/reject')
def reject(id:str,body:Action=Action(),db:Session=Depends(get_db)):
    t=get_task(id,db); approval=db.scalar(select(Approval).where(Approval.task_id==t.id,Approval.status=='PENDING').order_by(Approval.id.desc()))
    if approval: approval.status='REJECTED'; approval.comment=body.reason; approval.resolved_at=datetime.now(timezone.utc)
    transition(db,t,'ANALYZING',actor='local-user',reason=body.reason or 'rejected for revision')
    SqliteQueue(db).enqueue('PLAN_TASK',t.id,payload=json.dumps({'title':t.title,'description':t.description,'project_path':t.project_path},ensure_ascii=False)); db.commit(); return task_dict(t)
@app.get('/api/tasks/{id}/runs')
def runs(id:str,db:Session=Depends(get_db)):
    t=get_task(id,db)
    from .models import AgentRun as AR
    return [{'id':r.id,'agent':r.agent_type,'status':r.status,'exit_code':r.exit_code,'prompt':r.prompt[:200],
        'stdout_path':r.stdout_path,'stderr_path':r.stderr_path,'result':json.loads(r.result_json) if r.result_json else None,
        'started_at':r.started_at.isoformat() if r.started_at else None,'finished_at':r.finished_at.isoformat() if r.finished_at else None}
        for r in db.scalars(select(AR).where(AR.task_id==t.id).order_by(AR.id)).all()]
@app.get('/api/tasks/{id}/report')

def report(id:str,db:Session=Depends(get_db)):
    from .report import task_report
    t=get_task(id,db); return task_report(t,db.scalars(select(SubTask).where(SubTask.task_id==t.id)).all(),db.scalars(select(VerificationRun).where(VerificationRun.task_id==t.id)).all(),db.scalars(select(EventLog).where(EventLog.task_id==t.id)).all())
def action(id,target,db,reason=''):
    t=get_task(id,db); transition(db,t,target,actor='local-user',reason=reason); db.commit(); return task_dict(t)
@app.post('/api/tasks/{id}/pause')
def pause(id:str,body:Action=Action(),db:Session=Depends(get_db)): return action(id,'PAUSED',db,body.reason or 'paused')
@app.post('/api/tasks/{id}/resume')
def resume(id:str,body:Action=Action(),db:Session=Depends(get_db)):
    t=get_task(id,db); return action(id,t.paused_from,db,body.reason or 'resumed')
@app.post('/api/tasks/{id}/cancel')
def cancel(id:str,body:Action=Action(),db:Session=Depends(get_db)): return action(id,'CANCEL_REQUESTED',db,body.reason or 'cancel requested')
@app.post('/api/tasks/{id}/retry')
def retry(id:str,body:Action=Action(),db:Session=Depends(get_db)): return action(id,'ANALYZING',db,body.reason or 'retry')
class DingTalkTest(BaseModel): message:str='PersonZit \u9489\u9489\u901a\u77e5\u6d4b\u8bd5'
@app.get('/api/dingtalk/status')
def dingtalk_status():
    from .dingtalk import notify_config,stream_runtime_state
    cfg=notify_config(); stream=stream_runtime_state()
    nl_cfg=cfg.get('natural_language',{}) or {}
    nl_provider=str(nl_cfg.get('provider','local'))
    nl_settings=provider_settings(nl_provider,nl_cfg)
    nl_model=str(nl_settings.get('model') or ('claude/codex' if nl_provider=='local' else ''))
    nl_key_env='' if nl_provider=='local' else str(nl_settings.get('api_key_env') or '')
    nl_configured=(nl_provider=='local') or bool(nl_model and environment_value(nl_key_env))
    return {'enabled':bool(cfg.get('enabled')),'webhook_configured':bool(cfg.get('webhook')),
            'inbound_configured':bool(cfg.get('client_id') and cfg.get('client_secret')),
            'stream_running':bool(stream.get('running')),'stream_responsive':bool(stream.get('responsive')),
            'stream_status':stream.get('status'),'stream_pid':stream.get('pid'),
            'stream_heartbeat_age_seconds':stream.get('heartbeat_age_seconds'),
            'log_file':stream.get('log_file'),
            'default_project':cfg.get('default_project'),
            'natural_language_enabled':bool(nl_cfg.get('enabled',True)),
            'natural_language_provider':nl_provider,
            'natural_language_model':nl_model,
            'natural_language_api_key_env':nl_key_env,
            'natural_language_configured':nl_configured}
@app.post('/api/dingtalk/test')
def dingtalk_test(body:DingTalkTest):
    from .dingtalk import send_notification,stream_runtime_state
    ok,detail=send_notification('PersonZit \u6d4b\u8bd5',f'### PersonZit \u6d4b\u8bd5\n\n{body.message}')
    result={'sent':ok,'detail':detail,'stream':stream_runtime_state()}
    logger.info('DingTalk test response: %s',json.dumps(result,ensure_ascii=False))
    return result

@app.get('/api/ai/providers')
def ai_providers():
    from .ai_providers import provider_status
    return provider_status()
@app.get('/api/agents')


def agents(): return [{'name':a.name,'available':a.detect().available,'executable':a.detect().executable,'reason':a.detect().reason} for a in registry().values()]
@app.post('/api/agents/detect')
def detect(): return agents()


if __name__ == '__main__':
    import uvicorn
    from .config import load_config
    server = load_config().get('server', {})
    uvicorn.run(app, host=server.get('host', '127.0.0.1'), port=int(server.get('port', 8765)))








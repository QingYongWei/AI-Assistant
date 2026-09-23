from enum import StrEnum
from datetime import datetime, timezone
import json
from .models import EventLog
class TaskStatus(StrEnum):
    NEW='NEW'; ANALYZING='ANALYZING'; WAITING_FOR_CLARIFICATION='WAITING_FOR_CLARIFICATION'; PLAN_PROPOSED='PLAN_PROPOSED'; WAITING_FOR_APPROVAL='WAITING_FOR_APPROVAL'; QUEUED='QUEUED'; EXECUTING='EXECUTING'; VERIFYING='VERIFYING'; REPAIRING='REPAIRING'; WAITING_FOR_HUMAN='WAITING_FOR_HUMAN'; PAUSED='PAUSED'; CANCEL_REQUESTED='CANCEL_REQUESTED'; CANCELLED='CANCELLED'; COMPLETED='COMPLETED'; FAILED='FAILED'
TRANSITIONS={'NEW':{'ANALYZING'},'ANALYZING':{'WAITING_FOR_CLARIFICATION','PLAN_PROPOSED','FAILED'},'WAITING_FOR_CLARIFICATION':{'ANALYZING','CANCELLED'},'PLAN_PROPOSED':{'WAITING_FOR_APPROVAL'},'WAITING_FOR_APPROVAL':{'QUEUED','ANALYZING','CANCELLED'},'QUEUED':{'EXECUTING'},'EXECUTING':{'VERIFYING','WAITING_FOR_HUMAN','FAILED'},'VERIFYING':{'COMPLETED','REPAIRING','WAITING_FOR_HUMAN','FAILED'},'COMPLETED':{'REPAIRING'},'WAITING_FOR_HUMAN':{'VERIFYING','EXECUTING','ANALYZING'},'REPAIRING':{'EXECUTING','VERIFYING','FAILED'},'CANCEL_REQUESTED':{'CANCELLED'}}
TERMINAL={'CANCELLED','COMPLETED','FAILED'}
def transition(db,task,target,actor='system',reason=''):
    target=TaskStatus(target).value; current=task.status
    allowed=target in TRANSITIONS.get(current,set()) or (target=='PAUSED' and current not in TERMINAL|{'PAUSED'}) or (current=='PAUSED' and target==task.paused_from) or (target=='CANCEL_REQUESTED' and current not in TERMINAL)
    if not allowed: raise ValueError(f'非法状态转换: {current} -> {target}')
    if target=='PAUSED': task.paused_from=current
    task.status=target; task.updated_at=datetime.now(timezone.utc)
    db.add(EventLog(task_id=task.id,event_type='TASK_STATUS_CHANGED',actor_type='system',actor_id=actor,payload_json=json.dumps({'from':current,'to':target,'reason':reason},ensure_ascii=False)))


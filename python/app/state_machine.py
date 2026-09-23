from enum import StrEnum
from datetime import datetime, timezone
import json
from .models import EventLog
class TaskStatus(StrEnum):
    NEW='NEW'; ANALYZING='ANALYZING'; WAITING_FOR_CLARIFICATION='WAITING_FOR_CLARIFICATION'; PLAN_PROPOSED='PLAN_PROPOSED'; WAITING_FOR_APPROVAL='WAITING_FOR_APPROVAL'; QUEUED='QUEUED'; EXECUTING='EXECUTING'; VERIFYING='VERIFYING'; REPAIRING='REPAIRING'; WAITING_FOR_HUMAN='WAITING_FOR_HUMAN'; PAUSED='PAUSED'; CANCEL_REQUESTED='CANCEL_REQUESTED'; CANCELLED='CANCELLED'; COMPLETED='COMPLETED'; FAILED='FAILED'
TRANSITIONS={'NEW':{'ANALYZING'},'ANALYZING':{'WAITING_FOR_CLARIFICATION','PLAN_PROPOSED','FAILED'},'WAITING_FOR_CLARIFICATION':{'ANALYZING','CANCELLED'},'PLAN_PROPOSED':{'WAITING_FOR_APPROVAL','QUEUED'},'WAITING_FOR_APPROVAL':{'QUEUED','ANALYZING','CANCELLED'},'QUEUED':{'EXECUTING'},'EXECUTING':{'VERIFYING','WAITING_FOR_HUMAN','FAILED'},'VERIFYING':{'COMPLETED','REPAIRING','WAITING_FOR_HUMAN','FAILED'},'COMPLETED':{'REPAIRING'},'WAITING_FOR_HUMAN':{'VERIFYING','EXECUTING','ANALYZING'},'REPAIRING':{'EXECUTING','VERIFYING','FAILED'},'CANCEL_REQUESTED':{'CANCELLED'}}
TERMINAL={'CANCELLED','COMPLETED','FAILED'}
STATUS_LABELS={'PENDING':'待处理','NEW':'新建','ANALYZING':'需求分析中','WAITING_FOR_CLARIFICATION':'等待澄清','PLAN_PROPOSED':'计划已生成','WAITING_FOR_APPROVAL':'等待审批','QUEUED':'已入队待执行','EXECUTING':'执行中','VERIFYING':'验收中','REPAIRING':'修复中','WAITING_FOR_HUMAN':'等待人工处理','PAUSED':'已暂停','CANCEL_REQUESTED':'取消中','CANCELLED':'已取消','COMPLETED':'已完成','FAILED':'失败'}
def status_label(status):
    """返回状态的中文名；未知状态原样返回，便于排查新状态。"""
    value=str(status or '')
    return STATUS_LABELS.get(value,value)
def transition(db,task,target,actor='system',reason=''):
    target=TaskStatus(target).value; current=task.status
    allowed=target in TRANSITIONS.get(current,set()) or (target=='PAUSED' and current not in TERMINAL|{'PAUSED'}) or (current=='PAUSED' and target==task.paused_from) or (target=='CANCEL_REQUESTED' and current not in TERMINAL)
    if not allowed: raise ValueError(f'非法状态转换: {current} -> {target}')
    if target=='PAUSED': task.paused_from=current
    task.status=target; task.updated_at=datetime.now(timezone.utc)
    db.add(EventLog(task_id=task.id,event_type='TASK_STATUS_CHANGED',actor_type='system',actor_id=actor,payload_json=json.dumps({'from':current,'to':target,'reason':reason},ensure_ascii=False)))


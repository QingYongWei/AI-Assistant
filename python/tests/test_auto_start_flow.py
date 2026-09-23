"""钉钉任务自动执行与澄清上下文绑定测试。"""
import json
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app.dingtalk as dingtalk
import app.worker as worker
from app.database import Base
from app.models import Task, Job, EventLog
from app.state_machine import transition


def make_session_factory():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


class FakePlanner:
    name = 'fake'
    def __init__(self, plan): self.plan_data = plan
    def plan(self, *args, **kwargs): return self.plan_data


CLEAR_PLAN = {
    'summary': 'clear requirement',
    'requires_clarification': False,
    'clarification_questions': [],
    'risk_level': 'medium',
    'acceptance_criteria': ['done'],
    'subtasks': [{'key': 'impl', 'title': 'Implement', 'description': 'do it',
                  'role': 'implementation', 'suggested_agent': 'codex',
                  'dependencies': [], 'acceptance_criteria': ['done']}],
    'verification_commands': [],
}


def test_dingtalk_clear_plan_auto_starts_without_approval(monkeypatch, tmp_path):
    db = make_session_factory()()
    task = Task(id=1, public_id='TASK-自动执行-000001', title='t', description='d',
                project_path='.', status='ANALYZING', source='dingtalk')
    db.add(task); db.commit()
    notifications = []
    monkeypatch.setattr(worker, 'make_planner', lambda: FakePlanner(CLEAR_PLAN))
    monkeypatch.setattr(worker, '_write_plan_document', lambda task, plan: tmp_path / 'plan.md')
    monkeypatch.setattr(worker, 'notify_task_event', lambda event, task, **kw: notifications.append(event))
    job = Job(id=1, public_id='JOB-1', job_type='PLAN_TASK', task_id=1, status='RUNNING')
    db.add(job); db.commit()

    worker.handle_plan(db, task, job)

    assert task.status == 'QUEUED'
    assert task.approved_at is not None
    execute_jobs = db.scalars(select(Job).where(Job.job_type == 'EXECUTE_SUBTASK')).all()
    assert len(execute_jobs) == 1
    assert db.scalar(select(EventLog).where(EventLog.event_type == 'AUTO_STARTED'))
    assert notifications == ['auto_started']


def test_dingtalk_unclear_plan_keeps_clarification(monkeypatch, tmp_path):
    db = make_session_factory()()
    task = Task(id=1, public_id='TASK-需澄清-000002', title='t', description='d',
                project_path='.', status='ANALYZING', source='dingtalk')
    db.add(task); db.commit()
    plan = dict(CLEAR_PLAN, requires_clarification=True,
                clarification_questions=['使用哪种存储？'])
    notifications = []
    monkeypatch.setattr(worker, 'make_planner', lambda: FakePlanner(plan))
    monkeypatch.setattr(worker, '_write_plan_document', lambda task, plan: tmp_path / 'plan.md')
    monkeypatch.setattr(worker, 'notify_task_event', lambda event, task, **kw: notifications.append(event))
    job = Job(id=1, public_id='JOB-1', job_type='PLAN_TASK', task_id=1, status='RUNNING')
    db.add(job); db.commit()

    worker.handle_plan(db, task, job)

    assert task.status == 'WAITING_FOR_CLARIFICATION'
    assert not db.scalars(select(Job).where(Job.job_type == 'EXECUTE_SUBTASK')).all()
    assert notifications == ['clarification']


def test_clarify_reply_is_merged_into_task_context(monkeypatch):
    factory = make_session_factory()
    db = factory()
    task = Task(id=1, public_id='TASK-上下文-000003', title='实现导出',
                description='实现导出功能', project_path='.', status='WAITING_FOR_CLARIFICATION',
                source='dingtalk')
    db.add(task); db.commit()
    monkeypatch.setattr(dingtalk, 'SessionLocal', factory)

    reply = dingtalk.dispatch_command(
        {'action': 'clarify', 'task_id': 'TASK-上下文-000003', 'answer': '导出格式是 CSV，编码 UTF-8'},
        message_id='msg-1')

    assert '已收到澄清答复' in reply
    db.expire_all(); task=db.get(Task,1)
    assert task.status == 'ANALYZING'
    assert '导出格式是 CSV，编码 UTF-8' in task.description
    assert db.scalar(select(EventLog).where(EventLog.event_type == 'TASK_CLARIFICATION_RECEIVED'))
    plan_jobs = db.scalars(select(Job).where(Job.job_type == 'PLAN_TASK')).all()
    assert plan_jobs and '导出格式是 CSV' in plan_jobs[-1].payload_json


def test_supplement_during_clarification_replans_instead_of_waiting(monkeypatch):
    factory = make_session_factory()
    db = factory()
    task = Task(id=1, public_id='TASK-补充澄清-000004', title='实现校验',
                description='实现校验功能', project_path='.', status='WAITING_FOR_CLARIFICATION',
                source='dingtalk')
    db.add(task); db.commit()
    monkeypatch.setattr(dingtalk, 'SessionLocal', factory)

    reply = dingtalk.dispatch_command(
        {'action': 'supplement', 'task_id': 'TASK-补充澄清-000004',
         'requirement': 'A.id 关联的是 B.id，不是 C.id'},
        message_id='msg-2')

    assert '正在重新规划' in reply
    assert '需要你按原流程回复' not in reply
    db.expire_all(); task=db.get(Task,1)
    assert task.status == 'ANALYZING'
    assert 'A.id 关联的是 B.id' in task.description
    plan_jobs = db.scalars(select(Job).where(Job.job_type == 'PLAN_TASK')).all()
    assert plan_jobs and 'A.id 关联的是 B.id' in plan_jobs[-1].payload_json


def test_clarification_binding_protects_against_misroute():
    active = {'public_id': 'TASK-保护-000005', 'status': 'WAITING_FOR_CLARIFICATION'}
    cmd = dingtalk._clarification_binding(active, ' 用户ID是数字，范围 1-999 ')
    assert cmd == {'action': 'clarify', 'task_id': 'TASK-保护-000005',
                   'answer': '用户ID是数字，范围 1-999', 'confidence': 0.95}
    assert dingtalk._clarification_binding({'status': 'EXECUTING'}, '普通补充') is None
    assert dingtalk._clarification_binding(active, '   ') is None


def test_plan_proposed_can_transition_directly_to_queued():
    class DB:
        def __init__(self): self.events = []
        def add(self, x): self.events.append(x)
    t = Task(status='PLAN_PROPOSED', public_id='TASK-1', title='x', description='x', project_path='.')
    db = DB()
    transition(db, t, 'QUEUED', reason='auto start')
    assert t.status == 'QUEUED'

def test_query_replies_use_chinese_status(monkeypatch):
    from app.models import SubTask

    factory = make_session_factory()
    db = factory()
    task = Task(id=1, public_id='TASK-中文状态-000006', title='实现导出',
                description='实现导出功能', project_path='.', status='WAITING_FOR_CLARIFICATION',
                source='dingtalk')
    sub = SubTask(id=1, task_id=1, public_id='TASK-中文状态-000006-ST-001',
                  title='实现功能', description='d', status='PENDING')
    db.add_all([task, sub]); db.commit()
    monkeypatch.setattr(dingtalk, 'SessionLocal', factory)

    reply = dingtalk.dispatch_command({'action': 'status', 'task_id': 'TASK-中文状态-000006'})
    assert '状态：等待澄清' in reply
    assert '[待处理]' in reply

    task.status = 'EXECUTING'; db.commit()
    reply = dingtalk.dispatch_command({'action': 'list'})
    assert '[执行中]' in reply

    reply = dingtalk.dispatch_command({'action': 'clarify', 'task_id': 'TASK-中文状态-000006', 'answer': 'x'})
    assert '当前状态为执行中，无需澄清' in reply
"""WAITING_FOR_HUMAN 任务出口与补充误绑定修复的测试（TASK-000005 事故回归）。"""
import json
from pathlib import Path
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

import app.dingtalk as dingtalk
import app.worker as worker
from app.database import Base
from app.models import Task, SubTask, Job, EventLog, VerificationRun
from app import intents


def make_session_factory():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _seed_waiting_human(factory, tmp_path, *, public_id='TASK-卡点-000005'):
    db = factory()
    task = Task(id=1, public_id=public_id, title='创建 ding.txt', description='创建 ding.txt',
                project_path='.', status='WAITING_FOR_HUMAN', source='dingtalk', retry_count=3,
                max_retries=2)
    db.add(task)
    db.add(EventLog(task_id=1, event_type='TASK_STATUS_CHANGED', actor_type='system', actor_id='worker',
                    payload_json=json.dumps({'from': 'VERIFYING', 'to': 'WAITING_FOR_HUMAN',
                                             'reason': '自动修复 2 次后仍未通过'})))
    stderr = tmp_path / 'verify.stderr.log'
    stderr.write_text('[WinError 2] 系统找不到指定的文件。', encoding='utf-8')
    db.add(VerificationRun(task_id=1, command_json=json.dumps('cat ding.txt'), status='FAILED',
                           exit_code=126, passed=False, summary='未执行', working_directory='.',
                           stderr_path=str(stderr)))
    db.commit()
    return task


def test_parse_retry_and_reverify_commands():
    assert dingtalk.parse_message('重试 TASK-000005')=={'action':'retry','task_id':'TASK-000005'}
    assert dingtalk.parse_message('重试')=={'action':'retry','task_id':'LATEST'}
    assert dingtalk.parse_message('retry task-000010')=={'action':'retry','task_id':'TASK-000010'}
    assert dingtalk.parse_message('重新验收 TASK-000005')=={'action':'reverify','task_id':'TASK-000005'}
    assert dingtalk.parse_message('再次验收 TASK-规则-000010')=={'action':'reverify','task_id':'TASK-规则-000010'}
    # 原有关键词指令不受影响
    assert dingtalk.parse_message('运行TASK-000005任务')['action']=='continue'
    assert dingtalk.parse_message('继续执行任务')=={'action':'continue','task_id':'LATEST'}


def test_heuristic_retry_rules():
    assert intents._heuristic('重试任务')=={'action':'retry','task_id':'LATEST','confidence':1.0}
    assert intents._heuristic('重新验收一下')=={'action':'reverify','task_id':'LATEST','confidence':1.0}


def test_sanitize_intent_normalizes_retry_task_id():
    result=intents.sanitize_intent({'action':'retry','task_id':'task-5','confidence':0.8})
    assert result=={'action':'retry','task_id':'TASK-000005','confidence':0.8}
    # 模型输出的 retry/reverify 必须带显式任务ID，与 approve/reject 同级安全约束
    result=intents.sanitize_intent({'action':'reverify','confidence':0.8})
    assert result['action']=='need_task_id' and result['requested_action']=='reverify'


def test_retry_replans_waiting_human_task(monkeypatch, tmp_path):
    factory = make_session_factory()
    task = _seed_waiting_human(factory, tmp_path)
    monkeypatch.setattr(dingtalk, 'SessionLocal', factory)

    reply = dingtalk.dispatch_command({'action': 'retry', 'task_id': task.public_id}, message_id='msg-r')

    assert '重新进入规划' in reply
    db = factory(); db.expire_all(); task = db.get(Task, 1)
    assert task.status == 'ANALYZING'
    assert task.retry_count == 0
    plan_jobs = db.scalars(select(Job).where(Job.job_type == 'PLAN_TASK')).all()
    assert plan_jobs and '创建 ding.txt' in plan_jobs[-1].payload_json


def test_reverify_only_reruns_verification(monkeypatch, tmp_path):
    factory = make_session_factory()
    task = _seed_waiting_human(factory, tmp_path)
    monkeypatch.setattr(dingtalk, 'SessionLocal', factory)

    reply = dingtalk.dispatch_command({'action': 'reverify', 'task_id': task.public_id}, message_id='msg-v')

    assert '重新进入验收' in reply
    db = factory(); db.expire_all(); task = db.get(Task, 1)
    assert task.status == 'VERIFYING'
    verify_jobs = db.scalars(select(Job).where(Job.job_type == 'VERIFY_SUBTASK')).all()
    assert verify_jobs and not db.scalars(select(Job).where(Job.job_type == 'EXECUTE_SUBTASK')).all()
    # 重新验收不重置修复次数
    assert task.retry_count == 3


def test_continue_and_status_expose_real_block_point(monkeypatch, tmp_path):
    factory = make_session_factory()
    task = _seed_waiting_human(factory, tmp_path)
    monkeypatch.setattr(dingtalk, 'SessionLocal', factory)

    reply = dingtalk.dispatch_command({'action': 'continue', 'task_id': task.public_id}, message_id='msg-c')
    assert '等待人工处理' in reply
    assert '卡点：自动修复 2 次后仍未通过' in reply
    assert 'cat ding.txt' in reply and 'WinError 2' in reply
    assert f'重试 {task.public_id}' in reply
    assert '查看具体卡点' not in reply  # 环形指引已移除

    db = factory()
    db.add(SubTask(id=1, task_id=1, public_id=f'{task.public_id}-ST-001', title='创建',
                   description='创建文件', status='PENDING'))
    db.commit()
    reply = dingtalk.dispatch_command({'action': 'status', 'task_id': task.public_id})
    assert '卡点：自动修复 2 次后仍未通过' in reply
    assert '重试' in reply


def test_supplement_to_waiting_human_includes_block_and_exit(monkeypatch, tmp_path):
    factory = make_session_factory()
    task = _seed_waiting_human(factory, tmp_path)
    monkeypatch.setattr(dingtalk, 'SessionLocal', factory)

    reply = dingtalk.dispatch_command({'action': 'supplement', 'task_id': task.public_id,
                                       'requirement': 'ding.txt 需要加 BOM 头'}, message_id='msg-s')
    assert '已记录' in reply
    assert '按原流程回复' not in reply
    assert f'重试 {task.public_id}' in reply


def test_active_task_context_marks_waiting_human_unbindable(monkeypatch):
    factory = make_session_factory()

    def ctx_with_status(status):
        db = factory()
        db.execute(text('DELETE FROM tasks'))
        db.add(Task(id=1, public_id='TASK-绑定-000001', title='t', description='d',
                    status=status, project_path='.'))
        db.commit()
        return dingtalk._active_task_context()

    monkeypatch.setattr(dingtalk, 'SessionLocal', factory)
    waiting = ctx_with_status('WAITING_FOR_HUMAN')
    assert waiting['supplement_bindable'] is False
    executing = ctx_with_status('EXECUTING')
    assert executing['supplement_bindable'] is True
    clarification = ctx_with_status('WAITING_FOR_CLARIFICATION')
    assert clarification['supplement_bindable'] is True


def test_question_messages_are_not_supplements():
    # TASK-000005 事故中的两条真实消息：提问/评估请求不是补充
    assert intents._looks_like_task_supplement('@助手小王 输出 personzit 人工审核步骤是什么样的') is False
    assert intents._looks_like_task_supplement(
        '当前项目还存在哪些性能上的问题？数据库使用polardb，融合事件表大约300万，只输出评估结果') is False
    # 陈述式纠错仍会被识别为补充
    assert intents._looks_like_task_supplement('user_id 关联的是 B.id，不是 C.id') is True


def test_ensure_workspace_dir_reuses_previous_workspace(monkeypatch, tmp_path):
    """workspace.tasks 变更后，重新验收/重试应复用旧 worktree 而不是退回空目录。"""
    factory = make_session_factory(); db = factory()
    prior = tmp_path / 'prior-ws'; prior.mkdir(); (prior / '.git').mkdir()
    (prior / 'ding.txt').write_text('DingTalk OK', encoding='utf-8')
    task = Task(id=2, public_id='TASK-复用-000006', title='t', description='d',
                project_path=str(tmp_path / 'proj'), status='VERIFYING', source='dingtalk')
    db.add(task)
    db.add(EventLog(task_id=2, event_type='WORKSPACE_PREPARED', actor_type='system', actor_id='worker',
                    payload_json=json.dumps({'workspace': str(prior), 'isolated': True})))
    db.commit()

    class BrokenRepo:  # 模拟“分支已被旧 worktree 检出，新建必失败”
        def __init__(self, *args, **kwargs): pass
        def is_repo(self): return False
    monkeypatch.setattr(worker, 'GitWorkspace', BrokenRepo)

    ws, isolated = worker.ensure_workspace_dir(db, task)
    assert ws == prior and isolated is True
    assert db.scalar(select(EventLog).where(EventLog.event_type == 'WORKSPACE_REUSED'))


def test_running_lists_waiting_tasks_and_splits_completed_sessions(monkeypatch, tmp_path):
    factory = make_session_factory()
    _seed_waiting_human(factory, tmp_path)
    monkeypatch.setattr(dingtalk, 'SessionLocal', factory)
    monkeypatch.setattr(dingtalk.process_registry, 'active_tasks', lambda: {})
    monkeypatch.setattr(dingtalk.external_agents, 'list_external_sessions', lambda: [
        {'external_id': 'EXT-CODEX-520603', 'status': 'COMPLETED', 'agent': 'codex',
         'title': '敏感字段必须加密存储', 'workspace': 'D:\\Workspace\\conflict-event-hub'},
        {'external_id': 'EXT-CLAUDE-520605', 'status': 'WAITING_INPUT_OR_BACKGROUND', 'agent': 'claude',
         'title': '三色预警核对', 'workspace': 'D:\\Workspace\\conflict-event-hub'},
    ])

    reply = dingtalk.dispatch_command({'action': 'running'})

    assert '等待人工处理的任务' in reply
    assert 'TASK-卡点-000005' in reply and '自动修复 2 次后仍未通过' in reply
    assert 'EXT-CLAUDE-520605' in reply.split('近期已完成')[0]
    assert '近期已完成的手动会话' in reply
    assert 'EXT-CODEX-520603' in reply.split('近期已完成')[1]

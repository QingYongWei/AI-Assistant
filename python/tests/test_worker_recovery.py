from datetime import datetime, timezone
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from app.database import Base
from app.models import Task, SubTask, AgentRun, Job, EventLog
import app.worker as worker


def make_db():
    engine=create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine,expire_on_commit=False)()

def test_recover_orphaned_job_marks_run_interrupted_and_requeues(monkeypatch):
    db=make_db()
    task=Task(id=1,public_id='TASK-000001',title='t',description='d',project_path='.',status='EXECUTING')
    sub=SubTask(id=1,task_id=1,public_id='TASK-000001-ST-001',title='s',description='d',status='RUNNING')
    run=AgentRun(id=1,task_id=1,subtask_id=1,agent_type='codex',command_json='[]',prompt='p',status='RUNNING')
    job=Job(id=1,public_id='JOB-1',job_type='EXECUTE_SUBTASK',task_id=1,status='RUNNING',
            locked_by='worker-LTX-999999',attempts=1)
    db.add_all([task,sub,run,job]); db.commit()
    monkeypatch.setattr(worker,'_worker_process_alive',lambda pid: False)
    recovered,skipped=worker.recover_orphaned_jobs(db,'worker-LTX-111111')
    assert recovered==['JOB-1'] and skipped==[]
    assert job.status=='QUEUED' and job.locked_by is None
    assert run.status=='INTERRUPTED' and run.finished_at is not None
    assert sub.status=='PENDING'
    assert db.scalars(select(EventLog).where(EventLog.event_type=='ORPHAN_JOB_RECOVERED')).one()

def test_recover_keeps_job_locked_by_live_worker(monkeypatch):
    db=make_db()
    task=Task(id=1,public_id='TASK-000001',title='t',description='d',project_path='.',status='EXECUTING')
    job=Job(id=1,public_id='JOB-1',job_type='EXECUTE_SUBTASK',task_id=1,status='RUNNING',
            locked_by='worker-OTHER-999999',attempts=1)
    db.add_all([task,job]); db.commit()
    monkeypatch.setattr(worker,'_worker_process_alive',lambda pid: True)
    recovered,skipped=worker.recover_orphaned_jobs(db,'worker-LTX-111111')
    assert recovered==[] and len(skipped)==1
    assert job.status=='RUNNING' and job.locked_by=='worker-OTHER-999999'

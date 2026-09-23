import json
from pathlib import Path
from app.models import Task
import app.worker as worker

class FakeDB:
    def __init__(self): self.objects=[]
    def add(self,obj): self.objects.append(obj)
    def flush(self): pass
    def commit(self): pass

class FakeJob:
    payload_json='{}'

def test_high_risk_verification_creates_pending_approval(monkeypatch,tmp_path):
    monkeypatch.setattr(worker,'ensure_workspace_dir',lambda db,task:(tmp_path,True))
    monkeypatch.setattr(worker,'notify_task_event',lambda *args,**kwargs:None)
    task=Task(id=1,public_id='TASK-T',status='VERIFYING',title='t',description='d',project_path=str(tmp_path),plan_json=json.dumps({'verification_commands':['git push origin main']}))
    db=FakeDB()
    worker.handle_verify(db,task,FakeJob())
    assert task.status=='WAITING_FOR_HUMAN'
    approvals=[x for x in db.objects if x.__tablename__=='approvals']
    assert len(approvals)==1 and approvals[0].approval_type=='VERIFICATION'
    assert approvals[0].risk_level=='high'
    assert any(x.__tablename__=='events' and x.event_type=='HIGH_RISK_PENDING' for x in db.objects)

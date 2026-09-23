from datetime import datetime, timezone, timedelta
from sqlalchemy import select
from .models import Job

def now(): return datetime.now(timezone.utc)

class SqliteQueue:
    def __init__(self,db): self.db=db
    def enqueue(self,job_type,task_id,subtask_id=None,payload='{}',priority=0):
        j=Job(public_id=f'JOB-{int(datetime.now().timestamp()*1000000)}',job_type=job_type,task_id=task_id,
              subtask_id=subtask_id,payload_json=payload,priority=priority)
        self.db.add(j); self.db.commit(); return j
    def claim_next(self,worker_id):
        j=self.db.scalars(select(Job).where(Job.status=='QUEUED',Job.available_at<=now())
                          .order_by(Job.priority.desc(),Job.id).with_for_update()).first()
        if not j: return None
        j.status='RUNNING'; j.locked_by=worker_id; j.locked_at=now(); j.heartbeat_at=now(); j.attempts+=1
        self.db.commit(); return j
    def complete(self,j): j.status='SUCCEEDED'; j.updated_at=now(); self.db.commit()
    def fail(self,j,error):
        j.last_error=str(error); j.status='RETRY_WAIT' if j.attempts<j.max_attempts else 'FAILED'
        j.available_at=now()+timedelta(seconds=min(60,2**j.attempts)); self.db.commit()
    def recover_stale(self,seconds=300,worker_id=None):
        """回收僵死 Job；跳过当前 Worker 自己长时间运行中的任务（Agent 执行可能远超阈值）。"""
        cutoff=now()-timedelta(seconds=seconds)
        for j in self.db.scalars(select(Job).where(Job.status=='RUNNING',Job.heartbeat_at<cutoff)).all():
            if worker_id and j.locked_by==worker_id: continue
            self.fail(j,'stale worker heartbeat')

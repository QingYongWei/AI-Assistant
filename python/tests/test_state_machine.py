from app.state_machine import transition
from app.models import Task
def test_transition_records_event():
    class DB:
        def add(self,x): self.event=x
    t=Task(id=1,status='NEW',public_id='TASK-1',title='x',description='x',project_path='.')
    db=DB(); transition(db,t,'ANALYZING',reason='test'); assert t.status=='ANALYZING'; assert db.event.event_type=='TASK_STATUS_CHANGED'

def test_waiting_for_human_can_resume_to_verifying():
    class DB:
        def add(self,x): self.event=x
    t=Task(status='WAITING_FOR_HUMAN',public_id='TASK-1',title='x',description='x',project_path='.')
    db=DB(); transition(db,t,'VERIFYING',reason='approved')
    assert t.status=='VERIFYING'


def test_status_label_returns_chinese():
    from app.state_machine import status_label
    assert status_label('WAITING_FOR_CLARIFICATION')=='等待澄清'
    assert status_label('EXECUTING')=='执行中'
    assert status_label('SOMETHING_NEW')=='SOMETHING_NEW'

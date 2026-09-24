from pathlib import Path
from app.models import Task
import app.worker as worker

class FakeDB:
    def __init__(self): self.objects=[]
    def add(self,value): self.objects.append(value)
    def scalar(self,query): return None  # 无历史 WORKSPACE_PREPARED 记录
    def scalars(self,query):
        class _Empty:
            def all(self): return []
        return _Empty()

class FakeRepo:
    def __init__(self, root, workspaces=None, branch='feature/test', created=None):
        self.root=Path(root).resolve(); self.branch=branch; self.created=created or (Path(root)/'TASK-WORKTREE')
    def is_repo(self): return True
    def current_branch(self): return self.branch
    def create(self, task_id, base='HEAD'): return self.created

def make_task(tmp_path):
    return Task(id=1,public_id='TASK-000001',title='t',description='d',project_path=str(tmp_path))

def test_current_branch_project_is_used_directly(monkeypatch,tmp_path):
    repo=FakeRepo(tmp_path,branch='feature/1.0.0.1')
    monkeypatch.setattr(worker,'GitWorkspace',lambda root,workspaces:repo)
    task=make_task(tmp_path); path,isolated=worker.ensure_workspace_dir(FakeDB(),task)
    assert path==tmp_path.resolve() and isolated is False
    assert task.execution_branch=='feature/1.0.0.1'

def test_master_branch_still_uses_worktree(monkeypatch,tmp_path):
    created=tmp_path/'TASK-000001'
    repo=FakeRepo(tmp_path,branch='master',created=created)
    monkeypatch.setattr(worker,'GitWorkspace',lambda root,workspaces:repo)
    task=make_task(tmp_path); path,isolated=worker.ensure_workspace_dir(FakeDB(),task)
    assert path==created and isolated is True
    assert task.execution_branch=='personzit/task-000001'

def test_always_worktree_policy_can_be_selected(monkeypatch,tmp_path):
    created=tmp_path/'TASK-000001'
    repo=FakeRepo(tmp_path,branch='feature/test',created=created)
    monkeypatch.setattr(worker,'GitWorkspace',lambda root,workspaces:repo)
    monkeypatch.setattr(worker.config,'load_config',lambda:{'workspace':{'isolation_policy':'always-worktree'}})
    task=make_task(tmp_path); path,isolated=worker.ensure_workspace_dir(FakeDB(),task)
    assert path==created and isolated is True


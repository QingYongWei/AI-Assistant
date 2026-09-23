from pathlib import Path
import os, subprocess
from .security import ensure_workspace,PolicyViolation
from .config import load_config,workspace_root,workspace_tasks,workspace_documents

class GitWorkspace:
    def __init__(self, root:str|Path, workspaces:str|Path):
        self.root=Path(root).resolve(); self.workspaces=Path(workspaces).resolve()
    def _git(self,*args,check=True):
        return subprocess.run(['git',*args],cwd=self.root,text=True,capture_output=True,check=check,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    def is_repo(self): return (self.root/'.git').exists()
    def _branch_exists(self,branch):
        return self._git('rev-parse','--verify',f'refs/heads/{branch}',check=False).returncode==0
    def current_branch(self):
        result=self._git('branch','--show-current',check=False)
        if result.returncode!=0:
            return ''
        return result.stdout.strip()
    def create(self, task_id:str, base='HEAD') -> Path:
        """为任务创建（或复用）隔离的 Git Worktree，分支名 personzit/<task_id>。"""
        if not self.is_repo(): raise PolicyViolation('项目不是 Git 仓库')
        target=ensure_workspace(self.workspaces/task_id,self.workspaces)
        if (target/'.git').exists(): return target  # 幂等：修复/重试时复用
        target.parent.mkdir(parents=True,exist_ok=True)
        branch=f'personzit/{task_id.lower()}'
        if self._branch_exists(branch):
            r=self._git('worktree','add',str(target),branch)
        else:
            r=self._git('worktree','add','-b',branch,str(target),base)
        if r.returncode!=0: raise RuntimeError(f'创建 worktree 失败: {r.stderr.strip()}')
        return target
    def diff(self, path=None):
        return subprocess.run(['git','diff','--stat'],cwd=path or self.root,text=True,capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0).stdout
    def status(self, path=None):
        return subprocess.run(['git','status','--short'],cwd=path or self.root,text=True,capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0).stdout



def ensure_default_workspace() -> Path:
    """Create the default authorized root and document/task directories."""
    root=workspace_root(); root.mkdir(parents=True,exist_ok=True)
    workspace_tasks().mkdir(parents=True,exist_ok=True)
    workspace_documents().mkdir(parents=True,exist_ok=True)
    cfg=load_config().get('workspace',{}) or {}
    if cfg.get('initialize_git',True) and not (root/'.git').exists():
        result=subprocess.run(['git','init'],cwd=root,capture_output=True,text=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        if result.returncode != 0:
            raise RuntimeError(f'初始化授权工作区失败: {result.stderr.strip()}')
    return root

"""安全策略：命令、路径和高风险操作的统一检查。"""
from pathlib import Path
import shlex

HIGH_RISK = ('git push', 'git merge', 'git reset --hard', 'drop database', 'rm -rf', 'format ')
class PolicyViolation(ValueError): pass
def ensure_workspace(path: str|Path, workspace: str|Path) -> Path:
    target=Path(path).resolve(); root=Path(workspace).resolve()
    if target != root and root not in target.parents: raise PolicyViolation(f'路径超出工作区: {target}')
    return target
def is_high_risk(command: str) -> bool:
    return any(x in command.lower() for x in HIGH_RISK)

def check_command(command: str, workspace: str|Path, allow_high_risk=False) -> list[str]:
    if not command.strip(): raise PolicyViolation('命令不能为空')
    if not allow_high_risk and any(x in command.lower() for x in HIGH_RISK): raise PolicyViolation('高风险命令需要人工审批')
    return shlex.split(command, posix=False)


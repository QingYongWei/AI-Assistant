from dataclasses import dataclass
from pathlib import Path
import os
import subprocess, time, shutil
from .security import check_command, ensure_workspace, PolicyViolation
@dataclass
class VerificationResult:
    command:str; passed:bool; exit_code:int; stdout:str; stderr:str; duration_ms:int; summary:str

# Windows 上不存在、需要 Git Bash 提供的 unix 工具；与 cmd 内建命令不重叠。
_UNIX_TOOLS={'cat','ls','grep','sed','awk','sh','bash','touch','head','tail','wc','diff',
             'cp','mv','rm','pwd','test','basename','dirname','sort','uniq','tr','cut',
             'xargs','chmod','tar','gzip','true','false'}
def _find_bash():
    exe=shutil.which('bash')
    if exe: return exe
    git=shutil.which('git')
    if git:
        for candidate in (Path(git).parent.parent/'bin'/'bash.exe',Path(git).parent.parent/'usr'/'bin'/'bash.exe'):
            if candidate.exists(): return str(candidate)
    return None
def _looks_posix(command:str) -> bool:
    """POSIX sh 语法（cmd 无法执行）：[ 开头的 test、命令替换、环境变量展开。"""
    value=command.strip()
    return value.startswith('[') or '$(' in value or '${' in value
def run_verification(command:str, working_directory:str, timeout_seconds=300, allow_high_risk=False) -> VerificationResult:
    root=ensure_workspace(working_directory,working_directory); argv=check_command(command,root,allow_high_risk)
    if os.name=="nt":  # Windows 兼容：POSIX 语法经 Git Bash 执行，cmd 元字符/内建命令经 cmd /c 执行
        meta=("|",">","<","&&","||")
        builtins={"type","dir","echo","copy","del","move","md","rd","where","find","findstr","more"}
        argv0=str(argv[0]).lower() if argv else ''
        bash=_find_bash()
        if bash and _looks_posix(command):
            argv=[bash,'-c',command]
        elif any(m in command for m in meta) or argv0 in builtins:
            argv=["cmd","/c",command]
        else:
            exe=shutil.which(argv[0])
            if exe: argv=[exe,*argv[1:]]
            elif bash and argv0 in _UNIX_TOOLS:
                argv=[bash,'-c',command]  # cat/ls 等 unix 工具不在 PATH 时回退到 Git Bash
    start=time.monotonic()
    try:
        p=subprocess.run(argv,cwd=root,capture_output=True,text=True,timeout=timeout_seconds,shell=False,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        passed=p.returncode==0; return VerificationResult(command,passed,p.returncode,p.stdout,p.stderr,int((time.monotonic()-start)*1000),'通过' if passed else '命令失败')
    except subprocess.TimeoutExpired as e: return VerificationResult(command,False,124,e.stdout or '',e.stderr or '',int((time.monotonic()-start)*1000),'超时')
    except (OSError,PolicyViolation) as e: return VerificationResult(command,False,126,'',str(e),int((time.monotonic()-start)*1000),'未执行')


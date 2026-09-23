from dataclasses import dataclass
from pathlib import Path
import os
import subprocess, time, shutil
from .security import check_command, ensure_workspace, PolicyViolation
@dataclass
class VerificationResult:
    command:str; passed:bool; exit_code:int; stdout:str; stderr:str; duration_ms:int; summary:str
def run_verification(command:str, working_directory:str, timeout_seconds=300, allow_high_risk=False) -> VerificationResult:
    root=ensure_workspace(working_directory,working_directory); argv=check_command(command,root,allow_high_risk)
    if os.name=="nt":  # Windows 兼容：shell 元字符/cmd 内建命令需经 cmd /c 执行
        meta=("|",">","<","&&","||")
        builtins={"type","dir","echo","copy","del","move","md","rd","where","find","findstr","more"}
        if any(m in command for m in meta) or argv[0].lower() in builtins:
            argv=["cmd","/c",command]
        else:
            exe=shutil.which(argv[0])
            if exe: argv=[exe,*argv[1:]]
    start=time.monotonic()
    try:
        p=subprocess.run(argv,cwd=root,capture_output=True,text=True,timeout=timeout_seconds,shell=False,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        passed=p.returncode==0; return VerificationResult(command,passed,p.returncode,p.stdout,p.stderr,int((time.monotonic()-start)*1000),'通过' if passed else '命令失败')
    except subprocess.TimeoutExpired as e: return VerificationResult(command,False,124,e.stdout or '',e.stderr or '',int((time.monotonic()-start)*1000),'超时')
    except (OSError,PolicyViolation) as e: return VerificationResult(command,False,126,'',str(e),int((time.monotonic()-start)*1000),'未执行')



from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
import os
import subprocess
import threading

@dataclass
class ProcessRecord:
    task_id: str
    process: subprocess.Popen
    started_at: datetime

_lock=threading.RLock()
_processes: dict[int,ProcessRecord]={}

def _no_window():
    return subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0

def register(task_id: str,process: subprocess.Popen) -> None:
    with _lock:
        _processes[process.pid]=ProcessRecord(str(task_id),process,datetime.now(timezone.utc))

def unregister(process: subprocess.Popen) -> None:
    with _lock:
        _processes.pop(process.pid,None)

def active_tasks() -> dict[str,list[dict]]:
    with _lock:
        result: dict[str,list[dict]]={}
        for record in _processes.values():
            result.setdefault(record.task_id,[]).append({
                'pid':record.process.pid,
                'started_at':record.started_at.isoformat(),
                'returncode':record.process.returncode,
            })
        return result

def request_cancel(task_id: str) -> list[int]:
    """Terminate live local-agent processes belonging to a PersonZit task."""
    task_id=str(task_id)
    terminated: list[int]=[]
    targets: list[ProcessRecord]=[]
    with _lock:
        targets=[record for record in _processes.values() if record.task_id==task_id]
    for record in targets:
        process=record.process
        if process.poll() is not None:
            continue
        try:
            if os.name == 'nt':
                subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],capture_output=True,text=True,timeout=10,creationflags=_no_window())
            else:
                process.terminate()
            terminated.append(process.pid)
        except (OSError,subprocess.SubprocessError):
            try:
                process.kill()
                terminated.append(process.pid)
            except OSError:
                pass
    return terminated

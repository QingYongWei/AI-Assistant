from dataclasses import dataclass, field
from pathlib import Path
from datetime import datetime, timezone
import shutil, subprocess, os, json
from . import config
from . import process_registry
from .config import workspace_root

@dataclass
class AgentDetectionResult: name:str; available:bool; executable:str|None=None; version:str|None=None; reason:str|None=None
@dataclass
class AgentRequest:
    task_id:str; subtask_id:str; prompt:str; workspace:Path; timeout_seconds:int=1800
    environment:dict[str,str]|None=None; output_schema:dict|None=None
@dataclass
class AgentResult:
    success:bool; exit_code:int|None; summary:str; stdout_path:Path; stderr_path:Path
    structured_output:dict|None; changed_files:list[str]=field(default_factory=list)
    started_at:datetime=None; finished_at:datetime=None

class AgentUnavailableError(RuntimeError): pass

class AgentAdapter:
    name='base'
    def detect(self): raise NotImplementedError
    def run(self,request): raise NotImplementedError

class MockAgent(AgentAdapter):
    name='mock'
    def detect(self): return AgentDetectionResult(self.name,True,'built-in','0.1.0')
    def run(self,request):
        started=datetime.now(timezone.utc); request.workspace.mkdir(parents=True,exist_ok=True)
        out=request.workspace/'mock-agent.stdout.log'; err=request.workspace/'mock-agent.stderr.log'
        out.write_text(f'MockAgent completed: {request.prompt}\n',encoding='utf8'); err.write_text('',encoding='utf8')
        return AgentResult(True,0,'Mock agent completed',out,err,None,[],started,datetime.now(timezone.utc))

class CliAgent(AgentAdapter):
    """调用本机 AI CLI（codex/claude/zcode/deepseek）的真实执行适配器。"""
    def __init__(self,name): self.name=name
    def _cfg(self): return config.load_config().get('agents',{}).get(self.name,{})
    def detect(self):
        cfg=self._cfg(); exe=cfg.get('executable',self.name)
        available=bool(cfg.get('enabled',False)) and bool(shutil.which(exe))
        return AgentDetectionResult(self.name,available,exe,reason=None if available else '未启用或未安装')
    def _argv(self,request):
        cfg=self._cfg(); exe=cfg.get('executable',self.name); resolved=shutil.which(exe)
        if not resolved: raise AgentUnavailableError(f'未找到可执行文件: {exe}')
        defaults={'codex':['exec','{prompt}'],'claude':['-p','{prompt}']}.get(self.name,['{prompt}'])
        template=cfg.get('arguments',defaults)
        argv=[resolved]+[str(a).replace('{prompt}',request.prompt) for a in template]
        workspace_cfg=config.load_config().get('workspace',{}) or {}
        if workspace_cfg.get('authorize_agents',True):
            root=str(workspace_root())
            prompt_indexes=[index for index,value in enumerate(argv) if value==request.prompt]
            insert_at=prompt_indexes[0] if prompt_indexes else len(argv)
            if self.name=='codex':
                additions=['--skip-git-repo-check','--add-dir',root]
                if not any(value in ('workspace-write','read-only','danger-full-access') for value in argv):
                    additions=['--sandbox','workspace-write',*additions]
            elif self.name=='claude':
                additions=['--add-dir',root]
            else:
                additions=[]
            existing=set(argv)
            additions=[value for index,value in enumerate(additions) if index==0 or value not in existing or value==root]
            argv[insert_at:insert_at]=additions
        return argv
    @staticmethod
    def _changed_files(workspace:Path):
        if not (workspace/'.git').exists(): return []
        try:
            p=subprocess.run(['git','-C',str(workspace),'status','--porcelain'],capture_output=True,text=True,timeout=30,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
            return [line[3:].strip('"') for line in p.stdout.splitlines() if line.strip()]
        except Exception: return []
    def run(self,request):
        cfg=self._cfg()
        if not cfg.get('enabled',False): raise AgentUnavailableError(f'{self.name} 未在配置中启用')
        argv=self._argv(request)
        timeout=int(cfg.get('timeout_seconds',request.timeout_seconds or 1800))
        artifacts=config.home()/'artifacts'/request.task_id; artifacts.mkdir(parents=True,exist_ok=True)
        stamp=datetime.now().strftime('%Y%m%d-%H%M%S')
        out=artifacts/f'agent-{self.name}-{request.subtask_id}-{stamp}.stdout.log'
        err=artifacts/f'agent-{self.name}-{request.subtask_id}-{stamp}.stderr.log'
        env={**os.environ,**(request.environment or {})}
        request.workspace.mkdir(parents=True,exist_ok=True)
        started=datetime.now(timezone.utc); success=False; exit_code=None; summary=''
        process=None
        try:
            process=subprocess.Popen(argv,cwd=request.workspace,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=env,encoding='utf8',errors='replace',creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
            process_registry.register(request.task_id,process)
            stdout,stderr=process.communicate(timeout=timeout)
            exit_code=process.returncode; success=process.returncode==0
            out.write_text(stdout or '',encoding='utf8',errors='replace')
            err.write_text(stderr or '',encoding='utf8',errors='replace')
            tail=[l for l in (stdout or '').strip().splitlines() if l.strip()]
            summary=tail[-1][:500] if tail else (f'exit {process.returncode}' if not success else 'completed')
        except subprocess.TimeoutExpired as e:
            if process is not None:
                try: process.kill()
                except OSError: pass
                stdout,stderr=process.communicate()
            else:
                stdout,stderr=e.stdout,e.stderr
            out.write_text(stdout or '',encoding='utf8',errors='replace')
            err.write_text((stderr or '')+f'\n[PersonZit] 执行超时（{timeout}s）',encoding='utf8',errors='replace')
            summary=f'执行超时（{timeout}s）'
        except OSError as e:
            err.write_text(str(e),encoding='utf8',errors='replace'); summary=f'启动失败: {e}'
        finally:
            if process is not None:
                process_registry.unregister(process)
        changed=self._changed_files(request.workspace)
        return AgentResult(success,exit_code,summary,out,err,None,changed,started,datetime.now(timezone.utc))

def registry(): return {x.name:x for x in [MockAgent(),CliAgent('codex'),CliAgent('claude'),CliAgent('zcode'),CliAgent('deepseek')]}

# 角色路由表：首选 → 备用（对齐设计文档 13.3）
ROLE_ROUTE={'requirement':['claude','codex'],'design':['claude','codex'],'implementation':['codex','claude'],
            'test':['codex','claude'],'review':['claude','codex'],'ui-test':['zcode','codex']}

def resolve_agent(role='implementation',suggested=None):
    """按 子任务建议 → 角色路由 → 配置默认 顺序选择可用 Agent；显式 mock 模式才回退 mock。"""
    agents=registry(); default=config.load_config().get('agents',{}).get('default','mock')
    candidates=[]
    if suggested and suggested!='mock': candidates.append(suggested)
    candidates+=ROLE_ROUTE.get(role,['codex','claude'])
    if default!='mock': candidates.append(default)
    for name in candidates:
        agent=agents.get(name)
        if agent and agent.detect().available: return agent
    if default=='mock': return agents['mock']
    return None

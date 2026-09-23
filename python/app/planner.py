import json
from pathlib import Path
from datetime import datetime
from .config import load_config
from .ai_providers import complete_json,provider_settings
from . import config as config_mod


def detect_verification_commands(project_path):
    """Recommend commands that can run at a project root on Windows."""
    if not project_path: return []
    p=Path(project_path); cmds=[]
    pkg=p/'package.json'
    if pkg.exists():
        try: scripts=json.loads(pkg.read_text(encoding='utf8')).get('scripts',{})
        except Exception: scripts={}
        if 'test' in scripts: cmds.append('npm test')
        if 'build' in scripts: cmds.append('npm run build')
    if (p/'pyproject.toml').exists() or (p/'pytest.ini').exists() or (p/'tests').is_dir(): cmds.append('python -m pytest')
    return cmds


SUGGESTED={'requirement':'claude','design':'claude','implementation':'codex','test':'codex','review':'claude','ui-test':'zcode'}
PLAN_SCHEMA='''{
  "summary": "one sentence objective",
  "requires_clarification": false,
  "clarification_questions": [],
  "risk_level": "low|medium|high",
  "requires_approval": true,
  "acceptance_criteria": ["task-level criteria"],
  "subtasks": [
    {"key":"unique_english_key","title":"subtask title","description":"what to do",
     "role":"design|implementation|test|review","suggested_agent":"codex|claude",
     "dependencies":["other key"],"acceptance_criteria":["subtask criteria"]}
  ],
  "verification_commands": ["Windows PowerShell/cmd commands"]
}'''


class MockPlanner:
    name='mock'
    def plan(self,title,description,project_path=None,verification_commands=None,task_id=None,clarification=None):
        cmds=list(verification_commands) if verification_commands else detect_verification_commands(project_path)
        return {'summary':title,'requires_clarification':False,'clarification_questions':[],
            'risk_level':'medium','requires_approval':True,
            'acceptance_criteria':['Task requirement implemented','Basic verification passed'],
            'subtasks':[{'key':'implementation','title':f'Implement: {title}','description':description,
                'role':'implementation','suggested_agent':SUGGESTED['implementation'],'dependencies':[],
                'acceptance_criteria':['Implement the requirement']}],
            'verification_commands':cmds}


class AiPlanner:
    """Plan work with local Claude/Codex or an OpenAI-compatible API provider."""
    name='ai'

    @staticmethod
    def _parse_json(text):
        """Extract JSON while tolerating fences and explanatory text."""
        t=(text or '').strip()
        if t.startswith('```'):
            t=t.split('\n',1)[1] if '\n' in t else t
            if t.rstrip().endswith('```'): t=t.rstrip()[:-3]
        start=t.find('{'); end=t.rfind('}')
        if start<0 or end<=start: raise ValueError('No JSON object in model output')
        return json.loads(t[start:end+1])

    def _sanitize(self,plan,title,description,verification_commands,detected):
        def slist(v): return [str(x) for x in v] if isinstance(v,list) else []
        subtasks=[]
        raw_subtasks=plan.get('subtasks',[]) if isinstance(plan.get('subtasks'),list) else []
        for s in raw_subtasks:
            if not isinstance(s,dict) or not s.get('title'): continue
            role=s.get('role','implementation')
            subtasks.append({'key':str(s.get('key') or f'st{len(subtasks)+1}'),'title':str(s['title']),
                'description':str(s.get('description','')),'role':role if role in SUGGESTED else 'implementation',
                'suggested_agent':s.get('suggested_agent') if s.get('suggested_agent') in ('codex','claude') else SUGGESTED.get(role,'codex'),
                'dependencies':slist(s.get('dependencies')),'acceptance_criteria':slist(s.get('acceptance_criteria')) or ['Complete this subtask']})
        if not subtasks: subtasks=MockPlanner().plan(title,'')['subtasks']
        cmds=list(verification_commands) if verification_commands else (slist(plan.get('verification_commands')) or detected)
        return {'summary':str(plan.get('summary') or title),'requires_clarification':bool(plan.get('requires_clarification')),
            'clarification_questions':slist(plan.get('clarification_questions')),
            'risk_level':plan.get('risk_level') if plan.get('risk_level') in ('low','medium','high') else 'medium',
            'requires_approval':bool(plan.get('requires_approval',True)),'acceptance_criteria':slist(plan.get('acceptance_criteria')) or ['Task requirement implemented'],
            'subtasks':subtasks,'verification_commands':cmds}

    def plan(self,title,description,project_path=None,verification_commands=None,task_id=None,clarification=None):
        cfg=load_config().get('planner',{}); timeout=int(cfg.get('timeout_seconds',300))
        detected=detect_verification_commands(project_path)
        lines=['You are a software engineering planner. Return exactly one JSON object and no prose.',
            'Schema:',PLAN_SCHEMA,'','Rules:',
            '- Create 1-5 ordered subtasks; merge simple requirements.',
            '- dependencies may only reference keys already defined in subtasks.',
            '- suggested_agent: use codex for implementation/tests; claude for design/review.',
            '- verification_commands must run directly in Windows PowerShell/cmd; never use cat/grep/sed or Bash substitutions.',
            '- If information is insufficient, set requires_clarification=true and ask concrete questions.',
            f'- Available project commands: {", ".join(detected) if detected else "none"}','',
            f'Title: {title}',f'Description: {description}','']
        if project_path: lines.append(f'Project path: {project_path}')
        if verification_commands: lines.append(f'User verification commands: {", ".join(verification_commands)}')
        if clarification: lines.append('Clarification answers: '+'; '.join(f'Q{i+1}: {a}' for i,a in enumerate(clarification)))
        prompt='\n'.join(lines)
        provider_name=str(cfg.get('provider') or cfg.get('agent') or 'local').lower()
        settings=provider_settings(provider_name,cfg)
        raw,metadata=complete_json(prompt,settings['provider'],settings,timeout)
        if task_id:
            artifacts=config_mod.home()/'artifacts'/task_id; artifacts.mkdir(parents=True,exist_ok=True)
            stamp=datetime.now().strftime('%Y%m%d-%H%M%S')
            (artifacts/f'planner-{stamp}.json').write_text(json.dumps({'metadata':metadata,'raw_plan':raw},
                ensure_ascii=False,indent=2),encoding='utf8',errors='replace')
        return self._sanitize(raw,title,description,verification_commands,detected)


def make_planner():
    return AiPlanner() if load_config().get('planner',{}).get('type')=='ai' else MockPlanner()
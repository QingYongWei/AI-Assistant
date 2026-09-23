import json
from pathlib import Path
from app.planner import MockPlanner, AiPlanner, detect_verification_commands

def test_detect_node_verification_commands(tmp_path):
    (tmp_path/'package.json').write_text(json.dumps({'scripts':{'test':'node --test','build':'x'}}),encoding='utf8')
    assert detect_verification_commands(tmp_path)==['npm test','npm run build']

def test_user_override_verification_commands():
    plan=MockPlanner().plan('t','d',verification_commands=['echo ok'])
    assert plan['verification_commands']==['echo ok']

def test_mock_plan_default_shape():
    plan=MockPlanner().plan('t','d')
    assert plan['subtasks'] and plan['requires_clarification'] is False

def test_parse_json_from_markdown_fence():
    text='前置说明\n```json\n{"summary":"s","subtasks":[]}\n```\n后置说明'
    assert AiPlanner._parse_json(text)=={'summary':'s','subtasks':[]}

def test_parse_json_plain():
    assert AiPlanner._parse_json('{"a":1}')=={'a':1}

def test_sanitize_fills_defaults_and_fixes_fields():
    raw={'summary':'s','subtasks':[{'title':'t1','role':'hack','suggested_agent':'gpu','dependencies':['x']}],
         'risk_level':'extreme','verification_commands':'bad'}
    plan=AiPlanner()._sanitize(raw,'title','desc',[],['npm test'])
    assert plan['subtasks'][0]['role']=='implementation'
    assert plan['subtasks'][0]['suggested_agent']=='codex'
    assert plan['risk_level']=='medium'
    assert plan['verification_commands']==['npm test']

def test_sanitize_empty_subtasks_falls_back():
    plan=AiPlanner()._sanitize({}, 't', 'd', [], [])
    assert plan['subtasks'] and plan['summary']=='t'

def test_user_override_beats_plan_commands():
    raw={'summary':'s','subtasks':[{'title':'t'}],'verification_commands':['echo from-ai']}
    plan=AiPlanner()._sanitize(raw,'t','d',['echo user'],['echo detected'])
    assert plan['verification_commands']==['echo user']

import os
from app import ai_providers
from app.planner import AiPlanner

def test_provider_settings_merge_global_model_and_scope(monkeypatch):
    monkeypatch.setattr(ai_providers,'load_config',lambda:{'ai_providers':{'zhipu':{'model':'glm-test','api_key_env':'ZHIPU_API_KEY'}}})
    settings=ai_providers.provider_settings('zhipu',{'timeout_seconds':7})
    assert settings['model']=='glm-test'
    assert settings['api_key_env']=='ZHIPU_API_KEY'
    assert settings['timeout_seconds']==7

def test_provider_status_reports_missing_model_or_key(monkeypatch):
    monkeypatch.setattr(ai_providers,'load_config',lambda:{'ai_providers':{'zhipu':{'model':'glm-test'}},'planner':{}})
    monkeypatch.delenv('ZHIPU_API_KEY',raising=False)
    monkeypatch.setattr(ai_providers,'environment_value',lambda name: None)
    status={x['name']:x for x in ai_providers.provider_status()}
    assert status['zhipu']['configured'] is False
    assert status['zhipu']['api_key_env']=='ZHIPU_API_KEY'

def test_remote_planner_uses_selected_provider(monkeypatch,tmp_path):
    captured={}
    def complete_json(prompt,provider,scope,timeout):
        captured.update(prompt=prompt,provider=provider,scope=scope,timeout=timeout)
        return {'summary':'ok','subtasks':[{'title':'implement','role':'implementation','suggested_agent':'codex'}],'verification_commands':['npm test']},{'provider':'zhipu','model':'glm-test'}
    monkeypatch.setattr('app.planner.complete_json',complete_json)
    monkeypatch.setattr('app.planner.load_config',lambda:{'planner':{'provider':'zhipu','type':'ai'}})
    plan=AiPlanner().plan('title','description',tmp_path)
    assert captured['provider']=='zhipu'
    assert plan['summary']=='ok' and plan['subtasks'][0]['suggested_agent']=='codex'
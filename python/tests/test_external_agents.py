import json
import time

from app import external_agents
from app.dingtalk import parse_message,_priority_ack_text


def _write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf8', newline='') as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + '\n')


def test_codex_manual_sessions_are_monitored_after_baseline(tmp_path):
    session=tmp_path/'.codex'/'sessions'/'2026'/'09'/'22'/'rollout.jsonl'
    _write_jsonl(session, [
        {'type':'session_meta','payload':{'session_id':'codex-manual','originator':'codex-tui','cwd':r'D:\Workspace\demo'}},
        {'type':'event_msg','payload':{'type':'task_started'}},
    ])
    snapshots,pending=external_agents.scan_external_agents(user_home=tmp_path,state_home=tmp_path/'.personzit')
    assert not pending
    record=next(x for x in snapshots if x['agent']=='codex')
    assert record['status']=='RUNNING'
    assert record['workspace']==r'D:\Workspace\demo'

    _write_jsonl(session, [
        {'type':'event_msg','payload':{'type':'task_complete','last_agent_message':'已完成修复并通过测试'}},
    ])
    snapshots,pending=external_agents.scan_external_agents(user_home=tmp_path,state_home=tmp_path/'.personzit')
    assert [x['kind'] for x in pending]==['completed']
    assert pending[0]['summary']=='已完成修复并通过测试'
    assert next(x for x in snapshots if x['external_id']==record['external_id'])['status']=='COMPLETED'


def test_claude_sdk_sessions_are_excluded_but_manual_cli_sessions_are_kept(tmp_path):
    root=tmp_path/'.claude'/'projects'/'D--Workspace-demo'
    sdk=root/'11111111-1111-1111-1111-111111111111.jsonl'
    manual=root/'22222222-2222-2222-2222-222222222222.jsonl'
    _write_jsonl(sdk, [
        {'type':'user','entrypoint':'sdk-cli','promptSource':'sdk','cwd':r'D:\Workspace\demo','sessionId':'sdk','message':{'role':'user','content':'internal'}},
        {'type':'assistant','entrypoint':'sdk-cli','cwd':r'D:\Workspace\demo','sessionId':'sdk','message':{'role':'assistant','content':[{'type':'text','text':'internal output'}]}},
    ])
    _write_jsonl(manual, [
        {'type':'user','entrypoint':'cli','promptSource':'typed','cwd':r'D:\Workspace\demo','sessionId':'claude-manual','message':{'role':'user','content':'手动查看项目规则'}},
        {'type':'assistant','entrypoint':'cli','cwd':r'D:\Workspace\demo','sessionId':'claude-manual','message':{'role':'assistant','content':[{'type':'text','text':'规则已经核对完成'}]}},
    ])
    snapshots,pending=external_agents.scan_external_agents(user_home=tmp_path,state_home=tmp_path/'.personzit')
    assert not pending
    assert [x['agent'] for x in snapshots]==['claude']
    assert snapshots[0]['session_id']=='claude-manual'
    assert snapshots[0]['workspace']==r'D:\Workspace\demo'
    assert snapshots[0]['summary']=='规则已经核对完成'


def test_external_followup_command_parse_and_priority_acknowledgement():
    result=parse_message('指挥 EXT-CODEX-0001 修改登录超时逻辑并补测试')
    assert result=={'action':'external_command','external_id':'EXT-CODEX-0001','instruction':'修改登录超时逻辑并补测试'}
    ack=_priority_ack_text('external_command',result)
    assert '已理解你的修订指挥' in ack
    assert 'EXT-CODEX-0001' in ack


def test_external_notifications_retry_until_send_succeeds(tmp_path):
    event={'kind':'completed','agent':'codex','external_id':'EXT-CODEX-0001',
           'session_id':'session','title':'fix login','workspace':r'D:\Workspace\demo',
           'summary':'done','status':'COMPLETED'}
    with external_agents._LOCK:
        state=external_agents._load_state(tmp_path/'.personzit')
        state['pending_notifications']=[event]
        external_agents._save_state(state,tmp_path/'.personzit')
    assert external_agents.drain_external_notifications(tmp_path/'.personzit',sender=lambda *_: False)==[]
    assert external_agents.drain_external_notifications(tmp_path/'.personzit',sender=lambda *_: True)==[event]
    assert external_agents.list_external_sessions(tmp_path/'.personzit')==[]

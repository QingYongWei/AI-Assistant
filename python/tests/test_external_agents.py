import json
import time
from datetime import datetime, timedelta, timezone

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
    # 发现时任务仍在运行：必须补发 started（多窗口漏通知修复）
    assert [x['kind'] for x in pending]==['started']
    assert pending[0]['session_id']=='codex-manual'
    record=next(x for x in snapshots if x['agent']=='codex')
    assert record['status']=='RUNNING'
    assert record['workspace']==r'D:\Workspace\demo'

    _write_jsonl(session, [
        {'type':'event_msg','payload':{'type':'task_complete','last_agent_message':'已完成修复并通过测试'}},
    ])
    snapshots,pending=external_agents.scan_external_agents(user_home=tmp_path,state_home=tmp_path/'.personzit')
    completed=[x for x in pending if x['kind']=='completed']
    assert [x['kind'] for x in completed]==['completed']
    assert completed[0]['summary']=='已完成修复并通过测试'
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


def test_new_running_codex_window_gets_started_notification_on_discovery(tmp_path):
    """多个新开 Codex 窗口：发现时任务已在运行，也必须补发 started 通知。"""
    for index,name in enumerate(('win-a','win-b'),1):
        session=tmp_path/'.codex'/'sessions'/'2026'/'09'/'23'/f'rollout-{name}.jsonl'
        _write_jsonl(session, [
            {'type':'session_meta','payload':{'session_id':f'codex-{name}','originator':'codex-tui','cwd':rf'D:\Workspace\{name}'}},
            {'type':'response_item','payload':{'type':'message','role':'user','content':[{'type':'input_text','text':f'窗口{name}的任务'}]}},
            {'type':'event_msg','payload':{'type':'task_started'}},
        ])
    snapshots,pending=external_agents.scan_external_agents(user_home=tmp_path,state_home=tmp_path/'.personzit')
    started=[x for x in pending if x['kind']=='started']
    assert len(started)==2
    assert {x['session_id'] for x in started}=={'codex-win-a','codex-win-b'}
    assert {x['title'] for x in started}=={'窗口win-a的任务','窗口win-b的任务'}
    assert all(x['status']=='RUNNING' for x in started)


def test_completed_history_discovered_late_stays_silent(tmp_path):
    """发现前就已结束的旧任务不补发通知。"""
    session=tmp_path/'.codex'/'sessions'/'2026'/'09'/'23'/'rollout-old.jsonl'
    _write_jsonl(session, [
        {'type':'session_meta','payload':{'session_id':'codex-old','originator':'codex-tui','cwd':r'D:\Workspace\old'}},
        {'type':'event_msg','payload':{'type':'task_started'}},
        {'type':'event_msg','payload':{'type':'task_complete','last_agent_message':'done'}},
    ])
    snapshots,pending=external_agents.scan_external_agents(user_home=tmp_path,state_home=tmp_path/'.personzit')
    assert pending==[]
    assert next(x for x in snapshots if x['agent']=='codex')['status']=='COMPLETED'


def test_non_manual_claude_session_is_ignored_without_reregistration(tmp_path):
    """非手动 Claude 会话标记 IGNORED 后不再反复重建记录（EXT ID 不漂移）。"""
    sdk=tmp_path/'.claude'/'projects'/'D--demo'/'sdk.jsonl'
    _write_jsonl(sdk, [
        {'type':'user','entrypoint':'sdk-cli','promptSource':'sdk','cwd':r'D:\demo','sessionId':'sdk','message':{'role':'user','content':'internal'}},
    ])
    snapshots,pending=external_agents.scan_external_agents(user_home=tmp_path,state_home=tmp_path/'.personzit')
    assert pending==[] and snapshots==[]
    state_path=tmp_path/'.personzit'/'runtime'/'external-agent-monitor.json'
    state=json.loads(state_path.read_text(encoding='utf8'))
    seq1=state['next_external_sequence']; assert len(state['sessions'])==1

    snapshots,pending=external_agents.scan_external_agents(user_home=tmp_path,state_home=tmp_path/'.personzit')
    state=json.loads(state_path.read_text(encoding='utf8'))
    assert state['next_external_sequence']==seq1
    assert len(state['sessions'])==1


def test_state_load_falls_back_to_backup_and_scan_does_not_reset(tmp_path):
    """主状态文件被写坏时读备份；两者都坏时跳过扫描而不是覆盖。"""
    state=external_agents._empty_state()
    state['next_external_sequence']=42
    state['sessions']={'codex:live':{'external_id':'EXT-CODEX-0042','agent':'codex','session_id':'live',
        'path':r'D:\missing.jsonl','offset':100,'cwd':'','title':'live task','summary':'','status':'RUNNING',
        'created_at':'2026-01-01T00:00:00+00:00','updated_at':'2026-01-01T00:00:00+00:00','last_event_at':'2026-01-01T00:00:00+00:00'}}
    home=tmp_path/'.personzit'
    external_agents._save_state(state,home)
    state['next_external_sequence']=43
    external_agents._save_state(state,home)
    backup=home/'runtime'/'external-agent-monitor.json.bak'
    assert backup.exists() and json.loads(backup.read_text(encoding='utf8'))['next_external_sequence']==42

    # 主文件写坏但有备份：应从备份恢复
    home=tmp_path/'.personzit'
    state_path=home/'runtime'/'external-agent-monitor.json'
    state_path.write_text('{"version":1,"next_external_seq',encoding='utf8')
    loaded=external_agents._load_state(home)
    assert loaded['next_external_sequence']==42  # 回退读备份内容

    # 主/备都写坏：返回 unreadable 标记，扫描跳过且不覆盖现有文件
    backup.write_text('{"broken":',encoding='utf8')
    snapshots,pending=external_agents.scan_external_agents(user_home=tmp_path,state_home=tmp_path)
    assert snapshots==[] and pending==[]
    assert 'next_external_seq' in state_path.read_text(encoding='utf8')


def test_state_save_is_atomic_and_creates_backup(tmp_path):
    home=tmp_path/'.personzit'
    state=external_agents._empty_state(); state['next_external_sequence']=7
    external_agents._save_state(state,home)
    state['next_external_sequence']=8
    external_agents._save_state(state,home)
    base=home/'runtime'/'external-agent-monitor.json'
    backup=base.with_name(base.name+'.bak')
    assert json.loads(base.read_text(encoding='utf8'))['next_external_sequence']==8
    assert json.loads(backup.read_text(encoding='utf8'))['next_external_sequence']==7

def test_notification_status_is_chinese():
    title,markdown=external_agents.notification_message({
        'kind':'started','agent':'codex','external_id':'EXT-CODEX-0001',
        'session_id':'s','title':'修复登录','workspace':r'D:\demo',
        'summary':'','status':'RUNNING'})
    assert '**状态**：执行中' in markdown
    title,markdown=external_agents.notification_message({
        'kind':'completed','agent':'claude','external_id':'EXT-CLAUDE-0002',
        'session_id':'s','title':'查看规则','workspace':r'D:\demo',
        'summary':'完成','status':'WAITING_INPUT_OR_BACKGROUND'})
    assert '**状态**：等待输入或后台运行' in markdown


def test_claude_updates_are_coalesced_until_session_quiet(tmp_path):
    """Claude 每条 end-turn 输出不再逐条推送：缓冲并在会话静默后只推最后一条。"""
    manual=tmp_path/'.claude'/'projects'/'D--demo'/'33333333-3333-3333-3333-333333333333.jsonl'
    _write_jsonl(manual, [
        {'type':'user','entrypoint':'cli','promptSource':'typed','cwd':r'D:\demo','sessionId':'claude-manual','message':{'role':'user','content':'修复登录超时'}},
    ])
    home=tmp_path/'.personzit'
    snapshots,pending=external_agents.scan_external_agents(user_home=tmp_path,state_home=home)
    assert [x['kind'] for x in pending]==['started']

    now=datetime.now(timezone.utc).isoformat()
    _write_jsonl(manual, [
        {'type':'assistant','entrypoint':'cli','cwd':r'D:\demo','sessionId':'claude-manual','timestamp':now,'message':{'role':'assistant','content':[{'type':'text','text':'第一步：定位到超时代码'}]}},
        {'type':'assistant','entrypoint':'cli','cwd':r'D:\demo','sessionId':'claude-manual','timestamp':now,'message':{'role':'assistant','content':[{'type':'text','text':'第二步：修复完成并自测通过'}]}},
    ])
    snapshots,pending=external_agents.scan_external_agents(user_home=tmp_path,state_home=home)
    # 会话仍在活跃（事件时间戳是刚刚）：update 只缓冲不入队（started 是首轮遗留，未 drain）
    assert [x for x in pending if x['kind']=='update']==[]
    state_path=home/'runtime'/'external-agent-monitor.json'
    state=json.loads(state_path.read_text(encoding='utf8'))
    record=next(r for r in state['sessions'].values() if r['agent']=='claude')
    assert record['pending_update']['summary']=='第二步：修复完成并自测通过'

    # 模拟静默：last_event_at 回拨到 10 分钟前，扫描时合并推送最后一条
    record['last_event_at']=(datetime.now(timezone.utc)-timedelta(seconds=600)).isoformat()
    with external_agents._LOCK:
        external_agents._save_state(state,home)
    snapshots,pending=external_agents.scan_external_agents(user_home=tmp_path,state_home=home)
    updates=[x for x in pending if x['kind']=='update']
    assert len(updates)==1
    assert updates[0]['summary']=='第二步：修复完成并自测通过'
    state=json.loads(state_path.read_text(encoding='utf8'))
    record=next(r for r in state['sessions'].values() if r['agent']=='claude')
    assert 'pending_update' not in record


def test_notification_message_distills_markdown_noise():
    """通知正文提纯：代码块省略、markdown 修饰剥离、超长内容截断。"""
    title,markdown=external_agents.notification_message({
        'kind':'update','agent':'claude','external_id':'EXT-CLAUDE-0003',
        'session_id':'s','title':'## 修复登录\n超时问题',
        'workspace':r'D:\demo',
        'summary':'分析如下\n```python\nprint("hello")\n```\n- 修改 a.py\n- 修改 b.py\n- 补充测试\n- 提交代码',
        'status':'WAITING_INPUT_OR_BACKGROUND'})
    assert '```' not in markdown and '代码已省略' in markdown
    assert '## 修复登录' not in markdown and '修复登录' in markdown
    assert 'b.py' not in markdown and '…' in markdown
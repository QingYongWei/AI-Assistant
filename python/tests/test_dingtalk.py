from app.dingtalk import parse_message, HELP_TEXT
from app import dingtalk
from app import intents

def test_help_and_list():
    assert parse_message('帮助')['action']=='help'
    assert parse_message('help')['action']=='help'
    assert parse_message('列表')['action']=='list'

def test_create_with_and_without_project():
    r=parse_message('任务 给项目加登录功能')
    assert r=={'action':'create','requirement':'给项目加登录功能','project':None}
    r=parse_message('任务 加登录功能 项目=D:/x/y')
    assert r['action']=='create' and r['project']=='D:/x/y' and '项目=' not in r['requirement']

def test_approve_reject_with_reason():
    r=parse_message('通过 TASK-000001 方案可行')
    assert r=={'action':'approve','task_id':'TASK-000001','reason':'方案可行'}
    r=parse_message('approve task-000002')
    assert r['action']=='approve' and r['task_id']=='TASK-000002' and r['reason'] is None
    r=parse_message('驳回 TASK-000003 需要调整')
    assert r['action']=='reject' and r['reason']=='需要调整'

def test_clarify_and_status():
    r=parse_message('澄清 TASK-000004 用 SQLite 保存')
    assert r=={'action':'clarify','task_id':'TASK-000004','answer':'用 SQLite 保存'}
    r=parse_message('状态 task-000004')
    assert r=={'action':'status','task_id':'TASK-000004'}

def test_unknown_returns_none():
    assert parse_message('你好') is None
    assert parse_message('') is None

def test_identity_command():
    assert parse_message('身份')['action']=='identity'
    assert parse_message('whoami')['action']=='identity'


def test_running_and_cancel_commands():
    assert parse_message('运行')['action']=='running'
    assert parse_message('当前codex正在执行什么')['action']=='running'
    assert parse_message('TASK-000008把这个任务结束把')=={'action':'cancel','task_id':'TASK-000008'}


def test_workspace_and_broad_inspection_commands():
    assert parse_message('当前工作目录')['action']=='workspace'
    assert parse_message('切换工作目录 D:\\Workspace\\PersonZit')['action']=='set_workspace'
    assert parse_message('清除工作目录')['action']=='clear_workspace'
    assert parse_message('输出一下工作目录的项目三色预警规则')['action']=='inspect'

def test_workspace_rules():
    assert intents._heuristic('切换工作目录 D:\\Workspace\\PersonZit')['action']=='set_workspace'
    assert intents._heuristic('清除工作目录')['action']=='clear_workspace'
    assert intents._heuristic('输出一下工作目录的项目三色预警规则')['action']=='inspect'


class _FakeTextMessage:
    message_type='text'
    class text: content=' 通过 '
    @classmethod
    def get_text_list(cls): return [' 通过 ']

class _FakeRichTextMessage:
    message_type='richText'
    text=None
    def get_text_list(self): return [{'text':'@PersonZit'},{'text':'通过'}]

def test_contextual_approval_and_rich_text_extraction():
    assert parse_message('通过')=={'action':'approve','task_id':'LATEST'}
    assert parse_message('驳回')['action']=='reject'
    assert dingtalk._extract_incoming_text(_FakeTextMessage())=='通过'
    assert '通过' in dingtalk._extract_incoming_text(_FakeRichTextMessage())

def test_natural_language_contextual_approval():
    assert intents._heuristic('同意')['action']=='approve'
    result=intents.sanitize_intent({'action':'approve','task_id':'LATEST','confidence':0.9})
    assert result=={'action':'approve','task_id':'LATEST','confidence':0.9}


def test_contextual_continue_and_status_rules():
    assert parse_message('任务执行的怎么样了')=={'action':'status','task_id':'LATEST'}
    assert parse_message('继续执行任务')=={'action':'continue','task_id':'LATEST'}
    assert parse_message('继续执行 TASK-000009')=={'action':'continue','task_id':'TASK-000009'}

def test_contextual_continue_intent_rule():
    assert intents._heuristic('继续执行任务')=={'action':'continue','task_id':'LATEST','confidence':1.0}




def test_priority_acknowledgement_is_sent_before_create_operation():
    reply=dingtalk._priority_ack_text('create',{
        'requirement':'fix login timeout',
        'project':'D:\\Workspace\\conflict-event-hub',
    })
    assert 'fix login timeout' in reply
    assert 'ID' in reply
    assert 'Codex/Claude' in reply


def test_priority_acknowledgement_covers_read_only_inspection():
    reply=dingtalk._priority_ack_text('inspect',{'requirement':'inspect deployment docs'})
    assert 'inspect deployment docs' in reply
    assert 'Agent' in reply


def test_chat_does_not_generate_priority_acknowledgement():
    assert dingtalk._priority_ack_text('chat',{'reply':'hello'}) is None



def test_supplement_command_and_priority_acknowledgement():
    r=parse_message('补充 TASK-规则修正-000010 A.id 关联 B.id，不是 C.id')
    assert r=={'action':'supplement','task_id':'TASK-规则修正-000010','requirement':'A.id 关联 B.id，不是 C.id'}
    reply=dingtalk._priority_ack_text('supplement',r)
    assert 'A.id' in reply and '重新执行' in reply

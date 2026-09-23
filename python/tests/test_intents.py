from app import intents

def test_sanitize_create_and_project():
    result=intents.sanitize_intent({'action':'create','requirement':'fix login timeout','project':'D:/x','confidence':1.2})
    assert result['action']=='create'
    assert result['requirement']=='fix login timeout'
    assert result['project']=='D:/x'
    assert result['confidence']==1.0

def test_mutating_action_requires_explicit_task_id():
    result=intents.sanitize_intent({'action':'approve','task_id':'latest one','confidence':0.99})
    assert result=={'action':'need_task_id','requested_action':'approve','confidence':0.99}

def test_contextual_reject_can_use_latest_selector():
    result=intents.sanitize_intent({'action':'reject','task_id':'LATEST','reason':'no'})
    assert result=={'action':'reject','task_id':'LATEST','reason':'no','confidence':0.0}

def test_status_can_use_latest_selector():
    result=intents.sanitize_intent({'action':'status','task_id':'current task'})
    assert result=={'action':'status','task_id':'LATEST','confidence':0.0}

def test_clarify_requires_answer_and_task_id():
    result=intents.sanitize_intent({'action':'clarify','task_id':'task 12','answer':'use SQLite'})
    assert result['task_id']=='TASK-000012' and result['answer']=='use SQLite'

def test_interpret_disabled(monkeypatch):
    monkeypatch.setattr(intents,'load_config',lambda:{'dingtalk':{'natural_language':{'enabled':False}}})
    result,meta=intents.interpret_intent('please optimize home page speed')
    assert result=={'action':'unknown'} and meta=={'provider':'disabled'}

def test_openai_compatible_request(monkeypatch):
    class Response:
        def __enter__(self): return self
        def __exit__(self,*args): return False
        def read(self):
            return b'{"choices":[{"message":{"content":"{\\"action\\":\\"unknown\\"}"}}]}'
    captured={}
    def urlopen(request,timeout=None):
        captured['url']=request.full_url
        captured['auth']=request.get_header('Authorization')
        captured['body']=request.data.decode('utf8')
        return Response()
    monkeypatch.setattr(intents.urllib.request,'urlopen',urlopen)
    monkeypatch.setenv('TEST_KEY','secret')
    monkeypatch.setattr(intents,'load_config',lambda:{'dingtalk':{'natural_language':{
        'provider':'zhipu','model':'glm-test','api_key_env':'TEST_KEY'}}})
    result,meta=intents.interpret_intent('a special engineering status question')
    assert result['action']=='unknown'
    assert meta['provider']=='zhipu' and meta['model']=='glm-test'
    assert captured['url'].endswith('/chat/completions')
    assert captured['auth']=='Bearer secret'

def test_cancel_can_use_latest_selector():
    result=intents.sanitize_intent({'action':'cancel','task_id':'latest','confidence':0.99})
    assert result=={'action':'cancel','task_id':'LATEST','confidence':0.99}

def test_cancel_natural_language_uses_local_rule():
    result=intents._heuristic(' TASK-000008把这个任务结束把')
    assert result=={'action':'cancel','task_id':'TASK-000008','confidence':1.0}

def test_running_question_uses_local_rule():
    assert intents._heuristic('当前codex正在执行什么任务')['action']=='running'

def test_contextual_cancel_can_target_latest_task():
    result=intents.sanitize_intent({'action':'cancel','task_id':'LATEST','confidence':0.9})
    assert result=={'action':'cancel','task_id':'LATEST','confidence':0.9}


def test_set_workspace_requires_absolute_path():
    result=intents.sanitize_intent({'action':'set_workspace','project':'relative/path','confidence':1.0})
    assert result['action']=='unknown'
    assert 'absolute Windows path' in result['error']


def test_continue_can_use_latest_selector():
    result=intents.sanitize_intent({'action':'continue','task_id':'LATEST','confidence':0.9})
    assert result=={'action':'continue','task_id':'LATEST','confidence':0.9}



def test_supplement_can_use_latest_selector():
    result=intents.sanitize_intent({'action':'supplement','task_id':'latest','requirement':'A.id 关联 B.id','confidence':0.9})
    assert result=={'action':'supplement','task_id':'LATEST','requirement':'A.id 关联 B.id','confidence':0.9}


def test_intent_failure_preserves_active_task_correction(monkeypatch):
    text='d_event_unified_open_sensitive_word的event_id关联的是d_event_unified的id，d_event_unified的SOURCE_DATA_ID才是对应的数据工厂df_archive_event源事件id。'
    active={'public_id':'TASK-规则修正-000010','status':'EXECUTING','title':'事件三色预警','description':'原需求'}
    def failed(*args,**kwargs):
        raise RuntimeError('model output does not contain a JSON object')
    monkeypatch.setattr(intents,'complete_json',failed)
    monkeypatch.setattr(intents,'load_config',lambda:{'dingtalk':{'natural_language':{'provider':'zhipu','model':'GLM-5.3-FlashX'}}})
    result,meta=intents.interpret_intent(text,[],active)
    # The exact user correction is exercised through the exported helper too.
    assert intents._looks_like_task_supplement(text)
    assert result=={'action':'supplement','task_id':'TASK-规则修正-000010','requirement':text,'confidence':0.9}
    assert meta['provider']=='task-context-fallback'
    assert meta['original_error'].startswith('model output')

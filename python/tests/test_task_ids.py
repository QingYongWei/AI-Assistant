from app.task_ids import semantic_task_id
from app.intents import _normalize_task_id, sanitize_intent
from app.dingtalk import parse_message


def test_semantic_task_id_uses_issue_file_and_error_code():
    title='修复前置事件融合处理失败：EventSubjectDOMapper.xml 中 UPDATE event_subject 的 SET 子句使用子查询，PXC 报 PXC-4518'
    task_id=semantic_task_id(title,10)
    assert task_id=='TASK-前置事件融合处理失败-EventSubjectDOMapper-PXC-4518-000010'
    assert '/' not in task_id and ':' not in task_id


def test_semantic_task_id_for_business_requirement():
    assert semantic_task_id('帮我实现一个登录功能',23)=='TASK-登录功能-000023'


def test_semantic_task_ids_remain_parseable():
    task_id='TASK-登录功能-000023'
    assert _normalize_task_id(task_id)==task_id
    assert parse_message(f'状态 {task_id}')=={'action':'status','task_id':task_id}
    assert parse_message(f'通过 {task_id} 方案可行')=={'action':'approve','task_id':task_id,'reason':'方案可行'}
    assert parse_message(f'{task_id}把这个任务结束把')=={'action':'cancel','task_id':task_id}
    assert sanitize_intent({'action':'status','task_id':task_id,'confidence':0.9})=={'action':'status','task_id':task_id,'confidence':0.9}

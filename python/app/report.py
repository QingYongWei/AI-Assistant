import json
from datetime import datetime, timezone
def task_report(task, subtasks=(), verifications=(), events=()):
    return {'task_id':task.public_id,'title':task.title,'status':task.status,'summary':task.description,'subtasks':[{'id':s.public_id,'status':s.status,'summary':s.result_summary} for s in subtasks],'verifications':[{'command':v.command_json,'passed':v.passed,'summary':v.summary} for v in verifications],'events':len(list(events)),'generated_at':datetime.now(timezone.utc).isoformat()}
def markdown_report(report):
    lines=[f"# {report['task_id']} {report['title']}",f"状态：**{report['status']}**",'',report['summary'],'','## 子任务']
    lines += [f"- {x['id']}: {x['status']} — {x['summary'] or ''}" for x in report['subtasks']] or ['- 无']
    lines += ['','## 验收']+[f"- {'通过' if x['passed'] else '失败'}: {x['command']}" for x in report['verifications']] or ['- 无']
    return '\n'.join(lines)+'\n'

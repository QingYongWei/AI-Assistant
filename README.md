## 钉钉使用

### 执行中任务补充 / 纠错

当存在非终态任务时，直接发送字段关系、表关系、业务规则等说明性补充，例如：

```text
A 表的 event_id 关联 B 表 id，B 的 SOURCE_DATA_ID 才对应源事件 id。
```

PersonZit 会：

1. 优先回复已理解的补充内容；
2. 将补充写入任务描述与事件日志；
3. 当前 Agent 完成本轮后，把补充作为修订要求重新执行相关子任务；
4. 若模型意图解析异常，也会基于当前任务上下文兜底识别，不会再降级成普通聊天。

也可显式指定任务：

```text
补充 TASK-任务语义ID-000010 <补充规则>
```

详细部署、配置、使用与故障排查步骤请见：[docs/实施手册.md](docs/实施手册.md)

# PersonZit

PersonZit 是运行在 Windows 本地的 AI 工程任务编排 MVP：Node.js CLI 作为统一入口，Python FastAPI 提供控制面，SQLite 保存任务、审批、事件和持久化 Job 队列。

## 快速开始

```powershell
npm install
node bin/personzit.js init
node bin/personzit.js start
node bin/personzit.js task create --title "示例任务" --description "实现并测试一个功能" --project "$PWD"
node bin/personzit.js task list
node bin/personzit.js task approve <创建任务时返回的任务ID>
```

默认使用无需外部模型的 MockPlanner/MockAgent，便于端到端验证。运行数据位于 `%USERPROFILE%\.personzit`，可用 `PERSONZIT_HOME` 覆盖。

## API

- `GET /health`
- `GET/POST /api/tasks`
- `GET /api/tasks/{TASK-ID}`
- `GET /api/tasks/{TASK-ID}/events`
- `POST /api/tasks/{TASK-ID}/approve|pause|resume|cancel|retry`
- `GET /api/agents`、`POST /api/agents/detect`
- `GET /api/dingtalk/status`，`POST /api/dingtalk/test`
- `personzit logs --service api|worker|dingtalk --follow`

Codex/Claude 适配器只有在配置中显式启用且本机可检测到可执行文件时才会报告可用；未配置时不会静默调用未知命令。

## 执行链路

- 钉钉消息 -> 云端模型识别自然语言意图 -> `chat` / `inspect` / `create` / `status` / `running` / `cancel` 等动作；固定指令先走本地规则
- 混合模式：云端模型负责自然语言理解与非任务聊天；PersonZit 负责授权、排队、审批、执行目录策略和留痕；任务规划与执行由本机 Codex/Claude 完成
- 普通聊天：直接自然回复；Provider 失败时降级本地 Codex/Claude，再兜底规则回复
- 百炼 Provider 使用普通按量计费 API；不要把 Coding Plan / Token Plan Key 或专用 Endpoint 用于 PersonZit 后端直连（官方限制其仅可用于 AI 编程工具/OpenClaw 类 Agent）
- 本地查看/搜索/总结：路径白名单校验 -> 只读 Agent；文档问答推荐 Claude `plan`，Codex `read-only` 可配置
- 工程任务：队列 -> AI Planner -> 方案文档 -> 人工审批 -> Codex `workspace-write` / Claude `acceptEdits`；默认直接使用项目当前分支，只有 `master` 才创建 Git Worktree 隔离分支
- 手动会话监管：Worker 周期扫描手动打开的 Codex TUI 与 Claude CLI 会话日志；任务开始、Claude 阶段输出、Codex 完成会推送钉钉，可用 `指挥 EXT-CODEX-0001 <修订要求>` 继续原会话
- 监管命令：`运行` 查看 QUEUED/RUNNING 队列与已登记 Agent 进程；`结束 <任务ID>` 会终止本次由 PersonZit 启动的 Agent 进程并落地取消状态
- 监管短口令：`任务执行的怎么样了` 查询最新任务；`继续执行任务` 会根据状态继续，待审批时等价于批准当前计划
- 服务重启恢复：Worker 启动时会检查 RUNNING Job 的锁持有进程；确认旧 Worker 已退出时，立即把孤儿 Job 重新入队，并把被中断的 AgentRun 标记为 `INTERRUPTED`，不再等待约 30 分钟心跳超时
- 会话工作目录：`切换工作目录 D:\路径`、`当前工作目录`、`清除工作目录`；状态持久化，服务重启后保留
- 钉钉富文本：同时支持 `text` / `richText`；`通过 / 批准 / 同意 / 驳回` 可作用于当前最新待审批任务
- PersonZit 不抢占/嵌入手工打开的 Codex/Claude 交互式窗口；它通过官方会话日志监管手动任务，Codex 修订用原生 queue 插回原会话，Claude 用 resume 恢复原会话
- 语义任务 ID：从问题标题、文件名和错误码提取关键信息，例如 `TASK-前置事件融合处理失败-EventSubjectDOMapper-PXC-4518-000010`；旧数字 ID 仍兼容。
- 默认授权工作区：`D:\Workspace\PersonZit`；需要隔离时任务目录使用语义任务 ID，方案文档为 `<语义任务ID>-plan.md`

查看处理链路：

```powershell
node bin/personzit.js logs --service dingtalk --follow
node bin/personzit.js logs --service worker --follow
```

钉钉日志使用 `message_id` 输出 `received -> authorization -> intent-precheck/start/finished -> priority-ack -> route-selected -> local-inspection/task-created/task-cancel -> reply -> trace-summary`；Worker 日志使用 `task_id` 输出 `planner -> workspace -> agent -> verification` 各阶段、执行者、模式、目录、耗时和结果。详细示例见实施手册“查看日志”和“钉钉双向集成”章节。

### 自然语言延迟调优

- `GLM-5.3-Flash` 为强制思考模型，简单意图也曾出现 6-12 秒延迟；当前默认推荐 `GLM-5.3-FlashX`。
- 实测同一意图提示下 `GLM-5.3-FlashX` 约 1.8-3.2 秒，`max_tokens=768` 足够返回可解析 JSON（含 reasoning token），且不会截断。
- `你能做点什么`、`当前 Codex 正在执行什么`、`TASK-xxxxxx 结束` 等高置信管理语义先走本地规则，不访问云端模型。


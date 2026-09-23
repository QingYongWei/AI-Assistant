# AI-Assistant

> 运行在 Windows 本地的 AI 工程任务编排框架 —— Node.js CLI 统一入口 + Python FastAPI 控制面 + SQLite 持久化，将 Codex / Claude 等本机 Agent 与钉钉消息、审批流程、任务队列串联为完整的自动化执行链路。

**当前版本：v0.1.0（MVP）**

---

## 目录

- [项目介绍](#项目介绍)
- [核心特性](#核心特性)
- [系统架构](#系统架构)
- [环境要求](#环境要求)
- [快速开始](#快速开始)
- [配置说明](#配置说明)
- [使用指南](#使用指南)
  - [服务管理](#服务管理)
  - [任务管理](#任务管理)
  - [Agent 管理](#agent-管理)
  - [日志查看](#日志查看)
- [钉钉集成](#钉钉集成)
- [API 参考](#api-参考)
- [运行测试](#运行测试)
- [项目结构](#项目结构)
- [已知限制](#已知限制)
- [文档](#文档)

---

## 项目介绍

AI-Assistant 是一个本地优先（local-first）的 AI 工程任务编排框架。它把「提出需求 → 生成方案 → 人工审批 → Agent 执行 → 验证留痕」这条链路固化成可管理的服务，适合个人开发者或小团队在本机安全地调度 AI 编程助手完成真实工程任务。

> 说明：项目名称为 **AI-Assistant**；CLI 命令、环境变量与运行数据目录仍为 `personzit` / `PERSONZIT_*` / `.personzit`。

典型的使用方式：

- **CLI 驱动**：通过 `personzit` 命令创建任务、审批方案、查看执行记录；
- **钉钉驱动**：在钉钉里发送自然语言指令，云端模型负责意图识别，AI-Assistant 负责授权、排队、审批与执行，执行结果与关键节点自动推送回钉钉；
- **混合监管**：手动打开的 Codex / Claude 交互式会话也会被 Worker 周期扫描并纳入监管，修订指令可插回原会话继续执行。

默认内置无需外部模型的 MockPlanner / MockAgent，开箱即可端到端验证链路；接入真实 Codex / Claude 或云端模型只需修改配置。

## 核心特性

- 🔄 **完整任务生命周期**：创建 → 规划 → 审批 → 排队 → 执行 → 验证 → 终态，支持暂停 / 恢复 / 取消 / 重试与澄清；
- 🤖 **多 Agent 适配**：内置 Codex、Claude、zcode、DeepSeek、Mock 适配器，仅在显式启用且可检测到可执行文件时生效，不会静默调用未知命令；
- 💬 **钉钉双向集成**：消息意图识别、审批通知、富文本（text / richText）、审批短口令（通过 / 驳回等）、任务进度推送；
- 🧠 **混合模式**：云端模型负责自然语言理解与非任务聊天，本机 Agent 负责规划与执行，AI-Assistant 负责授权、隔离与留痕；
- 🔐 **安全边界**：路径白名单校验、只读 / workspace-write / acceptEdits 等最小权限模式、Git Worktree 隔离策略、默认授权工作区；
- 📋 **手动会话监管**：不抢占手工打开的 Codex / Claude 窗口，通过官方会话日志监管；Codex 修订用原生 queue 插回原会话，Claude 用 resume 恢复；
- 🆔 **语义任务 ID**：从问题标题、文件名和错误码提取关键信息生成可读 ID，例如 `TASK-前置事件融合处理失败-EventSubjectDOMapper-PXC-4518-000010`；
- ♻️ **服务重启恢复**：Worker 启动时检查 RUNNING Job 的锁持有进程，自动重新入队孤儿 Job，并将被中断的 AgentRun 标记为 `INTERRUPTED`；
- 🧾 **全程留痕**：钉钉日志按 `message_id` 输出完整处理链，Worker 日志按 `task_id` 输出 `planner -> workspace -> agent -> verification` 各阶段。

## 系统架构

```text
┌────────────┐   自然语言    ┌──────────────┐   HTTP    ┌──────────────────┐
│   钉钉      │ ───────────→ │  云端 NLU 模型 │ ────────→ │  FastAPI 控制面    │
│ (双向通知)  │ ←─────────── │ (意图识别)     │           │  (任务/审批/事件)  │
└────────────┘   推送/审批   └──────────────┘           └────────┬─────────┘
                                                                    │ SQLAlchemy
                                                                    ▼
┌────────────┐   CLI/API    ┌──────────────┐                  ┌──────────────┐
│  Node.js    │ ──────────→ │  FastAPI      │ ←─────────────── │  SQLite      │
│  CLI 入口   │             │  API + Worker │    持久化 Job 队列 │  (任务/事件)  │
└────────────┘             └──────┬───────┘                  └──────────────┘
                                  │ 启动/监管
                                  ▼
                      ┌────────────────────────┐
                      │  本机 Agent 执行器       │
                      │  Codex / Claude / Mock… │
                      └────────────────────────┘
```

- **Node.js CLI**（`bin/personzit.js`）：统一入口，负责初始化、配置、服务启停、任务管理与日志；
- **Python FastAPI**：控制面 API + 后台 Worker，负责任务状态机、审批、持久化 Job 队列与 Agent 进程管理；
- **SQLite**：保存任务、审批、事件和 Job 队列，默认位于 `%USERPROFILE%\.personzit\data\personzit.db`。

## 环境要求

| 依赖 | 版本 / 说明 |
|---|---|
| 操作系统 | Windows（当前版本面向 Windows 本地场景开发） |
| Node.js | ≥ 20 |
| Python | ≥ 3.11 |
| Git | Worktree 隔离模式需要 |
| Codex / Claude CLI | 可选；仅在配置中启用后才会被检测和调用 |
| 钉钉机器人 | 可选；用于双向集成时配置 |

## 快速开始

```powershell
# 1. 安装 Node.js 依赖
npm install

# 2. 初始化运行环境（创建 venv、安装 Python 依赖、生成默认配置）
node bin/personzit.js init

# 3. （推荐）交互式配置 Provider、Agent、钉钉与工作区
node bin/personzit.js configure

# 4. 启动 API 与 Worker（后台方式）
node bin/personzit.js start

# 5. 创建一个示例任务
node bin/personzit.js task create `
  --title "示例任务" `
  --description "实现并测试一个功能" `
  --project "$PWD"

# 6. 查看任务并审批
node bin/personzit.js task list
node bin/personzit.js task approve <创建任务时返回的任务ID>
```

默认使用无需外部模型的 MockPlanner / MockAgent，可立即完成端到端验证。运行数据位于 `%USERPROFILE%\.personzit`。

如需全局命令，可在项目根目录执行 `npm link`，之后即可直接使用 `personzit`。

## 配置说明

推荐使用交互式向导：

```powershell
node bin/personzit.js configure
```

也可手工编辑配置文件（可参考 [`config/config.example.yaml`](config/config.example.yaml)）。主要配置段：

| 配置段 | 说明 |
|---|---|
| `server` | API 监听地址与端口（默认 `127.0.0.1:8765`） |
| `worker` | 轮询间隔与心跳过期时间 |
| `ai_providers` | 智谱 / 百炼等云端 Provider 模型配置 |
| `planner` | 任务规划器；推荐 `local` + `codex`（本机生成工程方案） |
| `agents` | Agent 适配器与调用参数（可执行文件、参数模板、超时） |
| `dingtalk` | 钉钉机器人、白名单、自然语言理解、通知与本地只读动作 |
| `workspace` | 授权工作区、Git 隔离策略（`current-branch-except-master` / `always-worktree`） |

### 支持的环境变量

| 变量 | 说明 | 默认值 |
|---|---|---|
| `PERSONZIT_HOME` | 运行数据根目录 | `%USERPROFILE%\.personzit` |
| `PERSONZIT_PYTHON` | 用于创建虚拟环境的 Python 解释器 | `python` |
| `PERSONZIT_API_URL` | CLI 访问 API 的地址 | `http://127.0.0.1:8765` |
| `PERSONZIT_API_TOKEN` | 请求 API 时附加的 Bearer Token | 未设置 |
| `PERSONZIT_DB` | 覆盖 SQLite 数据库文件路径 | `$PERSONZIT_HOME\data\personzit.db` |

> ⚠️ 如设置 `PERSONZIT_PYTHON`，服务启动时也会优先使用该解释器，请确保其已安装全部依赖。

## 使用指南

### 服务管理

```powershell
node bin/personzit.js start      # 启动 API 与 Worker
node bin/personzit.js start --foreground   # API 前台运行（Worker 仍后台）
node bin/personzit.js stop       # 停止服务
node bin/personzit.js restart    # 重启服务
node bin/personzit.js status     # 查看服务状态
node bin/personzit.js doctor     # 运行环境自检
```

### 任务管理

```powershell
# 创建任务（可指定优先级与验证命令）
node bin/personzit.js task create `
  --title "修复登录超时" `
  --description "定位并修复偶发登录超时问题" `
  --project "$PWD" `
  --priority 1 `
  --verify "npm test"

# 查询与详情
node bin/personzit.js task list
node bin/personzit.js task show <任务ID>
node bin/personzit.js task runs <任务ID>     # Agent 执行记录
node bin/personzit.js task events <任务ID>   # 事件时间线

# 审批与状态流转
node bin/personzit.js task approve <任务ID>
node bin/personzit.js task reject <任务ID>
node bin/personzit.js task clarify <任务ID> --message "补充说明"
node bin/personzit.js task pause <任务ID>
node bin/personzit.js task resume <任务ID>
node bin/personzit.js task cancel <任务ID>
node bin/personzit.js task retry <任务ID>

# 一步式：创建并执行一个带验证命令的任务
node bin/personzit.js run "实现并测试一个功能" --project "$PWD" --verify "npm test"
```

### Agent 管理

```powershell
node bin/personzit.js agent list    # 查看已启用的 Agent
node bin/personzit.js agent detect  # 检测本机可用的可执行文件
```

### 日志查看

```powershell
node bin/personzit.js logs --service api --follow
node bin/personzit.js logs --service worker --follow
node bin/personzit.js logs --service dingtalk --follow --lines 200
```

钉钉日志按 `message_id` 输出 `received → authorization → intent → route → task/reply → trace-summary`；Worker 日志按 `task_id` 输出规划、工作区、Agent、验证各阶段的执行者、模式、目录、耗时与结果。

## 钉钉集成

在配置文件中启用 `dingtalk` 后，即可通过钉钉完成完整闭环：

- **自然语言操作**：创建任务、查询状态、取消任务、普通聊天；固定高置信指令（如 `运行`、`任务执行的怎么样了`、`TASK-xxx 结束`）直接走本地规则，无需云端模型；
- **审批短口令**：`通过 / 批准 / 同意 / 驳回` 作用于当前最新待审批任务；
- **执行中补充 / 纠错**：存在非终态任务时直接发送补充说明，或显式指定任务：

  ```text
  补充 TASK-任务语义ID-000010 <补充规则>
  ```

  补充会写入任务描述与事件日志，并在当前 Agent 完成本轮后作为修订要求重新执行相关子任务；
- **手动会话监管**：Worker 周期扫描手动打开的 Codex TUI / Claude CLI 会话日志，任务开始、阶段输出、完成时推送钉钉；可用 `指挥 EXT-CODEX-0001 <修订要求>` 继续原会话；
- **工作目录会话**：`切换工作目录 D:\路径`、`当前工作目录`、`清除工作目录`，状态持久化。

> 百炼 Provider 请使用普通按量计费 API；不要将 Coding Plan / Token Plan Key 或专用 Endpoint 用于 AI-Assistant 后端直连。

## API 参考

服务启动后默认监听 `http://127.0.0.1:8765`：

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/health` | 健康检查 |
| `GET` / `POST` | `/api/tasks` | 任务列表 / 创建任务 |
| `GET` | `/api/tasks/{TASK-ID}` | 任务详情 |
| `GET` | `/api/tasks/{TASK-ID}/events` | 任务事件 |
| `POST` | `/api/tasks/{TASK-ID}/approve` | 通过计划，进入执行队列 |
| `POST` | `/api/tasks/{TASK-ID}/pause` | 暂停 |
| `POST` | `/api/tasks/{TASK-ID}/resume` | 恢复 |
| `POST` | `/api/tasks/{TASK-ID}/cancel` | 取消 |
| `POST` | `/api/tasks/{TASK-ID}/retry` | 重试 |
| `GET` | `/api/agents` | Agent 列表 |
| `POST` | `/api/agents/detect` | 检测本机 Agent |
| `GET` | `/api/dingtalk/status` | 钉钉集成状态 |
| `POST` | `/api/dingtalk/test` | 发送钉钉测试消息 |

## 运行测试

```powershell
# Node.js 测试
npm test

# Python 测试
cd python
python -m pytest
```

Python 开发依赖：`pytest`、`httpx`、`ruff`，可通过 `pip install -e ".[dev]"` 安装。

## 项目结构

```text
AI-Assistant/
├── bin/                    # CLI 入口（personzit.js）
├── config/                 # 示例配置
├── docs/                   # 实施手册与详细文档
│   └── 实施手册.md
├── python/
│   ├── app/                # FastAPI 控制面 + Worker
│   │   ├── main.py         # FastAPI 应用
│   │   ├── worker.py       # 后台 Worker
│   │   ├── queue.py        # 持久化 Job 队列
│   │   ├── state_machine.py# 任务状态机
│   │   ├── planner.py      # 任务规划器
│   │   ├── agents.py       # Agent 适配
│   │   └── ...
│   └── tests/              # Python 测试
├── src/                    # Node.js CLI 源码
├── package.json
└── README.md
```

## 已知限制

当前为 v0.1.0 MVP，使用前请了解：

1. **取消未闭环**：`cancel` 仅将状态置为 `CANCEL_REQUESTED`，暂无组件将其推进到 `CANCELLED`；
2. **retry 限制**：状态机未开放从终态重试的转换；
3. **无鉴权**：API 未实现身份校验（`PERSONZIT_API_TOKEN` 仅由 CLI 附加请求头，服务端不校验）。

## 文档

- [实施手册](docs/实施手册.md) —— 部署步骤、配置细节、日常使用、钉钉双向集成、故障排查与数据备份。

## License

本项目暂未声明开源许可证（License），如需复用或分发请先与作者确认。
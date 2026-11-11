# TeamFlow.dev

TeamFlow 是一个面向小团队的研发协作工作台：议题（Issue）、看板、里程碑、路线图、周计划、日程与团队设置。
本仓库是 TeamFlow 的移植版：后端迁移到 `server/`，前端全部改用 gh-ui 组件库（`D:\Projects\GitHub UI\dist`
中的单文件 `gh-ui.js` + `gh-ui.css`）重写，并**移除了网盘（Drive）与会议（Meeting）两个功能模块**。

- 技术栈：Python 3.11+ / FastAPI / SQLAlchemy 2 / SQLite（默认，也可 PostgreSQL、MySQL）；前端为原生 ES Module + Web Components，无构建步骤。
- 前端零运行时依赖：只使用 `static/vendor/gh-ui.js` 与 `static/vendor/gh-ui.css`。
- 单页应用：由后端 `static` 目录托管，任意非 API 路径回退到 `index.html`。
- 改型进行中：正在把工作台升级为「端侧探针 + 云端多智能体推理 + 人机确认 + 自动写回」的进度推断系统，
  引擎为 [orchify](https://github.com/)（`orchify>=0.0.2`）。方案见 `docs/改型方案-多智能体进度推断.md`，
  当前完成 **P0（服务端地基 + 端侧通信 SDK）**、**P1（WS 网关 + 配对 + 规则推断 + 洞察收件箱 + 引擎页面）**、
  **P2.5（议题层级 + 规划提案 + 验收标准 + 技能建议）**，以及**常驻 AI 助手对话框与「用 AI 新建项目组」**
  （复用同一份 `ai_*` 模型配置）。

## 目录结构

```
TeamFlow.dev/
├─ server/                  # 服务端（FastAPI + 静态前端）
│  ├─ main.py               # 启动器：参数解析、端口探测、uvicorn 启动
│  ├─ requirements.txt      # Python 依赖
│  ├─ .env.example          # 环境变量示例
│  ├─ app/                  # 后端应用
│  │  ├─ main.py            # FastAPI 应用、安全响应头、静态托管
│  │  ├─ config.py          # 配置、数据库 URL 解析、引擎开关与预算常量
│  │  ├─ deps.py            # 请求上下文、团队权限
│  │  ├─ middleware.py      # 写保护 / gzip 压缩中间件
│  │  ├─ security.py        # 口令哈希、Cookie、客户端 IP
│  │  ├─ db/                # 模型、会话、建表与增量迁移、演示数据
│  │  │  └─ engine_models.py # 推理引擎 9 张表（设备/仓库/代码事件/作业/洞察/审计…）
│  │  ├─ routers/           # system / auth / settings / issues / milestones / labels /
│  │  │                     # schedules / week / ai / teams / edge(引擎 REST + WS) / avatars
│  │  └─ services/          # 业务逻辑（议题、里程碑、日程、周计划、概览、序列化…）
│  │     ├─ assistant.py    # AI 助手：团队快照问答 + 项目草稿生成 + 一次性建项目组
│  │     ├─ engine/         # orchify 适配层：配置/LLM/Agent/结构化输出/hooks/落盘/预算/密钥
│  │     ├─ inference/      # 推理业务：事件包校验、审计哈希链、规则推断(rules/pipeline)、
│  │     │                  # 作业 worker、规划提案(proposals)、提案执行器(apply)
│  │     ├─ edge/           # 端侧接入：设备配对(devices)、WS 网关(gateway)、事件入库(ingest)
│  │     └─ hierarchy.py    # 议题层级：深度/环校验、叶子口径、父议题关闭守卫
│  ├─ static/               # 前端（gh-ui 重构）
│  │  ├─ index.html
│  │  ├─ vendor/            # gh-ui.js / gh-ui.css（来自 GitHub UI/dist，原样拷贝）
│  │  ├─ css/app.css        # 业务样式（在 gh-ui 设计令牌之上扩展）
│  │  └─ js/                # api / store / ui / shell / router / app / ai-dock + pages/*（含 ai 与 engine 页面）
│  └─ tests/                # pytest（139 例：API 冒烟 + 引擎地基 + 端侧接入 P1 + 层级与提案 P2.5 + AI 助手 + 端侧 SDK）
├─ client/                  # 端侧 SDK（探针 watchdog/脱敏/Diff 不在本次范围）
│  ├─ config.py             # EdgeConfig / edge.json / TEAMFLOW_EDGE_* 环境变量
│  ├─ api.py                # REST 兜底上报 + /api/health 健康检查
│  ├─ main.py               # 命令行：status / set / send / run / clear
│  └─ core/network.py       # EdgeClient：WS 长连接/重连/心跳/离线队列/反向查询
├─ docs/                    # 改型方案
└─ transfer/                # 交付说明
```

## 快速开始

```powershell
cd server
pip install -r requirements.txt
python main.py --port 8000
```

浏览器打开 `http://127.0.0.1:8000`。启动器支持 `--host`（默认 `127.0.0.1`）、`--port`（默认 `8000`）、
`--reload`、`--log-level`、`--env-file`；端口被占用时会给出提示，而不是直接抛出 uvicorn 的异常。

默认带一套演示数据（两个团队 `default` / `infra`、十几条议题、里程碑、日程）：

| 账号 | 姓名 | 岗位 |
| --- | --- | --- |
| `linwei` | 林薇 | 产品负责人 |
| `chenmo` | 陈默 | 后端工程师 |
| `suxiao` | 苏晓 | 前端工程师 |
| `zhouyuan` | 周远 | 测试工程师 |

演示口令统一是 `demo1234`。关掉演示数据：`TEAMFLOW_SEED=0`（首次建库时生效）。
默认 `require_login=0`，即无需登录即可浏览，写操作通过侧栏「操作身份」切换身份、或在「设置 → 编辑权限」
里加一道编辑口令。

## 配置

配置来自 `server/.env`（可复制 `.env.example`），进程环境变量优先。

| 变量 | 说明 | 默认 |
| --- | --- | --- |
| `TEAMFLOW_DB_URL` / `DATABASE_URL` / `TMT_DB` | 数据库 URL，按此优先级取第一个 | `sqlite:///server/data.db` |
| `TEAMFLOW_SEED` | 首次建库是否写入演示数据 | `1` |
| `TEAMFLOW_BACKUP` / `TEAMFLOW_BACKUP_DIR` | 写操作前是否备份 SQLite / 备份目录 | `1` / `server/backups` |
| `TEAMFLOW_AVATAR_DIR` | 头像文件目录 | `server/data/avatars` |
| `TEAMFLOW_COOKIE_SECURE` / `TEAMFLOW_HSTS` | Cookie 是否强制 Secure / 是否发送 HSTS | 跟随协议 / `1` |
| `TEAMFLOW_ENGINE` | 推理引擎总开关，`0` 时整块关闭（不建引擎目录、不加载 orchify） | `1` |
| `TEAMFLOW_ENGINE_DIR` | 引擎运行数据目录（事件 JSONL / 检查点 / runs.db / 向量） | `server/data/engine` |
| `TEAMFLOW_INFERENCE_WORKER` | 是否启动推理作业后台 worker，`0` 时改为手动 drain | `1` |
| `TEAMFLOW_ENGINE_BASE_URL` / `_MODEL` / `_API_KEY` | 推理模型端点（OpenAI 兼容），覆盖团队配置 | 空 |
| `TEAMFLOW_SECRET_KEY` | 加密 `team_engine.api_key` 的主密钥（不少于 24 字符） | 空 |

数据库 URL 支持 `sqlite:///相对路径`、`sqlite:///:memory:`、`postgresql://`、`mysql://`。
AI 助手的模型配置（`ai_base_url` / `ai_api_key` / `ai_model`）存在数据库 `settings` 表，在「设置 → AI 助手」
中填写，不走环境变量；推理引擎则优先读 `team_engine` 表，其次读同一组 `ai_*` 设置，最后才看 `TEAMFLOW_ENGINE_*`
环境变量。

## 功能

- **概览**：未关闭 / 进行中 / 已完成 / 完成率四项统计、里程碑进度、最近动态、成员负载、近 14 天新建与关闭趋势。
- **议题**：按状态、负责人、里程碑、标签与层级筛选；支持父子议题（最多 3 层）、预估工时、评论、标签与关闭/重新打开。
- **看板**：待办 / 进行中 / 已完成三列，拖拽即改状态。
- **周计划**：按成员分组的本周计划、未排期议题一键排入、上周未完成项顺延。
- **里程碑**：进度条（只统计叶子议题）、验收标准清单、计划型 / 持续型两种类型。
- **路线图**：按时间轴排列里程碑，含完成度和今天的标记。
- **日程**：团队周历（每周重复 / 单次）、参与人、ICS 订阅。
- **设置**：团队资料与公开范围、成员与技能、标签、编辑权限、登录策略、AI 助手、危险操作。
- **引擎**（改型新增）：设备配对、在线状态、洞察收件箱、规划提案、审计链校验。
- **AI 助手**：三处入口共用一个模型配置——任意页面右下角常驻对话框（Ctrl/⌘+J 开关）、
  团队级 **AI 工作台**（`/<team>/ai`：大输入框 + 问答/规划/复盘模式 + `@` 引用议题 + 最近会话）、
  以及「用 AI 新建项目组」页面（`/teams/new`）把一句项目描述变成里程碑 + 议题草稿。

### 议题层级

`issues.parent_id` 自引用构成父子关系，索引 `idx_issues_parent(parent_id, sort_order)`：

- 编号仍是团队内的全局序号（子议题也是 `#n`，不会出现 `#12.1`）。
- 深度上限 3 层；移动时做环检测与子树深度校验，跨团队挂载会被拒绝。
- 子议题默认继承父议题的里程碑，标签与负责人不继承。
- 有子议题的父议题**不能直接删除**；子议题未完成时**不能把父议题关闭**（父议题改为提示「可以关闭了」）。
- **统计口径只算最底层议题**：议题列表计数、概览统计、成员负载、里程碑进度、团队计数全部按叶子议题计算，
  父议题不重复计数；列表页会显示「父议题 N」与「统计只算叶子议题」提示。
- 排期只落叶子议题；列表支持 `?parent=none`（仅顶层）与 `?parent=<编号>`（该议题的子级）。

## 推理引擎（改型 P0 + P1 + P2.5）

目标是把「人手动改状态」升级为「保存代码 → 端侧脱敏上报 → 多智能体推断 → 人确认 → 写回」。
P0 落**服务端地基**与**端侧通信 SDK**；P1 落**端侧接入链路**与**规则推断的洞察收件箱**；
P2.5 落**议题层级**、**规划提案**（拆分 / 任务树 / 验收标准 / 负责人建议）与**可撤销的执行器**。
仍未接入 LLM 调用（P2）与外部系统写回（P3）。

已交付：

- **9 张表**（`app/db/engine_models.py`）：`devices` 配对设备、`repo_bindings` 仓库绑定、
  `code_events` 脱敏代码事件（`event_uid` 幂等）、`inference_jobs` 推理作业、
  `insights` 洞察（`draft/confirmed/rejected/expired`）、`insight_events` 洞察时间线、
  `team_engine` 团队引擎配置、`integration_credentials` 外部集成凭据、`inference_audit` 审计。
- **审计哈希链**（`app/services/inference/audit.py`）：每行 `prev_hash → hash` 按团队串链，
  `verify()` 可定位第一处断点，用于追溯「谁在什么时候确认了哪条推断」。
- **端侧事件包 schema v1**（`app/services/inference/events.py`）：Pydantic 校验体积、schema 版本、
  `event_uid` 字符集、相对路径（挡住 `../`、盘符、绝对路径）、文件数上限，并在文件名/备注里
  兜底扫描密钥样式（`sk-`、`AKIA`、私钥头等），命中即拒收。
- **orchify 适配层**（`app/services/engine/`）：团队配置读写（`config`）、OpenAI 兼容后端与探活（`llm`）、
  四个角色 Agent 与预算切分（`agents`/`budget`）、异步结构化输出与防幻觉闸门（`structured`）、
  事件订阅与卸载（`hooks`）、事件/检查点/runs.db 落盘（`persist`）、密钥加密（`secrets`）。
- **端侧 SDK**（`client/`）：`EdgeClient` 负责 WS 长连接（指数退避重连）、心跳、
  JSONL 离线队列（按 `event_uid` 去重、上限 500 条、ack 超时重投最多 5 次）、`ack` 处理、
  `pause/resume` 背压，以及服务端反向查询（`on_query_request` / `on_query`）。
  组包侧带 `validate_event()` 本地自检（与服务端同一套 schema v1 规则，错误在端侧就拦下来），
  `client/api.py` 提供 REST 兜底与 `/api/health` 探活，`client/main.py` 是可用的命令行入口。
  依赖 `websockets`，仅在真正连接时导入。
- **反向查询（点名文件）**（`client/core/query.py`）：服务端可以指定一批相对路径，
  端侧在 `QuerySandbox` 里回答：路径必须在允许的根目录内（拒绝绝对路径、盘符、`..`、符号链接逃逸），
  `mode="stat"` 只回大小 / sha256 / 行数 / 语言 / 修改时间；
  `mode="read"` 才回文本，且必须由端侧显式开启（`allow_content=True` / CLI `--allow-content`），
  单文件按 `max_bytes`（默认 8 KB，上限 256 KB）截断，二进制与未知后缀一律拒答，
  内容里的 `sk-` / `AKIA` / 私钥头 / `password=` / 邮箱会先被替换成 `[REDACTED]` 并回报计数。
  服务端入口是 `POST /api/teams/{slug}/engine/edge/devices/{id}/query`，引擎页设备行有「查文件」按钮。

```python
from client.core.network import EdgeClient, build_event, new_event_uid
from client.core.query import QuerySandbox

client = EdgeClient("ws://127.0.0.1:8000/api/edge/ws", token="配对码",
                    device={"name": "linwei-mbp", "platform": "darwin"})
# 反向查询：端点名要哪些文件，端侧在根目录内回答（默认不传内容）
client.on_query_request = QuerySandbox(r"D:\Projects\TeamFlow.dev", allow_content=False)
client.enqueue(build_event(
    event_uid=new_event_uid(),
    repo={"label": "teamflow", "path_hash": "9f2c1a77b04e5d63", "branch": "feat/engine"},
    files=[{"path": "server/app/main.py", "status": "modified", "added": 30, "removed": 5}],
))
await client.run_forever()
```

命令行（`pip install websockets` 后可用）：

```powershell
# 1. 写入端侧配置（也可用 TEAMFLOW_EDGE_* 环境变量）
python -m client.main set --url ws://127.0.0.1:8000/api/edge/ws `
    --token tfd_xxx --team default --device-name linwei-mbp --platform darwin

# 2. 看配置、离线队列与连通性
python -m client.main status --check

# 3. 手工上报一个事件（--file path[:added:removed]、--added-file、--deleted-file）
python -m client.main send --branch feat/12-login --head abc1234def5678 `
    --path-hash 9f2c1a77b04e5d63 `
    --file server/app/routers/auth.py:40:5 --added-file server/tests/test_login.py

# 4. 常驻：清空离线队列并保持长连接（--repo-root 打开反向查询，
#    --allow-content 才允许回文本内容，默认只回大小/摘要）
python -m client.main run --repo-root D:\Projects\TeamFlow.dev --allow-content

# 5. 丢弃离线队列（会改名成 events.jsonl.bak）
python -m client.main clear
```

SDK 行为要点：

| 场景 | 行为 |
| --- | --- |
| 握手前连接被拒 | `hello_ok` 之外的 `error` 帧若 code ∈ `bad_device_token / device_revoked / pair_expired`，抛 `FatalClose(4401)` |
| 服务端关闭 4401 / 4409 / 1008 | 记下 `client.fatal = {code, reason}` 并**停止重连**（吊销、别处重连、拒连都不该无限重试） |
| 网络抖动、ack 超时 | 在途事件放回队首重投，`attempts` 超过 5 次计入 `dropped` 并回调 `on_reject` |
| `pause` / `resume` | 服务端背压，暂停期间只发心跳，恢复后继续补传 |
| 组包非法 | `build_event` / `validate_event` 抛 `EventInvalid`（带 `field`），CLI 退出码 2 |
| WS 完全不通 | 用 `client/api.py` 的 `RestFallback` 走 `POST /api/teams/{slug}/engine/edge/events`（仍受编辑口令保护） |

配套开关：`TEAMFLOW_ENGINE=0` 可整体关闭（启动日志只留一行，不建目录、不加载 orchify；
测试覆盖此路径）。`ORCHIFY_APPROVAL_TIMEOUT` 等 orchify 进程级配置由
`engine.configure_orchify_env()` 补默认值（orchify 自身默认是「永久等待审批」）。

### P1：端侧接入与洞察收件箱

- **配对与设备**（`app/services/edge/devices.py`、`app/routers/edge.py`）：
  管理员在「引擎」页生成配对码（默认 60 分钟内首次接入有效），端侧把 token 放进 `hello` 帧；
  支持多设备、吊销即时生效（在线连接被 4401 关闭）、暂停/恢复（服务端下发 `pause/resume`）。
- **WS 网关**（`app/services/edge/gateway.py`）：`/api/edge/ws` 上跑 `hello / event / heartbeat /
  query / query_result` 帧，事件按设备 60 秒滑动窗口限流，同设备新连接替换旧连接（4409），
  并提供服务端反向查询：`registry.query(device_id, files, mode=..., max_bytes=...)` 指定文件清单，
  端侧按 `client/core/query.py` 的沙箱规则回答（`stat` 只给指纹，`read` 才给脱敏内容）。
  REST 入口是 `POST /api/teams/{slug}/engine/edge/devices/{id}/query`，引擎页设备行的「查文件」按钮就是它。
- **入库**（`app/services/edge/ingest.py`）：事件再校验 → `event_uid` 幂等去重 → 落 `code_events`
  → upsert 仓库绑定 → 按「设备+仓库+分支」合并 5 秒窗口内的作业（`inference_jobs`）。
- **规则推断**（`app/services/inference/rules.py` + `pipeline.py`）：不调 LLM，用文件路径特征、
  测试变更、是否 push、分支上的议题号打分，产出完成度/建议状态/证据引用；仍要过
  `engine.check_analysis` 的防幻觉闸门，并按 `dedupe_key` 在 30 分钟内合并同一议题的重复洞察。
- **作业 worker**（`app/services/inference/worker.py`）：队列轮询、同事务认领、失败退避重试，
  产出新洞察后通过 WS 向在线设备广播 `insight` 帧；`TEAMFLOW_INFERENCE_WORKER=0` 可关闭，
  改用 `POST /api/teams/{slug}/engine/jobs/drain` 手动驱动（测试走这条路）。
- **审计**：配对、仓库绑定、洞察创建/驳回都写 `inference_audit` 哈希链，
  `GET /api/teams/{slug}/engine/audit/verify` 可一键校验未被篡改。
- **前端「引擎」页**（`static/js/pages/engine.js`）：在线设备/待确认洞察/排队作业/累计事件四项统计、
  洞察卡片（证据 chip、置信度进度条、建议状态）、设备表（查文件·暂停·恢复·吊销）、审计链与最近事件，
  以及配对码弹窗（含 `EdgeClient` 用法与一键复制）。「查文件」可以直接点名让在线设备回报
  指定文件的大小/摘要/行数，勾「读取内容」才会带回脱敏后的文本，结果在弹窗里逐文件展示。

### P2.5：议题层级、规划提案与验收标准

- **议题层级**（`app/services/hierarchy.py`）：`issues.parent_id / sort_order / estimate_hours`，
  深度上限 3，环检测、深度校验、跨团队拒绝；有子议题的父议题不能直接删除，子议题未完成时不能关闭父议题
  （409 `has_children`）；四处统计统一改为叶子口径。
- **数据补全**：`team_members.skills`（JSON 数组，`level 1..3`，设置页用「前端!, 登录+」语法编辑）、
  `milestones.acceptance`（验收标准清单，里程碑卡与编辑弹窗都可维护）。
- **规划提案**（`app/services/inference/proposals.py`，规则版、不依赖 LLM），`kind ∈ split / plan / criteria / assignment`：
  - `split`：按代码事件的文件顶层目录分组（或退回议题正文清单）建议子议题，单次最多 8 条；
  - `plan`：按里程碑验收标准生成任务树，同名议题自动去重；
  - `criteria`：里程碑缺验收标准时，从描述里抽取 `- [ ]` 清单；
  - `assignment`：`score = 0.5×技能覆盖 + 0.3×历史同类完成率 + 0.2×空闲度`，只给建议与理由。
  同一 `dedupe_key` 30 分钟内合并为一条洞察（`insight_events` 记 `merged`），不会刷屏。
- **提案执行器**（`app/services/inference/apply.py`）：`POST /engine/insights/{id}/apply` 默认 `dry_run`
  （只回预览、写 `previewed` 事件与审计）；真实执行在**单事务**内按序执行白名单动作
  （`create_issue / create_milestone / update_issue / update_milestone / close_issue / assign`），
  并记录 `before` 快照；`POST /engine/insights/{id}/revert` 可在 **5 分钟内**整体撤销
  （删除新建的议题/里程碑、恢复被改字段；若已被他人加评论或子议题则拒绝）。
- **前端**：议题详情显示面包屑、子议题进度、预估工时，并禁止误关父议题；议题列表新增「仅顶层议题」筛选；
  引擎页新增「生成提案」入口、提案卡片（类型徽章、动作预览、`预览并应用` / `驳回`）
  与「最近已应用（5 分钟内可撤销）」。

P2 起才做：LLM Analyst（`OpenAICompat` + 结构化输出）、人工确认后写回议题状态/周计划/评论、
服务端驱动端侧的定向取证，以及 GitHub / Jira 写回（P3）。

## AI 助手（常驻对话框 + AI 工作台 + 用 AI 新建项目组）

模型配置与「设置 → AI 助手」共用一份 `settings` 表里的 `ai_base_url` / `ai_api_key` / `ai_model`（OpenAI 兼容端点），
没配置时接口返回 400 `ai_unconfigured`，界面上会提示去设置页填写。

- **常驻对话框**（`static/js/ai-dock.js` + `POST /api/teams/{slug}/ai/chat`）：
  - 挂在 `document.body` 上一次，路由切换不重建，因此对话不会因为换页丢失；
    切团队时按 `localStorage` 里的 `tf:ai-chat:team:<slug>` 分别存取，最多保留 24 条。
  - 每次提问都会带上**团队快照**（叶子口径统计、最近 6 个里程碑与验收标准摘要、最近 10 个未关闭议题、成员技能），
    系统提示明确「只能基于快照回答、不得声称已改数据、涉及写操作让用户去界面确认」。
  - 快捷提问：概括当前进度 / 建议里程碑 / 安排本周 / 起草新项目（最后一项直接跳到 `/teams/new`）。
  - 限流 20 次 / 60 秒（`app/services/ratelimit.py`），超限返回 429 `rate_limited`；
    提问会写一条 `activity`（`asked`），便于团队追溯。
- **AI 工作台**（`static/js/pages/ai.js` + `/{team}/ai`）：把 Copilot 式首页搬进团队工作区。
  - 输入卡支持 `@` 引用议题（最多 8 个，`#n 标题 + 正文摘要` 会拼进系统提示）、三种模式
    （`ask` 问答 / `plan` 规划 / `review` 复盘，模式提示词影响回答侧重）、团队范围与当前模型 pill。
  - 快捷动作：排查风险、规划本周、新建议题（直接开新建弹窗）、起草新项目、引擎洞察。
  - **会话落库**（`ai_sessions` / `ai_messages` 两张表）：首条提问自动生成标题，最多保留最近 20 个会话；
    `GET/POST /api/teams/{slug}/ai/sessions`、`GET/DELETE /api/teams/{slug}/ai/sessions/{id}`；
    对话走 `POST /api/teams/{slug}/ai/chat {message, session_id, context, mode}`，服务端把
    user/assistant 两条消息写库并回传 `session_id`。
  - 悬浮对话框右上角「在 AI 工作台打开」跳到同一会话页；两处共享同一套团队快照与限流。
- **用 AI 新建项目组**（`static/js/pages/project-new.js` + `/api/ai/project/*`）：
  - `POST /api/ai/project/draft {goal, name?}` → 草稿（名称、地址、里程碑、议题），**只生成不落库**；
    服务端会归一化：日期颠倒自动交换并告警、缺截止日期按两周一个里程碑补齐、验收标准统一成 `- [ ]` 清单、
    工时夹在 1–80、非法优先级回落 `medium`、越界的里程碑序号归「未归属」。
  - 页面里可以改名、调日期、改验收标准、增删里程碑与议题；`POST /api/ai/project/create {draft}`
    在**单个事务**里建团队、里程碑、议题与缺省标签，并写一条团队级 `activity`。
  - 创建需要登录模式（`require_login=1`）：关掉登录时全平台只允许一个团队，页面会禁用按钮并说明原因。
  - 项目地址优先用 AI 建议的英文 slug；没有或非法时退化为 `proj-xxxx`，不会占掉 `/team` 这种泛化路径。
- **写保护豁免**：`/api/teams/{team}/ai/chat` 与 `/api/ai/project/draft` 是只读语义，免登录模式下不会被
  「编辑口令」中间件拦成 401；`/api/ai/project/create` 仍然是被保护的写操作。

## 与上游版本的差异

- 删除「网盘」模块：`routers/drive.py`、`services/drive.py`、`services/cloud.py`、`services/unifile/`、`DriveItem` 模型、`Team.drive_public`、`DRIVE_*` 配置与相关前端页面。
- 删除「会议」模块：`routers/meetings.py`、`services/meetings.py`、`MEETING_*` / ICE 配置与相关前端页面。
- 头像存储从 unifile 抽象改为本地磁盘（`TEAMFLOW_AVATAR_DIR`），不再依赖远端存储。
- 前端整体重写：使用 gh-ui 的 Web Components（`gh-app` / `gh-side` / `gh-box` / `gh-table` / `gh-dialog` 等），
  由 `js/` 下的 ES Module 编排；不引入构建工具、不写内联脚本（CSP 仅允许同源脚本）。

## 排查：改了代码却没有洞察

现象：本机明明在改代码，网页上的「最近一次分析」和洞察列表都不更新。

按这三步查，基本能定位问题：

1. 看端侧日志 `~/.teamflow/client.log`。第一行会写实际监听的目录、文件数、分支。
   如果目录不是你在改的那个（曾经出现过监听另一个同名空目录的情况），就是启动方式的问题：
   `start-client.cmd` 现在会显式传 `--root`，不要绕过它直接跑 `python -m client.main ui`。
2. 看有没有「待上报 N 个文件（N 秒后结算）」。有这行说明扫描正常，只是还没到空闲窗口；
   一直没有这行，说明改动没被看到——要么目录不对，要么改的是被忽略的运行产物。
3. 看 `inference_jobs.budget` 里的 `admission`。`verdict=skip` 会给出具体理由
   （只改文档、批量删除、敏感文件等），这不是「没分析」，而是判定不值得建卡。

还有一个容易忽略的坑：仓库里放着 `server/data.db`，服务端每 20 秒心跳会写 WAL。
这些文件现在已被排除在扫描之外。早期版本没有排除，空闲窗口会被无限重置，
表现就是「等多久都没有洞察」——和「AI 不看内容」是两回事。

## 测试

```powershell
cd server
python -m pytest tests -q
```

覆盖：
- `tests/test_api.py`：健康检查与 bootstrap、团队详情、议题全生命周期与筛选、里程碑、标签、日程、周计划、
  概览与路线图、已删除模块不再暴露路由、静态资源与 SPA 回退。
- `tests/test_engine_foundation.py`：9 张表建表、审计链正常/篡改检测/团队隔离、事件包 9 类非法输入、
  密钥泄漏拒收、超大包拒收、配置读写与环境变量优先级、Agent 预算与 JSON 开关、`FakeLLM` 走
  `aask_structured`、防幻觉闸门、hooks 注册/卸载、`TEAMFLOW_ENGINE=0` 零副作用、
  SDK 队列去重与上限、握手发送 ack、反向查询回包。
- `tests/test_edge_p1.py`：配对与吊销、未知配对码拒连、WS 上报幂等（`stored → duplicate`）、
  非法事件包 `ack rejected`、规则推断产出洞察并合并重复洞察、未 push / 无测试降级、
  纯文档改动被闸门跳过、驳回与二次驳回 409、审计链校验（配对→绑定→创建→驳回）、REST 兜底上报、
  `TEAMFLOW_ENGINE=0` 时配对返回 503，以及路径分类/周次换算/配对码不落明文等纯函数用例。
- `tests/test_edge_p2.py`：子议题只算叶子、子议题继承里程碑、自挂/反挂环检测、深度上限、
  父议题关闭与删除守卫、提升为顶层、成员技能往返、`split` 提案 apply（dry-run / 真实 / 二次 409 / revert）、
  重复提案合并、无证据提案降置信度、`criteria` 写入验收标准、负责人打分优先命中技能、非法 kind 400。
- `tests/test_ai_assistant.py`：未配置时 400、配置后状态与模型、对话往返（系统提示含团队快照）、
  空消息 400、草稿归一化（日期交换 / 验收标准统一 / 优先级回落 / 工时夹取 / 越界里程碑 / 空标题丢弃）、
  goal 过短 400、免登录时创建 403、服务层一次性建团队+里程碑+议题+标签、团队快照含成员与叶子口径说明、
  AI 工作台会话：context 归一化与落库、标题取首条、多轮历史、会话列表与详情、删除后 404、空消息 400、空会话。
- `tests/test_edge_sdk.py`：端侧 SDK 的组包与自动统计、`validate_event` 拒绝非法输入（版本/uid/kind/时间/
  路径绝对化/盘符/`../`/超长/超 400 文件）、离线队列去重与上限与重投、窗口流水线并发发送、
  `ack` 的 stored/rejected 分支、`pause/resume` 背压、反向查询回包（旧式 `on_query` 与新式
  `on_query_request` 两条路径）、4401/4409/`device_revoked` 的致命关闭（停止重连且 `fatal` 有值）、
  配置读写与环境变量覆盖、`ensure_ready` 参数校验、REST 兜底（`ws://` 地址自动换 `http://`、
  duplicate 不算错误、rejected 透传）、查询沙箱（点名文件、越界拒绝、`read` 需授权、二进制拒答、
  文件数上限、密钥脱敏）与 CLI（`--version`、`set`/`status`、`send --offline`）。

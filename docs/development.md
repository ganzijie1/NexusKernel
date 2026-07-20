# 开发扩展指南

## 新增模块

模块采用与两个来源项目一致的目录语义：

```text
app/modules/<module_name>/
├── __init__.py
├── module.py
├── api/                 # contracts.py、schemas.py、endpoints.py、agent_tools.py 按需
├── services/            # service.py 或按职责拆分
├── repositories.py      # 按需
├── workers.py           # 按需
└── data/                # 按需
```

1. 在 `module.py` 定义无参 Module、继承 `ModuleConfig` 的 strict/frozen schema，并把它
   赋给 `config_schema`；业务代码进入 services。
2. 通过 Module 的 `dependencies`、`optional_dependencies` 和 Provider 名声明运行时
   能力；跨模块只调用生命周期参数 `ModuleContext` 返回的白名单 API 代理。
3. 在 `config/modules.yaml` 注册与 `Module.name` 一致的键、
   `app.modules.<name>.module.<Class>` 类路径、私有配置，以及与 Module 属性按集合严格
   一致的 `dependencies`、`optional_dependencies` 导入前清单。模块映射顺序没有语义。
4. 无鉴权的简单 HTTP 接口使用 `HttpEndpointSpec`；鉴权或定制异常映射放到
   `app/routes` 与 `app/services`。
5. 先测试 schema、Protocol 越权、拓扑、降级、回滚和停止，再写实现。

跨模块静态导入只允许显式公共 `api`、`schema`、`event`、`protocol` 路径；禁止导入
其他模块的包根、聚合导出及私有 Module/Service/Repository/Worker。也禁止访问
`Kernel._entries`、让模块自行创建或关闭共享 Provider，或把业务实现放进
`nexus_kernel`。

`app.modules` 与模块包初始化文件必须零副作用或使用惰性导出，不得聚合导入具体 Module。
loader 会在业务实现导入前校验清单，并按稳定拓扑完成 import、配置验证和无参构造；必需
依赖缺失或禁用、循环、清单与类声明不一致都会 fail-fast。Module 构造函数只能建立与配置
无关的安全占位值，不得读取 `self.config`；纯内存服务装配进入 `on_register()`，外部 I/O
进入 `on_start()`。

依赖和 Provider 声明必须是 tuple，名称不得为空或重复；模块不得自依赖，必需与可选
Provider 及模块依赖分别互斥，Provider 名遵循 `[a-z][a-z0-9_]*`。Kernel 构造时校验类与
实例声明，并保存不可变能力快照；不得依靠构造期或生命周期中改写属性来改变拓扑、权限或
降级行为。

`ModuleContext.require_module()`、`optional_module()`、`require_provider()` 和
`optional_provider()` 都只能访问模块声明过的名字，未声明访问必须立即失败。模块通过
`get_worker_specs()` 返回本模块 WorkerSpec；Kernel 在构造时完成类型与重复名检查，
ApplicationRuntime 在 Kernel 启动后注册可运行模块的 Worker。

## Provider 与 Worker

Provider 只实现 `start`、`health_check`、`close`，由 app 装配 `ProviderSpec`。
必需 Provider 失败阻断启动；可选 Provider 自身失败为 `FAILED`，依赖它的 Module 在仍可
运行时为 `DEGRADED`；必需与可选 Provider 分类必须互斥，`required=True, enabled=False`
在配置和合同层都无效。factory 返回
candidate 后即把所有权交给 Registry；仅签名合法的异步 `close()` 在后续校验、start、
timeout 或取消失败时获得独立预算内单次清理保证。同步、缺失或签名非法的 close 不调用，
并报告 `provider_cleanup_unavailable`。Worker 通过 WorkerSpec 注册，必须协作响应 stop
event 和任务取消；API 角色不注册或启动 Worker。状态 listener（包括其主动抛出的
`CancelledError`）不得影响 Supervisor。

Module/Worker 停止超时不代表 task 已终止。框架保留原 task 及其依赖，显式重试复用该
task；只有 task 达到真实终态后才能继续关闭 Provider。需要硬终止能力时使用独立进程。

## 基础设施适配器

- Mongo 提供连接探测、DocumentStore ID CRUD、事务上下文和关闭。
- Redis 提供连接探测、KV/TTL、Stream、消费组与确认；`block_milliseconds` 和非空
  `ttl_seconds` 只接受严格正整数，bool、float、零和负数必须在 client I/O 前失败；消费
  循环必须传播取消。
- PostgreSQL 基于 asyncpg 提供连接池、execute/fetch 和事务；不包含 ORM 或迁移。
- Qdrant 提供连接探测、集合确保和 VectorStore CRUD；OTel 只负责通用追踪适配。

共享连接由 `app/providers` 按 `config/providers.yaml` 显式装配并交给 ProviderRegistry；
模块只声明并获取能力，不读取环境变量、不创建共享客户端、不负责关闭。

## AI 与工具

普通 completion 适配器实现 ModelClient；SDK 自己管理 agent loop、session 和工具执行时
实现 AgentRuntime。所有实现都必须归一化 ChatResponse/TokenUsage、设置超时、关闭
资源，并将 SDK 异常转换为不包含原始响应的稳定错误。
SDK 适配器构造函数必须无网络且不导入重型可选 SDK；幂等异步 `start()` 负责导入和创建
client，直接 `chat/run` 也只能复用同一个 start task。ModelClient 的 `chat()` 必须使用
关键字参数 `identity`；AgentRuntime 的 `run()` 身份参数保持现有合同。

业务工具由 app 注册 AgentToolSpec。参数使用 Draft 2020-12 JSON Schema 完整校验；结果
必须是严格 JSON。默认单工具 10 秒、最多 4 轮、每请求最多 16 次 handler，单参数和单结果
各最多 65,536 UTF-8 bytes；同一绝对 deadline 覆盖有界参数遍历、Schema、handler 与
增量结果编码。同步 handler 在线程中运行，timeout 无法撤销线程已发生的副作用，所以
副作用工具必须幂等。所有 handler 固定为 `handler(arguments, identity)`，并使用可信
`AgentIdentity` 授权；不得从 arguments、session 或请求正文重建身份。请求体不再接受
`user_id`。

多智能体定义同样归 app，并通过启动时冻结的 `AdkApplicationSpec` 注册表交给
WorkflowRuntime。Graph 中的 LLM 节点固定为 `single_turn`，Team 只允许 coordinator 加
不嵌套的 leaf `task`/`single_turn` Agent。每次 factory 创建独立 ADK App，但复用运行时
模型与绑定可信身份的 ToolExecutor；不能让客户端提交指令、拓扑、工具名、模型或类路径。
首版禁止循环、嵌套/动态 Workflow、有副作用的原生 FunctionTool 和并行 task 委派。

启用 Agent Runs 前先由 app 显式注入非空 `agent_applications` 注册表，再设置
`NEXUS_KERNEL_AGENT_RUNS_ENABLED=true`；仅设置环境开关会在启动期失败。仓库内的
`default_adk_applications()` 只用于本地验证，不会被生产启动器自动加载。
所有标准入口从 `app.agent_applications.registered_agent_runs()` 获取生产定义、工具、授权
和可选可信身份解析器；业务应用应在该代码边界装配指令、工具 allowlist、访问策略与
graph/team 工厂。入口把该函数作为 lazy factory 交给 bootstrap；只有 API 角色且模块启用
时才求值，Worker 不得主动构造 Agent Runs 注册表。
创建与恢复请求必须实现幂等，副作用 handler 还需使用业务幂等键，因为恢复按至少一次语义
执行。默认存储是进程内实现，开发多副本时必须使用粘性路由，不能把它当作持久任务队列。
新增状态或 phase 结果时必须同时扩展 MetricsRegistry 的固定 allowlist；不得把主体、
agent、run、session、Prompt、工具载荷或异常原文加入指标标签。

## 开发流程

先读取所有者、配置、直接调用方和测试；写一个会因缺少目标行为而失败的测试；完成最小
实现后运行聚焦测试，再执行 README 的完整门禁。环境需要创建或重建时只使用
`scripts/setup_python_env.sh` 或 `scripts/setup_python_env.ps1`。
开发与测试支持 Python 3.11-3.13；Ruff 和 mypy 保持以最低版本 3.11 为目标。
交付 wheel 时使用 `python -m build --wheel --outdir dist`，随后运行
`python scripts/verify_wheel.py dist`。校验器读取目标项目的 `pyproject.toml`，并核对
文件名、METADATA/WHEEL 单值字段基数、互不重复的 Tag 值、RECORD、CRC、唯一 dist-info
与允许的顶层目录；Tag 集合必须匹配文件名。文件名含 build tag 时 `WHEEL` 必须有且
仅有一个规范化后匹配的 `Build`，否则该字段必须省略。RECORD 摘要和大小必须匹配实际
字节。不得跳过旧包、未知顶层目录和敏感文件检查。构建前清理 `build`、`dist` 和
`*.egg-info`。`constraints/py311-313.txt` 只能由项目固定的 uv 版本从 `pyproject.toml`
重新生成，Linux 环境、Docker 和 CI 只能消费或验证该生成物，不得手工维护第二份版本
清单。

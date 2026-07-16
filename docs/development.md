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
2. 通过 `dependencies`、`optional_dependencies` 和 Provider 名声明依赖；跨模块只调用
   生命周期参数 `ModuleContext` 返回的白名单 API 代理。
3. 在 `config/modules.yaml` 注册与 `Module.name` 一致的键、
   `app.modules.<name>.module.<Class>` 类路径和私有配置。
4. 无鉴权的简单 HTTP 接口使用 `HttpEndpointSpec`；鉴权或定制异常映射放到
   `app/routes` 与 `app/services`。
5. 先测试 schema、Protocol 越权、拓扑、降级、回滚和停止，再写实现。

禁止静态导入其他模块的私有 Service/Repository、访问 `Kernel._entries`、让模块自行
创建或关闭共享 Provider，或把业务实现放进 `nexus_kernel`。

`ModuleContext.require_module()`、`optional_module()`、`require_provider()` 和
`optional_provider()` 都只能访问模块声明过的名字，未声明访问必须立即失败。模块通过
`get_worker_specs()` 返回本模块 WorkerSpec；Kernel 在构造时完成类型与重复名检查，
ApplicationRuntime 在 Kernel 启动后注册可运行模块的 Worker。

## Provider 与 Worker

Provider 只实现 `start`、`health_check`、`close`，由 app 装配 `ProviderSpec`。
必需 Provider 失败阻断启动；可选 Provider 自身失败为 `FAILED`，依赖它的 Module 在仍可
运行时为 `DEGRADED`。Worker 通过 WorkerSpec 注册，必须协作响应 stop event 和任务取消；
API 角色不注册或启动 Worker。状态 listener 不得抛出异常影响 Supervisor。

## 基础设施适配器

- Mongo 提供连接探测、DocumentStore ID CRUD、事务上下文和关闭。
- Redis 提供连接探测、KV/TTL、Stream、消费组与确认；消费循环必须传播取消。
- PostgreSQL 基于 asyncpg 提供连接池、execute/fetch 和事务；不包含 ORM 或迁移。
- Qdrant 提供连接探测、集合确保和 VectorStore CRUD；OTel 只负责通用追踪适配。

共享连接由 `app/providers` 按 `config/providers.yaml` 显式装配并交给 ProviderRegistry；
模块只声明并获取能力，不读取环境变量、不创建共享客户端、不负责关闭。

## AI 与工具

普通 completion 适配器实现 ModelClient；SDK 自己管理 agent loop、session 和工具执行时
实现 AgentRuntime。所有实现都必须归一化 ChatResponse/TokenUsage、设置超时、关闭
资源，并将 SDK 异常转换为不包含原始响应的稳定错误。

业务工具由 app 注册 AgentToolSpec。参数使用 Draft 2020-12 JSON Schema 完整校验；结果
必须是严格 JSON。默认单工具 10 秒、最多 4 轮、每请求最多 16 次 handler、单结果最多
65,536 UTF-8 bytes。同步 handler 在线程中运行，timeout 无法撤销线程已发生的副作用，
所以副作用工具必须幂等并接受可信 `AgentIdentity` 授权。请求体不再接受 `user_id`。

## 开发流程

先读取所有者、配置、直接调用方和测试；写一个会因缺少目标行为而失败的测试；完成最小
实现后运行聚焦测试，再执行 README 的完整门禁。环境需要创建或重建时只使用
`scripts/setup_python_env.sh` 或 `scripts/setup_python_env.ps1`。
开发与测试支持 Python 3.11-3.13；Ruff 和 mypy 保持以最低版本 3.11 为目标。
交付 wheel 时使用 `python -m build --wheel --outdir dist`，随后运行
`python scripts/verify_wheel.py dist`。校验器读取目标项目的 `pyproject.toml`，并核对
文件名、METADATA、WHEEL、RECORD、CRC、唯一 dist-info 与允许的顶层目录；不得跳过
旧包、未知顶层目录和敏感文件检查。构建前清理 `build`、`dist` 和 `*.egg-info`。

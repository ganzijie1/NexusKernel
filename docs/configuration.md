# 配置与环境

## 配置合同

`config/application.yaml` 只包含 Core 配置；`config/modules.yaml` 保存模块注册及
`modules.<name>.config` 私有配置；`config/providers.yaml` 保存 app 共享基础设施 Provider
的严格配置。YAML 禁止重复键，Core、Module 与 app Provider schema 均为 strict 且禁止
未知字段，未解析
`${VAR}`、无效类型和无效模块类都会阻止启动。

server 的响应规范化缓冲由 `NEXUS_KERNEL_MAX_RESPONSE_BUFFER_BYTES` 控制，默认
10485760 字节；超限请求返回稳定的 `response_too_large`。Module 清理预算由
`NEXUS_KERNEL_MODULE_CLEANUP_TIMEOUT_SECONDS` 控制，默认 5 秒；未协作结束的 Module
会保留依赖并等待显式重试，框架不会提前关闭其 Provider。

环境占位符支持 `${VAR}` 和 `${VAR:-default}`。凭据示例只能为空值。初始化器生成
`docker/.env.dev`、`.env.preprod`、`.env.prod`；POSIX 权限精确为 `0600`，Windows 验证
普通非链接文件，ACL 由部署平台限制。它们被 Git 和 Docker context 同时排除。初始化器
只接受且要求这三份 profile，未知或缺失示例在任何写入前
失败。Compose 命令必须用 `--env-file` 完成宿主侧插值，service 内的
`env_file` 继续负责容器注入。preprod/prod 的 `NEXUS_KERNEL_IMAGE` 必须显式设置。
环境变量默认前缀为 `NEXUS_KERNEL_`，初始化器会替换为新项目指定的前缀。

Chat 模块新增配置与默认值如下；AI 整体 timeout 覆盖排队、session、模型、工具与结果
归一化，并且必须小于 server request timeout。

| 配置键 | 环境变量 | 默认值 |
| --- | --- | ---: |
| `provider_id` | `NEXUS_KERNEL_AI_PROVIDER` | `openai_compatible` |
| `model` | `NEXUS_KERNEL_AI_MODEL` | `gpt-4.1-mini` |
| `service_bearer_token` | `NEXUS_KERNEL_SERVICE_BEARER_TOKEN` | 空 |
| `ai_api_key` | `NEXUS_KERNEL_AI_API_KEY` | 空 |
| `base_url` | `NEXUS_KERNEL_AI_BASE_URL` | 空 |
| `timeout_seconds` | `NEXUS_KERNEL_AI_TIMEOUT_SECONDS` | 25 |
| `tool_timeout_seconds` | `NEXUS_KERNEL_AI_TOOL_TIMEOUT_SECONDS` | 10 |
| `max_tool_rounds` | `NEXUS_KERNEL_AI_MAX_TOOL_ROUNDS` | 4 |
| `max_tool_calls` | `NEXUS_KERNEL_AI_MAX_TOOL_CALLS` | 16 |
| `max_tool_argument_bytes` | `NEXUS_KERNEL_AI_MAX_TOOL_ARGUMENT_BYTES` | 65536 |
| `max_tool_result_bytes` | `NEXUS_KERNEL_AI_MAX_TOOL_RESULT_BYTES` | 65536 |
| `adk_app_name` | `NEXUS_KERNEL_ADK_APP_NAME` | `nexus_kernel` |
| `adk_session_ttl_seconds` | `NEXUS_KERNEL_ADK_SESSION_TTL_SECONDS` | 1800 |
| `adk_session_max_entries` | `NEXUS_KERNEL_ADK_SESSION_MAX_ENTRIES` | 256 |
| `adk_session_max_events` | `NEXUS_KERNEL_ADK_SESSION_MAX_EVENTS` | 200 |
| `adk_session_cleanup_timeout_seconds` | `NEXUS_KERNEL_ADK_SESSION_CLEANUP_TIMEOUT_SECONDS` | 2 |

`adk_session_cleanup_timeout_seconds` 不得大于 AI 整体 timeout。选择 LiteLLM 时还必须在
进程启动前设置 SDK 自身环境键 `LITELLM_LOCAL_MODEL_COST_MAP=true`；它不是 YAML 配置键，
Docker、CI 与 env 示例负责提供，应用代码不会修改 `os.environ`。

Agent Runs 模块默认关闭。启用后，它以进程内有界队列运行服务端已注册的多智能体定义；
客户端只能选择 `agent_id`，不能提交模型、指令、拓扑、工具或 Python 类路径。配置如下：

| 配置键 | 环境变量 | 默认值 |
| --- | --- | ---: |
| 模块 `enabled` | `NEXUS_KERNEL_AGENT_RUNS_ENABLED` | `false` |
| `max_entries` | `NEXUS_KERNEL_AGENT_RUNS_MAX_ENTRIES` | `256` |
| `max_concurrent_phases` | `NEXUS_KERNEL_AGENT_RUNS_MAX_CONCURRENT_PHASES` | `4` |
| `phase_timeout_seconds` | `NEXUS_KERNEL_AGENT_RUNS_PHASE_TIMEOUT_SECONDS` | `300` |
| `overall_lifetime_seconds` | `NEXUS_KERNEL_AGENT_RUNS_OVERALL_LIFETIME_SECONDS` | `3600` |
| `action_timeout_seconds` | `NEXUS_KERNEL_AGENT_RUNS_ACTION_TIMEOUT_SECONDS` | `900` |
| `terminal_retention_seconds` | `NEXUS_KERNEL_AGENT_RUNS_TERMINAL_RETENTION_SECONDS` | `3600` |
| `max_resumes` | `NEXUS_KERNEL_AGENT_RUNS_MAX_RESUMES` | `16` |
| `max_payload_bytes` | `NEXUS_KERNEL_AGENT_RUNS_MAX_PAYLOAD_BYTES` | `65536` |
| `poll_interval_seconds` | `NEXUS_KERNEL_AGENT_RUNS_POLL_INTERVAL_SECONDS` | `1` |

`phase_timeout_seconds` 和 `action_timeout_seconds` 均不得超过整体寿命，并发数不得超过
队列容量。关闭排空复用 `action_timeout_seconds` 预算，从 API 进程停止接纳新 run 后开始
计算；预算耗尽才进入强制取消与清理。session、run、幂等记录和待恢复 action 都不持久化；首版必须单实例部署，或由
入口保证同一主体与 session 的粘性路由。进程重启、滚动替换和跨副本请求不能恢复旧 run。
`poll_interval_seconds` 控制进程内 TTL 清扫频率；HTTP 非终态轮询提示固定为
`Retry-After: 1`。

生产定义、工具、授权和可选可信身份解析器由
`app.agent_applications.registered_agent_runs()` 以代码注册；默认定义和工具为空，授权
fail-closed，因此 API 角色仅打开模块开关仍会 fail-fast。Worker 角色不会求值该 API-only
注册表，也不要求 Agent Runs 凭据。仓库示例只能由本地测试显式注入。HITL 还依赖
ADK 实验性的 `ResumabilityConfig`，只有模块开关开启且定义显式声明 `resumable=True` 时
才启用；升级 ADK 前必须先在 canary 验证恢复合同。

ADK 2.4 的内容捕获采用两层关闭：每次调用的 `TelemetryConfig` 禁止采集内容，进程环境还
必须设置 `ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS=false` 和
`OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=NO_CONTENT`。若显式设置
`ADK_TELEMETRY_IGNORE_RUN_CONFIG=true`，却没有同时保持这两项安全值，ADK Provider
必须拒绝启动。它们都是 SDK 环境键，不属于 YAML，应用代码不会修改全局环境。

## 依赖分组

`pyproject.toml` 是唯一依赖来源，不维护 `requirements.txt`。Core 不依赖外部基础设施；
`ai`、`mongo`、`redis`、`postgres`、`qdrant`、`otel`、`dev` 按需安装。新增可选适配器时，不要自动把其 extra 变成所有
环境必装项；只有运行制品明确启用该能力时才同步 Docker/CI 安装组。
`dev` 同时提供固定版本的构建前端和 Starlette 1.x TestClient 使用的 `httpx2`；wheel
构建前清理 `build/dist`，完成后执行内容校验。`ai` 固定 ADK 2.4 与直接使用的
Google Gen AI 2.12，Core 固定直接使用的 Starlette 1.3。

当前精确版本约束只固定项目的直接依赖，传递依赖仍由安装器解析，不等价于完整锁文件。
若以后要求可重复供应链，应从 `pyproject.toml` 生成按 Python/平台区分的 constraints 或
标准 lock；生成物不得成为第二个人工编辑入口。

共享 Provider 配置为空时表示未启用，也不会导入对应 SDK。配置存在但 extra 未安装时，
必需 Provider 启动失败并回滚，可选 Provider 进入 degraded。Mongo URI、Redis URL、
PostgreSQL DSN 与 Qdrant API key 按敏感值处理，不得进入日志、健康状态或错误文本。
`required=true` 与 `enabled=false` 的组合自相矛盾，会在 app 配置模型和 `ProviderSpec`
构造时同时拒绝；合法的可选 disabled Provider 不影响健康状态。

## Python 环境

| 系统 | 入口 | 目录 |
| --- | --- | --- |
| Windows PowerShell | `scripts/setup_python_env.ps1` | `.venv-win` |
| Windows Git Bash | `scripts/setup_python_env.sh` | `.venv-win` |
| Linux/WSL | `scripts/setup_python_env.sh` | `.venv-linux` |
| macOS | `scripts/setup_python_env.sh` | `.venv-macos` |

两套脚本都接受 Python 3.11-3.13，统一通过版本检查器验证 `>=3.11,<3.14`，同时验证
venv 的 `sys.prefix/base_prefix`，安全重建项目内白名单目录，并安装 `.[ai,dev]`。
Bash 在目标 venv 已存在且未指定 `--recreate` 时，先验证并直接复用其中的解释器，
无需 PATH 中存在全局 Python。首次创建和安全重建仍需通过 `--python` 指定或自动发现
受支持的全局解释器；损坏或归属错误的 venv 只提示使用 `--recreate`，不会直接删除。

## 初始化器

`scripts/init_project.sh` 只转交同一个 Python 初始化器。它完整校验参数与目标文件，
预生成所有写入，失败时恢复原字节，成功后写 `.nexus-kernel-initialized`。替换白名单
覆盖 README、项目元数据、配置、Docker/Compose/env 示例、GitLab CI、`AGENTS.md`、
`.codex/skills/**/SKILL.md` 和产品文档，
但永久保留 `nexus_kernel` 包名。

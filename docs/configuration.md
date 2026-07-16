# 配置与环境

## 配置合同

`config/application.yaml` 只包含 Core 配置；`config/modules.yaml` 保存模块注册及
`modules.<name>.config` 私有配置；`config/providers.yaml` 保存 app 共享基础设施 Provider
的严格配置。YAML 禁止重复键，Core、Module 与 app Provider schema 均为 strict 且禁止
未知字段，未解析
`${VAR}`、无效类型和无效模块类都会阻止启动。

环境占位符支持 `${VAR}` 和 `${VAR:-default}`。凭据示例只能为空值。初始化器生成
`docker/.env.dev`、`.env.preprod`、`.env.prod` 并保持 `0600`；它们被 Git 和 Docker
context 同时排除。Compose 命令必须用 `--env-file` 完成宿主侧插值，service 内的
`env_file` 继续负责容器注入。preprod/prod 的 `NEXUS_KERNEL_IMAGE` 必须显式设置。
环境变量默认前缀为 `NEXUS_KERNEL_`，初始化器会替换为新项目指定的前缀。

Chat 模块新增配置与默认值如下；AI 整体 timeout 覆盖排队、session、模型、工具与结果
归一化，并且必须小于 server request timeout。

| 配置键 | 环境变量 | 默认值 |
| --- | --- | ---: |
| `provider_id` | `NEXUS_KERNEL_AI_PROVIDER` | `openai_compatible` |
| `model` | `NEXUS_KERNEL_AI_MODEL` | `gpt-4.1-mini` |
| `ai_api_key` | `NEXUS_KERNEL_AI_API_KEY` | 空 |
| `base_url` | `NEXUS_KERNEL_AI_BASE_URL` | 空 |
| `timeout_seconds` | `NEXUS_KERNEL_AI_TIMEOUT_SECONDS` | 25 |
| `tool_timeout_seconds` | `NEXUS_KERNEL_AI_TOOL_TIMEOUT_SECONDS` | 10 |
| `max_tool_rounds` | `NEXUS_KERNEL_AI_MAX_TOOL_ROUNDS` | 4 |
| `max_tool_calls` | `NEXUS_KERNEL_AI_MAX_TOOL_CALLS` | 16 |
| `max_tool_result_bytes` | `NEXUS_KERNEL_AI_MAX_TOOL_RESULT_BYTES` | 65536 |
| `adk_app_name` | `NEXUS_KERNEL_ADK_APP_NAME` | `nexus_kernel` |
| `adk_session_ttl_seconds` | `NEXUS_KERNEL_ADK_SESSION_TTL_SECONDS` | 1800 |
| `adk_session_max_entries` | `NEXUS_KERNEL_ADK_SESSION_MAX_ENTRIES` | 256 |
| `adk_session_max_events` | `NEXUS_KERNEL_ADK_SESSION_MAX_EVENTS` | 200 |

## 依赖分组

`pyproject.toml` 是唯一依赖来源，不维护 `requirements.txt`。Core 不依赖外部基础设施；
`ai`、`mongo`、`redis`、`postgres`、`qdrant`、`otel`、`dev` 按需安装。新增可选适配器时，不要自动把其 extra 变成所有
环境必装项；只有运行制品明确启用该能力时才同步 Docker/CI 安装组。
`dev` 同时提供固定版本的构建前端；wheel 构建前清理 `build/dist`，完成后执行内容校验。

当前精确版本约束只固定项目的直接依赖，传递依赖仍由安装器解析，不等价于完整锁文件。
若以后要求可重复供应链，应从 `pyproject.toml` 生成按 Python/平台区分的 constraints 或
标准 lock；生成物不得成为第二个人工编辑入口。

共享 Provider 配置为空时表示未启用，也不会导入对应 SDK。配置存在但 extra 未安装时，
必需 Provider 启动失败并回滚，可选 Provider 进入 degraded。Mongo URI、Redis URL、
PostgreSQL DSN 与 Qdrant API key 按敏感值处理，不得进入日志、健康状态或错误文本。

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
覆盖 README、项目元数据、配置、Docker/Compose/env 示例、GitLab CI 和产品文档，
但永久保留 `nexus_kernel` 包名。

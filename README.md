# NexusKernel Starter

NexusKernel 是一个支持 Python 3.11-3.13 的模块化 AI 服务模板。可复用框架位于
`nexus_kernel/`，项目装配、鉴权和业务模块位于 `app/`；初始化新项目后，
Python 导入包名 `nexus_kernel` 永久保留。

## 框架结构

```text
NexusKernel/
├── nexus_kernel/              # 与业务无关的可复用框架
│   ├── ai/                    # ModelClient、AgentRuntime、Tool 与 usage
│   ├── config/                # 严格 YAML、环境变量和 Core schema
│   ├── gateway/               # FastAPI factory、中间件、envelope 与健康端点
│   ├── infrastructure/        # Mongo、Redis、PostgreSQL、Qdrant、OTel 可选适配器
│   ├── kernel/                # Module、拓扑、Protocol 代理与生命周期
│   ├── observability/         # 结构化日志和 Prometheus 指标
│   ├── providers/             # 外部资源生命周期、健康、降级和关闭
│   └── runtime/               # 请求上下文、EventBus 与 WorkerSupervisor
├── app/                       # 当前项目的装配与业务所有者
│   ├── modules/               # 独立业务模块
│   ├── providers/             # 共享 Provider 的严格配置与显式工厂
│   ├── routes/                # 鉴权或自定义异常映射路由
│   ├── services/              # 跨路由应用服务
│   ├── bootstrap.py           # 配置、Provider、Kernel、Worker 总装配
│   └── launcher.py            # API/Worker 同镜像入口
├── config/                    # application、modules、providers 配置
├── docker/                    # Dockerfile、Compose 与脱敏 env 示例
├── scripts/                   # 环境、初始化、安全和 wheel 校验脚本
└── tests/                     # 架构、合同、单元、部署与 smoke 测试
```

唯一允许的顶层依赖方向是 `app -> nexus_kernel`。Gateway 不依赖 AI 或 app，Provider
不依赖 Kernel，模块之间不静态导入彼此的私有 Service 或 Repository。业务、鉴权、
共享资源装配和部署选择归 `app`；框架只提供可复用合同与生命周期。

## 模块文件规范

```text
app/modules/<module_name>/
├── __init__.py
├── module.py
├── api/                 # 按需：contracts.py、schemas.py、endpoints.py、agent_tools.py
├── services/            # 按需：service.py 或按职责拆分
├── repositories.py      # 按需
├── workers.py           # 按需
└── data/                # 按需静态资源
```

- `module.py` 只放严格配置、生命周期和对象装配，不承载业务流程或持久化实现。
- `api/contracts.py` 只定义对其他模块开放的 Protocol；schemas 与 endpoint 声明归 `api/`。
- 业务规则归 `services/`，存储访问归 `repositories.py`，协作式后台任务归 `workers.py`。
- 简单模块不创建空目录；目录名、`Module.name` 与 `config/modules.yaml` 键必须一致。
- 每个 Module 用 `config_schema` 声明继承 `ModuleConfig` 的 strict/frozen 配置模型；配置只
  验证一次，生命周期只接收受限的 `ModuleContext`。
- `ModuleContext.require_module()`、`optional_module()`、`require_provider()` 和
  `optional_provider()` 只能访问 Module 预先声明的白名单；Module 不持有 Kernel。
- 注册期可以取得处于 `REGISTERED` 的依赖代理用于装配，但代理只有在目标进入
  `READY/DEGRADED` 后才能调用；提前调用会被拒绝。
- `Module.get_worker_specs()` 只返回本模块 WorkerSpec；Kernel 启动后只汇总
  `READY/DEGRADED` 模块的 Worker。
- 新模块类路径固定为 `app.modules.<module_name>.module.<ModuleClass>`；先写失败测试，再注册。

## 初始化项目

在新的模板副本中执行一次：

```bash
bash scripts/init_project.sh --name "My Service" --slug my-service --env-prefix MY_SERVICE --port 8030
```

初始化器先完整校验，再原子替换项目名、发行名、环境变量前缀、端口、Compose
标识和中文文档；它要求 dev、preprod、prod 三份 env 示例恰好齐全，在任何写入前拒绝
未知或缺失 profile，生成被 Git 忽略的本地 Docker env，并拒绝二次执行。

## Python 环境

`pyproject.toml` 是唯一依赖来源，项目不维护 `requirements.txt`，避免两份版本清单
漂移。Core 只安装基础 Web 能力；`ai`、`mongo`、`redis`、`postgres`、`qdrant`、
`otel`、`dev` 通过 extra 按需安装。精确直接依赖不等于传递依赖锁定；需要完全可重复
构建时应另行引入生成型 constraints 或标准 lock 文件。

`ai` 当前固定 Google ADK 2.4 与 Google Gen AI 2.12；运行时使用 ADK `App + Runner`，
同时提供兼容的同步 Chat 和默认关闭的 Graph/Task 多智能体 Agent Runs。Core 直接固定
Starlette 1.3，开发测试通过 `dev` extra 安装 httpx2。

Bash 支持 Windows Git Bash、Linux/WSL 和
macOS，分别创建 `.venv-win`、`.venv-linux`、`.venv-macos`：

```bash
bash scripts/setup_python_env.sh
```

Windows 也可使用完整 PowerShell 入口：

```powershell
.\scripts\setup_python_env.ps1
```

需要重建时分别使用 `--recreate` 或 `-Recreate`。
项目要求 `>=3.11,<3.14`；Python 3.14 暂不支持，因为固定的 LiteLLM 版本要求 `<3.14`。
已有且有效的 venv 会优先使用其中的解释器，复用时无需 PATH 中存在全局 Python；首次
创建或使用重建参数时，仍必须提供或发现受支持的全局解释器。

开发环境默认安装 `.[ai,dev]`；使用基础设施适配器时按能力安装，例如：

```bash
# Linux/WSL
.venv-linux/bin/python -m pip install --editable ".[mongo,redis,postgres,qdrant,otel]"

# macOS
.venv-macos/bin/python -m pip install --editable ".[mongo,redis,postgres,qdrant,otel]"
```

```powershell
# Windows PowerShell
.\.venv-win\Scripts\python.exe -m pip install --editable ".[mongo,redis,postgres,qdrant,otel]"
```

Docker 默认只包含 `ai`。构建需要基础设施驱动的镜像时，通过受控 build arg 指定：

```bash
docker build -f docker/Dockerfile \
  --build-arg NEXUS_KERNEL_RUNTIME_EXTRAS=ai,mongo,redis,postgres,qdrant,otel \
  -t nexus-kernel-starter:full .
```

## 启动

无需外部服务或 AI 凭据即可启动。Linux/WSL、macOS、Windows PowerShell 分别执行：

```bash
.venv-linux/bin/python -m app.launcher
```

```bash
.venv-macos/bin/python -m app.launcher
```

```powershell
& .\.venv-win\Scripts\python.exe -m app.launcher
```

默认地址为 `http://127.0.0.1:8030`，提供：

- `GET /health/live`
- `GET /health/ready`
- `GET /metrics`
- `POST /api/v1/example/echo`
- `POST /api/v1/chat`

设置 `NEXUS_KERNEL_AGENT_RUNS_ENABLED=true` 并在 app 注册多智能体定义后，还会提供：

- `POST /api/v1/agents/{agent_id}/runs`
- `GET /api/v1/agent-runs/{run_id}`
- `POST /api/v1/agent-runs/{run_id}/resume`
- `DELETE /api/v1/agent-runs/{run_id}`
- `DELETE /api/v1/agents/{agent_id}/sessions/{session_id}`

标准入口从 `app.agent_applications.registered_agent_runs()` 读取生产定义、工具、授权和
可选可信身份解析器；该注册表只在 API 角色且 Agent Runs 模块启用时求值，Worker 不加载
API-only 定义或要求对应凭据。定义与工具默认为空且授权 fail-closed，仓库示例不会被自动加载。

无 AI 凭据时进程保持可用，readiness 返回 HTTP 200 且状态为 `degraded`。
Chat 未配置服务 Token 时返回 `503 auth_not_configured`；Token 缺失或错误返回
`401 unauthorized`；Token 正确但 AI 不可用返回 `503 ai_provider_unavailable`。
请求体不接受 `user_id`；可信主体由 app 的 `TrustedIdentityResolver` 从 Request 解析。
未提供 `session_id` 的调用使用请求结束即删除的临时会话。
可信主体 ID 与 `session_id` 都只接受
`[A-Za-z0-9][A-Za-z0-9._:-]{0,127}`。ModelClient 与 AgentRuntime 两条分派路径都会把
resolver 产生的同一 `AgentIdentity` 传入服务端工具；身份不会进入模型消息、Schema 或
工具参数。选择 LiteLLM 时，进程启动前必须设置
`LITELLM_LOCAL_MODEL_COST_MAP=true`。Google ADK 还要求
`ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS=false` 与
`OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=NO_CONTENT`；代码不会修改全局环境。

Agent Runs 使用 REST 创建、轮询、恢复和取消；客户端只能选择服务端注册的 `agent_id`。
run、session、幂等记录和待恢复 action 目前只在进程内保存，因此启用后必须单实例部署或
使用粘性路由，进程重启不能恢复旧 run。完整字段与状态合同见 [API 文档](docs/api.md)，
容量和 timeout 默认值见 [配置文档](docs/configuration.md)。进程关闭先停止创建新 run，
在默认 900 秒预算内允许既有轮询、恢复与取消继续；预算耗尽后才强制取消并等待清理收敛。
排空期间 readiness 的 `agent_runs.lifecycle` 为 `draining`，总体状态为 `degraded`。

`GET /metrics` 还公开 Agent Runs 的队列、运行 phase、待 action、八态分布、终态、phase
时延、容量拒绝与清理债务。标签只接受固定状态和结果，不包含 principal、run 或 session ID。

服务端响应在最终 body 生成前受统一 deadline 和默认 10 MiB 规范化缓冲限制；超限返回
`500 response_too_large`。最终 body 提交后才运行 BackgroundTask，其失败只记录稳定事件，
不会把已经成功的响应改写为 504。

## 验证

Linux/WSL 将下列 `PYTHON` 设为 `.venv-linux/bin/python`，macOS 设为
`.venv-macos/bin/python`：

```bash
PYTHON=.venv-linux/bin/python
"$PYTHON" -m compileall -q nexus_kernel app scripts
"$PYTHON" -m pytest -m "not real_llm" -q
"$PYTHON" -m ruff format --check .
"$PYTHON" -m ruff check .
"$PYTHON" -m ruff check --select C901 nexus_kernel app scripts
"$PYTHON" -m mypy nexus_kernel app scripts/init_project.py scripts/check_python_version.py scripts/check_text_contract.py scripts/delivery_contract.py scripts/security_scan.py scripts/verify_wheel.py
"$PYTHON" -m pip check
"$PYTHON" scripts/security_scan.py
"$PYTHON" scripts/check_text_contract.py
rm -rf build dist ./*.egg-info
"$PYTHON" -m build --wheel --outdir dist
"$PYTHON" scripts/verify_wheel.py dist
docker compose --env-file docker/.env.dev -f docker/docker-compose-dev.yml config --quiet
```

Windows PowerShell 使用相同模块命令：

```powershell
$Python = ".\.venv-win\Scripts\python.exe"
& $Python -m compileall -q nexus_kernel app scripts
& $Python -m pytest -m "not real_llm" -q
& $Python -m ruff format --check .
& $Python -m ruff check .
& $Python -m ruff check --select C901 nexus_kernel app scripts
& $Python -m mypy nexus_kernel app scripts\init_project.py scripts\check_python_version.py scripts\check_text_contract.py scripts\delivery_contract.py scripts\security_scan.py scripts\verify_wheel.py
& $Python -m pip check
& $Python scripts\security_scan.py
& $Python scripts\check_text_contract.py
Remove-Item -Recurse -Force build, dist, *.egg-info -ErrorAction SilentlyContinue
& $Python -m build --wheel --outdir dist
& $Python scripts\verify_wheel.py dist
docker compose --env-file docker/.env.dev -f docker/docker-compose-dev.yml config --quiet
```

wheel 校验器从目标项目的 `pyproject.toml` 读取发行名、版本和 Python 要求，并严格核验
METADATA/WHEEL 单值字段、Tag 与文件名、RECORD 摘要/大小及 CRC；文件名含 build tag 时
`WHEEL` 必须有且仅有一个匹配的 `Build`，否则必须省略。因此初始化后的项目仍
使用同一命令。CI 已配置 Python 3.11、3.12、3.13 矩阵；只有对应 Pipeline
实际通过后，才能将 3.12/3.13 标记为已验证。

继续阅读：[架构](docs/architecture.md)、[开发扩展](docs/development.md)、
[配置与环境](docs/configuration.md)、[API 契约](docs/api.md)、
[安全](docs/security.md)、[提取核查](docs/extraction-audit.md) 和
[容器运行](docker/README.md)。从 0.1 升级时先阅读 [0.2.0 迁移指南](docs/migration-0.2.md)。

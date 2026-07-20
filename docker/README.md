# 容器运行

API 与 Worker 使用同一 `nexus-kernel-starter` 镜像，通过
`NEXUS_KERNEL_RUNTIME_ROLE=api|worker` 切换角色。镜像为多阶段构建，最终以 UID/GID
10001 的 `nexus_kernel` 非 root 用户运行。
项目支持 Python 3.11-3.13；容器继续使用 Python 3.11 作为最低生产运行基线。

```bash
docker build -f docker/Dockerfile -t nexus-kernel-starter:local .
docker build -f docker/Dockerfile --build-arg NEXUS_KERNEL_RUNTIME_EXTRAS=ai,mongo,redis,postgres,qdrant,otel -t nexus-kernel-starter:full .
docker compose --env-file docker/.env.dev -f docker/docker-compose-dev.yml up --build api
docker compose --env-file docker/.env.dev -f docker/docker-compose-dev.yml --profile worker up worker
docker compose --env-file docker/.env.dev -f docker/docker-compose-dev.yml --profile infra up mongo redis
```

受限网络可把既有 pip 配置作为 BuildKit secret 临时挂载，配置只对 builder 的 wheel
下载步骤可见，不进入构建上下文、镜像层或最终镜像：

```bash
docker build --secret id=pip_config,src=/path/to/pip.conf -f docker/Dockerfile -t nexus-kernel-starter:local .
```

`pip_config` 可省略；省略时使用 pip 默认索引。不得通过 `--build-arg` 传递带凭据的索引
URL，也不得把真实 pip 配置复制进仓库或镜像。

Dockerfile 独立构建默认只安装 `ai` extra；dev Compose 默认构建 `ai,mongo,redis`，因此
启用 infra profile 时镜像包含对应客户端。`NEXUS_KERNEL_RUNTIME_EXTRAS` 只接受
`ai,mongo,redis,postgres,qdrant,otel` 的白名单组合；启动 Compose `infra` profile 只会
创建 Mongo/Redis 服务，其他 Provider 仍需在构建参数中显式包含对应 extra。
dev 基础设施镜像固定到经过多架构 OCI index 核验的
`mongo:8.0@sha256:3ce3de7f40e914034b03b7dec654005ab54f7dc8306937e44ec6760d9e9409a1`
和
`redis:8.0-alpine@sha256:5f61955be8ab2ccee9372b84ae4d4da2e2b156f87281e3f218544055e7ee04d4`。
升级 tag 时必须重新核验多架构 index digest，并同步 Compose 与部署合同测试，不能只改 tag。
构建阶段使用由 `pyproject.toml` 生成的 `constraints/py311-313.txt` 固定 Python 3.11-3.13
和全部 extras 的传递依赖。Linux 环境运行
`scripts/compile_constraints.sh` 更新生成物，CI 使用 `--check` 拒绝漂移；该文件不得手工
维护。CI 同时记录本地镜像内容 digest 与字节数，默认镜像上限为 475 MiB，full 镜像上限
为 640 MiB。
Dockerfile、GitLab Python 矩阵和 Docker CLI/DinD 都使用 `tag@sha256` 固定 OCI index；
升级 tag 时必须先用 `docker buildx imagetools inspect` 验证 tag 与 digest 指向同一 index，
再同步 Dockerfile、CI 和部署合同测试。container smoke 总预算为 15 分钟，单次 `wget`
健康请求最多 2 秒且只尝试一次，外层循环负责有限重试和失败日志。

真实 `docker/.env.*` 由初始化器从空凭据示例生成且不会提交；POSIX 权限精确为 `0600`，
Windows 只验证普通非链接文件，NTFS ACL 由部署平台限制。
`--env-file` 负责 Compose 插值，service 的 `env_file` 负责容器注入，二者不可互相替代。
三份 profile 必须恰好为 dev、preprod、prod。选择 LiteLLM 时，镜像和 env 示例会在进程
启动前提供 `LITELLM_LOCAL_MODEL_COST_MAP=true`。Google ADK 同时使用
`ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS=false` 与
`OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=NO_CONTENT` 关闭 span 内容采集；应用
代码不会修改容器环境。
dev 可通过 profile 启动 Mongo/Redis；preprod/prod 要求非空 `NEXUS_KERNEL_IMAGE`，只连接
外部依赖，不定义本地 Mongo、Redis、volumes、固定 IP、静态子网或外部共享网络。
不带 `--profile worker` 时只启动 API：

```bash
docker compose --env-file docker/.env.preprod -f docker/docker-compose-preprod.yml up -d
docker compose --env-file docker/.env.prod -f docker/docker-compose-prod.yml up -d
```

需要同时启动 API 与 Worker 时必须显式启用 worker profile：

```bash
docker compose --env-file docker/.env.preprod -f docker/docker-compose-preprod.yml --profile worker up -d
docker compose --env-file docker/.env.prod -f docker/docker-compose-prod.yml --profile worker up -d
```

preprod/prod 的 API 与 Worker 都显式使用 `user: "10001:10001"`，因此自定义
`NEXUS_KERNEL_IMAGE` 必须包含 UID/GID 10001。三套 API healthcheck 都只探测 live，使用
`urlopen(..., timeout=2)`、Compose `timeout: 2s` 和 5 次有限重试；网络等待与 Compose
单次探针预算保持一致。

Liveness 只表示进程存活。按模板合同，readiness 在可选 AI 不可用时仍返回 HTTP 200，
但 JSON 状态为 `degraded`；监控必须同时检查状态字段，不能只看退出码。Worker smoke
只证明角色入口与进程生命周期，实际业务 Worker 需另加状态或任务级验证。

CI 在 Python 3.11-3.13 上执行非真实 LLM 测试，并在 wheel 构建后安装包含全部 runtime
extras 的成品、执行 SDK import smoke 与 `pip check`；随后分别验证 default/full 镜像的
UID/GID 10001、SDK 隔离、`no-new-privileges`、只读根文件系统、API live/ready JSON、Worker
持续运行和优雅退出。失败时只采集安全日志再清理容器。CI 不推送
镜像、不部署。当前 3.12/3.13 状态为“已配置、待执行验证”；只有 GitLab 对应矩阵任务
完成依赖安装、全部非真实 LLM 测试和 `pip check` 后才能声明兼容性已验证。真实 LLM
smoke 仅在显式变量和 CI Secret 存在时运行。机器没有 Docker CLI 或 daemon 时，只能声明静态
Compose/Docker 合同结果，并把构建、非 root、API/Worker 启动留作未验证项。

`agent_runs` 默认关闭。首版 run、session、幂等记录和 HITL 恢复状态只保存在 API 进程
内；启用时必须将 API 固定为单实例，或配置能让同一主体和 session 始终到达同一副本的
粘性路由。Worker 角色不加载 API-only Agent 注册表。滚动替换前先停止接纳，在默认 900 秒
内排空非终态 run；超时才强制取消，且必须确认清理债务归零后替换实例，不能依赖跨进程恢复。

真实 provider smoke 是显式 Secret 驱动的发布前门禁，覆盖 Chat、Graph sequence/route/
fan-out/Join、Team clarification/continuation 和 HITL pause/resume；本地非真实测试或静态 CI
配置不表示该 smoke 已执行。单实例粘性 canary 只向内部 allowlist 开放，至少运行 60 分钟
并完成 30 个 run；framework 错误、敏感信息泄漏、串会话、卡死和 cleanup debt 必须为 0，
P95 小于 phase deadline 的 80%，队列在一个 phase deadline 内归零。回退顺序是停止接纳、
最多排空 900 秒、强制取消残余 run、确认清理收敛、关闭功能开关、替换实例。

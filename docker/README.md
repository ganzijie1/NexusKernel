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

Dockerfile 独立构建默认只安装 `ai` extra；dev Compose 默认构建 `ai,mongo,redis`，因此
启用 infra profile 时镜像包含对应客户端。`NEXUS_KERNEL_RUNTIME_EXTRAS` 只接受
`ai,mongo,redis,postgres,qdrant,otel` 的白名单组合；启动 Compose `infra` profile 只会
创建 Mongo/Redis 服务，其他 Provider 仍需在构建参数中显式包含对应 extra。

真实 `docker/.env.*` 由初始化器从空凭据示例生成、权限为 `0600` 且不会提交。
`--env-file` 负责 Compose 插值，service 的 `env_file` 负责容器注入，二者不可互相替代。
dev 可通过 profile 启动 Mongo/Redis；preprod/prod 要求非空 `NEXUS_KERNEL_IMAGE`，只连接
外部依赖，不定义本地 Mongo、Redis、volumes、固定 IP、静态子网或外部共享网络：

```bash
docker compose --env-file docker/.env.preprod -f docker/docker-compose-preprod.yml up -d
docker compose --env-file docker/.env.prod -f docker/docker-compose-prod.yml up -d
```

Liveness 只表示进程存活。按模板合同，readiness 在可选 AI 不可用时仍返回 HTTP 200，
但 JSON 状态为 `degraded`；监控必须同时检查状态字段，不能只看退出码。Worker smoke
只证明角色入口与进程生命周期，实际业务 Worker 需另加状态或任务级验证。

CI 在 Python 3.11-3.13 上执行非真实 LLM 测试，并在 wheel 构建后安装包含全部 runtime
extras 的成品、执行 SDK import smoke 与 `pip check`；随后执行容器 smoke。CI 不推送
镜像、不部署。当前 3.12/3.13 状态为“已配置、待执行验证”；只有 GitLab 对应矩阵任务
完成依赖安装、全部非真实 LLM 测试和 `pip check` 后才能声明兼容性已验证。真实 LLM
smoke 仅在显式变量和 CI Secret 存在时运行。机器没有 Docker CLI 或 daemon 时，只能声明静态
Compose/Docker 合同结果，并把构建、非 root、API/Worker 启动留作未验证项。

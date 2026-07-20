---
name: nexus-kernel-config-and-environment
description: Use when changing or reviewing NexusKernel YAML, environment variables, dependency extras, module registration, project initialization, Python environments, or config-driven startup.
---

# NexusKernel 配置与环境

先阅读[配置与环境](../../../docs/configuration.md)和[安全合同](../../../docs/security.md)。
项目支持 Python 3.11-3.13；环境入口必须统一执行项目版本检查器。

1. 搜索 key 的 schema、YAML、env 示例、Docker、CI、文档、初始化器和测试消费者。
2. Core 字段放 `application.yaml`；模块私有字段放 `modules.<name>.config` 并由 Module 的
   strict/frozen `config_schema` 唯一验证一次。模块的 required/optional 依赖同时写入
   配置清单与 Module 类，清单用于导入前拓扑，两处按集合严格一致。
3. Mongo、Redis、PostgreSQL、Qdrant 等共享 Provider 放 `config/providers.yaml`，由 `app`
   的严格 schema 和显式工厂装配；不接受任意类路径。空配置表示禁用。Module 的必需与
   可选 Provider 声明必须互斥，名称遵循 `[a-z][a-z0-9_]*`。
4. 空凭据规范化为未配置；重复键、未知字段、未解析变量、非法类型和类路径必须 fail-fast。
5. 真实凭据只来自运行环境，示例保持空值，错误与健康状态只输出稳定代码。初始化器生成
   的 Docker env 只允许 dev、preprod、prod 三份且被 Git/Docker 双重排除；POSIX 权限
   必须精确 `0600`，Windows 只验证普通非链接文件，ACL 由部署平台控制。
6. LiteLLM 的 `LITELLM_LOCAL_MODEL_COST_MAP=true` 是 SDK 进程环境键，不属于 YAML；同步
   Docker、CI 和三份 env 示例，代码不得修改 `os.environ`。

server 响应缓冲、runtime Module 清理预算和 Chat 工具参数大小分别归
`server.max_response_buffer_bytes`、`runtime.module_cleanup_timeout_seconds` 与
`modules.chat.config.max_tool_argument_bytes`；新增或修改时必须同步三份 env 示例和消费
矩阵。Module 清理预算不等价于强制终止，未收敛时必须保留 Provider。
Redis Stream 的 `block_milliseconds` 和非空 `ttl_seconds` 只接受严格正整数，必须在任何
client I/O 前拒绝 bool、float、零与负数。

`pyproject.toml` 是唯一依赖来源。新增可选适配器时保持独立 extra；只有运行镜像或 CI
明确启用该能力时才增加安装组，不要把可选依赖自动变成全局基线。PostgreSQL 使用
`postgres` extra 和 asyncpg；不得新增或手工维护 `requirements.txt` 作为第二依赖入口。
传递依赖使用项目固定的 uv 从 `pyproject.toml` 生成
`constraints/py311-313.txt`；Linux 环境、Docker 和 CI 消费同一生成物并检查漂移，禁止
手工编辑 constraints。

创建或重建环境只能使用 `scripts/setup_python_env.sh` 或
`scripts/setup_python_env.ps1`。修改初始化器时必须在完整临时副本验证参数、原子回滚、
二次执行拒绝、env 生成、中文文档替换和永久 `nexus_kernel` 包名。
初始化器必须在任何写入前拒绝未知或缺失 env profile；替换白名单包含 `AGENTS.md` 与
`.codex/skills/**/SKILL.md`，测试名称、前缀和端口从项目合同派生，不批量改写测试源码。
有效 venv 的 Bash 复用无需全局 Python；首次创建、`--recreate` 和损坏环境恢复仍必须
验证受支持的全局解释器。初始化后继续从目标 `pyproject.toml` 建立 wheel 校验合同。
挂载盘环境不可作为发布证据时，在 `/tmp` 隔离副本运行同一 setup 脚本，不得改用手工
venv 命令或其他环境目录约定。

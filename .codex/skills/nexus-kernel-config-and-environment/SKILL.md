---
name: nexus-kernel-config-and-environment
description: Use when changing or reviewing NexusKernel YAML, environment variables, dependency extras, module registration, project initialization, Python environments, or config-driven startup.
---

# NexusKernel 配置与环境

先阅读[配置与环境](../../../docs/configuration.md)和[安全合同](../../../docs/security.md)。
项目支持 Python 3.11-3.13；环境入口必须统一执行项目版本检查器。

1. 搜索 key 的 schema、YAML、env 示例、Docker、CI、文档、初始化器和测试消费者。
2. Core 字段放 `application.yaml`；模块私有字段放 `modules.<name>.config` 并由 Module 的
   strict/frozen `config_schema` 唯一验证一次。
3. Mongo、Redis、PostgreSQL、Qdrant 等共享 Provider 放 `config/providers.yaml`，由 `app`
   的严格 schema 和显式工厂装配；不接受任意类路径。空配置表示禁用。
4. 空凭据规范化为未配置；重复键、未知字段、未解析变量、非法类型和类路径必须 fail-fast。
5. 真实凭据只来自运行环境，示例保持空值，错误与健康状态只输出稳定代码。初始化器生成
   的三份 Docker env 必须保持 `0600` 且被 Git/Docker 双重排除。

`pyproject.toml` 是唯一依赖来源。新增可选适配器时保持独立 extra；只有运行镜像或 CI
明确启用该能力时才增加安装组，不要把可选依赖自动变成全局基线。PostgreSQL 使用
`postgres` extra 和 asyncpg；不得新增或手工维护 `requirements.txt` 作为第二依赖入口。

创建或重建环境只能使用 `scripts/setup_python_env.sh` 或
`scripts/setup_python_env.ps1`。修改初始化器时必须在完整临时副本验证参数、原子回滚、
二次执行拒绝、env 生成、中文文档替换和永久 `nexus_kernel` 包名。
有效 venv 的 Bash 复用无需全局 Python；首次创建、`--recreate` 和损坏环境恢复仍必须
验证受支持的全局解释器。初始化后继续从目标 `pyproject.toml` 建立 wheel 校验合同。

# 项目引导与模板工具历史实施计划

状态：已完成。当前使用方式以根目录 [README](../../../README.md) 和
[配置文档](../../configuration.md)为准。

## 已完成内容

- `pyproject.toml` 固定 Python 3.11-3.13 支持范围、精确依赖和 `ai/mongo/redis/qdrant/otel/dev` 分组。
- `scripts/setup_python_env.sh` 与 `scripts/setup_python_env.ps1` 创建系统对应的项目 venv，
  校验 Python 版本与 venv 所有权，并安装 `.[ai,dev]`。
- `scripts/init_project.sh` 转交同一个 Python 初始化器，覆盖参数校验、白名单替换、env
  生成、初始化 marker、并发锁、失败回滚和二次执行拒绝。
- Git/Docker 排除规则覆盖 Windows、Linux、macOS venv、真实 env、缓存与构建输出。
- 测试覆盖依赖清单、脚本语法与真实环境、原子初始化、安全路径、权限与完整模板副本。

## 当前技术标识

Python 包为 `nexus_kernel`，发行与 Docker 标识为 `nexus-kernel-starter`，环境变量为
`NEXUS_KERNEL_*`，初始化 marker 为 `.nexus-kernel-initialized`。不提供旧标识兼容层。

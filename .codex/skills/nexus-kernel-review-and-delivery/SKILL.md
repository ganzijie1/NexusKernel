---
name: nexus-kernel-review-and-delivery
description: Use when reviewing NexusKernel diffs, Docker and Compose changes, GitLab CI, health checks, logs, security contracts, documentation, packaging, or release readiness.
---

# NexusKernel 审查与交付

先阅读[容器运行](../../../docker/README.md)、[API 契约](../../../docs/api.md)和
[安全合同](../../../docs/security.md)。审查时只报告有失败机制的 finding。
项目支持 Python 3.11-3.13；交付检查必须覆盖三个版本的非真实 LLM 测试矩阵。

1. 确认 diff、未跟踪文件和技术标识完整，再检查 pyproject、镜像、入口和配置链。
2. 核对 API/Worker 同镜像、非 root、role、env 示例、Compose profile 和无固定网络。
   Dockerfile 默认保持 `ai`，dev Compose 默认 `ai,mongo,redis`；preprod/prod 必须使用显式
   外部镜像且不定义本地基础设施。构建参数 `<ENV_PREFIX>_RUNTIME_EXTRAS` 只能使用项目
   白名单，所有 Compose 命令使用对应 `--env-file`。
3. 核对 live/ready：模板允许 HTTP 200 degraded，监控必须读取 JSON 状态。
4. 检查 CI 轮询有总超时和失败日志，失败后清理；真实 LLM 只由显式 Secret 触发。
5. 检查日志配置已接入且不输出凭据、Prompt、工具载荷、请求体或模型原始响应。
6. 同步 README、Docker、架构、配置、API、安全文档和项目技能。

运行静态解析、compileall、pytest、Ruff（含复杂度）、mypy、pip check、wheel、security scan、
完整索引文本合同和初始化副本测试；工作树另执行 `git diff --check` 与 EOL 检查。测试文件
必须非空且可由 AST 解析，关键 Gateway 合同名必须存在。Docker 可用时分别验证
default/full 的 Compose、build、UID/GID 10001、
`pip check`、SDK 隔离、`no-new-privileges`、只读根文件系统及 API/Worker 双角色；Docker 不可用时
明确写“容器运行未验证”，不能把静态检查描述为镜像可运行。
wheel 必须从目标 `pyproject.toml` 校验文件名、版本、Requires-Python、全部 Provides-Extra、
对应 `Requires-Dist`、METADATA/WHEEL 单值字段、值不重复且匹配文件名的 Tag 集合、RECORD 摘要/大小、
CRC 和顶层目录；文件名 build tag 与 `WHEEL` 的单值 `Build` 必须双向一致，无 tag 时必须
省略该字段。仅能解压不代表制品合格。成品
安装矩阵必须安装全部运行时 extras，执行关键 SDK import smoke 与 `pip check`。Python
3.12/3.13 在 GitLab 矩阵完成前只标记为“已配置、待执行验证”，不得以静态配置代替结果。

Findings 按 P0-P3 排序，包含文件行、触发条件、失败机制、影响和修复方向；没有 finding
时列出未执行验证与剩余风险。

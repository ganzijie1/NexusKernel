# NexusKernel 模板初始设计记录

状态：已实施，并由本轮通用化与技术标识迁移补充。

## 初始目标

建立支持 Python 3.11-3.13 的模块化 AI 服务模板，以 `nexus_kernel/` 作为永久框架包，`app/`
作为项目装配与业务层。依赖仅来自 `pyproject.toml`，模板初始化必须一次性、原子且不
复制源项目凭据。

## 固定决策

- 展示名称为 `NexusKernel Starter`，发行名为 `nexus-kernel-starter`，默认环境前缀为
  `NEXUS_KERNEL`，默认端口为 8030。
- Bash 环境入口支持 Windows/Linux/macOS；Windows 另有完整 PowerShell 入口。
- 初始化器只替换白名单项目元数据、配置、Docker/CI 和产品文档，永久保留
  `nexus_kernel`。
- 真实 env、venv、构建输出、缓存与日志不进入 Git 或 Docker context。

## 后续实现

Kernel、Provider、Gateway、AI、运行时、可选基础设施、Docker 和 CI 已按
[架构文档](../../architecture.md)实现；本记录仅说明最初的模板与初始化决策。

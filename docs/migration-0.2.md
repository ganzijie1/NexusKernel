# 0.2.0 迁移指南

0.2.0 是破坏性升级，不提供旧无下划线包名、旧 Module 生命周期或
`ChatRequest.user_id` 兼容层。Python 3.11-3.13 继续受支持。

## 命名空间

把所有旧无下划线包导入改为 `nexus_kernel`，并确保旧目录和旧标识从 Git 索引中完全
删除。迁移文件必须整体进入同一个 changeset；只存在于工作树但未进入索引的文件不能
构建发布制品。

## Module 与配置

- 配置模型继承 `ModuleConfig` 并通过 Module 的 `config_schema` 声明；模型默认
  `extra="forbid"`、`strict=True`、`frozen=True`。
- 生命周期改为 `on_register/on_start/on_stop(context: ModuleContext)`，删除
  `bind_kernel()` 及 Module 对 Kernel 的直接访问。
- 将旧 dependency/provider accessor 改为 Context 的 `require_module()`、
  `optional_module()`、`require_provider()` 或 `optional_provider()`，并在 Module 类上声明
  对应白名单。
- `DEGRADED` 现在表示仍可调用但可选能力不完整；组件自身启动失败为 `FAILED`。

## Chat、身份与会话

删除请求体 `user_id`。app 必须通过异步 `TrustedIdentityResolver.resolve(Request)` 产生
`AgentIdentity(principal_id)`；默认 Bearer 实现只适用于单一服务主体，多租户必须注入
自己的 resolver。`session_id` 保持可选；缺省时请求使用临时会话，结束后删除且不返回。

Google ADK session 改由 `AdkSessionStore.lease()` 管理。默认
`BoundedMemoryAdkSessionStore` 的 TTL 为 1800 秒、容量为 256、每 session 最多 200 个
events；需要跨进程持久化时由应用注入实现，本项目不内置 Redis 适配器。

## 工具与超时

安装时 `jsonschema` 已成为 Core 依赖。工具参数按 Draft 2020-12 完整校验，结果必须是
严格 JSON；默认单工具 10 秒、最多 4 轮、每请求 16 次 handler、单结果 65,536 UTF-8
bytes，AI 整体 timeout 为 25 秒且必须小于 server request timeout。同步工具在线程中
运行，timeout 不能撤销已经发生的副作用，因此副作用工具必须幂等。

升级后运行 README 中的非真实 LLM 测试、Ruff、mypy、安全扫描、wheel 校验和三套
Compose 静态解析；真实模型与外部基础设施仍是显式 smoke，不属于默认门禁。

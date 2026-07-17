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
- 注册期允许取得 `REGISTERED` 依赖代理用于装配，但只有目标进入 `READY/DEGRADED` 后才能
  调用；Protocol 现在还要求实例方法、staticmethod、classmethod descriptor 形态一致。
- `DEGRADED` 现在表示仍可调用但可选能力不完整；组件自身启动失败为 `FAILED`。
- `ProviderSpec(required=True, enabled=False)` 不再合法；factory 返回 candidate 后，仅
  签名合法的异步 `close()` 享有独立预算内单次清理保证。同步、缺失或签名非法的 close
  不调用，并报告 `provider_cleanup_unavailable`。
- Module 清理默认预算为 5 秒。Module 或 Worker 未达到 task 真实终态时保留依赖，重复
  stop 复用原 task；外部编排器负责在 grace period 后终止永久不协作进程。

## Chat、身份与会话

删除请求体 `user_id`。app 必须通过异步 `TrustedIdentityResolver.resolve(Request)` 产生
`AgentIdentity(principal_id)`；默认 Bearer 实现只适用于单一服务主体，多租户必须注入
自己的 resolver。`session_id` 保持可选；缺省时请求使用临时会话，结束后删除且不返回。
`principal_id` 与 `session_id` 都必须匹配
`[A-Za-z0-9][A-Za-z0-9._:-]{0,127}`。

Google ADK session 改由 `AdkSessionStore.lease()` 管理。默认
`BoundedMemoryAdkSessionStore` 的 TTL 为 1800 秒、容量为 256、每 session 最多 200 个
events；淘汰、temporary 和失败 session 会转为 tombstone，同 key 必须等 backend cleanup
完成才能重新 lease。`AdkSessionStore.invalidate()` 已删除，lease 通过 `mark_touched()`、
`reserve_cleanup()`、`mark_cleanup_required()`、`invalidate()` 和 `cleanup_keys` 报告清理，
runtime 完成后调用 `complete_cleanup(key)`；外部 Store 不再访问下划线字段。
清理默认预算为 2 秒且不得大于 AI 总 timeout；预算独立于请求响应，从 cleanup task 创建
时开始，超时发出取消且只在 backend 删除成功后解除 tombstone。未收敛清理保持健康降级，
close 不会提前关闭 backend/store。需要跨进程持久化时由应用注入实现，本项目不内置
Redis 适配器。

## 工具与超时

安装时 `jsonschema` 已成为 Core 依赖。工具参数按 Draft 2020-12 完整校验，结果必须是
严格 JSON；默认单工具 10 秒、最多 4 轮、每请求 16 次 handler，参数和单结果各
65,536 UTF-8 bytes。单一绝对工具 deadline 覆盖参数遍历、Schema、handler 和增量结果
编码；AI 整体 timeout 为 25 秒且必须小于 server request timeout。同步工具在线程中运行，
timeout 不能撤销已经发生的副作用，因此副作用工具必须幂等。

旧单参数工具 handler 和无 identity 的 ModelClient 不提供兼容层。工具改为
`handler(arguments, identity)`；ModelClient 改为 `chat(request, *, identity, tools=())`。
三个 SDK adapter 的构造函数不再导入 SDK 或创建客户端，首次异步 `start/chat/run` 才完成
初始化；close 预算内尚未完成的 lazy start 不会被错误取消，晚到结果由共享 close 接管并
关闭。LiteLLM 运行环境必须预设 `LITELLM_LOCAL_MODEL_COST_MAP=true`。

Gateway 新增 `server.max_response_buffer_bytes`，默认 10 MiB。最终 body 是 deadline 的响应
提交点；start/body 提交后才运行 BackgroundTask，后台失败不能把成功响应改写为 504。

升级后运行 README 中的非真实 LLM 测试、Ruff、mypy、安全扫描、wheel 校验和三套
Compose 静态解析；真实模型与外部基础设施仍是显式 smoke，不属于默认门禁。

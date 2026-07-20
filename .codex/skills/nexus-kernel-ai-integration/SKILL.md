---
name: nexus-kernel-ai-integration
description: Use when adding, changing, or reviewing NexusKernel model clients, Agent runtimes, provider IDs, Chat messages, token usage, server tools, sessions, or AI SDK lifecycle behavior.
---

# NexusKernel AI 集成

先阅读[架构边界](../../../docs/architecture.md)、[开发扩展](../../../docs/development.md)
和[安全合同](../../../docs/security.md)。
项目支持 Python 3.11-3.13；新增 SDK 依赖必须在三个版本上通过 fake 测试与 `pip check`。
AI SDK 只在 `pyproject.toml` 的聚合 `ai` extra 中维护；不得新增 `requirements.txt` 或
其他依赖编辑入口。wheel 交付时同时核对该 extra 的 Provides-Extra 与 Requires-Dist。

- completion 由适配器完成时实现 ModelClient；SDK 管理 agent loop、session 和原生工具时
  实现 AgentRuntime，不伪装成 ModelClient，也不要同时暴露 `chat()` 与 `run()`。
- ModelClient 使用必需关键字参数 `identity`，AgentRuntime 保持显式 identity；两条路径把
  resolver 生成的同一身份绑定到 ToolExecutor。handler 固定接收 `arguments, identity`，
  身份不得进入消息、Schema 或工具参数。
- 先用 SDK fake 写失败测试，再实现消息角色、system/history/current turn、工具、usage、
  超时和取消映射；默认测试不得访问真实模型或依赖真实凭据。
- 所有公开 timeout、TTL、轮询与 cleanup 预算同时拒绝 bool、NaN、正负无穷和非正数；
  Pydantic 配置与直接构造入口必须使用相同的正有限数合同。
- 输出统一为 assistant ChatMessage、ProviderId、模型名、finish reason 与 TokenUsage。
- 工具只来自服务端 AgentToolSpec；使用 Draft 2020-12 校验完整 JSON Schema，并限制名称、
  handler 次数、轮数、超时、参数与 UTF-8 输出大小。单一绝对 deadline 覆盖有界严格 JSON
  参数遍历、Schema、handler 和增量结果编码；拒绝非字符串键、tuple、非有限数与超深结构。
- AgentRuntime 显式接收可信 `AgentIdentity` 和可选 session ID；请求不接受 `user_id`。
  principal/session ID 使用统一 ASCII 格式。session 通过 `AdkSessionStore.lease()` 管理
  TTL、容量、LRU、事件上限、tombstone 与同 key cleanup barrier；清理成功后调用
  `complete_cleanup(key)`。外部 Store 只使用 `reserve_cleanup()`、`mark_touched()`、
  `mark_cleanup_required()`、`invalidate()` 和 `cleanup_keys`，不得访问下划线字段。
- SDK 构造函数必须无网络且不导入重型 SDK；幂等异步 `start()` 才创建 client，直接调用
  复用同一个 start task。LiteLLM 所需本地 cost map 环境只能由启动环境提供。
- `close()` 使用共享 close task，必须幂等关闭 client、runner、session store 与后台任务；
  等待者取消不得取消底层关闭；session cleanup 未收敛时不得提前关闭 backend/store，
  cleanup 自创建起使用独立预算，超时发取消且非协作 task 继续跟踪；未收敛债务保持健康
  降级。关闭失败后允许显式重试，晚到 lazy-start 结果必须由共享 close 接管。所有共享
  task 安装安全结果消费 callback。client close 前使用 active-call gate 等待已接纳请求；
  ADK Runner close 使用独立受观察预算，未收敛时不得关闭 backend/client。

禁止写入全局环境变量、patch SDK 私有 API，或记录 Prompt、工具载荷、Authorization、
SDK 原始异常与模型响应。测试覆盖空响应、非法 usage、未知工具、超时、取消、脱敏、
资源关闭，以及 ModelClient/AgentRuntime 两条 Chat 分派路径。
SDK fake 如需密钥形态、凭据 URL 或敏感字面量赋值，必须与 `security-test-fixture`
位于同一行，且该豁免
只能出现在 `tests/`；优先通过字符串分段构造不具有真实凭据形态的假数据。

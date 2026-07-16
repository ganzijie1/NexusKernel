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
- 先用 SDK fake 写失败测试，再实现消息角色、system/history/current turn、工具、usage、
  超时和取消映射；默认测试不得访问真实模型或依赖真实凭据。
- 输出统一为 assistant ChatMessage、ProviderId、模型名、finish reason 与 TokenUsage。
- 工具只来自服务端 AgentToolSpec；使用 Draft 2020-12 校验完整 JSON Schema，并限制名称、
  handler 次数、轮数、超时和 UTF-8 输出大小。
- AgentRuntime 显式接收可信 `AgentIdentity` 和可选 session ID；请求不接受 `user_id`。
  session 通过 `AdkSessionStore.lease()` 管理 TTL、容量、LRU、事件上限与同 key 串行。
- `close()` 使用共享 close task，必须幂等关闭 client、runner、session store 与后台任务；
  等待者取消不得取消底层关闭，关闭失败后允许显式重试。

禁止写入全局环境变量、patch SDK 私有 API，或记录 Prompt、工具载荷、Authorization、
SDK 原始异常与模型响应。测试覆盖空响应、非法 usage、未知工具、超时、取消、脱敏、
资源关闭，以及 ModelClient/AgentRuntime 两条 Chat 分派路径。
SDK fake 如需密钥形态或凭据 URL，必须与 `security-test-fixture` 位于同一行，且该豁免
只能出现在 `tests/`；优先通过字符串分段构造不具有真实凭据形态的假数据。

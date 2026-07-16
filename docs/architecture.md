# 架构与边界

## 依赖方向

唯一允许的顶层方向是 `app -> nexus_kernel`。`nexus_kernel` 不得静态或相对导入
`app`，该约束由 AST 测试固化。

| 所有者 | 职责 | 不负责 |
| --- | --- | --- |
| `nexus_kernel.kernel` | 模块加载、拓扑、生命周期、Protocol API 代理 | 业务流程、鉴权 |
| `nexus_kernel.providers` | 外部资源创建、健康、降级、逆序关闭 | 业务重试策略 |
| `nexus_kernel.gateway` | FastAPI、中间件、envelope、健康、指标、通用路由 | Chat 鉴权、AI 选择 |
| `nexus_kernel.ai` | 消息、模型、Agent、Tool、usage 与 SDK 适配 | 服务鉴权、业务工具 |
| `nexus_kernel.runtime` | 请求上下文、EventBus、协作式 Worker | 业务任务定义 |
| `nexus_kernel.infrastructure` | 文档、事务、Outbox、队列、向量、追踪接口及可选适配器 | 领域数据模型 |
| `app.providers` | 共享外部连接的严格配置与显式 Provider 工厂 | 动态类路径、业务查询 |
| `app` | 配置装配、业务模块、Chat Router、鉴权与工具注册 | 修改框架私有状态 |

## 生命周期

Kernel 先初始化 Provider，再按稳定拓扑注册和启动 Module。Provider 的异步 factory、
实例校验与 `start()` 共用一次 cooperative deadline；同步 factory 必须非阻塞。必需资源
失败会终止并逆序回滚，可选 Provider 或 Module 自身失败进入 `FAILED`。`DEGRADED` 只表示
组件仍可调用但可选能力不完整，因此路由和代理允许 `READY/DEGRADED`。

注册成功或部分注册失败的 Module 都进入一次性逆序清理栈；取消、启动回滚、正常停止和
重复停止不会重复调用 `on_stop(context)`。Worker 角色只在 Kernel 启动完成后注册可运行
模块的 Worker，停止时先停 Worker；API 角色不注册 Worker，也不会把未启动 Worker 计为
故障。`WorkerStatus` 是不可变状态快照，健康、日志和指标只暴露稳定错误码。

WorkerRunner 是协作式合同：必须响应 stop event，且不得持续吞掉
`CancelledError`。超时会被报告为 `worker_stop_timeout`，Python 线程内无法安全强杀
任意不合作协程；需要强隔离的任务应放入独立进程或外部队列消费者。

Module 通过 `config_schema` 声明 strict/frozen 的 `ModuleConfig`，生命周期签名统一为
`on_register/on_start/on_stop(context: ModuleContext)`。Context 只提供
`require_module()`、`optional_module()`、`require_provider()` 与 `optional_provider()`，
且每次访问都检查声明白名单。Kernel 校验 Protocol 的参数名、顺序、kind、默认值存在性
及 sync/async 形态；API 代理在每次调用时重新检查状态，缓存 callable 也不能绕过停止。

## HTTP 与 AI

Gateway 要求显式传入 settings、Kernel 和应用 Router，并统一检查 FastAPI 系统、
内置、应用和模块路由冲突。声明式模块路由发布请求/响应 OpenAPI schema；需要鉴权、
请求头或业务异常映射的路由属于 `app/routes`。

`openai_compatible` 与 `litellm` 实现 ModelClient；`google_adk` 实现独立的
AgentRuntime。app 将 Request 交给 `TrustedIdentityResolver` 产生
`AgentIdentity(principal_id)`，正文、请求 ID 和客户端字段都不参与可信命名空间。
AgentRuntime 使用可插拔 `AdkSessionStore.lease()`；默认有界内存实现负责 TTL、LRU、
事件上限和同 key 串行。V1 只支持非流式文本、受预算约束的服务端工具和 usage 归一化。

扩展步骤见[开发扩展](development.md)，来源与排除项见[提取核查](extraction-audit.md)。

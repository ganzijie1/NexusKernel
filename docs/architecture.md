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
`required=True, enabled=False` 是无效 Provider 声明，会在 app 配置层和框架合同层直接
拒绝。factory 一旦返回 candidate，Registry 就接管其清理责任；合同校验、启动、超时或
取消失败时，只有签名合法的异步 `close()` 才会在独立有界预算内执行且只执行一次。
同步、缺失或签名非法的 close 不会在线程中调用，并报告稳定
`provider_cleanup_unavailable`。

注册成功或部分注册失败的 Module 都进入一次性逆序清理栈；取消、启动回滚、正常停止和
重复停止不会重复调用 `on_stop(context)`。Worker 角色只在 Kernel 启动完成后注册可运行
模块的 Worker，停止时先停 Worker；API 角色不注册 Worker，也不会把未启动 Worker 计为
故障。ApplicationRuntime 的并发或重复 stop 共享同一个 shutdown task；调用者取消不会
跳过 Worker 终态，也不会让 Kernel 提前关闭。`WorkerStatus` 是不可变状态快照，健康、
日志和指标只暴露稳定错误码。

Module 的 `on_stop()` 使用独立 5 秒预算；超时后仅发出一次取消并保留原 task、逆序栈和
Provider，显式重试继续等待同一 task 达到真实终态。Worker 同样以 task 真实终态作为
shutdown 完成条件；Worker 未收敛时 ApplicationRuntime 不关闭 Kernel。框架不会为追求
进程内“成功退出”冒险关闭仍被使用的依赖，永久不协作任务由编排器在外部 grace period
后终止进程。

WorkerRunner 是协作式合同：必须响应 stop event，且不得持续吞掉
`CancelledError`。超时会被报告为 `worker_stop_timeout`，Python 线程内无法安全强杀
任意不合作协程；需要强隔离的任务应放入独立进程或外部队列消费者。

Module 通过 `config_schema` 声明 strict/frozen 的 `ModuleConfig`，生命周期签名统一为
`on_register/on_start/on_stop(context: ModuleContext)`。Context 只提供
`require_module()`、`optional_module()`、`require_provider()` 与 `optional_provider()`，
且每次访问都检查声明白名单。Kernel 校验 Protocol 的参数名、顺序、kind、默认值存在性
及 sync/async 形态，并使用静态 descriptor 检查区分实例方法、staticmethod 和
classmethod。注册期 accessor 可以返回处于 `REGISTERED` 的依赖代理用于缓存与装配，
但 API 代理在每次调用时仍要求目标为 `READY/DEGRADED`，缓存 callable 也不能绕过停止。

## HTTP 与 AI

Gateway 要求显式传入 settings、Kernel 和应用 Router，并统一检查 FastAPI 系统、
内置、应用和模块路由的实际匹配集合冲突，包括嵌套 Router、Mount 前缀子树、动态参数、
converter、catch-all 与尾斜杠。
中间件外到内固定为请求上下文、请求日志、指标、CORS、单一服务端截止时间、正文大小限制
和应用，因此慢分块正文也受同一截止时间保护。响应 start/header/body 在有界规范化缓冲中
合并，默认最多 10 MiB；最终 body 必须在 deadline 内生成，随后先提交 start/body，再释放
应用执行 BackgroundTask。提交后的网络发送和后台任务不再转换为 504，后台失败只记录
`http_background_task_failed` 与低基数计数。超限返回 `response_too_large`，非法 ASGI
响应返回 `invalid_response`；通用 Gateway 不提供渐进式流式响应语义。声明式模块路由
发布请求/响应 OpenAPI schema；需要鉴权、请求头或业务异常映射的路由属于 `app/routes`。

`openai_compatible` 与 `litellm` 实现 ModelClient；`google_adk` 基于 ADK 2.4 的
`App + Runner` 实现 AgentRuntime 与 WorkflowRuntime。app 将 Request 交给
`TrustedIdentityResolver` 产生
`AgentIdentity(principal_id)`，正文、请求 ID 和客户端字段都不参与可信命名空间。
ModelClient 的 `chat(..., identity=...)` 与 AgentRuntime 的 `run(..., identity=...)` 都把同一
身份绑定到本次 ToolExecutor；工具 handler 固定接收 `arguments, identity`，身份不进入
模型上下文、Schema 或参数。三个 SDK 适配器的构造函数不导入重型 SDK 或联网，幂等异步
`start()` 才创建客户端；直接调用也复用同一个 start task。

三个 AI 实现使用同一 active-call gate：close 开始后拒绝新请求，已接纳请求达到终态后
才关闭 SDK client；关闭失败允许显式重试。工具的单一绝对 deadline 覆盖有界参数遍历、
Schema、handler 和结果规范化；参数与结果均为严格 JSON，拒绝非字符串对象键、tuple、
自定义对象、非有限数值和超深结构，参数与结果默认各限制 65,536 UTF-8 bytes。

AgentRuntime 使用可插拔 `AdkSessionStore.lease()`；默认有界内存实现负责 TTL、LRU、
事件上限和同 key 串行。淘汰、temporary 或失败 session 会原子转为 tombstone，后续同 key
lease 等待 backend cleanup 完成，不同 key 保持并发；清理有独立 2 秒预算，未收敛时保留
tombstone 并阻止 backend/store 提前关闭。预算从 cleanup task 创建时独立计时，请求先
结束也会按时发出取消；不协作的 SDK task 继续被跟踪，未收敛期间 Provider 健康保持降级。
自定义 Store 只使用 lease 的 `reserve_cleanup()`、`mark_touched()`、
`mark_cleanup_required()`、`invalidate()` 与 `cleanup_keys`，不得访问下划线字段。TTL
过期 backlog 只有在能原子转移全部过期 idle session 后才发放新 lease。ADK Runner close
是独立受观察的 2 秒任务，未收敛时同样阻止 backend/client 提前关闭。lazy start 晚于
close 预算完成时，由共享 close task 接管结果并只关闭 backend/store 一次。
同步 Chat V1 只支持非流式文本和受预算约束的服务端工具。ADK 结果只采用预期 root 的
terminal 内容，过滤 thought 与子 Agent final；usage 将 prompt/tool-use 归为输入、
candidate/thought 归为输出，并以 SDK total 校验而不重复计算 cached token。工具声明使用
`parameters_json_schema` 保留 Draft 2020-12 语义。

Agent Runs 是 app 所有的可选 Module，默认关闭。它冻结 app 注册的 graph/team 定义，并以
有界 FIFO、固定 phase 并发与 TTL sweeper 管理异步 run。Graph V1 支持静态顺序、条件路由、
fan-out/Join、节点 timeout/retry 和单 terminal；Team V1 支持 chat coordinator 委派 leaf
task/single-turn Agent。客户端不能定义模型、拓扑、指令或工具。有副作用的原生
FunctionTool、循环/嵌套/动态 Workflow、live/A2A、ManagedAgent、Workflow-as-Tool 与事件
压缩不在首版范围。

`requires_action` 会释放 phase 槽但固定 session；取消和停止必须等待 ADK generator、Runner
及 session 清理真正收敛。API 关闭采用 quiesce、有限排空、强制 close 三阶段：quiesce 后
拒绝新建 run，但既有 GET、resume、cancel 仍可工作；排空预算耗尽才取消残余 run。排空或
清理未收敛时 readiness 保持 degraded，显式重试继续使用同一关闭 task。Agent Runs 模块及
业务注册表只在 API 角色装配，Worker 不要求 API-only 凭据或定义。

服务通过无标识符 observer 输出队列深度、运行 phase、待 action、八态快照、终态、phase
时延、容量拒绝和清理债务。指标标签只使用固定状态/结果 allowlist，禁止 principal、run、
session、agent 或错误原文。run/session/幂等与恢复状态仅在进程内，首版部署要求单实例或
粘性路由；重启和跨副本不会恢复旧 run。HITL 使用 ADK 仍标为实验性的
`ResumabilityConfig`，受默认关闭的 Agent Runs 模块和定义级 `resumable` 双重开关约束。

扩展步骤见[开发扩展](development.md)，来源与排除项见[提取核查](extraction-audit.md)。

# API 契约

## 通用响应

成功响应：

```json
{"success": true, "data": {}, "meta": {"request_id": "...", "trace_id": "..."}}
```

错误响应：

```json
{"success": false, "error": {"code": "stable_code", "message": "safe message"}, "meta": {"request_id": "...", "trace_id": "..."}}
```

请求和响应不会回显 Prompt、Authorization、凭据或 SDK 原始内容。声明式模块路由的
Pydantic 请求/响应模型会进入 `/openapi.json`。

## 基础端点

| 端点 | 行为 |
| --- | --- |
| `GET /health/live` | 进程存活时返回 200 |
| `GET /health/ready` | 返回 200；依赖不可用或 Agent Runs 排空时 data.status 为 `degraded` |
| `GET /metrics` | Prometheus 文本，使用低基数标签 |
| `POST /api/v1/example/echo` | 无外部依赖的声明式模块示例 |
| `POST /api/v1/chat` | 应用层 Bearer 鉴权和 AI 调用 |
| `POST /api/v1/agents/{agent_id}/runs` | 创建异步多智能体 run |
| `GET /api/v1/agent-runs/{run_id}` | 获取 run 快照 |
| `POST /api/v1/agent-runs/{run_id}/resume` | 原子提交全部待处理 action |
| `DELETE /api/v1/agent-runs/{run_id}` | 取消非终态 run |
| `DELETE /api/v1/agents/{agent_id}/sessions/{session_id}` | 删除空闲 session |

Chat 请求只接受 `messages`、可选 `session_id` 和服务端已注册工具。`messages` 只允许
system 前缀，最后一条必须是 user；外部 tool history、尾随 assistant 或中途 system
返回 `422 invalid_request`。`user_id` 是未知字段，不能选择可信主体。

app 的异步 `TrustedIdentityResolver` 从 Request 产生 `AgentIdentity`。默认 Bearer resolver
使用 constant-time 比较并把所有通过同一服务 Token 的调用映射到固定主体 `service`；
多租户必须注入自定义 resolver。未传 `session_id` 时生成临时会话，成功、失败、取消或
超时后均删除，且响应不返回该 ID。
`session_id` 和 resolver 返回的 `principal_id` 使用同一个 ASCII 合同：
`[A-Za-z0-9][A-Za-z0-9._:-]{0,127}`。空格、控制字符、CR/LF、Unicode 和超长值均被拒绝，
请求字段错误返回 `422 invalid_request`。

ModelClient 与 AgentRuntime 都接收同一个可信 identity，ToolExecutor 只在服务端调用
`handler(arguments, identity)`。identity 不写入模型消息、工具 JSON Schema 或参数，正文、
session、请求 ID 和工具输入都不能覆盖它。

Chat 未配置服务 Token 返回 `503 auth_not_configured`；Token 缺失或错误返回
`401 unauthorized`；AI、工具或 store 超时返回 `504 ai_timeout`，其他 Provider、工具或
store 故障返回 `503 ai_provider_unavailable`。请求合同错误返回 `422 invalid_request`，
请求体超限返回 413，未知异常返回脱敏 500。`ChatResponse` wire shape 保持不变，usage
只接受非负整数。

## Agent Runs

Agent Runs 模块默认关闭；关闭时不会注册上述五个端点。`agent_id` 只能选择 app 启动时
注册并冻结的定义，客户端不能提交模型、指令、拓扑、工具或类路径。未知或当前主体无权
访问的 agent、run 和 session 均返回 404，避免资源枚举。

创建请求只接受 `input`、`session_action` 与可选 `session_id`。`session_action=new` 可由
服务端生成 session ID；`continue` 必须提供同一主体、agent 和 definition revision 下已存在
且空闲的 ID。创建和恢复都必须携带 `Idempotency-Key`：同一 key 与相同请求返回当前快照，
内容不同返回 409。服务端仅保存进程随机 HMAC 摘要，不保存用于幂等比较的原始输入。

创建与恢复返回 202、标准成功 envelope、当前 `AgentRunSnapshot` 和 `Location`。GET 返回
最新快照并设置 `Cache-Control: no-store`；非终态快照还会设置 `Retry-After: 1`。状态固定为
`queued`、`running`、`requires_action`、`canceling`、`succeeded`、`failed`、`canceled`、
`expired`；快照还包含 version、definition revision、session、时间戳，以及终态 output、
usage、model、finish reason 或稳定 error code。

OpenAPI 为五个端点声明实际成功状态、`Location`、`Retry-After`、`Cache-Control`，并把
401、404、409、413、422、429、500、503 等实际错误统一描述为标准错误 envelope。运行时
所有成功快照和错误都设置 `Cache-Control: no-store`，不会由框架默认 422 schema 误导客户端。

`requires_action` 只公开随机 `action_id`、allowlist `code`、`input|confirmation` 类型和
截止时间。恢复请求必须携带当前 version，并一次性、无重复地覆盖所有待处理 action；过期、
版本冲突、缺项或多项均返回 409。OAuth/credential interrupt 不向客户端透传，统一失败为
`agent_action_unsupported`。恢复可能按至少一次语义执行，带副作用的业务工具必须自行实现
幂等。

同一 `(principal_id, agent_id, session_id)` 只允许一个非终态 run。DELETE run 先进入
`canceling`，待 ADK 执行与 session 清理收敛后才成为 `canceled`；空闲 session 删除不允许
越过正在运行或等待 action 的 run。所有状态都只保存在当前进程，重启、滚动替换和跨副本
请求无法继续旧 run；首版部署必须单实例或使用粘性路由。

API 进程关闭先进入 `draining`：拒绝创建新 run，但已接纳 run 的 GET、resume、cancel
继续可用。`GET /health/ready` 此时仍返回 HTTP 200，但总体状态和 `agent_runs.status` 为
`degraded`，`agent_runs.lifecycle` 为 `draining`。默认排空 900 秒；超时后强制取消残余 run，
并且只有执行、Runner 和 session 清理真正收敛后才关闭依赖。

Prometheus 指标使用配置的 prefix，Agent Runs 指标包括
`agent_runs_queue_depth`、`agent_runs_active_phases`、`agent_runs_waiting_actions`、
`agent_runs_current{status}`、`agent_runs_status_transitions_total{status}`、
`agent_runs_terminal_results_total{status}`、`agent_runs_phase_duration_seconds{outcome}`、
`agent_runs_capacity_rejections_total` 和 `agent_runs_cleanup_debt`。`status` 与 `outcome` 只有
代码 allowlist 中的固定值，绝不使用 principal、agent、run 或 session 作为标签。

单一 server timeout 覆盖无 Content-Length 的正文接收、大小限制、下游处理和最终响应
body 生成；超时响应仍经过 CORS、request/trace ID、日志与指标中间件。start/header/body
先进入默认 10 MiB 的规范化缓冲，最终 body 到达后先提交响应，再运行 BackgroundTask。
提交后的发送或后台工作不会被改写为 504；后台失败只产生稳定日志与指标。响应缓冲超限
返回 `500 response_too_large`，重复 start、未知扩展或非法消息返回
`500 invalid_response`。应用 Router 不应依赖渐进式流式下发。系统、内置、应用和模块
路由若实际匹配集合重叠，会在启动期返回安全配置错误而不是依赖注册顺序选路；嵌套
Router 会递归展开，Mount 视为占用全部 HTTP 方法的前缀子树。

客户端可传符合格式的 `X-Request-ID` 与 `X-Trace-ID`；否则中间件生成新值并写入响应。

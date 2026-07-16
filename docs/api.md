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
| `GET /health/ready` | 返回 200；依赖不可用时 data.status 为 `degraded` |
| `GET /metrics` | Prometheus 文本，使用低基数标签 |
| `POST /api/v1/example/echo` | 无外部依赖的声明式模块示例 |
| `POST /api/v1/chat` | 应用层 Bearer 鉴权和 AI 调用 |

Chat 请求只接受 `messages`、可选 `session_id` 和服务端已注册工具。`messages` 只允许
system 前缀，最后一条必须是 user；外部 tool history、尾随 assistant 或中途 system
返回 `422 invalid_request`。`user_id` 是未知字段，不能选择可信主体。

app 的异步 `TrustedIdentityResolver` 从 Request 产生 `AgentIdentity`。默认 Bearer resolver
使用 constant-time 比较并把所有通过同一服务 Token 的调用映射到固定主体 `service`；
多租户必须注入自定义 resolver。未传 `session_id` 时生成临时会话，成功、失败、取消或
超时后均删除，且响应不返回该 ID。

Chat 未配置服务 Token 返回 `503 auth_not_configured`；Token 缺失或错误返回
`401 unauthorized`；AI、工具或 store 超时返回 `504 ai_timeout`，其他 Provider、工具或
store 故障返回 `503 ai_provider_unavailable`。请求合同错误返回 `422 invalid_request`，
请求体超限返回 413，未知异常返回脱敏 500。`ChatResponse` wire shape 保持不变，usage
只接受非负整数。

客户端可传符合格式的 `X-Request-ID` 与 `X-Trace-ID`；否则中间件生成新值并写入响应。

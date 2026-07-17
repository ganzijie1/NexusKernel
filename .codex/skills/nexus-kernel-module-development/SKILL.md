---
name: nexus-kernel-module-development
description: Use when creating, changing, or reviewing NexusKernel application modules, Protocol APIs, module dependencies, declarative routes, Providers, Workers, or lifecycle behavior.
---

# NexusKernel 模块开发

先阅读[架构边界](../../../docs/architecture.md)和[开发扩展](../../../docs/development.md)。
项目支持 Python 3.11-3.13；模块实现与测试必须兼容最低版本 3.11。

## 所有权

- 业务 Module、Service、Router 和工具归 `app`；通用生命周期与接口归 `nexus_kernel`。
- 目录名、`Module.name` 和 `config/modules.yaml` 键保持一致。
- 跨模块只使用 Protocol 白名单代理、EventBus 或 Provider；不得导入其他模块私有实现。

## 文件格式

- 标准入口固定为 `app/modules/<module_name>/module.py`，配置类较小时也放在该文件。
- `module.py` 只负责配置、生命周期和装配；业务逻辑放 `services/`，持久化放
  `repositories.py`，WorkerSpec 放 `workers.py`，HTTP 契约与路由放 `api/`。
- 简单模块不创建空目录；配置类路径统一为
  `app.modules.<module_name>.module.<ModuleClass>`。

## 工作流

1. 搜索已有模块、公共契约、配置、直接调用方和测试，说明复用或新增理由。
2. 先写失败测试，覆盖 schema、Protocol 越权、依赖拓扑和目标行为。
3. 保持 Module 构造函数无参；通过 `config_schema` 声明 `ModuleConfig`，生命周期只接收
   `ModuleContext` 并管理本模块资源。
4. 声明 required/optional 模块与 Provider；Context 的四个 accessor 只能访问已声明项。
   注册期可取得 `REGISTERED` 依赖代理用于装配，但只能在目标 `READY/DEGRADED` 后调用。
   可选能力缺失使消费者降级，组件自身失败为 `FAILED`，必需资源失败必须回滚；禁止
   `ProviderSpec(required=True, enabled=False)`。
5. 简单无鉴权接口使用 `HttpEndpointSpec`；鉴权、请求头或业务异常映射放入 `app/routes`。
   新路由不得与系统、应用、模块、嵌套 Router 或 Mount 前缀子树的实际匹配集合重叠。
   Gateway 在有界缓冲内以最终 body 为提交点；提交后 BackgroundTask 不再受响应 deadline
   改写。通用 Gateway 不提供渐进式流式响应语义。
6. 通过 `get_worker_specs()` 声明 Worker，由 Kernel 构造期校验，ApplicationRuntime 在
   Kernel 启动后注册并按 Kernel、Worker 顺序启停；不要自行创建长期后台任务。
7. 运行模块、Kernel、Gateway、配置和 AST 边界测试，再执行完整质量门禁。

测试中的密钥形态、凭据 URL、私网地址或敏感字面量赋值假数据必须与
`security-test-fixture` 位于同一行；
该行级豁免只允许用于 `tests/`，不得用来绕过生产代码或配置扫描。

禁止访问 Kernel 私有状态、绕过 ModuleApiProxy、由业务模块创建共享 Provider、对可选
Provider 使用 `require()`，或让 `nexus_kernel` 导入 `app`。WorkerRunner 必须协作处理
stop event 和取消；需要强制终止的任务使用独立进程或外部消费者。
Protocol 修改必须覆盖实例方法、staticmethod、classmethod descriptor 形态及参数 kind；
Provider factory 返回 candidate 后，仅合法无参 async `close()` 保证有界关闭一次；同步、
缺失或非法 close 不调用并报告 `provider_cleanup_unavailable`。Module/Worker 停止未达到
task 真实终态时必须保留同一 task 和依赖，显式重试继续等待，不能提前关闭 Provider。

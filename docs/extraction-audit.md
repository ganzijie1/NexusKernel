# 基础框架提取核查

本模板只提取 ai-pet 与 LoongMemory 中可跨项目复用的技术合同，不复制业务模块、领域
模型、既有环境配置或凭据。

| 能力 | 通用化结果 | 所有者与证据 |
| --- | --- | --- |
| 模块拓扑、回滚、逆序停止 | Module、Protocol API 代理、Kernel 状态机 | `nexus_kernel/kernel` 与 Kernel 测试 |
| 严格配置与动态加载 | 重复键/未知字段/变量/类路径 fail-fast | `nexus_kernel/config` 与配置测试 |
| Provider 生命周期 | required/optional、candidate 接管、deadline、健康、降级、单次关闭 | `nexus_kernel/providers` 与 Registry 测试 |
| EventBus、请求上下文、Worker | 独立运行时原语与协作停止 | `nexus_kernel/runtime` 与运行时测试 |
| 多模型与服务端工具 | 可信 identity、lazy SDK、Tool 预算、session tombstone、usage | `nexus_kernel/ai` 与 SDK fake 测试 |
| Gateway 与指标容错 | envelope、ID、慢正文截止时间、匹配集合冲突、OpenAPI、低基数指标 | `nexus_kernel/gateway` 与 Gateway 测试 |
| Mongo/Redis/PostgreSQL/Qdrant/OTel | 独立 extra、基础连接能力和按需加载适配器 | `nexus_kernel/infrastructure` 与合同测试 |
| Docker/CI | 单镜像双角色、非 root、Python 3.11-3.13 测试矩阵、动态 wheel 合同、无自动部署 | `docker`、`.gitlab-ci.yml` 与部署测试 |

明确排除：宠物/NPC/记忆领域模块、Job DTO、Scope 与集合、AIProxy/WebSocket、旧路径
兼容、数据库迁移、成本落库、流式输出、结构化输出、源项目部署地址和任何凭据。

来源项目的扁平 requirements 方案未迁移。模板以 `pyproject.toml` 统一项目元数据、基础
依赖与 extras；PostgreSQL 只提供 asyncpg 基础连接，不引入 ORM、迁移或业务表结构。

完整验证包括 AST 依赖合同、初始化临时副本、安全扫描和 README 中的质量门禁。安全扫描
不跳过整个 `tests/`，只接受测试目录内同行 fixture 豁免；wheel 校验从初始化后项目的
元数据建立合同，拒绝未知顶层目录、重复条目、路径穿越、CRC 损坏、不一致的 dist-info、
重复单值字段和与文件名不匹配的 Tag。初始化器还在写入前验证精确 env profile 集合，并在
隔离副本运行非真实 LLM 测试、安全扫描、Compose 解析与真实 wheel 构建校验。

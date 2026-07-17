# 安全合同

- 仓库只跟踪空凭据或占位符。初始化器生成的 `docker/.env.dev`、`.env.preprod`、
  `.env.prod` 是唯一允许存在的真实 env，必须同时未被跟踪、被 `.gitignore` 与
  `.dockerignore` 排除；POSIX 权限必须精确为 `0600`。Windows 只能验证文件是普通、非
  链接文件，NTFS ACL 不在扫描器能力范围内，必须由部署平台另行限制。扫描器验证元数据
  但不会读取或输出其内容。
- Chat 鉴权 fail-closed：服务 Token 未配置返回 503，调用 Token 缺失或错误返回 401。
- AI 凭据显式传入 SDK；禁止代码写入全局环境变量或 monkey patch LiteLLM 私有 API。
  LiteLLM 所需 `LITELLM_LOCAL_MODEL_COST_MAP=true` 必须由进程启动环境提供。
- Google ADK 每次调用都必须以 `TelemetryConfig` 禁止内容捕获；Docker、CI 与三份 env
  示例还必须设置 `ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS=false` 和
  `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=NO_CONTENT`。如果 SDK 被配置为忽略
  RunConfig 且环境没有保持安全值，Provider 必须 fail-fast，代码不得改写环境规避错误。
- `google.adk`、`google_adk` 与 `google.genai` 日志采用 fail-closed 处理：不格式化 SDK
  message、参数、异常或 traceback，只保留稳定级别事件码。多智能体 API 只公开随机
  `action_id`、allowlist code、响应类型和期限，不公开 SDK interrupt 标识或内部事件。
- Mongo URI、Redis URL、PostgreSQL DSN 和 Qdrant API key 使用脱敏配置类型；日志、错误
  与健康状态只允许 Provider 名、状态和稳定错误码，不得输出连接字符串。
- 日志、健康、指标和错误不得包含密钥、Authorization、凭据 URL、Prompt、工具参数与
  结果、完整请求体、SDK 异常或原始模型响应。
- Agent Runs 指标标签只允许固定状态和 phase 结果；principal、agent、run、session、action、
  definition revision、错误码和其他运行时字符串都不能成为标签。
- Agent Runs 的幂等摘要使用进程随机 HMAC，不保存或记录请求输入。可能被恢复流程按至少
  一次语义重放的副作用工具必须在业务层使用幂等键；OAuth/credential interrupt 不向
  公共 API 透传，统一转换为稳定的不支持错误。
- `scripts/security_scan.py` 用有界 AST 检查 Python 常量赋值、keyword、字典、静态
  f-string/拼接/join 与安全包装调用，用带行号节点检查 YAML/JSON，并检查
  TOML/env/INI/Shell/PowerShell/批处理与文档中的敏感字面量；Shell 还覆盖
  readonly、declare/typeset、`.envrc` 和 PowerShell `Set-Item Env:`。同时扫描密钥形态、
  私网地址、真实 env 和 Docker context。受支持的结构化文件解析失败时必须失败关闭，
  不得当作无 finding 继续；输出只含类别、路径和行号。
- 扫描通过 `os.walk(topdown=True, followlinks=False)` 在下降前剪除 Git、venv、缓存、
  build/dist 和 egg-info；每个候选先 `lstat()`，符号链接、FIFO 与其他非普通文件直接
  拒绝。`.env*`、敏感文件名及 `.key/.pem/.p12/.pfx` 形式的文件或目录在下降前直接拒绝，
  唯一三份真实 Docker env 只按元数据特例验证。JSON 必须先通过严格语法、重复键和有限
  数值检查；带 conversion/format spec 的常量 f-string 保守视为风险。不存在可复用的
  整行扫描 suppression marker。
- 扫描器不跳过整个 `tests/`。测试必须使用假数据；确需模拟密钥形态、凭据 URL、私网
  地址或敏感字面量赋值时，只能在 `tests/` 内风险数据的同一行添加
  `security-test-fixture`。该标记不能
  用于生产代码、配置、脚本或文档中的风险数据。
- `.venv-win/.venv-linux/.venv-macos`、`build/dist` 和 egg-info 属于生成目录，不进入源码
  安全扫描；wheel 由独立校验器拒绝旧包、未知顶层目录、缓存、env 与凭据文件，并核验
  重复条目、路径穿越、CRC，以及 RECORD 的三列、唯一性、SHA-256 摘要和实际字节大小。
- 共享或 shield 后台 task 必须消费结果；asyncio 未观察 task 日志只能转换成稳定事件码，
  不得格式化 task/future、原始异常或不可信 traceback。
- `scripts/check_text_contract.py` 对 Git 索引完整快照检查 UTF-8、尾空格、最终换行与
  `.gitattributes` EOL 合同；本地工作树改动仍需额外执行 `git diff --check`。

来自源项目的任何已跟踪凭据都不会迁移；源凭据轮换是独立运维事项。

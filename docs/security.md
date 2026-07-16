# 安全合同

- 仓库只跟踪空凭据或占位符。初始化器生成的 `docker/.env.dev`、`.env.preprod`、
  `.env.prod` 是唯一允许存在的真实 env，必须同时未被跟踪、被 `.gitignore` 与
  `.dockerignore` 排除并保持 `0600`；扫描器验证元数据但不会读取或输出其内容。
- Chat 鉴权 fail-closed：服务 Token 未配置返回 503，调用 Token 缺失或错误返回 401。
- AI 凭据显式传入 SDK；禁止写入全局环境变量或 monkey patch LiteLLM 私有 API。
- Mongo URI、Redis URL、PostgreSQL DSN 和 Qdrant API key 使用脱敏配置类型；日志、错误
  与健康状态只允许 Provider 名、状态和稳定错误码，不得输出连接字符串。
- 日志、健康、指标和错误不得包含密钥、Authorization、凭据 URL、Prompt、工具参数与
  结果、完整请求体、SDK 异常或原始模型响应。
- `scripts/security_scan.py` 用 AST 检查 Python 常量赋值、keyword 和字典，用带行号节点
  检查 YAML/JSON，并检查 TOML/env/INI/Shell/PowerShell/批处理与文档中的敏感字面量；
  同时扫描密钥形态、私网地址、真实 env 和 Docker context。输出只含类别、路径和行号。
- 扫描通过 `os.walk(topdown=True, followlinks=False)` 在下降前剪除 Git、venv、缓存、
  build/dist 和 egg-info，且不跟随符号链接。
- 扫描器不跳过整个 `tests/`。测试必须使用假数据；确需模拟密钥形态、凭据 URL 或私网
  地址时，只能在 `tests/` 内风险数据的同一行添加 `security-test-fixture`。该标记不能
  用于生产代码、配置、脚本或文档中的风险数据。
- `.venv-win/.venv-linux/.venv-macos`、`build/dist` 和 egg-info 属于生成目录，不进入源码
  安全扫描；wheel 由独立校验器拒绝旧包、未知顶层目录、缓存、env 与凭据文件，并核验
  重复条目、路径穿越、CRC，以及 RECORD 的三列、唯一性、SHA-256 摘要和实际字节大小。

来自源项目的任何已跟踪凭据都不会迁移；源凭据轮换是独立运维事项。

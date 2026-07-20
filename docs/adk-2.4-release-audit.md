# ADK 2.4 与框架稳定性最终核查记录

## 结论

本记录基于 `develop@5266643` 的干净隔离快照，核查日期为 2026-07-21。框架代码、测试、
文档、约束、wheel 与容器门禁均通过，原“未跟踪源码未进入交付集”阻断项已关闭。本轮四个
实现/文档提交可以进入统一稳定性审计提交。

生产发布仍判定为 **No-Go**：真实 Provider smoke、单实例 canary、GitLab/macOS pipeline
和镜像 CVE 扫描未执行；preprod/prod Compose 也未显式声明与 Agent Runs 最长 900 秒
drain 协调的 `stop_grace_period`。这些是发布门禁，不否定本次框架稳定性提交结果。

## 候选提交

| Commit | 内容 | 状态 |
| --- | --- | --- |
| `b9235fe` | `fix: make module loading dependency deterministic` | 已验证 |
| `e3bde3e` | `fix: harden runtime boundary validation` | 已验证 |
| `683c8e3` | `build: harden reproducible delivery contracts` | 已验证 |
| `5266643` | `docs: document framework stability contracts` | 已验证 |

本文作为上述四个提交后的纯文本审计提交，不记录自身尚未生成的 commit ID。已有历史未被
amend、squash、rebase 或重写；本轮未 push、未触发 GitLab pipeline、未部署。

## 框架合同

| 方向 | 最终合同 | 证据 |
| --- | --- | --- |
| 模块加载 | YAML 映射无序；清单校验后稳定拓扑；依赖优先 import/construct/register/start，逆序 stop | 三模块 6 种排列、四模块菱形图 24 种排列及独立子进程真实导入测试通过 |
| 声明与能力 | manifest、类、实例声明一致；Provider 分类互斥；Kernel 使用不可变 name/capability 快照 | loader、Kernel、声明漂移和生命周期改写测试通过 |
| 配置绑定 | strict/frozen 配置只绑定一次；构造期提前读取后拒绝覆盖 | 必填、默认、误读、脱敏异常测试通过 |
| 导入边界 | 根模块包零副作用；跨 owner 只允许显式公共 api/schema/event/protocol | bootstrap 预加载与 AST module/alias/相对导入绕过测试通过 |
| Gateway | 同步 handler 在线程执行；全局/endpoint deadline 覆盖等待；converter 重叠 fail-fast | timeout、晚到结果、awaitable 返回、两种 Router 顺序与响应头预算测试通过 |
| 数值边界 | 外部 timeout/TTL/轮询/cleanup 拒绝 bool、NaN、无穷和非正数；Redis 严格正整数 | Pydantic、直接构造与 Redis client 零调用测试通过 |
| 可复现交付 | `pyproject.toml` 为唯一依赖源；固定 uv 生成 Python 3.11-3.13 universal constraints | `compile_constraints.sh --check` 零漂移；三版本环境与六套 wheel 安装通过 |
| 容器 | 固定基础镜像 digest；UID/GID 10001；只读根、tmpfs、no-new-privileges；有限健康超时 | default/full 构建、运行和三份 Compose 解析通过 |

## 验证矩阵

| 门禁 | 环境 | 实际结果 |
| --- | --- | --- |
| 完整非真实 LLM pytest | Python 3.11 固定镜像 | `965 passed, 3 skipped, 3 deselected, 8 warnings` |
| 完整非真实 LLM pytest | Python 3.12 固定镜像 | `965 passed, 3 skipped, 3 deselected, 8 warnings` |
| 完整非真实 LLM pytest | Python 3.13 固定镜像 | `965 passed, 3 skipped, 3 deselected, 8 warnings` |
| 依赖一致性 | 三个脚本创建的 `.venv-linux` | 三版本 `pip check` 均为 `No broken requirements found` |
| 静态质量 | Python 3.11 固定镜像 | compileall、Ruff format/check/C901、mypy 全通过；120 个文件格式合格、80 个源码文件类型检查无问题 |
| 安全与文本 | 干净 `5266643` 快照 | 源码敏感信息扫描、tracked text contract、`git diff --check` 通过 |
| constraints | 固定 `uv==0.11.29` | `constraints/py311-313.txt` 重建检查零漂移 |
| 初始化器 | 隔离完整项目副本 | 初始化幂等、完整非真实 LLM pytest、wheel 构建与严格校验通过 |
| wheel | Python 3.11 构建 | metadata、RECORD、CRC、Tag、包边界严格校验通过 |
| wheel 安装矩阵 | Python 3.11-3.13 core/full | 六套安装、关键 import、core SDK 隔离与 `pip check` 全通过 |
| Docker default/full | 本机 Docker | 大小、内容 ID、UID/GID、只读根、tmpfs、no-new-privileges、SDK 隔离全通过 |
| Docker API | default/full | live=`alive`；无凭据 ready=`degraded`；SIGTERM 后 exit 0 |
| Docker Worker | default/full | 启动后持续运行；SIGTERM 后 exit 0 |
| Compose | dev/preprod/prod | dev 直接解析；preprod/prod 空 image fail-fast，注入显式候选 image 后解析通过 |

pytest 警告只来自 ADK 的实验性 resumability 与 SDK deprecation 提示。测试未设置真实模型
开关或凭据，未访问真实 LLM。

## 制品

- wheel：`nexus_kernel_starter-0.2.0-py3-none-any.whl`，129,195 bytes，SHA-256
  `dd73b3b9bccdd8654036416c059bf72eff089827a8daa0bbbca662826a801916`。
- default 镜像：453,247,992 bytes，内容 ID
  `sha256:acb5661ed2fab774e2e312ce16d4243bc8335ff968e752845ac6ef98fd029622`，低于
  475 MiB 门槛。
- full 镜像：610,199,865 bytes，内容 ID
  `sha256:8a202ad6d333960e6e70b598b8a82adc855fb1e2c10e5823a41a39af59ea35da`，低于
  640 MiB 门槛。
- default/full 均以 `10001:10001` 运行；运行时根文件系统只读，`/tmp` 为 tmpfs，
  `no-new-privileges:true` 生效。

`scripts/security_scan.py` 只表示源码敏感信息合同扫描，`pip check` 只表示已安装依赖一致性；
二者不代表镜像或 Python 包漏洞扫描。本记录不把它们表述为 CVE 证据。

## 问题台账

| ID | 级别 | 问题与影响 | 状态 |
| --- | --- | --- | --- |
| ADK24-001 | P1 | 新增源码、脚本和测试未跟踪会从 commit/wheel 消失 | **已关闭**：相关内容已进入前四个提交，干净 HEAD 全门禁通过 |
| STAB-001 | P2/发布 | preprod/prod Compose 未显式设置覆盖 Agent Runs 最长 900 秒 drain 的 `stop_grace_period`；平台默认值可能提前终止排空 | **开放**：生产发布前需明确平台停止预算或形成书面豁免 |
| STAB-002 | 发布门禁 | 真实 Provider、单实例 60 分钟/30 run canary 无可用环境和 Secret | **未执行** |
| STAB-003 | 发布门禁 | GitLab pipeline、macOS runner 和真实 Windows/PowerShell 环境未执行 | **未执行**；PowerShell 静态合同和 CRLF 属性已通过 |
| STAB-004 | 发布门禁 | 镜像与依赖 CVE 扫描不在本地源码扫描范围内 | **未执行** |

## Canary 与回退

Canary 仅向内部主体开放，并使用单实例或粘性路由。验收要求 cleanup debt 为 0，phase P95
低于 deadline 的 80%，队列在一个 phase deadline 内归零。真实 Provider 的 429/5xx 单独
统计，不得归为框架成功。

回退先停止接纳新 run，保留已有 run 的查询、恢复和取消；最多排空 900 秒后强制取消残余
run，确认 generator、Runner、session 与 Provider 清理收敛，再关闭功能开关并替换实例。
任何 cleanup debt、非终态 backlog 或 degraded 未恢复均判定为 No-Go。

## 签署边界

框架稳定性本地提交门禁：**通过**。生产发布门禁：**No-Go**，直到 STAB-001 至 STAB-004
关闭或按发布制度获得限时书面豁免。后续若 CI 发现问题，只允许新增前向修复提交，不改写
本轮提交历史。

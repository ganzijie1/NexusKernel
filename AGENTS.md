# 项目准则

- 保持 `app -> nexus_kernel` 单向依赖；业务、鉴权和装配归 `app`，框架不得反向导入应用。
- 需要 Python 环境时，只能使用 `scripts/setup_python_env.sh` 或 Windows 的 `scripts/setup_python_env.ps1` 创建 Python 3.11-3.13 venv。
- 配置、env 示例、Docker、CI、文档和测试必须同步；`pyproject.toml` 是唯一依赖来源，不维护 `requirements.txt`。
- 不提交或输出密钥、Authorization、凭据 URL、Prompt、工具载荷或模型原始响应。
- 用户文档使用中文；代码符号保持英文，文本使用 UTF-8 并遵循 `.gitattributes`。
- 修改前阅读相关实现、配置和测试，只改当前任务范围；行为变更必须补充测试，完成后运行相关测试及 `git diff --check`，涉及文本格式时运行 `git ls-files --eol`，最后运行 `git status --short --branch`；未经明确要求不得暂存、提交、推送、切换分支或改写历史。

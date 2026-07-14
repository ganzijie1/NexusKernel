# NexusKernel Template Design

## Purpose And Boundary

NexusKernel Starter is a reusable modular AI service foundation. This bootstrap task establishes only the Python packaging, reproducible environment entry point, repository safety rules, and one-time template initializer. Kernel, Gateway, provider, and application runtime code are explicitly outside this task.

The source repositories `D:/work/ai-pet` and `D:/work/LoongMemory` are reference-only. The new repository is rebuilt from an explicit whitelist and must not copy business modules, credentials, private addresses, generated artifacts, or tracked environment files.

## Runtime Direction

The finished framework will expose three AI runtime choices with a shared service contract:

- OpenAI-compatible through the `openai` SDK.
- LiteLLM for its supported provider matrix.
- Google ADK for agent and session-oriented integrations.

The bootstrap installs all three real AI dependencies through the `ai` extra so the default developer environment exercises the intended dependency path. Runtime implementation remains a later task.

An application without credentials must still start. Liveness remains healthy, readiness reports a degraded state without revealing secrets, and AI requests return a stable service-unavailable response. No bootstrap or initialization step may require a credential.

## Template Initialization

`scripts/init_project.sh` is a Bash entry point to a Python initializer. It performs a one-time, in-place initialization with an explicit display name, distribution slug, environment-variable prefix, and port. All arguments and required files are validated before any write.

Only documented template files are eligible for replacement: the root README and project metadata, YAML configuration, Docker build/Compose/environment examples and documentation, GitLab CI, and product documentation. Replacements are limited to `NexusKernel Starter`, `nexuskernel-starter`, `NEXUSKERNEL`, and the standalone default port `8030`. The Python import package `nexuskernel` is permanent.

All changed and generated files are prepared as temporary siblings. Commit failures restore original bytes, remove newly generated environment files, and leave no initialization marker. A successful run generates real Docker environment files from `*.example` files and writes `.nexuskernel-initialized`; subsequent runs are rejected.

## Acceptance

- Python 3.11 is created only through `scripts/setup_python_env.sh`, using `.venv-win` on Windows-family shells and `.venv-linux` on Linux.
- `pyproject.toml` is the sole dependency source and defines the requested core, AI, optional infrastructure, and development groups.
- Git and Docker ignore rules exclude credentials, virtual environments, caches, build output, and logs while Git continues to allow example environment files.
- Automated tests prove valid initialization, argument rejection, one-time execution, failure safety, package-name preservation, environment-script contracts, dependency groups, and ignore behavior.
- The generated `.venv-win` passes the focused test suite, Ruff, mypy, and `pip check` before the bootstrap commit is created.

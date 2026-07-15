# NexusKernel Starter

Python 3.11 modular AI service template. The reusable framework lives in
`nexuskernel/`; project policy and business modules live in `app/`. The import
package name `nexuskernel` is permanent even after template initialization.

## Initialize a new project

Run once in a fresh repository clone:

```bash
bash scripts/init_project.sh \
  --name "My Service" \
  --slug my-service \
  --env-prefix MY_SERVICE \
  --port 8030
```

The initializer validates every argument before writing, changes only the
documented metadata/configuration/Docker/README whitelist, generates ignored
local Docker env files, and refuses a second run.

## Python environment

Use the required Bash entry point. It creates `.venv-win` on Windows-family
shells and `.venv-linux` on Linux.

```bash
bash scripts/setup_python_env.sh
```

On this Windows workstation the verified command is:

```powershell
& 'D:\Program Files\Git\bin\bash.exe' scripts/setup_python_env.sh
```

## Run

No external service or AI credential is needed to start:

```powershell
.\.venv-win\Scripts\python.exe -m app.launcher
```

Default URL: `http://127.0.0.1:8030`.

- `GET /health/live`
- `GET /health/ready`
- `GET /metrics`
- `POST /api/v1/example/echo`
- `POST /api/v1/chat`

Without AI credentials, liveness is healthy and readiness is `degraded`.
Chat returns `503 auth_not_configured` until
`NEXUSKERNEL_SERVICE_BEARER_TOKEN` is set. With a configured service token,
missing/incorrect Bearer tokens return `401`; missing AI credentials return
`503 ai_provider_unavailable`.

Select `openai_compatible`, `litellm`, or `google_adk` through
`NEXUSKERNEL_AI_PROVIDER`; configure the model, API key, and optional base URL in
the generated local env file. Streaming and structured output are outside V1.

## Quality gates

```powershell
.\.venv-win\Scripts\python.exe -m compileall -q nexuskernel app
.\.venv-win\Scripts\python.exe -m pytest -m "not real_llm" -q
.\.venv-win\Scripts\ruff.exe format --check .
.\.venv-win\Scripts\ruff.exe check .
.\.venv-win\Scripts\mypy.exe nexuskernel app scripts\init_project.py
.\.venv-win\Scripts\python.exe -m pip check
.\.venv-win\Scripts\python.exe scripts\security_scan.py
```

See `docs/architecture.md`, `docs/security.md`, and `docker/README.md` for the
runtime boundaries and container workflows. GitLab CI performs quality, test,
wheel, and non-root container smoke stages; it does not push or deploy.

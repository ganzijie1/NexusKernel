# Project Bootstrap And Template Tooling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Establish a Python 3.11 project entry point and a safe, one-time in-place initializer for NexusKernel Starter.

**Architecture:** `pyproject.toml` is the only dependency manifest. A strict Bash environment builder owns virtual-environment creation, while a thin Bash initializer delegates validation, whitelist discovery, transformation, staging, rollback, and marker creation to a focused Python module.

**Tech Stack:** Bash, Python 3.11 standard library, setuptools, pytest, Ruff, mypy

---

### Task 1: Executable Contracts

**Files:**
- Create: `tests/tooling/test_project_contracts.py`
- Create: `tests/tooling/test_init_project.py`
- Create: `tests/tooling/test_setup_python_env.py`

- [ ] **Step 1: Write failing project-contract tests**

Assert exact dependency lists parsed through `tomllib`, package discovery for `nexuskernel*` and `app*`, absence of requirements manifests, Git ignore handling through `git check-ignore`, Docker context exclusions, and the strict shell/environment contract.

- [ ] **Step 2: Write failing initializer tests**

Build each case in a temporary repository fixture, copy only the initializer entry points into that fixture, and execute them with a subprocess. Cover valid replacements and generated env files, invalid slug/prefix/port, missing required files, second-run rejection, and preservation of standalone `nexuskernel` imports and package discovery.

- [ ] **Step 3: Verify RED**

Run:

```powershell
D:\work\LoongMemory\.venv\Scripts\python.exe -m pytest tests/tooling -q
```

Expected: collection succeeds and tests fail because the project metadata and scripts do not exist yet.

### Task 2: Static Project Foundation

**Files:**
- Create: `pyproject.toml`
- Create: `nexuskernel/__init__.py`
- Create: `app/__init__.py`
- Create: `.gitignore`
- Create: `.dockerignore`
- Create: `.gitattributes`
- Modify: `README.md`

- [ ] **Step 1: Add the sole dependency manifest**

Define setuptools packaging, Python `>=3.11,<3.12`, the exact core pins, separate `ai`, `mongo`, `redis`, `qdrant`, `otel`, and `dev` extras, and pytest/Ruff/mypy configuration.

- [ ] **Step 2: Add import packages and repository safety rules**

Create minimal package initializers. Ignore `.venv-win`, `.venv-linux`, Python/tool caches, builds, logs, and all real environment files; explicitly unignore `*.example`. Exclude those same sensitive/generated paths from Docker context.

- [ ] **Step 3: Give README a template identity**

Use `NexusKernel Starter`, `nexuskernel-starter`, `NEXUSKERNEL`, and `8030` only as initializer-owned identity defaults while documenting that the package remains `nexuskernel`.

### Task 3: Python Environment Entry Point

**Files:**
- Create: `scripts/setup_python_env.sh`

- [ ] **Step 1: Implement strict option and platform handling**

Parse `--python`, `--recreate`, and `--help`; require Python 3.11; map MSYS/MINGW/CYGWIN to `.venv-win` and Linux to `.venv-linux`.

- [ ] **Step 2: Implement safe reuse and recreation**

Only remove a path proven equal to one of the two project-root environment paths. Reuse an existing environment only when its interpreter is Python 3.11; otherwise require `--recreate`.

- [ ] **Step 3: Install the real development path**

Upgrade pip, run editable installation of `.[ai,dev]`, and print `sys.executable` from the selected environment.

### Task 4: Atomic One-Time Initializer

**Files:**
- Create: `scripts/init_project.sh`
- Create: `scripts/init_project.py`

- [ ] **Step 1: Validate the fixed CLI before discovery**

Require non-empty single-line display name, slug matching `^[a-z0-9]+(?:-[a-z0-9]+)*$`, prefix matching `^[A-Z][A-Z0-9_]*$`, and an integer port from 1 through 65535. Reject an existing marker and require root `README.md` and `pyproject.toml`.

- [ ] **Step 2: Discover only whitelisted files**

Include root metadata, top-level YAML configuration, Docker Dockerfile/Compose/env-example/README files, GitLab CI, and Markdown product documents. Reject symlinked or root-escaping targets.

- [ ] **Step 3: Precompute and stage changes**

Replace only the three identity defaults and standalone `8030`. Generate each real Docker env path by removing `.example`; fail rather than overwrite an existing real environment file. Write and fsync temporary siblings for every output before replacement.

- [ ] **Step 4: Commit with rollback**

Replace staged files deterministically, write `.nexuskernel-initialized` last, and on any exception restore original bytes and remove every newly created output before returning a non-zero exit.

### Task 5: Green Verification And Delivery

**Files:**
- Verify all files above

- [ ] **Step 1: Run focused tests with the existing interpreter**

```powershell
D:\work\LoongMemory\.venv\Scripts\python.exe -m pytest tests/tooling -q
```

Expected: all tooling tests pass.

- [ ] **Step 2: Create the mandated environment through Bash**

```powershell
& 'D:\Program Files\Git\bin\bash.exe' scripts/setup_python_env.sh
```

Expected: `.venv-win` is created or validly reused, `.[ai,dev]` is installed, and the final line identifies `.venv-win\Scripts\python.exe`.

- [ ] **Step 3: Run the final quality gate from `.venv-win`**

```powershell
.\.venv-win\Scripts\python.exe -m pytest tests/tooling -q
.\.venv-win\Scripts\ruff.exe check .
.\.venv-win\Scripts\mypy.exe nexuskernel app scripts/init_project.py
.\.venv-win\Scripts\python.exe -m pip check
```

Expected: every command exits zero with no failures.

- [ ] **Step 4: Review scope and create one local commit**

Inspect `git diff --check`, the complete diff, and staged paths. Commit only the bootstrap/tooling files with:

```powershell
git commit -m "build: establish project bootstrap and template tooling"
```

Do not push.

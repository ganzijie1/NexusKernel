from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from scripts.security_scan import scan
from scripts.verify_wheel import load_project_contract, validate_wheel


ROOT = Path(__file__).resolve().parents[2]


def _bash() -> str:
    candidates = [
        r"D:\Program Files\Git\bin\bash.exe",
        shutil.which("bash"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    pytest.skip("Bash is required for the full-template initializer test")


def test_full_repository_copy_initializes_consistently(tmp_path: Path) -> None:
    target = tmp_path / "template"
    shutil.copytree(
        ROOT,
        target,
        ignore=shutil.ignore_patterns(
            ".git",
            ".venv-win",
            ".venv-linux",
            ".pytest_cache",
            ".mypy_cache",
            ".ruff_cache",
            "__pycache__",
            "dist",
            "build",
            "*.egg-info",
        ),
    )
    arguments = [
        _bash(),
        "scripts/init_project.sh",
        "--name",
        "Atlas Service",
        "--slug",
        "atlas-service",
        "--env-prefix",
        "ATLAS",
        "--port",
        "9124",
    ]

    first = subprocess.run(
        arguments,
        cwd=target,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    second = subprocess.run(
        arguments,
        cwd=target,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    assert first.returncode == 0, first.stderr
    assert second.returncode != 0
    assert "already initialized" in second.stderr
    assert 'name = "atlas-service"' in (target / "pyproject.toml").read_text(
        encoding="utf-8"
    )
    assert 'include = ["nexus_kernel*", "app*"]' in (
        target / "pyproject.toml"
    ).read_text(encoding="utf-8")
    application = (target / "config/application.yaml").read_text(encoding="utf-8")
    modules = (target / "config/modules.yaml").read_text(encoding="utf-8")
    providers = (target / "config/providers.yaml").read_text(encoding="utf-8")
    readme = (target / "README.md").read_text(encoding="utf-8")
    dockerfile = (target / "docker/Dockerfile").read_text(encoding="utf-8")
    compose = (target / "docker/docker-compose-dev.yml").read_text(encoding="utf-8")
    assert "${ATLAS_PORT:-9124}" in application
    assert "ATLAS_AI_API_KEY" in modules
    assert "ATLAS_MONGO_DSN" in providers
    assert "ATLAS_POSTGRES_DSN" in providers
    assert "--port 9124" in readme
    assert "127.0.0.1:9124" in readme
    assert "EXPOSE 9124" in dockerfile
    assert "ARG ATLAS_RUNTIME_EXTRAS=ai" in dockerfile
    assert "${ATLAS_PORT:-9124}:${ATLAS_PORT:-9124}" in compose
    product_documents = [
        target / "README.md",
        target / "docker/README.md",
        *(
            path
            for path in (target / "docs").rglob("*.md")
            if "superpowers" not in path.parts
        ),
    ]
    documentation = "\n".join(
        path.read_text(encoding="utf-8") for path in product_documents
    )
    for old_value in (
        "NexusKernel Starter",
        "nexus-kernel-starter",
        "NEXUS_KERNEL",
        "8030",
    ):
        assert old_value not in documentation
    for new_value in ("Atlas Service", "atlas-service", "ATLAS", "9124"):
        assert new_value in documentation
    assert "nexus_kernel" in documentation
    for profile in ("dev", "preprod", "prod"):
        generated = target / f"docker/.env.{profile}"
        assert generated.is_file()
        assert "ATLAS_PORT=9124" in generated.read_text(encoding="utf-8")
        assert generated.stat().st_mode & 0o777 == 0o600
    marker = json.loads(
        (target / ".nexus-kernel-initialized").read_text(encoding="utf-8")
    )
    assert marker["slug"] == "atlas-service"
    from scripts import security_scan

    original_root = security_scan.ROOT
    try:
        security_scan.ROOT = target
        assert scan() == []
    finally:
        security_scan.ROOT = original_root

    contract_result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "tests/config/test_project_tooling.py::test_pyproject_defines_build_and_core_project_contract",
            "tests/deployment/test_artifacts.py::test_dockerfile_is_multistage_non_root_and_role_driven",
            "tests/deployment/test_artifacts.py::test_compose_files_are_parseable_and_have_no_external_or_fixed_networks",
            "tests/deployment/test_artifacts.py::test_gitlab_pipeline_has_no_push_or_deploy_commands",
            "tests/deployment/test_artifacts.py::test_gitlab_installs_built_wheel_with_all_runtime_extras_on_all_pythons",
            "tests/deployment/test_artifacts.py::test_gitlab_container_smoke_builds_all_runtime_extras",
            "-q",
        ],
        cwd=target,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert contract_result.returncode == 0, (
        contract_result.stdout + contract_result.stderr
    )

    contract = load_project_contract(target)
    dist_info = contract.dist_info_directory
    wheel = target / "atlas_service-0.2.0-py3-none-any.whl"
    entries = {
        "nexus_kernel/__init__.py": "",
        "app/__init__.py": "",
        f"{dist_info}/METADATA": (
            "Metadata-Version: 2.4\n"
            "Name: atlas-service\n"
            "Version: 0.2.0\n"
            "Requires-Python: >=3.11,<3.14\n"
            + "".join(
                f"Requires-Dist: {dependency}\n" for dependency in contract.dependencies
            )
            + "".join(
                f"Provides-Extra: {extra}\n" for extra in contract.optional_dependencies
            )
            + "".join(
                f'Requires-Dist: {dependency}; extra == "{extra}"\n'
                for extra, dependencies in contract.optional_dependencies.items()
                for dependency in dependencies
            )
            + "\n"
        ),
        f"{dist_info}/WHEEL": (
            "Wheel-Version: 1.0\n"
            "Generator: initializer-test\n"
            "Root-Is-Purelib: true\n"
            "Tag: py3-none-any\n\n"
        ),
    }
    record = f"{dist_info}/RECORD"
    rows = []
    for name, content in entries.items():
        data = content.encode("utf-8")
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
        rows.append(f"{name},sha256={digest.decode('ascii')},{len(data)}\n")
    entries[record] = "".join((*rows, f"{record},,\n"))
    with ZipFile(wheel, "w", ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)

    assert validate_wheel(wheel, contract) == []

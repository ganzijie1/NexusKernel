from __future__ import annotations

import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
REQUIRED_DOCS = (
    "README.md",
    "docs/architecture.md",
    "docs/development.md",
    "docs/configuration.md",
    "docs/api.md",
    "docs/security.md",
    "docs/migration-0.2.md",
    "docs/extraction-audit.md",
    "docker/README.md",
)
SKILLS = (
    "nexus-kernel-module-development",
    "nexus-kernel-ai-integration",
    "nexus-kernel-config-and-environment",
    "nexus-kernel-review-and-delivery",
)
VERSION_GUIDANCE = (
    "README.md",
    "AGENTS.md",
    "docs/development.md",
    "docs/configuration.md",
    "docker/README.md",
    "docs/superpowers/specs/2026-07-15-nexus-kernel-template-design.md",
    "docs/superpowers/plans/2026-07-15-project-bootstrap-and-template-tooling.md",
)
MARKDOWN_LINK = re.compile(r"\[[^]]+]\(([^)]+)\)")
CJK = re.compile(r"[\u4e00-\u9fff]")


def test_project_guidance_is_chinese_and_complete() -> None:
    combined = ""
    for relative_path in REQUIRED_DOCS:
        path = ROOT / relative_path
        assert path.is_file(), relative_path
        content = path.read_text(encoding="utf-8")
        assert CJK.search(content), relative_path
        combined += content
    assert "nexus_kernel" in combined
    assert "NEXUS_KERNEL" in combined


def test_python_version_policy_is_consistent_in_guidance_and_skills() -> None:
    paths = [ROOT / relative_path for relative_path in VERSION_GUIDANCE]
    paths.extend(ROOT / ".codex/skills" / skill / "SKILL.md" for skill in SKILLS)

    for path in paths:
        content = path.read_text(encoding="utf-8")
        assert "Python 3.11-3.13" in content, path
        assert ">=3.11,<3.12" not in content, path


def test_project_markdown_relative_links_resolve() -> None:
    paths = [ROOT / path for path in REQUIRED_DOCS]
    paths.append(ROOT / "AGENTS.md")
    for skill in SKILLS:
        paths.append(ROOT / ".codex" / "skills" / skill / "SKILL.md")

    for path in paths:
        content = path.read_text(encoding="utf-8")
        for target in MARKDOWN_LINK.findall(content):
            clean_target = target.strip("<>").split("#", 1)[0]
            if not clean_target or "://" in clean_target:
                continue
            assert (path.parent / clean_target).resolve().exists(), (path, target)


def test_guidance_documents_hardening_contracts() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    development = (ROOT / "docs/development.md").read_text(encoding="utf-8")
    configuration = (ROOT / "docs/configuration.md").read_text(encoding="utf-8")
    security = (ROOT / "docs/security.md").read_text(encoding="utf-8")
    audit = (ROOT / "docs/extraction-audit.md").read_text(encoding="utf-8")
    docker = (ROOT / "docker/README.md").read_text(encoding="utf-8")

    assert "无需 PATH 中存在全局 Python" in readme + configuration
    assert "目标项目的 `pyproject.toml`" in readme + development
    assert "security-test-fixture" in security
    assert "同一行" in security
    assert "不跳过整个 `tests/`" in security + audit
    assert "未知顶层目录" in development + audit
    assert "已配置、待执行验证" in docker


def test_readme_documents_complete_framework_and_module_contract() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    required_structure = (
        "app/modules/<module_name>/",
        "module.py",
        "api/",
        "services/",
        "repositories.py",
        "workers.py",
    )
    required_boundaries = (
        "ModuleContext.require_module()",
        "optional_module()",
        "require_provider()",
        "optional_provider()",
        "Module.get_worker_specs()",
        "PostgreSQL",
        "NEXUS_KERNEL_RUNTIME_EXTRAS",
        "`pyproject.toml` 是唯一依赖来源",
        "不维护 `requirements.txt`",
    )

    assert all(value in readme for value in required_structure)
    assert all(value in readme for value in required_boundaries)


def test_guidance_documents_optional_infrastructure_installation_contract() -> None:
    configuration = (ROOT / "docs/configuration.md").read_text(encoding="utf-8")
    development = (ROOT / "docs/development.md").read_text(encoding="utf-8")
    docker = (ROOT / "docker/README.md").read_text(encoding="utf-8")
    audit = (ROOT / "docs/extraction-audit.md").read_text(encoding="utf-8")
    combined = configuration + development + docker + audit

    assert "postgres" in combined
    assert "asyncpg" in combined
    assert "NEXUS_KERNEL_RUNTIME_EXTRAS" in docker
    assert "传递依赖" in configuration
    assert "requirements.txt" in configuration


def test_project_skills_include_hardening_boundaries() -> None:
    config_skill = (
        ROOT / ".codex/skills/nexus-kernel-config-and-environment/SKILL.md"
    ).read_text(encoding="utf-8")
    delivery_skill = (
        ROOT / ".codex/skills/nexus-kernel-review-and-delivery/SKILL.md"
    ).read_text(encoding="utf-8")
    module_skill = (
        ROOT / ".codex/skills/nexus-kernel-module-development/SKILL.md"
    ).read_text(encoding="utf-8")
    ai_skill = (ROOT / ".codex/skills/nexus-kernel-ai-integration/SKILL.md").read_text(
        encoding="utf-8"
    )

    assert "无需全局 Python" in config_skill
    assert "pyproject.toml" in delivery_skill
    assert "已配置、待执行验证" in delivery_skill
    assert "security-test-fixture" in module_skill
    assert "security-test-fixture" in ai_skill
    assert "pyproject.toml" in ai_skill
    assert "`ai` extra" in ai_skill
    assert "requirements.txt" in ai_skill
    assert "app/modules/<module_name>/module.py" in module_skill
    assert "get_worker_specs" in module_skill
    assert "config/providers.yaml" in config_skill
    assert "postgres" in config_skill
    assert "NEXUS_KERNEL_RUNTIME_EXTRAS" in delivery_skill
    assert "Requires-Dist" in delivery_skill


def test_agents_file_is_concise_and_preserves_core_rules() -> None:
    path = ROOT / "AGENTS.md"
    content = path.read_text(encoding="utf-8")
    rules = [line for line in content.splitlines() if line.startswith("- ")]

    assert 4 <= len(rules) <= 6
    assert "app -> nexus_kernel" in content
    assert "scripts/setup_python_env.sh" in content
    assert "Python 3.11" in content
    assert "密钥" in content
    assert "中文" in content


def test_project_skills_have_valid_minimal_structure() -> None:
    skills_root = ROOT / ".codex" / "skills"
    assert sorted(
        path.name for path in skills_root.iterdir() if path.is_dir()
    ) == sorted(SKILLS)

    forbidden = ("LoongMemory", "AI-PET", "tenant_id", "memory_records")
    for name in SKILLS:
        skill_root = skills_root / name
        skill_path = skill_root / "SKILL.md"
        metadata_path = skill_root / "agents" / "openai.yaml"
        assert skill_path.is_file()
        assert metadata_path.is_file()

        content = skill_path.read_text(encoding="utf-8")
        frontmatter = content.split("---", 2)[1]
        metadata = yaml.safe_load(frontmatter)
        assert metadata == {
            "name": name,
            "description": metadata["description"],
        }
        assert metadata["description"].startswith("Use when")
        assert CJK.search(content)
        assert not any(value in content for value in forbidden)

        interface = yaml.safe_load(metadata_path.read_text(encoding="utf-8"))[
            "interface"
        ]
        assert set(interface) == {
            "display_name",
            "short_description",
            "default_prompt",
        }
        assert f"${name}" in interface["default_prompt"]

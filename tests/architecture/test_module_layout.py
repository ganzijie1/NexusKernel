from __future__ import annotations

from pathlib import Path

from nexus_kernel.config import load_module_definitions


ROOT = Path(__file__).resolve().parents[2]


def test_application_modules_use_package_module_layout() -> None:
    modules_root = ROOT / "app" / "modules"
    missing = [
        path.relative_to(ROOT).as_posix()
        for name in ("example", "chat", "agent_runs")
        for path in (
            modules_root / name / "__init__.py",
            modules_root / name / "module.py",
        )
        if not path.is_file()
    ]
    obsolete = [
        path.relative_to(ROOT).as_posix()
        for path in (modules_root / "example.py", modules_root / "chat.py")
        if path.exists()
    ]

    assert missing == []
    assert obsolete == []


def test_project_module_class_paths_target_module_files() -> None:
    settings = load_module_definitions(ROOT / "config" / "modules.yaml")

    assert settings.modules["example"].class_path == (
        "app.modules.example.module.ExampleModule"
    )
    assert settings.modules["chat"].class_path == "app.modules.chat.module.ChatModule"
    assert settings.modules["chat"].dependencies == []
    assert settings.modules["chat"].optional_dependencies == []
    agent_runs = settings.modules["agent_runs"]
    assert agent_runs.class_path == "app.modules.agent_runs.module.AgentRunsModule"
    assert agent_runs.enabled is False
    assert agent_runs.required is False
    assert agent_runs.config == {
        "max_entries": 256,
        "max_concurrent_phases": 4,
        "phase_timeout_seconds": 300,
        "overall_lifetime_seconds": 3600,
        "action_timeout_seconds": 900,
        "terminal_retention_seconds": 3600,
        "max_resumes": 16,
        "max_payload_bytes": 65536,
        "poll_interval_seconds": 1,
    }

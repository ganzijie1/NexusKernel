from __future__ import annotations

from pathlib import Path

from nexus_kernel.config import load_module_definitions


ROOT = Path(__file__).resolve().parents[2]


def test_application_modules_use_package_module_layout() -> None:
    modules_root = ROOT / "app" / "modules"
    missing = [
        path.relative_to(ROOT).as_posix()
        for name in ("example", "chat")
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

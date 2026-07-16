from __future__ import annotations

import ast
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _imports_under(root: Path) -> list[tuple[Path, str]]:
    imports: list[tuple[Path, str]] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend((path, alias.name) for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                relative_prefix = "." * node.level
                imports.append((path, relative_prefix + (node.module or "")))
    return imports


def test_import_scanner_preserves_relative_import_levels(tmp_path: Path) -> None:
    package = tmp_path / "gateway"
    package.mkdir()
    (package / "sample.py").write_text(
        "from .. import ai\nfrom ..ai import Client\n", encoding="utf-8"
    )

    imported = [name for _, name in _imports_under(package)]

    assert imported == ["..", "..ai"]


def test_framework_uses_absolute_imports() -> None:
    violations = [
        path.relative_to(_PROJECT_ROOT)
        for path, imported in _imports_under(_PROJECT_ROOT / "nexus_kernel")
        if imported.startswith(".")
    ]
    assert violations == []


def test_framework_never_depends_on_application_layer() -> None:
    violations = [
        path.relative_to(_PROJECT_ROOT)
        for path, imported in _imports_under(_PROJECT_ROOT / "nexus_kernel")
        if imported == "app" or imported.startswith("app.")
    ]
    assert violations == []


def test_gateway_does_not_depend_on_ai_or_application_layer() -> None:
    violations = [
        (path.relative_to(_PROJECT_ROOT), imported)
        for path, imported in _imports_under(_PROJECT_ROOT / "nexus_kernel" / "gateway")
        if imported == "app"
        or imported.startswith("app.")
        or imported == "nexus_kernel.ai"
        or imported.startswith("nexus_kernel.ai.")
    ]
    assert violations == []


def test_providers_do_not_depend_on_kernel() -> None:
    violations = [
        (path.relative_to(_PROJECT_ROOT), imported)
        for path, imported in _imports_under(
            _PROJECT_ROOT / "nexus_kernel" / "providers"
        )
        if imported == "nexus_kernel.kernel"
        or imported.startswith("nexus_kernel.kernel.")
    ]
    assert violations == []


def test_application_layer_depends_on_framework_in_the_allowed_direction() -> None:
    imports = _imports_under(_PROJECT_ROOT / "app")
    assert any(
        imported == "nexus_kernel" or imported.startswith("nexus_kernel.")
        for _, imported in imports
    )

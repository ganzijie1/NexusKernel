from __future__ import annotations

import ast
import subprocess
import sys
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
                imported = relative_prefix + (node.module or "")
                for alias in node.names:
                    separator = "" if imported.endswith(".") else "."
                    imports.append((path, f"{imported}{separator}{alias.name}"))
    return imports


def _resolve_import_from_path(path: Path, package_root: Path, imported: str) -> str:
    if not imported.startswith("."):
        return imported
    level = len(imported) - len(imported.lstrip("."))
    relative = path.relative_to(package_root)
    package = ["app", "modules", *relative.parts[:-1]]
    keep = len(package) - (level - 1)
    if keep < 0:
        return imported
    suffix = imported[level:]
    if suffix:
        return ".".join((*package[:keep], suffix))
    return ".".join(package[:keep])


def test_import_scanner_preserves_relative_import_levels(tmp_path: Path) -> None:
    package = tmp_path / "gateway"
    package.mkdir()
    (package / "sample.py").write_text(
        "from .. import ai\nfrom ..ai import Client\n", encoding="utf-8"
    )

    imported = [name for _, name in _imports_under(package)]

    assert imported == ["..ai", "..ai.Client"]


def test_relative_module_imports_resolve_to_their_application_owner(
    tmp_path: Path,
) -> None:
    modules_root = tmp_path / "app" / "modules"
    source = modules_root / "consumer" / "module.py"

    assert _resolve_import_from_path(source, modules_root, ".services") == (
        "app.modules.consumer.services"
    )
    assert _resolve_import_from_path(source, modules_root, "..base.services") == (
        "app.modules.base.services"
    )


def _private_cross_module_imports(modules_root: Path) -> list[tuple[Path, str]]:
    public_boundaries = {"api", "event", "protocol", "schema"}
    violations: list[tuple[Path, str]] = []
    for path, imported in _imports_under(modules_root):
        relative = path.relative_to(modules_root)
        imported = _resolve_import_from_path(path, modules_root, imported)
        if len(relative.parts) < 2:
            continue
        if imported == "app.modules":
            violations.append((path, imported))
            continue
        if not imported.startswith("app.modules."):
            continue
        source_owner = relative.parts[0]
        imported_parts = imported.split(".")
        if len(imported_parts) < 3:
            continue
        target_owner = imported_parts[2]
        if target_owner == source_owner:
            continue
        if len(imported_parts) == 3 or imported_parts[3] not in public_boundaries:
            violations.append((path, imported))
    return violations


def test_cross_module_import_scanner_rejects_alias_and_package_root_bypasses(
    tmp_path: Path,
) -> None:
    modules_root = tmp_path / "app" / "modules"
    source = modules_root / "consumer" / "module.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "import app.modules as modules_package\n"
        "from app import modules\n"
        "import app.modules.chat as chat_package\n"
        "from app.modules import chat\n"
        "from app.modules.chat import ChatModule, module\n"
        "from ..chat import ChatModule as RelativeChatModule\n"
        "from ..chat.services import ChatService\n"
        "from app.modules.chat.api import ChatApi\n"
        "from ..chat import protocol\n",
        encoding="utf-8",
    )

    violations = [
        imported for _, imported in _private_cross_module_imports(modules_root)
    ]

    assert violations == [
        "app.modules",
        "app.modules",
        "app.modules.chat",
        "app.modules.chat",
        "app.modules.chat.ChatModule",
        "app.modules.chat.module",
        "app.modules.chat.ChatModule",
        "app.modules.chat.services.ChatService",
    ]


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


def test_application_modules_do_not_import_other_modules_private_implementations() -> (
    None
):
    modules_root = _PROJECT_ROOT / "app" / "modules"
    violations = [
        (path.relative_to(_PROJECT_ROOT), imported)
        for path, imported in _private_cross_module_imports(modules_root)
    ]
    assert violations == []


def test_module_aggregator_and_bootstrap_do_not_preload_implementations() -> None:
    script = (
        "import sys\n"
        "import app.modules\n"
        "import app.bootstrap\n"
        "unexpected = sorted(name for name in sys.modules "
        "if name.startswith('app.modules.') and "
        "('.module' in name or '.services' in name or '.repositories' in name "
        "or '.workers' in name))\n"
        "assert unexpected == [], unexpected\n"
    )

    subprocess.run(
        [sys.executable, "-c", script],
        cwd=_PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

from __future__ import annotations

from pathlib import Path
import pytest

from scripts import security_scan


def _prepare_root(root: Path) -> None:
    (root / ".gitignore").write_text(
        ".env\n.env.*\n**/.env\n**/.env.*\n!*.example\n!**/*.example\n",
        encoding="utf-8",
    )
    (root / ".dockerignore").write_text(
        ".env\n.env.*\n**/.env\n**/.env.*\n!*.example\n!**/*.example\n",
        encoding="utf-8",
    )


@pytest.mark.parametrize("suffix", [".sh", ".ps1", ".json"])
def test_security_scan_checks_extended_text_types(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suffix: str
) -> None:
    _prepare_root(tmp_path)
    monkeypatch.setattr(security_scan, "ROOT", tmp_path)
    secret = "sk-" + "not-a-real-secret"  # security-test-fixture
    (tmp_path / f"unsafe{suffix}").write_text(secret, encoding="utf-8")

    errors = security_scan.scan()

    assert any("secret-shaped key" in error for error in errors)


def test_security_scan_checks_unmarked_test_fixtures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare_root(tmp_path)
    monkeypatch.setattr(security_scan, "ROOT", tmp_path)
    fixture = tmp_path / "tests/unit/test_fixture.py"
    fixture.parent.mkdir(parents=True)
    fixture.write_text("sk-" + "not-a-real-secret", encoding="utf-8")

    errors = security_scan.scan()

    assert any("tests/unit/test_fixture.py" in error for error in errors)


def test_security_scan_allows_marked_test_fixture_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare_root(tmp_path)
    monkeypatch.setattr(security_scan, "ROOT", tmp_path)
    fixture = tmp_path / "tests/unit/test_fixture.py"
    fixture.parent.mkdir(parents=True)
    fixture.write_text(
        "sk-" + "not-a-real-secret # security-test-fixture\n",
        encoding="utf-8",
    )

    assert security_scan.scan() == []


def test_security_scan_rejects_security_pattern_marker_without_a_finding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare_root(tmp_path)
    monkeypatch.setattr(security_scan, "ROOT", tmp_path)
    target = tmp_path / "docs/unsafe.txt"
    target.parent.mkdir(parents=True)
    target.write_text("security-" + "pattern", encoding="utf-8")

    assert any("unapproved" in error for error in security_scan.scan())


@pytest.mark.parametrize(
    ("relative_path", "marker"),
    [
        ("docs/unsafe.txt", "security-" + "pattern"),
        ("docs/unsafe.txt", "security-test-" + "fixture"),
    ],
)
def test_security_scan_rejects_suppression_markers_outside_allowed_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative_path: str,
    marker: str,
) -> None:
    _prepare_root(tmp_path)
    monkeypatch.setattr(security_scan, "ROOT", tmp_path)
    target = tmp_path / relative_path
    target.parent.mkdir(parents=True)
    target.write_text("sk-" + f"not-a-real-secret # {marker}", encoding="utf-8")

    errors = security_scan.scan()

    assert any("unapproved" in error for error in errors)


@pytest.mark.parametrize(
    "relative_path",
    ["scripts/security_scan.py", "nexus_kernel/observability/logging.py"],
)
def test_security_scan_allows_security_pattern_only_in_owned_definitions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative_path: str,
) -> None:
    _prepare_root(tmp_path)
    monkeypatch.setattr(security_scan, "ROOT", tmp_path)
    target = tmp_path / relative_path
    target.parent.mkdir(parents=True)
    marker = "security-" + "pattern"
    target.write_text(
        "sk-" + f"not-a-real-secret # {marker}\n",
        encoding="utf-8",
    )

    assert security_scan.scan() == []


@pytest.mark.parametrize("profile", ["dev", "preprod", "prod"])
def test_security_scan_allows_only_generated_untracked_env_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: str,
) -> None:
    _prepare_root(tmp_path)
    monkeypatch.setattr(security_scan, "ROOT", tmp_path)
    generated = tmp_path / f"docker/.env.{profile}"
    generated.parent.mkdir()
    generated.write_bytes(b"not-utf8-\xff")
    real_read_text = Path.read_text

    def guarded_read_text(path: Path, *args: object, **kwargs: object) -> str:
        assert path != generated, "generated environment contents must not be read"
        return real_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read_text)

    assert security_scan.scan() == []


@pytest.mark.parametrize(
    "relative_path",
    [".env", ".env.dev", "docker/.env", "docker/.env.local", "nested/.env.prod"],
)
def test_security_scan_rejects_other_real_env_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative_path: str,
) -> None:
    _prepare_root(tmp_path)
    monkeypatch.setattr(security_scan, "ROOT", tmp_path)
    target = tmp_path / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("VALUE=opaque\n", encoding="utf-8")

    assert any("real environment file" in error for error in security_scan.scan())


@pytest.mark.parametrize("ignore_name", [".gitignore", ".dockerignore"])
def test_generated_env_requires_both_ignore_contracts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ignore_name: str,
) -> None:
    _prepare_root(tmp_path)
    monkeypatch.setattr(security_scan, "ROOT", tmp_path)
    (tmp_path / ignore_name).write_text("!docker/.env.dev\n", encoding="utf-8")
    generated = tmp_path / "docker/.env.dev"
    generated.parent.mkdir()
    generated.write_text("VALUE=opaque\n", encoding="utf-8")

    assert any("real environment file" in error for error in security_scan.scan())


def test_generated_env_must_not_be_tracked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare_root(tmp_path)
    monkeypatch.setattr(security_scan, "ROOT", tmp_path)
    monkeypatch.setattr(
        security_scan,
        "_tracked_files",
        lambda: frozenset({"docker/.env.dev"}),
    )
    generated = tmp_path / "docker/.env.dev"
    generated.parent.mkdir()
    generated.write_text("VALUE=opaque\n", encoding="utf-8")

    assert any("real environment file" in error for error in security_scan.scan())


def test_generated_env_is_rejected_when_git_inventory_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare_root(tmp_path)
    monkeypatch.setattr(security_scan, "ROOT", tmp_path)
    (tmp_path / ".git").mkdir()
    generated = tmp_path / "docker/.env.dev"
    generated.parent.mkdir()
    generated.write_text("VALUE=opaque\n", encoding="utf-8")

    def unavailable(*args: object, **kwargs: object) -> object:
        raise FileNotFoundError

    monkeypatch.setattr(security_scan.subprocess, "run", unavailable)

    assert any("real environment file" in error for error in security_scan.scan())


@pytest.mark.parametrize(
    ("relative_path", "content"),
    [
        ("unsafe.py", "api_key = 'opaque-value'\n"),
        ("unsafe.py", "client(token='opaque-value')\n"),
        ("unsafe.py", "settings = {'password': 'opaque-value'}\n"),
        ("unsafe.yaml", "service:\n  credential: opaque-value\n"),
        ("unsafe.json", '{"authorization": "Bearer opaque-value"}\n'),
        ("unsafe.toml", 'client_secret = "opaque-value"\n'),
        ("unsafe.env.example", "ACCESS_TOKEN=opaque-value\n"),
        ("unsafe.ini", "passwd = opaque-value\n"),
        ("unsafe.sh", "export API_KEY=opaque-value\n"),
        ("unsafe.ps1", '$env:API_KEY = "opaque-value"\n'),
        ("unsafe.bat", "set SERVICE_TOKEN=opaque-value\n"),
        ("unsafe.cfg", "client_secret = opaque-value\n"),
        ("unsafe.conf", "credential = opaque-value\n"),
        ("unsafe.properties", "client.password=opaque-value\n"),
        ("unsafe.md", 'authorization = "Bearer opaque-value"\n'),
        ("unsafe.rst", "access_token = opaque-value\n"),
        ("unsafe.txt", "passwd = opaque-value\n"),
    ],
)
def test_security_scan_rejects_sensitive_literal_assignments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative_path: str,
    content: str,
) -> None:
    _prepare_root(tmp_path)
    monkeypatch.setattr(security_scan, "ROOT", tmp_path)
    target = tmp_path / relative_path
    target.write_text(content, encoding="utf-8")

    errors = security_scan.scan()

    assert any("sensitive literal assignment" in error for error in errors)
    assert all("opaque-value" not in error for error in errors)


@pytest.mark.parametrize(
    ("relative_path", "content"),
    [
        ("safe.py", "api_key = None\ntoken = '${SERVICE_TOKEN}'\n"),
        ("safe.yaml", "api_key: ${SERVICE_API_KEY:-}\npassword: null\n"),
        ("safe.json", '{"credential": null, "authorization": ""}\n'),
        ("safe.toml", 'client_secret = "${SERVICE_SECRET}"\n'),
        ("safe.env.example", "ACCESS_TOKEN=\nAPI_KEY=${SERVICE_API_KEY:-}\n"),
        ("safe.ini", "password = None\n"),
        ("safe.sh", "export API_KEY=$SERVICE_API_KEY\n"),
    ],
)
def test_security_scan_allows_empty_null_and_environment_placeholders(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative_path: str,
    content: str,
) -> None:
    _prepare_root(tmp_path)
    monkeypatch.setattr(security_scan, "ROOT", tmp_path)
    (tmp_path / relative_path).write_text(content, encoding="utf-8")

    assert security_scan.scan() == []


def test_security_scan_allows_marked_sensitive_assignment_in_tests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare_root(tmp_path)
    monkeypatch.setattr(security_scan, "ROOT", tmp_path)
    fixture = tmp_path / "tests/unit/test_fixture.py"
    fixture.parent.mkdir(parents=True)
    fixture.write_text(
        "api_key = 'opaque-value'  # security-test-fixture\n",
        encoding="utf-8",
    )

    assert security_scan.scan() == []


def test_security_scan_prunes_excluded_directories_before_descent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare_root(tmp_path)
    monkeypatch.setattr(security_scan, "ROOT", tmp_path)
    excluded = tmp_path / ".venv-linux/deep/package.py"
    excluded.parent.mkdir(parents=True)
    excluded.write_text("api_key = 'opaque-value'\n", encoding="utf-8")
    visited: list[Path] = []
    real_walk = security_scan.os.walk

    def recording_walk(*args: object, **kwargs: object):
        for directory, names, files in real_walk(*args, **kwargs):
            visited.append(Path(directory))
            yield directory, names, files

    monkeypatch.setattr(security_scan.os, "walk", recording_walk)

    assert security_scan.scan() == []
    assert not any(".venv-linux" in path.parts for path in visited)

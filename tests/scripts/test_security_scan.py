from __future__ import annotations

import os
import socket
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


@pytest.mark.parametrize("suffix", [".sh", ".ps1"])
def test_security_scan_checks_extended_text_types(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suffix: str
) -> None:
    _prepare_root(tmp_path)
    monkeypatch.setattr(security_scan, "ROOT", tmp_path)
    secret = "sk-" + "not-a-real-secret"  # security-test-fixture
    (tmp_path / f"unsafe{suffix}").write_text(secret, encoding="utf-8")

    errors = security_scan.scan()

    assert any("secret-shaped key" in error for error in errors)


def test_security_scan_checks_valid_json_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare_root(tmp_path)
    monkeypatch.setattr(security_scan, "ROOT", tmp_path)
    secret = "sk-" + "not-a-real-secret"  # security-test-fixture
    (tmp_path / "unsafe.json").write_text(
        f'{{"value": "{secret}"}}\n', encoding="utf-8"
    )

    assert any("secret-shaped key" in error for error in security_scan.scan())


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
        'VALUE = "sk-' + 'not-a-real-secret"  # security-test-fixture\n',
        encoding="utf-8",
    )

    assert security_scan.scan() == []


def test_security_pattern_text_does_not_suppress_a_finding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare_root(tmp_path)
    monkeypatch.setattr(security_scan, "ROOT", tmp_path)
    target = tmp_path / "docs/unsafe.txt"
    target.parent.mkdir(parents=True)
    marker = "security-" + "pattern"
    target.write_text("sk-" + f"not-a-real-secret # {marker}\n", encoding="utf-8")

    assert any("secret-shaped key" in error for error in security_scan.scan())


def test_security_scan_has_no_generic_line_suppression() -> None:
    source = Path(security_scan.__file__).read_text(encoding="utf-8")

    assert "PATTERN_MARKER" not in source
    assert "PATTERN_MARKER_ALLOWED" not in source


def test_security_scan_rejects_test_fixture_marker_outside_allowed_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_root(tmp_path)
    monkeypatch.setattr(security_scan, "ROOT", tmp_path)
    target = tmp_path / "docs/unsafe.txt"
    target.parent.mkdir(parents=True)
    marker = "security-test-" + "fixture"
    target.write_text("sk-" + f"not-a-real-secret # {marker}", encoding="utf-8")

    errors = security_scan.scan()

    assert any("unapproved" in error for error in errors)


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
    generated.chmod(0o600)
    real_read_text = Path.read_text

    def guarded_read_text(path: Path, *args: object, **kwargs: object) -> str:
        assert path != generated, "generated environment contents must not be read"
        return real_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read_text)

    assert security_scan.scan() == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are not NTFS ACLs")
@pytest.mark.parametrize("mode", [0o400, 0o640, 0o644, 0o660])
def test_generated_env_requires_exact_private_posix_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: int
) -> None:
    _prepare_root(tmp_path)
    monkeypatch.setattr(security_scan, "ROOT", tmp_path)
    generated = tmp_path / "docker/.env.dev"
    generated.parent.mkdir()
    generated.write_text("VALUE=opaque\n", encoding="utf-8")
    generated.chmod(mode)

    errors = security_scan.scan()

    assert any("real environment file" in error for error in errors)
    assert all("opaque" not in error for error in errors)


def test_generated_env_symlink_is_rejected_without_reading_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare_root(tmp_path)
    monkeypatch.setattr(security_scan, "ROOT", tmp_path)
    outside = tmp_path / "outside-value"
    outside.write_text("VALUE=must-not-be-read\n", encoding="utf-8")
    generated = tmp_path / "docker/.env.dev"
    generated.parent.mkdir()
    try:
        generated.symlink_to(outside)
    except OSError:
        pytest.skip("symbolic links are unavailable")

    errors = security_scan.scan()

    assert any("real environment file" in error for error in errors)
    assert all("must-not-be-read" not in error for error in errors)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO is unavailable")
def test_generated_env_fifo_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare_root(tmp_path)
    monkeypatch.setattr(security_scan, "ROOT", tmp_path)
    generated = tmp_path / "docker/.env.dev"
    generated.parent.mkdir()
    os.mkfifo(generated)

    assert any("real environment file" in error for error in security_scan.scan())


@pytest.mark.parametrize("ignore_name", [".gitignore", ".dockerignore"])
def test_ignore_symlink_is_rejected_without_reading_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ignore_name: str,
) -> None:
    _prepare_root(tmp_path)
    monkeypatch.setattr(security_scan, "ROOT", tmp_path)
    target = tmp_path / "ignore-target.bin"
    target.write_text("must-not-be-read\n", encoding="utf-8")
    ignore = tmp_path / ignore_name
    ignore.unlink()
    try:
        ignore.symlink_to(target)
    except OSError:
        pytest.skip("symbolic links are unavailable")
    real_read_text = Path.read_text

    def guarded_read_text(path: Path, *args: object, **kwargs: object) -> str:
        assert path != target, "ignore symlink target must not be read"
        return real_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read_text)

    errors = security_scan.scan()

    assert f"unscannable file type: {ignore_name}:1" in errors
    assert all("must-not-be-read" not in error for error in errors)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO is unavailable")
@pytest.mark.parametrize("ignore_name", [".gitignore", ".dockerignore"])
def test_ignore_fifo_is_rejected_without_blocking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ignore_name: str,
) -> None:
    _prepare_root(tmp_path)
    monkeypatch.setattr(security_scan, "ROOT", tmp_path)
    ignore = tmp_path / ignore_name
    ignore.unlink()
    os.mkfifo(ignore)

    assert f"unscannable file type: {ignore_name}:1" in security_scan.scan()


@pytest.mark.parametrize("ignore_name", [".gitignore", ".dockerignore"])
def test_ignore_file_must_be_utf8(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ignore_name: str,
) -> None:
    _prepare_root(tmp_path)
    monkeypatch.setattr(security_scan, "ROOT", tmp_path)
    (tmp_path / ignore_name).write_bytes(b"not-utf8-\xff")

    assert f"unscannable text file: {ignore_name}:1" in security_scan.scan()


def test_nontext_symlink_is_rejected_without_reading_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare_root(tmp_path)
    monkeypatch.setattr(security_scan, "ROOT", tmp_path)
    target = tmp_path / "outside-value"
    target.write_text("must-not-be-read", encoding="utf-8")
    linked = tmp_path / "payload.bin"
    try:
        linked.symlink_to(target)
    except OSError:
        pytest.skip("symbolic links are unavailable")

    errors = security_scan.scan()

    assert "unscannable file type: payload.bin:1" in errors
    assert all("must-not-be-read" not in error for error in errors)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO is unavailable")
def test_nontext_fifo_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare_root(tmp_path)
    monkeypatch.setattr(security_scan, "ROOT", tmp_path)
    fifo = tmp_path / "payload.bin"
    os.mkfifo(fifo)

    assert "unscannable file type: payload.bin:1" in security_scan.scan()


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="Unix sockets unavailable")
def test_nontext_socket_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare_root(tmp_path)
    monkeypatch.setattr(security_scan, "ROOT", tmp_path)
    socket_path = tmp_path / "payload.bin"
    with socket.socket(socket.AF_UNIX) as listener:
        try:
            listener.bind(str(socket_path))
        except OSError:
            pytest.skip("Unix socket creation is unavailable")
        assert "unscannable file type: payload.bin:1" in security_scan.scan()


def test_symlink_directory_is_rejected_before_descent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare_root(tmp_path)
    monkeypatch.setattr(security_scan, "ROOT", tmp_path)
    target = tmp_path / "target-directory"
    target.mkdir()
    linked = tmp_path / "linked-directory"
    try:
        linked.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symbolic links are unavailable")

    assert "unscannable file type: linked-directory:1" in security_scan.scan()


def test_excluded_symlink_directory_is_rejected_before_pruning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare_root(tmp_path)
    monkeypatch.setattr(security_scan, "ROOT", tmp_path)
    target = tmp_path / "target-directory"
    target.mkdir()
    linked = tmp_path / ".venv-linux"
    try:
        linked.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symbolic links are unavailable")

    assert "unscannable file type: .venv-linux:1" in security_scan.scan()


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
        ("settings.env", "ACCESS_TOKEN=opaque-value\n"),
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
        ("unsafe.py", "api_key = 'opaque-value'\ninvalid syntax\n"),
        ("unsafe.yaml", "token: [opaque-value\n"),
        ("unsafe.json", '{"api_key": "opaque-value"\n'),
    ],
)
def test_security_scan_fails_closed_on_invalid_structured_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative_path: str,
    content: str,
) -> None:
    _prepare_root(tmp_path)
    monkeypatch.setattr(security_scan, "ROOT", tmp_path)
    (tmp_path / relative_path).write_text(content, encoding="utf-8")

    errors = security_scan.scan()

    assert f"unscannable structured text: {relative_path}:1" in errors
    assert all("opaque-value" not in error for error in errors)


@pytest.mark.parametrize(
    "content",
    [
        "{api_key: null}\n",
        '{"value": 1, "value": 2}\n',
        '{"value": NaN}\n',
    ],
)
def test_security_scan_requires_strict_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str
) -> None:
    _prepare_root(tmp_path)
    monkeypatch.setattr(security_scan, "ROOT", tmp_path)
    (tmp_path / "unsafe.json").write_text(content, encoding="utf-8")

    assert security_scan.scan() == ["unscannable structured text: unsafe.json:1"]


@pytest.mark.parametrize(
    ("relative_path", "content"),
    [
        ("safe.py", "api_key = None\ntoken = '${SERVICE_TOKEN}'\n"),
        ("safe.py", "api_key += load_secret()\n"),
        ("safe.yaml", "api_key: ${SERVICE_API_KEY:-}\npassword: null\n"),
        ("safe.json", '{"credential": null, "authorization": ""}\n'),
        ("safe.toml", 'client_secret = "${SERVICE_SECRET}"\n'),
        ("safe.env.example", "ACCESS_TOKEN=\nAPI_KEY=${SERVICE_API_KEY:-}\n"),
        ("safe.ini", "password = None\n"),
        ("safe.sh", "export API_KEY=$SERVICE_API_KEY\n"),
        ("safe.sh", "API_KEY+=$SERVICE_API_KEY\n"),
        ("safe.ps1", "$env:API_KEY += $env:SERVICE_API_KEY\n"),
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


@pytest.mark.parametrize(
    "relative_path",
    [
        "private.key",
        "certificate.pem",
        "bundle.p12",
        "bundle.pfx",
        "credentials.json",
        "nested/service-account.json",
    ],
)
def test_security_scan_rejects_sensitive_artifacts_without_reading_contents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative_path: str,
) -> None:
    _prepare_root(tmp_path)
    monkeypatch.setattr(security_scan, "ROOT", tmp_path)
    target = tmp_path / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"must-not-be-read-\xff")

    errors = security_scan.scan()

    assert any(f"sensitive artifact: {relative_path}:1" in error for error in errors)
    assert all("must-not-be-read" not in error for error in errors)


@pytest.mark.parametrize(
    "relative_path",
    [".env.secret", ".envrc", "private.pem", "credentials.json"],
)
def test_security_scan_rejects_sensitive_directories_before_descent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative_path: str,
) -> None:
    _prepare_root(tmp_path)
    monkeypatch.setattr(security_scan, "ROOT", tmp_path)
    directory = tmp_path / relative_path
    directory.mkdir()
    (directory / "unreadable.bin").write_bytes(b"must-not-be-read-\xff")

    errors = security_scan.scan()

    expected = (
        "real environment file is present"
        if relative_path.startswith(".env")
        else "sensitive artifact"
    )
    assert errors == [f"{expected}: {relative_path}:1"]
    assert all("unreadable.bin" not in error for error in errors)


@pytest.mark.parametrize(
    "content",
    [
        "api_key = f'opaque-value'\n",
        "api_key = f\"{'opaque-value'!s}\"\n",
        "api_key = f\"{'opaque-value':>20}\"\n",
        'api_key = f"{load_secret()!r}"\n',
        "api_key = 'opaque-' * 2\n",
        "api_key = ''.join(('opaque', '-value'))\n",
        "api_key = SecretStr('opaque-value')\n",
        "api_key, ordinary = ('opaque-value', 'ordinary')\n",
        "api_key += 'opaque-value'\n",
    ],
)
def test_security_scan_rejects_static_python_secret_expressions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str
) -> None:
    _prepare_root(tmp_path)
    monkeypatch.setattr(security_scan, "ROOT", tmp_path)
    (tmp_path / "unsafe.py").write_text(content, encoding="utf-8")

    assert any(
        "sensitive literal assignment" in error for error in security_scan.scan()
    )


@pytest.mark.parametrize(
    ("relative_path", "content"),
    [
        ("unsafe.sh", "readonly API_KEY=opaque-value\n"),
        ("unsafe.sh", "readonly -a TOKEN=opaque-value\n"),
        ("unsafe.sh", "declare -xr API_KEY=opaque-value\n"),
        ("unsafe.sh", "declare -- API_KEY=opaque-value\n"),
        ("unsafe.sh", "typeset +x API_KEY=opaque-value\n"),
        ("unsafe.sh", "API_KEY+=opaque-value\n"),
        ("unsafe.ps1", '$env:API_KEY += "opaque-value"\n'),
        ("unsafe.ps1", 'Set-Item Env:API_KEY "opaque-value"\n'),
        (
            "unsafe.ps1",
            'Set-Item -Value "opaque-value" -Path Env:API_KEY\n',
        ),
        (
            "unsafe.ps1",
            "[Environment]::SetEnvironmentVariable('API_KEY', 'opaque-value')\n",
        ),
        (".envrc", "export API_KEY=opaque-value\n"),
    ],
)
def test_security_scan_rejects_extended_shell_secret_assignments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative_path: str,
    content: str,
) -> None:
    _prepare_root(tmp_path)
    monkeypatch.setattr(security_scan, "ROOT", tmp_path)
    (tmp_path / relative_path).write_text(content, encoding="utf-8")

    assert any(
        "sensitive literal assignment" in error for error in security_scan.scan()
    )


def test_security_scan_handles_yaml_alias_cycle_without_recursion_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare_root(tmp_path)
    monkeypatch.setattr(security_scan, "ROOT", tmp_path)
    (tmp_path / "safe.yaml").write_text("value: &loop [*loop]\n", encoding="utf-8")

    assert security_scan.scan() == []


def test_security_scan_reports_sensitive_yaml_alias_cycle_stably(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare_root(tmp_path)
    monkeypatch.setattr(security_scan, "ROOT", tmp_path)
    (tmp_path / "unsafe.yaml").write_text("token: &loop [*loop]\n", encoding="utf-8")

    assert security_scan.scan() == ["sensitive literal assignment: unsafe.yaml:1"]


def test_security_scan_checks_each_yaml_document_independently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare_root(tmp_path)
    monkeypatch.setattr(security_scan, "ROOT", tmp_path)
    (tmp_path / "unsafe.yaml").write_text(
        "value: first\n---\napi_key: opaque-value\n", encoding="utf-8"
    )

    assert security_scan.scan() == ["sensitive literal assignment: unsafe.yaml:3"]


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


def test_docker_context_findings_include_stable_line_number(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare_root(tmp_path)
    monkeypatch.setattr(security_scan, "ROOT", tmp_path)
    (tmp_path / ".dockerignore").write_text("", encoding="utf-8")

    errors = security_scan.scan()

    assert "Docker context does not exclude: .env:1" in errors
    assert "Docker context does not exclude: docker/.env.prod:1" in errors
    assert "Docker context does not exclude: nested/service/.env.secret:1" in errors

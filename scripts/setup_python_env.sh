#!/usr/bin/env bash
set -euo pipefail

case "${OSTYPE:-}" in
    msys*|mingw*|cygwin*)
        PATH="/usr/bin:/bin${PATH:+:$PATH}"
        export PATH
        ;;
esac

usage() {
    cat <<'EOF'
Usage: scripts/setup_python_env.sh [OPTIONS]

Create or reuse a project Python 3.11-3.13 virtual environment and install .[ai,dev].

Options:
  --python PATH  Supported Python interpreter used to create the environment.
  --recreate     Safely replace an existing project environment.
  --help         Show this help message.
EOF
}

fail() {
    printf 'Error: %s\n' "$1" >&2
    exit 1
}

python_command=""
recreate=false

while (($# > 0)); do
    case "$1" in
        --python)
            (($# >= 2)) || fail "--python requires an interpreter path"
            python_command="$2"
            shift 2
            ;;
        --python=*)
            python_command="${1#*=}"
            [[ -n "$python_command" ]] || fail "--python requires an interpreter path"
            shift
            ;;
        --recreate)
            recreate=true
            shift
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            fail "unknown option: $1"
            ;;
    esac
done

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
project_root="$(cd -- "$script_dir/.." && pwd -P)"
version_checker="$script_dir/check_python_version.py"

[[ -f "$version_checker" ]] || fail "missing Python version checker: $version_checker"

case "$(uname -s)" in
    MINGW*|MSYS*|CYGWIN*)
        environment_name=".venv-win"
        ;;
    Linux*)
        environment_name=".venv-linux"
        ;;
    Darwin*)
        environment_name=".venv-macos"
        ;;
    *)
        fail "unsupported platform: $(uname -s)"
        ;;
esac

environment_dir="$project_root/$environment_name"
environment_python=""

find_venv_python() {
    local target_environment="$1"
    local candidate

    for candidate in \
        "$target_environment/Scripts/python.exe" \
        "$target_environment/bin/python"; do
        if [[ -f "$candidate" && -x "$candidate" ]]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done

    return 1
}

validate_venv() {
    local interpreter="$1"
    local expected_environment="$2"

    "$interpreter" -c '
import sys
from pathlib import Path

expected = Path(sys.argv[1]).resolve()
prefix = Path(sys.prefix).resolve()
base_prefix = Path(sys.base_prefix).resolve()
raise SystemExit(0 if prefix == expected and base_prefix != prefix else 1)
' "$expected_environment"
}

safe_remove_environment() {
    local target="$1"
    local target_parent
    local canonical_target

    target_parent="$(cd -- "$(dirname -- "$target")" && pwd -P)"
    canonical_target="$target_parent/$(basename -- "$target")"

    case "$canonical_target" in
        "$project_root/.venv-win"|"$project_root/.venv-linux"|"$project_root/.venv-macos")
            ;;
        *)
            fail "refusing to remove unsafe environment path: $canonical_target"
            ;;
    esac

    rm -rf -- "$canonical_target"
}

create_environment=true
if [[ -e "$environment_dir" ]]; then
    if [[ "$recreate" == false ]]; then
        environment_python="$(find_venv_python "$environment_dir")" \
            || fail "existing environment is invalid; rerun with --recreate"
        validate_venv "$environment_python" "$environment_dir" \
            || fail "existing environment is invalid; rerun with --recreate"
        "$environment_python" "$version_checker" --quiet \
            || fail "existing environment uses unsupported Python; rerun with --recreate"
        create_environment=false
    fi
fi

if [[ "$create_environment" == true ]]; then
    if [[ -z "$python_command" ]]; then
        for candidate in python3 python python3.13 python3.12 python3.11; do
            if command -v "$candidate" >/dev/null 2>&1 \
                && "$candidate" "$version_checker" --quiet; then
                python_command="$candidate"
                break
            fi
        done
        [[ -n "$python_command" ]] \
            || fail "could not find Python >=3.11,<3.14"
    fi

    "$python_command" "$version_checker" --quiet \
        || fail "Python >=3.11,<3.14 is required"
    if [[ -e "$environment_dir" ]]; then
        safe_remove_environment "$environment_dir"
    fi
    "$python_command" -m venv "$environment_dir"
    environment_python="$(find_venv_python "$environment_dir")" \
        || fail "created environment does not contain an executable Python interpreter"
fi

cd -- "$project_root"
"$environment_python" -m pip install --upgrade pip
"$environment_python" -m pip install --editable ".[ai,dev]"
"$environment_python" -c 'import sys; print(sys.executable)'

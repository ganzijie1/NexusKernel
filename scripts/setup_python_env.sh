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

Create or reuse the project Python 3.11 virtual environment and install .[ai,dev].

Options:
  --python PATH  Python 3.11 interpreter used to create the environment.
  --recreate     Safely replace an existing project environment.
  --help         Show this help message.
EOF
}

fail() {
    printf 'Error: %s\n' "$1" >&2
    exit 1
}

python_command="python"
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

case "$(uname -s)" in
    MINGW*|MSYS*|CYGWIN*)
        environment_name=".venv-win"
        environment_python_relative="Scripts/python.exe"
        ;;
    Linux*)
        environment_name=".venv-linux"
        environment_python_relative="bin/python"
        ;;
    *)
        fail "unsupported platform: $(uname -s)"
        ;;
esac

environment_dir="$project_root/$environment_name"
environment_python="$environment_dir/$environment_python_relative"

python_minor_version() {
    "$1" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")'
}

safe_remove_environment() {
    local target="$1"
    local target_parent
    local canonical_target

    target_parent="$(cd -- "$(dirname -- "$target")" && pwd -P)"
    canonical_target="$target_parent/$(basename -- "$target")"

    case "$canonical_target" in
        "$project_root/.venv-win"|"$project_root/.venv-linux")
            ;;
        *)
            fail "refusing to remove unsafe environment path: $canonical_target"
            ;;
    esac

    rm -rf -- "$canonical_target"
}

create_environment=true
if [[ -e "$environment_dir" ]]; then
    if [[ "$recreate" == true ]]; then
        source_version="$(python_minor_version "$python_command")" \
            || fail "cannot run Python interpreter: $python_command"
        [[ "$source_version" == "3.11" ]] \
            || fail "Python 3.11 is required; found $source_version"
        safe_remove_environment "$environment_dir"
    else
        [[ -f "$environment_python" ]] \
            || fail "existing environment is invalid; rerun with --recreate"
        environment_version="$(python_minor_version "$environment_python")" \
            || fail "existing environment is invalid; rerun with --recreate"
        [[ "$environment_version" == "3.11" ]] \
            || fail "existing environment uses Python $environment_version; rerun with --recreate"
        create_environment=false
    fi
fi

if [[ "$create_environment" == true ]]; then
    source_version="${source_version:-$(python_minor_version "$python_command")}" \
        || fail "cannot run Python interpreter: $python_command"
    [[ "$source_version" == "3.11" ]] \
        || fail "Python 3.11 is required; found $source_version"
    "$python_command" -m venv "$environment_dir"
fi

cd -- "$project_root"
"$environment_python" -m pip install --upgrade pip
"$environment_python" -m pip install --editable ".[ai,dev]"
"$environment_python" -c 'import sys; print(sys.executable)'

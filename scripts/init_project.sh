#!/usr/bin/env bash
set -euo pipefail

case "${OSTYPE:-}" in
    msys*|mingw*|cygwin*)
        PATH="/usr/bin:/bin${PATH:+:$PATH}"
        export PATH
        ;;
esac

script_path="${BASH_SOURCE[0]}"
script_dir="${script_path%/*}"
[[ "$script_dir" != "$script_path" ]] || script_dir="."
script_dir="$(cd -- "$script_dir" && pwd -P)"
version_checker="$script_dir/check_python_version.py"

is_supported_python() {
    "$1" "$version_checker" --quiet >/dev/null 2>&1
}

[[ -f "$version_checker" ]] \
    || { printf 'Error: missing Python version checker\n' >&2; exit 1; }

python_command=""
if [[ -n "${PYTHON:-}" ]]; then
    is_supported_python "$PYTHON" \
        || {
            printf 'Error: PYTHON must be an executable Python >=3.11,<3.14 interpreter\n' >&2
            exit 1
        }
    python_command="$PYTHON"
else
    for candidate in python3 python python3.13 python3.12 python3.11; do
        if command -v "$candidate" >/dev/null 2>&1 \
            && is_supported_python "$candidate"; then
            python_command="$candidate"
            break
        fi
    done
fi

if [[ -z "$python_command" ]]; then
    printf 'Error: could not find an executable Python >=3.11,<3.14 interpreter\n' >&2
    exit 1
fi

exec "$python_command" "$script_dir/init_project.py" "$@"

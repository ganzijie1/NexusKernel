#!/usr/bin/env bash
set -euo pipefail

case "${OSTYPE:-}" in
    msys*|mingw*|cygwin*)
        PATH="/usr/bin:/bin${PATH:+:$PATH}"
        export PATH
        ;;
esac

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"

is_python_311() {
    "$1" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 11) else 1)' \
        >/dev/null 2>&1
}

python_command=""
if [[ -n "${PYTHON:-}" ]]; then
    is_python_311 "$PYTHON" \
        || {
            printf 'Error: PYTHON must be an executable Python 3.11 interpreter\n' >&2
            exit 1
        }
    python_command="$PYTHON"
else
    for candidate in python3.11 python3 python; do
        if command -v "$candidate" >/dev/null 2>&1 && is_python_311 "$candidate"; then
            python_command="$candidate"
            break
        fi
    done
fi

if [[ -z "$python_command" ]]; then
    printf 'Error: could not find an executable Python 3.11 interpreter\n' >&2
    exit 1
fi

exec "$python_command" "$script_dir/init_project.py" "$@"

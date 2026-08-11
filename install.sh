#!/usr/bin/env bash
#
# Install claude-link (Linux / macOS).
#
# This script does one thing: find a Python 3.10+ interpreter and hand over to
# link/install.py, which is where the actual installation lives -- one
# implementation for every host application and every platform, with tests
# behind it instead of three copies that drift.
#
# Every option is passed straight through:
#   --agent auto|claude|codex|both   default: whatever is on this machine
#   --skip-hook                      no notification hook
#   --self-test smoke|suite|all|none default: smoke, about a second
#   --dev                            editable install, for working on this
#   --quiet
#
#   ./install.sh --help              the full list

set -euo pipefail

LINK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PYTHON=''
for candidate in python3 python python3.14 python3.13 python3.12 python3.11 python3.10; do
    resolved="$(command -v "$candidate" 2>/dev/null || true)"
    [ -n "$resolved" ] || continue
    if "$resolved" -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3, 10) else 1)' 2>/dev/null; then
        PYTHON="$resolved"
        break
    fi
done

if [ -z "$PYTHON" ]; then
    printf '\033[31mX   No Python 3.10+ found.\033[0m\n' >&2
    echo '    Install it with your package manager, for example:' >&2
    echo '      Debian/Ubuntu:  sudo apt install python3 python3-pip' >&2
    echo '      Fedora:         sudo dnf install python3 python3-pip' >&2
    echo '      macOS:          brew install python@3.12' >&2
    exit 1
fi

cd "$LINK_ROOT"
exec "$PYTHON" -X utf8 -m link.install "$@"

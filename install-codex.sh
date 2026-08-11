#!/usr/bin/env bash
#
# Install claude-link for OpenAI Codex CLI.
#
# Kept because it is the name people have in their notes. There is one
# installer now; this is `./install.sh --agent codex`.

set -euo pipefail
exec "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/install.sh" --agent codex "$@"

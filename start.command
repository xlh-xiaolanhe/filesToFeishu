#!/bin/bash
# macOS Finder double-click entry point; keep startup logic in start.sh.
set -euo pipefail
PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec /bin/bash "$PROJECT_DIR/start.sh" "$@"

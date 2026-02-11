#!/usr/bin/env bash
# Run tests that can be done before opening a PR (no MySQL server required for lint).
# Optional: set MYSQL_SOURCE_DIR to run the component build too.
# Usage: ./scripts/pre-pr.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

FAILED=0

echo "=== 1. Lint (cppcheck) ==="
if command -v cppcheck >/dev/null 2>&1; then
  cppcheck --enable=warning,style,performance \
    --suppress=missingIncludeSystem \
    --error-exitcode=0 \
    -I include \
    src/ || FAILED=1
  cppcheck --enable=warning,style,performance \
    --suppress=missingIncludeSystem \
    --error-exitcode=0 \
    -I include -I src/component_src \
    src/component_src/ || FAILED=1
  echo "Lint OK"
else
  echo "cppcheck not installed; skip (CI will run it). Install with: brew install cppcheck  # or apt install cppcheck"
fi

echo ""
echo "=== 2. Component build (optional: set MYSQL_SOURCE_DIR) ==="
if [ -n "${MYSQL_SOURCE_DIR:-}" ] && [ -f "$MYSQL_SOURCE_DIR/include/mysql/components/component_implementation.h" ]; then
  ./scripts/build-component.sh "mysql-8.4.8" "$MYSQL_SOURCE_DIR" || FAILED=1
  echo "Component build OK"
else
  echo "MYSQL_SOURCE_DIR not set or missing component headers; skip (CI will run build-component)."
fi

echo ""
if [ "$FAILED" -eq 0 ]; then
  echo "Pre-PR checks passed."
else
  echo "Pre-PR checks had failures."
  exit 1
fi

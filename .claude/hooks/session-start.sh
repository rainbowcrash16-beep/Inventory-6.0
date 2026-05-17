#!/bin/bash
# SessionStart hook: install dev deps so `npm run lint` and `npm test` work.
set -euo pipefail

cd "${CLAUDE_PROJECT_DIR:-$(dirname "$0")/../..}"

if [ ! -f package.json ]; then
  echo "session-start: no package.json, skipping."
  exit 0
fi

echo "session-start: installing npm dependencies..."
npm install --no-audit --no-fund --silent

echo "session-start: done."

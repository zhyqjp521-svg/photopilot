#!/bin/bash
set -euo pipefail
REPO_DIR="$HOME/photopilot"
BRANCH="$(git -C "$REPO_DIR" branch --show-current 2>/dev/null || echo main)"
cd "$REPO_DIR"
git pull --rebase origin "$BRANCH" >/dev/null 2>&1 || true
TASK=$(ls tasks/pending/*.md 2>/dev/null | head -n 1 || true)
if [ -z "$TASK" ]; then
  echo "$(date '+%F %T'): no pending tasks"
  exit 0
fi
NAME=$(basename "$TASK")
echo "$(date '+%F %T'): running task $NAME"
codex exec --full-auto "$(cat "$TASK")"
mkdir -p tasks/done
mv "$TASK" tasks/done/
git add -A
git commit -m "codex: complete $NAME" >/dev/null || true
git push origin "$BRANCH" >/dev/null 2>&1 || true
echo "$(date '+%F %T'): done $NAME"

#!/usr/bin/env bash
# Клонировать ветку autonomy-state в $STATE_DIR. Если передан SHA — встать на него (detached):
# недоверенный job обязан работать ровно на том состоянии, которое записал доверенный.
# Токен после клонирования убирается из .git/config — в каталоге состояния его нет.
set -euo pipefail
: "${STATE_DIR:?}" "${GH_TOKEN:?}" "${GITHUB_REPOSITORY:?}"
pin="${1:-}"
git clone -q "https://x-access-token:${GH_TOKEN}@github.com/${GITHUB_REPOSITORY}.git" \
  --branch autonomy-state --single-branch "$STATE_DIR" \
  || { echo "::error::нет ветки autonomy-state: сначала autonomy-watch или цель владельца (AE_V1_RUNBOOK.md)" >&2; exit 1; }
git -C "$STATE_DIR" remote set-url origin "https://github.com/${GITHUB_REPOSITORY}.git"
if [ -n "$pin" ]; then git -C "$STATE_DIR" checkout -q --detach "$pin"; fi
git -C "$STATE_DIR" rev-parse HEAD

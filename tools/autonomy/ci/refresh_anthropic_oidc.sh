#!/usr/bin/env bash
# Обновить одноразовый OIDC-токен GitHub, который Claude Code обменивает на
# короткоживущий токен Claude API (Workload Identity Federation, без API-ключа).
#
# Исполняется ОРКЕСТРАТОРОМ перед каждым вызовом агента и фоновым циклом раз в 4 минуты.
# Токен одноразовый (jti): два процесса, обменявшие один и тот же файл, получат jti_reused.
# Агенту переменные ACTIONS_ID_TOKEN_REQUEST_* не передаются (tools/autonomy/agents.py
# SCRUB_ENV), поэтому сам агент новых токенов выпустить не может.
set -euo pipefail
: "${ACTIONS_ID_TOKEN_REQUEST_URL:?нет ACTIONS_ID_TOKEN_REQUEST_URL: job без id-token: write}"
: "${ACTIONS_ID_TOKEN_REQUEST_TOKEN:?нет ACTIONS_ID_TOKEN_REQUEST_TOKEN}"
: "${ANTHROPIC_IDENTITY_TOKEN_FILE:?нет ANTHROPIC_IDENTITY_TOKEN_FILE}"
case "$ANTHROPIC_IDENTITY_TOKEN_FILE" in
  "${GITHUB_WORKSPACE:-/nonexistent}"/*) echo "токен не должен лежать внутри рабочего каталога" >&2; exit 2 ;;
esac
aud="${ANTHROPIC_OIDC_AUDIENCE:-https://api.anthropic.com}"
tmp="$(mktemp "${ANTHROPIC_IDENTITY_TOKEN_FILE}.XXXXXX")"
curl -fsS --retry 3 -H "Authorization: bearer ${ACTIONS_ID_TOKEN_REQUEST_TOKEN}" \
  "${ACTIONS_ID_TOKEN_REQUEST_URL}&audience=${aud}" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["value"])' > "$tmp"
chmod 600 "$tmp"
mv -f "$tmp" "$ANTHROPIC_IDENTITY_TOKEN_FILE"

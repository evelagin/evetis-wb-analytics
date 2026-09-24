#!/usr/bin/env bash
# Записать $STATE_DIR в ветку autonomy-state ЯВНЫМ refspec и вывести новый SHA последней строкой.
# Только доверенные jobs (без исполнения кода агента или кандидата). Наблюдатель пишет в ту же
# ветку, но в другие каталоги (watch/, objectives/), поэтому конфликт снимается rebase.
set -euo pipefail
: "${STATE_DIR:?}" "${GH_TOKEN:?}" "${GITHUB_REPOSITORY:?}"
msg="${1:?сообщение коммита}"
url="https://x-access-token:${GH_TOKEN}@github.com/${GITHUB_REPOSITORY}.git"
cd "$STATE_DIR"
git checkout -q -B autonomy-state
git add -A
if ! git diff --cached --quiet; then
  git -c user.name=evetis-autonomy -c user.email=autonomy@users.noreply.github.com commit -q -m "$msg"
fi
for attempt in 1 2 3; do
  if git push -q "$url" HEAD:refs/heads/autonomy-state 2>/dev/null; then git rev-parse HEAD; exit 0; fi
  echo "push отклонён (попытка $attempt): rebase на свежую ветку состояния" >&2
  git pull -q --rebase "$url" autonomy-state >&2
done
echo "::error::не удалось записать состояние после 3 попыток" >&2
exit 1

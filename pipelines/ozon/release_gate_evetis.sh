#!/usr/bin/env bash
# Release gates EVETIS Ozon runtime (ветка release/evetis-ozon-runtime-08dfb88-promo). Только чтение.
#   G1  база = развёрнутый 08dfb88 (образ 24e3c6d6, Cloud Build 7e1fe4e2; исходник сборки = git archive 08dfb88)
#   G2  в каталоге образа (pipelines/ozon/runtime) относительно базы изменён только promo.py
#   G3  в каталоге образа нет ни одного модуля мультиарендности (файлы runtime = файлы базы)
#   G4  в promo.py нет мутирующих путей и источника ПДн (закрытый список чтения)
#   G5  офлайн-тесты Ozon проходят
#   G6  (после сборки, по ACK) архив исходника Cloud Build побайтно = git archive HEAD pipelines/ozon/runtime
# Использование: pipelines/ozon/release_gate_evetis.sh [cloudbuild-source-gs-uri]
set -euo pipefail
BASE=08dfb8877e0e3c0fd3a0c5f658cc98a41ada4c48
RT=pipelines/ozon/runtime
cd "$(git rev-parse --show-toplevel)"
fail() { echo "GATE FAIL: $*" >&2; exit 1; }

git merge-base --is-ancestor "$BASE" HEAD || fail "G1: HEAD не наследует $BASE"
echo "G1 OK  база $BASE"

changed=$(git diff --name-only "$BASE" HEAD -- "$RT")
[ "$changed" = "$RT/promo.py" ] || fail "G2: в каталоге образа изменено не только promo.py: $changed"
echo "G2 OK  в образе изменён только $RT/promo.py"

[ "$(git ls-tree --name-only HEAD "$RT/")" = "$(git ls-tree --name-only "$BASE" "$RT/")" ] || fail "G3: состав файлов образа отличается от базы"
echo "G3 OK  состав файлов образа = база ($(git ls-tree --name-only HEAD "$RT/" | wc -l | tr -d ' ') файлов, модулей арендатора нет)"

for p in /v1/actions/products/activate /v1/actions/products/deactivate /v1/actions/products/update \
         /v2/actions/products/deactivate /v1/actions/auto-add/products/update /v1/actions/auto-add/products/delete \
         /v2/actions/auto-add/products/update /v2/actions/auto-add/products/delete /v1/actions/discounts-task/list; do
  ! grep -qF "\"$p\"" "$RT/promo.py" || fail "G4: в promo.py есть запрещённый путь $p"
done
echo "G4 OK  мутирующих путей и источника ПДн в promo.py нет"

python3 -m pytest -q pipelines/ozon/tests >/tmp/evetis_gate_pytest.$$ 2>&1 || { tail -20 /tmp/evetis_gate_pytest.$$; fail "G5: тесты"; }
echo "G5 OK  $(tail -1 /tmp/evetis_gate_pytest.$$)"; rm -f /tmp/evetis_gate_pytest.$$

if [ "${1:-}" != "" ]; then
  tmp=$(mktemp -d); mkdir "$tmp/cb" "$tmp/git"
  gcloud storage cp "$1" "$tmp/src.tgz" >/dev/null
  tar -xzf "$tmp/src.tgz" -C "$tmp/cb"; git archive HEAD "$RT" | tar -x -C "$tmp/git"
  diff -r "$tmp/cb" "$tmp/git/$RT" >/dev/null || fail "G6: исходник Cloud Build ≠ git archive HEAD"
  echo "G6 OK  исходник Cloud Build побайтно = git archive $(git rev-parse --short HEAD) $RT"
else
  echo "G6 --  не проверялся (нет URI архива сборки; выполняется после сборки по ACK)"
fi

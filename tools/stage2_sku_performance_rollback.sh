#!/usr/bin/env bash
# ============================================================================
# STAGE 2 — SKU PERFORMANCE · ОТКАТ
#
# Что откатывает: dashboard 3 «EVETIS · WB SKU Performance», 16 карточек 56–71
# и коллекцию 7 «02 SKU Performance».
#
# Карточки 70 «Выкупы по дням» и 71 «Период и покрытие данных SKU» добавлены
# в Stage 2.0.1; карточки 64, 65, 67, 68, 69 в Stage 2.0.1 переопределены.
#
# Объектов BigQuery Stage 2 НЕ создавал — в BigQuery откатывать нечего.
# Executive (dashboard 2, cards 40–55) Stage 2 НЕ менял: восстановление
# не требуется. Бэкапы лежат рядом на случай ручной сверки.
#
# Карточки и дашборд АРХИВИРУЮТСЯ, а не удаляются (правило проекта:
# существующие карточки физически не удалять). Архив обратим из UI Metabase.
#
# Запуск:  bash tools/stage2_sku_performance_rollback.sh [--dry-run]
# ============================================================================
set -euo pipefail

PROFILE="${MB_PROFILE:-evetis-dev}"
DASHBOARD_ID=3
COLLECTION_ID=7
CARDS=(56 57 58 59 60 61 62 63 64 65 66 67 68 69 70 71)

DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

run() {
  if [[ $DRY_RUN -eq 1 ]]; then
    echo "DRY-RUN: $*"
  else
    "$@"
  fi
}

echo "== Stage 2 rollback (профиль: $PROFILE) =="

echo "-- 0. Проверка, что Executive не затронут --------------------------------"
mb --profile "$PROFILE" dashboard get 2 --format json --max-bytes 0 \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["name"]=="EVETIS · WB Executive" and not d["archived"], "Executive изменён — остановись и разберись"; print("   Executive на месте, не архивирован")'

echo "-- 1. Архивирование dashboard $DASHBOARD_ID ------------------------------"
run mb --profile "$PROFILE" dashboard archive "$DASHBOARD_ID"

echo "-- 2. Архивирование карточек ${CARDS[*]} ---------------------------------"
for c in "${CARDS[@]}"; do
  echo "   card $c"
  run mb --profile "$PROFILE" card archive "$c"
done

echo "-- 3. Архивирование коллекции $COLLECTION_ID -----------------------------"
run mb --profile "$PROFILE" collection archive "$COLLECTION_ID"

echo "-- 4. Контроль ----------------------------------------------------------"
if [[ $DRY_RUN -eq 0 ]]; then
  mb --profile "$PROFILE" dashboard get 2 --format json --max-bytes 0 \
    | python3 -c 'import json,sys; d=json.load(sys.stdin); print("   Executive archived =", d["archived"])'
  for c in 42 43 44 45 46 47 53 54 55; do
    mb --profile "$PROFILE" card get "$c" --format json --max-bytes 0 \
      | python3 -c 'import json,sys; d=json.load(sys.stdin); print("   card",d["id"],d["name"],"archived =",d["archived"])'
  done
fi

echo "== Откат завершён. Объекты BigQuery не затрагивались. =="

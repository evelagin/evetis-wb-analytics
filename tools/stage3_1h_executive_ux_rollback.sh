#!/usr/bin/env bash
# ============================================================================
# STAGE 3.1H — ОТКАТ UX / SEMANTIC CLEANUP DASHBOARD 2 (EVETIS · WB Executive)
# Дата: 2026-08-28.
#
# 🔴 BIGQUERY НЕ ЗАТРОНУТ. Stage 3.1H не создал, не удалил и не переопределил
#    ни одного объекта BigQuery. wb_mart = 42, wb_raw = 56, evetis_ref = 4 —
#    как до этапа. Экономика (Stage 3.1C–3.1G) не менялась. Поэтому здесь нет
#    ни одного DDL: откат целиком живёт в Metabase.
#
# 🔴 ЧТО ИМЕННО ОТКАТЫВАЕТСЯ:
#      1. раскладка dashboard 2 (высота 47 -> прежние 52);
#      2. определения 11 изменённых карточек: 44, 46, 49, 50, 51, 52, 53,
#         72, 73, 75, 76 — формат чисел, названия, описания, а у 50 и 51
#         ещё SQL и display;
#      3. три созданные карточки 77, 78, 79 — архивируются.
#
#    Снимки «ДО» сняты перед записью и лежат ВНЕ репозитория:
#      ~/.claude/evetis-rollback/stage3_1h/card-<id>.json
#      ~/.claude/evetis-rollback/stage3_1h/dashboard-2.json
#
# 🔴 ПОРЯДОК ИМЕЕТ ЗНАЧЕНИЕ. Сначала восстанавливается ДАШБОРД (он перестаёт
#    ссылаться на карточки 77–79), затем определения карточек, и только потом
#    архивируются новые карточки. Обратный порядок оставит на дашборде
#    ссылки на архивные карточки.
#
# 🔴 ЧЕГО ОТКАТ НЕ ТРОГАЕТ: dashboard 3 (SKU Performance) и карточки 56–71,
#    карточки 40–43, 45, 47, 48, 54, 55, 74 (их определения этап не менял),
#    коллекции, фильтр периода и его parameter_mappings, любые объекты
#    BigQuery. Контрольные числа периода 27.07–23.08.2026 от отката не
#    меняются — они не зависят от представления.
#
# --dry-run печатает план и проверяет наличие всех снимков, не выполняя
#    ни одной записи; возвращает exit 0.
# ============================================================================
set -euo pipefail

PROFILE="${MB_PROFILE:-evetis-dev}"
SNAP_DIR="${STAGE31H_SNAPSHOT:-$HOME/.claude/evetis-rollback/stage3_1h}"
DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

MODIFIED_CARDS=(44 46 49 50 51 52 53 72 73 75 76)
CREATED_CARDS=(77 78 79)

echo "=== Stage 3.1H rollback ==="
echo "профиль : ${PROFILE}"
echo "снимки  : ${SNAP_DIR}"
echo "mode    : $([[ ${DRY_RUN} -eq 1 ]] && echo 'DRY-RUN (ни одной записи)' || echo 'EXECUTE')"
echo
echo "--- ПРЕДУСЛОВИЯ ---"
echo "  1. BigQuery не затронут — DDL в этом откате нет."
echo "  2. Снимки 'ДО' доступны для дашборда 2 и карточек ${MODIFIED_CARDS[*]}."
echo "  3. Карточки ${CREATED_CARDS[*]} созданы Stage 3.1H и будут заархивированы."
echo

command -v mb >/dev/null 2>&1 || { echo "ОШИБКА: не найден 'mb' в PATH" >&2; exit 1; }
command -v jq >/dev/null 2>&1 || { echo "ОШИБКА: не найден 'jq' в PATH" >&2; exit 1; }

# ── Fail-closed: без полного комплекта снимков откат не начинается ──────────
MISSING=0
for f in "dashboard-2" $(printf 'card-%s ' "${MODIFIED_CARDS[@]}"); do
  if [[ ! -s "${SNAP_DIR}/${f}.json" ]]; then
    echo "ОШИБКА: нет снимка ${SNAP_DIR}/${f}.json" >&2
    MISSING=1
  fi
done
if [[ ${MISSING} -ne 0 ]]; then
  echo >&2
  echo "Без полного комплекта снимков откат невозможен: этап переопределил" >&2
  echo "существующие объекты, вернуть их можно только из снимка или из" >&2
  echo "metabase/ соответствующего коммита (be29e4d — состояние до 3.1H)." >&2
  exit 1
fi
echo "все снимки на месте: $(( ${#MODIFIED_CARDS[@]} + 1 )) файл(ов)"
echo

echo "--- ПЛАН ---"
echo "  шаг 1: dashboard 2 <- ${SNAP_DIR}/dashboard-2.json (28 dashcards, высота 52)"
for id in "${MODIFIED_CARDS[@]}"; do
  echo "  шаг 2: card ${id} <- ${SNAP_DIR}/card-${id}.json"
done
for id in "${CREATED_CARDS[@]}"; do
  echo "  шаг 3: card ${id} -> archive"
done
echo

if [[ ${DRY_RUN} -eq 1 ]]; then
  echo "DRY-RUN: ничего не записано."
  exit 0
fi

# ── Шаг 1. Дашборд: раскладка и состав dashcards ────────────────────────────
echo "шаг 1: восстановление dashboard 2..."
jq '{dashcards: [ .dashcards[] | {
      id, card_id, dashboard_tab_id, row, col, size_x, size_y,
      visualization_settings, parameter_mappings,
      series: (.series // [])
    } ]}' "${SNAP_DIR}/dashboard-2.json" \
  | mb dashboard update 2 --profile "${PROFILE}" --json --max-bytes 0 >/dev/null
echo "  dashboard 2 восстановлен"

# ── Шаг 2. Определения изменённых карточек ──────────────────────────────────
for id in "${MODIFIED_CARDS[@]}"; do
  echo "шаг 2: восстановление card ${id}..."
  jq '{name, description, display, collection_id, dataset_query, visualization_settings}
      | .dataset_query.stages[0]."template-tags" as $t
      | if ($t | type) == "array"
        then .dataset_query.stages[0]."template-tags" = ( $t | map({key: .name, value: .}) | from_entries )
        else . end' "${SNAP_DIR}/card-${id}.json" \
    | mb card update "${id}" --profile "${PROFILE}" --json --max-bytes 0 >/dev/null
  echo "  card ${id} восстановлена"
done

# ── Шаг 3. Архивация созданных карточек ─────────────────────────────────────
for id in "${CREATED_CARDS[@]}"; do
  echo "шаг 3: архивация card ${id}..."
  mb card archive "${id}" --profile "${PROFILE}" --json --max-bytes 0 >/dev/null
  echo "  card ${id} заархивирована"
done

echo
echo "=== ОТКАТ ЗАВЕРШЁН ==="
echo "Проверьте dashboard 2 на периоде 27.07.2026–23.08.2026:"
echo "  586 / 56 / 513 · 363 159 ₽ · 110 752 ₽ · 93 151 ₽ · −26 183 ₽ · 110 102 ₽"
echo "Эти числа не должны отличаться ни до, ни после отката."
echo
echo "Снимок в репозитории верните отдельно:"
echo "  git checkout be29e4d -- metabase/"
echo "  (и удалите metabase/cards/card-7[789]-*.json — их до 3.1H не было)"

#!/usr/bin/env bash
# =====================================================================================
# EVETIS OWNER CONTROL TOWER — PHASE 1.2 — ОТКАТ К СОСТОЯНИЮ PHASE 1.1 (коммит 7736eb0)
# =====================================================================================
# Phase 1.2 не добавляла таблиц, процедур и инфраструктуры — только текстовые/структурные
# правки витрин и карточек Metabase. Откат = вернуть Phase 1.1-определения шести витрин и
# перестроить карточки/дашборды Metabase скриптом Phase 1.1.
#
#   BigQuery  — CREATE OR REPLACE из git: sql/control_tower/ct_04b_views_owner.sql (V_CT_ATTENTION,
#               V_CT_ACTION_CANDIDATES, V_CT_ACTION_QUEUE, V_CT_OWNER_HOME) и ct_07_phase11_objects.sql
#               (V_CT_SKU_CONTROL, V_CT_DAILY_BRIEF_LINES; MERGE в CT_CONFIG идемпотентен).
#               Данные CT_* не трогаются. Журналы сохраняются.
#   Metabase  — ручной шаг: tools/metabase_ct_phase12_rollback.js (см. инструкцию ниже).
#
# Использование:
#   tools/ct_phase12_rollback.sh --dry-run
#   tools/ct_phase12_rollback.sh --bigquery --confirm
# =====================================================================================
set -euo pipefail
PROJECT="${BQ_PROJECT:-project-fa311fc0-4d87-4781-986}"
BASE_COMMIT="${CT_PHASE11_COMMIT:-7736eb0}"
DRY_RUN=1; DO_BQ=0; DO_MB=0
for arg in "$@"; do
  case "$arg" in
    --dry-run)  DRY_RUN=1 ;;
    --confirm)  DRY_RUN=0 ;;
    --bigquery) DO_BQ=1 ;;
    --metabase) DO_MB=1 ;;
    --all)      DO_BQ=1; DO_MB=1 ;;
    *) echo "Неизвестный аргумент: $arg" >&2; exit 2 ;;
  esac
done
if [[ $DO_BQ -eq 0 && $DO_MB -eq 0 ]]; then DO_BQ=1; DO_MB=1; fi
bq_file() {
  if [[ $DRY_RUN -eq 1 ]]; then echo "  [dry-run] bq query < git show ${BASE_COMMIT}:$1   # $2"; else git show "${BASE_COMMIT}:$1" | bq query --use_legacy_sql=false --project_id="$PROJECT"; fi
}
bq_run() {
  if [[ $DRY_RUN -eq 1 ]]; then echo "  [dry-run] bq query: ${1:0:110}..."; else bq query --use_legacy_sql=false --project_id="$PROJECT" "$1"; fi
}
echo "=== CONTROL TOWER PHASE 1.2 ROLLBACK (→ Phase 1.1, ${BASE_COMMIT}) ==="
[[ $DRY_RUN -eq 1 ]] && echo "РЕЖИМ: dry-run. Для реального отката добавьте --confirm."
echo
if [[ $DO_BQ -eq 1 ]]; then
  echo "--- BigQuery: возврат Phase 1.1-определений витрин ---"
  bq_file sql/control_tower/ct_04b_views_owner.sql   "V_CT_ATTENTION / V_CT_ACTION_CANDIDATES / V_CT_ACTION_QUEUE / V_CT_OWNER_HOME (Phase 1.1)"
  bq_file sql/control_tower/ct_07_phase11_objects.sql "V_CT_SKU_CONTROL / V_CT_DAILY_BRIEF_LINES / V_CT_DAILY_BRIEF (Phase 1.1; CT_CONFIG MERGE идемпотентен)"
  echo "--- BigQuery: проверка ---"
  bq_run "SELECT COUNT(*) AS owner_home_rows FROM \`${PROJECT}.wb_mart.V_CT_OWNER_HOME\`"
  echo "    затем: bq query < sql/control_tower/ct_phase11_validation.sql (ожидается 10/10; T28b в Phase 1.1-редакции)"
  echo
fi
if [[ $DO_MB -eq 1 ]]; then
  echo "--- Metabase (ручной шаг, same-origin) ---"
  echo "  1. Открыть http://localhost:3000 под владельцем → DevTools → Console"
  echo "  2. Вставить tools/metabase_ct_phase11_build.js (определяет window.ctBuild Phase 1.1)"
  echo "  3. Вставить tools/metabase_ct_phase12_rollback.js, затем: await ctRollback12.all()"
  echo "     — карточки Phase 1.1 (по ключу в описании) возвращаются к определениям Phase 1.1,"
  echo "       раскладки дашбордов 5/6/8/9 — к Phase 1.1, карточки только Phase 1.2 архивируются."
  echo
fi
echo "Готово."

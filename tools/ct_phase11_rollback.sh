#!/usr/bin/env bash
# =====================================================================================
# EVETIS OWNER CONTROL TOWER — PHASE 1.1 — ОТКАТ К СОСТОЯНИЮ PHASE 1 (коммит 075f45a)
# =====================================================================================
# Что делает:
#   BigQuery  — удаляет объекты, добавленные Phase 1.1 (CT_CONFIG, V_CT_REFRESH_STATUS,
#               V_CT_SKU_CONTROL, V_CT_DAILY_BRIEF_LINES, V_CT_DAILY_BRIEF) и возвращает
#               Phase 1-определения V_CT_ACTION_QUEUE, V_CT_OWNER_HOME и sp_ct_refresh_daily
#               (берутся из git: `git show 075f45a:sql/control_tower/...`).
#   Metabase  — печатает инструкцию: выполнить tools/metabase_ct_phase11_rollback.js в консоли
#               браузера на localhost:3000 (восстанавливает карточки 97–116 и раскладку
#               дашбордов 5/6, архивирует карточки и дашборды Phase 1.1).
#   Terraform — печатает инструкцию: удалить infra/terraform/ct_refresh.tf и ключ ct_refresh
#               в iam.tf, затем workflow `infra` action=apply (удалит SA, IAM и scheduler).
# Чего НЕ делает: не трогает объекты Phase 1 (CT_* таблицы с данными, остальные V_CT_*),
#               production-слои, загрузчики. Журналы CT_REFRESH_LOG / CT_ACTION_STATUS_LOG
#               сохраняются как есть.
#
# Использование:
#   tools/ct_phase11_rollback.sh --dry-run          # показать план
#   tools/ct_phase11_rollback.sh --bigquery --confirm
#   tools/ct_phase11_rollback.sh --all --confirm
# =====================================================================================
set -euo pipefail

PROJECT="${BQ_PROJECT:-project-fa311fc0-4d87-4781-986}"
BASE_COMMIT="${CT_PHASE1_COMMIT:-075f45a}"
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

bq_run() {
  if [[ $DRY_RUN -eq 1 ]]; then echo "  [dry-run] bq query: ${1:0:110}..."; else bq query --use_legacy_sql=false --project_id="$PROJECT" "$1"; fi
}
bq_file() {
  # $1 = git path, $2 = описание. Применяет Phase 1 версию файла целиком (CREATE OR REPLACE идемпотентен).
  if [[ $DRY_RUN -eq 1 ]]; then echo "  [dry-run] bq query < git show ${BASE_COMMIT}:$1   # $2"; else git show "${BASE_COMMIT}:$1" | bq query --use_legacy_sql=false --project_id="$PROJECT"; fi
}

echo "=== CONTROL TOWER PHASE 1.1 ROLLBACK (→ Phase 1, ${BASE_COMMIT}) ==="
[[ $DRY_RUN -eq 1 ]] && echo "РЕЖИМ: dry-run. Для реального отката добавьте --confirm."
echo

if [[ $DO_BQ -eq 1 ]]; then
  echo "--- BigQuery: удаление объектов Phase 1.1 ---"
  for v in V_CT_DAILY_BRIEF V_CT_DAILY_BRIEF_LINES V_CT_SKU_CONTROL; do
    bq_run "DROP VIEW IF EXISTS \`${PROJECT}.wb_mart.${v}\`"
  done
  echo "--- BigQuery: возврат Phase 1-определений (V_CT_ACTION_QUEUE, V_CT_OWNER_HOME, sp_ct_*) ---"
  bq_file sql/control_tower/ct_04b_views_owner.sql "владельческие витрины Phase 1 (Owner Home без ссылки на V_CT_REFRESH_STATUS)"
  bq_file sql/control_tower/ct_05_procedures.sql   "процедуры Phase 1 (TRUNCATE-версия sp_ct_refresh_daily)"
  echo "--- BigQuery: удаление оставшихся объектов Phase 1.1 ---"
  bq_run "DROP VIEW IF EXISTS \`${PROJECT}.wb_mart.V_CT_REFRESH_STATUS\`"
  bq_run "DROP TABLE IF EXISTS \`${PROJECT}.evetis_ref.CT_CONFIG\`"
  echo "--- BigQuery: проверка, что Phase 1 жива ---"
  bq_run "SELECT COUNT(*) AS owner_home_rows FROM \`${PROJECT}.wb_mart.V_CT_OWNER_HOME\`"
  echo
fi

if [[ $DO_MB -eq 1 ]]; then
  echo "--- Metabase (ручной шаг, same-origin) ---"
  echo "  1. Открыть http://localhost:3000 под владельцем → DevTools → Console"
  echo "  2. Вставить содержимое tools/metabase_ct_phase11_rollback.js, затем: await ctRollback.all()"
  echo "     Восстанавливает карточки 97–116 и раскладку дашбордов 5/6 (width=fixed), архивирует"
  echo "     карточки Phase 1.1 (по описанию «EVETIS Control Tower Phase 1.1») и дашборды «ДЕТАЛИ», «DAILY BRIEF»."
  echo
fi

echo "--- Terraform / Cloud Scheduler (штатный путь) ---"
echo "  git rm infra/terraform/ct_refresh.tf; убрать ключ ct_refresh из local.terraform_actas_targets (iam.tf);"
echo "  workflow infra → action=plan (ожидается 14 to destroy) → action=apply с approval."
echo
echo "Готово."
